"""Workflow routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from ..auth import AuthContext
from ..codec import workflow_to_api
from ..deps import AppContext, current_auth, get_context, paged_limit
from ..schemas import WorkflowCreate

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workflow(
    body: WorkflowCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    workflow = ctx.submissions.submit_workflow(auth, body)
    response.headers["Location"] = f"/v1/workflows/{workflow.workflow_id}"
    return {"workflow": workflow_to_api(workflow)}


@router.get("")
def list_workflows(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    page = ctx.store.list_workflows(
        auth.tenant_id, limit=paged_limit(ctx, limit), page_token=page_token
    )
    return {
        "workflows": [workflow_to_api(w) for w in page.items],
        "next_page_token": page.next_page_token,
        "tenant_id": auth.tenant_id,
    }


@router.get("/{workflow_id}")
def get_workflow(
    workflow_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    workflow = ctx.store.get_workflow(auth.tenant_id, workflow_id)
    tasks = ctx.store.list_tasks(auth.tenant_id, workflow_id=workflow_id, limit=200)
    from ..codec import task_to_api

    return {
        "workflow": workflow_to_api(workflow),
        "tasks": [task_to_api(t) for t in tasks.items],
    }


@router.post("/{workflow_id}/cancel")
def cancel_workflow(
    workflow_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.store.cancel_workflow(auth.tenant_id, workflow_id, by=auth.email)
