"""/v1/capacity serves each pool's applied limit beside its ceiling, and why (#3047).

The applied value is `effective_limit`; `lowered_by` names the input that holds
it below `hard_limit` (the AIMD target, the quota-derived value) and when the
broker last set it, read from the broker's quota document for a per-tenant
provider pool. Offline: FakeFirestore, no credentials, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from .conftest import auth_header, seed_pool, seed_tenant

TENANT = "eng"
POOL = f"provider:anthropic:tenant:{TENANT}"
SET_AT = datetime(2026, 10, 24, 10, 31, tzinfo=timezone.utc)
CUT_AT = datetime(2026, 10, 24, 10, 30, tzinfo=timezone.utc)


def _pools(client) -> dict[str, dict]:
    response = client.get("/v1/capacity", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return {p["name"]: p for p in response.json()["pools"]}


def _seed(db, *, target: int | None) -> str:
    seed_tenant(db, TENANT, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=64)
    seed_pool(db, POOL, hard_limit=40, active=7, adaptive_target=target)
    fake = secrets.token_hex(16)
    db.docs[f"quota/anthropic:{TENANT}"] = {
        "provider": "anthropic", "tenant_id": TENANT, "state": "available",
        "configured_hard_max": 50, "adaptive_target": target,
        "updated_at": SET_AT, "last_429_at": CUT_AT, "api_key_hint": fake,
    }
    return fake


def test_capacity_serves_an_adaptive_target_of_7_beside_the_ceiling_of_40_and_why(client, db):
    fake = _seed(db, target=7)

    pool = _pools(client)[POOL]

    assert pool["effective_limit"] == 7
    assert pool["hard_limit"] == 40
    assert pool["lowered_by"] == [
        {
            "input": "adaptive_target",
            "value": 7,
            "set_at": SET_AT.isoformat(),
            "last_rate_limited_at": CUT_AT.isoformat(),
        }
    ]
    assert fake not in repr(pool)


def test_a_pool_at_its_ceiling_is_lowered_by_nothing(client, db):
    _seed(db, target=None)

    pools = _pools(client)

    assert pools[POOL]["effective_limit"] == 40
    assert pools[POOL]["lowered_by"] == []
    assert pools["global"]["lowered_by"] == []
