"""`GET /v1/profile`: one read for Settings › Profile, one function per section.

docs/design/user-profile.md §4.1, §4.2 (lane PROFILE-API, part of #1505).

EACH SECTION FAILS ALONE. A section that cannot be read is left out and named
in `unavailable` (`preferences`, `github`, `typesafe`, `claude_accounts`), as
`notifications.py` reports `partial`: a broker outage costs the Claude
accounts, not the page, and a failed GitHub batch is never served as "not
connected" -- that would tell a person their token is gone when it is only
unread.

EVERY SECTION IS BUILT FROM AN ALLOW-LIST. Nothing here spreads a source
document into the response, so a field a source gains tomorrow (a secret's
name, a token id) does not reach the page by default.
`tests/unit/control_plane/test_profile_no_credentials.py` plants a sentinel in
every secret-shaped field of every source and looks for it.

ONLY THE CALLER'S OWN THINGS (invariant 9). Per workspace the caller can act
in, only documents keyed by (that workspace, this email): the GitHub
connection. The TypeSafe status of the caller's own confirmed `u-` workspace
and of the acting workspace, which the API-keys page already shows them. The
Claude accounts OWNED BY or LENT TO the caller's own personal workspace. A
group's pool accounts and another member's connection are never read into the
response, and a connection document that does not name this person and this
workspace is treated as absent.

NO CLOUD IDENTITY CALL. `workspaces` is exactly `AuthContext.tenant_choices`,
which the request already resolved (§2.1: membership costs a call per group).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from swarm_common.identity import tenant_id_for_group

from .admins import ROLES
from .auth import AuthContext
from .brokerclient import AccountPool
from .display_names import check as check_display_name
from .errors import ApiError
from .forgeapp import CONNECTIONS, connection_id_for, user_hash
from .preferences import TOPICS, Preferences, StoredPreferences
from .provider_keys import TENANT, TYPESAFE, USER, ProviderKeys, resolve_provider_key
from .workspace_role import role_in
from .workspaces import READY, WORKSPACES, personal_tenant_id, state_of

log = logging.getLogger(__name__)

#: What `unavailable` may name, one per section that can fail alone.
PREFERENCES = "preferences"
GITHUB = "github"
TYPESAFE_SECTION = "typesafe"
CLAUDE_ACCOUNTS = "claude_accounts"
SECTIONS = (PREFERENCES, GITHUB, TYPESAFE_SECTION, CLAUDE_ACCOUNTS)

#: `connections` holds exactly these (Q6: SwarmCloud has no personal API keys).
CONNECTION_KEYS = ("github", "typesafe")

#: A workspace with no connection document for this person.
NOT_CONNECTED = "not_connected"

#: The connection fields served (§4.2). Never `secret_name`, `token_id`,
#: `connection_id` or `forge_user_id`.
GITHUB_FIELDS = ("state", "forge_login", "method", "access_expires_at", "refreshed_at")
#: A failure's fields: its code and the recovery copy a person acts on.
FAILURE_FIELDS = ("code", "recovery")
#: The key status fields served. Never `secret_id`, although
#: `provider_keys._status` returns one.
KEY_FIELDS = ("set", "set_at", "set_by", "version")
#: The quota windows served, by name.
WINDOWS = ("five_hour", "seven_day")

#: `default_workspace.source` (§4.2, §4.5).
SOURCE_PROFILE = "profile"
SOURCE_PERSONAL = "personal"
SOURCE_FIRST_GROUP = "first_group"
SOURCE_UNAVAILABLE = "unavailable"
#: `default_workspace.ignored`: why a stored default was not honoured. The
#: stored value is KEPT, so a membership that blinks during a Cloud Identity
#: outage does not cost the person their setting.
IGNORED_NOT_A_MEMBER = "not_a_member"
IGNORED_NOT_READY = "not_ready"
IGNORED_UNKNOWN = "unknown"


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


# -- identity ---------------------------------------------------------------------


def platform_role(app_ctx: Any, auth: AuthContext) -> str | None:
    """`owner` or `admin` from `admin_roles/{email}` (15-second cache), else
    `"admin"` for an admin-group member or listed admin, else None. Shown once,
    on the identity, and never as a workspace role."""
    if auth.is_owner:
        return "owner"
    roles = getattr(app_ctx.authenticator, "admin_roles", None)
    if roles is not None:
        try:
            role = roles.role_of(auth.email)
        except Exception as exc:  # noqa: BLE001 - an unread role is not a role
            log.warning("profile: platform role unread (%s)", type(exc).__name__)
            role = None
        if role in ROLES:
            return role
    return "admin" if auth.is_admin else None


def suggested_name(auth: AuthContext, stored: StoredPreferences | None) -> str | None:
    """The token's `name` claim after §4.4's rules 1-5, only while no name is
    stored, so the editor can offer it. None once a name is saved, when the
    preferences could not be read (whether one is stored is then unknown), and
    for a claim a rule refuses. Never shown to anyone else."""
    if stored is None or stored.display_name is not None or not auth.display_name:
        return None
    try:
        return check_display_name(auth.display_name)
    except ApiError:
        return None


def identity(app_ctx: Any, auth: AuthContext, stored: StoredPreferences | None) -> dict[str, Any]:
    return {
        "email": auth.email,
        "display_name": stored.display_name if stored is not None else None,
        "display_name_suggested": suggested_name(auth, stored),
        "domain": auth.principal.domain,
        "platform_role": platform_role(app_ctx, auth),
        "is_admin": auth.is_admin,
        "is_admin_unresolved": auth.admin_unresolved,
    }


# -- workspaces and the default ---------------------------------------------------


def own_personal(auth: AuthContext) -> str | None:
    """The caller's personal workspace when it is CONFIRMED ready: the entry of
    `tenant_choices` whose principal is the caller, which only
    `Authenticator._with_own_workspace` makes, after `PersonalTenants.ready`
    checked both its documents name this email (the frozen slug drops the
    domain, so the id alone is not enough)."""
    for tenant_id, principal in auth.tenant_choices:
        if principal == auth.email:
            return tenant_id
    return None


def default_of(
    settings: Any, auth: AuthContext, stored: StoredPreferences | None
) -> dict[str, Any]:
    """What a request naming no workspace acts in, and why (§4.5).

    The stored default when it is one of this request's `tenant_choices`, else
    today's rule: the ready personal workspace, else the first confirmed group.
    One rule with authentication's (PROFILE-DEFAULT moves an honoured default to
    the front of `tenant_choices`), so `is_default` here and the first entry of
    `GET /v1/tenants/mine` name the same workspace."""
    choices = [tenant_id for tenant_id, _ in auth.tenant_choices]
    personal = own_personal(auth)
    if stored is None:
        return {
            "tenant_id": choices[0] if choices else None,
            "source": SOURCE_UNAVAILABLE,
            "ignored": None,
        }
    wanted = stored.default_workspace
    if wanted is not None and wanted in choices:
        return {"tenant_id": wanted, "source": SOURCE_PROFILE, "ignored": None}
    ignored = None
    if wanted is not None:
        if wanted == personal_tenant_id(auth.email):
            ignored = IGNORED_NOT_READY
        elif wanted in {tenant_id_for_group(g) for g in settings.tenant_groups}:
            ignored = IGNORED_NOT_A_MEMBER
        else:
            ignored = IGNORED_UNKNOWN
    if not choices:
        return {"tenant_id": None, "source": None, "ignored": ignored}
    source = SOURCE_PERSONAL if choices[0] == personal else SOURCE_FIRST_GROUP
    return {"tenant_id": choices[0], "source": source, "ignored": ignored}


def _personal_record(db: Any, auth: AuthContext) -> dict[str, Any] | None:
    """`workspaces/u-<caller>`, only when it names this email; None when it
    does not, is absent, or cannot be read."""
    tenant_id = personal_tenant_id(auth.email)
    try:
        snap = db.collection(WORKSPACES).document(tenant_id).get()
    except Exception as exc:  # noqa: BLE001 - the row's state is then unknown
        log.warning("profile: personal workspace unread (%s)", type(exc).__name__)
        return None
    record = (snap.to_dict() or {}) if snap.exists else None
    if record is None or str(record.get("principal") or "").strip().lower() != auth.email:
        return None
    return record


def workspaces(
    db: Any, auth: AuthContext, default_tenant: str | None
) -> list[dict[str, Any]]:
    """Exactly `tenant_choices`, in its order, plus the caller's own personal
    workspace as a non-selectable row while it is not ready yet, so the page
    can say "requested" instead of hiding it."""
    personal = own_personal(auth)
    record = _personal_record(db, auth)
    rows: list[dict[str, Any]] = []
    for tenant_id, principal in auth.tenant_choices:
        is_personal = tenant_id == personal
        rows.append({
            "tenant_id": tenant_id,
            "display_name": f"Your workspace ({tenant_id})" if is_personal else principal,
            "kind": "user" if is_personal else "group",
            "role": role_in(auth, tenant_id),
            "state": (state_of(record) if record is not None else READY) if is_personal
            else None,
            "selectable": True,
            "is_acting": tenant_id == auth.tenant_id,
            "is_default": tenant_id == default_tenant,
        })
    if personal is None and record is not None:
        own = personal_tenant_id(auth.email)
        if own not in {row["tenant_id"] for row in rows}:
            rows.append({
                "tenant_id": own,
                "display_name": f"Your workspace ({own})",
                "kind": "user",
                "role": role_in(auth, own),
                "state": state_of(record),
                "selectable": False,
                "is_acting": False,
                "is_default": False,
            })
    return rows


# -- connections --------------------------------------------------------------------


def _failure(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {name: _text(value.get(name)) for name in FAILURE_FIELDS}


def github(db: Any, auth: AuthContext) -> list[dict[str, Any]]:
    """One entry for every workspace the caller can act in (Q10), from ONE
    `get_all` over `forge_connections/<connection_id_for(t, email)>`: N
    computed ids, no query. Raises when the batch fails; the caller names
    `github` in `unavailable` rather than serving "not connected"."""
    tenants = [tenant_id for tenant_id, _ in auth.tenant_choices]
    if not tenants:
        return []
    ids = {tenant_id: connection_id_for(tenant_id, auth.email) for tenant_id in tenants}
    refs = [db.collection(CONNECTIONS).document(ids[t]) for t in tenants]
    found = {snap.id: snap.to_dict() or {} for snap in db.get_all(refs) if snap.exists}
    mine = user_hash(auth.email)
    entries: list[dict[str, Any]] = []
    for tenant_id in tenants:
        doc = found.get(ids[tenant_id])
        # The id is a hash of (tenant, person): a document that names another
        # tenant or another person is a collision, never this person's.
        if doc is None or doc.get("tenant_id") != tenant_id or doc.get("user_hash") != mine:
            entries.append({"tenant_id": tenant_id, "state": NOT_CONNECTED})
            continue
        entry: dict[str, Any] = {"tenant_id": tenant_id}
        for name in GITHUB_FIELDS:
            entry[name] = _text(_iso(doc.get(name)))
        entry["failure"] = _failure(doc.get("failure"))
        entries.append(entry)
    return entries


def _key_status(status: dict[str, Any]) -> dict[str, Any]:
    return {name: status.get(name) for name in KEY_FIELDS}


def typesafe(app_ctx: Any, auth: AuthContext) -> dict[str, Any]:
    """The caller's own key and the acting workspace's, set or not, and which a
    task the caller submits there would use. Never a value, never a secret id.

    The acting workspace goes through `scope_for`, the collision guard every
    tenant-scoped read declares: two groups whose addresses slug to one id must
    not see each other's key state."""
    keys = ProviderKeys(app_ctx.db, now=app_ctx.now)
    personal = own_personal(auth)
    acting = app_ctx.submissions.scope_for(auth)
    user = keys.status(personal, TYPESAFE) if personal else None
    tenant = keys.status(acting, TYPESAFE)
    present = {s["secret_id"] for s in (user, tenant) if s is not None and s["set"]}
    chosen = resolve_provider_key(acting, personal, TYPESAFE, present)
    if chosen is None:
        uses = "none"
    else:
        uses = USER if user is not None and chosen == user["secret_id"] else TENANT
    return {
        "personal": _key_status(user) if user is not None else None,
        "acting_workspace": {"tenant_id": acting, **_key_status(tenant)},
        "uses": uses,
    }


# -- Claude accounts ------------------------------------------------------------------


def _windows(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    out: dict[str, Any] = {}
    for name in WINDOWS:
        reading = value.get(name)
        if isinstance(reading, dict):
            out[name] = {
                "utilization": reading.get("utilization"),
                "resets_at": _text(reading.get("resets_at")),
            }
    return out


def claude_accounts(pool: AccountPool, auth: AuthContext) -> dict[str, Any]:
    """The accounts the caller's own ready personal workspace owns, and the
    ones lent to it. Never a group's pool: the broker is asked about the
    personal workspace only, and an answer it gives about any other owner that
    is not lent to it is dropped. `lend_to` is served as a count, on the
    caller's own accounts only. Raises when the broker fails."""
    personal = own_personal(auth)
    if personal is None:
        return {"personal_workspace": None, "accounts": [], "unreadable_document_count": 0}
    payload = pool.list_accounts(personal)
    accounts: list[dict[str, Any]] = []
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        lend_to = account.get("lend_to")
        lend_to = lend_to if isinstance(lend_to, list) else []
        if account.get("owner_tenant") == personal:
            relation = "own"
        elif personal in lend_to:
            relation = "borrowed"
        else:
            continue
        accounts.append({
            "account_id": _text(account.get("account_id")),
            "label": _text(account.get("label")),
            "relation": relation,
            "state": _text(account.get("state")),
            "windows": _windows(account.get("windows")),
            "lent_to_count": len(lend_to) if relation == "own" else None,
            "stale": bool(account.get("stale", True)),
        })
    unreadable = payload.get("unreadable_document_count")
    return {
        "personal_workspace": personal,
        "accounts": accounts,
        "unreadable_document_count": unreadable
        if isinstance(unreadable, int) and not isinstance(unreadable, bool) else 0,
    }


# -- notifications ----------------------------------------------------------------------


def notifications(stored: StoredPreferences) -> dict[str, Any]:
    """The saved bell settings and every topic, each of which may be muted (Q3)."""
    return {
        "preferences": {"muted": list(stored.muted), "reminder": stored.reminder},
        "topics": list(TOPICS),
    }


# -- the whole page ---------------------------------------------------------------------


def _section(name: str, unavailable: list[str], build: Any) -> Any:
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - a section fails alone
        log.warning("profile: %s unavailable (%s)", name, type(exc).__name__)
        unavailable.append(name)
        return None


def read_profile(
    app_ctx: Any, auth: AuthContext, preferences: Preferences, pool: AccountPool
) -> dict[str, Any]:
    """`GET /v1/profile` for the verified caller, and only for them."""
    unavailable: list[str] = []
    stored = _section(PREFERENCES, unavailable, lambda: preferences.read(auth.email))
    default = default_of(app_ctx.settings, auth, stored)
    body: dict[str, Any] = {
        "identity": identity(app_ctx, auth, stored),
        "workspaces": workspaces(app_ctx.db, auth, default["tenant_id"]),
        "default_workspace": default,
    }
    connections: dict[str, Any] = {}
    gh = _section(GITHUB, unavailable, lambda: github(app_ctx.db, auth))
    if gh is not None:
        connections["github"] = gh
    ts = _section(TYPESAFE_SECTION, unavailable, lambda: typesafe(app_ctx, auth))
    if ts is not None:
        connections["typesafe"] = ts
    body["connections"] = connections
    if stored is not None:
        body["notifications"] = notifications(stored)
    accounts = _section(CLAUDE_ACCOUNTS, unavailable, lambda: claude_accounts(pool, auth))
    if accounts is not None:
        body["claude_accounts"] = accounts
    body["revision"] = stored.revision if stored is not None else None
    body["unavailable"] = unavailable
    return body


def saved_profile(
    app_ctx: Any, auth: AuthContext, stored: StoredPreferences
) -> dict[str, Any]:
    """What `PUT /v1/profile/preferences` answers: the GET's
    `identity.display_name`, `default_workspace`, `notifications` and
    `revision`, from the document the save wrote."""
    return {
        "identity": {
            "display_name": stored.display_name,
            "display_name_suggested": suggested_name(auth, stored),
        },
        "default_workspace": default_of(app_ctx.settings, auth, stored),
        "notifications": notifications(stored),
        "revision": stored.revision,
    }
