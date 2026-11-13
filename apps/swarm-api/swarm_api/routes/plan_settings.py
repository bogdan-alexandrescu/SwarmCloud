"""A workspace's plan-review settings (docs/design/plans-pipeline.md §6.4, §7.8; lane P9).

    GET /v1/workspaces/{tenant_id}/plan-settings   the workspace's choices, what
                                                   each resolves to, and the
                                                   caps, read-only; any member
    PUT /v1/workspaces/{tenant_id}/plan-settings   change `diagrams` or
                                                   `chat_model_class`; its admin,
                                                   audited

Who may read and change it is `plansettings.authorize_read`/`authorize_write`'s:
another workspace's settings are the established 404. The caps are the
platform's (`PUT /v1/admin/settings`): the PUT refuses a `caps` key by name.
Nothing here takes or returns a credential, an image, a command or a resource
spec (invariant 10): the body is two words.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..judgment import settings_mac
from ..plansettings import PlanSettingsPut, PlanSettingsStore, authorize_read, authorize_write, can_edit, view

router = APIRouter(prefix="/v1/workspaces", tags=["workspace"])


def plan_settings_store(ctx: AppContext) -> PlanSettingsStore:
    """The store every plans route builds per request, as the settings MAC's
    readers are built from `ctx.settings`."""
    return PlanSettingsStore(ctx.store.db, now=ctx.now, mac=settings_mac(ctx.settings),
                             platform=ctx.store.get_platform_settings)


@router.get("/{tenant_id}/plan-settings")
def get_plan_settings(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return view(tenant_id, plan_settings_store(ctx),
                editable=can_edit(auth, tenant, ctx.roles))


@router.put("/{tenant_id}/plan-settings")
def put_plan_settings(
    tenant_id: str,
    body: PlanSettingsPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    authorize_write(auth, ctx.store.get_tenant(tenant_id), tenant_id, ctx.roles)
    store = plan_settings_store(ctx)
    store.put(tenant_id, body, by=(auth.email or "").strip().lower())
    return view(tenant_id, store, editable=True)
