"""Two admin reads that could not say how much they covered, or when.

Both were recorded on 2026-09-20 in
docs/audits/2026-09-20/data-gaps-found-by-fanout.md, sections 1 and 2.

1. `GET /v1/admin/leases` reads the newest `limit` lease documents and only
   THEN drops the released ones. A short page therefore meant either "few
   leases hold capacity" or "the window filled with released ones before it
   reached the live ones", and nothing in the response told them apart. The
   Holders screen's accounting-drift check compares `units_held` against
   `pool.active`; a silently cut window produces a delta that looks exactly
   like a leaked lease. `truncated` and `examined` are that signal.

2. `GET /v1/admin/quota` carried no `generated_at`, while `/v1/capacity`,
   `/v1/stats` and `/v1/providers` all do. A panel built on it could only show
   when the bytes ARRIVED, not when the platform computed them.

Offline: the real routes over FakeFirestore, with the clock injected through
`build_context(now=...)` -- the same constructor the process uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header

NOW = datetime(2026, 9, 24, 10, 30, 15, 123456, tzinfo=timezone.utc)


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


def _lease(db, lease_id: str, tenant: str, *, minutes_ago: int, released: bool, units: int = 1):
    created = NOW - timedelta(minutes=minutes_ago)
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id,
        "task_id": f"task_{lease_id}",
        "attempt_id": f"att_{lease_id}",
        "tenant_id": tenant,
        "generation": 1,
        "pools": ["global", f"tenant:{tenant}"],
        "units": units,
        "state": "DISPATCHED",
        "created_at": created,
        "dispatch_deadline": created + timedelta(minutes=5),
        "expires_at": created + timedelta(minutes=30),
        "heartbeat_at": created + timedelta(seconds=30),
        "released_at": created + timedelta(minutes=1) if released else None,
        "release_reason": "terminal:SUCCEEDED" if released else None,
    }


def _leases(client, **params) -> dict:
    response = client.get("/v1/admin/leases", params=params, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return response.json()


# -- 1. /v1/admin/leases says when its window was cut -------------------------


def test_a_window_full_of_released_leases_says_it_was_cut(fixed_client, db):
    """THE CASE FROM THE AUDIT. The three newest documents are released; the
    one lease still holding capacity is older than all of them. At limit=3 the
    active-only page is EMPTY -- and before this change it was an empty page
    with nothing to say it was not the whole answer."""
    for i in range(3):
        _lease(db, f"l_done{i}", "eng", minutes_ago=i + 1, released=True)
    _lease(db, "l_live", "eng", minutes_ago=60, released=False, units=4)

    body = _leases(fixed_client, limit=3)

    assert body["leases"] == []
    assert body["truncated"] is True, (
        "an empty active-only page whose window filled with released leases "
        "must say so; otherwise 'nothing holds capacity' and 'did not look far "
        "enough' are the same response"
    )
    assert body["examined"] == 3
    assert body["units_held"] == 0


def test_a_window_that_reached_the_end_says_it_was_not_cut(fixed_client, db):
    """The other half. A flag that is always true proves nothing."""
    for i in range(3):
        _lease(db, f"l_done{i}", "eng", minutes_ago=i + 1, released=True)
    _lease(db, "l_live", "eng", minutes_ago=60, released=False, units=4)

    # Exactly as many documents as the window: reaching the last one is not a cut.
    body = _leases(fixed_client, limit=4)

    assert [row["lease_id"] for row in body["leases"]] == ["l_live"]
    assert body["truncated"] is False
    assert body["examined"] == 4
    assert body["units_held"] == 4


def test_examined_counts_documents_read_not_rows_returned(fixed_client, db):
    """`examined` is the unfiltered count the audit asked for: what the
    `active_only`, `state` and `overdue_only` filters were applied TO."""
    for i in range(5):
        _lease(db, f"l{i}", "eng", minutes_ago=i + 1, released=(i % 2 == 0))

    body = _leases(fixed_client, limit=10)
    assert len(body["leases"]) == 2
    assert body["examined"] == 5
    assert body["truncated"] is False

    everything = _leases(fixed_client, limit=10, active_only="false")
    assert len(everything["leases"]) == 5
    assert everything["examined"] == 5


def test_the_tenant_filter_bounds_what_is_examined(fixed_client, db):
    for i in range(4):
        _lease(db, f"l_eng{i}", "eng", minutes_ago=i + 1, released=False)
    for i in range(4):
        _lease(db, f"l_res{i}", "research", minutes_ago=i + 1, released=False)

    body = _leases(fixed_client, limit=4, tenant_id="eng")
    assert body["examined"] == 4
    assert body["truncated"] is False, (
        "research's leases are outside the query, so they cannot make eng's "
        "window look cut"
    )


def test_the_store_reports_the_same_window_the_route_does(db):
    """The flag is computed where the query runs, not guessed from the page
    length by the route -- a route that inferred `truncated` from
    `len(leases) == limit` would be wrong exactly when released leases were
    filtered out, which is the case this exists for."""
    from swarm_api.store import Store

    for i in range(3):
        _lease(db, f"l_done{i}", "eng", minutes_ago=i + 1, released=True)
    _lease(db, "l_live", "eng", minutes_ago=60, released=False)

    scan = Store(db).scan_leases("eng", limit=3)
    assert scan.leases == []
    assert scan.truncated is True
    assert scan.examined == 3

    # And `list_leases` -- the method existing callers use -- is unchanged.
    assert Store(db).list_leases("eng", limit=3) == []
    assert [x.lease_id for x in Store(db).list_leases("eng", limit=4)] == ["l_live"]


# -- 2. /v1/admin/quota says when it was computed ------------------------------


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_admin_quota_carries_the_servers_own_generated_at(fixed_client, db):
    db.docs["quota/anthropic:eng"] = {
        "provider": "anthropic", "tenant_id": "eng", "state": "AVAILABLE",
        "updated_at": NOW - timedelta(minutes=3), "configured_hard_max": 50,
        "adaptive_target": 10, "quota_derived_limit": None,
        "requests_remaining": None, "tokens_remaining": None, "reset_at": None,
        "cooldown_until": None, "last_429_at": None, "retry_after_seconds": None,
        "success_count": 0, "rate_limit_count": 0,
    }

    response = fixed_client.get("/v1/admin/quota", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()

    assert "generated_at" in body, (
        "/v1/admin/quota must say when the platform computed it, as "
        "/v1/capacity, /v1/stats and /v1/providers already do"
    )
    assert _parse(body["generated_at"]) == NOW, (
        "generated_at must be the server's clock, not a document's updated_at"
    )


def test_an_empty_quota_answer_is_still_dated(fixed_client):
    """No quota documents is an answer, and an answer has a time."""
    response = fixed_client.get("/v1/admin/quota", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quota"] == []
    assert _parse(body["generated_at"]) == NOW
