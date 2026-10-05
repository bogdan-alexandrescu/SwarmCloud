"""The workflow list does not serve UNKNOWN because its read budget ran out (F9).

THE DEFECT. `Store.workflow_step_states` spent one SHARED budget of 500 step
reads per request across every workflow on the page. Past it the remaining
steps came back unread and their workflows derived UNKNOWN -- 20 of 343 rows
in the 2026-10-05 history analysis. Nothing was wrong with those workflows;
the page had simply listed them after some bigger ones.

THE CHANGE (owner decision 2026-10-05). The budget is per WORKFLOW, not per
request: each workflow on the page may read up to `max_workflow_steps` (50)
step tasks, which is every step of any workflow the submission validation
admits. So the read cost follows the page -- one read per step the response
already serves -- and one workflow can no longer spend another's share. A
workflow with more steps than that (only possible for data written outside
the validation) still derives UNKNOWN, alone.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api import store as store_module
from swarm_api.codec import workflow_to_firestore
from swarm_api.store import Store
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def _workflow(db, workflow_id: str, steps: int, created_at: datetime) -> Workflow:
    task_ids = [f"task_{workflow_id}_{i:02d}" for i in range(steps)]
    for task_id in task_ids:
        seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED",
                  created_at=created_at, workflow_id=workflow_id)
    workflow = Workflow(
        workflow_id=workflow_id,
        tenant_id="eng",
        created_at=created_at,
        updated_at=created_at,
        state=TaskState.SUCCEEDED,
        submitted_by="alice@saga.xyz",
        steps=[
            WorkflowStep(step_id=f"s{i:02d}", runner_profile="mock", input={},
                         depends_on=[], task_id=task_id)
            for i, task_id in enumerate(task_ids)
        ],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)
    return workflow


def test_a_page_with_more_steps_than_the_old_budget_derives_every_row(db, client):
    """12 workflows of 50 steps: 600 step reads, past the old shared 500."""
    seed_tenant(db, "eng")
    for i in range(12):
        _workflow(db, f"wf_{i:02d}", 50, T0 - timedelta(minutes=i))

    response = client.get("/v1/workflows", params={"limit": 12}, headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    states = [w["state"] for w in body["workflows"]]
    assert len(states) == 12
    assert "UNKNOWN" not in states, states
    assert states == ["SUCCEEDED"] * 12
    assert body["rollup_report"]["step_read_budget_exhausted"] is False
    assert body["rollup_report"]["unknown"] == 0
    assert body["rollup_report"]["step_reads"] == 600


def test_under_a_small_per_workflow_budget_only_the_oversized_workflow_is_unknown(
    db, client, monkeypatch
):
    """A workflow bigger than its own allowance cannot starve the rows after it:
    with the shared budget, the first big workflow spent every read."""
    monkeypatch.setattr(store_module, "_STEP_READS_PER_WORKFLOW", 3)
    seed_tenant(db, "eng")
    _workflow(db, "wf_big", 5, T0)  # newest: listed first
    for i in range(4):
        _workflow(db, f"wf_small{i}", 3, T0 - timedelta(minutes=i + 1))

    body = client.get("/v1/workflows", headers=auth_header("alice")).json()

    states = {w["workflow_id"]: w["state"] for w in body["workflows"]}
    assert states.pop("wf_big") == "UNKNOWN"
    assert states == {f"wf_small{i}": "SUCCEEDED" for i in range(4)}
    assert body["rollup_report"]["step_read_budget_exhausted"] is True
    assert body["rollup_report"]["unknown"] == 1


def test_the_store_reads_each_workflow_within_its_own_allowance(db, monkeypatch):
    monkeypatch.setattr(store_module, "_STEP_READS_PER_WORKFLOW", 2)
    seed_tenant(db, "eng")
    big = _workflow(db, "wf_big", 4, T0)
    small = _workflow(db, "wf_small", 2, T0)

    read = Store(db).workflow_step_states("eng", [big, small])

    assert read.unread == ["task_wf_big_02", "task_wf_big_03"]
    assert set(read.states) == {
        "task_wf_big_00", "task_wf_big_01", "task_wf_small_00", "task_wf_small_01",
    }
    assert read.reads == 4


def test_an_explicit_budget_is_still_one_shared_cap(db):
    """`budget=` keeps its meaning for a caller that asks for a hard total."""
    seed_tenant(db, "eng")
    big = _workflow(db, "wf_big", 4, T0)

    read = Store(db).workflow_step_states("eng", [big], budget=1)

    assert read.reads == 1
    assert read.unread == ["task_wf_big_01", "task_wf_big_02", "task_wf_big_03"]
