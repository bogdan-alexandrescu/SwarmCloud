"""Platform read models.

/v1/stats, /v1/capacity, /v1/providers, /v1/resource-classes, /v1/runtimes,
/v1/version, /v1/settings/model-prices.
"""

from __future__ import annotations

import os

from typing import Any, Callable

from fastapi import APIRouter, Depends, Request

from swarm_common.profiles import RUNNER_PROFILES, Backend, effective_size, resolve_backend

from .. import runnersizes
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..served_limits import configured_limits
from .accounts import account_pool

router = APIRouter(prefix="/v1", tags=["platform"])


@router.get("/stats")
def stats(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.stats(auth)


@router.get("/version")
def version(
    # Declared, not used: it gates the route so /v1 keeps no unauthenticated
    # hole, as on /v1/resource-classes. Nothing here is tenant-specific.
    auth: AuthContext = Depends(current_auth),  # noqa: ARG001
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Which build is serving, and the limits it was configured with (U25).

    `git_sha` and `build_time` are the `GIT_SHA` / `BUILD_TIME` build args
    `scripts/build-images.sh` passes, which images/swarm-api/Dockerfile sets as
    ENV. They name the commit whose build MADE this image, not the release
    that deployed it: main's incremental build re-tags an unchanged image's
    previous digest, so a reused swarm-api reports its manifest entry's
    `built_from`, possibly several merges back (docs/ci.md, "Main builds only
    the images a commit changed"). `revision` and `service` are Cloud Run's own `K_REVISION` and
    `K_SERVICE`. Each is NULL when its variable is unset or empty -- a laptop
    build, a test, a run outside Cloud Run -- never "unknown" and never a
    guess, because a placeholder string reads as an answer.

    `limits` is `served_limits.configured_limits`, the object `/v1/stats`
    serves, so the two cannot disagree. It is CONFIGURATION: no remaining or
    headroom figure is served here or anywhere, because the limiter is a token
    bucket per principal per instance and a remaining count from the instance
    that answered is wrong for the next request (see `served_limits`).
    """
    return {
        "git_sha": _env("GIT_SHA"),
        "build_time": _env("BUILD_TIME"),
        "revision": _env("K_REVISION"),
        "service": _env("K_SERVICE"),
        "limits": configured_limits(ctx.settings),
    }


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@router.get("/settings/model-prices")
def model_prices(
    # Declared, not used: every signed-in member may read the table, and the
    # dependency is what keeps /v1 free of an unauthenticated hole.
    auth: AuthContext = Depends(current_auth),  # noqa: ARG001
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The model list price table, read-only, for every signed-in member
    (PRICES-FOLLOWUP, part of #1481).

    The figures a stopped attempt is priced at are no secret of the admins':
    a member reading "cost not reported", or an estimate, on their own
    attempt needs the table to know why. So this serves exactly
    `model_prices` as `Store.get_platform_settings` reads it -- the seed
    while the setting was never written, every well-formed entry after, a
    tiered model with its `tiers` -- and nothing else of the settings
    document: its `updated_at` and `updated_by` name the last change to ANY
    setting, which is not when a price changed; each entry carries its own
    `as_of` and `updated_by`. Writing stays `PUT /v1/admin/settings`,
    admin-only.
    """
    return {"model_prices": ctx.store.get_platform_settings()["model_prices"]}


@router.get("/capacity")
def capacity(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.capacity(auth)


@router.get("/providers")
def providers(
    request: Request,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.providers(auth, _pool_listing(request, ctx))


def _pool_listing(
    request: Request, ctx: AppContext
) -> Callable[[str], dict[str, Any]] | None:
    """The broker's account listing, or None on a deployment with no broker.

    None is admission's "no broker configured" (`AccountPool.configured`,
    which reads the same URL): the pool serves nobody there. The client is
    built inside the call, not here, so a broker that is misconfigured or down
    degrades this read to `account_pool: unreadable` instead of failing it --
    the key half of /v1/providers needs no broker.
    """
    injected = getattr(request.app.state, "account_pool", None)
    if injected is None and not (ctx.settings.quota_broker_url or "").strip():
        return None

    def listing(tenant_id: str) -> dict[str, Any]:
        return account_pool(request, ctx).list_accounts(tenant_id)

    return listing


@router.get("/resource-classes")
def resource_classes(
    # Declared, not used: it gates the route so /v1 has no unauthenticated
    # hole. There is nothing tenant-specific to compute -- this is the same
    # published catalogue every caller already picks from by name.
    auth: AuthContext = Depends(current_auth),  # noqa: ARG001
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The REQUESTED side of "requested vs utilised", at the CURRENT size version.

    Every attempt records what it USED (`peak_rss_bytes`, `peak_disk_bytes`)
    and nothing served what it was GIVEN, so a reader could see "peak RSS
    6.1 GiB" with no way to tell whether that is comfortable or one chatty
    prompt from an OOM kill. Memory is request == limit everywhere (invariant
    7), so there is no burst headroom to absorb an overshoot, and
    `oom_near_miss` exists precisely because the attempt that finished at 97%
    of its ceiling looks like a clean success. CPU alone has two numbers:
    `cpu_request`, guaranteed and billed, and `cpu_limit`, what a GKE pod may
    burst to; `cpu` is the request under its old name.

    WHY A ROUTE RATHER THAN A TABLE IN THE CLIENT. A served value cannot drift
    at all, and since runner sizes became an admin setting (SIZES-API, design
    §4.5) a client table would be wrong after the first resize with no deploy
    to correct it. Each class is the current version's
    (`runnersizes.current`), validated on read, the code defaults when no
    version is written or the stored one is malformed; `size_version` says
    which (0: the defaults). Two document reads, no tenant filter, nothing
    here that is not already implied by the `resource_class` a caller submits.
    """
    setting = runnersizes.current(ctx.db)
    return {
        "size_version": setting.in_effect,
        "resource_classes": {
            name: runnersizes.class_entry(name, size, setting.in_effect)
            for name, size in sorted(setting.sizes.items())
        },
    }


@router.get("/runtimes")
def runtimes(
    # Declared, not used -- the same reason as /v1/resource-classes above: it
    # gates the route so /v1 keeps no unauthenticated hole. Nothing here is
    # tenant-specific. Whether THIS tenant holds the credential a runtime needs
    # is /v1/providers, which reads the tenant document to answer it.
    auth: AuthContext = Depends(current_auth),  # noqa: ARG001
    ctx: AppContext = Depends(get_context),
) -> dict:
    """What a `runner_profile` NAME actually means.

    Invariant 10: a caller picks a runner profile BY NAME and supplies nothing
    else -- no image, no command, no resource spec, no backend. That is what
    stops an authenticated caller turning the swarm into arbitrary compute, and
    it is not negotiable. Its cost is that the name is the caller's entire
    vocabulary: `claude-code` carries no hint that it runs on GKE Autopilot,
    sizes to `standard`, times out at two hours, or accepts a subscription token
    INSTEAD of an API key. Before this route the only way to learn any of that
    was to read the frozen catalogue in the repository, which someone calling
    the API over HTTP does not have.

    PUBLISHING THE CATALOGUE DOES NOT WEAKEN THE INVARIANT. Reading which
    profiles an admin defined is not supplying one. The refusal path is
    untouched: `validation.validate_runner_profile` still rejects any name that
    is not in the frozen catalogue, and `FORBIDDEN_CALLER_FIELDS` still refuses
    `image`, `backend`, `secrets` and the rest if a caller sends back a field
    they read here.

    EVERY VALUE IS READ FROM `swarm_common.profiles`; none is copied. A
    hand-written table in this file is exactly the drift
    `check-contract-parity.sh` exists to catch, and that script does not cover
    this file -- so the rule is enforced here by there being no literal to
    drift. Resize a class or retime a profile in the frozen catalogue and this
    route moves with it in the same commit.

    `secrets` lists environment variable NAMES. This route reads no environment
    and no secret store. Two values are not the catalogue's. `models`, each
    class's model ID, is Terraform's (`runner_models`, owner's Q1 of #1435),
    read from this service's own WORKER_CLASS_MODELS at start, the map the
    scheduler dispatches with, so the route reports what a step of each class
    will run. `resources` is the current runner size version's for a GKE
    profile and the code default for a Cloud Run one (`effective_size`, design
    §4.7): the two documents `/v1/resource-classes` reads.
    """
    class_models = ctx.settings.worker_class_models
    setting = runnersizes.current(ctx.db)
    # EVERY profile, including disabled ones, each carrying its own
    # `available` flag and reason. A caller reading a task that names `codex`
    # still needs its image, backend and resource class to render that task;
    # omitting the entry would make an existing run unreadable rather than
    # merely unrepeatable. The flag is what a submit form filters on.
    catalogue = {}
    for name, profile in sorted(RUNNER_PROFILES.items()):
        backend = resolve_backend(profile)
        size = effective_size(profile.resource_class, setting.sizes, backend)
        # A Cloud Run profile runs at the code defaults, version 0, whatever
        # the setting says: Cloud Run fixes resources on the Job.
        size_version = setting.in_effect if backend is Backend.GKE_AUTOPILOT else 0
        catalogue[name] = {
            # Exactly the string to send as `runner_profile` on a submission --
            # unless `available` is false, in which case submitting it is
            # refused with `disabled_reason`.
            "name": profile.name,
            "available": profile.available,
            "disabled_reason": profile.disabled_reason,
            "image": profile.image,
            # DECLARED and RESOLVED, both, because they are different questions.
            # `Backend.AUTO` is a value the frozen enum permits and
            # `resolve_backend` is the frozen function that turns it into the
            # backend the task really runs on. Serving only the declared value
            # would answer "AUTO", which tells a caller nothing; serving only
            # the resolved one would hide that the platform, not the profile,
            # made the choice. No profile in the catalogue is AUTO today, so
            # the two agree -- that is a fact about the catalogue, not a
            # guarantee of this route.
            "backend": profile.backend.value,
            "resolved_backend": backend.value,
            # None means the runner consumes no external provider's quota, so it
            # runs for a tenant who has registered no credential at all. That is
            # what keeps `mock` usable as a smoke path on a brand-new tenant.
            "provider": profile.provider,
            "secrets": list(profile.secrets),
            # True  -> the names above are INTERCHANGEABLE; supply exactly one.
            # False -> all of them are required.
            # The list without this flag is actively misleading: it would tell a
            # tenant who pays for a Claude subscription that they must also buy
            # metered API access, which is the refusal `secrets_any_of` exists
            # in the frozen catalogue to prevent.
            "secrets_any_of": profile.secrets_any_of,
            "timeout_seconds": profile.timeout_seconds,
            "resource_class": profile.resource_class,
            # The model classes a step on this profile may name, its default,
            # and each class's model ID (docs/design/model-per-step.md §4.4).
            # `[]`, null and `{}` on a profile that runs no Claude model; an
            # empty `models` on a deployment that pins none yet, where only
            # the default may be named.
            "model_classes": list(profile.model_classes),
            "default_model_class": profile.default_model_class,
            "models": {
                name: class_models[name] for name in profile.model_classes if name in class_models
            },
            # Resolved inline rather than left as a name to look up elsewhere.
            # "How big is this runtime" is the question the route exists to
            # answer, and an answer that needs a second request plus a
            # client-side join against /v1/resource-classes is the shape that
            # ends up hand-copied into a client instead.
            "resources": runnersizes.class_entry(profile.resource_class, size, size_version),
        }
    return {"runtimes": catalogue}
