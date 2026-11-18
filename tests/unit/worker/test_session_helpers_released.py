"""A step ends within seconds of its agent, whatever the agent's helpers do.

WHY (owner, 2026-11-02). In the 2026-11-01 code intelligence batch, review
steps with the code graph switched on held their pod 6 to 31 minutes after the
agent's last turn, against under 10 seconds without it. The CLI's stdio tool
servers (`swarm-graph` among them) inherit its stderr, so a server that
outlived the CLI held the write end of the pipe the runner captures; the
reader never saw end-of-file, and `ChildProcess.finish` closed that pipe under
the blocked reader, which in CPython waits for the reader's lock: the step
waited exactly as long as the server lived.

These run real processes: a fake session that starts a "tool server" which
inherits its streams and sleeps, writes its last line, and exits.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from agent_worker.logs import build_logger
from agent_worker.procman import TRUNCATION_MARK, ChildProcess, run_child

#: How long a helper lives unless something stops it: far past every bound here.
HELPER_SECONDS = 120
#: What "within seconds of the agent's last turn" means in these tests.
PROMPT_SECONDS = 8.0


def _session(pid_file: Path, *, ignore_term: bool = False, escape: bool = False,
             pad_bytes: int = 0) -> str:
    """A fake agent session: start a tool server that keeps our streams, answer, exit."""
    server = (
        "import os, signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        + f"time.sleep({HELPER_SECONDS})\n"
    )
    return (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {server!r}], "
        f"start_new_session={escape!r})\n"
        f"while not __import__('os').path.exists({str(pid_file)!r}):\n"
        "    time.sleep(0.02)\n"
        f"sys.stdout.write('x' * {pad_bytes})\n"
        "sys.stdout.write('\\n{\"type\": \"result\", \"result\": \"done\"}\\n')\n"
        "sys.stdout.flush()\n"
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is dead for every purpose here.
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            return not any(line.startswith("State:") and "Z" in line.split()[1] for line in fh)
    except OSError:
        return False


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def logger(log_stream):
    return build_logger(
        task_id="t", attempt_id="a", tenant_id="eng", generation=1,
        runner_profile="mock", stream=log_stream,
    )


@pytest.fixture
def helper_pid(tmp_path):
    """The helper's pid file; whatever it names is killed after the test."""
    pid_file = tmp_path / "helper.pid"
    yield pid_file
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def _run(tmp_path: Path, logger, script: str, *, limit: int = 1 << 20):
    return run_child(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=60,
        grace_seconds=2,
        max_stdout_bytes=limit,
        max_stderr_bytes=limit,
        logger=logger,
        keep_tail=True,
    )


def test_a_tool_server_that_outlives_the_session_does_not_hold_the_step(
        tmp_path, logger, helper_pid):
    started = time.monotonic()
    result = _run(tmp_path, logger, _session(helper_pid))
    assert time.monotonic() - started < PROMPT_SECONDS
    assert result.exit_code == 0
    assert b'"type": "result"' in (tmp_path / "out.log").read_bytes()


def test_the_tool_server_is_stopped_when_the_session_ends(tmp_path, logger, helper_pid):
    started = time.monotonic()
    _run(tmp_path, logger, _session(helper_pid))
    assert _wait_dead(int(helper_pid.read_text())), "the session's tool server is still running"
    assert time.monotonic() - started < PROMPT_SECONDS


def test_a_tool_server_that_ignores_sigterm_is_killed(tmp_path, logger, helper_pid):
    started = time.monotonic()
    _run(tmp_path, logger, _session(helper_pid, ignore_term=True))
    assert time.monotonic() - started < PROMPT_SECONDS
    assert _wait_dead(int(helper_pid.read_text()))


def test_the_kept_end_of_a_capped_stream_is_written_with_the_helper_alive(
        tmp_path, logger, helper_pid):
    # The capped capture writes its kept end when the stream closes; a stream
    # that never closed lost the `result` line, the last one.
    _run(tmp_path, logger, _session(helper_pid, pad_bytes=200_000), limit=64 * 1024)
    out = (tmp_path / "out.log").read_bytes()
    assert TRUNCATION_MARK in out
    assert out.rstrip().endswith(b'{"type": "result", "result": "done"}')


def test_a_helper_outside_the_group_never_blocks_the_end(tmp_path, logger, helper_pid):
    # A helper that left the group (`setsid`) is out of the group signal's
    # reach; it still holds the pipe, and the step still ends promptly with
    # the session's whole output. The pre-publish reap is what stops it.
    started = time.monotonic()
    result = _run(tmp_path, logger, _session(helper_pid, escape=True, pad_bytes=200_000),
                  limit=64 * 1024)
    assert time.monotonic() - started < PROMPT_SECONDS
    assert result.exit_code == 0
    assert (tmp_path / "out.log").read_bytes().rstrip().endswith(
        b'{"type": "result", "result": "done"}')


def test_finish_after_a_terminate_also_stops_the_group(tmp_path, logger, helper_pid):
    # A session stopped by the runner (budget, swap, deadline) whose CLI died
    # on SIGTERM but whose helper ignored it: the helper is killed too.
    child = ChildProcess(
        [sys.executable, "-c", _session(helper_pid, ignore_term=True) + "time.sleep(60)\n"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        max_stdout_bytes=1 << 20,
        max_stderr_bytes=1 << 20,
        logger=logger,
    )
    child.start()
    while not helper_pid.exists():
        time.sleep(0.02)
    started = time.monotonic()
    child.terminate(2, reason="test")
    child.finish()
    assert time.monotonic() - started < PROMPT_SECONDS
    assert _wait_dead(int(helper_pid.read_text()))
