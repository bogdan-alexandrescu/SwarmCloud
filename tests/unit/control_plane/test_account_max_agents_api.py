"""swarm-api's `PUT /v1/accounts/{account_id}/max-agents` (#2222, ACCOUNTS-API-SC).

The broker's route (POOL-ACCOUNT-CAP, PR 2231) existed and nothing a person
uses reached it. This is the proxy, and what it adds to the broker's own route
is what every account write here adds: the caller's resolved tenant, the
owner-only rule, and -- for this one -- the range check, so a bad number is
refused before the broker is asked anything.

The platform-admin half (an admin acting in the owner's workspace passes and
is audited) is in `test_roles_gates_automate.py`, beside the fixtures that seed
a platform admin and read `admin_audit`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from quota_broker import accounts as broker_accounts
from swarm_api.brokerclient import BrokerClient, StaticIdToken
from swarm_api.main import create_app
from swarm_api.routes import accounts as api_accounts

from .conftest import auth_header, seed_tenant
from .test_account_api import FakePool, _recording_transport, account

ROUTE = "/v1/accounts/{account_id}/max-agents"


class CapPool(FakePool):
    """`FakePool`, with the broker's max-agents route."""

    def set_max_agents(self, account_id: str, *, max_agents: int) -> dict:
        self.calls.append(("set_max_agents", account_id, max_agents))
        self._maybe_fail()
        self.accounts[account_id]["max_agents"] = max_agents
        return {"account": dict(self.accounts[account_id])}


@pytest.fixture
def pool() -> CapPool:
    return CapPool(
        account("eng", "primary"),
        account("research", "shared", lend_to=("eng",)),
    )


@pytest.fixture
def client(api_context, pool) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _put(client, account_id: str, body, user: str = "alice"):
    return client.put(
        ROUTE.format(account_id=account_id), json=body, headers=auth_header(user)
    )


def test_the_route_is_published():
    paths = {getattr(r, "path", None) for r in api_accounts.router.routes}
    assert ROUTE in paths


def test_the_owner_sets_the_cap_and_the_broker_is_asked_once(client, pool):
    response = _put(client, "eng:primary", {"max_agents": 3})

    assert response.status_code == 200, response.text
    assert response.json()["account"]["max_agents"] == 3
    assert [c for c in pool.mutations] == [("set_max_agents", "eng:primary", 3)]


def test_a_borrower_cannot_set_the_lenders_cap(client, pool):
    """Lent to eng, owned by research: usable, not eng's to change. 404, as
    every other write on a lent account answers."""
    response = _put(client, "research:shared", {"max_agents": 3})

    assert response.status_code == 404, response.text
    assert pool.mutations == []


def test_another_tenants_member_cannot_set_it(client, pool):
    response = _put(client, "eng:primary", {"max_agents": 3}, user="bob")

    assert response.status_code == 404, response.text
    assert pool.mutations == []


@pytest.mark.parametrize("value", [0, -1, 51, 1000])
def test_a_number_out_of_range_is_refused_before_the_broker_is_asked(client, pool, value):
    response = _put(client, "eng:primary", {"max_agents": value})

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert f"1 to {api_accounts.MAX_AGENTS_LIMIT}" in response.json()["message"]
    # Not even the ownership read: a bad number costs the broker nothing.
    assert pool.calls == []


@pytest.mark.parametrize("body", [{"max_agents": True}, {"max_agents": "6"},
                                  {"max_agents": 6.5}, {"max_agents": None}, {}])
def test_a_body_that_is_not_a_whole_number_is_refused_before_the_broker(client, pool, body):
    response = _put(client, "eng:primary", body)

    assert response.status_code == 422, response.text
    assert pool.calls == []


@pytest.mark.parametrize("value", [1, 50])
def test_the_bounds_are_accepted(client, pool, value):
    response = _put(client, "eng:primary", {"max_agents": value})

    assert response.status_code == 200, response.text
    assert ("set_max_agents", "eng:primary", value) in pool.calls


def test_the_limit_is_the_brokers():
    """Restated because swarm-api does not import the broker; held equal here,
    so the two cannot refuse different numbers."""
    assert api_accounts.MAX_AGENTS_LIMIT == broker_accounts.MAX_AGENTS_LIMIT
    with pytest.raises(broker_accounts.AccountError):
        broker_accounts.validate_max_agents(api_accounts.MAX_AGENTS_LIMIT + 1)
    assert broker_accounts.validate_max_agents(api_accounts.MAX_AGENTS_LIMIT) == 50


def test_a_disabled_tenant_is_refused_before_the_broker(client, pool, db):
    seed_tenant(db, "eng", enabled=False)

    response = _put(client, "eng:primary", {"max_agents": 3})

    assert response.status_code == 403, response.text
    assert pool.calls == []


def test_a_foreign_duplicate_in_the_answer_is_not_named(client, pool):
    """The answer goes through `_own_view` like every other account write's."""
    pool.accounts["eng:primary"]["duplicate_of"] = "research:shared"

    body = _put(client, "eng:primary", {"max_agents": 3}).json()

    assert body["account"]["duplicate_of"] is None
    assert body["account"]["duplicate_reason"] == api_accounts.ANOTHER_WORKSPACE


def test_the_broker_client_puts_the_number_to_the_brokers_route():
    transport, seen = _recording_transport(200, {"account": {"account_id": "eng:a b"}})
    broker = BrokerClient(
        base_url="https://broker.example",
        tokens=StaticIdToken("t"),
        transport=transport,
    )

    broker.set_max_agents("eng:a b", max_agents=4)

    assert seen[0]["method"] == "PUT"
    assert seen[0]["url"] == "https://broker.example/v1/accounts/eng%3Aa%20b/max-agents"
    assert json.loads(seen[0]["body"]) == {"max_agents": 4}
