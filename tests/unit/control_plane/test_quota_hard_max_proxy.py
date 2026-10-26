"""A platform admin can set a tenant's configured max from the console (POOL-LIMITS-QUOTA).

BGD, 2026-10-25 (part of #2302): the anthropic · eng pool sat full at 14 and
Admin › Pool limits could only edit its hard limit (100), which was not the
term that bound. The effective limit is the least of the hard limit, the
broker's `configured_hard_max` and the AIMD `adaptive_target`; the target had
a platform-admin route already (QUOTA-RESET) and the max had none in
swarm-api. `PUT /v1/admin/quota/{provider}/{tenant_id}/hard-max` proxies to
the broker's own route, and these tests hold what it may and may not do:

  * only a PLATFORM admin reaches it -- a tenant member and a pool admin are
    refused 403 and nothing is sent to the broker or written to the audit;
    `reset_adaptive_target` is held to the same rule here;
  * an unknown provider or tenant is a 404, before the broker is called;
  * who asked is the token's verified email: a body that names a
    `requested_by` is refused, and the audit's `by` is never the body's;
  * the broker client sends the broker's body, `{hard_max, requested_by,
    reason}`, to its route, and `requested_by` is the token's email
    (ACCOUNT-429-FOLLOWUP: the broker audits the change in `quota_audit` too).

No credentials, no network: a fake broker and the shared FakeFirestore.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, NoForgeTokens, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore

PROVIDER = "anthropic"
TENANT = "eng"
ADMIN = "root@example.com"
HARD_MAX = f"/v1/admin/quota/{PROVIDER}/{TENANT}/hard-max"
TARGET = f"/v1/admin/quota/{PROVIDER}/{TENANT}/adaptive-target"

PEOPLE = {
    "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    "alice@example.com": (ENG_GROUP,),
    # Holds the pool-admin capability (`admin_pool_users`) and nothing else.
    "gate@example.com": (),
}


class _FakeBroker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def set_hard_max(self, provider: str, tenant_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"route": "hard-max", "provider": provider, "tenant_id": tenant_id, **kwargs})
        return {"quota": {"provider": provider, "tenant_id": tenant_id,
                          "configured_hard_max": kwargs["hard_max"],
                          "adaptive_target": min(40, kwargs["hard_max"]),
                          "effective_limit": min(40, kwargs["hard_max"])}}

    def set_adaptive_target(self, provider: str, tenant_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"route": "adaptive-target", "provider": provider,
                           "tenant_id": tenant_id, **kwargs})
        return {"quota": {"adaptive_target": kwargs["target"]}, "reset": {}}


def _tokens() -> dict[str, dict[str, Any]]:
    return {
        f"token-{email.split('@')[0]}": {
            "email": email, "email_verified": True, "sub": f"sub-{email}", "hd": "example.com",
        }
        for email in PEOPLE
    }


def _client(db: FakeFirestore, broker: _FakeBroker) -> TestClient:
    context = build_context(
        settings=api_settings(admin_pool_users=("gate@example.com",)),
        db=db,
        verifier=StaticTokenVerifier(_tokens()),
        groups=StaticGroups(PEOPLE),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(context)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


def _seed_quota(db: FakeFirestore, **fields: Any) -> None:
    db.docs[f"quota/{PROVIDER}:{TENANT}"] = {
        "provider": PROVIDER, "tenant_id": TENANT, "state": "AVAILABLE",
        "configured_hard_max": 50, "adaptive_target": 40, "success_count": 0,
        "rate_limit_count": 0, **fields,
    }


def _audits(db: FakeFirestore) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items() if path.startswith(f"{AUDIT_COLLECTION}/")
            and doc.get("action") == "quota_hard_max_set"]


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


def test_a_platform_admin_sets_the_max_and_it_is_audited_as_the_token_says(db):
    seed_tenant(db, TENANT)
    _seed_quota(db)
    broker = _FakeBroker()
    response = _client(db, broker).put(
        HARD_MAX, json={"hard_max": 30, "reason": "eng asked for less"}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert broker.calls == [{"route": "hard-max", "provider": PROVIDER, "tenant_id": TENANT,
                             "hard_max": 30, "requested_by": ADMIN,
                             "reason": "eng asked for less"}]
    change = response.json()["change"]
    assert change["from_hard_max"] == 50 and change["to_hard_max"] == 30
    # The broker's clamp, said: 40 did not fit under 30.
    assert change["from_target"] == 40 and change["to_target"] == 30
    assert change["target_clamped"] is True
    assert change["requested_by"] == ADMIN
    [audit] = _audits(db)
    assert audit["by"] == ADMIN
    assert audit["target_tenant_id"] == TENANT
    assert audit["detail"]["reason"] == "eng asked for less"
    assert audit["detail"]["to_hard_max"] == 30


def test_a_raised_max_does_not_say_clamped(db):
    seed_tenant(db, TENANT)
    _seed_quota(db, adaptive_target=14)
    broker = _FakeBroker()
    response = _client(db, broker).put(
        HARD_MAX, json={"hard_max": 80, "reason": "lent more accounts"}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["change"]["target_clamped"] is False


def test_the_body_cannot_name_who_asked(db):
    seed_tenant(db, TENANT)
    broker = _FakeBroker()
    response = _client(db, broker).put(
        HARD_MAX,
        json={"hard_max": 30, "reason": "r", "requested_by": "someone-else@example.com"},
        headers=auth_header("root"),
    )
    assert response.status_code == 422
    assert broker.calls == []
    assert _audits(db) == []


def test_a_reason_is_required(db):
    seed_tenant(db, TENANT)
    broker = _FakeBroker()
    response = _client(db, broker).put(HARD_MAX, json={"hard_max": 30}, headers=auth_header("root"))
    assert response.status_code == 422
    assert broker.calls == []


@pytest.mark.parametrize("who", ["alice", "gate"])
@pytest.mark.parametrize("path,body", [
    (HARD_MAX, {"hard_max": 30, "reason": "mine"}),
    (TARGET, {"target": 50, "reason": "mine"}),
])
def test_a_member_and_a_pool_admin_are_refused_with_nothing_written(db, who, path, body):
    seed_tenant(db, TENANT)
    _seed_quota(db)
    before = dict(db.docs)
    broker = _FakeBroker()
    response = _client(db, broker).put(path, json=body, headers=auth_header(who))
    assert response.status_code == 403, response.text
    assert broker.calls == []
    assert db.docs == before


def test_an_unknown_tenant_is_404_before_the_broker(db):
    broker = _FakeBroker()
    response = _client(db, broker).put(
        f"/v1/admin/quota/{PROVIDER}/no-such-tenant/hard-max",
        json={"hard_max": 30, "reason": "r"}, headers=auth_header("root"))
    assert response.status_code == 404
    assert broker.calls == []
    assert _audits(db) == []


def test_an_unknown_provider_is_404_before_the_broker(db):
    seed_tenant(db, TENANT)
    broker = _FakeBroker()
    response = _client(db, broker).put(
        f"/v1/admin/quota/no-such-provider/{TENANT}/hard-max",
        json={"hard_max": 30, "reason": "r"}, headers=auth_header("root"))
    assert response.status_code == 404
    assert broker.calls == []
    assert _audits(db) == []


def test_the_broker_client_puts_the_brokers_own_body_to_its_route():
    from swarm_api.brokerclient import BrokerClient

    calls: list[dict[str, Any]] = []

    def transport(method, url, *, headers, body=None, timeout=30.0):
        calls.append({"method": method, "url": url,
                      "body": json.loads(body.decode()) if body else None})
        return 200, {"quota": {"configured_hard_max": 30}}

    class _Token:
        def token(self, audience: str) -> str:
            return "t"

    client = BrokerClient(base_url="https://broker.invalid", audience="https://broker.invalid",
                          tokens=_Token(), transport=transport)
    assert client.set_hard_max(
        PROVIDER, TENANT, hard_max=30, requested_by=ADMIN, reason="eng asked for less"
    ) == {"quota": {"configured_hard_max": 30}}
    assert calls == [{"method": "PUT",
                      "url": f"https://broker.invalid/v1/quota/{PROVIDER}/{TENANT}/hard-max",
                      # The broker's `HardMaxRequest`: who and why are audited
                      # there too (ACCOUNT-429-FOLLOWUP).
                      "body": {"hard_max": 30, "requested_by": ADMIN,
                               "reason": "eng asked for less"}}]
