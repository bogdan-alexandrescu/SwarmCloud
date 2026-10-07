"""`/v1/admin/quota` serves the ceiling of the pool each row's cap feeds (G5-02).

QA, 2026-10-07: Provider quota showed `Quota cap 50` for anthropic . eng, and
Pools showed the pool it feeds, `provider:anthropic:tenant:eng`, at 40. Both
routes were reporting a true figure about a DIFFERENT thing --
`/v1/admin/quota`'s `effective_limit` is the quota document's
(`QuotaState.effective_limit`: configured hard max, AIMD target, provider cap),
`/v1/capacity`'s is the pool's (`SlotPool.effective_limit`: hard limit, AIMD
target, quota-derived limit) -- but only the pool's is what admission enforces
(`SlotPool.has_capacity`), and the quota route gave no way to see it. An admin
reading Quota alone would believe the tenant can run 50.

The pool's figure is the right answer to "how many may this tenant run on this
provider", so the quota route now serves it beside the cap, from the same
request, as `feeds_pool` -- additively: every existing key is unchanged. A
pool document that does not exist is `null`, never a stand-in pool.

Offline: the real route over FakeFirestore.
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

NOW = datetime(2026, 10, 7, 5, 28, 51, tzinfo=timezone.utc)


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


def _quota(db, provider: str, tenant: str) -> None:
    db.docs[f"quota/{provider}:{tenant}"] = {
        "provider": provider, "tenant_id": tenant, "state": "AVAILABLE",
        "updated_at": NOW - timedelta(minutes=1), "configured_hard_max": 50,
        "adaptive_target": None, "quota_derived_limit": None,
        "requests_remaining": None, "tokens_remaining": None, "reset_at": None,
        "cooldown_until": None, "last_429_at": None, "retry_after_seconds": None,
        "success_count": 0, "rate_limit_count": 0,
    }


def _rows(client) -> dict[str, dict]:
    response = client.get("/v1/admin/quota", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return {row["tenant_id"]: row for row in response.json()["quota"]}


def test_each_row_carries_the_ceiling_of_the_pool_its_cap_feeds(fixed_client, db):
    _quota(db, "anthropic", "eng")
    db.docs["pools/provider:anthropic:tenant:eng"] = {
        "hard_limit": 40, "adaptive_target": None, "quota_derived_limit": 50,
        "active": 2, "enabled": True, "updated_at": NOW - timedelta(minutes=2),
    }

    row = _rows(fixed_client)["eng"]

    # The quota document's own figure is unchanged: additive, not a rename.
    assert row["effective_limit"] == 50
    pool = row.get("feeds_pool")
    assert pool is not None, "the row does not say what the pool it feeds is capped at"
    assert pool["name"] == "provider:anthropic:tenant:eng"
    assert pool["hard_limit"] == 40
    assert pool["quota_derived_limit"] == 50
    # The figure admission enforces: min(40, 50).
    assert pool["effective_limit"] == 40


def test_a_pool_that_does_not_exist_is_null_not_a_stand_in(fixed_client, db):
    _quota(db, "anthropic", "smoke")

    row = _rows(fixed_client)["smoke"]

    assert "feeds_pool" in row
    assert row["feeds_pool"] is None


def test_each_row_is_matched_to_its_own_tenants_pool(fixed_client, db):
    _quota(db, "anthropic", "eng")
    _quota(db, "anthropic", "u-bogdan")
    db.docs["pools/provider:anthropic:tenant:eng"] = {"hard_limit": 40, "active": 0, "enabled": True}
    db.docs["pools/provider:anthropic:tenant:u-bogdan"] = {"hard_limit": 12, "active": 0, "enabled": True}

    rows = _rows(fixed_client)

    assert rows["eng"]["feeds_pool"]["effective_limit"] == 40
    assert rows["u-bogdan"]["feeds_pool"]["effective_limit"] == 12
