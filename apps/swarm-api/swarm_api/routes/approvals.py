"""Approval routes, and the SD3 merge switch (docs/schedules.md §4.2, §4.5-§4.7, §7.1, lane S5).

    GET  /v1/approvals[?kind=]                 the inbox: records plus projected PLANNED runs
    GET  /v1/approvals/{id}                    one item, with what its kind needs to decide it
    POST /v1/approvals/{id}:approve            {digest, confirm?}
    POST /v1/approvals/{id}:reject             {reason}
    POST /v1/schedules/{id}:merge-mode         {mode: "approve" | "auto", revision, confirm?}

A projected issue run's id is `run:<run_id>`; its approve is the run's own
`routes.runs._approve`, the one function every approval goes through, and its
reject is the run's own transition to REJECTED. So the inbox, Work › Runs and
`sc plan approve` are one approval with one source of truth.

TENANT-SCOPED (§5.1, invariant 9). The tenant is `tenant_scope`'s, never the
body's. Another tenant's item answers the 404 a missing one does. An
`owner_only` item exists only in a tenant the owner is a member of.

THE ROLE IS THE FLOOR (docs/design/workspace-roles.md §4.1, §4.8; lane
ROLES-GATES-DECIDE). Every decision here -- approve, reject, the merge
switch, a projected plan's approve and reject -- first asks
`approvals.require_decider`: `member` or above in the selected workspace,
read fresh, else 403 `role_member_required` through its switch, asked before
the item is read so a refused one stays pending. A `viewer` reads the inbox
and decides nothing. A platform admin is `admin` in every GROUP workspace,
member or not (Q5, which supersedes this file's earlier "a platform admin
decides nothing of another tenant's"): they reach a group workspace they are
not in only through `platform_admin_reach`, each decision audited before it
runs (`deps.current_auth`), and never anyone's personal workspace. The rules
below are unchanged and still apply on top of the role, to them as to anyone:
a gate's `approvers` list, a hold, the last-editor rule and the `spec` rule.

A PLATFORM-SCOPE SCHEDULE'S ITEMS ARE A PLATFORM ADMIN'S (BGD, 2026-10-24,
"Platform admins only"; PLATFORM-SCHED-APPROVALS, part of #2157). Approving
a gated run has the effect of `:run`, which `schedule_routes._platform_only`
refuses to anyone else, so `:approve` and `:reject` of an item whose STORED
schedule reference -- a record's `subject.schedule_id`, a projected run's
`schedule.schedule_id`, never the request -- names a schedule whose stored
`scope.mode` is `platform` are the same 403 `platform_admin_required`, with
the same log line, before anything moves. Enforced at once, as PR 2272's.
Items of a workspace-scope schedule, or of none, keep the rules above
exactly; and a platform admin still meets every one of them.

THE MERGE SWITCH (SD3) is an audited edit of `gate.merge`, the one way to
`merge: auto`, which `PATCH /v1/schedules/{id}` refuses with 422
`use_merge_switch`. To `auto`: a platform admin (PR 860's admin roles,
PLATFORM_OWNER included) when the scope names a `platform: true`
repository or on a platform-scope schedule of an owner-only type (today
`failure-review`, whose only creator is the owner: decided 2026-10-22, "The
owner may switch an owner-only platform type"); elsewhere any member but
the last editor of the gate, scope or spec (SD5), and in a one-person tenant the person, after typing the
schedule's name. To `approve`: any member, at once. Both write a
`schedule_audit` entry (`gate_merge_auto`, `gate_merge_approve`) with the
before and after of `gate`, in the transaction that changes it. The switch
cannot open a §4.4 hold: a held run merges only on a `merge` approval from
the hold's approvers, whatever the gate says (`issueci.merge_gate`).

NOTHING HERE CREATES DEMAND (invariants 1-3). A decision moves a document:
a firing gains its `run_approval`, a run its `merge_approved`, a plan its
approval -- whose workflow `_approve` submits through `SubmissionService`,
QUEUED until admission leases it, exactly as an approval in Work › Runs.

WHO MAY REJECT IS WHO MAY APPROVE (BGD 2026-10-26, #2417 A5f). `:reject`
runs the same who-checks `:approve` does -- the gate's `approvers`, the
hold, the last-editor rule, the `spec` rule (`_who_refusal`) -- and a caller
they refuse is 403 `reject_approver_required`, through its switch, which
ships on. Off (`REFUSAL_REJECT_APPROVER_REQUIRED=off`), any member rejects,
as before; the log names the refusal it would have made.

WHAT THE DECISION PAGE READS (AUTO-API-APPROVALS, part of #2417). Each item
served carries `may_decide` -- `{approve, reject, why, deciders}` for the
reader, computed by the routes' own checks without moving or logging
anything -- and the detail adds, where its kind has them, `superseded_by` and
`head_sha_now` (a superseded merge), `merge.checks` (a merge) and
`reason_files` on a hold. Each is read, never stored, and absent when it
cannot be read; `approvals.py` says how each is.

NAMES BESIDE EMAILS (docs/design/user-profile.md §4.4). Each item served
gains `decided_by_name` beside `decided_by` and `approver_names` beside
`approvers` (index for index when `approvers` is a list of members, null when
it is a rule such as `members`), from one lookup per response
(`people.add_names`). A name is a label: who may decide is still the email.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Body, Depends, Query
from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from swarm_common.admission import _snapshot

from .. import approvals, refusals, roles, schedaudit, schedules, scheduletypes
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ApiError, NotFound
from ..issueruns import InvalidRunTransition, IssueRun, IssueRuns, RunState
from ..issuesync import sync_issue
from ..people import add_names, name_lookup
from ..redaction import redact_detail
from ..repositories import hard_stop_paths_of
from ..schedules import GateIn, ScheduleConflict, ScheduleForbidden, ScheduleInvalid
from . import runs as run_routes
from . import schedules as schedule_routes

log = logging.getLogger(__name__)

router = APIRouter(tags=["approvals"])


class _Verb(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApproveIn(_Verb):
    #: What the caller was shown: a plan digest or a firing's params digest
    #: (text), or a merge's `{head_sha, verdict}` (or `<head_sha>:<verdict>`).
    digest: str | dict[str, Any]
    #: A one-person tenant's typed confirmation of its own hold (§4.6, SD11).
    confirm: str | None = Field(default=None, max_length=2000)


class RejectIn(_Verb):
    reason: str = Field(min_length=1, max_length=1000)


class MergeModeIn(_Verb):
    mode: Literal["approve", "auto"]
    revision: StrictInt
    #: A one-person tenant's typed confirmation: the schedule's name.
    confirm: str | None = Field(default=None, max_length=200)


def _verb(model: type[BaseModel], body: Mapping[str, Any] | None) -> Any:
    try:
        return model.model_validate(body or {})
    except ValidationError as exc:
        raise ScheduleInvalid("invalid_request", "the request is not valid", errors=schedules._errors(exc)) from None


def _projected_run(ctx: AppContext, tenant_id: str, item_id: str) -> Any:
    run_id = item_id[len(approvals.PROJECTED_PREFIX):]
    try:
        return IssueRuns(ctx.db, now=ctx.now).get(tenant_id, run_id)
    except NotFound:
        raise NotFound(f"approval {item_id[:80]!r} not found") from None


def _platform_only(auth: AuthContext, schedule: Mapping[str, Any] | None, action: str) -> None:
    """PR 2272's refusal for a decision on a platform-scope schedule's item.
    `schedule` is the one the item's stored reference names; None (no
    schedule, or no longer this tenant's) keeps today's rules."""
    if schedule is not None:
        schedule_routes._platform_only(auth, schedule, action)


def _audit_run(ctx: AppContext, run: Any, action: str, by: str, detail: Mapping[str, Any]) -> None:
    """A scheduled run's plan decision in its schedule's audit (§4.8)."""
    schedule_id = (run.schedule or {}).get("schedule_id")
    if not schedule_id or approvals.read_schedule(ctx.db, run.tenant_id, schedule_id) is None:
        return
    batch = ctx.db.batch()
    schedaudit.append(batch, ctx.db, schedule_id=schedule_id, tenant_id=run.tenant_id, action=action,
                      by=by, at=ctx.now(), detail=dict(detail))
    batch.commit()


# --------------------------------------------------------------------------
# Who may decide, read (`may_decide`), and the reject rule
# --------------------------------------------------------------------------


def _who_refusal(ctx: AppContext, auth: AuthContext, tenant_id: str, item: Mapping[str, Any], *,
                 run: IssueRun | None, schedule: Mapping[str, Any] | None) -> ApiError | None:
    """The refusal an approve of `item` would meet from its kind's who-checks
    -- the gate's `approvers`, the hold, the last-editor rule, the `spec`
    rule -- or None. The same calls `approve` makes, in its order, with no
    digest, no move and no log line. The role floor and the platform-scope
    rule are asked apart, before this, by every decision.

    `run` is the projected run, or a merge's run (None when it could not be
    read: its hold is then not asked, and the gate still is)."""
    kind = item.get("kind")
    email, is_owner = auth.email, auth.is_owner
    try:
        if item.get("projected"):
            if schedule is not None:
                approvals.check_gate_approver(ctx, tenant_id, (schedule.get("gate") or {}).get("approvers"),
                                              email=email, is_owner=is_owner, schedule=schedule, merge_tier=False)
            return approvals.hold_refusal(ctx, run, email=email, is_owner=is_owner) if run is not None else None
        if kind == approvals.SPEC:
            if auth.is_admin or is_owner:
                return None
            return ScheduleForbidden("approver_not_allowed", "a custom-prompt spec is approved by a platform admin")
        if kind == approvals.MERGE:
            if run is not None:
                found = approvals.hold_refusal(ctx, run, email=email, is_owner=is_owner)
                if found is not None:
                    return found
            approvals.check_gate_approver(
                ctx, tenant_id, ((schedule or {}).get("gate") or {}).get("approvers"), email=email,
                is_owner=is_owner, schedule=schedule, merge_tier=True,
                hold=run.approval_hold if run is not None else None, owner_waiver=True,
            )
            return None
        approvals.check_gate_approver(ctx, tenant_id, item.get("approvers"), email=email, is_owner=is_owner,
                                      schedule=schedule, merge_tier=bool(item.get("merge_tier")), owner_waiver=True)
    except ScheduleForbidden as exc:
        return exc
    return None


def _role_floor(ctx: AppContext, auth: AuthContext) -> tuple[bool, str | None]:
    """`require_decider`, read: `(readable, why refused)`. Not readable when the
    role could not be read and its switch is on (the route would answer an
    error, not a decision). Read through the role cache, not fresh: this is a
    page, and the decision itself reads it fresh."""
    store = getattr(ctx, "roles", None)
    if store is None:
        return True, None
    code = roles.RoleMemberRequired.code
    me = store.try_resolve(auth)
    if me is None:
        return not refusals.enforced(code), None
    if roles.at_least(me.role, roles.MEMBER) or not refusals.enforced(code):
        return True, None
    return True, f"Your role in {auth.tenant_id} is {me.role}; deciding needs member or above."


def _still_open(item: Mapping[str, Any], now: Any) -> str | None:
    """Why nothing is left to decide on `item`, or None while it is open."""
    state = item.get("state")
    if state != approvals.PENDING:
        return f"This approval is already {state}; nothing is left to decide."
    expires = approvals._aware(item.get("expires_at"))
    if expires is not None and expires <= now:
        return "This approval has expired; nothing is left to decide."
    return None


def may_decide(ctx: AppContext, auth: AuthContext, tenant_id: str, item: Mapping[str, Any], *,
               run: IssueRun | None, schedule: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`{approve, reject, why, deciders}` for the reader (#2417 A4, A5f).

    `approve`: whether `:approve` would pass its who-checks for this reader
    now -- the role floor, the platform-scope rule, and `_who_refusal`. A
    one-person workspace's own hold counts as yes: the approve passes once
    the matched paths are typed. `reject`: the same, while the
    `reject_approver_required` switch is on (BGD 2026-10-26); off, any
    member past the role floor and the platform-scope rule. `why`: one
    sentence when either is false, else None. `deciders`: `approvals.deciders`.

    None (absent) for an await-input step, which is answered on its own
    route and never decided here, and when the reader's role cannot be read.
    The decision routes still decide; this reads their rules out before the
    click and never widens them."""
    if item.get("kind") == approvals.INPUT:
        return None
    readable, why = _role_floor(ctx, auth)
    if not readable:
        return None
    platform = schedule is not None and schedule_routes._platform_scope(schedule)
    if why is None and platform and not (auth.is_admin or auth.is_owner):
        why = "Only a platform admin may decide a platform-scope schedule's items."
    if why is None:
        why = _still_open(item, ctx.now())
    approve = reject = False
    if why is None:
        found = _who_refusal(ctx, auth, tenant_id, item, run=run, schedule=schedule)
        approve = found is None
        reject = approve or not refusals.enforced(approvals.RejectApproverRequired.code)
        why = None if approve else found.message
    # The approvers `approve` asks: a projected run's and a merge's are its
    # schedule's gate (the hold is asked beside them); a record's are its own.
    by_gate = item.get("projected") or item.get("kind") == approvals.MERGE
    return {
        "approve": approve,
        "reject": reject,
        "why": redact_detail(why, limit=approvals.SUMMARY_CHARS) if why else None,
        "deciders": approvals.deciders(
            ctx, tenant_id, kind=str(item.get("kind")),
            approvers=((schedule or {}).get("gate") or {}).get("approvers") if by_gate else item.get("approvers"),
            hold=run.approval_hold if run is not None else item.get("hold"), schedule=schedule,
            merge_tier=item.get("kind") == approvals.MERGE or bool(item.get("merge_tier")),
            platform_scope=platform, run=run,
        ),
    }


def _record_run(ctx: AppContext, tenant_id: str, doc: Mapping[str, Any]) -> IssueRun | None:
    """A merge record's run, or None when it is not a merge or cannot be read."""
    run_id = (doc.get("subject") or {}).get("run_id")
    if doc.get("kind") != approvals.MERGE or not run_id:
        return None
    try:
        return IssueRuns(ctx.db, now=ctx.now).get(tenant_id, str(run_id))
    except NotFound:
        return None


def _reject_rule(ctx: AppContext, auth: AuthContext, tenant_id: str, item: Mapping[str, Any], *,
                 run: IssueRun | None, schedule: Mapping[str, Any] | None) -> None:
    """403 `reject_approver_required` when the caller may not approve `item`
    (BGD 2026-10-26), through its switch: off, the rejection goes ahead and
    the log names the refusal it would have made."""
    found = _who_refusal(ctx, auth, tenant_id, item, run=run, schedule=schedule)
    if found is None:
        return
    refusals.refuse(approvals.RejectApproverRequired(
        f"only someone who may approve this item may reject it: {found.message}",
        detail={"approve_refusal": found.code, **found.detail},
    ))


# --------------------------------------------------------------------------
# The inbox
# --------------------------------------------------------------------------


#: The email fields of an approval that are served with a name beside them.
NAMED_FIELDS = ("decided_by", "approvers")


def _named(ctx: AppContext, item: dict) -> dict:
    add_names(name_lookup(ctx), [item], NAMED_FIELDS)
    return item


def _with_may_decide(out: dict, decided: dict[str, Any] | None) -> dict:
    if decided is not None:
        out["may_decide"] = decided
    return out


@router.get("/v1/approvals")
def list_approvals(
    kind: str | None = Query(default=None, max_length=20),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if kind is not None and kind not in approvals.KINDS:
        raise ScheduleInvalid("unknown_kind", f"kind is one of {', '.join(approvals.KINDS)}")
    items = []
    seen: dict[str, Mapping[str, Any] | None] = {}
    for item in approvals.inbox(ctx, tenant_id, kind=kind):
        run = None
        if item.get("projected") and item.get("kind") != approvals.INPUT:
            run = _projected_run(ctx, tenant_id, str(item["approval_id"]))
        elif item.get("kind") == approvals.MERGE:
            run = _record_run(ctx, tenant_id, item)
        sid = (item.get("subject") or {}).get("schedule_id")
        if sid and sid not in seen:
            seen[sid] = approvals.read_schedule(ctx.db, tenant_id, sid)
        decided = may_decide(ctx, auth, tenant_id, item, run=run, schedule=seen.get(sid) if sid else None)
        items.append(_with_may_decide(approvals.to_api(item), decided))
    add_names(name_lookup(ctx), items, NAMED_FIELDS)
    return {"approvals": items, "tenant_id": tenant_id}


@router.get("/v1/approvals/{item_id}")
def get_approval(
    item_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if item_id.startswith(approvals.PROJECTED_PREFIX):
        run = _projected_run(ctx, tenant_id, item_id)
        if run.state != RunState.PLANNED:
            raise NotFound(f"approval {item_id[:80]!r} not found")
        schedule = approvals.read_schedule(ctx.db, tenant_id, (run.schedule or {}).get("schedule_id"))
        item = approvals.project_run(run, schedule)
        decided = may_decide(ctx, auth, tenant_id, item, run=run, schedule=schedule)
        served = approvals.to_api(item)
        if run.approval_hold:
            served["hold"] = approvals.with_reason_files(
                served.get("hold"), hard_stop_paths_of(run_routes._registration(ctx, tenant_id, run)))
        return {"approval": _named(ctx, _with_may_decide(served, decided)), "run": run.to_api()}
    doc = approvals.get(ctx.db, tenant_id, item_id)
    schedule = approvals.read_schedule(ctx.db, tenant_id, (doc.get("subject") or {}).get("schedule_id"))
    run = _record_run(ctx, tenant_id, doc)
    served = approvals.to_api(doc)
    out: dict[str, Any] = {"approval": served}
    if doc.get("kind") == approvals.MERGE and (doc.get("subject") or {}).get("run_id"):
        if run is None:
            # The run is gone or another tenant's: the detail's 404, as before.
            raise NotFound(f"approval {item_id[:80]!r} not found")
        pull = run.pull_request or {}
        stop_paths = hard_stop_paths_of(run_routes._registration(ctx, tenant_id, run))
        head_now = pull.get("head_sha") if isinstance(pull.get("head_sha"), str) else None
        if head_now:
            served["head_sha_now"] = head_now
        newer = approvals.superseded_by(ctx.db, doc, head_now)
        if newer is not None:
            served["superseded_by"] = newer
        if served.get("hold"):
            served["hold"] = approvals.with_reason_files(served["hold"], stop_paths)
        out["merge"] = {
            "green_sha": run.green_sha,
            "pull_request": run.to_api()["pull_request"],
            "requirements_met": run.requirements_met,
            "requirements_unmet": list(run.requirements_unmet),
            # The changed files checked against the protected paths at this
            # head (`issueci.merge_gate`): what the hold says they matched.
            "files_checked_head": pull.get("files_checked_head"),
            "approval_hold": approvals.with_reason_files(run.to_api()["approval_hold"], stop_paths),
            # #2259: a hold the plan's prompt alone set, cleared there because
            # the pull request's own files and patches matched none of it.
            "hold_cleared": _hold_cleared(pull.get("hold_cleared")),
        }
        checks = approvals.merge_checks(doc, pull)
        if checks is not None:
            out["merge"]["checks"] = checks
    _with_may_decide(served, may_decide(ctx, auth, tenant_id, doc, run=run, schedule=schedule))
    _named(ctx, served)
    return out


def _hold_cleared(value: Any) -> dict[str, Any] | None:
    """`issueci.merge_gate`'s record of a cleared hold, as served."""
    if not isinstance(value, dict):
        return None
    at = value.get("at")
    return {"why": value.get("why"), "head_sha": value.get("head_sha"),
            "reasons": [str(r) for r in value.get("reasons") or []],
            "at": at.isoformat() if hasattr(at, "isoformat") else at}


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


def _approve_projected(ctx: AppContext, auth: AuthContext, tenant_id: str, item_id: str,
                       body: ApproveIn) -> dict:
    run = _projected_run(ctx, tenant_id, item_id)
    if run.state != RunState.PLANNED:
        raise ScheduleConflict("already_decided", f"run {run.id} is {run.state.value}", state=run.state.value)
    if not isinstance(body.digest, str):
        raise ScheduleInvalid("invalid_digest", "a plan is approved by its plan_digest")
    schedule = approvals.read_schedule(ctx.db, tenant_id, (run.schedule or {}).get("schedule_id"))
    _platform_only(auth, schedule, "approve")
    if schedule is not None:
        approvals.check_gate_approver(
            ctx, tenant_id, (schedule.get("gate") or {}).get("approvers"), email=auth.email,
            is_owner=auth.is_owner, schedule=schedule, merge_tier=False,
        )
    try:
        moved = run_routes._approve(ctx, auth, tenant_id, run, digest=body.digest, by=auth.email,
                                    confirm=body.confirm)
    except InvalidRunTransition:
        raise ScheduleConflict("already_decided", f"run {run.id} was decided first") from None
    _audit_run(ctx, run, schedaudit.APPROVED, auth.email, {
        "approval_id": item_id, "kind": approvals.HOLD if run.approval_hold else approvals.PLAN,
        "subject": {"run_id": run.id}, "digest": body.digest,
        "hold": (run.approval_hold or {}).get("code"),
        "confirmed": bool(body.confirm) and approvals.one_person(ctx, tenant_id),
    })
    return {"approval": {"approval_id": item_id, "state": approvals.APPROVED},
            "run": sync_issue(ctx, moved).to_api()}


@router.post("/v1/approvals/{item_id}:approve")
def approve(
    item_id: str,
    body: dict[str, Any] = Body(...),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    verb: ApproveIn = _verb(ApproveIn, body)
    approvals.require_decider(ctx, auth)
    if item_id.startswith(approvals.PROJECTED_PREFIX):
        return _approve_projected(ctx, auth, tenant_id, item_id, verb)
    doc = approvals.get(ctx.db, tenant_id, item_id)
    kind = doc.get("kind")
    now = ctx.now()
    schedule = approvals.read_schedule(ctx.db, tenant_id, (doc.get("subject") or {}).get("schedule_id"))
    _platform_only(auth, schedule, "approve")
    move = None
    run = None
    # #2259: where only the owner may decide, the owner decides as the
    # schedule's last editor too, and `decide` writes the waiver's audit row.
    waiver = None
    if kind == approvals.RUN:
        waiver = approvals.check_gate_approver(ctx, tenant_id, doc.get("approvers"), email=auth.email,
                                               is_owner=auth.is_owner, schedule=schedule,
                                               merge_tier=bool(doc.get("merge_tier")), owner_waiver=True)
        move = approvals.firing_move(ctx.db, now, approve=True, by=auth.email)
    elif kind == approvals.MERGE:
        run = IssueRuns(ctx.db, now=ctx.now).get(tenant_id, str((doc.get("subject") or {}).get("run_id")))
        if run.approval_hold:
            approvals.check_hold(ctx, run, email=auth.email, is_owner=auth.is_owner, confirm=verb.confirm)
        # A hold is checked above IN ADDITION to the schedule's approvers.
        # The run's CURRENT hold, as `issueci.merge_gate` last re-checked it
        # on the pull request's files, decides who may; not the record's copy.
        waiver = approvals.check_gate_approver(
            ctx, tenant_id, ((schedule or {}).get("gate") or {}).get("approvers"),
            email=auth.email, is_owner=auth.is_owner, schedule=schedule, merge_tier=True,
            hold=run.approval_hold, owner_waiver=True,
        )
        move = approvals.merge_move(ctx.db, now, by=auth.email, resign=run_routes.owner_resigner(ctx))
    elif kind == approvals.SPEC:
        # §4.5: a `custom-prompt` spec is approved by platform admins only.
        if not (auth.is_admin or auth.is_owner):
            raise ScheduleForbidden("approver_not_allowed", "a custom-prompt spec is approved by a platform admin")
    else:
        waiver = approvals.check_gate_approver(ctx, tenant_id, doc.get("approvers"), email=auth.email,
                                               is_owner=auth.is_owner, schedule=schedule,
                                               merge_tier=bool(doc.get("merge_tier")), owner_waiver=True)
    decided = approvals.decide(ctx.db, now, tenant_id, item_id, approve=True, by=auth.email,
                               digest=verb.digest, move=move, waiver=waiver)
    out: dict[str, Any] = {"approval": approvals.to_api(decided)}
    if run is not None:
        # The merge is submitted by the run's own loop, now rather than on
        # the next tick; a failure here leaves it to the tick.
        try:
            out["run"] = run_routes.advance_run(ctx, tenant_id, IssueRuns(ctx.db, now=ctx.now).get(
                tenant_id, run.id)).to_api()
        except ApiError as exc:
            log.warning("approval %s: run %s not advanced after the merge approval (%s)",
                        item_id, run.id, exc.code)
    return out


@router.post("/v1/approvals/{item_id}:reject")
def reject(
    item_id: str,
    body: dict[str, Any] = Body(...),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Only someone who may approve an item may reject it (BGD 2026-10-26,
    §4.5): `_reject_rule`, 403 `reject_approver_required` through its switch,
    on by default; off, any member rejects, as before. A `viewer` may not
    (workspace-roles §4.1), nor anyone but a platform admin an item of a
    platform-scope schedule (`_platform_only`), whatever the switch says."""
    verb: RejectIn = _verb(RejectIn, body)
    approvals.require_decider(ctx, auth)
    if item_id.startswith(approvals.PROJECTED_PREFIX):
        run = _projected_run(ctx, tenant_id, item_id)
        schedule = approvals.read_schedule(ctx.db, tenant_id, (run.schedule or {}).get("schedule_id"))
        _platform_only(auth, schedule, "reject")
        if run.state == RunState.PLANNED:
            # A decided run is the 409 below, not a who-refusal.
            _reject_rule(ctx, auth, tenant_id, approvals.project_run(run, schedule), run=run, schedule=schedule)
        try:
            moved = IssueRuns(ctx.db, now=ctx.now).transition(
                tenant_id, run.id, RunState.REJECTED, by=auth.email, from_states={RunState.PLANNED},
                patch={"rejected_by": auth.email, "rejection_reason": verb.reason},
            )
        except InvalidRunTransition:
            raise ScheduleConflict("already_decided", f"run {run.id} was decided first") from None
        _audit_run(ctx, run, schedaudit.REJECTED, auth.email, {
            "approval_id": item_id, "subject": {"run_id": run.id}, "reason": verb.reason,
        })
        return {"approval": {"approval_id": item_id, "state": approvals.REJECTED},
                "run": sync_issue(ctx, moved).to_api()}
    doc = approvals.get(ctx.db, tenant_id, item_id)
    schedule = approvals.read_schedule(ctx.db, tenant_id, (doc.get("subject") or {}).get("schedule_id"))
    _platform_only(auth, schedule, "reject")
    if doc.get("state") == approvals.PENDING:
        # A decided item is `decide`'s 409 `already_decided`, not a who-refusal.
        _reject_rule(ctx, auth, tenant_id, doc, run=_record_run(ctx, tenant_id, doc), schedule=schedule)
    now = ctx.now()
    move = approvals.firing_move(ctx.db, now, approve=False, by=auth.email) if doc.get("kind") == approvals.RUN else None
    decided = approvals.decide(ctx.db, now, tenant_id, item_id, approve=False, by=auth.email,
                               reason=verb.reason, move=move)
    return {"approval": approvals.to_api(decided)}


# --------------------------------------------------------------------------
# The SD3 merge switch
# --------------------------------------------------------------------------


def _owner_only_platform(entry: scheduletypes.ScheduleType, scope: Mapping[str, Any]) -> bool:
    """A platform-scope schedule of a type only the owner may create there.

    Read from the type's own `creators` declaration (today `failure-review`),
    never from a list of names, so a type declared that way later is switched
    the same way and one declared admin-creatable keeps the two-person rule.
    """
    return scope.get("mode") == "platform" and entry.creators.get("platform") == "owner"


@router.post("/v1/schedules/{schedule_id}:merge-mode")
def merge_mode(
    schedule_id: str,
    body: dict[str, Any] = Body(...),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    verb: MergeModeIn = _verb(MergeModeIn, body)
    # Either direction is a decision (workspace-roles §4.1): `member` first.
    approvals.require_decider(ctx, auth)
    stored = schedule_routes._read(ctx, tenant_id, schedule_id)
    # A platform-scope schedule is a platform admin's to change, either
    # direction (BGD, 2026-10-24; `schedule_routes._platform_only`).
    schedule_routes._platform_only(auth, stored, "merge_mode")
    entry = scheduletypes.get(str(stored.get("type")))
    if entry is None:
        raise ScheduleInvalid("unknown_type", f"{stored.get('type')!r} is no longer a schedule type")
    now = schedule_routes._now(ctx)
    confirmed = False
    if verb.mode == "auto":
        if entry.floor_gate.merge != "auto":
            raise ScheduleInvalid("merge_auto_not_allowed", f"{entry.name} never merges unattended")
        _, platform = schedule_routes._registrations(ctx, tenant_id, schedules.ScopeIn(**stored["scope"]))
        if _owner_only_platform(entry, stored["scope"]):
            # Decided 2026-10-22 (FR3-TYPE, part of #1933): "The owner may
            # switch an owner-only platform type" -- Q8's "set once by you
            # through the audited merge switch at creation". The owner is its
            # only possible creator, so the two-person rule would refuse the
            # one person the switch is for.
            if not (auth.is_admin or auth.is_owner):
                raise ScheduleForbidden(
                    "platform_admin_required",
                    f"only the platform owner or a platform admin may switch a {entry.name} schedule's "
                    "merging to automatic",
                )
        elif platform:
            # Owner decision 2026-10-08: "only admins in the platform repos".
            if not (auth.is_admin or auth.is_owner):
                raise ScheduleForbidden(
                    "platform_admin_required",
                    "in a platform: true repository only a platform admin may switch merging to automatic",
                )
        elif approvals.one_person(ctx, tenant_id):
            # SD5 cannot apply with one person; the switch is typed instead.
            if (verb.confirm or "").strip() != str(stored.get("name") or "").strip():
                raise ScheduleInvalid(
                    "confirmation_required", "type the schedule's name to switch merging to automatic",
                )
            confirmed = True
        elif schedaudit.last_editor(ctx.db, tenant_id, stored) == auth.email.strip().lower():
            raise ScheduleForbidden(
                "second_person_required",
                "the member who last changed this schedule's gate, scope or spec cannot also switch "
                "its merging to automatic; another member must",
            )
    ref = schedule_routes._schedule_ref(ctx, schedule_id)

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        before = schedule_routes._owned(_snapshot(txn.get(ref)), tenant_id, schedule_id)
        schedule_routes._platform_only(auth, before, "merge_mode")
        if verb.revision != before.get("revision"):
            raise ScheduleConflict("schedule_changed", "the schedule was changed since it was read; read it again",
                                   revision=before.get("revision"))
        gate = schedules.resolve_gate(entry, GateIn(merge=verb.mode), current=before["gate"],
                                      is_admin=auth.is_admin, via_merge_switch=True)
        # The params restate the gate for issue-sweep (`merge`); the gate is
        # the authority, and nothing else in them changes here.
        params = schedules.resolve_params(entry, None, gate=gate, current=before["params"], is_admin=True)
        doc = {**before, "gate": gate, "params": params, "updated_by": auth.email, "updated_at": now,
               "revision": int(before.get("revision") or 0) + 1}
        txn.set(ref, doc)
        schedaudit.append(
            txn, ctx.db, schedule_id=schedule_id, tenant_id=tenant_id,
            action=schedaudit.GATE_MERGE_AUTO if verb.mode == "auto" else schedaudit.GATE_MERGE_APPROVE,
            by=auth.email, at=now,
            detail={"gate": {"from": dict(before["gate"]), "to": gate}, "revision": doc["revision"],
                    "confirmed": confirmed},
        )
        return doc

    doc = _apply(ctx.db.transaction())
    return {"schedule": schedule_routes.schedule_to_api(doc, now)}
