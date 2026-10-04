"""Issue-run routes (#454). The run document and its machine are `swarm_api.issueruns`.

    POST /v1/runs                         read the open work, then one planner task
    GET  /v1/runs                         the caller's tenant's runs, newest first
    GET  /v1/runs/{run_id}                one run, advanced to what its tasks say
    POST /v1/runs/{run_id}/plan:approve   {"plan_digest"}  -> a new workflow
    POST /v1/runs/{run_id}/plan:edit      {"plan_digest", "plan"}
    POST /v1/runs/{run_id}/plan:reject    {"plan_digest"?, "reason"?}

EVERY MOVE IS WRITTEN BACK to the issue (`issuesync.sync_issue`): the plan
comment once there is a plan, edited when it changes, and the one status
comment, edited in place. A write-back failure is recorded on the run and
never fails the request or the run.

EVERY READ ADVANCES, as every workflow read derives (`routes/workflows.py`),
AND SO DOES A TICK. A run's state is a consequence of its planner task and
then its workflow: the read that finds the planner ended reads `plan.json`
and moves the run to PLANNED, and the read that finds the workflow ended moves
it to CHECKING (it succeeded and opened its pull request), FAILED or
CANCELLED. From CHECKING the CI loop (`swarm_api.issueci`) reads the pull
request's CI and moves it to DONE, to FIXING for a fix round and back, or to
FAILED at the cap. Reads alone left a run nobody watches -- and
every `plan_approval: auto` run, whose whole point is that nobody has to --
where it was, so a Cloud Scheduler job per tenant calls
POST /v1/admin/runs/advance (`advance_tenant_runs`, owner decision on #454)
and the same `advance_run` moves it. A healthy run that has not moved costs
one task or workflow read and writes nothing. Each move is a transaction that
re-checks the state it moves from, so a reader and the tick racing produce one
transition, and the loser re-reads.

AN AUTO APPROVAL SUBMITS AS THE RUN'S CREATOR, in the run's tenant, whoever
or whatever advanced it (`run_owner_auth`): the approval was given when the
run was created, by the member who created it, and the tick's own identity
holds no tenant at all. A CI fix round is submitted the same way, for the same
reason: the run's creator asked for its pull request to be made green.

The tenant on every route comes from `tenant_scope`, never from the body: a
run is created in the caller's tenant (through `submit_tasks`, which runs
`tenant_for`) and every other route reads it under that same check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Query, Response, status

from swarm_common.identity import Principal
from swarm_common.models import Tenant, new_id
from swarm_common.states import TaskState

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..errors import ApiError, Conflict, Forbidden, Gone, NotFound, UpstreamUnavailable
from .. import issueci
from ..forge import ForgeReadError, preview, read_open_work
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
    issue_read_from_preview,
    parse_plan,
    plan_digest,
    planner_task,
    refuse_auto_merge,
)
from ..issuesync import sync_issue
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


#: What the compiled workflow's end moves a run to. SUCCEEDED is not DONE:
#: it opened a pull request whose CI has not been read (`issueci`).
_WORKFLOW_ENDS = {
    TaskState.SUCCEEDED.value: RunState.CHECKING,
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
    if to == RunState.CHECKING:
        return issueci.enter_checking(ctx, tenant_id, run, workflow)
    return _runs(ctx).transition(
        tenant_id, run.id, to, by=SYSTEM,
        patch={"error": f"the run's workflow {run.workflow_id} ended {derived}"},
    )


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


class RunOwnerNotMember(Forbidden):
    """The run's creator is no longer a member of the run's tenant.

    Nothing more is submitted as them: the run FAILS with this message
    (`_advance_state`, `issueci._start_round`).
    """

    code = "run_owner_not_member"


def run_owner_auth(ctx: AppContext, run: IssueRun) -> AuthContext:
    """The submitter of an AUTO approval: the run's creator, in the run's tenant.

    Built from the run document and the tenant document, never from the
    caller, so whatever advanced the run -- a member's read or the tick's
    service account -- the workflow lands in `run.tenant_id` and nowhere else.
    The same shape as a child task's submission (`swarm_api.children`), the
    other path that submits on a tenant's behalf with no person on the call:
    the tenant from the store, the submitter from the work it descends from.

    NOTHING WIDER than the member who created the run held. `is_admin`,
    `member_scope` and `tenant_member` are the ordinary member's empty values
    (a continuation-scoped account cannot reach POST /v1/runs, so no run was
    created by one), and `tenant_principal` is the STORED one, so
    `SubmissionService.tenant_for` runs its collision, secret-admin and
    disabled checks against the tenant as registered.

    AND ONLY WHILE THEY ARE STILL A MEMBER. The tick submits as the creator
    long after the run was created -- an auto approval, up to `fix_rounds`
    CI fix rounds that push to the tenant's repository -- so membership is
    asked of the directory again here, every time (`Authenticator.
    is_tenant_member`, invariant 9). Removed from the tenant:
    `RunOwnerNotMember`, and the run fails. A lookup that fails:
    `UpstreamUnavailable`, and the run waits for the next tick.
    """
    tenant = ctx.store.get_tenant(run.tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {run.tenant_id!r} not found")
    email = run.created_by
    if not ctx.authenticator.is_tenant_member(email, tenant):
        log.warning(
            "issue run %s tenant=%s: its creator is no longer a member; nothing submitted",
            run.id, run.tenant_id,
        )
        raise RunOwnerNotMember(
            f"the run's creator {email or '(not recorded)'} is no longer a member of tenant "
            f"{run.tenant_id!r}, so nothing more is submitted on their behalf"
        )
    return AuthContext(
        principal=Principal(
            email=email,
            # Not a token subject: this context was never authenticated, and
            # the subject names the run it was derived from instead.
            subject=f"issue-run:{run.id}",
            domain=email.rsplit("@", 1)[-1],
            groups=(),
        ),
        tenant_id=run.tenant_id,
        is_admin=False,
        tenant_principal=tenant.principal,
    )


def advance_run(ctx: AppContext, tenant_id: str, run: IssueRun) -> IssueRun:
    """Move `run` as far as its tasks say it has gone, then write it back to the issue.

    One sync after the last move, not one per move: the comments show where
    the run IS, and an intermediate state (APPROVED for the length of one
    submission) is not worth a write. A sync whose text is unchanged writes
    nothing.

    A run that is not `tenant_id`'s answers as missing, as every read does,
    before anything is read or moved.
    """
    if run.tenant_id != tenant_id:
        raise NotFound(f"run {run.id!r} not found")
    return sync_issue(ctx, _advance_state(ctx, tenant_id, run))


def _advance_state(ctx: AppContext, tenant_id: str, run: IssueRun) -> IssueRun:
    """Move `run` as far as its tasks say it has gone. Never raises for a race."""
    try:
        if run.state == RunState.PLANNING:
            run = _from_planner(ctx, tenant_id, run)
        # Only an `auto` run is ever submitted here: a `required` run stays
        # PLANNED, holding nothing (invariant 1), until a person approves it.
        if run.state == RunState.PLANNED and run.plan_approval == "auto" and run.plan_digest:
            try:
                run = _approve(
                    ctx, run_owner_auth(ctx, run), tenant_id, run,
                    digest=run.plan_digest, by=AUTO_APPROVER,
                )
            except RunOwnerNotMember as gone:
                return _runs(ctx).transition(
                    tenant_id, run.id, RunState.FAILED, by=SYSTEM,
                    from_states=_PLANNED_ONLY, patch={"error": failure_text(gone.message)},
                )
            except Conflict:
                raise
            except ApiError as refused:
                # Recorded on the run by `_approve` when it got as far as the
                # submission; a compile refusal leaves it PLANNED for an edit.
                log.info("issue run %s: auto-approval refused (%s)", run.id, refused.code)
                return _runs(ctx).get(tenant_id, run.id)
        if run.state == RunState.RUNNING:
            run = _from_workflow(ctx, tenant_id, run)
        # A round that ended goes back to CHECKING, and CI at the head it
        # pushed is read in the same visit.
        if run.state == RunState.FIXING:
            run = issueci.from_fix_round(ctx, tenant_id, run)
        if run.state == RunState.CHECKING:
            run = issueci.from_checks(ctx, tenant_id, run, owner_auth=run_owner_auth)
    except Conflict:
        # Another request moved it first; what it moved it to is the answer.
        return _runs(ctx).get(tenant_id, run.id)
    return run


@dataclass
class AdvanceReport:
    """What one tick did: how many runs it read, moved and could not advance."""

    visited: int = 0
    moved: int = 0
    failed: int = 0
    #: More live runs than one tick visits; the next tick reaches them.
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict:
        return {
            "visited": self.visited,
            "moved": self.moved,
            "failed": self.failed,
            "truncated": self.truncated,
        }


def advance_tenant_runs(ctx: AppContext, tenant_id: str, *, limit: int) -> AdvanceReport:
    """Advance up to `limit` of ONE tenant's live runs, oldest first.

    "Live" less the PLANNED runs waiting for a person, which only that person
    moves (`IssueRuns.tickable` says why they are left out).

    Every run comes from a query filtered on `tenant_id` and is advanced under
    that same tenant (`advance_run` refuses any other), so a tick for one
    tenant reads, moves and submits nothing of another's.

    ONE RUN'S FAILURE IS ONE RUN'S. Each run is advanced in its own
    try/except and counted; the rest of the page is still advanced. A failure
    is reported by the run id and the exception's TYPE only -- its message may
    quote a forge or store error, and nothing this route returns is passed
    through redaction a second time.

    Reads and transitions, and two kinds of submission. A `required` run the
    planner has finished moves to PLANNED and stops there with no task,
    workflow or lease (invariant 1), and later ticks do not visit it; a tick
    submits only an `auto` run's approved plan, and a CHECKING run's CI fix
    round when its pull request's CI is red (`issueci`) -- each as the run's
    creator, in the run's tenant (`run_owner_auth`).
    """
    rows, truncated = _runs(ctx).tickable(tenant_id, limit=limit)
    report = AdvanceReport(truncated=truncated)
    for run in rows:
        report.visited += 1
        try:
            advanced = advance_run(ctx, tenant_id, run)
        except Exception as exc:
            report.failed += 1
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            report.failures.append({"run_id": run.id, "error": code})
            log.warning("issue run %s tenant=%s not advanced by the tick (%s)", run.id, tenant_id, code)
            continue
        if advanced.state != run.state:
            report.moved += 1
    return report


# --------------------------------------------------------------------------
# what the issue said at submission
# --------------------------------------------------------------------------

def _read_issue(
    ctx: AppContext, auth: AuthContext, tenant_id: str, ref
) -> tuple[dict | None, dict | None]:
    """The issue as the preview reads it, for the run to keep; or why it could not be read.

    THE SAME READ AS `GET /v1/issues/preview` (lane U9 item 4): the caller's
    tenant's forge token, `forge.preview`'s masking and body bound. The token
    lives inside `preview` only; nothing about it is returned or logged here.
    A failure does not refuse the run -- the planner reads the issue itself,
    on the worker -- it is kept as `issue_read_error`, so the run page says
    why its card is empty instead of pretending the issue had nothing in it.
    """
    if ctx.forge_tokens is None or ctx.forge is None:
        return None, {"code": "read_failed", "message": "this API has no forge reader configured"}
    tenant = ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=auth.tenant_principal or auth.email,
        created_at=ctx.now(),
    )
    try:
        read = preview(ref, tenant, tokens=ctx.forge_tokens, issues=ctx.forge)
    except ForgeReadError as refused:
        log.info("issue run read tenant=%s issue=%s outcome=%s", tenant_id, ref.short, refused.code)
        return None, {"code": refused.code, "message": refused.message}
    except Exception as exc:  # noqa: BLE001 -- the run is created either way
        # The type only: an exception's text can quote a request.
        log.warning("issue run read tenant=%s issue=%s failed (%s)", tenant_id, ref.short, type(exc).__name__)
        return None, {"code": "read_failed", "message": f"the issue could not be read ({type(exc).__name__})"}
    log.info("issue run read tenant=%s issue=%s outcome=ok", tenant_id, ref.short)
    return issue_read_from_preview(read, ctx.now()), None


# --------------------------------------------------------------------------
# what the issue said at submission
# --------------------------------------------------------------------------

def _read_issue(
    ctx: AppContext, auth: AuthContext, tenant_id: str, ref
) -> tuple[dict | None, dict | None]:
    """The issue as the preview reads it, for the run to keep; or why it could not be read.

    THE SAME READ AS `GET /v1/issues/preview` (lane U9 item 4): the caller's
    tenant's forge token, `forge.preview`'s masking and body bound. The token
    lives inside `preview` only; nothing about it is returned or logged here.
    A failure does not refuse the run -- the planner reads the issue itself,
    on the worker -- it is kept as `issue_read_error`, so the run page says
    why its card is empty instead of pretending the issue had nothing in it.
    """
    if ctx.forge_tokens is None or ctx.forge is None:
        return None, {"code": "read_failed", "message": "this API has no forge reader configured"}
    tenant = ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=auth.tenant_principal or auth.email,
        created_at=ctx.now(),
    )
    try:
        read = preview(ref, tenant, tokens=ctx.forge_tokens, issues=ctx.forge)
    except ForgeReadError as refused:
        log.info("issue run read tenant=%s issue=%s outcome=%s", tenant_id, ref.short, refused.code)
        return None, {"code": refused.code, "message": refused.message}
    except Exception as exc:  # noqa: BLE001 -- the run is created either way
        # The type only: an exception's text can quote a request.
        log.warning("issue run read tenant=%s issue=%s failed (%s)", tenant_id, ref.short, type(exc).__name__)
        return None, {"code": "read_failed", "message": f"the issue could not be read ({type(exc).__name__})"}
    log.info("issue run read tenant=%s issue=%s outcome=ok", tenant_id, ref.short)
    return issue_read_from_preview(read, ctx.now()), None


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
    # The repository's open work, read with THIS caller's tenant's forge
    # token (invariant 9) before anything is created: a 403 or 404 refuses
    # the run (owner decision, "a forge 403 or 404 fails the planner") with
    # the preview's codes, and so does a list GitHub would not serve, because
    # a plan made blind to the work in flight is what #454 exists to stop.
    # `tenant_for` is the create path's tenant resolution, the one
    # `submit_tasks` runs again below.
    tenant = ctx.submissions.tenant_for(auth)
    try:
        open_work = read_open_work(
            ref, tenant, tokens=ctx.forge_tokens, issues=ctx.forge, read_at=ctx.now()
        )
    except ForgeReadError as refused:
        log.info(
            "issue run refused tenant=%s issue=%s open_work=%s",
            tenant.tenant_id, ref.short, refused.code,
        )
        raise
    log.info(
        "issue run open work tenant=%s issue=%s issues=%d pull_requests=%d",
        tenant.tenant_id, ref.short, len(open_work["issues"]), len(open_work["pull_requests"]),
    )
    run_id = new_id("run")
    # The planner first, carrying the run's id: a planner without its run is
    # one finished task nobody reads, while a run without its planner would
    # sit PLANNING for ever.
    submission = ctx.submissions.submit_tasks(auth, [planner_task(ref, run_id, open_work)])
    planner = submission.tasks[0]
    # After the planner, in the tenant `submit_tasks` resolved: what the run
    # page shows under "Read from the issue".
    issue_read, issue_read_error = _read_issue(ctx, auth, planner.tenant_id, ref)
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
            open_work=open_work,
            issue_read=issue_read,
            issue_read_error=issue_read_error,
        )
    )
    response.headers["Location"] = f"/v1/runs/{run.id}"
    return {"run": sync_issue(ctx, run).to_api()}


@router.get("")
def list_runs(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    rows, next_token = _runs(ctx).list(
        tenant_id, limit=paged_limit(ctx, limit), page_token=page_token
    )
    served = []
    for run in rows:
        if run.state not in TERMINAL_RUN_STATES:
            try:
                run = advance_run(ctx, tenant_id, run)
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
    ctx: AppContext = Depends(get_context),
) -> dict:
    run = _runs(ctx).get(tenant_id, run_id)
    return {"run": advance_run(ctx, tenant_id, run).to_api()}


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
    try:
        run = _approve(ctx, auth, tenant_id, run, digest=body.plan_digest, by=auth.email)
    except ApiError:
        # A submission refused after the claim left the run FAILED: the
        # issue is told before the caller is. The caller's error is the one
        # raised, whatever this read does.
        try:
            sync_issue(ctx, _runs(ctx).get(tenant_id, run_id))
        except ApiError:
            pass
        raise
    return {"run": sync_issue(ctx, run).to_api()}


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
    return {"run": sync_issue(ctx, run).to_api()}


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
    return {"run": sync_issue(ctx, run).to_api()}
