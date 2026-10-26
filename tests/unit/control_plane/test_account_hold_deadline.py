"""Each account's latest hold deadline is on its listing row (#2222, ACCOUNTS-TIMELINE-MERGE).

The Timeline (mock-up B, frames B3 and B4) draws a full account's agents up to
the latest instant any of them can still hold a slot: its holds' deadlines,
`Hold.expires_at`. `GET /v1/accounts` did not serve them, so the lane drew a
dash. Now:

  * THE BROKER SERVES `latest_hold_deadline` on every row: the latest
    `expires_at` among the account's LIVE holds, or null when it holds none.
    An expired hold has stopped counting (`Account.live_holds`), so its
    deadline is not the account's.
  * SWARM-API FORWARDS IT BY ALLOW-LIST: an instant with a zone passes, and
    anything else is null -- never a string the page would have to guess at.
    A row from a broker older than the field carries no key at all, so the
    page can say "not served" rather than "no hold".

Unit tests: no credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import (
    Account,
    AccountState,
    Hold,
    WindowReading,
    account_id_for,
)
from quota_broker.main import account_to_api
from swarm_api.main import create_app

from .conftest import auth_header
from .test_account_api import FakePool, account

ENG = "eng"
NOW = datetime(2026, 10, 24, 14, 4, tzinfo=timezone.utc)


def _hold(i: int, expires_at: datetime) -> Hold:
    return Hold(
        assignment_id=f"a{i}",
        tenant_id=ENG,
        expires_at=expires_at,
        assigned_at=expires_at - timedelta(hours=3),
    )


def _acct(holds: tuple[Hold, ...]) -> Account:
    return Account(
        account_id=account_id_for(ENG, "devops-team"),
        owner_tenant=ENG,
        label="devops-team",
        state=AccountState.AVAILABLE,
        windows={
            "five_hour": WindowReading(0.30, NOW + timedelta(hours=3)),
            "seven_day": WindowReading(0.60, NOW + timedelta(days=4)),
        },
        observed_at=NOW,
        holds=holds,
        assigned=len(holds),
    )


# --------------------------------------------------------------------------
# The broker
# --------------------------------------------------------------------------


def test_the_row_carries_the_latest_live_holds_deadline():
    late = NOW + timedelta(hours=2, minutes=41)
    holds = (
        _hold(0, NOW + timedelta(minutes=20)),
        _hold(1, late),
        _hold(2, NOW + timedelta(hours=1)),
    )

    row = account_to_api(_acct(holds), now=NOW)

    assert row["latest_hold_deadline"] == late.isoformat()


def test_an_expired_hold_is_not_the_accounts_deadline():
    """An expired hold stopped counting at its deadline, so a later-written
    but already-expired one never stands in for the live holds'."""
    live = NOW + timedelta(minutes=50)
    holds = (_hold(0, NOW - timedelta(minutes=1)), _hold(1, live))

    row = account_to_api(_acct(holds), now=NOW)

    assert row["latest_hold_deadline"] == live.isoformat()


def test_an_account_with_no_live_hold_serves_null():
    assert account_to_api(_acct(()), now=NOW)["latest_hold_deadline"] is None
    only_expired = (_hold(0, NOW - timedelta(seconds=1)),)
    assert account_to_api(_acct(only_expired), now=NOW)["latest_hold_deadline"] is None


# --------------------------------------------------------------------------
# swarm-api
# --------------------------------------------------------------------------


@pytest.fixture
def pool() -> FakePool:
    return FakePool(
        account("eng", "primary"),
        account("eng", "older"),
        account("research", "shared", lend_to=("eng",)),
    )


@pytest.fixture
def client(api_context, pool) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _rows(client) -> dict[str, dict]:
    body = client.get("/v1/accounts", headers=auth_header("alice")).json()
    return {a["account_id"]: a for a in body["accounts"]}


def test_the_listing_forwards_a_served_deadline(client, pool):
    at = (NOW + timedelta(hours=2)).isoformat()
    pool.accounts["eng:primary"]["latest_hold_deadline"] = at
    pool.accounts["research:shared"]["latest_hold_deadline"] = None

    rows = _rows(client)

    assert rows["eng:primary"]["latest_hold_deadline"] == at
    assert rows["research:shared"]["latest_hold_deadline"] is None


@pytest.mark.parametrize(
    "served",
    ["soon", "2026-10-24T16:45:00", 1761324300, "", True, {"at": "2026-10-24T16:45:00+00:00"}],
)
def test_a_deadline_that_is_not_an_instant_with_a_zone_is_null(client, pool, served):
    pool.accounts["eng:primary"]["latest_hold_deadline"] = served

    assert _rows(client)["eng:primary"]["latest_hold_deadline"] is None


def test_a_broker_older_than_the_field_serves_no_key(client, pool):
    """No key, not null: the page tells "this broker does not say" from
    "it holds no live hold"."""
    assert "latest_hold_deadline" not in _rows(client)["eng:older"]
