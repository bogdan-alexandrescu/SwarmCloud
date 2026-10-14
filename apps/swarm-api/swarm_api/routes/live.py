"""The live-browser routes (docs/design/live-browser.md §3.3, §4.2, §5.1-§5.2, lane LB-L9).

Two kinds of caller, as the hand-off routes have:

  * THE WORKER asks for a tunnel ticket (`POST /v1/attempts/{id}/live-session`).
    No `current_auth`: the tenant is its ID token checked against the body's,
    and the worker is the attempt key's signature over the raw body.
  * A PERSON, through the ordinary `tenant_scope` -- the same authenticator,
    the same per-group membership, the same principal check every route has --
    reads the session, gets a viewer ticket, takes, renews and returns control,
    switches the device while in control (LB-DEV-B), opens control to the
    tenant and closes it again (QA12-11), and reads the audit. The socket
    (`/v1/live/{session_id}/stream`) authenticates the same identity from the
    upgrade request's headers and selects the tenant the ticket names.

THE PROXY (owner decision 2026-10-11 (b)). Browsers never reach the relay.
They open wss to swarm-api through IAP, and swarm-api proxies the socket to
the relay as plain ws over the internal load balancer, at `SWARM_LIVE_URL`
(`ws://<internal address>:8080`) + `/v1/view`.

NO TICKET IN A URL (§5.2). Load balancer, IAP and Cloud Run logs record URLs.
A viewer ticket is returned in a response body and sent as the socket's first
message; the socket's URL carries the session id and nothing else, and a
socket opened with any query string is closed before anything is read.

THE SOCKET'S MESSAGES. Console to swarm-api: the first TEXT frame is
`{"ticket": "<viewer ticket>"}`; after it, BINARY frames are RFB and go to the
relay unchanged, and text frames are dropped (control is the HTTP routes').
swarm-api to the relay (lane L7's protocol, `swarm_live.main`): the viewer
ticket itself as the first text message, which the relay answers
`{"type": "ready"}` or refuses by closing; then
`{"type": "control", "ticket": <control or release ticket>}` whenever this
person takes or loses control, then binary RFB. Relay to console: forwarded
as is, except the gate's `{"type": "report", ...}` (the top frame's origin and
the device, LB-DEV-A), which is checked and recorded on the session
(`LiveService.record_report`) and never forwarded: the console reads it from
the session read, checked, like every other viewer. swarm-api to console: `{"type": "status", ...}` text frames. Close
codes: 4400 a malformed open (a query string, no ticket), 4401 no identity,
4404 refused (every §5.2 refusal, alike, the relay's included), 4429 the
relay's viewer cap, 4403 membership lost, 4409 the session closed, 4503 no
relay, 1012 the relay went away (reconnect).

CLOSING (decision (a)). When a session closes, or a stale generation is seen,
`LiveService` POSTs the relay's `/internal/close`; the relay drops every
connection of the session, this socket's relay link included.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Header, Query, Request, Response, WebSocket, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..auth import AuthContext, require_continuation_route
from ..children import PROOF_HEADER, TIMESTAMP_HEADER, ChildService, parse_body
from ..deps import AppContext, current_auth, get_context, submission_scope, tenant_scope
from ..egress import EgressService
from ..errors import ApiError, NotFound, ValidationFailed
from ..live import (
    Admission,
    LiveActionsView,
    LiveLimits,
    LiveRead,
    LiveRecordings,
    RecordingsView,
    LiveService,
    LiveTicketView,
    RelayCloser,
    REPORT_TYPE,
    TakeControl,
    count_input,
    parse_report,
    relay_closer_for,
)
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
CLOSE_FULL = 4429
CLOSE_NO_RELAY = 4503
#: The relay's viewer cap (`swarm_live.rendezvous.CLOSE_FULL`), passed on as
#: itself; every other refusal by the relay reads as 4404, as ours do.
RELAY_CLOSE_FULL = 4429
#: The relay's route for a proxied viewer (`swarm_live.main`).
RELAY_VIEW_PATH = "/v1/view"


# --------------------------------------------------------------------------
# The relay connection (lane L7 is the other end)
# --------------------------------------------------------------------------


class RelayLink(Protocol):
    async def send(self, data: str | bytes) -> None: ...
    async def recv(self) -> str | bytes | None: ...
    async def close(self) -> None: ...
    #: The relay's close code once `recv` has returned None, when it sent one.
    close_code: int | None


class RelayConnector(Protocol):
    async def connect(self, url: str) -> RelayLink: ...


class _WebsocketsLink:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.close_code: int | None = None

    async def send(self, data: str | bytes) -> None:
        await self._conn.send(data)

    async def recv(self) -> str | bytes | None:
        from websockets.exceptions import ConnectionClosed

        try:
            return await self._conn.recv()
        except ConnectionClosed as exc:
            received = getattr(exc, "rcvd", None)
            self.close_code = getattr(received, "code", None)
            return None

    async def close(self) -> None:
        await self._conn.close()


class WebsocketsRelay:
    """Dials `swarm-live` with the `websockets` client uvicorn[standard]
    already installs: plain ws inside the VPC (decision (b)). The ticket is the
    first MESSAGE, never part of the URL."""

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


def _closer(app: Any, ctx: AppContext) -> RelayCloser | None:
    """The relay's close route, from `SWARM_LIVE_URL` and the key; a test puts
    its own on `app.state.live_relay_closer`."""
    closer = getattr(app.state, "live_relay_closer", None)
    if closer is None:
        closer = relay_closer_for(ctx.settings)
        app.state.live_relay_closer = closer
    return closer


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
    request: Request,
    ctx: AppContext = Depends(get_context),
    children: ChildService = Depends(child_service),
) -> LiveService:
    return LiveService(settings=ctx.settings, db=ctx.db, store=ctx.store,
                       authenticator=ctx.authenticator, children=children,
                       relay=_closer(request.app, ctx), now=ctx.now)


def _person_service(ctx: AppContext, app: Any) -> LiveService:
    return LiveService(settings=ctx.settings, db=ctx.db, store=ctx.store,
                       authenticator=ctx.authenticator, relay=_closer(app, ctx), now=ctx.now)


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


@router.post("/attempts/{attempt_id}/egress-ticket", status_code=status.HTTP_201_CREATED)
async def mint_egress_ticket(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    ctx: AppContext = Depends(get_context),
    children: ChildService = Depends(child_service),
) -> dict:
    """WORKER ONLY: the attempt egress ticket the gate presents to swarm-egress
    on every CONNECT (docs/browser-egress.md §2.2, §2.5), under the same proof
    and fence as `/live-session`. 409 `egress_off` when the platform, the
    tenant or the profile is not switched on: the attempt then runs direct."""
    raw = await request.body()
    service = EgressService(settings=ctx.settings, db=ctx.db, children=children, now=ctx.now)
    return await run_in_threadpool(
        lambda: service.mint(
            serverless_authorization or authorization,
            attempt_id,
            raw,
            path=request.url.path,
            proof=proof,
            timestamp=timestamp,
        )
    )


# --------------------------------------------------------------------------
# People (§4.2, §5.1, §5.2)
# --------------------------------------------------------------------------


@router.get("/tasks/{task_id}/live/session", response_model=LiveRead)
def get_live_session(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The session as the console draws it: open or closed, who controls, and
    what THIS caller may do."""
    return _person_service(ctx, request.app).view(tenant_id, task_id, email=auth.email,
                                     submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/ticket", response_model=LiveTicketView)
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
    return _person_service(ctx, request.app).viewer_ticket(tenant_id, task_id, email=auth.email,
                                              submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/control", response_model=LiveRead)
async def take_control(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Take control (§4.2 step 1). The body is the console's `takeLiveControl`
    (`TakeControl`): `{"preempt": true}` lets the task's owner take it from
    whoever holds it, nobody else; `confirmed_mismatch` names the expected and
    actual domains a person confirmed past (§5.7 item 2), recorded in the
    `control_taken` audit entry. An empty body is `{}`."""
    raw = await request.body()
    if len(raw) > 4096:
        raise ValidationFailed("a take-control request is at most 4096 bytes")
    body: TakeControl = parse_body(TakeControl, raw) if raw.strip() else TakeControl()
    _limits(request.app, ctx).take(auth.email, task_id)
    return await run_in_threadpool(
        lambda: _person_service(ctx, request.app).take(
            tenant_id, task_id, email=auth.email, submitted_by=submitted_by,
            preempt=body.preempt, confirmed_mismatch=body.confirmed_mismatch,
        )
    )


@router.post("/tasks/{task_id}/live/control/renew", response_model=LiveRead)
def renew_control(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The controller's console says it is still there (§4.2: 60 seconds)."""
    return _person_service(ctx, request.app).renew(tenant_id, task_id, email=auth.email,
                                      submitted_by=submitted_by)


@router.delete("/tasks/{task_id}/live/control", response_model=LiveRead)
@router.post("/tasks/{task_id}/live/control/return", response_model=LiveRead)
def return_control(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Return control (§4.2 step 3); a waiting hand-off is resolved with it.
    `DELETE .../live/control` is the console's (`returnLiveControl`); the
    `POST .../control/return` it was first served as stays, for the
    acceptance driver and any script written against it."""
    return _person_service(ctx, request.app).return_control(tenant_id, task_id, email=auth.email,
                                               submitted_by=submitted_by)


@router.post("/tasks/{task_id}/live/device", response_model=LiveRead)
async def set_device(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The person in control switches the device (§6.1, Q13; LB-DEV-B). The
    body is the console's `setLiveDevice` (`SetDevice`): `{"preset": <a name
    from the session's presets>, "orientation"?: "portrait"|"landscape",
    "touch"?: bool}` -- a NAME, never a size (invariant 10): an unknown name or
    any other key is 422. Anyone not in control is 409 `not_controller`, as
    renew and return answer. The choice is audited and sent through the relay
    to the gate, which applies it and reports it; the answer is the session
    read, whose `device` follows the gate's report."""
    raw = await request.body()
    if len(raw) > 1024:
        raise ValidationFailed("a device request is at most 1024 bytes")
    return await run_in_threadpool(
        lambda: _person_service(ctx, request.app).set_device(
            tenant_id, task_id, email=auth.email, submitted_by=submitted_by, raw=raw,
        )
    )


@router.post("/tasks/{task_id}/live/open-to-members", response_model=LiveRead)
def open_to_members(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The owner lets any member of the tenant take control of this task's
    browser while its agent asks for help (§5.1, Q1). Audited."""
    return _person_service(ctx, request.app).open_to_members(tenant_id, task_id, email=auth.email,
                                                submitted_by=submitted_by)


@router.delete("/tasks/{task_id}/live/open-to-members", response_model=LiveRead)
def close_to_members(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The owner turns member control off again, for this attempt and the
    task's later ones (owner decision 2026-10-13, QA12-11). Audited."""
    return _person_service(ctx, request.app).close_to_members(tenant_id, task_id, email=auth.email,
                                                 submitted_by=submitted_by)


@router.get("/tasks/{task_id}/live/audit")
def live_audit(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The task's live audit (§5.8): members of its tenant, nobody else."""
    return {"entries": _person_service(ctx, request.app).audit_entries(tenant_id, task_id,
                                                          submitted_by=submitted_by)}


# --------------------------------------------------------------------------
# Recordings and the action timeline (§7)
# --------------------------------------------------------------------------

#: A segment name as the console's Recordings list sends it back.
_SEGMENT_NAME = re.compile(r"rec-([0-9]{1,13})\.live\.webm")


def _recordings(ctx: AppContext) -> LiveRecordings:
    return LiveRecordings(store=ctx.store, objects=ctx.inspection._objects, db=ctx.db)


@router.get("/tasks/{task_id}/live/recordings", response_model=RecordingsView)
def live_recordings(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every attempt's recorded segments, for members of the task's tenant
    (§7). Kept 7 days by the bucket's own rule."""
    return _recordings(ctx).recordings(tenant_id, task_id, submitted_by=submitted_by)


def _segment_response(ctx: AppContext, tenant_id: str, task_id: str, attempt_id: str,
                      seq: int, submitted_by: str | None) -> StreamingResponse:
    chunks, total = _recordings(ctx).segment(tenant_id, task_id, attempt_id, seq,
                                             submitted_by=submitted_by)
    return StreamingResponse(chunks, media_type="video/webm", headers={
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "X-Recording-Bytes": str(total),
    })


@router.get("/tasks/{task_id}/live/recordings/raw")
def live_recording_raw(
    task_id: str,
    attempt_id: str = Query(min_length=1, max_length=128),
    name: str = Query(min_length=1, max_length=64),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> StreamingResponse:
    """One segment's masked frames (Recordings.tsx's player). The object is
    built from the task, the attempt and the segment number; `name` must be a
    segment name, never a path. `?tenant=` is accepted (a `<video src>` sends
    no header), through the same membership check as the header."""
    match = _SEGMENT_NAME.fullmatch(name)
    if match is None:
        raise NotFound("no such recording")
    return _segment_response(ctx, tenant_id, task_id, attempt_id, int(match.group(1)),
                             submitted_by)


@router.get("/tasks/{task_id}/attempts/{attempt_id}/live/recordings/{seq}")
def live_recording_segment(
    task_id: str,
    attempt_id: str,
    seq: int,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> StreamingResponse:
    """The same segment by attempt and number (Theater.tsx's replay)."""
    return _segment_response(ctx, tenant_id, task_id, attempt_id, seq, submitted_by)


@router.get("/tasks/{task_id}/live/actions", response_model=LiveActionsView,
            response_model_exclude_none=True)
def live_actions(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The newest attempt's action timeline (§7): CDP methods as the relay
    recorded them, a typed text as its length, and what people did, from the
    audit -- each with who, and `you` when it was the reader (LB-V3)."""
    return _recordings(ctx).actions(tenant_id, task_id, submitted_by=submitted_by,
                                    viewer=auth.email)


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
    """§5.2 for one socket: identity, ticket, tenant, task, session. A close
    code when refused."""
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
        payload = ticket.split(".")[0]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        tenant = claims.get("tenant_id")
        return tenant if isinstance(tenant, str) and tenant else None
    except Exception:
        return None


@router.websocket("/live/{session_id}/stream")
async def live_socket(websocket: WebSocket, session_id: str) -> None:
    """Proxy one person's view to the relay, re-checking them while it lasts."""
    ctx: AppContext = websocket.app.state.ctx
    service = _person_service(ctx, websocket.app)
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
        link = await _relay(websocket.app).connect(url.rstrip("/") + RELAY_VIEW_PATH)
    except Exception as exc:
        log.warning("the live relay could not be reached (%s)", type(exc).__name__)
        await _close(websocket, CLOSE_NO_RELAY)
        return
    refused = await _hello(link, admission)
    if refused is not None:
        await _close(websocket, refused)
        return
    proxy = _Proxy(websocket, link, service, admission, settings)
    await proxy.run()


async def _hello(link: RelayLink, admission: Admission) -> int | None:
    """Present the viewer ticket to the relay as its first message and wait
    for `{"type": "ready"}`. A close code for the console when the relay
    refused it -- the relay spends each ticket once, so a ticket replayed to
    swarm-api passes our checks and stops here."""
    try:
        await link.send(admission.ticket)
        first = await asyncio.wait_for(link.recv(), FIRST_MESSAGE_SECONDS)
    except Exception as exc:
        log.info("the live relay did not answer the viewer ticket (%s)", type(exc).__name__)
        first = None
    ready = False
    if isinstance(first, str):
        try:
            ready = json.loads(first) == {"type": "ready"}
        except ValueError:
            ready = False
    if ready:
        return None
    code = getattr(link, "close_code", None)
    try:
        await link.close()
    except Exception:  # the relay already went
        pass
    log.info("live socket refused by the relay (close code %s)", code)
    return CLOSE_FULL if code == RELAY_CLOSE_FULL else CLOSE_REFUSED


def _is_report(text: str) -> bool:
    """A relay text that is the gate's report, well-formed or not: never forwarded."""
    try:
        message = json.loads(text)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("type") == REPORT_TYPE


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
        #: HANDOFF-POLICY: how often this open socket says "someone is
        #: watching", which keeps an open hand-off's hold from parking.
        self.watch = float(getattr(settings, "live_watch_flush_seconds", 15.0))
        self.in_control = False
        #: The episode `keys` and `pointers` belong to: counted while it was
        #: held, written to it even when it ends before they are flushed.
        self.episode: str | None = None
        self.keys = 0
        self.pointers = 0
        self.total_keys = 0
        self.total_pointers = 0
        self.close_code = 1000
        self.last_sent: dict[str, Any] | None = None
        self.last_report: dict[str, Any] | None = None

    async def run(self) -> None:
        await run_in_threadpool(self.service.viewer_event, self.admission, "viewer_joined", {})
        await self._watched()
        await self._status()
        tasks = [asyncio.ensure_future(c) for c in (self._from_viewer(), self._from_relay(),
                                                     self._tick())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.keys or self.pointers:
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
            # Closed, or its generation moved: the relay drops every
            # connection of it, not only this one (decision (a)).
            await run_in_threadpool(self.service.close_relay, self.admission.session_id)
            return False
        episode = status_.get("episode_id") if status_["in_control"] else None
        if self.in_control and episode != self.episode and (self.keys or self.pointers):
            # The episode ended (a Return press, a lapse, a preemption) with
            # input this socket had not flushed: it is that episode's, and goes
            # on its entries, never into the next one's (LB-POLISH).
            await self._flush_input()
        self.episode = episode
        if status_["in_control"] != self.in_control:
            self.in_control = status_["in_control"]
            ticket = await run_in_threadpool(self.service.control_ticket, self.admission,
                                             self.in_control)
            await self.link.send(json.dumps({"type": "control", "ticket": ticket}))
        await self._send_viewer({"type": "status", "open": True,
                                 "in_control": self.in_control,
                                 "controller": status_.get("controller")})
        return True

    async def _watched(self) -> None:
        """A viewer is here: move the open hand-off's hold (`LiveService.watched`).
        A failure is logged and retried on the next flush, never the socket's end."""
        try:
            await run_in_threadpool(self.service.watched, self.admission)
        except Exception as exc:  # noqa: BLE001 -- the view goes on
            log.info("live watch not recorded (%s)", type(exc).__name__)

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
            elif _is_report(data):
                await self._record_report(data)
            else:
                await self.ws.send_text(data)

    async def _record_report(self, text: str) -> None:
        report = parse_report(text)
        if report is None or report == self.last_report:
            return
        try:
            await run_in_threadpool(self.service.record_report, self.admission, report)
        except Exception as exc:  # noqa: BLE001 -- the view goes on; the next report retries
            log.info("live report not recorded (%s)", type(exc).__name__)
            return
        self.last_report = report

    async def _flush_input(self) -> None:
        # Swapped on the event loop, where the counters are incremented, and
        # written from a worker thread. To the episode they were counted in:
        # still held, onto the controller record; ended, onto its entries.
        keys, pointers, episode = self.keys, self.pointers, self.episode
        self.keys = self.pointers = 0
        held = await run_in_threadpool(
            lambda: self.service.record_input(self.admission, keys=keys, pointers=pointers,
                                              episode_id=episode)
        )
        if not held and episode is not None:
            await run_in_threadpool(
                lambda: self.service.record_episode_tail(self.admission, episode, keys=keys,
                                                         pointers=pointers)
            )

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        next_recheck = loop.time() + self.recheck
        next_flush = loop.time() + self.flush
        next_watch = loop.time() + self.watch
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
            if now >= next_watch:
                next_watch = now + self.watch
                await self._watched()
            try:
                if not await self._status():
                    return
            except ApiError as exc:
                if exc != last_status:
                    log.info("live session status unavailable (%s)", exc.code)
                last_status = exc
