"""The onboarding checklist route (docs/onboarding.md §2.1, §3.2; #780, lane OB1).

    GET /v1/onboarding    the caller's seven steps, each with its state, evidence
                          and §2.3 recovery copy, and the next step

The derivation is `swarm_api.onboarding`'s. The route only reads: the
tenant comes from `tenant_scope`, never the request, and the caller from the
verified identity, so a caller sees their own checklist in their own tenant
and nothing of anyone else's. No route here accepts or returns a token.

`app_installed` asks the access service, as this caller, whether the GitHub
App is installed anywhere they reach; `onboarding.read` asks it only for an
active App connection (#780, 2026-10-08).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from .. import onboarding
from ..forgeapp import Caller as AccessCaller

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])


@router.get("")
def get_onboarding(
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller = onboarding.Caller(email=auth.email, tenant_id=tenant_id, is_admin=auth.is_admin)
    service = getattr(request.app.state, "access", None)
    installations = None if service is None else (
        lambda: service.installations(AccessCaller(email=auth.email, tenant_id=tenant_id)))
    return onboarding.read(ctx.db, caller, tenant=ctx.store.get_tenant(tenant_id), now=ctx.now(),
                           installations=installations)
