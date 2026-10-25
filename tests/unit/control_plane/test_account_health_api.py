"""`GET /v1/accounts/{id}/health` and the list's health fields, per viewer (#2302).

The broker's record names every tenant and task that met an account, so the
human-facing boundary is swarm-api's, exactly as for the holder reads: the
account's owner, a workspace it is lent to, or a platform admin with
`scope=platform`; anyone else gets the 404 an unknown id gets. What each
viewer is shown of an event, and that every message is redacted on the way
out, is pinned here against a fake broker; the tasks are real documents in the
fake Firestore the real `Store` reads.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.main import create_app

from .conftest import auth_header, seed_task

NOW = datetime.now(timezone.utc)
SHARED = "eng:shared"
SOLO = "eng:solo"
FOREIGN = "research:own"


def _token() -> str:
    """Credential-shaped, built at runtime (CLAUDE.md: never a literal)."""
    return "-".join(("sk", "ant", "oat01", secrets.token_hex(24)))


TOKEN = _token()


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


def _counts(**n: int) -> dict[str, int]:
    kinds = ("rate_limit", "exhausted", "auth", "overloaded", "server_error", "network",
             "assign_refused", "reauth", "refresh_failed")
    return {k: n.get(k, 0) for k in kinds}


def _record(account_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "status": "limited",
        "limited_until": (NOW + timedelta(hours=2)).isoformat(),
        "counts": {
            "last_1h": _counts(rate_limit=2),
            "last_24h": _counts(rate_limit=2, network=1),
            "last_7d": _counts(rate_limit=3, network=1),
        },
        "hourly": [{"hour": (NOW - timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
                    .isoformat(), "counts": {"rate_limit": 1} if h == 0 else {}, "total": 0}
                   for h in range(167, -1, -1)],
        "events": [
            _event("rate_limit", "research", "research-task-1", minutes_ago=7,
                   message=f"refused with Authorization: Bearer {TOKEN}"),
            _event("rate_limit", "eng", "eng-task-1", minutes_ago=20,
                   message="claude-code reported a provider rate limit"),
            # eng's worker naming a task eng does not own: never shown as a task.
            _event("network", "eng", "research-task-1", minutes_ago=90),
        ],
        "observed_at": NOW.isoformat(),
        "secret": "swarm-account-eng--shared",
    }


class FakeBroker:
    def __init__(self) -> None:
        self.accounts = {
            SHARED: {"account_id": SHARED, "owner_tenant": "eng", "lend_to": ["research"],
                     "health_24h": _counts(rate_limit=2, network=1), "health_status": "limited"},
            SOLO: {"account_id": SOLO, "owner_tenant": "eng", "lend_to": [],
                   "health_24h": {"rate_limit": "lots"}, "health_status": "on fire"},
            FOREIGN: {"account_id": FOREIGN, "owner_tenant": "research", "lend_to": []},
        }
        self.calls: list[str] = []

    def list_accounts(self, tenant_id: str) -> dict:
        return {"accounts": [
            dict(a) for a in self.accounts.values()
            if a["owner_tenant"] == tenant_id or tenant_id in a["lend_to"]
        ]}

    def account_health(self, account_id: str) -> dict:
        self.calls.append(account_id)
        return _record(account_id)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def client(api_context, db, broker) -> TestClient:
    seed_task(db, task_id="eng-task-1", tenant_id="eng", state="RUNNING")
    seed_task(db, task_id="research-task-1", tenant_id="research", state="RUNNING")
    app = create_app(api_context)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


def _health(client, account: str, user: str, **params: str):
    return client.get(f"/v1/accounts/{account}/health", params=params, headers=auth_header(user))


def test_the_owner_reads_counts_series_events_and_status(client):
    r = _health(client, SHARED, "alice")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "limited"
    assert body["counts"]["last_1h"]["rate_limit"] == 2
    assert body["counts"]["last_24h"]["network"] == 1
    assert body["counts"]["last_7d"]["rate_limit"] == 3
    assert len(body["hourly"]) == 168
    assert [e["kind"] for e in body["events"]] == ["rate_limit", "rate_limit", "network"]
    by_tenant = [e["tenant_id"] for e in body["events"]]
    assert by_tenant == ["research", "eng", "eng"], "the owner sees which tenant met it"
    assert "secret" not in body and "swarm-account" not in r.text


def test_a_task_is_shown_only_to_its_own_tenant_and_only_when_it_owns_it(client):
    served = _health(client, SHARED, "alice").json()["events"]

    assert served[0]["task_id"] is None, "the owner is not shown a borrower's task"
    assert served[1]["task_id"] == "eng-task-1"
    assert served[2]["task_id"] is None, "a task eng does not own is never shown as eng's"


def test_a_borrower_sees_its_own_events_and_only_the_shape_of_anyone_elses(client):
    r = _health(client, SHARED, "bob")

    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert events[0]["own"] is True and events[0]["task_id"] == "research-task-1"
    for other in events[1:]:
        assert other["tenant_id"] is None and other["task_id"] is None
        assert other["message"] is None
        assert datetime.fromisoformat(other["at"]).minute == 0, "floored to the UTC hour"
    assert "eng-task-1" not in r.text
    assert "claude-code reported" not in r.text


def test_a_caller_who_may_not_read_the_account_is_answered_404(client, broker):
    r = _health(client, FOREIGN, "alice")

    assert r.status_code == 404, r.text
    assert broker.calls == [], "refused before the broker was asked"


def test_a_tenant_without_the_account_is_answered_404_for_a_solo_account(client, broker):
    r = _health(client, SOLO, "bob")

    assert r.status_code == 404, r.text
    assert broker.calls == []


def test_the_platform_view_needs_an_admin(client):
    assert _health(client, SHARED, "alice", scope="platform").status_code == 403
    r = _health(client, FOREIGN, "root", scope="platform")
    assert r.status_code == 200, r.text
    assert r.json()["events"][0]["task_id"] == "research-task-1"


@pytest.mark.parametrize("user", ["alice", "bob", "root"])
def test_every_message_is_redacted_on_the_way_out(client, user):
    params = {"scope": "platform"} if user == "root" else {}

    r = _health(client, SHARED, user, **params)

    assert r.status_code == 200, r.text
    assert TOKEN not in r.text


def test_the_list_carries_the_24h_counts_and_the_status_by_allow_list(client):
    rows = {a["account_id"]: a for a in
            client.get("/v1/accounts", headers=auth_header("alice")).json()["accounts"]}

    assert rows[SHARED]["health_status"] == "limited"
    assert rows[SHARED]["health_24h"]["rate_limit"] == 2
    assert rows[SHARED]["health_24h"]["network"] == 1
    assert rows[SOLO]["health_status"] is None, "a status outside the four is served as none"
    assert rows[SOLO]["health_24h"]["rate_limit"] == 0
    assert json.dumps(rows).count("lots") == 0
