"""FastAPI application assembly.

`create_app()` takes an optional AppContext so a test can build the entire real
application -- real routes, real validation, real tenant scoping -- around an
in-memory Firestore. Nothing in the request path reads a module-level global.
"""

from __future__ import annotations

import logging
import os
import threading
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
from .forgeperms import build_forge_permissions
from .awaitinput import MAX_CONCURRENT_MULTIPART
from .inputwriter import InputWriter, build_input_writer
from .routes import (
    access,
    accounts,
    admin,
    approvals,
    attemptcontext,
    attempts,
    attemptstate,
    attempts_mcp,
    attempts_secrets,
    await_input,
    browser_logins,
    browser_slots,
    checkpoints,
    children,
    codeintel,
    forgeapp,
    forgeperms,
    gittokens,
    gittools,
    graph_ticket,
    health,
    issues,
    judgment,
    leases,
    live,
    mcp_servers,
    merge_settings,
    notifications,
    onboarding,
    outcomes,
    profile,
    provider_keys,
    people,
    platform,
    repositories,
    runs,
    schedule_tick,
    search,
    strandedprs,
    schedules,
    tasks,
    tenants,
    typesafe_ticket,
    workflows,
    workspace_secrets,
    workspaces,
)
from .validation import FORBIDDEN_CALLER_FIELDS

from swarm_common.logging_setup import configure_logging

log = logging.getLogger(__name__)

TITLE = "swarm control-plane API"

#: Routes whose request body and response are NEVER captured by anything this
#: app logs (design workspace-secrets.md §4.5). `observe` below captures no body
#: or response for any route; a capture added to it later must skip these.
#: The release is the one route that serves credential material (owner
#: decision Q2), so its body and its answer stay out of every log by
#: construction, not by a reviewer noticing.
#:
#: The MCP attach (docs/design/registered-mcp-servers.md §4.6, owner decision
#: Q9 of #1433) is the second such route. And the three submission routes
#: join them, because their bodies carry a caller's per-run MCP credentials
#: (`mcp_credentials`, §4.3): a body capture must skip them too.
UNCAPTURED_ROUTES = frozenset({
    attempts_secrets.RELEASE_ROUTE,
    attempts_mcp.ATTACH_ROUTE,
    "/v1/tasks",
    "/v1/tasks/batch",
    "/v1/workflows",
    # An await-input answer (docs/design/human-steps.md §4.6): a person's
    # form and up to 16 MiB of their files.
    await_input.INPUT_ROUTE,
})


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
               access_service: AccessService | None = None,
               input_writer: InputWriter | None = None) -> FastAPI:
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

    # GitHub permissions health (#1086, PERM-1): read-only, over the App's
    # JWT and transport above; cached per tenant for a minute.
    app.state.forge_perms = build_forge_permissions(app.state.forge_app)

    # The await-input answer's create-only writer (docs/design/human-steps.md
    # §4.3, swarm_api.inputwriter): the one thing here that writes to the
    # artifact bucket. Builds no client until an answer arrives; None when no
    # bucket is configured, which the answer route says by name. A test
    # injects one over the in-memory reader.
    app.state.input_writer = input_writer if input_writer is not None else build_input_writer(
        bucket=app.state.ctx.settings.core.artifact_bucket,
        project_id=app.state.ctx.settings.project_id)
    # Multipart answers this instance holds at once (§4.3 step 3): a fifth is 429.
    app.state.answer_slots = threading.BoundedSemaphore(MAX_CONCURRENT_MULTIPART)

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
    # A workspace's merge settings, direct or merge train (TRAIN-WS,
    # docs/merge-trains.md §3): its owner or a platform admin, audited.
    app.include_router(merge_settings.router)
    # A workspace's code-intelligence setting (CTXQ-SET,
    # docs/design/context-quality.md §4.7): its owner or a platform admin, audited.
    app.include_router(codeintel.router)
    # Connect GitHub as yourself (docs/onboarding.md §3.2, lane OB3):
    # authorise, exchange, disconnect, and the refresh sweep the
    # swarm-forge-refresh job calls. No route returns a token.
    app.include_router(forgeapp.router)
    # The access API (lane OB4): owners, repositories, grants, verify. Acts
    # as the caller through their own connection; no route returns a token.
    app.include_router(access.router)
    # What GitHub permissions the platform needs, what the App and each org
    # granted, and what each registered repository can do (#1086, PERM-1).
    # The caller's own tenant; the App's settings link for admins only.
    app.include_router(forgeperms.router)
    app.include_router(tenants.router)
    # Provider keys a person or a tenant admin sets, TypeSafe first
    # (docs/provider-keys.md, lane KEYS-API): write-only, a person's own key in
    # their personal tenant, the tenant key for admins, one-time form links.
    app.include_router(provider_keys.router)
    # Settings › Profile (docs/design/user-profile.md §4.2, lane PROFILE-API,
    # part of #1505): the caller's own profile in one read, and the save of
    # the preferences that have no other home. For people; no route names one.
    app.include_router(profile.router)
    # The workspace secrets store's management API (docs/design/workspace-
    # secrets.md §5, lane SEC-STORE-API, part of #1347): records and grants,
    # write-only values, each write audited in its own transaction. The
    # release is SEC-RELEASE's, `attempts_secrets` below.
    app.include_router(workspace_secrets.router)
    # Each workspace's Jev judgment backend -- Internal Jev (the default),
    # Internal Laya or TypeSafe -- its owner's or an admin's to choose,
    # audited (swarm_api.judgment, lane JUDGE-SELECT).
    app.include_router(judgment.router)
    # Each workspace's Code intelligence switch -- whether Jev is ever sent
    # code, OFF by default -- one field of workspace_settings/{tenant_id}, its
    # owner's or an admin's to change, audited (CTXQ-UI, context-quality §4.7).
    # The account pool. Every route on it PROXIES to the quota broker, which is
    # the platform's single writer of subscription credentials; this service
    # supplies the caller's tenant and nothing else.
    app.include_router(accounts.router)
    app.include_router(platform.router)
    app.include_router(admin.router)
    # Admin roles in Firestore: grant and remove, audited (docs/workspaces.md
    # §6.5, lane W2). Admin-only; W7 adds the People list to the same router.
    app.include_router(people.router)
    # The schedule tick (docs/schedules.md §2, lane S2): one route, admitted
    # to the `swarm-schedule-tick` identity alone (SD10).
    app.include_router(schedule_tick.router)
    # Stranded pull requests (part of #295): GET /v1/stranded-prs, a member's
    # own tenant's last sweep, and the sweep the scheduler and an admin call.
    app.include_router(strandedprs.router)
    # Schedules (docs/schedules.md §7.1, lane S3): the tenant's own routes,
    # tenant-scoped like issue runs, and the admin list and actions (§5.3).
    # A caller names a type from the catalogue, never an image or command.
    app.include_router(schedules.router)
    # Approvals and the SD3 merge switch (docs/schedules.md §4.5, §7.1, lane
    # S5): the inbox of the caller's own tenant -- its records beside its
    # projected PLANNED runs -- each decision one transaction, and the one
    # audited route to `merge: auto` that PATCH refuses.
    app.include_router(approvals.router)
    # Child tasks (docs/design/child-tasks.md): worker-only routes, which
    # authenticate the tenant's worker service account and an attempt proof
    # rather than a person.
    app.include_router(children.router)
    # The runner git tools (docs/runner-git-tools.md, lane G5): worker-only,
    # authenticated as the child routes are, plus the audit's tenant read.
    app.include_router(gittools.router)
    # The search tool (docs/search-tool.md, lane SEARCH-TOOL): worker-only,
    # authenticated as the git tools are; a 404 while SEARCH_TOOL_ENABLED is off.
    app.include_router(search.router)
    # Layer I, in-flight work (docs/design/context-quality.md §4.2, lane CTXQ-I):
    # worker-only, authenticated and fenced as the git tools are.
    app.include_router(attemptcontext.router)
    # The worker's state (docs/design/tenant-settings-integrity.md §4, lane
    # ISO-STATE-API, part of #1457): worker-only, token for the tenant, attempt
    # key for the worker, fenced in one transaction. Mounted and unused until a
    # tenant's SWARM_STATE_VIA_API is on (lane ISO-WORKER-CORE).
    app.include_router(attemptstate.router)
    # The workspace secrets release (docs/design/workspace-secrets.md §3.3,
    # lane SEC-RELEASE, part of #1347): worker-only, signed under its own
    # purpose and fenced; the one route that serves credential material, and
    # in UNCAPTURED_ROUTES.
    app.include_router(attempts_secrets.router)
    # Registered MCP servers (docs/design/registered-mcp-servers.md, lane
    # MCP-API, part of #1433): the tenant's registrations, the per-run
    # credential sweep, and the worker-only attach, the second route that
    # serves credential material (owner decision Q9), in UNCAPTURED_ROUTES.
    # Every write and the attach are refused `mcp_unavailable` until
    # SWARM_MCP_SERVERS=on.
    app.include_router(mcp_servers.router)
    app.include_router(attempts_mcp.router)
    # Named browser logins and the workflow carry (docs/design/live-browser.md
    # §5.5.1, lane LB-L9b): the person's own logins, and two worker-only
    # routes that authenticate as the child routes do.
    app.include_router(browser_logins.router)
    # The browser-session slot (lane LB-L8c): swarm-api creates each personal
    # tenant's -browser- secret and grants its worker on that secret alone;
    # worker-only, authenticated as the routes above.
    app.include_router(browser_slots.router)
    # The live browser (docs/design/live-browser.md, lane LB-L9): the worker's
    # tunnel ticket, a member's view and control, and the socket proxied to
    # swarm-live. Tenant-scoped through the same authenticator as every route.
    app.include_router(live.router)
    # The TypeSafe forwarder's attempt ticket (docs/design/workspace-secrets.md
    # §3.5, lane SEC-INJECT-A): worker-only, proven and fenced as above.
    app.include_router(typesafe_ticket.router)
    # The shared graph service's attempt ticket (docs/design/graph-service.md
    # §3.1, lane GS-TICKET): worker-only, proven and fenced as above.
    app.include_router(graph_ticket.router)
    # The notification feed (#1206, NOTIF-API; docs/notifications.md): the
    # caller's own items, derived on each read from the records above, and
    # one document of read marks per person.
    app.include_router(notifications.router)
    # Await-input steps (docs/design/human-steps.md §4.6, lane HW-API-ANSWER):
    # a person answers a workflow step; the card and the bridge read it.
    app.include_router(await_input.router)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        # Metrics only: the route TEMPLATE, the method and the status class.
        # Never the body or the response of any route, and above all not of
        # UNCAPTURED_ROUTES.
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
