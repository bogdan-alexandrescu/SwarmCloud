"""Voice it › Settings: the dedicated key's status and the chain line
(docs/design/llm-providers.md §5.3, §6.4).

`dedicated_key` is the broker's `voice_dedicated` account read from its
platform-wide listing (`GET /v1/accounts`), never the key; `chain` is the
three Voice it tiers (plans, shared keys, this key), each `available`,
`no_quota_left` or `not_set_up` as the broker's listing says, never measured
here. A broker read that fails reads as unknown (None), never as not set. The
router is tested on its own app: Firestore is the in-memory fake, the broker a
fake pool, no cloud credential and no emulator. Every fake secret is built at
runtime with `secrets`.
"""

from __future__ import annotations

import logging
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_common.identity import Principal  # noqa: E402

from swarm_api import setup_links as sl  # noqa: E402
from swarm_api.auth import AuthContext  # noqa: E402
from swarm_api.brokerclient import BrokerClient, StaticIdToken  # noqa: E402
from swarm_api.deps import current_auth, get_context  # noqa: E402
from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.routes import voice_settings as routes  # noqa: E402
from swarm_api.routes.accounts import account_pool  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import settings as vsettings  # noqa: E402

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
ALICE = "alice@example.com"   # a Voice it person, not an admin
ROOT = "root@example.com"     # a platform admin, not a Voice it person
SECRET_NAME = "swarm-account-ops--voice-key"


def _account(label: str, *, provider: str = "openai-api", dedicated: bool = False,
             state: str = "AVAILABLE", can_take: bool = True, people=(ALICE,),
             pools=("voice-it",), exhausted_at: str | None = None,
             blocked_by: list | None = None, tenant: str = "ops", **extra: Any) -> dict:
    """One row of the broker's `GET /v1/accounts`, as `account_to_api` writes it."""
    return {"account_id": f"{tenant}:{label}", "owner_tenant": tenant, "label": label,
            "display_name": None, "provider": provider, "state": state,
            "voice_dedicated": dedicated, "pools": list(pools),
            "voice_it_people": list(people), "can_take_new_agent": can_take,
            "exhausted_at": exhausted_at, "blocked_by": list(blocked_by or []),
            "monthly_spend_cap_usd": None, "month_spend_usd": 0.0, **extra}


class FakePool:
    def __init__(self, accounts: list[dict] | None = None, *, fails: bool = False,
                 pool_answer: dict | None = None) -> None:
        self.accounts = accounts or []
        self.fails = fails
        self.pool_answer = pool_answer or {"account": None, "reason": vs.NO_LIVE_ACCOUNT}
        self.listed = 0

    def voice_pool(self, person: str) -> dict[str, Any]:
        if self.fails:
            raise OSError("broker unreachable")
        return self.pool_answer

    def voice_it_accounts(self) -> dict[str, Any]:
        self.listed += 1
        if self.fails:
            raise OSError("broker unreachable")
        return {"accounts": self.accounts, "tenant_id": None, "unreadable_documents": []}


def _app(db: FakeFirestore, pool: FakePool) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.state.speech_zones = ("us-central1-a",)

    @app.exception_handler(ApiError)
    async def _error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status_code)

    ctx = SimpleNamespace(db=db, now=lambda: NOW,
                          settings=SimpleNamespace(voice_people=(ALICE,)))

    def _auth(request: Request) -> AuthContext:
        email = request.headers.get("x-test-user", ALICE)
        return AuthContext(principal=Principal(email=email, subject=email, domain="example.com"),
                           tenant_id="eng", is_admin=email == ROOT)

    app.dependency_overrides[current_auth] = _auth
    app.dependency_overrides[get_context] = lambda: ctx
    app.dependency_overrides[account_pool] = lambda: pool
    return TestClient(app)


def _read(pool: FakePool, *, user: str = ROOT, db: FakeFirestore | None = None) -> dict:
    r = _app(db or FakeFirestore(), pool).get("/v1/voice/settings", headers={"x-test-user": user})
    assert r.status_code == 200, r.text
    return r.json()


def _link(db: FakeFirestore, *, owner: str, label: str, used_at, expires_at) -> None:
    db.collection(sl.COLLECTION).document(secrets.token_hex(32)).set({
        "kind": sl.VOICE_API_KEY, "label": label, "owner": owner, "tenant_id": "ops",
        "created_by": owner, "created_at": expires_at - sl.VOICE_KEY_TTL,
        "expires_at": expires_at, "used_at": used_at})


# ---- the dedicated key's status ------------------------------------------------------


@pytest.mark.parametrize(("row", "state"), [
    ({}, "available"),
    ({"state": "PAUSED", "can_take": False}, "paused"),
    ({"state": "DRAINING", "can_take": False}, "draining"),
    ({"exhausted_at": (NOW - timedelta(hours=2)).isoformat()}, "credit_gone"),
    ({"can_take": False, "blocked_by": [{"limited_until": (NOW + timedelta(minutes=1))
                                         .isoformat(), "window": None}]}, "limited"),
])
def test_the_dedicated_keys_state_is_the_brokers(row, state):
    dedicated = _account("voice-key", dedicated=True, **row)
    body = _read(FakePool([_account("shared-key"), dedicated]))

    key = body["dedicated_key"]
    assert (key["set"], key["label"], key["state"]) == (True, "voice-key", state)


def test_who_added_the_dedicated_key_is_the_brokers_record_or_the_link_that_set_it():
    added = NOW - timedelta(days=2)
    answer = {"account": {"account_id": "ops:voice-key", "label": "voice-key",
                          "provider": "openai-api", "key_added_by": "dana@example.com",
                          "key_added_at": added.isoformat()}, "reason": ""}
    pool = FakePool([_account("voice-key", dedicated=True)], pool_answer=answer)
    key = _read(pool)["dedicated_key"]
    assert (key["by"], key["at"]) == ("dana@example.com", added.isoformat())

    # The pool answer named another account (a shared key Live would take
    # first): the last link redeemed for the dedicated key's label says who.
    db = FakeFirestore()
    _link(db, owner=ROOT, label="voice-key", used_at=NOW - timedelta(hours=1),
          expires_at=NOW - timedelta(minutes=55))
    _link(db, owner="erin@example.com", label="other", used_at=NOW - timedelta(minutes=5),
          expires_at=NOW + timedelta(minutes=5))
    other = {"account": {"account_id": "ops:shared-key", "label": "shared-key",
                         "provider": "openai-api", "key_added_by": "erin@example.com",
                         "key_added_at": NOW.isoformat()}, "reason": ""}
    pool = FakePool([_account("shared-key"), _account("voice-key", dedicated=True)],
                    pool_answer=other)
    key = _read(pool, db=db)["dedicated_key"]
    assert (key["by"], key["at"]) == (ROOT, (NOW - timedelta(hours=1)).isoformat())


def test_no_dedicated_key_reads_as_not_set_with_an_open_link():
    db = FakeFirestore()
    expires = NOW + timedelta(minutes=6)
    _link(db, owner=ROOT, label="voice-key", used_at=None, expires_at=expires)

    key = _read(FakePool([_account("shared-key")]), db=db)["dedicated_key"]

    assert key == {"set": False, "label": None, "display_name": None, "by": None, "at": None,
                   "state": None, "link": {"open": True, "expires_at": expires.isoformat()},
                   "replace": None}


def test_the_admin_replaces_the_dedicated_key_through_the_voice_api_key_link():
    body = _read(FakePool([_account("voice-key", dedicated=True)]), user=ROOT)

    assert body["dedicated_key"]["replace"] == {"kind": sl.VOICE_API_KEY, "label": "voice-key"}


def test_a_reader_who_is_not_an_admin_gets_the_read_only_view():
    pool = FakePool([_account("voice-key", dedicated=True)])
    body = _read(pool, user=ALICE)

    assert body["dedicated_key"]["set"] is True
    assert body["dedicated_key"]["replace"] is None
    assert [t["state"] for t in body["chain"]] == ["not_set_up", "not_set_up", "available"]


def test_a_broker_read_that_fails_is_unknown_never_not_set(caplog):
    with caplog.at_level(logging.WARNING):
        body = _read(FakePool(fails=True))

    key = body["dedicated_key"]
    assert (key["set"], key["state"], key["replace"]) == (None, None, None)
    assert [t["state"] for t in body["chain"]] == [None, None, None]
    assert "OSError" in caplog.text


# ---- the chain line ---------------------------------------------------------------------


def _tiers(body: dict) -> list[tuple[str, Any]]:
    return [(t["tier"], t["state"]) for t in body["chain"]]


def test_the_chain_with_plans_available():
    pool = FakePool([_account("plan-a", provider="openai"),
                     _account("voice-key", dedicated=True)])

    assert _tiers(_read(pool)) == [("plan", "available"), ("api_key", "not_set_up"),
                                   ("voice_dedicated", "available")]


def test_the_chain_with_plans_spent_and_a_shared_key():
    spent = [{"window": "five_hour", "used": 0.99, "floor": 0.95,
              "resets_at": (NOW + timedelta(hours=1)).isoformat(), "aged_reading": False}]
    pool = FakePool([_account("plan-a", provider="openai", can_take=False, blocked_by=spent),
                     _account("shared-key"),
                     _account("voice-key", dedicated=True,
                              exhausted_at=(NOW - timedelta(hours=1)).isoformat())])

    assert _tiers(_read(pool)) == [("plan", "no_quota_left"), ("api_key", "available"),
                                   ("voice_dedicated", "no_quota_left")]


def test_the_chain_with_nothing_set_up():
    # A workspace plan outside the Voice it pool, an Anthropic plan in it and
    # a key that does not list the person are no tier of this chain.
    pool = FakePool([_account("plan-w", provider="openai", pools=("workspace",)),
                     _account("claude", provider="anthropic"),
                     _account("shared-key", people=("bob@example.com",))])
    body = _read(pool)

    assert _tiers(body) == [("plan", "not_set_up"), ("api_key", "not_set_up"),
                            ("voice_dedicated", "not_set_up")]
    assert body["dedicated_key"]["set"] is False


def test_a_key_over_its_spend_cap_or_out_of_credit_has_no_quota_left_whatever_the_flag():
    pool = FakePool([_account("shared-key", monthly_spend_cap_usd=50.0, month_spend_usd=50.0)])

    assert _tiers(_read(pool))[1] == ("api_key", "no_quota_left")


# ---- nothing credential-shaped ---------------------------------------------------------


def test_no_key_value_or_secret_name_in_any_answer_or_log_line(caplog):
    key = "sk-" + secrets.token_hex(24)
    row = _account("voice-key", dedicated=True, credential=key, secret_name=SECRET_NAME)
    answer = {"account": {"account_id": "ops:voice-key", "label": "voice-key",
                          "provider": "openai-api", "credential": key}, "reason": ""}
    with caplog.at_level(logging.DEBUG):
        r = _app(FakeFirestore(), FakePool([row], pool_answer=answer)).get(
            "/v1/voice/settings", headers={"x-test-user": ROOT})
        failed = _app(FakeFirestore(), FakePool(fails=True)).get(
            "/v1/voice/settings", headers={"x-test-user": ROOT})

    for text in (r.text, failed.text, caplog.text):
        assert key not in text
        assert SECRET_NAME not in text
    # Another workspace's account is never named: only the dedicated key's label.
    assert "ops:" not in r.text and "owner_tenant" not in r.text


def test_the_status_is_read_from_the_brokers_listing_once_per_answer():
    pool = FakePool([_account("voice-key", dedicated=True)])
    _read(pool)

    assert pool.listed == 1


def test_the_openai_key_status_is_unchanged_beside_it():
    status = vsettings.key_status({"account": None, "reason": vsettings.KEY_BUSY}, [], now=NOW)
    body = _read(FakePool([_account("voice-key", dedicated=True)],
                          pool_answer={"account": None, "reason": vsettings.KEY_BUSY}))

    assert body["openai_key"] == status


def test_the_client_reads_the_brokers_platform_wide_listing_with_no_tenant():
    seen: list[dict] = []

    def transport(method, url, *, headers, body=None, timeout=15.0):
        seen.append({"method": method, "url": url, "body": body})
        return 200, {"accounts": [], "tenant_id": None, "unreadable_documents": []}

    client = BrokerClient(base_url="https://broker.internal", audience="https://broker.example",
                          tokens=StaticIdToken(secrets.token_hex(16)), transport=transport)

    assert client.voice_it_accounts()["accounts"] == []
    assert seen == [{"method": "GET", "url": "https://broker.internal/v1/accounts",
                     "body": None}]
