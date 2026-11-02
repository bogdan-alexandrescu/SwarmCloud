"""A workspace's Jev usage and cost (JEV-USAGE-PAGE, part of #2688; `swarm_api.jevusage`).

    GET  /v1/jev-usage                           the selected workspace's
    GET  /v1/workspaces/{tenant_id}/jev-usage    that workspace's

The last 30 days of Jev calls, tokens, outcomes and dollars, per UTC day and
kind, the 30-day total, and the month's use against
`code_intelligence.jev_monthly_cap_tokens`. Dollars are priced at the
platform setting `jev_token_rates`, and are null while it is not set.

READ: a workspace is read by a caller whose selected tenant it is
(`tenant_scope`, through the collision guard) or by a platform admin; any
other id is a 404, as on `/v1/workspaces/{id}/judgment-backend`, so the
route does not say which workspaces exist. Nothing here writes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..auth import AuthContext
from ..codeintel import CodeIntelligenceStore
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import NotFound
from ..jevusage import RATE_SETTING, JevUsageReader, cap_of, rates_of
from ..judgment import settings_mac

router = APIRouter(tags=["jev-usage"])


def _usage(ctx: AppContext, auth: AuthContext, tenant_id: str, scope: str) -> dict[str, Any]:
    if ctx.store.get_tenant(tenant_id) is None or (tenant_id != scope and not auth.is_admin):
        raise NotFound(f"workspace {tenant_id!r} not found")
    block = CodeIntelligenceStore(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings)).block(
        tenant_id)
    rates = rates_of(ctx.store.get_platform_settings().get(RATE_SETTING))
    return JevUsageReader(ctx.db).read(tenant_id, now=ctx.now(), rates=rates,
                                       cap_tokens=cap_of(block))


@router.get("/v1/jev-usage")
def get_jev_usage(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's Jev usage, for Capacity › Jev usage."""
    return _usage(ctx, auth, tenant_id, tenant_id)


@router.get("/v1/workspaces/{tenant_id}/jev-usage")
def get_workspace_jev_usage(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    scope: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return _usage(ctx, auth, tenant_id, scope)
