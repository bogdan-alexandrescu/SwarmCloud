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
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.identity import SERVICE_ACCOUNT_EMAIL, tenant_id_for_user
from swarm_common.models import Tenant

from .admins import AUDIT_COLLECTION
from .errors import (
    Conflict,
    NoClaudeAccount,
    WorkspaceFailed,
    WorkspaceNotReady,
    WorkspaceNotRequested,
    WorkspaceRequestTooSoon,
)
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

#: §8's limits for a new personal workspace (WD5). quota_pods and quota_cpu
#: are 2 pods and 8 vCPU per agent, the ratio People keeps when an admin
#: raises the ceiling (§6.4): 8 claude-code pods at 4 vCPU with
#: `requests == limits`, plus headroom for Jobs finishing inside their TTL.
DEFAULT_LIMITS: Mapping[str, int] = {
    "max_active": 8, "capacity_units": 8, "quota_pods": 16, "quota_cpu": 64,
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


def view(record: Mapping[str, Any] | None, *, console_url: str = "") -> dict[str, Any]:
    """What a person is shown of their OWN record (`GET /v1/workspace`).

    Never the principal and never `decision.by`: the first the caller
    already knows, the second is an admin's address, which never leaves
    Firestore. The failure's copy is served from its code (§4.3)."""
    state = state_of(record)
    out: dict[str, Any] = {
        "state": state,
        "setup_url": setup_url(console_url, "workspace"),
        "setup_command": SETUP_COMMAND,
    }
    if record is None:
        return out
    decision = record.get("decision") or None
    failure = record.get("failure") or None
    again = request_again_at(record)
    out.update({
        "workspace_id": record.get("workspace_id"),
        "tenant_id": record.get("tenant_id"),
        "request_id": record.get("request_id"),
        "requested_at": _iso(record.get("requested_at")),
        "requested_via": record.get("requested_via"),
        "decision": None if not decision else {
            "verdict": decision.get("verdict"),
            "reason": decision.get("reason"),
            "at": _iso(decision.get("at")),
        },
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

    def request(self, *, tenant_id: str, principal: str, via: str) -> tuple[int, dict[str, Any]]:
        """One Firestore transaction: read the record, then create, re-open or
        answer it. Returns (HTTP status, the record as stored).

        The caller has already been checked as a human in an allowed domain,
        not a secret admin, and owning `tenant_id` (`routes.workspaces`)."""
        if via not in VIAS:
            raise ValueError(f"via must be one of {VIAS}")
        ref = self._db.collection(WORKSPACES).document(tenant_id)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[int, dict[str, Any]]:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            state = state_of(data) if data is not None else None
            if data is None:
                id_ref = self._draw_id(txn)
                record = self._new_record(tenant_id=tenant_id, workspace_id=id_ref.id,
                                          principal=principal, now=now, via=via)
                txn.set(id_ref, {"tenant_id": tenant_id})
                txn.set(ref, record)
                return 202, record
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
                    now: datetime, via: str) -> dict[str, Any]:
        """§1.1's record as a request creates it, in `requested`."""
        return {
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "principal": principal,
            "state": REQUESTED,
            "request_id": str(uuid.uuid4()),
            "requested_at": now,
            "requested_via": via,
            "decision": None,
            "history": [],
            "limits": dict(DEFAULT_LIMITS),
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
        * `requested`, `failed` or `denied`: completed IN PLACE. The
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
            elif state in MIGRATABLE:
                action = MIGRATE_UPDATE
            else:
                raise Conflict(
                    f"workspace {before.get('workspace_id')} is {state}"
                    + ("" if state != READY else " and was not migrated")
                    + "; only a requested, failed or denied record, or none, is migrated",
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
            teams = sorted({tid for tid, _ in (getattr(ctx, "tenant_choices", ()) or ())})
            ref = self._db.collection(PEOPLE).document(tenant_id)
            fields: dict[str, Any] = {"principal": email, "last_seen": now, "teams": teams}
            if not ref.get().exists:
                fields["first_seen"] = now
            ref.set(fields, merge=True)
        except Exception as exc:  # noqa: BLE001 - a sighting never fails a request
            log.warning("people sighting not recorded: %s", type(exc).__name__)
