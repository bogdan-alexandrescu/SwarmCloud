"""Task, workflow, failure and lease listings page on (created_at, id) and lose nothing.

THE DEFECT (#622, history analysis 2026-10-05, F2). `list_tasks`,
`list_failures` and `list_workflows` paged on a timestamp-only cursor
(`created_at < before`). Steps of one workflow are created in one batch and
share a `created_at`, so when a page ended inside such a batch the rest of it
was skipped: 39 of 2,258 eng tasks were unreachable by listing (whole
workflows' steps, e.g. wf_5be111ff). Each listing now pages through
`Store._keyset_page` with a (created_at, id) token, the helper attempts and
events already use.

THE OLD TOKEN is still accepted for one release, and continues INCLUSIVELY at
its instant: a client paging across the deploy may see the boundary batch's
rows twice, never zero times.

AND THE LEASES (F8). `GET /v1/admin/leases` said `truncated: true` at its
window and offered no way past it. It now carries `next_page_token`.

Offline: the real routes over FakeFirestore. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api.codec import workflow_to_firestore
from swarm_api.store import Store, encode_cursor
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

#: Rows written NEWEST FIRST and with ids out of order, so a reader that
#: forgot to order would come back wrong rather than accidentally right.
BATCH = ["task_b7", "task_b2", "task_b5", "task_b1", "task_b6", "task_b3", "task_b4"]


def _seed_tasks(db, *, tenant: str = "eng", state: str = "READY") -> list[str]:
    """Two loose tasks, a batch of seven sharing one instant, two more loose."""
    ids: list[str] = []
    for i, moment in ((0, T0 + timedelta(minutes=10)), (1, T0 + timedelta(minutes=5))):
        task_id = f"task_new{i}_{tenant}"
        seed_task(db, task_id=task_id, tenant_id=tenant, state=state, created_at=moment)
        ids.append(task_id)
    for task_id in BATCH:
        full = f"{task_id}_{tenant}"
        seed_task(db, task_id=full, tenant_id=tenant, state=state, created_at=T0,
                  workflow_id="wf_5be111ff")
        ids.append(full)
    for i, moment in ((0, T0 - timedelta(minutes=5)), (1, T0 - timedelta(minutes=10))):
        task_id = f"task_old{i}_{tenant}"
        seed_task(db, task_id=task_id, tenant_id=tenant, state=state, created_at=moment)
        ids.append(task_id)
    return ids


def _walk(client, path: str, key: str, user: str, *, limit: int, **params: Any) -> list[str]:
    """Follow `next_page_token` from the first page to the last; every id served."""
    seen: list[str] = []
    token = None
    for _ in range(50):
        query = {"limit": limit, **params}
        if token:
            query["page_token"] = token
        response = client.get(path, params=query, headers=auth_header(user))
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(row["id"] if "id" in row else row["workflow_id"] for row in body[key])
        token = body["next_page_token"]
        if token is None:
            return seen
    raise AssertionError("the listing never ended")


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 8])
def test_a_page_boundary_inside_a_same_timestamp_batch_lists_every_task_once(db, client, limit):
    seed_tenant(db, "eng")
    expected = _seed_tasks(db)

    seen = _walk(client, "/v1/tasks", "tasks", "alice", limit=limit)

    assert sorted(seen) == sorted(expected), "a task was dropped or repeated"
    assert len(seen) == len(set(seen))
    # Newest first, ties broken by id descending: a total order.
    created = {t: (T0, t) for t in expected}
    for i, moment in ((0, T0 + timedelta(minutes=10)), (1, T0 + timedelta(minutes=5))):
        created[f"task_new{i}_eng"] = (moment, f"task_new{i}_eng")
    for i, moment in ((0, T0 - timedelta(minutes=5)), (1, T0 - timedelta(minutes=10))):
        created[f"task_old{i}_eng"] = (moment, f"task_old{i}_eng")
    assert seen == sorted(expected, key=lambda t: created[t], reverse=True)


def test_a_filtered_task_listing_pages_through_the_batch_too(db, client):
    """`workflow_id=` is the filter wf_5be111ff's steps were lost under."""
    seed_tenant(db, "eng")
    _seed_tasks(db)

    seen = _walk(client, "/v1/tasks", "tasks", "alice", limit=3, workflow_id="wf_5be111ff")

    assert sorted(seen) == sorted(f"{t}_eng" for t in BATCH)


def test_the_task_token_is_not_a_bare_timestamp_any_more(db, client):
    seed_tenant(db, "eng")
    _seed_tasks(db)

    body = client.get("/v1/tasks", params={"limit": 3}, headers=auth_header("alice")).json()

    assert body["next_page_token"] != encode_cursor(T0)
    assert body["next_page_token"] != encode_cursor(T0 + timedelta(minutes=5))


def test_an_old_timestamp_token_is_still_accepted_and_loses_nothing(db, client):
    """For one release: the token a client got before the deploy continues AT
    its instant, so the batch that shares it is served (again), not skipped."""
    seed_tenant(db, "eng")
    _seed_tasks(db)

    response = client.get(
        "/v1/tasks", params={"limit": 50, "page_token": encode_cursor(T0)},
        headers=auth_header("alice"),
    )

    assert response.status_code == 200, response.text
    served = [t["id"] for t in response.json()["tasks"]]
    assert sorted(served) == sorted(
        [f"{t}_eng" for t in BATCH] + ["task_old0_eng", "task_old1_eng"]
    )


def test_a_garbage_token_is_still_a_422(db, client):
    seed_tenant(db, "eng")

    response = client.get(
        "/v1/tasks", params={"page_token": "not-a-token"}, headers=auth_header("alice")
    )

    assert response.status_code == 422


def test_a_workflow_token_is_refused_by_the_task_listing(db, client):
    seed_tenant(db, "eng")
    for i in range(3):
        _seed_workflow(db, f"wf_{i}", T0)
    page = client.get("/v1/workflows", params={"limit": 1}, headers=auth_header("alice")).json()

    response = client.get(
        "/v1/tasks", params={"page_token": page["next_page_token"]}, headers=auth_header("alice")
    )

    assert response.status_code == 422


# -- workflows -----------------------------------------------------------------


def _seed_workflow(db, workflow_id: str, created_at: datetime, tenant: str = "eng") -> None:
    task_id = f"task_of_{workflow_id}"
    seed_task(db, task_id=task_id, tenant_id=tenant, state="SUCCEEDED",
              created_at=created_at, workflow_id=workflow_id)
    workflow = Workflow(
        workflow_id=workflow_id,
        tenant_id=tenant,
        created_at=created_at,
        updated_at=created_at,
        state=TaskState.SUCCEEDED,
        submitted_by="alice@saga.xyz",
        steps=[WorkflowStep(step_id="a", runner_profile="mock", input={}, depends_on=[],
                            task_id=task_id)],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
def test_workflows_sharing_a_created_at_are_each_listed_once(db, client, limit):
    seed_tenant(db, "eng")
    expected = ["wf_new"] + [f"wf_batch{i}" for i in (3, 0, 4, 1, 2)] + ["wf_old"]
    _seed_workflow(db, "wf_new", T0 + timedelta(minutes=1))
    for workflow_id in expected[1:-1]:
        _seed_workflow(db, workflow_id, T0)
    _seed_workflow(db, "wf_old", T0 - timedelta(minutes=1))

    seen = _walk(client, "/v1/workflows", "workflows", "alice", limit=limit)

    assert sorted(seen) == sorted(expected)
    assert len(seen) == len(set(seen))


def test_an_old_workflow_token_is_still_accepted(db, client):
    seed_tenant(db, "eng")
    for i in range(3):
        _seed_workflow(db, f"wf_batch{i}", T0)
    _seed_workflow(db, "wf_old", T0 - timedelta(minutes=1))

    response = client.get(
        "/v1/workflows", params={"page_token": encode_cursor(T0)}, headers=auth_header("alice")
    )

    assert response.status_code == 200, response.text
    served = sorted(w["workflow_id"] for w in response.json()["workflows"])
    assert served == ["wf_batch0", "wf_batch1", "wf_batch2", "wf_old"]


# -- admin failures ------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_failures_sharing_a_created_at_are_each_listed_once(db, client, limit):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    expected = _seed_tasks(db, tenant="eng", state="FAILED")
    expected += _seed_tasks(db, tenant="research", state="FAILED")
    seed_task(db, task_id="task_fine", tenant_id="eng", state="SUCCEEDED", created_at=T0)

    seen = _walk(client, "/v1/admin/failures", "tasks", "root", limit=limit)

    assert sorted(seen) == sorted(expected)
    assert len(seen) == len(set(seen))


def test_an_old_failures_token_is_still_accepted(db, client):
    seed_tenant(db, "eng")
    _seed_tasks(db, state="FAILED")

    response = client.get(
        "/v1/admin/failures", params={"page_token": encode_cursor(T0)},
        headers=auth_header("root"),
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["tasks"]) == len(BATCH) + 2


# -- admin leases --------------------------------------------------------------


def _lease(db, lease_id: str, *, created_at: datetime, released: bool, tenant: str = "eng"):
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id,
        "task_id": f"task_{lease_id}",
        "attempt_id": f"att_{lease_id}",
        "tenant_id": tenant,
        "generation": 1,
        "pools": ["global", f"tenant:{tenant}"],
        "units": 1,
        "state": "DISPATCHED",
        "created_at": created_at,
        "dispatch_deadline": created_at + timedelta(minutes=5),
        "expires_at": created_at + timedelta(minutes=30),
        "heartbeat_at": created_at + timedelta(seconds=30),
        "released_at": created_at + timedelta(minutes=1) if released else None,
        "release_reason": "terminal:SUCCEEDED" if released else None,
    }


def _seed_leases(db, *, released: bool) -> list[str]:
    ids = ["l_new"]
    _lease(db, "l_new", created_at=T0 + timedelta(minutes=1), released=released)
    for lease_id in ("l_b3", "l_b1", "l_b4", "l_b2"):
        _lease(db, lease_id, created_at=T0, released=released)
        ids.append(lease_id)
    _lease(db, "l_old", created_at=T0 - timedelta(minutes=1), released=released)
    ids.append("l_old")
    return ids


def _walk_leases(client, *, limit: int, **params: Any) -> tuple[list[str], list[dict]]:
    seen: list[str] = []
    bodies: list[dict] = []
    token = None
    for _ in range(50):
        query = {"limit": limit, **params}
        if token:
            query["page_token"] = token
        response = client.get("/v1/admin/leases", params=query, headers=auth_header("root"))
        assert response.status_code == 200, response.text
        body = response.json()
        bodies.append(body)
        seen.extend(row["lease_id"] for row in body["leases"])
        token = body["next_page_token"]
        if token is None:
            return seen, bodies
    raise AssertionError("the lease listing never ended")


@pytest.mark.parametrize("active_only", ["true", "false"])
@pytest.mark.parametrize("limit", [1, 2, 3, 4])
def test_lease_page_two_is_reachable(db, client, active_only, limit):
    expected = _seed_leases(db, released=active_only == "false")

    seen, bodies = _walk_leases(client, limit=limit, active_only=active_only)

    assert len(bodies) > 1, "the listing should take more than one page"
    assert sorted(seen) == sorted(expected)
    assert len(seen) == len(set(seen))
    # `truncated` keeps its meaning: more lay behind THIS page.
    assert [b["truncated"] for b in bodies] == [True] * (len(bodies) - 1) + [False]


def test_the_lease_token_keeps_the_mode_it_was_issued_for(db, client):
    _seed_leases(db, released=False)
    first = client.get("/v1/admin/leases", params={"limit": 2},
                       headers=auth_header("root")).json()

    response = client.get(
        "/v1/admin/leases",
        params={"limit": 2, "active_only": "false", "page_token": first["next_page_token"]},
        headers=auth_header("root"),
    )

    assert response.status_code == 422


def test_the_store_pages_live_leases_and_still_counts_what_the_rows_left_out(db):
    expected = _seed_leases(db, released=False)
    store = Store(db)

    first = store.scan_leases("eng", limit=2)
    second = store.scan_leases("eng", limit=2, page_token=first.next_page_token)

    # Newest first, the batch at T0 by id descending.
    assert [x.lease_id for x in first.leases] == ["l_new", "l_b4"]
    assert [x.lease_id for x in second.leases] == ["l_b3", "l_b2"]
    # Live leases not in THESE rows -- the drift check's input, per page.
    assert first.active_beyond_window == len(expected) - 2
    assert second.active_beyond_window == len(expected) - 2
