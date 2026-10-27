"""A workspace's per-agent budget (BUDGET-CAP, part of #2459).

    GET /v1/budget                             the caller's selected workspace's
                                               budget (what the console reads)
    GET /v1/workspaces/{tenant_id}/budget      the default, the platform ceiling
                                               it must stay under, the bounds,
                                               and whether the caller may change it
    PUT /v1/workspaces/{tenant_id}/budget      `{budget: {unit, amount}}` or
                                               `{budget: null}` to clear it: its
                                               owner or admin, or a platform
                                               admin; audited

`budget` holds where the default is stored, who may change it, how a step's
own budget may only lower it, and why it is read shape-checked. A default
above the platform ceiling in its unit is refused 422 `budget_above_ceiling`,
naming the ceiling. Nothing here takes or returns a credential, an image, a
command or a URL (invariant 10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..budget import (
    CEILING_SETTING,
    BudgetSettingsPut,
    BudgetStore,
    Limits,
    authorize_write,
    ceiling_of,
    refuse_default_above_ceiling,
    view,
)
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..judgment import settings_mac
from ..qasettings import authorize_read, can_edit

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> BudgetStore:
    return BudgetStore(ctx.store.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _ceiling(ctx: AppContext) -> Limits:
    return ceiling_of(ctx.store.get_platform_settings().get(CEILING_SETTING))


def _answer(ctx: AppContext, auth: AuthContext, tenant: Tenant, store: BudgetStore) -> dict:
    return view(tenant.tenant_id, store.block(tenant.tenant_id), _ceiling(ctx),
                editable=can_edit(auth, tenant, ctx.roles))


@router.get("/v1/budget")
def get_selected_budget(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's budget: the same answer as the path form."""
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.get("/v1/workspaces/{tenant_id}/budget")
def get_budget(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/budget")
def put_budget(
    tenant_id: str,
    body: BudgetSettingsPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_write(auth, ctx.store.get_tenant(tenant_id), tenant_id, ctx.roles)
    default = body.limits()
    # Checked against the ceiling as it reads now; a ceiling lowered later
    # still binds, because every attempt's limit is the min of the three.
    refuse_default_above_ceiling(default, _ceiling(ctx))
    store = _store(ctx)
    store.put(tenant_id, default, by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)
