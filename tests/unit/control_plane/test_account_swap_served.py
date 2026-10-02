"""A swap, as each viewer may see it, and the refresh record on the listing.

The broker records a mid-run account swap on both holds (S13/S14): the one
left closes `swapped` with `swapped_to`, the one taken opens with
`swapped_from`. swarm-api serves it beside the hold or span it already serves,
under the same tenant rules (#379), and one more: the OTHER account's id and
label are shown only when the caller may see that account -- it is in the
caller's own listing, owned or lent. Otherwise the swap is `withheld`.

Accounts: `eng:first` (eng's, lent to research), `eng:second` (eng's alone),
`research:own` (research's alone). alice is eng, bob is research, root an admin.

Also here: `GET /v1/accounts` forwards each account's refresh record and the
sweep's liveness (U27) by allow-list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.main import create_app

from .conftest import auth_header, seed_task

NOW = datetime.now(timezone.utc)
FIRST, SECOND, OWN = "eng:first", "eng:second", "research:own"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _span(tenant, task, *, hours_ago, end=None, to=None, came=None, why="exhausted"):
    at = NOW - timedelta(hours=hours_ago)
    return {
        "tenant_id": tenant, "task_id": task, "attempt_id": None,
        "assigned_at": _iso(at),
        "released_at": _iso(at + timedelta(minutes=20)) if end else None,
        "end": end,
        "hold_expires_at": _iso(at + timedelta(hours=3)),
        "swapped_from": came, "swapped_from_reason": why if came else None,
        "swapped_to": to, "swapped_to_reason": why if to else None,
    }


#: eng's task moved first -> second; research's task moved first -> research:own.
SPANS = {
    FIRST: [
        _span("eng", "eng-task", hours_ago=2, end="swapped", to=SECOND),
        _span("research", "research-task", hours_ago=1, end="swapped", to=OWN, why="drain"),
    ],
    SECOND: [_span("eng", "eng-task", hours_ago=2, came=FIRST)],
    OWN: [_span("research", "research-task", hours_ago=1, came=FIRST, why="drain")],
}

HOLDS = {
    SECOND: [{"tenant_id": "eng", "task_id": "eng-task", "attempt_id": None,
              "assigned_at": _iso(NOW - timedelta(hours=2)),
              "expires_at": _iso(NOW + timedelta(hours=1)),
              "swapped_from": FIRST, "swapped_from_reason": "exhausted", "move": None}],
    OWN: [{"tenant_id": "research", "task_id": "research-task", "attempt_id": None,
           "assigned_at": _iso(NOW - timedelta(hours=1)),
           "expires_at": _iso(NOW + timedelta(hours=1)),
           "swapped_from": FIRST, "swapped_from_reason": "drain", "move": None}],
    FIRST: [],
}


class FakeBroker:
    def __init__(self) -> None:
        self.accounts = {
            FIRST: {"account_id": FIRST, "owner_tenant": "eng", "label": "first",
                    "lend_to": ["research"]},
            SECOND: {"account_id": SECOND, "owner_tenant": "eng", "label": "second",
                     "lend_to": []},
            OWN: {"account_id": OWN, "owner_tenant": "research", "label": "own",
                  "lend_to": []},
        }
        self.listing_extra: dict[str, Any] = {}

    def list_accounts(self, tenant_id: str) -> dict:
        return {"accounts": [
            dict(a) for a in self.accounts.values()
            if a["owner_tenant"] == tenant_id or tenant_id in a["lend_to"]
        ], **self.listing_extra}

    def holds(self, account_id: str) -> dict:
        return {"account_id": account_id, "holds": [dict(h) for h in HOLDS[account_id]]}

    def hold_history(self, account_id: str, *, start, end, cursor) -> dict:
        return {"account_id": account_id, "from": start or _iso(NOW - timedelta(days=7)),
                "to": end or _iso(NOW), "spans": [dict(s) for s in SPANS[account_id]],
                "next_cursor": None}


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def client(api_context, db, broker) -> TestClient:
    seed_task(db, task_id="eng-task", tenant_id="eng", state="RUNNING")
    seed_task(db, task_id="research-task", tenant_id="research", state="RUNNING")
    app = create_app(api_context)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


def _history(client, account, user, **params):
    r = client.get(f"/v1/accounts/{account}/history", params=params, headers=auth_header(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_both_accounts_history_reads_show_the_owners_swap(client):
    left = _history(client, FIRST, "alice")
    taken = _history(client, SECOND, "alice")

    mine_left = [s for s in left["spans"] if s["mine"]]
    assert [s["end"] for s in mine_left] == ["swapped"]
    assert mine_left[0]["swapped_to"] == {
        "account_id": SECOND, "label": "second", "reason": "exhausted", "withheld": False,
    }
    assert taken["spans"][0]["swapped_from"] == {
        "account_id": FIRST, "label": "first", "reason": "exhausted", "withheld": False,
    }


def test_an_owner_sees_a_borrower_move_off_its_account_but_not_where(client):
    """The borrower moved to its OWN account, which eng cannot see. eng learns
    that the borrower's span ended in a swap and why; not the account's label
    or id."""
    left = _history(client, FIRST, "alice")

    theirs = [s for s in left["spans"] if not s["mine"]]
    assert theirs[0]["end"] == "swapped"
    assert theirs[0]["swapped_to"] == {
        "account_id": None, "label": None, "reason": "drain", "withheld": True,
    }
    served = json.dumps(left)
    assert OWN not in served and '"own"' not in served


def test_a_borrower_sees_its_own_swap_with_both_labels(client):
    left = _history(client, FIRST, "bob")
    taken = _history(client, OWN, "bob")

    assert [s["swapped_to"]["label"] for s in left["spans"]] == ["own"]
    assert left["others"] == 1, "eng's span is a count, its swap with it"
    assert SECOND not in json.dumps(left), "eng's other account is not named to research"
    assert taken["spans"][0]["swapped_from"]["label"] == "first", "a lent account it may see"


def test_the_holders_read_serves_where_a_holder_came_from(client):
    body = client.get(f"/v1/accounts/{SECOND}/holders", headers=auth_header("alice")).json()

    assert body["holders"][0]["swapped_from"] == {
        "account_id": FIRST, "label": "first", "reason": "exhausted", "withheld": False,
    }


def test_the_admin_sees_every_swap_named(client):
    left = _history(client, FIRST, "root", scope="platform")

    assert {s["swapped_to"]["account_id"] for s in left["spans"]} == {SECOND, OWN}
    assert all(not s["swapped_to"]["withheld"] for s in left["spans"])


def test_a_span_with_no_swap_keeps_its_shape(client, broker):
    SPANS[SECOND].append(_span("eng", "eng-task", hours_ago=5, end="released"))
    try:
        spans = _history(client, SECOND, "alice")["spans"]
    finally:
        SPANS[SECOND].pop()
    plain = [s for s in spans if s["end"] == "released"][0]
    assert "swapped_from" not in plain and "swapped_to" not in plain


# -- the refresh record on the listing (U27) ------------------------------


def test_the_listing_forwards_the_refresh_record_and_the_sweeps_liveness(client, broker):
    expires = _iso(NOW + timedelta(hours=4))
    broker.accounts[SECOND].update(
        last_refresh_at=_iso(NOW), token_expires_at=expires, last_refresh_reason="refreshed",
    )
    broker.listing_extra = {"refresh_sweep": {
        "last_completed_at": _iso(NOW), "last_outcome": "ok", "readable": True,
        "holder": "an-instance-name",
    }}

    body = client.get("/v1/accounts", headers=auth_header("alice")).json()

    second = next(a for a in body["accounts"] if a["account_id"] == SECOND)
    assert (second["token_expires_at"], second["last_refresh_reason"]) == (expires, "refreshed")
    assert body["refresh_sweep"] == {
        "last_completed_at": _iso(NOW), "last_outcome": "ok", "readable": True,
    }, "by allow-list: the lease holder is not served"


def test_a_broker_that_serves_no_liveness_reads_as_unknown(client):
    body = client.get("/v1/accounts", headers=auth_header("alice")).json()

    assert body["refresh_sweep"] == {
        "last_completed_at": None, "last_outcome": None, "readable": False,
    }
