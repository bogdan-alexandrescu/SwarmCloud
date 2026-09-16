"""Child processes: the timeout must actually kill things.

An agent that ignores SIGTERM and a worker that politely waits for it is how a
"10 minute" task occupies a slot for an hour. These tests use a child that
deliberately ignores SIGTERM, because a child that exits on the first signal
proves nothing about escalation.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from agent_worker.procman import ChildProcess, ProcessError, run_child, validate_argv
from swarm_common.states import TaskState

from conftest import seed_attempt

IGNORES_SIGTERM = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('started', flush=True)\n"
    "time.sleep(300)\n"
)

HANDLES_SIGTERM = (
    "import signal, sys, time\n"
    "stop = False\n"
    "def handler(signum, frame):\n"
    "    global stop\n"
    "    stop = True\n"
    "signal.signal(signal.SIGTERM, handler)\n"
    "print('started', flush=True)\n"
    "while not stop:\n"
    "    time.sleep(0.05)\n"
    "print('clean exit', flush=True)\n"
    "sys.exit(0)\n"
)


@pytest.fixture
def logger(log_stream):
    return build_logger(
        task_id="t", attempt_id="a", tenant_id="eng", generation=1,
        runner_profile="mock", stream=log_stream,
    )


def test_timeout_escalates_sigterm_to_sigkill(tmp_path, logger):
    started = time.monotonic()
    result = run_child(
        [sys.executable, "-c", IGNORES_SIGTERM],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=1.0,
        grace_seconds=1.0,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.killed is True, "SIGTERM was ignored; SIGKILL must follow"
    assert result.exit_code == -9, "exit code must show death by SIGKILL"
    assert result.term_signal == 9
    # timeout + grace, and not a second longer than that plus reaping.
    assert 2.0 <= elapsed < 8.0
    assert "started" in (tmp_path / "out.log").read_text()


def test_sigterm_is_tried_before_sigkill(tmp_path, logger):
    result = run_child(
        [sys.executable, "-c", HANDLES_SIGTERM],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=1.0,
        grace_seconds=10.0,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    assert result.timed_out is True
    assert result.killed is False, "a child that honours SIGTERM must not be killed"
    assert result.exit_code == 0
    assert "clean exit" in (tmp_path / "out.log").read_text()


def test_the_whole_process_group_dies(tmp_path, logger):
    """A runner's grandchildren must not outlive it holding the CPU."""
    marker = tmp_path / "grandchild.pid"
    program = (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(300)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(300)\n"
    )
    run_child(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=1.5,
        grace_seconds=1.0,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except (ProcessLookupError, PermissionError):
            alive = False
            break
        time.sleep(0.1)
    assert alive is False, "the grandchild survived; killpg did not reach the process group"


def test_no_zombie_is_left_behind(tmp_path, logger):
    child = ChildProcess(
        [sys.executable, "-c", "print('bye')"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    child.start()
    pid = child.pid
    child.wait(20)
    child.finish()
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_output_cap_truncates_without_blocking_the_child(tmp_path, logger):
    """The reader keeps draining past the cap; stopping would deadlock at 64KB."""
    result = run_child(
        [sys.executable, "-c", "import sys;[sys.stdout.write('x'*4096) for _ in range(2000)]"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=30,
        grace_seconds=5,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=4096,
        logger=logger,
    )
    assert result.exit_code == 0, "the child completed rather than blocking on a full pipe"
    assert result.stdout_truncated is True
    assert result.stdout_bytes == 64 * 1024
    assert (tmp_path / "out.log").stat().st_size < 80 * 1024


def test_argv_must_be_a_list_never_a_command_string():
    with pytest.raises(ProcessError, match="never uses a shell"):
        validate_argv("rm -rf / # a string would need a shell")
    with pytest.raises(ProcessError):
        validate_argv([])
    assert validate_argv(["echo", "hello world; rm -rf /"]) == ["echo", "hello world; rm -rf /"]


def test_worker_timeout_fails_the_task_and_releases_the_lease(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "slow", "steps": 50, "sleep_seconds": 30.0})
    worker, _, _ = worker_factory(timeout_seconds=2, termination_grace_seconds=2)

    assert worker.run() == ExitCode.TIMEOUT
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert "timeout" in task["last_error"]
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 0
