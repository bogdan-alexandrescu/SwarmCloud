"""One malformed account document must cost one account, never the pool.

THE DEFECT (#180): `AccountStore.list_reporting` skipped a document that
`Account.from_firestore` could not decode only when the decode raised
`KeyError` or `ValueError`. The decoder raises two more on shapes Firestore
will happily store:

  * `TypeError` -- `assigned: null` reaches `int(None)`;
  * `AttributeError` -- a `windows` that is not a map has no `.items()`.

Either one escaped the listing. The scheduler reads that listing at the top of
every drain (`scheduler.credentials.AccountPool.serves`), so one bad document
made every drain raise before admission, for every tenant, and the broker's
own `/v1/accounts` answered with a bare 500.

The single-account reads (`get`, the re-registration inside `register`,
`mark_reauth_required`, `clear_reauth_required`, `record_reading`) had no guard
at all, so a route that touched the bad document answered with a traceback
rather than a reason.

What is pinned here:

  1. the listing returns the readable account and names BOTH bad documents in
     `unreadable`, exactly as it already names a KeyError skip;
  2. the log line names the document id and the exception class, and carries
     NONE of the document's field values -- an account document holds
     credential metadata (`secret`, `reason`, lending), and a log line is read
     by far more people than the document;
  3. every single-account path turns the same four exceptions into one typed
     `MalformedAccountError`, and the broker answers it with a named error
     that says which document, not with a traceback;
  4. the readable account beside the bad ones keeps working, through every
     one of those routes.

Nothing here needs a credential, a network or an emulator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import AccountError, WindowReading
from quota_broker.accountstore import AccountStore, MalformedAccountError
from quota_broker.main import create_app

ENG = "eng"
GOOD = f"{ENG}:good"
NULL_ASSIGNED = f"{ENG}:null-assigned"
STRING_WINDOWS = f"{ENG}:string-windows"
BAD = (NULL_ASSIGNED, STRING_WINDOWS)

#: Planted in every field of the bad documents that could carry a value. If
#: it appears in a log record or a response body, a field value leaked.
SENTINEL = "sentinel-credential-metadata-7f3a"


class _Identity:
    """A verified ID token, as `WorkerIdentity.resolve` returns one."""

    def __init__(self) -> None:
        self.tenant: str | None = None
        self.platform = True

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False


def _plant_bad_documents(db) -> None:
    """Two documents Firestore stores without complaint and the decoder cannot read.

    Written straight onto the collection, because nothing in the broker writes
    either shape -- they arrive by a hand edit in the console, a script, or an
    older writer, which is exactly why the reader must survive them.
    """
    base = {
        "owner_tenant": ENG,
        "provider": "anthropic",
        "state": "AVAILABLE",
        "secret": f"swarm-tenant-{ENG}-{SENTINEL}",
        "reason": SENTINEL,
        "lend_to": [],
    }
    db.document(f"accounts/{NULL_ASSIGNED}").set(
        {**base, "account_id": NULL_ASSIGNED, "label": "null-assigned", "assigned": None}
    )
    db.document(f"accounts/{STRING_WINDOWS}").set(
        {
            **base,
            "account_id": STRING_WINDOWS,
            "label": "string-windows",
            "assigned": 0,
            "windows": SENTINEL,
        }
    )


@pytest.fixture()
def accounts(broker) -> AccountStore:
    store = AccountStore(broker.db)
    store.register(ENG, "good")
    _plant_bad_documents(broker.db)
    return store


@pytest.fixture()
def client(broker, accounts):
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _rendered(record: logging.LogRecord) -> str:
    """Everything a log handler could emit for this record, message and extras."""
    return " ".join([record.getMessage(), *(repr(v) for v in vars(record).values())])


# --------------------------------------------------------------------------
# 0. The two shapes really do raise what the issue says they raise
# --------------------------------------------------------------------------


def test_the_planted_documents_raise_the_two_exceptions_the_old_skip_missed(broker):
    """The control. If the decoder ever stops raising these, the tests below
    would pass for the wrong reason, so the premise is asserted, not assumed."""
    from quota_broker.accounts import Account

    _plant_bad_documents(broker.db)
    null_assigned = broker.db.document(f"accounts/{NULL_ASSIGNED}").get().to_dict()
    string_windows = broker.db.document(f"accounts/{STRING_WINDOWS}").get().to_dict()

    with pytest.raises(TypeError):
        Account.from_firestore(null_assigned)
    with pytest.raises(AttributeError):
        Account.from_firestore(string_windows)


# --------------------------------------------------------------------------
# 1. The listing
# --------------------------------------------------------------------------


def test_the_listing_keeps_the_readable_account_and_names_both_bad_ones(accounts):
    listing = accounts.list_reporting()

    assert [a.account_id for a in listing.accounts] == [GOOD]
    assert listing.unreadable == sorted(BAD)
    assert [a.account_id for a in accounts.list()] == [GOOD]


def test_a_skip_is_logged_loudly_by_document_id_and_exception_class_only(
    accounts, caplog
):
    with caplog.at_level(logging.WARNING, logger="quota_broker.accountstore"):
        accounts.list_reporting()

    records = [r for r in caplog.records if r.name == "quota_broker.accountstore"]
    assert len(records) == len(BAD), [r.getMessage() for r in records]
    assert all(r.levelno >= logging.WARNING for r in records)

    by_id = {getattr(r, "account_id", None): r for r in records}
    assert set(by_id) == set(BAD)
    assert by_id[NULL_ASSIGNED].error == "TypeError"
    assert by_id[STRING_WINDOWS].error == "AttributeError"
    for record in records:
        assert SENTINEL not in _rendered(record), "a field value reached the log"


def test_the_old_keyerror_skip_is_reported_the_same_way(broker):
    """The two exceptions that were already skipped keep their behaviour, and
    lose the `str(exc)` that could quote a value (a ValueError from an
    unrecognised `state` names the value it refused)."""
    store = AccountStore(broker.db)
    store.register(ENG, "good")
    broker.db.document(f"accounts/{ENG}:bad-state").set(
        {
            "account_id": f"{ENG}:bad-state",
            "owner_tenant": ENG,
            "label": "bad-state",
            "state": SENTINEL,
        }
    )

    listing = store.list_reporting()

    assert [a.account_id for a in listing.accounts] == [GOOD]
    assert listing.unreadable == [f"{ENG}:bad-state"]


def test_a_bad_state_value_is_not_quoted_into_the_log(broker, caplog):
    store = AccountStore(broker.db)
    broker.db.document(f"accounts/{ENG}:bad-state").set(
        {
            "account_id": f"{ENG}:bad-state",
            "owner_tenant": ENG,
            "label": "bad-state",
            "state": SENTINEL,
        }
    )

    with caplog.at_level(logging.WARNING, logger="quota_broker.accountstore"):
        store.list_reporting()

    (record,) = [r for r in caplog.records if r.name == "quota_broker.accountstore"]
    assert record.error == "ValueError"
    assert SENTINEL not in _rendered(record)


def test_the_route_serves_the_readable_account_and_counts_both_bad_ones(client):
    r = client.get("/v1/accounts")

    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["account_id"] for a in body["accounts"]] == [GOOD]
    assert body["unreadable_document_count"] == 2
    assert body["unreadable_documents"] == sorted(BAD)


def test_the_scheduler_still_sees_the_readable_account(broker, accounts):
    """The path the issue is about: admission reads the pool through
    `AccountStore.list` at the top of every drain, for every tenant."""
    from scheduler.credentials import AccountPool

    pool = AccountPool.for_deployment(
        SimpleNamespace(quota_broker_url="https://broker.invalid"), broker.db
    )

    assert pool.serves(ENG, "anthropic") is True


# --------------------------------------------------------------------------
# 2. The single-account store reads
# --------------------------------------------------------------------------


_READING = {
    "five_hour": WindowReading(0.1, datetime.now(timezone.utc) + timedelta(hours=2)),
}

SINGLE_ACCOUNT_READS = {
    "get": lambda s, i: s.get(i),
    "mark_reauth_required": lambda s, i: s.mark_reauth_required(i, "refresh failed"),
    "clear_reauth_required": lambda s, i: s.clear_reauth_required(i),
    "record_reading": lambda s, i: s.record_reading(
        i, _READING, datetime.now(timezone.utc)
    ),
    "register": lambda s, i: s.register(ENG, i.split(":", 1)[1]),
}


@pytest.mark.parametrize("bad_id", BAD)
@pytest.mark.parametrize("call", sorted(SINGLE_ACCOUNT_READS))
def test_a_single_account_read_raises_one_typed_error_naming_the_document(
    accounts, bad_id, call
):
    with pytest.raises(MalformedAccountError) as caught:
        SINGLE_ACCOUNT_READS[call](accounts, bad_id)

    exc = caught.value
    assert isinstance(exc, AccountError), "it is an account error like the others"
    assert exc.account_id == bad_id
    assert exc.error in {"TypeError", "AttributeError"}
    assert bad_id in str(exc)
    assert SENTINEL not in str(exc)


@pytest.mark.parametrize("call", sorted(SINGLE_ACCOUNT_READS))
def test_the_readable_account_beside_them_is_unaffected(accounts, call):
    result = SINGLE_ACCOUNT_READS[call](accounts, GOOD)
    assert result is not None
    assert result.account_id == GOOD


def test_a_bad_document_is_not_rewritten_by_the_path_that_failed_to_read_it(
    accounts, broker
):
    """`register` on an existing id is a `set`. Failing to decode must stop it
    BEFORE the write, or the half-read document would be overwritten with
    defaults -- losing its holds and state -- by a call that never read them."""
    before = broker.db.document(f"accounts/{NULL_ASSIGNED}").get().to_dict()

    with pytest.raises(MalformedAccountError):
        accounts.register(ENG, "null-assigned")
    with pytest.raises(MalformedAccountError):
        accounts.mark_reauth_required(NULL_ASSIGNED, "refresh failed")

    assert broker.db.document(f"accounts/{NULL_ASSIGNED}").get().to_dict() == before


# --------------------------------------------------------------------------
# 3. The broker routes that read one account
# --------------------------------------------------------------------------


SINGLE_ACCOUNT_ROUTES = {
    "lending": lambda c, i: c.put(f"/v1/accounts/{i}/lending", json={"lend_to": []}),
    "state": lambda c, i: c.put(f"/v1/accounts/{i}/state", json={"state": "PAUSED"}),
    "refresh": lambda c, i: c.post(f"/v1/accounts/{i}/refresh"),
    "remove": lambda c, i: c.delete(f"/v1/accounts/{i}"),
    "release": lambda c, i: c.post(
        f"/v1/accounts/{i}/release", json={"assignment_id": "a-1"}
    ),
}


@pytest.mark.parametrize("bad_id", BAD)
@pytest.mark.parametrize("route", sorted(SINGLE_ACCOUNT_ROUTES))
def test_a_route_on_a_bad_document_fails_with_a_named_error(client, bad_id, route):
    """Not a bare 500. An unhandled exception under TestClient answers with the
    plain text "Internal Server Error", which is what these routes did."""
    r = SINGLE_ACCOUNT_ROUTES[route](client, bad_id)

    assert r.status_code == 500, r.text
    assert r.headers["content-type"].startswith("application/json"), r.text
    body = r.json()
    assert body["code"] == "account_malformed"
    assert body["account_id"] == bad_id
    assert bad_id in body["message"]
    assert SENTINEL not in r.text
    assert "Traceback" not in r.text


def test_a_route_on_a_bad_document_leaves_the_readable_one_working(client):
    """The same client, the same app, straight after every bad-document
    failure: the readable account still answers every route."""
    for route in sorted(SINGLE_ACCOUNT_ROUTES):
        for bad_id in BAD:
            SINGLE_ACCOUNT_ROUTES[route](client, bad_id)

    assert client.put(f"/v1/accounts/{GOOD}/lending", json={"lend_to": []}).status_code == 200
    assert (
        client.put(f"/v1/accounts/{GOOD}/state", json={"state": "PAUSED"}).status_code
        == 200
    )
    released = client.post(f"/v1/accounts/{GOOD}/release", json={"assignment_id": "a-1"})
    assert released.status_code == 200, released.text
    assert client.delete(f"/v1/accounts/{GOOD}").status_code == 200


def test_assignment_skips_the_bad_documents_and_hands_out_the_readable_one(client):
    client.identity.as_tenant(ENG)

    r = client.post("/v1/accounts/assign", json={"provider": "anthropic"})

    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == GOOD
