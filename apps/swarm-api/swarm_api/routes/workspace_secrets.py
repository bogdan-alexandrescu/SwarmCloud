"""The workspace secrets store's management API (SEC-STORE-API, part of #1347).

    GET    /v1/workspace/secrets                         records, grants, the read-only
                                                         provider keys and pool accounts;
                                                         never a value
    GET    /v1/workspace/secrets/consumers               the grant catalogue, with slots
    POST   /v1/workspace/secrets                         add (the value, once)
    PUT    /v1/workspace/secrets/{name}/value            rotate
    DELETE /v1/workspace/secrets/{name}                  destroy; the body types the name
    PUT    /v1/workspace/secrets/{name}/grants/{consumer}  {enabled, slot?}
    PUT    /v1/workspace/secrets/{name}/use-in           Q3: a personal secret's opt-in list
    POST   /v1/workspace/secrets/links                   a one-time link to the add/rotate form
    POST   /v1/admin/workspace-secrets/rotation-sweep    Q5: disable versions past their grace
    POST   /v1/admin/workspace-secrets/attest-grants     SEC-GRANT-ATTEST: sign grants written
                                                         before attestations, by their audit row

Design: docs/design/workspace-secrets.md §5. There is NO release route here:
handing a value to a worker is SEC-RELEASE (phase 2), behind its own owner
decision (§9 Q2). Nothing in this file reads a payload.

NO ROUTE TAKES A TENANT, A PERSON, AN IMAGE OR A COMMAND. Every route acts on
`tenant_scope`; a consumer is a catalogue name; the bodies forbid every field
they do not name, so a `tenant_id` or an `image` is a 422 rather than ignored.

THE VALUE IS ACCEPTED ON TWO ROUTES (add, rotate) AND RETURNED BY NONE. Every
check that can refuse a write -- the workspace, the manager rule, the name,
the kind, the shape, the link -- runs before the value reaches Secret
Manager, and the validation handler (`main.create_app`) never echoes a body.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import Field

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..brokerclient import AccountPool
from ..deps import AppContext, admin_auth, current_auth, get_context, tenant_scope
from ..errors import NotFound, ValidationFailed
from ..grant_attest import GrantPolicy
from ..schemas import StrictModel
from ..workspace_secrets import (
    CONSUMERS,
    KINDS,
    MAX_USE_IN,
    MAX_VALUE_BYTES,
    PROVIDERS,
    SecretManagerWorkspaceSecrets,
    SecretWriter,
    WorkspaceSecrets,
    can_manage,
    check_kind,
    check_name,
    check_provider,
    legacy_rows,
    payload_for,
    refuse_name_in_value,
    require_manager,
)
from .accounts import account_pool

log = logging.getLogger(__name__)

router = APIRouter(tags=["workspace-secrets"])

BASE = "/v1/workspace/secrets"
NOTE = "stored write-only; no route in this API returns a secret's value"


class SecretCreate(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(max_length=32)
    kind: str = Field(max_length=32)
    #: Write-only, for every kind but the structured two. No response carries
    #: it, its length or any part of it.
    value: str | None = Field(default=None, max_length=MAX_VALUE_BYTES)
    #: Write-only, for `basic_auth` (username, password) and `oauth_client`
    #: (client_id, client_secret).
    #: `Any`, not `str`: pydantic's error for a non-string entry puts the
    #: entry's KEY in `loc`, and a person can paste a value into a key.
    #: `payload_for` checks every entry with constant messages instead.
    fields: dict[str, Any] | None = Field(default=None, max_length=2)
    link_token: str | None = Field(default=None, max_length=256)


class SecretValue(StrictModel):
    value: str | None = Field(default=None, max_length=MAX_VALUE_BYTES)
    fields: dict[str, Any] | None = Field(default=None, max_length=2)
    link_token: str | None = Field(default=None, max_length=256)


class SecretDelete(StrictModel):
    #: The typed confirmation (§5): the secret's name, repeated.
    name: str = Field(max_length=64)


class GrantWrite(StrictModel):
    enabled: bool
    #: Defaults to the consumer's only slot when it has one (§2.2).
    slot: str | None = Field(default=None, max_length=32)


class UseInWrite(StrictModel):
    workspaces: list[str] = Field(max_length=MAX_USE_IN)


class LinkRequest(StrictModel):
    action: Literal["add", "rotate"]
    name: str | None = Field(default=None, max_length=64)


def secret_writer(request: Request, ctx: AppContext = Depends(get_context)) -> SecretWriter:
    """The Secret Manager writer, built once per process on `app.state` (as
    `account_pool` is) so a test substitutes one without rebuilding the
    context. Constructing it opens no connection and reads no credential."""
    writer = getattr(request.app.state, "workspace_secret_writer", None)
    if writer is None:
        writer = SecretManagerWorkspaceSecrets(ctx.settings.project_id)
        request.app.state.workspace_secret_writer = writer
    return writer


def _store(ctx: AppContext, writer: SecretWriter) -> WorkspaceSecrets:
    return WorkspaceSecrets(ctx.db, writer, now=ctx.now,
                            grants=GrantPolicy.from_settings(ctx.settings))


def _tenant(ctx: AppContext, tenant_id: str) -> Tenant:
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} does not exist yet")
    return tenant


@router.get(BASE)
def list_secrets(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Every member reads names, kinds, versions, grants, who and when (Q4).
    Never a value, a length or the last characters."""
    tenant = _tenant(ctx, tenant_id)
    payload: dict[str, Any] | None = None
    error: str | None = None
    try:
        payload = pool.list_accounts(tenant_id)
    except Exception as exc:  # noqa: BLE001 - a side listing never breaks the page
        # The class only: a broker error's text is not this page's to repeat.
        error = f"the account pool could not be read ({type(exc).__name__})"
        log.warning("workspace secrets: pool listing unreadable tenant=%s error=%s",
                    tenant_id, type(exc).__name__)
    return {
        "tenant_id": tenant_id,
        "can_manage": can_manage(auth, tenant),
        "secrets": _store(ctx, writer).list(tenant_id),
        "platform_credentials": legacy_rows(tenant, payload, error),
        "note": NOTE,
    }


@router.get(f"{BASE}/consumers")
def list_consumers(_auth: AuthContext = Depends(current_auth)) -> dict:
    return {
        "consumers": [c.to_api() for c in CONSUMERS],
        "kinds": list(KINDS),
        "providers": list(PROVIDERS),
    }


@router.post(BASE, status_code=status.HTTP_201_CREATED)
def create_secret(
    body: SecretCreate,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant)
    name = check_name(body.name)
    provider = check_provider(body.provider)
    kind = check_kind(body.kind)
    payload = payload_for(kind, body.value, body.fields)
    refuse_name_in_value(name, payload)
    store = _store(ctx, writer)
    via_link = store.consume_link(body.link_token, email=auth.email, tenant_id=tenant_id,
                                  action="add", name=name)
    record = store.create(tenant=tenant, name=name, provider=provider, kind=kind,
                          payload=payload, by=auth.email, via_link=via_link)
    return {"secret": record, "via_link": via_link, "note": NOTE}


@router.put(f"{BASE}/{{name}}/value")
def rotate_secret(
    name: str,
    body: SecretValue,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """Q5: the previous version stays enabled for 24 hours, then the
    rotation sweep disables it; it is never destroyed by a rotation."""
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant)
    name = check_name(name)
    store = _store(ctx, writer)
    kind = str(store.find(tenant_id, name).get("kind"))
    payload = payload_for(kind, body.value, body.fields)
    via_link = store.consume_link(body.link_token, email=auth.email, tenant_id=tenant_id,
                                  action="rotate", name=name)
    record = store.rotate(tenant_id=tenant_id, name=name, kind_payload=lambda _kind: payload,
                          by=auth.email, via_link=via_link)
    return {"secret": record, "via_link": via_link, "note": NOTE}


@router.delete(f"{BASE}/{{name}}")
def delete_secret(
    name: str,
    body: SecretDelete,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """The typed confirmation: the body repeats the secret's name exactly.
    Running attempts keep the copy they were given (§4.3)."""
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant)
    name = check_name(name)
    if body.name != name:
        raise ValidationFailed(
            "type the secret's name to delete it: the body's `name` must equal the "
            "name in the path")
    return _store(ctx, writer).delete(tenant_id=tenant_id, name=name, by=auth.email)


@router.put(f"{BASE}/{{name}}/grants/{{consumer}}")
def set_grant(
    name: str,
    consumer: str,
    body: GrantWrite,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """Tick or untick one cell of the matrix. Takes effect at the next
    attempt: a running attempt keeps what it was released (§4.3)."""
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant)
    name = check_name(name)
    record = _store(ctx, writer).set_grant(tenant_id=tenant_id, name=name, consumer=consumer,
                                           slot=body.slot, enabled=body.enabled, by=auth.email)
    return {"secret": record, "takes_effect": "at the next attempt"}


@router.put(f"{BASE}/{{name}}/use-in")
def set_use_in(
    name: str,
    body: UseInWrite,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """Q3: "use in my submissions to <workspace>". Only on a personal secret,
    only by its person, and only naming group workspaces the caller is a
    confirmed member of now (`auth.tenant_choices`). Only tasks that person
    submits there may be released it (SEC-RELEASE reads this list)."""
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant)
    name = check_name(name)
    mine = {choice[0] for choice in auth.tenant_choices}
    wanted: list[str] = []
    for workspace in body.workspaces:
        if workspace in wanted:
            continue
        target = ctx.store.get_tenant(workspace) if workspace in mine else None
        if target is None or target.kind == "user" or workspace == tenant_id:
            raise ValidationFailed(
                "each workspace must be a group workspace you are a member of",
                detail={"your_group_workspaces": sorted(
                    t for t in mine if t != tenant_id and not t.startswith("u-"))})
        wanted.append(workspace)
    record = _store(ctx, writer).set_use_in(tenant_id=tenant_id, name=name,
                                            workspaces=wanted, by=auth.email)
    return {"secret": record}


@router.post(f"{BASE}/links", status_code=status.HTTP_201_CREATED)
def mint_link(
    body: LinkRequest,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """A one-time link to the console's add or rotate form (§5, the
    provider-keys §3 flow): ten minutes, single use, bound to this caller,
    this workspace, this action and (for rotate) this secret. Minted only for
    a write the caller could make; it grants nothing more."""
    tenant = _tenant(ctx, tenant_id)
    require_manager(auth, tenant)
    store = _store(ctx, writer)
    name: str | None = None
    if body.action == "rotate":
        if body.name is None:
            raise ValidationFailed("a rotate link names the secret")
        name = check_name(body.name)
        store.find(tenant_id, name)
    elif body.name is not None:
        raise ValidationFailed("an add link names no secret; the form asks for the name")
    return store.mint_link(email=auth.email, tenant_id=tenant_id, action=body.action,
                           name=name, console=ctx.settings.console_url)


@router.post("/v1/admin/workspace-secrets/rotation-sweep")
def rotation_sweep(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """Q5: disable every previous version whose 24-hour grace has passed,
    across workspaces, each with its audit row. Never destroys."""
    return _store(ctx, writer).sweep_rotations(by=auth.email)


@router.post("/v1/admin/workspace-secrets/attest-grants")
def attest_grants(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    writer: SecretWriter = Depends(secret_writer),
) -> dict:
    """SEC-GRANT-ATTEST's migration (docs/runbooks/workspace-grant-attestation.md):
    sign every grant whose fields match its newest audited write, re-sign one
    made under the previous key, and list by id every grant it would not sign.
    Platform admins only, as the rotation sweep. Answers ids, never a MAC."""
    return _store(ctx, writer).attest_grants(by=auth.email)
