"""Workflow routes.

The tenant on every read comes from `tenant_scope`, not from `auth.tenant_id`:
a tenant id is derived from the verified identity but is not unique to one, so
the raw string filters by a value two unrelated principals can both hold. See
`swarm_api.deps.tenant_scope`.

EVERY READ DERIVES. `Workflow.state` in Firestore is a cache of a computation
over the step tasks, and until this change nothing ever refreshed it -- a
workflow read QUEUED for its whole life. Both read routes now compute the state
from the steps, serve that, report whether it agrees with the stored copy, and
write the stored copy back when it does not. See `swarm_api.rollup` for why the
derived value is the one a reader is given.

WRITING ON A GET is deliberate and is the narrow kind: the write fires only when
the two records already disagree, so a healthy platform does zero writes here,
and the disagreement is counted and logged BEFORE the repair (`rollup._record`)
so nothing is resolved without a trace. The alternative -- a background sweep as
the only writer -- would leave every workflow nobody had swept unqueryable, which
is the half of the owner's decision the write exists to satisfy.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from ..auth import AuthContext
from ..children import PARENT_CANCELLED, ChildService
from ..codec import task_to_api, workflow_dispatch, workflow_to_api
from ..task_accounts import accounts_for
from ..deps import (
    AppContext,
    current_auth,
    get_context,
    paged_limit,
    submission_scope,
    tenant_scope,
)
from ..schemas import WorkflowCreate
from ..stalls import stalled_workflows

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workflow(
    body: WorkflowCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    submission = ctx.submissions.submit_workflow(auth, body)
    workflow = submission.workflow
    response.headers["Location"] = f"/v1/workflows/{workflow.workflow_id}"
    return {
        # No rollup on the create response. The step tasks were written
        # microseconds ago by `create_workflow` and deriving over them would
        # spend a read per step to be told what this request just decided. The
        # response says `state_source: "stored"`, which is true and is the point
        # of that field.
        #
        # Each step's input is masked by its own task's masker (the PR #229
        # review): the tasks this request just built carry the workflow's
        # metadata, and the step copy must mask what the task copy masks.
        "workflow": workflow_to_api(
            workflow,
            step_tasks={task.id: task for task in submission.tasks},
            console_url=ctx.settings.console_url,
        ),
        # Echoed so the caller sees what was ACCEPTED rather than what they
        # sent: a submission that named neither field gets the defaults back,
        # and an `integrate` workflow is told which step will open the one PR.
        "dispatch": {
            "strategy": submission.dispatch.strategy,
            "carrier": submission.dispatch.carrier,
            "integrator_step_id": submission.integrator_step_id,
            # Only when set, and as RESOLVED: a continuation of a continuation
            # names the original task, whose branch is the one pushed to.
            **(
                {"continues_task": submission.dispatch.continues}
                if submission.dispatch.continues
                else {}
            ),
        },
    }


@router.get("")
def list_workflows(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    state: list[str] | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One page of the tenant's workflows, newest first.

    `state` (repeatable) keeps only the workflows whose DERIVED state is one of
    those named -- the bridge's `sc workflows` asks for the unfinished ones
    (owner decision 2026-10-02: running workflows show in Claude Code without
    attaching each by id). It filters THIS page after the rollup, because the
    stored state is the cache this module exists not to trust, so a filtered
    page can be short or empty and still carry a `next_page_token`; page on
    until the token is null.
    """
    page = ctx.store.list_workflows(
        tenant_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
        submitted_by=submitted_by,
    )
    results, report = ctx.rollups.for_workflows(tenant_id, page.items)
    if state:
        wanted = set(state)
        results = [r for r in results if r.to_api().get("state") in wanted]
    return {
        # Each step's input is masked by its own task's masker, from the step
        # tasks the rollup already read (the PR #229 review); a step whose task
        # the read budget left unread borrows a sibling's, and a workflow none
        # of whose tasks were read serves its step inputs as null.
        "workflows": [
            workflow_to_api(
                r.workflow,
                r.to_api(),
                step_tasks=r.step_tasks,
                console_url=ctx.settings.console_url,
            )
            for r in results
        ],
        "next_page_token": page.next_page_token,
        "tenant_id": tenant_id,
        # What deriving this page cost and whether it was complete. A caller
        # that saw `step_read_budget_exhausted` knows some rows below read
        # UNKNOWN because this route stopped reading, not because anything is
        # wrong with those workflows.
        "rollup_report": report.to_api(),
        # What the reconciler's workflow stall check last found for this
        # tenant (#616): the console's Overview "Needs a look" and `sc trouble`
        # read it here. `count` is null, with `check_error` saying why, when
        # the check's result is not known -- never zero (swarm_api.stalls).
        "stalled_workflows": stalled_workflows(ctx.store.db, tenant_id),
    }


@router.get("/{workflow_id}")
def get_workflow(
    workflow_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    workflow = ctx.store.get_workflow(tenant_id, workflow_id, submitted_by=submitted_by)
    # The same filter on the step tasks: a workflow the caller submitted has
    # step tasks the caller submitted, and a row that is not theirs is not
    # served whatever workflow it claims to belong to.
    tasks = ctx.store.list_tasks(
        tenant_id, workflow_id=workflow_id, limit=200, submitted_by=submitted_by
    )
    # Derived from the tasks this route already loaded rather than from a second
    # read of the same documents. `complete` is false when that page truncated,
    # which turns a step missing from it into "not read" instead of "not there"
    # -- `max_workflow_steps` is 50 against a limit of 200, so it cannot happen
    # today, and a route that would start lying if the limit ever changed is a
    # route that is already wrong.
    result = ctx.rollups.for_workflow_from_tasks(
        workflow, tasks.items, complete=tasks.next_page_token is None
    )
    accounts = accounts_for(ctx.db, tenant_id, tasks.items)

    return {
        # The step copies of each input are masked by the tasks' own maskers,
        # so `workflow.steps[i].input` and `tasks[i].input` agree (the PR #229
        # review), from the tasks this route already loaded.
        "workflow": workflow_to_api(
            workflow,
            result.to_api(),
            step_tasks={t.id: t for t in tasks.items},
            console_url=ctx.settings.console_url,
        ),
        # Read back off the tasks, which is where the options are stored; the
        # frozen `Workflow` dataclass has no metadata field to hold them. The
        # list route has no equivalent because it loads no tasks.
        "dispatch": workflow_dispatch(tasks.items),
        # #379: each step's account, one bounded query per 30 steps.
        "tasks": [
            task_to_api(t, account=accounts.get(t.id), console_url=ctx.settings.console_url)
            for t in tasks.items
        ],
    }


@router.post("/{workflow_id}/cancel")
def cancel_workflow(
    workflow_id: str,
    # The scope decides whose workflow may be cancelled; `auth` records who did.
    background: BackgroundTasks,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    targets: list = []
    result = ctx.store.cancel_workflow(
        tenant_id, workflow_id, by=auth.email, tenant_member=auth.tenant_member,
        targets=targets,
    )
    # Each running step's execution is asked to stop now, as the task route
    # does (#627); a step cancelled before names none.
    for target in targets:
        background.add_task(ctx.executions.cancel, target)
    # A cancelled step's children are cancelled with it (OD-B15-4,
    # docs/design/child-tasks.md §3.4); the scheduler's sweep makes it certain.
    service = ChildService(
        settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
        verifier=None, now=ctx.now,
    )
    children: list = []
    for task_id in result.get("tasks_cancelled") or []:
        try:
            service.cascade(
                tenant_id, task_id, why=PARENT_CANCELLED, by=auth.email, targets=children
            )
        except Exception:
            log.exception("child cascade of step %s failed; the scheduler sweep retries it", task_id)
    for child_target in children:
        background.add_task(ctx.executions.cancel, child_target)
    return result
