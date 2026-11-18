"""The refresh sweep writes down what it did, per account, and that it ran (U27).

docs/web-ui/06-accounts.md P2: the sweep had each account's
`RefreshOutcome.expires_at` in hand and threw it away with the rest of a
response Cloud Scheduler discards, so the Accounts screen could show a token's
expiry only after somebody pressed Refresh. Pinned here:

  * every sweep writes `last_refresh_at`, `token_expires_at` and
    `last_refresh_reason` onto each account it visited, KEYED BY ACCOUNT ID --
    two tenants may share a label, and a write keyed on it lands on whichever
    document it found first;
  * each reason the refresher can answer is written as answered;
  * `GET /v1/accounts` serves those fields and the sweep's liveness -- when the
    credential sweep last completed and how -- read from the sweep lease
    document, and null when that was never written;
  * no key material is served: an instant and a reason, nothing else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import REFRESH_REASONS
from quota_broker.accountstore import AccountStore
from quota_broker.credentials import RefreshOutcome
from quota_broker.main import WorkerIdentity, create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings
from quota_broker.sweeplease import COLLECTION as LEASE_COLLECTION
from quota_broker.sweeplease import QUOTA_SWEEP

from .conftest import core_settings
from .fakes import FakeFirestore

LEASE_DOC = f"{LEASE_COLLECTION}/{QUOTA_SWEEP}"


class _Refresher:
    """Answers each account with a chosen reason and expiry, by the key it was handed."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.handed: list[str] = []

    def sweep(self, tenants, keep_going=None):
        return []

    def _answer(self, key):
        reason, expires = self.answers.get(key, ("still_valid", None))
        return RefreshOutcome(key, "account", reason == "refreshed", reason, expires)

    def sweep_accounts(self, secrets, keep_going=None, held=()):
        self.handed = [key for _base, key in secrets]
        return [self._answer(key) for key in self.handed]

    def refresh_secret(self, base, *, label=""):
        return self._answer(label)


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def store(db) -> AccountStore:
    s = AccountStore(db)
    s.register("eng", "personal")
    s.register("research", "personal")
    return s


def _client(db, store, refresher) -> TestClient:
    broker = QuotaBroker(db, settings=BrokerSettings(core=core_settings(), default_hard_max=50))
    app = create_app(
        broker,
        identity=WorkerIdentity(required=False),
        credential_refresher=refresher,
        subscription_tenants=lambda: [],
        account_store=store,
    )
    app.state.usage_poller = None   # reaches the network; not this file's subject
    return TestClient(app, raise_server_exceptions=False)


def _listed(client, account_id):
    return next(a for a in client.get("/v1/accounts").json()["accounts"]
                if a["account_id"] == account_id)


def test_the_write_back_is_keyed_by_account_id_when_two_tenants_share_a_label(db, store):
    expires = (datetime.now(timezone.utc) + timedelta(hours=5)).replace(microsecond=0)
    refresher = _Refresher({
        "research:personal": ("refreshed", expires),
        "eng:personal": ("refresh_failed", None),
    })
    client = _client(db, store, refresher)

    r = client.post("/v1/quota/sweep")

    assert r.status_code == 200, r.text
    assert sorted(refresher.handed) == ["eng:personal", "research:personal"]
    assert r.json()["accounts"]["recorded"] == 2
    research = _listed(client, "research:personal")
    eng = _listed(client, "eng:personal")
    assert research["last_refresh_reason"] == "refreshed"
    assert research["token_expires_at"] == expires.isoformat()
    assert eng["last_refresh_reason"] == "refresh_failed"
    assert eng["token_expires_at"] is None, "the other tenant's expiry did not land here"
    assert research["last_refresh_at"] and eng["last_refresh_at"]


@pytest.mark.parametrize("reason", REFRESH_REASONS)
def test_each_reason_is_written_as_answered(db, store, reason):
    client = _client(db, store, _Refresher({"eng:personal": (reason, None)}))

    client.post("/v1/quota/sweep")

    assert db.docs["accounts/eng:personal"]["last_refresh_reason"] == reason


def test_a_failed_refresh_keeps_the_expiry_a_previous_one_recorded(db, store):
    expires = datetime.now(timezone.utc) + timedelta(hours=4)
    _client(db, store, _Refresher({"eng:personal": ("still_valid", expires)})).post(
        "/v1/quota/sweep"
    )
    client = _client(db, store, _Refresher({"eng:personal": ("store_unavailable", None)}))

    client.post("/v1/quota/sweep")

    account = _listed(client, "eng:personal")
    assert account["last_refresh_reason"] == "store_unavailable"
    assert account["token_expires_at"] == expires.isoformat()


def test_a_manual_refresh_writes_back_under_the_account_id(db, store):
    expires = datetime.now(timezone.utc) + timedelta(hours=6)
    client = _client(db, store, _Refresher({"research:personal": ("refreshed", expires)}))

    r = client.post("/v1/accounts/research:personal/refresh")

    assert r.status_code == 200, r.text
    assert r.json()["refresh"]["tenant_id"] == "research:personal"
    assert db.docs["accounts/research:personal"]["token_expires_at"] == expires
    assert "last_refresh_at" not in db.docs["accounts/eng:personal"] or (
        db.docs["accounts/eng:personal"]["last_refresh_at"] is None
    )


def test_re_registering_an_account_keeps_its_refresh_record(db, store):
    expires = datetime.now(timezone.utc) + timedelta(hours=4)
    _client(db, store, _Refresher({"eng:personal": ("refreshed", expires)})).post(
        "/v1/quota/sweep"
    )

    store.register("eng", "personal", lend_to=("research",))   # how lending changes

    assert store.get("eng:personal").token_expires_at == expires


def test_liveness_is_null_when_the_lease_was_never_written(db, store):
    client = _client(db, store, _Refresher({}))

    sweep = client.get("/v1/accounts").json()["refresh_sweep"]

    assert LEASE_DOC not in db.docs
    assert sweep == {"last_completed_at": None, "last_outcome": None, "readable": True}


def test_liveness_comes_from_the_lease_document(db, store):
    client = _client(db, store, _Refresher({}))
    assert client.post("/v1/quota/sweep").status_code == 200

    lease = db.docs[LEASE_DOC]
    sweep = client.get("/v1/accounts").json()["refresh_sweep"]

    assert lease["last_outcome"] == "ok"
    assert sweep["last_outcome"] == "ok"
    assert sweep["last_completed_at"] == lease["last_completed_at"].isoformat()


def test_a_planted_lease_document_is_what_the_listing_serves(db, store):
    """The listing reads the document, not anything the process remembers."""
    at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    db.docs[LEASE_DOC] = {"holder": "", "last_completed_at": at, "last_outcome": "error"}
    client = _client(db, store, _Refresher({}))

    sweep = client.get("/v1/accounts").json()["refresh_sweep"]

    assert sweep["last_completed_at"] == at.isoformat()
    assert sweep["last_outcome"] == "error"


def test_the_completion_survives_the_next_sweep_taking_the_lease(db, store):
    client = _client(db, store, _Refresher({}))
    client.post("/v1/quota/sweep")
    first = db.docs[LEASE_DOC]["last_completed_at"]

    from quota_broker.sweeplease import FirestoreSweepLease

    FirestoreSweepLease(db).acquire("another-sweep")

    assert db.docs[LEASE_DOC]["last_completed_at"] == first
    assert db.docs[LEASE_DOC]["last_outcome"] == "ok"


def test_a_sweep_whose_account_phase_failed_says_error(db, store):
    class _Broken(_Refresher):
        def sweep_accounts(self, secrets, keep_going=None, held=()):
            raise RuntimeError("secret manager is down")

    client = _client(db, store, _Broken({}))
    client.post("/v1/quota/sweep")

    assert db.docs[LEASE_DOC]["last_outcome"] == "error"


def test_no_key_material_is_served_with_the_refresh_record(db, store):
    token = "sk-ant-" + "oat01-" + "y" * 30
    client = _client(db, store, _Refresher({"eng:personal": ("refreshed", None)}))
    client.post("/v1/quota/sweep")

    body = client.get("/v1/accounts").text

    assert token not in body
    for leaf in ("access_token", "refresh_token", "access_token_len"):
        assert leaf not in body
