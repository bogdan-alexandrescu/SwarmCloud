"""`/v1/repositories`: register, list, read, change and unregister a repository (lane RI1).

docs/repo-index.md §6.1. Tenant-scoped exactly as issue runs are: the caller's
tenant comes from `tenant_scope`, never from the body or the path, and another
tenant's `repo_id` is a 404 indistinguishable from a missing one.

`GET /v1/repositories/readable` is the Register C picker's list (PICKS.md,
2026-10-05): what the tenant's git token can read, one GitHub page per call,
capped at `repositories.MAX_READABLE_PAGES`. Everything about the token is
`swarm_api.forge`'s and `swarm_api.repositories`'s; these routes log the
outcome's code -- never the token, never the forge's text.

Who may register: any member of the tenant, as for issue runs. The design's
"an admin of the tenant" (repo-index.md §1, git-tokens.md §1) names a role
this API does not have yet; a platform admin acts in a tenant only as a member
of it, like everywhere else here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Response, status

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..forge import ForgeReadError
from ..repositories import (
    MAX_READABLE_PAGES,
    Repositories,
    RepositoryCreate,
    RepositoryPatch,
    readable,
    register,
    to_api,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/repositories", tags=["repositories"])


def _store(ctx: AppContext) -> Repositories:
    return Repositories(ctx.db, now=ctx.now)


def _tenant(ctx: AppContext, tenant_id: str, auth: AuthContext) -> Tenant:
    # The secret is named from the tenant id through the frozen
    # `Tenant.secret_name`; a tenant declared in Terraform may have no
    # Firestore document yet, and its secret is named the same either way
    # (the issue preview's rule, routes/issues.py).
    return ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=auth.tenant_principal or auth.email,
        created_at=ctx.now(),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_repository(
    body: RepositoryCreate,
    response: Response,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = _tenant(ctx, tenant_id, auth)
    try:
        record, created = register(
            body, tenant, created_by=auth.email, store=_store(ctx),
            tokens=ctx.forge_tokens, forge=ctx.forge, now=ctx.now,
        )
    except ForgeReadError as refused:
        log.info("repository register tenant=%s outcome=%s", tenant_id, refused.code)
        raise
    if not created:
        response.status_code = status.HTTP_200_OK
    log.info(
        "repository register tenant=%s repo_id=%s outcome=%s",
        tenant_id, record["repo_id"], "created" if created else "existing",
    )
    return {"repository": to_api(record), "created": created, "tenant_id": tenant_id}


@router.get("")
def list_repositories(
    limit: int | None = Query(default=None),
    page_token: str | None = Query(default=None, max_length=256),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    rows, next_token = _store(ctx).list(
        tenant_id, limit=paged_limit(ctx, limit), page_token=page_token
    )
    return {
        "repositories": [to_api(row) for row in rows],
        "next_page_token": next_token,
        "tenant_id": tenant_id,
    }


# Declared before `/{repo_id}`, which would otherwise match "readable".
@router.get("/readable")
def readable_repositories(
    page: int = Query(default=1, ge=1, le=MAX_READABLE_PAGES),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = _tenant(ctx, tenant_id, auth)
    try:
        body = readable(
            tenant, page, store=_store(ctx), tokens=ctx.forge_tokens, forge=ctx.forge
        )
    except ForgeReadError as refused:
        log.info("repository readable tenant=%s page=%d outcome=%s", tenant_id, page, refused.code)
        raise
    log.info(
        "repository readable tenant=%s page=%d listed=%d outcome=ok",
        tenant_id, page, len(body["repositories"]),
    )
    return {**body, "tenant_id": tenant_id}


@router.get("/{repo_id}")
def get_repository(
    repo_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"repository": to_api(_store(ctx).get(tenant_id, repo_id)), "tenant_id": tenant_id}


@router.patch("/{repo_id}")
def patch_repository(
    repo_id: str,
    body: RepositoryPatch,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    record = _store(ctx).patch(tenant_id, repo_id, body)
    log.info(
        "repository patch tenant=%s repo_id=%s by=%s fields=%s", tenant_id, record["repo_id"],
        auth.email, ",".join(sorted(body.model_dump(exclude_none=True))),
    )
    return {"repository": to_api(record), "tenant_id": tenant_id}


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repository(
    repo_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> Response:
    _store(ctx).delete(tenant_id, repo_id)
    log.info("repository delete tenant=%s repo_id=%s by=%s", tenant_id, repo_id, auth.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
