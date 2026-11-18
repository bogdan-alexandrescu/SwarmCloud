"""A workspace's allowed agent plugins (docs/design/agent-plugins.md).

    GET /v1/plugins                            the caller's selected
                                               workspace's allowed plugins
    GET /v1/workspaces/{tenant_id}/plugins     the allowed plugins, the
                                               catalogue, and whether the
                                               caller may change them
    PUT /v1/workspaces/{tenant_id}/plugins     `{allowed}`: catalogue names;
                                               its owner or admin, or a
                                               platform admin; audited

A step names plugins from the platform's closed catalogue in its work
contract (`capabilities.plugins`), and submission accepts only those the
workspace allowed here (`swarm_api.plugins.check_step`). Every PUT stores the
block's MAC under the `swarm-settings-key`, and a step's plugins are accepted
only under a block that verifies. Nothing here takes or returns a credential,
an image, a command, a path or a URL (invariant 10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..judgment import settings_mac
from ..plugins import (
    PluginsPut,
    PluginsStore,
    authorize_read,
    authorize_write,
    can_edit,
    view,
)

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> PluginsStore:
    return PluginsStore(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _answer(ctx: AppContext, auth: AuthContext, tenant: Tenant, store: PluginsStore) -> dict:
    return view(tenant.tenant_id, store.document(tenant.tenant_id),
                editable=can_edit(auth, tenant, ctx.roles), mac=store.mac)


@router.get("/v1/plugins")
def get_selected_plugins(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's plugins: the same answer as the path form."""
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.get("/v1/workspaces/{tenant_id}/plugins")
def get_plugins(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/plugins")
def put_plugins(
    tenant_id: str,
    body: PluginsPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_write(auth, ctx.store.get_tenant(tenant_id), tenant_id, ctx.roles)
    store = _store(ctx)
    store.put(tenant_id, body.allowed, by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)
