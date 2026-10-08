"""A person's own workspace (docs/workspaces.md §1.3, §6.3; #847, lane W1).

    GET  /v1/workspace                the caller's record, or {"state": "none"}
    POST /v1/workspace                request it: 202 when this call made or
                                      re-opened the request, 200 when it
                                      already stood; idempotent
    POST /v1/workspace/loan-request   ask an admin to lend a Claude account;
                                      idempotent

ALWAYS THE CALLER'S OWN. The target is `tenant_id_for_user(verified email)`,
never a body field and never `X-Swarm-Tenant`, so nobody can read or request
anyone else's -- a member of `eng` asking here asks for their personal one.
No route here takes or returns a credential, an image, a command or a
resource spec (invariant 10); the one body field is where the request came
from.

The approval, denial, retry and lending routes are lane W7's, under
`/v1/admin/`, and address a person by workspace id.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..errors import WorkspaceIdTaken, WorkspaceNotForServiceAccounts, WorkspacePrincipalForbidden
from ..schemas import StrictModel
from ..workspaces import is_service_identity, personal_tenant_id, view

router = APIRouter(prefix="/v1/workspace", tags=["workspace"])


class WorkspaceRequest(StrictModel):
    #: Recorded as `requested_via`; nothing else is read from a body.
    via: Literal["console", "plugin", "api"] = "api"


def _own(auth: AuthContext, ctx: AppContext) -> tuple[str, str]:
    """(personal tenant id, principal) of a caller who may have a workspace.

    §1.3's checks 2-5; check 1, the allowed domain, is `current_auth`'s."""
    email = (auth.email or "").strip().lower()
    if is_service_identity(email) or auth.member_scope or auth.is_rollup_sweeper \
            or auth.tenant_member:
        raise WorkspaceNotForServiceAccounts(
            "A workspace is a person's own space. Service accounts run in the tenant "
            "declared for them and cannot request one.")
    forbidden = {p.strip().lower() for p in ctx.settings.secret_admin_principals if p.strip()}
    if email in forbidden:
        # `SubmissionService.tenant_for`'s refusal, for the reason given there.
        raise WorkspacePrincipalForbidden(
            "This identity administers every tenant's provider-key secrets, so it may not "
            "own a tenant or a workspace of its own. Sign in as an ordinary user.")
    tenant_id = personal_tenant_id(email)
    existing = ctx.store.get_tenant(tenant_id)
    if existing is not None and existing.principal.strip().lower() != email:
        # Never the other principal: the support route instead.
        raise WorkspaceIdTaken(
            "The workspace name that belongs to your account is already registered to a "
            "different identity. Nothing was changed. Ask an admin to look at it; this "
            "needs a person, not a retry.")
    return tenant_id, email


@router.get("")
def get_workspace(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant_id, _ = _own(auth, ctx)
    workspaces = ctx.submissions.workspaces
    return view(workspaces.get(tenant_id), console_url=workspaces.console_url)


@router.post("")
def post_workspace(
    body: WorkspaceRequest | None = None,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> JSONResponse:
    tenant_id, principal = _own(auth, ctx)
    workspaces = ctx.submissions.workspaces
    workspaces.touch_person(auth)
    status, record = workspaces.request(
        tenant_id=tenant_id, principal=principal, via=(body.via if body else "api"))
    return JSONResponse(status_code=status,
                        content=view(record, console_url=workspaces.console_url))


@router.post("/loan-request")
def post_loan_request(
    body: WorkspaceRequest | None = None,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> JSONResponse:
    tenant_id, principal = _own(auth, ctx)
    status, record = ctx.submissions.workspaces.request_loan(
        tenant_id=tenant_id, principal=principal, via=(body.via if body else "api"))
    return JSONResponse(status_code=status, content={
        "state": record.get("state"),
        "workspace_id": record.get("workspace_id"),
        "request_id": record.get("request_id"),
        "requested_at": record["requested_at"].isoformat()
        if hasattr(record.get("requested_at"), "isoformat") else record.get("requested_at"),
    })
