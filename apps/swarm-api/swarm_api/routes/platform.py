"""Platform read models.

/v1/stats, /v1/capacity, /v1/providers, /v1/resource-classes, /v1/runtimes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context

router = APIRouter(prefix="/v1", tags=["platform"])


@router.get("/stats")
def stats(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.stats(auth)


@router.get("/capacity")
def capacity(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.capacity(auth)


@router.get("/providers")
def providers(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.submissions.providers(auth)


@router.get("/resource-classes")
def resource_classes(
    # Declared, not used: it gates the route so /v1 has no unauthenticated
    # hole. There is nothing tenant-specific to compute -- this is the same
    # published catalogue every caller already picks from by name.
    auth: AuthContext = Depends(current_auth),  # noqa: ARG001
) -> dict:
    """The REQUESTED side of "requested vs utilised".

    Every attempt records what it USED (`peak_rss_bytes`, `peak_disk_bytes`)
    and nothing served what it was GIVEN, so a reader could see "peak RSS
    6.1 GiB" with no way to tell whether that is comfortable or one chatty
    prompt from an OOM kill. On this platform it is the latter more often than
    it looks: `requests == limits` everywhere, so there is no burst headroom to
    absorb an overshoot, and `oom_near_miss` exists precisely because the
    attempt that finished at 97% of its ceiling looks like a clean success.

    WHY A ROUTE RATHER THAN A TABLE IN THE CLIENT. `check-contract-parity.sh`
    asserts that shell and jq restatements of the frozen catalogue still match
    the Python; it does not cover TypeScript. A copy of these five numbers in a
    .ts file would therefore drift silently the first time a class is resized,
    and the screen most likely to be trusted -- "did this agent fit?" -- would
    be the one answering from stale figures.

    Static and non-sensitive: no store read, no tenant filter, nothing here
    that is not already implied by the `resource_class` a caller submits.
    """
    return {
        "resource_classes": {
            name: {
                "name": rc.name,
                # Both the request and the limit. Not a floor with burst above it.
                "cpu": rc.cpu,
                "memory_gib": rc.memory_gib,
                # A SLICE OF memory_gib, not capacity on top of it: the
                # workspace is a memory-backed tmpfs, because the Terraform
                # google provider cannot express Cloud Run's disk-backed
                # empty_dir (`medium` accepts only "MEMORY").
                "disk_gib": rc.disk_gib,
                # Weighted capacity units, which is what admission counts --
                # never a number of agents.
                "units": rc.units,
            }
            for name, rc in sorted(RESOURCE_CLASSES.items())
        }
    }


@router.get("/runtimes")
def runtimes(
    # Declared, not used -- the same reason as /v1/resource-classes above: it
    # gates the route so /v1 keeps no unauthenticated hole. Nothing here is
    # tenant-specific. Whether THIS tenant holds the credential a runtime needs
    # is /v1/providers, which reads the tenant document to answer it.
    auth: AuthContext = Depends(current_auth),  # noqa: ARG001
) -> dict:
    """What a `runner_profile` NAME actually means.

    Invariant 10: a caller picks a runner profile BY NAME and supplies nothing
    else -- no image, no command, no resource spec, no backend. That is what
    stops an authenticated caller turning the swarm into arbitrary compute, and
    it is not negotiable. Its cost is that the name is the caller's entire
    vocabulary: `claude-code` carries no hint that it runs on Cloud Run Jobs,
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

    `secrets` lists environment variable NAMES. This route reads no environment,
    no secret store and no Firestore document.
    """
    catalogue = {}
    for name, profile in sorted(RUNNER_PROFILES.items()):
        rc = RESOURCE_CLASSES[profile.resource_class]
        catalogue[name] = {
            # Exactly the string to send as `runner_profile` on a submission.
            "name": profile.name,
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
            "resolved_backend": resolve_backend(profile).value,
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
            # Resolved inline rather than left as a name to look up elsewhere.
            # "How big is this runtime" is the question the route exists to
            # answer, and an answer that needs a second request plus a
            # client-side join against /v1/resource-classes is the shape that
            # ends up hand-copied into a client instead.
            "resources": {
                "name": rc.name,
                # Both the request and the limit: `requests == limits`
                # platform-wide, so there is no burst headroom above this.
                "cpu": rc.cpu,
                "memory_gib": rc.memory_gib,
                # A SLICE OF memory_gib, not storage on top of it: the workspace
                # is a memory-backed tmpfs because the Terraform google provider
                # cannot express Cloud Run's disk-backed empty_dir
                # (`medium` accepts only "MEMORY").
                "disk_gib": rc.disk_gib,
                # Weighted capacity units, which is what admission counts --
                # never a number of agents. Same units /v1/capacity reports per
                # pool, so the two can be read together.
                "units": rc.units,
            },
        }
    return {"runtimes": catalogue}
