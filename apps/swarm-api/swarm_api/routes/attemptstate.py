"""The worker-only state routes (docs/design/tenant-settings-integrity.md §4.2).

`/v1/attempts/{attempt_id}/state/...` carries every read and write a worker
makes once its tenant is switched to the routes (`SWARM_STATE_VIA_API`, lane
ISO-WORKER-CORE); until then nothing calls them. Like the child routes and
`routes/attemptcontext.py`, none declares `current_auth` or `tenant_scope`: no
person may call them, and the tenant is the worker's ID token checked against
the body's tenant, then the attempt key. The handlers only read the RAW body
(the attempt proof signs its exact bytes) and hand it to
`swarm_api.attemptstate`, which holds every check.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Query, Request
from starlette.concurrency import run_in_threadpool

from ..attemptstate import (
    AttemptEndBody,
    AttemptStartBody,
    AttemptState,
    CheckpointBody,
    CiFixBody,
    EventBody,
    FinishBody,
    HeartbeatBody,
    MergeSlotBody,
    MergeTrainBody,
    ParkBody,
    ReleaseBody,
    RetryBody,
    TransitionBody,
    UsageBody,
)
from ..children import PROOF_HEADER, TIMESTAMP_HEADER
from ..deps import AppContext, get_context
from .children import _presented, _worker_verifier

router = APIRouter(tags=["attempt-state"])

PREFIX = "/v1/attempts/{attempt_id}/state"


def attempt_state(ctx: AppContext = Depends(get_context)) -> AttemptState:
    return AttemptState.from_context(ctx, verifier=_worker_verifier(ctx))


@router.post(f"{PREFIX}/open")
async def state_open(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    service: AttemptState = Depends(attempt_state),
) -> dict:
    # The one route without a proof: it registers the key every later proof
    # is checked against, and spends the one-use nonce instead (§4.1).
    raw = await request.body()
    return await run_in_threadpool(
        lambda: service.open(
            attempt_id, raw, authorization=_presented(authorization, serverless_authorization)
        )
    )


#: POST route -> (body model, the service method that runs it).
_POSTS: dict[str, tuple[type, Callable[[AttemptState], Callable[..., dict]]]] = {
    "transition": (TransitionBody, lambda s: s.transition),
    "attempt-start": (AttemptStartBody, lambda s: s.attempt_start),
    "checkpoint": (CheckpointBody, lambda s: s.checkpoint),
    "usage": (UsageBody, lambda s: s.usage),
    "heartbeat": (HeartbeatBody, lambda s: s.heartbeat),
    "park": (ParkBody, lambda s: s.park),
    "finish": (FinishBody, lambda s: s.finish),
    "retry": (RetryBody, lambda s: s.retry),
    "attempt-end": (AttemptEndBody, lambda s: s.attempt_end),
    "release": (ReleaseBody, lambda s: s.release),
    "event": (EventBody, lambda s: s.event),
    "merge-slot": (MergeSlotBody, lambda s: s.merge_slot),
    "merge-train": (MergeTrainBody, lambda s: s.merge_train),
    "ci-fix-tasks": (CiFixBody, lambda s: s.ci_fix_tasks),
}


def _post_route(op: str) -> None:
    model, method = _POSTS[op]

    async def handler(
        attempt_id: str,
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        serverless_authorization: str | None = Header(
            default=None, alias="X-Serverless-Authorization"
        ),
        proof: str | None = Header(default=None, alias=PROOF_HEADER),
        timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
        service: AttemptState = Depends(attempt_state),
    ) -> dict:
        raw = await request.body()

        def _run() -> dict:
            call = service.call(
                model,
                attempt_id,
                raw,
                authorization=_presented(authorization, serverless_authorization),
                proof=proof,
                timestamp=timestamp,
                path=request.url.path,
            )
            return method(service)(call)

        return await run_in_threadpool(_run)

    handler.__name__ = f"state_{op.replace('-', '_')}"
    router.add_api_route(f"{PREFIX}/{op}", handler, methods=["POST"], name=handler.__name__)


for _op in _POSTS:
    _post_route(_op)


def _get_route(op: str, run: Callable[[AttemptState, Any, Request], dict]) -> None:
    async def handler(
        attempt_id: str,
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
        service: AttemptState = Depends(attempt_state),
    ) -> dict:
        # The query string is signed with the path, so the tuple cannot be
        # swapped under a captured proof.
        query = request.url.query
        signed_path = request.url.path + (f"?{query}" if query else "")

        def _run() -> dict:
            attempt = service.call_get(
                attempt_id,
                tenant_id=tenant_id,
                task_id=task_id,
                lease_id=lease_id,
                generation=generation,
                authorization=_presented(authorization, serverless_authorization),
                proof=proof,
                timestamp=timestamp,
                signed_path=signed_path,
            )
            return run(service, attempt, request)

        return await run_in_threadpool(_run)

    handler.__name__ = f"state_{op}"
    router.add_api_route(f"{PREFIX}/{op}", handler, methods=["GET"], name=handler.__name__)


def _upstream(service: AttemptState, attempt: Any, request: Request) -> dict:
    upstream = request.query_params.get("upstream_task_id") or ""
    if not upstream or len(upstream) > 128:
        from ..errors import ValidationFailed

        raise ValidationFailed("upstream_task_id is required")
    return service.upstream(attempt, upstream)


_get_route("poll", lambda s, a, r: s.poll(a, r.query_params.get("provider")))
_get_route("context", lambda s, a, r: s.context(a))
_get_route("upstream", _upstream)
_get_route("repository", lambda s, a, r: s.repository(a))
