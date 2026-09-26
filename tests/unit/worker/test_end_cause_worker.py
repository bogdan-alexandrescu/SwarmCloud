"""The worker records WHY it ended a task, beside when (contract request 23).

Accepted by the owner on 2026-09-25 (#185, decision 9). Until then the outcome
ledger learned why a task failed by reading the worker's `last_error` text, and
10 of dev's 13 "runner errors" were in fact the worker refusing to stage a
declared input (`InputUnavailable`) before the agent ever started (decision 4).

Every terminal write the worker makes is driven here through the real
lifecycle, over the in-memory Firestore, and the task document is read back:

    success                          end_cause written, and None
    the runner failed                runner_error
    the runner ran out of time       timeout
    a declared input not staged      inputs_unavailable
    expected outputs never written   outputs_missing (on the last attempt)
    cancelled before the start       cancel_requested
    cancelled while running          cancel_requested
    cancelled as the attempt ended   cancel_requested (the retryable path)

WRITTEN TO FAIL ON THE CODE BEFORE THE CHANGE, on its assertions: nothing new is
imported at module level, and until then no terminal write carried the field.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from agent_worker import lifecycle as lifecycle_mod
from agent_worker.errors import ConfigError, ExitCode, InputUnavailable, WorkerError
from swarm_common.states import TaskState

from conftest import seed_attempt


def _task(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")


def _ended(db: Any, state: TaskState, cause: str | None) -> None:
    task = _task(db)
    assert task["state"] == state.value, task["state"]
    assert task["completed_at"] is not None
    assert "end_cause" in task, "the terminal write recorded no end cause at all"
    assert task["end_cause"] == cause, (
        f"the task ended {state.value} with end_cause={task['end_cause']!r}; the worker knew it was {cause!r}"
    )
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_a_success_records_that_there_was_no_cause(db, worker_factory):
    """Written as None, not left out: the document says what THIS write decided."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    _ended(db, TaskState.SUCCEEDED, None)


def test_a_runner_that_failed_is_a_runner_error(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01,
                                 "fail": True, "fail_message": "the agent gave up"})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.FAILED
    _ended(db, TaskState.FAILED, "runner_error")


def test_a_runner_that_ran_out_of_time_is_a_timeout(db, worker_factory):
    """The ledger could only find this by its text, because the worker records
    the killed child's status and never 76."""
    seed_attempt(db, task_input={"prompt": "slow", "steps": 50, "sleep_seconds": 30.0})
    worker, _, _ = worker_factory(timeout_seconds=2, termination_grace_seconds=2)
    assert worker.run() == ExitCode.TIMEOUT
    _ended(db, TaskState.FAILED, "timeout")


def test_an_input_the_worker_would_not_stage_is_inputs_unavailable(db, worker_factory):
    """Decision 4: not a runner error. The agent never started."""
    seed_attempt(db, task_input={"prompt": "use it", "steps": 1, "sleep_seconds": 0.01})
    _task(db)["metadata"] = {"input_from": {"task_gone": "summary.md"}}
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.FAILED
    assert "task_gone" in _task(db)["last_error"]
    _ended(db, TaskState.FAILED, "inputs_unavailable")


def test_the_last_attempt_without_its_expected_outputs_is_outputs_missing(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "write the notes", "steps": 1, "sleep_seconds": 0.01,
                                 "artifact_name": "notes.md", "artifact_text": "the notes\n"})
    _task(db)["metadata"] = {"expected_outputs": ["scan-01.md"]}
    _task(db)["attempt_count"] = 3
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.FAILED
    _ended(db, TaskState.FAILED, "outputs_missing")


def test_an_attempt_that_will_be_retried_ends_nothing_and_records_no_cause(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "write the notes", "steps": 1, "sleep_seconds": 0.01,
                                 "artifact_name": "notes.md", "artifact_text": "the notes\n"})
    _task(db)["metadata"] = {"expected_outputs": ["scan-01.md"]}
    worker, _, _ = worker_factory()
    worker.run()
    task = _task(db)
    assert task["state"] == TaskState.READY.value
    assert task.get("end_cause") is None


def test_a_cancel_before_the_start_is_requested(db, worker_factory):
    seed_attempt(db, cancel_requested=True)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.CANCELLED
    _ended(db, TaskState.CANCELLED, "cancel_requested")


def test_a_cancel_while_running_is_requested(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "cancel me", "steps": 40, "sleep_seconds": 8.0})
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def cancel() -> None:
        time.sleep(1.5)
        _task(db)["cancel_requested"] = True

    thread = threading.Thread(target=cancel)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.CANCELLED
    _ended(db, TaskState.CANCELLED, "cancel_requested")


def test_a_cancel_that_lands_as_the_attempt_ends_is_requested(db, worker_factory, monkeypatch):
    """The retryable path (`control.fail_retryably`) picks CANCELLED from the flag
    it re-reads, so its cause is the request's, whatever the attempt failed of."""
    seed_attempt(db, task_input={"prompt": "write the notes", "steps": 1, "sleep_seconds": 0.01,
                                 "artifact_name": "notes.md", "artifact_text": "the notes\n"})
    _task(db)["metadata"] = {"expected_outputs": ["scan-01.md"]}
    worker, _, _ = worker_factory()
    upload = worker._upload_outputs

    def cancel_then_upload(**kwargs: Any) -> dict[str, Any]:
        _task(db)["cancel_requested"] = True
        return upload(**kwargs)

    monkeypatch.setattr(worker, "_upload_outputs", cancel_then_upload)
    worker.run()
    _ended(db, TaskState.CANCELLED, "cancel_requested")


def test_a_deliberate_worker_failure_is_named_by_what_it_was():
    """The crash path's mapping (`run`'s `except WorkerError`): an input refusal,
    the worker's CANNOT-START (exit 78), and everything else, which the text
    classifier has always called the runner's error."""
    end_cause_of = getattr(lifecycle_mod, "_end_cause_of", None)
    assert end_cause_of is not None, "the lifecycle does not name the cause of a WorkerError"
    assert end_cause_of(InputUnavailable("upstream task t did not produce an artifact named 'x'")).value == (
        "inputs_unavailable"
    )
    assert end_cause_of(ConfigError("no TASK_ID")).value == "cannot_start"
    assert end_cause_of(WorkerError("the workspace is not clean")).value == "runner_error"
