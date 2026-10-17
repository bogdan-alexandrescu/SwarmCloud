"""A workspace's Jev judgment backend (JUDGE-SELECT; `swarm_api.judgment`).

    GET  /v1/judgment                              the selected workspace's backend,
                                                   its label and availability
    GET  /v1/workspaces/{tenant_id}/judgment-backend   the same, for that workspace
    PUT  /v1/workspaces/{tenant_id}/judgment-backend   choose it: its owner or an admin

READ: a workspace is read by a caller whose selected tenant it is
(`tenant_scope`, through the collision guard) or by a platform admin; any
other id is a 404, so the route does not say which workspaces exist.

WRITE: the owner of a personal workspace -- `kind: user`, its principal the
caller's verified email -- or a platform admin (`require_admin`, which
answers 503 rather than 403 when the directory could not be asked). Every
write is an `admin_audit` entry in the same transaction. The body forbids
every field it does not name, so a URL or a key is a 422, never ignored:
the backend is picked by NAME, and its address is the platform's.

`available` is for THIS caller: TypeSafe is available when a TypeSafe key
resolves for them in that workspace -- their own, else the tenant's -- the
same resolution `GET /v1/provider-keys/typesafe` reports.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext, require_admin
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import Forbidden, NotFound
from ..judgment import JudgmentSettings, check_backend, jev_status, settings_mac
from ..provider_keys import TYPESAFE, ProviderKeys, own_personal_tenant, resolve_provider_key
from ..schemas import StrictModel

router = APIRouter(tags=["judgment"])


class JudgmentBackendWrite(StrictModel):
    judgment_backend: str


def typesafe_key_resolves(ctx: AppContext, auth: AuthContext, tenant_id: str) -> bool:
    """Whether a TypeSafe key would be used for this caller's task in `tenant_id`."""
    keys = ProviderKeys(ctx.db, now=ctx.now)
    try:
        personal: str | None = own_personal_tenant(ctx.store, ctx.db, auth).tenant_id
    except NotFound:
        personal = None
    present = {s["secret_id"] for s in (
        keys.status(personal, TYPESAFE) if personal else keys.unavailable(),
        keys.status(tenant_id, TYPESAFE),
    ) if s["set"]}
    return resolve_provider_key(tenant_id, personal, TYPESAFE, present) is not None


def jev_for(ctx: AppContext, auth: AuthContext, tenant_id: str, *,
            typesafe_key: bool | None = None) -> dict[str, Any]:
    """The `jev` object for `tenant_id` as this caller sees it."""
    stored = JudgmentSettings(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings)).read(tenant_id)
    if typesafe_key is None:
        typesafe_key = typesafe_key_resolves(ctx, auth, tenant_id)
    return jev_status(
        stored["judgment_backend"],
        internal_enabled=bool(ctx.settings.internal_decision_api_url),
        typesafe_key=typesafe_key,
        set_at=stored["set_at"],
        set_by=stored["set_by"],
    )


def _is_owner(tenant: Tenant, auth: AuthContext) -> bool:
    email = (auth.email or "").strip().lower()
    return bool(email) and tenant.kind == "user" and (
        tenant.principal or "").strip().lower() == email


def _body(ctx: AppContext, auth: AuthContext, tenant: Tenant) -> dict[str, Any]:
    jev = jev_for(ctx, auth, tenant.tenant_id)
    return {
        "tenant_id": tenant.tenant_id,
        "judgment_backend": jev["backend"],
        "jev": jev,
        "can_edit": bool(auth.is_admin) or _is_owner(tenant, auth),
    }


def _readable(ctx: AppContext, auth: AuthContext, tenant_id: str, scope: str) -> Tenant:
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None or (tenant_id != scope and not auth.is_admin):
        raise NotFound(f"workspace {tenant_id!r} not found")
    return tenant


@router.get("/v1/judgment")
def get_judgment(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's Jev backend, for the Submit picker and the strip."""
    return _body(ctx, auth, _readable(ctx, auth, tenant_id, tenant_id))


@router.get("/v1/workspaces/{tenant_id}/judgment-backend")
def get_judgment_backend(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    scope: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return _body(ctx, auth, _readable(ctx, auth, tenant_id, scope))


@router.put("/v1/workspaces/{tenant_id}/judgment-backend")
def put_judgment_backend(
    tenant_id: str,
    body: JudgmentBackendWrite,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    backend = check_backend(body.judgment_backend)
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        if not auth.is_admin:
            raise Forbidden("only a workspace's owner or a platform admin can choose its "
                            "judgment backend")
        raise NotFound(f"workspace {tenant_id!r} not found")
    if not _is_owner(tenant, auth):
        try:
            require_admin(auth)
        except Forbidden:
            raise Forbidden("only a workspace's owner or a platform admin can choose its "
                            "judgment backend") from None
    JudgmentSettings(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings)).set(
        tenant_id, backend, by=auth.email)
    return _body(ctx, auth, tenant)
