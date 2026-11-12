"""PR #3901, the owner's pick "Serve checks with the workflow's pull request".

Each workflow read (`GET /v1/workflows?ids=` and `GET /v1/workflows/{id}`)
serves `pull_request` on the workflow: the pull request the issue run that
started it opened, with the run's last stored CI reading (`checks`,
`check_counts`). Null when no issue run of the tenant started it; another
tenant's run naming the same id is never served.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_api import workflowpr
from swarm_api.codec import workflow_to_firestore
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant
from .fakes import FakeFirestore

T0 = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)


def _workflow(db, workflow_id: str) -> None:
    task_id = f"task_{workflow_id}"
    seed_task(db, task_id=task_id, tenant_id="eng", state="RUNNING", created_at=T0, workflow_id=workflow_id)
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(Workflow(
        workflow_id=workflow_id, tenant_id="eng", created_at=T0, updated_at=T0,
        state=TaskState.RUNNING, submitted_by="alice@example.com",
        steps=[WorkflowStep(step_id="implement", runner_profile="mock", input={"prompt": "x"},
                            depends_on=[], task_id=task_id)],
    ))


def _run(db, run_id: str, workflow_id: str, *, tenant: str = "eng", checks: str | None = "red",
         updated: str = "2026-11-11T18:00:00+00:00") -> None:
    db.docs[f"issue_runs/{run_id}"] = {
        "id": run_id, "tenant_id": tenant, "workflow_id": workflow_id, "updated_at": updated,
        "pull_request": {
            "number": 41, "url": "https://github.com/example-org/widgets/pull/41", "head_sha": "a" * 40,
            "checks": checks, "check_counts": {"passed": 5, "failed": 1, "pending": 0, "skipped": 2},
            "checked_at": T0,
        },
    }


def test_a_workflow_an_issue_run_started_serves_its_pull_request_with_the_checks(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_run")
    _workflow(db, "wf_plain")
    _run(db, "run_1", "wf_run")

    page = client.get("/v1/workflows", headers=auth_header("alice"),
                      params={"ids": ["wf_run", "wf_plain"], "view": "summary"})
    assert page.status_code == 200, page.text
    rows = {w["workflow_id"]: w for w in page.json()["workflows"]}
    pull = rows["wf_run"]["pull_request"]
    assert pull["number"] == 41 and pull["checks"] == "red"
    assert pull["url"] == "https://github.com/example-org/widgets/pull/41"
    assert pull["check_counts"] == {"passed": 5, "failed": 1, "pending": 0, "skipped": 2}
    assert pull["checked_at"] == T0.isoformat()
    assert rows["wf_plain"]["pull_request"] is None

    one = client.get("/v1/workflows/wf_run", headers=auth_header("alice"))
    assert one.status_code == 200, one.text
    assert one.json()["workflow"]["pull_request"] == pull


def test_another_tenants_run_is_never_served_and_the_newest_run_wins():
    db = FakeFirestore()
    _run(db, "run_other", "wf_1", tenant="other", checks="green")
    assert workflowpr.pull_requests(db, "eng", ["wf_1"]) == {}

    _run(db, "run_old", "wf_1", checks="pending", updated="2026-11-11T17:00:00+00:00")
    _run(db, "run_new", "wf_1", checks="green", updated="2026-11-11T19:00:00+00:00")
    assert workflowpr.pull_requests(db, "eng", ["wf_1"])["wf_1"]["checks"] == "green"


def test_a_reading_never_stored_is_null_never_a_state():
    assert workflowpr.served_pull({"number": 7, "url": "javascript:x", "checks": "weird"}) == {
        "number": 7, "url": None, "head_sha": None, "checks": None,
        "check_counts": None, "checked_at": None, "merged": None,
    }
    assert workflowpr.served_pull({"number": 0}) is None
    assert workflowpr.served_pull(None) is None


def test_more_ids_than_one_in_filter_takes_are_read_in_chunks():
    db = FakeFirestore()
    ids = [f"wf_{i:02d}" for i in range(workflowpr.IN_CHUNK + 5)]
    for i, wf in enumerate(ids):
        _run(db, f"run_{i:02d}", wf)
    assert set(workflowpr.pull_requests(db, "eng", ids)) == set(ids)
