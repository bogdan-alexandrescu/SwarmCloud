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
body's. Another tenant's item answers the 404 a missing one does. A platform
admin decides nothing of another tenant's by being an admin (§4.6, §5.3):
an `owner_only` item exists only in a tenant the owner is a member of.

THE MERGE SWITCH (SD3) is an audited edit of `gate.merge`, the one way to
`merge: auto`, which `PATCH /v1/schedules/{id}` refuses with 422
`use_merge_switch`. To `auto`: a platform admin (PR 860's admin roles,
PLATFORM_OWNER included) when the scope names a `platform: true`
repository; elsewhere any member but the last editor of the gate, scope or
spec (SD5), and in a one-person tenant the person, after typing the
schedule's name. To `approve`: any member, at once. Both write a
`schedule_audit` entry (`gate_merge_auto`, `gate_merge_approve`) with the
before and after of `gate`, in the transaction that changes it. The switch
cannot open a §4.4 hold: a held run merges only on a `merge` approval from
the hold's approvers, whatever the gate says (`issueci.merge_gate`).

NOTHING HERE CREATES DEMAND (invariants 1-3). A decision moves a document:
a firing gains its `run_approval`, a run its `merge_approved`, a plan its
approval -- whose workflow `_approve` submits through `SubmissionService`,
QUEUED until admission leases it, exactly as an approval in Work › Runs.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Body, Depends, Query
from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from swarm_common.admission import _snapshot

from .. import approvals, schedaudit, schedules, scheduletypes
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ApiError, NotFound
from ..issueruns import InvalidRunTransition, IssueRuns, RunState
from ..issuesync import sync_issue
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
# The inbox
# --------------------------------------------------------------------------


@router.get("/v1/approvals")
def list_approvals(
    kind: str | None = Query(default=None, max_length=20),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if kind is not None and kind not in approvals.KINDS:
        raise ScheduleInvalid("unknown_kind", f"kind is one of {', '.join(approvals.KINDS)}")
    items = approvals.inbox(ctx, tenant_id, kind=kind)
    return {"approvals": [approvals.to_api(i) for i in items], "tenant_id": tenant_id}


@router.get("/v1/approvals/{item_id}")
def get_approval(
    item_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if item_id.startswith(approvals.PROJECTED_PREFIX):
        run = _projected_run(ctx, tenant_id, item_id)
        if run.state != RunState.PLANNED:
            raise NotFound(f"approval {item_id[:80]!r} not found")
        schedule = approvals.read_schedule(ctx.db, tenant_id, (run.schedule or {}).get("schedule_id"))
        return {"approval": approvals.to_api(approvals.project_run(run, schedule)), "run": run.to_api()}
    doc = approvals.get(ctx.db, tenant_id, item_id)
    out: dict[str, Any] = {"approval": approvals.to_api(doc)}
    run_id = (doc.get("subject") or {}).get("run_id")
    if doc.get("kind") == approvals.MERGE and run_id:
        run = IssueRuns(ctx.db, now=ctx.now).get(tenant_id, str(run_id))
        out["merge"] = {
            "green_sha": run.green_sha,
            "pull_request": run.to_api()["pull_request"],
            "requirements_met": run.requirements_met,
            "requirements_unmet": list(run.requirements_unmet),
            # The changed files checked against the protected paths at this
            # head (`issueci.merge_gate`): what the hold says they matched.
            "files_checked_head": (run.pull_request or {}).get("files_checked_head"),
            "approval_hold": run.to_api()["approval_hold"],
        }
    return out


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
    if item_id.startswith(approvals.PROJECTED_PREFIX):
        return _approve_projected(ctx, auth, tenant_id, item_id, verb)
    doc = approvals.get(ctx.db, tenant_id, item_id)
    kind = doc.get("kind")
    now = ctx.now()
    schedule = approvals.read_schedule(ctx.db, tenant_id, (doc.get("subject") or {}).get("schedule_id"))
    move = None
    run = None
    if kind == approvals.RUN:
        approvals.check_gate_approver(ctx, tenant_id, doc.get("approvers"), email=auth.email,
                                      is_owner=auth.is_owner, schedule=schedule,
                                      merge_tier=bool(doc.get("merge_tier")))
        move = approvals.firing_move(ctx.db, now, approve=True, by=auth.email)
    elif kind == approvals.MERGE:
        run = IssueRuns(ctx.db, now=ctx.now).get(tenant_id, str((doc.get("subject") or {}).get("run_id")))
        if run.approval_hold:
            approvals.check_hold(ctx, run, email=auth.email, is_owner=auth.is_owner, confirm=verb.confirm)
        # A hold is checked above IN ADDITION to the schedule's approvers.
        approvals.check_gate_approver(
            ctx, tenant_id, ((schedule or {}).get("gate") or {}).get("approvers"),
            email=auth.email, is_owner=auth.is_owner, schedule=schedule, merge_tier=True,
        )
        move = approvals.merge_move(ctx.db, now, by=auth.email)
    elif kind == approvals.SPEC:
        # §4.5: a `custom-prompt` spec is approved by platform admins only.
        if not (auth.is_admin or auth.is_owner):
            raise ScheduleForbidden("approver_not_allowed", "a custom-prompt spec is approved by a platform admin")
    else:
        approvals.check_gate_approver(ctx, tenant_id, doc.get("approvers"), email=auth.email,
                                      is_owner=auth.is_owner, schedule=schedule,
                                      merge_tier=bool(doc.get("merge_tier")))
    decided = approvals.decide(ctx.db, now, tenant_id, item_id, approve=True, by=auth.email,
                               digest=verb.digest, move=move)
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
    """Anyone in the tenant may reject (§4.5): a rejection ends a hold, it never opens one."""
    verb: RejectIn = _verb(RejectIn, body)
    if item_id.startswith(approvals.PROJECTED_PREFIX):
        run = _projected_run(ctx, tenant_id, item_id)
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
    now = ctx.now()
    move = approvals.firing_move(ctx.db, now, approve=False, by=auth.email) if doc.get("kind") == approvals.RUN else None
    decided = approvals.decide(ctx.db, now, tenant_id, item_id, approve=False, by=auth.email,
                               reason=verb.reason, move=move)
    return {"approval": approvals.to_api(decided)}


# --------------------------------------------------------------------------
# The SD3 merge switch
# --------------------------------------------------------------------------


@router.post("/v1/schedules/{schedule_id}:merge-mode")
def merge_mode(
    schedule_id: str,
    body: dict[str, Any] = Body(...),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    verb: MergeModeIn = _verb(MergeModeIn, body)
    stored = schedule_routes._read(ctx, tenant_id, schedule_id)
    entry = scheduletypes.get(str(stored.get("type")))
    if entry is None:
        raise ScheduleInvalid("unknown_type", f"{stored.get('type')!r} is no longer a schedule type")
    now = schedule_routes._now(ctx)
    confirmed = False
    if verb.mode == "auto":
        if entry.floor_gate.merge != "auto":
            raise ScheduleInvalid("merge_auto_not_allowed", f"{entry.name} never merges unattended")
        _, platform = schedule_routes._registrations(ctx, tenant_id, schedules.ScopeIn(**stored["scope"]))
        if platform:
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
