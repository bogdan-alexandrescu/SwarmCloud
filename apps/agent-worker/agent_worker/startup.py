"""A worker that cannot start says so, and says where, within seconds.

WHY THIS EXISTS. On 2026-09-24 a GKE worker (task_1f699ef4cdbb4cc79c16, attempt
att_78e34e7537de487ba71d) ran for 390 seconds and printed nothing. The tenant's
egress NetworkPolicy dropped every DNS query it made, and each layer between the
process and the log hid that:

  * the entrypoint printed nothing before its first Firestore call, and the
    first line the lifecycle wrote came after a Firestore write;
  * google-auth and grpc attach NullHandlers, and nothing configured the root
    logger, so their warnings went nowhere;
  * the Firestore read behind `validate_generation` retries UNAVAILABLE for up
    to 300 seconds and logs each retry at DEBUG;
  * SIGTERM only set a flag, and nothing read the flag until a runner child
    existed. The reconciler's SIGTERM at 390 s was ignored, and 120 s later the
    kubelet's SIGKILL ended the process with exit 137. It had not written a
    single byte.

This module holds what the entrypoint and the lifecycle share to prevent that:

  * `Phases`: one flushed JSON line per startup phase, so the last line of a
    silent pod names the phase it was stuck in;
  * `dns_preflight`: before any client is built, resolve the names the first
    Firestore call needs, under a 10 s budget. A failure is one structured
    error naming the hosts, then exit 78. It is not 390 s of nothing;
  * `route_signals`: SIGTERM and SIGINT go to one handler, and a SIGTERM also
    dumps every thread's stack to stderr (`faulthandler`), so a hang inside
    grpc shows where it is;
  * the Firestore budgets that `__main__` gives the control plane for every
    call it makes before the runner (`ControlPlane.startup_budget`).

Nothing here imports google-cloud at module level. Unit tests import it with no
credentials and no grpc, like every other module in the package.
"""

from __future__ import annotations

import faulthandler
import io
import os
import platform
import signal
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .logs import StructuredLogger

#: The exit code for a worker that a SIGTERM (or SIGINT) stopped BEFORE its
#: runner child existed. It is 128 + SIGTERM, which is also what the process
#: would have returned had Python's default disposition killed it, and it is
#: distinct from every deliberate code in `errors.ExitCode`. This exit writes
#: neither the task nor the lease (see `Worker._exit_interrupted_before_runner`).
EXIT_INTERRUPTED = 128 + int(signal.SIGTERM)

#: How long the DNS preflight may take, in total, for every host at once.
#:
#: Ten seconds. A resolver that answers takes milliseconds, even when it
#: walks a five-entry search list (GKE sets `ndots:5`). One whose packets are
#: dropped never answers, and glibc waits out its per-nameserver timeout on
#: every try. The incident's timeline bounds google-auth's five project-id
#: attempts at about 262 s in total, about 52 s each. That figure is inferred
#: from the exit code and the SIGTERM time, not timed directly. Ten seconds is
#: orders of magnitude above a working resolver, far below a dropped one, and
#: inside the 120 s SIGTERM grace a GKE worker gets (`dispatch.py`).
DNS_PREFLIGHT_BUDGET_SECONDS = 10.0

#: The retry budget for each startup Firestore call, and each try's own
#: deadline.
#:
#: Without them, `DocumentReference.get` inherits `batch_get_documents`'
#: defaults: retry UNAVAILABLE, DEADLINE_EXCEEDED and INTERNAL for 300 s, with a
#: 300 s call timeout, logging only at DEBUG. The incident report places the
#: worker inside that retry when the reconciler killed it (inferred from the
#: exit code, not observed). Thirty seconds of retries rides out a Firestore
#: blip. Ten seconds per try lets one hung connection cost a third of that
#: budget, not all of it. A try that starts just inside the budget still gets
#: its own ten seconds, so one read that cannot be made ends startup within
#: about 40 s, with an error that names the exception. The old path could spend
#: 300 s on each read and print nothing.
FIRESTORE_STARTUP_RETRY_SECONDS = 30.0
FIRESTORE_STARTUP_CALL_SECONDS = 10.0

#: The only environment variables the pre-configuration log lines read. They
#: are identifiers the dispatcher sets and the same values `build_logger` binds
#: once the configuration has parsed. They name the attempt, not a secret. No
#: other variable is ever printed, because the environment is where a
#: misconfigured deployment would put a key.
_IDENTITY_ENV = (
    ("TASK_ID", "task_id"),
    ("ATTEMPT_ID", "attempt_id"),
    ("TENANT_ID", "tenant_id"),
    ("GENERATION", "generation"),
    ("RUNNER_PROFILE", "runner_profile"),
)

#: The Firestore endpoint the client library dials when no emulator is set.
FIRESTORE_HOST = "firestore.googleapis.com"

#: google-auth's default metadata host, used when neither GCE_METADATA_HOST nor
#: its legacy spelling GCE_METADATA_ROOT is set.
DEFAULT_METADATA_HOST = "metadata.google.internal"


class StartupInterrupted(BaseException):
    """A SIGTERM or SIGINT arrived while the lifecycle was preparing the runner.

    Raised FROM the signal handler, so it surfaces wherever the main thread
    was: inside a grpc wait, a subprocess wait, a sleep. It is a BaseException
    so that no `except Exception` on the way up (the checkpoint's, the account
    broker's, a library's retry loop) can swallow it and carry on starting an
    agent the platform has asked to stop.
    """

    def __init__(self, signum: int, phase: str) -> None:
        self.signum = int(signum)
        self.signal_name = signal_name(signum)
        self.phase = phase
        #: What a library raised in its place on the way up, if anything. Set
        #: by `Worker._execute`, which routes on the recorded interrupt and
        #: not on the exception that reached it.
        self.replaced_by: BaseException | None = None
        super().__init__(f"{self.signal_name} during startup phase {phase}")


def signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------


def bootstrap_logger(
    environ: Mapping[str, str] | None = None, *, stream: Any = None
) -> StructuredLogger:
    """A logger for the lines written before the configuration has parsed.

    It carries the attempt's identity from the environment, when present, so
    the very first line is attributable to a task. Unvalidated, because
    validating it is exactly the step that may fail.
    """
    env = os.environ if environ is None else environ
    labels: dict[str, Any] = {}
    for name, label in _IDENTITY_ENV:
        value = (env.get(name) or "").strip()
        if not value:
            continue
        if label == "generation":
            try:
                labels[label] = int(value)
                continue
            except ValueError:
                pass
        labels[label] = value[:200]
    return StructuredLogger(stream=stream, labels=labels)


class Phases:
    """Where the worker is in its startup, announced as it moves.

    One JSON line per phase, each flushed as it is written (`StructuredLogger`
    flushes every line). The last "startup phase" line of a pod that went
    silent is the phase it went silent in.
    """

    def __init__(
        self,
        logger: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        hard_exit: Callable[[int], Any] = os._exit,
    ) -> None:
        self.log = logger
        self._clock = clock
        #: `os._exit` in production. Replaceable so a test can observe the
        #: exit without the test process being the one that exits.
        self.hard_exit = hard_exit
        self._t0 = clock()
        self._since = self._t0
        self.current = "process_start"

    def rebind(self, logger: Any) -> None:
        """Log through `logger` from now on: the worker's own, once it exists."""
        self.log = logger

    def seconds_in_phase(self) -> float:
        return round(self._clock() - self._since, 3)

    def seconds_since_start(self) -> float:
        return round(self._clock() - self._t0, 3)

    def started(self, **fields: Any) -> None:
        """The first line the process writes, before anything can fail."""
        self.log.info(
            "worker process started",
            pid=os.getpid(),
            python=platform.python_version(),
            **fields,
        )

    def enter(self, phase: str, **fields: Any) -> None:
        now = self._clock()
        previous, spent = self.current, now - self._since
        self.current, self._since = phase, now
        self.log.info(
            "startup phase",
            phase=phase,
            previous_phase=previous,
            previous_phase_seconds=round(spent, 3),
            seconds_since_start=round(now - self._t0, 3),
            **fields,
        )

    def exit_on_signal(self, signum: int, _frame: Any = None) -> None:
        """Signal handler for the phases in which nothing has been written.

        Before the configuration, the DNS preflight, the clients and the
        generation check, this worker has written nothing, and so there is
        nothing to record or undo. So it says which phase it was in, then
        leaves at once. `os._exit` rather than an exception, because an
        exception would have to unwind through whichever library call the
        main thread was blocked in, and then through interpreter shutdown.
        Either could hold the process for as long as the thing that was
        already hanging.

        The line is written from a helper thread with a one-second cap. The
        main thread may have been interrupted while it held the logger's
        lock, and a handler that waited for that lock would wait for ever.
        """
        name = signal_name(signum)
        record = dict(
            signal=name,
            phase=self.current,
            seconds_in_phase=self.seconds_in_phase(),
            seconds_since_start=self.seconds_since_start(),
            exit_code=EXIT_INTERRUPTED,
        )

        def _say() -> None:
            try:
                self.log.warning(
                    f"{name} before the runner started; exiting now, having written nothing",
                    **record,
                )
            except Exception:
                pass

        speaker = threading.Thread(target=_say, name="sigterm-log", daemon=True)
        speaker.start()
        speaker.join(1.0)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        self.hard_exit(EXIT_INTERRUPTED)


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def route_signals(handler: Callable[[int, Any], Any]) -> bool:
    """Send SIGTERM and SIGINT to `handler`, and keep the SIGTERM stack dump armed.

    Returns whether the stack dump is armed.

    Re-arming is part of the routing, and not a separate step, because
    `signal.signal` replaces the C-level handler that `faulthandler` installed.
    `faulthandler` does not notice. It still believes it is registered, so a
    second `register` call only updates its options. Only `unregister` followed
    by `register` puts the dump back in front of the Python handler. With
    `chain=True` the dump runs first, and the Python handler after it.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not the main thread, or a platform without the signal. The
            # lifecycle still works: nothing delivers the signal to it.
            pass
    return arm_stack_dump()


def arm_stack_dump() -> bool:
    """On SIGTERM, write every thread's Python stack to stderr, then chain."""
    stream = sys.__stderr__ if sys.__stderr__ is not None else sys.stderr
    try:
        faulthandler.unregister(signal.SIGTERM)
        faulthandler.register(signal.SIGTERM, file=stream, all_threads=True, chain=True)
    except (AttributeError, ValueError, RuntimeError, OSError, io.UnsupportedOperation):
        return False
    return True


# ---------------------------------------------------------------------------
# DNS preflight
# ---------------------------------------------------------------------------


def _host_only(value: str) -> str:
    """`host`, `host:port`, `[v6]:port` or a stray `http://host/...` -> host."""
    value = value.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    try:
        parsed = urllib.parse.urlsplit("//" + value).hostname
    except ValueError:
        parsed = None
    return parsed or value.split("/", 1)[0]


def preflight_hosts(environ: Mapping[str, str] | None = None) -> list[str]:
    """The names this process must resolve before its first Firestore call.

    * **The Firestore endpoint.** That is the emulator's host when
      FIRESTORE_EMULATOR_HOST is set, and nothing else is needed then: the
      client uses anonymous credentials against an emulator.
    * **The metadata server**, which is where a Cloud Run or GKE worker gets
      its token and project id. google-auth names it GCE_METADATA_HOST, then
      the legacy GCE_METADATA_ROOT, then `metadata.google.internal`, and this
      resolves the same name. When that is an address, as the GKE worker env
      sets it, resolving it needs no DNS and costs nothing. It is skipped when
      GOOGLE_APPLICATION_CREDENTIALS names a key file, because credentials then
      do not come from the metadata server.
    """
    env = os.environ if environ is None else environ
    emulator = (env.get("FIRESTORE_EMULATOR_HOST") or "").strip()
    if emulator:
        return [_host_only(emulator)]
    hosts: list[str] = []
    if not (env.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip():
        metadata = (
            (env.get("GCE_METADATA_HOST") or "").strip()
            or (env.get("GCE_METADATA_ROOT") or "").strip()
            or DEFAULT_METADATA_HOST
        )
        hosts.append(_host_only(metadata))
    hosts.append(FIRESTORE_HOST)
    return hosts


def _resolve(host: str) -> Any:
    # Looked up on `socket` at call time, not bound at import, so that a test
    # can stand in for the resolver the way the network policy stood in for it.
    return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


def dns_preflight(
    hosts: Sequence[str],
    *,
    budget_seconds: float = DNS_PREFLIGHT_BUDGET_SECONDS,
    resolve: Callable[[str], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    """Resolve every host at once, under ONE deadline. One result per host.

    Each result is `{"host", "ok", "seconds"}` plus `"addresses"` when it
    resolved, or `"error"` when it did not. A host that has not answered by the
    deadline is reported as not resolved, whatever it does later.

    `getaddrinfo` is a blocking C call with no timeout of its own, so each
    lookup runs in a daemon thread and the deadline is enforced by the join. A
    lookup still stuck when the process exits is abandoned with it.
    """
    lookup = resolve if resolve is not None else _resolve
    results: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    started = clock()

    def _one(host: str) -> None:
        began = clock()
        try:
            infos = lookup(host)
            addresses = sorted({str(info[4][0]) for info in infos or []})
            outcome: dict[str, Any] = {
                "host": host,
                "ok": bool(addresses),
                "seconds": round(clock() - began, 3),
                "addresses": addresses[:4],
            }
            if not addresses:
                outcome["error"] = "resolver returned no addresses"
        except Exception as exc:
            outcome = {
                "host": host,
                "ok": False,
                "seconds": round(clock() - began, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
        with lock:
            results[host] = outcome

    threads = [
        threading.Thread(target=_one, args=(host,), name=f"dns-preflight:{host}", daemon=True)
        for host in hosts
    ]
    for thread in threads:
        thread.start()
    deadline = started + budget_seconds
    for thread in threads:
        thread.join(max(0.0, deadline - clock()))

    with lock:
        answered = dict(results)
    report: list[dict[str, Any]] = []
    for host in hosts:
        outcome = answered.get(host)
        if outcome is None:
            outcome = {
                "host": host,
                "ok": False,
                "seconds": round(clock() - started, 3),
                "error": f"no answer within {budget_seconds:g}s (DNS unreachable or dropping packets)",
            }
        report.append(outcome)
    return report


def resolver_nameservers(path: str = "/etc/resolv.conf") -> list[str]:
    """The nameservers this pod asks, for the operator reading the DNS error.

    On a GKE cluster with NodeLocal DNSCache this is the node-local cache's
    address (169.254.20.10), not kube-dns. The egress policy has to allow that
    address. Best effort: an unreadable file gives an empty list.
    """
    try:
        text = Path(path).read_text(errors="replace")[:8192]
    except OSError:
        return []
    servers: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers[:8]
