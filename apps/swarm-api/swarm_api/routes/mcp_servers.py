"""Registered MCP servers' API, and the per-run credential sweep (MCP-API, part of #1433).

    GET   /v1/mcp-servers                       the workspace's registrations
    GET   /v1/mcp-servers/{name}                one; another workspace's is 404
    POST  /v1/mcp-servers                       register (Q1: admins, or a person
                                                in their own workspace)
    PATCH /v1/mcp-servers/{name}                url, description, auth, allowed_profiles,
                                                allowed_tools, limits, enabled
    POST  /v1/admin/mcp-credentials/sweep       destroy every per-run credential
                                                whose task ended or whose expiry passed

Design: docs/design/registered-mcp-servers.md §4.2, §4.4 and §4.11. There is no
DELETE: a server is disabled, never removed, so "which URL did attempt X call"
keeps an answer (`revision` on every attach row).

NO ROUTE TAKES A TENANT, AND NO RESPONSE CARRIES A CREDENTIAL: a registration
has none to carry. The bodies forbid every field they do not name, so a
`tenant_id`, an `image` or a `credential` is a 422 rather than ignored, and
the validation handler (`main.create_app`) never echoes a body.

WHO WRITES (owner decision Q1): `workspace_secrets.require_manager`, the rule
the workspace's secrets follow -- in a group workspace a full platform admin,
in a personal workspace its person. Every member reads.

DARK UNTIL SWARM_MCP_SERVERS=on: a write is 503 `mcp_unavailable` before
anything is checked or stored. Reads answer what is stored, which while the
switch has always been off is nothing. The sweep runs regardless: destroying
an expired credential is always safe.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import Field

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, admin_auth, current_auth, get_context, tenant_scope
from ..errors import NotFound
from ..mcp_credentials import (
    McpCredentials,
    McpCredentialWriter,
    RowKeys,
    SecretManagerMcpCredentials,
)
from ..mcp_servers import (
    LIMITS,
    MAX_DESCRIPTION,
    MAX_TOOLS,
    MAX_URL,
    TRANSPORT,
    McpServers,
    require_enabled,
)
from ..schemas import StrictModel
from ..workspace_secrets import can_manage, require_manager

router = APIRouter(tags=["mcp-servers"])

BASE = "/v1/mcp-servers"
SWEEP_ROUTE = "/v1/admin/mcp-credentials/sweep"
NOTE = ("swarm-api never calls a registered URL; the first request a server sees is "
        "a worker's, through the relay")


class AuthBody(StrictModel):
    mode: Literal["none", "dispatch", "workspace_secret"]
    header: str | None = Field(default=None, max_length=64)
    scheme: Literal["Bearer", ""] | None = None


class LimitsBody(StrictModel):
    max_request_bytes: int | None = Field(default=None, ge=1, le=LIMITS["max_request_bytes"][1])
    max_response_bytes: int | None = Field(default=None, ge=1,
                                           le=LIMITS["max_response_bytes"][1])
    calls_per_minute: int | None = Field(default=None, ge=1, le=LIMITS["calls_per_minute"][1])
    call_timeout_seconds: int | None = Field(default=None, ge=1,
                                             le=LIMITS["call_timeout_seconds"][1])

    def given(self) -> dict[str, int]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class ServerCreate(StrictModel):
    name: str = Field(min_length=1, max_length=31)
    url: str = Field(min_length=1, max_length=MAX_URL)
    transport: Literal["streamable-http"] = TRANSPORT
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    auth: AuthBody = Field(default_factory=lambda: AuthBody(mode="none"))
    allowed_profiles: list[str] = Field(default_factory=list, max_length=16)
    allowed_tools: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)
    limits: LimitsBody | None = None
    enabled: bool = True


class ServerPatch(StrictModel):
    url: str | None = Field(default=None, min_length=1, max_length=MAX_URL)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    auth: AuthBody | None = None
    allowed_profiles: list[str] | None = Field(default=None, max_length=16)
    allowed_tools: list[str] | None = Field(default=None, max_length=MAX_TOOLS)
    limits: LimitsBody | None = None
    enabled: bool | None = None


def _servers(ctx: AppContext) -> McpServers:
    return McpServers(ctx.db, now=ctx.now)


def _tenant(ctx: AppContext, tenant_id: str) -> Tenant:
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} does not exist yet")
    return tenant


@router.get(BASE)
def list_servers(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = _tenant(ctx, tenant_id)
    return {"tenant_id": tenant_id, "can_manage": can_manage(auth, tenant),
            "servers": _servers(ctx).list(tenant_id), "note": NOTE}


@router.get(f"{BASE}/{{name}}")
def get_server(
    name: str,
    _auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"server": _servers(ctx).get(tenant_id, name)}


@router.post(BASE, status_code=status.HTTP_201_CREATED)
def register_server(
    body: ServerCreate,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    require_enabled()
    require_manager(auth, _tenant(ctx, tenant_id))
    server = _servers(ctx).register(
        tenant_id=tenant_id, name=body.name, url=body.url, description=body.description,
        auth=body.auth.model_dump(), allowed_profiles=body.allowed_profiles,
        allowed_tools=body.allowed_tools,
        limits=body.limits.given() if body.limits is not None else None,
        enabled=body.enabled, by=auth.email)
    return {"server": server, "note": NOTE}


@router.patch(f"{BASE}/{{name}}")
def update_server(
    name: str,
    body: ServerPatch,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Takes effect at the next attempt: a running attempt keeps what it was
    attached (§4.6 step 5)."""
    require_enabled()
    require_manager(auth, _tenant(ctx, tenant_id))
    changes: dict[str, Any] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None:
            continue
        if field == "auth":
            value = body.auth.model_dump() if body.auth is not None else None
        elif field == "limits":
            value = body.limits.given() if body.limits is not None else None
        changes[field] = value
    server = _servers(ctx).update(tenant_id=tenant_id, name=name, changes=changes,
                                  by=auth.email)
    return {"server": server, "takes_effect": "at the next attempt"}


def credential_writer(request: Request,
                      ctx: AppContext = Depends(get_context)) -> McpCredentialWriter:
    """The per-run credential writer, built once per process on `app.state`
    so a test substitutes one. Constructing it opens no connection."""
    writer = getattr(request.app.state, "mcp_credential_writer", None)
    if writer is None:
        writer = SecretManagerMcpCredentials(ctx.settings.project_id)
        request.app.state.mcp_credential_writer = writer
    return writer


@router.post(SWEEP_ROUTE)
def sweep_credentials(
    _auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    writer: McpCredentialWriter = Depends(credential_writer),
) -> dict:
    """§4.4: every 10 minutes, as the rollup sweeper (jobs.tf, lane MCP-IAM).
    Takes no argument and reads no value: what it destroys is fixed by each
    row's own owner and `expires_at`."""
    return McpCredentials(ctx.db, writer, now=ctx.now,
                          keys=RowKeys.from_settings(ctx.settings)).sweep()
