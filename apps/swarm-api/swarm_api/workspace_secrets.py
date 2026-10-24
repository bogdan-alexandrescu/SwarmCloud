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
  * Q4. In a GROUP workspace its `admin` writes -- a workspace admin, or a
    platform admin, who holds `admin` in every group workspace
    (docs/design/workspace-roles.md §4.8, owner's Q5 and Q6) -- and members
    read names and grants. A personal workspace is managed by its person.
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

REPOSITORY SECRETS (SSC-REPO-SCOPE, part of #2158; docs/design/secrets-scopes.md
§2, owner decisions Q1, Q2 and Q9 of its §11). A repository is an attribute of
a record, not a consumer: a record of `scope: repository` is an ordinary
`-wsec-` secret of the workspace the repository is registered in, carrying the
registration's `repo_id` (`repositories._REPO_ID`, derived from the tenant, so
it cannot name another workspace's registration) and its `repository`
(`owner/repo`, for display). Its name is unique within (workspace,
repository), so its index entry is `<tenant>~<repo_id>~<name>`, which no
workspace name can collide with (a name has no `~`). Its grants copy the
`repo_id` and are signed in form "v2" (`grant_attest`), which covers it, and
the release honours one only when that attestation verifies, whatever
`grant_attestation_required` says, and only for an attempt whose SIGNED spec
clones that repository (`attempt_repositories`). When one `(slot, name)` is
offered by several scopes, the person's wins, then the repository's, then the
workspace's (Q1, `_resolve_shadowing`); each other item is withheld with
`shadowed_by_<scope>`. Who may write one is the route's rule
(`routes/repository_secrets.py`): `require_manager`, or a member GitHub calls
admin of that repository, asked live (`github_repo_admin`).

The consumer catalogue (§2.3) lives at the end of this module. Its names,
kinds and slots are the frozen contract's (requests 80-82): profiles and their
slots from `RUNNER_PROFILES[...].secret_slots`, tools and theirs from
`RUNTIME_TOOLS`, components from `PLATFORM_COMPONENTS`, every spelling from
`consumer_name`. Only the components' slots are declared here: the contract
names the components and leaves what each reads to its reader.
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
from typing import Any, Callable, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import PLATFORM_COMPONENTS, Tenant, consumer_name, parse_consumer
from swarm_common.profiles import (
    MCP_SECRET_SLOTS,
    RUNNER_PROFILES,
    RUNTIME_TOOLS,
    SECRET_KINDS,
    SecretSlot,
    tools_launched,
)

from .admins import AUDIT_COLLECTION
from .auth import AuthContext, require_admin
from .codec import console_origin
from .errors import ApiError, Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed
from .grant_attest import (
    ATTESTATION_FIELD,
    FORM_REPOSITORY,
    REPO_ID_FIELD,
    VALID,
    GrantKeys,
    GrantPolicy,
    attested_form,
)
from .mcp_servers import enabled_names, read_registration
from .refusals import refuse
from .repositories import COLLECTION as REPOSITORIES_COLLECTION
from .repositories import _REPO_ID, parse_repository, repo_id_for
from .roles import ADMIN, RoleStore, at_least, with_tenant

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
#: SSC-REPO-SCOPE: a secret bound to one registered repository of its workspace.
SCOPE_REPOSITORY = "repository"

AUDIT_CREATED = "workspace_secret_created"
AUDIT_ROTATED = "workspace_secret_rotated"
AUDIT_DELETED = "workspace_secret_deleted"
AUDIT_GRANT_ENABLED = "workspace_secret_grant_enabled"
AUDIT_GRANT_DISABLED = "workspace_secret_grant_disabled"
AUDIT_USE_IN_SET = "workspace_secret_use_in_set"
AUDIT_VERSION_DISABLED = "workspace_secret_version_disabled"
#: SEC-GRANT-ATTEST's migration: a grant written before attestations existed,
#: re-signed by `attest_grants` because its fields match its newest audit row.
AUDIT_GRANT_ATTESTED = "workspace_secret_grant_attested"
#: A registration removed: every enabled grant on its secrets disabled, in one
#: transaction per secret with this row (secrets-scopes.md §2.1).
AUDIT_REPOSITORY_GRANTS_DISABLED = "workspace_secret_repository_grants_disabled"

#: The display name: it becomes a file name in phase 2 (§4.1), so it is
#: lowercase letters, digits and hyphens, and starts with a letter or digit.
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

#: `provider` (§2.1) drives the console's help text, never behaviour. NOT
#: `github`: a forge token is never readable by an agent (owner rule
#: 2026-09-25), and a store whose only use is to hand a value to an agent's
#: profile or tool must not offer to hold one. Forge tokens are listed by
#: SEC-MIGRATE-FORGE from their own records, never written here.
PROVIDERS = ("anthropic", "brave", "openai", "typesafe", "other")

#: `kind` (§2.1): the contract's SECRET_KINDS (request 80) but `forge_token`,
#: which this store never holds (see PROVIDERS). Unpacked from one tuple
#: rather than one `NAME = "..."` line each: the worker's publish-time
#: credential scan reads a line like that as a key assignment.
KINDS = tuple(kind for kind in SECRET_KINDS if kind != "forge_token")
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


class GrantKeyRequired(ApiError):
    """Enabling a grant on a repository secret where this deployment has no
    grant key: such a grant is never released (§2.2), so it is refused rather
    than written inert. A new refusal: behind `refusals.SWITCHES`, and while
    off the grant is written unattested and the release drops it."""

    status_code = 422
    code = "grant_key_required"


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


def workspace_admin(roles: RoleStore | None, auth: AuthContext, tenant: Tenant) -> bool:
    """Whether the caller holds `admin` in GROUP workspace `tenant`
    (docs/design/workspace-roles.md §4.8; Q6, Q13), read fresh: a narrowing
    made a moment ago refuses the next write, not the one 15 s later.

    The gate on every route of ROLES-GATES-ADMIN: secrets, MCP servers, the
    tenant provider key, git-token tenant and repository slots, and the
    workspace's settings. True lets the caller through on the role alone.
    False is not a refusal by itself -- the caller then meets the rule the
    route had before roles, so a switch that is off never lets a member past
    a gate they were stopped at:

    * `role_admin_required` (403) through its switch (`refusals.py`, Q10).
      On, a member is refused it here; off, `RoleStore.require` logs
      `refusal report-only code=role_admin_required` and this answers False;
    * a role that could not be read is False with the switch off and 503
      `role_unavailable` with it on, never a guess;
    * a personal workspace is never decided here: its person is its admin,
      and every caller checks that first by the tenant document's principal;
    * a caller who is neither in `tenant` (their selected tenant or a
      verified membership) nor a platform admin is False before any grant is
      read: a grant to a non-member is stored and inert until they join
      (Q3), and a route whose path names a workspace must not let it act.
    """
    if roles is None or is_personal(tenant):
        return False
    if auth.tenant_id != tenant.tenant_id:
        if not auth.is_admin and not any(c[0] == tenant.tenant_id for c in auth.tenant_choices):
            return False
        auth = with_tenant(auth, tenant.tenant_id, tenant.principal or "")
    me = roles.require(auth, ADMIN, fresh=True)
    return me is not None and at_least(me.role, ADMIN)


def require_manager(auth: AuthContext, tenant: Tenant, roles: RoleStore | None = None) -> None:
    """Q4, Q6. A personal workspace: its person, by the tenant document's
    principal (the frozen slug drops the domain, so the id alone does not say
    whose it is). A group workspace: its `admin` (`workspace_admin`), else a
    full platform admin (`require_admin` with no route, so a pool admin's
    narrow capability never reaches here, and 503 when the directory could not
    say). Members of a group read; they never write, whatever the role
    switch says."""
    if is_personal(tenant):
        email = (auth.email or "").strip().lower()
        if not email or (tenant.principal or "").strip().lower() != email:
            raise Forbidden("a personal workspace's secrets are managed by its person only")
        return
    if workspace_admin(roles, auth, tenant):
        return
    try:
        require_admin(auth)
    except Forbidden:
        raise Forbidden(
            "in a group workspace only its admins add, rotate, grant or delete "
            "secrets and MCP servers; members see names and grants") from None


def can_manage(auth: AuthContext, tenant: Tenant, roles: RoleStore | None = None) -> bool:
    """What the console offers: whether `require_manager` would let this
    caller write. Asked without the gate, so a member's listing logs no
    would-be refusal and costs no fresh read."""
    if is_personal(tenant) or auth.is_admin or roles is None:
        try:
            require_manager(auth, tenant)
        except Forbidden:
            return False
        return True
    me = roles.try_resolve(auth) if auth.tenant_id == tenant.tenant_id else None
    return me is not None and at_least(me.role, ADMIN)


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
        return consumer_name(self.kind, self.name)

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


#: Every profile that declares a workspace-secret slot (contract request 80):
#: the agent profiles. `mock`, `generic` and the worker actions declare none.
#: Read from the frozen catalogue so a profile added there is offered here.
PROFILE_NAMES: tuple[str, ...] = tuple(sorted(
    name for name, profile in RUNNER_PROFILES.items() if profile.secret_slots
))

#: The components' slots. The contract fixes WHICH components exist
#: (PLATFORM_COMPONENTS, request 82) and that `git` takes forge tokens only
#: (design §6.2); what each reads is this reader's, so it is declared here,
#: keyed by the contract's names and held to them by `_catalogue`.
_COMPONENT_SLOTS: dict[str, tuple[Slot, ...]] = {
    "judge": (Slot("typesafe", (API_KEY,), "the TypeSafe key"),),
    "merge": (),
    "indexer": (),
    "git": (Slot("forge", ("forge_token",), "the forge token"),),
}


def _slots(declared: Mapping[str, SecretSlot]) -> tuple[Slot, ...]:
    return tuple(Slot(name, slot.kinds, slot.means) for name, slot in declared.items())


def _catalogue() -> tuple[Consumer, ...]:
    """Every consumer the contract names, with its slots (design §2.3, §2.4).

    A consumer with no slot is listed and not grantable: a grant would
    deliver nothing."""
    if set(_COMPONENT_SLOTS) != set(PLATFORM_COMPONENTS):
        raise RuntimeError("workspace secrets: component slots do not match PLATFORM_COMPONENTS")
    no_slot = "declares no secret slot yet; a grant would deliver nothing"
    profiles = [Consumer("profile", name, _slots(RUNNER_PROFILES[name].secret_slots))
                for name in PROFILE_NAMES]
    tools = [Consumer("tool", name, _slots(tool.secret_slots),
                      note="" if tool.secret_slots else no_slot)
             for name, tool in RUNTIME_TOOLS.items()]
    components = []
    for name in PLATFORM_COMPONENTS:
        if name == "git":
            components.append(Consumer(
                "component", name, _COMPONENT_SLOTS[name],
                note="forge tokens only, never granted from this page (design §6.2)",
                locked=True))
        else:
            components.append(Consumer("component", name, _COMPONENT_SLOTS[name],
                                       note="" if _COMPONENT_SLOTS[name] else no_slot))
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
# Contract request 89: `mcp:<name>`, a tenant's registered MCP server
# ---------------------------------------------------------------------------
#
# An `mcp:` consumer is NOT in the catalogue above: its names are a tenant's
# registrations (mcp_servers.py), which change at runtime, so it is checked
# against the tenant's registry and never against CONSUMERS_BY_NAME. That is
# also why the release can never deliver one: `_deliverable` reads the
# catalogue, and `attempt_consumers` never derives an `mcp:` name (design
# §4.5). The attach route (routes/attempts_mcp.py) is the one reader of an
# `mcp:` grant, and it never writes the value to a file.


def mcp_consumer(name: str) -> Consumer:
    """The consumer `mcp:<name>`, with its one slot, `auth`."""
    return Consumer("mcp", name, _slots(MCP_SECRET_SLOTS))


def mcp_consumers(db: Any, tenant_id: str) -> list[Consumer]:
    """`mcp:<name>` for every ENABLED registration of `tenant_id`: the
    consumers the console's grant matrix adds to the catalogue for this
    workspace (§4.5)."""
    return [mcp_consumer(name) for name in enabled_names(db, tenant_id)]


def check_mcp_grant(db: Any, tenant_id: str, consumer: str, slot: str | None,
                    kind: str) -> tuple[Consumer, Slot]:
    """`check_grant` for an `mcp:` consumer: the name must be a registration
    of THIS tenant (any state, so a disabled server's grant can still be
    unticked), the slot `auth`, the kind one it reads."""
    try:
        _, name = parse_consumer(consumer)
    except ValueError:
        raise ValidationFailed(
            "an mcp: consumer names a registered MCP server, e.g. mcp:kb") from None
    if read_registration(db, tenant_id, name) is None:
        raise ValidationFailed(
            "no MCP server by that name is registered in this workspace "
            "(GET /v1/mcp-servers)")
    entry = mcp_consumer(name)
    chosen = entry.slot(slot)
    if chosen is None:
        raise ValidationFailed(f"{consumer} has no such slot",
                               detail={"slots": [s.name for s in entry.slots]})
    if kind not in chosen.kinds:
        raise ValidationFailed(
            f"{consumer} reads {', '.join(chosen.kinds)} in its {chosen.name} slot, "
            f"not a {kind}")
    return entry, chosen


def is_mcp_consumer(consumer: Any) -> bool:
    return isinstance(consumer, str) and consumer.startswith("mcp:")


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


def _name_id(tenant_id: str, name: str, repo_id: str | None = None) -> str:
    """The name index's key: unique per workspace, or per (workspace,
    repository) for a repository secret. A name never holds `~`."""
    return f"{tenant_id}~{name}" if repo_id is None else f"{tenant_id}~{repo_id}~{name}"


#: A repository secret's Secret Manager id is marked, so that what it IS
#: does not rest on its tenant-writable record: the 16 hex of
#: `swarm-tenant-<t>-wsec-<16 hex>` are 8 random hex and the first 8 hex of
#: sha256 over this tag and those 8. The mark is no secret and needs none: a
#: tenant's agent cannot create a Secret Manager secret, only point a record at
#: one swarm-api made, and the id it points at says which kind it is. A
#: workspace secret's id is drawn until it does NOT carry the mark.
_REPOSITORY_MARK = b"swarm-repository-secret:"


def _mark(head: str) -> str:
    return hashlib.sha256(_REPOSITORY_MARK + head.encode("ascii")).hexdigest()[:8]


def repository_hex() -> str:
    head = _random.token_hex(4)
    return head + _mark(head)


def workspace_hex() -> str:
    while True:
        tail = _random.token_hex(8)
        if not _marked(tail):
            return tail


def _marked(tail: str) -> bool:
    return len(tail) == 16 and tail[8:] == _mark(tail[:8])


def is_repository_secret_id(secret_id: Any) -> bool:
    """Was `secret_id` made for a repository secret (`repository_hex`)?"""
    if not isinstance(secret_id, str) or "-wsec-" not in secret_id:
        return False
    return _marked(secret_id.rsplit("-wsec-", 1)[1])


def is_repository_record(doc: Mapping[str, Any]) -> bool:
    """A record bound to a repository, by its secret's id or by either field:
    a record rewritten to drop `scope` or `repo_id` is still treated as bound,
    never as the workspace's."""
    return (doc.get("scope") == SCOPE_REPOSITORY or doc.get(REPO_ID_FIELD) is not None
            or is_repository_secret_id(doc.get("secret_id")))


def _bound_to(doc: Mapping[str, Any], repo_id: str | None) -> bool:
    """Is `doc` the workspace's own (repo_id None) or that repository's?"""
    if repo_id is None:
        return not is_repository_record(doc)
    return doc.get("scope") == SCOPE_REPOSITORY and doc.get(REPO_ID_FIELD) == repo_id


def _with_repo(fields: dict[str, Any], repo_id: str | None) -> dict[str, Any]:
    """A grant (or its audit detail) with its repository when it has one.
    A workspace grant carries no `repo_id` key at all, so it stays form "v1"."""
    return fields if repo_id is None else {**fields, REPO_ID_FIELD: repo_id}


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
        "repo_id": doc.get(REPO_ID_FIELD),
        "repository": doc.get("repository"),
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
    with its `admin_audit` row in the same Firestore transaction.

    `grants` is the attestation policy (`swarm_api.grant_attest`): every grant
    document written here carries an `attestation` made with its current key.
    Without one (no `swarm-workspace-grant-key` in this deployment) a grant
    is written unattested, which the release accepts only while
    `grant_attestation_required` is off."""

    def __init__(self, db: Any, writer: SecretWriter, *, now: Any,
                 grants: GrantPolicy | None = None, authority: str | None = None) -> None:
        self._db = db
        self._writer = writer
        self._now = now
        self._grants = grants or GrantPolicy(keys=GrantKeys(), required=False)
        #: Why the writer may write, in every audit row this store writes
        #: (`routes/repository_secrets.py`: `workspace_admin` or
        #: `github_repo_admin`); None for the routes that have one rule.
        self._authority = authority

    def _attested(self, grant: dict[str, Any]) -> dict[str, Any]:
        """`grant` with its attestation, or with none when there is no key.
        Never a stale one: the field is always written, so a grant re-written
        without a key cannot keep a MAC made for its earlier fields."""
        return {**grant, ATTESTATION_FIELD: self._grants.keys.sign(grant)}

    # -- reads -------------------------------------------------------------------

    def _audit(self, txn: Any, action: str, *, by: str, at: datetime, secret_id: str,
               detail: dict[str, Any]) -> None:
        if self._authority is not None:
            detail = {**detail, "authority": self._authority}
        txn.set(self._db.collection(AUDIT_COLLECTION).document(), {
            "action": action, "target_secret_id": secret_id, "by": by, "at": at,
            "detail": detail,
        })

    def find(self, tenant_id: str, name: str, repo_id: str | None = None) -> dict[str, Any]:
        """The ACTIVE record named `name` in `tenant_id`, or 404. Bounded by the
        tenant twice: the index is keyed by it and the record must carry it.
        With `repo_id`, the repository's secret of that name; without, the
        workspace's own, never a repository's."""
        index = self._db.collection(NAMES_COLLECTION) \
            .document(_name_id(tenant_id, name, repo_id)).get()
        secret_id = (index.to_dict() or {}).get("secret_id") if index.exists else None
        if secret_id:
            snap = self._db.collection(SECRETS_COLLECTION).document(secret_id).get()
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") == tenant_id and doc.get("state") == ACTIVE \
                    and _bound_to(doc, repo_id):
                return doc
        raise NotFound("no secret by that name in this workspace"
                       if repo_id is None else "no secret by that name for this repository")

    def grants_of(self, secret_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for secret_id in secret_ids:
            query = self._db.collection(GRANTS_COLLECTION).where(
                filter=FieldFilter("secret_id", "==", secret_id))
            out[secret_id] = [s.to_dict() or {} for s in query.stream()]
        return out

    def list(self, tenant_id: str, repo_id: str | None = None) -> list[dict[str, Any]]:
        """The workspace's own secrets, or with `repo_id` that repository's."""
        query = self._db.collection(SECRETS_COLLECTION) \
            .where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
            .where(filter=FieldFilter("state", "==", ACTIVE))
        docs = [s.to_dict() or {} for s in query.stream()]
        docs = [d for d in docs if d.get("tenant_id") == tenant_id and _bound_to(d, repo_id)]
        grants = self.grants_of(d["secret_id"] for d in docs)
        return [record_api(d, grants.get(d["secret_id"], []))
                for d in sorted(docs, key=lambda d: str(d.get("name")))]

    # -- create, rotate, delete --------------------------------------------------

    def create(self, *, tenant: Tenant, name: str, provider: str, kind: str,
               payload: bytes, by: str, via_link: bool,
               repository: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """`repository` is the registration a repository secret is bound to
        (its `repo_id` and `owner`/`repo`), read by the route from the
        caller's own workspace; None for a workspace or personal secret."""
        tenant_id = tenant.tenant_id
        repo_id: str | None = None
        repo_ref = None
        if repository is not None:
            repo_id = str(repository.get("repo_id") or "")
            if not _REPO_ID.match(repo_id) or repository.get("tenant_id") != tenant_id:
                raise NotFound(f"repository {repo_id[:64]!r} not found")
            repo_ref = self._db.collection(REPOSITORIES_COLLECTION).document(repo_id)
        name_ref = self._db.collection(NAMES_COLLECTION) \
            .document(_name_id(tenant_id, name, repo_id))
        if name_ref.get().exists:
            # Before the value reaches Secret Manager: a refused write stores nothing.
            raise Conflict("a secret by that name already exists in this workspace"
                           if repo_id is None else
                           "a secret by that name already exists for this repository")
        secret_id = tenant.secret_name(
            f"wsec-{repository_hex() if repository is not None else workspace_hex()}")
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
        if repository is not None:
            doc.update(scope=SCOPE_REPOSITORY, repo_id=repo_id,
                       repository=f"{repository.get('owner')}/{repository.get('repo')}")
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)

        @firestore.transactional
        def _write(txn: Any) -> None:
            if repo_ref is not None:
                # The registration is re-read in the transaction: one removed
                # while the value was being stored binds nothing.
                registration = _snapshot(txn.get(repo_ref))
                if not registration.exists \
                        or (registration.to_dict() or {}).get("tenant_id") != tenant_id:
                    raise NotFound(f"repository {repo_id!r} not found")
            if _snapshot(txn.get(name_ref)).exists:
                raise Conflict("a secret by that name already exists in this workspace"
                               if repo_id is None else
                               "a secret by that name already exists for this repository")
            txn.set(ref, doc)
            index = {"tenant_id": tenant_id, "name": name, "secret_id": secret_id}
            if repo_id is not None:
                index[REPO_ID_FIELD] = repo_id
            txn.set(name_ref, index)
            detail = {"tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                      "provider": provider, "kind": kind, "version": version,
                      "via_link": via_link}
            if repo_id is not None:
                detail.update(repo_id=repo_id, repository=doc["repository"])
            self._audit(txn, AUDIT_CREATED, by=by, at=now, secret_id=secret_id, detail=detail)

        try:
            _write(self._db.transaction())
        except (Conflict, NotFound):
            # Lost a race on the name: the version just added is nobody's.
            self._writer.destroy_versions(secret_id)
            raise
        log.info("workspace secret created tenant=%s secret=%s name=%s kind=%s repo_id=%s",
                 tenant_id, secret_id, name, kind, repo_id)
        return record_api(doc, [])

    def rotate(self, *, tenant_id: str, name: str, kind_payload: Any, by: str,
               via_link: bool, repo_id: str | None = None) -> dict[str, Any]:
        """Q5: add a version, make it current, and schedule the previous one to
        be DISABLED by the sweep after `ROTATION_GRACE` -- enabled until then,
        so an attempt already holding it is not broken mid-run."""
        found = self.find(tenant_id, name, repo_id)
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
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE \
                    or not _bound_to(doc, repo_id):
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

    def delete(self, *, tenant_id: str, name: str, by: str,
               repo_id: str | None = None) -> dict[str, Any]:
        """Destroy every version, disable every grant, free the name; the
        record stays (state `deleted`) so its audit trail has something to
        name. Destroy, not disable: a person who deletes means it gone."""
        found = self.find(tenant_id, name, repo_id)
        secret_id = found["secret_id"]
        destroyed = self._writer.destroy_versions(secret_id)
        now = self._now()
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)
        name_ref = self._db.collection(NAMES_COLLECTION) \
            .document(_name_id(tenant_id, name, repo_id))
        grants = self.grants_of([secret_id]).get(secret_id, [])
        disabled = sorted(str(g.get("consumer")) for g in grants if g.get("enabled"))
        slots = {str(g.get("consumer")): g.get("slot") for g in grants}

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE \
                    or not _bound_to(doc, repo_id):
                raise NotFound("no secret by that name in this workspace")
            versions = [{**v, "state": DESTROYED} if v.get("state") != DESTROYED else dict(v)
                        for v in doc.get("versions") or []]
            txn.set(ref, {**doc, "state": DELETED, "versions": versions,
                          "deleted_at": now, "deleted_by": by})
            txn.delete(name_ref)
            for consumer in disabled:
                # Re-signed as DISABLED: a MAC over enabled=false can never
                # release, and the one it replaces stops verifying.
                slot = slots.get(consumer)
                txn.set(self._db.collection(GRANTS_COLLECTION)
                        .document(_grant_id(secret_id, consumer)),
                        self._attested(_with_repo({
                            "secret_id": secret_id, "tenant_id": tenant_id,
                            "consumer": consumer,
                            "slot": slot if isinstance(slot, str) and slot else None,
                            "enabled": False, "changed_at": now, "changed_by": by}, repo_id)),
                        merge=True)
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
                  enabled: bool, by: str, repo_id: str | None = None) -> dict[str, Any]:
        found = self.find(tenant_id, name, repo_id)
        secret_id = found["secret_id"]
        if repo_id is not None and is_mcp_consumer(consumer):
            # An MCP server is the workspace's, and its attach route matches no
            # repository (`routes/attempts_mcp.py`): a repository secret granted
            # to one would reach attempts on every repository.
            raise ValidationFailed(
                "a repository's secret is granted to profiles, tools and components; an "
                "MCP server's credential is a workspace secret")
        if is_mcp_consumer(consumer):
            _, chosen = check_mcp_grant(self._db, tenant_id, consumer, slot,
                                        str(found.get("kind")))
        else:
            _, chosen = check_grant(consumer, slot, str(found.get("kind")))
        now = self._now()
        ref = self._db.collection(SECRETS_COLLECTION).document(secret_id)
        grant_ref = self._db.collection(GRANTS_COLLECTION).document(_grant_id(secret_id, consumer))
        if enabled and repo_id is not None and not self._grants.keys.configured:
            # §2.2: a repository grant is released only when attested.
            refuse(GrantKeyRequired(
                "this deployment has no swarm-workspace-grant-key, and a repository "
                "secret's grant is released only when signed; nothing was written"))
        if enabled and self._grants.required and not self._grants.keys.configured:
            raise UpstreamUnavailable(
                "this deployment requires attested grants and has no "
                "swarm-workspace-grant-key, so an enabled grant could never be "
                "released; nothing was written")
        grant = self._attested(_with_repo({
            "secret_id": secret_id, "tenant_id": tenant_id, "consumer": consumer,
            "slot": chosen.name, "enabled": enabled, "changed_at": now, "changed_by": by},
            repo_id))

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE \
                    or not _bound_to(doc, repo_id):
                raise NotFound("no secret by that name in this workspace")
            txn.set(grant_ref, grant)
            self._audit(txn, AUDIT_GRANT_ENABLED if enabled else AUDIT_GRANT_DISABLED,
                        by=by, at=now, secret_id=secret_id, detail=_with_repo({
                            "tenant_id": tenant_id, "secret_id": secret_id, "name": name,
                            "consumer": consumer, "slot": chosen.name}, repo_id))

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

    # -- SEC-GRANT-ATTEST's migration ------------------------------------------------

    def _newest_grant_audit(self, grant: Mapping[str, Any]) -> dict[str, Any] | None:
        """The newest AUDIT_GRANT_ENABLED or AUDIT_GRANT_DISABLED row for
        this grant's (secret, consumer), or None."""
        query = self._db.collection(AUDIT_COLLECTION).where(
            filter=FieldFilter("target_secret_id", "==", grant.get("secret_id")))
        newest: dict[str, Any] | None = None
        for snap in query.stream():
            row = snap.to_dict() or {}
            detail = row.get("detail") if isinstance(row.get("detail"), Mapping) else {}
            if (row.get("action") not in (AUDIT_GRANT_ENABLED, AUDIT_GRANT_DISABLED)
                    or detail.get("consumer") != grant.get("consumer")
                    or not isinstance(row.get("at"), datetime)):
                continue
            if newest is None or row["at"] > newest["at"]:
                newest = row
        return newest

    @staticmethod
    def _matches_audit(grant: Mapping[str, Any], row: Mapping[str, Any] | None) -> bool:
        """Are the grant's attested fields exactly what its newest audited
        write wrote? Types too: `True` is not `1`, and a slot is text or absent."""
        if row is None:
            return False
        detail = row.get("detail") if isinstance(row.get("detail"), Mapping) else {}
        enabled = row.get("action") == AUDIT_GRANT_ENABLED
        return (type(grant.get("enabled")) is bool and grant.get("enabled") is enabled
                and all(isinstance(grant.get(k), str) and grant.get(k) == detail.get(k)
                        for k in ("secret_id", "tenant_id", "consumer"))
                and grant.get("slot") == detail.get("slot")
                and grant.get(REPO_ID_FIELD) == detail.get(REPO_ID_FIELD)
                and (grant.get("slot") is None or isinstance(grant.get("slot"), str)))

    def attest_grants(self, *, by: str) -> dict[str, Any]:
        """Sign every grant written before attestations existed, but only one
        whose fields are what its newest audited write (`set_grant`'s row)
        wrote. A grant without such a row is NOT signed and is listed by id:
        it was written by something other than this store, or changed since.
        A grant that already verifies under the CURRENT key is left alone; one
        that verifies only under the previous key was signed by swarm-api and
        is re-signed under the current one, which is what lets a rotation end.

        Each signature is written in a transaction that re-reads the grant and
        signs only if it is unchanged, with its own audit row. Answers grant
        ids only: never an attestation or a key."""
        keys = self._grants.keys
        if not keys.configured:
            raise UpstreamUnavailable(
                "this deployment has no swarm-workspace-grant-key, so no grant can be "
                "attested; nothing was written")
        current_only = GrantKeys(current=keys.current)
        now = self._now()
        examined = current = 0
        signed: list[str] = []
        unsigned: list[str] = []
        for snap in self._db.collection(GRANTS_COLLECTION).stream():
            examined += 1
            grant = snap.to_dict() or {}
            if current_only.check(grant) == VALID:
                current += 1
                continue
            previous = keys.check(grant) == VALID
            row = None if previous else self._newest_grant_audit(grant)
            if not previous and not self._matches_audit(grant, row):
                unsigned.append(snap.id)
                continue
            if self._sign_unchanged(snap.id, grant, by=by, at=now,
                                    reason="previous_key" if previous else "audit_match"):
                signed.append(snap.id)
            else:
                unsigned.append(snap.id)
        log.info("workspace secret grants attested examined=%d already=%d signed=%d "
                 "unsigned=%d", examined, current, len(signed), len(unsigned))
        return {"examined": examined, "already_attested": current, "signed": sorted(signed),
                "unsigned": sorted(unsigned), "attested_at": now.isoformat()}

    def _sign_unchanged(self, grant_id: str, seen: Mapping[str, Any], *, by: str,
                        at: datetime, reason: str) -> bool:
        ref = self._db.collection(GRANTS_COLLECTION).document(grant_id)
        fields = ("secret_id", "tenant_id", "consumer", "slot", "enabled", REPO_ID_FIELD,
                  ATTESTATION_FIELD)

        @firestore.transactional
        def _write(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if not snap.exists or any(doc.get(k) != seen.get(k) for k in fields):
                return False
            txn.set(ref, {ATTESTATION_FIELD: self._grants.keys.sign(doc)}, merge=True)
            self._audit(txn, AUDIT_GRANT_ATTESTED, by=by, at=at,
                        secret_id=str(doc.get("secret_id")), detail={
                            "tenant_id": doc.get("tenant_id"), "secret_id": doc.get("secret_id"),
                            "consumer": doc.get("consumer"), "slot": doc.get("slot"),
                            "enabled": doc.get("enabled"), "grant_id": grant_id,
                            "reason": reason})
            return True

        return _write(self._db.transaction())

    # -- a removed registration (secrets-scopes.md §2.1) ---------------------------

    def disable_repository_grants(self, *, tenant_id: str, repo_id: str,
                                  by: str) -> dict[str, Any]:
        """Disable every enabled grant on every live secret of `tenant_id`
        bound to `repo_id`, re-signed as disabled, each secret's in one
        transaction with its audit row, as a deleted secret's are. The
        records stay: the release already withholds a repository secret whose
        registration is gone (`release_plan`), and re-registering the same
        repository (the same `repo_id`) must not revive a grant nobody ticked
        again. Answers names and consumers, never a value."""
        if not _REPO_ID.match(repo_id or ""):
            raise NotFound(f"repository {(repo_id or '')[:64]!r} not found")
        now = self._now()
        query = self._db.collection(SECRETS_COLLECTION) \
            .where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
            .where(filter=FieldFilter("state", "==", ACTIVE))
        docs = [d for d in (s.to_dict() or {} for s in query.stream())
                if d.get("tenant_id") == tenant_id and d.get(REPO_ID_FIELD) == repo_id]
        disabled: dict[str, list[str]] = {}
        for doc in sorted(docs, key=lambda d: str(d.get("name"))):
            secret_id = str(doc.get("secret_id") or "")
            grants = [g for g in self.grants_of([secret_id]).get(secret_id, [])
                      if g.get("enabled") is True and g.get("tenant_id") == tenant_id
                      and isinstance(g.get("consumer"), str)]
            if not grants:
                continue

            @firestore.transactional
            def _write(txn: Any, doc: dict[str, Any] = doc, grants: list = grants,
                       secret_id: str = secret_id) -> None:
                for grant in grants:
                    slot = grant.get("slot")
                    txn.set(self._db.collection(GRANTS_COLLECTION)
                            .document(_grant_id(secret_id, grant["consumer"])),
                            self._attested(_with_repo({
                                "secret_id": secret_id, "tenant_id": tenant_id,
                                "consumer": grant["consumer"],
                                "slot": slot if isinstance(slot, str) and slot else None,
                                "enabled": False, "changed_at": now, "changed_by": by},
                                repo_id)))
                self._audit(txn, AUDIT_REPOSITORY_GRANTS_DISABLED, by=by, at=now,
                            secret_id=secret_id, detail={
                                "tenant_id": tenant_id, "secret_id": secret_id,
                                "name": doc.get("name"), "repo_id": repo_id,
                                "grants_disabled": sorted(g["consumer"] for g in grants)})

            _write(self._db.transaction())
            disabled[str(doc.get("name"))] = sorted(g["consumer"] for g in grants)
        log.info("repository secret grants disabled tenant=%s repo_id=%s secrets=%d",
                 tenant_id, repo_id, len(disabled))
        return {"repo_id": repo_id, "grants_disabled": disabled}

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
                  console: str | None, repo_id: str | None = None) -> dict[str, Any]:
        """With `repo_id`, a link to that repository's form on Config ›
        Secrets (secrets-scopes.md §6.1, §6.3), bound to the repository too:
        it is refused for the workspace's own form, and the other way round."""
        token = _random.token_urlsafe(32)
        now = self._now()
        expires_at = now + LINK_TTL
        link = {
            "email": email, "tenant_id": tenant_id, "action": action, "name": name,
            "created_at": now, "expires_at": expires_at, "used_at": None,
        }
        if repo_id is not None:
            link[REPO_ID_FIELD] = repo_id
        self._db.collection(LINKS_COLLECTION).document(_digest(token)).set(link)
        path = (f"/settings/workspace/secrets?action={action}" if repo_id is None else
                f"/config/secrets?scope=repository&repo={repo_id}&action={action}")
        if name:
            path += f"&name={name}"
        path += f"&t={token}"
        base = console_origin(console)
        log.info("minted a workspace-secret form link tenant=%s action=%s repo_id=%s",
                 tenant_id, action, repo_id)
        return {"url": f"{base}{path}" if base else None, "path": path, "action": action,
                "name": name, "expires_at": expires_at.isoformat()}

    def consume_link(self, token: str | None, *, email: str, tenant_id: str, action: str,
                     name: str, repo_id: str | None = None) -> bool:
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
            if (link.get("email"), link.get("tenant_id"), link.get("action"),
                    link.get(REPO_ID_FIELD)) != (email, tenant_id, action, repo_id):
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


# ---------------------------------------------------------------------------
# SEC-RELEASE's queries (design §3.3, §4.4): which secrets an attempt is due
# ---------------------------------------------------------------------------
#
# Read by `routes/attempts_secrets.py`, the one route that serves a value.
# Nothing here reads a payload either: these functions decide WHICH records
# and slots an attempt is released, from Firestore and the frozen catalogue;
# the Secret Manager read, the fence and the rows are the route's.
#
# EVERY DOCUMENT READ HERE IS ONE A TENANT'S AGENT CAN WRITE (docs/multi-
# tenancy.md, "The Firestore row, in full": no collection-level IAM, and a
# document id it can guess is one it can rewrite). So nothing a record or a
# grant says is trusted to name ANOTHER workspace's secret: a record is
# released only when its Secret Manager id and its version both sit under
# `swarm-tenant-<the tenant the signed spec names>-wsec-` (`owned_by`), and
# that tenant comes from the signed task, never from a document.

#: One row per secret per attempt (§4.4). Never a value.
RELEASES_COLLECTION = "workspace_secret_releases"
#: One document per attempt that was released anything or nothing, keyed by
#: the attempt id: what makes the release once per attempt.
RELEASE_MARKS_COLLECTION = "workspace_secret_release_marks"
#: Owner decision Q6: release rows are kept 90 days, by a Firestore TTL on
#: `expire_at` (the TTL policy itself is a Terraform field on the collection).
RELEASE_RETENTION = timedelta(days=90)

#: Owner decision Q8 (§10): the TypeSafe key never enters an agent pod; a
#: platform forwarder adds it to jev's requests (lane SEC-INJECT-TYPESAFE). A
#: grant into one of these slots is therefore matched (so the console's "last
#: used" and the forwarder lane read the same rule) and WITHHELD from the
#: release, by name, with this reason.
INJECT_ONLY_SLOTS: tuple[str, ...] = ("typesafe",)
INJECT_ONLY = "inject_only"

_WSEC_HEX = re.compile(r"[0-9a-f]{16}")
_VERSION_TAIL = re.compile(r"[0-9]+")

#: Q1 (secrets-scopes.md §2.4, §11): withheld reasons of an item another
#: scope's item of the same `(slot, name)` won. Names only, never a value.
SHADOWED_BY = {SCOPE_PERSONAL: "shadowed_by_personal",
               SCOPE_REPOSITORY: "shadowed_by_repository",
               SCOPE_WORKSPACE: "shadowed_by_workspace"}
#: Q1's order: the person's, then the repository's, then the workspace's.
_PRECEDENCE = {SCOPE_PERSONAL: 0, SCOPE_REPOSITORY: 1, SCOPE_WORKSPACE: 2}
#: Logged when an attempt's signed `additional_repositories` entry names a
#: `repo_id` that is not its repository's: every repository secret is withheld.
REPOSITORY_BINDING_MISMATCH = "repository_binding_mismatch"
#: Logged when a repository grant is dropped for its binding or attestation.
REPOSITORY_GRANT_DROPPED = "repository_grant_dropped"
_GITHUB_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def _owner_repo(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    matched = _GITHUB_URL.match(value.strip())
    if matched is None:
        return None
    try:
        return parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        return None


def attempt_repositories(task: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The `repo_id`s an attempt clones (secrets-scopes.md §2.2), from its
    SIGNED task: its `repository_url`'s registration id first, then each
    `additional_repositories` entry's, in order. The caller has verified the
    spec's signature first.

    Each entry's `repo_id` is RECOMPUTED from its `repository` and the task's
    tenant: swarm-api wrote it, but a mismatch means it is not the field
    swarm-api wrote, so None is answered and every repository secret of the
    attempt is withheld (`REPOSITORY_BINDING_MISMATCH`). An attempt that
    clones nothing is `()`, and is released no repository secret."""
    tenant_id = task.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        return ()
    out: list[str] = []
    primary = _owner_repo(task.get("repository_url"))
    if primary is not None:
        out.append(repo_id_for(tenant_id, *primary))
    extras = task.get("additional_repositories")
    if extras is None:
        return tuple(out)
    if not isinstance(extras, list):
        return None
    for entry in extras:
        if not isinstance(entry, Mapping):
            return None
        repository, repo_id = entry.get("repository"), entry.get("repo_id")
        try:
            owner, repo = parse_repository(repository if isinstance(repository, str) else "")
        except ValueError:
            return None
        if not isinstance(repo_id, str) or repo_id != repo_id_for(tenant_id, owner, repo):
            return None
        out.append(repo_id)
    return tuple(dict.fromkeys(out))


def attempt_consumers(task: Mapping[str, Any]) -> tuple[str, ...]:
    """The consumers an attempt IS (design §2.3), from its SIGNED task fields.

    `profile:<runner_profile>` when the profile declares a slot; `tool:<t>`
    for each RUNTIME_TOOLS entry its signed `browser_tool` launches
    (`tools_launched`: absent is `auto`); `component:merge` for the merge
    worker action and `component:indexer` for the indexer profile. The judge
    reads its TypeSafe key outside this release (Q8), and `component:git`
    never takes a workspace secret (§6.2), so neither is derived here. The
    caller has verified the spec's signature first: every field read here is
    one a tenant's agent can rewrite otherwise."""
    profile = RUNNER_PROFILES.get(str(task.get("runner_profile") or ""))
    if profile is None:
        return ()
    out: list[str] = []
    if profile.secret_slots:
        out.append(consumer_name("profile", profile.name))
    inputs = task.get("input") if isinstance(task.get("input"), Mapping) else {}
    out.extend(consumer_name("tool", name) for name in tools_launched(profile, inputs))
    action = getattr(profile.worker_action, "value", profile.worker_action)
    if action == "merge":
        out.append(consumer_name("component", "merge"))
    if profile.name == "indexer":
        out.append(consumer_name("component", "indexer"))
    return tuple(dict.fromkeys(out))


def wsec_prefix(tenant_id: str) -> str:
    """`swarm-tenant-<tenant>-wsec-`: every store secret of `tenant_id` starts so."""
    return f"swarm-tenant-{tenant_id}-wsec-"


def _version_number(doc: Mapping[str, Any]) -> str | None:
    """The `<n>` of `projects/<p>/secrets/<secret_id>/versions/<n>`, or None.

    Secret Manager answers `add_secret_version` with the project NUMBER, so
    the stored name's project segment is not compared with an id; the read
    is made under this project by `version_to_read`, whatever it says."""
    secret_id, version = doc.get("secret_id"), doc.get("current_version")
    if not isinstance(secret_id, str) or not isinstance(version, str):
        return None
    head, sep, number = version.rpartition("/versions/")
    parts = head.split("/")
    if (not sep or len(parts) != 4 or parts[0] != "projects" or not parts[1]
            or parts[2] != "secrets" or parts[3] != secret_id):
        return None
    return number if _VERSION_TAIL.fullmatch(number) else None


def owned_by(doc: Mapping[str, Any], tenant_id: str, project_id: str, *,
             repository: bool = False) -> bool:
    """Is `doc` a live store record of `tenant_id` whose current version is
    that tenant's own `-wsec-` secret, of a WORKSPACE (or personal) secret, or
    with `repository` of a repository secret (`is_repository_secret_id`)?
    The default keeps every reader that predates repository secrets
    (`routes/attempts_mcp.py`, a person's opt-in) off them, whatever their
    tenant-writable record says.

    The record is tenant-writable, so its `secret_id` is checked as a NAME,
    `swarm-tenant-<tenant>-wsec-<16 hex>`, and its `current_version` as a
    version of that same secret. A rewritten record can then point only at
    its own workspace's secrets, and only in this project (`version_to_read`)."""
    secret_id = doc.get("secret_id")
    if doc.get("tenant_id") != tenant_id or doc.get("state") != ACTIVE or not project_id:
        return False
    prefix = wsec_prefix(tenant_id)
    if not isinstance(secret_id, str) or not secret_id.startswith(prefix):
        return False
    tail = secret_id[len(prefix):]
    if not _WSEC_HEX.fullmatch(tail) or _marked(tail) != repository:
        return False
    return _version_number(doc) is not None


def version_to_read(doc: Mapping[str, Any], project_id: str) -> str:
    """The version name the release reads: rebuilt under THIS project from a
    record `owned_by` has accepted, never the stored string itself."""
    number = _version_number(doc)
    if number is None:
        raise ValueError("not a store record's version")
    return f"projects/{project_id}/secrets/{doc['secret_id']}/versions/{number}"


@dataclass(frozen=True)
class Due:
    """One secret an attempt is due, in one slot, for these consumers."""

    record: Mapping[str, Any]
    slot: str
    consumers: tuple[str, ...]
    #: Which scope offered it, for Q1's order: personal, repository, workspace.
    scope: str = SCOPE_WORKSPACE
    #: Among repository items: the position of its repository in the
    #: attempt's set, so the primary repository's wins over an additional one's.
    order: int = 0

    @property
    def name(self) -> str:
        return str(self.record.get("name"))


@dataclass
class ReleasePlan:
    due: list[Due]
    #: `(name, reason)`: matched, not released. Names only, never a value.
    withheld: list[tuple[str, str]]


def _deliverable(grant: Mapping[str, Any], kind: Any) -> str | None:
    """The slot an enabled grant delivers into, or None. The catalogue's rule
    (`check_grant`) re-applied at read time: a grant document is tenant-
    writable, so the slot, the consumer and the kind are checked again."""
    entry = CONSUMERS_BY_NAME.get(str(grant.get("consumer") or ""))
    if entry is None or entry.locked or not entry.slots:
        return None
    chosen = entry.slot(grant.get("slot") if isinstance(grant.get("slot"), str) else None)
    if chosen is None or kind not in chosen.kinds or kind == "forge_token":
        return None
    return chosen.name


def _repository_grant_admitted(grant: Mapping[str, Any], grant_id: str, bound: str | None,
                               grants: GrantPolicy) -> bool:
    """§2.2's binding, on a grant the release has matched by consumer.

    A grant on a workspace or personal record (`bound` None) that carries a
    repository is dropped: the record says one thing and the signed grant
    another, which only a rewrite produces. A grant on a repository record
    must carry that record's `repo_id` and a "v2" attestation that VERIFIES,
    whatever `grant_attestation_required` says: no repository grant was ever
    written unsigned, so the migration window does not apply to one. A "v1"
    attestation does not cover `repo_id`, so it never vouches for a binding."""
    grant_repo = grant.get(REPO_ID_FIELD)
    if bound is None:
        if grant_repo is None:
            return grants.admits(grant, grant_id)
        reason = "a repository-bound grant on a record that names no repository"
    elif grant_repo != bound:
        reason = "the grant's repository is not the record's"
    elif attested_form(grant) != FORM_REPOSITORY or grants.keys.check(grant) != VALID:
        reason = "not attested in form v2"
    else:
        return True
    log.warning("%s grant=%r tenant=%r secret=%r: %s", REPOSITORY_GRANT_DROPPED, grant_id,
                grant.get("tenant_id"), grant.get("secret_id"), reason)
    return False


def _plan(db: Any, records: Iterable[Mapping[str, Any]], consumers: tuple[str, ...],
          grant_tenant: str, plan: ReleasePlan, grants: GrantPolicy, *,
          scope: str = SCOPE_WORKSPACE, order: dict[str, int] | None = None) -> None:
    wanted = set(consumers)
    for record in records:
        bound = record.get(REPO_ID_FIELD) if scope == SCOPE_REPOSITORY else None
        secret_id = record["secret_id"]
        query = db.collection(GRANTS_COLLECTION) \
            .where(filter=FieldFilter("secret_id", "==", secret_id)) \
            .where(filter=FieldFilter("enabled", "==", True))
        slots: dict[str, list[str]] = {}
        for snap in query.stream():
            grant = snap.to_dict() or {}
            if (grant.get("secret_id") != secret_id or grant.get("tenant_id") != grant_tenant
                    or grant.get("enabled") is not True or grant.get("consumer") not in wanted):
                continue
            # SEC-GRANT-ATTEST: a grant swarm-api did not sign, or signed for
            # other fields, is dropped exactly as a disabled one is, so a
            # required slot it would have filled parks CREDENTIAL_MISSING.
            # SSC-REPO-SCOPE: and a repository grant must be bound and v2.
            if not _repository_grant_admitted(grant, snap.id, bound, grants):
                continue
            slot = _deliverable(grant, record.get("kind"))
            if slot is not None:
                slots.setdefault(slot, []).append(str(grant["consumer"]))
        for slot, by in sorted(slots.items()):
            if slot in INJECT_ONLY_SLOTS:
                plan.withheld.append((str(record.get("name")), INJECT_ONLY))
                continue
            plan.due.append(Due(record, slot, tuple(sorted(by)), scope=scope,
                                order=(order or {}).get(str(bound), 0)))


def _resolve_shadowing(plan: ReleasePlan) -> None:
    """Q1: one `(slot, name)` offered by several scopes is released ONCE, from
    the person's, else the repository's (the primary repository's before an
    additional one's), else the workspace's. Every other item is withheld,
    with the scope that won. Decided here, because the worker refuses an item
    that arrives twice (`agent_worker/secretrelease.py`)."""
    winners: dict[tuple[str, str], Due] = {}
    for due in plan.due:
        key = (due.slot, due.name)
        held = winners.get(key)
        if held is None or (_PRECEDENCE[due.scope], due.order) < (_PRECEDENCE[held.scope],
                                                                   held.order):
            winners[key] = due
    kept: list[Due] = []
    for due in plan.due:
        winner = winners[(due.slot, due.name)]
        if winner is due:
            kept.append(due)
            continue
        log.info("workspace secret shadowed name=%s slot=%s scope=%s by=%s", due.name,
                 due.slot, due.scope, winner.scope)
        plan.withheld.append((due.name, SHADOWED_BY[winner.scope]))
    plan.due = kept


def release_plan(db: Any, *, tenant_id: str, consumers: tuple[str, ...], project_id: str,
                 grants: GrantPolicy, personal: tuple[str, str] | None = None,
                 personal_refusal: Callable[[], str | None] | None = None,
                 repositories: tuple[str, ...] | None = ()) -> ReleasePlan:
    """What an attempt of `tenant_id` with `consumers` is released (§3.3 step 3).

    THE WORKSPACE'S OWN: every enabled grant of `tenant_id` naming one of
    `consumers`, on a record `owned_by` the same tenant, whose attestation
    `grants` admits (`swarm_api.grant_attest.GrantPolicy`). Required, not
    defaulted: a caller that forgot it would release forged grants.

    A PERSON'S OWN, ON A GROUP TASK (owner decision Q3): `personal` is
    `(personal tenant id, submitter email)`, set by the caller only when the
    signed submitter's personal tenant is confirmed theirs. Its records are
    those whose opt-in list names `tenant_id` and whose creator is that
    person; `personal_refusal` re-checks their membership of `tenant_id` NOW,
    and only when there is something to release: a refusal (a lapsed or
    unresolved membership) withholds every one of them, by name.

    A REPOSITORY'S (SSC-REPO-SCOPE): a record of the workspace bound to a
    `repo_id` in `repositories` (`attempt_repositories` of the signed task;
    `()` clones nothing, None is a binding mismatch and releases none), whose
    registration still exists in this workspace, on a grant bound to the same
    repository and attested "v2" (`_repository_grant_admitted`).

    Then Q1 (`_resolve_shadowing`): one item per `(slot, name)`."""
    plan = ReleasePlan(due=[], withheld=[])
    if not consumers:
        return plan
    query = db.collection(GRANTS_COLLECTION) \
        .where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
        .where(filter=FieldFilter("enabled", "==", True))
    secret_ids = sorted({str((s.to_dict() or {}).get("secret_id") or "") for s in query.stream()}
                        - {""})
    own: list[Mapping[str, Any]] = []
    bound: list[Mapping[str, Any]] = []
    order = {repo_id: i for i, repo_id in enumerate(repositories or ())}
    for secret_id in secret_ids:
        snap = db.collection(SECRETS_COLLECTION).document(secret_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
        if doc.get("secret_id") != secret_id:
            continue
        if owned_by(doc, tenant_id, project_id):
            # A workspace secret's record rewritten to claim a repository
            # only loses its own grants; it is never released as one.
            if not is_repository_record(doc):
                own.append(doc)
        elif owned_by(doc, tenant_id, project_id, repository=True) \
                and _released_to(db, doc, tenant_id, order):
            bound.append(doc)
    if repositories is None:
        log.warning("%s tenant=%s: an additional repository's repo_id is not its "
                    "repository's; every repository secret withheld",
                    REPOSITORY_BINDING_MISMATCH, tenant_id)
    _plan(db, own, consumers, tenant_id, plan, grants)
    _plan(db, bound, consumers, tenant_id, plan, grants, scope=SCOPE_REPOSITORY, order=order)

    if personal is not None:
        personal_tenant, email = personal
        query = db.collection(SECRETS_COLLECTION) \
            .where(filter=FieldFilter("tenant_id", "==", personal_tenant)) \
            .where(filter=FieldFilter("use_in_workspaces", "array_contains", tenant_id))
        opted = [d for d in (s.to_dict() or {} for s in query.stream())
                 if owned_by(d, personal_tenant, project_id)
                 and d.get("scope") == SCOPE_PERSONAL
                 and tenant_id in (d.get("use_in_workspaces") or [])
                 and str(d.get("created_by") or "").strip().lower() == email]
        mine = ReleasePlan(due=[], withheld=[])
        _plan(db, sorted(opted, key=lambda d: str(d.get("name"))), consumers,
              personal_tenant, mine, grants, scope=SCOPE_PERSONAL)
        refusal = personal_refusal() if (mine.due and personal_refusal is not None) else None
        if refusal:
            plan.withheld.extend((due.name, refusal) for due in mine.due)
        else:
            plan.due.extend(mine.due)
        plan.withheld.extend(mine.withheld)
    _resolve_shadowing(plan)
    return plan


def _released_to(db: Any, doc: Mapping[str, Any], tenant_id: str,
                 order: Mapping[str, int]) -> bool:
    """Is repository record `doc` one this attempt clones, of a registration
    that still exists in `tenant_id`? A record with only one of `scope` and
    `repo_id`, or a `repo_id` of another shape, is never released."""
    repo_id = doc.get(REPO_ID_FIELD)
    if doc.get("scope") != SCOPE_REPOSITORY or not isinstance(repo_id, str) \
            or not _REPO_ID.match(repo_id) or repo_id not in order:
        return False
    snap = db.collection(REPOSITORIES_COLLECTION).document(repo_id).get()
    registration = (snap.to_dict() or {}) if snap.exists else {}
    if registration.get("tenant_id") != tenant_id:
        log.info("repository secret withheld tenant=%s secret=%s: its registration %s is "
                 "gone", tenant_id, doc.get("secret_id"), repo_id)
        return False
    return True


def release_row(due: Due, *, task: Mapping[str, Any], attempt_id: str, generation: int,
                at: datetime) -> dict[str, Any]:
    """The §4.4 row for one released secret. Names, ids and a version: never a value."""
    record = due.record
    return {
        "at": at, "expire_at": at + RELEASE_RETENTION,
        "task_id": task.get("id"), "attempt_id": attempt_id, "generation": generation,
        "tenant_id": task.get("tenant_id"), "secret_tenant_id": record.get("tenant_id"),
        "secret_id": record.get("secret_id"), "name": record.get("name"),
        "version": record.get("current_version"), "slot": due.slot,
        "consumers": list(due.consumers), "submitted_by": task.get("submitted_by"),
    }
