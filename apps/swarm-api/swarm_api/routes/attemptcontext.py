"""The in-flight route (docs/design/context-quality.md §4.2 layer I, §4.4; lane CTXQ-I).

`POST /v1/attempts/{attempt_id}/context/inflight` is WORKER-ONLY, like the git
tools' `/v1/attempts/{attempt_id}/git/{tool}`: it declares neither
`current_auth` nor `tenant_scope`, because no person may call it and the
tenant is the worker's ID token checked against the body, then the attempt
key. The handler only reads the RAW body (the attempt proof signs its exact
bytes) and hands it to `swarm_api.attemptcontext`, which holds every check.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..attemptcontext import AttemptContext
from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context
from .children import _presented, _worker_verifier

router = APIRouter(tags=["attempt-context"])


def attempt_context(ctx: AppContext = Depends(get_context)) -> AttemptContext:
    return AttemptContext.from_context(ctx, verifier=_worker_verifier(ctx))


@router.post("/v1/attempts/{attempt_id}/context/inflight")
async def in_flight(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: AttemptContext = Depends(attempt_context),
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
