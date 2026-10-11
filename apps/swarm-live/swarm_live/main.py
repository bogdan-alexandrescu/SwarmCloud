"""swarm-live: the live-browser relay's HTTP surface (LB-L7).

docs/design/live-browser.md §3.3. A single-replica StatefulSet in
`swarm-system` on the GKE cluster -- one process, so a session's two ends always
meet in it -- listening on TCP 8443 with TLS (the NetworkPolicy of §8.3 admits
only tenant pods labelled `swarm-live: "enabled"` and swarm-api's subnet).

Two websocket routes, and a health check:

* `/v1/tunnel` -- the gate dials out to here (§3.3 step 2). First message: the
  tunnel ticket, as text. Then the framed tunnel protocol of rendezvous.py.
* `/v1/view`   -- swarm-api proxies one console viewer to here (§3.3 step 3).
  First message: the viewer ticket swarm-api minted, as text. Then the viewer's
  RFB bytes as binary, and swarm-api's control messages as text.
* `/healthz`   -- counts only.

NOTHING SECRET IN A URL (§5.2): tickets travel as each socket's first message,
and the routes take no path or query parameter at all. A socket that sends no
ticket within `FIRST_MESSAGE_SECONDS` is closed.

Configuration, read once at start (never at import, so tests import freely):

* `SWARM_LIVE_KEY_FILE` -- path of the mounted `swarm-live-key` secret, whose
  value `scripts/create-secrets.sh` creates. A file, not an environment
  variable: a key in the environment is in every `/proc/<pid>/environ` read
  and every crash dump of it.
* `PROJECT_ID`, `FIRESTORE_DATABASE`, `ARTIFACT_BUCKET` -- through
  `swarm_common.config.Settings`, as every service reads them.
* `SWARM_LIVE_RECORD` -- `true` (default): record every session (Q11, yes).
* `SWARM_LIVE_TLS_CERT`, `SWARM_LIVE_TLS_KEY` -- the serving certificate.
  `main()` refuses to start without them: TLS on every hop is what §4.2 and
  §5.11 rely on for keys in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .recorder import FfmpegVp8Encoder, GcsSegmentWriter, Recorder
from .rendezvous import (
    CLOSE_REFUSED,
    FirestoreSessionStore,
    Refused,
    Rendezvous,
    SessionRecord,
)

log = logging.getLogger(__name__)

PORT = 8443
#: A socket gets this long to present its ticket. The tunnel ticket lives 60 s
#: and the viewer's 30 s, so a socket that has not sent one by then holds a
#: ticket that is about to be useless anyway.
FIRST_MESSAGE_SECONDS = 10
#: How often every open session's document is re-read (§8.5 invariant 5: a
#: stale generation's session is closed). swarm-api moves the document when the
#: lease's generation moves; this bounds how long the relay keeps splicing after.
SWEEP_SECONDS = 15
#: A full raw 1440x900 RGBX update is ~5.2 MB; allow headroom, refuse more.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


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


def create_app(rendezvous: Rendezvous | None = None, *, sweep_seconds: float = SWEEP_SECONDS) -> FastAPI:
    """The relay app. Tests pass a `Rendezvous` built over doubles; uvicorn
    calls this with no argument (`--factory`) and the real one is built from
    the environment."""
    state: dict[str, Any] = {"rz": rendezvous}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if state["rz"] is None:
            state["rz"] = build_rendezvous()
        sweeper = asyncio.create_task(_sweep(state["rz"], sweep_seconds)) if sweep_seconds > 0 else None
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                await asyncio.gather(sweeper, return_exceptions=True)
            await state["rz"].shutdown()

    app = FastAPI(title="swarm-live", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def rz() -> Rendezvous:
        if state["rz"] is None:
            state["rz"] = build_rendezvous()
        return state["rz"]

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        r = rz()
        return {"ok": True, "tunnels": r.tunnel_count(), "viewers": r.viewer_count()}

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


async def _sweep(rendezvous: Rendezvous, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            await rendezvous.sweep()
        except Exception as exc:  # noqa: BLE001 - the sweeper must outlive one bad pass
            log.warning("live relay: sweep failed: %s", type(exc).__name__)


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def read_key(path: str | None) -> bytes:
    if not path:
        raise RuntimeError("SWARM_LIVE_KEY_FILE is not set; the relay verifies nothing without swarm-live-key")
    key = Path(path).read_bytes().strip()
    if len(key) < 32:
        raise RuntimeError("swarm-live-key is shorter than 32 bytes")
    return key


def build_rendezvous() -> Rendezvous:
    from google.cloud import firestore, storage
    from swarm_common.config import Settings

    settings = Settings.from_env()
    key = read_key(os.environ.get("SWARM_LIVE_KEY_FILE"))
    store = FirestoreSessionStore(
        firestore.Client(project=settings.project_id, database=settings.firestore_database))
    factory = None
    if _truthy(os.environ.get("SWARM_LIVE_RECORD"), True):
        writer = GcsSegmentWriter(storage.Client(project=settings.project_id).bucket(settings.artifact_bucket))
        encoder = FfmpegVp8Encoder()

        def factory(session: SessionRecord, send: Any) -> Recorder:
            return Recorder(tenant_id=session.tenant_id, task_id=session.task_id,
                            attempt_id=session.attempt_id, send=send, writer=writer, encoder=encoder)

    return Rendezvous(key, store, recorder_factory=factory)


def main() -> None:
    import uvicorn
    from swarm_common.logging_setup import configure_logging

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    cert, key = os.environ.get("SWARM_LIVE_TLS_CERT"), os.environ.get("SWARM_LIVE_TLS_KEY")
    if not cert or not key:
        raise SystemExit("swarm-live: SWARM_LIVE_TLS_CERT and SWARM_LIVE_TLS_KEY are required; "
                         "keys travel through this relay and every hop is TLS (§4.2)")
    uvicorn.run("swarm_live.main:create_app", factory=True, host="0.0.0.0", port=PORT,
                ssl_certfile=cert, ssl_keyfile=key, ws_max_size=MAX_MESSAGE_BYTES,
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()


__all__ = ["create_app", "build_rendezvous", "main"]
