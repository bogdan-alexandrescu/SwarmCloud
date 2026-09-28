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
* **A capped agent stream keeps its END** (`keep_tail`). See `StreamCapture`.
* **No zombies.** Every exit path waits on the process and joins the readers.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

#: The first bytes of every line a capture writes where it cut a stream.
#: `swarm_api.agent_streams.TRUNCATION_MARK` restates it, so the transcript
#: and answer routes can tell a capped capture from a whole one;
#: `tests/unit/control_plane/test_agent_stream_parity.py` holds the two equal.
TRUNCATION_MARK = b"[swarm] output truncated"
TRUNCATION_NOTICE = b"\n" + TRUNCATION_MARK + b": size cap reached\n"
#: What a `keep_tail` capture writes where its head filled, while the stream is
#: still running: the live-tail publisher reads the file as it grows, and a
#: live view that simply stopped growing would read as an agent that stopped
#: printing. Replaced when the stream closes -- by the kept end, with a notice
#: counting what was dropped, or by the rest of the stream when nothing was.
TAIL_PENDING_NOTICE = (
    b"\n" + TRUNCATION_MARK
    + b": size cap reached; the end of the stream is kept and written here when it closes\n"
)

#: How much of a capped stream's END a `keep_tail` capture keeps: at most this,
#: and never more than half the cap, so the head and the end share the cap.
#: The end is where claude-code's `result` event is -- always its last line,
#: and a few KB: the answer, the usage, the cost -- and 1 MiB holds it with
#: room for the last turns before it.
TAIL_KEEP_BYTES = 1024 * 1024


@dataclass
class StreamCapture:
    path: Path
    limit: int
    #: Keep the END of a stream that passes `limit`, not only its start.
    #:
    #: WHY (#188 review). claude-code prints stream-json since #184, so its
    #: stdout is the whole conversation and its `result` event -- the answer,
    #: the spend, the evidence the rate-limit decision reads -- is the LAST
    #: line. A capture that kept only the first `limit` bytes lost exactly
    #: that line on every long session, and nothing said so.
    #:
    #: With it, the first `limit - tail` bytes are written as they arrive (the
    #: live view), the last `tail` bytes are held in memory, and when the
    #: stream closes they are written after a notice counting what was dropped
    #: -- starting on a whole line, so an NDJSON reader loses nothing but the
    #: line the cut went through. The file stays within `limit` plus the
    #: notice. OFF by default: git's captures judge a patch by the file's size,
    #: and a patch with its middle removed must never read as one that fitted.
    keep_tail: bool = False
    #: Bytes of the stream in the file: the head, plus the kept end.
    written: int = 0
    truncated: bool = False
    #: Bytes of the stream that are in no file.
    dropped: int = 0
    _thread: threading.Thread | None = field(default=None, repr=False)

    def pump(self, source: Any) -> None:
        """Drain `source` into `path`, writing at most `limit` bytes of it.

        `read1`, NOT `read`, AND THAT ONE CALL IS WHAT MAKES OUTPUT LIVE. The
        Popen pipe is a `BufferedReader`, and `BufferedReader.read(n)` blocks
        until `n` bytes have arrived or the pipe closes. With `read(65536)` a
        child that printed and flushed one 392-byte line at t=0 had that line
        reach this file only when it EXITED (measured: 3.1 s later, at exit).
        So nothing under 64 KiB of any stream -- the worker's capture of the
        runner and the runner's capture of the agent CLI both use this class --
        existed on disk while the attempt ran, and the live-tail publisher had
        nothing to publish: staging held zero `logs/live/` objects across 118
        attempts of two tenants (#184). `read1(n)` returns whatever one read of
        the pipe yields, up to `n`, as soon as there is any.

        A source without `read1` (a plain raw stream in a test) falls back to
        `read`, which for a raw stream already returns what is available.

        THE CUT IS ALWAYS MARKED. The notice is written with the first byte
        past the head, whichever read brings it. It used to be written only
        when a read STRADDLED the cap, so a cap reached exactly at the end of
        one read dropped the rest of the stream without a word.
        """
        reader = getattr(source, "read1", None) or source.read
        tail_room = min(TAIL_KEEP_BYTES, self.limit // 2) if self.keep_tail else 0
        head_limit = self.limit - tail_room
        held: deque[bytes] = deque()
        held_bytes = 0
        past = 0  # every byte that arrived after the head was full
        with self.path.open("wb") as sink:
            try:
                while True:
                    try:
                        chunk = reader(65536)
                    except (OSError, ValueError):
                        break  # the pipe was closed under the reader: the stream is over
                    if not chunk:
                        break
                    room = head_limit - self.written
                    if room > 0:
                        head = chunk[:room]
                        sink.write(head)
                        self.written += len(head)
                        chunk = chunk[room:]
                    if chunk:
                        if past == 0:
                            sink.write(TAIL_PENDING_NOTICE if tail_room else TRUNCATION_NOTICE)
                        past += len(chunk)
                        if tail_room:
                            # Hold the newest bytes, dropping whole reads from
                            # the front while what is left still fills the room.
                            held.append(chunk)
                            held_bytes += len(chunk)
                            while len(held) > 1 and held_bytes - len(held[0]) >= tail_room:
                                held_bytes -= len(held.popleft())
                        if past > tail_room:
                            # Bytes are being dropped for certain. Said now, not
                            # only at a close a wedged pipe may never reach.
                            self.truncated = True
                    sink.flush()  # keep draining past the cap so the child never blocks
            finally:
                if past:
                    self._close_tail(sink, head_end=head_limit, held=held, past=past,
                                     tail_room=tail_room)
        try:
            source.close()
        except Exception:
            pass

    def _close_tail(
        self, sink: Any, *, head_end: int, held: deque[bytes], past: int, tail_room: int
    ) -> None:
        """Finish a stream that passed its head: count the cut, write the kept end."""
        if not tail_room:
            self.dropped = past
            self.truncated = True
            return
        end = b"".join(held)
        sink.seek(head_end)
        sink.truncate()  # the pending notice goes; what replaces it says what happened
        if past <= tail_room:
            # Past the head, within the cap: nothing was dropped after all, and
            # the file is the whole stream.
            sink.write(end)
            self.written += len(end)
            sink.flush()
            return
        kept = end[-tail_room:]
        newline = kept.find(b"\n")
        if 0 <= newline < len(kept) - 1:
            # Start on a whole line, as the live tail does: for an NDJSON
            # stream a fragment is a line that parses as nothing. Kept whole
            # when the only newline ends it -- one long final line is still
            # the newest output there is.
            kept = kept[newline + 1 :]
        self.dropped = past - len(kept)
        self.truncated = True
        counted = (
            b": size cap reached; %d bytes were dropped here, and the last %d bytes "
            b"of the stream follow\n" % (self.dropped, len(kept))
        )
        sink.write(b"\n" + TRUNCATION_MARK + counted)
        sink.write(kept)
        self.written += len(kept)
        sink.flush()


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
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def capture_report(self) -> dict[str, Any]:
        """What the caps did to each stream, for a runner's result.json.

        A runner puts this into `RunnerContext.report`, which every
        `write_result` carries -- a failed or parked run included -- and the
        worker reads `stdout_truncated`/`stderr_truncated` into
        `result_summary.agent_streams`.
        """
        return {
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_dropped_bytes": self.stdout_dropped_bytes,
            "stderr_dropped_bytes": self.stderr_dropped_bytes,
        }


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
        keep_tail: bool = False,
        log_argv: Sequence[str] | None = None,
    ) -> None:
        self.argv = validate_argv(argv)
        # WHAT THE `child started` LINE SAYS THE ARGV WAS (the PR #229 review).
        # A CLI runner passes the task's prompt as its last argument, and this
        # line logged it whole into the runner's stderr, which `/logs` serves:
        # the prompt the task routes mask was printed in the clear, and a
        # value the task's metadata named as a secret with it. The runner
        # passes the argv with the prompt replaced by its length; everyone
        # else logs the argv itself, which holds nothing a caller wrote.
        self._log_argv = list(log_argv) if log_argv is not None else self.argv
        self._cwd = Path(cwd)
        self._env = dict(env)
        self._log = logger
        self._stdout = StreamCapture(Path(stdout_path), max_stdout_bytes, keep_tail=keep_tail)
        self._stderr = StreamCapture(Path(stderr_path), max_stderr_bytes, keep_tail=keep_tail)
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
        self._log.info("child started", pid=self._proc.pid, argv=self._log_argv, cwd=str(self._cwd))

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
            stdout_dropped_bytes=self._stdout.dropped,
            stderr_dropped_bytes=self._stderr.dropped,
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
    keep_tail: bool = False,
    log_argv: Sequence[str] | None = None,
) -> ChildResult:
    """Start, wait with a hard deadline, escalate, reap. One call, no leaks.

    Used for the short-lived helpers (git clone) where there is nothing to
    supervise, and by the runners for the agent child they start; the runner
    itself is driven by the lifecycle loop instead, which needs to heartbeat
    and checkpoint while it waits. `keep_tail` is `StreamCapture`'s: the
    runners set it for the agent's streams, whose last line is the result.
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
        keep_tail=keep_tail,
        log_argv=log_argv,
    )
    child.start()
    if child.wait(timeout_seconds) is None:
        child.mark_timed_out()
        child.terminate(grace_seconds, reason="timeout")
    return child.finish()


# ---------------------------------------------------------------------------
# Pre-publish reap -- nothing the agent started is alive when the token is used
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. The runner is started `start_new_session=True`, and stopping
# it signals its process GROUP (`ChildProcess.terminate` -> `killpg`). That
# reaps the runner and every helper that stayed in its group -- but NOT a
# process the agent itself put in a different group. A double fork plus
# `setsid()`, or `nohup ... & disown`, moves a child into its own session, and
# such a process survives the group kill and is re-parented to PID 1 (tini).
#
# The agent and the worker run as the SAME uid (`images/agent-runtime-base`:
# `USER swarm:swarm`; workspace.py: `ws.private` "is not a permission
# boundary"). So a surviving agent process could, in the window the publish path
# holds the tenant credential, watch `ws.private` and drop an `insteadOf` into
# the freshly `mkdtemp`'d publish repository before the push (a TOCTOU the
# unpredictable name alone does not close, because the watcher does not need to
# predict the name -- only to notice the directory appear), or read the
# short-lived 0600 credential file while the push runs. Either hands it the
# tenant's PAT.
#
# The guarantee this restores: when the worker runs its first git command that
# carries the token, no process the agent started is alive. `os.kill(-1, sig)`
# from a non-PID-1 process signals every process this uid may signal EXCEPT
# itself and PID 1 (POSIX kill(2)) -- i.e. tini stays, the worker stays, and
# every escaped agent process dies. tini reaps the corpses. The verify then
# reads /proc and refuses to publish if anything the uid owns is still alive,
# so a race that a kill missed becomes a refusal, never a leak.
#
# By the time this runs the runner has been reaped (`_child_ended`), the
# sampler thread is stopped, and the worker's own git helpers are synchronous
# (`run_child` waits) -- so there is no worker-owned subprocess to protect, and
# the worker's background workers (sampler, signal-log, DNS preflight) are
# THREADS, which share the worker's pid and never appear as a separate /proc
# entry. A liveness/readiness exec probe (v2 GKE) could be mid-run and get
# killed; its failureThreshold is 3, so one lost probe is harmless.


def _sigkill_all_other_processes() -> None:
    """SIGKILL every process this uid may signal except PID 1 and this process.

    `os.kill(-1, sig)` is the single-syscall form: the kernel delivers to the
    whole set at once, which is why a fork racing the kill is the only gap, and
    the caller's retry closes it. `ESRCH` means there was nothing else to
    signal, which is success, not failure.
    """
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _live_foreign_pids(
    *, proc_root: str = "/proc", uid: int | None = None, self_pid: int | None = None
) -> tuple[int, ...]:
    """PIDs of live processes this uid owns, other than PID 1 and this process.

    Reads `/proc/<pid>/status`: a process counts only when its REAL uid matches
    ours (kernel threads and PID 1, which may run as root, are skipped) and it
    is not a zombie -- a zombie has been killed and is merely awaiting a reap by
    tini, so counting it would refuse a publish over a corpse. `/proc` entries
    that vanish mid-scan are a process exiting under us and are skipped.

    `proc_root`, `uid` and `self_pid` are injectable so the logic is testable
    against a fabricated /proc without spawning anything.
    """
    uid = os.getuid() if uid is None else uid
    me = os.getpid() if self_pid is None else self_pid
    survivors: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return ()
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == 1 or pid == me:
            continue
        real_uid: int | None = None
        state = ""
        try:
            with open(f"{proc_root}/{entry}/status", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Uid:"):
                        # `Uid:\treal\teffective\tsaved\tfsuid`
                        parts = line.split()
                        if len(parts) >= 2:
                            real_uid = int(parts[1])
                    elif line.startswith("State:"):
                        # `State:\tR (running)` -- the code letter is what matters.
                        state = line.split(None, 2)[1] if len(line.split(None, 2)) >= 2 else ""
                    if real_uid is not None and state:
                        break
        except (OSError, ValueError):
            continue  # vanished, or a line we could not parse; not a survivor we can act on
        if real_uid != uid:
            continue
        if state == "Z":  # zombie: already dead, tini will reap it
            continue
        survivors.append(pid)
    return tuple(survivors)


def reap_foreign_processes(
    *,
    logger: Any,
    killer: Any = None,
    lister: Any = None,
    attempts: int = 20,
    delay: float = 0.05,
) -> tuple[int, ...]:
    """Kill every foreign process this uid owns, then verify none survive.

    Returns `()` when the container holds only PID 1 and this process after the
    reap; otherwise the PIDs still alive after `attempts` kill-and-check rounds,
    which the caller turns into a refusal to publish. Never raises: a reap that
    cannot prove the container is clean must fail closed at the call site, not
    crash the teardown path.

    `killer` and `lister` are injectable so a unit test can exercise the
    orchestration -- and so no test ever runs the real `os.kill(-1, SIGKILL)`,
    which in a shared test process would take the test runner down with it.
    """
    kill = killer or _sigkill_all_other_processes
    live = lister or _live_foreign_pids
    kill()
    survivors = live()
    tries = 0
    while survivors and tries < attempts:
        time.sleep(delay)
        kill()  # a process mid-fork when the last kill landed is caught now
        survivors = live()
        tries += 1
    if survivors:
        logger.error(
            "processes the agent started are still alive after the pre-publish reap; "
            "refusing to publish so the tenant credential is never in hand while "
            "agent code can run",
            surviving_pids=list(survivors),
        )
    else:
        logger.info("pre-publish reap complete; no foreign process remains")
    return tuple(survivors)
