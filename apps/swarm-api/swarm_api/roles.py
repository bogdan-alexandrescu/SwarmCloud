"""Workspace roles: viewer < member < admin, per workspace (docs/design/workspace-roles.md).

Lane ROLES-API-CORE, part of #1518. This module is the ONLY place a person's
role in a workspace is decided, and the only place the ladder is written down;
the console and the plugin read it from `GET /v1/workspace/roles`.

THE RULES, in the order `RoleStore.resolve` applies them (§4.1, §4.2, §4.5):

* A personal workspace's person is its `admin`, fixed (`source: personal`).
  Nobody else can select it, and nothing is stored for it.
* A platform admin (`AuthContext.is_admin`) is `admin` in every GROUP
  workspace it is in or reaches (`source: platform_admin`, Q5). No grant is
  read for them -- so a flood of grants cannot touch their role -- and a grant
  naming them is stored and shown but inert while they are one.
* Everyone else holds their newest VALID per-person grant (`source:
  override`), else the workspace default: the newest valid `"*"` grant, else
  `member` (§4.7: on the day this ships everyone keeps exactly what they have).

A role never admits anyone: it is asked only about the tenant the request
already selected through a verified membership, the caller's own personal
workspace, or a platform admin's reach (`auth._select_tenant`).

GRANTS ARE SIGNED AND APPEND-ONLY (§4.4, Q9). A tenant's worker can get, create
and update any Firestore document whose id it can name, and cannot list. So a
grant is a new document under a Firestore auto-id that no route ever serves,
MAC'd over (tenant_id, subject, role, granted_by, granted_at) with the settings
key under its own domain string, and the reader takes the newest grant that
VERIFIES. A forged grant does not verify; a copied old grant keeps its old
`granted_at`, so the newer one still wins; a grant copied into another tenant
fails the MAC, which covers `tenant_id`. A flood of unsigned newer grants can
push the valid one past the bounded read, and that is `role_unavailable` --
denial of service, never a wider role.

A FAILED READ IS "UNKNOWN", NOT A GUESS (§4.4). At a gate whose switch is off
the request goes through as it does today, and the log says so; with the
switch on it is 503 `role_unavailable`. Turning a switch on is therefore the
only thing that adds a Firestore dependency to a route that had none.

THE PLATFORM-ADMIN AUDIT (§4.2.4, Q12) is `audit_platform_admin`, called from
`deps.current_auth` for every authenticated request, before the route body:
a platform admin acting in a group workspace they are not a member of, or a
member platform admin whose own role is below what the route needs, gets an
`admin_audit` row before a write runs and one per UTC hour for reads, and the
request is refused 503 if the row cannot be written.
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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.identity import tenant_id_for_group

from . import refusals
from .admins import AUDIT_COLLECTION
from .auth import VIA_PLATFORM_MEMBER, AuthContext
from .errors import (
    PlatformAdminAuditUnavailable,
    RoleAdminRequired,
    RoleMemberRequired,
    RolePersonRequired,
    RoleSigningUnavailable,
    RoleUnavailable,
    UnknownRole,
    ValidationFailed,
)
from .gitidentity import is_service_account
from .judgment import key_id, settings_keys
from .metrics import ApiMetrics
from .redaction import redact

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The ladder (§4.1). Nothing else restates it.
# ---------------------------------------------------------------------------

VIEWER = "viewer"
MEMBER = "member"
ADMIN = "admin"
ROLES: tuple[str, ...] = (VIEWER, MEMBER, ADMIN)
RANK: dict[str, int] = {VIEWER: 0, MEMBER: 1, ADMIN: 2}
#: What the default may be: never `admin`, so a workspace has no admin until a
#: platform admin names one (§4.6, Q2).
DEFAULT_ROLES: tuple[str, ...] = (VIEWER, MEMBER)
#: The default with no default grant (§4.7): exactly what a member does today.
MIGRATION_DEFAULT = MEMBER

#: The sentences `GET /v1/workspace/roles` serves, so the explanation a screen
#: shows is the server's own.
SUMMARIES: dict[str, str] = {
    VIEWER: "Sees everything in this workspace; changes nothing.",
    MEMBER: "Submits, approves, answers, cancels, and manages schedules, repositories "
            "and accounts.",
    ADMIN: "A member who also manages secrets, MCP servers, provider keys, the "
           "workspace's settings and people's roles.",
}

#: `source` of a resolved role.
SOURCE_OVERRIDE = "override"
SOURCE_DEFAULT = "default"
SOURCE_PERSONAL = "personal"
SOURCE_PLATFORM_ADMIN = "platform_admin"

#: `TenantChoice.via` (§4.2.2 step 5).
VIA_MEMBER = "member"
VIA_PERSONAL = "personal"
VIA_PLATFORM_ADMIN = "platform_admin"
#: A grant's `via`: shown, never acted on, and not under the MAC.
GRANT_VIAS: tuple[str, ...] = ("console", "cli", VIA_PLATFORM_ADMIN)


def rank(role: str | None) -> int:
    return RANK.get(role or "", -1)


def at_least(role: str | None, needed: str) -> bool:
    return rank(role) >= RANK[needed]


def check_role(role: Any) -> str:
    if not isinstance(role, str) or role.strip().lower() not in RANK:
        raise UnknownRole(
            "a role is viewer, member or admin", detail={"roles": list(ROLES)})
    return role.strip().lower()


# ---------------------------------------------------------------------------
# Storage (§4.4)
# ---------------------------------------------------------------------------

GRANTS_COLLECTION = "workspace_role_grants"
#: The workspace default's subject.
DEFAULT_SUBJECT = "*"
#: The bounded read: three pages of 20. Read as ONE query of 60 -- the same
#: bound, in one round trip. Past it with no valid grant the answer is
#: `role_unavailable`, never the default, because the grant past the bound
#: may be the one that narrowed this person.
PAGE_SIZE = 20
PAGES = 3
SCAN_LIMIT = PAGE_SIZE * PAGES
#: How many grants one roster read looks at, and how many people it lists.
ROSTER_SCAN = 2000
ROSTER_MAX = 500
REASON_MAX = 200
#: The workspace audit list (§4.3): the last 50 role and platform-admin rows.
AUDIT_SHOWN = 50
#: Read this many of the tenant's newest `admin_audit` rows to find them:
#: the same tenant's merge-settings and judgment rows are interleaved.
AUDIT_SCAN = 400

MAC_VERSION = 1
_MAC_DOMAIN = b"swarm-workspace-role-grant/v1\n"
#: Fixed width, so the string order Firestore sorts `granted_at` by is the
#: time order.
_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: `admin_audit` actions (§4.10), in the collection's snake_case past tense.
AUDIT_GRANTED = "workspace_role_granted"
AUDIT_CLEARED = "workspace_role_cleared"
AUDIT_DEFAULT_SET = "workspace_default_role_set"
AUDIT_GRANT_INVALID = "workspace_role_grant_invalid"
AUDIT_PLATFORM_ADMIN_ACTED = "platform_admin_acted"
AUDIT_PLATFORM_ADMIN_VIEWED = "platform_admin_viewed"
WORKSPACE_AUDIT_ACTIONS = frozenset({
    AUDIT_GRANTED, AUDIT_CLEARED, AUDIT_DEFAULT_SET, AUDIT_GRANT_INVALID,
    AUDIT_PLATFORM_ADMIN_ACTED, AUDIT_PLATFORM_ADMIN_VIEWED,
})

_EMAIL = re.compile(r"^[^@\s\"'<>(),;:\\]+@[^@\s\"'<>(),;:\\]+\.[^@\s\"'<>(),;:\\]+$")


def granted_at_text(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(_AT_FORMAT)


def _canonical(fields: Mapping[str, Any]) -> bytes:
    signed = {k: fields.get(k) for k in ("tenant_id", "subject", "role", "granted_by",
                                         "granted_at")}
    return _MAC_DOMAIN + json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sig(key: str, fields: Mapping[str, Any]) -> str:
    digest = hmac.new(key.encode("utf-8"), _canonical(fields), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_grant(keys: tuple[str, ...], fields: Mapping[str, Any]) -> dict[str, Any]:
    """The `mac` field for a grant, under the current key. The settings key
    `workspace_settings` blocks are MAC'd with, under this module's own domain
    string, so neither MAC can ever verify as the other."""
    if not keys:
        raise RoleSigningUnavailable(
            "roles cannot be changed in this environment: it has no settings key to "
            "sign them with (SWARM_SETTINGS_KEY). Nothing was written.")
    return {"v": MAC_VERSION, "key": key_id(keys[0]), "sig": _sig(keys[0], fields)}


def grant_verifies(keys: tuple[str, ...], doc: Mapping[str, Any]) -> bool:
    mac = doc.get("mac")
    if not isinstance(mac, Mapping) or mac.get("v") != MAC_VERSION:
        return False
    sig = mac.get("sig")
    if not isinstance(sig, str):
        return False
    return any(hmac.compare_digest(_sig(key, doc), sig) for key in keys)


def shape_problem(doc: Mapping[str, Any], tenant_id: str, subject: str) -> str | None:
    """Why `doc` cannot be a grant for (tenant_id, subject) by its shape, or
    None. A `"*"` grant of `admin` is invalid by shape: the default can never
    be `admin`, whatever signed it."""
    if doc.get("tenant_id") != tenant_id or doc.get("subject") != subject:
        return "shape"
    if not isinstance(doc.get("granted_at"), str) or not isinstance(doc.get("granted_by"), str):
        return "shape"
    role = doc.get("role")
    if subject == DEFAULT_SUBJECT:
        return None if role in DEFAULT_ROLES else "shape"
    return None if role is None or role in RANK else "shape"


@dataclass(frozen=True)
class Grant:
    tenant_id: str
    subject: str
    #: None: "back to the default" (a person's grant only).
    role: str | None
    granted_by: str
    granted_at: str
    via: str = ""
    reason: str = ""
    key: str = ""

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "Grant":
        mac = doc.get("mac") or {}
        return cls(
            tenant_id=str(doc["tenant_id"]), subject=str(doc["subject"]), role=doc.get("role"),
            granted_by=str(doc["granted_by"]), granted_at=str(doc["granted_at"]),
            via=str(doc.get("via") or ""), reason=str(doc.get("reason") or ""),
            key=str(mac.get("key") or "") if isinstance(mac, Mapping) else "",
        )


@dataclass(frozen=True)
class Resolution:
    """A person's role in one workspace, and why."""

    role: str
    source: str
    #: The person's own grant when `source` is `override`.
    grant: Grant | None = None

    def to_api(self) -> dict[str, Any]:
        return {"name": self.role, "source": self.source}


def is_personal(auth: AuthContext) -> bool:
    """The selected tenant is the caller's own personal workspace: its
    principal is the caller themselves (`_with_own_workspace`, and the
    personal fallback, are the only places such a tenant is selected). A
    group tenant's principal is the group's address."""
    email = (auth.email or "").strip().lower()
    principal = (auth.tenant_principal or auth.email or "").strip().lower()
    return bool(email) and principal == email


def clean_email(email: Any) -> str:
    value = (email if isinstance(email, str) else "").strip().lower()
    if len(value) > 254 or not _EMAIL.match(value):
        raise ValidationFailed("email must be one person's address", detail={"field": "email"})
    return value


def clean_reason(reason: Any) -> str:
    if reason is None:
        return ""
    if not isinstance(reason, str):
        raise ValidationFailed("reason is text", detail={"field": "reason"})
    if len(reason) > REASON_MAX:
        raise ValidationFailed(f"reason is at most {REASON_MAX} characters",
                               detail={"field": "reason"})
    # Redacted BEFORE it is written: a pasted token in a reason must never
    # reach a document every member can read.
    return redact(reason.strip()).text[:REASON_MAX]


def registered_group_tenants(settings: Any) -> dict[str, str]:
    """tenant_id -> group for every registered group workspace a platform
    admin may administer: the walk `Authenticator._reachable_groups` makes --
    admin order, the first group per id -- with every `u-` id dropped (§2.1:
    a group slug can land in the personal namespace)."""
    out: dict[str, str] = {}
    for group in getattr(settings, "tenant_groups", ()) or ():
        out.setdefault(tenant_id_for_group(group), group.lower())
    return {t: g for t, g in out.items() if not t.startswith("u-")}


# ---------------------------------------------------------------------------
# Which role a route needs, for the platform-admin audit (§4.1, §4.2.4)
# ---------------------------------------------------------------------------

#: The person's own routes and the platform's: no workspace role reaches or
#: narrows them (§4.1 "What every role keeps", "What no workspace role
#: reaches"), so a member platform admin's own role never decides them.
PERSON_ROUTES: frozenset[str] = frozenset({
    "/v1/tenants/me", "/v1/tenants/mine", "/v1/workspace", "/v1/workspace/loan-request",
    "/v1/provider-keys/{provider}/user",
})
PERSON_ROUTE_PREFIXES: tuple[str, ...] = (
    "/v1/admin/", "/v1/onboarding/github", "/v1/profile",
    # A person's own join requests (join-requests.md §4.8): they are not in
    # the workspace yet, so no role there decides them.
    "/v1/join/", "/v1/join-requests",
)
#: Writes a `viewer` may make (§4.1's viewer row).
VIEWER_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/tasks/{task_id}/live/ticket"),
    ("POST", "/v1/notifications/read"),
    ("POST", "/v1/notifications/read-all"),
    ("POST", "/v1/onboarding/dismiss"),
})
#: Writes only the workspace's `admin` makes (§4.1's admin row, Q6, Q13). A
#: route whose role depends on its body (a git-token slot's scope, a
#: repository PATCH's fields, a provider-key link's scope) is listed here, so
#: the audit errs toward a row too many, never one too few.
ADMIN_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/workspace/secrets"),
    ("PUT", "/v1/workspace/secrets/{name}/value"),
    ("DELETE", "/v1/workspace/secrets/{name}"),
    ("PUT", "/v1/workspace/secrets/{name}/grants/{consumer}"),
    ("PUT", "/v1/workspace/secrets/{name}/use-in"),
    ("POST", "/v1/workspace/secrets/links"),
    ("POST", "/v1/mcp-servers"),
    ("PATCH", "/v1/mcp-servers/{name}"),
    ("PUT", "/v1/provider-keys/{provider}/tenant"),
    ("DELETE", "/v1/provider-keys/{provider}/tenant"),
    ("POST", "/v1/provider-keys/{provider}/links"),
    ("POST", "/v1/tenants/me/credentials"),
    # The one-time links to the runner-key and QA-target credential forms
    # (SSC-LINKS), admin writes like the two links above (LINK-ADMIN-WRITES).
    ("POST", "/v1/tenants/me/credentials/links"),
    ("POST", "/v1/qa-targets/{target_id}/credential-links"),
    ("POST", "/v1/git-tokens"),
    ("POST", "/v1/git-tokens/{token_id}/verify"),
    ("DELETE", "/v1/git-tokens/{token_id}"),
    ("PUT", "/v1/workspaces/{tenant_id}/merge-settings"),
    ("PUT", "/v1/workspaces/{tenant_id}/code-intelligence"),
    ("PUT", "/v1/workspaces/{tenant_id}/qa-settings"),
    ("PUT", "/v1/workspaces/{tenant_id}/review-gate"),
    ("PUT", "/v1/workspaces/{tenant_id}/judgment-backend"),
    ("PUT", "/v1/workspaces/{tenant_id}/name"),
    ("PATCH", "/v1/repositories/{repo_id}"),
    ("PUT", "/v1/workspace/roles/person"),
    ("DELETE", "/v1/workspace/roles/person"),
    ("PUT", "/v1/workspace/roles/default"),
    # The join link and removing a platform membership (join-requests.md §4.6).
    ("PUT", "/v1/workspace/join-link"),
    ("DELETE", "/v1/workspace/members/person"),
})
_READS = frozenset({"GET", "HEAD", "OPTIONS"})


def required_role(method: str, route: str | None) -> str | None:
    """The lowest role `method route` needs in the selected workspace, or None
    for a route that is the person's own or the platform's. A route this
    table does not know is a `member` write or a `viewer` read: a route added
    tomorrow is audited like its neighbours without anyone remembering."""
    method = (method or "").upper()
    if route is not None and (route in PERSON_ROUTES or route.startswith(PERSON_ROUTE_PREFIXES)):
        return None
    if method in _READS:
        return VIEWER
    if route is None:
        return MEMBER
    if (method, route) in VIEWER_WRITES:
        return VIEWER
    if (method, route) in ADMIN_WRITES:
        return ADMIN
    return MEMBER


def _hour(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class RoleStore:
    """Reads, caches, verifies and writes workspace role grants, and keeps the
    platform-admin audit. One per app (`deps.AppContext.roles`), so its caches
    are per instance and a test gets a fresh one with its app."""

    def __init__(self, db: Any, settings: Any, *, metrics: ApiMetrics | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._db = db
        self._settings = settings
        self._metrics = metrics or ApiMetrics()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl = float(getattr(settings, "role_cache_seconds", 15.0) or 0.0)
        self._lock = threading.Lock()
        #: (tenant, subject) -> (monotonic expiry, newest valid grant or None)
        self._cache: dict[tuple[str, str], tuple[float, Grant | None]] = {}
        #: Grant documents already reported invalid on this instance: the row
        #: is written once per document per instance, so a flood writes at
        #: most SCAN_LIMIT rows per read (§4.10).
        self._reported: set[str] = set()
        #: (admin, tenant, UTC hour) whose `platform_admin_viewed` row this
        #: instance has written.
        self._viewed: set[tuple[str, str, str]] = set()

    # -- keys -------------------------------------------------------------

    def _keys(self) -> tuple[str, ...]:
        return settings_keys(getattr(self._settings, "settings_key", ""),
                             getattr(self._settings, "settings_key_previous", ""))

    # -- reading ------------------------------------------------------------

    def _grants(self) -> Any:
        return self._db.collection(GRANTS_COLLECTION)

    def newest_valid(self, tenant_id: str, subject: str, *, fresh: bool = False) -> Grant | None:
        """The newest grant for (tenant_id, subject) that verifies, or None
        when the bounded read found none and was not cut short. Raises
        `RoleUnavailable` when Firestore fails or SCAN_LIMIT grants were read
        and none verified."""
        key = (tenant_id, subject)
        if not fresh and self._ttl > 0:
            with self._lock:
                hit = self._cache.get(key)
            if hit is not None and hit[0] > time.monotonic():
                return hit[1]
        try:
            query = (self._grants()
                     .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                     .where(filter=FieldFilter("subject", "==", subject))
                     .order_by("granted_at", direction=firestore.Query.DESCENDING)
                     .limit(SCAN_LIMIT))
            snaps = list(query.stream())
        except Exception as exc:  # noqa: BLE001 - any Firestore failure is "unknown"
            log.warning("role grants unreadable tenant=%s (%s)", tenant_id, type(exc).__name__)
            raise RoleUnavailable(
                "this workspace's roles could not be read; retry shortly") from None
        keys = self._keys()
        found: Grant | None = None
        for snap in snaps:
            doc = snap.to_dict() or {}
            reason = shape_problem(doc, tenant_id, subject)
            if reason is None and not grant_verifies(keys, doc):
                reason = "mac"
            if reason is not None:
                self._skip(snap.id, tenant_id, subject, reason)
                continue
            found = Grant.from_doc(doc)
            break
        if found is None and len(snaps) >= SCAN_LIMIT:
            log.warning("role grants exhausted the bounded read tenant=%s", tenant_id)
            raise RoleUnavailable(
                "this workspace's roles could not be read: too many grants failed "
                "verification; an operator has been alerted")
        if self._ttl > 0:
            with self._lock:
                self._cache[key] = (time.monotonic() + self._ttl, found)
        return found

    def _skip(self, doc_id: str, tenant_id: str, subject: str, reason: str) -> None:
        log.warning("role grant invalid tenant=%s reason=%s", tenant_id, reason)
        self._metrics.role_grant_invalid.labels(reason=reason).inc()
        with self._lock:
            if doc_id in self._reported:
                return
            if len(self._reported) > 100_000:
                self._reported.clear()
            self._reported.add(doc_id)
        try:
            # NO document id: a grant's id is what a worker would need to
            # overwrite it, and every member reads this list (§4.4).
            self._db.collection(AUDIT_COLLECTION).document().set({
                "action": AUDIT_GRANT_INVALID, "target_tenant_id": tenant_id,
                "by": "swarm-api", "at": self._now(),
                "detail": {"tenant_id": tenant_id, "subject": str(subject)[:254],
                           "reason": reason},
            })
        except Exception as exc:  # noqa: BLE001 - the skip stands; the row is a report
            log.warning("role grant invalid row unwritten (%s)", type(exc).__name__)

    def default_role(self, tenant_id: str, *, fresh: bool = False) -> tuple[str, Grant | None]:
        grant = self.newest_valid(tenant_id, DEFAULT_SUBJECT, fresh=fresh)
        if grant is None or grant.role not in DEFAULT_ROLES:
            return MIGRATION_DEFAULT, None
        return grant.role, grant  # type: ignore[return-value]

    def person_role(self, tenant_id: str, email: str, *, fresh: bool = False) -> Resolution:
        """What `email`'s GRANTS alone say in group workspace `tenant_id`:
        their override, else the default. Platform-admin status is not asked
        here; `resolve` adds it."""
        own = self.newest_valid(tenant_id, (email or "").strip().lower(), fresh=fresh)
        if own is not None and own.role is not None:
            return Resolution(role=own.role, source=SOURCE_OVERRIDE, grant=own)
        role, _ = self.default_role(tenant_id, fresh=fresh)
        return Resolution(role=role, source=SOURCE_DEFAULT)

    def resolve(self, auth: AuthContext, *, fresh: bool = False) -> Resolution:
        """The caller's role in the tenant the request selected."""
        if is_personal(auth):
            return Resolution(role=ADMIN, source=SOURCE_PERSONAL)
        if auth.is_admin:
            return Resolution(role=ADMIN, source=SOURCE_PLATFORM_ADMIN)
        return self.person_role(auth.tenant_id, auth.email, fresh=fresh)

    def try_resolve(self, auth: AuthContext) -> Resolution | None:
        try:
            return self.resolve(auth)
        except RoleUnavailable:
            return None

    # -- the gate other routes call -------------------------------------------

    def require(self, auth: AuthContext, needed: str, *, fresh: bool = False) -> Resolution | None:
        """The role gate on an EXISTING route (§4.9): `role_member_required`
        or `role_admin_required`, each through its switch. Returns the
        caller's role, or None when it could not be read and the switch is
        off (the request goes through, as today). `fresh` for the decision
        routes, which must see a narrowing made a moment ago."""
        if needed not in (MEMBER, ADMIN):
            return None
        error_cls = RoleMemberRequired if needed == MEMBER else RoleAdminRequired
        code = error_cls.code
        try:
            me = self.resolve(auth, fresh=fresh)
        except RoleUnavailable:
            if refusals.enforced(code):
                raise
            log.warning("role unavailable report-only code=%s tenant=%s email=%s",
                        code, auth.tenant_id, auth.email)
            return None
        if at_least(me.role, needed):
            return me
        enforced = refusals.enforced(code)
        self._metrics.role_refusals.labels(code=code, enforced=str(enforced).lower()).inc()
        if not enforced:
            log.warning("refusal report-only code=%s tenant=%s email=%s role=%s",
                        code, auth.tenant_id, auth.email, me.role)
        # The admins are named only in a 403 that is served: a report-only
        # refusal's message is logged, and a roster read on every request a
        # viewer makes would be the cost of a switch that is off.
        refusals.refuse(self.refusal(auth, me, needed, name_admins=enforced))
        return me

    def refusal(self, auth: AuthContext, me: Resolution, needed: str, *,
                name_admins: bool = True) -> Exception:
        """The 403 a role too low gets, naming up to three of the workspace's
        admins so the person knows whom to ask -- every member can read the
        roster already (Q7), so naming them is no leak."""
        admins = self.admins(auth.tenant_id) if name_admins else []
        ask = (f" Ask an admin of {auth.tenant_id} ({', '.join(admins)}) to change it."
               if admins else f" Ask an admin of {auth.tenant_id} to change it.")
        message = (f"Your role in {auth.tenant_id} is {me.role}; this needs {needed} or "
                   f"above.{ask} Nothing was changed.")
        detail = {"tenant_id": auth.tenant_id, "role": me.role, "required": needed,
                  "source": me.source, "admins": admins}
        cls = RoleMemberRequired if needed == MEMBER else RoleAdminRequired
        return cls(message, detail=detail)

    # -- the roster ---------------------------------------------------------

    def overrides(self, tenant_id: str) -> list[tuple[str, Grant]]:
        """Every person whose newest valid grant is an override, as (email,
        grant), newest first. Each person's role is the one `newest_valid`
        decides, so the roster and the gates can never disagree."""
        try:
            snaps = list(self._grants()
                         .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                         .limit(ROSTER_SCAN).stream())
        except Exception as exc:  # noqa: BLE001
            log.warning("role roster unreadable tenant=%s (%s)", tenant_id, type(exc).__name__)
            raise RoleUnavailable(
                "this workspace's roles could not be read; retry shortly") from None
        keys = self._keys()
        subjects: list[str] = []
        for snap in snaps:
            doc = snap.to_dict() or {}
            subject = doc.get("subject")
            if (not isinstance(subject, str) or subject == DEFAULT_SUBJECT
                    or subject in subjects):
                continue
            if shape_problem(doc, tenant_id, subject) is None and grant_verifies(keys, doc):
                subjects.append(subject)
            if len(subjects) >= ROSTER_MAX:
                break
        out: list[tuple[str, Grant]] = []
        for subject in subjects:
            grant = self.newest_valid(tenant_id, subject)
            if grant is not None and grant.role is not None:
                out.append((subject, grant))
        out.sort(key=lambda item: item[1].granted_at, reverse=True)
        return out

    def admins(self, tenant_id: str, limit: int = 3) -> list[str]:
        try:
            return sorted(e for e, g in self.overrides(tenant_id) if g.role == ADMIN)[:limit]
        except RoleUnavailable:
            return []

    def audit(self, tenant_id: str) -> list[dict[str, Any]]:
        """The last AUDIT_SHOWN role and platform-admin rows for this tenant."""
        try:
            snaps = list(self._db.collection(AUDIT_COLLECTION)
                         .where(filter=FieldFilter("target_tenant_id", "==", tenant_id))
                         .order_by("at", direction=firestore.Query.DESCENDING)
                         .limit(AUDIT_SCAN).stream())
        except Exception as exc:  # noqa: BLE001
            log.warning("role audit unreadable tenant=%s (%s)", tenant_id, type(exc).__name__)
            raise RoleUnavailable(
                "this workspace's role events could not be read; retry shortly") from None
        out: list[dict[str, Any]] = []
        for snap in snaps:
            row = snap.to_dict() or {}
            if row.get("action") not in WORKSPACE_AUDIT_ACTIONS:
                continue
            entry = {"action": row.get("action"), "by": row.get("by"), "at": row.get("at"),
                     "detail": dict(row.get("detail") or {})}
            if row.get("target_email"):
                entry["target_email"] = row["target_email"]
            out.append(entry)
            if len(out) >= AUDIT_SHOWN:
                break
        return out

    # -- writing --------------------------------------------------------------

    def _next_granted_at(self, previous: Grant | None) -> str:
        """Now, and strictly after the grant it supersedes: the newer write
        must sort first even on a clock that did not move."""
        when = self._now().astimezone(timezone.utc)
        if previous is not None:
            try:
                before = datetime.strptime(previous.granted_at, _AT_FORMAT).replace(
                    tzinfo=timezone.utc)
            except ValueError:
                before = None
            if before is not None and when <= before:
                when = before + timedelta(microseconds=1)
        return granted_at_text(when)

    def write(self, *, tenant_id: str, subject: str, role: str | None, by: str, via: str,
              reason: str, previous: Grant | None, action: str,
              detail: Mapping[str, Any], writer: Any = None) -> str:
        """Append one signed grant and its `admin_audit` row in ONE batch:
        both commit or neither does. With `writer` (a transaction or batch
        the caller commits) both are set on it instead, as
        `MembershipStore.write` does, so a join approval's decision,
        membership, lowered role and audit rows land in one commit or none
        do (join-requests.md §4.3). Returns the grant's `granted_at`. The
        document id is Firestore's auto-id and is returned to nobody."""
        keys = self._keys()
        granted_at = self._next_granted_at(previous)
        fields = {"tenant_id": tenant_id, "subject": subject, "role": role,
                  "granted_by": by, "granted_at": granted_at}
        doc = dict(fields, via=via, reason=reason, mac=sign_grant(keys, fields))
        row: dict[str, Any] = {
            "action": action, "target_tenant_id": tenant_id, "by": by, "at": self._now(),
            "detail": dict(detail, via=via, reason=reason),
        }
        if subject != DEFAULT_SUBJECT:
            row["target_email"] = subject
        own = writer is None
        target = self._db.batch() if own else writer
        target.set(self._grants().document(), doc)
        target.set(self._db.collection(AUDIT_COLLECTION).document(), self.audit_row(row))
        if own:
            target.commit()
        with self._lock:
            self._cache.pop((tenant_id, subject), None)
        log.info("workspace role written tenant=%s action=%s via=%s", tenant_id, action, via)
        return granted_at

    @staticmethod
    def audit_row(row: dict[str, Any]) -> dict[str, Any]:
        """The audit row as written: the seam a test injects a failure at, to
        show the grant and its row commit together or not at all."""
        return row

    # -- the platform-admin audit (§4.2.4) ------------------------------------

    def audit_platform_admin(self, auth: AuthContext, *, method: str, route: str | None,
                             path_params: Mapping[str, Any] | None = None) -> str | None:
        """Write the row a platform admin's request needs BEFORE the route
        runs, and refuse it 503 when the row cannot be written. Returns the
        action written, or None.

        * a non-member (`tenant_via == "platform_admin"`): every write a
          `platform_admin_acted` row, every read one `platform_admin_viewed`
          row per (admin, tenant, UTC hour) on this instance;
        * a member whom a grant (their override or the workspace's `*`
          default) holds below the role a write needs -- `viewer` for a
          member write, `viewer` or `member` for an admin-level one -- so
          the write passes only as a platform admin: the same; and, once a
          grant holds them below `member`, the hourly row for reads (Q12) --
          so being added to the group as a viewer, or held at `member`, is
          never a way out of the audit;
        * a member whose own role is enough, a member with no grant at all
          (everyone is `member` then and nobody `admin`, §4.2.1), and a
          personal workspace: none.

        A grant read that fails is treated as "would have refused": the row
        is written, so an unknown answer costs a row, never the audit.
        """
        if not auth.is_admin:
            return None
        target = self._target(auth, route, path_params)
        if target is None:
            return None
        tenant_id, member = target
        method = (method or "").upper()
        write = method not in _READS
        reach = "non_member"
        if member:
            needed = required_role(method, route)
            if needed is None:
                return None
            # Audited when a GRANT holds them below what the action needs
            # (§4.2.1: "no, unless a grant narrowed them below what the
            # action needs"; §4.2.4: "the grant alone would have refused
            # this route"). With no grant at all -- no override and no
            # default grant -- everyone holds `member` and nobody `admin`,
            # so an admin-level write by a member platform admin is how such
            # writes are made today and is measured against `member`; once
            # an override or a `*` default decides their role, it is
            # measured against the role the route needs, so a grant holding
            # them at `member` makes every admin-level write a row. Reads:
            # the hourly row once a grant puts them below `member`.
            try:
                mine = self.person_role(tenant_id, auth.email)
                granted = mine.source == SOURCE_OVERRIDE \
                    or self.default_role(tenant_id)[1] is not None
                own: str | None = mine.role
            except RoleUnavailable:
                granted, own = True, None
            if not write:
                floor = MEMBER
            elif granted or rank(needed) < RANK[MEMBER]:
                floor = needed
            else:
                floor = MEMBER
            if at_least(own, floor):
                return None
            reach = "role_below"
        auth = replace(auth, tenant_id=tenant_id)
        now = self._now()
        if write:
            row = {
                "action": AUDIT_PLATFORM_ADMIN_ACTED, "target_tenant_id": auth.tenant_id,
                "by": auth.email, "at": now,
                # The template and its path ids, never the concrete path with
                # its query and never the body: a secret value or a prompt
                # cannot land in the audit log.
                "detail": {"method": method, "route": route or "unknown",
                           "path_params": {str(k): str(v)
                                           for k, v in (path_params or {}).items()},
                           "via": VIA_PLATFORM_ADMIN, "reach": reach},
            }
            self._write_platform_row(row)
            return AUDIT_PLATFORM_ADMIN_ACTED
        hour = _hour(now)
        seen = (auth.email, auth.tenant_id, hour)
        with self._lock:
            if seen in self._viewed:
                return None
        row = {
            "action": AUDIT_PLATFORM_ADMIN_VIEWED, "target_tenant_id": auth.tenant_id,
            "by": auth.email, "at": now,
            "detail": {"route": route or "unknown", "hour": hour, "via": VIA_PLATFORM_ADMIN,
                       "reach": reach},
        }
        self._write_platform_row(row)
        with self._lock:
            # Only this hour's keys are worth keeping.
            self._viewed = {k for k in self._viewed if k[2] == hour}
            self._viewed.add(seen)
        return AUDIT_PLATFORM_ADMIN_VIEWED

    def _target(self, auth: AuthContext, route: str | None,
                path_params: Mapping[str, Any] | None) -> tuple[str, bool] | None:
        """(the group workspace the request acts in, whether the caller is a
        member of it), or None when it acts in no group workspace.

        The selected tenant, except on `/v1/workspaces/{tenant_id}/...`,
        whose path names the workspace it changes (merge settings, code
        intelligence, the judgment backend): there the path's tenant is the
        one acted in, whatever the header selected. A member of it is
        audited only when their own role falls short; a registered group
        workspace they are not in is audited as a non-member's; anything
        else -- their own personal workspace, another person's, an id no
        group registers -- is no group workspace."""
        named = (path_params or {}).get("tenant_id")
        if (route or "").startswith("/v1/workspaces/{tenant_id}") and isinstance(named, str) \
                and named != auth.tenant_id:
            principal = dict(auth.tenant_choices).get(named)
            if principal is not None:
                if principal.strip().lower() == (auth.email or "").strip().lower():
                    return None
                return named, True
            if named in registered_group_tenants(self._settings):
                return named, False
            return None
        if is_personal(auth):
            return None
        return auth.tenant_id, auth.tenant_via != VIA_PLATFORM_ADMIN

    def _write_platform_row(self, row: dict[str, Any]) -> None:
        try:
            self._db.collection(AUDIT_COLLECTION).document().set(row)
        except Exception as exc:  # noqa: BLE001 - fail closed on any failure
            log.warning("platform_admin audit unwritten tenant=%s action=%s (%s)",
                        row.get("target_tenant_id"), row.get("action"), type(exc).__name__)
            raise PlatformAdminAuditUnavailable(
                "this action by a platform admin could not be recorded in the workspace's "
                "audit log, so it was not taken; retry shortly") from None


# ---------------------------------------------------------------------------
# What the routes serve
# ---------------------------------------------------------------------------


def roles_table() -> list[dict[str, str]]:
    return [{"name": role, "summary": SUMMARIES[role]} for role in ROLES]


def tenant_via(auth: AuthContext) -> str:
    """`TenantChoice.via` for the tenant this request selected:
    "platform_member" for a group workspace reached by a platform membership
    (lane JOIN-WIRE), the same test `GET /v1/tenants/mine` applies. A label
    only -- the role is resolved as any member's."""
    if auth.tenant_via == VIA_PLATFORM_ADMIN:
        return VIA_PLATFORM_ADMIN
    if is_personal(auth):
        return VIA_PERSONAL
    if (auth.tenant_id, auth.tenant_principal) in auth.platform_memberships:
        return VIA_PLATFORM_MEMBER
    return VIA_MEMBER


def grant_api(email: str, grant: Grant) -> dict[str, Any]:
    """A person's override as served: never the document id, never the MAC."""
    return {"email": email, "role": grant.role, "source": SOURCE_OVERRIDE,
            "granted_by": grant.granted_by, "granted_at": grant.granted_at,
            "granted_via": grant.via, "reason": grant.reason}


def workspace_payload(store: RoleStore, auth: AuthContext) -> dict[str, Any]:
    """`GET /v1/workspace/roles` (§4.3), readable by every role (Q7)."""
    me = store.resolve(auth)
    mine: dict[str, Any] = {"email": auth.email, "role": me.role, "source": me.source,
                            "via": tenant_via(auth)}
    if me.grant is not None:
        mine.update(granted_by=me.grant.granted_by, granted_at=me.grant.granted_at)
    if is_personal(auth):
        return {"tenant_id": auth.tenant_id, "kind": "user", "default_role": None,
                "default_source": None, "me": mine, "people": [], "roles": roles_table(),
                "can_manage": False, "audit": []}
    payload = admin_payload(store, auth.tenant_id)
    payload["me"] = mine
    payload["can_manage"] = me.role == ADMIN
    return payload


def admin_payload(store: RoleStore, tenant_id: str) -> dict[str, Any]:
    default, grant = store.default_role(tenant_id)
    return {
        "tenant_id": tenant_id, "kind": "group", "default_role": default,
        "default_source": "grant" if grant is not None else "migration",
        "people": [grant_api(email, g) for email, g in store.overrides(tenant_id)],
        "roles": roles_table(),
        "audit": store.audit(tenant_id),
    }


def subject_for_write(email: Any) -> str:
    """The email a role write names: one person, never a service account."""
    subject = clean_email(email)
    if is_service_account(subject):
        raise RolePersonRequired(
            "a role is held by a person; a service account keeps the scope its listing "
            "gives it", detail={"field": "email"})
    return subject


def with_tenant(auth: AuthContext, tenant_id: str, principal: str) -> AuthContext:
    """`auth` as if it had selected (tenant_id, principal): for asking a role
    in a workspace the caller lists but did not select."""
    return replace(auth, tenant_id=tenant_id, tenant_principal=principal, tenant_via="")
