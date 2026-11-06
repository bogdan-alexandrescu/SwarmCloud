"""A workflow's stored state is written where its steps change state (#2838).

Before this, `workflows/{id}.state` was written only by the read paths, the
API's rollup sweep and the reconciler's `state_drift` repair. A step that
parked, woke, finished or was appended left the stored value behind the
derived one, and the reconciler found and repaired the same workflows on every
pass (wf_c72f0006b01a4cd99a02: stored RUNNING, derived PARKED).

Each test makes one transition the way production makes it -- the worker's
state routes over the in-memory Firestore, mergewake's round append and its
release wake -- and then asks the RECONCILER's own check
(`reconciler.detect.detect_stalled_workflows`) whether it would find drift.
It must find none. Offline: no credentials, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from reconciler.config import ReconcilerConfig
from reconciler.detect import WorkflowStallKind, detect_stalled_workflows
from reconciler.model import StepTaskView, WorkflowRead, WorkflowView
from swarm_api.mergewake import _append_round_step, _write_release
from swarm_api.rollup import derived_state_write

from .conftest import seed_task, seed_tenant
from .test_attemptstate import (  # noqa: F401 -- the fixtures are used by name
    POST_BODIES,
    TASK,
    Worker,
    client,
    opened,
    worker_tokens,
)

WORKFLOW = "wf_state_written"
IMPLEMENT = "task_wf_implement"


def _seed_workflow(db, *, state: str, steps: list[dict[str, Any]], tenant: str = "eng") -> None:
    db.docs[f"workflows/{WORKFLOW}"] = {
        "workflow_id": WORKFLOW, "tenant_id": tenant, "state": state, "steps": steps,
        "on_step_failure": "fail_workflow", "cancel_requested": False,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }


def _two_steps(merge_task: str = TASK) -> list[dict[str, Any]]:
    return [
        {"step_id": "implement", "runner_profile": "mock", "depends_on": [], "task_id": IMPLEMENT},
        {"step_id": "merge", "runner_profile": "merge", "depends_on": ["implement"],
         "task_id": merge_task},
    ]


def _drift(db) -> list[Any]:
    """What the reconciler's workflow check finds as `state_drift` right now."""
    doc = db.docs[f"workflows/{WORKFLOW}"]
    view = WorkflowView.from_doc(doc, WORKFLOW)
    tasks = {
        step.task_id: StepTaskView.from_doc(db.docs[f"tasks/{step.task_id}"], step.task_id)
        for step in view.steps
        if step.task_id and f"tasks/{step.task_id}" in db.docs
    }
    read = WorkflowRead(workflows=[view], tasks=tasks)
    config = ReconcilerConfig(project_id="test-project", region="test-region",
                              firestore_database="(default)")
    stalls = detect_stalled_workflows(read, {}, config, datetime.now(timezone.utc))
    return [s for s in stalls if s.kind is WorkflowStallKind.STATE_DRIFT]


def _seed_implement(db, state: str = "SUCCEEDED") -> None:
    seed_task(db, task_id=IMPLEMENT, tenant_id="eng", state=state, workflow_id=WORKFLOW)


# --------------------------------------------------------------------------
# the worker's state routes
# --------------------------------------------------------------------------


def test_a_merge_step_parking_writes_parked_onto_its_workflow(client, db):
    worker = Worker()
    opened(client, db, worker, workflow_id=WORKFLOW)
    _seed_implement(db)
    _seed_workflow(db, state="RUNNING", steps=_two_steps())
    assert _drift(db) == []

    response = worker.post(client, "park", POST_BODIES["park"])

    assert response.status_code == 200, response.text
    assert db.docs[f"tasks/{TASK}"]["state"] == "PARKED"
    assert db.docs[f"workflows/{WORKFLOW}"]["state"] == "PARKED"
    assert _drift(db) == []


def test_the_walk_to_running_writes_running_onto_its_workflow(client, db):
    worker = Worker()
    opened(client, db, worker, state="LEASED", workflow_id=WORKFLOW)
    _seed_implement(db)
    _seed_workflow(db, state="RUNNING", steps=_two_steps())
    # A lease holds capacity, so the workflow derives RUNNING from LEASED on;
    # stored QUEUED is the drift the report names (wf_0a3acbc95f004c3bb28b).
    db.docs[f"workflows/{WORKFLOW}"]["state"] = "QUEUED"

    response = worker.post(client, "transition", {"request_id": "walk-1", "to": "DISPATCHED"})

    assert response.status_code == 200, response.text
    assert db.docs[f"workflows/{WORKFLOW}"]["state"] == "RUNNING"
    assert _drift(db) == []


def test_the_last_step_finishing_writes_succeeded_onto_its_workflow(client, db):
    worker = Worker()
    opened(client, db, worker, workflow_id=WORKFLOW)
    _seed_implement(db)
    _seed_workflow(db, state="RUNNING", steps=_two_steps())

    response = worker.post(client, "finish", POST_BODIES["finish"])

    assert response.status_code == 200, response.text
    assert db.docs[f"workflows/{WORKFLOW}"]["state"] == "SUCCEEDED"
    assert _drift(db) == []


def test_a_retry_writes_ready_onto_its_workflow(client, db):
    worker = Worker()
    opened(client, db, worker, workflow_id=WORKFLOW)
    _seed_implement(db)
    _seed_workflow(db, state="RUNNING", steps=_two_steps())

    response = worker.post(client, "retry", POST_BODIES["retry"])

    assert response.status_code == 200, response.text
    assert db.docs[f"tasks/{TASK}"]["state"] == "READY"
    assert db.docs[f"workflows/{WORKFLOW}"]["state"] == "READY"
    assert _drift(db) == []


def test_a_transition_that_leaves_the_derived_state_unchanged_does_not_write_the_workflow(
        client, db):
    worker = Worker()
    opened(client, db, worker, state="DISPATCHED", workflow_id=WORKFLOW)
    _seed_implement(db)
    _seed_workflow(db, state="RUNNING", steps=_two_steps())
    marker = db.docs[f"workflows/{WORKFLOW}"]["updated_at"]

    response = worker.post(client, "transition", {"request_id": "walk-2", "to": "STARTING"})

    assert response.status_code == 200, response.text
    assert db.docs[f"workflows/{WORKFLOW}"]["updated_at"] == marker


# --------------------------------------------------------------------------
# mergewake: a round step appended, a release wait woken
# --------------------------------------------------------------------------


def test_a_ci_fix_round_appended_to_a_parked_merge_writes_the_derived_state(db):
    seed_tenant(db, "eng")
    _seed_implement(db)
    seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="CI_PENDING",
              workflow_id=WORKFLOW)
    _seed_workflow(db, state="PARKED", steps=_two_steps())
    fix_task = "task_wf_fix_round"
    seed_task(db, task_id=fix_task, tenant_id="eng", state="RUNNING")
    assert _drift(db) == []

    written = _append_round_step(db, "eng", WORKFLOW, {
        "step_id": "ci-fix:1", "runner_profile": "claude-code", "depends_on": [],
        "task_id": fix_task,
    }, merge_task=TASK, now=datetime.now(timezone.utc))

    assert written is True
    assert [s["step_id"] for s in db.docs[f"workflows/{WORKFLOW}"]["steps"]][-1] == "ci-fix:1"
    assert db.docs[f"workflows/{WORKFLOW}"]["state"] == "RUNNING"
    assert _drift(db) == []


def test_a_release_wait_woken_to_ready_writes_the_derived_state(db):
    seed_tenant(db, "eng")
    _seed_implement(db)
    attempt = "att_" + secrets.token_hex(6)
    sha = secrets.token_hex(20)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED",
                    park_reason="DEPENDENCY_INCOMPLETE", workflow_id=WORKFLOW)
    doc["blocked_by"] = [{"release_wait": {"sha": sha, "attempt_id": attempt}}]
    _seed_workflow(db, state="PARKED", steps=_two_steps())
    assert _drift(db) == []

    woken = _write_release(db, "eng", TASK, {"attempt_id": attempt, "sha": sha},
                           {"sha": sha, "state": "completed"}, "completed",
                           datetime.now(timezone.utc))

    assert woken is True
    assert db.docs[f"tasks/{TASK}"]["state"] == "READY"
    assert db.docs[f"workflows/{WORKFLOW}"]["state"] == "READY"
    assert _drift(db) == []


# --------------------------------------------------------------------------
# the helper's refusals
# --------------------------------------------------------------------------


def _write(db, **kwargs: Any) -> Any:
    txn = db.transaction()
    return derived_state_write(txn, db, tenant_id="eng", workflow_id=WORKFLOW,
                               now=datetime.now(timezone.utc), **kwargs)


def test_another_tenants_workflow_is_never_written(db):
    _seed_implement(db)
    seed_task(db, task_id=TASK, tenant_id="eng", state="RUNNING", workflow_id=WORKFLOW)
    _seed_workflow(db, state="QUEUED", steps=_two_steps(), tenant="research")

    assert _write(db) is None


def test_a_step_task_that_is_missing_writes_nothing(db):
    _seed_implement(db)
    _seed_workflow(db, state="QUEUED", steps=_two_steps("task_wf_gone"))

    assert _write(db) is None


def test_the_changed_task_is_judged_as_the_transaction_leaves_it(db):
    _seed_implement(db)
    seed_task(db, task_id=TASK, tenant_id="eng", state="RUNNING", workflow_id=WORKFLOW)
    _seed_workflow(db, state="RUNNING", steps=_two_steps())

    ref, patch = _write(db, changed={TASK: {**db.docs[f"tasks/{TASK}"], "state": "PARKED"}})

    assert ref.id == WORKFLOW
    assert patch["state"] == "PARKED"
