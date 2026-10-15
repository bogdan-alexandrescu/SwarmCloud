"""The workspace secrets store: records, grants and their management (SEC-STORE-API).

Design: docs/design/workspace-secrets.md §2 (the record and the grant) and §5
(the management API), phase 1 of #1347. The release route that hands a
granted secret to a worker is phase 2 (SEC-RELEASE) and is NOT here: nothing
in this module or its routes reads a payload back.

Owner decisions this module implements (the lane brief, 2026-10-15):

  * Q3. A PERSONAL secret (one in a `u-<user>` workspace) carries an opt-in
    list, "use in my submissions to <workspace>": the group workspaces whose
    tasks THAT person submits may be released it. Nobody else's tasks, and
    only workspaces the person is a confirmed member of when they tick it.
  * Q4. In a GROUP workspace only platform admins write; members read names
    and grants. A personal workspace is managed by its person.
  * Q5. Rotate keeps the previous version ENABLED for `ROTATION_GRACE`
    (24 h), so a running attempt that read it keeps working; then
    `sweep_rotations` DISABLES it. It never destroys one: a disabled version
    is one `versions enable` away from use again. Only a delete destroys.
  * Q7. The runner provider keys and the pool accounts are LISTED, read-only,
    beside the store's own secrets, never their values (`legacy_rows`).

What keeps a value out of everything but Secret Manager:

  * The value is a local of `create`/`rotate` and goes to exactly one call,
    `SecretWriter.add_version`. No record, audit row, log line, response or
    error message names it, its length, a prefix or a suffix; a refusal of a
    malformed value says what is wrong with its SHAPE and never echoes it (or
    a key of a structured value, which a person can paste a value into).
  * `SecretManagerWorkspaceSecrets` has NO read path and NO IAM write. The
    tenant's worker account is never bound to a `-wsec-` secret (§3.3: the
    agent can mint that account's token, so a binding would make every
    checkbox advisory). Only swarm-api reads, in phase 2, under the
    conditioned grant SEC-IAM writes.

What keeps one workspace out of another's:

  * No route takes a tenant. Every route acts on `tenant_scope`, the caller's
    selected tenant through the collision guard; a secret is looked up by
    `(tenant_id, name)` in `workspace_secret_names`, so another workspace's
    secret is a 404 whatever its name, and the record's own `tenant_id` is
    checked again before anything is returned or written.

The consumer catalogue (§2.3) lives at the end of this module. Profiles come
from the frozen `RUNNER_PROFILES`; tools and components are listed here until
SEC-CONTRACT's request (design §7, requests A-C) moves them into
`swarm_common`, at which point this list is deleted in favour of it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets as _random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from .admins import AUDIT_COLLECTION
from .auth import AuthContext, require_admin
from .codec import console_origin
from .errors import Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed

log = logging.getLogger(__name__)

#: One document per secret, keyed by its Secret Manager secret id. Never the value.
SECRETS_COLLECTION = "workspace_secrets"
#: `{tenant_id}~{name}` -> secret id: what makes a name unique in a workspace
#: and what every route looks a secret up by, so a lookup is tenant-bounded.
NAMES_COLLECTION = "workspace_secret_names"
#: One document per (secret, consumer), `{secret_id}~{consumer}`. Never deleted.
GRANTS_COLLECTION = "workspace_secret_grants"
#: One document per minted form link, keyed by sha256(token). Never the token.
LINKS_COLLECTION = "workspace_secret_links"

#: Q5: how long the previous version stays enabled after a rotation. A day is
#: longer than any attempt's run time, so an attempt released the old value
#: just before the rotation finishes on it; then the sweep disables it.
ROTATION_GRACE = timedelta(hours=24)
#: The form link's life, as provider keys' (`provider_keys.KEY_LINK_TTL`).
LINK_TTL = timedelta(minutes=10)
#: How many disabled or destroyed versions a record keeps listing. An ENABLED
#: version is never trimmed: the sweep finds what to disable from this list.
MAX_LISTED_VERSIONS = 10
#: Q3's list, bounded so a record cannot grow without limit.
MAX_USE_IN = 20

ACTIVE = "active"
DELETED = "deleted"
ENABLED = "enabled"
DISABLED = "disabled"
DESTROYED = "destroyed"

#: `scope` (§2.1): a group's secret is the workspace's; a personal tenant's is its person's.
SCOPE_WORKSPACE = "workspace"
SCOPE_PERSONAL = "personal"

AUDIT_CREATED = "workspace_secret_created"
AUDIT_ROTATED = "workspace_secret_rotated"
AUDIT_DELETED = "workspace_secret_deleted"
AUDIT_GRANT_ENABLED = "workspace_secret_grant_enabled"
AUDIT_GRANT_DISABLED = "workspace_secret_grant_disabled"
AUDIT_USE_IN_SET = "workspace_secret_use_in_set"
AUDIT_VERSION_DISABLED = "workspace_secret_version_disabled"

#: The display name: it becomes a file name in phase 2 (§4.1), so it is
#: lowercase letters, digits and hyphens, and starts with a letter or digit.
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

#: `provider` (§2.1) drives the console's help text, never behaviour. NOT
#: `github`: a forge token is never readable by an agent (owner rule
#: 2026-09-25), and a store whose only use is to hand a value to an agent's
#: profile or tool must not offer to hold one. Forge tokens are listed by
#: SEC-MIGRATE-FORGE from their own records, never written here.
PROVIDERS = ("anthropic", "brave", "openai", "typesafe", "other")

#: `kind` (§2.1). Unpacked from one tuple rather than one `NAME = "..."`
#: line each: the worker's publish-time credential scan reads a line like
#: that as a key assignment.
KINDS = ("api_key", "bearer_token", "basic_auth", "oauth_client", "opaque")
API_KEY, BEARER_TOKEN, BASIC_AUTH, OAUTH_CLIENT, OPAQUE = KINDS
#: The structured kinds' exact keys (§2.1): the value is a JSON object of these.
STRUCTURED_FIELDS: dict[str, tuple[str, ...]] = {
    BASIC_AUTH: ("username", "password"),
    OAUTH_CLIENT: ("client_id", "client_secret"),
}
#: Kinds whose value is one line: a header value cannot carry a newline.
SINGLE_LINE = (API_KEY, BEARER_TOKEN)
#: The store's shape check (`credentials.validate_key_material`'s bounds):
#: under 8 bytes is not a credential; over 8 KiB is a file, not a key.
MIN_VALUE_BYTES = 8
MAX_VALUE_BYTES = 8 * 1024
MAX_FIELD_CHARS = 4096


class SecretLinkInvalid(Forbidden):
    """A secrets-form link that is unknown, used, expired, or minted for
    another person, workspace, action or secret. Refused outright: these
    routes are new, so there is no earlier behaviour a switch would keep
    (provider keys' `KeyLinkInvalid` ships behind one for that reason)."""

    code = "SECRET_LINK_INVALID"


# ---------------------------------------------------------------------------
# Secret Manager: write-only, no IAM
# ---------------------------------------------------------------------------


class SecretWriter(Protocol):
    def create_secret(self, tenant_id: str, secret_id: str) -> None: ...

    def add_version(self, secret_id: str, payload: bytes) -> str: ...

    def disable_version(self, version: str) -> None: ...

    def destroy_versions(self, secret_id: str) -> int: ...


class SecretManagerWorkspaceSecrets:
    """The store's Secret Manager writes, and nothing else.

    NO READ PATH: no method returns payload bytes. NO IAM WRITE: unlike
    `credentials.SecretManagerCredentials`, nothing here binds the tenant's
    worker account (or anyone) to the secret -- the design's whole
    enforcement rests on that account holding no read on a `-wsec-` secret
    (§3.3). The permissions it uses are swarm-api's existing
    `provider_key_writer` set on `swarm-tenant-*` (secrets.create,
    versions.add, versions.list, versions.destroy), plus
    `secretmanager.versions.disable` for the rotation sweep, which SEC-IAM
    adds. Builds no client until the first call, so `create_app()` needs no
    credentials.
    """

    def __init__(self, project_id: str, *, client: Any | None = None,
                 replication_locations: tuple[str, ...] = ()) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._project_id = project_id
        self._client = client
        self._replication_locations = tuple(replication_locations)

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self._project_id}"

    def create_secret(self, tenant_id: str, secret_id: str) -> None:
        from google.api_core import exceptions as gexc

        replication: dict[str, Any] = (
            {"user_managed": {"replicas": [{"location": loc}
                                           for loc in self._replication_locations]}}
            if self._replication_locations else {"automatic": {}}
        )
        try:
            self._secret_client().create_secret(request={
                "parent": self.parent,
                "secret_id": secret_id,
                "secret": {
                    "replication": replication,
                    "labels": {"managed-by": "swarm-api", "tenant": tenant_id,
                               "purpose": "workspace-secret"},
                },
            })
        except gexc.AlreadyExists:
            # The id carries 64 random bits; a collision is a retry of this
            # very create, whose secret is empty and ours.
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(
                f"could not create the workspace secret: {exc.message}") from None

    def add_version(self, secret_id: str, payload: bytes) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        name = f"{self.parent}/secrets/{secret_id}"
        try:
            version = self._secret_client().add_secret_version(request={
                "parent": name, "payload": secretmanager.SecretPayload(data=payload),
            })
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not store the value: {exc.message}") from None
        return str(getattr(version, "name", "") or f"{name}/versions/latest")

    def disable_version(self, version: str) -> None:
        from google.api_core import exceptions as gexc

        try:
            self._secret_client().disable_secret_version(request={"name": version})
        except gexc.FailedPrecondition:
            # Already disabled or destroyed: the state the sweep wants.
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(
                f"could not disable a previous version: {exc.message}") from None

    def destroy_versions(self, secret_id: str) -> int:
        from google.api_core import exceptions as gexc

        client = self._secret_client()
        name = f"{self.parent}/secrets/{secret_id}"
        destroyed = 0
        try:
            for version in list(client.list_secret_versions(
                    request={"parent": name, "filter": "state:ENABLED OR state:DISABLED"})):
                client.destroy_secret_version(request={"name": version.name})
                destroyed += 1
        except gexc.NotFound:
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not destroy the secret: {exc.message}") from None
        return destroyed


# ---------------------------------------------------------------------------
# The shape check: never echoes the value
# ---------------------------------------------------------------------------


def check_name(name: str) -> str:
    text = (name or "").strip()
    if not NAME_PATTERN.fullmatch(text):
        # Not echoed: a person can paste anything into a name field.
        raise ValidationFailed(
            "a secret's name is 1-64 characters of a-z, 0-9 and '-', starting with "
            "a letter or a digit")
    return text


def check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValidationFailed("unknown provider", detail={"providers": list(PROVIDERS)})
    return provider


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValidationFailed("unknown kind", detail={"kinds": list(KINDS)})
    return kind


def payload_for(kind: str, value: str | None, fields: Mapping[str, Any] | None) -> bytes:
    """The bytes to store for `kind`, or a refusal that names the SHAPE only.

    Every message here is a constant: none interpolates the value, a field's
    value, a key of `fields`, or a length."""
    check_kind(kind)
    if kind in STRUCTURED_FIELDS:
        expected = STRUCTURED_FIELDS[kind]
        if value is not None or fields is None:
            raise ValidationFailed(
                f"a {kind} secret is given as `fields` ({', '.join(expected)}), not `value`")
        if set(fields) != set(expected):
            raise ValidationFailed(f"`fields` must hold exactly {', '.join(expected)}")
        for key in expected:
            text = fields[key]
            if not isinstance(text, str) or not text or len(text) > MAX_FIELD_CHARS \
                    or "\x00" in text or "\n" in text or "\r" in text:
                raise ValidationFailed(
                    f"`fields.{key}` must be one line of 1-{MAX_FIELD_CHARS} characters "
                    "with no NUL")
        data = json.dumps({k: fields[k] for k in expected}, sort_keys=True).encode("utf-8")
    else:
        if fields is not None or value is None:
            raise ValidationFailed(f"a {kind} secret is given as `value`, not `fields`")
        if "\x00" in value:
            raise ValidationFailed("the value must not contain a NUL byte")
        if kind in SINGLE_LINE and ("\n" in value or "\r" in value):
            raise ValidationFailed(f"a {kind} value is a single line")
        data = value.encode("utf-8")
    if len(data) < MIN_VALUE_BYTES:
        raise ValidationFailed("the value is too short to be a credential")
    if len(data) > MAX_VALUE_BYTES:
        raise ValidationFailed(f"the value exceeds {MAX_VALUE_BYTES} bytes; that is a file, not a key")
    return data


def refuse_name_in_value(name: str, payload: bytes) -> None:
    """A name that is part of its own value is a value pasted into the name
    field, and the name is served, logged and audited everywhere. Refused
    with a constant message, before anything is stored."""
    if len(name) >= MIN_VALUE_BYTES and name.encode("utf-8") in payload:
        raise ValidationFailed(
            "the name is part of the value: the value was pasted into the name field. "
            "Choose a name that says what the secret is for")


# ---------------------------------------------------------------------------
# Who may manage
# ---------------------------------------------------------------------------


def is_personal(tenant: Tenant) -> bool:
    return tenant.kind == "user"


def require_manager(auth: AuthContext, tenant: Tenant) -> None:
    """Q4. A personal workspace: its person, by the tenant document's
    principal (the frozen slug drops the domain, so the id alone does not say
    whose it is). A group workspace: a full platform admin (`require_admin`
    with no route, so a pool admin's narrow capability never reaches here).
    Members of a group read; they never write."""
    if is_personal(tenant):
        email = (auth.email or "").strip().lower()
        if not email or (tenant.principal or "").strip().lower() != email:
            raise Forbidden("a personal workspace's secrets are managed by its person only")
        return
    try:
        require_admin(auth)
    except Forbidden:
        raise Forbidden(
            "in a group workspace only platform admins add, rotate, grant or delete "
            "secrets; members see names and grants") from None


def can_manage(auth: AuthContext, tenant: Tenant) -> bool:
    try:
        require_manager(auth, tenant)
    except Forbidden:
        return False
    return True


# ---------------------------------------------------------------------------
# The consumer catalogue (§2.3, §2.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    name: str
    kinds: tuple[str, ...]
    means: str


@dataclass(frozen=True)
class Consumer:
    kind: str
    name: str
    slots: tuple[Slot, ...]
    #: Why a consumer with no slot takes no grant, or why it is locked.
    note: str = ""
    #: `component:git` (§6.2): shown, never grantable here.
    locked: bool = False

    @property
    def consumer(self) -> str:
        return f"{self.kind}:{self.name}"

    def slot(self, name: str | None) -> Slot | None:
        if name is None:
            return self.slots[0] if len(self.slots) == 1 else None
        return next((s for s in self.slots if s.name == name), None)

    def to_api(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer, "kind": self.kind, "name": self.name,
            "slots": [{"slot": s.name, "kinds": list(s.kinds), "means": s.means}
                      for s in self.slots],
            "grantable": bool(self.slots) and not self.locked,
            "note": self.note,
        }


#: Every agent profile: one that starts a runner (no `worker_action`) and
#: holds a provider (so not `mock` or `generic`, which run no agent). Read
#: from the frozen catalogue so a profile added there is offered here.
PROFILE_NAMES: tuple[str, ...] = tuple(sorted(
    name for name, profile in RUNNER_PROFILES.items()
    if profile.worker_action is None and profile.provider is not None
))

#: The frozen `browser_tool` input's choices (claude-code-browser), but `auto`,
#: to the program each launches. `jev` is spelled `jev-browser` because that is
#: the program the secret reaches (§2.3). Held equal to the contract's choices
#: by tests/unit/control_plane/test_workspace_secrets.py.
BROWSER_TOOLS: dict[str, str] = {
    "jev": "jev-browser",
    "dev-browser": "dev-browser",
    "playwright": "playwright",
}

_FILES = Slot("files", KINDS, "one file per secret, named in the attempt's secrets manifest")
_TYPESAFE = Slot("typesafe", (API_KEY,), "the TypeSafe key")


def _catalogue() -> tuple[Consumer, ...]:
    """THESE MOVE INTO THE CONTRACT once SEC-CONTRACT's request is applied
    (design §7, requests A-C: `RunnerProfile.secret_slots`, `RUNTIME_TOOLS`,
    `PLATFORM_COMPONENTS`); until then the tools, the components and every
    slot are declared here and nowhere else."""
    profiles = [Consumer("profile", name, (_FILES,)) for name in PROFILE_NAMES]
    no_slot = "declares no secret slot yet; a grant would deliver nothing"
    tools = [
        Consumer("tool", "jev-browser", (_TYPESAFE,)),
        Consumer("tool", "dev-browser", (), note=no_slot),
        Consumer("tool", "playwright", (), note=no_slot),
    ]
    components = [
        Consumer("component", "judge", (_TYPESAFE,)),
        Consumer("component", "merge", (), note=no_slot),
        Consumer("component", "indexer", (), note=no_slot),
        Consumer("component", "git", (Slot("forge", ("forge_token",), "the forge token"),),
                 note="forge tokens only, never granted from this page (design §6.2)",
                 locked=True),
    ]
    return tuple(profiles + tools + components)


CONSUMERS: tuple[Consumer, ...] = _catalogue()
CONSUMERS_BY_NAME: dict[str, Consumer] = {c.consumer: c for c in CONSUMERS}


def check_grant(consumer: str, slot: str | None, kind: str) -> tuple[Consumer, Slot]:
    """The consumer and slot a grant names, or a 422 that says which rule."""
    entry = CONSUMERS_BY_NAME.get(consumer)
    if entry is None:
        raise ValidationFailed(
            "unknown consumer: a grant names a runner profile, a tool or a component "
            "from the catalogue (GET /v1/workspace/secrets/consumers), never an image "
            "or a command (CONTRACT.md invariant 10)",
            detail={"consumers": sorted(CONSUMERS_BY_NAME)})
    if entry.locked:
        raise ValidationFailed(f"{consumer} is not grantable here: {entry.note}")
    if not entry.slots:
        raise ValidationFailed(f"{consumer} {entry.note}")
    chosen = entry.slot(slot)
    if chosen is None:
        raise ValidationFailed(
            f"{consumer} has no such slot",
            detail={"slots": [s.name for s in entry.slots]})
    if kind not in chosen.kinds:
        raise ValidationFailed(
            f"{consumer} reads {', '.join(chosen.kinds)} in its {chosen.name} slot, "
            f"not a {kind}")
    return entry, chosen


# ---------------------------------------------------------------------------
# Q7: the platform's other credentials, listed read-only
# ---------------------------------------------------------------------------


#: The fields a pool account row may carry onto the Secrets page: an
#: ALLOW-LIST, so a broker field added later is not forwarded by accident.
POOL_ACCOUNT_FIELDS = ("account_id", "owner_tenant", "label", "provider", "state")


def legacy_rows(tenant: Tenant, pool_payload: Mapping[str, Any] | None,
                pool_error: str | None) -> dict[str, Any]:
    """The runner provider keys and pool accounts, never a value (Q7: listed
    read-only now, not moved). Their writers are unchanged: Settings › API
    keys and the account pool."""
    keys = []
    for provider in sorted(set(tenant.credentials or [])):
        keys.append({
            "provider": provider,
            "secret_id": tenant.secret_name(provider),
            "managed": "legacy",
            "read_only": True,
            "consumers": [f"profile:{name}" for name, profile in sorted(RUNNER_PROFILES.items())
                          if profile.provider == provider and profile.worker_action is None],
        })
    if pool_error is not None:
        accounts: dict[str, Any] = {"readable": False, "reason": pool_error, "accounts": []}
    else:
        listed = [a for a in (pool_payload or {}).get("accounts") or [] if isinstance(a, dict)]
        accounts = {
            "readable": True,
            "accounts": [{**{f: a.get(f) for f in POOL_ACCOUNT_FIELDS},
                          "managed": "legacy", "read_only": True} for a in listed],
        }
    return {"runner_provider_keys": keys, "pool_accounts": accounts}


# ---------------------------------------------------------------------------
# Records, grants, audit
# ---------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _grant_id(secret_id: str, consumer: str) -> str:
    return f"{secret_id}~{consumer}"


def _name_id(tenant_id: str, name: str) -> str:
    return f"{tenant_id}~{name}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _trim(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first; every enabled version kept, the rest to the listing cap."""
    kept: list[dict[str, Any]] = []
    for entry in versions:
        if entry.get("state") == ENABLED or len(kept) < MAX_LISTED_VERSIONS:
            kept.append(entry)
    return kept


def _version_api(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": entry.get("version"),
        "added_at": _iso(entry.get("added_at")),
        "added_by": entry.get("added_by"),
        "state": entry.get("state"),
        "disable_after": _iso(entry.get("disable_after")),
    }


def record_api(doc: Mapping[str, Any], grants: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """A record as the API serves it: an ALLOW-LIST of fields, so nothing a
    later writer adds to the document is served by accident. No value."""
    return {
        "name": doc.get("name"),
        "secret_id": doc.get("secret_id"),
        "provider": doc.get("provider"),
        "kind": doc.get("kind"),
        "scope": doc.get("scope"),
        "managed": doc.get("managed"),
        "current_version": doc.get("current_version"),
        "versions": [_version_api(v) for v in doc.get("versions") or []],
        "created_at": _iso(doc.get("created_at")),
        "created_by": doc.get("created_by"),
        "rotated_at": _iso(doc.get("rotated_at")),
        "rotated_by": doc.get("rotated_by"),
        "use_in_workspaces": list(doc.get("use_in_workspaces") or []),
        "grants": sorted(
            ({"consumer": g.get("consumer"), "slot": g.get("slot"),
              "enabled": bool(g.get("enabled")), "changed_at": _iso(g.get("changed_at")),
              "changed_by": g.get("changed_by")} for g in grants),
            key=lambda g: str(g["consumer"])),
    }


class WorkspaceSecrets:
    """`workspace_secrets/`, its name index, grants and links; every write
    with its `admin_audit` row in the same Firestore transaction."""

    def __init__(self, db: Any, writer: SecretWriter, *, now: Any) -> None:
        self._db = db
        self._writer = writer
        self._now = now

    # -- reads -------------------------------------------------------------------

    def _audit(self, txn: Any, action: str, *, by: str, at: datetime, secret_id: str,
               detail: dict[str, Any]) -> None:
        txn.set(self._db.collection(AUDIT_COLLECTION).document(), {
            "action": action, "target_secret_id": secret_id, "by": by, "at": at,
            "detail": detail,
        })

    def find(self, tenant_id: str, name: str) -> dict[str, Any]:
        """The ACTIVE record named `name` in `tenant_id`, or 404. Bounded by the
        tenant twice: the index is keyed by it and the record must carry it."""
        index = self._db.collection(NAMES_COLLECTION).document(_name_id(tenant_id, name)).get()
        secret_id = (index.to_dict() or {}).get("secret_id") if index.exists else None
        if secret_id:
            snap = self._db.collection(SECRETS_COLLECTION).document(secret_id).get()
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") == tenant_id and doc.get("state") == ACTIVE:
                return doc
        raise NotFound("no secret by that name in this workspace")

    def grants_of(self, secret_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for secret_id in secret_ids:
            query = self._db.collection(GRANTS_COLLECTION).where(
                filter=FieldFilter("secret_id", "==", secret_id))
            out[secret_id] = [s.to_dict() or {} for s in query.stream()]
        return out

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        query = self._db.collection(SECRETS_COLLECTION) \
            .where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
            .where(filter=FieldFilter("state", "==", ACTIVE))
        docs = [s.to_dict() or {} for s in query.stream()]
        docs = [d for d in docs if d.get("tenant_id") == tenant_id]
        grants = self.grants_of(d["secret_id"] for d in docs)
        return [record_api(d, grants.get(d["secret_id"], []))
                for d in sorted(docs, key=lambda d: str(d.get("name")))]

    # -- create, rotate, delete --------------------------------------------------

    def create(self, *, tenant: Tenant, name: str, provider: str, kind: str,
               payload: bytes, by: str, via_link: bool) -> dict[str, Any]:
        tenant_id = tenant.tenant_id
        name_ref = self._db.collection(NAMES_COLLECTION).document(_name_id(tenant_id, name))
        if name_ref.get().exists:
            # Before the value reaches Secret Manager: a refused write stores nothing.
            raise Conflict("a secret by that name already exists in this workspace")
        secret_id = tenant.secret_name(f"wsec-{_random.token_hex(8)}")
        self._writer.create_secret(tenant_id, secret_id)
        version = self._writer.add_version(secret_id, payload)
        payload = b""
        now = self._now()
        doc = {
            "secret_id": secret_id, "tenant_id": tenant_id, "name": name,
            "provider": provider, "kind": kind,
            "scope": SCOPE_PERSONAL if is_personal(tenant) else SCOPE_WORKSPACE,
            "managed": "store", "state": ACTIVE, "current_version": version,
            "versions": [{"version": version, "added_at": now, "added_by": by,
                          "state": ENABLED, "disable_after": None}],
            "created_at": now, "created_by": by, "rotated_at": None, "rotated_by": None,
            "use_in_workspaces": [],
        }
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)

        @firestore.transactional
        def _write(txn: Any) -> None:
            if _snapshot(txn.get(name_ref)).exists:
                raise Conflict("a secret by that name already exists in this workspace")
            txn.set(ref, doc)
            txn.set(name_ref, {"tenant_id": tenant_id, "name": name, "secret_id": secret_id})
            self._audit(txn, AUDIT_CREATED, by=by, at=now, secret_id=secret_id, detail={
                "tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                "provider": provider, "kind": kind, "version": version, "via_link": via_link})

        try:
            _write(self._db.transaction())
        except Conflict:
            # Lost a race on the name: the version just added is nobody's.
            self._writer.destroy_versions(secret_id)
            raise
        log.info("workspace secret created tenant=%s secret=%s name=%s kind=%s",
                 tenant_id, secret_id, name, kind)
        return record_api(doc, [])

    def rotate(self, *, tenant_id: str, name: str, kind_payload: Any, by: str,
               via_link: bool) -> dict[str, Any]:
        """Q5: add a version, make it current, and schedule the previous one to
        be DISABLED by the sweep after `ROTATION_GRACE` -- enabled until then,
        so an attempt already holding it is not broken mid-run."""
        found = self.find(tenant_id, name)
        secret_id = found["secret_id"]
        payload = kind_payload(found["kind"])
        version = self._writer.add_version(secret_id, payload)
        payload = b""
        now = self._now()
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)

        @firestore.transactional
        def _write(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE:
                raise NotFound("no secret by that name in this workspace")
            previous = doc.get("current_version")
            versions = []
            for entry in doc.get("versions") or []:
                entry = dict(entry)
                if entry.get("version") == previous and entry.get("state") == ENABLED:
                    entry["disable_after"] = now + ROTATION_GRACE
                versions.append(entry)
            versions.insert(0, {"version": version, "added_at": now, "added_by": by,
                                "state": ENABLED, "disable_after": None})
            doc.update(current_version=version, versions=_trim(versions),
                       rotated_at=now, rotated_by=by)
            txn.set(ref, doc)
            self._audit(txn, AUDIT_ROTATED, by=by, at=now, secret_id=secret_id, detail={
                "tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                "provider": doc.get("provider"), "kind": doc.get("kind"), "version": version,
                "previous_version": previous,
                "previous_disable_after": _iso(now + ROTATION_GRACE) if previous else None,
                "via_link": via_link})
            return doc

        doc = _write(self._db.transaction())
        log.info("workspace secret rotated tenant=%s secret=%s name=%s", tenant_id, secret_id, name)
        return record_api(doc, self.grants_of([secret_id]).get(secret_id, []))

    def delete(self, *, tenant_id: str, name: str, by: str) -> dict[str, Any]:
        """Destroy every version, disable every grant, free the name; the
        record stays (state `deleted`) so its audit trail has something to
        name. Destroy, not disable: a person who deletes means it gone."""
        found = self.find(tenant_id, name)
        secret_id = found["secret_id"]
        destroyed = self._writer.destroy_versions(secret_id)
        now = self._now()
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)
        name_ref = self._db.collection(NAMES_COLLECTION).document(_name_id(tenant_id, name))
        grants = self.grants_of([secret_id]).get(secret_id, [])
        disabled = sorted(str(g.get("consumer")) for g in grants if g.get("enabled"))

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE:
                raise NotFound("no secret by that name in this workspace")
            versions = [{**v, "state": DESTROYED} if v.get("state") != DESTROYED else dict(v)
                        for v in doc.get("versions") or []]
            txn.set(ref, {**doc, "state": DELETED, "versions": versions,
                          "deleted_at": now, "deleted_by": by})
            txn.delete(name_ref)
            for consumer in disabled:
                txn.set(self._db.collection(GRANTS_COLLECTION)
                        .document(_grant_id(secret_id, consumer)),
                        {"enabled": False, "changed_at": now, "changed_by": by}, merge=True)
            self._audit(txn, AUDIT_DELETED, by=by, at=now, secret_id=secret_id, detail={
                "tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                "provider": doc.get("provider"), "kind": doc.get("kind"),
                "version": doc.get("current_version"), "versions_destroyed": destroyed,
                "grants_disabled": disabled})

        _write(self._db.transaction())
        log.info("workspace secret deleted tenant=%s secret=%s name=%s versions=%d",
                 tenant_id, secret_id, name, destroyed)
        return {"name": name, "secret_id": secret_id, "versions_destroyed": destroyed,
                "grants_disabled": disabled}

    # -- grants and Q3's list ------------------------------------------------------

    def set_grant(self, *, tenant_id: str, name: str, consumer: str, slot: str | None,
                  enabled: bool, by: str) -> dict[str, Any]:
        found = self.find(tenant_id, name)
        secret_id = found["secret_id"]
        _, chosen = check_grant(consumer, slot, str(found.get("kind")))
        now = self._now()
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)
        grant_ref = self._db.collection(GRANTS_COLLECTION).document(_grant_id(secret_id, consumer))
        grant = {"secret_id": secret_id, "tenant_id": tenant_id, "consumer": consumer,
                 "slot": chosen.name, "enabled": enabled, "changed_at": now, "changed_by": by}

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE:
                raise NotFound("no secret by that name in this workspace")
            txn.set(grant_ref, grant)
            self._audit(txn, AUDIT_GRANT_ENABLED if enabled else AUDIT_GRANT_DISABLED,
                        by=by, at=now, secret_id=secret_id, detail={
                            "tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                            "consumer": consumer, "slot": chosen.name})

        _write(self._db.transaction())
        log.info("workspace secret grant tenant=%s secret=%s consumer=%s enabled=%s",
                 tenant_id, secret_id, consumer, enabled)
        return record_api(found, self.grants_of([secret_id]).get(secret_id, []))

    def set_use_in(self, *, tenant_id: str, name: str, workspaces: list[str],
                   by: str) -> dict[str, Any]:
        found = self.find(tenant_id, name)
        if found.get("scope") != SCOPE_PERSONAL:
            raise ValidationFailed(
                "only a personal secret carries a 'use in my submissions to' list; a "
                "group workspace's secret is that workspace's already")
        secret_id = found["secret_id"]
        now = self._now()
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)

        @firestore.transactional
        def _write(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE:
                raise NotFound("no secret by that name in this workspace")
            before = list(doc.get("use_in_workspaces") or [])
            doc["use_in_workspaces"] = workspaces
            txn.set(ref, doc)
            self._audit(txn, AUDIT_USE_IN_SET, by=by, at=now, secret_id=secret_id, detail={
                "tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                "before": before, "after": workspaces})
            return doc

        doc = _write(self._db.transaction())
        return record_api(doc, self.grants_of([secret_id]).get(secret_id, []))

    # -- Q5's sweep ------------------------------------------------------------------

    def sweep_rotations(self, *, by: str) -> dict[str, Any]:
        """Disable every previous version whose grace has run out. Never
        destroys. One Secret Manager call and one transaction (the record and
        its audit row) per version; a failure is counted and retried by the
        next sweep, never skipped silently."""
        now = self._now()
        query = self._db.collection(SECRETS_COLLECTION).where(
            filter=FieldFilter("state", "==", ACTIVE))
        examined = disabled = 0
        failed: list[str] = []
        for snap in query.stream():
            examined += 1
            doc = snap.to_dict() or {}
            secret_id = str(doc.get("secret_id") or "")
            for entry in doc.get("versions") or []:
                due = entry.get("disable_after")
                version = entry.get("version")
                if entry.get("state") != ENABLED or not isinstance(due, datetime) or due > now \
                        or version == doc.get("current_version"):
                    continue
                try:
                    self._writer.disable_version(str(version))
                except UpstreamUnavailable:
                    failed.append(str(version))
                    continue
                self._mark_disabled(secret_id, str(version), by=by, at=now)
                disabled += 1
        log.info("workspace secret rotation sweep examined=%d disabled=%d failed=%d",
                 examined, disabled, len(failed))
        return {"examined": examined, "disabled": disabled, "failed": failed,
                "swept_at": now.isoformat()}

    def _mark_disabled(self, secret_id: str, version: str, *, by: str, at: datetime) -> None:
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            versions = [{**v, "state": DISABLED} if v.get("version") == version
                        and v.get("state") == ENABLED else dict(v)
                        for v in doc.get("versions") or []]
            txn.set(ref, {**doc, "versions": versions})
            self._audit(txn, AUDIT_VERSION_DISABLED, by=by, at=at, secret_id=secret_id, detail={
                "tenant_id": doc.get("tenant_id"), "secret_id": secret_id,
                "name": doc.get("name"), "version": version})

        _write(self._db.transaction())

    # -- the one-time form link (§5) -------------------------------------------------

    def mint_link(self, *, email: str, tenant_id: str, action: str, name: str | None,
                  console: str | None) -> dict[str, Any]:
        token = _random.token_urlsafe(32)
        now = self._now()
        expires_at = now + LINK_TTL
        self._db.collection(LINKS_COLLECTION).document(_digest(token)).set({
            "email": email, "tenant_id": tenant_id, "action": action, "name": name,
            "created_at": now, "expires_at": expires_at, "used_at": None,
        })
        path = f"/settings/workspace/secrets?action={action}"
        if name:
            path += f"&name={name}"
        path += f"&t={token}"
        base = console_origin(console)
        log.info("minted a workspace-secret form link tenant=%s action=%s", tenant_id, action)
        return {"url": f"{base}{path}" if base else None, "path": path, "action": action,
                "name": name, "expires_at": expires_at.isoformat()}

    def consume_link(self, token: str | None, *, email: str, tenant_id: str, action: str,
                     name: str) -> bool:
        """False when no link was presented. A link that is unknown, used,
        expired, or minted for another person, workspace, action or secret is
        refused outright (403) and left unburnt; the message never carries
        the token. `name` must match for a rotate link; an add link names none."""
        if token is None:
            return False
        ref = self._db.collection(LINKS_COLLECTION).document(_digest(token)) \
            if 0 < len(token) <= 256 else None
        now = self._now()

        @firestore.transactional
        def _consume(txn: Any) -> bool:
            if ref is None:
                return False
            snap = _snapshot(txn.get(ref))
            link = (snap.to_dict() or {}) if snap.exists else None
            if not link or link.get("used_at") is not None:
                return False
            expires_at = link.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                return False
            if (link.get("email"), link.get("tenant_id"), link.get("action")) \
                    != (email, tenant_id, action):
                return False
            if action == "rotate" and link.get("name") != name:
                return False
            txn.update(ref, {"used_at": now})
            return True

        if not _consume(self._db.transaction()):
            raise SecretLinkInvalid(
                "this secrets-form link is unknown, already used, expired, or was minted "
                "for someone else, another workspace or another secret; mint a new one or "
                "open the settings page directly")
        return True
