"""A workspace's visual-QA settings (QA-FLOWS-CAP, part of #2136).

    GET /v1/qa-settings                          the caller's selected workspace's
                                                 settings (what the console reads)
    GET /v1/workspaces/{tenant_id}/qa-settings   the settings, their defaults and
                                                 bounds, and whether the caller may
                                                 change them
    PUT /v1/workspaces/{tenant_id}/qa-settings   `{qa_flows_per_step}`, 1-30: its
                                                 owner or admin, or a platform
                                                 admin; audited

`qa_flows_per_step` is how many flows one live-browser step of a QA run
runs when the run names no `flows_per_step` of its own (`qasettings`, which
says where it is stored, who may change it and why it is read range-checked).
Nothing here takes or returns a credential, an image, a command or a URL
(invariant 10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..qasettings import QaSettingsPut, QaSettingsStore, authorize_read, authorize_write, can_edit, view

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> QaSettingsStore:
    return QaSettingsStore(ctx.db, now=ctx.now)


def _answer(ctx: AppContext, auth: AuthContext, tenant: Tenant, store: QaSettingsStore) -> dict:
    return view(tenant.tenant_id, store.document(tenant.tenant_id), editable=can_edit(auth, tenant, ctx.roles))


@router.get("/v1/qa-settings")
def get_selected_qa_settings(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's settings: the same answer as the path form."""
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.get("/v1/workspaces/{tenant_id}/qa-settings")
def get_qa_settings(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/qa-settings")
def put_qa_settings(
    tenant_id: str,
    body: QaSettingsPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_write(auth, ctx.store.get_tenant(tenant_id), tenant_id, ctx.roles)
    store = _store(ctx)
    store.put(tenant_id, body.qa_flows_per_step, by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)
