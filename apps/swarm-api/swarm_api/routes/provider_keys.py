"""Provider keys a person or a tenant admin sets: TypeSafe first (KEYS-API).

    GET    /v1/provider-keys/{provider}         whether your key and your tenant's are set,
                                                and which your tasks here would use
    PUT    /v1/provider-keys/{provider}/user    store YOUR key, in your personal tenant
    DELETE /v1/provider-keys/{provider}/user    destroy it
    PUT    /v1/provider-keys/{provider}/tenant  store the tenant key (admins)
    DELETE /v1/provider-keys/{provider}/tenant  destroy it (admins)
    POST   /v1/provider-keys/{provider}/links   a one-time link to the console's key form

THE KEY IS ACCEPTED ON THE TWO PUTS AND NOWHERE ELSE, and no route returns it,
its length, a prefix or a suffix (docs/provider-keys.md). The console form
and a terminal hidden prompt call them; no MCP tool does. The record and the
audit are `swarm_api.provider_keys`'s; the Secret Manager write is
`swarm_api.credentials`', as it is for every tenant provider key.

NO ROUTE NAMES A PERSON OR A TENANT. The user routes act on the caller's own
personal tenant, derived from the verified email; the tenant routes on
`tenant_scope`, the caller's selected tenant through the collision guard.
The bodies forbid every field they do not name, so a `user` or `tenant_id`
is a 422 rather than ignored. "Admin" is the platform's `is_admin`
(`admin_auth`): there is no per-tenant admin role to ask instead, exactly as
on the Git token routes.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, status
from pydantic import Field

from swarm_common.models import Tenant

from ..auth import AuthContext, require_admin
from ..credentials import validate_key_material
from ..deps import AppContext, admin_auth, current_auth, get_context, tenant_scope
from ..errors import NotFound, ValidationFailed
from ..provider_keys import (
    TENANT,
    TYPESAFE,
    USER,
    ProviderKeys,
    check_provider,
    own_personal_tenant,
    resolve_provider_key,
)
from ..schemas import StrictModel
from .judgment import jev_for

router = APIRouter(prefix="/v1/provider-keys", tags=["provider-keys"])

NOTE = "stored write-only; no route in this API can return key material"


class ProviderKeyWrite(StrictModel):
    #: Write-only. No response in this service carries it, its length or any
    #: part of it, and the validation handler never echoes a body.
    api_key: str = Field(min_length=8, max_length=8192)
    #: The `t` of a one-time link (`POST .../links`), when the form was opened
    #: from one. Optional: the caller's own session is what authorises.
    link_token: str | None = Field(default=None, max_length=256)


class LinkRequest(StrictModel):
    scope: Literal["user", "tenant"]


def _keys(ctx: AppContext) -> ProviderKeys:
    return ProviderKeys(ctx.db, now=ctx.now)


@router.get("/{provider}")
def get_status(
    provider: str,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Your own key, your selected tenant's, and which a task you submit here
    would use. Never a value: `set`, `set_at`, `set_by`, `version`."""
    provider = check_provider(provider)
    keys = _keys(ctx)
    try:
        personal: str | None = own_personal_tenant(ctx.store, ctx.db, auth).tenant_id
    except NotFound:
        personal = None
        user = keys.unavailable()
    else:
        user = keys.status(personal, provider)
    tenant = {**keys.status(tenant_id, provider), "tenant_id": tenant_id}
    present = {s["secret_id"] for s in (user, tenant) if s["set"]}
    chosen = resolve_provider_key(tenant_id, personal, provider, present)
    uses = "none" if chosen is None else (USER if chosen == user["secret_id"] else TENANT)
    body = {
        "provider": provider,
        "user": user,
        "tenant": tenant,
        "resolution": {"tenant_id": tenant_id, "uses": uses, "secret_id": chosen},
        # No runner reads these keys yet (no Jev runner exists); the
        # resolution says what one would use, not what anything uses now.
        "consumed_by": [],
    }
    if provider == TYPESAFE:
        # Which backend this workspace's Jev judges with (JUDGE-SELECT): a
        # TypeSafe key is needed only when it is `typesafe`.
        body["jev"] = jev_for(ctx, auth, tenant_id, typesafe_key=chosen is not None)
    return body


@router.put("/{provider}/user")
def put_user_key(
    provider: str,
    body: ProviderKeyWrite,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = check_provider(provider)
    validate_key_material(body.api_key)
    tenant = own_personal_tenant(ctx.store, ctx.db, auth)
    return _store(ctx, auth, tenant, provider, USER, body)


@router.delete("/{provider}/user")
def delete_user_key(
    provider: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = check_provider(provider)
    tenant = own_personal_tenant(ctx.store, ctx.db, auth, require_ready=False)
    return _destroy(ctx, auth, tenant, provider, USER)


@router.put("/{provider}/tenant")
def put_tenant_key(
    provider: str,
    body: ProviderKeyWrite,
    auth: AuthContext = Depends(admin_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = check_provider(provider)
    validate_key_material(body.api_key)
    return _store(ctx, auth, _group_tenant(ctx, tenant_id), provider, TENANT, body)


@router.delete("/{provider}/tenant")
def delete_tenant_key(
    provider: str,
    auth: AuthContext = Depends(admin_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = check_provider(provider)
    return _destroy(ctx, auth, _group_tenant(ctx, tenant_id), provider, TENANT)


@router.post("/{provider}/links", status_code=status.HTTP_201_CREATED)
def mint_link(
    provider: str,
    body: LinkRequest,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A one-time link to the console's key form, for the /sc flow: single
    use, `KEY_LINK_TTL`, bound to this caller, scope and tenant. Minted only
    for a write this caller could make: their own workspace for `user`, an
    admin for `tenant`."""
    provider = check_provider(provider)
    if body.scope == USER:
        target = own_personal_tenant(ctx.store, ctx.db, auth).tenant_id
    else:
        # The full-admin rule: with no route, no narrower capability (a pool
        # admin's) gets through.
        require_admin(auth)
        target = tenant_id
    return _keys(ctx).mint_link(email=auth.email, scope=body.scope, provider=provider,
                                tenant_id=target, console=ctx.settings.console_url)


def _group_tenant(ctx: AppContext, tenant_id: str) -> Tenant:
    """The selected tenant for a TENANT key, which must be a group's. In a
    personal workspace the tenant key would be the same secret as that
    person's own key, recorded under the wrong scope: that is `.../user`."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} does not exist yet")
    if tenant.kind == "user":
        raise ValidationFailed(
            f"{tenant_id!r} is a personal workspace; its key is set with "
            "PUT /v1/provider-keys/{provider}/user by its owner. Select a group "
            "tenant (X-Swarm-Tenant) to set a tenant key"
        )
    return tenant


def _store(ctx: AppContext, auth: AuthContext, tenant: Tenant, provider: str, scope: str,
           body: ProviderKeyWrite) -> dict:
    keys = _keys(ctx)
    via_link = keys.check_link(body.link_token, email=auth.email, scope=scope,
                               provider=provider, tenant_id=tenant.tenant_id)
    result = ctx.credentials.put_credential(tenant, provider, body.api_key)
    ctx.metrics.credentials_written.labels(tenant=tenant.tenant_id, provider=provider).inc()
    recorded = keys.record(tenant_id=tenant.tenant_id, provider=provider, scope=scope,
                           by=auth.email, version=result.version, via_link=via_link)
    return {"status": recorded, "scope": scope, "via_link": via_link, "note": NOTE}


def _destroy(ctx: AppContext, auth: AuthContext, tenant: Tenant, provider: str,
             scope: str) -> dict:
    keys = _keys(ctx)
    tenant_id = tenant.tenant_id
    was_set = keys.status(tenant_id, provider)["set"]
    destroyed = ctx.credentials.delete_credential(tenant, provider)
    if not destroyed and not was_set:
        raise NotFound(f"no {provider} key is set for {scope} scope")
    recorded = keys.record(tenant_id=tenant_id, provider=provider, scope=scope,
                           by=auth.email, version=None, via_link=False)
    return {"status": recorded, "scope": scope, "versions_destroyed": destroyed}
