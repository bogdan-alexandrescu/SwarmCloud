"""The worker-only forge-credential route (docs/design/user-scoped-secrets.md §3.2).

Like the child routes it declares neither `current_auth` nor `tenant_scope`:
no person may call it. The tenant comes from the worker's ID token, the
worker from its attempt proof, and the slot from the signed task -- never
from the body, which names no secret. `swarm_api.credbroker` holds every
check; this handler reads the raw body (the proof signs its exact bytes) and
answers with `Cache-Control: no-store`, so no cache between the worker and
swarm-api keeps the token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..credbroker import ForgeCredentialBroker
from ..deps import AppContext, get_context
from .children import _presented, child_service

router = APIRouter(tags=["credentials"])


def credential_broker(ctx: AppContext = Depends(get_context)) -> ForgeCredentialBroker:
    return ForgeCredentialBroker(
        settings=ctx.settings,
        db=ctx.db,
        store=ctx.store,
        children=child_service(ctx),
        grant_mode=ctx.submissions._grant_mode,
        tokens=ctx.forge_tokens,
        now=ctx.now,
    )


@router.post("/v1/attempts/forge-credential")
async def forge_credential(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    broker: ForgeCredentialBroker = Depends(credential_broker),
) -> JSONResponse:
    raw = await request.body()
    released = await run_in_threadpool(
        lambda: broker.release(
            _presented(authorization, serverless_authorization),
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    return JSONResponse(
        {"secret": released.secret, "token": released.token},
        headers={"Cache-Control": "no-store"},
    )
