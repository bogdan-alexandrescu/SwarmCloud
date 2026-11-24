"""The scheduler's half of workflow pause (docs/design/workflow-pause.md
section 4, lane P2): a paused workflow admits nothing.

* A READY step of a paused workflow is parked WORKFLOW_PAUSED at admission,
  before any lease: no lease document, no pool counter moved, nothing
  dispatched (invariants 1 to 3).
* The workflow-pause sweep leaves it parked while the pause holds and
  promotes it once the workflow is resumed; admission then admits it.
* A failed workflow's paused step is cancelled, not promoted.
* A step whose stale mirror outlived its pause is promoted with the mirror
  cleared in the same guarded write.
* Pause landing during admission cannot leave a step both parked and leased:
  the park is guarded on the READY state it read, so a lease that landed first
  wins and the park writes nothing.
* The workflow is read at most once per drain, tenant-checked.

No cloud and no emulator: the in-memory Firestore and the real scheduler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .conftest import seed_pool, seed_task, seed_tenant

PAUSED_AT = datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc)


def _seed_workflow(db, workflow_id: str = "wf_paused", *, tenant_id: str = "eng",
                   paused: bool = True, state: str = "RUNNING",
                   task_ids: tuple[str, ...] = ("task_a",)) -> dict[str, Any]:
    doc = {
        "workflow_id": workflow_id,
        "tenant_id": tenant_id,
        "created_at": PAUSED_AT,
        "updated_at": PAUSED_AT,
        "state": state,
        "submitted_by": "alice@example.com",
        "steps": [{"step_id": t, "runner_profile": "mock", "task_id": t} for t in task_ids],
        "on_step_failure": "fail_workflow",
        "priority": 0,
        "cancel_requested": False,
        "paused": paused,
        "paused_at": PAUSED_AT if paused else None,
        "paused_by": "alice@example.com" if paused else None,
    }
    db.docs[f"workflows/{workflow_id}"] = doc
    return doc


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _leases(db) -> list[str]:
    return [k for k in db.docs if k.startswith("leases/")]


def _active(db) -> dict[str, int]:
    return {k: v.get("active", 0) for k, v in db.docs.items()
            if k.startswith("pools/") and k.count("/") == 1}


def _events(db, task_id: str, type_: str) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [d for k, d in db.docs.items() if k.startswith(prefix) and d.get("type") == type_]


def test_a_ready_step_of_a_paused_workflow_is_parked_never_admitted(
    db, make_scheduler, dispatcher
):
    _base(db)
    _seed_workflow(db)
    seed_task(db, task_id="task_a", tenant_id="eng", state="READY", workflow_id="wf_paused")
    pools_before = _active(db)

    make_scheduler().drain()

    task = db.docs["tasks/task_a"]
    assert task["state"] == "PARKED"
    assert task["park_reason"] == "WORKFLOW_PAUSED"
    assert task["current_lease_id"] is None
    assert _leases(db) == []
    assert _active(db) == pools_before
    assert dispatcher.dispatched == []
    parked = _events(db, "task_a", "parked")
    assert len(parked) == 1
    assert parked[0]["detail"]["reason"] == "WORKFLOW_PAUSED"
    assert parked[0]["detail"]["workflow_id"] == "wf_paused"
    assert parked[0]["detail"]["paused_by"] == "alice@example.com"


def test_the_sweep_holds_the_park_while_paused_and_promotes_it_on_resume(
    db, make_scheduler, dispatcher
):
    _base(db)
    workflow = _seed_workflow(db)
    seed_task(db, task_id="task_a", tenant_id="eng", state="READY", workflow_id="wf_paused")
    scheduler = make_scheduler()

    scheduler.drain()
    scheduler.drain()
    assert db.docs["tasks/task_a"]["state"] == "PARKED"
    assert db.docs["tasks/task_a"]["park_reason"] == "WORKFLOW_PAUSED"
    assert dispatcher.dispatched == []
    assert len(_events(db, "task_a", "parked")) == 1, "re-readied and re-parked while paused"

    workflow.update({"paused": False, "paused_at": None})
    scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_a"]
    ready = _events(db, "task_a", "ready")
    assert ready and ready[0]["detail"]["reason"] == "workflow_resumed"


def test_a_step_parked_by_its_worker_for_the_pause_is_promoted_on_resume(
    db, make_scheduler, dispatcher
):
    _base(db)
    _seed_workflow(db, paused=False)
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="PARKED",
                    park_reason="WORKFLOW_PAUSED", workflow_id="wf_paused")
    doc["pause_requested_at"] = PAUSED_AT

    make_scheduler().drain()

    task = db.docs["tasks/task_a"]
    assert task["state"] == "DISPATCHED"
    # The stale mirror is cleared in the promoting write, so the next worker
    # does not park again on a pause that was lifted.
    assert task.get("pause_requested_at") is None


def test_a_failed_workflows_paused_step_is_cancelled_not_promoted(
    db, make_scheduler, dispatcher
):
    _base(db)
    _seed_workflow(db, paused=False, task_ids=("task_bad", "task_a"))
    seed_task(db, task_id="task_bad", tenant_id="eng", state="FAILED", workflow_id="wf_paused")
    db.docs["tasks/task_bad"]["step_id"] = "task_bad"
    seed_task(db, task_id="task_a", tenant_id="eng", state="PARKED",
              park_reason="WORKFLOW_PAUSED", workflow_id="wf_paused")

    make_scheduler().drain()

    assert db.docs["tasks/task_a"]["state"] == "CANCELLED"
    assert dispatcher.dispatched == []


def test_a_paused_workflow_of_another_tenant_does_not_park_this_tenants_step(
    db, make_scheduler, dispatcher
):
    _base(db)
    _seed_workflow(db, tenant_id="research")
    seed_task(db, task_id="task_a", tenant_id="eng", state="READY", workflow_id="wf_paused")

    make_scheduler().drain()

    assert db.docs["tasks/task_a"]["park_reason"] != "WORKFLOW_PAUSED"


def test_pause_landing_during_admission_cannot_leave_a_step_parked_and_leased(
    db, make_scheduler, dispatcher
):
    """A concurrent drain leases the step between this drain's read of it and
    its park. The park is guarded on the READY it read, so it writes nothing:
    the step is leased, not parked, and holds exactly the one lease."""
    _base(db)
    _seed_workflow(db)
    seed_task(db, task_id="task_a", tenant_id="eng", state="READY", workflow_id="wf_paused")
    scheduler = make_scheduler()
    store = scheduler._store
    real = store.workflow_pause

    def lease_lands_first(*args, **kwargs):
        answer = real(*args, **kwargs)
        db.docs["tasks/task_a"].update(
            {"state": "LEASED", "current_lease_id": "lease_other", "current_generation": 1}
        )
        return answer

    store.workflow_pause = lease_lands_first

    scheduler.drain()

    task = db.docs["tasks/task_a"]
    assert task["state"] == "LEASED"
    assert task["current_lease_id"] == "lease_other"
    assert task["park_reason"] != "WORKFLOW_PAUSED"
    assert _events(db, "task_a", "parked") == []
    assert dispatcher.dispatched == []


def test_the_workflow_is_read_once_per_drain(db, make_scheduler):
    _base(db)
    _seed_workflow(db, task_ids=("task_a", "task_b", "task_c"))
    for task_id in ("task_a", "task_b", "task_c"):
        seed_task(db, task_id=task_id, tenant_id="eng", state="READY", workflow_id="wf_paused")
    scheduler = make_scheduler()
    store = scheduler._store
    real = store.workflow_pause
    calls: list[tuple] = []

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    store.workflow_pause = counted

    scheduler.drain()

    assert len(calls) == 1
    assert all(db.docs[f"tasks/{t}"]["park_reason"] == "WORKFLOW_PAUSED"
               for t in ("task_a", "task_b", "task_c"))
