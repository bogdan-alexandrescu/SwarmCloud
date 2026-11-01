"""A workspace's quality gates (JEV-LANES).

    GET /v1/quality-gates                          the caller's selected
                                                   workspace's switches
    GET /v1/workspaces/{tenant_id}/quality-gates   the switches, their defaults
                                                   and whether the caller may
                                                   change them
    PUT /v1/workspaces/{tenant_id}/quality-gates   `{jev}`, a bool: its owner or
                                                   admin, or a platform admin;
                                                   audited

    POST /v1/attempts/{attempt_id}/quality-gates   WORKER-ONLY: whether this
                                                   attempt's workspace turned
                                                   the gates on, by a block
                                                   whose MAC verifies

`jev` on makes the workspace's claude-code lanes run jev-belay and
jev-enforce with the workspace's TypeSafe key (`qualitygates`, which says
where it is stored, who may change it, why, and how the block is signed:
every PUT stores its MAC under the `swarm-settings-key`). Nothing here takes or
returns a credential, an image, a command or a URL (invariant 10).

The worker-only route declares neither `current_auth` nor `tenant_scope`, as
the context routes beside it do (`routes.attemptcontext`): no person may
call it. Its body is the attempt's tuple and nothing else; the worker's ID
token must be its tenant's, the attempt key's proof must sign the exact
bytes, and the attempt must still hold its live lease (409 otherwise,
invariant 5). The tenant whose block is read is the proven tuple's, so a
worker learns only its own workspace's verdict (invariant 9).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from google.api_core import exceptions as gexc
from starlette.concurrency import run_in_threadpool

from swarm_common.models import Tenant

from ..attemptcontext import (
    MAX_BODY_BYTES,
    AttemptContext,
    ContextRefused,
    GraphContextBody,
    spec_refusal,
)
from ..attemptstate import StateUnavailable
from ..auth import AuthContext
from ..childkey import AttemptTuple
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, parse_body
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..judgment import settings_mac
from ..qualitygates import (
    QualityGatesPut,
    QualityGatesStore,
    authorize_read,
    authorize_write,
    can_edit,
    view,
    worker_verdict,
)
from .attemptcontext import attempt_context
from .children import _presented

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> QualityGatesStore:
    return QualityGatesStore(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _answer(ctx: AppContext, auth: AuthContext, tenant: Tenant, store: QualityGatesStore) -> dict:
    return view(tenant.tenant_id, store.document(tenant.tenant_id),
                editable=can_edit(auth, tenant, ctx.roles), mac=store.mac)


@router.get("/v1/quality-gates")
def get_selected_quality_gates(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's switches: the same answer as the path form."""
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.get("/v1/workspaces/{tenant_id}/quality-gates")
def get_quality_gates(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/quality-gates")
def put_quality_gates(
    tenant_id: str,
    body: QualityGatesPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_write(auth, ctx.store.get_tenant(tenant_id), tenant_id, ctx.roles)
    store = _store(ctx)
    store.put(tenant_id, body.jev, by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)


def attempt_verdict(service: AttemptContext, ctx: AppContext, attempt_id: str, raw: bytes, *,
                    authorization: str | None, proof: str | None, timestamp: str | None,
                    path: str) -> dict[str, Any]:
    """Who, the fence, the signed spec, then the attempt's own tenant's
    verdict: the context routes' order (`AttemptContext.handle_graph`)."""
    service.children._require_configured()
    if len(raw) > MAX_BODY_BYTES:
        raise ContextRefused(f"a quality gates request is at most {MAX_BODY_BYTES} bytes")
    # The attempt's tuple and nothing else (extra="forbid"), exactly what
    # `agent_worker.qualitygates.GatesRoute` sends.
    body: GraphContextBody = parse_body(GraphContextBody, raw)
    attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                           body.generation)
    service._prove(body, attempt, raw=raw, authorization=authorization, proof=proof,
                   timestamp=timestamp, path=path)
    task = service._fence(attempt)
    unverified = spec_refusal(service.settings, task, attempt.task_id)
    if unverified:
        raise ContextRefused(f"spec_unverified: {unverified}")
    store = _store(ctx)
    try:
        doc = store.document(attempt.tenant_id)
    except gexc.GoogleAPIError as exc:
        raise StateUnavailable(
            f"the state store did not answer ({type(exc).__name__}); retry") from None
    return {"ok": True, **worker_verdict(attempt.tenant_id, doc, store.mac)}


@router.post("/v1/attempts/{attempt_id}/quality-gates")
async def post_attempt_quality_gates(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: AttemptContext = Depends(attempt_context),
    ctx: AppContext = Depends(get_context),
) -> dict:
    raw = await request.body()
    return await run_in_threadpool(
        lambda: attempt_verdict(
            service, ctx, attempt_id, raw,
            authorization=_presented(authorization, serverless_authorization),
            proof=proof,
            timestamp=timestamp,
            path=request.url.path,
        )
    )
