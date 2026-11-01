"""GPT-Live sessions: create from the browser's offer, hold the sideband, end
(docs/design/voice-it.md §3.1, §3.3, §3.4; section 10, phase one).

OpenAI's Live API, as its reference documents it (read 2026-11-01):

* CREATE: `POST /v1/live/sessions`, JSON
  `{"session": {"model": "gpt-live-1", ...}, "transport": {"type": "webrtc",
  "sdp": <offer>}}`, answered `201` with `session.id` (`live_...`) and the SDP
  answer in `transport.sdp` (/api/reference/resources/live/methods/create,
  /api/docs/guides/voice-webrtc?api=live).
* SIDEBAND: `wss://api.openai.com/v1/live/sessions/{id}/attach` with the same
  bearer. The server's first event is `session.updated`; attaching "does not
  create a session or replay earlier events"
  (/api/reference/resources/live/sideband-websocket).
* END: `{"type": "session.close"}`, answered by `session.closed` with the final
  `usage.seconds` and `reason` (/api/docs/guides/live-conversations, "Usage and
  graceful close"). The Live API documents no HTTP hangup, so the sideband is
  the only way the server can end a session, which is why it is held for the
  whole session and attached again when it drops.

WHY THE SERVER ANSWERS THE OFFER (§3.1): the browser never holds a key or a
client secret, and nothing in the page can override the session's
instructions. The caller (swarm-api) passes the offer and nothing else that
shapes the session: the model and the instructions are this module's own
(the spirit of invariant 10).

ENDING IS WHAT STOPS THE BILLING. GPT-Live bills per second until the session
closes (§6), so every path that forgets a session closes it first: an end from
swarm-api, a process shutdown (`Sessions.end_all`), and a sideband that drops
(attached again on the next end). A session OpenAI closes on its own -- its
duration limit, or the browser's own `session.close` on the data channel -- is
forgotten when its `session.closed` arrives.

Phase one holds talk and captions only: no delegation is configured, so the
voice never waits on a planner that does not exist yet (phase two adds
`delegation: {"type": "client"}` with the planner). Transcript events are passed
to `on_event` and not stored here: storing them waits for the redaction step of
§3.9, which is phase two's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from .credential import Credential, CredentialError

log = logging.getLogger("swarm_voice.live")
#: The websockets library logs every handshake header at DEBUG, the bearer among
#: them. Its connections log here instead, and this logger never goes below INFO.
_ws_log = logging.getLogger("swarm_voice.live.socket")
_ws_log.setLevel(logging.INFO)

OPENAI_API = "https://api.openai.com"
OPENAI_WS = "wss://api.openai.com"
MODEL = "gpt-live-1"
#: How long an attach may take to see its `session.updated`.
ATTACH_SECONDS = 10.0
#: Attempts at the sideband before a start gives up, or an end of a dropped one.
ATTACH_TRIES = 2
#: Largest SDP offer accepted (a browser's is a few KiB).
MAX_OFFER_BYTES = 64 * 1024

#: The voice's standing instructions for phase one (§3.3: fixed for the session's
#: life; §3.9: credentials are never spoken).
INSTRUCTIONS = (
    "You are the voice of Voice it, a planning partner for the person you are talking to. "
    "Speak briefly and plainly, and let them interrupt you. "
    "Never ask for, repeat or read out a password, key, token or any other credential. "
    "If the person starts to read one aloud, stop them at once and tell them to use the "
    "console's secret form instead, because anything said aloud is heard and transcribed."
)


class LiveError(RuntimeError):
    """OpenAI refused or failed a Live call. The message names a status, never a value."""


class Credentials(Protocol):
    async def for_person(self, ref: str, *, person: str) -> Credential: ...


EventSink = Callable[[str, dict], Awaitable[None]]


class LiveApi:
    """The two Live calls swarm-voice makes, over one shared HTTP client."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str = OPENAI_API, ws_base: str = OPENAI_WS) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.ws_base = ws_base.rstrip("/")

    async def create(self, credential: Credential, offer: str, session: dict) -> tuple[str, str]:
        """(live session id, SDP answer). A refused bearer is refreshed and tried once more."""
        for attempt in range(2):
            bearer = await credential.bearer()
            try:
                response = await self.http.post(
                    f"{self.base_url}/v1/live/sessions",
                    json={"session": session, "transport": {"type": "webrtc", "sdp": offer}},
                    headers={"Authorization": f"Bearer {bearer.reveal()}"},
                    timeout=30,
                )
            except httpx.HTTPError as exc:
                raise LiveError(f"OpenAI's Live session create failed ({type(exc).__name__})") from None
            if response.status_code == 401 and attempt == 0:
                await credential.refused()
                continue
            if response.status_code not in (200, 201):
                # The status only: OpenAI's error body may quote the request.
                raise LiveError(f"OpenAI refused the Live session (HTTP {response.status_code})")
            try:
                body = response.json()
                live_id = body["session"]["id"]
                answer = body["transport"]["sdp"]
            except (ValueError, KeyError, TypeError):
                raise LiveError("OpenAI's Live session create returned no session id or answer") from None
            if not isinstance(live_id, str) or not live_id or not isinstance(answer, str) or not answer:
                raise LiveError("OpenAI's Live session create returned no session id or answer")
            return live_id, answer
        raise LiveError("OpenAI refused the Live session (HTTP 401)")

    async def attach(self, credential: Credential, live_id: str) -> Any:
        """The sideband socket, once its first `session.updated` has arrived."""
        bearer = await credential.bearer()
        try:
            socket = await ws_connect(
                f"{self.ws_base}/v1/live/sessions/{live_id}/attach",
                additional_headers={"Authorization": f"Bearer {bearer.reveal()}"},
                open_timeout=ATTACH_SECONDS,
                logger=_ws_log,
                max_size=4 * 1024 * 1024,
            )
        except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
            raise LiveError(f"the Live sideband could not attach ({type(exc).__name__})") from None
        try:
            first = json.loads(await asyncio.wait_for(socket.recv(), ATTACH_SECONDS))
        except (WebSocketException, asyncio.TimeoutError, ValueError, TypeError) as exc:
            await socket.close()
            raise LiveError(f"the Live sideband sent no session.updated ({type(exc).__name__})") from None
        if not isinstance(first, dict) or first.get("type") != "session.updated":
            await socket.close()
            raise LiveError("the Live sideband's first event was not session.updated")
        return socket


class LiveSession:
    """One GPT-Live session and the sideband held for it."""

    def __init__(self, voice_id: str, live_id: str, credential: Credential, api: LiveApi,
                 *, on_event: EventSink | None, on_closed: Callable[["LiveSession"], None]) -> None:
        self.voice_id = voice_id
        self.live_id = live_id
        self._credential = credential
        self._api = api
        self._on_event = on_event
        self._on_closed = on_closed
        self._socket: Any = None
        self._reader: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._lock = asyncio.Lock()
        self.answer = ""
        self.usage_seconds: float | None = None
        self.reason: str | None = None

    @property
    def attached(self) -> bool:
        return self._socket is not None and self._reader is not None and not self._reader.done()

    @property
    def socket_closed(self) -> bool:
        return self._socket is None or self._socket.state.name == "CLOSED"

    @property
    def ended(self) -> bool:
        return self._closed.is_set()

    async def attach(self) -> None:
        last: LiveError | None = None
        for _ in range(ATTACH_TRIES):
            try:
                self._socket = await self._api.attach(self._credential, self.live_id)
            except (LiveError, CredentialError) as exc:
                last = exc if isinstance(exc, LiveError) else LiveError(str(exc))
                continue
            self._reader = asyncio.create_task(self._read(self._socket), name=f"sideband-{self.voice_id}")
            return
        assert last is not None
        raise last

    async def _read(self, socket: Any) -> None:
        try:
            async for raw in socket:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type", ""))
                if kind == "session.closed":
                    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                    seconds = usage.get("seconds")
                    self.usage_seconds = seconds if isinstance(seconds, (int, float)) else None
                    self.reason = str(event.get("reason") or "closed")
                    self._closed.set()
                    break
                if kind == "error":
                    # The error's code only: its message may quote what was sent.
                    error = event.get("error") if isinstance(event.get("error"), dict) else {}
                    log.warning("voice session %s: sideband error %s", self.voice_id, error.get("code", "unknown"))
                if self._on_event is not None:
                    try:
                        await self._on_event(self.voice_id, event)
                    except Exception as exc:  # noqa: BLE001 - a sink's failure never drops the sideband
                        log.warning("voice session %s: event sink failed: %s", self.voice_id, type(exc).__name__)
        except WebSocketException as exc:
            log.info("voice session %s: sideband dropped (%s)", self.voice_id, type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                await socket.close()
            if self._closed.is_set():
                self._on_closed(self)
            else:
                log.info("voice session %s: sideband detached; the next end attaches again", self.voice_id)

    async def end(self, grace: float) -> None:
        """Send `session.close`, wait up to `grace` for `session.closed`, then close the socket."""
        async with self._lock:
            if self._closed.is_set():
                return
            if not self.attached:
                await self.attach()
            try:
                await self._socket.send(json.dumps({"type": "session.close"}))
            except WebSocketException:
                # Dropped between the check and the send: attach once more and resend.
                await self.attach()
                await self._socket.send(json.dumps({"type": "session.close"}))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._closed.wait(), grace)
            if not self._closed.is_set():
                log.warning("voice session %s: no session.closed within %.0fs; closing the sideband",
                            self.voice_id, grace)
                self.reason = "close_unconfirmed"
                self._closed.set()
            if self._socket is not None:
                await self._socket.close()
            if self._reader is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(self._reader, grace)


class SessionExists(Exception):
    """The voice session already has a live GPT-Live session."""


class Sessions:
    """Every live session this process holds, by swarm-api's voice session id.

    One process (a one-replica StatefulSet, §3.4) so one voice session's GPT-Live
    session is held exactly once."""

    def __init__(self, api: LiveApi, *, credentials: Credentials, close_grace: float = 5.0,
                 on_event: EventSink | None = None) -> None:
        self.api = api
        self._credentials = credentials
        self._grace = close_grace
        self._on_event = on_event
        self._live: dict[str, LiveSession] = {}
        self._starting: set[str] = set()

    def get(self, voice_id: str) -> LiveSession | None:
        return self._live.get(voice_id)

    def counts(self) -> dict[str, int]:
        return {"sessions": len(self._live), "attached": sum(1 for s in self._live.values() if s.attached)}

    def _forget(self, session: LiveSession) -> None:
        if self._live.get(session.voice_id) is session:
            del self._live[session.voice_id]

    async def start(self, voice_id: str, *, person: str, credential_ref: str, offer: str) -> LiveSession:
        """Create the GPT-Live session for `offer`, attach its sideband, and hold it.
        Raises SessionExists, CredentialRefused, CredentialError or LiveError."""
        if voice_id in self._live or voice_id in self._starting:
            raise SessionExists(voice_id)
        self._starting.add(voice_id)
        try:
            credential = await self._credentials.for_person(credential_ref, person=person)
            live_id, answer = await self.api.create(credential, offer, {"model": MODEL, "instructions": INSTRUCTIONS})
            session = LiveSession(voice_id, live_id, credential, self.api,
                                  on_event=self._on_event, on_closed=self._forget)
            session.answer = answer
            # Attached BEFORE the answer goes back: a session this process cannot end
            # must never be handed to a browser to start talking (and billing) on.
            await session.attach()
            self._live[voice_id] = session
            log.info("voice session %s: live session %s attached", voice_id, live_id)
            return session
        finally:
            self._starting.discard(voice_id)

    async def end(self, voice_id: str) -> LiveSession | None:
        session = self._live.get(voice_id)
        if session is None:
            return None
        # Forgotten only once it has ended: an end that raised (the re-attach failed)
        # leaves the session held, so swarm-api's retry can still close it and stop
        # its per-second billing.
        await session.end(self._grace)
        if session.ended:
            self._forget(session)
        log.info("voice session %s: ended (%s)", voice_id, session.reason)
        return session

    async def end_all(self) -> None:
        """Close every session: a shutdown must not leave one billing with no sideband."""
        for voice_id in list(self._live):
            try:
                await self.end(voice_id)
            except (LiveError, CredentialError) as exc:
                log.warning("voice session %s: could not end at shutdown: %s", voice_id, exc)
