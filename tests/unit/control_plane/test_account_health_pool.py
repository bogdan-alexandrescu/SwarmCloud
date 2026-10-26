"""`GET /v1/accounts/health`: every account's health record in ONE read (#2302).

Capacity › Account health (ACCOUNT-HEALTH-PAGE) draws a lane per account over
7 days and each account's newest event. Proposal B read that as one
`GET /v1/accounts/{id}/health` per account, every poll; on a pool of 30 that
is 30 reads a poll, and its own "open" list asked whether the broker should
serve the pool in one read instead. This is that read:

  * the broker's `GET /v1/accounts/health?tenant_id=<t>` -- platform only, like
    the per-account read -- answers every account `t` may run on (owned and
    lent, `may_serve`, the listing's own rule) with the same record
    `health_to_api` serves, plus the owner and the loan list swarm-api needs
    to decide each account's viewer;
  * swarm-api's `GET /v1/accounts/health` makes exactly ONE broker call (not a
    listing plus one per account), drops any record the caller's tenant may not
    read even if the broker sent it, and shows each event by the per-account
    read's own rule (`health_view`): owner, borrower or platform.

BREAK IT: read the records one call per account in swarm-api, serve research's
own account to eng, drop the borrower floor, or skip the redaction -- a case
below turns red.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import WindowReading
from quota_broker.accountstore import HEALTH_EVENTS_MAX, AccountStore
from quota_broker.main import create_app as create_broker
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings
from swarm_api.main import create_app as create_api

from .conftest import auth_header, core_settings, seed_task
from .fakes import FakeFirestore

ENG = "eng"
RESEARCH = "research"
OWN = f"{ENG}:own"
LENT_IN = f"{RESEARCH}:lent-in"
THEIRS = f"{RESEARCH}:theirs"


def _token() -> str:
    """Credential-shaped, built at runtime (CLAUDE.md: never a literal)."""
    return "-".join(("sk", "ant", "oat01", secrets.token_hex(24)))


TOKEN = _token()


# ---------------------------------------------------------------------------
# The broker's route
# ---------------------------------------------------------------------------


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = True

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


def _read(store: AccountStore, account_id: str) -> None:
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    store.record_reading(
        account_id,
        {"five_hour": WindowReading(0.2, later), "seven_day": WindowReading(0.2, later + timedelta(days=3))},
        datetime.now(timezone.utc),
    )


@pytest.fixture()
def store() -> AccountStore:
    s = AccountStore(FakeFirestore())
    s.register(ENG, "own")
    s.register(RESEARCH, "lent-in", lend_to=(ENG,))
    s.register(RESEARCH, "theirs")
    for a in (OWN, LENT_IN, THEIRS):
        _read(s, a)
    return s


@pytest.fixture()
def broker_client(store):
    db = store._db  # noqa: SLF001 - the broker and the store share one fake database
    broker = QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=40))
    identity = _Identity()
    c = TestClient(create_broker(broker, identity=identity, account_store=store),
                   raise_server_exceptions=False)
    c.identity = identity
    return c


def test_the_broker_serves_every_account_the_tenant_may_run_on_in_one_answer(broker_client, store):
    store.record_health(OWN, "network", tenant_id=ENG, task_id="task_a")
    store.record_health(LENT_IN, "overloaded", tenant_id=RESEARCH, task_id="task_b")
    store.record_health(THEIRS, "auth", tenant_id=RESEARCH, task_id="task_c")

    r = broker_client.get("/v1/accounts/health", params={"tenant_id": ENG})

    assert r.status_code == 200, r.text
    body = r.json()
    rows = {a["account_id"]: a for a in body["accounts"]}
    assert set(rows) == {OWN, LENT_IN}, "owned and lent, never research's own"
    assert rows[OWN]["owner_tenant"] == ENG
    assert rows[LENT_IN]["lend_to"] == [ENG]
    assert [e["kind"] for e in rows[OWN]["events"]] == ["network"]
    assert rows[OWN]["counts"]["last_24h"]["network"] == 1
    assert len(rows[OWN]["hourly"]) == 168
    assert rows[OWN]["status"] in ("healthy", "limited", "erroring", "reauth_needed")
    assert body["events_max"] == HEALTH_EVENTS_MAX


def test_the_broker_record_is_the_per_account_read_s_record(broker_client, store):
    store.record_health(OWN, "rate_limit", tenant_id=ENG, task_id="task_a", window="five_hour")

    pool = {a["account_id"]: a for a in
            broker_client.get("/v1/accounts/health", params={"tenant_id": ENG}).json()["accounts"]}
    one = broker_client.get(f"/v1/accounts/{OWN}/health").json()

    for key in ("status", "limited_until", "counts", "events"):
        assert pool[OWN][key] == one[key], key


def test_the_broker_keeps_at_most_50_events_per_account(broker_client, store):
    for n in range(HEALTH_EVENTS_MAX + 7):
        store.record_health(OWN, "network", tenant_id=ENG, task_id=f"task_{n}")

    rows = {a["account_id"]: a for a in
            broker_client.get("/v1/accounts/health", params={"tenant_id": ENG}).json()["accounts"]}

    assert len(rows[OWN]["events"]) == HEALTH_EVENTS_MAX


def test_the_broker_refuses_a_tenant_caller(broker_client):
    broker_client.identity.platform = False

    r = broker_client.get("/v1/accounts/health", params={"tenant_id": ENG})

    assert r.status_code == 403, r.text


def test_the_platform_unscoped_read_names_every_account(broker_client):
    r = broker_client.get("/v1/accounts/health")

    assert r.status_code == 200, r.text
    assert {a["account_id"] for a in r.json()["accounts"]} == {OWN, LENT_IN, THEIRS}


# ---------------------------------------------------------------------------
# swarm-api's route
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _event(kind: str, tenant: str | None, task: str | None, *, minutes_ago: int,
           message: str = "") -> dict[str, Any]:
    return {
        "at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "kind": kind,
        "tenant_id": tenant,
        "task_id": task,
        "window": "five_hour" if kind == "rate_limit" else None,
        "reset_at": (NOW + timedelta(hours=2)).isoformat() if kind == "rate_limit" else None,
        "message": message,
    }


def _record(account_id: str, owner: str, lend_to: list[str]) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "owner_tenant": owner,
        "lend_to": lend_to,
        "label": account_id.split(":", 1)[1],
        "status": "limited",
        "limited_until": (NOW + timedelta(hours=2)).isoformat(),
        "counts": {"last_1h": {"rate_limit": 1}, "last_24h": {"rate_limit": 2}, "last_7d": {"rate_limit": 2}},
        "hourly": [{"hour": (NOW - timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
                    .isoformat(), "counts": {}, "total": 0} for h in range(167, -1, -1)],
        "events": [
            _event("rate_limit", "research", "research-task-1", minutes_ago=7,
                   message=f"refused with Authorization: Bearer {TOKEN}"),
            _event("rate_limit", "eng", "eng-task-1", minutes_ago=20,
                   message="claude-code reported a provider rate limit"),
        ],
        "observed_at": NOW.isoformat(),
        "secret": "swarm-account-eng--own",
    }


class FakePool:
    """The broker as swarm-api sees it. It MISBEHAVES on purpose: asked for
    eng's pool it also sends research's own account, which the route must
    drop rather than trust the broker's filter alone."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def accounts_health(self, tenant_id: str | None) -> dict[str, Any]:
        self.calls.append(("accounts_health", tenant_id))
        return {
            "tenant_id": tenant_id,
            "accounts": [
                _record(OWN, ENG, ["research"]),
                _record(LENT_IN, RESEARCH, [ENG]),
                _record(THEIRS, RESEARCH, []),
            ],
            "events_max": 50,
            "observed_at": NOW.isoformat(),
        }

    def list_accounts(self, tenant_id: str) -> dict[str, Any]:
        self.calls.append(("list_accounts", tenant_id))
        return {"accounts": []}

    def account_health(self, account_id: str) -> dict[str, Any]:
        self.calls.append(("account_health", account_id))
        return {}


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def client(api_context, db, pool) -> TestClient:
    seed_task(db, task_id="eng-task-1", tenant_id="eng", state="RUNNING")
    seed_task(db, task_id="research-task-1", tenant_id="research", state="RUNNING")
    app = create_api(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _pool_health(client, user: str, **params: str):
    return client.get("/v1/accounts/health", params=params, headers=auth_header(user))


def test_the_pool_read_is_one_broker_call(client, pool):
    r = _pool_health(client, "alice")

    assert r.status_code == 200, r.text
    assert pool.calls == [("accounts_health", ENG)], "one call, never a listing plus one per account"


def test_the_pool_read_refuses_another_tenants_account_even_if_the_broker_sends_it(client):
    body = _pool_health(client, "alice").json()

    ids = [a["account_id"] for a in body["accounts"]]
    assert ids == [OWN, LENT_IN]
    assert THEIRS not in str(body)
    assert body["tenant_id"] == ENG


def test_each_account_is_shown_by_the_per_account_viewer_rule(client):
    rows = {a["account_id"]: a for a in _pool_health(client, "alice").json()["accounts"]}

    own = rows[OWN]["events"]
    assert [e["tenant_id"] for e in own] == ["research", "eng"], "the owner sees who met it"
    assert own[0]["task_id"] is None and own[1]["task_id"] == "eng-task-1"
    borrowed = rows[LENT_IN]["events"]
    assert borrowed[0]["tenant_id"] is None and borrowed[0]["message"] is None, (
        "a borrower sees only the shape of the owner's tenant's events"
    )
    assert datetime.fromisoformat(borrowed[0]["at"]).minute == 0
    assert borrowed[1]["own"] is True and borrowed[1]["task_id"] == "eng-task-1"
    assert rows[OWN]["label"] == "own" and rows[OWN]["status"] == "limited"
    assert len(rows[OWN]["hourly"]) == 168
    assert "secret" not in rows[OWN] and "swarm-account" not in str(rows)


def test_the_platform_view_needs_an_admin(client, pool):
    assert _pool_health(client, "alice", scope="platform").status_code == 403
    assert pool.calls == [], "refused before the broker was asked"

    r = _pool_health(client, "root", scope="platform")
    assert r.status_code == 200, r.text
    assert pool.calls == [("accounts_health", None)]
    assert {a["account_id"] for a in r.json()["accounts"]} == {OWN, LENT_IN, THEIRS}


@pytest.mark.parametrize("user", ["alice", "bob", "root"])
def test_every_message_is_redacted_on_the_way_out(client, user):
    params = {"scope": "platform"} if user == "root" else {}

    r = _pool_health(client, user, **params)

    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
