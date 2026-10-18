"""swarm-live: the live-browser relay's HTTP surface (LB-L7, wired by LB-L8d).

docs/design/live-browser.md §3.3. A single-replica StatefulSet in
`swarm-system` on the GKE cluster -- one process, so a session's two ends always
meet in it -- listening on plain WS, TCP 8080, inside the VPC (owner decision
2026-10-11 (b)). Browsers never reach it: swarm-api proxies each viewer here
over the internal load balancer, and the NetworkPolicy admits only swarm-api's
egress range and the tenant gates. There is no TLS and no certificate anywhere
on this hop; §3.3 records why that is acceptable.

Five routes, and a health check:

* `/v1/tunnel`        -- the gate dials out to here (§3.3 step 2). First
  message: the tunnel ticket, as text. Then the framed tunnel protocol of
  rendezvous.py.
* `/v1/view`          -- swarm-api proxies one console viewer to here (§3.3
  step 3). First message: the viewer ticket swarm-api minted, as text. Then
  the viewer's RFB bytes as binary, and swarm-api's control messages as text.
* `POST /internal/close` -- swarm-api ends a session (decision (a)): the body
  is a close request MAC'd with swarm-live-key (rendezvous.verify_close). Every
  connection of the session is dropped. 200 when it verified, 404 otherwise.
* `POST /internal/device` -- swarm-api passes the controller's device choice
  to the gate (LB-DEV-B): the body is a device request MAC'd with
  swarm-live-key (rendezvous.verify_device), sent to the session's gate as a
  `DEVICE` frame. 200 when it was sent, 404 otherwise -- a forged, replayed
  or expired request and a session with no tunnel here alike.
* `POST /internal/control` -- swarm-api says a person's control ended or was
  renewed (CONTROL-LAPSE): the body is a control request MAC'd with
  swarm-live-key (rendezvous.verify_control); a revoke reaches the gate as
  `CONTROL` with no controller, a renewal as `CONTROL` naming the controller
  again. 200 when it verified and matched a tunnel here, 404 otherwise.
* `/healthz`          -- counts only: tunnels, viewers, and sessions a person
  controls (`controlled`). scripts/roll-live-relay.sh reads it through
  `kubectl exec ... python -m swarm_live.status` -- on the pod's loopback,
  behind the Kubernetes API's own authentication -- and defers a roll while
  anyone is watching or in control (RELAY-ROLL).

SIGTERM (RELAY-ROLL). `RelayServer.shutdown` tells every gate and viewer to
reconnect (`Rendezvous.restarting`, close code 1012) BEFORE uvicorn's own
shutdown cuts the sockets, and every connection that arrives during the drain
is refused with the same code.

THE RELAY READS THE TICKET AND NOTHING ELSE. No Firestore client is built, and
no session document is read (decision (a)); rendezvous.py says how a session
is defined by its tunnel ticket instead.

NOTHING SECRET IN A URL (§5.2): tickets travel as each socket's first message,
and the routes take no path or query parameter at all. A socket that sends no
ticket within `FIRST_MESSAGE_SECONDS` is closed.

Configuration, read once at start (never at import, so tests import freely):

* `SWARM_LIVE_KEY_SECRET` -- the Secret Manager VERSION reference of
  `swarm-live-key` (`projects/<p>/secrets/swarm-live-key/versions/<v>`), which
  terraform/infra/live.tf renders into the manifest (decision (c)). The value
  is fetched once, with the pod's Workload Identity, and held in memory only:
  never a file, never an environment variable (a key in the environment is in
  every `/proc/<pid>/environ` read and every crash dump of it).
* `PROJECT_ID`, `ARTIFACT_BUCKET` -- through `swarm_common.config.Settings`,
  as every service reads them; the bucket is the recorder's.
* `SWARM_LIVE_RECORD` -- `true` (default): record every session (Q11, yes).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import re
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .recorder import FfmpegVp8Encoder, GcsSegmentWriter, Recorder
from .rendezvous import (
    CLOSE_REFUSED,
    MAX_TICKET_CHARS,
    MIN_KEY_BYTES,
    Refused,
    Rendezvous,
    SessionRecord,
)

log = logging.getLogger(__name__)

#: Fixed for both integration lanes (decision (b)): the manifest's Service, the
#: NetworkPolicy and swarm-api's SWARM_LIVE_URL all name it.
PORT = 8080
#: A socket gets this long to present its ticket. The tunnel ticket lives 60 s
#: and the viewer's 30 s, so a socket that has not sent one by then holds a
#: ticket that is about to be useless anyway.
FIRST_MESSAGE_SECONDS = 10
#: A full raw 1440x900 RGBX update is ~5.2 MB; allow headroom, refuse more.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

#: What SWARM_LIVE_KEY_SECRET must look like: a version, never a bare secret
#: (a bare name is not an API path) and never the value itself.
SECRET_VERSION = re.compile(r"projects/[^/\s]+/secrets/[^/\s]+/versions/[^/\s]+")
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1/"


class _Socket:
    """A starlette WebSocket as rendezvous.Endpoint."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def send_text(self, text: str) -> None:
        await self._ws.send_text(text)

    async def close(self, code: int, reason: str) -> None:
        if self._ws.application_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await self._ws.close(code=code, reason=reason)


async def _first_ticket(ws: WebSocket) -> str | None:
    try:
        message = await asyncio.wait_for(ws.receive(), FIRST_MESSAGE_SECONDS)
    except asyncio.TimeoutError:
        return None
    text = message.get("text")
    return text if isinstance(text, str) else None


async def _messages(ws: WebSocket) -> AsyncIterator[tuple[bytes | None, str | None]]:
    while True:
        message = await ws.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data, text = message.get("bytes"), message.get("text")
        if data is not None and len(data) > MAX_MESSAGE_BYTES:
            await ws.close(code=1009, reason="too_big")
            return
        yield data, text


def create_app(rendezvous: Rendezvous | None = None) -> FastAPI:
    """The relay app. Tests pass a `Rendezvous` built over doubles; uvicorn
    calls this with no argument (`--factory`) and the real one is built from
    the environment."""
    state: dict[str, Any] = {"rz": rendezvous}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if state["rz"] is None:
            state["rz"] = build_rendezvous()
        try:
            yield
        finally:
            await state["rz"].shutdown()

    app = FastAPI(title="swarm-live", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def restarting() -> int:
        # Nothing to tell when the rendezvous was never built.
        return 0 if state["rz"] is None else await state["rz"].restarting()

    #: What `RelayServer` calls on SIGTERM, before uvicorn closes anything.
    app.state.restarting = restarting

    def rz() -> Rendezvous:
        if state["rz"] is None:
            state["rz"] = build_rendezvous()
        return state["rz"]

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        r = rz()
        return {"ok": True, "tunnels": r.tunnel_count(), "viewers": r.viewer_count(),
                "controlled": r.controlled_count()}

    @app.post("/internal/close")
    async def close(request: Request) -> JSONResponse:
        raw = (await request.body())[: MAX_TICKET_CHARS + 1]
        try:
            session_id, dropped = await rz().close_request(raw.decode("ascii").strip())
        except (Refused, UnicodeDecodeError) as exc:
            log.info("live relay: close refused: %s", getattr(exc, "reason", "malformed"))
            # Every refusal alike, as a ticket's is.
            return JSONResponse({"closed": False}, status_code=404)
        log.info("live relay: session=%s closed by swarm-api (%d connections)", session_id, dropped)
        return JSONResponse({"closed": True, "session_id": session_id, "dropped": dropped})

    @app.post("/internal/device")
    async def device(request: Request) -> JSONResponse:
        raw = (await request.body())[: MAX_TICKET_CHARS + 1]
        try:
            session_id = await rz().device_request(raw.decode("ascii").strip())
        except (Refused, UnicodeDecodeError) as exc:
            log.info("live relay: device request refused: %s", getattr(exc, "reason", "malformed"))
            return JSONResponse({"sent": False}, status_code=404)
        # The session only: the device is the person's choice, not a log line's.
        log.info("live relay: session=%s device request sent to the gate", session_id)
        return JSONResponse({"sent": True, "session_id": session_id})

    @app.post("/internal/control")
    async def control(request: Request) -> JSONResponse:
        raw = (await request.body())[: MAX_TICKET_CHARS + 1]
        try:
            session_id, action, sent = await rz().control_request(raw.decode("ascii").strip())
        except (Refused, UnicodeDecodeError) as exc:
            log.info("live relay: control request refused: %s", getattr(exc, "reason", "malformed"))
            return JSONResponse({"sent": False}, status_code=404)
        log.info("live relay: session=%s control %s (sent to the gate: %s)", session_id, action, sent)
        return JSONResponse({"sent": sent, "session_id": session_id, "action": action})

    @app.websocket("/v1/tunnel")
    async def tunnel(ws: WebSocket) -> None:
        await ws.accept()
        raw = await _first_ticket(ws)
        endpoint = _Socket(ws)
        try:
            if raw is None:
                raise Refused("no_ticket")
            handle = await rz().open_tunnel(raw, endpoint)
        except Refused as exc:
            log.info("live relay: tunnel refused: %s", exc.reason)
            await endpoint.close(exc.code, "refused" if exc.code == CLOSE_REFUSED else exc.reason)
            return
        try:
            async for data, _text in _messages(ws):
                if data is not None:
                    await rz().from_tunnel(handle, data)
        except WebSocketDisconnect:
            pass
        finally:
            await rz().close_tunnel(handle)

    @app.websocket("/v1/view")
    async def view(ws: WebSocket) -> None:
        await ws.accept()
        raw = await _first_ticket(ws)
        endpoint = _Socket(ws)
        try:
            if raw is None:
                raise Refused("no_ticket")
            handle = await rz().open_viewer(raw, endpoint)
        except Refused as exc:
            log.info("live relay: viewer refused: %s", exc.reason)
            await endpoint.close(exc.code, "refused" if exc.code == CLOSE_REFUSED else exc.reason)
            return
        try:
            async for data, text in _messages(ws):
                if data is not None:
                    await rz().from_viewer(handle, data)
                elif text is not None:
                    await rz().viewer_text(handle, text)
        except WebSocketDisconnect:
            pass
        finally:
            await rz().close_viewer(handle)

    return app


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _access_secret(version: str) -> bytes:
    """`versions/<v>:access` over REST, with the pod's own credentials (the
    relay's KSA is the secret's accessor, live.tf). google-auth and requests
    come with google-cloud-storage, so this adds no dependency."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    response = AuthorizedSession(credentials).get(SECRET_MANAGER + version + ":access", timeout=30)
    if response.status_code != 200:
        # The status only: an error body may quote the request.
        raise RuntimeError(f"swarm-live-key could not be read from Secret Manager (HTTP {response.status_code})")
    try:
        return base64.b64decode(response.json()["payload"]["data"])
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise RuntimeError("Secret Manager returned no swarm-live-key payload") from None


def read_key(version: str | None, access: Callable[[str], bytes] | None = None) -> bytes:
    """swarm-live-key's value, by its version reference (decision (c)). The
    bytes swarm-api MACs with are the same secret's value, so both strip it
    the same way."""
    if not version:
        raise RuntimeError("SWARM_LIVE_KEY_SECRET is not set; the relay verifies nothing without swarm-live-key")
    version = version.strip()
    if not SECRET_VERSION.fullmatch(version):
        raise RuntimeError("SWARM_LIVE_KEY_SECRET must be a Secret Manager version reference, "
                           "projects/<p>/secrets/<name>/versions/<v>")
    key = (access or _access_secret)(version).strip()
    if len(key) < MIN_KEY_BYTES:
        raise RuntimeError("swarm-live-key is shorter than 32 bytes")
    return key


def build_rendezvous() -> Rendezvous:
    from swarm_common.config import Settings

    key = read_key(os.environ.get("SWARM_LIVE_KEY_SECRET"))
    factory = None
    if _truthy(os.environ.get("SWARM_LIVE_RECORD"), True):
        from google.cloud import storage

        settings = Settings.from_env()
        writer = GcsSegmentWriter(storage.Client(project=settings.project_id).bucket(settings.artifact_bucket))
        encoder = FfmpegVp8Encoder()

        def factory(session: SessionRecord, send: Any) -> Recorder:
            return Recorder(tenant_id=session.tenant_id, task_id=session.task_id,
                            attempt_id=session.attempt_id, send=send, writer=writer, encoder=encoder)

    return Rendezvous(key, recorder_factory=factory)


def relay_server(app: FastAPI) -> Any:
    """uvicorn's Server for `app`, whose shutdown first tells every gate and
    viewer the relay is restarting (module docstring, SIGTERM).

    uvicorn's own shutdown closes each websocket with 1012 by cutting it, and
    only then runs the app's lifespan shutdown -- too late to say anything.
    Overriding `shutdown` puts `Rendezvous.restarting` ahead of it."""
    import uvicorn

    class RelayServer(uvicorn.Server):
        async def shutdown(self, *args: Any, **kwargs: Any) -> None:
            try:
                await app.state.restarting()
            except Exception as exc:  # noqa: BLE001 - uvicorn's shutdown runs regardless
                log.warning("live relay: restart notice failed: %s", type(exc).__name__)
            await super().shutdown(*args, **kwargs)

    # Plain WS inside the VPC (decision (b)): no ssl_* argument, by design.
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, ws_max_size=MAX_MESSAGE_BYTES,
                            log_config=None, access_log=False)
    return RelayServer(config)


def main() -> None:
    from swarm_common.logging_setup import configure_logging

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    # The app is built here, not by uvicorn's --factory, so the server holds
    # the same one whose rendezvous it tells on SIGTERM. Still nothing at import.
    relay_server(create_app()).run()


if __name__ == "__main__":
    main()


__all__ = ["create_app", "build_rendezvous", "main", "read_key", "relay_server"]
