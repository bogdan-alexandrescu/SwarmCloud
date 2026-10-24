"""Config › Secrets' one read (SSC-OVERVIEW, part of #2158).

    GET /v1/secrets/overview?scope=workspace|personal|repository[&repo_id=..]

Design: docs/design/secrets-scopes.md §4 and §6.2, with Q1 of its §11; the
reads are `swarm_api.secrets_overview`, whose docstring says which store each
row comes from. No value, no length and no character of one is served.

NO PARAMETER NAMES A TENANT OR A PERSON. The workspace is `tenant_scope`; the
personal scope is the caller's own personal workspace, from their verified
email; a repository is a `repo_id` that must be a registration OF THAT
WORKSPACE (another workspace's is 404, as on every repository-secret route).
Any query parameter but `scope` and `repo_id` is a 422, rather than ignored.

Every member of the workspace reads the workspace and repository scopes
(§2.5); `can_manage` per row says what the console offers, and each row's
writer is the gate.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import NotFound, ValidationFailed
from ..provider_keys import own_personal_tenant
from ..secrets_overview import (
    NOT_LISTED,
    NOTE,
    SCOPES,
    personal_scope,
    repository_scope,
    workspace_scope,
)
from ..workspace_secrets import SCOPE_PERSONAL, SCOPE_REPOSITORY
from .repository_secrets import _registration
from .workspace_secrets import _tenant

router = APIRouter(tags=["secrets-overview"])

PARAMETERS = frozenset({"scope", "repo_id"})


def _own_personal(ctx: AppContext, auth: AuthContext) -> Tenant | None:
    """The caller's own personal workspace, or None. A continuation-scoped or
    other narrowed session has none here, as on the browser-login routes."""
    if auth.member_scope:
        return None
    try:
        return own_personal_tenant(ctx.store, ctx.db, auth, require_ready=False)
    except NotFound:
        return None


@router.get("/v1/secrets/overview")
def secrets_overview(
    request: Request,
    scope: Literal["workspace", "personal", "repository"] = Query(...),
    repo_id: str | None = Query(default=None, max_length=64),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    extra = sorted(set(request.query_params) - PARAMETERS)
    if extra:
        raise ValidationFailed(f"unknown parameter(s): {', '.join(extra)}; this route takes "
                               "scope and repo_id", detail={"field": extra[0]})
    if (scope == SCOPE_REPOSITORY) != (repo_id is not None):
        raise ValidationFailed("repo_id is given with scope=repository, and only then",
                               detail={"field": "repo_id"})
    tenant = _tenant(ctx, tenant_id)
    body: dict = {"tenant_id": tenant_id, "scope": scope, "scopes": list(SCOPES),
                  "repo_id": None, "repository": None, "personal_tenant_id": None}
    if scope == SCOPE_REPOSITORY:
        registration = _registration(ctx, tenant_id, str(repo_id))
        body.update(repo_id=repo_id,
                    repository=f"{registration['owner']}/{registration['repo']}")
        body.update(repository_scope(ctx.db, auth=auth, tenant=tenant, roles=ctx.roles,
                                     registration=registration, now=ctx.now))
    elif scope == SCOPE_PERSONAL:
        personal = _own_personal(ctx, auth)
        body["personal_tenant_id"] = personal.tenant_id if personal is not None else None
        body.update(personal_scope(ctx.db, auth=auth, tenant=tenant, personal=personal,
                                   now=ctx.now))
    else:
        body.update(workspace_scope(ctx.db, auth=auth, tenant=tenant, roles=ctx.roles,
                                    now=ctx.now))
    body["not_listed"] = [dict(entry) for entry in NOT_LISTED]
    body["note"] = NOTE
    return body
