"""swarm-live: the live-browser relay's HTTP surface (LB-L7, wired by LB-L8d).

docs/design/live-browser.md §3.3. A single-replica StatefulSet in
`swarm-system` on the GKE cluster -- one process, so a session's two ends always
meet in it -- listening on PLAIN WS on port 8080 inside the VPC (owner decision
(b), 2026-10-11). Browsers never reach it: they open wss to swarm-api, behind
IAP and HTTPS, and swarm-api proxies the socket here over the internal load
balancer. The NetworkPolicy of §8.3 admits only swarm-api's egress range and
tenant pods labelled `swarm-live: "enabled"`. There is no certificate anywhere
on this hop, by decision: the encrypted legs are the browser's to swarm-api and
Google's own VPC transport.

Three websocket and HTTP routes, and a health check:

* `/v1/tunnel`        -- the gate dials out to here (§3.3 step 2). First
  message: the tunnel ticket, as text. Then the framed tunnel protocol of
  rendezvous.py.
* `/v1/view`          -- swarm-api proxies one console viewer to here (§3.3
  step 3). First message: the viewer ticket swarm-api minted, as text. Then the
  viewer's RFB bytes as binary, and swarm-api's control messages as text.
* `POST /internal/close` -- swarm-api closed a session, or saw its generation
  go stale: the body is `{"session_id", "cause", "exp", "nonce"}` and the
  `X-Swarm-Live-Signature` header its MAC under swarm-live-key. 204 when done;
  every refusal is the same 403.
* `/healthz`          -- counts only.

NOTHING SECRET IN A URL (§5.2): tickets travel as each socket's first message,
and the routes take no path or query parameter at all. A socket that sends no
ticket within `FIRST_MESSAGE_SECONDS` is closed.

NO FIRESTORE (owner decision (a)). The relay trusts the ticket and nothing
else; it holds no Firestore client and no Firestore grant.

Configuration, read once at start (never at import, so tests import freely):

* `SWARM_LIVE_KEY_SECRET` -- the Secret Manager VERSION reference of
  `swarm-live-key` (`projects/<p>/secrets/swarm-live-key/versions/<v>`), read
  with the pod's own Workload Identity (decision (c), terraform/infra/live.tf).
  A reference, not the key: a key in the environment is in every
  `/proc/<pid>/environ` read and every crash dump of it.
* `SWARM_ARTIFACT_BUCKET` (or `ARTIFACT_BUCKET`) and `GOOGLE_CLOUD_PROJECT`
  (or `PROJECT_ID`) -- where the recorder writes (§7), as the manifest sets them.
* `SWARM_LIVE_RECORD` -- `true` (default): record every session (Q11, yes).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Mapping

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .recorder import FfmpegVp8Encoder, GcsSegmentWriter, Recorder
from .rendezvous import (
    CLOSE_REFUSED,
    CLOSE_SIGNATURE_HEADER,
    MAX_CLOSE_BODY,
    MIN_KEY_BYTES,
    Refused,
    Rendezvous,
    SessionRecord,
)

log = logging.getLogger(__name__)

#: Fixed for both integration lanes (owner decision (b)): the Service, the
#: NetworkPolicy, the probes and swarm-api's SWARM_LIVE_URL all name 8080.
PORT = 8080
#: A socket gets this long to present its ticket. A ticket lives 60 s at most,
#: so a socket that has not sent one by then holds one about to be useless.
FIRST_MESSAGE_SECONDS = 10
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

    def rz() -> Rendezvous:
        if state["rz"] is None:
            state["rz"] = build_rendezvous()
        return state["rz"]

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        r = rz()
        return {"ok": True, "tunnels": r.tunnel_count(), "viewers": r.viewer_count()}

    @app.post("/internal/close")
    async def internal_close(request: Request) -> Response:
        """swarm-api: drop every connection of one session (decision (a))."""
        body = await request.body()
        if len(body) > MAX_CLOSE_BODY:
            return Response(status_code=403)
        try:
            await rz().close_request(body, request.headers.get(CLOSE_SIGNATURE_HEADER))
        except Refused as exc:
            log.info("live relay: close refused: %s", exc.reason)
            return Response(status_code=403)
        return Response(status_code=204)

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


#: A Secret Manager secret VERSION reference, the only shape SWARM_LIVE_KEY_SECRET takes.
_VERSION_REF = re.compile(r"projects/[^/]+/secrets/[^/]+/versions/[^/]+")


def _access_secret(name: str) -> bytes:
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient().access_secret_version(name=name).payload.data


def read_key(ref: str | None, access: Callable[[str], bytes] | None = None) -> bytes:
    """swarm-live-key's value, from Secret Manager by its version reference."""
    if not ref:
        raise RuntimeError("SWARM_LIVE_KEY_SECRET is not set; the relay verifies nothing without swarm-live-key")
    if not _VERSION_REF.fullmatch(ref):
        raise RuntimeError("SWARM_LIVE_KEY_SECRET is not a secret version reference "
                           "(projects/<p>/secrets/<s>/versions/<v>)")
    key = (access or _access_secret)(ref).strip()
    if len(key) < MIN_KEY_BYTES:
        raise RuntimeError(f"swarm-live-key is shorter than {MIN_KEY_BYTES} bytes")
    return key


def build_rendezvous(env: Mapping[str, str] | None = None,
                     access: Callable[[str], bytes] | None = None) -> Rendezvous:
    env = os.environ if env is None else env
    key = read_key(env.get("SWARM_LIVE_KEY_SECRET"), access)
    factory = None
    if _truthy(env.get("SWARM_LIVE_RECORD"), True):
        from google.cloud import storage

        project = env.get("GOOGLE_CLOUD_PROJECT") or env.get("PROJECT_ID") or None
        bucket = env.get("SWARM_ARTIFACT_BUCKET") or env.get("ARTIFACT_BUCKET")
        if not bucket:
            raise RuntimeError("SWARM_ARTIFACT_BUCKET is not set and recording is on (SWARM_LIVE_RECORD)")
        writer = GcsSegmentWriter(storage.Client(project=project).bucket(bucket))
        encoder = FfmpegVp8Encoder()

        def factory(session: SessionRecord, send: Any) -> Recorder:
            return Recorder(tenant_id=session.tenant_id, task_id=session.task_id,
                            attempt_id=session.attempt_id, send=send, writer=writer, encoder=encoder)

    return Rendezvous(key, recorder_factory=factory)


def main() -> None:
    import uvicorn
    from swarm_common.logging_setup import configure_logging

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    # Plain ws, no ssl_* arguments: decision (b). The relay is reached only
    # through the internal load balancer, by swarm-api and the tenant gates.
    uvicorn.run("swarm_live.main:create_app", factory=True, host="0.0.0.0", port=PORT,
                ws_max_size=MAX_MESSAGE_BYTES, log_config=None, access_log=False)


if __name__ == "__main__":
    main()


__all__ = ["create_app", "build_rendezvous", "main"]
