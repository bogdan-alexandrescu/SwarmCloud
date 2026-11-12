"""The own-speech engine's routes: the Talk page's audio socket and the
machine's sweep (docs/design/voice-it-own-speech.md §3.3, §4.4, §4.5).

    WS   /v1/voice/sessions/{session_id}/audio
    POST /v1/internal/voice/speech/sweep          -> {enabled, lease, ended, stopped, instance,
                                                      abandoned}

THE AUDIO SOCKET (§3.3 step 2) is the person's, behind the same front door
and sign-in as every Voice it route: the caller is authenticated as a request
is (`Authenticator.authenticate`, the IAP assertion or the bearer), must be on
the Voice it list, and must be the session's person in a workspace of theirs
(`stream.find_session`, the SSE route's check). The session must be live, of
mode `speech`, and hold the speech machine's lease; then `SpeechRelay` runs
it. A refusal is a close code and nothing else:

    4401 not signed in     4403 not on Voice it      4404 no such session
    4409 not a live speech session holding the machine
    4503 the engine is not deployed (SWARM_SPEECH_ADDR empty)

Each socket swarm-api opens to the machine carries a Google ID token for
`SWARM_SPEECH_AUDIENCE` (default `swarm-speech`) minted from the metadata
server, the one thing the speech server admits (§3.2). No secret is read.

THE SWEEP (§4.5 stop 1) is Cloud Scheduler's, every two minutes (lane L5's
job), with an OIDC token for SWARM_SPEECH_SWEEP_AUDIENCE from an account in
SWARM_SPEECH_SWEEP_CALLERS; anyone else is 401 or 403, and with no audience
configured the route admits nobody (503). It ends a session whose lease lost
its heartbeat, its tab or its idle time and stops an instance no live lease
holds. Whether or not the engine is on, it also ends every Voice it session
of any mode left `live` without activity for sessions.LIVE_STALE_SECONDS
(`abandoned`: how many), so none lingers in lists or holds its person.

WHAT THE START LANE (L6) CALLS. `machine_for(app, ctx)` is the lifecycle a
`speech` start takes the lease and starts the instance with
(`SpeechMachine.start`, `NoFreeL4` and `MachineNotReady` being the text
fallbacks, then `SpeechMachine.fall_back`), and `SpeechMachine.end_session`
with `END_BY_PERSON` is Stop. `app.state.speech_turns`, when L6 sets it, is
the `speech_relay.Turns` the relay uses (its `VoiceSessions.delegation`);
without it the relay uses `ClientTurns` over the session client. L6 mounts
this `router` beside the Voice it router.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

from fastapi import APIRouter, Request, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..auth import GoogleTokenVerifier
from ..deps import AppContext
from ..errors import Forbidden, NotFound, Unauthenticated, UpstreamUnavailable
from ..voice import readback as vreadback
from ..voice import sessions as vs
from ..voice import settings as vsettings
from ..voice import speech_machine as sm
from ..voice import speech_relay as sr
from ..voice import speech_telemetry as vtelemetry
from ..voice import stream as vstream
from . import voice_readback as rvr

log = logging.getLogger("swarm_api.voice.speech")

router = APIRouter(tags=["voice"])

AUDIO_ROUTE = "/v1/voice/sessions/{session_id}/audio"
SWEEP_ROUTE = "/v1/internal/voice/speech/sweep"

CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_ALLOWED = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_NOT_HELD = 4409
CLOSE_NO_ENGINE = 4503

#: The VM's frames are at most a second of PCM; a megabyte bounds a bad one.
VM_MAX_FRAME = 1024 * 1024
DIAL_TIMEOUT_SECONDS = 10.0
TOKEN_TIMEOUT_SECONDS = 5.0


# --------------------------------------------------------------------------
# What the routes share
# --------------------------------------------------------------------------


def speech_config(app: Any) -> sm.SpeechConfig:
    config = getattr(app.state, "speech_config", None)
    if config is None:
        config = sm.SpeechConfig.from_env()
        app.state.speech_config = config
    return config


def machine_for(app: Any, ctx: AppContext) -> sm.SpeechMachine:
    """The lifecycle, with the Compute client for the one instance and
    Settings' `mark_applied` for readiness."""
    config = speech_config(app)
    compute = getattr(app.state, "speech_compute", None)
    if compute is None:
        compute = sm.RestCompute(config)
        app.state.speech_compute = compute
    settings = vsettings.VoiceSettings(ctx.db, now=ctx.now)

    def applied(values: Mapping[str, str], image: str) -> Any:
        return settings.mark_applied(values=values, image=image)

    return sm.SpeechMachine(ctx.db, compute, config=config, now=ctx.now,
                            marker_key=str(getattr(ctx.settings, "child_key", "") or ""),
                            applied=applied)


def _voice_client(app: Any, ctx: AppContext) -> Any:
    client = getattr(app.state, "voice_client", None)
    if client is None:
        client = vs.client_for(ctx.settings)
        app.state.voice_client = client
    return client


class VoiceSessionEnder:
    """Ends the Voice it session a lease names, with the machine's cause, and
    tells swarm-voice (which releases the front's pool account)."""

    def __init__(self, ctx: AppContext, client: Any) -> None:
        self._ctx = ctx
        self._client = client

    def end(self, lease: Mapping[str, Any], cause: str) -> None:
        tenant_id, person = str(lease.get("tenant_id") or ""), str(lease.get("person") or "")
        project_id, session_id = str(lease.get("project_id") or ""), str(lease.get("session_id") or "")
        if not (tenant_id and person and project_id and session_id):
            return
        service = vs.VoiceSessions(self._ctx.db, self._client,
                                   marker_key=str(getattr(self._ctx.settings, "child_key", "") or ""),
                                   now=self._ctx.now)
        try:
            # The one place sessions.py ends a session with a cause of the
            # caller's: the same transaction a person's end runs.
            before = service._close(tenant_id=tenant_id, person=person, project_id=project_id,
                                    session_id=session_id, cause=cause)
        except NotFound:
            return
        if before is not None and self._client is not None:
            self._client.end(session_id, cause=cause)


def _metadata_token(audience: str) -> str:
    return vs._metadata_id_token(audience, TOKEN_TIMEOUT_SECONDS)


class _VmSocket:
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


def vm_dialer(app: Any, config: sm.SpeechConfig) -> sr.Dial:
    """Dials the speech server with the `websockets` client uvicorn[standard]
    installs; `app.state.speech_dial` replaces it in tests."""
    dial = getattr(app.state, "speech_dial", None)
    if dial is not None:
        return dial

    async def _dial() -> sr.Socket:
        from websockets.asyncio.client import connect

        token = await asyncio.to_thread(_metadata_token, config.audience)
        conn = await connect(config.stream_url, open_timeout=DIAL_TIMEOUT_SECONDS,
                             max_size=VM_MAX_FRAME, compression=None,
                             additional_headers={"Authorization": f"Bearer {token}"})
        return _VmSocket(conn)

    return _dial


class _BrowserSocket:
    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            await self._ws.send_bytes(data)
        else:
            await self._ws.send_text(data)

    async def recv(self) -> str | bytes | None:
        try:
            message = await self._ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if message.get("type") == "websocket.disconnect":
            return None
        if message.get("bytes") is not None:
            return message["bytes"]
        return message.get("text")

    async def close(self) -> None:
        if self._ws.application_state != WebSocketState.DISCONNECTED:
            try:
                await self._ws.close()
            except RuntimeError:
                pass


async def _close(websocket: WebSocket, code: int) -> None:
    if websocket.application_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=code)
        except RuntimeError:
            pass


def _session_mode(ctx: AppContext, found: vstream.FoundSession) -> str | None:
    snap = (ctx.db.collection(vs.PROJECTS).document(found.project_id)
            .collection(vs.SESSIONS).document(found.session_id).get())
    data = (snap.to_dict() or {}) if snap.exists else {}
    return data.get("mode")


# --------------------------------------------------------------------------
# The audio socket (§3.3 step 2)
# --------------------------------------------------------------------------


async def _admit(websocket: WebSocket, session_id: str, ctx: AppContext) -> tuple[Any, str, Any] | int:
    headers = websocket.headers
    presented = headers.get("x-serverless-authorization") or headers.get("authorization")
    iap = headers.get("x-goog-iap-jwt-assertion")
    try:
        auth = await run_in_threadpool(ctx.authenticator.authenticate, presented, iap)
    except Exception as exc:  # noqa: BLE001 -- every refusal reads alike
        log.info("speech socket unauthenticated (%s)", type(exc).__name__)
        return CLOSE_UNAUTHENTICATED
    people = tuple(getattr(ctx.settings, "voice_people", ()) or ())
    if not vs.is_allowed(people, auth.email):
        return CLOSE_NOT_ALLOWED
    person = auth.email.strip().lower()
    try:
        found = await run_in_threadpool(vstream.find_session, ctx.db, person=person,
                                        session_id=session_id, tenants=vstream.tenants_of(auth))
    except Exception as exc:  # noqa: BLE001 -- a malformed id is the same 404
        log.info("speech socket for no session of theirs (%s)", type(exc).__name__)
        return CLOSE_NOT_FOUND
    if found.state != vs.LIVE or await run_in_threadpool(_session_mode, ctx, found) != sm.MODE_SPEECH:
        return CLOSE_NOT_HELD
    return auth, person, found


@router.websocket(AUDIO_ROUTE)
async def audio_socket(websocket: WebSocket, session_id: str) -> None:
    app = websocket.app
    ctx: AppContext = app.state.ctx
    await websocket.accept()
    admitted = await _admit(websocket, session_id, ctx)
    if isinstance(admitted, int):
        await _close(websocket, admitted)
        return
    _auth, person, found = admitted
    config = speech_config(app)
    if not config.enabled:
        await _close(websocket, CLOSE_NO_ENGINE)
        return
    machine = machine_for(app, ctx)
    lease = await run_in_threadpool(machine.read) or {}
    if lease.get("session_id") != found.session_id or lease.get("state") not in sm.HOLDING:
        await _close(websocket, CLOSE_NOT_HELD)
        return
    holder = sm.Holder(session_id=found.session_id, generation=int(lease.get("generation") or 0))
    client = _voice_client(app, ctx)
    ender = VoiceSessionEnder(ctx, client)
    session = sm.SessionRef(tenant_id=found.tenant_id, person=person,
                            project_id=found.project_id, session_id=found.session_id)
    where = vreadback.Where(tenant_id=found.tenant_id, person=person,
                            project_id=found.project_id, session_id=found.session_id)
    chosen = (await run_in_threadpool(vsettings.VoiceSettings(ctx.db, now=ctx.now).read))["settings"]

    # The read-back routes' own helpers: a spoken "go" confirms and tells
    # swarm-voice to re-send the held call, exactly as the answer route does.
    readbacks = rvr.readbacks_for(ctx)

    def _answer(text: str) -> bool:
        answered = readbacks.answer(where, text)
        if answered.get("answer") == vreadback.GO and answered.get("readback"):
            rvr._tell_voice(client, where, answered["readback"]["readback_id"], readbacks)
        return bool(answered.get("to_planner"))

    async def answer_readback(text: str) -> bool:
        return await run_in_threadpool(_answer, text)

    async def on_end(cause: str) -> None:
        await run_in_threadpool(machine.end_session, holder, cause=cause, ender=ender)

    async def on_engine_down(reason: str) -> None:
        def _down() -> None:
            machine.stop(holder, cause=reason)
            machine.fall_back(session, reason=reason)
        await run_in_threadpool(_down)

    turns = getattr(app.state, "speech_turns", None) or sr.ClientTurns(client)
    relay = sr.SpeechRelay(
        browser=_BrowserSocket(websocket), dial=vm_dialer(app, config), machine=machine,
        holder=holder, turns=turns, voice=str(chosen.get("tts_voice") or ""),
        answer_readback=answer_readback, on_end=on_end, on_engine_down=on_engine_down,
        on_frame=getattr(app.state, "speech_frames", None) or vtelemetry.sink(
            vtelemetry.Telemetry(ctx.db, now=ctx.now), session, holder.generation),
        started=sm.SpeechMachine.started_of(lease),
        budget_seconds=max(0.0, sm.READY_SECONDS - float(
            (lease.get("start_phases") or {}).get("start.boot") or 0.0)))
    why = await relay.run()
    log.info("speech socket closed session=%s generation=%s why=%s crashes=%d",
             found.session_id, holder.generation, why, relay.state.crashes)
    if why == sr.ENDED_BROWSER:
        await sr.wait_for_reconnect(machine, holder, on_end=on_end)


# --------------------------------------------------------------------------
# The sweep (§4.5 stop 1)
# --------------------------------------------------------------------------


def _sweep_verifier(app: Any, config: sm.SpeechConfig) -> Any:
    verifier = getattr(app.state, "speech_sweep_verifier", None)
    if verifier is None:
        verifier = GoogleTokenVerifier(config.sweep_audience, require_audience=True)
        app.state.speech_sweep_verifier = verifier
    return verifier


@router.post(SWEEP_ROUTE)
async def sweep(request: Request) -> dict[str, Any]:
    app = request.app
    ctx: AppContext = app.state.ctx
    config = speech_config(app)
    if not config.sweep_audience or not config.sweep_callers:
        raise UpstreamUnavailable("the speech sweep has no caller configured")
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("the speech sweep needs an ID token")
    try:
        claims = await run_in_threadpool(_sweep_verifier(app, config).verify, token.strip())
    except Exception:  # noqa: BLE001 -- the message may echo the token; never passed on
        raise Unauthenticated("the speech sweep's ID token was refused") from None
    email = str(claims.get("email") or "").strip().lower()
    if email not in config.sweep_callers:
        raise Forbidden("the speech sweep is Cloud Scheduler's alone")
    machine = machine_for(app, ctx) if config.enabled else None
    sessions = vs.VoiceSessions(ctx.db, _voice_client(app, ctx),
                                marker_key=str(getattr(ctx.settings, "child_key", "") or ""),
                                now=ctx.now, machine=machine)
    if machine is None:
        abandoned = len(await run_in_threadpool(sessions.sweep_abandoned))
        return {"enabled": False, "lease": None, "ended": None, "stopped": False, "instance": None,
                "abandoned": abandoned}
    # The machine's own causes first; what is left live without activity after.
    report = await run_in_threadpool(machine.sweep, VoiceSessionEnder(ctx, _voice_client(app, ctx)))
    abandoned = len(await run_in_threadpool(sessions.sweep_abandoned))
    log.info("speech sweep %s abandoned=%d", json.dumps(report, sort_keys=True), abandoned)
    return {"enabled": True, **report, "abandoned": abandoned}
