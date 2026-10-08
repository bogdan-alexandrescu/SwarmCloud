"""FastAPI application assembly.

`create_app()` takes an optional AppContext so a test can build the entire real
application -- real routes, real validation, real tenant scoping -- around an
in-memory Firestore. Nothing in the request path reads a module-level global.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from swarm_common.identity import AuthError
from swarm_common.states import InvalidTransition

from .access import AccessService, build_access
from .deps import AppContext, build_context
from .errors import ApiError, Conflict, RateLimited, Unauthenticated
from .forgeapp import ForgeApp, build_forge_app
from .routes import (
    access,
    accounts,
    admin,
    attempts,
    checkpoints,
    children,
    forgeapp,
    gittokens,
    health,
    issues,
    leases,
    onboarding,
    outcomes,
    platform,
    repositories,
    runs,
    tasks,
    tenants,
    workflows,
    workspaces,
)
from .validation import FORBIDDEN_CALLER_FIELDS

from swarm_common.logging_setup import configure_logging

log = logging.getLogger(__name__)

TITLE = "swarm control-plane API"


def _forbidden_field_hint(errors: list[dict[str, Any]]) -> str | None:
    """Turn 'extra_forbidden' into the actual reason the field is refused."""
    offending = []
    for error in errors:
        if error.get("type") != "extra_forbidden":
            continue
        location = error.get("loc") or ()
        field = str(location[-1]) if location else ""
        if field in FORBIDDEN_CALLER_FIELDS:
            offending.append(field)
    if not offending:
        return None
    return (
        "the platform never accepts "
        + ", ".join(sorted(set(offending)))
        + " from a caller; choose a runner_profile by name and the frozen "
        "catalogue supplies the image, command, resource class and backend"
    )


def create_app(ctx: AppContext | None = None, *, forge_app: ForgeApp | None = None,
               access_service: AccessService | None = None) -> FastAPI:
    # FIRST, before anything else can log. Nothing configured the root logger
    # previously, so every log.info() in this package was discarded and an
    # operator debugging a refused request saw the request and not the reason.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    app = FastAPI(
        title=TITLE,
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.ctx = ctx if ctx is not None else build_context()
    # The GitHub App's user authorisation (docs/onboarding.md §3.2, lane OB3).
    # Builds no client: the App's settings come from the environment, and
    # every route answers "not configured" until they and its client secret
    # exist. A test injects one over forge fakes.
    app.state.forge_app = forge_app if forge_app is not None else build_forge_app(
        app.state.ctx.db, app.state.ctx.settings.project_id, now=app.state.ctx.now)
    # The access API (docs/onboarding.md §3.2, lane OB4), over the connection
    # above: it acts as the caller through their own refreshed token.
    app.state.access = access_service if access_service is not None else build_access(
        app.state.ctx.db, app.state.forge_app, now=app.state.ctx.now)

    app.include_router(health.router)
    app.include_router(tasks.router)
    # What is INSIDE a checkpoint: listing, one file, the whole archive.
    app.include_router(checkpoints.router)
    # Attempts across every task of the caller's tenant. Tenant-scoped like
    # the per-task attempts route, not admin-gated: they are the caller's own.
    app.include_router(attempts.router)
    # Each slot-holding task's lease heartbeat (#179). Tenant-scoped, so a
    # member's Agents list can say a worker has gone silent without the
    # admin-gated /v1/admin/leases.
    app.include_router(leases.router)
    # What the work in a span ended as, by when it ended (#185). Tenant-scoped;
    # platform scope only behind require_admin, which the route runs first.
    app.include_router(outcomes.router)
    app.include_router(workflows.router)
    # Issue runs (#454): plan an issue, approve the plan's digest, run it as a
    # new workflow. Tenant-scoped; the document is `issueruns`'s own.
    app.include_router(runs.router)
    # The issue preview, read with the caller's tenant's forge token (#454 1A).
    app.include_router(issues.router)
    # Registered repositories (docs/repo-index.md §1, lane RI1). Tenant-scoped;
    # the document is `repositories`'s own, and the registration reads the
    # forge once with the caller's tenant's token.
    app.include_router(repositories.router)
    # The git token registry (docs/git-tokens.md, lane GT1): slot records,
    # never values. Tenant-scoped; the document is `gittokens`'s own.
    app.include_router(gittokens.router)
    # The onboarding checklist (docs/onboarding.md §2, lane OB1): derived on
    # each read from the records above, read-only, the caller's own tenant.
    app.include_router(onboarding.router)
    app.include_router(workspaces.router)
    # Connect GitHub as yourself (docs/onboarding.md §3.2, lane OB3):
    # authorise, exchange, disconnect, and the refresh sweep the
    # swarm-forge-refresh job calls. No route returns a token.
    app.include_router(forgeapp.router)
    # The access API (lane OB4): owners, repositories, grants, verify. Acts
    # as the caller through their own connection; no route returns a token.
    app.include_router(access.router)
    app.include_router(tenants.router)
    # The account pool. Every route on it PROXIES to the quota broker, which is
    # the platform's single writer of subscription credentials; this service
    # supplies the caller's tenant and nothing else.
    app.include_router(accounts.router)
    app.include_router(platform.router)
    app.include_router(admin.router)
    # Child tasks (docs/design/child-tasks.md): worker-only routes, which
    # authenticate the tenant's worker service account and an attempt proof
    # rather than a person.
    app.include_router(children.router)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        route = request.scope.get("path", "unknown")
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed = time.perf_counter() - started
            try:
                app.state.ctx.metrics.request_latency.labels(route=_template(request)).observe(
                    elapsed
                )
            except Exception:  # pragma: no cover - metrics must never 500 a request
                log.debug("latency metric failed for %s", route)
        try:
            app.state.ctx.metrics.requests.labels(
                route=_template(request),
                method=request.method,
                status=f"{response.status_code // 100}xx",
            ).inc()
        except Exception:  # pragma: no cover
            pass
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(max(1, int(exc.retry_after_seconds) + 1))
        if isinstance(exc, Unauthenticated):
            headers["WWW-Authenticate"] = 'Bearer realm="swarm", error="invalid_token"'
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_payload(), headers=headers
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        # Messages from the frozen identity module are token-free by contract.
        return JSONResponse(
            status_code=401,
            content={"code": "unauthenticated", "message": str(exc)},
            headers={"WWW-Authenticate": 'Bearer realm="swarm", error="invalid_token"'},
        )

    @app.exception_handler(InvalidTransition)
    async def transition_handler(request: Request, exc: InvalidTransition) -> JSONResponse:
        payload = Conflict(str(exc), detail={"from": exc.frm.value, "to": exc.to.value})
        return JSONResponse(status_code=payload.status_code, content=payload.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "loc": [str(part) for part in (error.get("loc") or ())],
                "msg": error.get("msg"),
                "type": error.get("type"),
            }
            for error in exc.errors()
        ]
        body: dict[str, Any] = {
            "code": "validation_failed",
            "message": "request body failed validation",
            "detail": {"errors": errors},
        }
        hint = _forbidden_field_hint(exc.errors())
        if hint:
            body["message"] = hint
            body["detail"]["invariant"] = (
                "API callers pick a runner_profile BY NAME (CONTRACT.md invariant 10)"
            )
        return JSONResponse(status_code=422, content=body)

    return app


def _template(request: Request) -> str:
    """The route template, so metric cardinality does not explode on task ids."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or request.scope.get("path", "unknown")


# Entrypoint: `uvicorn swarm_api.main:create_app --factory --host 0.0.0.0 --port $PORT`.
# A module-level `app = create_app()` would build a Firestore client at import
# time, which would make every unit test that imports this module need
# credentials and a PROJECT_ID.


def main() -> None:  # pragma: no cover - process entrypoint
    import os

    import uvicorn

    uvicorn.run(
        "swarm_api.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
