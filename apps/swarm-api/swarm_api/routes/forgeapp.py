"""The GitHub App's user-authorisation routes (docs/onboarding.md §3.2; #780,
lane OB3). The flow is `swarm_api.forgeapp`'s.

    POST   /v1/onboarding/github/authorize  {surface} -> {authorize_url, expires_in_seconds}
    POST   /v1/onboarding/github/exchange   {state, code} or {state, error} -> the
                                            connection, never a token
    POST   /v1/onboarding/github/token      {owner, token} (D5): a personal access token
                                            for ONE owner, probed, then stored once as
                                            a version of the caller's per-owner slot;
                                            answers the org and the token's names
    DELETE /v1/onboarding/github            disconnect: revoke at GitHub, disable the
                                            slot's versions and every owner token's,
                                            delete the caller's grants
    POST   /v1/admin/forge/refresh          the refresh sweep (D2): the Cloud Scheduler
                                            job swarm-forge-refresh, as the rollup
                                            sweeper, or an admin
    GET    /v1/admin/forge/app              the private key's self-check: signs an App
                                            JWT and reads the App back from GitHub;
                                            {ok, app_id, slug, reason}, admins only

NO ROUTE RETURNS A VALUE: not the user access token, the refresh token, the
App's client secret, the `code`, or the `state` after `authorize` issued it.
The bodies forbid every field they do not name, so a token posted in any
field is a 422 that does not echo it.

THE TOKEN ROUTE (D5) is the one route a value arrives on, from `uv run sc
setup token --owner <org>` (stdin, no echo) or the console's Admin settings
page. The value is a `SecretStr` from the moment it is parsed, is registered
with the redaction filter for as long as it is in hand, and goes to GitHub's
API and into Secret Manager and nowhere else.

The tenant comes from `tenant_scope` and the person from the verified
identity, never the body: the state is bound to both, so the exchange is
the platform's ordinary sign-in deciding whose GitHub account this is.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..access import org_to_api
from ..auth import AuthContext
from ..deps import admin_auth, current_auth, tenant_scope
from ..forgeapp import Caller, ForgeApp, TokenRefresher
from ..validation import _ISSUE_OWNER

router = APIRouter(tags=["onboarding"])


def get_forge_app(request: Request) -> ForgeApp:
    """The service `create_app` hung on the app; a test injects one over fakes."""
    return request.app.state.forge_app


class AuthorizeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    surface: Literal["console", "plugin"] = "console"


class ExchangeBody(BaseModel):
    """What the callback page posts: GitHub's `state` and `code`, or the
    `error` GitHub sent instead of a code."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    state: str = Field(min_length=1, max_length=256)
    code: str | None = Field(default=None, min_length=1, max_length=256)
    #: GitHub's error code on the callback, e.g. `access_denied`.
    error: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")


class OwnerTokenBody(BaseModel):
    """D5: a personal access token for one owner. `owner` and `token` and
    nothing else -- a tenant, a user or a scope in the body is a 422 that
    does not echo the value (the app's validation handler serves the field
    name and the rule, never the input)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner: str = Field(min_length=1, max_length=39, pattern=rf"^{_ISSUE_OWNER}$")
    #: Fine-grained tokens are 93 characters; the bound is generous and finite.
    token: SecretStr = Field(min_length=1, max_length=512)


@router.post("/v1/onboarding/github/authorize")
def authorize(
    body: AuthorizeBody | None = None,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: ForgeApp = Depends(get_forge_app),
) -> dict:
    surface = body.surface if body is not None else "console"
    return service.authorize(Caller(email=auth.email, tenant_id=tenant_id), surface)


@router.post("/v1/onboarding/github/exchange")
def exchange(
    body: ExchangeBody,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: ForgeApp = Depends(get_forge_app),
) -> dict:
    return service.exchange(Caller(email=auth.email, tenant_id=tenant_id), state=body.state,
                            code=body.code, error=body.error)


@router.post("/v1/onboarding/github/token")
def store_owner_token(
    body: OwnerTokenBody,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: ForgeApp = Depends(get_forge_app),
) -> dict:
    answer = service.store_owner_token(Caller(email=auth.email, tenant_id=tenant_id),
                                       body.owner, body.token.get_secret_value().strip())
    return {"org": org_to_api(answer["org"]), "token": answer["token"]}


@router.delete("/v1/onboarding/github")
def disconnect(
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    service: ForgeApp = Depends(get_forge_app),
) -> dict:
    return service.disconnect(Caller(email=auth.email, tenant_id=tenant_id))


@router.post("/v1/admin/forge/refresh")
def refresh_sweep(
    _auth: AuthContext = Depends(admin_auth),
    service: TokenRefresher = Depends(get_forge_app),
) -> dict:
    return service.sweep().to_api()


@router.get("/v1/admin/forge/app")
def app_key_check(
    _auth: AuthContext = Depends(admin_auth),
    service: ForgeApp = Depends(get_forge_app),
) -> dict:
    return service.check_app_key()
