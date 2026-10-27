"""A registered repository's secrets for agents (SSC-REPO-SCOPE, part of #2158).

    GET    /v1/repositories/{repo_id}/secrets                          names and grants
    POST   /v1/repositories/{repo_id}/secrets                          add (the value, once)
    PUT    /v1/repositories/{repo_id}/secrets/{name}/value             rotate
    DELETE /v1/repositories/{repo_id}/secrets/{name}                   destroy; the body types the name
    PUT    /v1/repositories/{repo_id}/secrets/{name}/grants/{consumer} {enabled, slot?}, signed "v2"
    POST   /v1/repositories/{repo_id}/secrets/links                    a one-time link to the form
    POST   /v1/repositories/{repo_id}/secrets:disable-grants           a removed registration's
                                                                       grants, all off

Design: docs/design/secrets-scopes.md §2 and §6.2, with the owner's answers of
its §11. The store is `workspace_secrets.WorkspaceSecrets`, the same records,
grants, links and Secret Manager writer as `/v1/workspace/secrets`: a
repository secret is a `-wsec-` secret of this workspace carrying
`scope: repository` and the registration's `repo_id` (Q9), released only to an
attempt whose signed spec clones that repository (`workspace_secrets.
release_plan`).

WHO MAY WRITE (Q2). Any one of:

  * the workspace's admin, or a platform admin, or in a personal workspace
    its person: `workspace_secrets.require_manager`, unchanged;
  * a member of the workspace whom GitHub calls ADMIN of that repository,
    asked live at this write with that person's OWN GitHub user token
    (`github_repo_admin`), never from a stored role and never with the App's
    installation token. No connection, an expired token, a 404 or a GitHub
    error is a refusal with `detail.reason`.

Every audit row names which rule let the writer through (`authority`).

NO ROUTE TAKES A TENANT, A PERSON, AN IMAGE OR A COMMAND. The workspace is
`tenant_scope`; the repository is a `repo_id` that must be a registration OF
THAT WORKSPACE (another workspace's is 404, whatever it names); a consumer is a
catalogue name. THE VALUE IS ACCEPTED ON TWO ROUTES AND RETURNED BY NONE, and
every check that can refuse a write runs before it reaches Secret Manager.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from swarm_common.models import Tenant

from .. import roles
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import Forbidden, ValidationFailed
from ..forgeapp import Caller
from ..github_repo_admin import require_repo_admin
from ..grant_attest import GrantPolicy
from ..repositories import Repositories, repo_id_for
from ..workspace_secrets import (
    WorkspaceSecrets,
    can_manage,
    check_kind,
    check_name,
    check_provider,
    is_personal,
    payload_for,
    refuse_name_in_value,
    require_manager,
)
from .access import get_access
from .workspace_secrets import (
    NOTE,
    GrantWrite,
    LinkRequest,
    SecretCreate,
    SecretDelete,
    SecretValue,
    SecretWriter,
    _tenant,
    secret_writer,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["repository-secrets"])

BASE = "/v1/repositories/{repo_id}/secrets"

#: The `authority` an audit row records.
WORKSPACE_ADMIN = "workspace_admin"
GITHUB_REPO_ADMIN = "github_repo_admin"


def _registration(ctx: AppContext, tenant_id: str, repo_id: str) -> dict[str, Any]:
    """This workspace's registration `repo_id`, or 404. Its `owner/repo` must
    still derive its id: a document rewritten to name another repository is
    not that registration (`repositories.repo_id_for` covers the tenant too)."""
    store = Repositories(ctx.db, now=ctx.now)
    registration = store.get(tenant_id, repo_id)
    owner, repo = registration.get("owner"), registration.get("repo")
    if not isinstance(owner, str) or not isinstance(repo, str) \
            or repo_id_for(tenant_id, owner, repo) != repo_id:
        raise store.not_found(repo_id)
    return registration


def _authority(request: Request, ctx: AppContext, auth: AuthContext, tenant: Tenant,
               registration: dict[str, Any]) -> str:
    """Q2: the manager rule, else GitHub's admin rule for this repository,
    asked now with the caller's own token. Raises when neither holds."""
    try:
        require_manager(auth, tenant, ctx.roles)
        return WORKSPACE_ADMIN
    except Forbidden:
        if is_personal(tenant):
            # A personal workspace is its person's alone, GitHub admin or not.
            raise
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.MEMBER)
    answer = require_repo_admin(get_access(request), ctx.forge,
                                Caller(email=auth.email, tenant_id=tenant.tenant_id),
                                str(registration["owner"]), str(registration["repo"]))
    log.info("repository secret write tenant=%s repo_id=%s by GitHub admin login=%s",
             tenant.tenant_id, registration.get("repo_id"), answer.login)
    return GITHUB_REPO_ADMIN


def _store(ctx: AppContext, writer: SecretWriter, authority: str | None = None
           ) -> WorkspaceSecrets:
    return WorkspaceSecrets(ctx.db, writer, now=ctx.now,
                            grants=GrantPolicy.from_settings(ctx.settings), authority=authority)


def _writable(request: Request, ctx: AppContext, auth: AuthContext, tenant_id: str,
              repo_id: str) -> tuple[Tenant, dict[str, Any], str]:
    """The workspace, its registration (404 before anything else) and the
    rule that lets this caller write."""
    tenant = _tenant(ctx, tenant_id)
    registration = _registration(ctx, tenant_id, repo_id)
    return tenant, registration, _authority(request, ctx, auth, tenant, registration)


@router.get(BASE)
def list_secrets(
    repo_id: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """Every member of the workspace reads the repository's names, kinds,
    versions and grants. Never a value, a length or the last characters.
    `can_manage` is the manager rule alone: whether GitHub calls the caller
    admin is asked at a write, never on a read."""
    tenant = _tenant(ctx, tenant_id)
    registration = _registration(ctx, tenant_id, repo_id)
    return {
        "tenant_id": tenant_id,
        "repo_id": repo_id,
        "repository": f"{registration['owner']}/{registration['repo']}",
        "can_manage": can_manage(auth, tenant, ctx.roles),
        "github_admin_may_write": not is_personal(tenant),
        "released_to": "only agents whose task clones this repository",
        "secrets": _store(ctx, writer).list(tenant_id, repo_id),
        "note": NOTE,
    }


@router.post(BASE, status_code=status.HTTP_201_CREATED)
def create_secret(
    repo_id: str,
    body: SecretCreate,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    tenant, registration, authority = _writable(request, ctx, auth, tenant_id, repo_id)
    name = check_name(body.name)
    provider = check_provider(body.provider)
    kind = check_kind(body.kind)
    payload = payload_for(kind, body.value, body.fields)
    refuse_name_in_value(name, payload)
    store = _store(ctx, writer, authority)
    via_link = store.consume_link(body.link_token, email=auth.email, tenant_id=tenant_id,
                                  action="add", name=name, repo_id=repo_id)
    record = store.create(tenant=tenant, name=name, provider=provider, kind=kind,
                          payload=payload, by=auth.email, via_link=via_link,
                          repository=registration)
    return {"secret": record, "via_link": via_link, "authority": authority, "note": NOTE}


@router.put(f"{BASE}/{{name}}/value")
def rotate_secret(
    repo_id: str,
    name: str,
    body: SecretValue,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """The workspace store's Q5: the previous version stays enabled 24 hours."""
    _, _, authority = _writable(request, ctx, auth, tenant_id, repo_id)
    name = check_name(name)
    store = _store(ctx, writer, authority)
    kind = str(store.find(tenant_id, name, repo_id).get("kind"))
    payload = payload_for(kind, body.value, body.fields)
    via_link = store.consume_link(body.link_token, email=auth.email, tenant_id=tenant_id,
                                  action="rotate", name=name, repo_id=repo_id)
    record = store.rotate(tenant_id=tenant_id, name=name, kind_payload=lambda _kind: payload,
                          by=auth.email, via_link=via_link, repo_id=repo_id)
    return {"secret": record, "via_link": via_link, "authority": authority, "note": NOTE}


@router.delete(f"{BASE}/{{name}}")
def delete_secret(
    repo_id: str,
    name: str,
    body: SecretDelete,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """The typed confirmation: the body repeats the secret's name exactly."""
    _, _, authority = _writable(request, ctx, auth, tenant_id, repo_id)
    name = check_name(name)
    if body.name != name:
        raise ValidationFailed(
            "type the secret's name to delete it: the body's `name` must equal the "
            "name in the path")
    answer = _store(ctx, writer, authority).delete(tenant_id=tenant_id, name=name,
                                                   by=auth.email, repo_id=repo_id)
    return {**answer, "authority": authority}


@router.put(f"{BASE}/{{name}}/grants/{{consumer}}")
def set_grant(
    repo_id: str,
    name: str,
    consumer: str,
    body: GrantWrite,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """One cell of the repository's matrix, signed in grant form "v2" with
    the repository it is bound to. Takes effect at the next attempt."""
    _, _, authority = _writable(request, ctx, auth, tenant_id, repo_id)
    name = check_name(name)
    record = _store(ctx, writer, authority).set_grant(
        tenant_id=tenant_id, name=name, consumer=consumer, slot=body.slot,
        enabled=body.enabled, by=auth.email, repo_id=repo_id, hosts=body.hosts)
    return {"secret": record, "takes_effect": "at the next attempt", "authority": authority}


@router.post(f"{BASE}/links", status_code=status.HTTP_201_CREATED)
def mint_link(
    repo_id: str,
    body: LinkRequest,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """A one-time link to the repository's add or rotate form: ten minutes,
    single use, bound to this caller, this workspace, this repository, this
    action and (for rotate) this secret. It grants nothing: the write it
    opens is authorised again, GitHub asked again, when it is submitted."""
    _writable(request, ctx, auth, tenant_id, repo_id)
    store = _store(ctx, writer)
    name: str | None = None
    if body.action == "rotate":
        if body.name is None:
            raise ValidationFailed("a rotate link names the secret")
        name = check_name(body.name)
        store.find(tenant_id, name, repo_id)
    elif body.name is not None:
        raise ValidationFailed("an add link names no secret; the form asks for the name")
    return store.mint_link(email=auth.email, tenant_id=tenant_id, action=body.action,
                           name=name, console=ctx.settings.console_url, repo_id=repo_id)


@router.post(f"{BASE}:disable-grants")
def disable_grants(
    repo_id: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """Every grant on this repository's secrets, off and re-signed off. For a
    registration being removed (secrets-scopes.md §2.1): the release already
    withholds a repository secret whose registration is gone, and this makes
    re-registering it revive nothing. Works on a registration that no longer
    exists, so it is the manager rule alone: there is no repository left to
    ask GitHub about."""
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant, ctx.roles)
    return _store(ctx, writer, WORKSPACE_ADMIN).disable_repository_grants(
        tenant_id=tenant_id, repo_id=repo_id, by=auth.email)
