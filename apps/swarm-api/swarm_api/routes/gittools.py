"""The runner git tools' routes (docs/runner-git-tools.md §1.3, §4; lane G5 of #1225).

`POST /v1/attempts/{attempt_id}/git/{tool}` is WORKER-ONLY, like
`/v1/attempts/{attempt_id}/child-key`: it declares neither `current_auth` nor
`tenant_scope`, because no person may call it and the tenant is the worker's
ID token checked against the body, then the attempt key. The handler only
reads the RAW body (the attempt proof signs its exact bytes) and hands it to
`swarm_api.gittools`, which holds every check.

`GET /v1/tasks/{task_id}/git-calls` is the audit's read: a tenant member's,
scoped like the task itself, 404 for a task that is not the caller's.

There is no `/git/token` route: the owner answered Q2 with A (2026-10-13),
so no runner is ever handed a forge token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context, submission_scope, tenant_scope
from ..gittools import GitTools
from .children import _presented, _worker_verifier

router = APIRouter(tags=["git-tools"])


def git_tools(ctx: AppContext = Depends(get_context)) -> GitTools:
    return GitTools.from_context(ctx, verifier=_worker_verifier(ctx))


@router.post("/v1/attempts/{attempt_id}/git/{tool}")
async def git_tool(
    attempt_id: str,
    tool: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: GitTools = Depends(git_tools),
) -> dict:
    raw = await request.body()
    return await run_in_threadpool(
        lambda: service.handle(
            tool,
            attempt_id,
            raw,
            authorization=_presented(authorization, serverless_authorization),
            proof=proof,
            timestamp=timestamp,
            path=request.url.path,
        )
    )


@router.get("/v1/tasks/{task_id}/git-calls")
def git_calls(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    service: GitTools = Depends(git_tools),
) -> dict:
    calls = service.calls_of(tenant_id, task_id, submitted_by=submitted_by)
    return {"task_id": task_id, "tenant_id": tenant_id, "calls": calls, "count": len(calls)}
