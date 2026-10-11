"""The live-browser routes (docs/design/live-browser.md §3.3, §4.2, §5.1-§5.2, lane LB-L9).

Two kinds of caller, as the hand-off routes have:

  * THE WORKER asks for a tunnel ticket (`POST /v1/attempts/{id}/live-session`).
    No `current_auth`: the tenant is its ID token checked against the body's,
    and the worker is the attempt key's signature over the raw body.
  * A PERSON, through the ordinary `tenant_scope` -- the same authenticator,
    the same per-group membership, the same principal check every route has --
    reads the session, gets a viewer ticket, takes, renews and returns control,
    opens control to the tenant, and reads the audit. The socket
    (`/v1/live/{session_id}/stream`) authenticates the same identity from the
    upgrade request's headers and selects the tenant the ticket names.

THE RELAY IS PROXIED, NEVER REACHED (owner decision (b), 2026-10-11). The
browser opens wss to swarm-api, behind the existing IAP and HTTPS; swarm-api
dials the relay at SWARM_LIVE_URL (`ws://<internal LB address>:8080`) as plain
ws inside the VPC and splices the two. No certificate exists on that hop.

NO TICKET IN A URL (§5.2). Load balancer, IAP and Cloud Run logs record URLs.
A viewer ticket is returned in a response body and sent as the socket's first
message; the socket's URL carries the task id and nothing else, and a socket
opened with any query string is closed before anything is read.

THE SOCKET'S MESSAGES. Console to swarm-api: the first TEXT frame is
`{"ticket": "<viewer ticket>"}`; after it, BINARY frames are RFB and go to the
relay unchanged, and text frames are dropped (control is the HTTP routes').
swarm-api to the relay (apps/swarm-live, `/v1/view`): the viewer ticket itself
as the first text message, which the relay verifies and spends once, then
`{"type": "control", "controller": true|false}` whenever this person takes or
loses control, then binary RFB. Relay to console: forwarded as is.
swarm-api to console: `{"type": "status", ...}` text frames. Close codes: 4400
a malformed open (a query string, no ticket), 4401 no identity, 4404 refused
(every §5.2 refusal, alike), 4403 membership lost, 4409 the session closed,
4503 no relay, 1012 the relay went away (reconnect).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from fastapi import APIRouter, Body, Depends, Header, Request, Response, WebSocket, status
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..auth import AuthContext, require_continuation_route
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService
from ..deps import AppContext, current_auth, get_context, submission_scope, tenant_scope
from ..errors import ApiError
from ..live import Admission, LiveLimits, LiveService, count_input, relay_view_url
from .children import child_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["live"])

#: How long the console has to send its ticket after the socket opens.
FIRST_MESSAGE_SECONDS = 10.0
#: A first message larger than this is not a ticket.
MAX_FIRST_MESSAGE = 4096

CLOSE_MALFORMED = 4400
CLOSE_UNAUTHENTICATED = 4401
CLOSE_MEMBERSHIP_LOST = 4403
CLOSE_REFUSED = 4404
CLOSE_SESSION_CLOSED = 4409
CLOSE_NO_RELAY = 4503


# --------------------------------------------------------------------------
# The relay connection (lane L7 is the other end)
# --------------------------------------------------------------------------


class RelayLink(Protocol):
    async def send(self, data: str | bytes) -> None: ...
    async def recv(self) -> str | bytes | None: ...
    async def close(self) -> None: ...


class RelayConnector(Protocol):
    async def connect(self, url: str) -> RelayLink: ...


class _WebsocketsLink:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send(self, data: str | bytes) -> None:
        await self._conn.send(data)

    async def recv(self) -> str | bytes | None:
        from websockets.exceptions import ConnectionClosed

        try:
            return await self._conn.recv()
        except ConnectionClosed:
            return None

    async def close(self) -> None:
        await self._conn.close()


class WebsocketsRelay:
    """Dials `swarm-live` with the `websockets` client uvicorn[standard]
    already installs. The ticket is the first MESSAGE, never part of the URL."""

    async def connect(self, url: str) -> RelayLink:
        from websockets.asyncio.client import connect

        conn = await connect(url, open_timeout=10, max_size=8 * 1024 * 1024,
                             compression=None)
        return _WebsocketsLink(conn)


def _relay(app: Any) -> RelayConnector:
    relay = getattr(app.state, "live_relay", None)
    if relay is None:
        relay = WebsocketsRelay()
        app.state.live_relay = relay
    return relay


def _limits(app: Any, ctx: AppContext) -> LiveLimits:
    limits = getattr(app.state, "live_limits", None)
    if limits is None:
        limits = LiveLimits(ctx.settings)
        app.state.live_limits = limits
    return limits


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def live_service(
    ctx: AppContext = Depends(get_context), children: ChildService = Depends(child_service)
) -> LiveService:
    return LiveService(settings=ctx.settings, db=ctx.db, store=ctx.store,
                       authenticator=ctx.authenticator, children=children, now=ctx.now)


def _person_service(ctx: AppContext) -> LiveService:
    return LiveService(settings=ctx.settings, db=ctx.db, store=ctx.store,
                       authenticator=ctx.authenticator, now=ctx.now)


# --------------------------------------------------------------------------
# The worker (§3.3 step 1)
# --------------------------------------------------------------------------


@router.post("/attempts/{attempt_id}/live-session", status_code=status.HTTP_201_CREATED)
async def open_live_session(
    attempt_id: str,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: LiveService = Depends(live_service),
) -> dict:
    """WORKER ONLY: a one-use tunnel ticket for this attempt's gate, valid 60
    seconds, in the body. 201 when the session was opened, 200 for a
    re-ticket of the same attempt's open session."""
    raw = await request.body()
    answer, created = await run_in_threadpool(
        lambda: service.open_session(
            serverless_authorization or authorization,
            attempt_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return answer


# --------------------------------------------------------------------------
# People (§4.2, §5.1, §5.2)
# --------------------------------------------------------------------------


@router.get("/tasks/{task_id}/live/session")
def get_live_session(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The session as the console draws it: open or closed, who controls, and
    what THIS caller may do."""
    return _person_service(ctx).view(tenant_id, task_id, email=auth.email,
                                     submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/ticket")
def mint_viewer_ticket(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A viewer ticket for this caller and this session, 30 seconds, one use.
    Returned in the BODY; the console sends it as the socket's first message."""
    _limits(request.app, ctx).ticket(auth.email)
    return _person_service(ctx).viewer_ticket(tenant_id, task_id, email=auth.email,
                                              submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/control")
def take_control(
    task_id: str,
    request: Request,
    payload: dict | None = Body(default=None),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Take control (§4.2 step 1). `{"preempt": true}` lets the task's owner
    take it from whoever holds it; nobody else can preempt."""
    _limits(request.app, ctx).take(auth.email, task_id)
    preempt = bool((payload or {}).get("preempt") is True)
    return _person_service(ctx).take(tenant_id, task_id, email=auth.email,
                                     submitted_by=submitted_by, preempt=preempt)


@router.post("/tasks/{task_id}/live/control/renew")
def renew_control(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The controller's console says it is still there (§4.2: 60 seconds)."""
    return _person_service(ctx).renew(tenant_id, task_id, email=auth.email,
                                      submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/control/return")
def return_control(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Return control (§4.2 step 3); a waiting hand-off is resolved with it."""
    return _person_service(ctx).return_control(tenant_id, task_id, email=auth.email,
                                               submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/open-to-members")
def open_to_members(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The owner lets any member of the tenant take control of this task's
    browser while its agent asks for help (§5.1, Q1). Audited."""
    return _person_service(ctx).open_to_members(tenant_id, task_id, email=auth.email,
                                                submitted_by=submitted_by)


@router.get("/tasks/{task_id}/live/audit")
def live_audit(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The task's live audit (§5.8): members of its tenant, nobody else."""
    return {"entries": _person_service(ctx).audit_entries(tenant_id, task_id,
                                                          submitted_by=submitted_by)}


# --------------------------------------------------------------------------
# The socket (§3.3 step 3, §5.2)
# --------------------------------------------------------------------------

#: The route template the continuation default-deny is checked against.
SOCKET_ROUTE = ("GET", "/v1/live/{session_id}/stream")


async def _close(websocket: WebSocket, code: int) -> None:
    if websocket.application_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=code)
        except RuntimeError:  # already closing
            pass


async def _admit(websocket: WebSocket, session_id: str, ctx: AppContext,
                 service: LiveService) -> Admission | int:
    """§5.2 for one socket: identity, ticket, tenant, task, session -- the same
    checks as every live route (own tenant, the personal-workspace rule, the
    session's fencing generation). A close code when refused."""
    if websocket.url.query:
        return CLOSE_MALFORMED
    headers = websocket.headers
    presented = headers.get("x-serverless-authorization") or headers.get("authorization")
    iap = headers.get("x-goog-iap-jwt-assertion")
    try:
        await run_in_threadpool(ctx.authenticator.authenticate, presented, iap)
    except Exception as exc:
        ctx.metrics.auth_failures.labels(kind=type(exc).__name__).inc()
        return CLOSE_UNAUTHENTICATED
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), FIRST_MESSAGE_SECONDS)
    except (asyncio.TimeoutError, KeyError, WebSocketDisconnect):
        return CLOSE_MALFORMED
    if len(raw) > MAX_FIRST_MESSAGE:
        return CLOSE_MALFORMED
    try:
        ticket = json.loads(raw).get("ticket")
    except (ValueError, AttributeError):
        return CLOSE_MALFORMED
    if not isinstance(ticket, str) or not ticket:
        return CLOSE_MALFORMED
    try:
        claims_tenant = _ticket_tenant(ticket)
        # The ticket SELECTS the tenant, as X-Swarm-Tenant does on a request:
        # among this identity's verified memberships, and nothing else.
        auth = await run_in_threadpool(
            lambda: ctx.authenticator.authenticate(presented, iap, tenant=claims_tenant)
        )
        require_continuation_route(auth, SOCKET_ROUTE)
        tenant_id = ctx.submissions.scope_for(auth)
        if tenant_id != auth.tenant_id:
            return CLOSE_REFUSED
        submitted_by = auth.email if auth.member_scope else None
        return await run_in_threadpool(
            lambda: service.admit(auth, session_id, ticket, submitted_by=submitted_by)
        )
    except Exception as exc:  # every refusal reads alike (§5.2 step 2)
        log.info("live socket refused (%s)", type(exc).__name__)
        return CLOSE_REFUSED


def _ticket_tenant(ticket: str) -> str | None:
    """The tenant a ticket NAMES, read before it is verified only to select
    among the caller's own memberships; `LiveService.admit` verifies it."""
    import base64

    try:
        payload = ticket.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        tenant = claims.get("tenant_id")
        return tenant if isinstance(tenant, str) and tenant else None
    except Exception:
        return None


@router.websocket("/live/{session_id}/stream")
async def live_socket(websocket: WebSocket, session_id: str) -> None:
    """Proxy one person's view to the relay, re-checking them while it lasts."""
    ctx: AppContext = websocket.app.state.ctx
    service = _person_service(ctx)
    settings = ctx.settings
    await websocket.accept()
    admitted = await _admit(websocket, session_id, ctx, service)
    if not isinstance(admitted, Admission):
        await _close(websocket, admitted)
        return
    admission = admitted
    url = getattr(settings, "live_relay_url", "")
    if not url:
        await _close(websocket, CLOSE_NO_RELAY)
        return
    try:
        link = await _relay(websocket.app).connect(relay_view_url(url))
    except Exception as exc:
        log.warning("the live relay could not be reached (%s)", type(exc).__name__)
        await _close(websocket, CLOSE_NO_RELAY)
        return
    proxy = _Proxy(websocket, link, service, admission, settings)
    await proxy.run()


class _Proxy:
    """One viewer's socket and its relay link, and the ticker that keeps
    checking them (§5.2)."""

    def __init__(self, websocket: WebSocket, link: RelayLink, service: LiveService,
                 admission: Admission, settings: Any) -> None:
        self.ws = websocket
        self.link = link
        self.service = service
        self.admission = admission
        self.poll = float(getattr(settings, "live_session_poll_seconds", 2.0))
        self.recheck = float(getattr(settings, "live_membership_recheck_seconds", 300.0))
        self.flush = float(getattr(settings, "live_input_flush_seconds", 15.0))
        self.in_control = False
        self.keys = 0
        self.pointers = 0
        self.total_keys = 0
        self.total_pointers = 0
        self.close_code = 1000
        self.last_sent: dict[str, Any] | None = None

    async def run(self) -> None:
        # The relay's first message is the ticket itself (apps/swarm-live).
        await self.link.send(self.admission.ticket)
        await run_in_threadpool(self.service.viewer_event, self.admission, "viewer_joined", {})
        await self._status()
        tasks = [asyncio.ensure_future(c) for c in (self._from_viewer(), self._from_relay(),
                                                     self._tick())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.in_control and (self.keys or self.pointers):
                await self._flush_input()
            try:
                await self.link.close()
            except Exception:  # the relay already went
                pass
            await _close(self.ws, self.close_code)
            await run_in_threadpool(
                self.service.viewer_event, self.admission, "viewer_left",
                {"close_code": self.close_code, "keys": self.total_keys,
                 "pointer_events": self.total_pointers},
            )

    async def _status(self) -> bool:
        status_ = await run_in_threadpool(self.service.status, self.admission)
        if not status_["open"]:
            await self._send_viewer({"type": "status", "open": False,
                                     "cause": status_.get("cause")})
            self.close_code = CLOSE_SESSION_CLOSED
            return False
        if status_["in_control"] != self.in_control:
            if not status_["in_control"] and (self.keys or self.pointers):
                self.keys = self.pointers = 0  # the episode ended; its counts are its own
            self.in_control = status_["in_control"]
            await self.link.send(json.dumps({"type": "control", "controller": self.in_control}))
        await self._send_viewer({"type": "status", "open": True,
                                 "in_control": self.in_control,
                                 "controller": status_.get("controller")})
        return True

    async def _send_viewer(self, message: dict[str, Any]) -> None:
        if message == self.last_sent:
            return  # a status is sent when it changes, not every tick
        self.last_sent = message
        await self.ws.send_text(json.dumps(message))

    async def _from_viewer(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                continue  # text after the ticket: control is the HTTP routes'
            if self.in_control:
                keys, pointers = count_input(data)
                self.keys += keys
                self.pointers += pointers
                self.total_keys += keys
                self.total_pointers += pointers
            await self.link.send(data)

    async def _from_relay(self) -> None:
        while True:
            data = await self.link.recv()
            if data is None:
                # The relay went (a restart, §3.3): 1012 tells noVNC to reconnect.
                self.close_code = 1012
                return
            if isinstance(data, bytes):
                await self.ws.send_bytes(data)
            else:
                await self.ws.send_text(data)

    async def _flush_input(self) -> None:
        # Swapped on the event loop, where the counters are incremented, and
        # written from a worker thread.
        keys, pointers = self.keys, self.pointers
        self.keys = self.pointers = 0
        await run_in_threadpool(
            lambda: self.service.record_input(self.admission, keys=keys, pointers=pointers)
        )

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        next_recheck = loop.time() + self.recheck
        next_flush = loop.time() + self.flush
        last_status = None
        while True:
            await asyncio.sleep(self.poll)
            now = loop.time()
            if now >= next_recheck:
                next_recheck = now + self.recheck
                member = await run_in_threadpool(self.service.recheck, self.admission)
                if not member:
                    keys, pointers = self.keys, self.pointers
                    self.keys = self.pointers = 0
                    await run_in_threadpool(
                        lambda: self.service.end_control(
                            self.admission, "membership_lost", keys=keys, pointers=pointers)
                    )
                    self.in_control = False
                    self.close_code = CLOSE_MEMBERSHIP_LOST
                    return
            if self.in_control and (self.keys or self.pointers) and now >= next_flush:
                next_flush = now + self.flush
                await self._flush_input()
            try:
                if not await self._status():
                    return
            except ApiError as exc:
                if exc != last_status:
                    log.info("live session status unavailable (%s)", exc.code)
                last_status = exc
