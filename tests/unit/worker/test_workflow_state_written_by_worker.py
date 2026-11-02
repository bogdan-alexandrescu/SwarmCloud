"""The worker's direct-Firestore path writes its workflow's derived state (#2838).

PR 2842 wrote `workflows/{id}.state` in the same transaction as the step
change on swarm-api's attempt-state routes, which the worker calls for a
tenant in `state_via_api_tenants`. Every other tenant's worker writes its
park, its finish and its retry straight to Firestore (`ControlPlane`), and
those left the workflow's stored state behind the derived one: stored RUNNING,
derived PARKED, repaired by the reconciler and wrong again on the next park.

Each test ends one attempt of a workflow step the way `ControlPlane` does and
checks that the stored state is what the steps derive. A FENCED attempt
(invariant 5) writes neither the step nor the workflow.

The runner refuses a read after a write inside one transaction, as Firestore
does: the derivation reads the workflow and its sibling steps, and the
release that follows reads the lease and its pools, so a body that put a
read after a write would pass on the in-memory fake and fail in production.
Offline: no credentials, no emulator.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import handoff as handoff_mod
from agent_worker.control import ControlPlane
from agent_worker.errors import FencedWriteRefused
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import ParkReason, TaskState
from swarm_rollup import derive, read_steps

from fakes import FakeFirestore, FakeTransaction, FakeTransactionRunner
from worker_seeds import TENANT, seed_attempt

WORKFLOW = "wf_worker_writes"
SIBLING = "task_sibling"


class _StrictTransaction(FakeTransaction):
    def __init__(self, db: FakeFirestore) -> None:
        super().__init__(db)
        self.wrote = False

    def get(self, ref: Any, **call_options: Any) -> Any:
        if self.wrote:
            raise AssertionError("a read after a write in one transaction: Firestore refuses it")
        return super().get(ref, **call_options)

    def set(self, ref: Any, data: dict[str, Any], merge: bool = False) -> None:
        self.wrote = True
        super().set(ref, data, merge=merge)

    def update(self, ref: Any, data: dict[str, Any]) -> None:
        self.wrote = True
        super().update(ref, data)


class StrictRunner(FakeTransactionRunner):
    """`FakeTransactionRunner`, all or nothing, with Firestore's read order."""

    def run(self, fn: Any, *, call_options: Any = None) -> Any:
        return super().run(lambda _txn: fn(_StrictTransaction(self._db)),
                           call_options=call_options)


def _control(db: FakeFirestore, *, generation: int = 1) -> ControlPlane:
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=generation,
        logger=build_logger(
            task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
            generation=generation, runner_profile="mock", stream=io.StringIO(),
        ),
        txn_runner=StrictRunner(db),
    )


def _seed(db: FakeFirestore, *, sibling: str = "SUCCEEDED", generation: int = 1,
          task_generation: int | None = None) -> None:
    """A two-step workflow: `task_1`, this attempt's step, RUNNING; and a
    sibling in `sibling`. The stored state starts equal to the derived one."""
    seed_attempt(db, state=TaskState.RUNNING, generation=generation,
                 task_generation=task_generation)
    db.documents["tasks/task_1"]["workflow_id"] = WORKFLOW
    db.seed(f"tasks/{SIBLING}", {"id": SIBLING, "tenant_id": TENANT, "state": sibling,
                                 "workflow_id": WORKFLOW})
    db.seed(f"workflows/{WORKFLOW}", {
        "workflow_id": WORKFLOW, "tenant_id": TENANT, "state": "RUNNING",
        "steps": [
            {"step_id": "sibling", "runner_profile": "mock", "depends_on": [],
             "task_id": SIBLING},
            {"step_id": "this", "runner_profile": "mock", "depends_on": [],
             "task_id": "task_1"},
        ],
    })
    assert _derived(db) == "RUNNING"


def _derived(db: FakeFirestore) -> str:
    steps = [SimpleNamespace(step_id=row["step_id"], task_id=row["task_id"])
             for row in db.doc(f"workflows/{WORKFLOW}")["steps"]]
    states = {s.task_id: TaskState(db.doc(f"tasks/{s.task_id}")["state"]) for s in steps}
    return derive(read_steps(steps, states)).state


def _stored(db: FakeFirestore) -> str:
    return db.doc(f"workflows/{WORKFLOW}")["state"]


def test_a_park_writes_the_workflow_parked():
    db = FakeFirestore()
    _seed(db)
    control = _control(db)

    control.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED, next_eligible_at=utcnow())

    assert db.doc("tasks/task_1")["state"] == "PARKED"
    assert _stored(db) == "PARKED" == _derived(db)


def test_a_ci_pending_park_writes_the_workflow_parked():
    db = FakeFirestore()
    _seed(db)
    control = _control(db)

    control.park_ci_pending(code="ci_pending", head="0" * 40, pull_request=7,
                            pending=["unit"], max_wakes=4, fallback_seconds=600)

    assert db.doc("tasks/task_1")["state"] == "PARKED"
    assert _stored(db) == "PARKED" == _derived(db)


@pytest.mark.parametrize(("state", "sibling", "expected"), [
    (TaskState.SUCCEEDED, "SUCCEEDED", "SUCCEEDED"),
    (TaskState.FAILED, "SUCCEEDED", "FAILED"),
    (TaskState.SUCCEEDED, "PARKED", "PARKED"),
])
def test_a_finish_writes_the_workflow_state(state, sibling, expected):
    db = FakeFirestore()
    _seed(db, sibling=sibling)
    control = _control(db)

    control.finish(state=state, exit_code=0 if state is TaskState.SUCCEEDED else 1,
                   error=None if state is TaskState.SUCCEEDED else "boom")

    assert db.doc("tasks/task_1")["state"] == state.value
    assert _stored(db) == expected == _derived(db)


def test_a_retryable_failure_writes_the_workflow_ready():
    db = FakeFirestore()
    _seed(db)
    control = _control(db)

    left = control.fail_retryably(exit_code=1, error="boom", cause="crash")

    assert left is TaskState.READY
    assert _stored(db) == "READY" == _derived(db)


def test_a_human_park_writes_the_workflow_parked():
    """`handoff.park_human_required` ends the attempt through the same
    `_end_in_transaction` without passing the task it read: the task is read
    again in the transaction, before its first write."""
    db = FakeFirestore()
    _seed(db)
    control = _control(db)

    handoff_mod.park_human_required(control, max_resumes=3,
                                    detail={"why": "a person must sign in"})

    assert db.doc("tasks/task_1")["state"] == "PARKED"
    assert _stored(db) == "PARKED" == _derived(db)


def test_a_fenced_park_writes_neither_the_step_nor_the_workflow():
    """Invariant 5: the reconciler has moved the task to generation 2."""
    db = FakeFirestore()
    _seed(db, generation=1, task_generation=2)
    control = _control(db, generation=1)
    db.writes.clear()

    with pytest.raises(FencedWriteRefused):
        control.park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow())

    assert db.writes == [], "a fenced park wrote something"
    assert db.doc("tasks/task_1")["state"] == "RUNNING"
    assert _stored(db) == "RUNNING"


def test_a_fenced_finish_writes_neither_the_step_nor_the_workflow():
    db = FakeFirestore()
    _seed(db, generation=1, task_generation=2)
    control = _control(db, generation=1)
    db.writes.clear()

    with pytest.raises(FencedWriteRefused):
        control.finish(state=TaskState.SUCCEEDED, exit_code=0)

    assert db.writes == []
    assert _stored(db) == "RUNNING"


def test_a_startup_transition_reads_no_workflow():
    """LEASED -> DISPATCHED -> STARTING -> RUNNING cannot move the derived
    state (`derive()` reports all four as RUNNING), so the startup window's
    transactions read nothing more than they did."""
    db = FakeFirestore()
    _seed(db)
    db.documents["tasks/task_1"]["state"] = "LEASED"
    db.documents[f"workflows/{WORKFLOW}"]["state"] = "QUEUED"
    control = _control(db)

    control.advance_to_running()

    assert db.doc("tasks/task_1")["state"] == "RUNNING"
    assert _stored(db) == "QUEUED", "a startup transition wrote the workflow"
