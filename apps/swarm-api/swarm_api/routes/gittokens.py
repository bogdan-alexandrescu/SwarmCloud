"""Git token registry routes (docs/git-tokens.md §7; lane GT1). The record is `swarm_api.gittokens`'s.

    GET    /v1/git-tokens              the tenant's slots, the default first
    GET    /v1/git-tokens/{token_id}   one slot
    POST   /v1/git-tokens              register a slot: {"scope", "repo_id"|"repository"?, "repo_ids"?}
    DELETE /v1/git-tokens/{token_id}   revoke a slot's record

NO ROUTE ACCEPTS A TOKEN VALUE. The operator picked Git tokens B on
2026-10-05 with no console paste box (docs/web-ui/mockups/PICKS.md), so
there is no `PUT .../value` and the registration body forbids every field it
does not name. A registration answers the exact
`scripts/create-secrets.sh --stdin` line for its slot; that script is the
only way a value is stored (CLAUDE.md, owner rule 2026-09-25).

Who may do what (§1): any member of the tenant reads its slots. A tenant or
repository slot is registered and revoked by an admin. A user slot is always
the caller's own -- the body has no user field -- and is revoked by that
user or an admin. "Admin" is the platform's `is_admin`: there is no
per-tenant admin role to ask instead.

The tenant comes from `tenant_scope`, never from the body, and another
tenant's token id is the same 404 as a missing one.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import Forbidden, ValidationFailed
from ..gittokens import (
    REPO_ID,
    REPOSITORY,
    GitTokenRecord,
    GitTokens,
    Scope,
    record_for_slot,
    repo_id_for,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/git-tokens", tags=["git-tokens"])


class GitTokenSlot(BaseModel):
    """A slot, by name. `extra="forbid"`: a value in any field is a 422, and
    the validation handler never echoes what was sent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scope: Literal["tenant", "repository", "user"]
    #: repository scope: the registration's id (docs/repo-index.md §1) ...
    repo_id: str | None = Field(default=None, max_length=64)
    #: ... or `owner/repo`, from which the same id is derived.
    repository: str | None = Field(default=None, max_length=256)
    #: user scope: narrow the slot to these repositories; absent for all.
    repo_ids: list[str] | None = Field(default=None, max_length=500)


def _tokens(ctx: AppContext) -> GitTokens:
    return GitTokens(ctx.db, now=ctx.now)


def _require_admin(auth: AuthContext, what: str) -> None:
    if auth.is_admin:
        return
    if auth.admin_unresolved:
        raise Forbidden(
            f"{what} needs an admin, and the directory could not say whether you are one; "
            "try again"
        )
    raise Forbidden(f"{what} needs an admin")


def _slot_from(body: GitTokenSlot, tenant_id: str, auth: AuthContext, ctx: AppContext
               ) -> GitTokenRecord:
    scope = Scope(body.scope)
    if scope is not Scope.REPOSITORY and (body.repo_id or body.repository):
        raise ValidationFailed(f"a {scope.value} slot takes no repository",
                               detail={"field": "repo_id"})
    if scope is not Scope.USER and body.repo_ids is not None:
        raise ValidationFailed("only a user slot is narrowed by repo_ids",
                               detail={"field": "repo_ids"})
    repo_id = None
    if scope is Scope.REPOSITORY:
        if bool(body.repo_id) == bool(body.repository):
            raise ValidationFailed("a repository slot takes one of repo_id or repository",
                                   detail={"field": "repo_id"})
        if body.repository:
            if not REPOSITORY.match(body.repository):
                raise ValidationFailed("repository must be owner/repo",
                                       detail={"field": "repository"})
            repo_id = repo_id_for(tenant_id, body.repository)
        else:
            repo_id = body.repo_id
            if not REPO_ID.match(repo_id or ""):
                raise ValidationFailed("repo_id must be repo_<16 hex>",
                                       detail={"field": "repo_id"})
        _require_admin(auth, "registering a repository's git token")
    elif scope is Scope.TENANT:
        _require_admin(auth, "registering the tenant's git token")
    return record_for_slot(
        tenant_id, scope,
        repo_id=repo_id,
        # Always the caller: an admin may revoke a user slot but never
        # register one for someone else (§1).
        user=auth.email if scope is Scope.USER else None,
        repo_ids=body.repo_ids,
        registered_by=auth.email,
        now=ctx.now(),
    )


@router.get("")
def list_git_tokens(
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tokens = _tokens(ctx)
    tokens.ensure_tenant_default(ctx.store.get_tenant(tenant_id))
    return {
        "tenant_id": tenant_id,
        "tokens": [record.to_api() for record in tokens.list(tenant_id)],
        # Which order a task's token is resolved in (docs/git-tokens.md §3.1).
        "resolution_order": "R2",
    }


@router.get("/{token_id}")
def get_git_token(
    token_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tokens = _tokens(ctx)
    tokens.ensure_tenant_default(ctx.store.get_tenant(tenant_id))
    return {"token": tokens.get(tenant_id, token_id).to_api()}


@router.post("", status_code=status.HTTP_201_CREATED)
def register_git_token(
    body: GitTokenSlot,
    response: Response,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    record, created = _tokens(ctx).register(_slot_from(body, tenant_id, auth, ctx))
    if not created:
        response.status_code = status.HTTP_200_OK
    api = record.to_api()
    return {
        "token": api,
        "store_command": api["store_command"],
        "note": (
            "this registered the slot's record only; store the token with the command, "
            "which reads it from stdin. No route of this API accepts a token value."
        ),
    }


@router.delete("/{token_id}")
def revoke_git_token(
    token_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    def allowed(record: GitTokenRecord) -> None:
        if record.scope is Scope.USER and record.user == auth.email.strip().lower():
            return
        _require_admin(auth, f"revoking a {record.scope.value} git token")

    record = _tokens(ctx).revoke(tenant_id, token_id, by=auth.email, allowed=allowed)
    return {
        "token": record.to_api(),
        "note": (
            f"the record is revoked and no task resolves to it; {record.secret_name}'s "
            "versions are unchanged, because swarm-api holds no grant that can disable them. "
            "Disable them in Secret Manager to stop the value working anywhere else."
        ),
    }
