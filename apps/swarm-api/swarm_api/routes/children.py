"""The worker-only child routes (docs/design/child-tasks.md §6.1, §6.1a, §6.2).

None of these declares `current_auth` or `tenant_scope`: no person may call
them, and the tenant is taken from the worker's ID token checked against the
body's tenant, then from the attested registration -- never from a group
lookup. `swarm_api.children` holds every check; these handlers only read the
raw body (the attempt proof signs its exact bytes) and hand it over.

People list a parent's children through `GET /v1/tasks?parent_task_id=`, under
the ordinary tenant and submission scope (routes/tasks.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from typing import Any

from ..auth import GoogleTokenVerifier
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..codec import task_to_api
from ..deps import AppContext, get_context

router = APIRouter(tags=["children"])

#: One verifier per worker audience, built on first use: it holds google-auth's
#: transport, which is worth keeping across requests.
_WORKER_VERIFIERS: dict[str, GoogleTokenVerifier] = {}


def _worker_verifier(ctx: AppContext) -> Any:
    """The verifier a worker's ID token is checked with.

    A worker mints its token for SWARM_API_AUDIENCE, the constant custom
    audience terraform gives this service (`local.push_audiences`), because it
    cannot know swarm-api's URL-shaped API_AUDIENCE, which people's clients
    use. So when that audience is configured and the deployment verifies with
    Google, the child routes pin `aud` to it -- never an unpinned check. Without
    it (local development, the tests' static verifier) the app's own verifier
    is used.
    """
    base = ctx.authenticator._verifier
    audience = ctx.settings.child_audience
    if not audience or not isinstance(base, GoogleTokenVerifier):
        return base
    if audience not in _WORKER_VERIFIERS:
        _WORKER_VERIFIERS[audience] = GoogleTokenVerifier(audience, require_audience=True)
    return _WORKER_VERIFIERS[audience]


def child_service(ctx: AppContext = Depends(get_context)) -> ChildService:
    return ChildService(
        settings=ctx.settings,
        db=ctx.db,
        store=ctx.store,
        submissions=ctx.submissions,
        verifier=_worker_verifier(ctx),
        limiter=ctx.limiter,
        metrics=ctx.metrics,
        now=ctx.now,
    )


def _presented(authorization: str | None, serverless: str | None) -> str | None:
    # The caller's original is preferred, as `current_auth` does: Cloud Run may
    # put its own token in Authorization and move the caller's here.
    return serverless or authorization


@router.post("/v1/attempts/{attempt_id}/child-key", status_code=status.HTTP_201_CREATED)
async def register_child_key(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    service: ChildService = Depends(child_service),
) -> dict:
    raw = await request.body()
    return await run_in_threadpool(
        service.register_key,
        _presented(authorization, serverless_authorization),
        attempt_id,
        raw,
    )


@router.post("/v1/tasks/{parent_task_id}/children", status_code=status.HTTP_201_CREATED)
async def submit_child(
    parent_task_id: str,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: ChildService = Depends(child_service),
    ctx: AppContext = Depends(get_context),
) -> dict:
    raw = await request.body()
    answer = await run_in_threadpool(
        lambda: service.submit(
            _presented(authorization, serverless_authorization),
            parent_task_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    if not answer.created:
        response.status_code = status.HTTP_200_OK
    else:
        ctx.submissions._wake("child_submitted", tenant_id=answer.task.tenant_id, count="1")
    return {
        "task": task_to_api(answer.task, console_url=ctx.settings.console_url),
        "created": answer.created,
    }


@router.get("/v1/tasks/{parent_task_id}/children")
async def list_children(
    parent_task_id: str,
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=64),
    attempt_id: str = Query(min_length=1, max_length=128),
    lease_id: str = Query(min_length=1, max_length=128),
    generation: int = Query(ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: ChildService = Depends(child_service),
) -> dict:
    # The query string is signed with the path, so the tuple cannot be swapped
    # under a captured proof.
    query = request.url.query
    signed_path = request.url.path + (f"?{query}" if query else "")
    children = await run_in_threadpool(
        lambda: service.list_children(
            _presented(authorization, serverless_authorization),
            parent_task_id,
            tenant_id=tenant_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            path=signed_path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    return {"children": children, "count": len(children)}
