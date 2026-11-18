"""swarm-api's plan caps (docs/design/llm-providers.md §4.6, §6.2): each lend's
agents cap on `PUT /v1/accounts/{id}/lending`, a plan's per-workspace caps on
`PUT /v1/accounts/{id}/per-workspace-max`, and each lend's live holders on the
account read.

The broker had both routes; nothing a person uses reached the caps. What this
service adds is what every account write here adds: the resolved tenant, the
owner-only rule and the role gate, all before the broker is asked anything,
and the numbers checked in the broker's own words.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from quota_broker import accounts as broker_accounts
from swarm_api.brokerclient import BrokerClient, StaticIdToken
from swarm_api.main import create_app
from swarm_api.routes import accounts as api_accounts

from .conftest import auth_header, seed_tenant
from .test_account_api import FakePool, _recording_transport, account

LENDING = "/v1/accounts/{account_id}/lending"
PER_WORKSPACE = "/v1/accounts/{account_id}/per-workspace-max"


def _at(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class CapsPool(FakePool):
    """`FakePool`, with the broker's lend caps, per-workspace route and holds."""

    def __init__(self, *accounts: dict, **kwargs) -> None:
        super().__init__(*accounts, **kwargs)
        self.holds_by_account: dict[str, list[dict]] = {}
        self.holds_error: Exception | None = None

    def set_lending(self, account_id: str, *, lend_to=None, lends=None) -> dict:
        if lends is not None:
            self.calls.append(("set_lending", account_id, "lends", json.dumps(lends, sort_keys=True)))
            self._maybe_fail()
            self.accounts[account_id]["lends"] = {
                t: {**c, "cap_set": True} for t, c in lends.items()}
            self.accounts[account_id]["lend_to"] = sorted(lends)
        else:
            self.calls.append(("set_lending", account_id, tuple(lend_to)))
            self._maybe_fail()
            self.accounts[account_id]["lend_to"] = list(lend_to)
        return {"account": dict(self.accounts[account_id])}

    def set_per_workspace_max(self, account_id: str, *, per_workspace_max: dict) -> dict:
        self.calls.append(("set_per_workspace_max", account_id,
                           json.dumps(per_workspace_max, sort_keys=True)))
        self._maybe_fail()
        self.accounts[account_id]["per_workspace_max"] = dict(per_workspace_max)
        return {"account": dict(self.accounts[account_id])}

    def holds(self, account_id: str) -> dict:
        self.calls.append(("holds", account_id))
        if self.holds_error is not None:
            raise self.holds_error
        return {"account_id": account_id, "holds": list(self.holds_by_account.get(account_id, []))}


@pytest.fixture
def pool() -> CapsPool:
    return CapsPool(
        account("eng", "primary", lend_to=("research",)),
        account("research", "shared", lend_to=("eng",)),
    )


@pytest.fixture
def client(api_context, pool) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _put(client, route: str, account_id: str, body, user: str = "alice"):
    return client.put(route.format(account_id=account_id), json=body, headers=auth_header(user))


def _writes(pool: CapsPool) -> list[tuple]:
    return [c for c in pool.calls if c[0] not in ("list_accounts", "holds")]


def test_both_routes_are_published():
    paths = {getattr(r, "path", None) for r in api_accounts.router.routes}
    assert {LENDING, PER_WORKSPACE} <= paths


# --------------------------------------------------------------------------
# Lending, with each lend's agents cap
# --------------------------------------------------------------------------


def test_each_lends_cap_is_forwarded_to_the_broker(client, pool):
    response = _put(client, LENDING, "eng:primary",
                    {"lends": {"research": {"max_agents": 2}, "ops": {"max_agents": 1}}})

    assert response.status_code == 200, response.text
    assert _writes(pool) == [("set_lending", "eng:primary", "lends", json.dumps(
        {"ops": {"max_agents": 1}, "research": {"max_agents": 2}}, sort_keys=True))]
    assert response.json()["account"]["lends"]["research"]["max_agents"] == 2


def test_an_old_lend_to_only_body_still_works(client, pool):
    response = _put(client, LENDING, "eng:primary", {"lend_to": ["research", "ops"]})

    assert response.status_code == 200, response.text
    assert _writes(pool) == [("set_lending", "eng:primary", ("research", "ops"))]


def test_an_empty_body_still_lends_to_nobody(client, pool):
    response = _put(client, LENDING, "eng:primary", {})

    assert response.status_code == 200, response.text
    assert _writes(pool) == [("set_lending", "eng:primary", ())]


def test_both_shapes_at_once_are_refused_in_the_brokers_words(client, pool):
    response = _put(client, LENDING, "eng:primary",
                    {"lend_to": ["research"], "lends": {"research": {"max_agents": 1}}})

    assert response.status_code == 422, response.text
    assert response.json()["message"] == "send lends or lend_to, not both"
    assert pool.calls == []


@pytest.mark.parametrize("value", [0, -1, 51])
def test_a_lend_cap_out_of_range_is_refused_before_the_broker(client, pool, value):
    response = _put(client, LENDING, "eng:primary", {"lends": {"research": {"max_agents": value}}})

    assert response.status_code == 422, response.text
    words = f"an account's max_agents is 1 to {broker_accounts.MAX_AGENTS_LIMIT}, and this is {value}"
    assert response.json()["message"].startswith(f"the lend to research: {words}")
    assert pool.calls == []
    # The broker refuses the same lend in the same words.
    with pytest.raises(broker_accounts.AccountError, match=f"the lend to research: {words}"):
        broker_accounts.validate_lends({"research": {"max_agents": value}}, owner_tenant="eng", key=False)


@pytest.mark.parametrize("cap", [{}, {"max_agents": True}, {"max_agents": "2"}, {"max_agents": 2, "cap_set": False}])
def test_a_lend_with_no_whole_number_cap_is_refused_before_the_broker(client, pool, cap):
    response = _put(client, LENDING, "eng:primary", {"lends": {"research": cap}})

    assert response.status_code == 422, response.text
    assert pool.calls == []


@pytest.mark.parametrize("value", [True, "2", 2.5])
def test_a_lend_cap_that_is_not_a_whole_number_is_refused_in_the_brokers_words(client, pool, value):
    response = _put(client, LENDING, "eng:primary", {"lends": {"research": {"max_agents": value}}})

    assert response.status_code == 422, response.text
    words = "the lend to research: an account's max_agents is a whole number"
    assert response.json()["message"] == words
    assert pool.calls == []
    with pytest.raises(broker_accounts.AccountError, match=words):
        broker_accounts.validate_lends({"research": {"max_agents": value}}, owner_tenant="eng", key=False)


def test_a_lend_with_a_null_cap_is_refused_in_the_brokers_words(client, pool):
    response = _put(client, LENDING, "eng:primary", {"lends": {"research": {"max_agents": None}}})

    assert response.status_code == 422, response.text
    words = "the lend to research carries no cap; every lend sets max_agents"
    assert response.json()["message"] == words
    assert pool.calls == []
    with pytest.raises(broker_accounts.AccountError, match=words):
        broker_accounts.validate_lends({"research": {"max_agents": None}}, owner_tenant="eng", key=False)


def test_a_lend_to_the_owner_itself_is_refused_in_the_brokers_words(client, pool):
    response = _put(client, LENDING, "eng:primary", {"lends": {"eng": {"max_agents": 1}}})

    assert response.status_code == 422, response.text
    assert "its own limit is per_workspace_max" in response.json()["message"]
    assert _writes(pool) == []


def test_a_borrower_cannot_set_the_lenders_lend_caps(client, pool):
    response = _put(client, LENDING, "research:shared", {"lends": {"ops": {"max_agents": 1}}})

    assert response.status_code == 404, response.text
    assert _writes(pool) == []


def test_another_workspace_cannot_set_lend_caps(client, pool):
    response = _put(client, LENDING, "eng:primary", {"lends": {"ops": {"max_agents": 1}}}, user="bob")

    assert response.status_code == 404, response.text
    assert _writes(pool) == []


def test_a_disabled_workspace_is_refused_before_the_broker(client, pool, db):
    seed_tenant(db, "eng", enabled=False)

    response = _put(client, LENDING, "eng:primary", {"lends": {"research": {"max_agents": 1}}})

    assert response.status_code == 403, response.text
    assert pool.calls == []


# --------------------------------------------------------------------------
# Per-workspace caps
# --------------------------------------------------------------------------


def test_the_per_workspace_caps_are_forwarded_to_the_broker(client, pool):
    response = _put(client, PER_WORKSPACE, "eng:primary",
                    {"per_workspace_max": {"eng": 3, "research": 1}})

    assert response.status_code == 200, response.text
    assert _writes(pool) == [("set_per_workspace_max", "eng:primary",
                              json.dumps({"eng": 3, "research": 1}, sort_keys=True))]
    assert response.json()["account"]["per_workspace_max"] == {"eng": 3, "research": 1}


def test_an_empty_map_clears_every_cap_and_no_body_is_refused(client, pool):
    assert _put(client, PER_WORKSPACE, "eng:primary", {"per_workspace_max": {}}).status_code == 200
    assert _writes(pool) == [("set_per_workspace_max", "eng:primary", "{}")]

    pool.calls.clear()
    response = _put(client, PER_WORKSPACE, "eng:primary", {})
    assert response.status_code == 422, response.text
    assert pool.calls == []


@pytest.mark.parametrize("value", [0, 51])
def test_a_per_workspace_cap_out_of_range_is_refused_before_the_broker(client, pool, value):
    response = _put(client, PER_WORKSPACE, "eng:primary", {"per_workspace_max": {"research": value}})

    assert response.status_code == 422, response.text
    assert response.json()["message"].startswith("per_workspace_max for research: an account's max_agents is 1 to")
    assert pool.calls == []
    with pytest.raises(broker_accounts.AccountError, match="per_workspace_max for research: an account's max_agents"):
        broker_accounts.validate_per_workspace_max({"research": value})


@pytest.mark.parametrize("value", [True, "2", 2.5, None])
def test_a_per_workspace_cap_that_is_not_a_whole_number_is_refused_in_the_brokers_words(client, pool, value):
    response = _put(client, PER_WORKSPACE, "eng:primary", {"per_workspace_max": {"research": value}})

    assert response.status_code == 422, response.text
    words = "per_workspace_max for research: an account's max_agents is a whole number"
    assert response.json()["message"] == words
    assert pool.calls == []
    with pytest.raises(broker_accounts.AccountError, match=words):
        broker_accounts.validate_per_workspace_max({"research": value})


def test_a_workspace_that_neither_owns_nor_borrows_is_refused_before_the_broker_writes(client, pool):
    response = _put(client, PER_WORKSPACE, "eng:primary", {"per_workspace_max": {"ops": 1}})

    assert response.status_code == 422, response.text
    assert response.json()["message"] == "per_workspace_max names ops, which neither owns nor borrows eng:primary"
    assert _writes(pool) == []


def test_a_borrower_cannot_set_the_lenders_per_workspace_caps(client, pool):
    response = _put(client, PER_WORKSPACE, "research:shared", {"per_workspace_max": {"eng": 1}})

    assert response.status_code == 404, response.text
    assert _writes(pool) == []


def test_another_workspace_cannot_set_per_workspace_caps(client, pool):
    response = _put(client, PER_WORKSPACE, "eng:primary", {"per_workspace_max": {"eng": 1}}, user="bob")

    assert response.status_code == 404, response.text
    assert _writes(pool) == []


def test_a_broker_refusal_reaches_the_caller(client, pool):
    from swarm_api.errors import ValidationFailed

    def refuse(account_id, *, per_workspace_max):
        raise ValidationFailed("per_workspace_max names at most 51 workspaces")

    pool.set_per_workspace_max = refuse  # type: ignore[method-assign]
    response = _put(client, PER_WORKSPACE, "eng:primary", {"per_workspace_max": {"eng": 1}})

    assert response.status_code == 422, response.text
    assert "at most 51" in response.text


# --------------------------------------------------------------------------
# The account read: each lend's live holders, the owner's rows only
# --------------------------------------------------------------------------


def test_the_owners_row_carries_each_lends_live_holders(client, pool):
    pool.holds_by_account["eng:primary"] = [
        {"tenant_id": "research", "expires_at": _at(30)},
        {"tenant_id": "research", "expires_at": _at(30)},
        {"tenant_id": "research", "expires_at": _at(-5)},  # past its deadline: not live
        {"tenant_id": "eng", "expires_at": _at(30)},
    ]

    rows = {a["account_id"]: a for a in client.get("/v1/accounts", headers=auth_header("alice")).json()["accounts"]}

    assert rows["eng:primary"]["lend_holders"] == {"research": 2}
    # Lent TO eng: a borrower is told nothing of the lender's borrowers.
    assert "lend_holders" not in rows["research:shared"]
    assert ("holds", "research:shared") not in pool.calls


def test_a_failed_holds_read_serves_unknown_not_zero(client, pool):
    pool.holds_error = RuntimeError("down")

    response = client.get("/v1/accounts", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    rows = {a["account_id"]: a for a in response.json()["accounts"]}
    assert rows["eng:primary"]["lend_holders"] is None


def test_an_unlent_row_reads_no_holds(client, pool):
    pool.accounts["eng:primary"]["lend_to"] = []

    client.get("/v1/accounts", headers=auth_header("alice"))

    assert not [c for c in pool.calls if c[0] == "holds"]


# --------------------------------------------------------------------------
# The broker client
# --------------------------------------------------------------------------


def _broker(transport) -> BrokerClient:
    return BrokerClient(base_url="https://broker.example", tokens=StaticIdToken("t"), transport=transport)


def test_the_broker_client_puts_the_lends_with_their_caps():
    transport, seen = _recording_transport(200, {"account": {"account_id": "eng:a"}})

    _broker(transport).set_lending("eng:a", lends={"research": {"max_agents": 2}})

    assert seen[0]["url"] == "https://broker.example/v1/accounts/eng%3Aa/lending"
    assert json.loads(seen[0]["body"]) == {"lends": {"research": {"max_agents": 2}}}


def test_the_broker_client_still_puts_a_bare_lend_to():
    transport, seen = _recording_transport(200, {"account": {"account_id": "eng:a"}})

    _broker(transport).set_lending("eng:a", lend_to=["research"])

    assert json.loads(seen[0]["body"]) == {"lend_to": ["research"]}


def test_the_broker_client_puts_the_per_workspace_caps():
    transport, seen = _recording_transport(200, {"account": {"account_id": "eng:a b"}})

    _broker(transport).set_per_workspace_max("eng:a b", per_workspace_max={"eng": 2})

    assert seen[0]["method"] == "PUT"
    assert seen[0]["url"] == "https://broker.example/v1/accounts/eng%3Aa%20b/per-workspace-max"
    assert json.loads(seen[0]["body"]) == {"per_workspace_max": {"eng": 2}}
