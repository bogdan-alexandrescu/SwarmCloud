"""`approvals/{approval_id}`: one record, one inbox (docs/schedules.md §4.5-§4.7, lane S5).

KINDS. `run` (a firing waiting to create work), `plan` (an issue run's plan),
`merge` (a green, reviewed pull request waiting), `proposal` (an observer or
flake proposal to file as an issue), `spec` (a `custom-prompt` digest) and
`hold` (any §4.4 hold, `NEEDS_OWNER` or `NEEDS_SECOND_MEMBER`).

AWAIT-INPUT STEPS ARE PROJECTED, NEVER RECORDED, AND NEVER DECIDED HERE
(docs/design/human-steps.md §4.5, lane HW-NOTIFY). A workflow step PARKED
`AWAITING_INPUT` waits for any member of its tenant to answer (Q2), so every
member's inbox shows it, as kind `input` with id `input:<task_id>`, derived
on each read from the task (`awaiting_input`, `project_input`). No record of
kind `input` is ever made (`request` refuses it), and no decide path takes
the id: `get` answers anything that is not an `apr_` id with 404, so approve
and reject on it are 404 and the answer route stays the step's one writer.
Once the step is answered, expired or cancelled it is no longer an open wait,
and the next read does not project it.

ISSUE RUNS JOIN THE INBOX WITHOUT MOVING. A PLANNED run's approve, edit and
reject stay in `routes/runs.py`; the inbox PROJECTS them (`project_run`, id
`run:<run_id>`) beside the records of this collection, and an approve from
the inbox calls the run's own `_approve` with the digest shown. So a plan
approved in Work › Runs, from `sc plan approve` or from the inbox is one
approval, with one source of truth: the run. A held plan is projected as
`kind: hold`, and the hold itself lives on the run (`approval_hold`), checked
inside `_approve`, so no approve path steps round it.

A DECISION IS ONE TRANSACTION (§4.5): the approval is re-read and must still
be `pending` (else 409 `already_decided`, so two approvers racing make one
transition) and unexpired, the digest shown must be the current one (409
`approval_changed`; for a merge, 409 `merge_changed` -- a push after the
request makes the approval stale), then the subject is moved and the
decision and its `schedule_audit` entry are written, all in that
transaction.

ONLY SOMEONE WHO MAY APPROVE AN ITEM MAY REJECT IT (BGD 2026-10-26, #2417
A5f). A rejection used to be any member's; it now passes the same who-checks
an approval does, else 403 `reject_approver_required` through its switch,
which ships on (`refusals.SWITCHES`). Off: any member rejects, as before.

WHO MAY DECIDE (§4.6). Any member of the item's tenant by default (SD5) --
`tenant_scope` puts only the caller's own tenant's items in reach, and a
platform admin decides another tenant's item by no route here. `owner_only`:
PLATFORM_OWNER, as a member of the tenant. `second_member`: any member but
the run's creator, its last plan editor and its schedule's last gate editor
-- except that a workspace admin decides alone, even as one of those (BGD
2026-10-26, "let an admin approve it, no need for two approvers");
a one-person `u-*` tenant types a confirmation naming the matched paths
(SD11). A named list: each a member now, asked of the directory. A
merge-tier (R3) item: never the schedule's last gate, scope or spec editor
when the tenant is a group (SD5).

A PENDING APPROVAL HOLDS NOTHING (invariant 1), before and after expiry. It
is a document: no task, lease or slot exists for it, and a plan's planner
task has already ended. Expiry (§4.7) tidies the inbox; it releases nothing.

TENANT-SCOPED (invariant 9). Every read compares the stored `tenant_id` with
the caller's and answers a mismatch with the same 404 as a missing id.

THE LAST 7 DAYS STAY READABLE (BGD 2026-10-27, #2417 A4). `history` serves
what was approved, rejected, answered or expired in the last HISTORY_DAYS,
newest first, each kind from its own source of truth -- a record, a run, a
task -- with one bounded read per source and nothing written.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_common.admission import _snapshot
from swarm_common.states import ParkReason, TaskState

from . import refusals, roles, schedaudit, schedulefire, schedules, scheduletypes
from .errors import Forbidden, NotFound
from .issueruns import (
    APPROVERS_OWNER_ONLY,
    APPROVERS_SECOND_MEMBER,
    AUTO_APPROVER,
    CONTRACT_PATTERNS,
    COST_PATTERNS,
    HOLD_NEEDS_OWNER,
    IAM_PATTERNS,
    OWNER_SIGNATURE,
    RUNS_COLLECTION,
    SECRETS_PATTERNS,
    SHARED_PATTERNS,
    STOP_CONTRACT,
    STOP_COST,
    STOP_IAM,
    STOP_PROTECTED_PATH,
    STOP_SECRETS,
    STOP_SECURITY,
    STOP_SHARED,
    STOP_TENANCY,
    STOP_WORKFLOWS,
    TENANCY_PATTERNS,
    WORKFLOW_HOLD_PATTERNS,
    WORKFLOWS_PATTERN,
    HoldApproverRequired,
    IssueRun,
    IssueRuns,
    Resign,
    RunState,
    hold_always_enforced,
    hold_confirmation,
    path_matches,
)
from .redaction import redact_detail
from .schedules import ScheduleConflict, ScheduleForbidden, ScheduleInvalid

log = logging.getLogger(__name__)

COLLECTION = "approvals"

RUN = "run"
PLAN = "plan"
MERGE = "merge"
PROPOSAL = "proposal"
SPEC = "spec"
HOLD = "hold"
#: A workflow step waiting for a member's answer: projected, never a record.
INPUT = "input"
#: The kinds a record of this collection is made with.
RECORD_KINDS = (RUN, PLAN, MERGE, PROPOSAL, SPEC, HOLD)
#: The kinds the inbox serves and filters on.
KINDS = RECORD_KINDS + (INPUT,)

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
SUPERSEDED = "superseded"
STATES = (PENDING, APPROVED, REJECTED, EXPIRED, SUPERSEDED)

#: A projected issue run's id in the inbox (§7.1).
PROJECTED_PREFIX = "run:"
#: A projected await-input step's id in the inbox (human-steps.md §4.5).
INPUT_PREFIX = "input:"
#: What the expiry and an inbox write record as the actor.
EXPIRY_ACTOR = "approval-expiry"

#: Pending items read per inbox or expiry pass. An inbox is what people are
#: waiting to decide: past this many the backlog is itself the news.
INBOX_SCAN = 500
#: The inbox summary: one line, masked.
SUMMARY_CHARS = 300

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: The schedule-firing states this module moves a run approval's firing
#: to. Named in §1.4; `schedulefire` owns the others.
FIRING_EXPIRED = "expired"
FIRING_REJECTED = "rejected"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _aware(moment: Any) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: Any) -> str | None:
    moment = _aware(moment)
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def approval_id(kind: str, key: str) -> str:
    """Deterministic, so a retried request finds the record it already made."""
    return "apr_" + hashlib.sha256(f"{kind}\n{key}".encode()).hexdigest()[:24]


def summary_text(text: str) -> str:
    """What the inbox shows: one line, masked by the API's redaction, bounded."""
    return redact_detail(" ".join(str(text or "").split()), limit=SUMMARY_CHARS)


def merge_digest(head_sha: str, verdict: str | None) -> dict[str, Any]:
    """A merge approval's digest (§4.5): the head and the review's verdict."""
    return {"head_sha": head_sha, "verdict": verdict}


def _digest_matches(stored: Any, sent: Any) -> bool:
    """A merge digest may be sent as the object or as `<head_sha>:<verdict>`."""
    if isinstance(stored, Mapping):
        if isinstance(sent, Mapping):
            return dict(stored) == dict(sent)
        return isinstance(sent, str) and sent == f"{stored.get('head_sha')}:{stored.get('verdict')}"
    return isinstance(sent, str) and sent == stored


def ttl_hours(schedule: Mapping[str, Any] | None) -> int:
    """§4.7: the schedule's `approval_ttl_hours`, else the default 72."""
    value = ((schedule or {}).get("gate") or {}).get("approval_ttl_hours")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, schedules.APPROVAL_TTL_MAX_HOURS)
    return schedules.APPROVAL_TTL_DEFAULT_HOURS


def read_schedule(db: Any, tenant_id: str, schedule_id: str | None) -> dict[str, Any] | None:
    """The schedule, if it is still this tenant's; None otherwise."""
    if not schedule_id:
        return None
    snap = db.collection(schedules.COLLECTION).document(str(schedule_id)).get()
    doc = snap.to_dict() if snap.exists else None
    return doc if doc and doc.get("tenant_id") == tenant_id else None


def _not_found(item_id: str) -> NotFound:
    return NotFound(f"approval {(item_id or '')[:80]!r} not found")


def to_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if isinstance(value, datetime):
            out[key] = _iso(value)
        elif isinstance(value, Mapping):
            out[key] = {k: _iso(v) if isinstance(v, datetime) else v for k, v in value.items()}
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def request(
    db: Any,
    now: datetime,
    *,
    tenant_id: str,
    kind: str,
    key: str,
    subject: Mapping[str, Any],
    digest: Any,
    summary: str,
    approvers: Any,
    ttl: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the approval, or return the one a previous request made.

    `approvers` is resolved at creation (§4.6) and stored; `expires_at` is
    `requested_at` + `ttl` hours.
    """
    if kind == INPUT:
        raise ValueError("an await-input step is projected from its task, never recorded")
    if kind not in RECORD_KINDS:
        raise ValueError(f"{kind!r} is not an approval kind")
    item_id = approval_id(kind, key)
    ref = db.collection(COLLECTION).document(item_id)

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        snap = _snapshot(txn.get(ref))
        if snap.exists:
            doc = snap.to_dict() or {}
            if doc.get("tenant_id") != tenant_id:
                raise ValueError(f"approval {item_id} belongs to another tenant")
            return doc
        doc = {
            "approval_id": item_id,
            "tenant_id": tenant_id,
            "kind": kind,
            "subject": dict(subject),
            "digest": dict(digest) if isinstance(digest, Mapping) else digest,
            "summary": summary_text(summary),
            "approvers": approvers,
            "state": PENDING,
            "requested_at": now,
            "expires_at": now + timedelta(hours=ttl),
            "decided_by": None,
            "decided_at": None,
            "reason": None,
            **dict(extra or {}),
        }
        txn.set(ref, doc)
        return doc

    return _apply(db.transaction())


def get(db: Any, tenant_id: str, item_id: str) -> dict[str, Any]:
    if not str(item_id or "").startswith("apr_"):
        raise _not_found(item_id)
    snap = db.collection(COLLECTION).document(item_id).get()
    doc = snap.to_dict() if snap.exists else None
    if doc is None or doc.get("tenant_id") != tenant_id:
        raise _not_found(item_id)
    return doc


def pending(db: Any, tenant_id: str, *, limit: int = INBOX_SCAN) -> list[dict[str, Any]]:
    """The tenant's pending records, oldest first. S4's index: tenant_id, state, requested_at."""
    query = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "==", PENDING))
        .limit(limit)
    )
    rows = [snap.to_dict() or {} for snap in query.stream()]
    rows = [r for r in rows if r.get("tenant_id") == tenant_id and r.get("state") == PENDING]
    rows.sort(key=lambda r: (_aware(r.get("requested_at")) or _EPOCH, str(r.get("approval_id"))))
    return rows


Move = Callable[[Any, Mapping[str, Any]], Mapping[str, Any] | None]


def decide(
    db: Any,
    now: datetime,
    tenant_id: str,
    item_id: str,
    *,
    approve: bool,
    by: str,
    digest: Any = None,
    reason: str | None = None,
    move: Move | None = None,
    audit_detail: Mapping[str, Any] | None = None,
    waiver: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """§4.5's transaction: state and digest checked, subject moved, decision and audit written.

    `move(txn, doc)` moves the subject inside the same transaction. It must
    do its reads before its writes, as every Firestore transaction must, and
    may raise to refuse; nothing is then written.

    `waiver` is `check_gate_approver`'s owner waiver: a second audit row,
    `second_person_waived_owner`, beside the decision's, in the same
    transaction, so no waived decision is ever stored without it (#2259).
    """
    if waiver and not approve:
        raise ValueError("a waiver is written only beside an approval")
    ref = db.collection(COLLECTION).document(item_id)
    if not approve and not (reason or "").strip():
        raise ScheduleInvalid("reason_required", "a rejection says why")

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        snap = _snapshot(txn.get(ref))
        doc = snap.to_dict() if snap.exists else None
        if doc is None or doc.get("tenant_id") != tenant_id:
            raise _not_found(item_id)
        if doc.get("state") != PENDING:
            raise ScheduleConflict(
                "already_decided", f"approval {item_id} is already {doc.get('state')}", state=doc.get("state")
            )
        expires = _aware(doc.get("expires_at"))
        if expires is not None and expires <= now:
            raise ScheduleConflict("approval_expired", f"approval {item_id} expired at {_iso(expires)}")
        if approve and not _digest_matches(doc.get("digest"), digest):
            code = "merge_changed" if doc.get("kind") == MERGE else "approval_changed"
            raise ScheduleConflict(
                code, "what is waiting has changed since it was shown; read it again",
                digest=to_api({"d": doc.get("digest")})["d"],
            )
        moved = dict(move(txn, doc) or {}) if move is not None else {}
        update = {
            "state": APPROVED if approve else REJECTED,
            "decided_by": by,
            "decided_at": now,
            "reason": (reason or "").strip()[:1000] or None,
        }
        txn.update(ref, update)
        subject = doc.get("subject") or {}
        if subject.get("schedule_id"):
            schedaudit.append(
                txn, db, schedule_id=subject["schedule_id"], tenant_id=tenant_id,
                action=schedaudit.APPROVED if approve else schedaudit.REJECTED, by=by, at=now,
                detail={"approval_id": item_id, "kind": doc.get("kind"), "subject": dict(subject),
                        "reason": update["reason"], **moved, **dict(audit_detail or {})},
            )
        if waiver:
            schedaudit.append(
                txn, db, schedule_id=str(waiver.get("schedule_id") or subject.get("schedule_id") or ""),
                tenant_id=tenant_id, action=schedaudit.SECOND_PERSON_WAIVED_OWNER, by=by, at=now,
                detail={"approval_id": item_id, "kind": doc.get("kind"), "subject": dict(subject),
                        **dict(waiver)},
            )
        return {**doc, **update}

    return _apply(db.transaction())


def _close(db: Any, now: datetime, doc: Mapping[str, Any], state: str, move: Move | None = None) -> bool:
    """Expire or supersede one pending record, in one transaction. False: it was not pending."""
    ref = db.collection(COLLECTION).document(str(doc.get("approval_id")))

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        current = snap.to_dict() if snap.exists else None
        if not current or current.get("state") != PENDING:
            return False
        if move is not None:
            move(txn, current)
        txn.update(ref, {"state": state, "decided_by": EXPIRY_ACTOR, "decided_at": now})
        subject = current.get("subject") or {}
        if state == EXPIRED and subject.get("schedule_id"):
            schedaudit.append(
                txn, db, schedule_id=subject["schedule_id"], tenant_id=current["tenant_id"],
                action=schedaudit.EXPIRED, by=EXPIRY_ACTOR, at=now,
                detail={"approval_id": current["approval_id"], "kind": current.get("kind"),
                        "subject": dict(subject)},
            )
        return True

    return _apply(db.transaction())


# --------------------------------------------------------------------------
# Who may decide (§4.6)
# --------------------------------------------------------------------------


def _tenant(ctx: Any, tenant_id: str) -> Any:
    return ctx.store.get_tenant(tenant_id)


def one_person(ctx: Any, tenant_id: str) -> bool:
    """A personal (`u-*`) tenant: its one member is its principal."""
    tenant = _tenant(ctx, tenant_id)
    return tenant is not None and getattr(tenant, "kind", "group") != "group"


def hold_excluded(ctx: Any, run: IssueRun) -> set[str]:
    """Who a `second_member` hold does not admit: the run's creator (and the
    member a swept run submits as), its last plan editor, and its schedule's
    last editor of gate, scope or spec."""
    people = {run.created_by, run.on_behalf_of, run.plan_edited_by}
    if run.schedule:
        schedule = read_schedule(ctx.db, run.tenant_id, run.schedule.get("schedule_id"))
        if schedule is not None:
            people.add(schedaudit.last_editor(ctx.db, run.tenant_id, schedule))
    return {str(p).strip().lower() for p in people if p}


def workspace_admin(ctx: Any, tenant_id: str, email: str) -> bool:
    """Whether `email` is an admin of workspace `tenant_id`: a platform admin
    acting there (`admin` in a group workspace, Q5), or a person whose own
    role grant there is `admin` -- what `roles.RoleStore.resolve` answers for
    their request, asked from the address because a hold is checked on paths
    that carry no request (`routes.runs._approve`, the GitHub inbox).

    Read FRESH, as `require_decider` reads the role floor: an admin demoted a
    moment ago decides alone no longer. FAILS CLOSED: a role that cannot be
    read, or an auto-approval, is not an admin, so the hold keeps its rule."""
    email = (email or "").strip().lower()
    if not email or email == AUTO_APPROVER:
        return False
    try:
        if ctx.authenticator.holds_platform_admin(email, fresh=True):
            return True
        store = getattr(ctx, "roles", None)
        return store is not None and store.person_role(tenant_id, email, fresh=True).role == roles.ADMIN
    except Exception as exc:  # noqa: BLE001 - an unread role never widens a hold
        log.warning("hold admin check unreadable tenant=%s (%s)", tenant_id, type(exc).__name__)
        return False


def _hold_refusal(ctx: Any, run: IssueRun, hold: Mapping[str, Any], *, email: str, is_owner: bool,
                  confirm: str | None) -> tuple[str, dict[str, Any]] | None:
    if email == AUTO_APPROVER:
        return "an auto-approval never satisfies a hold", {}
    if hold.get("approvers") == APPROVERS_OWNER_ONLY:
        if is_owner:
            return None
        return "only the platform owner may approve this held run", {}
    if hold.get("approvers") == APPROVERS_SECOND_MEMBER:
        if one_person(ctx, run.tenant_id):
            # SD11: no second member exists, so the one person approves
            # their own after typing the matched paths; the audit records it.
            expected = hold_confirmation(hold)
            if (confirm or "").strip() == expected:
                return None
            return ("this one-person workspace approves its own held run only with a typed "
                    "confirmation naming the matched paths", {"confirm": expected})
        # BGD 2026-10-26, "let an admin approve it, no need for two
        # approvers": a workspace admin is never one the hold excludes. Asked
        # only of someone excluded, so a second member costs no role read.
        if email.strip().lower() in hold_excluded(ctx, run) and not workspace_admin(ctx, run.tenant_id, email):
            return ("a second member or a workspace admin must approve this held run: not its "
                    "creator, its last plan editor or its schedule's last gate editor unless "
                    "they are an admin of the workspace", {})
        return None
    return f"the hold's approvers {hold.get('approvers')!r} are not ones this platform knows", {}


def check_hold(ctx: Any, run: IssueRun, *, email: str, is_owner: bool = False,
               confirm: str | None = None, hold: Mapping[str, Any] | None = None) -> None:
    """403 `hold_approver_required` when the run's hold does not admit `email`.

    The security-class stop is enforced always (SD3); every other stop goes
    through `refusals.refuse`, so with its switch off the approval goes
    ahead and the log names the refusal it would have made.
    """
    hold = run.approval_hold if hold is None else hold
    if not hold:
        return
    found = _hold_refusal(ctx, run, hold, email=email, is_owner=is_owner, confirm=confirm)
    if found is None:
        return
    message, extra = found
    error = HoldApproverRequired(
        f"run {run.id} is held {hold.get('code')}: {message}",
        detail={"hold": hold.get("code"), "approvers": hold.get("approvers"),
                "reasons": list(hold.get("reasons") or []), "matched": list(hold.get("matched") or []),
                **extra},
    )
    if hold_always_enforced(hold):
        raise error
    refusals.refuse(error)


#: The line S4's `schedule_needs_owner` log metric counts (terraform/modules/
#: monitoring/metrics.tf), exactly: the message is the filter.
NEEDS_OWNER_LINE = "schedule hold needs the owner"


def announce_hold(run: IssueRun, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> None:
    """Log the §4.9 alert line once, when a run first comes to need the owner.

    WARNING, with `tenant_id` and `approval_id` as `extra=` fields, which the
    platform's log setup writes at the top of the payload where the metric's
    label extractor reads them. The matched paths are not on the line.
    """
    if (after or {}).get("code") != HOLD_NEEDS_OWNER or (before or {}).get("code") == HOLD_NEEDS_OWNER:
        return
    log.warning(NEEDS_OWNER_LINE, extra={"tenant_id": run.tenant_id, "approval_id": PROJECTED_PREFIX + run.id})


def hold_blocks(hold: Mapping[str, Any] | None) -> bool:
    """Whether a hold stops an auto-approval or an auto-merge now."""
    if not hold:
        return False
    return hold_always_enforced(hold) or refusals.enforced(HoldApproverRequired.code)


def require_decider(ctx: Any, auth: Any) -> None:
    """The role floor under every decision (docs/design/workspace-roles.md
    §4.1, §4.8): the caller holds `member` or above in the selected
    workspace, or 403 `role_member_required` through its switch (off:
    the decision goes ahead and the log names the refusal it would have
    made).

    Asked BEFORE anything is read or moved, so a refused item stays pending,
    and read FRESH, past the instance's role cache: a narrowing made on
    another instance a moment ago already holds here. A platform admin is
    `admin` in a group workspace (Q5) and passes, and every narrower rule
    -- a gate's `approvers` list, a hold, the last editor, the `spec` rule
    -- is still checked after this, on top of it: the role is the floor,
    never the permission.
    """
    store = getattr(ctx, "roles", None)
    if store is not None:
        store.require(auth, roles.MEMBER, fresh=True)


_OWNER_ONLY = (APPROVERS_OWNER_ONLY, "owner_only")


def owner_alone(approvers: Any, hold: Mapping[str, Any] | None = None, *, owner: str | None = None) -> bool:
    """Whether only the platform owner may decide: the gate's approvers are
    `owner_only`, or the item's hold is, or the gate's approvers are a named
    list of the owner alone.

    THE NAMED LIST OF ONE (#2259, BGD's decision 2026-10-25, "Yes: treat
    [owner] like owner_only", task_f710f280d20349fc816a): a list whose
    entries, stripped and lower-cased, are exactly one address, and that
    address is `owner`. `owner` is the platform owner's address as the
    caller's own owner check found it (`check_gate_approver` passes the
    caller's email only when `is_owner`); without it no list is this. A list
    of two, the owner beside anyone else, a blank entry, an alias, a list
    naming someone else, and `members` are never this."""
    if approvers in _OWNER_ONLY or (bool(hold) and (hold or {}).get("approvers") in _OWNER_ONLY):
        return True
    return _names_owner_alone(approvers, owner)


def _names_owner_alone(approvers: Any, owner: str | None) -> bool:
    """`owner_alone`'s named list of one: every entry is the owner's address."""
    owner = (owner or "").strip().lower()
    if not owner or not isinstance(approvers, list) or not approvers:
        return False
    return {str(n).strip().lower() for n in approvers} == {owner}


def check_gate_approver(ctx: Any, tenant_id: str, approvers: Any, *, email: str, is_owner: bool,
                        schedule: Mapping[str, Any] | None, merge_tier: bool,
                        hold: Mapping[str, Any] | None = None,
                        owner_waiver: bool = False) -> dict[str, Any] | None:
    """§4.6 for an item with no hold: the gate's approvers, and SD5's second person.

    THE OWNER WAIVER (#2259, owner decision 2026-10-24 "Owner may decide,
    audited"): where only the platform owner may decide (`owner_alone` over
    the gate's approvers and the item's `hold`), the second-person rule would
    leave nobody, so the owner decides even as the schedule's last editor.
    Only a caller that writes the audit asks for it (`owner_waiver=True`, and
    `decide(waiver=...)` with what this returns): the waiver is returned, never
    applied silently. Nobody but `is_owner` reaches it; `members` and a named
    list keep the rule exactly, except a list of the owner alone (BGD
    2026-10-25), whose waiver records the list as it stands as `approvers`.
    None: no waiver was needed.
    """
    email = email.strip().lower()
    if approvers == APPROVERS_OWNER_ONLY or approvers == "owner_only":
        if not is_owner:
            raise ScheduleForbidden("approver_not_allowed", "only the platform owner approves this schedule's items")
    elif isinstance(approvers, list):
        names = {str(n).strip().lower() for n in approvers}
        tenant = _tenant(ctx, tenant_id)
        if email not in names or tenant is None or not ctx.authenticator.is_tenant_member(email, tenant):
            raise ScheduleForbidden("approver_not_allowed", "only the schedule's named approvers decide its items")
    if merge_tier and schedule is not None and not one_person(ctx, tenant_id):
        editor = schedaudit.last_editor(ctx.db, tenant_id, schedule)
        if editor == email:
            # The caller's own owner check, never a second reading of PLATFORM_OWNER.
            owner = email if is_owner else None
            alone = owner_alone(approvers, hold, owner=owner)
            if alone and is_owner and owner_waiver:
                named = not owner_alone(approvers, hold) and isinstance(approvers, list)
                return {"waived": "second_person_required", "last_editor": editor,
                        "schedule_id": schedule.get("schedule_id"),
                        "approvers": list(approvers) if named else APPROVERS_OWNER_ONLY,
                        "hold": (hold or {}).get("code") if hold else None}
            raise ScheduleForbidden(
                "second_person_required",
                "the member who last changed this schedule's gate, scope or spec cannot approve its "
                f"merge-tier items; {_who_may_decide(approvers, alone, email)}",
                may_decide=APPROVERS_OWNER_ONLY if alone else approvers,
            )
    return None


def _who_may_decide(approvers: Any, alone: bool, email: str) -> str:
    """The second-person refusal's last clause: who CAN decide, never a
    person the item's approvers would refuse -- the refused member included.
    A named list of the refused member alone leaves nobody (the rule is kept
    for a named list, #2259), and the clause says so rather than naming them."""
    if alone:
        return ("only the platform owner may decide it, and the owner may as its last editor, "
                "where the waiver is audited")
    if isinstance(approvers, list):
        others = [str(n) for n in approvers if str(n).strip().lower() != email]
        if not others:
            return ("the schedule's list of named approvers names only you, so nobody may decide it until "
                    "another member changes the schedule's approvers")
        return "another of the schedule's named approvers must: " + ", ".join(others)
    return "another member must"


def merge_tier(schedule: Mapping[str, Any] | None) -> bool:
    """Whether a schedule's work is R3 (§4.1): a merge, or a standing free-form instruction."""
    if not schedule:
        return False
    entry = scheduletypes.get(str(schedule.get("type")))
    if entry is None:
        return False
    try:
        return schedules.risk_tier(entry, schedule["gate"], schedule["params"]) == "R3"
    except Exception:  # noqa: BLE001 -- a malformed schedule is treated as the strictest
        return True


# --------------------------------------------------------------------------
# The inbox: records, materialised run approvals and projected PLANNED runs
# --------------------------------------------------------------------------


def _planned_at(run: IssueRun) -> datetime:
    for entry in reversed(run.history):
        if entry.get("to") == RunState.PLANNED.value and entry.get("from") != RunState.PLANNED.value:
            return _aware(entry.get("at")) or _aware(run.created_at)
    return _aware(run.updated_at) or _aware(run.created_at)


def project_run(run: IssueRun, schedule: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A PLANNED run as an inbox item. Its decision is the run's own approve or reject."""
    hold = run.approval_hold
    requested = _planned_at(run)
    gate_approvers = ((schedule or {}).get("gate") or {}).get("approvers") or "members"
    return {
        "approval_id": PROJECTED_PREFIX + run.id,
        "tenant_id": run.tenant_id,
        "kind": HOLD if hold else PLAN,
        "subject": {
            "run_id": run.id,
            "issue": run.issue.short,
            "schedule_id": (run.schedule or {}).get("schedule_id"),
            "firing_id": (run.schedule or {}).get("firing_id"),
        },
        "digest": run.plan_digest,
        "summary": summary_text(f"{run.issue.short}: {(run.plan or {}).get('summary') or ''}"),
        "approvers": hold.get("approvers") if hold else gate_approvers,
        "state": PENDING,
        "requested_at": requested,
        # §4.7 for a scheduled run; a run made by hand waits as it always has.
        "expires_at": requested + timedelta(hours=ttl_hours(schedule)) if schedule else None,
        "hold": hold,
        "plan_approval": run.plan_approval,
        "projected": True,
    }


def _moment(value: Any) -> datetime | None:
    """`_aware`, tolerant: a malformed stored time reads as absent."""
    try:
        return _aware(value) if isinstance(value, (datetime, str)) else None
    except ValueError:
        return None


def await_input_declaration(task: Any) -> Mapping[str, Any]:
    """The step's signed `metadata.dispatch.await_input`, or `{}`."""
    dispatch = (task.metadata or {}).get("dispatch")
    block = dispatch.get("await_input") if isinstance(dispatch, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def await_input_window(task: Any) -> Mapping[str, Any]:
    """The platform-written `metadata.await_input`: `opened_at`, `expires_at`, `answer`."""
    block = (task.metadata or {}).get("await_input")
    return block if isinstance(block, Mapping) else {}


def input_opened_at(task: Any) -> datetime | None:
    """When the wait opened: the park's `opened_at`, else the park instant."""
    return (_moment(await_input_window(task).get("opened_at"))
            or _moment(task.next_eligible_at) or _moment(task.updated_at))


def awaiting_input(store: Any, tenant_id: str, now: datetime, *, limit: int = INBOX_SCAN,
                   submitted_by: str | None = None) -> list[Any]:
    """The tenant's workflow steps open for a member's answer, newest first.

    PARKED `AWAITING_INPUT` with a declaration, and none of: a recorded
    answer, a passed `expires_at` (the expiry sweep has yet to end it, and an
    answer would be refused), a cancel requested. Read through the
    tenant-scoped store (index tasks-tenant-state-created) and checked again
    here, so another tenant's step is never returned (invariant 9). Reads only.
    """
    page = store.list_tasks(tenant_id, state=TaskState.PARKED, limit=limit,
                            submitted_by=submitted_by)
    return open_input_steps(page.items, tenant_id, now)


def open_input_steps(parked: Iterable[Any], tenant_id: str, now: datetime) -> list[Any]:
    """`awaiting_input`'s filter over PARKED tasks already read, in their
    order: the notification feed reads PARKED once per request and shares it
    between this and its hand-offs (lane LOAD-SPEED-NOTIFICATIONS). Checks
    the tenant and the state again, so a task read elsewhere is held to the
    same rule (invariant 9)."""
    open_steps = []
    for task in parked:
        if task.tenant_id != tenant_id or task.state != TaskState.PARKED \
                or task.park_reason != ParkReason.AWAITING_INPUT \
                or task.cancel_requested or not await_input_declaration(task):
            continue
        window = await_input_window(task)
        if window.get("answer"):
            continue
        expires = _moment(window.get("expires_at"))
        if expires is not None and expires <= now:
            continue
        open_steps.append(task)
    return open_steps


def project_input(task: Any) -> dict[str, Any]:
    """An open await-input step as an inbox item. Any member may answer it (Q2),
    and the answer is the step's own route, never a decision of this inbox."""
    block = await_input_declaration(task)
    window = await_input_window(task)
    form = block.get("form")
    prompt = (task.input or {}).get("prompt") if isinstance(task.input, Mapping) else None
    return {
        "approval_id": INPUT_PREFIX + task.id,
        "tenant_id": task.tenant_id,
        "kind": INPUT,
        "subject": {
            "task_id": task.id,
            "workflow_id": task.workflow_id,
            "step_id": task.step_id,
            "form": form,
            "artifact": block.get("artifact"),
            "max_files": block.get("max_files"),
        },
        "digest": None,
        "summary": summary_text(f"{task.step_id or task.id} in {task.workflow_id}: {prompt or ''}"),
        "approvers": "members",
        "state": PENDING,
        "requested_at": input_opened_at(task),
        "expires_at": _moment(window.get("expires_at")),
        "submitted_by": task.submitted_by,
        "projected": True,
    }


def sync_run_approvals(db: Any, now: datetime, tenant_id: str) -> int:
    """A `run` record for each of the tenant's `awaiting_approval` firings that has none.

    The tick (S2) holds a firing whose gate says `run: approve` as a
    document; this makes the record the inbox decides, with the firing's
    `params_digest`, and names it on the firing (`approval_id`, §1.2).
    """
    query = (
        db.collection(schedulefire.FIRINGS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "==", schedulefire.AWAITING_APPROVAL))
        .limit(INBOX_SCAN)
    )
    made = 0
    for snap in query.stream():
        firing = snap.to_dict() or {}
        if firing.get("tenant_id") != tenant_id or firing.get("approval_id"):
            continue
        schedule = read_schedule(db, tenant_id, firing.get("schedule_id"))
        if schedule is None:
            continue
        doc = request(
            db, now, tenant_id=tenant_id, kind=RUN, key=str(firing["firing_id"]),
            subject={"schedule_id": firing["schedule_id"], "firing_id": firing["firing_id"]},
            digest=firing.get("params_digest"),
            summary=f"{schedule.get('name')}: {schedule.get('type')} waits to run",
            approvers=(schedule.get("gate") or {}).get("approvers") or "members",
            ttl=ttl_hours(schedule),
            extra={"merge_tier": merge_tier(schedule)},
        )
        db.collection(schedulefire.FIRINGS).document(str(firing["firing_id"])).update(
            {"approval_id": doc["approval_id"]}
        )
        made += 1
    return made


def _live_merge(db: Any, doc: Mapping[str, Any]) -> bool:
    """Whether a pending merge record still has a run waiting at its head."""
    subject = doc.get("subject") or {}
    snap = db.collection(RUNS_COLLECTION).document(str(subject.get("run_id"))).get()
    return _merge_run_waits(doc, snap.to_dict() if snap.exists else None)


def live_merges(db: Any, docs: Iterable[Mapping[str, Any]]) -> set[str]:
    """The approval ids among pending merge `docs` that `_live_merge` keeps,
    their runs read in one batched `get_all` rather than one read per record
    (the notification feed, lane LOAD-SPEED-NOTIFICATIONS). Reads only."""
    merges = [d for d in docs if d.get("kind") == MERGE]
    if not merges:
        return set()
    runs = db.collection(RUNS_COLLECTION)
    found: dict[str, dict[str, Any]] = {}
    for snap in db.get_all([runs.document(str((d.get("subject") or {}).get("run_id")))
                            for d in merges]):
        if snap.exists:
            found[snap.id] = snap.to_dict() or {}
    return {str(d.get("approval_id")) for d in merges
            if _merge_run_waits(d, found.get(str((d.get("subject") or {}).get("run_id"))))}


def _merge_run_waits(doc: Mapping[str, Any], data: Mapping[str, Any] | None) -> bool:
    if not data or data.get("tenant_id") != doc.get("tenant_id"):
        return False
    head = ((data.get("pull_request") or {}).get("head_sha"))
    return data.get("state") == RunState.CHECKING.value and head == (doc.get("digest") or {}).get("head_sha")


def inbox(ctx: Any, tenant_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
    """§4.5's inbox: pending records beside projected PLANNED runs and open
    await-input steps, oldest first."""
    now = ctx.now()
    sync_run_approvals(ctx.db, now, tenant_id)
    items: list[dict[str, Any]] = []
    for doc in pending(ctx.db, tenant_id):
        if doc.get("kind") == MERGE and not _live_merge(ctx.db, doc):
            # Its run merged, failed or moved head: nothing waits on it now.
            _close(ctx.db, now, doc, SUPERSEDED)
            continue
        items.append(doc)
    runs, _ = IssueRuns(ctx.db, now=ctx.now).planned(tenant_id, limit=INBOX_SCAN)
    schedules_seen: dict[str, dict[str, Any] | None] = {}
    for run in runs:
        sid = (run.schedule or {}).get("schedule_id")
        if sid and sid not in schedules_seen:
            schedules_seen[sid] = read_schedule(ctx.db, tenant_id, sid)
        items.append(project_run(run, schedules_seen.get(sid) if sid else None))
    items.extend(project_input(task) for task in awaiting_input(ctx.store, tenant_id, now))
    if kind is not None:
        items = [i for i in items if i.get("kind") == kind]
    items.sort(key=lambda i: (_aware(i.get("requested_at")) or _EPOCH, str(i.get("approval_id"))))
    return items


# --------------------------------------------------------------------------
# The history: what was decided or expired lately (#2417 A4)
# --------------------------------------------------------------------------

#: How far back the history reaches. BGD 2026-10-27 (AUTO-A4A5): "the
#: approvals API returns recent decisions", the last 7 days of them.
HISTORY_DAYS = 7
#: Decided and expired rows one history read serves at most, and what each of
#: its three sources (records, runs, await-input steps) reads at most.
HISTORY_LIMIT = 200
#: An await-input step a member answered. It is the step's decision, but
#: neither an approval nor a rejection, so it keeps its own word.
ANSWERED = "answered"
#: The history's states. `superseded` is not one: nothing was decided, the
#: subject moved on (`_live_merge`).
DECIDED_STATES = (APPROVED, REJECTED, ANSWERED)
HISTORY_STATES = DECIDED_STATES + (EXPIRED,)


def _recent(moment: Any, cutoff: datetime) -> datetime | None:
    at = _moment(moment)
    return at if at is not None and at >= cutoff else None


def decided_records(db: Any, tenant_id: str, now: datetime, *,
                    limit: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """The tenant's records decided or expired in the last HISTORY_DAYS, and
    whether the read was full (so rows may be missing).

    ONE QUERY, ON AN INDEX THAT EXISTS: approvals-tenant-state-requested
    (tenant_id, state, requested_at ASC), with `state in` the closed states
    and `requested_at` no older than the window plus the longest TTL -- a
    record is decided or expired at most APPROVAL_TTL_MAX_HOURS after it was
    asked for. That index runs ascending, so a full read is the OLDEST
    `limit` of the window and the caller is told (`history_truncated`). The
    exact read -- `decided_at >= cutoff order by decided_at desc` -- needs a
    new index, (tenant_id, state, decided_at DESC), proposed beside the PR
    that added this rather than made here. Filtered again in the application:
    the tenant, the state, and `decided_at` inside the window.
    """
    limit = HISTORY_LIMIT if limit is None else limit
    cutoff = now - timedelta(days=HISTORY_DAYS)
    since = cutoff - timedelta(hours=schedules.APPROVAL_TTL_MAX_HOURS)
    query = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "in", [APPROVED, REJECTED, EXPIRED]))
        .where(filter=FieldFilter("requested_at", ">=", since))
        .order_by("requested_at")
        .limit(limit)
    )
    read = [snap.to_dict() or {} for snap in query.stream()]
    rows = [r for r in read if r.get("tenant_id") == tenant_id and r.get("state") in HISTORY_STATES
            and _recent(r.get("decided_at"), cutoff)]
    return rows, len(read) >= limit


def project_decided_run(run: IssueRun, schedule: Mapping[str, Any] | None,
                        cutoff: datetime) -> dict[str, Any] | None:
    """A run whose plan a person approved or rejected, or expiry rejected,
    since `cutoff`, as a history item; None otherwise. The run is the source
    of truth, as it is for the pending item: `approved_by`/`approved_at`, or
    its PLANNED -> REJECTED history entry with `rejected_by` and
    `rejection_reason`. A plan the tick approved (`plan_approval: auto`) was
    never waiting on anyone and is not served."""
    item = project_run(run, schedule)
    if run.approved_by and run.approved_by != AUTO_APPROVER:
        at = _recent(run.approved_at, cutoff)
        if at is None:
            return None
        decided = {"state": APPROVED, "decided_by": run.approved_by, "decided_at": at, "reason": None}
    elif run.state == RunState.REJECTED:
        entry = next((e for e in reversed(run.history) if e.get("to") == RunState.REJECTED.value
                      and e.get("from") == RunState.PLANNED.value), None)
        at = _recent((entry or {}).get("at"), cutoff)
        if at is None:
            return None
        expired = run.rejected_by == EXPIRY_ACTOR
        decided = {"state": EXPIRED if expired else REJECTED,
                   "decided_by": run.rejected_by or (entry or {}).get("by"), "decided_at": at,
                   "reason": None if expired else run.rejection_reason}
    else:
        return None
    return {**item, **decided}


def project_closed_input(task: Any, cutoff: datetime) -> dict[str, Any] | None:
    """An await-input step answered or expired since `cutoff`, as a history
    item; None otherwise. The step's `metadata.await_input` is the source of
    truth: `answer` (`answered_by`, `answered_at`) or `expired.at`."""
    if not await_input_declaration(task):
        return None
    window = await_input_window(task)
    answer = window.get("answer") if isinstance(window.get("answer"), Mapping) else None
    expired = window.get("expired") if isinstance(window.get("expired"), Mapping) else None
    if answer is not None:
        at = _recent(answer.get("answered_at"), cutoff)
        decided = {"state": ANSWERED, "decided_by": answer.get("answered_by"), "decided_at": at}
    elif expired is not None:
        at = _recent(expired.get("at"), cutoff)
        decided = {"state": EXPIRED, "decided_by": EXPIRY_ACTOR, "decided_at": at}
    else:
        return None
    if at is None:
        return None
    return {**project_input(task), **decided, "reason": None}


def history(ctx: Any, tenant_id: str, *, kind: str | None = None, states: tuple[str, ...] = HISTORY_STATES,
            limit: int | None = None) -> tuple[list[tuple[dict[str, Any], IssueRun | None]], bool]:
    """The tenant's items decided or expired in the last HISTORY_DAYS, NEWEST
    FIRST by `decided_at`, at most `limit`, each beside its run when it is a
    projected one; and whether a source's read was full, so rows may be
    missing (`history_truncated`).

    Three sources, each read once and bounded, each its own source of truth:
    the records (`decided_records`); the tenant's newest `limit` issue runs
    (`IssueRuns.list`, index issue-runs-tenant-created) for projected plans;
    the tenant's newest `limit` tasks (`list_tasks`, tasks-tenant-created)
    for await-input steps. Every one is tenant-scoped in the query and again
    here (invariant 9). Reads only: nothing is synced, closed or expired.
    """
    limit = HISTORY_LIMIT if limit is None else limit
    now = ctx.now()
    cutoff = now - timedelta(days=HISTORY_DAYS)
    records, truncated = decided_records(ctx.db, tenant_id, now, limit=limit)
    found: list[tuple[dict[str, Any], IssueRun | None]] = [(doc, None) for doc in records]
    runs, more_runs = IssueRuns(ctx.db, now=ctx.now).list(tenant_id, limit=limit)
    schedules_seen: dict[str, dict[str, Any] | None] = {}
    for run in runs:
        if run.tenant_id != tenant_id:
            continue
        sid = (run.schedule or {}).get("schedule_id")
        if sid and sid not in schedules_seen:
            schedules_seen[sid] = read_schedule(ctx.db, tenant_id, sid)
        item = project_decided_run(run, schedules_seen.get(sid) if sid else None, cutoff)
        if item is not None:
            found.append((item, run))
    page = ctx.store.list_tasks(tenant_id, limit=limit, submitted_by=None)
    for task in page.items:
        item = project_closed_input(task, cutoff) if task.tenant_id == tenant_id else None
        if item is not None:
            found.append((item, None))
    found = [(i, r) for i, r in found if i.get("state") in states and (kind is None or i.get("kind") == kind)]
    found.sort(key=lambda pair: (_aware(pair[0].get("decided_at")) or _EPOCH, str(pair[0].get("approval_id"))),
               reverse=True)
    # A full read of runs or tasks says rows may be missing only while what it
    # reached is still inside the window: past it, what was not read was made
    # earlier than anything this history can hold, but for a plan made by
    # hand and decided weeks later, which is not served.
    since = cutoff - timedelta(hours=schedules.APPROVAL_TTL_MAX_HOURS)
    truncated = (truncated or len(found) > limit
                 or (more_runs is not None and bool(runs) and _aware(runs[-1].created_at) >= since)
                 or (page.next_page_token is not None and bool(page.items)
                     and (_moment(page.items[-1].created_at) or _EPOCH) >= since))
    return found[:limit], truncated


# --------------------------------------------------------------------------
# Moving the subject: a run approval's firing, a merge's run
# --------------------------------------------------------------------------


def firing_move(db: Any, now: datetime, *, approve: bool, by: str) -> Move:
    """A `run` decision's move of its firing, inside the decision's transaction.

    Approved: the firing records `run_approval` (who, when, the digest) and
    stays `awaiting_approval` for the tick to create its work under the
    gate it was claimed with. Rejected: it ends `rejected` (§1.4) and the
    next slot fires normally.
    """

    def move(txn: Any, doc: Mapping[str, Any]) -> dict[str, Any]:
        fid = str((doc.get("subject") or {}).get("firing_id"))
        db_ref = db.collection(schedulefire.FIRINGS).document(fid)
        firing = _snapshot(txn.get(db_ref)).to_dict() or {}
        if firing.get("tenant_id") != doc.get("tenant_id") or firing.get("state") != schedulefire.AWAITING_APPROVAL:
            raise ScheduleConflict("subject_changed", f"firing {fid} is no longer awaiting approval")
        if approve and firing.get("params_digest") != doc.get("digest"):
            raise ScheduleConflict("approval_changed", "the firing's parameters changed since it was shown")
        if approve:
            txn.update(db_ref, {"run_approval": {"by": by, "at": now, "approval_id": doc["approval_id"],
                                                 "digest": doc.get("digest")}})
            return {"firing_id": fid}
        history = list(firing.get("history") or []) + [{"state": FIRING_REJECTED, "at": now, "by": by}]
        txn.update(db_ref, {"state": FIRING_REJECTED, "outcome": FIRING_REJECTED, "ended_at": now,
                            "history": history})
        return {"firing_id": fid}

    return move


def merge_move(db: Any, now: datetime, *, by: str, resign: Resign) -> Move:
    """An approved `merge`: the run records `merge_approved` at the head shown.

    Inside the decision's transaction, the run must still be CHECKING at that
    head (else 409 `merge_changed`) and must hold no approval at it already
    (else 409 `already_decided`). Its next CI read is made due at once, so the
    next visit submits the merge-only continuation through `issueci._merge`,
    the one place a merge is submitted, with every check it makes.

    `merge_approved` is a signed field (RUN-SIG-2, #1569): `resign`
    (`routes.runs.owner_resigner`) verifies the run as this transaction read
    it -- so a run whose hold a worker dropped is 403 `run_owner_unverified`
    here and nothing is decided -- and signs it with the approval, in the
    same write. A worker that writes `merge_approved` itself writes no
    signature over it, and the merge refuses the run (`issueci.merge_gate`).
    """

    def move(txn: Any, doc: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str((doc.get("subject") or {}).get("run_id"))
        ref = db.collection(RUNS_COLLECTION).document(run_id)
        data = _snapshot(txn.get(ref)).to_dict() or {}
        head = (doc.get("digest") or {}).get("head_sha")
        pull = dict(data.get("pull_request") or {})
        if data.get("tenant_id") != doc.get("tenant_id"):
            raise _not_found(str(doc.get("approval_id")))
        if data.get("state") != RunState.CHECKING.value or pull.get("head_sha") != head:
            raise ScheduleConflict("merge_changed", f"run {run_id}'s pull request is no longer at {str(head)[:12]}",
                                   head_sha=pull.get("head_sha"))
        if (data.get("merge_approved") or {}).get("head_sha") == head:
            raise ScheduleConflict("already_decided", f"the merge of run {run_id} at {str(head)[:12]} is approved")
        pull["checked_at"] = None
        changes = {
            "merge_approved": {"head_sha": head, "by": by, "at": now, "approval_id": doc["approval_id"]},
            "pull_request": pull,
        }
        changes[OWNER_SIGNATURE] = resign(IssueRun.from_firestore(data),
                                          IssueRun.from_firestore({**data, **changes}))
        txn.update(ref, changes)
        return {"run_id": run_id, "head_sha": head}

    return move


def request_merge(db: Any, now: datetime, run: IssueRun, *, head_sha: str, verdict: str | None) -> dict[str, Any]:
    """The `merge` record a held or merge-gated run's green pull request waits on.

    Idempotent per run and head. A pending request at another head is
    superseded: a push after the request makes it stale (§4.5).
    """
    schedule = read_schedule(db, run.tenant_id, (run.schedule or {}).get("schedule_id"))
    hold = run.approval_hold
    approvers = hold.get("approvers") if hold else ((schedule or {}).get("gate") or {}).get("approvers") or "members"
    for doc in pending(db, run.tenant_id):
        if (doc.get("kind") == MERGE and (doc.get("subject") or {}).get("run_id") == run.id
                and (doc.get("digest") or {}).get("head_sha") != head_sha):
            _close(db, now, doc, SUPERSEDED)
    number = (run.pull_request or {}).get("number")
    return request(
        db, now, tenant_id=run.tenant_id, kind=MERGE, key=f"{run.id}:{head_sha}",
        subject={"run_id": run.id, "pr": number, "issue": run.issue.short,
                 "schedule_id": (run.schedule or {}).get("schedule_id"),
                 "firing_id": (run.schedule or {}).get("firing_id")},
        digest=merge_digest(head_sha, verdict),
        summary=f"{run.issue.short}: pull request #{number} is green at {head_sha[:12]} and waits to merge",
        approvers=approvers, ttl=ttl_hours(schedule),
        extra={"hold": dict(hold) if hold else None, "merge_tier": True},
    )


# --------------------------------------------------------------------------
# Expiry (§4.7)
# --------------------------------------------------------------------------


def expire_due(ctx: Any, tenant_id: str) -> dict[str, int]:
    """Expire the tenant's pending items whose time is up, and apply §4.7.

      * `run`: the firing ends `expired`; the next slot fires normally.
      * `plan` (a SCHEDULED PLANNED run): REJECTED with reason "expired",
        the existing state, and the issue's cooldown starts. A run made by
        hand is not a schedule's and waits as it always has.
      * `merge`: the pull request stays open, green and unmerged, and the
        run, CHECKING while it waited, ends DONE at its green head. Nothing
        merges without a new approval.

    Releases nothing: a pending item held nothing (invariant 1).
    """
    db = ctx.db
    now = ctx.now()
    counts = {"records": 0, "plans": 0}
    runs = IssueRuns(db, now=ctx.now)
    for doc in pending(db, tenant_id):
        expires = _aware(doc.get("expires_at"))
        if expires is None or expires > now:
            continue
        move = None
        if doc.get("kind") == RUN:
            move = _expire_firing(db, now)
        if not _close(db, now, doc, EXPIRED, move):
            continue
        counts["records"] += 1
        if doc.get("kind") == MERGE:
            run_id = str((doc.get("subject") or {}).get("run_id"))
            head = (doc.get("digest") or {}).get("head_sha")
            try:
                runs.transition(tenant_id, run_id, RunState.DONE, by=EXPIRY_ACTOR,
                                from_states={RunState.CHECKING},
                                patch=lambda current: {"green_sha": head})
            except Exception as exc:  # noqa: BLE001 -- the run moved on its own
                log.info("approval %s expired; run %s not moved (%s)", doc.get("approval_id"), run_id,
                         type(exc).__name__)
    planned, _ = runs.planned(tenant_id, limit=INBOX_SCAN)
    for run in planned:
        if not run.schedule:
            continue
        schedule = read_schedule(db, tenant_id, run.schedule.get("schedule_id"))
        if _planned_at(run) + timedelta(hours=ttl_hours(schedule)) > now:
            continue
        try:
            runs.transition(tenant_id, run.id, RunState.REJECTED, by=EXPIRY_ACTOR,
                            from_states={RunState.PLANNED},
                            patch={"rejected_by": EXPIRY_ACTOR, "rejection_reason": "expired"})
        except Exception as exc:  # noqa: BLE001 -- a person decided it first
            log.info("run %s not expired (%s)", run.id, type(exc).__name__)
            continue
        counts["plans"] += 1
        if schedule is not None:
            batch = db.batch()
            schedaudit.append(batch, db, schedule_id=schedule["schedule_id"], tenant_id=tenant_id,
                              action=schedaudit.EXPIRED, by=EXPIRY_ACTOR, at=now,
                              detail={"approval_id": PROJECTED_PREFIX + run.id, "kind": PLAN,
                                      "subject": {"run_id": run.id}})
            batch.commit()
    return counts


def _expire_firing(db: Any, now: datetime) -> Move:
    def move(txn: Any, doc: Mapping[str, Any]) -> None:
        fid = str((doc.get("subject") or {}).get("firing_id"))
        ref = db.collection(schedulefire.FIRINGS).document(fid)
        firing = _snapshot(txn.get(ref)).to_dict() or {}
        if firing.get("tenant_id") != doc.get("tenant_id") or firing.get("state") != schedulefire.AWAITING_APPROVAL:
            return None
        history = list(firing.get("history") or []) + [{"state": FIRING_EXPIRED, "at": now, "by": EXPIRY_ACTOR}]
        txn.update(ref, {"state": FIRING_EXPIRED, "outcome": FIRING_EXPIRED, "ended_at": now, "history": history})
        return None

    return move


def merge_digest_text(digest: Mapping[str, Any]) -> str:
    """The short form of a merge digest, as a person or a comment types it."""
    return f"{digest.get('head_sha')}:{digest.get('verdict')}"


# --------------------------------------------------------------------------
# What the decision page reads (AUTO-API-APPROVALS, part of #2417)
#
# Every field here is READ, never stored: derived on each read from what the
# record, its run and its registration already hold, and ABSENT (None) when
# that cannot be read, never a guess.
# --------------------------------------------------------------------------


class RejectApproverRequired(Forbidden):
    """403: the caller may not approve this item, so may not reject it (BGD
    2026-10-26, #2417 A5f). Behind `refusals.SWITCHES`, on by default."""

    code = "reject_approver_required"


#: The path rules each §4.4 reason matches a file by (`issueruns.
#: hard_stop_hold`). `protected_path` reads the registration's own list and
#: `security` matches the issue, not a file; both are handled apart.
REASON_PATTERNS: dict[str, tuple[str, ...]] = {
    STOP_IAM: IAM_PATTERNS,
    STOP_CONTRACT: CONTRACT_PATTERNS,
    STOP_WORKFLOWS: WORKFLOW_HOLD_PATTERNS,
    STOP_SECRETS: SECRETS_PATTERNS,
    STOP_TENANCY: TENANCY_PATTERNS,
    STOP_SHARED: SHARED_PATTERNS,
    STOP_COST: COST_PATTERNS,
}
#: The `matched` entries a security-class stop writes (`is_security_issue`).
SECURITY_MATCH_PREFIXES = ("label:", "severity:")


def reason_files(hold: Mapping[str, Any] | None,
                 hard_stop_paths: Any = None) -> dict[str, list[str]] | None:
    """`{reason: [matched entries]}`: which of a hold's `matched` each of its
    `reasons` came from (#2417 A5 card 2).

    A hold stores the two lists apart, so this re-applies each reason's own
    path rule to the matched entries. `protected_path` uses
    `hard_stop_paths` (the run's registration, `repositories.
    hard_stop_paths_of`) and claims only what no other reason did, as
    `hard_stop_hold` does; None means the registration was not read and the
    reason is left out. A reason no entry matches by path -- an IAM stop
    found in a `.tf` file's patch, a category found by its lines or by a
    plan's prompt term -- is left out rather than paired with a guess; it is
    still in `hold.reasons`, and its entry in `hold.matched`. None when no
    reason pairs at all."""
    if not hold:
        return None
    matched = [str(m) for m in hold.get("matched") or []]
    reasons = [str(r) for r in hold.get("reasons") or []]
    out: dict[str, list[str]] = {}
    for reason in reasons:
        if reason == STOP_SECURITY:
            files = [m for m in matched if m.startswith(SECURITY_MATCH_PREFIXES)]
        else:
            patterns = REASON_PATTERNS.get(reason, ())
            files = [m for m in matched if any(path_matches(m, p) for p in patterns)]
        if files:
            out[reason] = files
    if STOP_PROTECTED_PATH in reasons and hard_stop_paths is not None:
        claimed = {m for files in out.values() for m in files}
        protected = [p for p in hard_stop_paths if p != WORKFLOWS_PATTERN]
        files = [m for m in matched if m not in claimed and any(path_matches(m, p) for p in protected)]
        if files:
            out[STOP_PROTECTED_PATH] = files
    return out or None


def with_reason_files(hold: Mapping[str, Any] | None, hard_stop_paths: Any = None) -> dict[str, Any] | None:
    """A served hold with its `reason_files`, which is absent when none pair."""
    if not hold:
        return None
    out = dict(hold)
    pairs = reason_files(hold, hard_stop_paths)
    if pairs is not None:
        out["reason_files"] = pairs
    return out


#: The buckets `merge.checks` serves (`forgechecks.COUNT_BUCKETS` less
#: `skipped`, which the A5 card does not draw).
CHECK_BUCKETS = ("passed", "failed", "pending")


def merge_checks(doc: Mapping[str, Any], pull_request: Mapping[str, Any] | None) -> dict[str, int] | None:
    """`{passed, failed, pending}` at the head a merge approval names (#2417 A5
    card 1), from the run's last CI reading (`issueci.check_summary`'s
    `check_counts`, written beside the `head_sha` it was read at).

    None when the run has no reading, a bucket is missing or not a count, or
    the reading is of another head: after a push the counts are the new
    head's, not the ones this approval was asked on."""
    head = (doc.get("digest") or {}).get("head_sha") if isinstance(doc.get("digest"), Mapping) else None
    pull = pull_request or {}
    counts = pull.get("check_counts")
    if not head or pull.get("head_sha") != head or not isinstance(counts, Mapping):
        return None
    out: dict[str, int] = {}
    for bucket in CHECK_BUCKETS:
        value = counts.get(bucket)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        out[bucket] = value
    return out


def superseded_by(db: Any, doc: Mapping[str, Any], head_now: str | None) -> dict[str, Any] | None:
    """`{approval_id, head_sha, at}`: the request to decide instead of a
    superseded merge approval (#2417 A5e).

    A merge request's id is derived from its run and head (`request_merge`,
    `approval_id`), so the run's head NOW names the request made for it: read
    that record, in this tenant, and serve it if it is another one. None when
    the item is not a superseded merge, the head is unknown or unchanged, or
    no request was made at the new head (the run merged, failed, or has not
    gone green there yet)."""
    if doc.get("kind") != MERGE or doc.get("state") != SUPERSEDED or not head_now:
        return None
    run_id = (doc.get("subject") or {}).get("run_id")
    if not run_id:
        return None
    newer_id = approval_id(MERGE, f"{run_id}:{head_now}")
    if newer_id == doc.get("approval_id"):
        return None
    snap = db.collection(COLLECTION).document(newer_id).get()
    newer = snap.to_dict() if snap.exists else None
    if not newer or newer.get("tenant_id") != doc.get("tenant_id"):
        return None
    return {"approval_id": newer_id, "head_sha": (newer.get("digest") or {}).get("head_sha"),
            "at": _iso(newer.get("requested_at"))}


#: `deciders` entries that are rules, not addresses: who may decide when the
#: item names nobody in particular (§4.6).
DECIDERS_MEMBERS = "members"
DECIDERS_PLATFORM_ADMINS = "platform_admins"
#: Beside `second_member`: a workspace admin decides a `second_member` hold
#: alone (BGD 2026-10-26).
DECIDERS_WORKSPACE_ADMINS = "workspace_admins"


def deciders(ctx: Any, tenant_id: str, *, kind: str, approvers: Any, hold: Mapping[str, Any] | None,
             schedule: Mapping[str, Any] | None, merge_tier: bool, platform_scope: bool,
             run: IssueRun | None = None) -> list[str]:
    """Who may approve an item, and so (BGD 2026-10-26) reject it: the
    narrowest rule that applies to it, as a list.

    Addresses where the item names people -- a named list, less the
    schedule's last editor of a merge-tier item in a group tenant (SD5) and,
    under a `second_member` hold, those the hold does not admit (a workspace
    admin it admits: BGD 2026-10-26) -- else one rule word from `approvers`'
    vocabulary: `owner_only` (the gate, or a `NEEDS_OWNER` hold),
    `platform_admins` (a `custom-prompt` spec, or a platform-scope
    schedule's item), `second_member` with `workspace_admins` beside it,
    `members`. Never the reader's own judgement: the decision routes still
    decide."""
    if kind == SPEC:
        return [DECIDERS_PLATFORM_ADMINS]
    if owner_alone(approvers, hold):
        return [APPROVERS_OWNER_ONLY]
    if isinstance(approvers, list):
        names = [str(n).strip().lower() for n in approvers if str(n).strip()]
        excluded: set[str] = set()
        if merge_tier and schedule is not None and not one_person(ctx, tenant_id):
            editor = schedaudit.last_editor(ctx.db, tenant_id, schedule)
            if editor:
                excluded.add(editor)
        if hold and hold.get("approvers") == APPROVERS_SECOND_MEMBER and run is not None \
                and not one_person(ctx, tenant_id):
            excluded |= {p for p in hold_excluded(ctx, run) if not workspace_admin(ctx, tenant_id, p)}
        return list(dict.fromkeys(n for n in names if n not in excluded))
    if platform_scope:
        return [DECIDERS_PLATFORM_ADMINS]
    if hold and hold.get("approvers") == APPROVERS_SECOND_MEMBER:
        return [APPROVERS_SECOND_MEMBER, DECIDERS_WORKSPACE_ADMINS]
    return [DECIDERS_MEMBERS]


def hold_refusal(ctx: Any, run: IssueRun, *, email: str, is_owner: bool) -> HoldApproverRequired | None:
    """`check_hold`'s refusal, READ: the error an approve would meet, or None.

    Nothing is logged, so a read of the inbox writes no report-only line. A
    one-person tenant's own hold, which an approve passes after the typed
    confirmation, is not a refusal, and neither is a stop whose switch is
    off -- the approve would go through."""
    hold = run.approval_hold
    if not hold:
        return None
    found = _hold_refusal(ctx, run, hold, email=email, is_owner=is_owner, confirm=None)
    if found is None or "confirm" in found[1]:
        return None
    if not (hold_always_enforced(hold) or refusals.enforced(HoldApproverRequired.code)):
        return None
    message, extra = found
    return HoldApproverRequired(
        f"run {run.id} is held {hold.get('code')}: {message}",
        detail={"hold": hold.get("code"), "approvers": hold.get("approvers"),
                "reasons": list(hold.get("reasons") or []), "matched": list(hold.get("matched") or []),
                **extra},
    )
