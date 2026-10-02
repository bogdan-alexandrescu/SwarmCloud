"""Moving a running attempt from one account to another: the broker's half of S13/S14.

Until now an agent never changed account mid-run, and DRAINING ("running
agents are being moved off") moved nobody: `set_account_state` stored the
state and every agent stayed. These pin what the broker now guarantees:

  * A SWAP IS ONE TRANSACTION. The left hold goes and the new one arrives in
    the same commit, so an attempt never holds two accounts and never holds
    none while a swap succeeds -- and never moves to the account it is leaving.
  * NO OTHER ACCOUNT, NO CHANGE. The swap says so with the assign route's
    reason and the current hold is untouched; the worker parks as it does today.
  * ONLY THE HOLDER MAY SWAP. A fenced attempt's holds are released by the
    reconciler (#380), so it holds nothing and its swap is a 403.
  * DRAIN MARKS EVERY LIVE HOLD in the same write as the state, and the
    holder's cheap status read says `move: drain`.
  * THE SWAP IS IN THE 90-DAY HISTORY from both sides: the left record closes
    `swapped` naming where it went, the new one opens naming where it came
    from, both with the reason and the attempt.

Assertions are on the fake store's documents or a route's response.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import HOLD_LOG_COLLECTION, holds_from_firestore
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore, FakeTransaction

ENG = "eng"
FIRST = f"{ENG}:first"
SECOND = f"{ENG}:second"
TASK = "task_1"
ATTEMPT = "att_1"


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


class _RecordingTransaction(FakeTransaction):
    def _commit(self) -> list[Any]:
        self._db.commits.append({ref.path for _op, ref, _data in self._buffer})
        return super()._commit()


class _Firestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.commits: list[set[str]] = []

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _RecordingTransaction(self)


@pytest.fixture()
def db() -> _Firestore:
    return _Firestore()


@pytest.fixture()
def client(db):
    broker = QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))
    store = AccountStore(broker.db)
    store.register(ENG, "first")
    store.register(ENG, "second")
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=store)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _holds(db, account_id: str):
    return holds_from_firestore(db.docs[f"accounts/{account_id}"].get("holds"))


def _assign(client, *, attempt: str = ATTEMPT) -> dict[str, Any]:
    r = client.post(
        "/v1/accounts/assign",
        json={"provider": "anthropic", "task_id": TASK, "attempt_id": attempt},
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == FIRST, "the fixture's accounts sort by label"
    return r.json()


def _swap(client, held: dict[str, Any], *, reason: str = "exhausted",
          attempt: str = ATTEMPT, **extra):
    return client.post(
        "/v1/accounts/swap",
        json={
            "account_id": held["account_id"],
            "assignment_id": held["assignment_id"],
            "task_id": TASK,
            "attempt_id": attempt,
            "reason": reason,
            **extra,
        },
    )


def _status(client, held: dict[str, Any], *, attempt: str = ATTEMPT):
    return client.post(
        f"/v1/accounts/{held['account_id']}/hold-status",
        json={"assignment_id": held["assignment_id"], "task_id": TASK, "attempt_id": attempt},
    )


# -- the swap is atomic ----------------------------------------------------


def test_a_swap_moves_the_hold_in_one_commit_and_never_leaves_two(client, db):
    held = _assign(client)
    db.commits.clear()

    r = _swap(client, held)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["swapped"] is True
    assert body["account_id"] == SECOND, "never the account being left"
    assert body["assignment_id"] and body["assignment_id"] != held["assignment_id"]
    assert body["secret"] == "swarm-account-eng--second"
    assert _holds(db, FIRST) == ()
    assert [h.attempt_id for h in _holds(db, SECOND)] == [ATTEMPT]
    assert db.docs[f"accounts/{FIRST}"]["assigned"] == 0
    assert db.docs[f"accounts/{SECOND}"]["assigned"] == 1

    # ONE commit wrote both accounts and both records: there is no instant at
    # which the attempt held both, or neither.
    assert db.commits == [{
        f"accounts/{FIRST}",
        f"accounts/{SECOND}",
        f"{HOLD_LOG_COLLECTION}/{held['assignment_id']}",
        f"{HOLD_LOG_COLLECTION}/{body['assignment_id']}",
    }]


def test_no_other_account_keeps_the_hold_and_says_why(client, db):
    held = _assign(client)
    db.docs[f"accounts/{SECOND}"]["state"] = "PAUSED"
    before = db.docs[f"accounts/{FIRST}"]["holds"]

    r = _swap(client, held)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["swapped"] is False
    assert body["account_id"] is None and body["assignment_id"] is None
    assert body["reason"] == "pool_paused"
    assert db.docs[f"accounts/{FIRST}"]["holds"] == before, "the current hold is untouched"
    assert _holds(db, SECOND) == ()


def test_an_excluded_account_is_not_swapped_to(client, db):
    held = _assign(client)

    body = _swap(client, held, exclude=[SECOND]).json()

    assert body["swapped"] is False
    assert len(_holds(db, FIRST)) == 1


def test_a_fenced_attempts_swap_is_refused(client, db):
    """The reconciler releases a fenced attempt's holds (#380). The worker
    that was fenced then holds nothing, and its swap must not hand it a
    second account."""
    held = _assign(client)
    client.identity.platform = True
    released = client.post(
        "/v1/holds/release-attempt", json={"task_id": TASK, "attempt_id": ATTEMPT}
    )
    assert released.json()["released"] == 1
    client.identity.platform = False

    r = _swap(client, held)

    assert r.status_code == 403, r.text
    assert _holds(db, SECOND) == (), "a refused swap takes nothing"


def test_a_swap_naming_another_attempt_is_refused(client, db):
    held = _assign(client)

    assert _swap(client, held, attempt="att_stale").status_code == 403
    assert len(_holds(db, FIRST)) == 1


def test_another_tenant_cannot_swap_a_hold_it_was_never_issued(client, db):
    held = _assign(client)
    client.identity.tenant = "research"

    assert _swap(client, held).status_code == 403
    assert len(_holds(db, FIRST)) == 1


def test_an_unknown_swap_reason_is_refused(client):
    held = _assign(client)
    assert _swap(client, held, reason="bored").status_code == 422


def test_a_swap_off_an_unusable_account_keeps_the_next_ask_away_from_it(client, db):
    held = _assign(client)

    assert _swap(client, held, reason="unusable").json()["swapped"] is True

    assert ENG in db.docs[f"accounts/{FIRST}"]["unreadable_by"]


# -- drain moves the holders -------------------------------------------------


def test_draining_marks_every_live_hold_in_the_same_write(client, db):
    one = _assign(client)
    db.docs[f"accounts/{SECOND}"]["state"] = "PAUSED"   # both agents land on FIRST
    two = client.post(
        "/v1/accounts/assign",
        json={"provider": "anthropic", "task_id": "task_2", "attempt_id": "att_2"},
    ).json()
    assert two["account_id"] == FIRST
    db.docs[f"accounts/{SECOND}"]["state"] = "AVAILABLE"
    db.commits.clear()

    client.identity.platform = True
    r = client.put(f"/v1/accounts/{FIRST}/state", json={"state": "DRAINING"})
    client.identity.platform = False

    assert r.status_code == 200, r.text
    assert r.json()["moving"] == 2
    assert db.commits == [{f"accounts/{FIRST}"}], "the state and the marks are one write"
    assert {h.move for h in _holds(db, FIRST)} == {"drain"}
    assert _status(client, one).json()["move"] == "drain"


def test_a_drained_account_reaches_zero_holders_by_swaps(client, db):
    held = _assign(client)
    client.identity.platform = True
    client.put(f"/v1/accounts/{FIRST}/state", json={"state": "DRAINING"})
    client.identity.platform = False

    assert _status(client, held).json()["move"] == "drain"
    moved = _swap(client, held, reason="drain").json()

    assert moved["swapped"] is True and moved["account_id"] == SECOND
    assert _holds(db, FIRST) == ()
    assert [h.move for h in _holds(db, SECOND)] == [None], "the new hold is not marked"


def test_leaving_draining_clears_the_marks(client, db):
    held = _assign(client)
    client.identity.platform = True
    client.put(f"/v1/accounts/{FIRST}/state", json={"state": "DRAINING"})
    client.put(f"/v1/accounts/{FIRST}/state", json={"state": "AVAILABLE"})
    client.identity.platform = False

    assert _status(client, held).json()["move"] is None


def test_a_hold_status_is_answered_only_for_the_holder(client):
    held = _assign(client)

    assert _status(client, held).json() == {
        "account_id": FIRST, "held": True, "move": None, "state": "AVAILABLE",
    }
    assert _status(client, held, attempt="att_other").status_code == 403
    client.identity.tenant = "research"
    assert _status(client, held).status_code == 403


# -- the swap is in the hold history ---------------------------------------


def _log(db, assignment_id: str) -> dict[str, Any]:
    return db.docs[f"{HOLD_LOG_COLLECTION}/{assignment_id}"]


def test_a_swap_writes_one_closed_and_one_opened_record(client, db):
    held = _assign(client)
    moved = _swap(client, held, reason="drain").json()

    left = _log(db, held["assignment_id"])
    taken = _log(db, moved["assignment_id"])

    assert left["end"] == "swapped"
    assert left["released_at"] is not None
    assert (left["swapped_to"], left["swapped_to_reason"]) == (SECOND, "drain")
    assert taken["end"] is None and taken["released_at"] is None
    assert (taken["swapped_from"], taken["swapped_from_reason"]) == (FIRST, "drain")
    assert (left["task_id"], left["attempt_id"]) == (TASK, ATTEMPT)
    assert (taken["task_id"], taken["attempt_id"]) == (TASK, ATTEMPT)
    assert left["released_at"] == taken["assigned_at"], "the swap has one instant"


def test_both_accounts_history_reads_serve_the_swap(client, db):
    held = _assign(client)
    moved = _swap(client, held).json()
    client.identity.platform = True

    first = client.get(f"/v1/accounts/{FIRST}/holds/history").json()["spans"]
    second = client.get(f"/v1/accounts/{SECOND}/holds/history").json()["spans"]
    holders = client.get(f"/v1/accounts/{SECOND}/holds").json()["holds"]

    assert [(s["end"], s["swapped_to"], s["swapped_to_reason"]) for s in first] == [
        ("swapped", SECOND, "exhausted")
    ]
    assert [(s["end"], s["swapped_from"], s["swapped_from_reason"]) for s in second] == [
        (None, FIRST, "exhausted")
    ]
    assert [(h["swapped_from"], h["swapped_from_reason"]) for h in holders] == [
        (FIRST, "exhausted")
    ]
    assert moved["assignment_id"] not in str(first + second + holders), (
        "no read serves the assignment id: it authorises a release"
    )


def test_an_ordinary_release_still_closes_released_with_no_swap_fields(client, db):
    held = _assign(client)
    client.post(f"/v1/accounts/{FIRST}/release", json={"assignment_id": held["assignment_id"]})

    record = _log(db, held["assignment_id"])
    assert record["end"] == "released"
    assert record["swapped_to"] is None and record["swapped_from"] is None


def test_the_swap_record_expires_with_the_hold_log(client, db):
    held = _assign(client)
    _swap(client, held)
    record = _log(db, held["assignment_id"])
    assert isinstance(record["released_at"], datetime)
    assert record["expires_at"] - record["released_at"] == timedelta(days=90)
