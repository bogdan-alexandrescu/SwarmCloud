"""Admin roles in Firestore, and the audit of every change to them.

docs/workspaces.md §6.5 (lane W2 of #847). Admin rights used to be configuration
only -- ADMIN_GROUPS membership or an ADMIN_USERS email -- so granting or
removing one meant a deploy. They now live in documents an admin can change:

    admin_roles/{email}   {role: "owner" | "admin", granted_by, granted_at}
    admin_audit/{id}      {action, target_email, by, at, detail}

WHAT CONFIGURATION STILL DECIDES, AND WHY.

* `PLATFORM_OWNER` is the owner. Configuration, not the UI, makes someone the
  owner, so no admin can make themselves one, and the owner's rights survive
  the loss of every admin document (a break glass). Their `role: owner`
  document is seeded from configuration and is a record, not the source.
* `ADMIN_GROUPS` membership still grants admin. A group is not an email, so it
  has no `admin_roles/{email}` document to live in.
* `ADMIN_USERS` is migrated ONCE into `role: admin` documents with
  `granted_by: "config-migration"`, and keeps granting admin until the
  operator trims it to the owner. That is the fallback: a deployment that has
  not migrated (or whose Firestore admin documents are lost) still has admins.
* `ADMIN_POOL_USERS` and the rollup identities are unchanged. They are narrower
  capabilities, not admin, and moving them here would turn them into admin.

THE TWO SAFEGUARDS, each checked inside the transaction that would break it:

* the owner cannot be demoted or changed by anyone else (`OWNER_PROTECTED`),
  and the owner's own document is not removable at all, because configuration
  would seed it again on the next start (`OWNER_FROM_CONFIG`);
* a removal that would leave no `admin` or `owner` document is refused
  (`LAST_ADMIN`).

THE AUDIT IS APPEND-ONLY. This module writes an entry in the same transaction
as the change it records and has no code path that updates or deletes one. It
is private (Firestore); nothing of it is logged beyond the action and the
target's domain.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import utcnow

from .errors import Conflict, Forbidden, NotFound, ValidationFailed

log = logging.getLogger(__name__)

ROLES_COLLECTION = "admin_roles"
AUDIT_COLLECTION = "admin_audit"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLES = (ROLE_OWNER, ROLE_ADMIN)

#: `granted_by` on a document the one-time migration of ADMIN_USERS wrote, and
#: `by` on its audit entry. Not an email, so it can never be mistaken for one.
CONFIG_MIGRATION = "config-migration"
#: `granted_by` on the owner's document, which configuration seeds.
CONFIG_OWNER = "config:PLATFORM_OWNER"

#: How long one instance trusts a role it read. Every authenticated request
#: asks "is this email an admin?", and a Firestore read on each would add its
#: latency to every call. 15 seconds is the bound on how long a REMOVED admin
#: keeps their rights on an instance other than the one that removed them
#: (that one forgets at once); the group cache it sits beside allows 120.
ROLE_CACHE_SECONDS = 15.0

#: A deliberately plain shape: one `@`, no whitespace, no `/` (which Firestore
#: reads as a path separator in a document id).
_EMAIL = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
_SERVICE_ACCOUNT_DOMAIN = "gserviceaccount.com"


class LastAdmin(Conflict):
    code = "LAST_ADMIN"


class OwnerProtected(Forbidden):
    code = "OWNER_PROTECTED"


class OwnerFromConfig(Conflict):
    code = "OWNER_FROM_CONFIG"


class NotAnAdminDocument(NotFound):
    code = "NOT_AN_ADMIN_DOCUMENT"


def normalise_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not _EMAIL.match(email):
        raise ValidationFailed(f"{raw!r} is not an email address")
    return email


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[1] if "@" in email else ""


def _migration_marker_id(email: str) -> str:
    # Deterministic, so a second run (or a second instance starting at the
    # same moment) finds the first one's entry inside its own transaction.
    return f"{CONFIG_MIGRATION}:{email}"


class AdminRoles:
    """`admin_roles/` and `admin_audit/`: read, migrate, grant, remove.

    Holds no client of its own: `db` is the Firestore client the context
    already built (or a test's fake), so importing this module needs no
    credentials.
    """

    def __init__(
        self,
        db: Any,
        *,
        owner: str = "",
        config_admins: tuple[str, ...] = (),
        allowed_domains: tuple[str, ...] = (),
        allowed_users: tuple[str, ...] = (),
        refused: tuple[str, ...] = (),
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = ROLE_CACHE_SECONDS,
    ) -> None:
        self._db = db
        self.owner = (owner or "").strip().lower()
        self._config_admins = tuple(
            dict.fromkeys(e.strip().lower() for e in config_admins if e.strip())
        )
        self._allowed_domains = {d.strip().lower() for d in allowed_domains if d.strip()}
        self._allowed_users = {u.strip().lower() for u in allowed_users if u.strip()}
        # Addresses that hold some OTHER role (pool admin, rollup sweeper, a
        # tenant's service account, a secret admin): one address with two
        # roles makes which applies depend on the order auth.py asks.
        self._refused = {r.strip().lower() for r in refused if r.strip()}
        self._now = now
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()
        self._migrated = False

    # -- reads ------------------------------------------------------------

    def _role_ref(self, email: str) -> Any:
        return self._db.collection(ROLES_COLLECTION).document(email)

    def role_of(self, email: str) -> str | None:
        """The email's role document's role, None when it has none.

        Raises whatever Firestore raises: the caller decides what an unknown
        answer means (auth.py turns it into a 503 on admin routes, never a
        grant and never a "no").
        """
        email = (email or "").strip().lower()
        if not email:
            return None
        self.ensure_migrated()
        stamp = self._clock()
        cached = self._cache.get(email)
        if cached is not None and cached[0] > stamp:
            return cached[1]
        snap = self._role_ref(email).get()
        role = None
        if snap.exists:
            value = (snap.to_dict() or {}).get("role")
            role = value if value in ROLES else None
        self._cache[email] = (stamp + self._cache_seconds, role)
        return role

    def is_owner(self, email: str) -> bool:
        return bool(self.owner) and (email or "").strip().lower() == self.owner

    def holds_admin(self, email: str) -> bool:
        """True for the configured owner or an email with a role document."""
        if self.is_owner(email):
            return True
        return self.role_of(email) is not None

    def _forget(self, email: str) -> None:
        self._cache.pop(email, None)

    # -- the one-time migration and the owner's seed -----------------------

    def ensure_migrated(self) -> None:
        """Seed the owner and migrate ADMIN_USERS, once per process.

        Idempotent across processes too: each email is one transaction that
        first looks for its own migration entry in the audit, so a second run
        writes nothing, and an admin someone REMOVED after the migration is
        not granted again just because ADMIN_USERS still names them. A failure
        leaves the flag down, so the next call tries again; until then the
        configuration fallback in auth.py keeps every configured admin in.
        """
        if self._migrated:
            return
        with self._lock:
            if self._migrated:
                return
            self.seed_owner()
            for email in self._config_admins:
                if email == self.owner:
                    continue
                if not _EMAIL.match(email):
                    log.warning("ADMIN_USERS entry is not an email; not migrated")
                    continue
                self.migrate_one(email)
            self._migrated = True

    def seed_owner(self) -> bool:
        """Make the configured owner's document `role: owner`; demote any other.

        True when anything was written. A previous owner (PLATFORM_OWNER was
        changed in configuration) becomes an ordinary admin rather than losing
        admin outright: the configuration change named a new owner, it did
        not say to remove the old one.
        """
        wrote = False
        previous = self._db.collection(ROLES_COLLECTION).where(
            filter=FieldFilter("role", "==", ROLE_OWNER)
        )
        for snap in previous.stream():
            if snap.id != self.owner:
                wrote |= self._demote_former_owner(snap.id)
        if self.owner:
            wrote |= self._seed(self.owner)
        return wrote

    def _seed(self, owner: str) -> bool:
        ref = self._role_ref(owner)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            current = (snap.to_dict() or {}) if snap.exists else {}
            if current.get("role") == ROLE_OWNER:
                return False
            txn.set(ref, {"role": ROLE_OWNER, "granted_by": CONFIG_OWNER,
                          "granted_at": self._now()})
            self._audit(txn, "seed_owner", owner, CONFIG_OWNER,
                        {"previous_role": current.get("role")})
            return True

        wrote = _apply(transaction)
        self._forget(owner)
        if wrote:
            log.info("admin owner seeded from PLATFORM_OWNER domain=%s", _domain(owner))
        return wrote

    def _demote_former_owner(self, email: str) -> bool:
        ref = self._role_ref(email)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            if not snap.exists or (snap.to_dict() or {}).get("role") != ROLE_OWNER:
                return False
            txn.set(ref, {"role": ROLE_ADMIN, "granted_by": CONFIG_OWNER,
                          "granted_at": self._now()})
            self._audit(txn, "owner_changed", email, CONFIG_OWNER,
                        {"previous_role": ROLE_OWNER, "role": ROLE_ADMIN})
            return True

        wrote = _apply(transaction)
        self._forget(email)
        return wrote

    def migrate_one(self, email: str) -> bool:
        """Migrate one ADMIN_USERS email. True when this call migrated it."""
        ref = self._role_ref(email)
        marker = self._db.collection(AUDIT_COLLECTION).document(_migration_marker_id(email))
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            # Both reads before any write, as a Firestore transaction requires.
            done = _snapshot(txn.get(marker))
            snap = _snapshot(txn.get(ref))
            if done.exists:
                return False
            created = not snap.exists
            if created:
                txn.set(ref, {"role": ROLE_ADMIN, "granted_by": CONFIG_MIGRATION,
                              "granted_at": self._now()})
            txn.set(marker, self._entry("migrate", email, CONFIG_MIGRATION,
                                        {"role_created": created}))
            return True

        migrated = _apply(transaction)
        self._forget(email)
        if migrated:
            log.info("ADMIN_USERS entry migrated to admin_roles domain=%s", _domain(email))
        return migrated

    # -- grant and remove ---------------------------------------------------

    def _check_grantable(self, email: str) -> None:
        domain = _domain(email)
        if domain == _SERVICE_ACCOUNT_DOMAIN or domain.endswith("." + _SERVICE_ACCOUNT_DOMAIN):
            raise ValidationFailed("admin is for people; a service account cannot be granted it")
        if email in self._refused:
            raise ValidationFailed(
                f"{email} already holds another platform role (pool admin, rollup "
                "sweeper, secret admin or a tenant's service account); one address "
                "holds one role"
            )
        if domain not in self._allowed_domains and email not in self._allowed_users:
            raise ValidationFailed(
                f"{email} is not in an allowed domain or ALLOWED_USERS, so it could "
                "not sign in to use the right"
            )

    def grant(self, email: str, *, by: str) -> dict[str, Any]:
        """Give `email` the admin role. Idempotent: a holder is not re-audited."""
        email = normalise_email(email)
        by = (by or "").strip().lower()
        self._check_grantable(email)
        self.ensure_migrated()
        if self.is_owner(email) and not self.is_owner(by):
            raise OwnerProtected("the platform owner's role can be changed only by the owner")
        ref = self._role_ref(email)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[dict[str, Any], bool]:
            snap = _snapshot(txn.get(ref))
            current = (snap.to_dict() or {}) if snap.exists else {}
            if current.get("role") == ROLE_OWNER and not self.is_owner(by):
                raise OwnerProtected(
                    "the platform owner's role can be changed only by the owner")
            if current.get("role") in ROLES:
                return current, False
            body = {"role": ROLE_ADMIN, "granted_by": by, "granted_at": self._now()}
            txn.set(ref, body)
            self._audit(txn, "grant", email, by, {"role": ROLE_ADMIN})
            return body, True

        body, changed = _apply(transaction)
        self._forget(email)
        if changed:
            log.info("admin granted target_domain=%s", _domain(email))
        return _to_api(email, body, changed=changed)

    def remove(self, email: str, *, by: str) -> dict[str, Any]:
        """Take the admin role from `email`, under both safeguards."""
        email = normalise_email(email)
        by = (by or "").strip().lower()
        self.ensure_migrated()
        if self.is_owner(email):
            if not self.is_owner(by):
                raise OwnerProtected("the platform owner cannot be demoted by anyone else")
            raise OwnerFromConfig(
                "the platform owner is set by PLATFORM_OWNER in swarm-api's "
                "configuration; change it there, not here"
            )
        ref = self._role_ref(email)
        # A query, not the collection: a transaction reads documents or
        # queries, and a CollectionReference is neither.
        everyone = self._db.collection(ROLES_COLLECTION).where(
            filter=FieldFilter("role", "in", list(ROLES))
        )
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            holders = {
                snap.id: (snap.to_dict() or {}).get("role")
                for snap in txn.get(everyone)
            }
            role = holders.get(email)
            if role not in ROLES:
                raise NotAnAdminDocument(
                    f"{email} has no admin role document. If they are still an "
                    "admin, it is from ADMIN_GROUPS membership or ADMIN_USERS, "
                    "which only a configuration change removes"
                )
            if role == ROLE_OWNER and not self.is_owner(by):
                # A former owner's document is demoted at seed; this is the
                # backstop if one is ever read before that has run.
                raise OwnerProtected("an owner's role can be changed only by the owner")
            remaining = [e for e, r in holders.items() if r in ROLES and e != email]
            if not remaining:
                raise LastAdmin(
                    f"removing {email} would leave no admin; grant someone else first")
            txn.delete(ref)
            self._audit(txn, "remove", email, by, {"previous_role": role})
            return {"email": email, "removed": True}

        result = _apply(transaction)
        self._forget(email)
        log.info("admin removed target_domain=%s", _domain(email))
        return result

    # -- the audit ----------------------------------------------------------

    def _entry(self, action: str, target: str, by: str,
               detail: dict[str, Any]) -> dict[str, Any]:
        return {"action": action, "target_email": target, "by": by,
                "at": self._now(), "detail": detail}

    def _audit(self, txn: Any, action: str, target: str, by: str,
               detail: dict[str, Any]) -> None:
        """Append one entry inside `txn`. The only writer of `admin_audit/`
        besides the migration marker, and it only ever creates."""
        ref = self._db.collection(AUDIT_COLLECTION).document()
        txn.set(ref, self._entry(action, target, by, detail))


def _to_api(email: str, body: dict[str, Any], *, changed: bool) -> dict[str, Any]:
    granted_at = body.get("granted_at")
    return {
        "email": email,
        "role": body.get("role"),
        "granted_by": body.get("granted_by"),
        "granted_at": granted_at.isoformat() if hasattr(granted_at, "isoformat") else granted_at,
        "changed": changed,
    }


def build_admin_roles(db: Any, settings: Any, *,
                      now: Callable[[], datetime] = utcnow) -> AdminRoles:
    """From ApiSettings. getattr because hand-built settings in tests predate
    `platform_owner`, exactly as auth.py reads `allowed_users`."""
    refused = (
        tuple(getattr(settings, "admin_pool_users", ()))
        + tuple(getattr(settings, "rollup_sweeper_users", ()))
        + tuple(getattr(settings, "secret_admin_principals", ()))
        + tuple(m.email for m in getattr(settings, "tenant_service_accounts", ()))
    )
    return AdminRoles(
        db,
        owner=getattr(settings, "platform_owner", ""),
        config_admins=tuple(getattr(settings, "admin_users", ())),
        allowed_domains=tuple(settings.core.allowed_domains),
        allowed_users=tuple(getattr(settings, "allowed_users", ())),
        refused=refused,
        now=now,
    )
