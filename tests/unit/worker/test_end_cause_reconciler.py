"""The reconciler records WHY it ended a task, beside when (contract request 23).

Accepted by the owner on 2026-09-25 (#185, decision 9). The reconciler ends a
task in `ControlStore.repair_task_state`, inside the transaction that also
decides the terminal state, and three causes come out of it:

    a worker that exited 78 (CANNOT-START)        cannot_start
    a requeue on the last attempt                  lost_worker
    either one, with a cancel requested            cancel_requested

Driven through the real `Reconciler` and `ControlStore` with the scenarios
`test_worker_cannot_start.py` builds, so the finding, the fence, the release
and the repair are all the shipped code's.

WRITTEN TO FAIL ON THE CODE BEFORE THE CHANGE, on its assertions: nothing new is
imported, and until then the repair wrote no `end_cause`.
"""

from __future__ import annotations

import pytest

from reconciler.config import ReconcilerConfig
from swarm_common.states import TaskState

from test_worker_cannot_start import (
    DNS_TERMINATION_MESSAGE,
    EXECUTION,
    LEASE,
    TASK,
    ended,
    finished_execution,
    reconcile,
    seed_an_attempt_that_never_started,
)


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def _ended_with(db, state: TaskState, cause: str) -> None:
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == state.value, task
    assert task["completed_at"] is not None
    assert task.get("end_cause") == cause, (
        f"the reconciler ended the task {state.value} with end_cause={task.get('end_cause')!r}; "
        f"it knew it was {cause!r}"
    )
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None


def test_a_worker_that_could_not_start_is_cannot_start(db, config):
    seed_an_attempt_that_never_started(db, age_seconds=60, attempt_count=1, max_attempts=3)
    reconcile(db, config, executions=[finished_execution()],
              terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)})
    _ended_with(db, TaskState.FAILED, "cannot_start")


def test_a_requeue_on_the_last_attempt_is_a_lost_worker(db, config):
    """Past the dispatch deadline, with every attempt spent: the requeue the
    finding asks for is downgraded to FAILED, and the worker was lost."""
    seed_an_attempt_that_never_started(db, age_seconds=400, attempt_count=3, max_attempts=3)
    reconcile(db, config, executions=[finished_execution()], terminations={EXECUTION: ended(1)})
    _ended_with(db, TaskState.FAILED, "lost_worker")
    assert db.doc(f"tasks/{TASK}")["last_error"].startswith("reconciled: ")


def test_a_requeue_with_attempts_left_ends_nothing_and_records_no_cause(db, config):
    seed_an_attempt_that_never_started(db, age_seconds=400, attempt_count=1, max_attempts=3)
    reconcile(db, config, executions=[finished_execution()], terminations={EXECUTION: ended(1)})
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value
    assert task.get("end_cause") is None


@pytest.mark.parametrize("exit_code, age", [(78, 60), (1, 400)], ids=["cannot-start", "requeue"])
def test_a_requested_cancel_is_ended_as_requested_whatever_the_finding(db, config, exit_code, age):
    seed_an_attempt_that_never_started(db, age_seconds=age, cancel_requested=True)
    reconcile(
        db, config, executions=[finished_execution()],
        terminations={EXECUTION: ended(exit_code, DNS_TERMINATION_MESSAGE if exit_code == 78 else None)},
    )
    _ended_with(db, TaskState.CANCELLED, "cancel_requested")
