"""Raising a per-tenant provider pool moves the broker's maximum with it.

The owner's follow-up of 2 November 2026 ("pool route also calls the
broker"). A `provider:<p>:tenant:<t>` pool's effective limit is the least of
its `hard_limit`, the quota document's `configured_hard_max` and the AIMD
target, so `PUT /v1/admin/limits/provider/<p>/tenant/<t>` raising the pool
alone could leave the quota document's maximum lower and change nothing.
Pinned here:

  * the route calls the broker's `set_hard_max` with the same number, as the
    token's verified admin, and the pool takes the number too;
  * a broker refusal fails the request with the broker's reason and leaves
    the pool as it was, with no audit row -- no half write;
  * one `admin_audit` row names both values, the pool's limit and the
    broker's maximum, before and after.

No credentials, no network: a fake broker and the shared FakeFirestore.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.brokerclient import AccountPoolUnavailable
from swarm_api.errors import ValidationFailed
from swarm_api.main import create_app

from .conftest import auth_header, seed_pool, seed_tenant
from .fakes import FakeFirestore

PROVIDER = "anthropic"
TENANT = "eng"
ADMIN = "root@example.com"
POOL = f"provider:{PROVIDER}:tenant:{TENANT}"
ROUTE = f"/v1/admin/limits/provider/{PROVIDER}/tenant/{TENANT}"


class _Broker:
    """The broker's `set_hard_max`, as the real one answers it: the quota
    document and the pool's `hard_limit` move together, the target clamped."""

    def __init__(self, db: FakeFirestore, refuse: Exception | None = None) -> None:
        self.db = db
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    def set_hard_max(self, provider: str, tenant_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"provider": provider, "tenant_id": tenant_id, **kwargs})
        if self.refuse is not None:
            raise self.refuse
        quota = self.db.docs[f"quota/{provider}:{tenant_id}"]
        quota["configured_hard_max"] = kwargs["hard_max"]
        quota["adaptive_target"] = min(quota["adaptive_target"], kwargs["hard_max"])
        self.db.docs[f"pools/provider:{provider}:tenant:{tenant_id}"]["hard_limit"] = kwargs["hard_max"]
        return {"quota": {"provider": provider, "tenant_id": tenant_id,
                          "configured_hard_max": quota["configured_hard_max"],
                          "adaptive_target": quota["adaptive_target"]}}


def _seed(db: FakeFirestore) -> None:
    seed_tenant(db, TENANT)
    seed_pool(db, POOL, hard_limit=40, active=3, adaptive_target=7)
    db.docs[f"quota/{PROVIDER}:{TENANT}"] = {
        "provider": PROVIDER, "tenant_id": TENANT, "state": "AVAILABLE",
        "configured_hard_max": 40, "adaptive_target": 7, "success_count": 0,
        "rate_limit_count": 0,
    }


def _client(api_context, broker: _Broker) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


def _audits(db: FakeFirestore) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items() if path.startswith(f"{AUDIT_COLLECTION}/")
            and doc.get("action") == "limit_provider_tenant_set"]


def test_raising_the_pool_moves_the_brokers_maximum_with_it(api_context, db):
    _seed(db)
    broker = _Broker(db)

    response = _client(api_context, broker).put(ROUTE, json={"limit": 60},
                                                headers=auth_header("root"))

    assert response.status_code == 200, response.text
    [call] = broker.calls
    assert call["provider"] == PROVIDER and call["tenant_id"] == TENANT
    assert call["hard_max"] == 60
    assert call["requested_by"] == ADMIN
    assert call["reason"]
    assert db.docs[f"pools/{POOL}"]["hard_limit"] == 60
    assert db.docs[f"quota/{PROVIDER}:{TENANT}"]["configured_hard_max"] == 60
    assert response.json()["pool"]["hard_limit"] == 60
    # Admission's own field is never touched by an admin write.
    assert db.docs[f"pools/{POOL}"]["active"] == 3


def test_the_audit_row_names_the_pool_limit_and_the_brokers_maximum(api_context, db):
    _seed(db)
    db.docs[f"quota/{PROVIDER}:{TENANT}"]["configured_hard_max"] = 50

    response = _client(api_context, _Broker(db)).put(ROUTE, json={"limit": 30},
                                                      headers=auth_header("root"))

    assert response.status_code == 200, response.text
    [audit] = _audits(db)
    assert audit["by"] == ADMIN and audit["target_tenant_id"] == TENANT
    detail = audit["detail"]
    assert detail["pool"] == POOL
    assert (detail["from_limit"], detail["to_limit"]) == (40, 30)
    assert (detail["from_hard_max"], detail["to_hard_max"]) == (50, 30)
    assert (detail["from_target"], detail["to_target"]) == (7, 7)
    assert detail["target_clamped"] is False
    assert response.json()["change"] == detail


@pytest.mark.parametrize(
    "refusal",
    [
        ValidationFailed("configured_hard_max cannot be negative"),
        AccountPoolUnavailable("the account pool is unavailable: could not reach the quota broker"),
    ],
    ids=["broker-refused", "broker-unreachable"],
)
def test_a_broker_refusal_fails_the_request_and_leaves_the_pool_as_it_was(api_context, db, refusal):
    _seed(db)
    pool_before = dict(db.docs[f"pools/{POOL}"])
    broker = _Broker(db, refuse=refusal)

    response = _client(api_context, broker).put(ROUTE, json={"limit": 60},
                                                headers=auth_header("root"))

    assert response.status_code == refusal.status_code, response.text
    assert refusal.message in response.text
    assert len(broker.calls) == 1
    assert db.docs[f"pools/{POOL}"] == pool_before
    assert "admin_changed_by" not in db.docs[f"pools/{POOL}"]
    assert _audits(db) == []
