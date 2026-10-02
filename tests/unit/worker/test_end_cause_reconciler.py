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


# -- contract request 41: a child its parent's cascade flagged ----------------


def _flag_as_cascaded_child(db) -> None:
    """What the child cascade (the API's cancel, the scheduler's sweeps) leaves
    on a child that held capacity: `cancel_requested` and the marker, and the
    child's worker still to finish it -- here, a worker that died first."""
    task = dict(db.doc(f"tasks/{TASK}"))
    task["parent_task_id"] = "task_parent"
    task["metadata"] = {"child_cascade": {"why": "parent_cancelled", "parent_task_id": "task_parent"}}
    db.seed(f"tasks/{TASK}", task)


@pytest.mark.parametrize("exit_code, age", [(78, 60), (1, 400)], ids=["cannot-start", "requeue"])
def test_a_flagged_child_whose_worker_died_ends_child_cascade(db, config, exit_code, age):
    """The reconciler's repair is one of request 41's writers: a child the
    cascade flagged ends `child_cascade`, not `cancel_requested`, when its
    worker is gone before it could end the child itself."""
    seed_an_attempt_that_never_started(db, age_seconds=age, cancel_requested=True)
    _flag_as_cascaded_child(db)
    reconcile(
        db, config, executions=[finished_execution()],
        terminations={EXECUTION: ended(exit_code, DNS_TERMINATION_MESSAGE if exit_code == 78 else None)},
    )
    _ended_with(db, TaskState.CANCELLED, "child_cascade")


def test_the_reconcilers_cascade_rule_is_the_workers_and_the_apis():
    """The key is restated in the reconciler (it carries neither swarm-api nor
    the worker); this holds the restatement and the rule equal to theirs."""
    from agent_worker import control as worker_control
    from reconciler import model as reconciler_model
    from scheduler import children as scheduler_children
    from swarm_api import validation as api_validation
    from swarm_common.models import EndCause

    assert reconciler_model.CHILD_CASCADE_METADATA_KEY == api_validation.CHILD_CASCADE_METADATA_KEY
    assert reconciler_model.CHILD_CASCADE_METADATA_KEY == scheduler_children.CHILD_CASCADE_METADATA_KEY
    for metadata in ({}, None, {"child_cascade": None}, {"child_cascade": {"why": "parent_ended"}}):
        expected = worker_control.cancel_end_cause({"metadata": metadata})
        assert reconciler_model.cancel_end_cause(metadata) is expected
        assert scheduler_children.cancel_end_cause(metadata) is expected
    assert reconciler_model.cancel_end_cause({"child_cascade": {"why": "x"}}) is EndCause.CHILD_CASCADE
