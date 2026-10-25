"""Who holds which role in a workspace, read from signed append-only grants
(docs/design/workspace-roles.md §4.4, §4.5, §4.7; lane ROLES-API-CORE, part
of #1518).

Tenant isolation (invariant 9) is the risk. A tenant's worker can create and
update any Firestore document whose id it can name and cannot list
(`swarmTenantWorkerFirestore`), so every attack below is written the way that
worker could write it: `collection(...).document()` to create, `.update()` on
an id it already holds, and never a list. Each must leave the person's role
exactly where the newest VALID grant put it, or make it unknown -- never
wider.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_common.identity import Principal

from swarm_api import roles
from swarm_api.auth import AuthContext
from swarm_api.errors import RoleUnavailable
from swarm_api.metrics import ApiMetrics
from swarm_api.roles import RoleStore

from .conftest import ENG_GROUP, api_settings
from .fakes import FakeFirestore

T0 = datetime(2026, 10, 20, 9, 10, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


class Recording(FakeFirestore):
    """Records every collection asked for, and breaks the ones in `broken`."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []
        self.broken: set[str] = set()

    def collection(self, path: str) -> Any:
        self.asked.append(path)
        if path in self.broken:
            return Broken()
        return super().collection(path)


class Broken:
    """A collection whose every read and write fails, as Firestore down does."""

    def where(self, *args: Any, **kwargs: Any) -> "Broken":
        return self

    order_by = limit = where

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("firestore unavailable")

    get = stream

    def document(self, *args: Any, **kwargs: Any) -> "Broken":
        return self

    def set(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("firestore unavailable")


def key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def signing_key() -> str:
    return key()


@pytest.fixture
def store(db, clock, signing_key) -> RoleStore:
    settings = api_settings(settings_key=signing_key, role_cache_seconds=15.0)
    return RoleStore(db, settings, metrics=ApiMetrics(), now=clock)


def ctx(email: str, tenant_id: str = "eng", principal: str = ENG_GROUP, *,
        is_admin: bool = False, via: str = "") -> AuthContext:
    return AuthContext(
        principal=Principal(email=email, subject=f"s-{email}", domain="example.com"),
        tenant_id=tenant_id, is_admin=is_admin, tenant_principal=principal, tenant_via=via)


def grant(store: RoleStore, subject: str, role: str | None, *, tenant_id: str = "eng",
          by: str = "ops@example.com") -> str:
    """A grant written the way the role routes write one."""
    previous = store.newest_valid(tenant_id, subject, fresh=True)
    return store.write(tenant_id=tenant_id, subject=subject, role=role, by=by,
                       via="console", reason="", previous=previous,
                       action=roles.AUDIT_GRANTED, detail={})


def grant_docs(db: FakeFirestore) -> dict[str, dict]:
    return db.dump(f"{roles.GRANTS_COLLECTION}/")


def worker_create(db: FakeFirestore, doc: dict) -> str:
    """What a tenant worker can do: create a document under a new id."""
    ref = FakeFirestore.collection(db, roles.GRANTS_COLLECTION).document()
    ref.set(doc)
    return ref.id


# -- the migration: nothing stored --------------------------------------------

def test_an_empty_grant_set_resolves_every_group_member_to_member(store):
    for email in ("alice@example.com", "bob@example.com", "dave@example.com"):
        me = store.resolve(ctx(email))
        assert (me.role, me.source) == ("member", "default")
    assert store.default_role("eng") == ("member", None)


def test_a_personal_workspaces_person_is_its_admin_with_nothing_read(store, db):
    me = store.resolve(ctx("carol@example.com", "u-carol", "carol@example.com"))
    assert (me.role, me.source) == ("admin", "personal")
    assert roles.GRANTS_COLLECTION not in db.asked


# -- a platform admin (Q5) ------------------------------------------------------

def test_a_platform_admin_is_admin_in_a_group_workspace_with_no_grant_read(store, db):
    me = store.resolve(ctx("ops@example.com", is_admin=True, via="platform_admin"))
    assert (me.role, me.source) == ("admin", "platform_admin")
    # The fake records no query of the grants at all.
    assert roles.GRANTS_COLLECTION not in db.asked


def test_a_grant_naming_a_platform_admin_viewer_changes_nothing(store, db):
    grant(store, "root@example.com", "viewer")
    db.asked.clear()
    me = store.resolve(ctx("root@example.com", is_admin=True), fresh=True)
    assert (me.role, me.source) == ("admin", "platform_admin")
    assert roles.GRANTS_COLLECTION not in db.asked
    # Stored, and applies if they stop being one.
    assert store.resolve(ctx("root@example.com"), fresh=True).role == "viewer"


# -- overrides and the default --------------------------------------------------

def test_the_newest_valid_override_wins_and_a_clear_returns_to_the_default(store, clock):
    grant(store, "carol@example.com", "admin")
    clock.tick(seconds=1)
    grant(store, "carol@example.com", "viewer")
    me = store.resolve(ctx("carol@example.com"), fresh=True)
    assert (me.role, me.source) == ("viewer", "override")
    clock.tick(seconds=1)
    grant(store, "carol@example.com", None)
    assert store.resolve(ctx("carol@example.com"), fresh=True).source == "default"


def test_a_default_grant_applies_to_everyone_without_an_override(store):
    grant(store, roles.DEFAULT_SUBJECT, "viewer")
    grant(store, "alice@example.com", "member")
    assert store.resolve(ctx("bob@example.com"), fresh=True).role == "viewer"
    assert store.resolve(ctx("alice@example.com"), fresh=True).role == "member"


def test_two_writes_on_a_clock_that_did_not_move_still_order(store):
    grant(store, "carol@example.com", "admin")
    grant(store, "carol@example.com", "viewer")
    assert store.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"


# -- what a worker can write, and why none of it wins --------------------------

def test_a_grant_with_a_bad_mac_does_not_win(store, db, clock):
    grant(store, "carol@example.com", "viewer")
    clock.tick(seconds=1)
    forged = {"tenant_id": "eng", "subject": "carol@example.com", "role": "admin",
              "granted_by": "ops@example.com", "granted_at": "2099-01-01T00:00:00.000000Z",
              "via": "console", "reason": "",
              "mac": {"v": 1, "key": "0" * 16, "sig": secrets.token_hex(16)}}
    worker_create(db, forged)
    assert store.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"


def test_a_signed_grant_rewritten_by_update_fails_its_mac(store, db, clock):
    """The worker somehow holds a real grant's id and updates its role: the
    MAC no longer verifies, and the next-older valid grant applies."""
    grant(store, "carol@example.com", "member")
    clock.tick(seconds=1)
    grant(store, "carol@example.com", "viewer")
    newest = max(grant_docs(db).items(), key=lambda kv: kv[1]["granted_at"])[0]
    FakeFirestore.document(db, newest).update({"role": "admin"})
    assert store.resolve(ctx("carol@example.com"), fresh=True).role == "member"


def test_another_tenants_grant_copied_in_does_not_verify(store, db):
    grant(store, "carol@example.com", "admin", tenant_id="research")
    (doc,) = grant_docs(db).values()
    worker_create(db, dict(doc, tenant_id="eng"))
    me = store.resolve(ctx("carol@example.com"), fresh=True)
    assert (me.role, me.source) == ("member", "default")


def test_a_copied_older_grant_does_not_win(store, db, clock):
    """Replay: the worker read carol's `admin` grant before an admin narrowed
    her, and writes a copy afterwards. The copy keeps its old `granted_at`
    under the MAC, so the newer grant still wins."""
    grant(store, "carol@example.com", "admin")
    (old,) = grant_docs(db).values()
    clock.tick(minutes=5)
    grant(store, "carol@example.com", "viewer")
    clock.tick(minutes=5)
    worker_create(db, dict(old))
    assert store.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"
    # And moving its granted_at forward breaks the MAC.
    worker_create(db, dict(old, granted_at=roles.granted_at_text(clock() + timedelta(days=1))))
    assert store.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"


def test_a_default_grant_of_admin_is_skipped_even_when_signed(store, db, signing_key):
    fields = {"tenant_id": "eng", "subject": "*", "role": "admin",
              "granted_by": "ops@example.com", "granted_at": roles.granted_at_text(T0)}
    worker_create(db, dict(fields, via="console", reason="",
                           mac=roles.sign_grant((signing_key,), fields)))
    assert store.default_role("eng", fresh=True) == ("member", None)
    assert store.resolve(ctx("alice@example.com"), fresh=True).role == "member"


def test_61_unsigned_newer_grants_give_role_unavailable_never_a_wider_role(store, db, clock):
    grant(store, "carol@example.com", "viewer")
    for n in range(61):
        worker_create(db, {
            "tenant_id": "eng", "subject": "carol@example.com", "role": "admin",
            "granted_by": "ops@example.com",
            "granted_at": roles.granted_at_text(clock() + timedelta(seconds=n + 1)),
            "via": "console", "reason": ""})
    with pytest.raises(RoleUnavailable):
        store.resolve(ctx("carol@example.com"), fresh=True)
    # The gate never turns "unknown" into a role: with the switch on it is
    # the 503, never member and never admin.
    with pytest.raises(RoleUnavailable):
        store.person_role("eng", "carol@example.com", fresh=True)


def test_59_unsigned_newer_grants_still_find_the_valid_one(store, db, clock):
    """The control: the bound, not the forgery, is what makes it unknown."""
    grant(store, "carol@example.com", "viewer")
    for n in range(59):
        worker_create(db, {
            "tenant_id": "eng", "subject": "carol@example.com", "role": "admin",
            "granted_by": "ops@example.com",
            "granted_at": roles.granted_at_text(clock() + timedelta(seconds=n + 1))})
    assert store.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"


def test_a_skipped_grant_is_reported_once_and_never_by_its_id(store, db):
    doc_id = worker_create(db, {"tenant_id": "eng", "subject": "carol@example.com",
                                "role": "admin", "granted_by": "x",
                                "granted_at": roles.granted_at_text(T0)})
    store.resolve(ctx("carol@example.com"), fresh=True)
    store.resolve(ctx("carol@example.com"), fresh=True)
    rows = [r for r in db.dump("admin_audit/").values()
            if r["action"] == "workspace_role_grant_invalid"]
    assert len(rows) == 1
    assert rows[0]["detail"] == {"tenant_id": "eng", "subject": "carol@example.com",
                                 "reason": "mac"}
    assert doc_id not in repr(rows)


def test_with_no_settings_key_every_grant_fails_toward_the_default(db, clock, signing_key):
    signed = RoleStore(db, api_settings(settings_key=signing_key), now=clock)
    grant(signed, "carol@example.com", "viewer")
    keyless = RoleStore(db, api_settings(settings_key=""), now=clock)
    assert keyless.resolve(ctx("carol@example.com"), fresh=True).role == "member"


def test_a_rotated_key_still_verifies_older_grants(db, clock):
    old, new = key(), key()
    grant(RoleStore(db, api_settings(settings_key=old), now=clock), "carol@example.com", "viewer")
    rotated = RoleStore(db, api_settings(settings_key=new, settings_key_previous=old), now=clock)
    assert rotated.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"


# -- the cache ------------------------------------------------------------------

def test_a_cached_read_may_be_stale_and_a_fresh_one_never_is(store):
    assert store.resolve(ctx("carol@example.com")).role == "member"
    other = RoleStore(store._db, store._settings, now=store._now)
    assert other.resolve(ctx("carol@example.com")).role == "member"
    # `write` drops the writing instance's entry; another instance's stands
    # for up to role_cache_seconds.
    grant(store, "carol@example.com", "viewer", by="alice@example.com")
    assert store.resolve(ctx("carol@example.com")).role == "viewer"
    assert other.resolve(ctx("carol@example.com")).role == "member"
    assert other.resolve(ctx("carol@example.com"), fresh=True).role == "viewer"


def test_a_failed_read_is_unavailable_not_a_guess(store, db):
    db.broken.add(roles.GRANTS_COLLECTION)
    with pytest.raises(RoleUnavailable):
        store.resolve(ctx("carol@example.com"), fresh=True)


# -- which role a route needs, for the audit -----------------------------------

@pytest.mark.parametrize(("method", "route", "needed"), [
    ("GET", "/v1/tasks", "viewer"),
    ("POST", "/v1/tasks", "member"),
    ("POST", "/v1/tasks/{task_id}/live/ticket", "viewer"),
    ("POST", "/v1/tasks/{task_id}/cancel", "member"),
    ("POST", "/v1/workspace/secrets", "admin"),
    ("POST", "/v1/tenants/me/credentials", "admin"),
    # The one-time links to the console's forms (SSC-LINKS): admin writes,
    # like the workspace-secret and provider-key links (LINK-ADMIN-WRITES).
    ("POST", "/v1/workspace/secrets/links", "admin"),
    ("POST", "/v1/provider-keys/{provider}/links", "admin"),
    ("POST", "/v1/tenants/me/credentials/links", "admin"),
    ("POST", "/v1/qa-targets/{target_id}/credential-links", "admin"),
    ("PUT", "/v1/workspace/roles/person", "admin"),
    ("POST", "/v1/a/route/added/tomorrow", "member"),
    ("PUT", "/v1/admin/limits/runner/{runner_profile}", None),
    ("GET", "/v1/tenants/mine", None),
])
def test_required_role(method, route, needed):
    assert roles.required_role(method, route) == needed
