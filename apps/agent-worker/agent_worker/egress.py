"""The egress probe: when a fresh instance's path to the forge really opens (#721 (a)).

MEASURED 2026-10-06 (#721, from 60 `clone_timed` events and 422 clones over
three days): 96 % of a clone's time was ONE TCP connect to GitHub, stalling
4 to 71 s on discrete values that double like Linux's SYN retransmission
backoff (1, 2, 4, 8, 16, 32 s). The likely reading -- an inference, not a
proof -- is that SYNs are dropped until the new instance's internet egress
path opens, and git's connect is then answered only on its NEXT retransmit,
up to 32 s after the path opened.

So at process start a background thread opens a FRESH connection to the
forge every ~1 s, `socket.create_connection(timeout=1)`, until one succeeds or
`EGRESS_PROBE_CAP_SECONDS` have passed. A fresh socket each time is the point:
it never inherits a retransmit backoff. The clone (`gitops.shallow_clone`,
`gitops.clone_at_commit`) waits for the clone host's success, bounded, and
then clones; if the probe failed or ran out, the clone runs exactly as it did
before the probe existed. Nothing else at startup waits for it.

What it measured -- `egress_ready_seconds` (process start to the first
connect that succeeded) and `probe_attempts` per target -- is written once per
attempt as the `egress_ready` startup mark, next to `clone_timed`.

The probe opens a TCP connection and closes it. It sends no byte, so no
request, header or credential is ever on it.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlparse

#: Always probed: the forge every SwarmCloud step's clone, issue fetch and
#: push talks to today. The clone host is probed as well when it is elsewhere.
DEFAULT_TARGET: tuple[str, int] = ("github.com", 443)

#: How long the probe keeps trying, from process start. 80 s, because the
#: longest connect stall #721 measured was 78 s (69-78 s mode, 18 % of 422
#: clones over three days) and 71 s in the 60 traced clones: a cap below that
#: would give up on paths that were about to open. Past it the probe stops
#: and the clone runs exactly as it did before the probe existed.
EGRESS_PROBE_CAP_SECONDS = 80.0

#: The gap between the starts of two rounds. ~1 s, the owner's figure in
#: #721 (a): it is the most a probe can trail the path opening, against the
#: up-to-32 s a single connect's SYN backoff can trail it.
EGRESS_PROBE_INTERVAL_SECONDS = 1.0

#: Each connect's own timeout. 1 s: a SYN the path drops is retransmitted
#: after 1 s, so a longer timeout would let the kernel's backoff back in.
EGRESS_CONNECT_TIMEOUT_SECONDS = 1.0

#: How long `stop` waits for the thread. One connect timeout and a margin:
#: the thread checks its stop flag between connects, never inside one.
EGRESS_STOP_JOIN_SECONDS = EGRESS_CONNECT_TIMEOUT_SECONDS + 1.0

_DEFAULT_PORTS = {"https": 443, "http": 80, "ssh": 22, "git": 9418}

Connect = Callable[..., Any]


def target_of(url: str | None) -> tuple[str, int] | None:
    """The (host, port) a clone of `url` connects to, or None (a local path, `file://`)."""
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if text.startswith("git@") and ":" in text and "://" not in text:
        # scp-style, as `gitops.validate_repository_url` rewrites it.
        host = text[4:].partition(":")[0]
        return (host.lower(), 22) if host else None
    try:
        parsed = urlparse(text)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    default = _DEFAULT_PORTS.get(parsed.scheme)
    if not host or default is None:
        return None
    return host.lower(), port or default


def probe_targets(repository_url: str | None) -> list[tuple[str, int]]:
    """github.com:443, and the clone host when the repository is elsewhere.

    github.com always, repository or not: the worker starts the probe at the
    top of `run`, before it has read the task document, and the dispatcher
    sets no REPOSITORY_URL -- the repository is on the signed task document
    (`Worker._maybe_clone`). The clone host joins later through
    `EgressProbe.add_target` once that document is verified.
    """
    clone = target_of(repository_url)
    if clone is None or clone == DEFAULT_TARGET:
        return [DEFAULT_TARGET]
    return [DEFAULT_TARGET, clone]


class EgressProbe:
    """A background thread that connects to each target until it answers.

    `start` returns at once. `add_target` probes one more host from then on
    (the clone host, known only once the task document is read). `wait(target,
    timeout)` blocks until that target answered, the probe ended, or
    `timeout`; it never raises. `stop` ends the thread and joins it.
    `result()` is what the `egress_ready` mark carries.
    """

    def __init__(
        self,
        targets: Iterable[tuple[str, int]],
        *,
        cap_seconds: float = EGRESS_PROBE_CAP_SECONDS,
        interval_seconds: float = EGRESS_PROBE_INTERVAL_SECONDS,
        connect_timeout_seconds: float = EGRESS_CONNECT_TIMEOUT_SECONDS,
        connect: Connect | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.targets: list[tuple[str, int]] = list(dict.fromkeys(targets))
        self.cap_seconds = cap_seconds
        self.interval_seconds = interval_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        # Looked up at call time when None, so a test may stand in for it.
        self._connect = connect
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._answered = {target: threading.Event() for target in self.targets}
        self._attempts = {target: 0 for target in self.targets}
        self._ready_seconds: dict[tuple[str, int], float] = {}
        self._last_error: dict[tuple[str, int], str] = {}
        self._started_at: float | None = None
        self._ended: str | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = self._clock()
        if not self.targets:
            with self._lock:
                self._ended = "no_targets"
            self._finished.set()
            return
        self._thread = threading.Thread(target=self._run, name="egress-probe", daemon=True)
        self._thread.start()

    def add_target(self, target: tuple[str, int] | None) -> None:
        """Probe `target` too, from now on. Thread-safe; never raises.

        The running loop picks it up on its next round. A probe whose thread
        already ended because every target had answered is started again for
        it, within the same cap counted from the same start; one that ended
        on its cap or was stopped is not -- `wait` then returns False at once
        and the clone runs as before.
        """
        if target is None:
            return
        with self._lock:
            if target in self._answered:
                return
            self.targets.append(target)
            self._answered[target] = threading.Event()
            self._attempts[target] = 0
            thread = self._thread
            # "ready" is written under this lock as the thread's last act
            # before it returns, so the thread is done with every target.
            restart = (
                thread is not None
                and self._ended == "ready"
                and not self._stop.is_set()
            )
        if restart:
            # Joined first, so its last `_finished.set()` cannot land after
            # the clear below.
            self._join_quietly(thread)
            with self._lock:
                self._ended = None
            self._finished.clear()
            self._thread = threading.Thread(
                target=self._run, name="egress-probe", daemon=True
            )
            self._thread.start()

    @staticmethod
    def _join_quietly(thread: threading.Thread | None) -> None:
        if thread is not None and thread is not threading.current_thread():
            thread.join(EGRESS_STOP_JOIN_SECONDS)

    def stop(self) -> None:
        """End the probe and join its thread. Idempotent; never raises."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(EGRESS_STOP_JOIN_SECONDS)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def covers(self, target: tuple[str, int] | None) -> bool:
        return target is not None and target in self._answered

    def wait(self, target: tuple[str, int] | None, timeout: float) -> bool:
        """True once `target` answered; False if it is not probed, the probe
        ended without it, or `timeout` passed first."""
        if target is None or target not in self._answered or self._thread is None:
            return False
        answered = self._answered[target]
        deadline = time.monotonic() + max(0.0, timeout)
        while not answered.is_set() and not self._finished.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            answered.wait(min(left, 0.2))
        return answered.is_set()

    def result(self) -> dict[str, Any]:
        """Numbers only: per target, when it answered and after how many tries."""
        with self._lock:
            targets = []
            for host, port in list(self.targets):
                entry: dict[str, Any] = {
                    "host": host,
                    "port": port,
                    "ready": (host, port) in self._ready_seconds,
                    "egress_ready_seconds": self._ready_seconds.get((host, port)),
                    "probe_attempts": self._attempts[(host, port)],
                }
                error = self._last_error.get((host, port))
                if error and not entry["ready"]:
                    entry["last_error"] = error
                targets.append(entry)
            ended = self._ended or ("running" if self.running else "stopped")
        first = targets[0] if targets else {}
        return {
            # The headline is github.com's (#721); every target is listed.
            "egress_ready_seconds": first.get("egress_ready_seconds"),
            "probe_attempts": first.get("probe_attempts", 0),
            "ended": ended,
            "cap_seconds": self.cap_seconds,
            "targets": targets,
        }

    # ------------------------------------------------------------------
    def _run(self) -> None:
        assert self._started_at is not None
        start = self._started_at
        connect = self._connect or socket.create_connection
        try:
            while not self._stop.is_set():
                with self._lock:
                    pending = [t for t in self.targets if not self._answered[t].is_set()]
                    if not pending:
                        # Under the lock, so an `add_target` racing this sees
                        # either the target picked up or the probe "ready".
                        self._ended = "ready"
                        return
                round_start = self._clock()
                if round_start - start >= self.cap_seconds:
                    self._set_ended("cap")
                    return
                for target in pending:
                    if self._stop.is_set():
                        break
                    self._try(connect, target, start)
                left = self.interval_seconds - (self._clock() - round_start)
                with self._lock:
                    waiting = any(not self._answered[t].is_set() for t in self.targets)
                if left > 0 and waiting:
                    self._stop.wait(left)
            # Stopped after every target had answered is still "ready".
            with self._lock:
                if self._ended is None:
                    done = all(self._answered[t].is_set() for t in self.targets)
                    self._ended = "ready" if done else "stopped"
        except Exception as exc:  # a measurement never takes the worker down
            self._set_ended(f"error: {type(exc).__name__}")
        finally:
            self._finished.set()

    def _set_ended(self, reason: str) -> None:
        with self._lock:
            self._ended = reason

    def _try(self, connect: Connect, target: tuple[str, int], start: float) -> None:
        with self._lock:
            self._attempts[target] += 1
        try:
            conn = connect(target, timeout=self.connect_timeout_seconds)
        except OSError as exc:
            with self._lock:
                self._last_error[target] = type(exc).__name__
            return
        try:
            conn.close()
        except OSError:
            pass
        with self._lock:
            self._ready_seconds[target] = round(max(0.0, self._clock() - start), 3)
        self._answered[target].set()
