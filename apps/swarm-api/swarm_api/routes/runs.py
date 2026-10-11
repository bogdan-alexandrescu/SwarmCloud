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
from .. import approvals
from ..issueruns import (
    AUTO_APPROVER,
    WORKFLOWS_PATH,
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
    parse_edited_plan,
    parse_planner_output,
    plan_digest,
    plan_files,
    plan_hold,
    planner_task,
    auto_merge_default,
    refuse_auto_merge,
    territory_overlap,
    widen_hold,
    workflow_paths,
)
from ..issuesync import sync_issue
from ..repositories import (
    Repositories,
    hard_stop_paths_of,
    platform_of,
    registered_merge_policy,
    repo_id_for,
)
from ..plancontext import read_plan_context
from ..reviewcontext import read_review_context
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

def _read_plan(ctx: AppContext, tenant_id: str, task_id: str) -> tuple[str, dict]:
    """`plan.json` out of the planner's artifacts, through the API's own masked
    reader: `("plan", plan)`, or `("not_ready", verdict)` when the planner
    found the issue not ready (`issueruns.parse_planner_output`)."""
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
    return parse_planner_output(window.get("content") or "")


def workflows_refusal(paths: list[str]) -> str:
    """The run's `error` when its plan names `.github/workflows/` files (§4.4)."""
    return (
        f"{WORKFLOWS_PATH}: the plan changes {', '.join(paths[:10])}. SwarmCloud's forge "
        "credential cannot push workflow files, so this change must be made by a person or "
        "split out of the issue"
    )


def _registration(ctx: AppContext, tenant_id: str, run: IssueRun) -> dict:
    """The run's repository as its OWN tenant registered it, or {} (unregistered)."""
    ref = run.issue
    return Repositories(ctx.db, now=ctx.now).find(tenant_id, repo_id_for(tenant_id, ref.owner, ref.repo)) or {}


def hard_stops_at_plan(ctx: AppContext, tenant_id: str, run: IssueRun, plan: dict) -> dict | None:
    """The §4.4 hold a plan puts on its run: IAM and bootstrap anywhere, the
    frozen contract in a `platform: true` repository, the registration's
    protected paths, and a security-class issue. Widened over the run's
    current hold, never narrower (`widen_hold`)."""
    record = _registration(ctx, tenant_id, run)
    found = plan_hold(
        plan, platform=platform_of(record), hard_stop_paths=hard_stop_paths_of(record),
        issue_read=run.issue_read, at=ctx.now(),
    )
    hold = widen_hold(run.approval_hold, found)
    if hold and hold != run.approval_hold:
        log.info(
            "issue run %s tenant=%s held %s (%s)", run.id, tenant_id, hold["code"],
            ", ".join(hold.get("reasons") or []),
        )
        approvals.announce_hold(run, run.approval_hold, hold)
    return hold


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
            kind, plan = _read_plan(ctx, tenant_id, task.id)
        except InvalidPlan as refused:
            return runs.transition(
                tenant_id, run.id, RunState.FAILED, by=SYSTEM,
                patch={"error": failure_text(refused.message)},
            )
        except UpstreamUnavailable:
            # The bucket, not the plan: the next read tries again.
            log.warning("issue run %s: the planner's %s could not be read yet", run.id, PLAN_FILE)
            return run
        if kind == "not_ready":
            # No plan, so nothing to approve and nothing submitted: the run
            # ends here holding nothing (invariant 1), and its one status
            # comment carries the reason and the needs (`sync_issue`).
            log.info(
                "issue run %s tenant=%s: the planner found %s not ready (%s)",
                run.id, tenant_id, run.issue.short, plan.get("kind") or "unspecified",
            )
            return runs.transition(
                tenant_id, run.id, RunState.NOT_READY, by=SYSTEM, patch={"not_ready": plan},
            )
        refused = workflow_paths(plan)
        if refused:
            # §4.4: refused, not held. The forge credential cannot push these
            # files, so the step would fail after spending. No plan is
            # stored, no workflow submitted, no re-plan attempted; the status
            # comment carries this error.
            log.info("issue run %s tenant=%s: plan names workflow files; refused (%s)",
                     run.id, tenant_id, WORKFLOWS_PATH)
            return runs.transition(
                tenant_id, run.id, RunState.FAILED, by=SYSTEM,
                patch={"error": failure_text(workflows_refusal(refused))},
            )
        return runs.transition(
            tenant_id, run.id, RunState.PLANNED, by=SYSTEM,
            patch={"plan": plan, "plan_digest": plan_digest(plan), "plan_revision": 1,
                   "approval_hold": hard_stops_at_plan(ctx, tenant_id, run, plan)},
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
    ctx: AppContext, auth: AuthContext, tenant_id: str, run: IssueRun, *, digest: str, by: str,
    confirm: str | None = None,
) -> IssueRun:
    """APPROVED under the digest check, then the workflow, then RUNNING.

    The plan is compiled BEFORE the claim, so a plan the compiler refuses
    leaves the run PLANNED for an edit, and AGAIN from the claimed document,
    so what is submitted is the plan whose digest the transaction matched.

    THE ONE FUNCTION EVERY APPROVAL GOES THROUGH, so the §4.4 hold is checked
    here (docs/schedules.md §4.5): an approver the hold does not admit is 403
    `hold_approver_required`, and an auto-approval (`by: AUTO_APPROVER`)
    never satisfies one. Checked before the claim, and again inside it
    against the document the claim read, so a hold set between the two is
    not stepped round.
    """
    runs = _runs(ctx)
    is_owner = bool(getattr(auth, "is_owner", False)) and by != AUTO_APPROVER
    approvals.check_hold(ctx, run, email=by, is_owner=is_owner, confirm=confirm)
    # §4.6: a scheduled run's named / owner_only approvers hold on EVERY
    # approval path (this route, `sc plan approve`, the inbox), not only the
    # inbox's. A blocking hold is judged by `check_hold` alone; an
    # auto-approval is the gate's own `auto` mode and is not a person.
    schedule_ref = run.schedule or {}
    if schedule_ref and by != AUTO_APPROVER and not approvals.hold_blocks(run.approval_hold):
        schedule = approvals.read_schedule(ctx.db, tenant_id, schedule_ref.get("schedule_id"))
        if schedule is not None:
            approvals.check_gate_approver(
                ctx, tenant_id, (schedule.get("gate") or {}).get("approvers"), email=by,
                is_owner=is_owner, schedule=schedule, merge_tier=False,
            )
    compile_plan(run)
    # docs/workspaces.md §5.3 (#847 W1): the submission gate, asked BEFORE the
    # claim, so a run whose submitter's workspace is not ready is refused and
    # stays awaiting approval -- approving again once it is ready works --
    # rather than being claimed and then FAILED by `submit_workflow`'s
    # refusal below. Off (WORKSPACE_GATE), it returns at once.
    ctx.submissions.workspace_gate(auth, ctx.submissions.tenant_for(auth))
    def claim(current: IssueRun) -> dict:
        if current.approval_hold != run.approval_hold:
            approvals.check_hold(ctx, current, email=by, is_owner=is_owner, confirm=confirm)
        return {
            "approved_by": by,
            "approved_at": ctx.now(),
            "approved_digest": current.plan_digest,
        }

    approved = runs.transition(
        tenant_id, run.id, RunState.APPROVED, by=by, digest=digest, from_states=_PLANNED_ONLY,
        patch=claim,
    )
    try:
        # The review's IMPACT block from the run's own tenant's index (lane
        # KG6); None, and today's review prompt, when there is no v3 index.
        review = read_review_context(ctx, tenant_id, approved)
        submission = ctx.submissions.submit_workflow(auth, compile_plan(approved, review))
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
    # A run the sweep created (`created_by: issue-sweep`) names the member it
    # submits as in `on_behalf_of`: its repository registration's creator,
    # held to exactly the same membership check below.
    email = run.on_behalf_of or run.created_by
    if not ctx.authenticator.is_tenant_member(email, tenant):
        log.warning(
            "issue run %s tenant=%s: its creator is no longer a member; nothing submitted",
            run.id, run.tenant_id,
        )
        raise RunOwnerNotMember(
            f"the run's owner {email or '(not recorded)'} is no longer a member of tenant "
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


#: The most live runs the territory guard reads per check: the sweeper's cap
#: is 8 per tenant, and people create runs too.
TERRITORY_SCAN = 200

#: Runs whose plan is being worked: always ahead of a run waiting for approval.
_WORKING = frozenset({RunState.APPROVED, RunState.RUNNING, RunState.CHECKING, RunState.FIXING})


def territory_conflict(
    ctx: AppContext, tenant_id: str, run: IssueRun
) -> tuple[IssueRun, list[str]] | None:
    """The first live run AHEAD of `run` in its repository whose plan names a
    file `run`'s plan does, and the shared paths; or None.

    TWO ISSUES THAT EDIT ONE FILE ARE ONE LANE (CLAUDE.md): with up to eight
    runs a tenant going at once (the sweeper's cap), two of them editing one
    file is the conflict most likely to cost a pull request, so the second
    waits for the first to end.

    "Ahead" is a run being worked (APPROVED, RUNNING, CHECKING, FIXING), or
    an OLDER `auto` run still PLANNED -- the order the tick approves them in.
    That makes the oldest of any overlapping set free to go, so two waiting
    runs never wait for each other. A PLANNED run waiting for a person is not
    ahead: nobody knows when, or whether, it will be approved. A plan whose
    steps name no files cannot be compared and holds nothing. Only the run's
    own tenant's runs are read (invariant 9).
    """
    ours = plan_files(run.plan)
    if not ours:
        return None
    rows, _ = _runs(ctx).live(tenant_id, limit=TERRITORY_SCAN)
    repository = run.issue.repository.lower()
    for other in rows:
        if other.id == run.id or other.issue.repository.lower() != repository:
            continue
        ahead = other.state in _WORKING or (
            other.state == RunState.PLANNED and other.plan_approval == "auto"
            and (other.created_at, other.id) < (run.created_at, run.id)
        )
        if not ahead:
            continue
        shared = territory_overlap(ours, plan_files(other.plan))
        if shared:
            return other, shared
    return None


def _territory_hold(ctx: AppContext, tenant_id: str, run: IssueRun) -> IssueRun | None:
    """Hold an auto run PLANNED while `territory_conflict` finds one, recording
    why in `hold`; clear a hold that no longer applies. None: approve it now.

    Re-checked on every tick (a PLANNED `auto` run is one the tick visits),
    so the run is approved on the first tick after the run ahead of it ends.
    """
    found = territory_conflict(ctx, tenant_id, run)
    runs = _runs(ctx)
    if found is None:
        if run.hold:
            runs.patch(tenant_id, run.id, {"hold": None})
            run.hold = None
        return None
    other, shared = found
    reason = f"territory_overlap: {other.id}"
    if run.hold != reason:
        log.info(
            "issue run %s tenant=%s waits for %s: both plans edit %s",
            run.id, tenant_id, other.id, ", ".join(shared[:10]),
        )
        return runs.patch(tenant_id, run.id, {"hold": reason})
    return run


def _advance_state(ctx: AppContext, tenant_id: str, run: IssueRun) -> IssueRun:
    """Move `run` as far as its tasks say it has gone. Never raises for a race."""
    try:
        if run.state == RunState.PLANNING:
            run = _from_planner(ctx, tenant_id, run)
        # Only an `auto` run is ever submitted here: a `required` run stays
        # PLANNED, holding nothing (invariant 1), until a person approves it.
        if run.state == RunState.PLANNED and run.plan_approval == "auto" and run.plan_digest:
            # Not while another live run's plan edits the same files: it
            # waits, PLANNED and holding nothing, and the tick asks again.
            held = _territory_hold(ctx, tenant_id, run)
            if held is not None:
                return held
            if approvals.hold_blocks(run.approval_hold):
                # §4.5: an auto-approval never satisfies a hard-stop hold. The
                # run stays PLANNED, holding nothing (invariant 1), and the
                # inbox shows it as a `kind: hold` item for its approvers.
                return run
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
            except approvals.HoldApproverRequired:
                return run
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
    # §4.7 on the tenant's own tick: expired approvals leave the inbox, and a
    # scheduled plan nobody decided in time is REJECTED with reason "expired".
    # A failure here is logged and never stops the runs below.
    try:
        approvals.expire_due(ctx, tenant_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("approvals of tenant=%s not expired by the tick (%s)", tenant_id, type(exc).__name__)
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
# routes
# --------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
def create_run(
    body: RunCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # Before anything is created: a refused option writes nothing. A run that
    # does not say takes the `merge_policy` its tenant registered the issue's
    # repository with, else the platform's `merge_by_default` (contract
    # request 47, WF-MERGE-API), resolved once the tenant is known and
    # recorded on the run, so a later change to either does not change a run
    # already made.
    if body.auto_merge is not None:
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
    auto_merge = (
        body.auto_merge if body.auto_merge is not None
        else auto_merge_default(
            registered_merge_policy(ctx.db, ctx.now, tenant.tenant_id, ref.owner, ref.repo),
            bool(ctx.store.get_platform_settings().get("merge_by_default")),
        )
    )
    refuse_auto_merge(auto_merge)
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
    run = start_run(
        ctx, auth, ref, open_work=open_work, plan_approval=body.plan_approval,
        auto_merge=auto_merge, fix_rounds=body.fix_rounds,
    )
    response.headers["Location"] = f"/v1/runs/{run.id}"
    return {"run": run.to_api()}


def start_run(
    ctx: AppContext,
    auth: AuthContext,
    ref,
    *,
    open_work: dict,
    plan_approval: str,
    auto_merge: bool,
    fix_rounds: int,
    created_by: str | None = None,
    schedule: dict | None = None,
    merge_approval: str | None = None,
) -> IssueRun:
    """The planner task, then the run document, then the first write-back.

    POST /v1/runs and the issue sweeper (`issuesweep`) both start a run here.
    `auth` is who the planner is submitted as -- the caller, or for the sweep
    the registration's creator -- and it resolves the run's tenant
    (`submit_tasks` runs `tenant_for`). `created_by` is what the run records
    as its creator when that is not `auth` (`issue-sweep`); the member it
    submits as is then kept in `on_behalf_of`, so every later submission is
    made as them, and only while they are still a member (`run_owner_auth`).
    Options are the caller's to have checked (`refuse_auto_merge`).

    A schedule's firing (docs/schedules.md §2.7) passes `schedule`, its
    `metadata.schedule` mark, which `IssueRuns.for_firing` finds again so a
    retried firing adopts the run instead of making a second; and
    `merge_approval="required"` when its gate says `merge: approve`, so the
    run's green pull request waits in the inbox (§4.2).
    """
    if merge_approval not in (None, "required"):
        raise ValueError(f"merge_approval is 'required' or None, not {merge_approval!r}")
    run_id = new_id("run")
    # The issue and the tenant's own index of its repository BEFORE the
    # planner (lane KG1): the issue's words are what the planner's REPO GRAPH
    # section is searched for, and the section is in the planner's prompt.
    # `tenant_for` is the resolution `submit_tasks` runs again below. Neither
    # read refuses the run: a failed issue read is `issue_read_error`, and a
    # missing or unreadable index is today's prompt with `index_context`
    # saying why.
    tenant_id = ctx.submissions.tenant_for(auth).tenant_id
    issue_read, issue_read_error = _read_issue(ctx, auth, tenant_id, ref)
    context = read_plan_context(ctx, tenant_id, ref, run_id=run_id, issue=issue_read,
                                open_work=open_work)
    # The planner next, carrying the run's id: a planner without its run is
    # one finished task nobody reads, while a run without its planner would
    # sit PLANNING for ever.
    submission = ctx.submissions.submit_tasks(
        auth, [planner_task(ref, run_id, open_work, context=context.section)]
    )
    planner = submission.tasks[0]
    now = ctx.now()
    run = _runs(ctx).create(
        IssueRun(
            id=run_id,
            tenant_id=planner.tenant_id,
            created_by=created_by or auth.email,
            created_at=now,
            updated_at=now,
            state=RunState.PLANNING,
            issue=ref,
            plan_approval=plan_approval,
            auto_merge=auto_merge,
            fix_rounds=fix_rounds,
            planner_task_id=planner.id,
            open_work=open_work,
            index_sha=context.index_sha,
            index_digest=context.index_digest,
            index_context=context.record,
            issue_read=issue_read,
            issue_read_error=issue_read_error,
            on_behalf_of=auth.email if created_by else None,
            schedule=dict(schedule) if schedule else None,
            merge_approval=merge_approval,
        )
    )
    return sync_issue(ctx, run)


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
    confirm: str | None = Query(default=None, max_length=2000),
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
        run = _approve(ctx, auth, tenant_id, run, digest=body.plan_digest, by=auth.email,
                       confirm=confirm)
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
    current = runs.get(tenant_id, run_id)  # 404 before 422: another tenant's run is not there
    # An overlap carried over unchanged from a plan stored before
    # `PlanOverlap.action` (#587) may still lack one; any other may not. The
    # transition's digest check refuses the edit if the plan moved since.
    plan = parse_edited_plan(body.plan, current.plan)
    refused = workflow_paths(plan)
    if refused:
        # §4.4: an edit cannot add what the planner's plan would have been
        # refused for.
        raise InvalidPlan(
            f"the plan may not change workflow files ({', '.join(refused[:10])}): "
            "SwarmCloud's forge credential cannot push them",
            detail={"code": WORKFLOWS_PATH, "paths": refused[:10]},
        )
    # An edit can add a hold but never clears one (§4.5): `widen_hold` over
    # the hold the claim reads.
    found = hard_stops_at_plan(ctx, tenant_id, current, plan)
    run = runs.transition(
        tenant_id, run_id, RunState.PLANNED, by=auth.email, digest=body.plan_digest,
        from_states=_PLANNED_ONLY,
        patch=lambda latest: {
            "plan": plan,
            "plan_digest": plan_digest(plan),
            "plan_revision": latest.plan_revision + 1,
            "plan_edited_by": auth.email,
            "approval_hold": widen_hold(latest.approval_hold, found),
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
