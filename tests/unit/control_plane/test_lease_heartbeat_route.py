"""`GET /v1/leases`: the heartbeat of each slot-holding task, for its own tenant.

THE GAP (#179). A worker's heartbeat is written to its LEASE
(`agent_worker/control.py` `heartbeat()`: `heartbeat_at` and `expires_at`), not
to its task, so an Agents-list row built from `GET /v1/tasks` cannot say how
long a running agent has been quiet. The one route that served a heartbeat,
`GET /v1/admin/leases`, is admin-gated, so a tenant member's list got no
"silent worker" line at all -- one of the four kinds AG-14 (#82) says must be
drawn in `--warn`.

WHAT THIS HOLDS:

  * a tenant member who is NOT an admin reads it, and reads only their own
    tenant's leases (invariant 9);
  * only a task in LEASED/DISPATCHED/STARTING/RUNNING has a row (invariant 1),
    and only through the lease the task names as current -- a superseded
    generation's unreleased lease is the reconciler's finding, not a heartbeat;
  * `heartbeat_at: null` stays null, and so does `silent_seconds`: "never beat"
    and "beat long ago" are different facts, and the admin route's fallback to
    `created_at` is exactly what this shape refuses to repeat;
  * the grace arrives with the data, resolved the way the reconciler resolves
    it, and `silent` is judged on the reconciler's own rule;
  * no free text crosses: no `last_error`, no `release_reason`, nothing a
    masker would have to catch.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api.store import Store

from .conftest import auth_header, seed_task, seed_tenant


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease(
    db,
    lease_id: str,
    *,
    tenant: str = "eng",
    task_id: str,
    beat_seconds_ago: float | None,
    created_seconds_ago: float = 900,
    generation: int = 1,
    state: str = "DISPATCHED",
    released: bool = False,
) -> None:
    created = _now() - timedelta(seconds=created_seconds_ago)
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id,
        "task_id": task_id,
        "attempt_id": f"att_{lease_id}",
        "tenant_id": tenant,
        "generation": generation,
        "pools": ["global", f"tenant:{tenant}"],
        "units": 1,
        "state": state,
        "created_at": created,
        "dispatch_deadline": created + timedelta(minutes=8),
        "expires_at": created + timedelta(minutes=30),
        # Written as an explicit null, as `acquire_lease_in_transaction` does.
        "heartbeat_at": (
            None if beat_seconds_ago is None else _now() - timedelta(seconds=beat_seconds_ago)
        ),
        "released_at": created + timedelta(minutes=1) if released else None,
        "release_reason": "terminal:SUCCEEDED" if released else None,
    }


def _task(db, task_id: str, *, tenant: str = "eng", state: str = "RUNNING",
          lease_id: str | None = None, generation: int = 1) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state=state)
    doc["current_lease_id"] = lease_id
    doc["current_generation"] = generation
    return doc


def _running(db, task_id: str, lease_id: str, *, tenant: str = "eng",
             beat_seconds_ago: float | None, state: str = "RUNNING") -> None:
    _task(db, task_id, tenant=tenant, state=state, lease_id=lease_id)
    _lease(db, lease_id, tenant=tenant, task_id=task_id, beat_seconds_ago=beat_seconds_ago)


def _page(client, user: str = "alice") -> dict[str, Any]:
    response = client.get("/v1/leases", headers=auth_header(user))
    assert response.status_code == 200, f"/v1/leases -> {response.status_code}: {response.text}"
    return response.json()


def _by_task(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["task_id"]: row for row in body["heartbeats"]}


# -- the issue's acceptance: silent shows, healthy does not, for a non-admin --

def test_a_member_who_is_not_an_admin_reads_the_heartbeat(client, db):
    """alice is in eng and not in the admin group; the admin route 403s her."""
    seed_tenant(db, "eng")
    _running(db, "task_quiet", "lease_quiet", beat_seconds_ago=600)

    assert client.get("/v1/admin/leases", headers=auth_header("alice")).status_code == 403, (
        "the premise: the only other heartbeat route is closed to a member"
    )
    row = _by_task(_page(client))["task_quiet"]
    assert row["heartbeat_at"] is not None
    assert 590 <= row["silent_seconds"] <= 660


def test_a_worker_past_the_grace_is_silent_and_a_healthy_one_is_not(client, db, api_context):
    from swarm_api.routes.admin import _heartbeat_grace_seconds

    seed_tenant(db, "eng")
    grace = _heartbeat_grace_seconds(api_context.settings.core)
    _running(db, "task_quiet", "lease_quiet", beat_seconds_ago=grace + 60)
    _running(db, "task_well", "lease_well", beat_seconds_ago=5)

    body = _page(client)
    rows = _by_task(body)
    assert rows["task_quiet"]["silent"] is True
    assert rows["task_well"]["silent"] is False
    assert body["thresholds"]["heartbeat_grace_seconds"] == grace


def test_the_grace_served_is_the_one_the_reconciler_acts_on(client, db, monkeypatch):
    """Not 90: an operator override moves the reconciler, so it moves this."""
    seed_tenant(db, "eng")
    monkeypatch.setenv("HEARTBEAT_GRACE_SECONDS", "301")
    _running(db, "task_a", "lease_a", beat_seconds_ago=200)

    body = _page(client)
    assert body["thresholds"]["heartbeat_grace_seconds"] == 301
    assert _by_task(body)["task_a"]["silent"] is False, (
        "200s quiet is inside a 301s grace; the reconciler would not act on it"
    )


# -- never beat is not beat long ago -------------------------------------------

def test_a_lease_that_never_beat_serves_null_not_its_creation_time(client, db):
    seed_tenant(db, "eng")
    _task(db, "task_new", state="DISPATCHED", lease_id="lease_new")
    _lease(db, "lease_new", task_id="task_new", beat_seconds_ago=None, created_seconds_ago=3600)

    row = _by_task(_page(client))["task_new"]
    assert row["heartbeat_at"] is None
    assert row["silent_seconds"] is None, (
        "a fallback to created_at would make 'never beat' read as 'silent for an hour'"
    )
    # Before the first beat the reconciler judges by the dispatch deadline
    # alone (detect.py `detect_stale_leases`), so no worker exists to be silent.
    assert row["silent"] is False
    assert row["dispatch_overdue"] is True


# -- tenant scope (invariant 9) ------------------------------------------------

def test_another_tenants_lease_never_appears(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _running(db, "task_eng", "lease_eng", tenant="eng", beat_seconds_ago=10)
    _running(db, "task_res", "lease_res", tenant="research", beat_seconds_ago=900)

    alice = _page(client, "alice")
    assert set(_by_task(alice)) == {"task_eng"}
    assert alice["tenant_id"] == "eng"
    assert "lease_res" not in str(alice) and "task_res" not in str(alice)

    bob = _page(client, "bob")
    assert set(_by_task(bob)) == {"task_res"}


def test_a_tenant_id_in_the_query_string_is_ignored(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _running(db, "task_res", "lease_res", tenant="research", beat_seconds_ago=900)

    response = client.get("/v1/leases?tenant_id=research", headers=auth_header("alice"))
    assert response.status_code == 200
    assert response.json()["heartbeats"] == []
    assert response.json()["tenant_id"] == "eng"


def test_a_lease_naming_another_tenants_task_is_not_served(client, db):
    """The store filters leases by tenant; the task join is tenant-checked too."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _task(db, "task_res", tenant="research", lease_id="lease_odd")
    _lease(db, "lease_odd", tenant="eng", task_id="task_res", beat_seconds_ago=10)

    assert _page(client)["heartbeats"] == []


def test_an_unauthenticated_caller_is_refused(client, db):
    seed_tenant(db, "eng")
    assert client.get("/v1/leases").status_code == 401


def test_the_store_read_refuses_to_run_without_a_tenant(db):
    """`None` means every tenant to the admin read; this one has no such value."""
    with pytest.raises(ValueError):
        Store(db).live_leases_of("")
    with pytest.raises(ValueError):
        Store(db).live_leases_of(None)  # type: ignore[arg-type]


def test_the_store_read_returns_only_that_tenants_live_leases(db):
    _lease(db, "l_eng", tenant="eng", task_id="t1", beat_seconds_ago=5)
    _lease(db, "l_done", tenant="eng", task_id="t2", beat_seconds_ago=5, released=True)
    _lease(db, "l_res", tenant="research", task_id="t3", beat_seconds_ago=5)
    assert [x.lease_id for x in Store(db).live_leases_of("eng")] == ["l_eng"]


# -- only concurrency states, only the current lease (invariant 1) -------------

@pytest.mark.parametrize("state", ["LEASED", "DISPATCHED", "STARTING", "RUNNING"])
def test_every_concurrency_state_has_a_row(client, db, state):
    seed_tenant(db, "eng")
    _running(db, "task_x", "lease_x", beat_seconds_ago=10, state=state)
    row = _by_task(_page(client))["task_x"]
    assert row["task_state"] == state


@pytest.mark.parametrize("state", ["QUEUED", "PARKED", "READY", "SUCCEEDED", "FAILED", "CANCELLED"])
def test_a_task_outside_the_concurrency_states_has_no_row(client, db, state):
    """A terminal task with a lease not yet released holds no worker to watch."""
    seed_tenant(db, "eng")
    _running(db, "task_x", "lease_x", beat_seconds_ago=600, state=state)
    assert _page(client)["heartbeats"] == []


def test_a_released_lease_has_no_row(client, db):
    seed_tenant(db, "eng")
    _task(db, "task_x", lease_id="lease_x")
    _lease(db, "lease_x", task_id="task_x", beat_seconds_ago=600, released=True)
    assert _page(client)["heartbeats"] == []


def test_a_superseded_generations_lease_is_not_the_tasks_heartbeat(client, db):
    """Generation 1 left unreleased beside generation 2 (the reconciler's
    ORPHAN_LEASE): its silence is not the running worker's."""
    seed_tenant(db, "eng")
    _task(db, "task_x", lease_id="lease_g2", generation=2)
    _lease(db, "lease_g1", task_id="task_x", beat_seconds_ago=3000, generation=1)
    _lease(db, "lease_g2", task_id="task_x", beat_seconds_ago=5, generation=2,
           created_seconds_ago=60)

    rows = _page(client)["heartbeats"]
    assert [r["lease_id"] for r in rows] == ["lease_g2"]
    assert rows[0]["silent"] is False


# -- nothing a masker would have to catch --------------------------------------

def test_a_row_carries_no_free_text(client, db):
    """`last_error` can be an agent's stderr tail and `release_reason` a
    writer's string; neither is needed to say a worker is quiet, so neither
    is served and there is nothing on this route to mask."""
    seed_tenant(db, "eng")
    doc = _task(db, "task_x", lease_id="lease_x")
    doc["last_error"] = "stderr tail that must not reach this route"
    _lease(db, "lease_x", task_id="task_x", beat_seconds_ago=10)

    body = _page(client)
    row = body["heartbeats"][0]
    assert set(row) == {
        "task_id", "lease_id", "attempt_id", "generation", "task_state",
        "dispatch_state", "created_at", "dispatch_deadline", "expires_at",
        "heartbeat_at", "silent_seconds", "silent", "expired", "dispatch_overdue",
    }
    assert "stderr tail" not in str(body)


def test_the_page_says_when_it_was_read_and_what_it_covers(client, db):
    seed_tenant(db, "eng")
    _running(db, "task_x", "lease_x", beat_seconds_ago=10)
    body = _page(client)
    assert set(body) == {"tenant_id", "read_at", "thresholds", "heartbeats"}
    assert set(body["thresholds"]) == {"heartbeat_grace_seconds", "lease_timeout_seconds"}
