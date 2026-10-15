"""The TypeSafe forwarder's attempt ticket (docs/design/workspace-secrets.md
§3.5; lane SEC-INJECT-A, part of #1347). One worker-only route; the service is
swarm_api.typesafe_ticket."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, status
from starlette.concurrency import run_in_threadpool

from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..deps import AppContext, get_context
from ..typesafe_ticket import TypesafeTicketService
from .children import child_service

router = APIRouter(prefix="/v1", tags=["typesafe"])


@router.post("/attempts/{attempt_id}/typesafe-ticket", status_code=status.HTTP_201_CREATED)
async def mint_typesafe_ticket(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    ctx: AppContext = Depends(get_context),
    children: ChildService = Depends(child_service),
) -> dict:
    """WORKER ONLY: the ticket the TypeSafe forwarder spends a key on, for
    this attempt alone. It names the key swarm-api resolved (the submitter's
    own, else the tenant's) by secret id and version, never its value. 403
    `typesafe_backend_off` when the workspace did not choose TypeSafe, 403
    `typesafe_no_key` when no key is set, 409 `typesafe_fenced` for a stale
    generation; nothing is minted on any of them."""
    raw = await request.body()
    service = TypesafeTicketService(settings=ctx.settings, db=ctx.db, store=ctx.store,
                                    children=children, now=ctx.now)
    return await run_in_threadpool(
        lambda: service.mint(
            serverless_authorization or authorization,
            attempt_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
