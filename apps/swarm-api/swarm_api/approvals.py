"""`approvals/{approval_id}`: one record, one inbox (docs/schedules.md §4.5-§4.7, lane S5).

KINDS. `run` (a firing waiting to create work), `plan` (an issue run's plan),
`merge` (a green, reviewed pull request waiting), `proposal` (an observer or
flake proposal to file as an issue), `spec` (a `custom-prompt` digest) and
`hold` (any §4.4 hold, `NEEDS_OWNER` or `NEEDS_SECOND_MEMBER`).

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

WHO MAY DECIDE (§4.6). Any member of the item's tenant by default (SD5) --
`tenant_scope` puts only the caller's own tenant's items in reach, and a
platform admin decides another tenant's item by no route here. `owner_only`:
PLATFORM_OWNER, as a member of the tenant. `second_member`: any member but
the run's creator, its last plan editor and its schedule's last gate editor;
a one-person `u-*` tenant types a confirmation naming the matched paths
(SD11). A named list: each a member now, asked of the directory. A
merge-tier (R3) item: never the schedule's last gate, scope or spec editor
when the tenant is a group (SD5).

A PENDING APPROVAL HOLDS NOTHING (invariant 1), before and after expiry. It
is a document: no task, lease or slot exists for it, and a plan's planner
task has already ended. Expiry (§4.7) tidies the inbox; it releases nothing.

TENANT-SCOPED (invariant 9). Every read compares the stored `tenant_id` with
the caller's and answers a mismatch with the same 404 as a missing id.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_common.admission import _snapshot

from . import refusals, schedaudit, schedulefire, schedules, scheduletypes
from .errors import NotFound
from .issueruns import (
    APPROVERS_OWNER_ONLY,
    APPROVERS_SECOND_MEMBER,
    AUTO_APPROVER,
    HOLD_NEEDS_OWNER,
    RUNS_COLLECTION,
    HoldApproverRequired,
    IssueRun,
    IssueRuns,
    RunState,
    hold_always_enforced,
    hold_confirmation,
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
KINDS = (RUN, PLAN, MERGE, PROPOSAL, SPEC, HOLD)

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
SUPERSEDED = "superseded"
STATES = (PENDING, APPROVED, REJECTED, EXPIRED, SUPERSEDED)

#: A projected issue run's id in the inbox (§7.1).
PROJECTED_PREFIX = "run:"
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
    if kind not in KINDS:
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
) -> dict[str, Any]:
    """§4.5's transaction: state and digest checked, subject moved, decision and audit written.

    `move(txn, doc)` moves the subject inside the same transaction. It must
    do its reads before its writes, as every Firestore transaction must, and
    may raise to refuse; nothing is then written.
    """
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
        if email.strip().lower() in hold_excluded(ctx, run):
            return ("a second member must approve this held run: not its creator, its last "
                    "plan editor or its schedule's last gate editor", {})
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


def check_gate_approver(ctx: Any, tenant_id: str, approvers: Any, *, email: str, is_owner: bool,
                        schedule: Mapping[str, Any] | None, merge_tier: bool) -> None:
    """§4.6 for an item with no hold: the gate's approvers, and SD5's second person."""
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
        if schedaudit.last_editor(ctx.db, tenant_id, schedule) == email:
            raise ScheduleForbidden(
                "second_person_required",
                "the member who last changed this schedule's gate, scope or spec cannot approve its "
                "merge-tier items; another member must",
            )


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
    data = snap.to_dict() if snap.exists else None
    if not data or data.get("tenant_id") != doc.get("tenant_id"):
        return False
    head = ((data.get("pull_request") or {}).get("head_sha"))
    return data.get("state") == RunState.CHECKING.value and head == (doc.get("digest") or {}).get("head_sha")


def inbox(ctx: Any, tenant_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
    """§4.5's inbox: pending records beside projected PLANNED runs, oldest first."""
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
    if kind is not None:
        items = [i for i in items if i.get("kind") == kind]
    items.sort(key=lambda i: (_aware(i.get("requested_at")) or _EPOCH, str(i.get("approval_id"))))
    return items


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


def merge_move(db: Any, now: datetime, *, by: str) -> Move:
    """An approved `merge`: the run records `merge_approved` at the head shown.

    Inside the decision's transaction, the run must still be CHECKING at that
    head (else 409 `merge_changed`) and must hold no approval at it already
    (else 409 `already_decided`). Its next CI read is made due at once, so the
    next visit submits the merge-only continuation through `issueci._merge`,
    the one place a merge is submitted, with every check it makes.
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
        txn.update(ref, {
            "merge_approved": {"head_sha": head, "by": by, "at": now, "approval_id": doc["approval_id"]},
            "pull_request": pull,
        })
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
