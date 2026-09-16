"""Liveness, readiness and metrics.

/healthz and /readyz are deliberately UNAUTHENTICATED. Cloud Run's health checks
are not Google-ID-token callers, and putting the token requirement on /healthz is
how a revision ends up looking unhealthy for reasons that have nothing to do with
its health. Neither route exposes tenant data.

/metrics is NOT in that set, and used not to be. The registry it renders carries
per-tenant labels -- `swarm_api_tasks_submitted_total{tenant,runner_profile}`,
`swarm_api_tenant_credentials_written_total{tenant,provider}`,
`swarm_api_rate_limited_total{tenant}`, `swarm_api_workflows_submitted_total
{tenant}` -- and every tenant group is a Cloud Run invoker on this service, so an
unauthenticated /metrics let any member of one tenant read another tenant's
submission volume, workflow counts, which providers it holds keys for, and the
full list of tenant ids. That is cross-tenant business intelligence and a tenant
enumeration oracle, so the route requires admin group membership.

A Prometheus scrape therefore authenticates as an identity in one of
ADMIN_GROUPS. A Google group can contain a service account, which is how a
collector gets in without a shared bearer token -- and a shared bearer token is
exactly what the contract forbids.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from swarm_common.profiles import RUNNER_PROFILES

from ..auth import AuthContext
from ..deps import AppContext, admin_auth, get_context

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness: the process is up and can serve. No dependency is touched."""
    return {"status": "ok", "runner_profiles": sorted(RUNNER_PROFILES)}


@router.get("/readyz")
def readyz(response: Response, ctx: AppContext = Depends(get_context)) -> dict[str, object]:
    ok, detail = ctx.ready()
    if not ok:
        response.status_code = 503
    return {
        "status": "ready" if ok else "not-ready",
        "detail": detail,
        "database": ctx.settings.core.firestore_database,
        "project": ctx.settings.project_id,
    }


@router.get("/metrics")
def metrics(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> Response:
    body, content_type = ctx.metrics.render()
    return Response(content=body, media_type=content_type)
