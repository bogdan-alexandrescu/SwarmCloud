"""Workflow routes.

The tenant on every read comes from `tenant_scope`, not from `auth.tenant_id`:
a tenant id is derived from the verified identity but is not unique to one, so
the raw string filters by a value two unrelated principals can both hold. See
`swarm_api.deps.tenant_scope`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from ..auth import AuthContext
from ..codec import task_to_api, workflow_dispatch, workflow_to_api
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..schemas import WorkflowCreate

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
        "workflow": workflow_to_api(workflow),
        # Echoed so the caller sees what was ACCEPTED rather than what they
        # sent: a submission that named neither field gets the defaults back,
        # and an `integrate` workflow is told which step will open the one PR.
        "dispatch": {
            "strategy": submission.dispatch.strategy,
            "carrier": submission.dispatch.carrier,
            "integrator_step_id": submission.integrator_step_id,
        },
    }


@router.get("")
def list_workflows(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    page = ctx.store.list_workflows(
        tenant_id, limit=paged_limit(ctx, limit), page_token=page_token
    )
    return {
        "workflows": [workflow_to_api(w) for w in page.items],
        "next_page_token": page.next_page_token,
        "tenant_id": tenant_id,
    }


@router.get("/{workflow_id}")
def get_workflow(
    workflow_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    workflow = ctx.store.get_workflow(tenant_id, workflow_id)
    tasks = ctx.store.list_tasks(tenant_id, workflow_id=workflow_id, limit=200)

    return {
        "workflow": workflow_to_api(workflow),
        # Read back off the tasks, which is where the options are stored; the
        # frozen `Workflow` dataclass has no metadata field to hold them. The
        # list route has no equivalent because it loads no tasks.
        "dispatch": workflow_dispatch(tasks.items),
        "tasks": [task_to_api(t) for t in tasks.items],
    }


@router.post("/{workflow_id}/cancel")
def cancel_workflow(
    workflow_id: str,
    # The scope decides whose workflow may be cancelled; `auth` records who did.
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.store.cancel_workflow(tenant_id, workflow_id, by=auth.email)
