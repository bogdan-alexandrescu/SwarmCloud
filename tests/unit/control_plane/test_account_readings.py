"""A holding worker's rate-limit readings reach the account (S15).

Every claude-code run streams `rate_limit_event` readings and they were thrown
away; the only writer of `Account.windows` was the broker's usage poller, on a
budget of about five calls per five minutes shared with every human and laptop
on the same accounts. `POST /v1/accounts/{id}/readings` is where a worker's
readings land, and these pin its rules:

  * ONLY THE HOLDER. A reading steers `choose()`, so a reading for an account
    the caller does not hold -- for this task and attempt -- is a 403.
  * A MERGE PER WINDOW. A reading carrying only `seven_day` keeps the stored
    `five_hour` (docs/web-ui/06-accounts.md P4, audit finding 4).
  * OUT-OF-RANGE IS REFUSED: a utilization outside 0..1, a non-finite one, and
    a window whose reset is already in the past.
  * NOTHING BUT FIGURES: a body with any other field is refused, so a reading
    cannot carry a token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import Account, WindowReading
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

ENG = "eng"
ACCOUNT = f"{ENG}:personal"
TASK = "task_1"
ATTEMPT = "att_1"


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def store(db) -> AccountStore:
    s = AccountStore(db)
    s.register(ENG, "personal", lend_to=("research",))
    return s


@pytest.fixture()
def client(db, store):
    broker = QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))
    identity = _Identity()
    c = TestClient(create_app(broker, identity=identity, account_store=store),
                   raise_server_exceptions=False)
    c.identity = identity
    return c


def _held(client) -> dict[str, Any]:
    r = client.post("/v1/accounts/assign",
                    json={"provider": "anthropic", "task_id": TASK, "attempt_id": ATTEMPT})
    assert r.json()["account_id"] == ACCOUNT, r.text
    return r.json()


def _in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _report(client, held, windows, **overrides):
    body = {
        "assignment_id": held["assignment_id"],
        "task_id": TASK,
        "attempt_id": ATTEMPT,
        "windows": windows,
        **overrides,
    }
    return client.post(f"/v1/accounts/{ACCOUNT}/readings", json=body)


def _windows(store) -> dict[str, float]:
    return {name: w.utilization for name, w in store.get(ACCOUNT).windows.items()}


def test_the_holder_records_a_reading(client, store):
    held = _held(client)

    r = _report(client, held, {"five_hour": {"utilization": 0.42, "resets_at": _in(2)}})

    assert r.status_code == 200, r.text
    assert r.json() == {"account_id": ACCOUNT, "recorded": True, "windows": ["five_hour"]}
    assert _windows(store) == {"five_hour": 0.42}
    assert store.get(ACCOUNT).observed_at is not None


def test_a_reading_of_one_window_keeps_the_other(client, store):
    held = _held(client)
    _report(client, held, {"five_hour": {"utilization": 0.9, "resets_at": _in(2)}})

    _report(client, held, {"seven_day": {"utilization": 0.3, "resets_at": _in(90)}})

    assert _windows(store) == {"five_hour": 0.9, "seven_day": 0.3}
    assert store.get(ACCOUNT).headroom(datetime.now(timezone.utc)) == pytest.approx(0.1), (
        "the binding five_hour still binds"
    )


def test_the_merge_keeps_the_newer_reading_of_each_window():
    """`Account.with_reading` itself: per window, newest wins, other windows stay."""
    now = datetime.now(timezone.utc)
    later = now + timedelta(hours=3)
    a = Account(account_id=ACCOUNT, owner_tenant=ENG, label="personal")
    a = a.with_reading({"five_hour": WindowReading(0.9, later)}, now)
    a = a.with_reading({"seven_day": WindowReading(0.2, later)}, now + timedelta(minutes=1))
    stale = a.with_reading({"five_hour": WindowReading(0.1, later)}, now - timedelta(minutes=5))

    assert {n: w.utilization for n, w in stale.windows.items()} == {
        "five_hour": 0.9, "seven_day": 0.2,
    }, "an older five_hour must not replace a newer one, whatever seven_day's age"
    newer = stale.with_reading(
        {"five_hour": WindowReading(0.5, later)}, now + timedelta(seconds=30)
    )
    assert newer.windows["five_hour"].utilization == 0.5
    assert newer.observed_at == now + timedelta(minutes=1), "the newest reading dates the account"


def test_a_reading_for_an_account_the_caller_does_not_hold_is_refused(client, store):
    held = _held(client)

    other_attempt = _report(client, held, {"five_hour": {"utilization": 0.1, "resets_at": _in(2)}},
                            attempt_id="att_other")
    forged = _report(client, held, {"five_hour": {"utilization": 0.1, "resets_at": _in(2)}},
                     assignment_id="not-the-hold")
    client.identity.tenant = "research"    # a borrower, which may_serve allows
    borrower = _report(client, held, {"five_hour": {"utilization": 0.1, "resets_at": _in(2)}})
    client.identity.platform = True
    platform = _report(client, held, {"five_hour": {"utilization": 0.1, "resets_at": _in(2)}})

    assert [r.status_code for r in (other_attempt, forged, borrower, platform)] == [403] * 4
    assert _windows(store) == {}, "nothing was recorded"


def test_a_released_hold_can_no_longer_report(client, store):
    held = _held(client)
    client.post(f"/v1/accounts/{ACCOUNT}/release", json={"assignment_id": held["assignment_id"]})

    r = _report(client, held, {"five_hour": {"utilization": 0.1, "resets_at": _in(2)}})

    assert r.status_code == 403


@pytest.mark.parametrize("window", [
    {"utilization": 1.01, "resets_at": "IN2"},
    {"utilization": -0.01, "resets_at": "IN2"},
    {"utilization": "NaN", "resets_at": "IN2"},
    {"utilization": 0.5, "resets_at": "PAST"},
])
def test_an_out_of_range_reading_is_refused(client, store, window):
    held = _held(client)
    resets = {"IN2": _in(2), "PAST": _in(-0.01)}[window["resets_at"]]
    utilization = float(window["utilization"]) if window["utilization"] == "NaN" else window[
        "utilization"
    ]
    payload = {"utilization": utilization, "resets_at": resets}
    if window["utilization"] == "NaN":
        # JSON has no NaN; send what a non-strict encoder would.
        r = client.post(
            f"/v1/accounts/{ACCOUNT}/readings",
            content=(
                '{"assignment_id": "%s", "task_id": "%s", "attempt_id": "%s", '
                '"windows": {"five_hour": {"utilization": NaN, "resets_at": "%s"}}}'
                % (held["assignment_id"], TASK, ATTEMPT, resets)
            ),
            headers={"Content-Type": "application/json"},
        )
    else:
        r = _report(client, held, {"five_hour": payload})

    assert r.status_code == 422, r.text
    assert _windows(store) == {}


def test_a_window_name_that_is_not_one_is_refused(client, store):
    held = _held(client)
    r = _report(client, held, {"Five Hour!": {"utilization": 0.2, "resets_at": _in(2)}})
    assert r.status_code == 422
    assert _windows(store) == {}


def test_a_reading_cannot_carry_anything_but_figures(client, store):
    held = _held(client)
    smuggled = "sk-ant-" + "oat01-" + "x" * 24
    r = _report(client, held, {"five_hour": {"utilization": 0.2, "resets_at": _in(2)}},
                token=smuggled)
    assert r.status_code == 422
    assert smuggled not in r.text, "the refusal must not echo the value it refused"
    assert smuggled not in str(store.get(ACCOUNT).to_firestore())
