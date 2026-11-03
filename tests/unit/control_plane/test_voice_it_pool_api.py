"""The Voice it pool in swarm-api (owner, 2026-11-02).

The ChatGPT plan account is reserved for Voice it through the quota broker's
"voice-it" pool; swarm-voice leases from it per session. swarm-api's part:

  * `GET /v1/voice/pool` -- Voice it › Preferences: which account the CALLER's
    next session would lease, through the broker's `GET /v1/voice/pool`,
    for the verified caller only (never a query parameter).
  * `PUT /v1/accounts/{account_id}/pools` -- Capacity › Accounts: a platform
    admin moves an account between pools; `confirm_label` forwarded, the
    broker's 400/409 passed through with its sentence, one `admin_audit` row.
  * the account rows carry the broker's `pools`, `voice_it_people` and
    `voice_it_holder`.

Offline: the broker is a fake on `app.state.account_pool`, or a recording
transport under the real `BrokerClient`. No credentials, no emulator.
"""

from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from swarm_api.brokerclient import BrokerClient, StaticIdToken
from swarm_api.deps import build_context
from swarm_api.errors import ApiError, Conflict
from swarm_api.main import create_app
from swarm_api.routes import accounts as api_accounts

from .conftest import api_settings, auth_header, seed_tenant
from .test_account_api import FakePool, _recording_transport, account

POOLS_ROUTE = "/v1/accounts/{account_id}/pools"


class VoicePoolBroker(FakePool):
    """`FakePool`, with the broker's two Voice it pool routes."""

    def __init__(self, *accounts: dict, **kwargs) -> None:
        super().__init__(*accounts, **kwargs)
        self.next_voice: dict[str, dict] = {}
        self.pools_error: Exception | None = None

    def voice_pool(self, person: str) -> dict:
        self.calls.append(("voice_pool", person))
        self._maybe_fail()
        return self.next_voice.get(person, {"account": None, "reason": "not_in_voice_pool"})

    def set_pools(self, account_id: str, *, pools: list[str], confirm_label: str) -> dict:
        self.calls.append(("set_pools", account_id, tuple(pools), confirm_label))
        if self.pools_error is not None:
            raise self.pools_error
        row = self.accounts[account_id]
        row["pools"] = list(pools)
        row["voice_it_people"] = ["bogdan@saga.xyz"] if "voice-it" in pools else []
        row["voice_it_holder"] = None
        return {"account": dict(row)}


def _voice_account(owner: str, label: str) -> dict:
    row = account(owner, label)
    row.update({"provider": "openai", "display_name": "Voice plan",
                "pools": ["voice-it"], "voice_it_people": ["alice@example.com"],
                "voice_it_holder": {"voice_session_id": "vs_" + secrets.token_hex(16),
                                    "person": "alice@example.com",
                                    "since": "2026-11-02T10:00:00+00:00"}})
    return row


@pytest.fixture
def broker() -> VoicePoolBroker:
    return VoicePoolBroker(account("eng", "primary"), _voice_account("eng", "voice"))


def _app(db, tokens, group_map, objects, broker, **settings) -> TestClient:
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.groups import StaticGroups
    from swarm_api.metrics import ApiMetrics
    from swarm_api.waker import NullWaker

    from .conftest import NoForgeTokens

    ctx = build_context(
        settings=api_settings(**settings), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=NoForgeTokens())
    app = create_app(ctx)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(db, tokens, group_map, objects, broker) -> TestClient:
    seed_tenant(db, "eng")
    return _app(db, tokens, group_map, objects, broker,
                voice_people=("alice@example.com", "bob@example.com"))


# --------------------------------------------------------------------------
# GET /v1/voice/pool
# --------------------------------------------------------------------------


def test_the_pool_route_answers_the_brokers_account_for_the_caller(client, broker):
    broker.next_voice["alice@example.com"] = {
        "account": {"account_id": "eng:voice", "label": "voice", "display_name": "Voice plan",
                    "provider": "openai"},
        "reason": ""}

    response = client.get("/v1/voice/pool", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "pool": "voice-it",
        "account": {"label": "voice", "display_name": "Voice plan", "provider": "openai"},
        "reason": ""}
    assert broker.calls == [("voice_pool", "alice@example.com")]


def test_the_pool_route_asks_only_about_the_caller_whatever_the_query_says(client, broker):
    broker.next_voice["alice@example.com"] = {
        "account": {"account_id": "eng:voice", "label": "voice", "display_name": None,
                    "provider": "openai"}, "reason": ""}

    response = client.get("/v1/voice/pool", params={"person": "alice@example.com"},
                          headers=auth_header("bob"))

    assert response.status_code == 200, response.text
    assert response.json() == {"pool": "voice-it", "account": None, "reason": "not_in_voice_pool"}
    assert broker.calls == [("voice_pool", "bob@example.com")]


def test_the_pool_route_passes_the_brokers_reason(client, broker):
    broker.next_voice["alice@example.com"] = {
        "account": None, "reason": "no_voice_account_available",
        "next_reset_at": "2026-11-02T15:00:00+00:00"}

    body = client.get("/v1/voice/pool", headers=auth_header("alice")).json()

    assert body == {"pool": "voice-it", "account": None, "reason": "no_voice_account_available"}


def test_the_pool_route_is_403_for_anyone_voice_it_is_not_open_to(client, broker):
    response = client.get("/v1/voice/pool", headers=auth_header("carol"))

    assert response.status_code == 403, response.text
    assert broker.calls == []


# --------------------------------------------------------------------------
# The account rows
# --------------------------------------------------------------------------


def test_the_account_rows_carry_the_voice_pool_fields(client, broker):
    response = client.get("/v1/accounts", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    rows = {a["account_id"]: a for a in response.json()["accounts"]}
    voice = rows["eng:voice"]
    assert voice["pools"] == ["voice-it"]
    assert voice["voice_it_people"] == ["alice@example.com"]
    assert voice["voice_it_holder"]["person"] == "alice@example.com"
    assert set(voice["voice_it_holder"]) == {"voice_session_id", "person", "since"}
    # A broker older than the pool: nothing invented.
    assert "pools" not in rows["eng:primary"]


# --------------------------------------------------------------------------
# PUT /v1/accounts/{account_id}/pools
# --------------------------------------------------------------------------


def _move(client, account_id: str, body: dict, user: str = "root"):
    return client.put(POOLS_ROUTE.format(account_id=account_id), json=body,
                      headers=auth_header(user))


def test_the_pools_route_is_published():
    paths = {getattr(r, "path", None) for r in api_accounts.router.routes}
    assert POOLS_ROUTE in paths


@pytest.mark.parametrize("user", ["alice", "bob", "carol"])
def test_a_non_admin_cannot_move_an_account(client, broker, db, user):
    response = _move(client, "eng:primary", {"pools": ["voice-it"], "confirm_label": "primary"},
                     user=user)

    assert response.status_code == 403, response.text
    assert not [c for c in broker.calls if c[0] == "set_pools"]
    assert not [d for d in db.dump("admin_audit/").values()
                if d.get("action") == api_accounts.ACCOUNT_POOLS_AUDIT_ACTION]


def test_an_admin_moves_an_account_forwarding_confirm_label_and_it_is_audited(client, broker, db):
    response = _move(client, "eng:primary",
                     {"pools": ["workspace", "voice-it"], "confirm_label": "primary"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account"]["pools"] == ["workspace", "voice-it"]
    assert body["account"]["voice_it_people"] == ["bogdan@saga.xyz"]
    assert ("set_pools", "eng:primary", ("workspace", "voice-it"), "primary") in broker.calls
    (row,) = [d for d in db.dump("admin_audit/").values()
              if d.get("action") == api_accounts.ACCOUNT_POOLS_AUDIT_ACTION]
    assert row["by"] == "root@example.com" and row["target_tenant_id"] == "eng"
    assert row["detail"] == {"account_id": "eng:primary", "label": "primary",
                             "from_pools": ["workspace"], "to_pools": ["workspace", "voice-it"]}


def test_a_wrong_confirm_label_is_the_brokers_400_in_its_words(client, broker, db):
    broker.pools_error = ApiError("confirm_label must be the account's label, 'primary'")

    response = _move(client, "eng:primary", {"pools": ["voice-it"], "confirm_label": "nope"})

    assert response.status_code == 400, response.text
    assert response.json()["message"] == "confirm_label must be the account's label, 'primary'"
    assert ("set_pools", "eng:primary", ("voice-it",), "nope") in broker.calls
    assert not [d for d in db.dump("admin_audit/").values()
                if d.get("action") == api_accounts.ACCOUNT_POOLS_AUDIT_ACTION]


def test_a_pool_in_use_is_the_brokers_409_in_its_words(client, broker):
    broker.pools_error = Conflict("eng:voice holds a live Voice it lease; end it first",
                                  detail={"reason": "pool_in_use"})

    response = _move(client, "eng:voice", {"pools": ["workspace"], "confirm_label": "voice"})

    assert response.status_code == 409, response.text
    assert response.json()["message"] == "eng:voice holds a live Voice it lease; end it first"
    assert response.json()["detail"] == {"reason": "pool_in_use"}


def test_an_unknown_account_is_404_and_the_broker_changes_nothing(client, broker):
    response = _move(client, "eng:missing", {"pools": ["voice-it"], "confirm_label": "missing"})

    assert response.status_code == 404, response.text
    assert not [c for c in broker.calls if c[0] == "set_pools"]


def test_an_empty_pools_list_is_422_before_the_broker(client, broker):
    response = _move(client, "eng:primary", {"pools": [], "confirm_label": "primary"})

    assert response.status_code == 422, response.text
    assert not [c for c in broker.calls if c[0] == "set_pools"]


# --------------------------------------------------------------------------
# BrokerClient: the two routes on the wire
# --------------------------------------------------------------------------


def _broker(status: int, payload) -> tuple[BrokerClient, list[dict]]:
    transport, seen = _recording_transport(status, payload)
    return BrokerClient(base_url="https://broker.internal", audience="https://broker.aud",
                        tokens=StaticIdToken(secrets.token_hex(16)), transport=transport), seen


def test_the_client_puts_pools_with_the_confirm_label():
    client, seen = _broker(200, {"account": {"account_id": "eng:a b", "pools": ["voice-it"]}})

    client.set_pools("eng:a b", pools=["voice-it"], confirm_label="a b")

    assert seen[0]["method"] == "PUT"
    assert seen[0]["url"] == "https://broker.internal/v1/accounts/eng%3Aa%20b/pools"
    assert json.loads(seen[0]["body"]) == {"pools": ["voice-it"], "confirm_label": "a b"}


def test_the_client_reads_the_brokers_400_as_the_callers():
    client, _ = _broker(400, {"code": "bad_request", "message": "confirm_label does not match"})

    with pytest.raises(ApiError) as raised:
        client.set_pools("eng:a", pools=["voice-it"], confirm_label="b")
    assert raised.value.status_code == 400
    assert raised.value.message == "confirm_label does not match"


def test_the_client_reads_pool_in_use_as_a_conflict_with_its_reason():
    client, _ = _broker(409, {"code": "pool_in_use", "message": "a live lease holds it"})

    with pytest.raises(Conflict) as raised:
        client.set_pools("eng:a", pools=["workspace"], confirm_label="a")
    assert raised.value.message == "a live lease holds it"
    assert raised.value.detail == {"reason": "pool_in_use"}


def test_the_client_asks_the_voice_pool_for_one_person():
    client, seen = _broker(200, {"account": None, "reason": "not_in_voice_pool"})

    assert client.voice_pool("alice@example.com") == {"account": None, "reason": "not_in_voice_pool"}
    assert seen[0]["method"] == "GET"
    assert seen[0]["url"] == "https://broker.internal/v1/voice/pool?person=alice%40example.com"


def test_a_voice_only_account_moves_back_to_the_workspace_pool(client, broker, db):
    """The broker's default listing leaves a Voice-it-only account out (it
    serves no tenant), so the move looks it up with `include_reserved`; else
    a reserved account could never be moved back."""
    response = _move(client, "eng:voice", {"pools": ["workspace"], "confirm_label": "voice"})

    assert response.status_code == 200, response.text
    assert ("list_accounts", "eng") in broker.calls
    assert ("set_pools", "eng:voice", ("workspace",), "voice") in broker.calls
    assert response.json()["change"]["from_pools"] == ["voice-it"]


def test_the_default_listing_leaves_a_voice_only_account_out(broker):
    """What every capacity count reads: no reserved account in it."""
    ids = [a["account_id"] for a in broker.list_accounts("eng")["accounts"]]
    assert ids == ["eng:primary"]


def test_the_client_asks_for_reserved_accounts_only_when_told():
    client, seen = _broker(200, {"accounts": []})

    client.list_accounts("eng")
    client.list_accounts("eng", include_reserved=True)

    assert seen[0]["url"] == "https://broker.internal/v1/accounts?tenant_id=eng"
    assert seen[1]["url"] == "https://broker.internal/v1/accounts?tenant_id=eng&include_reserved=true"
