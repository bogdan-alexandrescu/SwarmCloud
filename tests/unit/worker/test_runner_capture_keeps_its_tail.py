"""The worker's capture of its RUNNER keeps the end of each stream (#208).

#188 gave `StreamCapture` a `keep_tail` mode and opted the agent CLI's own
streams into it. The worker's capture of the runner process kept the default,
head only. A runner's stderr ends with the line it failed on, and when the
runner writes no `error` into `result.json`, `last_error` is the last 2,000
characters of that file. Past `max_stderr_bytes` the file ended with the
truncation notice instead, and `last_error` quoted whatever the runner wrote
just before the cap.

Head-only is right for git's captures -- a patch with its middle removed must
never read as one that fitted -- and that reason does not apply to a log.
"""

from __future__ import annotations

import sys

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt

FAILURE_LINE = "FATAL: the line the runner actually failed on"

#: 64 KiB of noise on stderr, then the failure line, then a non-zero exit with
#: no result.json -- so `last_error` comes from the end of the capture.
RUNNER = (
    "import sys\n"
    "for n in range(2048):\n"
    "    sys.stderr.write(f'noise line {n:05d} ' + '.' * 13 + '\\n')\n"
    f"sys.stderr.write({FAILURE_LINE!r} + '\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(1)\n"
)


def test_last_error_quotes_the_runners_last_line_when_stderr_passed_its_cap(
    db, worker_factory, monkeypatch
):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    monkeypatch.setattr(lifecycle, "_runner_argv", lambda cfg: [sys.executable, "-c", RUNNER])
    worker, _, _ = worker_factory(max_stderr_bytes=16 * 1024)

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert FAILURE_LINE in task["last_error"], task["last_error"]


def test_the_runner_capture_is_built_to_keep_its_tail(db, worker_factory, monkeypatch):
    """The call site itself, so a later refactor that drops the argument fails
    here even if no runner in the suite writes past its cap."""
    seen: list[bool] = []
    real = lifecycle.ChildProcess

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            seen.append(bool(kwargs.get("keep_tail")))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "ChildProcess", Recording)
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    assert seen == [True], seen
