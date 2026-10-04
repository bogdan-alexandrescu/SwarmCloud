"""A cancel with no live worker behind it ends the task, here and now (#560).

On 2026-10-04 swarm-verify cancelled task_59d1cff58cf8435fb9b0 at 10:53:58Z.
The task was DISPATCHED, its worker had never started, and its generation had
been fenced twenty minutes earlier. The API set `cancel_requested` and nothing
else, and waited for a worker to see the flag. There was no worker, so the
lease and every pool it reserved stayed held for ten hours.

  C-1  QUEUED, READY and PARKED end CANCELLED at once, as before.
  C-2  DISPATCHED whose worker never started ends CANCELLED in the cancel's own
       transaction: end cause cancel_requested, the lease released through the
       frozen release, every pool back where admission found it, and the
       generation fenced so a container that starts late runs nothing.
  C-3  A fenced generation whose lease has gone silent ends the same way.
  C-4  A LIVE worker -- the lease heartbeated, the attempt started -- is still
       only flagged: its lease and pools are untouched and the worker ends it.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from .conftest import auth_header, seed_pool, seed_task, seed_tenant


def dispatched(client, db, make_scheduler) -> str:
    """One mock task, admitted and dispatched; no worker has run yet."""
    seed_pool(db, "global", hard_limit=10)
    submitted = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {}},
    )
    assert submitted.status_code in (200, 201), submitted.text
    task_id = submitted.json()["task"]["id"]
    make_scheduler().drain()
    task = db.docs[f"tasks/{task_id}"]
    assert task["state"] == "DISPATCHED", task["state"]
    assert task["current_lease_id"], "nothing was admitted; these tests would be vacuous"
    return task_id


def lease_of(db, task_id: str) -> tuple[str, dict[str, Any]]:
    lease_id = db.docs[f"tasks/{task_id}"]["current_lease_id"]
    return lease_id, db.docs[f"leases/{lease_id}"]


def worker_started(db, task_id: str, *, seconds_ago: int = 5) -> None:
    """What a worker's first control-plane writes leave behind."""
    lease_id, lease = lease_of(db, task_id)
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    lease["heartbeat_at"] = at
    db.docs[f"attempts/{lease['attempt_id']}"]["started_at"] = at


def active_pools(db) -> dict[str, int]:
    return {
        path: int(doc.get("active", 0))
        for path, doc in db.docs.items()
        if path.startswith("pools/")
    }


def event_types(db, task_id: str) -> list[str]:
    return [event.get("type") for event in db.collection_docs(f"tasks/{task_id}/events")]


@pytest.mark.parametrize("state", ["QUEUED", "READY", "PARKED"])
def test_a_task_holding_no_capacity_is_cancelled_outright(client, db, state) -> None:
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_idle", tenant_id="eng", state=state)

    body = client.post("/v1/tasks/task_idle/cancel", headers=auth_header("alice")).json()

    assert body["released_immediately"] is True
    assert db.docs["tasks/task_idle"]["state"] == "CANCELLED"
    assert db.docs["tasks/task_idle"]["end_cause"] == "cancel_requested"


def test_a_dispatched_task_whose_worker_never_started_is_cancelled_and_freed(
    client, db, make_scheduler
) -> None:
    task_id = dispatched(client, db, make_scheduler)
    lease_id, _ = lease_of(db, task_id)
    generation = db.docs[f"tasks/{task_id}"]["current_generation"]
    assert any(active_pools(db).values()), "admission reserved nothing"

    response = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["released_immediately"] is True
    task = db.docs[f"tasks/{task_id}"]
    assert task["state"] == "CANCELLED"
    assert task["end_cause"] == "cancel_requested"
    assert task["current_lease_id"] is None
    assert task["current_generation"] == generation + 1, (
        "a container that starts late must find its generation fenced"
    )
    assert db.docs[f"leases/{lease_id}"]["released_at"] is not None
    # Invariant 2: every pool the lease reserved is back, and none went below.
    pools_now = active_pools(db)
    assert len(pools_now) > 1 and set(pools_now.values()) == {0}, pools_now
    types = event_types(db, task_id)
    assert "cancelled" in types and "lease_released" in types, types
    assert "cancel_requested" not in types


def test_a_fenced_generation_with_a_silent_lease_is_cancelled_and_freed(
    client, db, make_scheduler
) -> None:
    """The #560 state: fenced past the lease, which heartbeated once, long ago."""
    task_id = dispatched(client, db, make_scheduler)
    worker_started(db, task_id, seconds_ago=3600)
    db.docs[f"tasks/{task_id}"]["current_generation"] += 1
    lease_id, _ = lease_of(db, task_id)

    body = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice")).json()

    assert body["released_immediately"] is True
    assert db.docs[f"tasks/{task_id}"]["state"] == "CANCELLED"
    assert db.docs[f"leases/{lease_id}"]["released_at"] is not None
    assert set(active_pools(db).values()) == {0}


def test_a_live_worker_is_still_only_flagged(client, db, make_scheduler) -> None:
    task_id = dispatched(client, db, make_scheduler)
    worker_started(db, task_id)
    leases_before = copy.deepcopy(db.dump("leases/"))
    pools_before = copy.deepcopy(db.dump("pools/"))

    body = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice")).json()

    assert body["released_immediately"] is False
    task = db.docs[f"tasks/{task_id}"]
    assert task["state"] == "DISPATCHED"
    assert task["cancel_requested"] is True
    assert db.dump("leases/") == leases_before
    assert db.dump("pools/") == pools_before
    assert "cancel_requested" in event_types(db, task_id)


def test_a_fenced_worker_that_is_still_heartbeating_is_only_flagged(
    client, db, make_scheduler
) -> None:
    """Fenced seconds ago: the worker may still be stopping its agent."""
    task_id = dispatched(client, db, make_scheduler)
    worker_started(db, task_id, seconds_ago=5)
    db.docs[f"tasks/{task_id}"]["current_generation"] += 1
    pools_before = copy.deepcopy(db.dump("pools/"))

    body = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice")).json()

    assert body["released_immediately"] is False
    assert db.dump("pools/") == pools_before
