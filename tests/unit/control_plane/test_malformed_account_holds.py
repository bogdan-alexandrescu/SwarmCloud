"""A malformed account document must still give its holds back (#243).

#180 made one bad document cost one account rather than the pool: the listing
skips it, and every single-account read raises one typed
`MalformedAccountError`. Three paths that only ever needed the RAW hold fields
were caught by that same error and froze the document's holds in place:

  1. `release_account` decoded the whole `Account` before `release_hold`, so a
     worker with a live hold on a document that then went bad got a 500 and
     could not give the hold back;
  2. `_prune_all_holds` walked `store.list()`, which leaves malformed documents
     out by design, so the TTL sweep never reclaimed their holds either --
     between the two, nothing could ever remove them;
  3. `assign_account` committed the hold and THEN decoded the document with no
     guard, so a document that went bad in between answered 500 over a hold
     the caller never learned the id of.

Every assertion here is on the fake store's documents -- the holds and the
`assigned` count Firestore would hold -- and not on which functions were
called, because the property is "no hold is stranded", not "this call ran".

The two bad shapes are the ones #180 pinned: `assigned: None` (TypeError in
the decoder) and `windows` as a string (AttributeError).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import holds_from_firestore
from quota_broker.accountstore import AccountStore, MalformedAccountError
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore, FakeTransaction
from .test_malformed_account_document import ENG, SENTINEL, _rendered
from .test_malformed_account_document import _Identity as _DocumentIdentity

HELD = f"{ENG}:held"

#: The two shapes #180 pinned, applied to a document that is otherwise
#: perfectly good and already carries a live hold. `reason` is planted with
#: the sentinel as well, so any log line or body that quotes a field leaks it.
CORRUPTIONS = {
    "assigned-null": {"assigned": None, "reason": SENTINEL},
    "windows-string": {"windows": SENTINEL, "reason": SENTINEL},
}


class _Identity(_DocumentIdentity):
    """The #180 file's identity, able to act as the platform again after a tenant."""

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True


class _CorruptingTransaction(FakeTransaction):
    """Commits normally, then -- once, when armed -- corrupts the account.

    This is the window defect 3 lives in: after `acquire_hold`'s transaction
    has committed the hold and before anything else reads the document. A
    real transaction, driven by the real `firestore.transactional`; only the
    moment of the corruption is staged.
    """

    def _commit(self) -> list[Any]:
        result = super()._commit()
        armed = self._db.corrupt_after_next_commit
        if armed is not None:
            self._db.corrupt_after_next_commit = None
            self._db.document(f"accounts/{HELD}").update(armed)
        return result


class _Firestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.corrupt_after_next_commit: dict[str, Any] | None = None

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _CorruptingTransaction(self)


@pytest.fixture()
def db() -> _Firestore:
    return _Firestore()


@pytest.fixture()
def broker(db) -> QuotaBroker:
    return QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))


@pytest.fixture()
def accounts(broker) -> AccountStore:
    store = AccountStore(broker.db)
    store.register(ENG, "held")
    return store


@pytest.fixture()
def client(broker, accounts):
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _doc(db) -> dict[str, Any]:
    return db.docs[f"accounts/{HELD}"]


def _hold_ids(db) -> list[str]:
    return sorted(h.assignment_id for h in holds_from_firestore(_doc(db).get("holds")))


def _assign(client) -> str:
    client.identity.as_tenant(ENG)
    r = client.post("/v1/accounts/assign", json={"provider": "anthropic"})
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == HELD
    return r.json()["assignment_id"]


def _corrupt(db, shape: str) -> None:
    db.document(f"accounts/{HELD}").update(CORRUPTIONS[shape])


def _assert_malformed(accounts) -> None:
    """The control: the document really is unreadable, so the test below it
    is about a malformed document and not a healthy one."""
    with pytest.raises(MalformedAccountError):
        accounts.get(HELD)


# --------------------------------------------------------------------------
# 1. Release
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(CORRUPTIONS))
def test_a_live_hold_on_a_document_gone_malformed_can_be_released(
    client, accounts, db, shape
):
    assignment_id = _assign(client)
    assert _hold_ids(db) == [assignment_id]

    _corrupt(db, shape)
    _assert_malformed(accounts)

    r = client.post(f"/v1/accounts/{HELD}/release", json={"assignment_id": assignment_id})

    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "", "the hold was found and given back"
    assert r.json()["assigned"] == 0
    assert SENTINEL not in r.text
    # THE PROPERTY: the hold is gone from the document, and the count Firestore
    # holds is the projection of what is left.
    assert _hold_ids(db) == []
    assert _doc(db)["assigned"] == 0


@pytest.mark.parametrize("shape", sorted(CORRUPTIONS))
def test_a_release_on_a_malformed_document_still_releases_only_its_own_hold(
    client, accounts, db, shape
):
    """Working from the raw fields must not weaken the two checks: another
    tenant's worker may not release, and a hold is released by its own id."""
    first = _assign(client)
    second = _assign(client)
    _corrupt(db, shape)

    client.identity.as_tenant("research")
    refused = client.post(f"/v1/accounts/{HELD}/release", json={"assignment_id": first})
    assert refused.status_code == 403, refused.text
    assert _hold_ids(db) == sorted([first, second])

    client.identity.as_tenant(ENG)
    forged = client.post(f"/v1/accounts/{HELD}/release", json={"assignment_id": "forged"})
    assert forged.status_code == 200, forged.text
    assert forged.json()["reason"] == "not_held"
    assert _hold_ids(db) == sorted([first, second])

    released = client.post(f"/v1/accounts/{HELD}/release", json={"assignment_id": first})
    assert released.status_code == 200, released.text
    assert _hold_ids(db) == [second]
    assert _doc(db)["assigned"] == 1


# --------------------------------------------------------------------------
# 2. The TTL sweep
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(CORRUPTIONS))
def test_an_expired_hold_on_a_malformed_document_is_pruned_by_the_sweep(
    client, accounts, db, shape, caplog
):
    _assign(client)
    live = _assign(client)
    # The first worker was killed outright: its deadline has passed.
    holds = [dict(h) for h in _doc(db)["holds"]]
    holds[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.document(f"accounts/{HELD}").update({"holds": holds})
    _corrupt(db, shape)
    _assert_malformed(accounts)

    client.identity.as_platform()
    with caplog.at_level(logging.INFO):
        r = client.post("/v1/quota/sweep")

    assert r.status_code == 200, r.text
    assert r.json()["holds"]["reclaimed"] == 1, r.json()["holds"]
    assert _hold_ids(db) == [live], "the expired hold went and the live one stayed"
    assert _doc(db)["assigned"] == 1
    for record in caplog.records:
        assert SENTINEL not in _rendered(record), "a field value reached the log"


def test_the_sweep_names_a_malformed_document_by_id_and_class_only(
    client, accounts, db, caplog
):
    _assign(client)
    _corrupt(db, "windows-string")

    client.identity.as_platform()
    with caplog.at_level(logging.WARNING):
        client.post("/v1/quota/sweep")

    named = [r for r in caplog.records if getattr(r, "account_id", None) == HELD]
    assert named, "the sweep met a malformed document and said nothing about it"
    assert any(getattr(r, "error", None) == "AttributeError" for r in named)
    for record in caplog.records:
        assert SENTINEL not in _rendered(record)


# --------------------------------------------------------------------------
# 3. Assignment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(CORRUPTIONS))
def test_assignment_never_strands_a_hold_when_the_document_goes_bad_after_it(
    client, accounts, db, shape
):
    """The document goes malformed between the hold's commit and anything
    after it. Either the caller is told its assignment -- and can release it --
    or no hold was left behind. A 500 over a committed hold is neither."""
    db.corrupt_after_next_commit = CORRUPTIONS[shape]
    client.identity.as_tenant(ENG)

    r = client.post("/v1/accounts/assign", json={"provider": "anthropic"})

    assert db.corrupt_after_next_commit is None, "the corruption was not staged"
    _assert_malformed(accounts)
    assert r.status_code == 200, r.text
    assert SENTINEL not in r.text
    assignment_id = r.json()["assignment_id"]
    # THE PROPERTY: every hold on the document is one the caller holds the id
    # of. Nothing on it is unreleasable.
    assert _hold_ids(db) == ([assignment_id] if assignment_id else [])

    if assignment_id:
        released = client.post(
            f"/v1/accounts/{HELD}/release", json={"assignment_id": assignment_id}
        )
        assert released.status_code == 200, released.text
        assert _hold_ids(db) == []


# --------------------------------------------------------------------------
# 4. Epic #227: a pool that is ALL malformed is loud, the sweep reads once,
#    and a release that matched nothing writes nothing
# --------------------------------------------------------------------------


def _decoder_bug(_raw: Any) -> tuple[Any, ...]:
    """What a decoder bug looks like: the same TypeError for every document."""
    raise TypeError("decoder bug")


def test_the_malformed_set_is_public_and_is_the_one_the_broker_catches():
    """The four classes are the owner's decision on #180 and stay four; the
    broker's hold paths import the PUBLIC name, so the two cannot drift."""
    from quota_broker import accountstore
    from quota_broker import main as broker_main

    assert accountstore.MALFORMED_ERRORS == (KeyError, ValueError, TypeError, AttributeError)
    assert broker_main.MALFORMED_ERRORS is accountstore.MALFORMED_ERRORS


def test_a_listing_where_every_document_is_unreadable_logs_an_error(accounts, db, caplog):
    accounts.register(ENG, "second")
    _corrupt(db, "assigned-null")
    db.document(f"accounts/{ENG}:second").update(CORRUPTIONS["windows-string"])

    with caplog.at_level(logging.WARNING):
        listing = accounts.list_reporting()

    assert listing.accounts == []
    assert listing.unreadable == sorted([HELD, f"{ENG}:second"])
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a whole pool went unreadable and nothing said so above WARNING"
    assert getattr(errors[0], "unreadable", None) == sorted([HELD, f"{ENG}:second"])
    for record in caplog.records:
        assert SENTINEL not in _rendered(record)


def test_one_unreadable_document_among_readable_ones_is_only_a_warning(
    accounts, db, caplog
):
    """The control: the ERROR is for the whole pool, not for one bad document."""
    accounts.register(ENG, "second")
    _corrupt(db, "assigned-null")

    with caplog.at_level(logging.WARNING):
        listing = accounts.list_reporting()

    assert [a.account_id for a in listing.accounts] == [f"{ENG}:second"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_sweep_that_can_read_no_document_s_holds_reports_an_error(
    client, accounts, db, caplog, monkeypatch
):
    import quota_broker.main as broker_main

    accounts.register(ENG, "second")
    _assign(client)
    monkeypatch.setattr(broker_main, "holds_from_firestore", _decoder_bug)

    client.identity.as_platform()
    with caplog.at_level(logging.WARNING):
        r = client.post("/v1/quota/sweep")

    assert r.status_code == 200, r.text
    holds = r.json()["holds"]
    assert "error" in holds, holds
    assert holds["unreadable"] == sorted([HELD, f"{ENG}:second"])
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "every prune raised and the sweep said nothing above WARNING"
    )


def test_a_sweep_where_some_documents_prune_reports_no_error(
    client, accounts, db, monkeypatch
):
    """The control: one document failing is that document, not the sweep."""
    import quota_broker.main as broker_main

    accounts.register(ENG, "second")
    real = broker_main.holds_from_firestore
    calls = {"n": 0}

    def _first_fails(raw: Any) -> tuple[Any, ...]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("one bad document")
        return real(raw)

    monkeypatch.setattr(broker_main, "holds_from_firestore", _first_fails)
    client.identity.as_platform()
    r = client.post("/v1/quota/sweep")

    assert r.status_code == 200, r.text
    assert calls["n"] >= 2, "the sweep did not visit both documents"
    assert "error" not in r.json()["holds"], r.json()["holds"]


def test_the_hold_sweep_streams_the_accounts_collection_once(accounts, db, monkeypatch):
    from quota_broker.main import _prune_all_holds

    from .fakes import FakeCollectionRef

    accounts.register(ENG, "second")
    _corrupt(db, "windows-string")
    real_stream = FakeCollectionRef.stream
    streamed: list[str] = []

    def _counting(self, *args: Any, **kwargs: Any):
        streamed.append(self._path)
        return real_stream(self, *args, **kwargs)

    monkeypatch.setattr(FakeCollectionRef, "stream", _counting)
    summary = _prune_all_holds(db, accounts, datetime.now(timezone.utc))

    assert summary["registered"] == 1
    assert streamed.count("accounts") == 1, streamed


def test_a_release_that_matched_nothing_does_not_write_the_account(client, accounts, db):
    """A document malformed only in `assigned`: an unmatched release must not
    re-project `assigned` from `holds` and so repair it on a caller's behalf."""
    _corrupt(db, "assigned-null")
    _assert_malformed(accounts)
    client.identity.as_tenant(ENG)

    r = client.post(f"/v1/accounts/{HELD}/release", json={"assignment_id": "not-a-hold"})

    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "not_held"
    assert _doc(db)["assigned"] is None, "the unmatched release rewrote the document"


def test_a_release_that_matched_nothing_still_drops_an_expired_hold(client, accounts, db):
    """The control: the write is skipped only when there is nothing to write."""
    _assign(client)
    holds = [dict(h) for h in _doc(db)["holds"]]
    holds[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.document(f"accounts/{HELD}").update({"holds": holds})

    r = client.post(f"/v1/accounts/{HELD}/release", json={"assignment_id": "not-a-hold"})

    assert r.status_code == 200, r.text
    assert _hold_ids(db) == []
    assert _doc(db)["assigned"] == 0
