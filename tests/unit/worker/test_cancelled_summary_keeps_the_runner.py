"""A cancelled task's result_summary carries the runner's own report (#361).

The cancel path in `_apply_control_signals` uploaded the outputs and finished
the task without the `runner` block `_finalise` assembles, so a cancelled task
lost the one record of how far the stopped run got: the steps the runner
reported and what it said about itself. The mock writes its result file on
SIGTERM ("terminated"), so the block is there to write.

Driven through the real lifecycle over the in-memory Firestore, like
test_end_cause_worker.py. WRITTEN TO FAIL ON THE CODE BEFORE THE CHANGE: the
summary had no `runner` key at all.
"""

from __future__ import annotations

import threading
import time

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt


def test_a_cancelled_task_keeps_the_runner_block(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "cancel me", "steps": 40, "sleep_seconds": 8.0})
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def cancel() -> None:
        time.sleep(1.5)
        db.doc("tasks/task_1")["cancel_requested"] = True

    thread = threading.Thread(target=cancel)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.CANCELLED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value
    summary = task["result_summary"]
    assert "runner" in summary, (
        "the cancelled task's result_summary has no runner block, though the "
        f"runner wrote its result file; keys: {sorted(summary)}"
    )
    runner = summary["runner"]
    assert runner["status"] == "terminated", runner
    output = runner["output"]
    assert output["requested_steps"] == 40
    assert 0 <= output["completed_steps"] < 40, output
    assert "usage" in runner
