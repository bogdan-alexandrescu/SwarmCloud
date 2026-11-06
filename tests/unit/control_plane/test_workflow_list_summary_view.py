"""Live QA CH-21 (2026-10-11): `GET /v1/workflows?limit=100` took 3.1-4.0 s.

Two halves. The step states the list derives from are read in `get_all`
batches of 100 rather than one sequential point read per step, so a page of
100 workflows is a handful of round trips; the documents read, and the
`rollup_report.step_reads` that counts them, are unchanged. And
`view=summary` serves each step without its `input` and that input's masking
counts -- the keys ABSENT, not null, as `GET /v1/tasks?view=summary` drops its
rows' -- and says `"view": "summary"`. No `view`, or `view=full`, is the
response as it was; an unknown view is a 422 naming the allowed ones.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api.codec import workflow_to_firestore
from swarm_api.store import Store
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
STEP_INPUT_KEYS = ("input", "input_redaction_count", "input_masked_by")


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
        submitted_by="alice@example.com",
        steps=[
            WorkflowStep(step_id=f"s{i:02d}", runner_profile="mock",
                         input={"prompt": "write the report " * 20},
                         depends_on=[], task_id=task_id)
            for i, task_id in enumerate(task_ids)
        ],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)
    return workflow


def _list(client, **params):
    return client.get("/v1/workflows", headers=auth_header("alice"), params=params)


def test_summary_steps_carry_no_input_and_the_rest_is_the_full_view(client, db):
    seed_tenant(db, "eng")
    for i in range(3):
        _workflow(db, f"wf_{i}", 2, T0 - timedelta(minutes=i))

    full = _list(client, limit=10).json()
    summary_response = _list(client, limit=10, view="summary")
    assert summary_response.status_code == 200, summary_response.text
    summary = summary_response.json()

    assert summary["view"] == "summary"
    assert "view" not in full
    assert len(summary["workflows"]) == 3
    for row in summary["workflows"]:
        for step in row["steps"]:
            for key in STEP_INPUT_KEYS:
                assert key not in step, f"{key} is in a summary step"
    # Everything else is the full view's, field for field, beside each
    # step's `task_summary`, which only the summary reads (the Workflows
    # board's one read, 2026-11-01; test_workflow_step_board.py).
    stripped = [
        {**w, "steps": [{k: v for k, v in s.items() if k not in STEP_INPUT_KEYS} for s in w["steps"]]}
        for w in full["workflows"]
    ]
    unboarded = [
        {**w, "steps": [{k: v for k, v in s.items() if k != "task_summary"} for s in w["steps"]]}
        for w in summary["workflows"]
    ]
    assert unboarded == stripped
    assert summary["rollup_report"] == full["rollup_report"]
    assert [w["state"] for w in summary["workflows"]] == ["SUCCEEDED"] * 3


def test_full_and_absent_are_the_response_as_it_was(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_a", 2, T0)
    absent = _list(client).json()
    full = _list(client, view="full").json()
    assert absent == full
    for step in absent["workflows"][0]["steps"]:
        for key in STEP_INPUT_KEYS:
            assert key in step


def test_an_unknown_view_is_refused_with_the_allowed_ones(client, db):
    seed_tenant(db, "eng")
    response = _list(client, view="brief")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["known_views"] == ["full", "summary"]


def test_step_states_are_read_in_batches_not_one_round_trip_per_step(db, monkeypatch):
    """250 steps over 5 workflows: 3 `get_all` calls of at most 100, the same 250 documents."""
    seed_tenant(db, "eng")
    workflows = [_workflow(db, f"wf_{i}", 50, T0 - timedelta(minutes=i)) for i in range(5)]

    batches: list[int] = []
    real_get_all = db.get_all

    def counting_get_all(references, *args, **kwargs):
        refs = list(references)
        batches.append(len(refs))
        return real_get_all(refs, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", counting_get_all)
    read = Store(db).workflow_step_states("eng", workflows)

    assert batches == [100, 100, 50]
    assert read.reads == 250
    assert len(read.states) == 250
    assert read.absent == []


# ---------------------------------------------------------------------------
# `GET /v1/workflows?ids=` (live QA CH-6): the Timeline's one read for the
# workflows its lanes name, where it made one `GET /v1/workflows/{id}` each.
# ---------------------------------------------------------------------------


def _other_tenant_workflow(db, workflow_id: str) -> None:
    seed_tenant(db, "ops")
    seed_task(db, task_id=f"task_{workflow_id}", tenant_id="ops", state="SUCCEEDED",
              created_at=T0, workflow_id=workflow_id)
    workflow = Workflow(
        workflow_id=workflow_id, tenant_id="ops", created_at=T0, updated_at=T0,
        state=TaskState.SUCCEEDED, submitted_by="eve@example.com",
        steps=[WorkflowStep(step_id="s", runner_profile="mock", input={}, depends_on=[],
                            task_id=f"task_{workflow_id}")],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)


def test_ids_reads_the_named_workflows_in_order_with_their_step_tasks(client, db):
    seed_tenant(db, "eng")
    for i in range(4):
        _workflow(db, f"wf_{i}", 2, T0 - timedelta(minutes=i))

    response = _list(client, ids=["wf_2", "wf_0"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert [w["workflow_id"] for w in body["workflows"]] == ["wf_2", "wf_0"]
    assert body["missing"] == []
    assert body["next_page_token"] is None
    assert body["filter"]["ids"] == ["wf_2", "wf_0"]
    assert sorted(t["id"] for t in body["tasks"]) == [
        "task_wf_0_00", "task_wf_0_01", "task_wf_2_00", "task_wf_2_01",
    ]
    # Each row and each task is what the single read serves for it.
    one = client.get("/v1/workflows/wf_2", headers=auth_header("alice")).json()
    assert body["workflows"][0] == one["workflow"]
    by_id = {t["id"]: t for t in body["tasks"]}
    for task in one["tasks"]:
        assert by_id[task["id"]] == task


def test_ids_names_what_it_did_not_serve_without_telling_why(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_mine", 1, T0)
    _other_tenant_workflow(db, "wf_theirs")

    body = _list(client, ids=["wf_mine", "wf_theirs", "wf_nope"]).json()

    assert [w["workflow_id"] for w in body["workflows"]] == ["wf_mine"]
    assert body["missing"] == ["wf_theirs", "wf_nope"]
    assert all(t["tenant_id"] == "eng" for t in body["tasks"])
    assert [t["id"] for t in body["tasks"]] == ["task_wf_mine_00"]


def test_ids_takes_the_summary_view(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_a", 2, T0)
    body = _list(client, ids=["wf_a"], view="summary").json()
    assert body["view"] == "summary"
    for step in body["workflows"][0]["steps"]:
        for key in STEP_INPUT_KEYS:
            assert key not in step


def test_ids_is_capped_and_is_not_a_page(client, db):
    from swarm_api.routes.workflows import MAX_WORKFLOW_IDS

    seed_tenant(db, "eng")
    over = _list(client, ids=[f"wf_{i}" for i in range(MAX_WORKFLOW_IDS + 1)])
    assert over.status_code == 422, over.text
    assert over.json()["detail"]["max_ids"] == MAX_WORKFLOW_IDS
    at_cap = _list(client, ids=[f"wf_{i}" for i in range(MAX_WORKFLOW_IDS)])
    assert at_cap.status_code == 200, at_cap.text
    assert len(at_cap.json()["missing"]) == MAX_WORKFLOW_IDS
    for extra in ({"active": "true"}, {"state": "SUCCEEDED"}, {"page_token": "x"}):
        refused = _list(client, ids=["wf_a"], **extra)
        assert refused.status_code == 422, (extra, refused.text)


def test_the_store_reads_ids_in_batches_and_checks_the_tenant(db, monkeypatch):
    seed_tenant(db, "eng")
    for i in range(3):
        _workflow(db, f"wf_{i}", 1, T0)
    _other_tenant_workflow(db, "wf_theirs")
    calls: list[int] = []
    real = db.get_all

    def counting(references, *args, **kwargs):
        refs = list(references)
        calls.append(len(refs))
        return real(refs, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", counting)
    got = Store(db).workflows_by_id("eng", ["wf_2", "wf_theirs", "wf_0", "wf_2"], submitted_by=None)
    assert [w.workflow_id for w in got] == ["wf_2", "wf_0"]
    assert calls == [3]
    assert Store(db).workflows_by_id("eng", ["wf_0"], submitted_by="bob@example.com") == []
