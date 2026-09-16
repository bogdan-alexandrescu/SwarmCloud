"""Child process supervision.

The runner is a separate process, and everything unpleasant about running
untrusted-ish code lives here:

* **Never a shell.** `argv` is a list, `shell=False` always. Nothing a tenant
  puts in a task input is ever concatenated into a command line, so there is no
  quoting bug that could become command injection.
* **Its own session.** `start_new_session=True` puts the child in a new process
  group, so signals go to the whole tree with `killpg`. A runner that forks
  helpers -- and `claude`, `npm` and Playwright all do -- cannot leave orphans
  holding the CPU after the worker decides to stop.
* **SIGTERM, then SIGKILL.** The child gets `termination_grace_seconds` to flush
  its own output and exit cleanly. If it is still alive after that, it is killed
  outright; a process that ignores SIGTERM is exactly the process that would
  otherwise run until the platform's own timeout.
* **Capped output, but never a blocked pipe.** Reader threads keep draining
  stdout and stderr after the cap is reached and discard the excess. Stopping the
  read instead would fill the pipe buffer and deadlock the child at 64KB -- a
  hang that looks exactly like a slow agent.
* **No zombies.** Every exit path waits on the process and joins the readers.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

TRUNCATION_NOTICE = b"\n[swarm] output truncated: size cap reached\n"


@dataclass
class StreamCapture:
    path: Path
    limit: int
    written: int = 0
    truncated: bool = False
    _thread: threading.Thread | None = field(default=None, repr=False)

    def pump(self, source: Any) -> None:
        """Drain `source` into `path`, writing at most `limit` bytes."""
        with self.path.open("wb") as sink:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                if self.written >= self.limit:
                    self.truncated = True
                    continue  # keep draining so the child never blocks
                room = self.limit - self.written
                if len(chunk) > room:
                    sink.write(chunk[:room])
                    sink.write(TRUNCATION_NOTICE)
                    self.written = self.limit
                    self.truncated = True
                else:
                    sink.write(chunk)
                    self.written += len(chunk)
                sink.flush()
        try:
            source.close()
        except Exception:
            pass


@dataclass(frozen=True)
class ChildResult:
    exit_code: int | None
    term_signal: int | None
    timed_out: bool
    killed: bool
    duration_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ProcessError(RuntimeError):
    pass


def validate_argv(argv: Sequence[str]) -> list[str]:
    if not argv:
        raise ProcessError("argv must not be empty")
    if isinstance(argv, (str, bytes)):
        raise ProcessError("argv must be a list, never a string; the worker never uses a shell")
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise ProcessError(f"argv entries must be strings, got {type(item).__name__}")
        if "\x00" in item:
            raise ProcessError("argv entries must not contain NUL")
        out.append(item)
    return out


class ChildProcess:
    """A started runner child, supervised by the worker's main loop."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        logger: Any,
    ) -> None:
        self.argv = validate_argv(argv)
        self._cwd = Path(cwd)
        self._env = dict(env)
        self._log = logger
        self._stdout = StreamCapture(Path(stdout_path), max_stdout_bytes)
        self._stderr = StreamCapture(Path(stderr_path), max_stderr_bytes)
        self._proc: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._started_at = 0.0
        self._timed_out = False
        self._killed = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None:
            raise ProcessError("child already started")
        self._started_at = time.monotonic()
        self._proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False by construction
            self.argv,
            cwd=str(self._cwd),
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
        for capture, stream in ((self._stdout, self._proc.stdout), (self._stderr, self._proc.stderr)):
            thread = threading.Thread(target=capture.pump, args=(stream,), daemon=True)
            thread.start()
            self._threads.append(thread)
        self._log.info("child started", pid=self._proc.pid, argv=self.argv, cwd=str(self._cwd))

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at if self._started_at else 0.0

    def poll(self) -> int | None:
        return self._proc.poll() if self._proc else None

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait up to `timeout` seconds. Returns None if still running."""
        if self._proc is None:
            raise ProcessError("child not started")
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    # -- termination -------------------------------------------------------
    def _signal_group(self, sig: int) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def terminate(self, grace_seconds: float, *, reason: str = "terminated") -> None:
        """SIGTERM the process group, then SIGKILL anything still alive."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._log.warning("terminating child", reason=reason, pid=self._proc.pid, grace=grace_seconds)
        self._signal_group(signal.SIGTERM)
        if self.wait(grace_seconds) is not None:
            return
        self._log.error(
            "child ignored SIGTERM; escalating to SIGKILL", pid=self._proc.pid, reason=reason
        )
        self._killed = True
        self._signal_group(signal.SIGKILL)
        self.wait(10)

    def mark_timed_out(self) -> None:
        self._timed_out = True

    # -- finalisation ------------------------------------------------------
    def finish(self) -> ChildResult:
        """Reap the child and join the reader threads. Safe to call twice."""
        if self._proc is None:
            raise ProcessError("child not started")
        code = self._proc.poll()
        if code is None:
            code = self._proc.wait()
        for thread in self._threads:
            thread.join(timeout=10)
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass
        term_signal = -code if code is not None and code < 0 else None
        return ChildResult(
            exit_code=code,
            term_signal=term_signal,
            timed_out=self._timed_out,
            killed=self._killed,
            duration_seconds=self.elapsed_seconds,
            stdout_bytes=self._stdout.written,
            stderr_bytes=self._stderr.written,
            stdout_truncated=self._stdout.truncated,
            stderr_truncated=self._stderr.truncated,
        )


def run_child(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    grace_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    logger: Any,
) -> ChildResult:
    """Start, wait with a hard deadline, escalate, reap. One call, no leaks.

    Used for the short-lived helpers (git clone) where there is nothing to
    supervise; the runner itself is driven by the lifecycle loop instead, which
    needs to heartbeat and checkpoint while it waits.
    """
    child = ChildProcess(
        argv,
        cwd=cwd,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        logger=logger,
    )
    child.start()
    if child.wait(timeout_seconds) is None:
        child.mark_timed_out()
        child.terminate(grace_seconds, reason="timeout")
    return child.finish()
