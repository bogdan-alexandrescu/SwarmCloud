"""A person's personal workspace: the request record and the submission gate
(docs/workspaces.md §1, §5, §6.1-§6.3; #847, lane W1).

WHAT THIS IS. Each person's isolated space to run agents in -- an identity,
a storage prefix and a namespace only their work uses -- is requested here,
approved by an admin (lane W7) and made by a guarded Cloud Build job (W6).
This module owns the record that flow moves through, the request a person
makes, the loan request for a Claude account, and the gate that refuses a
person's own submissions until the workspace is `ready` and has an account.

THE RECORD, `workspaces/{tenant_id}`. Keyed by
`identity.tenant_id_for_user(email)` of the VERIFIED caller -- the same
function the API resolves a caller with, so the record is keyed by the only id
that person can ever resolve to, and nobody can request one for anyone else.
It is not a field on `Tenant`, because `Tenant` is frozen. The document id is
derived from the email, so it is a name: it never appears in anything public.

KINDS (docs/design/workspace-kinds.md §4.1, lane WK-RECORD). The record
carries `kind`: `personal`, `group` (a Google group) or `service` (a service
account of this project). ABSENT READS `personal` (`kind_of`), so every
record written before kinds needs no backfill. A group's record is keyed by
`tenant_id_for_group`, the others' by `tenant_id_for_user`, so each is still
keyed by the only id its principal resolves to. Only an admin files a group
or service record, or a person's record for them (`People.create`,
`POST /v1/admin/workspaces`); `requested_by` says who, and like
`decision.by` it never leaves Firestore.

THE WORKSPACE ID, `w-` and six random hex digits (`secrets.token_hex(3)`), is
the only id that does. It is RANDOM, never derived from the email, so nobody
can recover it by hashing a guessed address. `workspace_ids/{w-...}` is a
one-field index holding the tenant id; the request draws an id and creates
that document in the same transaction that creates the record, and a drawn id
whose index document exists is drawn again, so two people never share one.

STATES (§1.2). `requested` (here), `approved`/`denied` (an admin, W7),
`applying`, `needs_owner`, `failed` and `ready` (the job, W6). swarm-api NEVER
writes `ready`: it is evidence the job's final check writes after reading
back every object, and nothing a client sends can set it. The one exception is
`Workspaces.migrate` (§3.3), which no route calls: an operator runs it through
`scripts/workspace-migrate-record.sh` for a Terraform-era tenant whose
resources already exist, and the `--mode verify` run that follows reads them
back.

THE GATE (§5) is behind `WORKSPACE_GATE`, which ships OFF (WD8). Off, it reads
nothing and refuses nothing, `tenant_for` creates personal tenants on first
sight as it always has, and the two onboarding steps are served for
information without holding the checklist back: nothing changes for anyone.
It is turned on only after one real person has been approved end to end and
every existing personal tenant has a `ready` record (§5.5, step 3).

PRIVACY. No email is ever logged here: a log line names the workspace id,
or nothing. `decision.by` (an admin's address) never leaves Firestore.
"""

from __future__ import annotations

import logging
import os
import secrets
import base64
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common import specsign
from swarm_common.admission import _snapshot
from swarm_common.identity import SERVICE_ACCOUNT_EMAIL, tenant_id_for_group, tenant_id_for_user
from swarm_common.models import Tenant

from .admins import AUDIT_COLLECTION
from .errors import (
    Conflict,
    NoClaudeAccount,
    ValidationFailed,
    WorkspaceDomainNotAllowed,
    WorkspaceFailed,
    WorkspaceGroupIdReserved,
    WorkspaceGroupIsAdmin,
    WorkspaceIdTaken,
    WorkspaceIdTerraform,
    WorkspaceNotForServiceAccounts,
    WorkspaceNotReady,
    WorkspaceNotRequested,
    WorkspacePrincipalForbidden,
    WorkspaceRequestTooSoon,
    WorkspaceServiceAlreadyResolves,
    WorkspaceServiceNotInProject,
    WorkspaceServicePlatform,
)
from .specsigning import SpecSigningUnavailable
from .store import TENANTS
from .validation import is_service_submitter

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# collections and vocabulary
# --------------------------------------------------------------------------

WORKSPACES = "workspaces"
WORKSPACE_IDS = "workspace_ids"
LOAN_REQUESTS = "loan_requests"
PEOPLE = "people"
#: The broker's pool (`quota_broker.accountstore.COLLECTION`), read here for
#: the account check only. swarm-api's image does not carry the broker, so the
#: name and the three fields read (`owner_tenant`, `lend_to`, `state`) are
#: restated, as `scheduler.credentials` restates them.
ACCOUNTS = "accounts"

NONE = "none"
REQUESTED = "requested"
APPROVED = "approved"
APPLYING = "applying"
NEEDS_OWNER = "needs_owner"
READY = "ready"
DENIED = "denied"
FAILED = "failed"
STATES = (REQUESTED, APPROVED, APPLYING, NEEDS_OWNER, READY, DENIED, FAILED)

#: A request against one of these writes nothing and answers the record:
#: clicking twice, or in the console and then in the plugin, is one request.
UNCHANGED_ON_REQUEST = frozenset({REQUESTED, APPROVED, APPLYING, NEEDS_OWNER, READY})

#: How long after a denial a person may ask again (§1.3, confirmed by the
#: owner 2026-10-08). An admin may approve the denied record at any time.
REREQUEST_WAIT = timedelta(hours=24)

#: Where a request came from. A body naming anything else is refused.
VIAS = ("console", "plugin", "api")
#: `requested_via` of a record an admin filed in Admin › Tenants
#: (`POST /v1/admin/workspaces`). Not one of VIAS: no person's body names it.
ADMIN_VIA = "admin"

#: The record's kinds (docs/design/workspace-kinds.md §4.1). A record with
#: no `kind` is PERSONAL (`kind_of`), so every record written before kinds
#: existed reads exactly as it did, with no backfill.
PERSONAL = "personal"
GROUP = "group"
SERVICE = "service"
KINDS = (PERSONAL, GROUP, SERVICE)

#: `display_name` is a label for the switcher and Admin › Tenants, never an
#: identity; nothing decides anything by it.
MAX_DISPLAY_NAME = 60

#: §8's limits for a new personal workspace (WD5). quota_pods and quota_cpu
#: are 2 pods and 8 vCPU per agent, the ratio People keeps when an admin
#: raises the ceiling (§6.4): 8 claude-code pods at 4 vCPU with
#: `requests == limits`, plus headroom for Jobs finishing inside their TTL.
DEFAULT_LIMITS: Mapping[str, int] = {
    "max_active": 8, "capacity_units": 8, "quota_pods": 16, "quota_cpu": 64,
}

#: Each kind's limits when the request names none (§4.2.2). Personal and
#: group are §8's 8/8. Service is 4/8, the pattern of the two Terraform
#: service tenants (`u-sw-c90291`, `u-or-9a0792`); its quotas keep People's
#: ratio of 2 pods and 8 vCPU per agent (`people.ceiling_limits`).
DEFAULT_LIMITS_BY_KIND: Mapping[str, Mapping[str, int]] = {
    PERSONAL: DEFAULT_LIMITS,
    GROUP: DEFAULT_LIMITS,
    SERVICE: {"max_active": 4, "capacity_units": 8, "quota_pods": 8, "quota_cpu": 32},
}

#: Draws of a workspace id before giving up. 16.7 million ids; a second draw
#: is already rare, a sixteenth means something other than chance.
MAX_ID_DRAWS = 16

#: §3.3's migration: who its decision names, and why. Not an email, so no
#: admin's address is invented for a decision no admin made.
MIGRATION_BY = "migration"
MIGRATION_REASON = "Terraform-era tenant moved by W9"
#: `requested_via` of a record the migration creates: not one of VIAS, because
#: no person asked for it.
MIGRATION_VIA = "migration"
#: States the migration completes IN PLACE, keeping the record's workspace id:
#: a person may have asked for a workspace before their Terraform-era tenant
#: moved (u-bogdan did, 2026-10-09: w-752763), and a fresh id would orphan the
#: one their request, its index entry and any admin's view already name.
#: Not approved, applying or needs_owner: a build may be about to make, or be
#: making, the resources the tenant already has. Not ready unless migrated: the
#: job made that one, and there is nothing to move.
MIGRATABLE = frozenset({REQUESTED, FAILED, DENIED})


#: The record's Firestore switch (#1457 design B, lane ISO-WS-FIRESTORE): the
#: job-made path's twin of terraform/modules/tenancy `worker_firestore_access`
#: and `register-tenant.sh --worker-firestore-access`. The workspace job's
#: guard copies it into its expectation (scripts/lib/workspace-guard.sh
#: `expect`): true, or absent, grants the worker database role at A5 as before
#: the switch existed; false grants none, and A9 then requires the worker to
#: hold no Firestore role. Written by `People` on every admin action that
#: starts a run for the record, from `worker_firestore_access` below.
WORKER_FIRESTORE_ACCESS = "worker_firestore_access"


def worker_firestore_access(tenant_id: str, off_tenants: Iterable[str]) -> bool:
    """False when TENANT_ID's worker is cut over to swarm-api's state routes:
    named in WORKER_FIRESTORE_OFF_TENANTS (`ApiSettings.worker_firestore_off_tenants`),
    or that list is `*`. True otherwise -- an empty list, the default, is
    every workspace's grant exactly as before (owner decision Q7: dev first,
    u-bogdan, then eng, then all, and only then is the role removed)."""
    off = tuple(off_tenants or ())
    return not ("*" in off or tenant_id in off)


def never_dispatched(record: Mapping[str, Any] | None) -> bool:
    """An `approved` record no build was ever sent for: no dispatch attempt and
    no run. Nothing can be making its resources, so it may be migrated or
    denied like a `requested` one. Before 2026-10-10 an approved record had no
    way out but a build claiming it, so one approved while publishing was off
    (w-752763, 07:09Z 2026-10-09) could be neither migrated, denied nor
    retried (#847)."""
    if not record or state_of(record) != APPROVED:
        return False
    return not record.get("dispatch") and not record.get("run")
#: The keys of `limits`, in the order §1.1 lists them.
LIMIT_KEYS = ("max_active", "capacity_units", "quota_pods", "quota_cpu")
#: What `migrate` did, or would do.
MIGRATE_CREATE = "create"
MIGRATE_UPDATE = "update"
MIGRATE_NOTHING = "nothing"

#: The Claude provider. A pool account is a Claude subscription
#: (`quota_broker.accounts.Account.provider` defaults to it) and a provider
#: key in `Tenant.credentials` is named by it.
CLAUDE_PROVIDER = "anthropic"

#: Account states that count as having a Claude account
#: (`quota_broker.accounts.USABLE_STATES`): one that may be assigned, and a
#: paused one, whose work waits rather than failing. DRAINING (being emptied
#: to remove) and REAUTH_REQUIRED (its refresh token is gone) are accounts
#: nothing new can run on, so a person holding only those is told to add one.
USABLE_ACCOUNT_STATES = frozenset({"AVAILABLE", "PAUSED"})

#: Accounts one read of each kind considers: the check needs one.
_ACCOUNT_READ_LIMIT = 20

#: How often a person's `people/` row is written (§6.4): at most once per
#: ten minutes per person, per API instance.
PEOPLE_TOUCH_EVERY_SECONDS = 600.0

#: §4.2's console labels, by step id, for the failure copy's {step}.
STEP_LABELS: Mapping[str, str] = {
    "A1": "Approved", "A2": "Checking the name is free", "A3": "Identity",
    "A4": "Identity", "A5": "Access", "A6": "Access", "A7": "Namespace",
    "A8": "Limits", "A9": "Final check",
}

#: §4.3's failure copy, word for word. Served from the code; the record never
#: stores free-form failure text, so no command output reaches a person.
_RETRY_COPY = (
    "Setting up your workspace stopped at {step}. Nothing half-made can run: your work "
    "stays refused until every step is done. An admin has been shown this and can retry "
    "it; the reference is {request_id}.")
FAILURE_COPY: Mapping[str, str] = {
    "APPLY_FAILED": _RETRY_COPY,
    "GRANT_FAILED": _RETRY_COPY,
    "CONTROL_PLANE_WRITE_FAILED": _RETRY_COPY,
    "CLUSTER_UNREACHABLE": (
        "Your workspace is made except its Kubernetes namespace, because the cluster did "
        "not answer. This is usually brief. An admin can retry it from People."),
    "NAMESPACE_APPLY_FAILED": (
        "Your workspace's Kubernetes namespace could not be applied in full, so it is not "
        "isolated yet and nothing will run in it. An admin can retry it; the reference is "
        "{request_id}."),
    "VERIFY_FAILED": (
        "Every step reported success, but the final check could not find one of your "
        "workspace's objects. Nothing runs until it can. An admin can retry it."),
    "IDENTITY_NOT_OURS": (
        "An identity with your workspace's name already exists and carries access "
        "SwarmCloud never grants, so it was not adopted. Nothing was changed. The platform "
        "owner must look at request {request_id} before this can continue."),
    "WORKSPACE_ID_TAKEN": (
        "The workspace name that belongs to your account is already registered to a "
        "different identity. Nothing was changed. Ask an admin to look at request "
        "{request_id}; this needs a person, not a retry."),
    "WORKSPACE_ID_TERRAFORM": (
        "The workspace name this request was given already belongs to a workspace the "
        "platform's own configuration manages. Nothing was changed, and nothing half-made "
        "can run. Ask an admin to look at request {request_id}; this needs a person, not a "
        "retry."),
    # The personal steps' holds (`needs_owner`, the code on the step): what the
    # job found was made by someone else, and undoing it is the owner's call.
    "STALE_BUCKET_BINDING": (
        "Setting up your workspace stopped at Access, because its identity already holds "
        "storage access SwarmCloud does not grant. Nothing half-made can run: your work "
        "stays refused until every step is done. The platform owner must look at request "
        "{request_id} before this can continue."),
    "SLOT_NOT_OURS": (
        "Setting up your workspace stopped at Access, because the place its GitHub "
        "connection is kept already belongs to another workspace. Nothing half-made can "
        "run: your work stays refused until every step is done. The platform owner must "
        "look at request {request_id} before this can continue."),
    "TWIN_BOUND": (
        "Setting up your workspace stopped at Access, because its identity can already "
        "read a part of its GitHub connection no workspace may read. Nothing half-made can "
        "run: your work stays refused until every step is done. The platform owner must "
        "look at request {request_id} before this can continue."),
    # The codes workspace kinds added (design/workspace-kinds.md §4.8) and the
    # signed approval's. A service's account and front door are the platform
    # owner's to act on (Q2 (a): only the owner approves or retries a service).
    "DIRECTORY_CHECK_STALE": (
        "Setting up your workspace stopped before anything was made, because the check that "
        "its group can be read is more than a day old. Nothing half-made can run: your work "
        "stays refused until every step is done. An admin can retry it, which checks the "
        "group again; the reference is {request_id}."),
    "SERVICE_ACCOUNT_REFUSED": (
        "Setting up your workspace stopped before anything was granted, because its service "
        "account is not one SwarmCloud can admit. Nothing half-made can run: the account "
        "stays refused until every step is done. The platform owner must look at request "
        "{request_id} before this can continue."),
    "FRONT_DOOR_OWNER": (
        "Setting up your workspace stopped at the front door, because admitting its service "
        "account to SwarmCloud's API is a grant only the platform owner can make. Nothing "
        "half-made can run: the account stays refused until every step is done. The "
        "platform owner must act on request {request_id} before this can continue."),
    "FRONT_DOOR_FAILED": (
        "Setting up your workspace stopped at the front door, because the step that admits "
        "its service account to SwarmCloud's API got no answer. Nothing half-made can run: "
        "the account stays refused until every step is done. The platform owner can retry "
        "it; the reference is {request_id}."),
    "NAMESPACE_LABEL_MISSING": (
        "Every step reported success, but the final check found your workspace's Kubernetes "
        "namespace without the label that ties it to your workspace. Nothing half-made can "
        "run: nothing runs in it until the label is there. Ask an admin to look at request "
        "{request_id}; this needs a person, not a retry."),
    "APPROVAL_UNSIGNED": (
        "Setting up your workspace stopped before anything was granted, because its approval "
        "does not carry SwarmCloud's signature. Nothing half-made can run, and nothing was "
        "changed. An admin, or the platform owner for a service account, must deny it and "
        "approve it again as shown; a retry does not help. The reference is {request_id}."),
}
#: A code the table does not know (a later lane's, or a malformed record):
#: still a sentence, never the raw code alone.
_UNKNOWN_FAILURE_COPY = (
    "Setting up your workspace stopped at {step}. An admin can see why in People; the "
    "reference is {request_id}.")

#: §5.2's state phrases: "Your SwarmCloud workspace {phrase}, so ...".
_STATE_PHRASE: Mapping[str, str] = {
    NONE: "is not ready yet",
    REQUESTED: "is waiting for an admin's approval",
    APPROVED: "is being created",
    APPLYING: "is being created",
    NEEDS_OWNER: "is waiting for the platform owner's review",
}

#: `held` on a `requested` record an admin asked for whose personal tenant
#: predates the workspace job (§1.3, §3.3): it is not approved automatically,
#: and never published, because the apply's squat check would fail
#: IDENTITY_NOT_OURS on the identity Terraform made. Lane W9 moves it.
HELD_MIGRATING = "migrating"


class MigrationHold(Exception):
    """Raised by an automatic approval (`People._approve_in`, `auto=True`) on
    a record whose tenant predates the workspace job. Internal, never served:
    `request` turns it into `held`, and the request still succeeds. It is not
    an `ApiError`, because a hold is not a refusal (refusals.py)."""

MIGRATING_COPY = (
    "Your workspace already exists from before self-service setup and is being migrated "
    "into it by the platform owner. Nothing needs doing on your side; it shows as ready "
    "once the migration is done.")

_HELD_PHRASE: Mapping[str, str] = {
    HELD_MIGRATING: "is being migrated by the platform owner",
}

#: The action every WORKSPACE_NOT_READY message ends on (the owner's words in
#: #847: "finish setup — create your workspace").
FINISH_SETUP = "Finish setup: create your workspace."

NO_CLAUDE_ACCOUNT_MESSAGE = (
    "No Claude account yet. Add your own, or ask an admin to lend you one, and then try "
    "again.")

SETUP_COMMAND = "/sc:setup"


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------

_ON = frozenset({"on", "true", "1", "yes"})
_OFF = frozenset({"", "off", "false", "0", "no"})


def gate_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """`WORKSPACE_GATE`: OFF unless it says on (WD8).

    OFF BY DEFAULT because turning it on refuses every personal tenant
    without a `ready` record, which today is every personal tenant: it is
    turned on only after one real approval end to end and a `ready` record
    for every existing personal tenant (§5.5). A value that is neither on nor
    off stops the service at start rather than being guessed at: a typo
    meaning "on" read as off would leave the platform open while saying shut.

    Read here rather than in `ApiSettings` because settings.py is lane W2's
    file in the same phase (§10); moving it there later changes no behaviour.
    """
    raw = (os.environ if environ is None else environ).get("WORKSPACE_GATE", "")
    value = raw.strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise ValueError(f"WORKSPACE_GATE must be 'on' or 'off', got {raw!r}")


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------

def new_workspace_id() -> str:
    """`w-` and six random hex digits. Random, so no email can be hashed to it."""
    return "w-" + secrets.token_hex(3)


def is_service_identity(email: str | None) -> bool:
    """A service account, by the frozen pattern or by its domain (D4)."""
    address = (email or "").strip().lower()
    return bool(SERVICE_ACCOUNT_EMAIL.fullmatch(address)) or is_service_submitter(address)


def personal_tenant_id(email: str) -> str:
    return tenant_id_for_user(email.strip().lower())


def kind_of(record: Mapping[str, Any] | None) -> str:
    """The record's kind. Absent, or anything this build does not know, reads
    PERSONAL: every record before kinds existed is a person's."""
    kind = str((record or {}).get("kind") or "")
    return kind if kind in KINDS else PERSONAL


def tenant_kind_for(kind: str) -> str:
    """The frozen `Tenant.kind` a workspace of `kind` is filed under: a group
    is `group`; a person and a service account are `user`, as
    `u-sw-c90291` and `u-or-9a0792` are today (design §5)."""
    return "group" if kind == GROUP else "user"


def default_display_name(principal: str) -> str:
    """The local part of the address, cut to MAX_DISPLAY_NAME."""
    return principal.split("@", 1)[0][:MAX_DISPLAY_NAME]


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def failure_copy(failure: Mapping[str, Any] | None, request_id: str | None) -> str:
    failure = failure or {}
    step = STEP_LABELS.get(str(failure.get("step") or ""), str(failure.get("step") or "a step"))
    template = FAILURE_COPY.get(str(failure.get("code") or ""), _UNKNOWN_FAILURE_COPY)
    return template.format(step=step, request_id=request_id or "unknown")


def state_of(record: Mapping[str, Any] | None) -> str:
    if not record:
        return NONE
    state = str(record.get("state") or "")
    return state if state in STATES else NONE


def not_ready_message(record: Mapping[str, Any] | None) -> str:
    """§5.2's message, varying with the state."""
    state = state_of(record)
    if state == DENIED:
        reason = str(((record or {}).get("decision") or {}).get("reason") or "").strip()
        phrase = f"was not approved: {reason}" if reason else "was not approved"
    elif state == FAILED:
        phrase = "could not be created: " + failure_copy(
            (record or {}).get("failure"), (record or {}).get("request_id"))
    elif state == REQUESTED and (record or {}).get("held") in _HELD_PHRASE:
        phrase = _HELD_PHRASE[(record or {})["held"]]
    else:
        phrase = _STATE_PHRASE.get(state, _STATE_PHRASE[NONE])
    if state == FAILED:
        # The failure copy is whole sentences, so the refusal follows it as one.
        return (f"Your SwarmCloud workspace {phrase} No task or workflow can start until it "
                f"is. {FINISH_SETUP}")
    return f"Your SwarmCloud workspace {phrase}, so no task or workflow can start. {FINISH_SETUP}"


def setup_url(console_url: str, anchor: str) -> str:
    base = (console_url or "").rstrip("/")
    return f"{base}/setup#{anchor}"


def request_again_at(record: Mapping[str, Any] | None) -> datetime | None:
    """When a denied record may be requested again; None when it is not denied
    or its denial carries no instant."""
    if state_of(record) != DENIED:
        return None
    at = ((record or {}).get("decision") or {}).get("at")
    return at + REREQUEST_WAIT if isinstance(at, datetime) else None


# --------------------------------------------------------------------------
# an approved record nobody is advancing (the 2026-10-09 incident, w-752763)
# --------------------------------------------------------------------------

#: Why an `approved` record is not moving. The three values are the shared
#: contract with the WS-WIRE lane's alert on `jsonPayload.event =
#: "workspace_stuck"`: rename one and that alert stops matching it.
#:
#:   publishing_off        this deployment publishes nothing
#:                         (WORKSPACE_APPLY_PUBLISH off, so People holds a
#:                         `NullWorkspacePublisher`): no build will ever come;
#:   never_dispatched      publishing is on, yet nothing was attempted for this
#:                         request NEVER_DISPATCHED_AFTER after the approval;
#:   dispatched_unclaimed  the first attempt for this request is
#:                         UNCLAIMED_AFTER old and no build has claimed it.
STUCK_PUBLISHING_OFF = "publishing_off"
STUCK_NEVER_DISPATCHED = "never_dispatched"
STUCK_DISPATCHED_UNCLAIMED = "dispatched_unclaimed"
#: 15 minutes: the approval publishes at once and the sweep (every 10 minutes)
#: again, so a record with no attempt after 15 has missed both.
NEVER_DISPATCHED_AFTER = timedelta(minutes=15)
#: 30 minutes from the FIRST attempt for this request, not the last: the
#: sweep re-publishes every 10 minutes, so "the last attempt is 30 minutes
#: old" never comes true while it runs, which is exactly when a build that
#: never claims anything has to be noticed.
UNCLAIMED_AFTER = timedelta(minutes=30)


def approved_at(record: Mapping[str, Any] | None) -> datetime | None:
    """When the record last became `approved`: the later of the admin's
    approval and a retry, else (a record written by hand) its request."""
    record = record or {}
    decision = record.get("decision") or {}
    at = decision.get("at") if decision.get("verdict") == APPROVED else None
    seen = [v for v in (at, (record.get("retry") or {}).get("at")) if isinstance(v, datetime)]
    if seen:
        return max(seen)
    requested = record.get("requested_at")
    return requested if isinstance(requested, datetime) else None


def first_dispatched_at(record: Mapping[str, Any] | None) -> datetime | None:
    """The first dispatch attempt for the record's CURRENT request, or None.
    An attempt recorded for an earlier request id (before a retry) is not
    one; a `dispatch` written before it carried a request id counts by its
    own first (or last) attempt."""
    record = record or {}
    dispatch = record.get("dispatch") or {}
    if not dispatch:
        return None
    if "request_id" in dispatch and dispatch.get("request_id") != record.get("request_id"):
        return None
    at = dispatch.get("first_attempt_at") or dispatch.get("last_attempt_at")
    return at if isinstance(at, datetime) else None


def waiting_because(record: Mapping[str, Any] | None, *, publishing: bool,
                    now: datetime) -> str | None:
    """The stuck rule. None unless the record is `approved` and either
    publishing is off, or no attempt was made NEVER_DISPATCHED_AFTER after the
    approval, or the first attempt is UNCLAIMED_AFTER old (the state is still
    `approved`, so no build claimed it). A record approved a minute ago is
    not stuck: it is being sent."""
    if state_of(record) != APPROVED:
        return None
    if not publishing:
        return STUCK_PUBLISHING_OFF
    first = first_dispatched_at(record)
    if first is None:
        since = approved_at(record)
        if since is not None and now - since > NEVER_DISPATCHED_AFTER:
            return STUCK_NEVER_DISPATCHED
        return None
    if now - first > UNCLAIMED_AFTER:
        return STUCK_DISPATCHED_UNCLAIMED
    return None


def provisioning(record: Mapping[str, Any] | None, *, publishing: bool,
                 now: datetime) -> dict[str, Any]:
    """The `provisioning` block of a view, computed on read: whether this
    deployment builds workspaces at all, why an approved record is waiting
    (None when it is not), and how long ago it was approved (None when it is
    not approved). No secret, email or tenant id."""
    since = approved_at(record) if state_of(record) == APPROVED else None
    return {
        "available": bool(publishing),
        "waiting_because": waiting_because(record, publishing=publishing, now=now),
        "approved_minutes_ago": None if since is None
        else max(0, int((now - since).total_seconds() // 60)),
    }


def view(record: Mapping[str, Any] | None, *, console_url: str = "",
         publishing: bool | None = None, now: datetime | None = None) -> dict[str, Any]:
    """What a person is shown of their OWN record (`GET /v1/workspace`).

    Never the principal and never `decision.by`: the first the caller
    already knows, the second is an admin's address, which never leaves
    Firestore. The failure's copy is served from its code (§4.3).

    `publishing` is the deployment's publisher's `enabled`; given (with
    `now`), the view carries the `provisioning` block, so an approved record
    that nothing will advance says so instead of reading as in progress."""
    state = state_of(record)
    out: dict[str, Any] = {
        "state": state,
        "kind": kind_of(record),
        "setup_url": setup_url(console_url, "workspace"),
        "setup_command": SETUP_COMMAND,
    }
    if publishing is not None and now is not None:
        out["provisioning"] = provisioning(record, publishing=publishing, now=now)
    if record is None:
        return out
    decision = record.get("decision") or None
    failure = record.get("failure") or None
    again = request_again_at(record)
    out.update({
        "workspace_id": record.get("workspace_id"),
        "tenant_id": record.get("tenant_id"),
        "display_name": record.get("display_name"),
        "request_id": record.get("request_id"),
        "requested_at": _iso(record.get("requested_at")),
        "requested_via": record.get("requested_via"),
        "decision": None if not decision else {
            "verdict": decision.get("verdict"),
            "reason": decision.get("reason"),
            "at": _iso(decision.get("at")),
            # True when nobody clicked: the requester is an admin (§1.3).
            "auto": decision.get("auto") is True,
        },
        "held": {"reason": HELD_MIGRATING, "copy": MIGRATING_COPY}
        if state == REQUESTED and record.get("held") == HELD_MIGRATING else None,
        "limits": dict(record.get("limits") or {}),
        "steps": {k: {**v, "at": _iso(v.get("at"))} if isinstance(v, dict) else v
                  for k, v in (record.get("steps") or {}).items()},
        "failure": None if not failure else {
            "code": failure.get("code"),
            "step": failure.get("step"),
            "retryable": failure.get("retryable"),
            "at": _iso(failure.get("at")),
            "copy": failure_copy(failure, record.get("request_id")),
        },
        "ready_at": _iso(record.get("ready_at")),
        "request_again_at": _iso(again),
    })
    return out


# --------------------------------------------------------------------------
# an admin's request, per kind (docs/design/workspace-kinds.md §4.2.3)
# --------------------------------------------------------------------------

#: Google-managed defaults that are never a tenant: the default compute and
#: App Engine accounts and the Cloud Build agent (check 10). None is in this
#: project's `iam.gserviceaccount.com` domain, so check 9 would refuse them
#: too; naming them gives the refusal that says what they are.
GOOGLE_MANAGED_SA_DOMAINS = frozenset({
    "developer.gserviceaccount.com",
    "appspot.gserviceaccount.com",
    "cloudbuild.gserviceaccount.com",
})

#: The platform's own accounts all start with this (`swarm-api`,
#: `swarm-scheduler`, `swarm-reconciler`, `swarm-workspace-deployer`,
#: `swarm-workspace-dispatch`, every `swarm-agent-worker-*`, `swarm-verify`).
PLATFORM_SA_PREFIX = "swarm-"


@dataclass(frozen=True)
class KindRules:
    """What §4.2.3's checks read from the deployment's settings. Built once
    per request from `ApiSettings` (`from_settings`); a test builds one by
    hand. Holds no credential and no client."""

    project_id: str
    allowed_domains: tuple[str, ...]
    allowed_users: tuple[str, ...] = ()
    admin_groups: tuple[str, ...] = ()
    tenant_groups: tuple[str, ...] = ()
    secret_admins: tuple[str, ...] = ()
    #: TENANT_SERVICE_ACCOUNTS' emails: accounts a tenant already lists.
    listed_services: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Any) -> "KindRules":
        def lower(values: Any) -> tuple[str, ...]:
            return tuple(v.strip().lower() for v in (values or ()) if str(v).strip())

        return cls(
            project_id=settings.project_id,
            allowed_domains=lower(settings.core.allowed_domains),
            allowed_users=lower(getattr(settings, "allowed_users", ())),
            admin_groups=lower(settings.admin_groups),
            tenant_groups=lower(settings.tenant_groups),
            secret_admins=lower(getattr(settings, "secret_admin_principals", ())),
            listed_services=tuple(m.email for m in
                                  getattr(settings, "tenant_service_accounts", ())),
        )

    def terraform_ids(self) -> frozenset[str]:
        """The ids of the tenants Terraform owns, as swarm-api knows them: each
        TENANT_GROUPS entry, and each service account ALLOWED_USERS admits
        (the Terraform service tenants). A person in ALLOWED_USERS is not
        here: their personal tenant is made on first sight, not by Terraform.
        The tenant document's `managed_by` is the other half (`People`)."""
        ids = {tenant_id_for_group(g) for g in self.tenant_groups}
        ids |= {tenant_id_for_user(u) for u in self.allowed_users if is_service_identity(u)}
        return frozenset(ids)


def validate_kind_request(kind: str, principal: str, rules: KindRules) -> tuple[str, str]:
    """§4.2.3's checks that read no stored state: the principal's shape for
    its kind, and the Terraform ids swarm-api is configured with. Returns
    (tenant id, normalised principal). Raises the refusal; writes nothing.

    The shared deny-list is NOT restated here: the job's S1 enforces it
    through `scripts/lib/common.sh`, the one place it lives (rule 2), and a
    second copy is the copy that drifts."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    address = (principal or "").strip().lower()
    local, _, domain = address.partition("@")
    if not local or not domain or "@" in domain or any(c.isspace() for c in address):
        raise ValidationFailed("principal must be one email address")
    try:
        tenant_id_for_user(address)
    except ValueError:
        raise ValidationFailed("no workspace name can be derived from that address") from None
    allowed = set(rules.allowed_domains)
    if kind == PERSONAL:
        if is_service_identity(address):
            raise WorkspaceNotForServiceAccounts(
                "A personal workspace is a person's. A service account gets a service "
                "workspace instead.")
        if domain not in allowed and address not in rules.allowed_users:
            raise WorkspaceDomainNotAllowed(
                f"{domain or 'that address'} is not an allowed domain, so this person "
                "cannot sign in, and a workspace would admit nobody.")
        if address in rules.secret_admins:
            raise WorkspacePrincipalForbidden(
                "This identity administers every tenant's provider-key secrets, so it may "
                "not own a tenant or a workspace of its own.")
        tenant_id = tenant_id_for_user(address)
    elif kind == GROUP:
        if is_service_identity(address) or domain not in allowed:
            raise WorkspaceDomainNotAllowed(
                f"{domain or 'that address'} is not an allowed domain. A group workspace "
                "is for a Google group in one.")
        if address in rules.admin_groups:
            raise WorkspaceGroupIsAdmin(
                "That group is an admin group. As a tenant it would mix the admin lookup "
                "with the tenant lookup, which are kept apart; use another group.")
        tenant_id = tenant_id_for_group(address)
        if tenant_id.startswith("u-"):
            raise WorkspaceGroupIdReserved(
                f"The group's workspace name would be {tenant_id!r}, and names starting "
                "'u-' are people's own workspaces. Use a group whose address does not "
                "start with 'u-'.")
    else:
        if domain in GOOGLE_MANAGED_SA_DOMAINS:
            raise WorkspaceServicePlatform(
                "That is a Google-managed default account, which is never a tenant.")
        if not SERVICE_ACCOUNT_EMAIL.fullmatch(address) \
                or domain != f"{rules.project_id}.iam.gserviceaccount.com":
            raise WorkspaceServiceNotInProject(
                f"A service workspace is for a service account of {rules.project_id}, "
                "where the setup job can check it exists and holds no key. That address "
                "is not one.")
        if address.startswith(PLATFORM_SA_PREFIX):
            raise WorkspaceServicePlatform(
                "That is one of the platform's own accounts ('swarm-…'), which never "
                "files work as a tenant.")
        if address in rules.secret_admins:
            raise WorkspacePrincipalForbidden(
                "This identity administers every tenant's provider-key secrets, so it may "
                "not own a tenant or a workspace of its own.")
        if address in rules.allowed_users or address in rules.listed_services:
            raise WorkspaceServiceAlreadyResolves(
                "That account already resolves to a tenant (ALLOWED_USERS or a tenant's "
                "service-account listing), so a workspace would give it a second one.")
        tenant_id = tenant_id_for_user(address)
    if tenant_id in rules.terraform_ids():
        raise WorkspaceIdTerraform(
            f"{tenant_id!r} is one of the tenants Terraform owns, and the setup job never "
            "builds over those. Nothing was changed.")
    return tenant_id, address


def nested(new_id: str, existing_id: str) -> bool:
    """Whether two tenant ids nest through `-git-`, so one tenant's forge
    user-slot names (`swarm-tenant-<id>-git-u-…`) would start with the
    other's (`forge_user_slots.tf::forge_nested_tenants`, applied here over
    every stored id rather than `var.tenants` only)."""
    if not new_id or not existing_id or new_id == existing_id:
        return False
    return new_id.startswith(existing_id + "-git-") or existing_id.startswith(new_id + "-git-")


# --------------------------------------------------------------------------
# the signed approval (lane WK-SIGNED-APPROVAL of #847, owner 2026-10-19)
# --------------------------------------------------------------------------
#
# R8 of docs/design/workspace-kinds.md §4.5: tenant workers hold Firestore
# write across the database (terraform/modules/tenancy/main.tf), so a worker
# can write a record whose `decision.by` names the platform owner, and the
# job would admit that service account at the front door. So swarm-api signs
# every approval and every retry, with the step-spec KMS key only it may sign
# with (`specsigning.KmsSpecSigner`, SPEC_SIGNING_KEY_VERSION), and the job
# (images/workspace-apply/entry.py) verifies the signature before any step
# that grants. A worker can write the record, never the signature: it holds
# no `signer` on the key and cannot make swarm-api sign (approve and retry
# are admin routes), and the purpose inside the bytes keeps any step-spec
# signature swarm-api makes for it from reading as an approval.

#: Domain separation: inside the signed bytes, never a step spec's
#: (`specsign.SPEC_PURPOSE`). A change to the fields or their encoding is a
#: new purpose, never an edit to this one.
APPROVAL_PURPOSE = "swarm.workspace-approval/v1"
#: What an approval signature covers. entry.py restates these (it runs on the
#: image's bare python3) and a unit test holds the two byte for byte equal.
APPROVAL_FIELDS = ("workspace_id", "tenant_id", "kind", "principal",
                   "decided_by", "decided_at", "request_id")
#: The record's field the signature and what it covers are stored in.
APPROVAL = "approval"


def canonical_approval(fields: Mapping[str, Any]) -> bytes:
    """JCS of {"purpose": APPROVAL_PURPOSE, <APPROVAL_FIELDS>}: the bytes signed.

    Every field is a non-empty string. Anything else is a ValueError, never
    signed as something the job would read differently."""
    payload: dict[str, str] = {"purpose": APPROVAL_PURPOSE}
    for key in APPROVAL_FIELDS:
        value = fields.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"an approval's {key} must be a non-empty string")
        payload[key] = value
    return specsign.jcs(payload)


def sign_approval(signer: Any, record: Mapping[str, Any], *, decided_by: str,
                  decided_at: datetime, request_id: str) -> dict[str, Any] | None:
    """The record's `approval` map for an approval or retry by DECIDED_BY.

    Covers the record's workspace id, tenant id, kind and principal as they
    stand, who decided, when, and the request id the job will run. None when
    no signer is configured, which only local development and tests allow
    (`specsigning.signer_from_settings` refuses to start a deployed swarm-api
    without one); the job then stops a group or service record for the owner
    (APPROVAL_UNSIGNED) and runs a personal one as before.

    A KMS failure is `SpecSigningUnavailable` (503), raised inside the
    caller's transaction, so the approval is not written at all."""
    fields = {
        "workspace_id": str(record.get("workspace_id") or ""),
        "tenant_id": str(record.get("tenant_id") or ""),
        "kind": kind_of(record),
        "principal": str(record.get("principal") or ""),
        "decided_by": decided_by,
        "decided_at": decided_at.isoformat(),
        "request_id": request_id,
    }
    canonical = canonical_approval(fields)
    if signer is None:
        log.warning("no SPEC_SIGNING_KEY_VERSION: workspace %s approved UNSIGNED; the job "
                    "refuses a group or service record without a signature",
                    fields["workspace_id"])
        return None
    try:
        signed = signer.sign(specsign.spec_digest(canonical))
    except SpecSigningUnavailable:
        raise
    except Exception as exc:
        raise SpecSigningUnavailable(
            "the approval could not be signed (Cloud KMS); nothing was changed") from exc
    return {
        "decided_by": fields["decided_by"],
        "decided_at": fields["decided_at"],
        "request_id": request_id,
        "key_version": signed.key_version,
        "signature": base64.b64encode(signed.signature).decode("ascii"),
    }


def approval_refusal(record: Mapping[str, Any], settings: Any) -> str | None:
    """Why the record's stored `approval` is not swarm-api's, or None when it is.

    entry.py's `approval_payload` and `verify_approval`, over the platform's
    SPEC_VERIFY_KEYS (`verdictpublish`'s reading of them) instead of a Cloud
    KMS read: swarm-api holds `signer` on the key, never `publicKeyViewer`.
    `People.retry` asks this before it signs a group or service record
    afresh, so a retry re-signs only a decision swarm-api signed before and
    never one a worker wrote (R8). Unset or malformed keys verify nothing:
    every such retry is refused. The reason names the check, never a value.
    """
    from .verdictpublish import _VERSION_SUFFIX, _signature_matches, _verify_keys

    approval = record.get(APPROVAL)
    if not isinstance(approval, Mapping):
        return "the record carries no approval signature"
    decision = record.get("decision") if isinstance(record.get("decision"), Mapping) else {}
    retry = record.get("retry") if isinstance(record.get("retry"), Mapping) else {}
    decided_by = approval.get("decided_by")
    deciders = {str(who).strip().lower() for who in (decision.get("by"), retry.get("by")) if who}
    if not isinstance(decided_by, str) or decided_by not in deciders:
        return "the signed approver is not the record's approver"
    if approval.get("request_id") != record.get("request_id"):
        return "the signature is for another request than the record's"
    signing = str(getattr(settings, "spec_signing_key_version", "") or "")
    matched = _VERSION_SUFFIX.search(signing)
    crypto_key = signing[:matched.start()] if matched and matched.end() == len(signing) else ""
    keys = _verify_keys(settings)
    version = approval.get("key_version")
    if (not crypto_key or not isinstance(version, str) or not version.startswith(crypto_key)
            or not _VERSION_SUFFIX.fullmatch(version[len(crypto_key):]) or version not in keys):
        return "the approval is signed by a key version this platform does not trust"
    try:
        canonical = canonical_approval({
            "workspace_id": record.get("workspace_id"),
            "tenant_id": record.get("tenant_id"),
            "kind": kind_of(record),
            "principal": record.get("principal"),
            "decided_by": decided_by,
            "decided_at": approval.get("decided_at"),
            "request_id": record.get("request_id"),
        })
    except (ValueError, UnicodeEncodeError):
        return "the approval has no canonical form"
    if not _signature_matches(keys[version], approval.get("signature"),
                              specsign.spec_digest(canonical)):
        return "the approval's signature does not verify over the record"
    return None


# --------------------------------------------------------------------------
# the store of records
# --------------------------------------------------------------------------

class Workspaces:
    """The records, the request, the loan request, the account check and the gate.

    `gate` is `WORKSPACE_GATE`, read once at construction (`gate_from_env`)
    unless a caller passes it."""

    def __init__(
        self,
        db: Any,
        *,
        now: Callable[[], datetime],
        gate: bool | None = None,
        new_id: Callable[[], str] = new_workspace_id,
        console_url: str = "",
    ) -> None:
        self._db = db
        self._now = now
        self.gate = gate_from_env() if gate is None else gate
        self._new_id = new_id
        self.console_url = console_url
        self._touched: dict[str, float] = {}
        self._touch_lock = threading.Lock()

    # -- reads ---------------------------------------------------------------

    def get(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(WORKSPACES).document(tenant_id).get()
        return snap.to_dict() if snap.exists else None

    def loan_request(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(LOAN_REQUESTS).document(tenant_id).get()
        return snap.to_dict() if snap.exists else None

    def claude_accounts(self, tenant_id: str, tenant: Tenant | None) -> dict[str, Any]:
        """§5.1 (3): an active pool account the tenant owns, one lent to it, or
        a provider key in its `credentials` (only `u-bogdan` holds one, §3.3).

        Counts only; no account id, label or secret name is returned."""
        def usable(field: str, op: str) -> int:
            query = self._db.collection(ACCOUNTS).where(
                filter=FieldFilter(field, op, tenant_id)).limit(_ACCOUNT_READ_LIMIT)
            count = 0
            for snap in query.stream():
                data = snap.to_dict() or {}
                if data.get("provider", CLAUDE_PROVIDER) != CLAUDE_PROVIDER:
                    continue
                if str(data.get("state") or "AVAILABLE") not in USABLE_ACCOUNT_STATES:
                    continue
                if field == "lend_to" and data.get("owner_tenant") == tenant_id:
                    continue  # counted once, as owned
                count += 1
            return count

        own = usable("owner_tenant", "==")
        lent = usable("lend_to", "array_contains")
        key = tenant is not None and tenant.tenant_id == tenant_id and \
            CLAUDE_PROVIDER in (tenant.credentials or [])
        return {"own": own, "lent": lent, "provider_key": key,
                "has_account": bool(own or lent or key)}

    # -- who the gate applies to ---------------------------------------------

    @staticmethod
    def applies_to(ctx: Any, tenant: Tenant) -> bool:
        """Whether a submission by `ctx` into `tenant` is a PERSON's into their
        OWN tenant -- the only kind the gate judges (WD7).

        Exempt: a group tenant (a member of `eng` submits as `eng` at once);
        a service account, by the frozen pattern; a listed continuation,
        rollup or tenant-member identity. Their tenants are declared in
        `dev.tfvars`, and their callers cannot click a button."""
        if tenant.kind == "group":
            return False
        if is_service_identity(getattr(ctx, "email", "")):
            return False
        if getattr(ctx, "member_scope", "") or getattr(ctx, "is_rollup_sweeper", False) \
                or getattr(ctx, "tenant_member", ""):
            return False
        return True

    def withholds_tenant(self, ctx: Any) -> bool:
        """With the gate on, `tenant_for` does not WRITE a person's tenant on
        first sight: the workspace job makes it (A8). True for a human caller
        resolving to their own personal tenant."""
        if not self.gate:
            return False
        email = (getattr(ctx, "email", "") or "").strip().lower()
        if not email or is_service_identity(email):
            return False
        if getattr(ctx, "member_scope", "") or getattr(ctx, "is_rollup_sweeper", False) \
                or getattr(ctx, "tenant_member", ""):
            return False
        return getattr(ctx, "tenant_id", None) == personal_tenant_id(email)

    def check(self, ctx: Any, tenant: Tenant) -> None:
        """The gate (§5.1). Reads nothing when it is off.

        Refuses with WORKSPACE_NOT_READY until the record is `ready`, then
        with NO_CLAUDE_ACCOUNT until the tenant has a Claude account, for
        every runner profile (WD6, confirmed by the owner 2026-10-08)."""
        if not self.gate or not self.applies_to(ctx, tenant):
            return
        record = self.get(tenant.tenant_id)
        state = state_of(record)
        if state != READY:
            log.info("workspace gate refused: workspace=%s state=%s",
                     (record or {}).get("workspace_id") or "-", state)
            raise WorkspaceNotReady(not_ready_message(record), detail={
                "workspace_id": (record or {}).get("workspace_id"),
                "state": state,
                "setup_url": setup_url(self.console_url, "workspace"),
                "setup_command": SETUP_COMMAND,
            })
        if not self.claude_accounts(tenant.tenant_id, tenant)["has_account"]:
            log.info("workspace gate refused: workspace=%s no claude account",
                     record.get("workspace_id") if record else "-")
            raise NoClaudeAccount(NO_CLAUDE_ACCOUNT_MESSAGE, detail={
                "setup_url": setup_url(self.console_url, "claude-account"),
                "setup_command": SETUP_COMMAND,
            })

    # -- the request (§1.3) ----------------------------------------------------

    def request(
        self,
        *,
        tenant_id: str,
        principal: str,
        via: str,
        approve: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None,
        kind: str = PERSONAL,
        requested_by: str | None = None,
        display_name: str | None = None,
        limits: Mapping[str, int] | None = None,
        extra: Mapping[str, Any] | None = None,
        on_create: Callable[[Any, dict[str, Any]], None] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """One Firestore transaction: read the record, then create, re-open or
        answer it. Returns (HTTP status, the record as stored).

        The caller has already been checked as a human in an allowed domain,
        not a secret admin, and owning `tenant_id` (`routes.workspaces`).

        `approve` is given for an admin's own request (§1.3, owner decision
        2026-10-09): `People.request_own` passes `People._approve_in`, the
        approval the admin route makes, and it runs here, inside this
        transaction, on the record as it stands `requested`. It writes its
        audit entry and returns the record's patch, which is written with the
        request in one write; a `MigrationHold` from it leaves the record
        `requested` and `held` instead (§3.3).

        KINDS (docs/design/workspace-kinds.md §4.1). A person's own request
        passes none of the kind arguments and is unchanged. An admin's request
        (`People.create`) names the `kind`, who filed it (`requested_by`), a
        `display_name`, the kind's `limits` and the kind's own fields
        (`extra`: a group's `directory`). An existing record of another kind
        or principal under the same tenant id is WORKSPACE_ID_TAKEN, read in
        this transaction. `on_create` writes the audit entry of a request no
        approval goes with, only when this call creates or re-opens the
        record, so a repeated request writes nothing."""
        if via not in (*VIAS, ADMIN_VIA):
            raise ValueError(f"via must be one of {VIAS} or {ADMIN_VIA!r}")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        ref = self._db.collection(WORKSPACES).document(tenant_id)
        now = self._now()
        transaction = self._db.transaction()

        def _decide(txn: Any, record: dict[str, Any]) -> dict[str, Any]:
            """The patch an automatic approval adds to a `requested` record,
            or the hold; nothing for a request no approval goes with."""
            if approve is None:
                return {}
            try:
                return approve(txn, record)
            except MigrationHold:
                return {"held": HELD_MIGRATING}

        @firestore.transactional
        def _apply(txn: Any) -> tuple[int, dict[str, Any]]:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            state = state_of(data) if data is not None else None
            if data is not None and (
                    kind_of(data) != kind
                    or str(data.get("principal") or "").strip().lower() != principal):
                # Never the other principal or kind: the record is theirs.
                raise WorkspaceIdTaken(
                    "That workspace name is already recorded for a different identity or "
                    "kind. Nothing was changed; this needs a person, not a retry.",
                    detail={"workspace_id": data.get("workspace_id")})
            if data is None:
                id_ref = self._draw_id(txn)
                record = self._new_record(tenant_id=tenant_id, workspace_id=id_ref.id,
                                          principal=principal, now=now, via=via, kind=kind,
                                          requested_by=requested_by,
                                          display_name=display_name, limits=limits)
                record.update(dict(extra or {}))
                # Every read (the approval's included) before the first write.
                record.update(_decide(txn, record))
                if on_create is not None and record.get("state") == REQUESTED:
                    on_create(txn, record)
                txn.set(id_ref, {"tenant_id": tenant_id})
                txn.set(ref, record)
                return 202, record
            if state == REQUESTED and approve is not None:
                patch = {k: v for k, v in _decide(txn, data).items() if data.get(k) != v}
                if patch:
                    txn.update(ref, patch)
                    # Approving a standing request is this call's doing; a
                    # hold recorded on it is not a new request.
                    status = 202 if patch.get("state") == APPROVED else 200
                    return status, {**data, **patch}
                return 200, data
            if state in UNCHANGED_ON_REQUEST:
                return 200, data
            if state == DENIED:
                again = request_again_at(data)
                if again is not None and now < again:
                    raise WorkspaceRequestTooSoon(
                        "Your workspace request was not approved, and it can be asked for "
                        "again 24 hours after that decision. An admin can still approve "
                        "it before then.",
                        detail={"request_again_at": again.isoformat(),
                                "workspace_id": data.get("workspace_id")})
                previous = {"request_id": data.get("request_id"),
                            "requested_at": data.get("requested_at"),
                            "decision": data.get("decision")}
                patch = {
                    "state": REQUESTED,
                    "request_id": str(uuid.uuid4()),
                    "requested_at": now,
                    "requested_via": via,
                    "decision": None,
                    "history": [*(data.get("history") or []), previous],
                }
                if requested_by is not None:
                    patch["requested_by"] = requested_by
                patch.update(dict(extra or {}))
                patch.update(_decide(txn, {**data, **patch}))
                if on_create is not None and patch.get("state") == REQUESTED:
                    on_create(txn, {**data, **patch})
                txn.update(ref, patch)
                return 202, {**data, **patch}
            if state == FAILED:
                raise WorkspaceFailed(
                    failure_copy(data.get("failure"), data.get("request_id")),
                    detail={"workspace_id": data.get("workspace_id"), "state": FAILED})
            raise Conflict("your workspace record is in a state this request cannot change",
                           detail={"workspace_id": data.get("workspace_id")})

        status, record = _apply(transaction)
        log.info("workspace request workspace=%s state=%s status=%d via=%s",
                 record.get("workspace_id"), record.get("state"), status, via)
        return status, record

    # -- the shape of a new record, shared by the request and the migration --

    def _draw_id(self, txn: Any) -> Any:
        """The ref of a `workspace_ids/` entry no record holds, read in `txn`.
        A drawn id whose index document exists is drawn again (§1.3)."""
        for _ in range(MAX_ID_DRAWS):
            id_ref = self._db.collection(WORKSPACE_IDS).document(self._new_id())
            if not _snapshot(txn.get(id_ref)).exists:
                return id_ref
        raise Conflict("no free workspace id could be drawn; try again")

    @staticmethod
    def _new_record(*, tenant_id: str, workspace_id: str, principal: str,
                    now: datetime, via: str, kind: str = PERSONAL,
                    requested_by: str | None = None, display_name: str | None = None,
                    limits: Mapping[str, int] | None = None) -> dict[str, Any]:
        """§1.1's record as a request creates it, in `requested`, with the
        kind fields of docs/design/workspace-kinds.md §4.1. `requested_by` is
        the person themselves for their own request; it never leaves
        Firestore (`view` does not serve it)."""
        return {
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "kind": kind,
            "principal": principal,
            "requested_by": requested_by or principal,
            "display_name": display_name or default_display_name(principal),
            "state": REQUESTED,
            "request_id": str(uuid.uuid4()),
            "requested_at": now,
            "requested_via": via,
            "decision": None,
            "history": [],
            "limits": dict(limits or DEFAULT_LIMITS_BY_KIND[kind]),
            "run": None,
            "steps": {},
            "failure": None,
            "ready_at": None,
            "migrated": False,
        }

    # -- the migration of a Terraform-era tenant (§3.3) -----------------------

    def migrate(
        self,
        tenant_id: str,
        *,
        quota_pods: int,
        quota_cpu: int,
        apply: bool,
        expect: str | None = None,
    ) -> dict[str, Any]:
        """Make `workspaces/{tenant_id}` the `ready`, `migrated` record of a
        personal tenant whose resources Terraform made (§3.3). One transaction.

        Read from `tenants/{tenant_id}`, which must exist and be a personal
        tenant whose principal derives to `tenant_id`: the principal, the
        tenant's live `max_active` and `capacity_units`, and its provider
        keys (`credentials`) as `providers`. `quota_pods` and `quota_cpu` are
        the namespace's live ResourceQuota, which the caller read: the verify
        run compares the record's limits with both.

        * No record: created as a request would create it, with a FRESH
          workspace id and its `workspace_ids/` entry, then made ready.
        * `requested`, `failed` or `denied`, or `approved` with no dispatch
          attempt and no run (`never_dispatched`): completed IN PLACE. The
          workspace id, its index entry and the request id are kept; an
          earlier decision moves to `history`.
        * `ready` and `migrated`: nothing is written (a re-run).
        * anything else is refused, and nothing is written.

        Both writes record `decision` {by: MIGRATION_BY, verdict: approved,
        reason: MIGRATION_REASON} and one `admin_audit` entry, in the same
        transaction. `ready_at` stays empty: the verify run writes it once it
        has read every object back (§4.2, A9).

        `apply=False` reads and writes nothing; it returns what `apply=True`
        would write (a created record's id is drawn again then). `expect` is
        the action a dry run reported: if the record moved since, the write
        is refused rather than doing something nobody saw.

        Returns {action, workspace_id, before, after}. No route calls this."""
        if expect is not None and expect not in (MIGRATE_CREATE, MIGRATE_UPDATE, MIGRATE_NOTHING):
            raise ValueError(f"expect must be one of create, update, nothing; got {expect!r}")
        quota = {"quota_pods": quota_pods, "quota_cpu": quota_cpu}
        for name, value in quota.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a whole number above 0, got {value!r}")
        ref = self._db.collection(WORKSPACES).document(tenant_id)
        tenant_ref = self._db.collection(TENANTS).document(tenant_id)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            before = snap.to_dict() if snap.exists else None
            state = state_of(before) if before is not None else None
            if before is not None and state == READY and before.get("migrated") is True:
                action = MIGRATE_NOTHING
            elif before is None:
                action = MIGRATE_CREATE
            elif state in MIGRATABLE or never_dispatched(before):
                action = MIGRATE_UPDATE
            else:
                raise Conflict(
                    f"workspace {before.get('workspace_id')} is {state}"
                    + ("" if state != READY else " and was not migrated")
                    + "; only a requested, failed or denied record, an approved one no build "
                    "was ever sent for, or none, is migrated",
                    detail={"workspace_id": before.get("workspace_id"), "state": state})
            if expect is not None and action != expect:
                raise Conflict(
                    f"the record changed since the dry run: it said {expect}, it is now {action}; "
                    "nothing was written. Run it again and read the new plan.",
                    detail={"workspace_id": (before or {}).get("workspace_id")})
            if action == MIGRATE_NOTHING:
                return {"action": action, "workspace_id": before.get("workspace_id"),
                        "before": before, "after": before}

            tenant_snap = _snapshot(txn.get(tenant_ref))
            tenant = tenant_snap.to_dict() if tenant_snap.exists else None
            if not tenant:
                raise Conflict(f"tenants/{tenant_id} does not exist; there is nothing to migrate")
            principal = str(tenant.get("principal") or "").strip().lower()
            if tenant.get("kind") != "user" or not principal \
                    or personal_tenant_id(principal) != tenant_id:
                raise Conflict(f"tenants/{tenant_id} is not a personal tenant whose principal "
                               "derives to its id; only those are migrated")
            if before is not None and \
                    str(before.get("principal") or "").strip().lower() != principal:
                raise Conflict("the workspace record and the tenant name different principals",
                               detail={"workspace_id": before.get("workspace_id")})
            limits = {"max_active": tenant.get("max_active"),
                      "capacity_units": tenant.get("capacity_units"), **quota}
            for name in LIMIT_KEYS:
                value = limits[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise Conflict(f"tenants/{tenant_id} has no whole {name} above 0 "
                                   f"(read {value!r})")

            id_ref = None
            if before is None:
                id_ref = self._draw_id(txn)
                base = self._new_record(tenant_id=tenant_id, workspace_id=id_ref.id,
                                        principal=principal, now=now, via=MIGRATION_VIA)
            else:
                base = before
            history = list(base.get("history") or [])
            if base.get("decision"):
                history.append({"request_id": base.get("request_id"),
                                "requested_at": base.get("requested_at"),
                                "decision": base.get("decision")})
            patch: dict[str, Any] = {
                "state": READY,
                "migrated": True,
                "providers": sorted({str(p) for p in (tenant.get("credentials") or [])}),
                "limits": {name: limits[name] for name in LIMIT_KEYS},
                "decision": {"by": MIGRATION_BY, "at": now, "verdict": APPROVED,
                             "reason": MIGRATION_REASON},
                "history": history,
                "failure": None,
                "steps": {},
                "ready_at": None,
            }
            after = {**base, **patch}
            if apply:
                if id_ref is not None:
                    txn.set(id_ref, {"tenant_id": tenant_id})
                    txn.set(ref, after)
                else:
                    txn.update(ref, patch)
                audit = self._db.collection(AUDIT_COLLECTION).document()
                txn.set(audit, {"action": "migrate",
                                "target_workspace_id": after["workspace_id"],
                                "by": MIGRATION_BY, "at": now,
                                "detail": {"from_state": state or NONE,
                                           "request_id": after.get("request_id"),
                                           "reason": MIGRATION_REASON}})
            return {"action": action, "workspace_id": after["workspace_id"],
                    "before": before, "after": after}

        result = _apply(transaction)
        log.info("workspace migration workspace=%s action=%s applied=%s",
                 result["workspace_id"], result["action"], apply)
        return result

    # -- the loan request (§6.1) ---------------------------------------------

    def request_loan(self, *, tenant_id: str, principal: str, via: str) -> tuple[int, dict[str, Any]]:
        """`loan_requests/{tenant_id}`: idempotent. An open request is answered
        as it is; a closed one (an admin's, lane W7) is re-opened with its
        predecessor kept in `history`. Needs a workspace record: admins see
        the request against the workspace id, in People."""
        if via not in VIAS:
            raise ValueError(f"via must be one of {VIAS}")
        workspace_ref = self._db.collection(WORKSPACES).document(tenant_id)
        ref = self._db.collection(LOAN_REQUESTS).document(tenant_id)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[int, dict[str, Any]]:
            workspace = _snapshot(txn.get(workspace_ref))
            if not workspace.exists:
                raise WorkspaceNotRequested(
                    "Request your workspace first: a Claude account is lent to a workspace.",
                    detail={"setup_url": setup_url(self.console_url, "workspace")})
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is not None and data.get("state") == REQUESTED:
                return 200, data
            history = [*(data.get("history") or []), {
                k: data.get(k) for k in ("request_id", "requested_at", "state", "decision")
            }] if data is not None else []
            record = {
                "tenant_id": tenant_id,
                "workspace_id": (workspace.to_dict() or {}).get("workspace_id"),
                "principal": principal,
                "state": REQUESTED,
                "request_id": str(uuid.uuid4()),
                "requested_at": now,
                "requested_via": via,
                "decision": None,
                "history": history,
            }
            txn.set(ref, record)
            return 202, record

        status, record = _apply(transaction)
        log.info("loan request workspace=%s status=%d", record.get("workspace_id"), status)
        return status, record

    # -- people/ (§6.4 part 1) ------------------------------------------------

    def touch_person(self, ctx: Any) -> None:
        """`people/{tenant_id}`: `principal`, `first_seen`, `last_seen` and the
        `teams` resolved at this sight, at most once per ten minutes per person
        per instance. Humans only. Never raises: a request must not fail
        because its sighting could not be recorded."""
        email = (getattr(ctx, "email", "") or "").strip().lower()
        if self._db is None or not email or is_service_identity(email) \
                or getattr(ctx, "member_scope", "") or getattr(ctx, "is_rollup_sweeper", False):
            return
        try:
            tenant_id = personal_tenant_id(email)
            clock = time.monotonic()
            with self._touch_lock:
                last = self._touched.get(tenant_id)
                if last is not None and clock - last < PEOPLE_TOUCH_EVERY_SECONDS:
                    return
                self._touched[tenant_id] = clock
            now = self._now()
            # The groups only: the person's own workspace is listed among
            # `tenant_choices` too (#1200), and it is not a team.
            teams = sorted({tid for tid, _ in (getattr(ctx, "tenant_choices", ()) or ())
                            if tid != tenant_id})
            ref = self._db.collection(PEOPLE).document(tenant_id)
            fields: dict[str, Any] = {"principal": email, "last_seen": now, "teams": teams}
            if not ref.get().exists:
                fields["first_seen"] = now
            ref.set(fields, merge=True)
        except Exception as exc:  # noqa: BLE001 - a sighting never fails a request
            log.warning("people sighting not recorded: %s", type(exc).__name__)
