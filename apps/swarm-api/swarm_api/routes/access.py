"""The access API (docs/onboarding.md §3.2; #780, lane OB4). The logic is
`swarm_api.access`'s.

    GET    /v1/access                               connection, enabled owners, grants
    GET    /v1/access/orgs                          reachable owners, install_state, sso
    POST   /v1/access/orgs                          {owner}: enable an installed owner
    DELETE /v1/access/orgs/{owner}                  disable it, and delete its grants
    GET    /v1/access/orgs/{owner}/repositories     ?page=N&q=text: one page, each with
                                                    registered, granted, mode
    PUT    /v1/access/grants/{repo_id}              {repository, mode}: grant read or write
    DELETE /v1/access/grants/{repo_id}              revoke the grant
    POST   /v1/access/grants/{repo_id}/verify       {checks?}: clone, push, pull_request now;
                                                    push_test only when named (write grants):
                                                    creates and deletes one branch (D6)
    GET    /v1/access/members                       admin: every member's connection and
                                                    grants in the caller's tenant

NO ROUTE RETURNS A VALUE: the person's token is in hand for one request,
inside `access.AccessService.user_token`, and no answer carries it. The
tenant comes from `tenant_scope` and the person from the verified identity,
never the body or the path; the bodies forbid every field they do not name.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from swarm_common.models import Tenant

from ..access import CHECKS, MAX_PAGES, MAX_QUERY_CHARS, OPT_IN_CHECKS, AccessService
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..forgeapp import Caller

router = APIRouter(prefix="/v1/access", tags=["access"])


def get_access(request: Request) -> AccessService:
    """The service `create_app` hung on the app; a test injects one over fakes."""
    return request.app.state.access


def _caller(auth: AuthContext, tenant_id: str) -> Caller:
    return Caller(email=auth.email, tenant_id=tenant_id)


def caller_tenant(ctx: AppContext, tenant_id: str, auth: AuthContext) -> Tenant:
    # As routes/repositories.py names it: a tenant declared in Terraform may
    # have no Firestore document yet, and its secrets are named the same.
    return ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id, kind="group", principal=auth.tenant_principal or auth.email,
        created_at=ctx.now())


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EnableOrgBody(_Body):
    owner: str = Field(min_length=1, max_length=39)


class GrantBody(_Body):
    repository: str = Field(min_length=3, max_length=141)
    mode: Literal["read", "write"]


class VerifyBody(_Body):
    # `push_test` is D6's opt-in write: never in the default set, so a body
    # that does not name it reads only.
    checks: list[Literal["clone", "push", "pull_request", "push_test"]] | None = Field(
        default=None, min_length=1, max_length=len(CHECKS) + len(OPT_IN_CHECKS))


@router.get("")
def overview(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.overview(_caller(auth, tenant_id)), "tenant_id": tenant_id}


@router.get("/orgs")
def owners(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.owners(_caller(auth, tenant_id)), "tenant_id": tenant_id}


@router.post("/orgs")
def enable_owner(
    body: EnableOrgBody,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.enable(_caller(auth, tenant_id), body.owner), "tenant_id": tenant_id}


@router.delete("/orgs/{owner}")
def disable_owner(
    owner: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.disable(_caller(auth, tenant_id), owner), "tenant_id": tenant_id}


@router.get("/orgs/{owner}/repositories")
def owner_repositories(
    owner: str,
    page: int = Query(default=1, ge=1, le=MAX_PAGES),
    q: str | None = Query(default=None, max_length=MAX_QUERY_CHARS),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.repositories(_caller(auth, tenant_id), owner, page=page, query=q),
            "tenant_id": tenant_id}


@router.put("/grants/{repo_id}")
def put_grant(
    repo_id: str,
    body: GrantBody,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    tenant = caller_tenant(ctx, tenant_id, auth)
    return {**service.grant(_caller(auth, tenant_id), tenant, repo_id,
                            repository=body.repository, mode=body.mode),
            "tenant_id": tenant_id}


@router.delete("/grants/{repo_id}")
def delete_grant(
    repo_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.revoke(_caller(auth, tenant_id), repo_id), "tenant_id": tenant_id}


@router.post("/grants/{repo_id}/verify")
def verify_grant(
    repo_id: str,
    body: VerifyBody | None = None,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    checks = list(body.checks) if body is not None and body.checks else None
    return {**service.verify(_caller(auth, tenant_id), repo_id, checks), "tenant_id": tenant_id}


@router.get("/members")
def members(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.members(tenant_id, is_admin=auth.is_admin), "tenant_id": tenant_id}
