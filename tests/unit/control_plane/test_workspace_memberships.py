"""Platform-side workspace memberships: signed, append-only grants beside the
Google group (docs/design/join-requests.md §4.4; lane JOIN-STORE, part of
#2325).

Tenant isolation (invariant 9) is the risk. A tenant's worker can get, create
and update any Firestore document whose id it can name and cannot list, so
every attack below is written the way that worker could write it: a new
document under `collection(...).document()`, or an update of one it holds.
Each must leave the person exactly where the newest VALID grant put them, or
make the answer unknown -- never admitted where they were not.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import memberships
from swarm_api.errors import (
    JoinNotOffered,
    JoinSigningUnavailable,
    MembershipUnavailable,
    ValidationFailed,
)
from swarm_api.memberships import MembershipStore
from swarm_api.metrics import ApiMetrics

from .conftest import ENG_GROUP, RESEARCH_GROUP, api_settings
from .fakes import FakeFirestore

T0 = datetime(2026, 10, 25, 6, 41, tzinfo=timezone.utc)
LEE = "lee@example.com"
REGISTERED = {"eng": ENG_GROUP, "research": RESEARCH_GROUP}


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


class Recording(FakeFirestore):
    """Counts the membership queries, and breaks reads when told to."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0
        self.broken = False

    def collection(self, path: str) -> Any:
        if path == memberships.COLLECTION:
            self.asked += 1
            if self.broken:
                return Broken()
        return super().collection(path)


class Broken:
    def where(self, *args: Any, **kwargs: Any) -> "Broken":
        return self

    order_by = limit = where

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("firestore unavailable")


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def metrics() -> ApiMetrics:
    return ApiMetrics()


@pytest.fixture
def signing_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def store(db, clock, metrics, signing_key) -> MembershipStore:
    settings = api_settings(settings_key=signing_key, role_cache_seconds=15.0)
    return MembershipStore(db, settings, metrics=metrics, now=clock)


def add(store: MembershipStore, subject: str = LEE, tenant_id: str = "eng",
        group: str = ENG_GROUP, state: str = memberships.MEMBER) -> memberships.Membership:
    return store.write(tenant_id=tenant_id, group=group, subject=subject, state=state,
                       by="alex@example.com", via=memberships.VIA_JOIN_REQUEST,
                       detail={"ref": "j-" + secrets.token_hex(6)})


def worker_create(db: FakeFirestore, doc: dict) -> str:
    ref = FakeFirestore.collection(db, memberships.COLLECTION).document()
    ref.set(doc)
    return ref.id


def stored(db: FakeFirestore) -> dict[str, dict]:
    return db.dump(f"{memberships.COLLECTION}/")


def invalid_count(metrics: ApiMetrics, reason: str) -> float:
    value = metrics.registry.get_sample_value(
        "swarm_api_membership_invalid_total", {"reason": reason})
    return value or 0.0


# -- what a valid membership does ---------------------------------------------

def test_a_written_membership_counts_for_its_registered_group(store, db):
    add(store)
    assert store.counted(LEE, REGISTERED, fresh=True) == (("eng", ENG_GROUP),)
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is True
    assert store.is_member(LEE, "research", REGISTERED, fresh=True) is False
    (doc,) = stored(db).values()
    assert doc["mac"]["v"] == memberships.MAC_VERSION
    assert set(memberships.SIGNED_FIELDS) <= set(doc)


def test_the_membership_and_its_audit_row_are_one_batch(store, db, monkeypatch):
    add(store)
    rows = [r for r in db.collection_docs("admin_audit")
            if r["action"] == memberships.AUDIT_ADDED]
    assert len(rows) == 1 and rows[0]["target_email"] == LEE
    assert rows[0]["target_tenant_id"] == "eng"
    # No row names the grant's document id: it is what an overwrite needs.
    doc_id = next(iter(stored(db))).rsplit("/", 1)[1]
    assert doc_id not in repr(rows)

    def boom(row):
        raise RuntimeError("injected")

    before = db.dump()
    monkeypatch.setattr(MembershipStore, "audit_row", staticmethod(boom))
    with pytest.raises(RuntimeError):
        add(store, subject="kim@example.com")
    assert db.dump() == before


def test_a_group_replaced_since_the_grant_admits_nobody(store):
    add(store)
    replaced = {"eng": "eng-new@example.com"}
    assert store.counted(LEE, replaced, fresh=True) == ()
    assert store.is_member(LEE, "eng", replaced, fresh=True) is False
    # A workspace no longer registered at all admits nobody either.
    assert store.is_member(LEE, "eng", {}, fresh=True) is False


def test_counted_memberships_come_in_at_order(store, clock):
    add(store, tenant_id="research", group=RESEARCH_GROUP)
    clock.tick(seconds=1)
    add(store)
    assert store.counted(LEE, REGISTERED, fresh=True) == (
        ("research", RESEARCH_GROUP), ("eng", ENG_GROUP))


def test_a_removal_ends_it_and_two_writes_on_a_still_clock_still_order(store):
    add(store)
    add(store, state=memberships.REMOVED)
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is False


# -- what a worker can write, and why none of it admits -----------------------

def test_a_forged_unsigned_membership_is_skipped_and_counted(store, db, metrics):
    worker_create(db, {"tenant_id": "eng", "group": ENG_GROUP, "subject": LEE,
                       "state": "member", "by": "alex@example.com",
                       "at": "2026-10-25T06:41:00.000000Z", "via": "join_request"})
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is False
    assert invalid_count(metrics, "mac") == 1.0


def test_a_membership_with_a_bad_mac_is_skipped_and_counted(store, db, metrics):
    add(store, state=memberships.REMOVED)
    forged = {"tenant_id": "eng", "group": ENG_GROUP, "subject": LEE, "state": "member",
              "by": "alex@example.com", "at": "2099-01-01T00:00:00.000000Z",
              "mac": {"v": 1, "key": "k", "sig": secrets.token_hex(16)}}
    worker_create(db, forged)
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is False
    assert invalid_count(metrics, "mac") == 1.0


def test_a_replayed_old_member_loses_to_the_newer_removed(store, db, clock):
    add(store)
    (old,) = stored(db).values()
    clock.tick(seconds=5)
    add(store, state=memberships.REMOVED)
    # The worker writes its saved copy back under a new id: the copy keeps
    # its old `at`, under the MAC, so the newer removal still wins.
    worker_create(db, old)
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is False


def test_an_overwritten_current_grant_falls_back_to_the_previous_one(store, db, clock):
    add(store, state=memberships.REMOVED)
    clock.tick(seconds=1)
    add(store)
    newest = max(stored(db).items(), key=lambda kv: kv[1]["at"])[0]
    db.document(newest).update({"subject": LEE, "tenant_id": "research"})
    assert store.is_member(LEE, "research", REGISTERED, fresh=True) is False
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is False


def test_a_membership_copied_into_another_tenant_does_not_verify(store, db, metrics):
    add(store)
    (doc,) = stored(db).values()
    worker_create(db, dict(doc, tenant_id="research", group=RESEARCH_GROUP))
    assert store.is_member(LEE, "research", REGISTERED, fresh=True) is False
    assert invalid_count(metrics, "mac") == 1.0


def test_a_flood_past_the_bounded_read_is_unknown_never_member(store, db):
    add(store)
    for i in range(memberships.SCAN_LIMIT):
        worker_create(db, {"tenant_id": "research", "group": RESEARCH_GROUP, "subject": LEE,
                           "state": "member", "by": "x@example.com",
                           "at": f"2099-01-01T00:00:00.{i:06d}Z"})
    read = store.read(LEE, fresh=True)
    assert read.cut_short is True
    assert read.answer("eng", REGISTERED) is None
    with pytest.raises(MembershipUnavailable):
        store.is_member(LEE, "eng", REGISTERED, fresh=True)


# -- who can never hold one ----------------------------------------------------

@pytest.mark.parametrize("subject", [
    "builder@my-project.iam.gserviceaccount.com", "*", "not-an-email",
])
def test_a_service_account_or_non_person_subject_is_refused_at_write(store, db, subject):
    with pytest.raises(ValidationFailed):
        add(store, subject=subject)
    assert stored(db) == {}


def test_a_personal_workspace_is_refused_at_write(store, db):
    with pytest.raises(JoinNotOffered):
        add(store, tenant_id="u-lee", group=LEE)
    assert stored(db) == {}


def test_a_signed_personal_or_service_account_grant_is_skipped_at_read(
        store, db, signing_key, metrics):
    keys = (signing_key,)
    service = "builder@my-project.iam.gserviceaccount.com"
    for tenant_id, subject in (("u-lee", LEE), ("eng", service)):
        fields = {"tenant_id": tenant_id, "group": ENG_GROUP, "subject": subject,
                  "state": "member", "by": "alex@example.com",
                  "at": "2026-10-25T06:41:00.000000Z"}
        worker_create(db, dict(fields, mac=memberships.sign_membership(keys, fields)))
    assert store.read(LEE, fresh=True).answer("u-lee", {"u-lee": ENG_GROUP}) is False
    assert store.is_member(service, "eng", REGISTERED, fresh=True) is False
    assert invalid_count(metrics, "shape") == 2.0


# -- reads ---------------------------------------------------------------------

def test_a_failed_read_is_membership_unavailable(store, db):
    db.broken = True
    with pytest.raises(MembershipUnavailable) as caught:
        store.read(LEE, fresh=True)
    assert caught.value.status_code == 503
    assert caught.value.code == "membership_unavailable"


def test_reads_are_cached_per_subject_and_fresh_reads_skip_the_cache(store, db):
    add(store)
    store.read(LEE)
    asked = db.asked
    store.read(LEE)
    assert db.asked == asked
    store.read(LEE, fresh=True)
    assert db.asked == asked + 1


def test_a_write_drops_the_subjects_cache(store):
    assert store.is_member(LEE, "eng", REGISTERED) is False
    add(store)
    assert store.is_member(LEE, "eng", REGISTERED) is True


def test_an_environment_without_a_settings_key_writes_nothing(db, clock):
    store = MembershipStore(db, api_settings(settings_key=""), now=clock)
    with pytest.raises(JoinSigningUnavailable):
        add(store)
    assert stored(db) == {}
