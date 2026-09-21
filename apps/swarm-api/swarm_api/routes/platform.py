"""Platform read models: /v1/stats, /v1/capacity, /v1/providers, /v1/resource-classes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.profiles import RESOURCE_CLASSES

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
