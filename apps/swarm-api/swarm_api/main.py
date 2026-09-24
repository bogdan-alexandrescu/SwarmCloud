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

from .deps import AppContext, build_context
from .errors import ApiError, Conflict, RateLimited, Unauthenticated
from .routes import (
    accounts,
    admin,
    attempts,
    checkpoints,
    health,
    platform,
    tasks,
    tenants,
    workflows,
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


def create_app(ctx: AppContext | None = None) -> FastAPI:
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

    app.include_router(health.router)
    app.include_router(tasks.router)
    # What is INSIDE a checkpoint: listing, one file, the whole archive.
    app.include_router(checkpoints.router)
    # Attempts across every task of the caller's tenant. Tenant-scoped like
    # the per-task attempts route, not admin-gated: they are the caller's own.
    app.include_router(attempts.router)
    app.include_router(workflows.router)
    app.include_router(tenants.router)
    # The account pool. Every route on it PROXIES to the quota broker, which is
    # the platform's single writer of subscription credentials; this service
    # supplies the caller's tenant and nothing else.
    app.include_router(accounts.router)
    app.include_router(platform.router)
    app.include_router(admin.router)

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
