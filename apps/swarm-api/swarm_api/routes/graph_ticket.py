"""The shared graph service's attempt ticket (docs/design/graph-service.md
§3.1-§3.2; lane GS-TICKET, part of #1384). One worker-only route; the service
is swarm_api.graph_ticket."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..deps import AppContext, get_context
from ..graph_ticket import GraphTicketService
from .children import child_service

router = APIRouter(prefix="/v1", tags=["graph"])


@router.post("/attempts/{attempt_id}/graph-ticket")
async def mint_graph_ticket(
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
    """WORKER ONLY: the ticket the graph service reads this attempt's graph
    with. It pins the tenant, the task's own repository, the generation, the
    body's `index_sha` and the manifest digest swarm-api recorded at
    promotion. 403 with `detail.reason` `context_disabled` when the stored
    setting is off, 404 `no_graph` when the commit has no promoted graph, 409
    for a stale generation; nothing is minted on any of them."""
    raw = await request.body()
    service = GraphTicketService(settings=ctx.settings, db=ctx.db, children=children,
                                 now=ctx.now)
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
