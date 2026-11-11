"""Bundle builds' routes (docs/design/managed-plugins.md sections 1.4.3, 1.4.4 and 1.4.7; lane 16).

    GET  {base}                                   every bundle, and what retention keeps
    GET  {base}/{bundle_id}                       one bundle's build status (finishes it when it ended)
    POST {base}/installs/{install_id}/apply       Apply on a bundled install: starts one build
    POST {base}/installs/{install_id}/roll-back   the previous version, through its retained bundle
    POST {base}/rebuild                           `swarm upgrade`: every retained key on the new base
    POST /v1/internal/plugin-bundles/finish       Cloud Build's notification (Pub/Sub push, OIDC)

Every route but the notification is a platform admin's (`admin_auth`): a
bundle is platform scope, built once per deployment for every workspace. The
rebuild route is the one `swarm upgrade` calls between its image copy and
infra (the owner, 10 November 2026); it names the new release's base images,
each checked to be this deployment's own copy by digest.

The catalogue's own Apply route delegates a bundled install's Apply here
(`start_bundled_apply`), so either route starts the same one build.

Apply and Roll back read the diff as the catalogue's Apply does (section 9.1):
the admin sends the `diff_sha256` they read, and the plugin's name typed when
the diff is marked.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from .. import pluginbundle as pb
from .. import pluginstore as ps
from ..auth import AuthContext, GoogleTokenVerifier
from ..deps import AppContext, admin_auth, get_context
from ..errors import Forbidden, NotFound, Unauthenticated, UpstreamUnavailable
from ..judgment import settings_mac
from ..schemas import StrictModel

log = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])

BASE = "/v1/admin/plugin-bundles"
FINISH_ROUTE = "/v1/internal/plugin-bundles/finish"

AUDIENCE_ENV = "SWARM_PLUGIN_BUNDLE_AUDIENCE"
CALLERS_ENV = "SWARM_PLUGIN_BUNDLE_CALLERS"


class ApplyBody(StrictModel):
    version_id: str = Field(max_length=16)
    #: The `diff_sha256` the catalogue's diff answered: what the admin read.
    diff_sha256: str = Field(max_length=64)
    #: The plugin's name, typed, when the diff is marked.
    confirm_name: str | None = Field(default=None, max_length=64)


class RebuildBody(StrictModel):
    #: The new release's runner images, this deployment's copies, by digest.
    base: dict[str, str] = Field(max_length=16)


def _store(ctx: AppContext) -> ps.PluginStore:
    return ps.PluginStore(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _bundles(ctx: AppContext, store: ps.PluginStore) -> pb.BundleStore:
    return pb.BundleStore(store, ctx.db, now=ctx.now)


def _config(ctx: AppContext) -> pb.BundleSettings:
    return pb.bundle_settings(ctx.settings.project_id, ctx.settings.core.region)


def _builds(request: Request, ctx: AppContext) -> pb.BundleBuilds:
    builds = getattr(request.app.state, "plugin_bundle_builds", None)
    if builds is None:
        builds = pb.CloudBuildBundleBuilds(ctx.settings.project_id, ctx.settings.core.region)
        request.app.state.plugin_bundle_builds = builds
    return builds


def _by(auth: AuthContext) -> str:
    return (auth.email or "").strip().lower()


def _checked_diff(store: ps.PluginStore, install_id: str, body: ApplyBody) -> dict[str, Any]:
    """The bundled install, after the diff the admin read and their typed name."""
    install = store.install(install_id)
    if install is None or install.get("scope") != ps.PLATFORM:
        raise NotFound("no such plugin in this catalogue")
    if install.get("state") != ps.STATE_ACTIVE:
        raise ps.PluginRefused("install_not_active", status=409, install_id=install_id)
    if not ps.is_bundled(install):
        raise ps.PluginRefused("install_not_bundled", status=409, install_id=install_id)
    target = store.version(install_id, body.version_id)
    if target is None:
        raise ps.PluginRefused("version_not_found", status=404, version_id=body.version_id)
    diff = ps.version_diff(store.current(install) or {}, target)
    if ps.diff_digest(diff) != body.diff_sha256.strip().lower():
        raise ps.PluginRefused("diff_changed", status=409, install_id=install_id)
    if diff["needs_confirmation"]:
        if not body.confirm_name:
            raise ps.PluginRefused("confirmation_required", install_id=install_id)
        if body.confirm_name != install.get("name"):
            raise ps.PluginRefused("confirmation_mismatch", install_id=install_id)
    return install


def _status(request: Request, ctx: AppContext, bundle_id: str) -> dict[str, Any]:
    store = _store(ctx)
    bundles = _bundles(ctx, store)
    bundle = bundles.bundle(bundle_id)
    if bundle is None:
        raise ps.PluginRefused("bundle_not_found", status=404, bundle_id=bundle_id)
    if bundle.get("state") == pb.STATE_BUILDING:
        try:
            bundle = pb.finish(store, bundles, _builds(request, ctx), bundle, _config(ctx).repository)
        except pb.GrantMissing:
            raise ps.PluginRefused("bundle_grant_missing", status=503, bundle_id=bundle_id) from None
        except (pb.BuildNotStarted, OSError) as exc:
            log.warning("plugin bundle status not read bundle_id=%s (%s)", bundle_id, type(exc).__name__)
            raise UpstreamUnavailable("the bundle build's state could not be read; try again") from None
    return pb.bundle_view(bundle)


@router.get(BASE)
def list_bundles(auth: AuthContext = Depends(admin_auth), ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    bundles = _bundles(ctx, store)
    kept = pb.retention(bundles)
    return {"bundles": [pb.bundle_view(b) for b in bundles.bundles()],
            "retained": [b["bundle_id"] for b in kept["keep"]],
            "retirable": [b["bundle_id"] for b in kept["retire"]]}


@router.get(BASE + "/{bundle_id}")
def bundle_status(bundle_id: str, request: Request, auth: AuthContext = Depends(admin_auth),
                  ctx: AppContext = Depends(get_context)) -> dict:
    return _status(request, ctx, bundle_id)


def start_bundled_apply(request: Request, ctx: AppContext, install_id: str, body: ApplyBody, by: str) -> dict:
    """Apply on a bundled install: the diff checked, one build started.

    The one path for both Apply routes: this module's, and the catalogue's
    own Apply, which delegates here for a bundled install (the owner's pick
    on Q21) so the console's one Apply button starts the build."""
    store = _store(ctx)
    store.require_key()
    _checked_diff(store, install_id, body)
    config = _config(ctx)
    bundles = _bundles(ctx, store)
    started = pb.start_apply(store, bundles, _builds(request, ctx), config, install_id, body.version_id, by=by)
    return pb.bundle_view(started)


@router.post(BASE + "/installs/{install_id}/apply", status_code=202)
def apply_bundled(install_id: str, body: ApplyBody, request: Request, auth: AuthContext = Depends(admin_auth),
                  ctx: AppContext = Depends(get_context)) -> dict:
    return start_bundled_apply(request, ctx, install_id, body, _by(auth))


@router.post(BASE + "/installs/{install_id}/roll-back")
def roll_back(install_id: str, body: ApplyBody, auth: AuthContext = Depends(admin_auth),
              ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    store.require_key()
    _checked_diff(store, install_id, body)
    moved = pb.roll_back(store, _bundles(ctx, store), _config(ctx), install_id, body.version_id, by=_by(auth))
    install = store.install(install_id) or {}
    return {"bundle": pb.bundle_view(moved), "install": ps.install_view(install, store.current(install))}


@router.post(BASE + "/rebuild", status_code=202)
def rebuild(body: RebuildBody, request: Request, auth: AuthContext = Depends(admin_auth),
            ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    store.require_key()
    config = _config(ctx)
    bundles = _bundles(ctx, store)
    started = pb.rebuild(store, bundles, _builds(request, ctx), config, body.base, by=_by(auth))
    building = [pb.bundle_view(b) for b in bundles.bundles(pb.STATE_BUILDING)]
    return {"started": [b["bundle_id"] for b in started], "building": building}


# --- Cloud Build's notification ------------------------------------------------------


def _verifier(request: Request, audience: str) -> Any:
    verifier = getattr(request.app.state, "plugin_bundle_verifier", None)
    if verifier is None:
        verifier = GoogleTokenVerifier(audience, require_audience=True)
        request.app.state.plugin_bundle_verifier = verifier
    return verifier


def _bundle_of(envelope: Any) -> str | None:
    """The bundle id among a Cloud Build notification's build tags. Only
    the id is read; the build itself is read again from Cloud Build."""
    message = envelope.get("message") if isinstance(envelope, Mapping) else None
    data = message.get("data") if isinstance(message, Mapping) else None
    try:
        build = json.loads(base64.b64decode(data, validate=True)) if isinstance(data, str) else None
    except (ValueError, TypeError):
        return None
    tags = build.get("tags") if isinstance(build, Mapping) else None
    found = [t for t in tags if isinstance(t, str) and pb.BUNDLE_ID.match(t)] if isinstance(tags, list) else []
    return found[0] if len(found) == 1 and "swarm-plugin-bundle" in tags else None


@router.post(FINISH_ROUTE)
async def bundle_finished(request: Request) -> dict[str, Any]:
    ctx: AppContext = request.app.state.ctx
    audience = os.environ.get(AUDIENCE_ENV, "").strip()
    callers = {c.strip().lower() for c in os.environ.get(CALLERS_ENV, "").split(",") if c.strip()}
    if not audience or not callers:
        raise UpstreamUnavailable("the plugin bundle notification has no caller configured")
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("the plugin bundle notification needs an ID token")
    try:
        claims = await run_in_threadpool(_verifier(request, audience).verify, token.strip())
    except Exception:  # noqa: BLE001 -- the message may echo the token; never passed on
        raise Unauthenticated("the plugin bundle notification's ID token was refused") from None
    email = str(claims.get("email") or "").strip().lower()
    if email not in callers or claims.get("email_verified") is False:
        raise Forbidden("the plugin bundle notification is Cloud Build's subscription's alone")
    try:
        envelope = json.loads(await request.body())
    except ValueError:
        return {"ignored": True, "reason": "not_a_notification"}
    bundle_id = _bundle_of(envelope)
    if bundle_id is None:
        # 200, so Pub/Sub does not redeliver a build that is not a bundle's.
        return {"ignored": True, "reason": "not_a_bundle_build"}
    try:
        return await run_in_threadpool(_status, request, ctx, bundle_id)
    except ps.PluginRefused as exc:
        if exc.code == "bundle_not_found":
            return {"ignored": True, "reason": "unknown_bundle"}
        raise
