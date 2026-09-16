"""Liveness, readiness and metrics.

These three are deliberately UNAUTHENTICATED. Cloud Run's health checks and the
Prometheus scraper are not Google-ID-token callers, and putting the token
requirement on /healthz is how a revision ends up looking unhealthy for reasons
that have nothing to do with its health. Nothing here exposes tenant data:
/metrics carries aggregate counters and the tenant labels of work already
submitted, which the same operator can already see in Cloud Logging.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from swarm_common.profiles import RUNNER_PROFILES

from ..deps import AppContext, get_context

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
def metrics(ctx: AppContext = Depends(get_context)) -> Response:
    body, content_type = ctx.metrics.render()
    return Response(content=body, media_type=content_type)
