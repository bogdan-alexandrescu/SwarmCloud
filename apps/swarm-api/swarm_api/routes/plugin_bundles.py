"""Bundle builds' routes (docs/design/managed-plugins.md sections 1.4.3, 1.4.4 and 1.4.7; lane 16).

    GET  {base}                                   every bundle, and what retention keeps
    GET  {base}/{bundle_id}                       one bundle's build status (finishes it when it ended)
    POST {base}/installs/{install_id}/apply       Apply on a bundled install: runs one job execution
    POST {base}/installs/{install_id}/roll-back   the previous version, through its retained bundle
    POST {base}/rebuild                           `swarm upgrade`: every retained key on the new base

Every route is a platform admin's (`admin_auth`): a bundle is platform
scope, built once per deployment for every workspace. A build is one
execution of the Cloud Run job `swarm-plugin-bundle` (the owner, 11 November
2026); there is no Cloud Build build and so no Cloud Build notification: the
status read is the one finish, its `executions.get` capped per bundle. The
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

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from .. import pluginbundle as pb
from .. import pluginstore as ps
from ..auth import AuthContext
from ..deps import AppContext, admin_auth, get_context
from ..errors import NotFound, UpstreamUnavailable
from ..judgment import settings_mac
from ..schemas import StrictModel

log = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])

BASE = "/v1/admin/plugin-bundles"


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


def _jobs(request: Request) -> pb.BundleJobs:
    jobs = getattr(request.app.state, "plugin_bundle_jobs", None)
    if jobs is None:
        jobs = pb.CloudRunBundleJobs()
        request.app.state.plugin_bundle_jobs = jobs
    return jobs


def _registry(request: Request) -> pb.BundleRegistry:
    registry = getattr(request.app.state, "plugin_bundle_registry", None)
    if registry is None:
        registry = pb.ArtifactRegistryReader()
        request.app.state.plugin_bundle_registry = registry
    return registry


def _sources(request: Request, ctx: AppContext, config: pb.BundleSettings) -> pb.BundleSources:
    sources = getattr(request.app.state, "plugin_bundle_sources", None)
    if sources is None:
        sources = pb.GcsBundleSources(config.plugins_bucket, ctx.settings.project_id)
        request.app.state.plugin_bundle_sources = sources
    return sources


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
        config = _config(ctx)
        try:
            bundle = pb.finish(store, bundles, _jobs(request), _registry(request), _sources(request, ctx, config),
                               config, bundle)
        except pb.GrantMissing:
            raise ps.PluginRefused("bundle_grant_missing", status=503, bundle_id=bundle_id) from None
        except (pb.BuildNotStarted, pb.RegistryUnavailable, OSError) as exc:
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
    """Apply on a bundled install: the diff checked, one job execution run.

    The one path for both Apply routes: this module's, and the catalogue's
    own Apply, which delegates here for a bundled install (the owner's pick
    on Q21) so the console's one Apply button starts the build."""
    store = _store(ctx)
    store.require_key()
    _checked_diff(store, install_id, body)
    config = _config(ctx)
    bundles = _bundles(ctx, store)
    started = pb.start_apply(store, bundles, _jobs(request), _registry(request), config, install_id,
                             body.version_id, by=by)
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
    started = pb.rebuild(store, bundles, _jobs(request), _registry(request), config, body.base, by=_by(auth))
    building = [pb.bundle_view(b) for b in bundles.bundles(pb.STATE_BUILDING)]
    return {"started": [b["bundle_id"] for b in started], "building": building}
