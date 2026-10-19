"""The stopped-run price route (#1481): `swarm_api.spendestimate`.

`POST /v1/attempts/{attempt_id}/spend-estimate` is WORKER-ONLY, like the
in-flight route: it declares neither `current_auth` nor `tenant_scope`,
because no person may call it and the tenant is the worker's ID token checked
against the body, then the attempt key. The handler only reads the RAW body
(the attempt proof signs its exact bytes) and hands it to
`swarm_api.spendestimate`, which holds every check.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context
from ..spendestimate import SpendEstimate
from .children import _presented, _worker_verifier

router = APIRouter(tags=["spend-estimate"])


def spend_estimate(ctx: AppContext = Depends(get_context)) -> SpendEstimate:
    return SpendEstimate.from_context(ctx, verifier=_worker_verifier(ctx))


@router.post("/v1/attempts/{attempt_id}/spend-estimate")
async def estimate(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: SpendEstimate = Depends(spend_estimate),
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
