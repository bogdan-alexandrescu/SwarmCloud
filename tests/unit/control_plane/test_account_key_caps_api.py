"""swarm-api's caps routes for an account (docs/design/llm-providers.md §4.6).

The broker's `PUT /v1/accounts/{id}/spend-cap`, `/per-workspace-max` and the
capped form of `/lending` (`lends`) existed and nothing a person uses reached
them. These are the proxies, and what each adds to the broker's own route is
what every account write here adds: the caller's resolved tenant and the
owner-only rule. What a cap may be stays the broker's.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from swarm_api.brokerclient import BrokerClient, StaticIdToken
from swarm_api.main import create_app
from swarm_api.routes import accounts as api_accounts

from .conftest import auth_header
from .test_account_api import FakePool, _recording_transport, account


class CapsPool(FakePool):
    """`FakePool`, with the broker's caps routes."""

    def set_lending(self, account_id: str, *, lend_to: list[str], lends=None) -> dict:
        self.calls.append(("set_lending", account_id, tuple(lend_to), lends))
        self._maybe_fail()
        if lends is not None:
            self.accounts[account_id]["lends"] = dict(lends)
            self.accounts[account_id]["lend_to"] = sorted(lends)
        else:
            self.accounts[account_id]["lend_to"] = list(lend_to)
        return {"account": dict(self.accounts[account_id])}

    def set_spend_cap(self, account_id: str, *, monthly_spend_cap_usd) -> dict:
        self.calls.append(("set_spend_cap", account_id, monthly_spend_cap_usd))
        self._maybe_fail()
        self.accounts[account_id]["monthly_spend_cap_usd"] = monthly_spend_cap_usd
        return {"account": dict(self.accounts[account_id])}

    def set_per_workspace_max(self, account_id: str, *, per_workspace_max) -> dict:
        self.calls.append(("set_per_workspace_max", account_id, dict(per_workspace_max)))
        self._maybe_fail()
        self.accounts[account_id]["per_workspace_max"] = dict(per_workspace_max)
        return {"account": dict(self.accounts[account_id])}


@pytest.fixture
def pool() -> CapsPool:
    return CapsPool(
        account("eng", "team-key"),
        account("research", "shared", lend_to=("eng",)),
    )


@pytest.fixture
def client(api_context, pool) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _put(client, account_id: str, route: str, body, user: str = "alice"):
    return client.put(f"/v1/accounts/{account_id}/{route}", json=body,
                      headers=auth_header(user))


def test_the_routes_are_published():
    paths = {getattr(r, "path", None) for r in api_accounts.router.routes}
    assert "/v1/accounts/{account_id}/spend-cap" in paths
    assert "/v1/accounts/{account_id}/per-workspace-max" in paths


def test_the_owner_sets_and_clears_the_spend_cap(client, pool):
    response = _put(client, "eng:team-key", "spend-cap", {"monthly_spend_cap_usd": 250})
    assert response.status_code == 200, response.text
    assert response.json()["account"]["monthly_spend_cap_usd"] == 250.0
    cleared = _put(client, "eng:team-key", "spend-cap", {"monthly_spend_cap_usd": None})
    assert cleared.status_code == 200, cleared.text
    assert pool.mutations == [("set_spend_cap", "eng:team-key", 250.0),
                              ("set_spend_cap", "eng:team-key", None)]


@pytest.mark.parametrize("body", [{}, {"monthly_spend_cap_usd": "50"},
                                  {"monthly_spend_cap_usd": True}])
def test_a_spend_cap_that_is_not_a_number_is_refused_before_the_broker(client, pool, body):
    response = _put(client, "eng:team-key", "spend-cap", body)
    assert response.status_code == 422, response.text
    assert pool.calls == []


def test_the_owner_sets_per_workspace_caps(client, pool):
    response = _put(client, "eng:team-key", "per-workspace-max",
                    {"per_workspace_max": {"eng": 3}})
    assert response.status_code == 200, response.text
    assert pool.mutations == [("set_per_workspace_max", "eng:team-key", {"eng": 3})]


@pytest.mark.parametrize("body", [{}, {"per_workspace_max": {"eng": "3"}},
                                  {"per_workspace_max": {"eng": 2.5}}])
def test_a_per_workspace_cap_that_is_not_a_whole_number_is_refused(client, pool, body):
    response = _put(client, "eng:team-key", "per-workspace-max", body)
    assert response.status_code == 422, response.text
    assert pool.calls == []


def test_lends_with_their_caps_are_forwarded_alone(client, pool):
    response = _put(client, "eng:team-key", "lending",
                    {"lends": {"research": {"max_agents": 2, "monthly_spend_cap_usd": 40}}})
    assert response.status_code == 200, response.text
    assert pool.mutations == [("set_lending", "eng:team-key", (),
                               {"research": {"max_agents": 2, "monthly_spend_cap_usd": 40.0}})]


def test_lends_and_lend_to_together_are_refused_before_the_broker(client, pool):
    response = _put(client, "eng:team-key", "lending",
                    {"lend_to": ["research"],
                     "lends": {"research": {"max_agents": 2, "monthly_spend_cap_usd": 40}}})
    assert response.status_code == 422, response.text
    assert pool.calls == []


def test_a_names_only_lend_still_reaches_the_broker_as_before(client, pool):
    response = _put(client, "eng:team-key", "lending", {"lend_to": ["research"]})
    assert response.status_code == 200, response.text
    assert pool.mutations == [("set_lending", "eng:team-key", ("research",), None)]


@pytest.mark.parametrize("route,body", [
    ("spend-cap", {"monthly_spend_cap_usd": 10}),
    ("per-workspace-max", {"per_workspace_max": {"eng": 1}}),
    ("lending", {"lends": {"eng": {"max_agents": 1, "monthly_spend_cap_usd": 5}}}),
])
def test_a_borrower_or_a_stranger_cannot_set_a_cap(client, pool, route, body):
    """Lent to eng, owned by research: usable, not eng's to change. 404, as
    every other write on a lent account answers; bob is in neither."""
    assert _put(client, "research:shared", route, body).status_code == 404
    assert _put(client, "eng:team-key", route, body, user="bob").status_code == 404
    assert pool.mutations == []


def test_the_broker_client_puts_each_cap_to_the_brokers_route():
    transport, seen = _recording_transport(200, {"account": {"account_id": "eng:k"}})
    broker = BrokerClient(base_url="https://broker.example", tokens=StaticIdToken("t"),
                          transport=transport)

    broker.set_spend_cap("eng:k", monthly_spend_cap_usd=None)
    broker.set_per_workspace_max("eng:k", per_workspace_max={"eng": 2})
    broker.set_lending("eng:k", lend_to=[], lends={"research": {"max_agents": 1}})
    broker.set_lending("eng:k", lend_to=["research"])

    assert [(s["method"], s["url"].rsplit("/", 1)[1], json.loads(s["body"])) for s in seen] == [
        ("PUT", "spend-cap", {"monthly_spend_cap_usd": None}),
        ("PUT", "per-workspace-max", {"per_workspace_max": {"eng": 2}}),
        ("PUT", "lending", {"lends": {"research": {"max_agents": 1}}}),
        ("PUT", "lending", {"lend_to": ["research"]}),
    ]
