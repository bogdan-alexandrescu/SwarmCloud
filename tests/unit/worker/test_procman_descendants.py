"""A stop ends what the child started in other sessions too (issue 492).

Claude Code runs each Bash tool command in its OWN session and process group.
`ChildProcess.terminate` signals the CLI's group, so a command the CLI started
survived the stop, was re-parented to pid 1 and kept its memory until the
container was OOM-killed. `terminate(..., sweep_descendants=True)` snapshots
the child's descendants while it is alive and, once it is dead, stops every
foreign group and session in that snapshot -- and nothing else: not a process
the caller started itself, not the caller, never `os.kill(-1)`.

These run on real processes, as test_procman_timeout.py does; every process a
test starts is SIGKILLed in its `finally`, so a red run leaks nothing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_worker.logs import build_logger
from agent_worker.procman import (
    ChildProcess,
    ProcEntry,
    descendants_of,
    sweep_targets,
)

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="no procfs")

#: The great-grandchild: ignores SIGTERM, stays in its parent's session.
_INNER = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"

#: The grandchild, started by the child in its OWN session with none of the
#: child's pipes, as the Bash tool runs a command: ignores SIGTERM, starts the
#: great-grandchild, then writes both pids (atomically) to argv[1].
_GRANDCHILD = (
    "import os, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "inner = subprocess.Popen([sys.executable, '-c', " + repr(_INNER) + "],\n"
    "                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
    "                         stderr=subprocess.DEVNULL)\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f:\n"
    "    f.write(f'{os.getpid()} {inner.pid}')\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(300)\n"
)

#: The child (the agent CLI's stand-in): starts the grandchild, then waits.
_CHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', " + repr(_GRANDCHILD) + ", sys.argv[1]],\n"
    "                 start_new_session=True, stdin=subprocess.DEVNULL,\n"
    "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print('started', flush=True)\n"
    "time.sleep(300)\n"
)


@pytest.fixture
def logger(log_stream):
    return build_logger(
        task_id="t", attempt_id="a", tenant_id="eng", generation=1,
        runner_profile="mock", stream=log_stream,
    )


def _alive(pid: int) -> bool:
    """A /proc entry that is not a zombie: a killed orphan may stay one."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError:
        return False
    return raw[raw.rindex(")") + 2 :].split()[0] != "Z"


def _kill(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _start(tmp_path: Path, logger) -> tuple[ChildProcess, int, int]:
    """The child, and the pids of its foreign-session grandchild and great-grandchild."""
    pids = tmp_path / "pids"
    child = ChildProcess(
        [sys.executable, "-c", _CHILD, str(pids)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    child.start()
    deadline = time.monotonic() + 15
    while not pids.exists():
        if time.monotonic() > deadline:
            child.terminate(1.0)
            child.finish()
            pytest.fail("the grandchild never wrote its pids")
        time.sleep(0.05)
    grandchild, inner = (int(x) for x in pids.read_text().split())
    return child, grandchild, inner


def test_a_foreign_session_grandchild_that_ignores_sigterm_is_gone_after_the_sweep(
    tmp_path, logger, log_stream
):
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child, grandchild, inner = _start(tmp_path, logger)
    try:
        assert os.getsid(grandchild) == grandchild != os.getsid(child.pid)

        survivors = child.terminate(1.0, reason="test", sweep_descendants=True)
        child.finish()

        # MUTATION: signal only the child's group and both are alive here.
        assert not _alive(grandchild), "SIGTERM was ignored; the sweep must SIGKILL it"
        assert not _alive(inner), "what the leftover started is in its session: gone too"
        assert survivors == ()
        # Not `os.kill(-1)`: a process the test started itself, and the test.
        assert bystander.poll() is None and _alive(bystander.pid)
        assert _alive(os.getpid())
        assert "stopped what the child left in other sessions" in log_stream.getvalue()
    finally:
        _kill(grandchild, inner, bystander.pid)
        bystander.wait(5)


def test_a_plain_terminate_leaves_a_foreign_session_grandchild_alive(tmp_path, logger):
    child, grandchild, inner = _start(tmp_path, logger)
    try:
        assert child.terminate(1.0, reason="test") == ()
        child.finish()

        # Opt-in: every caller that does not ask keeps today's behaviour.
        assert _alive(grandchild) and _alive(inner)
    finally:
        _kill(grandchild, inner)


def _entry(pid: int, ppid: int, pgid: int, sid: int, start: int, *, state: str = "S",
           name: str = "cmd") -> ProcEntry:
    return ProcEntry(pid=pid, ppid=ppid, pgid=pgid, sid=sid, starttime=start, state=state,
                     name=name)


#: Pid 100 is the runner (session 100); 200 the CLI it started in session
#: 200; 201 a helper in the CLI's group; 300 a Bash tool command in its own
#: session; 301 what that command started; 900 is unrelated.
TABLE = {
    e.pid: e
    for e in (
        _entry(1, 0, 1, 1, 1, name="tini"),
        _entry(100, 1, 100, 100, 10, name="python"),
        _entry(200, 100, 200, 200, 20, name="claude"),
        _entry(201, 200, 200, 200, 21, name="node"),
        _entry(300, 200, 300, 300, 30, name="bash"),
        _entry(301, 300, 300, 300, 31, name="pytest"),
        _entry(900, 1, 900, 900, 90, name="other"),
    )
}


def test_the_snapshot_is_the_descendants_by_parent_pid_with_their_ids():
    snapshot = descendants_of(200, TABLE)

    assert set(snapshot) == {201, 300, 301}
    assert snapshot[300] == TABLE[300]
    assert (snapshot[301].pgid, snapshot[301].sid, snapshot[301].starttime) == (300, 300, 31)


def test_the_targets_are_the_snapshot_its_groups_and_sessions_and_what_descends_from_them():
    snapshot = descendants_of(200, TABLE)
    # The CLI is dead: its children were re-parented to pid 1, and one of
    # them forked after the snapshot, inside the recorded group.
    later = {
        **{pid: e for pid, e in TABLE.items() if pid != 200},
        201: _entry(201, 1, 200, 200, 21),
        300: _entry(300, 1, 300, 300, 30),
        302: _entry(302, 300, 300, 300, 40),
        303: _entry(303, 302, 303, 303, 41),
    }

    targets = sweep_targets(snapshot, later, protected_pids={1, 100}, protected_groups={100})

    assert set(targets) == {201, 300, 301, 302, 303}


def test_a_reused_pid_is_not_signalled():
    snapshot = descendants_of(200, TABLE)
    # 301 exited and its pid went to an unrelated process in another
    # session; group 300 emptied and its id was reused by a new session leader.
    later = {
        1: TABLE[1],
        100: TABLE[100],
        300: _entry(300, 1, 300, 300, 77, name="reused"),
        301: _entry(301, 1, 900, 900, 78, name="reused"),
        900: TABLE[900],
    }

    assert sweep_targets(snapshot, later, protected_pids={1, 100}, protected_groups={100}) == {}


def test_a_zombie_and_a_protected_process_are_never_targets():
    snapshot = descendants_of(200, TABLE)
    later = {
        **TABLE,
        201: _entry(201, 1, 200, 200, 21, state="Z"),
        # In a recorded session, but this process's own group: never.
        400: _entry(400, 300, 100, 300, 50),
    }
    del later[200]

    targets = sweep_targets(snapshot, later, protected_pids={1, 100, 300},
                            protected_groups={100})

    assert set(targets) == {301}
