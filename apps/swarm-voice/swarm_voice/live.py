"""GPT-Live sessions: create from the browser's offer, hold the sideband, end
(docs/design/voice-it.md §3.1, §3.3, §3.4; section 10, phase one).

OpenAI's Live API, as its reference documents it (read 2026-11-01):

* CREATE: `POST /v1/live/sessions`, JSON
  `{"session": {"model": "gpt-live-1", ...}, "transport": {"type": "webrtc",
  "sdp": <offer>}}`, answered `201` with `session.id` (`live_...`) and the SDP
  answer in `transport.sdp` (/api/reference/resources/live/methods/create,
  /api/docs/guides/voice-webrtc?api=live).
* SIDEBAND: `wss://api.openai.com/v1/live/sessions/{id}/attach` with the same
  bearer. The reference says the server's first event is `session.updated`;
  attaching "does not create a session or replay earlier events"
  (/api/reference/resources/live/sideband-websocket).

WHAT THE WIRE SENT (live QA, 9 November 2026): on a funded developer API key the
create answered 201 and the sideband's first event was NOT `session.updated`, so
an attach that read exactly one event refused a good session (the console saw a
502). The log did not record what the event was. So an attach no longer trusts
the first event: within ATTACH_SECONDS it waits for the session to be confirmed.
Any other event -- rate limits, conversation or input-buffer events -- is held,
not refused, and handed to the session's reader once it starts, so a re-attach
loses no transcript final. An `error` event is still refused at once, with its
code and type. Each attach logs the TYPES of the events it skipped, once and
never their content (which can carry transcript text), and a refusal after the
bound names the types it saw.

AND AGAIN (live check, 10 November 2026): with that fix the sideband of a
created session sent only `session.usage.updated` within the bound, which the
then-closed list (`session.updated`, `session.created`, `session.closed`) did not
accept, so a good session was refused. A socket opened on
`/v1/live/sessions/{id}/attach` carries only the events of that existing
session, so ANY `session.*` server event (CONFIRMS_PREFIX) proves the attach:
`session.closed` still reads as a session that has already ended (a re-attach),
and `error` is still refused at once. The same check showed the second defect
below (THE START HAS ONE DEADLINE).

THE START HAS ONE DEADLINE (10 November 2026). swarm-api waits
START_TIMEOUT_SECONDS (20.0, swarm_api.voice.sessions) for a start's answer and
reads a later one as "swarm-voice could not be reached". Two attach tries of
ATTACH_SECONDS each, after the lease and the create, took longer than that, so
the person saw "could not be reached" in place of the refusal this process had.
So a start -- lease, create, every attach try -- runs inside START_SECONDS, and a
failed start waits at most RELEASE_WAIT_SECONDS for its lease's release before it
answers: START_WORST_SECONDS is held below swarm-api's timeout with a margin by a
unit test (tests/unit/voice/test_live_sideband_events.py).
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

THE ACCOUNT IS LEASED (owner, 2026-11-02: the Voice it pool). Each session
leases a ChatGPT plan account from the quota broker's Voice it pool before it
creates anything (accountlease.py); every Live call of the session -- create,
sideband, re-attach -- presents that lease's token, and the lease is released
when the session is forgotten or its start fails. No account, no session.

ENDING IS WHAT STOPS THE BILLING. GPT-Live bills per second until the session
closes (§6), so every path that forgets a session closes it first: an end from
swarm-api, a process shutdown (`Sessions.end_all`), and a sideband that drops
(attached again on the next end). A session OpenAI closes on its own -- its
duration limit, or the browser's own `session.close` on the data channel -- is
forgotten when its `session.closed` arrives.

DELEGATION (phase two, §3.3). With a planner (planner.py), each session is
created with `delegation: {"type": "client"}` and its voice is told to delegate.
On the sideband, `session.delegation.created` names a delegation id and no task
text ("metadata, not task text"), so this module keeps a window of the final
transcript segments of both sides and hands it to `Planner.delegate` with the
session's person and grant; the planner's at most two sentences go back as
`session.commentary.append` for that delegation id (at most 500 tokens an
append). Without a planner nothing changes: no delegation is configured, so
the voice never waits on one. Transcript events are still passed to `on_event`
and not stored here: storing them waits for the redaction step of §3.9.

THE PLANNER HEARD NOTHING (live use, 10 November 2026). A person asked the voice
to help build a website; every planner turn drew nothing and read only the
inbox, and the voice said nothing had come back. The window was filled from
five exact event names only, so finals sent under any other name left it empty
and the planner was handed no request. So a final is recognised by its family
(`final_speaker`: a type ending in `transcript.done`, `transcription.completed`
or `transcript.completed`; `input`/`user` is the person, `output`/`response`
the voice), any text the delegation event carries of its own
(`delegation_request`) goes to the planner as the delegation's request, and a
turn with neither does not run: the voice asks the person to say it again
(SAY_ASK_AGAIN). A turn that starts with an empty transcript logs the counts
of the sideband event TYPES the session has seen -- types only, never content
-- so the next live run shows what arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Protocol

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .credential import Credential, CredentialError
from . import readback as vreadback
from .grant import SessionGrant

if TYPE_CHECKING:
    from .planner import Planner

log = logging.getLogger("swarm_voice.live")
#: The websockets library logs every handshake header at DEBUG, the bearer among
#: them. Its connections log here instead, and this logger never goes below INFO.
_ws_log = logging.getLogger("swarm_voice.live.socket")
_ws_log.setLevel(logging.INFO)

OPENAI_API = "https://api.openai.com"
OPENAI_WS = "wss://api.openai.com"
MODEL = "gpt-live-1"
#: How long one attach try may take to see the session confirmed (CONFIRMS_PREFIX);
#: inside a start, also no longer than what is left of START_SECONDS.
ATTACH_SECONDS = 10.0
#: Sideband events that confirm an attach: any `session.*` server event, since a
#: socket on /v1/live/sessions/{id}/attach carries only that session's events
#: (the reference names `session.updated`; 10 November 2026 the wire sent only
#: `session.usage.updated`, see above). `session.closed` among them answers a
#: re-attach to a session that has already ended; `error` is refused, not skipped.
CONFIRMS_PREFIX = "session."
#: Events held for the reader while an attach waits; later ones are skipped unheld.
ATTACH_HELD = 64
#: Event types named in an attach's log line or refusal.
ATTACH_TYPES_NAMED = 20
#: What an event type must look like to be logged: OpenAI's are dotted words.
_EVENT_TYPE = re.compile(r"[a-z0-9_.]{1,64}")
#: Attempts at the sideband before a start gives up, or an end of a dropped one.
#: Inside a start the tries share START_SECONDS: a try is not begun with less
#: than ATTACH_MIN_SECONDS left.
ATTACH_TRIES = 2
ATTACH_MIN_SECONDS = 1.0
#: The attach tries of a start end this long before its deadline, so their own
#: refusal (naming the events seen) is what answers, not the bare deadline.
ATTACH_RESERVE_SECONDS = 0.5
#: The whole of a Live start -- the lease, the create, every attach try. It MUST
#: stay below swarm-api's START_TIMEOUT_SECONDS (20.0, swarm_api.voice.sessions)
#: with a margin for the hop and the answer: past that, swarm-api reads a
#: refusal this process would have given as "swarm-voice could not be reached"
#: (10 November 2026). START_WORST_SECONDS is what a start can take at most.
START_SECONDS = 15.0
#: How long a failed start waits for its lease's release before it answers; the
#: release goes on in the background past it (drained at shutdown).
RELEASE_WAIT_SECONDS = 2.0
START_WORST_SECONDS = START_SECONDS + RELEASE_WAIT_SECONDS
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

#: Added to the instructions when a planner is configured (§3.3 step 1).
DELEGATE_INSTRUCTIONS = (
    " When the person asks for something to be planned, drawn, looked up or explained from "
    "SwarmCloud, delegate it, say a few words while you wait, then speak the result you are "
    "given and point at the card on screen. Never invent a plan or a status yourself."
)

#: The sideband event that starts a planner turn, and the one its result goes back as.
DELEGATION_CREATED = "session.delegation.created"
COMMENTARY_APPEND = "session.commentary.append"
#: Final transcript segments, by side: what the planner reads (captions.ts names the same).
TRANSCRIPT_FINAL = {
    "session.input_transcript.done": "person",
    "conversation.item.input_audio_transcription.completed": "person",
    "session.output_transcript.done": "voice",
    "response.output_audio_transcript.done": "voice",
    "response.audio_transcript.done": "voice",
}
#: The families a final of another name is recognised by (`final_speaker`), and
#: the words of its type that say whose it is.
TRANSCRIPT_FINAL_SUFFIXES = ("transcript.done", "transcription.completed", "transcript.completed")
PERSON_WORDS = frozenset({"input", "user"})
VOICE_WORDS = frozenset({"output", "response"})
#: Fields of a delegation event (or of its `delegation`) that may carry the
#: delegation's own text; the reference says it carries none, so any is a bonus.
DELEGATION_TEXT_FIELDS = ("arguments", "instructions", "description", "input")
#: How deep `delegation_request` reads nested arguments, and the most it keeps.
DELEGATION_TEXT_DEPTH = 4
DELEGATION_TEXT_MAX = 4000
#: What the voice says for a turn with no transcript and no delegation text,
#: in place of a planner turn that would read the inbox and report "nothing".
SAY_ASK_AGAIN = "I didn't catch what you asked for. Could you say it again?"
#: Distinct sideband event types counted per session; later ones count as "(other)".
EVENT_TYPES_COUNTED = 64
#: Segments of transcript kept for the planner.
TRANSCRIPT_LINES = 200


class LiveError(RuntimeError):
    """OpenAI refused or failed a Live call. The message names a status, never a value."""


#: What of OpenAI's error is kept: `error.code` and `error.type`, each only when
#: it matches this closed pattern. Never `error.message` or any other field: the
#: message may quote the request, the bearer included (measured 2026-11-04: a
#: 403 on a Voice it pool account nobody could explain, because only the status
#: was kept).
OPENAI_ERROR_TOKEN = re.compile(r"[a-z0-9_.-]{1,64}")
#: Read in place of a code or type that is absent, not a string, or off the pattern.
UNREADABLE = "unreadable"
#: Largest error body parsed; anything longer reads as unreadable.
MAX_ERROR_BYTES = 16 * 1024


def openai_error(raw: bytes | bytearray | str | None) -> tuple[str, str]:
    """(code, type) of OpenAI's `{"error": {"code", "type", ...}}`, each held to
    OPENAI_ERROR_TOKEN or read as UNREADABLE. Never raises."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MAX_ERROR_BYTES:
                return UNREADABLE, UNREADABLE
            raw = bytes(raw).decode("utf-8")
        if not isinstance(raw, str) or len(raw) > MAX_ERROR_BYTES:
            return UNREADABLE, UNREADABLE
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return UNREADABLE, UNREADABLE
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return UNREADABLE, UNREADABLE

    def kept(value: Any) -> str:
        if isinstance(value, str) and OPENAI_ERROR_TOKEN.fullmatch(value):
            return value
        return UNREADABLE

    return kept(error.get("code")), kept(error.get("type"))


class LiveRefused(LiveError):
    """OpenAI answered a Live call with a refusal. Carries the HTTP status (None
    for an `error` event on the sideband before the session is confirmed) and OpenAI's error code and
    type, already held to OPENAI_ERROR_TOKEN; never the error's message."""

    def __init__(self, what: str, status: int | None, code: str, kind: str) -> None:
        self.status = status
        self.code = code
        self.type = kind
        where = f"HTTP {status}, " if status is not None else ""
        super().__init__(f"OpenAI refused {what} ({where}code {code}, type {kind})")


def _refused(what: str, status: int | None, raw: bytes | bytearray | str | None) -> LiveRefused:
    code, kind = openai_error(raw)
    log.warning("OpenAI refused %s: status %s, code %s, type %s", what, status, code, kind)
    return LiveRefused(what, status, code, kind)


class LeasedCredential(Credential, Protocol):
    """A credential held for one session (accountlease.PoolCredential)."""

    def start_renewing(self) -> None: ...

    async def release(self) -> None: ...


class Leases(Protocol):
    """Where a session's credential comes from: the quota broker's Voice it pool
    (accountlease.VoiceBroker). Raises accountlease.VoicePoolRefused when it leases none."""

    async def lease(self, *, person: str, voice_session_id: str) -> LeasedCredential: ...


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
                    headers=bearer.headers(),
                    timeout=30,
                )
            except httpx.HTTPError as exc:
                raise LiveError(f"OpenAI's Live session create failed ({type(exc).__name__})") from None
            if response.status_code == 401 and attempt == 0:
                await credential.refused()
                continue
            if response.status_code not in (200, 201):
                # The status, and the error's code and type held to a closed pattern:
                # its message may quote the request.
                raise _refused("the Live session", response.status_code, response.content)
            try:
                body = response.json()
                live_id = body["session"]["id"]
                answer = body["transport"]["sdp"]
            except (ValueError, KeyError, TypeError):
                raise LiveError("OpenAI's Live session create returned no session id or answer") from None
            if not isinstance(live_id, str) or not live_id or not isinstance(answer, str) or not answer:
                raise LiveError("OpenAI's Live session create returned no session id or answer")
            return live_id, answer
        # Unreached: the second attempt's 401 is refused above, with its code and type.
        raise LiveError("OpenAI refused the Live session (HTTP 401)")

    async def attach(self, credential: Credential, live_id: str,
                     held: list[dict] | None = None, *, seconds: float | None = None) -> Any:
        """The sideband socket, once a `session.*` event (CONFIRMS_PREFIX) has arrived.

        Other events before it are skipped (appended to `held`, when given, for
        the session's reader); an `error` is refused at once; nothing confirming
        within `seconds` (ATTACH_SECONDS when not given) is refused with the
        event types seen."""
        if seconds is None:
            seconds = ATTACH_SECONDS
        bearer = await credential.bearer()
        try:
            socket = await ws_connect(
                f"{self.ws_base}/v1/live/sessions/{live_id}/attach",
                additional_headers=bearer.headers(),
                open_timeout=seconds,
                logger=_ws_log,
                max_size=4 * 1024 * 1024,
            )
        except InvalidStatus as exc:
            raise _refused("the Live sideband", exc.response.status_code, exc.response.body) from None
        except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
            raise LiveError(f"the Live sideband could not attach ({type(exc).__name__})") from None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        skipped: list[str] = []
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), max(0.0, deadline - loop.time()))
            except (WebSocketException, asyncio.TimeoutError) as exc:
                await socket.close()
                raise LiveError(f"the Live sideband confirmed no session ({type(exc).__name__}; "
                                f"saw {_named(skipped)})") from None
            except asyncio.CancelledError:
                # A start's deadline (START_SECONDS) passed: the socket goes with it.
                await socket.close()
                raise
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                event = None
            kind = _event_type(event)
            if kind == "error":
                await socket.close()
                raise _refused("the Live sideband", None, json.dumps(event))
            if kind.startswith(CONFIRMS_PREFIX):
                if skipped:
                    log.info("the Live sideband sent %s before %s", _named(skipped), kind)
                if held is not None:
                    held.append(event)
                return socket
            skipped.append(kind)
            if held is not None and isinstance(event, dict) and len(held) < ATTACH_HELD:
                held.append(event)


def _event_type(event: Any) -> str:
    """An event's type, safe to log: a dotted word, else a placeholder."""
    if not isinstance(event, dict):
        return "(unreadable)"
    kind = event.get("type")
    if isinstance(kind, str) and _EVENT_TYPE.fullmatch(kind):
        return kind
    return "(unnamed)"


def _named(kinds: list[str]) -> str:
    """The event types an attach skipped, in order, at most ATTACH_TYPES_NAMED."""
    if not kinds:
        return "no event"
    named = ", ".join(kinds[:ATTACH_TYPES_NAMED])
    if len(kinds) > ATTACH_TYPES_NAMED:
        named += f" and {len(kinds) - ATTACH_TYPES_NAMED} more"
    return named


def final_speaker(kind: str) -> str | None:
    """Whose final transcript segment an event of type `kind` is -- "person" or
    "voice" -- or None when it is not one: the names of TRANSCRIPT_FINAL, then
    any type of a final's family (TRANSCRIPT_FINAL_SUFFIXES) whose words name
    a side, the person's first."""
    if kind in TRANSCRIPT_FINAL:
        return TRANSCRIPT_FINAL[kind]
    if not kind.endswith(TRANSCRIPT_FINAL_SUFFIXES):
        return None
    words = set(re.split(r"[._]", kind))
    if words & PERSON_WORDS:
        return "person"
    if words & VOICE_WORDS:
        return "voice"
    return None


def _final_text(event: dict) -> str:
    text = event.get("transcript")
    if not isinstance(text, str):
        text = event.get("text")
    return text.strip() if isinstance(text, str) else ""


def _texts(value: Any, out: list[str], depth: int = 0) -> None:
    """The strings in `value`, a JSON string read as its value, in order."""
    if depth > DELEGATION_TEXT_DEPTH:
        return
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                _texts(json.loads(text), out, depth + 1)
                return
            except ValueError:
                pass
        if text and text not in out:
            out.append(text)
    elif isinstance(value, dict):
        for item in value.values():
            _texts(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _texts(item, out, depth + 1)


def delegation_request(event: dict) -> str:
    """The text a delegation event carries of its own (DELEGATION_TEXT_FIELDS of
    its `delegation`, then of the event), at most DELEGATION_TEXT_MAX; "" for none."""
    delegation = event.get("delegation") if isinstance(event.get("delegation"), dict) else {}
    found: list[str] = []
    for source in (delegation, event):
        for field in DELEGATION_TEXT_FIELDS:
            _texts(source.get(field), found)
    return "\n".join(found)[:DELEGATION_TEXT_MAX]


class LiveSession:
    """One GPT-Live session and the sideband held for it."""

    def __init__(self, voice_id: str, live_id: str, credential: LeasedCredential, api: LiveApi,
                 *, on_event: EventSink | None, on_closed: Callable[["LiveSession"], None],
                 person: str = "", planner: "Planner | None" = None,
                 finals: vreadback.Finals | None = None) -> None:
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
        #: swarm-api's session grant (grant.py), in memory only; None once the
        #: session ends or when swarm-api sent none.
        self.grant: SessionGrant | None = None
        self.usage_seconds: float | None = None
        self.reason: str | None = None
        self.person = person
        self._planner = planner
        #: Posts the person's finals to swarm-api's read-back gate (readback.py).
        self._finals = finals
        #: (speaker, text) of the final transcript segments, newest last.
        self.transcript: list[tuple[str, str]] = []
        #: How many sideband events of each type arrived (`_event_type`, never content).
        self.event_types: dict[str, int] = {}
        self._turns: set[asyncio.Task] = set()
        #: The planner model read at this session's start; None keeps the pin.
        self.planner_model: str | None = None

    @property
    def attached(self) -> bool:
        return self._socket is not None and self._reader is not None and not self._reader.done()

    @property
    def socket_closed(self) -> bool:
        return self._socket is None or self._socket.state.name == "CLOSED"

    @property
    def ended(self) -> bool:
        return self._closed.is_set()

    async def attach(self, deadline: float | None = None) -> None:
        """Attach the sideband, in at most ATTACH_TRIES tries. With `deadline` (the
        event loop's clock, a start's START_SECONDS), each try gets no longer than
        what is left of it, and none starts with less than ATTACH_MIN_SECONDS."""
        loop = asyncio.get_running_loop()
        last: LiveError | None = None
        for _ in range(ATTACH_TRIES):
            seconds = ATTACH_SECONDS
            if deadline is not None:
                seconds = min(seconds, deadline - loop.time())
                if seconds < ATTACH_MIN_SECONDS:
                    break
            held: list[dict] = []
            try:
                self._socket = await self._api.attach(self._credential, self.live_id, held,
                                                      seconds=seconds)
            except (LiveError, CredentialError) as exc:
                last = exc if isinstance(exc, LiveError) else LiveError(str(exc))
                continue
            # A re-attach to a session that has already ended is answered with its
            # `session.closed`: noted now, so an end does not send a close after it.
            for event in held:
                if event.get("type") == "session.closed":
                    self._note_closed(event)
            self._reader = asyncio.create_task(self._read(self._socket, held),
                                               name=f"sideband-{self.voice_id}")
            return
        if last is None:
            raise LiveError(f"the Live sideband had no time left to attach within the start's "
                            f"{START_SECONDS:g} s")
        raise last

    async def _events(self, socket: Any, held: list[dict]) -> AsyncIterator[Any]:
        """The events the attach held, then the socket's."""
        for event in held:
            yield event
        async for raw in socket:
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue

    def _note_closed(self, event: dict) -> None:
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        seconds = usage.get("seconds")
        self.usage_seconds = seconds if isinstance(seconds, (int, float)) else None
        self.reason = str(event.get("reason") or "closed")
        self._closed.set()

    async def _read(self, socket: Any, held: list[dict] | None = None) -> None:
        try:
            async with contextlib.aclosing(self._events(socket, held or [])) as events:
                async for event in events:
                    if not isinstance(event, dict):
                        continue
                    kind = str(event.get("type", ""))
                    self._count(event)
                    if kind == "session.closed":
                        self._note_closed(event)
                        break
                    speaker = final_speaker(kind)
                    if speaker is not None:
                        self._note_transcript(speaker, event)
                        if speaker == "person":
                            self._answer(event)
                    elif kind == DELEGATION_CREATED and self._planner is not None:
                        self._delegated(event)
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

    def _count(self, event: Any) -> None:
        kind = _event_type(event)
        if kind not in self.event_types and len(self.event_types) >= EVENT_TYPES_COUNTED:
            kind = "(other)"
        self.event_types[kind] = self.event_types.get(kind, 0) + 1

    def _types_seen(self) -> str:
        """The sideband event types seen so far with their counts, most first."""
        if not self.event_types:
            return "no event"
        ranked = sorted(self.event_types.items(), key=lambda item: (-item[1], item[0]))
        return ", ".join(f"{kind} x{count}" for kind, count in ranked)

    def _note_transcript(self, speaker: str, event: dict) -> None:
        text = _final_text(event)
        if text:
            self.transcript.append((speaker, text))
            del self.transcript[:-TRANSCRIPT_LINES]

    def _answer(self, event: dict) -> None:
        """Post the person's final to swarm-api's read-back gate before the
        planner hears it (voice-it-own-speech.md §7.3): a "go" or a refusal
        answers a pending read-back there, whatever the voice does."""
        if self._finals is None or self.grant is None:
            return
        text = event.get("transcript")
        if not isinstance(text, str):
            text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        task = asyncio.create_task(self._finals.post(self.grant, text),
                                   name=f"readback-answer-{self.voice_id}")
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    def _delegated(self, event: dict) -> None:
        """Start a planner turn for a delegation; its result is spoken when it ends."""
        delegation = event.get("delegation") if isinstance(event.get("delegation"), dict) else {}
        delegation_id = delegation.get("id")
        if not isinstance(delegation_id, str) or not delegation_id:
            log.warning("voice session %s: a delegation without an id was ignored", self.voice_id)
            return
        task = asyncio.create_task(self._plan(delegation_id, delegation_request(event)),
                                   name=f"planner-{self.voice_id}")
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _plan(self, delegation_id: str, request: str = "") -> None:
        assert self._planner is not None
        transcript = list(self.transcript)
        if not transcript:
            log.warning("voice session %s: a planner turn started with an empty transcript "
                        "(delegation text: %s); sideband event types so far: %s", self.voice_id,
                        "yes" if request else "none", self._types_seen())
            if not request:
                # Nothing to plan from: a turn would read the inbox and report "nothing".
                await self._say(delegation_id, SAY_ASK_AGAIN)
                return
        result = await self._planner.delegate(person=self.person, grant=self.grant,
                                              transcript=transcript, delegation_id=delegation_id,
                                              model=self.planner_model, request=request or None)
        await self._say(delegation_id, result.spoken)

    async def _say(self, delegation_id: str, spoken: str) -> None:
        """Send `spoken` back as the delegation's commentary, unless the session ended."""
        if self.ended or self._socket is None or not spoken:
            return
        try:
            await self._socket.send(json.dumps({
                "type": COMMENTARY_APPEND, "event_id": f"planner_{delegation_id}"[:64],
                "delegation_id": delegation_id, "content": spoken}))
        except WebSocketException as exc:
            log.info("voice session %s: the planner's summary was not sent (%s)",
                     self.voice_id, type(exc).__name__)

    def cancel_turns(self) -> None:
        """Drop planner turns in flight: an ended session speaks nothing more."""
        for task in list(self._turns):
            task.cancel()

    async def end(self, grace: float) -> None:
        """Send `session.close`, wait up to `grace` for `session.closed`, then close the socket."""
        async with self._lock:
            if self._closed.is_set():
                return
            if not self.attached:
                await self.attach()
            # A re-attach can find the session already closed (its `session.closed`
            # arrived while the attach waited): there is nothing left to close.
            if not self._closed.is_set():
                try:
                    await self._socket.send(json.dumps({"type": "session.close"}))
                except WebSocketException:
                    # Dropped between the check and the send: attach once more and resend.
                    await self.attach()
                    if not self._closed.is_set():
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

    def __init__(self, api: LiveApi, *, leases: Leases, close_grace: float = 5.0,
                 on_event: EventSink | None = None, planner: "Planner | None" = None,
                 finals: vreadback.Finals | None = None) -> None:
        self.api = api
        self._planner = planner
        #: Calls swarm-api held for a read-back, by session (readback.py); the
        #: internal `/readback` route runs them from here.
        self.holds = finals.holds if finals is not None else vreadback.Holds()
        if finals is None and planner is not None:
            finals = vreadback.Finals.for_planner(planner, self.holds)
        self._finals = finals
        self._leases = leases
        self._grace = close_grace
        self._on_event = on_event
        self._live: dict[str, LiveSession] = {}
        self._starting: set[str] = set()
        self._releases: set[asyncio.Task] = set()

    def get(self, voice_id: str) -> LiveSession | None:
        return self._live.get(voice_id)

    def counts(self) -> dict[str, int]:
        return {"sessions": len(self._live), "attached": sum(1 for s in self._live.values() if s.attached)}

    def _forget(self, session: LiveSession) -> None:
        # The grant dies with the session (§3.6): an ended session presents none.
        session.grant = None
        session.cancel_turns()
        self.holds.forget(session.voice_id)
        if self._planner is not None:
            self._planner.forget(session.voice_id)
        if self._live.get(session.voice_id) is session:
            del self._live[session.voice_id]
        # The Voice it pool account goes back with the session: an end, a
        # shutdown, or OpenAI's own `session.closed` on the sideband.
        self._release(session._credential)

    def _release(self, credential: LeasedCredential) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(credential.release(),
                                                      name="voice-lease-release")
        self._releases.add(task)
        task.add_done_callback(self._releases.discard)
        return task

    async def drain_releases(self) -> None:
        """Wait for the releases in flight (a shutdown must not drop one)."""
        while self._releases:
            await asyncio.gather(*list(self._releases), return_exceptions=True)

    async def start(self, voice_id: str, *, person: str, offer: str,
                    grant: SessionGrant | None = None, planner_model: str | None = None) -> LiveSession:
        """Lease a Voice it pool account for `person`, create the GPT-Live session for
        `offer` on it, attach its sideband, and hold it. Raises SessionExists,
        accountlease.VoicePoolRefused (nothing started), CredentialError or LiveError;
        on any failure after the lease, the lease's release is begun before raising.

        The whole start runs inside START_SECONDS, and a failure waits at most
        RELEASE_WAIT_SECONDS for the release: swarm-api must have this answer
        before its START_TIMEOUT_SECONDS, or it shows the person "could not be
        reached" in place of the refusal (see THE START HAS ONE DEADLINE)."""
        if voice_id in self._live or voice_id in self._starting:
            raise SessionExists(voice_id)
        self._starting.add(voice_id)
        credential: LeasedCredential | None = None
        deadline = asyncio.get_running_loop().time() + START_SECONDS
        stage = "the lease"
        try:
            bound: asyncio.Timeout | None = None
            try:
                async with asyncio.timeout_at(deadline) as bound:
                    credential = await self._leases.lease(person=person, voice_session_id=voice_id)
                    credential.start_renewing()
                    config: dict[str, Any] = {"model": MODEL, "instructions": INSTRUCTIONS}
                    if self._planner is not None:
                        config = {"model": MODEL, "instructions": INSTRUCTIONS + DELEGATE_INSTRUCTIONS,
                                  "delegation": {"type": "client"}}
                    stage = "the create"
                    live_id, answer = await self.api.create(credential, offer, config)
                    session = LiveSession(voice_id, live_id, credential, self.api,
                                          on_event=self._on_event, on_closed=self._forget,
                                          person=person, planner=self._planner, finals=self._finals)
                    session.answer = answer
                    session.grant = grant
                    session.planner_model = planner_model
                    # Attached BEFORE the answer goes back: a session this process cannot end
                    # must never be handed to a browser to start talking (and billing) on.
                    stage = "the sideband"
                    await session.attach(deadline - ATTACH_RESERVE_SECONDS)
            except TimeoutError:
                if bound is None or not bound.expired():
                    raise
                raise LiveError(f"the Live start did not finish within {START_SECONDS:g} s "
                                f"(at {stage})") from None
            self._live[voice_id] = session
            log.info("voice session %s: live session %s attached", voice_id, live_id)
            credential = None  # held by the session now; released when it is forgotten
            return session
        except BaseException:
            if credential is not None:
                # Begun now and waited on briefly: the refusal must not wait out a
                # slow broker; a release still in flight is drained at shutdown.
                await asyncio.wait({self._release(credential)}, timeout=RELEASE_WAIT_SECONDS)
            raise
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
        await self.drain_releases()
