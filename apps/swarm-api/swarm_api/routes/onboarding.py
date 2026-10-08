"""The onboarding checklist route (docs/onboarding.md §2.1, §3.2; #780, lane OB1).

    GET /v1/onboarding    the caller's eight steps, each with its state, evidence
                          and §2.3 recovery copy, and the next step

The `workspace` and `claude_account` steps (docs/workspaces.md §6.1) are
required only while WORKSPACE_GATE is on and the gate judges the caller's
tenant -- the same `Workspaces.applies_to` the submission gate asks.

The derivation is `swarm_api.onboarding`'s. The route only reads: the
tenant comes from `tenant_scope`, never the request, and the caller from the
verified identity, so a caller sees their own checklist in their own tenant
and nothing of anyone else's. No route here accepts or returns a token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from .. import onboarding

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])


@router.get("")
def get_onboarding(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller = onboarding.Caller(email=auth.email, tenant_id=tenant_id, is_admin=auth.is_admin)
    tenant = ctx.store.get_tenant(tenant_id)
    workspaces = ctx.submissions.workspaces
    # A tenant not written yet is a person's own on first sight: the gate
    # judges it exactly as it would the tenant `tenant_for` would make.
    judged = tenant if tenant is not None else Tenant(
        tenant_id=tenant_id, kind="user" if tenant_id.startswith("u-") else "group",
        principal=auth.tenant_principal or auth.email, created_at=ctx.now())
    required = workspaces.gate and workspaces.applies_to(auth, judged)
    return onboarding.read(ctx.db, caller, tenant=tenant, now=ctx.now(),
                           workspaces=workspaces, workspace_required=required)
