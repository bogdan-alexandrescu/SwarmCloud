"""The access API (docs/onboarding.md §3.2; #780, lane OB4). The logic is
`swarm_api.access`'s.

    GET    /v1/access                               connection, enabled owners, grants,
                                                    and `github_quota`: each credential
                                                    the caller may see, as GitHub last
                                                    reported its rate limit
    GET    /v1/access/orgs                          reachable owners, install_state, sso
    POST   /v1/access/orgs                          {owner}: enable an installed owner
    DELETE /v1/access/orgs/{owner}                  disable it, and delete its grants (or
                                                    withdraw an install request)
    POST   /v1/access/orgs/{owner}/install-request  record that the person asked {owner}'s
                                                    owners to install the App: the owner
                                                    shows ORG_APPROVAL_PENDING until GitHub
                                                    lists the installation, re-checked on
                                                    every read and by the 15-minute sweep
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

WHO MAY WRITE (docs/design/workspace-roles.md §4.1, §4.8; lane
ROLES-GATES-AUTOMATE). Every write here -- enable or disable an owner, an
install request, a grant, its revocation and its verify (whose push test
creates a branch) -- needs the role `member` in the selected workspace
(`_member`), before the person's token or the forge is touched. A `viewer` is
`role_member_required`, through its switch. The reads stay open to a viewer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field

from swarm_common.models import Tenant

from .. import forge_limits, roles
from ..access import CHECKS, MAX_PAGES, MAX_QUERY_CHARS, OPT_IN_CHECKS, AccessService
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..forgeapp import CONNECTIONS, ORGS, Caller, connection_id_for

router = APIRouter(prefix="/v1/access", tags=["access"])


def get_access(request: Request) -> AccessService:
    """The service `create_app` hung on the app; a test injects one over fakes."""
    return request.app.state.access


def _caller(auth: AuthContext, tenant_id: str) -> Caller:
    return Caller(email=auth.email, tenant_id=tenant_id)


def _member(ctx: AppContext, auth: AuthContext) -> None:
    """The role gate on every write here (workspace-roles.md §4.8):
    `role_member_required` for a `viewer`, through its switch."""
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.MEMBER)


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


#: A tenant enables a handful of owners; more than this are not listed.
_MAX_QUOTA_OWNERS = 200


def github_quota(ctx: AppContext, caller: Caller) -> list[dict[str, Any]]:
    """QUOTA-THROTTLE (#1483): what `forge_limits.QUOTAS` last read for each
    credential the caller may see -- their own connection, their tenant's
    `-git` slot, and the App installations their tenant enabled -- with who
    it is (`kind`, `name`) and `low` under `forge_quota_defer_percent`, the
    line background reads stop at. Counts and times only: no token, no
    secret. A credential nothing in this process has read yet is left out:
    its quota is unknown, not full.

    THE CONNECTION LINE IS NOT YET FED. The person's own reads go through
    `access._AsUser`, outside this lane's territory, and do not record into
    the ledger, so in a deployed console only the tenant and installation
    lines appear until they do (the question in this lane's PR, #1483)."""
    named: list[tuple[str, str, str]] = [
        (forge_limits.connection_credential(connection_id_for(caller.tenant_id, caller.key)),
         "connection", "you"),
        (forge_limits.tenant_credential(caller.tenant_id), "tenant", caller.tenant_id),
    ]
    connection = ctx.db.collection(CONNECTIONS).document(
        connection_id_for(caller.tenant_id, caller.key)).get()
    doc = connection.to_dict() if connection.exists else None
    if doc and doc.get("tenant_id") == caller.tenant_id and doc.get("user") == caller.key \
            and isinstance(doc.get("forge_login"), str) and doc["forge_login"]:
        named[0] = (named[0][0], "connection", doc["forge_login"])
    seen: set[int] = set()
    query = (ctx.db.collection(ORGS)
             .where(filter=FieldFilter("tenant_id", "==", caller.tenant_id))
             .limit(_MAX_QUOTA_OWNERS))
    for snap in query.stream():
        org = snap.to_dict() or {}
        installation = org.get("installation_id")
        # Asked again: only this tenant's installations (invariant 9).
        if org.get("tenant_id") != caller.tenant_id or not isinstance(installation, int) \
                or isinstance(installation, bool) or installation in seen:
            continue
        seen.add(installation)
        named.append((forge_limits.installation_credential(installation), "installation",
                      str(org.get("owner_login") or org.get("owner") or installation)))
    percent = forge_limits.defer_percent(ctx)
    now = datetime.now(timezone.utc)
    found: list[dict[str, Any]] = []
    for credential, kind, name in named:
        quota = forge_limits.QUOTAS.quota(credential)
        if quota is not None:
            # Low only until it resets: after that GitHub has refilled it.
            found.append({"kind": kind, "name": name, **quota.to_api(),
                          "low": quota.below(percent) and quota.until() > now})
    return found


@router.get("")
def overview(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    caller = _caller(auth, tenant_id)
    return {**service.overview(caller), "github_quota": github_quota(ctx, caller),
            "tenant_id": tenant_id}


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
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    _member(ctx, auth)
    return {**service.enable(_caller(auth, tenant_id), body.owner), "tenant_id": tenant_id}


@router.delete("/orgs/{owner}")
def disable_owner(
    owner: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    _member(ctx, auth)
    return {**service.disable(_caller(auth, tenant_id), owner), "tenant_id": tenant_id}


@router.post("/orgs/{owner}/install-request")
def request_install(
    owner: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    _member(ctx, auth)
    # No body: the owner is the path's, the person the verified identity's.
    return {**service.request_install(_caller(auth, tenant_id), owner), "tenant_id": tenant_id}


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
    _member(ctx, auth)
    tenant = caller_tenant(ctx, tenant_id, auth)
    return {**service.grant(_caller(auth, tenant_id), tenant, repo_id,
                            repository=body.repository, mode=body.mode),
            "tenant_id": tenant_id}


@router.delete("/grants/{repo_id}")
def delete_grant(
    repo_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    _member(ctx, auth)
    return {**service.revoke(_caller(auth, tenant_id), repo_id), "tenant_id": tenant_id}


@router.post("/grants/{repo_id}/verify")
def verify_grant(
    repo_id: str,
    body: VerifyBody | None = None,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    service: AccessService = Depends(get_access),
) -> dict:
    _member(ctx, auth)
    checks = list(body.checks) if body is not None and body.checks else None
    return {**service.verify(_caller(auth, tenant_id), repo_id, checks), "tenant_id": tenant_id}


@router.get("/members")
def members(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: AccessService = Depends(get_access),
) -> dict:
    return {**service.members(tenant_id, is_admin=auth.is_admin), "tenant_id": tenant_id}
