"""Two admin reads that could not say how much they covered, or when.

Both were recorded on 2026-09-20 in
docs/audits/2026-09-20/data-gaps-found-by-fanout.md, sections 1 and 2.

1. `GET /v1/admin/leases` read the newest `limit` lease documents and only
   THEN dropped the released ones. A short page therefore meant either "few
   leases hold capacity" or "the window filled with released ones before it
   reached the live ones", and nothing in the response told them apart. The
   Holders screen's accounting-drift check compares `units_held` against
   `pool.active`; a silently cut window produces a delta that looks exactly
   like a leaked lease.

   THE FIRST ATTEMPT AT A SIGNAL ANSWERED THE WRONG QUESTION. `truncated` was
   "at least one lease document of any state is older than the window". Lease
   documents are never deleted and carry no TTL (indexes.tf expires only
   `events` and `reconciler_passes`), so after an environment's 201st
   admission that flag was true on every call, for ever, and a leak and a
   short read were again the same response. Its tests only ever built fewer
   documents than `limit`, a state a real deployment has already left.

   So the tests below build MORE documents than the window, and pin the
   property the drift check needs: is a LIVE lease outside these rows?
   `active_beyond_window` is that number, and `active_only` now reads the
   live set itself rather than filtering a window of history.

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


# -- 1. /v1/admin/leases says whether a live lease lies outside its rows -----

# The Holders screen asks for this window (apps/swarm-ui/src/api.ts), so the
# histories below hold more lease documents than it does.
WINDOW = 200


def _history(db, *, live: int, released: int, tenant: str = "eng") -> None:
    """`live` unreleased leases, newest first, then `released` older ones.

    `live + released > WINDOW` is the point: a real environment has admitted
    more than 200 attempts over its life, and every one left a document.
    """
    for i in range(live):
        _lease(db, f"l_live{i:03d}", tenant, minutes_ago=i + 1, released=False)
    for i in range(released):
        _lease(db, f"l_done{i:03d}", tenant, minutes_ago=live + 10 + i, released=True)


def test_the_audit_case_returns_the_live_lease_it_used_to_hide(fixed_client, db):
    """THE CASE FROM THE AUDIT. The three newest documents are released; the
    one lease still holding capacity is older than all of them. At limit=3 the
    active-only page used to be EMPTY. Reading the live set rather than a
    window of history is what puts the lease on the page."""
    for i in range(3):
        _lease(db, f"l_done{i}", "eng", minutes_ago=i + 1, released=True)
    _lease(db, "l_live", "eng", minutes_ago=60, released=False, units=4)

    body = _leases(fixed_client, limit=3)

    assert [row["lease_id"] for row in body["leases"]] == ["l_live"], (
        "a lease that holds capacity must be on the Holders page however many "
        "released leases were admitted after it"
    )
    assert body["units_held"] == 4
    assert body["active_beyond_window"] == 0
    assert body["truncated"] is False
    assert body["examined"] == 1


def test_released_history_past_the_window_is_not_a_cut(fixed_client, db):
    """THE REVIEWER'S CASE. More released documents than the window, all older
    than the three live leases. Nothing live lies outside the rows, and the
    response must say so -- a flag that is true whenever older documents exist
    is true on every call a real deployment ever makes."""
    _history(db, live=3, released=WINDOW + 47)

    body = _leases(fixed_client, limit=WINDOW)

    assert [row["lease_id"] for row in body["leases"]] == [
        "l_live000", "l_live001", "l_live002",
    ]
    assert body["active_beyond_window"] == 0, (
        "no live lease lies outside the rows; released history past the "
        "window is not a cut"
    )
    assert body["truncated"] is False, (
        "active_only rows were cut only if more LIVE leases exist than fit; "
        "older released documents are not rows this read could have returned"
    )
    assert body["units_held"] == 3

    # The history listing IS cut -- older documents exist -- but it too must
    # say that nothing live was left out.
    history = _leases(fixed_client, limit=WINDOW, active_only="false")
    assert len(history["leases"]) == WINDOW
    assert history["truncated"] is True
    assert history["active_beyond_window"] == 0


def test_a_live_lease_past_the_window_is_reported(fixed_client, db):
    """The inverse. The same history, plus one leaked lease far older than the
    window -- the finding's document #4,000. The response must differ from
    the clean one in a field the drift check reads."""
    _history(db, live=3, released=WINDOW + 47)
    _lease(db, "l_leaked", "eng", minutes_ago=10_000, released=False, units=2)

    # The history window does not reach it, and says a live lease was cut.
    history = _leases(fixed_client, limit=WINDOW, active_only="false")
    assert "l_leaked" not in [row["lease_id"] for row in history["leases"]]
    assert history["active_beyond_window"] == 1, (
        "a live lease older than the history window must be counted as cut"
    )

    # The live read reaches it, so the Holders page shows the leak itself.
    live = _leases(fixed_client, limit=WINDOW)
    assert [row["lease_id"] for row in live["leases"]] == [
        "l_live000", "l_live001", "l_live002", "l_leaked",
    ]
    assert live["units_held"] == 5
    assert live["active_beyond_window"] == 0
    assert live["truncated"] is False


def test_more_live_leases_than_the_window_is_a_cut(fixed_client, db):
    """The active-only read can still be cut, by live leases alone, and must
    say by how many. The rows are the NEWEST live leases."""
    _history(db, live=5, released=4)

    body = _leases(fixed_client, limit=3)

    assert [row["lease_id"] for row in body["leases"]] == [
        "l_live000", "l_live001", "l_live002",
    ]
    assert body["truncated"] is True
    assert body["active_beyond_window"] == 2
    assert body["examined"] == 3


def test_examined_is_what_the_state_and_overdue_filters_ran_over(fixed_client, db):
    """`examined` is the unfiltered count the audit asked for: the rows the
    `state` and `overdue_only` filters were applied TO. Every lease below is
    DISPATCHED, so a LEASED filter leaves nothing -- out of two examined."""
    for i in range(5):
        _lease(db, f"l{i}", "eng", minutes_ago=i + 1, released=(i % 2 == 0))

    body = _leases(fixed_client, limit=10, state="LEASED")
    assert body["leases"] == []
    assert body["examined"] == 2
    assert body["active_beyond_window"] == 0

    everything = _leases(fixed_client, limit=10, active_only="false")
    assert len(everything["leases"]) == 5
    assert everything["examined"] == 5
    assert everything["truncated"] is False
    assert everything["active_beyond_window"] == 0


def test_the_tenant_filter_bounds_what_is_counted(fixed_client, db):
    for i in range(4):
        _lease(db, f"l_eng{i}", "eng", minutes_ago=i + 1, released=False)
    for i in range(4):
        _lease(db, f"l_res{i}", "research", minutes_ago=100 + i, released=False)

    body = _leases(fixed_client, limit=4, tenant_id="eng")
    assert body["examined"] == 4
    assert body["truncated"] is False, (
        "research's leases are outside the query, so they cannot make eng's "
        "window look cut"
    )
    assert body["active_beyond_window"] == 0

    history = _leases(fixed_client, limit=4, tenant_id="eng", active_only="false")
    assert history["active_beyond_window"] == 0, (
        "research's older live leases are not eng's, and are not beyond eng's window"
    )

    everyone = _leases(fixed_client, limit=4, active_only="false")
    assert everyone["active_beyond_window"] == 4


def test_the_store_reports_the_same_answer_the_route_does(db):
    """Computed where the query runs, not guessed from the page length by the
    route -- `len(leases) == limit` is false in exactly the case this exists
    for."""
    from swarm_api.store import Store

    for i in range(3):
        _lease(db, f"l_done{i}", "eng", minutes_ago=i + 1, released=True)
    _lease(db, "l_live", "eng", minutes_ago=60, released=False)

    scan = Store(db).scan_leases("eng", limit=3)
    assert [x.lease_id for x in scan.leases] == ["l_live"]
    assert scan.truncated is False
    assert scan.active_beyond_window == 0
    assert scan.examined == 1

    history = Store(db).scan_leases("eng", active_only=False, limit=3)
    assert [x.lease_id for x in history.leases] == ["l_done0", "l_done1", "l_done2"]
    assert history.truncated is True
    assert history.active_beyond_window == 1

    # `list_leases` -- the method existing callers use -- returns the live
    # lease too; hiding it behind newer released ones was the defect.
    assert [x.lease_id for x in Store(db).list_leases("eng", limit=3)] == ["l_live"]


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
