"""`dispatch_overdue` is cleared by a worker that started, not by the lease's state.

Live QA, Capacity > Holders (`GET /v1/admin/leases`), 2026-10-11 ~12:30Z: 9 of
13 healthy, heartbeating leases came back `dispatch_overdue: true`, and their
Dispatch column read "dispatched" after 30+ minutes RUNNING. Both are the same
fact: nothing writes STARTING or RUNNING to a lease document (the worker
advances the TASK and touches the lease only to heartbeat), and the flag was
`Lease.dispatch_overdue()` -- "the deadline has passed" -- which is true of
every agent that outlives the dispatch window.

The fix is read-side (`codec.lease_dispatch_overdue`): a first heartbeat on the
lease, or the lease's task in STARTING/RUNNING on this lease, is proof the
dispatch landed. Nothing new is written, so the heartbeat path and admission's
one transaction are untouched.

Both halves are pinned, because each alone passes a constant: a healthy RUNNING
lease long past its deadline is NOT overdue, and a lease dispatched long ago
whose task never started still IS -- under DISPATCHED, which is what
`mark_dispatched` writes before any container runs (contract change request 9).

Offline: the real routes over FakeFirestore, the clock injected through
`build_context(now=...)`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.codec import lease_dispatch_overdue, lease_from_dict, task_from_dict
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_task, seed_tenant

NOW = datetime(2026, 10, 11, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def fixed_client(db, tokens, group_map, objects) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=lambda: NOW,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _seed(
    db,
    name: str,
    *,
    task_state: str,
    minutes_ago: int,
    beat_seconds_ago: float | None,
    tenant: str = "eng",
    lease_state: str = "DISPATCHED",
    task_generation: int = 1,
    task_lease: str | None = "same",
) -> None:
    """A task and its lease, `minutes_ago` old, with an 8-minute dispatch window."""
    created = NOW - timedelta(minutes=minutes_ago)
    lease_id = f"lease_{name}"
    task = seed_task(db, task_id=f"task_{name}", tenant_id=tenant, state=task_state)
    task["current_lease_id"] = lease_id if task_lease == "same" else task_lease
    task["current_generation"] = task_generation
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id,
        "task_id": f"task_{name}",
        "attempt_id": f"att_{name}",
        "tenant_id": tenant,
        "generation": 1,
        "pools": ["global", f"tenant:{tenant}"],
        "units": 1,
        # Only ever LEASED or DISPATCHED -- the premise of the defect.
        "state": lease_state,
        "created_at": created,
        "dispatch_deadline": created + timedelta(minutes=8),
        "expires_at": NOW + timedelta(minutes=2),
        "heartbeat_at": (
            None if beat_seconds_ago is None else NOW - timedelta(seconds=beat_seconds_ago)
        ),
        "released_at": None,
        "release_reason": None,
    }


def _admin_rows(client, **params) -> dict[str, dict]:
    response = client.get("/v1/admin/leases", params=params, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return {row["task_id"]: row for row in response.json()["leases"]}


def _tenant_rows(client) -> dict[str, dict]:
    response = client.get("/v1/leases", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return {row["task_id"]: row for row in response.json()["heartbeats"]}


# -- the QA finding, on the admin route ----------------------------------------

def test_a_running_heartbeating_task_is_not_overdue(fixed_client, db):
    """THE QA FINDING: 35 minutes RUNNING, beat 10s ago, lease still DISPATCHED."""
    _seed(db, "healthy", task_state="RUNNING", minutes_ago=35, beat_seconds_ago=10)

    row = _admin_rows(fixed_client)["task_healthy"]
    assert row["dispatch_state"] == "DISPATCHED", "the premise: the lease never advances"
    assert row["dispatch_overdue"] is False, (
        "a RUNNING task beating every few seconds was dispatched; its deadline "
        "passing 27 minutes ago says nothing about it"
    )


def test_a_lease_dispatched_long_ago_whose_task_never_started_is_overdue(fixed_client, db):
    """The other half: without it, `return False` passes the test above."""
    _seed(db, "stuck", task_state="DISPATCHED", minutes_ago=35, beat_seconds_ago=None)

    row = _admin_rows(fixed_client)["task_stuck"]
    assert row["dispatch_overdue"] is True, (
        "DISPATCHED is written when the backend accepts the create call; 27 "
        "minutes past the deadline with no beat and no STARTING is a lost dispatch"
    )


def test_a_still_leased_lease_whose_task_never_started_is_overdue(fixed_client, db):
    _seed(db, "leased", task_state="LEASED", minutes_ago=35, beat_seconds_ago=None,
          lease_state="LEASED")
    assert _admin_rows(fixed_client)["task_leased"]["dispatch_overdue"] is True


def test_the_qa_mix_flags_only_the_one_that_never_started(fixed_client, db):
    """13 holders, as on the screen: 12 healthy and old, one never started."""
    for i in range(12):
        _seed(db, f"ok{i:02d}", task_state="RUNNING", minutes_ago=30 + i, beat_seconds_ago=8)
    _seed(db, "lost", task_state="DISPATCHED", minutes_ago=20, beat_seconds_ago=None)

    rows = _admin_rows(fixed_client)
    assert len(rows) == 13
    assert [t for t, r in rows.items() if r["dispatch_overdue"]] == ["task_lost"]


def test_overdue_only_returns_the_stuck_lease_and_not_the_running_ones(fixed_client, db):
    """`overdue_only=1` is the operator's query; it filters on the same rule."""
    _seed(db, "healthy", task_state="RUNNING", minutes_ago=35, beat_seconds_ago=10)
    _seed(db, "stuck", task_state="DISPATCHED", minutes_ago=35, beat_seconds_ago=None)
    _seed(db, "young", task_state="DISPATCHED", minutes_ago=2, beat_seconds_ago=None)

    assert list(_admin_rows(fixed_client, overdue_only="true")) == ["task_stuck"]


def test_inside_the_window_a_booting_task_is_not_overdue(fixed_client, db):
    _seed(db, "young", task_state="DISPATCHED", minutes_ago=2, beat_seconds_ago=None)
    assert _admin_rows(fixed_client)["task_young"]["dispatch_overdue"] is False


# -- either proof clears it ------------------------------------------------------

def test_a_starting_task_before_its_first_beat_is_not_overdue(fixed_client, db):
    """The worker wrote STARTING, so a container ran, even if no beat has landed."""
    _seed(db, "booting", task_state="STARTING", minutes_ago=9, beat_seconds_ago=None)
    assert _admin_rows(fixed_client)["task_booting"]["dispatch_overdue"] is False


def test_a_task_running_on_a_later_attempt_does_not_clear_this_lease(fixed_client, db):
    """RUNNING on generation 2 says nothing about generation 1's dispatch."""
    _seed(db, "old", task_state="RUNNING", minutes_ago=35, beat_seconds_ago=None,
          task_generation=2, task_lease="lease_newer")
    assert _admin_rows(fixed_client)["task_old"]["dispatch_overdue"] is True


# -- the tenant route serves the same flag -------------------------------------

def test_the_tenant_heartbeat_route_agrees(db, tokens, group_map, objects):
    seed_tenant(db, "eng")
    _seed(db, "healthy", task_state="RUNNING", minutes_ago=35, beat_seconds_ago=10)
    _seed(db, "stuck", task_state="DISPATCHED", minutes_ago=35, beat_seconds_ago=None)
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects, now=lambda: NOW,
    )
    rows = _tenant_rows(TestClient(create_app(ctx), raise_server_exceptions=False))
    assert rows["task_healthy"]["dispatch_overdue"] is False
    assert rows["task_stuck"]["dispatch_overdue"] is True


# -- the predicate, without a route ----------------------------------------------

def test_the_predicate_without_a_task_still_honours_the_heartbeat(db):
    """A caller with no task in hand (the codec default) still clears on a beat."""
    _seed(db, "beat", task_state="RUNNING", minutes_ago=35, beat_seconds_ago=10)
    _seed(db, "silent", task_state="DISPATCHED", minutes_ago=35, beat_seconds_ago=None)
    beat = lease_from_dict(db.docs["leases/lease_beat"])
    silent = lease_from_dict(db.docs["leases/lease_silent"])
    assert lease_dispatch_overdue(beat, None, NOW) is False
    assert lease_dispatch_overdue(silent, None, NOW) is True
    # And the frozen predicate is unchanged: both deadlines HAVE passed.
    assert beat.dispatch_overdue(NOW) is True


def test_the_predicate_reads_task_state_not_lease_state(db):
    _seed(db, "t", task_state="RUNNING", minutes_ago=35, beat_seconds_ago=None)
    lease = lease_from_dict(db.docs["leases/lease_t"])
    task = task_from_dict(db.docs["tasks/task_t"])
    assert lease.state.value == "DISPATCHED"
    assert lease_dispatch_overdue(lease, task, NOW) is False
