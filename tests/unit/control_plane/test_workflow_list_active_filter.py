"""`GET /v1/workflows?active=true` is an indexed query, not a walk through history.

THE DEFECT (measured 2026-10-06). The `state=` filter ran AFTER the rollup, on
one page of the tenant's whole history, so the MCP tool `swarm_workflows`
paged through 4 pages of 50 workflows to list 7 running ones and still
answered `complete: false`. A tenant's running set is small; its finished
history is not, and the cost followed the history.

THE CHANGE (owner decision 2026-10-06, observer proposal P4). The query now
filters on the STORED `state` (`workflows-tenant-state-created`), then derives
each row and filters again on the DERIVED state, which is still the one served.
The stored state is a cache that can be stale, so the query never asks for the
state the caller named -- it asks for every stored state a workflow that
DERIVES that state could still carry. A stored state is excluded only when it
is final: SUCCEEDED and CANCELLED, whose steps can never move again. FAILED and
DEAD_LETTERED stay in, because the frozen state machine allows a FAILED step
back to READY.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api.codec import workflow_to_firestore
from swarm_api.store import Store
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def _workflow(
    db,
    workflow_id: str,
    *,
    stored: str,
    steps: tuple[str, ...],
    created_at: datetime,
    tenant_id: str = "eng",
) -> None:
    task_ids = []
    for i, state in enumerate(steps):
        task_id = f"task_{workflow_id}_{i}"
        seed_task(db, task_id=task_id, tenant_id=tenant_id, state=state,
                  created_at=created_at, workflow_id=workflow_id)
        task_ids.append(task_id)
    workflow = Workflow(
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        created_at=created_at,
        updated_at=created_at,
        state=TaskState(stored),
        submitted_by="alice@saga.xyz",
        steps=[
            WorkflowStep(step_id=f"s{i}", runner_profile="mock", input={},
                         depends_on=[], task_id=task_id)
            for i, task_id in enumerate(task_ids)
        ],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)


def _history(db, finished: int, running: int) -> list[str]:
    """`running` live workflows OLDER than `finished` finished ones, so an
    unfiltered newest-first walk meets every finished one first."""
    for i in range(finished):
        _workflow(db, f"wf_done_{i:03d}", stored="SUCCEEDED", steps=("SUCCEEDED",),
                  created_at=T0 - timedelta(minutes=i))
    live = []
    for i in range(running):
        workflow_id = f"wf_live_{i:02d}"
        _workflow(db, workflow_id, stored="RUNNING", steps=("RUNNING", "PARKED"),
                  created_at=T0 - timedelta(days=1, minutes=i))
        live.append(workflow_id)
    return live


def test_active_lists_every_running_workflow_in_one_page_past_older_history(db, client):
    """7 running workflows behind 120 finished ones: one page of 50 holds all 7.

    Without the stored-state query the first page is the 50 newest FINISHED
    workflows, filtered to nothing, with a next_page_token pointing at more."""
    seed_tenant(db, "eng")
    live = _history(db, finished=120, running=7)

    response = client.get("/v1/workflows", params={"active": "true", "limit": 50},
                          headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [w["workflow_id"] for w in body["workflows"]] == live
    assert {w["state"] for w in body["workflows"]} == {"RUNNING"}
    assert body["next_page_token"] is None
    # The route says it filtered in the query, so a client knows finished
    # SUCCEEDED/CANCELLED history was skipped there.
    assert body["filter"]["active"] is True
    assert "SUCCEEDED" not in body["filter"]["stored_states"]
    assert "CANCELLED" not in body["filter"]["stored_states"]
    # Only the live workflows' steps were read: two each, not one per finished
    # workflow on the way.
    assert body["rollup_report"]["examined"] == 7
    assert body["rollup_report"]["step_reads"] == 14


def test_a_state_filter_is_served_by_the_same_query(db, client):
    seed_tenant(db, "eng")
    live = _history(db, finished=120, running=3)

    response = client.get("/v1/workflows", params={"state": "RUNNING", "limit": 50},
                          headers=auth_header("alice"))

    body = response.json()
    assert [w["workflow_id"] for w in body["workflows"]] == live
    assert body["next_page_token"] is None
    assert body["filter"]["states"] == ["RUNNING"]
    assert body["filter"]["stored_states"] is not None


def test_active_pages_on_the_keyset_across_more_than_one_page(db, client):
    """Keyset paging is kept: (created_at, id), with a tie at one instant."""
    seed_tenant(db, "eng")
    _history(db, finished=30, running=0)
    live = []
    for i in range(5):
        workflow_id = f"wf_tie_{i}"
        # Every one at the SAME instant, so the page boundary falls inside a
        # run of equal timestamps (#622's shape).
        _workflow(db, workflow_id, stored="QUEUED", steps=("READY",),
                  created_at=T0 - timedelta(days=2))
        live.append(workflow_id)

    seen: list[str] = []
    token = None
    for _ in range(5):
        params = {"active": "true", "limit": 2}
        if token:
            params["page_token"] = token
        body = client.get("/v1/workflows", params=params, headers=auth_header("alice")).json()
        seen += [w["workflow_id"] for w in body["workflows"]]
        token = body["next_page_token"]
        if token is None:
            break
    assert token is None
    assert sorted(seen) == sorted(live) and len(seen) == len(set(seen))


def test_a_stale_stored_state_is_still_derived_correctly(db, client):
    """The query filters on the cache; the answer is still the derivation.

    * stored QUEUED, steps RUNNING -> served RUNNING (the cache lagged);
    * stored RUNNING, steps all SUCCEEDED -> read, derived SUCCEEDED, NOT
      listed as active, and its stored state repaired so the next query skips
      it;
    * stored FAILED, a step re-opened FAILED -> READY (legal in the frozen
      state machine) -> listed, as READY.
    """
    seed_tenant(db, "eng")
    _workflow(db, "wf_lagged", stored="QUEUED", steps=("RUNNING",), created_at=T0)
    _workflow(db, "wf_finished", stored="RUNNING", steps=("SUCCEEDED", "SUCCEEDED"),
              created_at=T0 - timedelta(minutes=1))
    _workflow(db, "wf_reopened", stored="FAILED", steps=("READY", "SUCCEEDED"),
              created_at=T0 - timedelta(minutes=2))

    body = client.get("/v1/workflows", params={"active": "true"},
                      headers=auth_header("alice")).json()

    served = {w["workflow_id"]: w["state"] for w in body["workflows"]}
    assert served == {"wf_lagged": "RUNNING", "wf_reopened": "READY"}
    assert db.docs["workflows/wf_finished"]["state"] == "SUCCEEDED"
    assert db.docs["workflows/wf_lagged"]["state"] == "RUNNING"


def test_a_final_stored_state_is_not_read_at_all(db, client):
    """The control that shows the query filters on the stored state.

    A stored SUCCEEDED over a RUNNING step is data the platform cannot produce
    (a SUCCEEDED or CANCELLED rollup means every step is in a state with no way
    out). It is seeded here only to show the row is never fetched: the
    unfiltered list derives it RUNNING, the active list does not see it."""
    seed_tenant(db, "eng")
    _workflow(db, "wf_impossible", stored="SUCCEEDED", steps=("RUNNING",), created_at=T0)

    everything = client.get("/v1/workflows", headers=auth_header("alice")).json()
    assert [w["state"] for w in everything["workflows"]] == ["RUNNING"]
    db.docs["workflows/wf_impossible"]["state"] = "SUCCEEDED"

    active = client.get("/v1/workflows", params={"active": "true"},
                        headers=auth_header("alice")).json()
    assert active["workflows"] == []


def test_the_filter_stays_inside_the_tenant(db, client):
    seed_tenant(db, "eng")
    seed_tenant(db, "ops")
    _workflow(db, "wf_ours", stored="RUNNING", steps=("RUNNING",), created_at=T0)
    _workflow(db, "wf_theirs", stored="RUNNING", steps=("RUNNING",), created_at=T0,
              tenant_id="ops")

    body = client.get("/v1/workflows", params={"active": "true"},
                      headers=auth_header("alice")).json()
    assert [w["workflow_id"] for w in body["workflows"]] == ["wf_ours"]


def test_unknown_in_a_state_filter_falls_back_to_the_unfiltered_query(db, client):
    """UNKNOWN is derived when a step cannot be read, whatever the cache says,
    so no stored state can be excluded for it: the route says so."""
    seed_tenant(db, "eng")
    _workflow(db, "wf_running", stored="RUNNING", steps=("RUNNING",), created_at=T0)

    body = client.get("/v1/workflows", params={"state": "UNKNOWN"},
                      headers=auth_header("alice")).json()
    assert body["workflows"] == []
    assert body["filter"]["stored_states"] is None


def test_stored_states_for_a_filter():
    """The pure rule, over every name a caller can send."""
    every = {s.value for s in TaskState}
    active = Store.stored_states_for(active=True, states=None)
    assert set(active) == every - {"SUCCEEDED", "CANCELLED"}
    assert set(Store.stored_states_for(active=False, states=["RUNNING"])) == set(active)
    # A final state named explicitly is added back: its own cache is right.
    assert "SUCCEEDED" in Store.stored_states_for(active=False, states=["SUCCEEDED"])
    assert "CANCELLED" not in Store.stored_states_for(active=False, states=["SUCCEEDED"])
    assert Store.stored_states_for(active=False, states=["UNKNOWN"]) is None
    assert Store.stored_states_for(active=False, states=["NOT_A_STATE"]) is None
    assert Store.stored_states_for(active=False, states=None) is None
    # Both: the narrower of the two.
    assert set(Store.stored_states_for(active=True, states=["UNKNOWN", "RUNNING"])) == set(active)
