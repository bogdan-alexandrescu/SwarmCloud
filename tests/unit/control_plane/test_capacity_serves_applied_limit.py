"""/v1/capacity serves each pool's applied limit beside its ceiling, and why (#3047).

The applied value is `effective_limit`; `lowered_by` names the input that holds
it below `hard_limit` (the AIMD target, the quota-derived value, an admin
override) and when that value was set -- the override's audit instant, else the
rate limit that cut it -- never the quota document's last write, which the
broker bumps on every success and is served apart as `quota_written_at`. Offline: FakeFirestore, no credentials, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from scheduler.limits import lowered_by, read_pool

from .conftest import auth_header, seed_pool, seed_tenant

TENANT = "eng"
POOL = f"provider:anthropic:tenant:{TENANT}"
CUT_AT = datetime(2026, 10, 24, 10, 30, tzinfo=timezone.utc)
#: The broker's last write, hours after the cut: a success that left the target
#: at 7 still bumps `updated_at`.
WRITTEN_AT = datetime(2026, 10, 24, 15, 58, tzinfo=timezone.utc)


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
        "updated_at": WRITTEN_AT, "last_429_at": CUT_AT, "api_key_hint": fake,
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
            "set_at": CUT_AT.isoformat(),
            "set_by": "rate_limit",
            "quota_written_at": WRITTEN_AT.isoformat(),
            "last_rate_limited_at": CUT_AT.isoformat(),
        }
    ]
    assert fake not in repr(pool)


def test_capacity_dates_a_target_an_admin_set_by_the_override_and_names_it(client, db):
    fake = _seed(db, target=7)
    reset_at = datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc)
    db.docs["quota_audit/audit_1"] = {
        "action": "adaptive_target_reset", "provider": "anthropic", "tenant_id": TENANT,
        "to_target": 7, "at": reset_at, "requested_by": "admin@example.com", "reason": fake,
    }

    lowered = _pools(client)[POOL]["lowered_by"]

    assert [(e["input"], e["set_at"]) for e in lowered] == [
        ("adaptive_target", reset_at.isoformat()),
        ("override", reset_at.isoformat()),
    ]
    assert lowered[0]["set_by"] == "override"
    assert lowered[1]["action"] == "adaptive_target_reset"
    assert fake not in repr(lowered) and "admin@example.com" not in repr(lowered)


def test_capacity_and_the_scheduler_record_the_same_lowered_by(client, db):
    """The two copies of the rule (scheduler and swarm-api) cannot drift apart."""
    _seed(db, target=7)
    db.docs["quota_audit/audit_1"] = {
        "action": "adaptive_target_reset", "provider": "anthropic", "tenant_id": TENANT,
        "to_target": 7, "at": datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc),
    }
    served = _pools(client)[POOL]["lowered_by"]

    reading = read_pool(db, POOL, datetime.now(timezone.utc))
    assert reading is not None
    assert served == lowered_by(reading, 7)


def test_a_pool_at_its_ceiling_is_lowered_by_nothing(client, db):
    _seed(db, target=None)

    pools = _pools(client)

    assert pools[POOL]["effective_limit"] == 40
    assert pools[POOL]["lowered_by"] == []
    assert pools["global"]["lowered_by"] == []
