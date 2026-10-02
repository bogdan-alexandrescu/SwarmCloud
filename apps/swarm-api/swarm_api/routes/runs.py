"""Issue-run routes (#454). The run document and its machine are `swarm_api.issueruns`.

    POST /v1/runs                         plan an issue: one planner task
    GET  /v1/runs                         the caller's tenant's runs, newest first
    GET  /v1/runs/{run_id}                one run, advanced to what its tasks say
    POST /v1/runs/{run_id}/plan:approve   {"plan_digest"}  -> a new workflow
    POST /v1/runs/{run_id}/plan:edit      {"plan_digest", "plan"}
    POST /v1/runs/{run_id}/plan:reject    {"plan_digest"?, "reason"?}

EVERY READ ADVANCES, as every workflow read derives (`routes/workflows.py`). A
run's state is a consequence of its planner task and then its workflow, and
there is no background writer for it: the read that finds the planner ended
reads `plan.json` and moves the run to PLANNED, and the read that finds the
workflow ended moves it to DONE, FAILED or CANCELLED. A healthy run that has
not moved costs one task or workflow read and writes nothing. Each move is a
transaction that re-checks the state it moves from, so two readers racing
produce one transition, and the loser re-reads.

The tenant on every route comes from `tenant_scope`, never from the body: a
run is created in the caller's tenant (through `submit_tasks`, which runs
`tenant_for`) and every other route reads it under that same check.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Response, status

from swarm_common.models import new_id
from swarm_common.states import TaskState

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..errors import ApiError, Conflict, Gone, NotFound, UpstreamUnavailable
from ..issueruns import (
    AUTO_APPROVER,
    MAX_PLAN_BYTES,
    PLAN_FILE,
    TERMINAL_RUN_STATES,
    InvalidPlan,
    IssueRun,
    IssueRuns,
    RunState,
    compile_plan,
    failure_text,
    parse_plan,
    plan_digest,
    planner_task,
    refuse_auto_merge,
)
from ..schemas import PlanApprove, PlanEdit, PlanReject, RunCreate
from ..validation import parse_issue_ref

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/runs", tags=["runs"])

#: What swarm-api writes as `by` on a transition nobody asked for by name.
SYSTEM = "swarm-api"

_PLANNED_ONLY = frozenset({RunState.PLANNED})


def _runs(ctx: AppContext) -> IssueRuns:
    return IssueRuns(ctx.db, now=ctx.now)


# --------------------------------------------------------------------------
# advancing a run from what its tasks say
# --------------------------------------------------------------------------

def _read_plan(ctx: AppContext, tenant_id: str, task_id: str) -> dict:
    """`plan.json` out of the planner's artifacts, through the API's own masked reader."""
    try:
        window = ctx.inspection.read_artifact(
            tenant_id, task_id, submitted_by=None, name=PLAN_FILE, limit_bytes=MAX_PLAN_BYTES
        )
    except NotFound:
        raise InvalidPlan(f"the planner task {task_id} wrote no {PLAN_FILE}") from None
    except Gone:
        raise InvalidPlan(f"the planner task's {PLAN_FILE} is no longer in the bucket") from None
    if window.get("status") != "ok":
        raise InvalidPlan(f"the planner task's {PLAN_FILE} is not text")
    if window.get("truncated"):
        raise InvalidPlan(f"the planner task's {PLAN_FILE} is larger than {MAX_PLAN_BYTES} bytes")
    return parse_plan(window.get("content") or "")


def _from_planner(ctx: AppContext, tenant_id: str, run: IssueRun) -> IssueRun:
    runs = _runs(ctx)
    try:
        task = ctx.store.get_task(tenant_id, run.planner_task_id, submitted_by=None)
    except NotFound:
        return runs.transition(
            tenant_id, run.id, RunState.FAILED, by=SYSTEM,
            patch={"error": f"the planner task {run.planner_task_id} no longer exists"},
        )
    if task.state == TaskState.SUCCEEDED:
        try:
            plan = _read_plan(ctx, tenant_id, task.id)
        except InvalidPlan as refused:
            return runs.transition(
                tenant_id, run.id, RunState.FAILED, by=SYSTEM,
                patch={"error": failure_text(refused.message)},
            )
        except UpstreamUnavailable:
            # The bucket, not the plan: the next read tries again.
            log.warning("issue run %s: the planner's %s could not be read yet", run.id, PLAN_FILE)
            return run
        return runs.transition(
            tenant_id, run.id, RunState.PLANNED, by=SYSTEM,
            patch={"plan": plan, "plan_digest": plan_digest(plan), "plan_revision": 1},
        )
    if task.state in (TaskState.FAILED, TaskState.DEAD_LETTERED):
        why = f"the planner task {task.id} ended {task.state.value}"
        if task.last_error:
            why += f": {task.last_error}"
        return runs.transition(
            tenant_id, run.id, RunState.FAILED, by=SYSTEM, patch={"error": failure_text(why)}
        )
    if task.state == TaskState.CANCELLED:
        return runs.transition(
            tenant_id, run.id, RunState.CANCELLED, by=SYSTEM,
            patch={"error": f"the planner task {task.id} was cancelled"},
        )
    return run


_WORKFLOW_ENDS = {
    TaskState.SUCCEEDED.value: RunState.DONE,
    TaskState.FAILED.value: RunState.FAILED,
    TaskState.DEAD_LETTERED.value: RunState.FAILED,
    TaskState.CANCELLED.value: RunState.CANCELLED,
}


def _from_workflow(ctx: AppContext, tenant_id: str, run: IssueRun) -> IssueRun:
    if not run.workflow_id:
        return run
    try:
        workflow = ctx.store.get_workflow(tenant_id, run.workflow_id, submitted_by=None)
    except NotFound:
        return run
    results, _ = ctx.rollups.for_workflows(tenant_id, [workflow])
    derived = results[0].to_api().get("state") if results else None
    to = _WORKFLOW_ENDS.get(str(derived))
    if to is None:
        return run
    patch = {} if to == RunState.DONE else {
        "error": f"the run's workflow {run.workflow_id} ended {derived}"
    }
    return _runs(ctx).transition(tenant_id, run.id, to, by=SYSTEM, patch=patch)


def _approve(
    ctx: AppContext, auth: AuthContext, tenant_id: str, run: IssueRun, *, digest: str, by: str
) -> IssueRun:
    """APPROVED under the digest check, then the workflow, then RUNNING.

    The plan is compiled BEFORE the claim, so a plan the compiler refuses
    leaves the run PLANNED for an edit, and AGAIN from the claimed document,
    so what is submitted is the plan whose digest the transaction matched.
    """
    runs = _runs(ctx)
    compile_plan(run)
    approved = runs.transition(
        tenant_id, run.id, RunState.APPROVED, by=by, digest=digest, from_states=_PLANNED_ONLY,
        patch=lambda current: {
            "approved_by": by,
            "approved_at": ctx.now(),
            "approved_digest": current.plan_digest,
        },
    )
    try:
        submission = ctx.submissions.submit_workflow(auth, compile_plan(approved))
    except Exception as exc:
        reason = exc.message if isinstance(exc, ApiError) else type(exc).__name__
        runs.transition(
            tenant_id, run.id, RunState.FAILED, by=SYSTEM,
            patch={"error": failure_text(f"the compiled workflow was refused: {reason}")},
        )
        raise
    return runs.transition(
        tenant_id, run.id, RunState.RUNNING, by=by,
        patch={"workflow_id": submission.workflow.workflow_id},
    )


def _advance(ctx: AppContext, auth: AuthContext, tenant_id: str, run: IssueRun) -> IssueRun:
    """Move `run` as far as its tasks say it has gone. Never raises for a race."""
    try:
        if run.state == RunState.PLANNING:
            run = _from_planner(ctx, tenant_id, run)
        if run.state == RunState.PLANNED and run.plan_approval == "auto" and run.plan_digest:
            try:
                run = _approve(ctx, auth, tenant_id, run, digest=run.plan_digest, by=AUTO_APPROVER)
            except Conflict:
                raise
            except ApiError as refused:
                # Recorded on the run by `_approve` when it got as far as the
                # submission; a compile refusal leaves it PLANNED for an edit.
                log.info("issue run %s: auto-approval refused (%s)", run.id, refused.code)
                return _runs(ctx).get(tenant_id, run.id)
        if run.state == RunState.RUNNING:
            run = _from_workflow(ctx, tenant_id, run)
    except Conflict:
        # Another request moved it first; what it moved it to is the answer.
        return _runs(ctx).get(tenant_id, run.id)
    return run


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
def create_run(
    body: RunCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # Before anything is created: a refused option writes nothing.
    refuse_auto_merge(body.auto_merge)
    ref = parse_issue_ref(body.issue)
    run_id = new_id("run")
    # The planner first, carrying the run's id: a planner without its run is
    # one finished task nobody reads, while a run without its planner would
    # sit PLANNING for ever.
    submission = ctx.submissions.submit_tasks(auth, [planner_task(ref, run_id)])
    planner = submission.tasks[0]
    now = ctx.now()
    run = _runs(ctx).create(
        IssueRun(
            id=run_id,
            tenant_id=planner.tenant_id,
            created_by=auth.email,
            created_at=now,
            updated_at=now,
            state=RunState.PLANNING,
            issue=ref,
            plan_approval=body.plan_approval,
            auto_merge=body.auto_merge,
            fix_rounds=body.fix_rounds,
            planner_task_id=planner.id,
        )
    )
    response.headers["Location"] = f"/v1/runs/{run.id}"
    return {"run": run.to_api()}


@router.get("")
def list_runs(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    rows, next_token = _runs(ctx).list(
        tenant_id, limit=paged_limit(ctx, limit), page_token=page_token
    )
    served = []
    for run in rows:
        if run.state not in TERMINAL_RUN_STATES:
            try:
                run = _advance(ctx, auth, tenant_id, run)
            except ApiError as exc:
                # One run's read failing must not hide the page; the row is
                # served as stored, and its own GET reports the failure.
                log.warning("issue run %s not advanced in the list (%s)", run.id, exc.code)
        served.append(run.to_api())
    return {"runs": served, "next_page_token": next_token, "tenant_id": tenant_id}


@router.get("/{run_id}")
def get_run(
    run_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    run = _runs(ctx).get(tenant_id, run_id)
    return {"run": _advance(ctx, auth, tenant_id, run).to_api()}


@router.post("/{run_id}/plan:approve")
def approve_plan(
    run_id: str,
    body: PlanApprove,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    run = _runs(ctx).get(tenant_id, run_id)
    if run.state != RunState.PLANNED:
        raise Conflict(
            f"run {run_id!r} is {run.state.value}; only a PLANNED run's plan can be approved",
            detail={"state": run.state.value},
        )
    run = _approve(ctx, auth, tenant_id, run, digest=body.plan_digest, by=auth.email)
    return {"run": run.to_api()}


@router.post("/{run_id}/plan:edit")
def edit_plan(
    run_id: str,
    body: PlanEdit,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    runs = _runs(ctx)
    runs.get(tenant_id, run_id)  # 404 before 422: another tenant's run is not there
    plan = parse_plan(body.plan)
    run = runs.transition(
        tenant_id, run_id, RunState.PLANNED, by=auth.email, digest=body.plan_digest,
        from_states=_PLANNED_ONLY,
        patch=lambda current: {
            "plan": plan,
            "plan_digest": plan_digest(plan),
            "plan_revision": current.plan_revision + 1,
            "plan_edited_by": auth.email,
        },
    )
    return {"run": run.to_api()}


@router.post("/{run_id}/plan:reject")
def reject_plan(
    run_id: str,
    body: PlanReject,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    run = _runs(ctx).transition(
        tenant_id, run_id, RunState.REJECTED, by=auth.email, digest=body.plan_digest,
        from_states=_PLANNED_ONLY,
        patch={"rejected_by": auth.email, "rejection_reason": body.reason},
    )
    return {"run": run.to_api()}
