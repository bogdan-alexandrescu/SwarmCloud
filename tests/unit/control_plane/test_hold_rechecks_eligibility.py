"""The hold transaction re-checks eligibility, not just existence (#227 box 19).

The assign route lists the accounts serving a tenant, chooses one, and then
takes the hold in a transaction. Between the listing and the transaction an
owner can revoke a loan, or an operator can pause or drain the account. The
transaction used to check only that the document still existed, so the
revoked borrower was handed the account anyway -- a loan that had been taken
back still served a tenant it no longer names, which is invariant 9.

These change the document AFTER the route's listing and BEFORE the hold, the
exact window the defect lived in, and pin that nothing is held, nothing is
logged and nothing is handed out.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import HOLD_LOG_COLLECTION
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

ENG = "eng"
RESEARCH = "research"
LENT = f"{ENG}:lent"


class _Identity:
    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (self.tenant, False)


class _RacingStore(AccountStore):
    """Lists the accounts, then applies `after_list` to the raw documents.

    That is the race: the route's eligibility check reads the listing, and
    the document the transaction reads has changed since.
    """

    after_list: Any = None

    def list(self):  # noqa: A003 - the store's own name
        listed = super().list()
        if self.after_list is not None:
            self.after_list()
        return listed


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


def _client(db: FakeFirestore, tenant: str) -> tuple[TestClient, _RacingStore]:
    broker = QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))
    store = _RacingStore(broker.db)
    store.register(ENG, "lent", lend_to=[RESEARCH])
    app = create_app(broker, identity=_Identity(tenant), account_store=store)
    return TestClient(app, raise_server_exceptions=False), store


def _assign(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/v1/accounts/assign",
        json={"provider": "anthropic", "task_id": "task_1", "attempt_id": "att_1"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _doc(db: FakeFirestore) -> dict[str, Any]:
    return db.docs[f"accounts/{LENT}"]


def _assert_nothing_held(db: FakeFirestore, answer: dict[str, Any]) -> None:
    assert answer["account_id"] is None
    assert answer["assignment_id"] is None
    assert answer["secret"] is None
    assert not _doc(db).get("holds"), "a hold was recorded on an account that no longer serves"
    logged = [p for p in db.docs if p.startswith(f"{HOLD_LOG_COLLECTION}/")]
    assert logged == [], "a hold record was opened for a hold that must not exist"


def test_the_control_takes_a_hold_when_nothing_changes(db):
    # The control: without the race the borrower IS served, so the refusals
    # below are the re-check and not a fixture that never served anyone.
    client, _store = _client(db, RESEARCH)
    answer = _assign(client)
    assert answer["account_id"] == LENT
    assert len(_doc(db)["holds"]) == 1


def test_a_loan_revoked_after_the_listing_takes_no_hold(db):
    client, store = _client(db, RESEARCH)
    store.after_list = lambda: _doc(db).update({"lend_to": []})
    _assert_nothing_held(db, _assign(client))


@pytest.mark.parametrize("state", ["PAUSED", "DRAINING", "REAUTH_REQUIRED"])
def test_an_account_leaving_available_after_the_listing_takes_no_hold(db, state):
    client, store = _client(db, RESEARCH)
    store.after_list = lambda: _doc(db).update({"state": state})
    _assert_nothing_held(db, _assign(client))


def test_the_owner_paused_after_the_listing_takes_no_hold_either(db):
    client, store = _client(db, ENG)
    store.after_list = lambda: _doc(db).update({"state": "PAUSED"})
    _assert_nothing_held(db, _assign(client))


def test_the_owner_still_takes_a_hold_after_its_loan_is_revoked(db):
    # Revoking a loan narrows who the account serves; the owner is not on it.
    client, store = _client(db, ENG)
    store.after_list = lambda: _doc(db).update({"lend_to": []})
    answer = _assign(client)
    assert answer["account_id"] == LENT
    assert len(_doc(db)["holds"]) == 1
