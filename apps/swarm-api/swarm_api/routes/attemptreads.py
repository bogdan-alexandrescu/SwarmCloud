"""The agent's state reads, worker-relayed (docs/design/agent-autonomy.md §3.4, §3.5;
lane AU-4 STATE-READS, part of #1933).

`GET /v1/attempts/{attempt_id}/reads/{tool}`: what the `swarm-state` MCP
server's tools read, relayed by the worker with the `swarm-state-req/v1`
proof. Like the state routes (`routes/attemptstate.py`), it declares neither
`current_auth` nor `tenant_scope`: no person calls it, the tenant is the
worker's ID token checked against the tuple, and every read is scoped to the
LEASE's tenant (`swarm_api.attemptreads`, which holds every check).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request
from starlette.concurrency import run_in_threadpool

from ..attemptreads import AttemptReads
from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context
from .children import _presented, _worker_verifier

router = APIRouter(tags=["attempt-reads"])

ROUTE = "/v1/attempts/{attempt_id}/reads/{tool}"


def attempt_reads(ctx: AppContext = Depends(get_context)) -> AttemptReads:
    return AttemptReads.from_context(ctx, verifier=_worker_verifier(ctx))


@router.get(ROUTE)
async def attempt_read(
    attempt_id: str,
    tool: str,
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=64),
    task_id: str = Query(min_length=1, max_length=128),
    lease_id: str = Query(min_length=1, max_length=128),
    generation: int = Query(ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: AttemptReads = Depends(attempt_reads),
) -> dict:
    # The query string is signed with the path, so neither the tuple nor a
    # tool argument can be swapped under a captured proof.
    query = request.url.query
    signed_path = request.url.path + (f"?{query}" if query else "")
    params = list(request.query_params.multi_items())
    return await run_in_threadpool(
        lambda: service.read(
            tool, attempt_id, params=params, tenant_id=tenant_id, task_id=task_id,
            lease_id=lease_id, generation=generation,
            authorization=_presented(authorization, serverless_authorization),
            proof=proof, timestamp=timestamp, signed_path=signed_path,
        )
    )
