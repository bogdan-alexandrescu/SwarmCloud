"""The search tool's routes (docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

`POST /v1/attempts/{attempt_id}/search` is WORKER-ONLY, like the git tools'
`/v1/attempts/{attempt_id}/git/{tool}`: it declares neither `current_auth` nor
`tenant_scope`, because no person may call it and the tenant is the worker's
ID token checked against the body, then the attempt key. The handler only
reads the RAW body (the attempt proof signs its exact bytes) and hands it to
`swarm_api.search`, which holds every check. A 404 while
SEARCH_TOOL_ENABLED is off.

`GET /v1/tasks/{task_id}/search-calls` is the audit's read: a tenant member's,
scoped like the task itself, 404 for a task that is not the caller's.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context, submission_scope, tenant_scope
from ..search import SearchTool
from .children import _presented, _worker_verifier

router = APIRouter(tags=["search-tool"])


def search_tool(ctx: AppContext = Depends(get_context)) -> SearchTool:
    return SearchTool.from_context(ctx, verifier=_worker_verifier(ctx))


@router.post("/v1/attempts/{attempt_id}/search")
async def search(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: SearchTool = Depends(search_tool),
) -> dict:
    raw = await request.body()
    return await run_in_threadpool(
        lambda: service.handle(
            attempt_id,
            raw,
            authorization=_presented(authorization, serverless_authorization),
            proof=proof,
            timestamp=timestamp,
            path=request.url.path,
        )
    )


@router.get("/v1/tasks/{task_id}/search-calls")
def search_calls(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    service: SearchTool = Depends(search_tool),
) -> dict:
    calls = service.calls_of(tenant_id, task_id, submitted_by=submitted_by)
    return {"task_id": task_id, "tenant_id": tenant_id, "calls": calls, "count": len(calls)}
