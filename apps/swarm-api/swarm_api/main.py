"""FastAPI application assembly.

`create_app()` takes an optional AppContext so a test can build the entire real
application -- real routes, real validation, real tenant scoping -- around an
in-memory Firestore. Nothing in the request path reads a module-level global.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from swarm_common.identity import AuthError
from swarm_common.states import InvalidTransition

from .access import AccessService, build_access
from .auth import qa_origin_refused
from .compression import GzipWholeResponses
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
    attemptreads,
    attempts,
    attemptstate,
    attempts_mcp,
    attempts_secrets,
    await_input,
    browser_logins,
    browser_slots,
    budget_settings,
    checkpoints,
    children,
    credentials,
    codeintel,
    failurefindings,
    forgeapp,
    forgeperms,
    gittokens,
    gittools,
    graph_cache,
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
    qa_identity,
    qa_settings,
    qatargets,
    quality_gates,
    repositories,
    review_gate_settings,
    roles,
    runs,
    schedule_tick,
    search,
    spendestimate,
    strandedprs,
    schedules,
    tasks,
    tenants,
    typesafe_ticket,
    workflows,
    workspace_name,
    workspace_secrets,
    workspaces,
)
from .routes import repository_secrets
from .routes import actions_secrets
from .routes import secrets_overview
from .routes import join_requests
from .routes import setup
from .routes import addon_probe
from .routes import voice
from .qa_identity import QaIdentityOrigin
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
    # A workflow's per-run MCP credentials, re-stored by its caller
    # (registered-mcp-servers.md §4.10, BGD 2026-10-26).
    workflows.MCP_CREDENTIALS_ROUTE,
    # An await-input answer (docs/design/human-steps.md §4.6): a person's
    # form and up to 16 MiB of their files.
    await_input.INPUT_ROUTE,
    # The QA identity's credential (docs/design/qa-identity.md §4.3): the
    # third route that serves credential material.
    qa_identity.ISSUE_ROUTE,
    # A QA target's test credential (docs/design/visual-qa-any-app.md §2.1,
    # §2.2): the one route that takes it, and the worker's that serves it.
    qatargets.CREDENTIAL_ROUTE,
    qatargets.ISSUE_ROUTE,
    # A QA target's challenge bypass value (§2.5, lane CAPTCHA-BYPASS): the
    # PUT's body is the value itself (BGD, 2026-10-23).
    qatargets.BYPASS_ROUTE,
    # The rest take a credential in their body and were missing until
    # test_uncaptured_routes_cover_credential_bodies.py read the routes
    # instead of this list (lane BYPASS-UNCAPTURED, part of #2058):
    # a workspace secret's value, on its creation and its replacement;
    "/v1/workspace/secrets",
    "/v1/workspace/secrets/{name}/value",
    # A repository's secrets (SSC-REPO-SCOPE): the same add and rotate bodies.
    "/v1/repositories/{repo_id}/secrets",
    "/v1/repositories/{repo_id}/secrets/{name}/value",
    # A GitHub Actions secret's value, before it is sealed (SSC-ACTIONS-API).
    "/v1/repositories/{repo_id}/actions/secrets/{name}",
    # a provider API key, a person's or the workspace's, and the tenant's;
    "/v1/provider-keys/{provider}/user",
    "/v1/provider-keys/{provider}/tenant",
    "/v1/tenants/me/credentials",
    # a Claude subscription's OAuth credential, and the authorization code
    # that redeems for one;
    "/v1/accounts",
    "/v1/accounts/exchange",
    # a GitHub personal access token, and the GitHub App's authorization code.
    "/v1/onboarding/github/token",
    "/v1/onboarding/github/exchange",
    # A workspace's join code (docs/design/join-requests.md §4.2, lane
    # JOIN-API): the two bodies that carry one, and the answer that makes one.
    # A code admits nobody, but it is what lets a person ask, so it stays out
    # of every log like the URL fragment it travels in.
    join_requests.LOOKUP_ROUTE,
    join_requests.ASK_ROUTE,
    join_requests.LINK_ROUTE,
    # Admin › Setup (docs/design/day-one-plan.md §5.3, lane
    # DAYONE-SETUP-API-1): the GitHub App manifest's one-time code, which
    # GitHub redeems for the App's key and secrets; the one-time link a
    # platform admin mints, in its answer; and the redemption, whose body is
    # the link's token and the value its form carries.
    "/v1/admin/setup/github-app/conversion",
    "/v1/admin/setup/links",
    "/v1/setup/links/redeem",
    # The Desktop sign-in client's secret (issue #2964), on its way to
    # Secret Manager.
    "/v1/admin/setup/sign-in-client",
})

# --------------------------------------------------------------------------
# The front door: credential headers beside an IAP assertion, and the scrubber
# (docs/design/qa-identity.md §4.3 "Standing rule", §4.9; lane QI-API)
# --------------------------------------------------------------------------

#: The IAP assertion header. Present means the request came through IAP.
IAP_ASSERTION_HEADER = b"x-goog-iap-jwt-assertion"
#: Headers that carry a credential meant for something in front of swarm-api.
#: Beside an IAP assertion they are REMOVED before any route runs (§4.9, M3
#: measured NO: the QA browser's gate sends the QA account's IAP JWT in
#: `Authorization`, and if IAP passes it through (M2, unmeasured) no route
#: may read it, use it or reflect it). Without an assertion they stay: that is
#: how a worker inside the VPC presents its ID token.
CREDENTIAL_HEADERS = frozenset({
    b"authorization", b"proxy-authorization", b"x-serverless-authorization",
})
#: Every header whose value is scrubbed from an error page.
SCRUBBED_HEADERS = CREDENTIAL_HEADERS | {
    IAP_ASSERTION_HEADER, b"cookie", b"x-swarm-attempt-proof",
}
#: Where `FrontDoor` leaves the scrubbed headers' values for the error
#: handlers, which no longer see the removed ones. In the request's own
#: scope, so it lives exactly as long as the request.
SCRUB_STATE = "_scrub_values"
REDACTED = "[redacted]"
#: A piece of a header value shorter than this is not scrubbed on its own:
#: "Bearer" is not a secret, and replacing short words would garble messages.
_MIN_PIECE = 8
_MIN_SEGMENT = 16

QA_ORIGIN_MESSAGE = (
    "the QA identity's writes are accepted only from the console's own origin"
)


def _present(names: set[bytes], name: bytes) -> str:
    return "true" if name in names else "false"


class FrontDoor:
    """Pure ASGI, so it covers the websocket upgrade as well as HTTP.

    For a request carrying an IAP assertion it: logs, once, whether
    `Authorization`, `Proxy-Authorization` and `X-Serverless-Authorization`
    arrived -- `true` or `false`, never a value -- so M2 is read from the logs
    after deploy; removes those three headers; and refuses a QA-account write
    from another origin 403 `qa_identity_origin` (`auth.qa_origin_refused`)
    before any route runs. For every request it leaves the scrubbed headers'
    values where the error handlers find them.
    """

    def __init__(self, app: Any, *, owner: FastAPI) -> None:
        self.app = app
        self.owner = owner

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers") or [])
        state = scope.setdefault("state", {})
        state[SCRUB_STATE] = tuple(
            v.decode("latin-1") for k, v in headers if k.lower() in SCRUBBED_HEADERS)
        assertion = next(
            (v.decode("latin-1") for k, v in headers if k.lower() == IAP_ASSERTION_HEADER), None)
        if assertion is None:
            await self.app(scope, receive, send)
            return
        names = {k.lower() for k, _ in headers}
        log.info("iap request authorization_present=%s proxy_authorization_present=%s "
                 "serverless_authorization_present=%s",
                 _present(names, b"authorization"), _present(names, b"proxy-authorization"),
                 _present(names, b"x-serverless-authorization"))
        scope = {**scope, "headers": [(k, v) for k, v in headers
                                      if k.lower() not in CREDENTIAL_HEADERS]}
        if scope["type"] == "http":
            origin = next((v.decode("latin-1") for k, v in headers if k.lower() == b"origin"),
                          None)
            ctx = self.owner.state.ctx
            refused = await run_in_threadpool(
                qa_origin_refused, ctx.settings, getattr(ctx.authenticator, "iap", None),
                method=str(scope.get("method") or ""), origin=origin, assertion=assertion)
            if refused:
                log.info("qa identity write refused: origin %s",
                         "absent" if origin is None else "not the console's")
                refusal = QaIdentityOrigin(QA_ORIGIN_MESSAGE)
                response = JSONResponse(status_code=refusal.status_code,
                                        content=refusal.to_payload())
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _scrub_values(request: Request) -> list[str]:
    """Every value, and every long piece of one, of the scrubbed headers this
    request carried, longest first so a whole value goes before its parts."""
    values = (request.scope.get("state") or {}).get(SCRUB_STATE)
    if values is None:
        values = tuple(v for k, v in request.headers.items()
                       if k.lower().encode("latin-1") in SCRUBBED_HEADERS)
    found: set[str] = set()
    for value in values:
        value = value.strip()
        if len(value) >= _MIN_PIECE:
            found.add(value)
        for piece in re.split(r"[\s,;]+", value):
            if len(piece) >= _MIN_PIECE:
                found.add(piece)
            for segment in re.split(r"[.=]", piece):
                if len(segment) >= _MIN_SEGMENT:
                    found.add(segment)
    return sorted(found, key=len, reverse=True)


def scrub(content: Any, values: list[str]) -> Any:
    """`content` with every one of `values` replaced by "[redacted]", through
    dicts and lists. swarm-api never echoes a request header (§4.3): no route
    does today (the audit is in QI-API's pull request), and this holds every
    later route's error page to it."""
    if not values:
        return content
    if isinstance(content, str):
        for value in values:
            content = content.replace(value, REDACTED)
        return content
    if isinstance(content, dict):
        return {scrub(k, values): scrub(v, values) for k, v in content.items()}
    if isinstance(content, (list, tuple)):
        return [scrub(v, values) for v in content]
    return content


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
    # Rename a workspace: its display name only, never its id (WORKSPACE-ADMIN-
    # FIELDS, part of #2157): its admins or a platform admin, audited.
    app.include_router(workspace_name.router)
    # A workspace's code-intelligence setting (CTXQ-SET,
    # docs/design/context-quality.md §4.7): its owner or a platform admin, audited.
    app.include_router(codeintel.router)
    # A workspace's visual-QA settings: how many flows one live-browser step
    # runs when a run names none (QA-FLOWS-CAP): its owner or admin, audited.
    app.include_router(qa_settings.router)
    # A workspace's quality gates: whether its claude-code lanes run
    # jev-belay and jev-enforce (JEV-LANES): its owner or admin, audited.
    app.include_router(quality_gates.router)
    # A workspace's per-agent budget, which a step may only lower and the
    # platform ceiling caps (BUDGET-CAP, #2459): its owner or admin, audited.
    app.include_router(budget_settings.router)
    # A workspace's default review gate, which every review step that names
    # none follows (#2524, `reviewgate`): its owner or admin, audited.
    app.include_router(review_gate_settings.router)
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
    # Workspace roles (docs/design/workspace-roles.md §4.3, lane
    # ROLES-API-CORE): a workspace's own roster and writes, and a platform
    # admin's for any registered group workspace.
    app.include_router(roles.router)
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
    app.include_router(repository_secrets.router)  # SSC-REPO-SCOPE, secrets-scopes.md §6.2
    app.include_router(actions_secrets.router)  # SSC-ACTIONS-API, secrets-scopes.md §3, §6.2
    app.include_router(secrets_overview.router)  # SSC-OVERVIEW, secrets-scopes.md §4, §6.2
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
    # Join requests (docs/design/join-requests.md §4.8, lane JOIN-API): a
    # person asks with a workspace's join link; its admins decide.
    app.include_router(join_requests.router)
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
    app.include_router(credentials.router)
    # The runner git tools (docs/runner-git-tools.md, lane G5): worker-only,
    # authenticated as the child routes are, plus the audit's tenant read.
    app.include_router(gittools.router)
    # The search tool (docs/search-tool.md, lane SEARCH-TOOL): worker-only,
    # authenticated as the git tools are; a 404 while SEARCH_TOOL_ENABLED is off.
    app.include_router(search.router)
    # Layer I, in-flight work (docs/design/context-quality.md §4.2, lane CTXQ-I):
    # worker-only, authenticated and fenced as the git tools are.
    app.include_router(attemptcontext.router)
    # The stopped-run estimate (#1481): worker-only, proven as the in-flight
    # route is, NOT fenced -- it is asked as an attempt stops, and it reads
    # only the platform's price table and writes nothing.
    app.include_router(spendestimate.router)
    # The worker's state (docs/design/tenant-settings-integrity.md §4, lane
    # ISO-STATE-API, part of #1457): worker-only, token for the tenant, attempt
    # key for the worker, fenced in one transaction. Mounted and unused until a
    # tenant's SWARM_STATE_VIA_API is on (lane ISO-WORKER-CORE).
    app.include_router(attemptstate.router)
    # The agent's reads of its own tenant's state (docs/design/agent-autonomy.md
    # §3.4, lane AU-4 STATE-READS, part of #1933): worker-relayed, proven as the
    # state routes are, fenced, scoped to the lease's tenant, read-only.
    app.include_router(attemptreads.router)
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
    # The console's Cache section (docs/design/graph-answer-cache.md §5.3,
    # lane CACHE-3-ROUTE): tenant-scoped, one stats-ticket read of swarm-graph.
    app.include_router(graph_cache.router)
    # The notification feed (#1206, NOTIF-API; docs/notifications.md): the
    # caller's own items, derived on each read from the records above, and
    # one document of read marks per person.
    app.include_router(notifications.router)
    # Await-input steps (docs/design/human-steps.md §4.6, lane HW-API-ANSWER):
    # a person answers a workflow step; the card and the bridge read it.
    app.include_router(await_input.router)
    # The QA identity (docs/design/qa-identity.md §4.3, lane QI-API): the
    # worker's credential, in UNCAPTURED_ROUTES; a member's read of its own
    # workspace's grant; and the platform admin's grants.
    app.include_router(qa_identity.router)
    # QA targets (docs/design/visual-qa-any-app.md §1, §2, §6, lane VQ-2):
    # registration, host approval, the credential link, `:run`, and the
    # worker's read of its attempt's target, in UNCAPTURED_ROUTES.
    app.include_router(qatargets.router)
    # Failure findings (docs/design/failure-review.md §1, lane FR-4): the
    # caller's tenant's findings from the failure review, read only, through
    # an allow-list that keeps the platform tenant's fields out.
    app.include_router(failurefindings.router)
    # Admin › Setup and Health (docs/design/day-one-plan.md §5.3, lane
    # DAYONE-SETUP-API-1): platform admins only, plus the deployer on the hook
    # and groups probe and swarm-doctor on the health write; the link
    # redemption is any signed-in person's, for their own link.
    app.include_router(setup.router)
    app.include_router(setup.links_router)
    # The brave-search add-on's probe (lane DAYONE-SEARCH-PROBE): a platform
    # admin's, one fixed search with swarm-api's own key, a count back.
    app.include_router(addon_probe.router)
    # Voice it's session routes (docs/design/voice-it.md §10 phase 1): the
    # people VOICE_IT_PEOPLE names (else the platform owner) only, in the
    # console's selected workspace; 503 while swarm-voice is not deployed.
    app.include_router(voice.router)

    # Gzip for a caller that asks (#1822, lane API-GZIP): 3.23 GB left this
    # API in 48 h uncompressed, a build-board task page averaging 1.43 MB and
    # `sc overview --json` 1.24 MB. Whole answers only, never a stream or
    # `text/event-stream` (compression.py says why). Added FIRST so it is the
    # innermost: `observe` below re-sends every body in parts, and outside it
    # every answer would look streamed and pass through uncompressed.
    app.add_middleware(GzipWholeResponses)
    # Inner to `observe`, so a request it refuses is still counted.
    app.add_middleware(FrontDoor, owner=app)

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
        # A platform admin's audited request (§4.2.4): its row was written
        # before the route ran, by `deps.current_auth`; the status it ended
        # with is logged here, on the same request.
        audited = getattr(request.state, "platform_admin", None)
        if audited is not None:
            log.info("platform_admin %s tenant=%s route=%s status=%s",
                     "acted" if audited[0] == "platform_admin_acted" else "viewed",
                     audited[1], audited[2], response.status_code)
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
            status_code=exc.status_code,
            content=scrub(exc.to_payload(), _scrub_values(request)),
            headers=headers,
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        # Messages from the frozen identity module are token-free by contract.
        return JSONResponse(
            status_code=401,
            content=scrub({"code": "unauthenticated", "message": str(exc)},
                          _scrub_values(request)),
            headers={"WWW-Authenticate": 'Bearer realm="swarm", error="invalid_token"'},
        )

    @app.exception_handler(InvalidTransition)
    async def transition_handler(request: Request, exc: InvalidTransition) -> JSONResponse:
        payload = Conflict(str(exc), detail={"from": exc.frm.value, "to": exc.to.value})
        return JSONResponse(status_code=payload.status_code,
                            content=scrub(payload.to_payload(), _scrub_values(request)))

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
        return JSONResponse(status_code=422, content=scrub(body, _scrub_values(request)))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Any:
        # FastAPI's own answer (404, 405 and any route's HTTPException), with
        # its detail scrubbed like every other error page here.
        scrubbed = StarletteHTTPException(
            status_code=exc.status_code,
            detail=scrub(exc.detail, _scrub_values(request)),
            headers=getattr(exc, "headers", None),
        )
        return await http_exception_handler(request, scrubbed)

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
