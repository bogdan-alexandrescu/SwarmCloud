"""Admin → People: the list, the approval flow, the ceiling and the loans
(docs/workspaces.md §1.3, §2.1-§2.2, §6.3-§6.4; #847, lane W7).

W1 made the record a person requests (`swarm_api.workspaces`) and W2 the admin
roles and their audit (`swarm_api.admins`). This module is what an admin does
with a record:

    approve   requested (or denied, at any time) -> approved, then publish
    deny      requested or failed -> denied, with a reason the person is shown
    retry     failed or needs_owner -> approved, with a fresh request id, then
              publish
    limits    the record's ceiling, the tenant's max_active AND capacity_units
              together, and, for a ready workspace, a `limits` run
    loan      lend or reclaim a pool account: only one owned by a group tenant
              or by the acting admin's own personal tenant. A lend to a
              workspace that is not `ready` is recorded PENDING and applied
              by the sweep once it is (LOAN-NEW, below)
    decline   close a person's open loan request without lending, with an
              optional reason they are shown (LOAN-DECLINE, below)
    sweep     publish again an `approved` record nobody claimed in 10 minutes,
              and report every `approved` record nothing is advancing
              (`workspace_stuck`), even when publishing is off

THE RECORD IS ADDRESSED BY WORKSPACE ID, through `workspace_ids/{w-...}`, so a
URL, a proxy log or a screenshot of the address bar names nobody. Every log
line here names the workspace id and nothing else about the person: no email,
no tenant id (`u-alice` is derived from the email, so it is a name).

EVERY ACTION WRITES `admin_audit` IN THE SAME TRANSACTION AS ITS CHANGE,
`{action, target_workspace_id, by, at, detail}` -- the shape W2's entries have,
with the workspace id where theirs name an email. The one exception is a loan:
the account is written by the quota broker, the pool's single writer (see
`routes/accounts.py`), so its audit entry is written in the transaction that
records the loan on swarm-api's side, AFTER the broker answered. A broker
failure therefore writes no audit entry for a change that did not happen.

A LOAN TO A WORKSPACE STILL BEING SET UP IS PENDING, NOT REFUSED (LOAN-NEW,
part of #1194). The broker grants a borrower's worker account,
`swarm-agent-worker-<tenant>`, read access to the account's secret BEFORE it
lists the borrower, and that account is created by the workspace job. Until
the job marks the workspace `ready` the account may not exist, and IAM refuses
a member that does not exist: on 2026-10-13 a lend to w-7f9285, approved an
hour earlier and still provisioning, answered 422 "...InvalidArgument.
Lending is unchanged." So a lend to a workspace that is not `ready` calls the
broker not at all; it writes `pending_loans/{tenant_id}` and its audit entry
(`lend_pending`) in one transaction, read against the record's state in that
same transaction, and the answer says it will apply when the workspace is
ready. The sweep (every 10 minutes) applies each pending loan of a `ready`
workspace through the same broker call a direct lend makes, and only after
the broker answered removes it, closes the loan request and writes `lend` --
so a loan is either pending, or lent and audited, never half of each. A grant
that fails on a READY workspace is an error naming the member and the
broker's reason (`LOAN_GRANT_FAILED`): there, the member should exist.

A LOAN REQUEST MAY BE DECLINED (LOAN-DECLINE, owner 2026-10-12, part of
#1206). Before, a request closed only when an account was lent against it, so
one an admin would not grant sat in "Waiting for you" for ever. `decline_loan`
moves an open (`requested`) request to `declined`, with `{verdict, by, at,
reason}` in its decision and a `loan_declined` audit entry, in one
transaction; anything else is `LOAN_REQUEST_NOT_OPEN` (409), so a declined or
lent request is not declined again. The reason is optional and short, and it
is shown to the person (the notification feed's `account` item); the audit
keeps its length, as a denial's does. The person may ask again: a new request
re-opens the document as `requested` and keeps the declined one in `history`
(`workspaces.request_loan`). Declining does not touch `pending_loans`: a
pending loan is a lending already decided, and only a reclaim cancels it.

swarm-api NEVER WRITES `ready`. Nothing here does: the job's final check is
the only writer (§1.2).

AN ADMIN'S OWN REQUEST IS APPROVED AUTOMATICALLY (owner, 2026-10-09; this
replaces 2026-10-08's "an admin may approve their own request, by hand").
`request_own` runs `_approve_in` -- the approval `approve` makes, not a copy
of it -- inside the request's own transaction, and then the same publish. The
decision says `auto: true`, and the audit entry is
`approve_own_workspace_auto`. The call guard bounds what any approval can
create, so an automatic one creates nothing an admin's click would not.

A TENANT THAT PREDATES THE WORKSPACE JOB IS NEVER APPROVED HERE (§3.3).
`u-bogdan`'s identity was made by Terraform, whose tenant document carries
`managed_by = swarm-terraform`, and lane W9 migrates it into a record with
`migrated = true`. Either holds an admin's own request `requested`
(`ws.MigrationHold`, not a refusal), so nothing is published that the apply's
squat check would fail with IDENTITY_NOT_OURS. A manual approval of it is
refused `WORKSPACE_MIGRATING` through `refusals.refuse`: a new refusal, so it
ships report-only until its switch is on (refusals.py).

A NAME IS SERVED ONLY BESIDE THE EMAIL IT BELONGS TO (docs/design/user-profile.md
§4.4, Q5; lane PROFILE-NAMES-API). A People row's `email` gains
`display_name`; the approval, task and workflow reads gain a `*_name` sibling
of each email field they serve, through `add_names`. A name is a label the
person chose, never an identity: nothing here or there decides anything by
it. One page asks for its names once (`display_names.DisplayNames.names_for`:
one `get_all`, a 60-second cache), and a lookup that fails serves every name
as null, so the page shows emails, as it did before, and is never failed by a
name. A row carries the name and NOTHING ELSE from the person's preferences:
their default workspace, mutes and reminder cadence are theirs alone, and no
admin route reads them (Q8).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.identity import worker_service_account_id
from swarm_common.models import Tenant

from . import refusals
from . import workspaces as ws
from .admins import AUDIT_COLLECTION
from .errors import (
    ApiError,
    Conflict,
    NotFound,
    ValidationFailed,
    WorkspaceApprovalOwnerOnly,
    WorkspaceGroupUnreadable,
    WorkspaceIdNested,
    WorkspaceIdTaken,
    WorkspaceIdTerraform,
    WorkspaceMigrating,
)
from .gittokens import COLLECTION as GIT_TOKENS
from .groups import GroupLookupError, MembershipResolver, check_group_readable
from .preferences import Preferences, key_versions
from .publish_workspace import MODE_CREATE, MODE_LIMITS, WorkspacePublisher
from .store import TENANTS

log = logging.getLogger(__name__)

#: §2.2: the sweep publishes an `approved` record again once its last attempt
#: is this old -- whether that attempt failed or its build never claimed the
#: record -- and never more often, so a slow trigger is not flooded.
DISPATCH_EVERY = timedelta(minutes=10)
#: Records one sweep call publishes at most. The tick runs every 5 minutes; a
#: backlog larger than this clears over a few ticks rather than one long call.
SWEEP_LIMIT = 50
#: A stuck record is reported at most once per this, through the record's
#: `stuck_reported_at`. An hour: the sweep runs every 10 minutes, and an alert
#: fed six identical entries an hour per record is one nobody reads twice.
STUCK_REPORT_EVERY = timedelta(hours=1)
#: The `jsonPayload.event` of the report, the contract with the WS-WIRE
#: lane's log-based alert. Its other fields are workspace_id, reason,
#: approved_at and minutes_waiting; never an email or a tenant id.
STUCK_EVENT = "workspace_stuck"

#: What `approve` and `retry` say, so an admin is never told a workspace is
#: being built when nothing will build it (the 2026-10-09 incident, w-752763).
NOT_SENT_PUBLISHING_OFF = (
    "Approved, and NOT sent for building: workspace provisioning is off in this "
    "deployment (WORKSPACE_APPLY_PUBLISH). The approval is kept and the record waits "
    "as approved; it is sent by the first sweep after provisioning is switched on "
    "(docs/workspaces.md §10).")
NOT_SENT_PUBLISH_FAILED = (
    "Approved, but the build could not be sent yet. The approval is kept and the "
    "dispatch sweep sends it again within 10 minutes.")
SENT = "Approved and sent for building."

#: §1.3: a denial's reason is required, and at most 500 characters.
MAX_REASON = 500

#: The ceiling an admin may give one person. 1, not 0: stopping a person's
#: work is the tenant's `enabled` switch, which says so, not a ceiling of 0
#: that reads as a limit. 100, because the namespace quota follows at 8 vCPU
#: per agent (§8): a typo of 800 would otherwise ask for a 6,400-vCPU quota.
MIN_CEILING = 1
MAX_CEILING = 100
#: §8's ratio, which People keeps when the ceiling moves (§6.4).
PODS_PER_AGENT = 2
CPU_PER_AGENT = 8

#: The rows and audit entries one list reads. People is a team's list, not a
#: directory; a bound keeps one page from reading an unbounded collection.
MAX_PEOPLE = 500
AUDIT_SHOWN = 50

#: `approve` is accepted from these. `requested` per §1.3, and `denied`
#: because "an admin may still approve the denied record at any time"
#: (owner, 2026-10-08).
APPROVABLE = frozenset({ws.REQUESTED, ws.DENIED})
DENIABLE = frozenset({ws.REQUESTED, ws.FAILED})
RETRYABLE = frozenset({ws.FAILED, ws.NEEDS_OWNER})
#: The code entry.py stops a record with when its approval does not verify
#: (images/workspace-apply/entry.py, lane WK-SIGNED-APPROVAL).
APPROVAL_UNSIGNED = "APPROVAL_UNSIGNED"

#: The value Terraform writes in `managed_by` on every tenant document it owns
#: (terraform/modules/firestore/bootstrap.tf). `ensure_tenant` and the
#: workspace job (A8) never write it, so on a personal tenant it means the
#: identity behind it predates the workspace job (§3.3).
TERRAFORM_MANAGED = "swarm-terraform"

#: The automatic approval of an admin's own request (§1.3): its audit action
#: and the reason its decision carries, which the person is shown.
AUTO_APPROVE_ACTION = "approve_own_workspace_auto"
AUTO_APPROVE_REASON = "requester is an admin"

#: An admin's request in Admin › Tenants (docs/design/workspace-kinds.md
#: §4.2): one audit entry per request that changed something -- the approval
#: when the requester may approve it, in the same transaction, or the request
#: itself when it waits (a service filed by an admin who is not the owner).
ADMIN_REQUEST_APPROVED_ACTION = "request_workspace_approved"
ADMIN_REQUEST_ACTION = "request_workspace"

#: The newest records `GET /v1/admin/workspaces` serves (§4.2.2).
MAX_RECORDS_LISTED = 200

#: A `limits` value an admin may set at request time: max_active and
#: capacity_units within People's ceiling bounds, and the namespace quota
#: within People's ratio at the highest ceiling. They become a namespace
#: quota and a pool ceiling, never a pod spec (invariant 10).
MAX_QUOTA_PODS_FACTOR = 2
MAX_QUOTA_CPU_FACTOR = 8

#: The loan request's state once an admin has lent an account against it.
LOAN_LENT = "lent"
#: ... and once an admin has declined it (LOAN-DECLINE). The person may ask again.
LOAN_DECLINED = "declined"
#: A decline's reason is optional and SHORT: one line the person reads in the
#: notification feed, not a denial's paragraph (MAX_REASON).
MAX_LOAN_REASON = 200

#: Loans an admin made to a workspace that was not `ready` yet, one document
#: per tenant: `{tenant_id, workspace_id, loans: [{account_id, owner_tenant,
#: by, at, last_error, last_attempt_at}]}`. Its own collection, not a field on
#: the workspace record, because the workspace job writes that record too.
PENDING_LOANS = "pending_loans"
#: The `state` of a loan answer: granted now, recorded for later, or reclaimed.
LOAN_APPLIED = "applied"
LOAN_PENDING = "pending"
LOAN_RECLAIMED = "reclaimed"


#: A lookup of display names: emails in, each email's name (or None) out.
NameLookup = Callable[[Iterable[str]], Mapping[str, "str | None"]]

#: Where an app context keeps its one `Preferences` for the name lookup, so
#: the 60-second name cache is per process, not per request.
_NAMES_ATTR = "_person_names"

#: The email fields whose sibling is a LIST of names, index for index, and
#: what the sibling is called. Every other field `f` gains `f + "_name"`.
LIST_SIBLINGS = {"approvers": "approver_names"}


def _no_names(emails: Iterable[str]) -> dict[str, str | None]:
    return {email: None for email in emails}


def name_lookup(ctx: Any) -> NameLookup:
    """The app context's display-name lookup.

    No `swarm-child-key` configured means no preference id can be derived,
    so every name is null and nothing is read: the pages show emails, as
    they did before names existed."""
    prefs = getattr(ctx, _NAMES_ATTR, None)
    if prefs is None:
        prefs = Preferences(ctx.db, key_versions(ctx.settings))
        setattr(ctx, _NAMES_ATTR, prefs)
    return prefs.names.names_for if prefs.configured else _no_names


def _is_email(value: Any) -> bool:
    # `decided_by` is also `approval-expiry`, and `approvers` also `members`:
    # neither is a person, and neither is looked up.
    return isinstance(value, str) and "@" in value


def add_names(lookup: NameLookup, rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> None:
    """Give each row's email `fields` their name siblings, from ONE lookup.

    A field the row does not serve gains no sibling, so no name is ever
    served for an email the response does not carry. A value that is not an
    email (a role word, the expiry actor) gets a null name."""
    rows = list(rows)
    fields = tuple(fields)
    emails: list[str] = []
    for row in rows:
        for f in fields:
            value = row.get(f)
            values = value if isinstance(value, list) else [value]
            emails.extend(v for v in values if _is_email(v))
    names = lookup(emails) if emails else {}

    def name(value: Any) -> str | None:
        return names.get(value) if _is_email(value) else None

    for row in rows:
        for f in fields:
            if f not in row:
                continue
            value = row[f]
            if f in LIST_SIBLINGS:
                row[LIST_SIBLINGS[f]] = [name(v) for v in value] if isinstance(value, list) else None
            else:
                row[f + "_name"] = name(value)


def worker_member(tenant_id: str, project_id: str) -> str:
    """The IAM member the broker grants for a borrower: its worker account."""
    account = worker_service_account_id(tenant_id)
    if project_id:
        account = f"{account}@{project_id}.iam.gserviceaccount.com"
    return f"serviceAccount:{account}"


def pending_message(state: str) -> str:
    return (
        f"Recorded as pending: the loan will apply when the workspace is ready. It is "
        f"{state} now, and its worker account is made by the workspace setup, so IAM would "
        "refuse a grant to it today. The workspace sweep applies the loan within 10 "
        "minutes of the workspace becoming ready; reclaiming before then cancels it.")


class WorkspaceNotFound(NotFound):
    code = "WORKSPACE_NOT_FOUND"


class WorkspaceWrongState(Conflict):
    code = "WORKSPACE_WRONG_STATE"


class AccountNotLendable(NotFound):
    code = "ACCOUNT_NOT_LENDABLE"


class AccountIsTheirs(Conflict):
    code = "ACCOUNT_IS_THEIRS"


class LoanRequestNotOpen(Conflict):
    """There is no open loan request to decline: none was made, or it was
    lent against or declined already (LOAN-DECLINE)."""
    code = "LOAN_REQUEST_NOT_OPEN"


class LoanGrantFailed(ValidationFailed):
    """The broker refused a lend to a READY workspace (LOAN-NEW). The
    message names the member it was asked to grant and the broker's reason;
    the broker changed nothing, so neither does swarm-api."""
    code = "LOAN_GRANT_FAILED"


def _stopped_unsigned(record: Mapping[str, Any]) -> bool:
    """A record the job stopped before any grant because its approval did not
    verify (entry.py's APPROVAL_UNSIGNED). Deniable, so the remedy is a
    fresh decision -- deny, then approve the record as shown -- since a retry
    will not sign an approval swarm-api never made (`retry`, R8)."""
    return (ws.state_of(record) == ws.NEEDS_OWNER
            and (record.get("failure") or {}).get("code") == APPROVAL_UNSIGNED)


def _owner_only(workspace_id: str, state: str) -> WorkspaceApprovalOwnerOnly:
    return WorkspaceApprovalOwnerOnly(
        f"workspace {workspace_id} is a service workspace: it admits a new account at the "
        "API's front door, so only the platform owner approves or retries it. Nothing was "
        "changed.",
        detail={"workspace_id": workspace_id, "state": state})


def request_limits(kind: str, given: Mapping[str, int] | None) -> dict[str, int]:
    """A new record's `limits` (design §4.2.2): the kind's defaults, with what
    the admin gave laid over them. A ceiling given without a quota keeps
    People's ratio (`ceiling_limits`), so a raised ceiling is never starved
    by the default quota. Each number is bounded; an unknown key is refused
    by the route's model before this runs."""
    limits = dict(ws.DEFAULT_LIMITS_BY_KIND[kind])
    given = {k: int(v) for k, v in (given or {}).items() if v is not None}
    unknown = set(given) - set(ws.LIMIT_KEYS)
    if unknown:
        raise ValidationFailed(f"unknown limits: {sorted(unknown)}")
    if "max_active" in given:
        derived = ceiling_limits(given["max_active"])
        limits.update({k: derived[k] for k in ("quota_pods", "quota_cpu")})
    limits.update(given)
    bounds = {"max_active": MAX_CEILING, "capacity_units": MAX_CEILING,
              "quota_pods": MAX_QUOTA_PODS_FACTOR * MAX_CEILING,
              "quota_cpu": MAX_QUOTA_CPU_FACTOR * MAX_CEILING}
    for key, top in bounds.items():
        if not MIN_CEILING <= limits[key] <= top:
            raise ValidationFailed(f"limits.{key} must be between {MIN_CEILING} and {top}")
    return limits


def ceiling_limits(max_active: int) -> dict[str, int]:
    """The record's `limits` for a ceiling: the one number, and §8's ratio.

    `capacity_units` moves WITH `max_active`, as the tenant-ceiling route does
    (`routes/admin.py::set_tenant_concurrency`): the pool is the smaller of the
    two, so writing one alone left tenant `smoke` held at 8 on 2026-10-07."""
    if not MIN_CEILING <= int(max_active) <= MAX_CEILING:
        raise ValidationFailed(f"max_active must be between {MIN_CEILING} and {MAX_CEILING}")
    n = int(max_active)
    return {"max_active": n, "capacity_units": n,
            "quota_pods": PODS_PER_AGENT * n, "quota_cpu": CPU_PER_AGENT * n}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _account_name(value: Any) -> str | None:
    """An account's Name (#1913) as the broker stored it, or None for none.

    The broker is its one writer and validates it (`validate_display_name`);
    the lent rows read the account document straight from Firestore, so a
    value that is not text is no Name rather than whatever it holds. Never
    the label in its place: the console tells a named account from an
    unnamed one by this being None."""
    return value if isinstance(value, str) else None


def _later(*values: Any) -> datetime | None:
    seen = [v for v in values if isinstance(v, datetime)]
    return max(seen) if seen else None


class People:
    """The admin side of the workspace records. `db` is the context's
    Firestore client (or a test's fake); `publisher` is
    `publish_workspace.publisher_for(settings)` or a test's fake."""

    def __init__(
        self,
        db: Any,
        *,
        publisher: WorkspacePublisher,
        now: Callable[[], datetime],
        console_url: str = "",
        project_id: str = "",
        names: NameLookup | None = None,
        groups: MembershipResolver | None = None,
        rules: ws.KindRules | None = None,
        signer: Any = None,
        verify_settings: Any = None,
        firestore_off_tenants: tuple[str, ...] = (),
    ) -> None:
        self._db = db
        # WORKER_FIRESTORE_OFF_TENANTS (#1457 design B): whose job-made
        # workspace worker gets no Firestore role. Written on the record as
        # `worker_firestore_access` by `_access_patch` at every approval,
        # retry and ceiling change; empty, the default, is today's grant.
        self._firestore_off = tuple(firestore_off_tenants or ())
        # Signs every approval and retry (`ws.sign_approval`, lane
        # WK-SIGNED-APPROVAL): the step-spec KMS signer. None only where
        # `specsigning.signer_from_settings` allows it (local development and
        # tests); the job then refuses a group or service approval.
        self._signer = signer
        # SPEC_SIGNING_KEY_VERSION and SPEC_VERIFY_KEYS, which a retry of a
        # group or service record verifies its stored approval with before it
        # signs anew (`ws.approval_refusal`). None: no such retry is signed.
        self._verify_settings = verify_settings
        self._publisher = publisher
        self._now = now
        self._console_url = console_url
        self._project_id = project_id
        # Each row's display name (`name_lookup`). None: every name is null.
        self._names = names or _no_names
        # The directory a group request is checked against (§4.2.3 check 7)
        # and the deployment's rules for every kind. None: no group can be
        # checked, so a group request is WORKSPACE_GROUP_UNREADABLE, and an
        # admin request has no rules to pass, so `create` refuses to run.
        self._groups = groups
        self._rules = rules

    # -- addressing ------------------------------------------------------------

    def _id_ref(self, workspace_id: str) -> Any:
        return self._db.collection(ws.WORKSPACE_IDS).document(workspace_id)

    def _record_ref(self, tenant_id: str) -> Any:
        return self._db.collection(ws.WORKSPACES).document(tenant_id)

    def _resolve(self, txn: Any, workspace_id: str) -> tuple[Any, dict[str, Any]]:
        """(record ref, record) for a workspace id, read inside `txn`. One
        404 for a malformed id, an unknown one and a dangling index entry."""
        missing = WorkspaceNotFound(f"no workspace {workspace_id!r}")
        if not isinstance(workspace_id, str) or not workspace_id.startswith("w-") \
                or "/" in workspace_id or len(workspace_id) > 16:
            raise missing
        index = _snapshot(txn.get(self._id_ref(workspace_id)))
        tenant_id = (index.to_dict() or {}).get("tenant_id") if index.exists else None
        if not tenant_id:
            raise missing
        ref = self._record_ref(tenant_id)
        snap = _snapshot(txn.get(ref))
        record = snap.to_dict() if snap.exists else None
        if not record or record.get("workspace_id") != workspace_id:
            raise missing
        return ref, record

    def record(self, workspace_id: str) -> dict[str, Any]:
        """The record for a workspace id, outside a transaction."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _read(txn: Any) -> dict[str, Any]:
            return self._resolve(txn, workspace_id)[1]

        return _read(transaction)

    # -- the audit ---------------------------------------------------------------

    def _audit(self, txn: Any, action: str, workspace_id: str, by: str,
               detail: Mapping[str, Any]) -> None:
        """One `admin_audit` entry inside `txn`. Created, never updated."""
        ref = self._db.collection(AUDIT_COLLECTION).document()
        txn.set(ref, {"action": action, "target_workspace_id": workspace_id,
                      "by": by, "at": self._now(), "detail": dict(detail)})

    # -- approve, deny, retry ----------------------------------------------------

    def _predates_workspaces(self, txn: Any, record: Mapping[str, Any]) -> bool:
        """Whether the person's tenant was made outside the workspace job
        (§3.3): the record says `migrated`, or the tenant document is
        Terraform's. Read inside `txn`, before any of its writes."""
        if record.get("migrated") is True:
            return True
        tenant_id = str(record.get("tenant_id") or "")
        if not tenant_id:
            return False
        snap = _snapshot(txn.get(self._db.collection(TENANTS).document(tenant_id)))
        return snap.exists and (snap.to_dict() or {}).get("managed_by") == TERRAFORM_MANAGED

    def _access_patch(self, record: Mapping[str, Any]) -> dict[str, bool]:
        """The record's Firestore switch as the cutover list has it now
        (`ws.worker_firestore_access`). The job reads the record, so writing
        it with each action that starts a run is what makes a change to the
        list reach a workspace: its next approval, retry or ceiling change."""
        return {ws.WORKER_FIRESTORE_ACCESS: ws.worker_firestore_access(
            str(record.get("tenant_id") or ""), self._firestore_off)}

    def _approve_in(self, txn: Any, record: Mapping[str, Any], *, by: str,
                    auto: bool = False, is_owner: bool = False,
                    action: str | None = None,
                    directory: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """THE APPROVAL, for an admin's click and for an admin's own request
        alike: the state check, the migration check, the decision and its
        audit entry, inside `txn`. Returns the record's patch; the caller
        writes it, with whatever else its transaction changes, so the record
        is written once. Nothing here creates a task or a lease (invariant 1):
        an approval is a record and, after it, one message.

        A SERVICE record is approved by the platform owner only (Q2 (a),
        docs/design/workspace-kinds.md §4.2.1): it admits a new principal at
        the front door. A GROUP record's approval carries the directory check
        made just before it (`directory`), so A1 reads a fresh one."""
        workspace_id = str(record.get("workspace_id") or "")
        state = ws.state_of(record)
        kind = ws.kind_of(record)
        if kind == ws.SERVICE and not is_owner:
            raise _owner_only(workspace_id, state)
        if state not in APPROVABLE:
            raise WorkspaceWrongState(
                f"workspace {workspace_id} is {state}; only a requested or denied "
                "workspace can be approved",
                detail={"workspace_id": workspace_id, "state": state})
        if self._predates_workspaces(txn, record):
            if auto:
                raise ws.MigrationHold(workspace_id)
            # Report-only until REFUSAL_WORKSPACE_MIGRATING=on: past this
            # call the admin's approval goes through.
            refusals.refuse(WorkspaceMigrating(
                f"workspace {workspace_id} predates self-service setup and is migrated by "
                "the platform owner (docs/workspaces.md §3.3), not approved: its identity "
                "was made by Terraform, so the setup job would refuse it. Nothing was "
                "changed and nothing was started.",
                detail={"workspace_id": workspace_id, "state": state}))
        at = self._now()
        patch: dict[str, Any] = {
            "state": ws.APPROVED,
            "decision": {"by": by, "at": at, "verdict": ws.APPROVED,
                         "reason": AUTO_APPROVE_REASON if auto else None, "auto": auto},
            # The job runs nothing that grants for a group or service record
            # unless this verifies over the record (R8, `ws.sign_approval`).
            ws.APPROVAL: ws.sign_approval(self._signer, record, decided_by=by, decided_at=at,
                                          request_id=str(record.get("request_id") or "")),
            **self._access_patch(record),
        }
        if directory is not None:
            patch["directory"] = dict(directory)
        if state == ws.DENIED:
            # The earlier denial stays where the admin can read it.
            patch["history"] = [*(record.get("history") or []), {
                "request_id": record.get("request_id"),
                "requested_at": record.get("requested_at"),
                "decision": record.get("decision"),
            }]
        detail = {
            "self_approval": (record.get("principal") or "").strip().lower() == by,
            "auto": auto,
            "from_state": state,
            "request_id": record.get("request_id"),
        }
        if kind != ws.PERSONAL:
            detail["kind"] = kind
        self._audit(txn, action or (AUTO_APPROVE_ACTION if auto else "approve"),
                    workspace_id, by, detail)
        return patch

    def approve(self, workspace_id: str, *, by: str, is_owner: bool = False) -> dict[str, Any]:
        """requested (or denied) -> approved, with the decision and its audit
        in one transaction; then the publish (§2.1). A group's directory check
        runs again first, outside the transaction (it is a network call), and
        its time is written with the approval; a service's is the owner's
        alone (`_approve_in`)."""
        by = by.strip().lower()
        directory = self._directory_before(workspace_id, by=by, is_owner=is_owner)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            ref, record = self._resolve(txn, workspace_id)
            patch = self._approve_in(txn, record, by=by, is_owner=is_owner,
                                     directory=directory
                                     if ws.kind_of(record) == ws.GROUP else None)
            txn.update(ref, patch)
            return {**record, **patch}

        record = _apply(transaction)
        log.info("workspace approved workspace=%s", workspace_id)
        dispatch = self._dispatch(record, MODE_CREATE)
        return self._decided(record, dispatch)

    def request_own(self, workspaces: ws.Workspaces, *, tenant_id: str, principal: str,
                    via: str, is_admin: bool) -> tuple[int, dict[str, Any]]:
        """`POST /v1/workspace` (§1.3): the request, and for an admin the
        approval in the same transaction, then the same publish `approve`
        makes. A non-admin's request is W1's, unchanged: it waits for an
        admin. Returns (HTTP status, the record as stored)."""
        principal = principal.strip().lower()

        def _auto(txn: Any, record: dict[str, Any]) -> dict[str, Any]:
            return self._approve_in(txn, record, by=principal, auto=True)

        status, record = workspaces.request(
            tenant_id=tenant_id, principal=principal, via=via,
            approve=_auto if is_admin else None)
        if status == 202 and ws.state_of(record) == ws.APPROVED:
            log.info("workspace approved automatically workspace=%s",
                     record.get("workspace_id"))
            self._dispatch(record, MODE_CREATE)
        return status, record

    # -- an admin's request for any kind (docs/design/workspace-kinds.md §4.2) --

    def _directory(self, group: str, *, by: str) -> dict[str, Any]:
        """§4.2.3 check 7: one membership question, of the requesting admin,
        in that one group. Any answer proves the group is readable. A lookup
        that fails is WORKSPACE_GROUP_UNREADABLE, before anything is
        written. No log line names the group or the admin."""
        unreadable = WorkspaceGroupUnreadable(
            "The directory did not answer for that group, so it cannot be shown to exist "
            "and resolve. Nothing was written; try again in a minute.",
            detail={"retryable": True})
        if self._groups is None:
            raise unreadable
        try:
            check_group_readable(self._groups, by, group)
        except GroupLookupError:
            log.warning("workspace directory check failed")
            raise unreadable from None
        return {"checked_at": self._now(), "readable": True}

    def _directory_before(self, workspace_id: str, *, by: str,
                          is_owner: bool) -> dict[str, Any] | None:
        """The directory check an approval or a retry of a GROUP record makes
        first (§4.2.3: "repeated at approval and at every retry"). None for
        any other kind. A service record a non-owner acts on is refused here,
        before any network call."""
        record = self.record(workspace_id)
        kind = ws.kind_of(record)
        if kind == ws.SERVICE and not is_owner:
            raise _owner_only(workspace_id, ws.state_of(record))
        if kind != ws.GROUP:
            return None
        return self._directory(str(record.get("principal") or ""), by=by)

    def _assert_stored_scope(self, tenant_id: str, principal: str, kind: str) -> None:
        """§4.2.3's checks 1-3 that read stored state, before any write.

        1. The tenant document, if any, is this principal's and this kind's
           (`WORKSPACE_ID_TAKEN`; the record itself is re-checked inside the
           request's transaction).
        2. It is not Terraform's (`managed_by`, `WORKSPACE_ID_TERRAFORM`).
        3. No stored tenant or workspace id nests with this one through
           `-git-` (`WORKSPACE_ID_NESTED`). Read outside the transaction:
           Firestore cannot hold a whole collection in one, and the job's own
           checks follow (§4.3)."""
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        if snap.exists:
            tenant = snap.to_dict() or {}
            if str(tenant.get("principal") or "").strip().lower() != principal \
                    or str(tenant.get("kind") or "") != ws.tenant_kind_for(kind):
                raise WorkspaceIdTaken(
                    "That workspace name is already a tenant of a different identity or "
                    "kind. Nothing was changed; this needs a person, not a retry.")
            if tenant.get("managed_by") == TERRAFORM_MANAGED:
                raise WorkspaceIdTerraform(
                    f"{tenant_id!r} is one of the tenants Terraform owns, and the setup job "
                    "never builds over those. Nothing was changed.")
        for collection in (TENANTS, ws.WORKSPACES):
            for doc in self._db.collection(collection).stream():
                if ws.nested(tenant_id, doc.id):
                    raise WorkspaceIdNested(
                        f"{tenant_id!r} and the existing {doc.id!r} nest through '-git-', so "
                        "one's forge slot names would start with the other's. Choose an "
                        "address that derives another name.")

    def create(self, workspaces: ws.Workspaces, *, kind: str, principal: str,
               display_name: str | None, limits: Mapping[str, int] | None, by: str,
               is_owner: bool) -> tuple[int, dict[str, Any]]:
        """`POST /v1/admin/workspaces` (§4.2.2): an admin files a personal,
        group or service workspace. Every check of §4.2.3 runs before any
        write; then one transaction creates the record and its index entry
        and, when the requester may approve it (any admin for personal and
        group, the owner for service), approves it with its single audit
        entry -- or, when not, writes the request's single audit entry. Then
        the same publish an approval makes. Returns (HTTP status, record):
        202 created, 200 already standing (and nothing written)."""
        if self._rules is None:
            raise ValidationFailed("this deployment has no workspace rules configured")
        by = by.strip().lower()
        tenant_id, principal = ws.validate_kind_request(kind, principal, self._rules)
        self._assert_stored_scope(tenant_id, principal, kind)
        extra: dict[str, Any] = {}
        if kind == ws.GROUP:
            extra["directory"] = self._directory(principal, by=by)
        record_limits = request_limits(kind, limits)
        may_approve = kind != ws.SERVICE or is_owner

        def _approve(txn: Any, record: dict[str, Any]) -> dict[str, Any]:
            return self._approve_in(txn, record, by=by, auto=True, is_owner=is_owner,
                                    action=ADMIN_REQUEST_APPROVED_ACTION)

        def _filed(txn: Any, record: dict[str, Any]) -> None:
            self._audit(txn, ADMIN_REQUEST_ACTION, str(record.get("workspace_id") or ""), by,
                        {"kind": kind, "request_id": record.get("request_id")})

        status, record = workspaces.request(
            tenant_id=tenant_id, principal=principal, via=ws.ADMIN_VIA,
            approve=_approve if may_approve else None, kind=kind, requested_by=by,
            display_name=(display_name or "").strip() or None, limits=record_limits,
            extra=extra, on_create=None if may_approve else _filed)
        log.info("workspace filed by an admin workspace=%s kind=%s state=%s status=%d",
                 record.get("workspace_id"), kind, record.get("state"), status)
        if status == 202 and ws.state_of(record) == ws.APPROVED:
            self._dispatch(record, MODE_CREATE)
        return status, record

    def admin_view(self, record: Mapping[str, Any] | None) -> dict[str, Any]:
        """`_admin_view`, for the routes."""
        return self._admin_view(record)

    def records(self, *, kind: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        """`GET /v1/admin/workspaces` (§4.2.2): records, newest request first,
        at most MAX_RECORDS_LISTED, each the admin view. The kind filter is
        applied after the read, because a record with no `kind` is personal
        and an absent field cannot be queried for."""
        query = self._db.collection(ws.WORKSPACES)
        if state is not None:
            query = query.where(filter=FieldFilter("state", "==", state))
        rows = [snap.to_dict() or {} for snap in query.stream()]
        if kind is not None:
            rows = [r for r in rows if ws.kind_of(r) == kind]

        def newest(record: Mapping[str, Any]) -> float:
            at = record.get("requested_at")
            return at.timestamp() if isinstance(at, datetime) else 0.0

        rows.sort(key=newest, reverse=True)
        return [self._admin_view(r) for r in rows[:MAX_RECORDS_LISTED]]

    def deny(self, workspace_id: str, *, by: str, reason: str) -> dict[str, Any]:
        """requested or failed -> denied. The reason is required and is shown
        to the person; the 24-hour wait before they may ask again is counted
        from `decision.at` (`workspaces.request_again_at`)."""
        by = by.strip().lower()
        reason = (reason or "").strip()
        if not reason:
            raise ValidationFailed("a denial needs a reason; the person is shown it")
        if len(reason) > MAX_REASON:
            raise ValidationFailed(f"a denial's reason is at most {MAX_REASON} characters")
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            ref, record = self._resolve(txn, workspace_id)
            state = ws.state_of(record)
            if (state not in DENIABLE and not ws.never_dispatched(record)
                    and not _stopped_unsigned(record)):
                raise WorkspaceWrongState(
                    f"workspace {workspace_id} is {state}; only a requested or failed "
                    "workspace, an approved one no build was ever sent for, or one the job "
                    "stopped as APPROVAL_UNSIGNED, can be denied",
                    detail={"workspace_id": workspace_id, "state": state})
            patch: dict[str, Any] = {"state": ws.DENIED, "decision": {
                "by": by, "at": self._now(), "verdict": ws.DENIED, "reason": reason},
                # A denied record carries no approval signature, so turning its
                # verdict back is not enough to make the job run it (R8). One
                # read before the denial can still be put back: docs/design/
                # workspace-kinds.md §4.5's amendment states that residual.
                ws.APPROVAL: None}
            if record.get("decision"):
                # Denying a failed record replaces its approval; the approval
                # stays in the history beside the run that failed.
                patch["history"] = [*(record.get("history") or []), {
                    "request_id": record.get("request_id"),
                    "state": state,
                    "decision": record.get("decision"),
                    "failure": record.get("failure"),
                }]
            txn.update(ref, patch)
            # The reason is in the record already; the audit keeps its length
            # only, so the entry is not a second copy of free text.
            self._audit(txn, "deny", workspace_id, by, {
                "from_state": state, "request_id": record.get("request_id"),
                "reason_length": len(reason)})
            return {**record, **patch}

        record = _apply(transaction)
        log.info("workspace denied workspace=%s", workspace_id)
        return {"workspace": self._admin_view(record)}

    def retry(self, workspace_id: str, *, by: str, is_owner: bool = False) -> dict[str, Any]:
        """failed or needs_owner -> approved, with a fresh request id, the
        previous failure kept in `history`, and the retry recorded; then the
        publish. The job's claim (A1) accepts `approved` whose decision is an
        admin's approval, so a retried record is claimed like a new one and the
        sweep covers its publish exactly as it covers an approval's.

        A service's retry is the platform owner's, as its approval is, and a
        group's re-runs the directory check before anything is written
        (docs/design/workspace-kinds.md §4.8)."""
        by = by.strip().lower()
        directory = self._directory_before(workspace_id, by=by, is_owner=is_owner)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            ref, record = self._resolve(txn, workspace_id)
            state = ws.state_of(record)
            if ws.kind_of(record) == ws.SERVICE and not is_owner:
                raise _owner_only(workspace_id, state)
            if state not in RETRYABLE:
                raise WorkspaceWrongState(
                    f"workspace {workspace_id} is {state}; only a failed workspace or one "
                    "waiting for the platform owner can be retried",
                    detail={"workspace_id": workspace_id, "state": state})
            decision = record.get("decision") or {}
            if decision.get("verdict") != ws.APPROVED:
                # Unreachable through these routes; a record written by hand
                # must not be started by a retry nobody approved.
                raise WorkspaceWrongState(
                    f"workspace {workspace_id} has no approval to retry",
                    detail={"workspace_id": workspace_id, "state": state})
            if ws.kind_of(record) in (ws.GROUP, ws.SERVICE):
                # A worker can write `decision` (R8): a group or service record
                # "approved by" anyone, waiting for a retry. A retry signs only
                # an approval swarm-api signed before; anything else is a fresh
                # decision, deny and then approve, on the record as shown.
                why = ws.approval_refusal(record, self._verify_settings)
                if why is not None:
                    raise WorkspaceWrongState(
                        f"workspace {workspace_id} carries no approval swarm-api signed ({why}); "
                        "a retry would sign a decision nobody made here. Deny it, then approve "
                        "the record as shown",
                        detail={"workspace_id": workspace_id, "state": state,
                                "reason": "approval_unverified"})
            now = self._now()
            request_id = str(uuid.uuid4())
            patch = {
                "state": ws.APPROVED,
                "request_id": request_id,
                "retry": {"by": by, "at": now, "from_state": state},
                # A retry runs a new request id, so it is signed afresh, by
                # whoever retried it: the job checks the signature's request
                # id is the record's own (R8, `ws.sign_approval`).
                ws.APPROVAL: ws.sign_approval(self._signer, record, decided_by=by,
                                              decided_at=now, request_id=request_id),
                "failure": None,
                **self._access_patch(record),
                "history": [*(record.get("history") or []), {
                    "request_id": record.get("request_id"),
                    "state": state,
                    "failure": record.get("failure"),
                    "steps": record.get("steps"),
                }],
            }
            if directory is not None and ws.kind_of(record) == ws.GROUP:
                patch["directory"] = dict(directory)
            txn.update(ref, patch)
            self._audit(txn, "retry", workspace_id, by, {
                "from_state": state, "previous_request_id": record.get("request_id"),
                "request_id": patch["request_id"],
                "failure_code": (record.get("failure") or {}).get("code")})
            return {**record, **patch}

        record = _apply(transaction)
        log.info("workspace retry workspace=%s", workspace_id)
        dispatch = self._dispatch(record, MODE_CREATE)
        return self._decided(record, dispatch)

    def _decided(self, record: Mapping[str, Any], dispatch: Mapping[str, Any]) -> dict[str, Any]:
        """An approval's (or a retry's) answer. The decision is durable either
        way; `sent_for_building` and `message` say plainly whether anything
        will build it, so a deployment with provisioning off cannot answer an
        approval as if the workspace were on its way."""
        sent = bool(dispatch.get("published"))
        if sent:
            message = SENT
        elif dispatch.get("reason") == "publishing_off":
            message = NOT_SENT_PUBLISHING_OFF
        else:
            message = NOT_SENT_PUBLISH_FAILED
        return {"workspace": self._admin_view(record), "dispatch": dict(dispatch),
                "sent_for_building": sent, "message": message}

    # -- the ceiling ---------------------------------------------------------------

    def set_ceiling(self, workspace_id: str, *, by: str, max_active: int,
                    store: Any) -> dict[str, Any]:
        """The record's `limits` and its audit in one transaction; then the
        tenant's `max_active` AND `capacity_units` (and so its pool's
        `hard_limit`) through `Store.set_tenant_limits`, when the tenant
        exists; then, for a `ready` workspace, a `limits` run, which re-applies
        the namespace quota and the documents and makes no IAM call (§2.2).

        A workspace that is not ready yet needs no run: the create run reads
        the record's limits when it reaches A7 and A8. Lowering the ceiling
        never cancels running work; the pool refuses new leases above it."""
        by = by.strip().lower()
        limits = ceiling_limits(max_active)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            ref, record = self._resolve(txn, workspace_id)
            previous = dict(record.get("limits") or {})
            # The one admin action on a ready record, so the one that brings
            # a cut-over tenant's switch to it before its next A9.
            patch = {"limits": limits, **self._access_patch(record)}
            txn.update(ref, patch)
            self._audit(txn, "limits", workspace_id, by, {
                "previous": previous, "limits": limits, "state": ws.state_of(record)})
            return {**record, **patch}

        record = _apply(transaction)
        tenant_id = record["tenant_id"]
        tenant_written = False
        if store.get_tenant(tenant_id) is not None:
            store.set_tenant_limits(tenant_id, max_active=limits["max_active"],
                                    capacity_units=limits["capacity_units"], by=by)
            tenant_written = True
        dispatch = None
        if ws.state_of(record) == ws.READY:
            dispatch = self._dispatch(record, MODE_LIMITS)
        log.info("workspace ceiling workspace=%s max_active=%d tenant_written=%s",
                 workspace_id, limits["max_active"], tenant_written)
        return {"workspace": self._admin_view(record), "tenant_written": tenant_written,
                "dispatch": dispatch}

    # -- the publish, its record, and the sweep ------------------------------------

    def _dispatch(self, record: Mapping[str, Any], mode: str) -> dict[str, Any]:
        """Publish once and record the attempt. Never raises: the decision
        this follows is already durable, and the sweep retries a failure."""
        workspace_id = str(record.get("workspace_id") or "")
        request_id = str(record.get("request_id") or "")
        if not self._publisher.enabled:
            # Nothing was attempted, so nothing is recorded as one: the first
            # sweep after publishing is turned on finds the record unpublished.
            return {"published": False, "mode": mode, "reason": "publishing_off"}
        try:
            published = bool(self._publisher.publish(workspace_id, mode, request_id))
            reason = None if published else "publish_failed"
        except ValueError:
            # A record whose ids are not the opaque shapes is not sent at all.
            published, reason = False, "malformed_record"
        self._record_attempt(record, mode, published)
        log.info("workspace dispatch workspace=%s mode=%s published=%s",
                 workspace_id, mode, published)
        return {"published": published, "mode": mode, "reason": reason}

    def _record_attempt(self, record: Mapping[str, Any], mode: str, published: bool) -> None:
        """`dispatch` counts every attempt; `run` (§1.1) is written only while
        the record is still `approved` for the same request -- once the job has
        claimed it, `run` is the job's."""
        ref = self._record_ref(str(record.get("tenant_id")))
        request_id = record.get("request_id")
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return
            data = snap.to_dict() or {}
            previous = data.get("dispatch") or {}
            attempts = int(previous.get("attempts") or 0) + 1
            # The first attempt for THIS request: the stuck rule measures an
            # unclaimed dispatch from it, since the sweep refreshes the last
            # one every 10 minutes. A retry's fresh request id restarts it.
            first = previous.get("first_attempt_at") \
                if previous.get("request_id") == request_id else None
            patch: dict[str, Any] = {"dispatch": {
                "attempts": attempts,
                "request_id": request_id,
                "first_attempt_at": first if isinstance(first, datetime) else now,
                "last_attempt_at": now,
                "last_published_at": now if published else previous.get("last_published_at"),
                "last_ok": published,
                "mode": mode,
            }}
            if published and mode == MODE_CREATE and data.get("state") == ws.APPROVED \
                    and data.get("request_id") == request_id:
                patch["run"] = {"build_id": None, "attempt": attempts,
                                "published_at": now, "mode": mode}
            txn.update(ref, patch)

        try:
            _apply(transaction)
        except Exception as exc:  # noqa: BLE001 - the message is out; the sweep re-reads
            log.warning("workspace dispatch not recorded workspace=%s: %s",
                        record.get("workspace_id"), type(exc).__name__)

    def sweep(self, pool: Any = None) -> dict[str, Any]:
        """§2.2's dispatch sweep, and the detector for a record nothing is
        advancing.

        Every `approved` record (at most MAX_PEOPLE) is walked, publishing on
        or off. Each is checked against the stuck rule
        (`workspaces.waiting_because`) as it was read, and a stuck one is
        reported (`_report_stuck`) at most once per STUCK_REPORT_EVERY. Then,
        only when publishing is on, a record whose last attempt is at least
        DISPATCH_EVERY old (or that has none) is published again, at most
        SWEEP_LIMIT per call; one published less than ten minutes ago is left
        alone, so a trigger that is merely slow is not sent the same workspace
        twice in a row.

        WHY THE WALK RUNS WITH PUBLISHING OFF: it returned early there until
        2026-10-10, and w-752763 then sat approved for 19 hours with nobody
        told. Publishing off is the one state in which no build will ever
        come, so it is the state that most needs reporting.

        With a `pool` it then applies the pending loans of every workspace
        now `ready` (`apply_pending_loans`, LOAN-NEW): the job that marks a
        workspace ready runs no swarm-api code, so this walk is what notices.
        Without one (the broker client could not be built) `loans` is null."""
        publishing = bool(self._publisher.enabled)
        now = self._now()
        query = self._db.collection(ws.WORKSPACES).where(
            filter=FieldFilter("state", "==", ws.APPROVED)).limit(MAX_PEOPLE)
        considered = published = failed = recent = stuck = reported = 0
        reasons: dict[str, int] = {}
        touched: list[dict[str, Any]] = []
        for snap in query.stream():
            record = snap.to_dict() or {}
            if record.get("state") != ws.APPROVED:
                continue
            considered += 1
            reason = ws.waiting_because(record, publishing=publishing, now=now)
            if reason is not None:
                stuck += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                if self._report_stuck(record, reason, now):
                    reported += 1
            if not publishing:
                continue
            last = (record.get("dispatch") or {}).get("last_attempt_at")
            if isinstance(last, datetime) and now - last < DISPATCH_EVERY:
                recent += 1
                continue
            if published + failed >= SWEEP_LIMIT:
                continue
            result = self._dispatch(record, MODE_CREATE)
            if result["published"]:
                published += 1
            else:
                failed += 1
            touched.append({"workspace_id": record.get("workspace_id"),
                            "published": result["published"]})
        log.info("workspace sweep publishing=%s considered=%d published=%d failed=%d "
                 "recent=%d stuck=%d reported=%d", publishing, considered, published,
                 failed, recent, stuck, reported)
        return {"publishing": publishing, "considered": considered, "published": published,
                "failed": failed, "recent": recent, "stuck": stuck,
                "stuck_reported": reported, "stuck_reasons": reasons,
                "workspaces": touched,
                "loans": None if pool is None else self.apply_pending_loans(pool)}

    def _report_stuck(self, record: Mapping[str, Any], reason: str, now: datetime) -> bool:
        """One `workspace_stuck` entry for a stuck record, unless one was
        written for it less than STUCK_REPORT_EVERY ago. The record's
        `stuck_reported_at` is written first, in a transaction that re-reads
        it, so two sweeps at once report it once; the entry is logged only
        after that write. True when this call reported it.

        The entry names the opaque workspace id and nothing else about the
        person: no email, no tenant id."""
        ref = self._record_ref(str(record.get("tenant_id")))
        request_id = record.get("request_id")
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("state") != ws.APPROVED or data.get("request_id") != request_id:
                return False
            last = data.get("stuck_reported_at")
            if isinstance(last, datetime) and now - last < STUCK_REPORT_EVERY:
                return False
            txn.update(ref, {"stuck_reported_at": now})
            return True

        try:
            due = _apply(transaction)
        except Exception as exc:  # noqa: BLE001 - one record, never the sweep
            log.warning("workspace stuck report not recorded workspace=%s: %s",
                        record.get("workspace_id"), type(exc).__name__)
            return False
        if not due:
            return False
        since = ws.approved_at(record)
        minutes = None if since is None else max(0, int((now - since).total_seconds() // 60))
        log.warning(
            "workspace stuck workspace=%s reason=%s minutes_waiting=%s",
            record.get("workspace_id"), reason, minutes,
            extra={"event": STUCK_EVENT,
                   "workspace_id": record.get("workspace_id"),
                   "reason": reason,
                   "approved_at": _iso(since),
                   "minutes_waiting": minutes})
        return True

    # -- loans ---------------------------------------------------------------------

    def lendable(self, pool: Any, *, admin_email: str, store: Any) -> list[dict[str, Any]]:
        """The pool accounts this admin may lend (§6.4, owner 2026-10-08):
        those owned by a GROUP tenant, or by the admin's OWN personal tenant.
        Read from the broker, the pool's single writer, owner by owner, and
        kept only where the broker says that owner owns it."""
        owners = [ws.personal_tenant_id(admin_email)]
        owners += sorted(t.tenant_id for t in store.list_tenants() if t.kind == "group")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for owner in dict.fromkeys(owners):
            payload = pool.list_accounts(owner)
            for account in payload.get("accounts") or []:
                if not isinstance(account, dict) or account.get("owner_tenant") != owner:
                    continue
                if account.get("provider", ws.CLAUDE_PROVIDER) != ws.CLAUDE_PROVIDER:
                    continue
                account_id = str(account.get("account_id") or "")
                if not account_id or account_id in seen:
                    continue
                seen.add(account_id)
                out.append({
                    "account_id": account_id,
                    "owner_tenant": owner,
                    "label": account.get("label"),
                    "state": account.get("state"),
                    "lend_to": list(account.get("lend_to") or []),
                    "display_name": _account_name(account.get("display_name")),
                })
        return out

    def set_loan(self, workspace_id: str, *, by: str, account_id: str, lend: bool,
                 pool: Any, store: Any) -> dict[str, Any]:
        """Lend (`lend=True`) or reclaim an account for the person behind
        `workspace_id`. The account must be one `lendable` lists for THIS
        admin; anything else is one 404, so the route confirms nothing about
        another tenant's account. A person's own account is refused: its
        owner keeps `PUT /v1/accounts/{id}/lending`.

        A lend to a workspace that is not `ready` is recorded pending and
        calls no IAM (the module docstring, LOAN-NEW); a reclaim cancels a
        pending loan as well as the lending itself."""
        by = by.strip().lower()
        record = self.record(workspace_id)
        tenant_id = str(record["tenant_id"])
        accounts = {a["account_id"]: a for a in self.lendable(pool, admin_email=by, store=store)}
        account = accounts.get(account_id)
        if account is None:
            raise AccountNotLendable(
                f"no account {account_id!r} you may lend: an admin lends accounts owned by "
                "a group tenant or by their own personal tenant")
        if account["owner_tenant"] == tenant_id:
            raise AccountIsTheirs(
                "this is the person's own account; its owner changes its lending, not People")
        answer = {"workspace_id": workspace_id, "account_id": account_id,
                  "owner_tenant": account["owner_tenant"], "lent": lend}
        current = list(account["lend_to"])
        if lend and tenant_id not in current:
            held = self._hold_unless_ready(workspace_id, by=by, account=account)
            if held is not None:
                changed, state = held
                log.info("workspace loan pending workspace=%s changed=%s", workspace_id, changed)
                return {**answer, "changed": changed, "state": LOAN_PENDING,
                        "message": pending_message(state)}
        cancelled = False if lend else self._cancel_pending(workspace_id, by=by, account=account)
        wanted = ([*current, tenant_id] if tenant_id not in current else current) if lend \
            else [t for t in current if t != tenant_id]
        changed = wanted != current
        if changed:
            self._set_lending(pool, record, account, wanted)
        self._record_loan(record, by=by, account=account, lend=lend, changed=changed)
        log.info("workspace loan workspace=%s lend=%s changed=%s", workspace_id, lend, changed)
        return {**answer, "changed": changed or cancelled,
                "state": LOAN_APPLIED if lend else LOAN_RECLAIMED}

    def _set_lending(self, pool: Any, record: Mapping[str, Any], account: Mapping[str, Any],
                     lend_to: list[str]) -> None:
        """The broker call. Its 422 on a lend -- the grant refused -- becomes
        an error naming the member and the broker's reason. The broker grants
        before it writes, so after a refusal nothing changed anywhere."""
        tenant_id = str(record["tenant_id"])
        try:
            pool.set_lending(account["account_id"], lend_to=lend_to)
        except ValidationFailed as exc:
            if tenant_id not in lend_to:
                raise
            member = worker_member(tenant_id, self._project_id)
            reason = exc.message
            refusals.refuse(LoanGrantFailed(
                f"could not lend {account['account_id']} to workspace "
                f"{record.get('workspace_id')}: the quota broker refused to grant {member} "
                f"read access to the account's secret ({reason}). The workspace is ready, so "
                "that service account should exist; check it in IAM. Nothing was changed.",
                detail={"workspace_id": record.get("workspace_id"),
                        "account_id": account["account_id"], "member": member,
                        "reason": reason}))
            # Switched off, the refusal is only logged: serve the broker's
            # own refusal, which is what this call raised before LOAN_GRANT_FAILED.
            raise

    def _pending_ref(self, tenant_id: str) -> Any:
        return self._db.collection(PENDING_LOANS).document(tenant_id)

    def _hold_unless_ready(self, workspace_id: str, *, by: str,
                           account: Mapping[str, Any]) -> tuple[bool, str] | None:
        """None when the workspace is `ready` (lend now). Otherwise the loan
        is recorded pending with its audit entry, read against the record's
        state in the same transaction, and this returns (changed, state)."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[bool, str] | None:
            _, record = self._resolve(txn, workspace_id)
            state = ws.state_of(record)
            if state == ws.READY:
                return None
            tenant_id = str(record["tenant_id"])
            ref = self._pending_ref(tenant_id)
            snap = _snapshot(txn.get(ref))
            loans = list(((snap.to_dict() or {}) if snap.exists else {}).get("loans") or [])
            if any(loan.get("account_id") == account["account_id"] for loan in loans):
                return False, state
            loans.append({"account_id": account["account_id"],
                          "owner_tenant": account["owner_tenant"], "by": by,
                          "at": self._now(), "last_error": None, "last_attempt_at": None})
            txn.set(ref, {"tenant_id": tenant_id, "workspace_id": workspace_id,
                          "loans": loans})
            self._audit(txn, "lend_pending", workspace_id, by, {
                "account_id": account["account_id"],
                "owner_tenant": account["owner_tenant"]})
            return True, state

        return _apply(transaction)

    def _cancel_pending(self, workspace_id: str, *, by: str,
                        account: Mapping[str, Any]) -> bool:
        """A reclaim drops the account's pending loan, if there is one, with
        its audit entry (`reclaim_pending`). True when one was dropped."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            _, record = self._resolve(txn, workspace_id)
            ref = self._pending_ref(str(record["tenant_id"]))
            snap = _snapshot(txn.get(ref))
            loans = list(((snap.to_dict() or {}) if snap.exists else {}).get("loans") or [])
            kept = [loan for loan in loans if loan.get("account_id") != account["account_id"]]
            if len(kept) == len(loans):
                return False
            txn.update(ref, {"loans": kept})
            self._audit(txn, "reclaim_pending", workspace_id, by, {
                "account_id": account["account_id"],
                "owner_tenant": account["owner_tenant"]})
            return True

        return _apply(transaction)

    def _record_loan(self, record: Mapping[str, Any], *, by: str, account: Mapping[str, Any],
                     lend: bool, changed: bool, pending_since: datetime | None = None) -> None:
        """The audit entry, and on a lend the person's open loan request
        closed, in one transaction. An unchanged call writes nothing --
        except that a pending loan being applied (`pending_since`) is always
        removed from `pending_loans` here, so once the broker has answered
        the loan is never both pending and lent."""
        if not changed and pending_since is None:
            return
        workspace_id = str(record.get("workspace_id"))
        tenant_id = str(record["tenant_id"])
        loan_ref = self._db.collection(ws.LOAN_REQUESTS).document(tenant_id)
        pending_ref = self._pending_ref(tenant_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            loan = _snapshot(txn.get(loan_ref))
            pending = _snapshot(txn.get(pending_ref)) if pending_since is not None else None
            now = self._now()
            if lend and loan.exists and (loan.to_dict() or {}).get("state") == ws.REQUESTED:
                txn.update(loan_ref, {"state": LOAN_LENT, "decision": {
                    "by": by, "at": now, "account_id": account["account_id"],
                    "owner_tenant": account["owner_tenant"]}})
            if pending is not None and pending.exists:
                loans = list((pending.to_dict() or {}).get("loans") or [])
                txn.update(pending_ref, {"loans": [
                    p for p in loans if p.get("account_id") != account["account_id"]]})
            if not changed:
                return
            detail = {"account_id": account["account_id"],
                      "owner_tenant": account["owner_tenant"]}
            if pending_since is not None:
                detail["pending_since"] = pending_since
            self._audit(txn, "lend" if lend else "reclaim", workspace_id, by, detail)

        _apply(transaction)

    def decline_loan(self, workspace_id: str, *, by: str,
                     reason: str | None = None) -> dict[str, Any]:
        """requested -> declined, for the person behind `workspace_id`
        (LOAN-DECLINE). The reason is optional, at most `MAX_LOAN_REASON`
        characters, and shown to the person; blank is none. Refused with
        `LOAN_REQUEST_NOT_OPEN` when no request is open."""
        by = by.strip().lower()
        text = " ".join((reason or "").split()) or None
        if text is not None and len(text) > MAX_LOAN_REASON:
            raise ValidationFailed(
                f"a decline's reason is at most {MAX_LOAN_REASON} characters")
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            _, record = self._resolve(txn, workspace_id)
            ref = self._db.collection(ws.LOAN_REQUESTS).document(str(record["tenant_id"]))
            snap = _snapshot(txn.get(ref))
            loan = (snap.to_dict() or {}) if snap.exists else {}
            state = loan.get("state")
            if state != ws.REQUESTED:
                raise LoanRequestNotOpen(
                    f"workspace {workspace_id} has no open loan request to decline"
                    + (f"; its last one is {state}" if state else ""),
                    detail={"workspace_id": workspace_id, "state": state})
            txn.update(ref, {"state": LOAN_DECLINED, "decision": {
                "verdict": LOAN_DECLINED, "by": by, "at": self._now(), "reason": text}})
            self._audit(txn, "loan_declined", workspace_id, by, {
                "request_id": loan.get("request_id"), "reason_length": len(text or "")})

        _apply(transaction)
        log.info("loan request declined workspace=%s", workspace_id)
        return {"workspace_id": workspace_id, "state": LOAN_DECLINED, "reason": text}

    def apply_pending_loans(self, pool: Any) -> dict[str, int]:
        """The sweep's second half: every pending loan of a `ready`
        workspace is lent through the same broker call a direct lend makes.
        A loan whose account is gone, or no longer its owner's, is dropped
        (`lend_pending_dropped`); one the broker refuses stays pending with
        the reason in `last_error`, and is tried again next sweep."""
        applied = failed = waiting = dropped = 0
        for snap in self._db.collection(PENDING_LOANS).limit(MAX_PEOPLE).stream():
            loans = list((snap.to_dict() or {}).get("loans") or [])
            if not loans:
                continue
            record_snap = self._record_ref(snap.id).get()
            record = record_snap.to_dict() if record_snap.exists else None
            if not record or ws.state_of(record) != ws.READY:
                waiting += len(loans)
                continue
            for loan in loans:
                try:
                    if self._apply_pending(pool, record, loan):
                        applied += 1
                    else:
                        dropped += 1
                except Exception as exc:  # noqa: BLE001 - one loan, never the sweep
                    failed += 1
                    self._note_pending_failure(snap.id, str(record.get("workspace_id")), loan, exc)
        if applied or failed or dropped:
            log.info("pending loans applied=%d failed=%d waiting=%d dropped=%d",
                     applied, failed, waiting, dropped)
        return {"applied": applied, "failed": failed, "waiting": waiting, "dropped": dropped}

    def _apply_pending(self, pool: Any, record: Mapping[str, Any],
                       loan: Mapping[str, Any]) -> bool:
        """One pending loan, on a ready workspace. False when it was dropped."""
        tenant_id = str(record["tenant_id"])
        workspace_id = str(record.get("workspace_id"))
        owner = str(loan.get("owner_tenant") or "")
        account_id = str(loan.get("account_id") or "")
        by = str(loan.get("by") or "")
        listed = pool.list_accounts(owner).get("accounts") or []
        found = next((a for a in listed if isinstance(a, dict)
                      and a.get("account_id") == account_id
                      and a.get("owner_tenant") == owner), None)
        if found is None or owner == tenant_id:
            self._drop_pending(record, loan)
            log.warning("pending loan dropped workspace=%s: the account is no longer its "
                        "owner's to lend", workspace_id)
            return False
        account = {"account_id": account_id, "owner_tenant": owner}
        current = list(found.get("lend_to") or [])
        changed = tenant_id not in current
        if changed:
            self._set_lending(pool, record, account, [*current, tenant_id])
        self._record_loan(record, by=by, account=account, lend=True, changed=changed,
                          pending_since=loan.get("at") or self._now())
        log.info("pending loan applied workspace=%s changed=%s", workspace_id, changed)
        return True

    def _drop_pending(self, record: Mapping[str, Any], loan: Mapping[str, Any]) -> None:
        ref = self._pending_ref(str(record["tenant_id"]))
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            loans = list(((snap.to_dict() or {}) if snap.exists else {}).get("loans") or [])
            txn.update(ref, {"loans": [
                p for p in loans if p.get("account_id") != loan.get("account_id")]})
            self._audit(txn, "lend_pending_dropped", str(record.get("workspace_id")),
                        str(loan.get("by") or ""), {
                            "account_id": loan.get("account_id"),
                            "owner_tenant": loan.get("owner_tenant")})

        _apply(transaction)

    def _note_pending_failure(self, tenant_id: str, workspace_id: str,
                              loan: Mapping[str, Any], exc: Exception) -> None:
        """The reason a pending loan was not applied, on the loan itself,
        where the People row shows it. The log line names the type only:
        the message names the member, and the member names the person."""
        reason = exc.message if isinstance(exc, ApiError) else type(exc).__name__
        log.warning("pending loan not applied workspace=%s: %s", workspace_id,
                    type(exc).__name__)
        ref = self._pending_ref(tenant_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            loans = list(((snap.to_dict() or {}) if snap.exists else {}).get("loans") or [])
            txn.update(ref, {"loans": [
                {**p, "last_error": reason, "last_attempt_at": self._now()}
                if p.get("account_id") == loan.get("account_id") else p for p in loans]})

        try:
            _apply(transaction)
        except Exception as note_exc:  # noqa: BLE001 - the next sweep tries again
            log.warning("pending loan failure not recorded: %s", type(note_exc).__name__)

    # -- the list (§6.4 part 1) ------------------------------------------------------

    def everyone(self, *, store: Any) -> dict[str, Any]:
        """Everyone who has signed in, and everyone with a workspace record:
        teams, GitHub, workspace state, Claude account source and last
        activity. Pending requests first, then the most recently active."""
        people: dict[str, dict[str, Any]] = {}
        for snap in self._db.collection(ws.PEOPLE).limit(MAX_PEOPLE).stream():
            data = snap.to_dict() or {}
            people[snap.id] = {"person": data}
        for snap in self._db.collection(ws.WORKSPACES).limit(MAX_PEOPLE).stream():
            data = snap.to_dict() or {}
            if ws.kind_of(data) != ws.PERSONAL:
                # A group's or a service's record is not a person: Admin ›
                # Tenants lists it (`records`), People does not.
                continue
            people.setdefault(snap.id, {})["record"] = data
        rows = [self._row(tenant_id, parts, store=store) for tenant_id, parts in people.items()]
        rows = [row for row in rows if row is not None]
        pending_first = {ws.REQUESTED: 0}
        rows.sort(key=lambda r: (pending_first.get(r["workspace"]["state"], 1),
                                 -(r["_last"].timestamp() if r["_last"] else 0.0),
                                 r["email"]))
        # Every row's name from one lookup; null for all when it fails.
        names = self._names([row["email"] for row in rows]) if rows else {}
        for row in rows:
            row.pop("_last")
            row["display_name"] = names.get(row["email"])
        approved = sum(1 for r in rows if r["workspace"]["state"] == ws.APPROVED)
        return {
            "people": rows,
            "count": len(rows),
            "pending": sum(1 for r in rows if r["workspace"]["state"] == ws.REQUESTED),
            # The banner's figures: whether this deployment builds workspaces
            # at all, and how many approved records wait on it.
            "provisioning": {"available": bool(self._publisher.enabled),
                             "approved_waiting": approved},
            "audit": self.audit(),
        }

    def _row(self, tenant_id: str, parts: Mapping[str, Any], *, store: Any) -> dict[str, Any] | None:
        person = parts.get("person") or {}
        record = parts.get("record")
        email = str(person.get("principal") or (record or {}).get("principal") or "").lower()
        if not email:
            return None
        tenant: Tenant | None = store.get_tenant(tenant_id)
        last_submitted = self._last_submission(store, tenant_id, email)
        last = _later(person.get("last_seen"), last_submitted)
        loan = self._db.collection(ws.LOAN_REQUESTS).document(tenant_id).get()
        loan_data = loan.to_dict() if loan.exists else None
        pending = self._pending_ref(tenant_id).get()
        pending_loans = list((pending.to_dict() or {}).get("loans") or []) \
            if pending.exists else []
        return {
            "email": email,
            "teams": list(person.get("teams") or []),
            "github": self._github(email),
            "workspace": self._admin_view(record),
            "claude_account": self._claude_account(tenant_id, tenant, store=store),
            # A decision's time and reason, never its `by`: the audit names
            # the admin. The reason is the one the person is shown.
            "loan_request": None if not loan_data else {
                "state": loan_data.get("state"),
                "requested_at": _iso(loan_data.get("requested_at")),
                "decided_at": _iso((loan_data.get("decision") or {}).get("at")),
                "reason": (loan_data.get("decision") or {}).get("reason"),
            },
            # Loans recorded while the workspace was not ready (LOAN-NEW).
            # Never `by`, like the decision: the audit names the admin.
            "pending_loans": [{
                "account_id": p.get("account_id"),
                "owner_tenant": p.get("owner_tenant"),
                "since": _iso(p.get("at")),
                "last_error": p.get("last_error"),
            } for p in pending_loans],
            "first_seen": _iso(person.get("first_seen")),
            "last_seen": _iso(person.get("last_seen")),
            "last_submitted": _iso(last_submitted),
            "last_active": _iso(last),
            "_last": last,
        }

    def _last_submission(self, store: Any, tenant_id: str, email: str) -> datetime | None:
        """The newest task this person submitted in their own tenant (index
        tasks-tenant-submitter-created). A team's tasks are not read: a
        submission there passes `tenant_for`, which updates `last_seen`."""
        try:
            page = store.list_tasks(tenant_id, limit=1, submitted_by=email)
        except Exception as exc:  # noqa: BLE001 - one column, never the list
            log.warning("people list: last submission unread: %s", type(exc).__name__)
            return None
        for task in page.items:
            created = getattr(task, "created_at", None)
            if isinstance(created, datetime):
                return created
        return None

    def _github(self, email: str) -> str:
        """`connected` when the person holds a user connection of their own
        that is not revoked or expired, `expired` when only an expired one,
        else `none`. The team's tenant token is not the person's connection."""
        query = self._db.collection(GIT_TOKENS).where(
            filter=FieldFilter("user", "==", email)).limit(20)
        states = {str((snap.to_dict() or {}).get("state") or "")
                  for snap in query.stream()
                  if (snap.to_dict() or {}).get("scope") == "user"}
        if states & {"active", "unverified"}:
            return "connected"
        if "expired" in states:
            return "expired"
        return "none"

    def _claude_account(self, tenant_id: str, tenant: Tenant | None, *,
                        store: Any) -> dict[str, Any]:
        """§5.1 (3), with who lent it: `own`, `lent`, `provider_key` or
        `none`. The counts are `Workspaces.claude_accounts`'s rule.

        `lent_accounts` names WHICH accounts are lent (PEOPLE-LOAN-VIEW, owner
        2026-10-13): `lent_by` is only the owner tenants, so the console said
        "lent by eng" and an admin could not see what they had lent. Each is
        the account's id, label, owner and state, its Name (#1913), and the
        owner's display name; never the document as it is, which names its
        secret."""
        counts = ws.Workspaces(self._db, now=self._now, gate=False).claude_accounts(
            tenant_id, tenant)
        lent: list[dict[str, Any]] = []
        if counts["lent"]:
            query = self._db.collection(ws.ACCOUNTS).where(
                filter=FieldFilter("lend_to", "array_contains", tenant_id)).limit(20)
            names: dict[str, str] = {}
            for snap in query.stream():
                data = snap.to_dict() or {}
                owner = str(data.get("owner_tenant") or "")
                if not owner or owner == tenant_id:
                    continue
                if data.get("provider", ws.CLAUDE_PROVIDER) != ws.CLAUDE_PROVIDER:
                    continue
                if owner not in names:
                    owner_tenant = store.get_tenant(owner)
                    names[owner] = getattr(owner_tenant, "display_name", None) or owner
                lent.append({"account_id": str(data.get("account_id") or snap.id),
                             "label": data.get("label"), "owner_tenant": owner,
                             "owner_name": names[owner], "state": data.get("state"),
                             "display_name": _account_name(data.get("display_name"))})
            lent.sort(key=lambda a: (a["owner_tenant"], a["account_id"]))
        lenders = sorted({a["owner_tenant"] for a in lent})
        source = "own" if counts["own"] else "lent" if counts["lent"] else \
            "provider_key" if counts["provider_key"] else "none"
        return {"source": source, "own": counts["own"], "lent": counts["lent"],
                "lent_by": lenders, "lent_accounts": lent,
                "provider_key": counts["provider_key"]}

    def _admin_view(self, record: Mapping[str, Any] | None) -> dict[str, Any]:
        """What an admin is shown of a record: the person's view (W1's
        `workspaces.view`), with the dispatch and retry state, and the
        decision's verdict and time. Never `decision.by`, which stays in
        Firestore, and never the tenant id."""
        out = ws.view(record, console_url=self._console_url,
                      publishing=bool(self._publisher.enabled), now=self._now())
        out.pop("tenant_id", None)
        if record:
            # The principal an admin filed or approves: a person's, a group's
            # or a service account's address (design §4.2.2). Never
            # `requested_by`, which, like `decision.by`, stays in Firestore.
            out["principal"] = record.get("principal")
            dispatch = record.get("dispatch") or {}
            run = record.get("run") or {}
            out["dispatch"] = None if not dispatch else {
                "attempts": dispatch.get("attempts"),
                "last_attempt_at": _iso(dispatch.get("last_attempt_at")),
                "last_published_at": _iso(dispatch.get("last_published_at")),
                "last_ok": dispatch.get("last_ok"),
                "mode": dispatch.get("mode"),
            }
            out["run"] = None if not run else {k: _iso(v) for k, v in run.items()}
            retry = record.get("retry") or {}
            out["retried_at"] = _iso(retry.get("at"))
            if ws.state_of(record) == ws.NEEDS_OWNER:
                out["needs_owner_step"] = (record.get("failure") or {}).get("step") or next(
                    (k for k, v in sorted((record.get("steps") or {}).items())
                     if isinstance(v, dict) and v.get("state") == "held"), None)
        return out

    def audit(self, limit: int = AUDIT_SHOWN) -> list[dict[str, Any]]:
        """The last `limit` admin_audit entries, newest first: who did what,
        to which workspace (or admin email), and when."""
        query = self._db.collection(AUDIT_COLLECTION).order_by(
            "at", direction=firestore.Query.DESCENDING).limit(limit)
        out = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            out.append({k: _iso(v) for k, v in data.items()})
        return out
