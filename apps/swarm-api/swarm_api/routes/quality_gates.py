"""A workspace's quality gates (JEV-LANES).

    GET /v1/quality-gates                          the caller's selected
                                                   workspace's switches
    GET /v1/workspaces/{tenant_id}/quality-gates   the switches, their defaults
                                                   and whether the caller may
                                                   change them
    PUT /v1/workspaces/{tenant_id}/quality-gates   `{jev}`, a bool: its owner or
                                                   admin, or a platform admin;
                                                   audited

`jev` on makes the workspace's claude-code lanes run jev-belay and
jev-enforce with the workspace's TypeSafe key (`qualitygates`, which says
where it is stored, who may change it, why, and how the block is signed:
every PUT stores its MAC under the `swarm-settings-key`). Nothing here takes or
returns a credential, an image, a command or a URL (invariant 10).

No worker asks this service whether a stored "on" is signed: the scheduler,
which holds the same key, verifies the block at dispatch and hands the worker
its verdict in the job's environment (`scheduler.judgment.quality_gates_env`,
the owner's pick on PR 2955).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..judgment import settings_mac
from ..qualitygates import (
    QualityGatesPut,
    QualityGatesStore,
    authorize_read,
    authorize_write,
    can_edit,
    view,
)

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

