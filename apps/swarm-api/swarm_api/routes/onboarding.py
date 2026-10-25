"""The onboarding checklist route (docs/onboarding.md §2.1, §3.2; #780, lane OB1).

    GET  /v1/onboarding           the caller's nine steps, each with its state,
                                  evidence and §2.3 recovery copy, the next step,
                                  and whether the caller hid the checklist
    POST /v1/onboarding/dismiss   {dismissed?: bool, default true}: hide the
                                  checklist, or show it again; steps keep their state

The `workspace` and `claude_account` steps (docs/workspaces.md §6.1) are
required only while WORKSPACE_GATE is on and the gate judges the caller's
tenant -- the same `Workspaces.applies_to` the submission gate asks.

The derivation is `swarm_api.onboarding`'s. The tenant comes from
`tenant_scope`, never the request, and the caller from the verified identity,
so a caller sees -- and dismisses -- their own checklist in their own tenant
and nothing of anyone else's; the dismiss body forbids every field it does
not name. No route here accepts or returns a token.

`app_installed` asks the access service, as this caller, whether the GitHub
App is installed anywhere they reach; `onboarding.read` asks it only for an
active App connection (#780, 2026-10-08).

`config_setup` (CONFIG-SETUP-READ, part of #2157) is the Config setup strip's
steps 3 and 4, Secrets set and People invited, measured by
`swarm_api.config_setup` for the same `tenant_scope` workspace. It rides on
this read, as a new field beside the checklist, so the console's strip makes
one read for all four steps; nothing the checklist served changes. It names
what is needed and never carries a value, so any member may read it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from .. import config_setup, onboarding
from ..forgeapp import Caller as AccessCaller
from ..publish_workspace import WorkspacePublisher
from .people import workspace_publisher
from .provider_keys import CONSUMERS as KEY_CONSUMERS

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])


@router.get("")
def get_onboarding(
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    publisher: WorkspacePublisher = Depends(workspace_publisher),
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
    service = getattr(request.app.state, "access", None)
    installations = None if service is None else (
        lambda: service.installations(AccessCaller(email=auth.email, tenant_id=tenant_id)))
    view = onboarding.read(ctx.db, caller, tenant=tenant, now=ctx.now(),
                           installations=installations, workspaces=workspaces,
                           workspace_required=required,
                           workspace_publishing=bool(publisher.enabled))
    view["config_setup"] = config_setup.read(ctx.db, auth=auth, tenant=judged, roles=ctx.roles,
                                             settings=ctx.settings, now=ctx.now,
                                             key_consumers=KEY_CONSUMERS)
    return view


class DismissBody(BaseModel):
    # Strict: "maybe" or 1 is a 422, not a guess; a tenant or a user is not
    # a field, so naming one is a 422 too.
    model_config = ConfigDict(extra="forbid", strict=True)

    dismissed: bool = True


@router.post("/dismiss")
def dismiss_onboarding(
    body: DismissBody | None = None,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller = onboarding.Caller(email=auth.email, tenant_id=tenant_id, is_admin=auth.is_admin)
    return onboarding.dismiss(ctx.db, caller, dismissed=True if body is None else body.dismissed,
                              now=ctx.now())
