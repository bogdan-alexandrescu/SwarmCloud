"""The GitHub App's user-authorisation routes (docs/onboarding.md §3.2; #780,
lane OB3). The flow is `swarm_api.forgeapp`'s.

    POST   /v1/onboarding/github/authorize  {surface} -> {authorize_url, expires_in_seconds}
    POST   /v1/onboarding/github/exchange   {state, code} or {state, error} -> the
                                            connection, never a token
    DELETE /v1/onboarding/github            disconnect: revoke at GitHub, disable the
                                            slot's versions, delete the caller's grants
    POST   /v1/admin/forge/refresh          the refresh sweep (D2): the Cloud Scheduler
                                            job swarm-forge-refresh, as the rollup
                                            sweeper, or an admin

NO ROUTE RETURNS A VALUE: not the user access token, the refresh token, the
App's client secret, the `code`, or the `state` after `authorize` issued it.
The bodies forbid every field they do not name, so a token posted in any
field is a 422 that does not echo it.

The tenant comes from `tenant_scope` and the person from the verified
identity, never the body: the state is bound to both, so the exchange is
the platform's ordinary sign-in deciding whose GitHub account this is.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth import AuthContext
from ..deps import admin_auth, current_auth, tenant_scope
from ..forgeapp import Caller, ForgeApp, TokenRefresher

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
