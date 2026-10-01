"""Who holds an account, and who held it: the broker's half of #379 parts 2-3.

A hold used to carry an id, a tenant and a deadline -- enough to count agents
on an account, and nothing that said WHICH agents. These pin what the approved
design (2026-09-30) added on the broker side:

  * A HOLD IS STAMPED with the task and attempt the worker named and the
    instant it was taken, and a hold written before those fields existed still
    decodes -- the pool must not lose its count over a deploy.
  * EVERY HOLD HAS A RECORD in `account_holds/{assignment_id}`, and the record
    is written INSIDE the transaction that changes the hold: acquire opens it,
    release and prune close it. A log written after the counter commits is a
    log that can disagree with it, and the history tab would then show a span
    the counter never had (or miss one it did).
  * AN EXPIRED HOLD IS NEVER SERVED as a current holder, even before the sweep
    has pruned it.
  * THE TWO READ ROUTES ARE PLATFORM-ONLY. They name every tenant's tasks on
    the account; swarm-api is the one caller, and it filters per viewer. A
    worker's token asking them is refused like the broker's other platform
    routes.

Every assertion is on the fake store's documents -- what Firestore would hold
-- or on a route's response, never on which functions ran.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import (
    DEFAULT_HOLD_TTL,
    HOLD_LOG_COLLECTION,
    HOLD_LOG_RETENTION,
    holds_from_firestore,
)
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore, FakeTransaction

ENG = "eng"
RESEARCH = "research"
ACCOUNT = f"{ENG}:personal"


class _Identity:
    """A verified ID token, switchable between a worker's tenant and the platform."""

    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True


class _RecordingTransaction(FakeTransaction):
    """Remembers which documents each COMMIT wrote, as one set per commit."""

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
def broker(db) -> QuotaBroker:
    return QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))


@pytest.fixture()
def accounts(broker) -> AccountStore:
    store = AccountStore(broker.db)
    store.register(ENG, "personal", lend_to=(RESEARCH,))
    return store


@pytest.fixture()
def client(broker, accounts):
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _assign(client, **body) -> dict[str, Any]:
    response = client.post("/v1/accounts/assign", json={"provider": "anthropic", **body})
    assert response.status_code == 200, response.text
    return response.json()


def _release(client, assignment_id: str, **body):
    return client.post(
        f"/v1/accounts/{ACCOUNT}/release",
        json={"assignment_id": assignment_id, **body},
    )


def _log(db, assignment_id: str) -> dict[str, Any]:
    return db.docs[f"{HOLD_LOG_COLLECTION}/{assignment_id}"]


def _expire(db, index: int, *, minutes_ago: int = 1) -> datetime:
    doc = db.docs[f"accounts/{ACCOUNT}"]
    holds = [dict(h) for h in doc["holds"]]
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    holds[index]["expires_at"] = when
    db.document(f"accounts/{ACCOUNT}").update({"holds": holds})
    return when


# -- stamping ---------------------------------------------------------------


def test_a_hold_is_stamped_with_the_task_attempt_and_instant_it_was_taken(client, db):
    before = datetime.now(timezone.utc)
    _assign(client, task_id="task-a1", attempt_id="att-a1")

    (hold,) = holds_from_firestore(db.docs[f"accounts/{ACCOUNT}"]["holds"])
    assert hold.task_id == "task-a1"
    assert hold.attempt_id == "att-a1"
    assert hold.assigned_at is not None and hold.assigned_at >= before
    assert hold.expires_at == hold.assigned_at + DEFAULT_HOLD_TTL


def test_a_worker_that_names_no_task_still_gets_an_account(client, db):
    """The ids are OPTIONAL: a worker image older than the broker sends neither,
    and refusing it would put every agent back on the tenant secret."""
    body = _assign(client)

    assert body["account_id"] == ACCOUNT
    (hold,) = holds_from_firestore(db.docs[f"accounts/{ACCOUNT}"]["holds"])
    assert hold.task_id is None and hold.attempt_id is None
    assert hold.assigned_at is not None


def test_a_hold_written_before_the_stamp_existed_still_decodes():
    """The shape every live account document has on the day this deploys."""
    expires = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    (hold,) = holds_from_firestore(
        [{"assignment_id": "old-1", "tenant_id": ENG, "expires_at": expires}]
    )

    assert hold.assignment_id == "old-1"
    assert hold.tenant_id == ENG
    assert hold.expires_at == expires
    assert (hold.task_id, hold.attempt_id, hold.assigned_at) == (None, None, None)


def test_a_malformed_stamp_reads_as_absent_rather_than_dropping_the_hold():
    """The stamp is descriptive. A bad one must not cost the COUNT a hold."""
    expires = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    (hold,) = holds_from_firestore(
        [{"assignment_id": "x", "tenant_id": ENG, "expires_at": expires,
          "task_id": 5, "attempt_id": "", "assigned_at": "yesterday"}]
    )
    assert (hold.task_id, hold.attempt_id, hold.assigned_at) == (None, None, None)


def test_a_task_id_is_bounded_like_every_other_body_field(client):
    response = client.post(
        "/v1/accounts/assign", json={"provider": "anthropic", "task_id": "t" * 500}
    )
    assert response.status_code == 422


# -- the log is written in the same transaction ----------------------------


def test_acquiring_opens_the_record_in_the_same_transaction(client, db):
    assignment_id = _assign(client, task_id="task-a1", attempt_id="att-a1")["assignment_id"]

    record = _log(db, assignment_id)
    assert record["account_id"] == ACCOUNT
    assert record["tenant_id"] == ENG
    assert record["task_id"] == "task-a1"
    assert record["attempt_id"] == "att-a1"
    assert record["released_at"] is None and record["end"] is None
    # An open record still ages out: one whose hold is never closed (the
    # account removed under it) must not live forever.
    assert record["expires_at"] == record["hold_expires_at"] + HOLD_LOG_RETENTION

    together = [c for c in db.commits if f"accounts/{ACCOUNT}" in c]
    assert together and f"{HOLD_LOG_COLLECTION}/{assignment_id}" in together[-1]


def test_releasing_closes_the_record_in_the_same_transaction(client, db):
    assignment_id = _assign(client, task_id="task-a1")["assignment_id"]
    db.commits.clear()

    assert _release(client, assignment_id).json()["reason"] == ""

    record = _log(db, assignment_id)
    assert record["end"] == "released"
    assert record["released_at"] is not None
    assert record["expires_at"] == record["released_at"] + HOLD_LOG_RETENTION
    assert record["task_id"] == "task-a1", "closing keeps what opening recorded"
    (commit,) = db.commits
    assert commit == {f"accounts/{ACCOUNT}", f"{HOLD_LOG_COLLECTION}/{assignment_id}"}


def test_a_release_reporting_the_secret_unreadable_ends_as_unusable(client, db):
    assignment_id = _assign(client)["assignment_id"]

    _release(client, assignment_id, unusable="permission denied")

    assert _log(db, assignment_id)["end"] == "unusable"


def test_a_duplicate_release_writes_nothing_to_the_log(client, db):
    assignment_id = _assign(client)["assignment_id"]
    _release(client, assignment_id)
    closed = dict(_log(db, assignment_id))

    _release(client, assignment_id)

    assert _log(db, assignment_id) == closed, "a second release must not re-close it"


def test_pruning_closes_an_expired_record_in_the_same_transaction(client, db):
    gone = _assign(client, task_id="task-dead")["assignment_id"]
    live = _assign(client, task_id="task-live")["assignment_id"]
    lapsed_at = _expire(db, 0)
    db.commits.clear()

    client.identity.as_platform()
    assert client.post("/v1/quota/sweep").json()["holds"]["reclaimed"] == 1

    record = _log(db, gone)
    assert record["end"] == "expired"
    # When it stopped counting, not when the sweep happened to notice.
    assert record["released_at"] == lapsed_at
    assert _log(db, live)["end"] is None, "the live agent's record is untouched"
    pruning = [c for c in db.commits if f"accounts/{ACCOUNT}" in c]
    assert pruning and f"{HOLD_LOG_COLLECTION}/{gone}" in pruning[0]


def test_an_expired_hold_dropped_by_a_later_assignment_is_closed_too(client, db):
    """Acquire drops expired holds on its way through. A hold that leaves the
    counter there and stays open in the log is the drift this log exists to
    make impossible."""
    gone = _assign(client)["assignment_id"]
    _expire(db, 0)

    _assign(client)

    assert _log(db, gone)["end"] == "expired"


def test_an_old_hold_with_no_record_gets_one_when_it_ends(client, db):
    """A hold from before this change has no open record. Closing it writes a
    complete one from the hold itself, with the stamp it never had left empty
    rather than invented."""
    _assign(client)
    doc = db.docs[f"accounts/{ACCOUNT}"]
    old = {"assignment_id": "pre-379", "tenant_id": ENG,
           "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
    db.document(f"accounts/{ACCOUNT}").update({"holds": [*doc["holds"], old]})

    assert _release(client, "pre-379").json()["reason"] == ""

    record = _log(db, "pre-379")
    assert record["end"] == "released"
    assert record["assigned_at"] is None and record["task_id"] is None


# -- the read routes --------------------------------------------------------


def test_current_holds_name_the_tasks_and_never_the_assignment(client, db):
    _assign(client, task_id="task-a1", attempt_id="att-a1")
    client.identity.as_tenant(RESEARCH)
    _assign(client, task_id="task-r1")

    client.identity.as_platform()
    body = client.get(f"/v1/accounts/{ACCOUNT}/holds").json()

    assert body["owner_tenant"] == ENG
    assert body["lend_to"] == [RESEARCH]
    assert sorted((h["tenant_id"], h["task_id"]) for h in body["holds"]) == [
        (ENG, "task-a1"), (RESEARCH, "task-r1"),
    ]
    text = str(body)
    for assignment in db.docs[f"accounts/{ACCOUNT}"]["holds"]:
        assert assignment["assignment_id"] not in text, "the assignment id authorises release"
    assert "secret" not in text


def test_an_expired_hold_is_not_served_even_before_the_sweep(client, db):
    _assign(client, task_id="task-dead")
    _assign(client, task_id="task-live")
    _expire(db, 0)

    client.identity.as_platform()
    body = client.get(f"/v1/accounts/{ACCOUNT}/holds").json()

    assert [h["task_id"] for h in body["holds"]] == ["task-live"]


def test_history_serves_ended_and_open_spans_newest_first(client, db):
    first = _assign(client, task_id="task-1")["assignment_id"]
    _release(client, first)
    _assign(client, task_id="task-2")

    client.identity.as_platform()
    body = client.get(f"/v1/accounts/{ACCOUNT}/holds/history").json()

    assert [(s["task_id"], s["end"]) for s in body["spans"]] == [
        ("task-2", None), ("task-1", "released"),
    ]
    assert "assignment_id" not in str(body)
    assert body["next_cursor"] is None


def test_history_pages_without_losing_spans_that_share_an_instant(client, db):
    """A cursor that is "strictly before the last instant served" drops every
    other span taken in that same instant. Three share one here."""
    same = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        db.docs[f"{HOLD_LOG_COLLECTION}/a{i}"] = {
            "account_id": ACCOUNT, "tenant_id": ENG, "task_id": f"t{i}",
            "attempt_id": None, "assigned_at": same, "released_at": None,
            "end": None, "hold_expires_at": same + DEFAULT_HOLD_TTL,
            "expires_at": same + HOLD_LOG_RETENTION,
        }
    client.identity.as_platform()

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"limit": 2, "from": "2026-09-29T00:00:00+00:00",
                  "to": "2026-10-01T00:00:00+00:00"}
        if cursor:
            params["cursor"] = cursor
        body = client.get(f"/v1/accounts/{ACCOUNT}/holds/history", params=params).json()
        seen.extend(s["task_id"] for s in body["spans"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == ["t0", "t1", "t2"]


def _history(client, **params):
    return client.get(f"/v1/accounts/{ACCOUNT}/holds/history", params=params)


_WINDOW = {"from": "2026-09-29T00:00:00+00:00", "to": "2026-10-01T00:00:00+00:00"}
_AT = "2026-09-30T12:00:00+00:00"


@pytest.mark.parametrize(
    "cursor",
    [
        f"{_AT}|" + "9" * 50,        # a forged skip: 10**50 - 1
        f"{_AT}|2147483648",         # one past int32
        f"{_AT}|501",                # one past HISTORY_PAGE_MAX
        f"{_AT}|abc",                # not a number
        f"{_AT}|-1",
        f"{_AT}|\u0663",            # an Arabic-Indic digit: isdigit() but not ASCII
        f"{_AT}|",
        "no-separator",
        "not-a-time|1",
    ],
)
def test_a_malformed_or_oversized_cursor_is_a_422_never_a_500(client, db, cursor):
    """The cursor's skip count used to size the Firestore read
    (`limit + skip + 1`), so a forged one read an account's whole hold log, and
    past int32 it was an unhandled 500."""
    client.identity.as_platform()

    response = _history(client, cursor=cursor, **_WINDOW)

    assert response.status_code == 422, (cursor, response.status_code, response.text[:200])


def test_a_window_longer_than_the_retention_is_a_422(client, db):
    """No record outlives HOLD_LOG_RETENTION (90 days); a longer window can
    only be a scan of everything."""
    client.identity.as_platform()

    over = _history(client, **{"from": "2026-01-01T00:00:00+00:00", "to": "2026-09-30T00:00:00+00:00"})
    exact = _history(client, **{"from": "2026-07-02T00:00:00+00:00", "to": "2026-09-30T00:00:00+00:00"})

    assert over.status_code == 422, over.text[:200]
    assert exact.status_code == 200, exact.text[:200]


def test_a_from_older_than_the_retention_with_no_to_is_a_422(client, db):
    client.identity.as_platform()

    response = _history(client, **{"from": "2020-01-01T00:00:00+00:00"})

    assert response.status_code == 422, response.text[:200]


def test_history_returns_every_row_exactly_once_across_pages_with_shared_instants(client, db):
    """Seven spans, four sharing one instant, two sharing another, paged three
    at a time: every row once, none twice, in newest-first order."""
    base = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    instants = [base] * 4 + [base - timedelta(minutes=5)] * 2 + [base - timedelta(minutes=9)]
    for i, at in enumerate(instants):
        db.docs[f"{HOLD_LOG_COLLECTION}/h{i}"] = {
            "account_id": ACCOUNT, "tenant_id": ENG, "task_id": f"t{i}",
            "attempt_id": None, "assigned_at": at, "released_at": None,
            "end": None, "hold_expires_at": at + DEFAULT_HOLD_TTL,
            "expires_at": at + HOLD_LOG_RETENTION,
        }
    client.identity.as_platform()

    seen: list[str] = []
    cursor = None
    pages = 0
    while pages < 10:
        params = {"limit": 3, **_WINDOW}
        if cursor:
            params["cursor"] = cursor
        response = _history(client, **params)
        assert response.status_code == 200, response.text[:200]
        body = response.json()
        seen.extend(s["task_id"] for s in body["spans"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break

    assert sorted(seen) == [f"t{i}" for i in range(7)], seen
    assert len(seen) == len(set(seen)) == 7


def test_history_is_scoped_to_the_account_and_the_window(client, db):
    inside = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    for doc_id, account_id, at in [
        ("in", ACCOUNT, inside),
        ("other-account", f"{ENG}:spare", inside),
        ("too-old", ACCOUNT, inside - timedelta(days=3)),
    ]:
        db.docs[f"{HOLD_LOG_COLLECTION}/{doc_id}"] = {
            "account_id": account_id, "tenant_id": ENG, "task_id": doc_id,
            "attempt_id": None, "assigned_at": at, "released_at": None, "end": None,
            "hold_expires_at": at + DEFAULT_HOLD_TTL, "expires_at": at + HOLD_LOG_RETENTION,
        }
    client.identity.as_platform()

    body = client.get(
        f"/v1/accounts/{ACCOUNT}/holds/history",
        params={"from": "2026-09-30T00:00:00+00:00", "to": "2026-10-01T00:00:00+00:00"},
    ).json()

    assert [s["task_id"] for s in body["spans"]] == ["in"]


@pytest.mark.parametrize("path", ["/holds", "/holds/history"])
def test_the_read_routes_refuse_a_worker(client, path):
    """A worker's token is a tenant's token. These routes name other tenants'
    tasks, so only the platform -- swarm-api, which filters per viewer -- may
    read them."""
    client.identity.as_tenant(ENG)

    response = client.get(f"/v1/accounts/{ACCOUNT}{path}")

    assert response.status_code == 403


def test_an_unknown_account_is_refused_by_name(client):
    client.identity.as_platform()

    response = client.get("/v1/accounts/eng:nope/holds")

    assert response.status_code == 422


#: Instants the interpreter parses and then cannot convert to UTC, or cannot
#: subtract a span from: each raised OverflowError, an unhandled 500.
_OVERFLOWING = [
    pytest.param({"from": "0001-01-01T00:00:00+01:00", "to": _WINDOW["to"]}, id="from-underflows-in-utc"),
    pytest.param({"to": "9999-12-31T23:59:59-01:00"}, id="to-overflows-in-utc"),
    pytest.param({"to": "0001-01-05T00:00:00Z"}, id="default-span-underflows"),
    pytest.param({"cursor": "0001-01-01T00:00:00+01:00|0", **_WINDOW}, id="cursor-underflows-in-utc"),
    pytest.param({"cursor": "2020-01-01T00:00:00+00:00|0", **_WINDOW}, id="cursor-older-than-retention"),
    pytest.param({"from": "2020-01-01T00:00:00+00:00", "to": "2020-01-02T00:00:00+00:00"}, id="window-older-than-retention"),
    pytest.param({"to": "2999-01-01T00:00:00+00:00"}, id="to-in-the-far-future"),
]


@pytest.mark.parametrize("params", _OVERFLOWING)
def test_an_instant_outside_the_retained_range_is_a_422_never_a_500(client, db, params):
    """Every instant (from, to, cursor) must lie within
    [now - HOLD_LOG_RETENTION - 1 day, now + 1 day]."""
    client.identity.as_platform()

    response = _history(client, **params)

    assert response.status_code == 422, (params, response.status_code, response.text[:200])
