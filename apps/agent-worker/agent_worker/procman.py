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
* **Below the worker's priority** (`niceness`). The runner child's process
  group is reniced as it starts, and everything it forks inherits it, so an
  agent compiling on every core cannot starve the worker's heartbeat (archived issue 426).
* **No zombies.** Every exit path waits on the process and joins the readers.
* **Nothing outlives its leader, and no pipe holds the step.** When the child
  exits, what it left in its process group -- the CLI's stdio tool servers,
  `swarm-graph` among them, which inherit its stderr -- is stopped (SIGTERM,
  then SIGKILL), and a reader whose pipe is still held by a process outside
  the group is told to stop instead of being waited on. See `finish`.
"""

from __future__ import annotations

import io
import os
import select
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

#: The first bytes of every line a capture writes where it cut a stream.
#: `swarm_api.agent_streams.TRUNCATION_MARK` restates it, so the transcript
#: and answer routes can tell a capped capture from a whole one; the two
#: are held equal.
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

#: How long the readers get, once the child has exited and its group has been
#: sent SIGTERM, before what is left of the group is killed; and again, once
#: it has been, before a reader whose pipe is still held is told to stop.
LEFTOVER_GRACE_SECONDS = 2.0
#: How often the descendant sweep (`ChildProcess.terminate(sweep_descendants=
#: True)`) reads /proc while it waits for what it signalled to exit.
SWEEP_POLL_SECONDS = 0.05
#: How many SIGKILL-and-read rounds the sweep makes before it reports the
#: survivors: 20 x 50 ms, a second, which a fork racing the kill cannot outlast.
SWEEP_VERIFY_ROUNDS = 20
#: How often a reader waiting on a quiet pipe looks at its stop flag.
READ_POLL_SECONDS = 0.2


@dataclass
class StreamCapture:
    path: Path
    limit: int
    #: Keep the END of a stream that passes `limit`, not only its start.
    #:
    #: WHY (archived pull request 188 review). claude-code prints stream-json since #184, so its
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
    #: Called with every chunk read from the pipe, BEFORE the cap is applied.
    #:
    #: WHY (the B11 review). The account watcher (`cliagent.AccountStreamWatcher`)
    #: must see every turn boundary and every `rate_limit_event` of a run, and
    #: a `keep_tail` file stops growing at its head: past it, the end is held in
    #: memory until the stream closes. A watcher reading the FILE went blind for
    #: the rest of exactly the long sessions most likely to exhaust or outlive an
    #: account. The tap sees the live pipe; the file keeps its head+tail shape.
    #: A tap that raises is dropped, never the stream: the capture is the record.
    on_chunk: Callable[[bytes], None] | None = None
    #: Bytes of the stream in the file: the head, plus the kept end.
    written: int = 0
    truncated: bool = False
    #: Bytes of the stream that are in no file.
    dropped: int = 0
    _thread: threading.Thread | None = field(default=None, repr=False)
    #: Set by `ChildProcess.finish` when the child has exited and its pipe is
    #: still held open by a process outside its group: the stream is over for
    #: this capture, which writes its kept end and closes its own end.
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def stop(self) -> None:
        self._stop.set()

    def _wait_readable(self, poller: Any) -> bool:
        """Wait until the pipe has bytes or end-of-file; False when told to stop first.

        A read is never started that could block past `stop`: a blocked read
        cannot be interrupted, and closing the pipe under it waits for it.
        """
        if poller is None:
            return True
        while not self._stop.is_set():
            try:
                if poller.poll(int(READ_POLL_SECONDS * 1000)):
                    return True
            except (OSError, ValueError):
                return True  # the read reports what is wrong with the pipe
        return False

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
        try:
            poller: Any = select.poll()
            poller.register(source.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            poller = None  # a stream with no descriptor (a test's) is read as it was
        tail_room = min(TAIL_KEEP_BYTES, self.limit // 2) if self.keep_tail else 0
        head_limit = self.limit - tail_room
        held: deque[bytes] = deque()
        held_bytes = 0
        past = 0  # every byte that arrived after the head was full
        with self.path.open("wb") as sink:
            try:
                while True:
                    if not self._wait_readable(poller):
                        break  # `stop`: the child is gone and someone else holds the pipe
                    try:
                        chunk = reader(65536)
                    except (OSError, ValueError):
                        break  # the pipe was closed under the reader: the stream is over
                    if not chunk:
                        break
                    if self.on_chunk is not None:
                        try:
                            self.on_chunk(chunk)
                        except Exception:  # noqa: BLE001
                            self.on_chunk = None
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
        except Exception:  # noqa: BLE001, S110
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


@dataclass(frozen=True)
class ProcEntry:
    """One process as the descendant sweep sees it, from /proc/<pid>/stat.

    The command name, never argv: an argv can carry a secret, and a survivor
    is logged. `starttime` (clock ticks since boot) tells a pid from the
    process that later took it."""

    pid: int
    ppid: int
    pgid: int
    sid: int
    starttime: int
    state: str
    name: str


def read_proc_table(proc_root: str = "/proc") -> dict[int, ProcEntry]:
    """Every process in `proc_root`, by pid.

    `/proc/<pid>/stat` is split after the LAST ')' (the name may hold one):
    state is field 3, ppid 4, pgid 5, session 6, starttime 22. An entry that
    vanishes mid-read is a process exiting and is skipped; `proc_root` itself
    failing to list raises `ProcessTableUnreadable`.
    """
    try:
        entries = os.listdir(proc_root)
    except OSError as exc:
        raise ProcessTableUnreadable(exc.errno, f"cannot list {proc_root}: {exc.strerror}") from exc
    table: dict[int, ProcEntry] = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc_root}/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            close = raw.rindex(")")
            fields = raw[close + 2 :].split()
            table[int(entry)] = ProcEntry(
                pid=int(entry),
                ppid=int(fields[1]),
                pgid=int(fields[2]),
                sid=int(fields[3]),
                starttime=int(fields[19]),
                state=fields[0],
                name=raw[raw.find("(") + 1 : close],
            )
        except (OSError, ValueError, IndexError):
            continue
    return table


def _descendants(roots: set[int], table: dict[int, ProcEntry]) -> set[int]:
    children: dict[int, list[int]] = {}
    for entry in table.values():
        children.setdefault(entry.ppid, []).append(entry.pid)
    seen: set[int] = set()
    stack = [child for root in roots for child in children.get(root, ())]
    while stack:
        pid = stack.pop()
        if pid in seen or pid in roots:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return seen


def descendants_of(root_pid: int, table: dict[int, ProcEntry]) -> dict[int, ProcEntry]:
    """`root_pid`'s live descendants by parent pid, each with its ids; not the root."""
    return {
        pid: table[pid]
        for pid in _descendants({root_pid}, table)
        if table[pid].state != "Z"
    }


def sweep_targets(
    snapshot: dict[int, ProcEntry],
    table: dict[int, ProcEntry],
    *,
    protected_pids: set[int],
    protected_groups: set[int],
) -> dict[int, ProcEntry]:
    """The live processes in `table` the sweep stops, given what `snapshot` recorded.

    A target is a process that is a snapshot entry with the SAME start time
    (a pid reused since is not), or whose group or session is one the
    snapshot recorded, or that descends from a target. A recorded group or
    session id whose leader pid is now a DIFFERENT process (other start time)
    was emptied and reused, and no longer counts. Never a zombie (already
    dead), a protected pid, or a process whose group or session is protected.
    """
    ids = {e.pgid for e in snapshot.values()} | {e.sid for e in snapshot.values()}
    for gid in list(ids):
        leader, recorded = table.get(gid), snapshot.get(gid)
        if leader is not None and recorded is not None and leader.starttime != recorded.starttime:
            ids.discard(gid)
    ids -= protected_groups

    def allowed(entry: ProcEntry) -> bool:
        return (
            entry.state != "Z"
            and entry.pid not in protected_pids
            and entry.pgid not in protected_groups
            and entry.sid not in protected_groups
        )

    roots = {
        pid
        for pid, entry in table.items()
        if allowed(entry)
        and (
            (pid in snapshot and snapshot[pid].starttime == entry.starttime)
            or entry.pgid in ids
            or entry.sid in ids
        )
    }
    found = roots | _descendants(roots, table)
    return {pid: table[pid] for pid in found if allowed(table[pid])}


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
        stdout_tap: Callable[[bytes], None] | None = None,
        niceness: int = 0,
        stdin_data: bytes | None = None,
    ) -> None:
        self.argv = validate_argv(argv)
        # WHAT THE CHILD READS ON STDIN, or nothing (/dev/null). A CLI runner
        # passes the task's prompt here rather than in the argv (owner
        # decision 2026-10-06, observer P9): every process listing shows the
        # argv, and an agent's `ps aux | grep <pattern from its brief> | xargs
        # kill` matched its own CLI's command line and killed it. One argv
        # string is also capped at 128 KiB; stdin is not.
        self._stdin_data = stdin_data
        # How far below this process's priority the child runs: 0 is the same.
        self._niceness = niceness
        # WHAT THE `child started` LINE SAYS THE ARGV WAS (the archived pull request 229 review).
        # A CLI runner passed the task's prompt as its last argument, and this
        # line logged it whole into the runner's stderr, which `/logs` serves:
        # the prompt the task routes mask was printed in the clear, and a
        # value the task's metadata named as a secret with it. The prompt now
        # goes on stdin (`stdin_data`), and only its size is logged; a runner
        # still passes `log_argv` to mask a resumed session's id. Everyone
        # else logs the argv itself, which holds nothing a caller wrote.
        self._log_argv = list(log_argv) if log_argv is not None else self.argv
        self._cwd = Path(cwd)
        self._env = dict(env)
        self._log = logger
        self._stdout = StreamCapture(
            Path(stdout_path), max_stdout_bytes, keep_tail=keep_tail, on_chunk=stdout_tap
        )
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
        # The input is written to an ANONYMOUS temporary file, rewound, and
        # that is the child's stdin: no writer thread to deadlock against a
        # child that never reads, no size limit, and no name on disk for the
        # prompt to be found under. The child holds its own descriptor; ours
        # is closed once it has started.
        stdin: Any = subprocess.DEVNULL
        if self._stdin_data is not None:
            stdin = tempfile.TemporaryFile()
            stdin.write(self._stdin_data)
            stdin.seek(0)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False by construction
                self.argv,
                cwd=str(self._cwd),
                env=self._env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            if stdin is not subprocess.DEVNULL:
                stdin.close()
        if self._niceness:
            self._lower_priority()
        for capture, stream in ((self._stdout, self._proc.stdout), (self._stderr, self._proc.stderr)):
            thread = threading.Thread(target=capture.pump, args=(stream,), daemon=True)
            thread.start()
            self._threads.append(thread)
        started: dict[str, Any] = {"pid": self._proc.pid, "argv": self._log_argv, "cwd": str(self._cwd)}
        if self._stdin_data is not None:
            # Its size, never its content: the prompt is served masked.
            started["stdin_bytes"] = len(self._stdin_data)
        self._log.info("child started", **started)

    def _lower_priority(self) -> None:
        """Run the child's process group `niceness` below this process (archived issue 426).

        THE GROUP, NOT THE PID. The child leads its own process group
        (`start_new_session`), and `PRIO_PGRP` reaches every process in it,
        so anything it has already forked is lowered with it; what it forks
        later inherits the value. Lowering a priority needs no capability,
        and the kernel caps it at 19.

        Not a `preexec_fn`, which is unsafe in a process that runs threads,
        as this one does. The window between the fork and this call is the
        child's interpreter starting, in which it forks nothing.

        A child that could not be lowered still runs: the heartbeat then
        competes with it at the same priority, as it did before, and the line
        below says so. Not raised, because stopping a running agent for this
        would cost the attempt the heartbeat exists to keep.
        """
        assert self._proc is not None
        try:
            target = min(19, os.getpriority(os.PRIO_PROCESS, 0) + self._niceness)
            os.setpriority(os.PRIO_PGRP, self._proc.pid, target)
        except OSError as exc:
            self._log.warning(
                "could not lower the child's priority; it competes with the worker's "
                "heartbeat at the same priority",
                pid=self._proc.pid,
                niceness=self._niceness,
                error=f"{type(exc).__name__}: {exc}",
            )

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def elapsed_seconds(self) -> float | None:
        """Seconds since the child started, or None when it never started.

        None, not 0.0 (issue 56): a 0.0 reads as "ran instantly" and was
        stored and summed as a measured duration.
        """
        if self._proc is None or not self._started_at:
            return None
        return time.monotonic() - self._started_at

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

    def terminate(
        self,
        grace_seconds: float,
        *,
        reason: str = "terminated",
        sweep_descendants: bool = False,
    ) -> tuple[int, ...]:
        """SIGTERM the process group, then SIGKILL anything still alive.

        With `sweep_descendants` (issue 492) it also stops what the child
        started in OTHER process groups and sessions -- claude-code runs each
        Bash tool command in its own -- which the group signal never reaches:
        the child's descendants are snapshotted while it is alive (and again
        right after the SIGTERM), and once it is dead `_sweep` stops them.
        Returns the pids that survived the sweep; `()` when it was clean, was
        not asked for, or there was nothing to stop. Never raises over /proc.
        """
        if self._proc is None or self._proc.poll() is not None:
            return ()
        snapshot = self._snapshot() if sweep_descendants else {}
        self._log.warning("terminating child", reason=reason, pid=self._proc.pid, grace=grace_seconds)
        self._signal_group(signal.SIGTERM)
        if sweep_descendants:
            snapshot.update(self._snapshot())
        if self.wait(grace_seconds) is None:
            self._log.error(
                "child ignored SIGTERM; escalating to SIGKILL", pid=self._proc.pid, reason=reason
            )
            self._killed = True
            self._signal_group(signal.SIGKILL)
            self.wait(10)
        if not sweep_descendants:
            return ()
        return self._sweep(snapshot, reason=reason)

    def _snapshot(self) -> dict[int, ProcEntry]:
        """The child and its live descendants by parent pid; {} when /proc cannot be read.

        The child's own entry is kept so its start time tells its group and
        session ids from a later process that took its pid."""
        assert self._proc is not None
        try:
            table = read_proc_table()
            snapshot = descendants_of(self._proc.pid, table)
            if self._proc.pid in table:
                snapshot[self._proc.pid] = table[self._proc.pid]
            return snapshot
        except OSError as exc:
            self._log.warning(
                "could not read the process table to find what the child started; "
                "a command it left in another session is not stopped here",
                pid=self._proc.pid,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {}

    def _protected(self) -> tuple[set[int], set[int]]:
        """(pids, group and session ids) the sweep never signals.

        Pid 1, this process (the runner, or the worker when it is the caller)
        and its parent; this process's own group and session, which are also
        the child's parent's group. A target in one of those groups or
        sessions is skipped by pid as well.
        """
        pids = {1, os.getpid(), os.getppid()}
        groups: set[int] = {1}
        for read in (lambda: os.getpgid(0), lambda: os.getsid(0)):
            try:
                groups.add(read())
            except OSError:
                pass
        return pids, groups

    def _sweep(self, snapshot: dict[int, ProcEntry], *, reason: str) -> tuple[int, ...]:
        """Stop what `snapshot` recorded, once the child is dead; return the survivors.

        SIGTERM every target -- `killpg` for a group the snapshot recorded,
        `os.kill` by pid otherwise -- give them `LEFTOVER_GRACE_SECONDS`,
        SIGKILL what is left, then read /proc again for up to
        `SWEEP_VERIFY_ROUNDS` rounds. Never `os.kill(-1, ...)`: from the
        runner that would hit the worker. Logs command names only, never an
        argv or an environment: an argv can carry a secret.
        """
        if not snapshot:
            return ()
        assert self._proc is not None
        protected_pids, protected_groups = self._protected()
        recorded_groups = {e.pgid for e in snapshot.values()} - protected_groups

        def targets() -> dict[int, ProcEntry]:
            return sweep_targets(
                snapshot, read_proc_table(),
                protected_pids=protected_pids, protected_groups=protected_groups,
            )

        def signal_all(found: dict[int, ProcEntry], sig: int) -> None:
            for pgid in sorted({e.pgid for e in found.values()} & recorded_groups):
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    pass
            for pid, entry in found.items():
                if entry.pgid in recorded_groups:
                    continue
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass

        try:
            found = targets()
            if not found:
                return ()
            groups = {e.pgid for e in found.values()}
            stopped = len(found)
            signal_all(found, signal.SIGTERM)
            deadline = time.monotonic() + LEFTOVER_GRACE_SECONDS
            while found and time.monotonic() < deadline:
                time.sleep(SWEEP_POLL_SECONDS)
                found = targets()
            rounds = 0
            while found and rounds < SWEEP_VERIFY_ROUNDS:
                signal_all(found, signal.SIGKILL)
                time.sleep(SWEEP_POLL_SECONDS)
                found = targets()
                rounds += 1
        except OSError as exc:
            self._log.warning(
                "could not read the process table while stopping what the child left in "
                "other sessions; what is left is stopped by the pre-publish reap or the pod's end",
                pid=self._proc.pid,
                reason=reason,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ()
        self._log.info(
            "stopped what the child left in other sessions and process groups",
            pid=self._proc.pid,
            reason=reason,
            groups=len(groups),
            processes=stopped,
        )
        if found:
            self._log.error(
                "processes the child started are still alive after SIGKILL",
                pid=self._proc.pid,
                reason=reason,
                survivors=[{"pid": pid, "name": e.name} for pid, e in sorted(found.items())],
            )
        return tuple(sorted(found))

    def mark_timed_out(self) -> None:
        self._timed_out = True

    # -- finalisation ------------------------------------------------------
    def _signal_leftovers(self, sig: int) -> bool:
        """Signal what is left of the exited child's process group; False if nothing is.

        The child led its own group (`start_new_session`), so the group's id is
        its pid, which no new process can take while the group has a member.
        "Left" counts a member that has exited and awaits its reaper too: a
        signal to it is harmless.
        """
        assert self._proc is not None
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def _join_readers(self, seconds: float) -> bool:
        """Join the reader threads within `seconds` in all; True when every one ended."""
        end = time.monotonic() + seconds
        for thread in self._threads:
            thread.join(timeout=max(0.0, end - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def _release_streams(self) -> None:
        """Stop what the exited child left behind, and end its captures within seconds.

        WHY (owner, 2026-11-02). Review steps with code intelligence held their
        pod 6 to 31 minutes after the agent's last turn. The agent CLI's stdio
        tool servers (`swarm-graph` and the other MCP shims) inherit its stderr,
        so one that outlived the CLI kept the write end of the pipe this
        process captures: the reader never saw end-of-file, the join below gave
        up after 10 s, and the `close()` that followed blocked on the buffered
        reader's lock, held by the reader blocked in its read, until the server
        exited. The step waited exactly as long as the server lived.

        So, once the child has exited: SIGTERM what is left of its group, give
        the readers `LEFTOVER_GRACE_SECONDS` to reach end-of-file, SIGKILL the
        rest, and if a process outside the group (`setsid`) still holds a pipe,
        tell the reader to stop: it writes its kept end and closes its own end
        of the pipe. A pipe a live reader is blocked on is never closed here.
        Nothing is cut off that the result needs: the child has exited, so its
        own output is all in the pipe, and the readers drain it first.
        """
        assert self._proc is not None
        if self._signal_leftovers(signal.SIGTERM):
            if not self._join_readers(LEFTOVER_GRACE_SECONDS):
                self._log.warning(
                    "the child exited and processes it started still hold its output; "
                    "killing what is left of its process group",
                    pid=self._proc.pid,
                )
            self._signal_leftovers(signal.SIGKILL)
        if not self._join_readers(LEFTOVER_GRACE_SECONDS):
            self._log.warning(
                "a process outside the child's group still holds its output; the capture "
                "ends here; the pre-publish reap, or the pod's end, stops that process",
                pid=self._proc.pid,
            )
            self._stdout.stop()
            self._stderr.stop()
            self._join_readers(LEFTOVER_GRACE_SECONDS)
        for thread, stream in zip(self._threads, (self._proc.stdout, self._proc.stderr)):
            if thread.is_alive():
                continue  # closing under a blocked read waits for the read
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:  # noqa: BLE001, S110
                pass

    def finish(self) -> ChildResult:
        """Reap the child and join the reader threads. Safe to call twice."""
        if self._proc is None:
            raise ProcessError("child not started")
        code = self._proc.poll()
        if code is None:
            code = self._proc.wait()
        self._release_streams()
        term_signal = -code if code is not None and code < 0 else None
        return ChildResult(
            exit_code=code,
            term_signal=term_signal,
            timed_out=self._timed_out,
            killed=self._killed,
            # Started, since `_proc` is set: the clock always has a reading.
            duration_seconds=time.monotonic() - self._started_at,
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
    stdin_data: bytes | None = None,
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
        stdin_data=stdin_data,
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


class ProcessTableUnreadable(OSError):
    """`/proc` could not be listed, so whether anything survives is unknown.

    Raised rather than answered with `()`: an empty answer is what a clean
    container gives, and a reap that read it as one would publish having
    checked nothing (archived issue 346, archived pull request 259 review)."""


#: What `reap_foreign_processes` returns when it could not read the process
#: table: not a real PID (none is negative), but a non-empty result, so every
#: caller's `if survivors:` refuses to publish -- fail closed.
UNVERIFIED_PID = -1


def _live_foreign_pids(
    *, proc_root: str = "/proc", uid: int | None = None, self_pid: int | None = None
) -> tuple[int, ...]:
    """PIDs of live processes this uid owns, other than PID 1 and this process.

    Reads `/proc/<pid>/status`: a process counts only when its REAL uid matches
    ours (kernel threads and PID 1, which may run as root, are skipped) and it
    is not a zombie -- a zombie has been killed and is merely awaiting a reap by
    tini, so counting it would refuse a publish over a corpse. `/proc` entries
    that vanish mid-scan are a process exiting under us and are skipped.
    `/proc` ITSELF failing to list raises `ProcessTableUnreadable`: that is not
    "no process survives", it is "nobody looked".

    `proc_root`, `uid` and `self_pid` are injectable so the logic is testable
    against a fabricated /proc without spawning anything.
    """
    uid = os.getuid() if uid is None else uid
    me = os.getpid() if self_pid is None else self_pid
    survivors: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError as exc:
        raise ProcessTableUnreadable(exc.errno, f"cannot list {proc_root}: {exc.strerror}") from exc
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
    which the caller turns into a refusal to publish; `(UNVERIFIED_PID,)` when
    the process table could not be read at all. Never raises: a reap that
    cannot prove the container is clean must fail closed at the call site, not
    crash the teardown path.

    `killer` and `lister` are injectable so a unit test can exercise the
    orchestration -- and so no test ever runs the real `os.kill(-1, SIGKILL)`,
    which in a shared test process would take the test runner down with it.
    """
    kill = killer or _sigkill_all_other_processes
    live = lister or _live_foreign_pids
    kill()
    try:
        survivors = live()
        tries = 0
        while survivors and tries < attempts:
            time.sleep(delay)
            kill()  # a process mid-fork when the last kill landed is caught now
            survivors = live()
            tries += 1
    except OSError as exc:
        # The process table could not be read, so the reap cannot prove the
        # container clean. Fail closed: a non-empty result is a refusal.
        logger.error(
            "the pre-publish reap could not read the process table; refusing to "
            "publish because no process the agent started can be ruled out",
            error=str(exc),
        )
        return (UNVERIFIED_PID,)
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
