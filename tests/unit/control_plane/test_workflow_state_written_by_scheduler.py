"""The scheduler writes a workflow's derived state where it moves a step (#2838).

PR 2842 wrote `workflows/{id}.state` in the same transaction as every step
change swarm-api makes. The scheduler's own transitions were left out: a step
admitted (READY -> LEASED), a park woken (PARKED -> READY: a dependency met, a
CI_PENDING merge step released), the `fail_workflow` sweep's cancels and a
failed dispatch's return. Each left the stored state behind the derived one
until the reconciler's `state_drift` repair, which then found the same
workflows again on its next pass.

Each test makes one transition the way the scheduler makes it, over the
in-memory Firestore, and then checks the stored state twice: against the value
the steps derive, and through the RECONCILER's own check
(`reconciler.detect.detect_stalled_workflows`), which must find no drift. A
guard that refuses the step's write must write neither.

The transaction is held to Firestore's rule that every read comes before the
first write (`_reads_before_writes`): the workflow's derivation reads the
workflow and its sibling steps, and a body that read them after its first
write would pass on the fake and fail on Firestore. Offline: no credentials,
no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from reconciler.config import ReconcilerConfig
from reconciler.detect import WorkflowStallKind, detect_stalled_workflows
from reconciler.model import StepTaskView, WorkflowRead, WorkflowView
from scheduler.children import cancel_child
from scheduler.codec import task_from_dict
from scheduler.store import SchedulerStore
from swarm_common.admission import AdmissionConfig, AdmissionDenied
from swarm_common.states import ParkReason, TaskState
from swarm_rollup import derive, read_steps

from . import fakes
from .conftest import auth_header, seed_pool, seed_task

FAIL = "fail_workflow"


@pytest.fixture(autouse=True)
def _reads_before_writes(monkeypatch):
    """A read after a write in one transaction raises, as Firestore's does."""
    real_get = fakes.FakeTransaction.get

    def get(self, ref, **kwargs):
        if self._buffer:
            raise AssertionError("a read after a write in one transaction: Firestore refuses it")
        return real_get(self, ref, **kwargs)

    monkeypatch.setattr(fakes.FakeTransaction, "get", get)


def step(step_id: str, *depends_on: str) -> dict[str, Any]:
    body: dict[str, Any] = {"step_id": step_id, "runner_profile": "mock"}
    if depends_on:
        body["depends_on"] = list(depends_on)
    return body


def submit(client, steps: list[dict[str, Any]]) -> tuple[str, dict[str, str]]:
    created = client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={"on_step_failure": FAIL, "steps": steps},
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    return workflow["workflow_id"], {s["step_id"]: s["task_id"] for s in workflow["steps"]}


def derived(db, workflow_id: str) -> str:
    """What the steps derive, from the stored documents, by `swarm_rollup.derive`."""
    doc = db.docs[f"workflows/{workflow_id}"]
    view = WorkflowView.from_doc(doc, workflow_id)
    states = {
        s.task_id: TaskState(db.docs[f"tasks/{s.task_id}"]["state"])
        for s in view.steps if s.task_id and f"tasks/{s.task_id}" in db.docs
    }
    return derive(read_steps(view.steps, states)).state


def drift(db, workflow_id: str) -> list[Any]:
    """What the reconciler's workflow check finds as `state_drift` right now."""
    doc = db.docs[f"workflows/{workflow_id}"]
    view = WorkflowView.from_doc(doc, workflow_id)
    tasks = {
        s.task_id: StepTaskView.from_doc(db.docs[f"tasks/{s.task_id}"], s.task_id)
        for s in view.steps if s.task_id and f"tasks/{s.task_id}" in db.docs
    }
    config = ReconcilerConfig(project_id="test-project", region="test-region",
                              firestore_database="(default)")
    stalls = detect_stalled_workflows(WorkflowRead(workflows=[view], tasks=tasks), {}, config,
                                      datetime.now(timezone.utc))
    return [s for s in stalls if s.kind is WorkflowStallKind.STATE_DRIFT]


def stored(db, workflow_id: str) -> str:
    return db.docs[f"workflows/{workflow_id}"]["state"]


def in_sync(db, workflow_id: str) -> None:
    """Start a test from a workflow whose stored state is already right, so the
    only thing that can move it is the transition under test."""
    db.docs[f"workflows/{workflow_id}"]["state"] = derived(db, workflow_id)


def set_state(db, task_id: str, state: str, **fields: Any) -> None:
    db.docs[f"tasks/{task_id}"].update({"state": state, **fields})


def task(db, task_id: str):
    return task_from_dict({**db.docs[f"tasks/{task_id}"], "id": task_id})


# --------------------------------------------------------------------------
# READY -> LEASED
# --------------------------------------------------------------------------

def test_admission_writes_the_workflow_running(client, db, make_scheduler) -> None:
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(client, [step("first"), step("second", "first")])
    make_scheduler().drain()  # promotes `first`; `second` parks on its dependency
    set_state(db, steps["first"], "READY", current_lease_id=None)
    in_sync(db, workflow_id)
    assert stored(db, workflow_id) == "READY"

    make_scheduler().drain()

    assert db.docs[f"tasks/{steps['first']}"]["state"] in ("LEASED", "DISPATCHED")
    assert stored(db, workflow_id) == "RUNNING" == derived(db, workflow_id)
    assert drift(db, workflow_id) == []


def test_a_denied_admission_writes_no_workflow_state(client, db) -> None:
    """The step is not READY when admission reads it: neither document moves."""
    workflow_id, steps = submit(client, [step("only")])
    set_state(db, steps["only"], "READY")
    stale = task(db, steps["only"])
    set_state(db, steps["only"], "PARKED", park_reason="MANUAL_PAUSE")
    db.docs[f"workflows/{workflow_id}"]["state"] = "QUEUED"
    before = dict(db.docs[f"workflows/{workflow_id}"])

    with pytest.raises(AdmissionDenied):
        SchedulerStore(db).acquire_lease(
            stale, units=1, backend="cloud_run", config=AdmissionConfig()
        )

    assert db.docs[f"workflows/{workflow_id}"] == before
    assert db.docs[f"tasks/{steps['only']}"]["state"] == "PARKED"


# --------------------------------------------------------------------------
# PARKED -> READY: a dependency met, a CI_PENDING step woken
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [ParkReason.DEPENDENCY_INCOMPLETE, ParkReason.CI_PENDING])
def test_a_woken_park_writes_the_workflow_ready(client, db, reason) -> None:
    workflow_id, steps = submit(client, [step("first"), step("second", "first")])
    set_state(db, steps["first"], "SUCCEEDED", completed_at=datetime.now(timezone.utc))
    set_state(db, steps["second"], "PARKED", park_reason=reason.value)
    in_sync(db, workflow_id)
    assert stored(db, workflow_id) == "PARKED"

    outcome = SchedulerStore(db).promote_to_ready(task(db, steps["second"]))

    assert outcome.applied, outcome
    assert stored(db, workflow_id) == "READY" == derived(db, workflow_id)
    assert drift(db, workflow_id) == []


def test_a_refused_wake_writes_no_workflow_state(client, db) -> None:
    """The guard finds the step no longer PARKED: neither document moves."""
    workflow_id, steps = submit(client, [step("first"), step("second", "first")])
    set_state(db, steps["first"], "SUCCEEDED")
    set_state(db, steps["second"], "PARKED", park_reason="DEPENDENCY_INCOMPLETE")
    stale = task(db, steps["second"])
    set_state(db, steps["second"], "CANCELLED", park_reason=None)
    db.docs[f"workflows/{workflow_id}"]["state"] = "PARKED"
    before = dict(db.docs[f"workflows/{workflow_id}"])

    outcome = SchedulerStore(db).promote_to_ready(stale)

    assert not outcome.applied
    assert db.docs[f"workflows/{workflow_id}"] == before


# --------------------------------------------------------------------------
# the fail_workflow cancels
# --------------------------------------------------------------------------

def test_the_fail_workflow_cancels_write_the_workflow_failed(client, db, make_scheduler) -> None:
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(
        client, [step("broken"), step("waiting"), step("after_broken", "broken")]
    )
    set_state(db, steps["broken"], "FAILED", completed_at=datetime.now(timezone.utc))
    set_state(db, steps["waiting"], "READY")
    set_state(db, steps["after_broken"], "PARKED", park_reason="DEPENDENCY_INCOMPLETE")
    in_sync(db, workflow_id)
    assert stored(db, workflow_id) == "READY"

    make_scheduler().drain()

    assert db.docs[f"tasks/{steps['waiting']}"]["state"] == "CANCELLED"
    assert db.docs[f"tasks/{steps['after_broken']}"]["state"] == "CANCELLED"
    assert stored(db, workflow_id) == "FAILED" == derived(db, workflow_id)
    assert drift(db, workflow_id) == []


def test_a_cancel_of_a_step_that_started_writes_no_workflow_state(client, db) -> None:
    """`cancel_if_not_started` finds the step LEASED: neither document moves."""
    workflow_id, steps = submit(client, [step("broken"), step("waiting")])
    set_state(db, steps["broken"], "FAILED")
    set_state(db, steps["waiting"], "READY")
    stale = task(db, steps["waiting"])
    set_state(db, steps["waiting"], "LEASED", current_lease_id="lease_x")
    db.docs[f"workflows/{workflow_id}"]["state"] = "RUNNING"
    before = dict(db.docs[f"workflows/{workflow_id}"])

    outcome = SchedulerStore(db).cancel_if_not_started(stale, "workflow failed")

    assert not outcome.applied
    assert db.docs[f"workflows/{workflow_id}"] == before


# --------------------------------------------------------------------------
# a failed dispatch's return, and a child cascade's cancel
# --------------------------------------------------------------------------

def test_a_failed_dispatch_on_its_last_attempt_writes_the_workflow_failed(
    client, db, make_scheduler, dispatcher
) -> None:
    """LEASED -> FAILED in `return_to_ready_after_failed_dispatch`, whose
    workflow read must come before the lease release's writes."""
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(client, [step("only")])
    set_state(db, steps["only"], "READY", attempt_count=0, max_attempts=1)
    in_sync(db, workflow_id)
    assert stored(db, workflow_id) == "READY"
    dispatcher.fail_for.add(steps["only"])

    make_scheduler().drain()

    assert db.docs[f"tasks/{steps['only']}"]["state"] == "FAILED"
    assert stored(db, workflow_id) == "FAILED" == derived(db, workflow_id)
    assert drift(db, workflow_id) == []


def test_a_cascaded_child_cancel_writes_its_workflow_state(db) -> None:
    now = datetime.now(timezone.utc)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="FAILED")
    seed_task(db, task_id="task_child", tenant_id="eng", state="READY", workflow_id="wf_child")
    db.docs["tasks/task_child"]["parent_task_id"] = "task_parent"
    db.docs["workflows/wf_child"] = {
        "workflow_id": "wf_child", "tenant_id": "eng", "state": "READY",
        "steps": [{"step_id": "child", "runner_profile": "mock", "task_id": "task_child"}],
        "created_at": now - timedelta(minutes=1), "updated_at": now,
    }

    assert cancel_child(db, "task_child", tenant_id="eng", parent_task_id="task_parent",
                        why="parent failed", now=now) == "cancelled"

    assert stored(db, "wf_child") == "CANCELLED" == derived(db, "wf_child")


def test_swarm_api_and_the_scheduler_write_by_one_function() -> None:
    """Moved beside `derive()`, re-exported under PR 2842's name: one rule."""
    import scheduler.store
    import swarm_api.rollup
    import swarm_rollup

    assert swarm_api.rollup.derived_state_write is swarm_rollup.derived_state_write
    assert scheduler.store.derived_state_write is swarm_rollup.derived_state_write
