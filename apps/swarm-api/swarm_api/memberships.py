"""Platform-side workspace memberships: who joined a group workspace by an
approved request, beside its Google group (docs/design/join-requests.md §4.4;
lane JOIN-STORE, part of #2325; owner's Q1 (A), 2026-10-25).

A membership ADMITS: it is what lets tenant resolution offer a group
workspace to a person the group does not list (§4.5, lane JOIN-RESOLVE,
behind `platform_memberships`, off). So it is written and read the way a
role grant is (`roles.py`), for the same reason -- a tenant's worker can get,
create and update any Firestore document whose id it can name, and cannot
list:

* APPEND-ONLY, under a Firestore auto-id no route serves and no audit row
  names. The newest grant per (subject, tenant) that VERIFIES wins; an
  overwrite of the current one fails its MAC and drops the person to their
  previous grant, at worst `removed`.
* SIGNED with the settings key under its own domain string,
  `swarm-workspace-membership/v1\\n`, over (tenant_id, group, subject,
  state, by, at). A forgery does not verify; a replayed old `member` keeps
  its old `at`, so the newer `removed` wins; a copy into another tenant
  fails the MAC, which covers `tenant_id` and `group`.
* COUNTED ONLY FOR THE GROUP IT NAMES. `(tenant_id, group)` must be an entry
  of the registered group workspaces NOW, so a group replaced in
  TENANT_GROUPS, or a record no longer `ready`, admits nobody through an
  old membership.
* NEVER a `u-` tenant, never `"*"`, never a service account: refused at
  write, and skipped (and counted) at read, whatever signed it.

A FAILED READ IS UNKNOWN, NEVER "NOT A MEMBER" (§4.4). Firestore failing is
`MembershipUnavailable`; a read that reached its bound answers only the
tenants it saw, and `is_member` on any other is `MembershipUnavailable` too.
A flood of unsigned newer documents is therefore denial of service of the
platform membership alone -- never escalation, and never a group member's
access, which this module is not asked about.

This module imports nothing from `auth.py` or `roles.py`: the resolver
imports it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from prometheus_client import Counter

from .admins import AUDIT_COLLECTION
from .errors import (
    JoinNotOffered,
    JoinSigningUnavailable,
    MembershipUnavailable,
    ValidationFailed,
)
from .gitidentity import is_service_account
from .judgment import key_id, settings_keys
from .metrics import ApiMetrics
from .redaction import redact
from .workspaces import is_service_identity

log = logging.getLogger(__name__)

COLLECTION = "workspace_memberships"

MEMBER = "member"
REMOVED = "removed"
STATES = frozenset({MEMBER, REMOVED})

VIA_JOIN_REQUEST = "join_request"
VIA_WORKSPACE_ADMIN = "workspace_admin"
VIA_PLATFORM_ADMIN = "platform_admin"
VIAS = frozenset({VIA_JOIN_REQUEST, VIA_WORKSPACE_ADMIN, VIA_PLATFORM_ADMIN})

#: The bounded read (§4.4): three pages of 20, read as ONE query of 60 -- the
#: bound `roles.SCAN_LIMIT` is, in one round trip. It is one query per
#: person across tenants, because the resolver needs every workspace the
#: person joined, not one.
PAGE_SIZE = 20
PAGES = 3
SCAN_LIMIT = PAGE_SIZE * PAGES
REASON_MAX = 200

MAC_VERSION = 1
DOMAIN = b"swarm-workspace-membership/v1\n"
SIGNED_FIELDS: tuple[str, ...] = ("tenant_id", "group", "subject", "state", "by", "at")
#: Fixed width, as `roles._AT_FORMAT`, so the string order Firestore sorts
#: `at` by is the time order.
AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: `admin_audit` actions (§4.7).
AUDIT_ADDED = "workspace_member_added"
AUDIT_REMOVED = "workspace_member_removed"

_EMAIL = re.compile(r"^[^@\s\"'<>(),;:\\]+@[^@\s\"'<>(),;:\\]+\.[^@\s\"'<>(),;:\\]+$")


# ---------------------------------------------------------------------------
# Signing: one primitive, a domain per record (joinrequests.py signs its
# three records with it too)
# ---------------------------------------------------------------------------


def at_text(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(AT_FORMAT)


def parse_at(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _canonical(domain: bytes, fields: Mapping[str, Any], names: Iterable[str]) -> bytes:
    signed = {k: fields.get(k) for k in names}
    return domain + json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sig(key: str, domain: bytes, fields: Mapping[str, Any], names: Iterable[str]) -> str:
    digest = hmac.new(key.encode("utf-8"), _canonical(domain, fields, names),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_record(keys: tuple[str, ...], domain: bytes, fields: Mapping[str, Any],
                names: Iterable[str]) -> dict[str, Any]:
    """The `mac` field for a record, under the current key and `domain`.
    The settings key `roles.sign_grant` uses; each record's own domain string
    means no record can ever verify as another kind."""
    if not keys:
        raise JoinSigningUnavailable(
            "join requests and memberships cannot be written in this environment: it has "
            "no settings key to sign them with (SWARM_SETTINGS_KEY). Nothing was written.")
    return {"v": MAC_VERSION, "key": key_id(keys[0]), "sig": _sig(keys[0], domain, fields, names)}


def record_verifies(keys: tuple[str, ...], domain: bytes, doc: Mapping[str, Any],
                    names: Iterable[str]) -> bool:
    mac = doc.get("mac")
    if not isinstance(mac, Mapping) or mac.get("v") != MAC_VERSION:
        return False
    sig = mac.get("sig")
    if not isinstance(sig, str):
        return False
    names = tuple(names)
    return any(hmac.compare_digest(_sig(key, domain, doc, names), sig) for key in keys)


def sign_membership(keys: tuple[str, ...], fields: Mapping[str, Any]) -> dict[str, Any]:
    return sign_record(keys, DOMAIN, fields, SIGNED_FIELDS)


def membership_verifies(keys: tuple[str, ...], doc: Mapping[str, Any]) -> bool:
    return record_verifies(keys, DOMAIN, doc, SIGNED_FIELDS)


# ---------------------------------------------------------------------------
# Who can hold one
# ---------------------------------------------------------------------------


def is_person(email: Any) -> bool:
    """One person's address: not `"*"`, not malformed, not a service
    account by either test the platform uses (`gitidentity`, the frozen
    pattern and the service-submitter domains)."""
    if not isinstance(email, str):
        return False
    value = email.strip().lower()
    if value != email or len(value) > 254 or not _EMAIL.match(value):
        return False
    return not (is_service_account(value) or is_service_identity(value))


def clean_person(email: Any, field: str) -> str:
    value = (email if isinstance(email, str) else "").strip().lower()
    if not is_person(value):
        raise ValidationFailed(f"{field} must be one person's address, not a service account",
                               detail={"field": field})
    return value


def offered_tenant(tenant_id: Any) -> str:
    """A tenant a request, membership or link may name: never a personal
    (`u-`) workspace (§4.1). A service workspace is not a registered group,
    so the registered-group check every reader makes refuses it there."""
    if not isinstance(tenant_id, str) or not tenant_id or tenant_id.startswith("u-"):
        raise JoinNotOffered(
            "only a group workspace takes join requests; a personal workspace has one "
            "person, and a service workspace has none")
    return tenant_id


def clean_text(text: Any, field: str, limit: int, *, required: bool = False) -> str:
    """Optional free text, bounded and redacted BEFORE it is written: a
    pasted token must never reach a document an admin or the person reads."""
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValidationFailed(f"{field} is text", detail={"field": field})
    if len(text) > limit:
        raise ValidationFailed(f"{field} is at most {limit} characters", detail={"field": field})
    value = redact(text.strip()).text[:limit]
    if required and not value:
        raise ValidationFailed(f"{field} is required", detail={"field": field})
    return value


def _shape_problem(doc: Mapping[str, Any], subject: str) -> str | None:
    tenant_id = doc.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id or tenant_id.startswith("u-"):
        return "shape"
    if doc.get("subject") != subject or not is_person(subject):
        return "shape"
    if doc.get("state") not in STATES:
        return "shape"
    if not isinstance(doc.get("group"), str) or not isinstance(doc.get("by"), str):
        return "shape"
    if parse_at(doc.get("at")) is None:
        return "shape"
    return None


# ---------------------------------------------------------------------------
# Metrics: registered on the app's registry the first time a store is made
# ---------------------------------------------------------------------------

_COUNTERS_LOCK = threading.Lock()


def counter(metrics: ApiMetrics, attr: str, name: str, doc: str) -> Counter:
    """A reason-labelled counter on `metrics`' registry, made once per
    registry. A rising count is a forged, edited or replayed record being
    skipped -- the same alert shape as `swarm_api_role_grant_invalid_total`."""
    with _COUNTERS_LOCK:
        found = getattr(metrics, attr, None)
        if found is None:
            found = Counter(name, doc, ["reason"], registry=metrics.registry)
            setattr(metrics, attr, found)
        return found


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Membership:
    tenant_id: str
    group: str
    subject: str
    state: str
    by: str
    at: str
    via: str = ""
    reason: str = ""

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "Membership":
        return cls(tenant_id=str(doc["tenant_id"]), group=str(doc["group"]),
                   subject=str(doc["subject"]), state=str(doc["state"]), by=str(doc["by"]),
                   at=str(doc["at"]), via=str(doc.get("via") or ""),
                   reason=str(doc.get("reason") or ""))

    def counts_for(self, registered: Mapping[str, str]) -> bool:
        group = registered.get(self.tenant_id)
        return (self.state == MEMBER and isinstance(group, str)
                and group.strip().lower() == self.group.strip().lower())


@dataclass(frozen=True)
class MembershipRead:
    """One person's newest valid grant per tenant, from one bounded read."""

    subject: str
    #: tenant_id -> the newest grant there that verified.
    newest: Mapping[str, Membership]
    #: True when the read reached SCAN_LIMIT: a tenant not in `newest` may
    #: hold a valid grant past the bound, so its answer is unknown.
    cut_short: bool

    def answer(self, tenant_id: str, registered: Mapping[str, str]) -> bool | None:
        """Whether the person is a platform member of `tenant_id` now; None
        when the bounded read cannot say."""
        grant = self.newest.get(tenant_id)
        if grant is not None:
            return grant.counts_for(registered)
        return None if self.cut_short else False

    def counted(self, registered: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        """(tenant_id, group) for every membership that admits now, in `at`
        order, oldest first (§4.5 step 1)."""
        live = sorted((g for g in self.newest.values() if g.counts_for(registered)),
                      key=lambda g: g.at)
        return tuple((g.tenant_id, registered[g.tenant_id].strip().lower()) for g in live)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class MembershipStore:
    """Reads, caches, verifies and writes platform memberships. One per app,
    so its cache is per instance (§4.4: per subject, for the role cache's
    15 s), and a test gets a fresh one with its app."""

    def __init__(self, db: Any, settings: Any, *, metrics: ApiMetrics | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._db = db
        self._settings = settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl = float(getattr(settings, "role_cache_seconds", 15.0) or 0.0)
        self._invalid = counter(
            metrics or ApiMetrics(), "membership_invalid", "swarm_api_membership_invalid_total",
            "Platform workspace memberships the reader skipped, by reason (mac, shape).")
        self._lock = threading.Lock()
        #: subject -> (monotonic expiry, the read)
        self._cache: dict[str, tuple[float, MembershipRead]] = {}

    def _keys(self) -> tuple[str, ...]:
        return settings_keys(getattr(self._settings, "settings_key", ""),
                             getattr(self._settings, "settings_key_previous", ""))

    def _grants(self) -> Any:
        return self._db.collection(COLLECTION)

    # -- reading ------------------------------------------------------------

    def read(self, subject: str, *, fresh: bool = False) -> MembershipRead:
        """The person's newest valid grant per tenant. Raises
        `MembershipUnavailable` when Firestore fails."""
        subject = (subject or "").strip().lower()
        if not fresh and self._ttl > 0:
            with self._lock:
                hit = self._cache.get(subject)
            if hit is not None and hit[0] > time.monotonic():
                return hit[1]
        try:
            query = (self._grants()
                     .where(filter=FieldFilter("subject", "==", subject))
                     .order_by("at", direction=firestore.Query.DESCENDING)
                     .limit(SCAN_LIMIT))
            snaps = list(query.stream())
        except Exception as exc:  # noqa: BLE001 - any Firestore failure is "unknown"
            log.warning("memberships unreadable (%s)", type(exc).__name__)
            raise MembershipUnavailable(
                "your workspace memberships could not be read; retry shortly") from None
        keys = self._keys()
        newest: dict[str, Membership] = {}
        for snap in snaps:
            doc = snap.to_dict() or {}
            tenant_id = doc.get("tenant_id")
            if isinstance(tenant_id, str) and tenant_id in newest:
                continue
            reason = _shape_problem(doc, subject)
            if reason is None and not membership_verifies(keys, doc):
                reason = "mac"
            if reason is not None:
                log.warning("membership invalid tenant=%s reason=%s",
                            str(tenant_id)[:64], reason)
                self._invalid.labels(reason=reason).inc()
                continue
            newest[tenant_id] = Membership.from_doc(doc)  # type: ignore[index]
        cut_short = len(snaps) >= SCAN_LIMIT
        if cut_short:
            log.warning("memberships reached the bounded read; unread tenants are unknown")
        result = MembershipRead(subject=subject, newest=newest, cut_short=cut_short)
        if self._ttl > 0:
            with self._lock:
                self._cache[subject] = (time.monotonic() + self._ttl, result)
        return result

    def counted(self, subject: str, registered: Mapping[str, str], *,
                fresh: bool = False) -> tuple[tuple[str, str], ...]:
        return self.read(subject, fresh=fresh).counted(registered)

    def is_member(self, subject: str, tenant_id: str, registered: Mapping[str, str], *,
                  fresh: bool = False) -> bool:
        """Whether `subject` holds a membership that admits to `tenant_id`
        now. `MembershipUnavailable` when that cannot be known."""
        answer = self.read(subject, fresh=fresh).answer(tenant_id, registered)
        if answer is None:
            raise MembershipUnavailable(
                "your workspace memberships could not be read: too many records failed "
                "verification; an operator has been alerted")
        return answer

    # -- writing --------------------------------------------------------------

    def _next_at(self, previous: Membership | None) -> str:
        """Now, and strictly after the grant it supersedes: the newer write
        must sort first even on a clock that did not move."""
        when = self._now().astimezone(timezone.utc)
        before = parse_at(previous.at) if previous is not None else None
        if before is not None and when <= before:
            when = before + timedelta(microseconds=1)
        return at_text(when)

    def write(self, *, tenant_id: str, group: str, subject: str, state: str, by: str,
              via: str, reason: str | None = None, detail: Mapping[str, Any] | None = None,
              writer: Any = None) -> Membership:
        """Append one signed grant and its `admin_audit` row together. With
        `writer` (a transaction or batch the caller commits) both are set on
        it, so an approval's decision, membership and audit rows land in one
        commit (§4.3); without one they are one batch of their own. The
        document id is Firestore's auto-id and is returned to nobody."""
        tenant_id = offered_tenant(tenant_id)
        subject = clean_person(subject, "subject")
        group = clean_person(group, "group")
        if state not in STATES:
            raise ValidationFailed("state is member or removed", detail={"field": "state"})
        if via not in VIAS:
            raise ValidationFailed("via is not one this platform writes", detail={"field": "via"})
        reason = clean_text(reason, "reason", REASON_MAX)
        keys = self._keys()
        previous = self.read(subject, fresh=True).newest.get(tenant_id)
        at = self._next_at(previous)
        fields = {"tenant_id": tenant_id, "group": group, "subject": subject, "state": state,
                  "by": (by or "").strip().lower(), "at": at}
        doc = dict(fields, via=via, reason=reason, mac=sign_membership(keys, fields))
        row = self.audit_row({
            "action": AUDIT_ADDED if state == MEMBER else AUDIT_REMOVED,
            "target_email": subject, "target_tenant_id": tenant_id, "by": fields["by"],
            "at": self._now(), "detail": dict(detail or {}, via=via),
        })
        own = writer is None
        target = self._db.batch() if own else writer
        target.set(self._grants().document(), doc)
        target.set(self._db.collection(AUDIT_COLLECTION).document(), row)
        if own:
            target.commit()
        with self._lock:
            self._cache.pop(subject, None)
        log.info("workspace membership written tenant=%s state=%s via=%s", tenant_id, state, via)
        return Membership.from_doc(doc)

    @staticmethod
    def audit_row(row: dict[str, Any]) -> dict[str, Any]:
        """The audit row as written: the seam a test injects a failure at, to
        show the grant and its row commit together or not at all."""
        return row
