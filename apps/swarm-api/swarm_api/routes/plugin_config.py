"""A managed plugin's settings, secrets and the workspace's defaults (docs/design/managed-plugins.md, lane 6).

    GET    {base}/{install_id}                                the form and the workspace's values
    PUT    {base}/{install_id}                                save non-secret values (names the revision read)
    POST   {base}/{install_id}/secrets/{field}/links          a one-time link to a secret's form
    PUT    {base}/{install_id}/secrets/{field}                store a secret or a file's content (with the link)
    DELETE {base}/{install_id}/secrets/{field}                destroy it
    PUT    {base}/{install_id}/secrets/{field}/binding        use a workspace key instead (TypeSafe)
    GET    /v1/workspaces/{tenant_id}/plugin-defaults         the plugins on for every step
    PUT    /v1/workspaces/{tenant_id}/plugin-defaults         set them

`{base}` is `/v1/workspaces/{tenant_id}/plugin-config`. Every route is a
workspace's: plugin settings and secrets are per workspace, also for a
platform plugin (the owner's answer to Q2), so there is no platform route.

WHO (the owner's answer to Q3, as the catalogue's routes read it). A member
of the workspace reads the form, the values (never a secret's) and the
defaults. Its owner or admin, the role read fresh, saves values, mints a
link, stores, removes or binds a secret and sets the defaults. A platform
admin who is neither may read and nothing else.

A SECRET NEVER COMES BACK (section 6.2). The value is accepted on one route,
`PUT .../secrets/{field}`, only with a one-time link this caller minted for
this workspace, plugin and field (ten minutes, one use); it goes to Secret
Manager under the frozen contract's name and nowhere else: not Firestore,
not a log, not the audit row, not a response. The route is in
`main.UNCAPTURED_ROUTES`. Every refusal is a fixed code naming ids and the
field, never a value.

Nothing here takes a command, an image, a resource spec or a backend
parameter from a caller (invariant 10): a value is checked against a field
the pinned version declares.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StrictInt

from .. import pluginconfig as pcfg
from .. import pluginstore as ps
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..errors import NotFound, RoleAdminRequired
from ..judgment import settings_mac
from ..plugincatalogue import BUILT_IN_PREFIX
from ..roles import ADMIN
from ..schemas import StrictModel
from .plugin_catalogue import _allowed_ids, _by, _is_workspace_admin, _store, _workspace_read

router = APIRouter(tags=["plugin-config"])

BASE = "/v1/workspaces/{tenant_id}/plugin-config"
DEFAULTS_ROUTE = "/v1/workspaces/{tenant_id}/plugin-defaults"
#: The one route that takes a secret's value; `main.UNCAPTURED_ROUTES` names it.
SECRET_ROUTE = BASE + "/{install_id}/secrets/{field}"

NOTE = "stored write-only; no route in this API can return a plugin secret"


class ValuesBody(StrictModel):
    #: The version whose form the values were read against, and the settings' revision.
    version_id: str = Field(max_length=32)
    revision: StrictInt = Field(ge=0)
    #: Field key -> value (None clears it). Non-secret fields only.
    values: dict[str, Any] = Field(max_length=256)


class SecretWrite(StrictModel):
    #: Write-only. No response carries it, its length or any part of it.
    value: str = Field(min_length=1, max_length=pcfg.MAX_FILE_BYTES)
    #: The `t` of the one-time link (`POST .../links`): required.
    link_token: str = Field(min_length=1, max_length=256)


class BindingBody(StrictModel):
    provider: str = Field(max_length=32)


class DefaultsBody(StrictModel):
    defaults: list[str] = Field(max_length=64)


def secret_writer(request: Request, ctx: AppContext = Depends(get_context)) -> pcfg.PluginSecretWriter:
    """The writer, built once per process on `app.state` so a test substitutes one."""
    writer = getattr(request.app.state, "plugin_secret_writer", None)
    if writer is None:
        writer = pcfg.SecretManagerPluginSecrets(ctx.settings.project_id)
        request.app.state.plugin_secret_writer = writer
    return writer


def _configs(ctx: AppContext) -> pcfg.PluginConfigs:
    return pcfg.PluginConfigs(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _admin(auth: AuthContext, ctx: AppContext, tenant_id: str) -> None:
    _, tenant = _workspace_read(auth, ctx, tenant_id)
    if not _is_workspace_admin(auth, ctx, tenant, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin may change its plugin settings, secrets or defaults. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})


def _plugin(ctx: AppContext, tenant_id: str, install_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The active install this workspace sees, and its current version."""
    if install_id.startswith(BUILT_IN_PREFIX):
        raise pcfg.ConfigRefused("plugin_has_no_settings", install_id=install_id)
    store = _store(ctx)
    install = store.install(install_id)
    if install is None or not ps.visible_to(install, tenant_id) or install.get("state") != ps.STATE_ACTIVE:
        raise NotFound("no such plugin in this workspace's catalogue", detail={"install_id": install_id})
    version = store.current(install)
    if version is None:
        raise NotFound("no such plugin in this workspace's catalogue", detail={"install_id": install_id})
    return install, version


def _field(version: dict[str, Any], field: str, *kinds: str) -> dict[str, Any]:
    for spec in pcfg.form_fields(version):
        if spec["key"] == field:
            if spec["kind"] not in kinds:
                raise pcfg.ConfigRefused("field_not_write_only", field=field)
            return spec
    raise pcfg.ConfigRefused("field_unknown", field=field if pcfg._KEY.match(field) else None)


def _answer(ctx: AppContext, auth: AuthContext, tenant_id: str, install: dict[str, Any],
            version: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    _, tenant = _workspace_read(auth, ctx, tenant_id)
    config = config if config is not None else _configs(ctx).config(tenant_id, install["install_id"])
    return {"tenant_id": tenant_id, "form": pcfg.form(install, version), **pcfg.values_view(version, config),
            "can_edit": _is_workspace_admin(auth, ctx, tenant, fresh=False)}


@router.get(BASE + "/{install_id}")
def read_config(tenant_id: str, install_id: str, auth: AuthContext = Depends(current_auth),
                ctx: AppContext = Depends(get_context)) -> dict:
    _workspace_read(auth, ctx, tenant_id)
    install, version = _plugin(ctx, tenant_id, install_id)
    return _answer(ctx, auth, tenant_id, install, version)


@router.put(BASE + "/{install_id}")
def save_values(tenant_id: str, install_id: str, body: ValuesBody, auth: AuthContext = Depends(current_auth),
                ctx: AppContext = Depends(get_context)) -> dict:
    _admin(auth, ctx, tenant_id)
    install, version = _plugin(ctx, tenant_id, install_id)
    if body.version_id != version.get("version_id"):
        raise pcfg.ConfigRefused("form_changed", status=409, install_id=install_id,
                                 version_id=version.get("version_id"))
    saved = _configs(ctx).save_values(tenant_id, install_id, version, values=body.values,
                                      revision=body.revision, by=_by(auth))
    return _answer(ctx, auth, tenant_id, install, version, saved)


@router.post(BASE + "/{install_id}/secrets/{field}/links", status_code=201)
def mint_secret_link(tenant_id: str, install_id: str, field: str, auth: AuthContext = Depends(current_auth),
                     ctx: AppContext = Depends(get_context)) -> dict:
    """A one-time link to the console's form for one secret or file setting,
    bound to this caller, workspace, plugin and field."""
    _admin(auth, ctx, tenant_id)
    _, version = _plugin(ctx, tenant_id, install_id)
    _field(version, field, pcfg.KIND_SECRET, pcfg.KIND_FILE)
    configs = _configs(ctx)
    configs.require_key()
    return configs.mint_link(email=_by(auth), tenant_id=tenant_id, install_id=install_id, field=field,
                             console=ctx.settings.console_url)


@router.put(SECRET_ROUTE)
def store_secret(tenant_id: str, install_id: str, field: str, body: SecretWrite,
                 auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context),
                 writer: pcfg.PluginSecretWriter = Depends(secret_writer)) -> dict:
    _admin(auth, ctx, tenant_id)
    _, version = _plugin(ctx, tenant_id, install_id)
    spec = _field(version, field, pcfg.KIND_SECRET, pcfg.KIND_FILE)
    payload = body.value.encode("utf-8")
    limit = pcfg.MAX_FILE_BYTES if spec["kind"] == pcfg.KIND_FILE else pcfg.MAX_SECRET_BYTES
    if not 0 < len(payload) <= limit:
        raise pcfg.ConfigRefused("secret_size", field=field)
    configs = _configs(ctx)
    configs.require_key()
    configs.consume_link(body.link_token, email=_by(auth), tenant_id=tenant_id, install_id=install_id,
                         field=field)
    name = pcfg.secret_name(tenant_id, install_id, field)
    writer.create_secret(tenant_id, install_id, name)
    writer.add_version(name, payload)
    is_file = spec["kind"] == pcfg.KIND_FILE
    saved = configs.record_secret(tenant_id, install_id, str(version.get("version_id")), spec, by=_by(auth),
                                  sha256=hashlib.sha256(payload).hexdigest() if is_file else None,
                                  size=len(payload) if is_file else None)
    return {"install_id": install_id, "field": field, **pcfg.values_view(version, saved)["values"][field],
            "note": NOTE}


@router.delete(SECRET_ROUTE)
def remove_secret(tenant_id: str, install_id: str, field: str, auth: AuthContext = Depends(current_auth),
                  ctx: AppContext = Depends(get_context),
                  writer: pcfg.PluginSecretWriter = Depends(secret_writer)) -> dict:
    _admin(auth, ctx, tenant_id)
    _, version = _plugin(ctx, tenant_id, install_id)
    _field(version, field, pcfg.KIND_SECRET, pcfg.KIND_FILE)
    configs = _configs(ctx)
    configs.require_key()
    entry = (configs.config(tenant_id, install_id).get("fields") or {}).get(field) or {}
    destroyed = writer.destroy_versions(pcfg.secret_name(tenant_id, install_id, field))
    if not entry and not destroyed:
        raise pcfg.ConfigRefused("secret_not_set", status=404, field=field)
    configs.record_removed(tenant_id, install_id, str(version.get("version_id")), field, by=_by(auth))
    return {"install_id": install_id, "field": field, "set": False, "versions_destroyed": destroyed}


@router.put(SECRET_ROUTE + "/binding")
def bind_secret(tenant_id: str, install_id: str, field: str, body: BindingBody,
                auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context),
                writer: pcfg.PluginSecretWriter = Depends(secret_writer)) -> dict:
    """Use the workspace's key for a secret field instead of a value (section
    6.2): a closed list of keys, and only for a plugin whose hooks and servers
    all run on node, which the Jev adapter wraps."""
    _admin(auth, ctx, tenant_id)
    _, version = _plugin(ctx, tenant_id, install_id)
    try:
        _field(version, field, pcfg.KIND_SECRET)
    except pcfg.ConfigRefused as refused:
        if refused.code == "field_not_write_only":
            raise pcfg.ConfigRefused("field_not_bindable", field=field) from None
        raise
    if body.provider not in pcfg.BINDABLE:
        raise pcfg.ConfigRefused("binding_provider_refused", field=field)
    if not pcfg.node_only(version):
        raise pcfg.ConfigRefused("binding_needs_node", field=field, install_id=install_id)
    configs = _configs(ctx)
    configs.require_key()
    # A value stored before stays nowhere: the binding replaces it.
    writer.destroy_versions(pcfg.secret_name(tenant_id, install_id, field))
    saved = configs.record_binding(tenant_id, install_id, str(version.get("version_id")), field, body.provider,
                                   by=_by(auth))
    return {"install_id": install_id, "field": field, **pcfg.values_view(version, saved)["values"][field]}


def _defaults_answer(ctx: AppContext, auth: AuthContext, tenant_id: str, defaults: list[str]) -> dict[str, Any]:
    _, tenant = _workspace_read(auth, ctx, tenant_id)
    return {"tenant_id": tenant_id, "defaults": defaults,
            "can_edit": _is_workspace_admin(auth, ctx, tenant, fresh=False)}


@router.get(DEFAULTS_ROUTE)
def read_defaults(tenant_id: str, auth: AuthContext = Depends(current_auth),
                  ctx: AppContext = Depends(get_context)) -> dict:
    _workspace_read(auth, ctx, tenant_id)
    return _defaults_answer(ctx, auth, tenant_id, _configs(ctx).defaults(tenant_id))


@router.put(DEFAULTS_ROUTE)
def set_defaults(tenant_id: str, body: DefaultsBody, auth: AuthContext = Depends(current_auth),
                 ctx: AppContext = Depends(get_context)) -> dict:
    _admin(auth, ctx, tenant_id)
    wanted = pcfg.check_defaults(_store(ctx), tenant_id, body.defaults, _allowed_ids(ctx, tenant_id))
    stored = _configs(ctx).set_defaults(tenant_id, wanted, by=_by(auth))
    return _defaults_answer(ctx, auth, tenant_id, stored)
