"""A registered repository's GitHub Actions secrets, write-only (SSC-ACTIONS-API, part of #2158).

    GET    /v1/repositories/{repo_id}/actions/secrets                      names and times, live;
                                                                           can_write, create_url
    GET    /v1/repositories/{repo_id}/actions/environments/{environment}/rule
                                                                           is it main-only?
    PUT    /v1/repositories/{repo_id}/actions/secrets/{name}               {value, environment?,
                                                                            confirm_environment?,
                                                                            link_token?}
    DELETE /v1/repositories/{repo_id}/actions/secrets/{name}               {confirm_name,
                                                                            environment?}
    POST   /v1/repositories/{repo_id}/actions/links                        {name, environment?}

Design: docs/design/secrets-scopes.md §3 and §6.2, with BGD's answers in its
§11; the service is `swarm_api.actions_secrets`, whose docstring says what
each answer changed. In short:

  * NO ROUTE CREATES, EDITS OR DELETES AN ENVIRONMENT (Q4). The one
    environment route is a GET; a missing environment is answered with the
    GitHub page where a person creates it.
  * WHO WRITES (Q6): a workspace manager AND a person GitHub calls admin of
    the repository, asked live with their own token
    (`github_repo_admin.require_repo_admin`). Every member of the workspace
    reads names and rules.
  * THE VALUE IS ACCEPTED ON ONE ROUTE AND RETURNED BY NONE. It is sealed in
    swarm-api (Q5) and dropped after GitHub's PUT; every check that can
    refuse the write runs first.

NO ROUTE TAKES A TENANT, A PERSON, AN IMAGE OR A COMMAND. The workspace is
`tenant_scope`; the repository is a `repo_id` that must be a registration OF
THAT WORKSPACE (another workspace's is 404).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import Field

from ..actions_secrets import ActionsSecrets, check_environment, check_secret_name
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ApiError, ValidationFailed
from ..forgeapp import Caller
from ..github_repo_admin import require_repo_admin
from ..schemas import StrictModel
from ..workspace_secrets import require_manager
from .access import get_access
from .repository_secrets import _registration
from .workspace_secrets import _tenant

log = logging.getLogger(__name__)

router = APIRouter(tags=["actions-secrets"])

BASE = "/v1/repositories/{repo_id}/actions"

#: Said beside every write: what SwarmCloud kept.
KEPT = ("SwarmCloud sent the value to GitHub sealed to its key and kept only the name, "
        "who set it and when")


class ActionsSecretWrite(StrictModel):
    #: The value, once. Sealed here and dropped after GitHub's PUT.
    value: str = Field(min_length=1, max_length=48 * 1024, repr=False)
    environment: str | None = Field(default=None, max_length=255)
    #: Q4: the environment's name, typed, when its rule is not main-only.
    confirm_environment: str | None = Field(default=None, max_length=255)
    link_token: str | None = Field(default=None, max_length=256, repr=False)


class ActionsSecretDelete(StrictModel):
    #: The typed confirmation: the secret's name, repeated.
    confirm_name: str = Field(max_length=200)
    environment: str | None = Field(default=None, max_length=255)


class ActionsLinkRequest(StrictModel):
    name: str = Field(max_length=200)
    environment: str | None = Field(default=None, max_length=255)


def actions_secrets(request: Request, ctx: AppContext = Depends(get_context)) -> ActionsSecrets:
    """The service, built once per process on `app.state` (a test hangs one
    over fakes there). Over the existing App: its installation mint (the
    context's, else one built over the App, which builds no client) and its
    transport."""
    service = getattr(request.app.state, "actions_secrets", None)
    if service is None:
        mint = getattr(ctx, "forge_installations", None)
        if not callable(getattr(mint, "actions_token", None)):
            mint = None
            if ctx.settings.project_id:
                from ..forge_installation import build_installation_tokens

                mint = build_installation_tokens(ctx.db, ctx.settings.project_id, now=ctx.now)
        forge_app = request.app.state.forge_app
        service = ActionsSecrets(ctx.db, mint=mint, send=forge_app.send, now=ctx.now,
                                 app_slug=forge_app.config.slug)
        request.app.state.actions_secrets = service
    return service


def _writer(request: Request, ctx: AppContext, auth: AuthContext, tenant_id: str,
            repo_id: str) -> dict[str, Any]:
    """Q6: the registration (404 first), the manager rule, then GitHub's
    admin rule asked now with the caller's own token. Both, never either."""
    tenant = _tenant(ctx, tenant_id)
    registration = _registration(ctx, tenant_id, repo_id)
    require_manager(auth, tenant, ctx.roles)
    _require_github_admin(request, ctx, auth, tenant_id, registration)
    return registration


def _require_github_admin(request: Request, ctx: AppContext, auth: AuthContext, tenant_id: str,
                          registration: dict[str, Any]) -> None:
    answer = require_repo_admin(get_access(request), ctx.forge,
                                Caller(email=auth.email, tenant_id=tenant_id),
                                str(registration["owner"]), str(registration["repo"]))
    log.info("actions secret writer tenant=%s repo_id=%s login=%s", tenant_id,
             registration["repo_id"], answer.login)


def _can_write(request: Request, ctx: AppContext, auth: AuthContext, tenant: Any,
               tenant_id: str, registration: dict[str, Any]) -> dict[str, Any]:
    """What the console offers, from the SAME two checks `_writer` runs, in
    its order (the functions called, never restated). A refusal of either is
    `can_write: false` with its sentence, never an error for the read. It
    decides only what the pane draws: every write route runs `_writer` again,
    and none reads this answer back. `checked_at` is when GitHub was asked;
    null when the manager rule refused first and GitHub was not."""
    checked_at = None
    try:
        require_manager(auth, tenant, ctx.roles)
        checked_at = ctx.now().isoformat()
        _require_github_admin(request, ctx, auth, tenant_id, registration)
    except ApiError as refused:
        return {"can_write": False, "cannot_write_because": refused.message,
                "checked_at": checked_at}
    return {"can_write": True, "cannot_write_because": None, "checked_at": checked_at}


@router.get(f"{BASE}/secrets")
def list_secrets(
    repo_id: str,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    service: ActionsSecrets = Depends(actions_secrets),
) -> dict:
    """GitHub's names and times, never cached; GitHub serves no value. Every
    member reads; `can_write` says whether this caller would pass a write's
    checks, asked after the names so a refused read costs no GitHub call."""
    tenant = _tenant(ctx, tenant_id)
    registration = _registration(ctx, tenant_id, repo_id)
    listed = service.list(tenant_id, registration)
    return {**listed, "tenant_id": tenant_id, "repo_id": repo_id,
            "repository": f"{registration['owner']}/{registration['repo']}",
            **_can_write(request, ctx, auth, tenant, tenant_id, registration)}


@router.get(f"{BASE}/environments/{{environment}}/rule")
def check_rule(
    repo_id: str,
    environment: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    service: ActionsSecrets = Depends(actions_secrets),
) -> dict:
    """Q4's "Check rule": whether the environment exists and lets only the
    default branch deploy; for a missing one, GitHub's page to create it."""
    _tenant(ctx, tenant_id)
    registration = _registration(ctx, tenant_id, repo_id)
    return {"repo_id": repo_id, **service.rule(tenant_id, registration,
                                               check_environment(environment) or "")}


@router.put(f"{BASE}/secrets/{{name}}")
def set_secret(
    repo_id: str,
    name: str,
    body: ActionsSecretWrite,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    service: ActionsSecrets = Depends(actions_secrets),
) -> dict:
    name = check_secret_name(name)
    environment = check_environment(body.environment)
    registration = _writer(request, ctx, auth, tenant_id, repo_id)
    rule = None
    if environment is not None:
        rule = service.guard_environment(tenant_id, registration, environment,
                                         body.confirm_environment)
    via_link = service.consume_link(body.link_token, email=auth.email, tenant_id=tenant_id,
                                    repo_id=repo_id, environment=environment, name=name)
    answer = service.set(tenant_id=tenant_id, registration=registration, name=name,
                         environment=environment, value=body.value, by=auth.email,
                         via_link=via_link)
    return {**answer, "via_link": via_link, "rule": rule["rule"] if rule else None,
            "note": KEPT}


@router.delete(f"{BASE}/secrets/{{name}}")
def delete_secret(
    repo_id: str,
    name: str,
    body: ActionsSecretDelete,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    service: ActionsSecrets = Depends(actions_secrets),
) -> dict:
    """The typed confirmation: the body repeats the secret's name."""
    name = check_secret_name(name)
    if body.confirm_name.upper() != name:
        raise ValidationFailed("type the secret's name to delete it: `confirm_name` must equal "
                               "the name in the path")
    environment = check_environment(body.environment)
    registration = _writer(request, ctx, auth, tenant_id, repo_id)
    return service.delete(tenant_id=tenant_id, registration=registration, name=name,
                          environment=environment, by=auth.email)


@router.post(f"{BASE}/links", status_code=status.HTTP_201_CREATED)
def mint_link(
    body: ActionsLinkRequest,
    repo_id: str,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    service: ActionsSecrets = Depends(actions_secrets),
) -> dict:
    """A one-time link to the Set form: ten minutes, single use, bound to
    this caller, workspace, repository, environment and name. It grants
    nothing: the write it opens is authorised again, GitHub asked again."""
    name = check_secret_name(body.name)
    environment = check_environment(body.environment)
    _writer(request, ctx, auth, tenant_id, repo_id)
    return service.mint_link(email=auth.email, tenant_id=tenant_id, repo_id=repo_id,
                             environment=environment, name=name,
                             console=ctx.settings.console_url)
