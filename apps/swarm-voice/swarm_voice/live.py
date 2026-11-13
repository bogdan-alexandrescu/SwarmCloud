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

THE NEXT TIER (docs/design/llm-providers.md §5.3). The session records the
tier its key serves from (`LiveSession.tier`, the broker's answer), and each
debug trace turn names it beside the engine. When the key's credit is gone
mid-session -- the sideband's `error` carries a spent code
(accountlease.SPENT_CODES), or the broker refuses the hold's renewal -- the
session leases the next tier (accountlease `VoiceBroker.move`) while it still
holds the spent key, and tells swarm-api through the event sink
(`REOPEN_EVENT`, with the new tier and nothing else). A GPT-Live session is
bound to the browser's SDP, so the reopen is the next start of the same voice
session with a fresh offer: it creates the Live session on the next tier's
key, carries the transcript, the grant, the planner model and the trace over,
closes the spent Live session and gives its key back. If OpenAI ends the spent
session first, the next tier stays held for the reopen for REOPEN_SECONDS and
is then given back. With no next tier nothing is told and the session ends
as it did before.

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

THE SIDEBAND SENDS ONLY DELTAS (dev, 10 November 2026). That log line then
showed `session.input_transcript.delta` and `session.output_transcript.delta`
and never a final, so every planner turn still started empty and no card or
mock-up was drawn. So deltas are read too (`delta_speaker`: a type ending in
`transcript.delta` or `transcription.delta`, by side as for finals; the text is
the event's `delta`, then `text`, then `transcript`). They are grouped into one
segment per item (`item_id`, then `response_id`; without either, a change of
speaker starts a new one), and the planner reads every segment, the ones still
open included, as one line per speaker turn in the order they began: a
delegation usually arrives while the person's request is still mid-segment. A
final for a segment replaces its accumulated text rather than adding a line,
and a late delta of a finished segment adds nothing. `session.commentary.appended`
is the model's own commentary and never becomes a line. A delta's text is never
logged; when one carries no text field its keys are logged once, at debug.

WHOLE UTTERANCES (the owner, 10 November 2026, after using it: "it should allow
me to speak an entire sentence before acting on it"). The person's deltas form
one utterance -- one line for the planner -- until it ends: its text ends a
sentence (a period, question mark or exclamation mark; never a comma) and
UTTERANCE_END_GAP_SECONDS pass with no new delta of theirs, or
UTTERANCE_IDLE_GAP_SECONDS pass whatever its text, or a new item starts, the
voice speaks over an utterance that names no item (one that does is still
being transcribed behind the reply), a final arrives, or the voice delegates.
Every sideband event, and a timer for when none comes, checks the gaps on an
injectable clock. Each ended utterance goes in one piece, once, to swarm-api's
read-back gate (`_answer`): this engine sends no person final, so the gate was
never posted before. And the voice is asked to wait for a whole sentence
(TURN_INSTRUCTIONS): the Live create documents no end-of-turn setting, only the
instructions, so that is where the patience goes.

NO CARD THAT IS NOT THERE (the same day: the voice said a card was on screen
when the planner drew none). The planner's result now ends by saying what it
drew, counted, or that it drew nothing (planner.drew), and DELEGATE_INSTRUCTIONS
lets the voice point at a card only when the result says one was drawn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Protocol

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .accountlease import SPENT_CODES, VoicePoolRefused
from .credential import Credential, CredentialError
from . import instructions as vinstructions
from . import readback as vreadback
from . import trace as vtrace
from .grant import GRANT_HEADER, SessionGrant
from . import updates as vupdates

if TYPE_CHECKING:
    from .planner import Planner, TurnResult

log = logging.getLogger("swarm_voice.live")
#: The websockets library logs every handshake header at DEBUG, the bearer among
#: them. Its connections log here instead, and this logger never goes below INFO.
_ws_log = logging.getLogger("swarm_voice.live.socket")
_ws_log.setLevel(logging.INFO)

OPENAI_API = "https://api.openai.com"
OPENAI_WS = "wss://api.openai.com"
MODEL = "gpt-live-1"
#: What the event sink is told when a session's next tier is held and the
#: browser should offer again (the next start of the same voice session).
REOPEN_EVENT = "voice.tier.reopen"
#: How long a next tier is held for a reopen after the spent session ended.
REOPEN_SECONDS = 60.0

#: THE PAUSE (§3.12.7; the owner's pick of 11 November 2026 was "about 15
#: seconds", moved to 45 on 12 November 2026 after three talks on the
#: development deployment in which the voice paused while the planner was still
#: plainly at work: the updates (updates.py) now speak while work runs).
#: In a GPT-Live session with a delegation in flight, no read-back pending and
#: nothing playing, this many seconds in which neither side speaks pause it:
#: GPT-Live bills every second it is open, and a voice waiting on delegated
#: work says nothing worth paying for. Shorter paused while the owner was still
#: watching the work start (15 s did); longer pays for more silence on every
#: delegation. The update queue's check-in counts the same silence.
PAUSE_AFTER_SECONDS = 45.0
#: The most the pause waits for its sentence to be spoken before it closes: the
#: sentence takes about four seconds to say, so twice that covers a slow voice
#: and still bounds a Live session whose audio events never come.
PAUSE_SAY_SECONDS = 8.0
#: The one sentence the voice says before it pauses.
PAUSE_SENTENCE = ("I'll pause while that runs; I'll be back when it's ready, or just start talking.")
#: What the event sink is told about the voice's state (§3.12.7), and the states.
SESSION_STATE = "session.state"
PAUSED, WAKING, LISTENING = "paused", "waking", "listening"
#: Why a voice session's Live session is opened again: the delegated work's
#: result is ready, the person started talking, Resume, or the tier move (§5.3).
WAKE_CAUSES = ("result", "speech", "resume")
TIER_CAUSE = "tier"
REOPEN_CAUSES = WAKE_CAUSES + (TIER_CAUSE,)
#: The sideband's audio playback events: while the voice plays, nothing pauses.
OUTPUT_AUDIO_STARTED = "session.output_audio.started"
OUTPUT_AUDIO_STOPPED = "session.output_audio.stopped"
#: The person's voice detection on the sideband, by the end of its type
#: (`input_audio_buffer.speech_started`, `session.input_audio.speech_started`):
#: between the two, an update waits for the gap (updates.py).
SPEECH_STARTED_SUFFIX = ".speech_started"
SPEECH_STOPPED_SUFFIX = ".speech_stopped"
#: The longest an update waits for the voice to end its current sentence when
#: its text never ends one (a reply cut short, a transcript that stopped).
VOICE_SENTENCE_SECONDS = 4.0
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
    " When the person asks for something to be planned, drawn, shown, looked up or explained from "
    "SwarmCloud, delegate it, say a few words while you wait, then speak the result you are "
    "given. Say a card or mock-up is on screen only when the result you are given says it drew "
    "or put one there, and point at that one; when it says nothing was drawn, never say "
    "anything is on screen. Never invent a plan or a status yourself."
)

#: Turn-taking, added to every session's instructions. The Live API offers no
#: end-of-turn setting at create: its reference lists only `model`, `audio`,
#: `client`, `delegation`, `input`, `instructions`, `store` and `triggers`
#: (/api/reference/resources/live/methods/create, read 10 November 2026), and its
#: prompting guide says turn-taking is shaped by the instructions
#: (/api/docs/guides/live-prompting: "Keep listening while the user pauses to
#: think"). So the patience is asked for here, in the guide's words: the owner
#: (10 November 2026) was answered and delegated for each word, mid-sentence.
TURN_INSTRUCTIONS = (
    " Let the person finish a whole sentence before you answer or delegate: keep listening "
    "while they pause to think or between clauses, and treat a comma or a rising voice as "
    "more to come. Answer once they have finished and fallen silent. Do not treat a cough, "
    "music or nearby conversation as a new request."
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
#: The families of transcript deltas (`delta_speaker`), by side as for finals:
#: the Live sideband sends `session.input_transcript.delta` (the person) and
#: `session.output_transcript.delta` (the voice) and no final (10 November 2026).
TRANSCRIPT_DELTA_SUFFIXES = ("transcript.delta", "transcription.delta")
#: Where a delta's text is, in order.
DELTA_TEXT_FIELDS = ("delta", "text", "transcript")
#: The model's own commentary on the sideband: never a line of the transcript.
COMMENTARY_APPENDED = "session.commentary.appended"
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
SAY_ASK_AGAIN = ("I didn't catch what you asked for, so nothing was drawn and nothing new is "
                 "on screen. Could you say it again?")
#: Distinct sideband event types counted per session; later ones count as "(other)".
EVENT_TYPES_COUNTED = 64
#: Segments of transcript kept for the planner.
TRANSCRIPT_LINES = 200
#: The most characters one segment keeps (a final's or its deltas'): an item
#: whose deltas never end cannot grow the window without bound. A minute of
#: speech is about 1,000 characters.
TRANSCRIPT_SEGMENT_MAX = 8000
#: WHOLE UTTERANCES (owner, 10 November 2026: "speak an entire sentence before
#: acting on it"). The person's deltas are one utterance until it ends: after
#: UTTERANCE_END_GAP_SECONDS with no new delta of theirs once its text ends in
#: UTTERANCE_END_MARKS, after UTTERANCE_IDLE_GAP_SECONDS with none whatever its
#: text, or at once on a new item, a change of speaker, a final or a delegation.
#: 0.8 s after a period is the low end of a cascade's end-of-speech wait
#: (voice-it.md §1, an estimate: about 0.8 to 1.3 s) -- the punctuation already says the
#: sentence ended; 1.6 s without it is twice that, since a pause mid-clause is
#: a person thinking, not done.
UTTERANCE_END_GAP_SECONDS = 0.8
UTTERANCE_IDLE_GAP_SECONDS = 1.6
#: What ends a sentence; a comma, a colon or a dash never ends an utterance. A
#: closing quote or bracket after it is skipped.
UTTERANCE_END_MARKS = (".", "?", "!", "\u2026")
_UTTERANCE_TRAILING = "\"')]}\u201d\u2019 \t\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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

    #: The tier it was granted on (accountlease.TIERS), None when none was named.
    tier: str | None


class Leases(Protocol):
    """Where a session's credential comes from: the quota broker's Voice it pool
    (accountlease.VoiceBroker). Raises accountlease.VoicePoolRefused when it leases none."""

    async def lease(self, *, person: str, voice_session_id: str) -> LeasedCredential: ...

    async def move(self, spent: LeasedCredential, *, person: str) -> LeasedCredential: ...


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
    return _side(kind, TRANSCRIPT_FINAL_SUFFIXES)


def delta_speaker(kind: str) -> str | None:
    """Whose transcript delta an event of type `kind` is -- "person" or "voice" --
    or None when it is not one: a type of a delta's family
    (TRANSCRIPT_DELTA_SUFFIXES) whose words name a side, the person's first."""
    return _side(kind, TRANSCRIPT_DELTA_SUFFIXES)


def _side(kind: str, suffixes: tuple[str, ...]) -> str | None:
    if not kind.endswith(suffixes):
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


def _delta_text(event: dict) -> str | None:
    """A delta's text, unstripped (its spaces join it to the next); None when no
    DELTA_TEXT_FIELDS holds a string."""
    for field in DELTA_TEXT_FIELDS:
        text = event.get(field)
        if isinstance(text, str):
            return text
    return None


def _segment_key(event: dict) -> str | None:
    """The item or response a transcript event belongs to, if it names one."""
    for field in ("item_id", "response_id"):
        value = event.get(field)
        if isinstance(value, str) and value:
            return f"{field}:{value}"
    return None


def ends_sentence(text: str) -> bool:
    """Whether `text` ends in sentence-ending punctuation (UTTERANCE_END_MARKS),
    past any closing quote or bracket; a comma never does."""
    return text.rstrip(_UTTERANCE_TRAILING).endswith(UTTERANCE_END_MARKS)


def utterance_gap(text: str) -> float:
    """How long the person must be silent after `text` for it to be a whole utterance."""
    return UTTERANCE_END_GAP_SECONDS if ends_sentence(text) else UTTERANCE_IDLE_GAP_SECONDS


def _after(text: str, heard: str) -> str:
    """What a final of an item adds to the utterances already heard of it: the
    rest of `text` when it starts with `heard` (spaces aside), else all of it."""
    flat, prefix = " ".join(text.split()), " ".join(heard.split())
    if prefix and flat.startswith(prefix):
        return flat[len(prefix):].strip()
    return text


class _Segment:
    """One speaker turn of the transcript: its deltas, or its final. A person's
    segment is one utterance: `ended` once it is whole (WHOLE UTTERANCES)."""

    __slots__ = ("speaker", "key", "text", "final", "ended", "last_at")

    def __init__(self, speaker: str, key: str | None, text: str = "", final: bool = False) -> None:
        self.speaker = speaker
        self.key = key
        self.text = text
        #: Its text is a final's: a late delta of its item adds nothing.
        self.final = final
        #: Nothing more joins it: a final, or a person's utterance that ended.
        self.ended = final
        #: The session clock at its last delta.
        self.last_at = 0.0


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
                 finals: vreadback.Finals | None = None,
                 clock: Callable[[], float] | None = None) -> None:
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
        #: The transcript's segments in the order they began, open ones (deltas
        #: still arriving) included, at most TRANSCRIPT_LINES; `transcript` reads them.
        self._segments: list[_Segment] = []
        #: Seconds, monotonic: what the utterance gaps are measured on (tests inject one).
        self._clock = clock or time.monotonic
        #: Wakes when the person's open utterance may have gone idle long enough.
        self._utterance_timer: asyncio.TimerHandle | None = None
        #: Whether a delta without text has had its keys logged (once a session).
        self._textless_logged = False
        #: How many sideband events of each type arrived (`_event_type`, never content).
        self.event_types: dict[str, int] = {}
        self._turns: set[asyncio.Task] = set()
        #: The planner model read at this session's start; None keeps the pin.
        self.planner_model: str | None = None
        #: Debug mode's trace (trace.py), set at the start only when swarm-api's
        #: start body said `debug: true`; None counts and sends nothing.
        self.trace: vtrace.SessionTrace | None = None
        self._trace_writes: set[asyncio.Task] = set()
        #: Told when the key is spent (Sessions moves the session to the next tier).
        self.on_spent: Callable[["LiveSession"], None] | None = None
        #: The next tier's credential, held for the reopen once the key is spent.
        self.next_credential: LeasedCredential | None = None
        #: Set once a reopen has replaced this session (its forget is its own only).
        self.replaced = False
        #: Whether a move to the next tier is in flight.
        self.moving = False
        #: THE PAUSE. When either side last spoke (the injected clock); whether
        #: the voice's audio is playing; the delegations in flight, by id, in
        #: order (shared with the session a reopen opens: the turns run on).
        self.spoken_at = self._clock()
        self._playing = False
        self.delegations: dict[str, None] = {}
        #: Seconds of silence that pause it (Sessions sets PAUSE_AFTER_SECONDS
        #: when the pause is armed); None never pauses.
        self.pause_after: float | None = None
        #: Told when the silence is long enough (Sessions runs the pause).
        self.on_quiet: Callable[["LiveSession"], None] | None = None
        #: Whether a read-back is held for the person's answer (Sessions' holds).
        self.readback_pending: Callable[[], bool] = lambda: False
        self._quiet_timer: asyncio.TimerHandle | None = None
        #: The pause is under way (its sentence sent, the close not yet done).
        self.pausing = False
        #: Whether the person spoke since the pause sentence was sent (no pause then).
        self.person_spoke = False
        self._spoke: asyncio.Event | None = None
        #: When the Live session was closed for the pause (ISO, UTC); None open.
        self.paused_at: str | None = None
        #: Results of delegations that finished while paused, said on the wake.
        self.held_results: list[str] = []
        #: Whether a wake was asked for (sent once; a reopen in flight absorbs the rest).
        self.waking = False
        #: Set once the voice session ends for good (a paused one is kept till then).
        self.ending = False
        #: The Live session a reopen opened in this one's place.
        self.successor: "LiveSession | None" = None
        #: The usage seconds of the voice session's earlier Live sessions (each reopen adds one).
        self.earlier_seconds: float = 0.0
        #: Set once Sessions has forgotten it (released its lease, or handed it on).
        self.forgotten = False
        #: THE UPDATES (updates.py): the voice session's queue, bound by Sessions
        #: when a planner is configured; None speaks each result at once, as before.
        self.updates: vupdates.UpdateQueue | None = None
        #: The newest delegation seen: an update after the work ended is said for it.
        self.last_delegation: str | None = None
        #: The person's voice detection says they are speaking (SPEECH_STARTED_SUFFIX).
        self._person_speaking = False
        self._update_lines = 0

    @property
    def tier(self) -> str | None:
        """The tier this session's key serves from (accountlease.TIERS)."""
        return getattr(self._credential, "tier", None)

    @property
    def reopen_pending(self) -> bool:
        """Whether the next tier is held and the browser was told to offer again."""
        return self.next_credential is not None

    @property
    def paused(self) -> bool:
        """Whether the Live session was closed for the pause and not reopened."""
        return self.paused_at is not None

    @property
    def talk_seconds(self) -> float:
        """The voice session's billed seconds so far: every Live session's
        `usage.seconds`, this one's once it has closed (§3.12.8)."""
        return self.earlier_seconds + (self.usage_seconds or 0)

    @property
    def transcript(self) -> list[tuple[str, str]]:
        """(speaker, text) of each segment with text, newest last: what the planner reads."""
        lines = []
        for segment in self._segments:
            text = segment.text.strip()
            if text:
                lines.append((segment.speaker, text))
        return lines

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
                    # Any event is a moment to see whether the person has been quiet long enough.
                    self._end_idle_utterances()
                    if kind == "session.closed":
                        self._note_closed(event)
                        break
                    speaker = final_speaker(kind)
                    delta = delta_speaker(kind) if speaker is None else None
                    if speaker is not None:
                        self._note_transcript(speaker, event)
                    elif delta is not None:
                        self._note_delta(delta, event)
                    elif kind == COMMENTARY_APPENDED:
                        # The model's own commentary: never a line of the person's.
                        pass
                    elif kind == DELEGATION_CREATED:
                        # A handoff ends what the person was saying: the planner and
                        # the read-back gate hear it whole, the gate first.
                        if self.trace is not None:
                            self.trace.note("handoffs")
                        self._end_utterances()
                        if self._planner is not None:
                            self._delegated(event)
                    # After the transcript has taken the event: a voice line's text is current.
                    self._note_activity(kind, event)
                    if kind == "error":
                        # The error's code only: its message may quote what was sent.
                        error = event.get("error") if isinstance(event.get("error"), dict) else {}
                        log.warning("voice session %s: sideband error %s", self.voice_id, error.get("code", "unknown"))
                        if error.get("code") in SPENT_CODES and self.on_spent is not None:
                            self.on_spent(self)
                    if self._on_event is not None:
                        try:
                            await self._on_event(self.voice_id, event)
                        except Exception as exc:  # noqa: BLE001 - a sink's failure never drops the sideband
                            log.warning("voice session %s: event sink failed: %s", self.voice_id, type(exc).__name__)
                    # ... and whether both sides have been quiet long enough to pause.
                    self.check_quiet()
                    # ... and whether a held update has its gap now.
                    if self.updates is not None and self.updates.pending:
                        self.updates.poke()
        except WebSocketException as exc:
            log.info("voice session %s: sideband dropped (%s)", self.voice_id, type(exc).__name__)
        finally:
            if self._utterance_timer is not None:
                self._utterance_timer.cancel()
                self._utterance_timer = None
            self._stop_quiet_timer()
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
        """A final: it replaces the text of its segment's deltas, else adds a
        segment. The person's ends their utterance, which then goes to the
        read-back gate; of an item already heard in part (an utterance ended by
        a gap), only the rest of the final is new."""
        text = _final_text(event)[:TRANSCRIPT_SEGMENT_MAX]
        if self.trace is not None and speaker == "person":
            self.trace.note("finals")
            if not text.strip():
                self.trace.note("empty_finals")
        if not text:
            return
        key = _segment_key(event)
        segment = self._open_segment(speaker, key)
        if speaker == "person" and key is not None:
            heard = " ".join(s.text for s in self._segments
                             if s.ended and s.speaker == speaker and s.key == key)
            text = _after(text, heard) if heard else text
            if not text:
                if segment is not None:
                    self._end(segment)
                return
        if segment is None:
            segment = self._add(_Segment(speaker, key, text, final=True))
            segment.ended = False
        else:
            segment.text = text
            segment.final = True
        self._end(segment)

    def _note_delta(self, speaker: str, event: dict) -> None:
        """A delta: added to its segment, or the start of a new one."""
        text = _delta_text(event)
        if text is None:
            if not self._textless_logged:
                self._textless_logged = True
                keys = sorted(k for k in event if isinstance(k, str) and _EVENT_TYPE.fullmatch(k))
                log.debug("voice session %s: a transcript delta had no text field; its keys: %s",
                          self.voice_id, ", ".join(keys) or "none")
            return
        key = _segment_key(event)
        if key is not None and any(s.final and s.key == key and s.speaker == speaker
                                   for s in self._segments):
            # A late delta of a segment its final has already replaced.
            return
        if speaker == "voice":
            # A change of speaker ends the person's utterance -- unless it names its
            # item: the person's transcription runs behind the voice's reply, and
            # an item is ended by the next item, the gaps, a final or a handoff.
            self._end_utterances(unkeyed_only=True)
        segment = self._open_segment(speaker, key)
        if self.trace is not None and speaker == "person":
            self.trace.note("pieces")
            if segment is None and any(s.speaker == "voice" and not s.final for s in self._segments):
                # The person starts again while the voice's own line is still arriving.
                self.trace.note("barge_ins")
        if segment is None:
            if speaker == "person":
                # A new item, or the person again after the voice: what was open is whole.
                self._end_utterances()
            segment = self._add(_Segment(speaker, key))
        elif segment.key is None:
            segment.key = key
        segment.text = (segment.text + text)[:TRANSCRIPT_SEGMENT_MAX]
        if speaker == "person":
            segment.last_at = self._clock()
            self._watch_utterance(segment)

    def _end(self, segment: _Segment) -> None:
        """Nothing more joins `segment`; a person's whole utterance goes, in one
        piece and once, to swarm-api's read-back gate."""
        if segment.ended:
            return
        segment.ended = True
        if segment.speaker == "person":
            self._answer(segment.text)

    def _end_utterances(self, *, unkeyed_only: bool = False) -> None:
        """End the person's open utterances (those naming no item only, if asked)."""
        for segment in list(self._segments):
            if segment.speaker == "person" and not segment.ended and not (unkeyed_only and segment.key):
                self._end(segment)

    def _end_idle_utterances(self) -> float | None:
        """End each open utterance of the person's that has been quiet for its
        gap (`utterance_gap`); the seconds until the next still open one is due,
        or None when none is open."""
        now = self._clock()
        wait: float | None = None
        for segment in list(self._segments):
            if segment.speaker != "person" or segment.ended:
                continue
            left = segment.last_at + utterance_gap(segment.text) - now
            if left <= 0:
                self._end(segment)
            else:
                wait = left if wait is None else min(wait, left)
        return wait

    def _watch_utterance(self, segment: _Segment) -> None:
        """Wake when `segment` would be due: the person may say nothing more and
        no other event may arrive to notice it."""
        if self._utterance_timer is not None:
            self._utterance_timer.cancel()
        self._utterance_timer = asyncio.get_running_loop().call_later(
            utterance_gap(segment.text), self._utterance_due)

    def _utterance_due(self) -> None:
        self._utterance_timer = None
        wait = self._end_idle_utterances()
        if wait is not None:
            # Woken a little early, or by an injected clock: look again when it is due.
            self._utterance_timer = asyncio.get_running_loop().call_later(
                max(wait, 0.05), self._utterance_due)

    def _open_segment(self, speaker: str, key: str | None) -> _Segment | None:
        """The open segment `speaker`'s event of `key` belongs to: the one of the
        same item or response; for an event naming neither, or a segment that
        named neither, the newest segment if it is still open and the speaker's
        (a change of speaker starts a new one)."""
        if key is not None:
            for segment in reversed(self._segments):
                if not segment.ended and segment.speaker == speaker and segment.key == key:
                    return segment
        if self._segments:
            newest = self._segments[-1]
            if not newest.ended and newest.speaker == speaker and (key is None or newest.key is None):
                return newest
        return None

    def _add(self, segment: _Segment) -> _Segment:
        self._segments.append(segment)
        del self._segments[:-TRANSCRIPT_LINES]
        return segment

    def _answer(self, text: str) -> None:
        """Post the person's whole utterance to swarm-api's read-back gate before
        the planner hears it (voice-it-own-speech.md §7.3): a "go" or a refusal
        answers a pending read-back there, whatever the voice does."""
        if self._finals is None or self.grant is None or not text.strip():
            return
        text = text.strip()
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
        self.delegations[delegation_id] = None
        self.last_delegation = delegation_id
        task = asyncio.create_task(self._plan(delegation_id, delegation_request(event)),
                                   name=f"planner-{self.voice_id}")
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _plan(self, delegation_id: str, request: str = "") -> None:
        try:
            await self._plan_turn(delegation_id, request)
        finally:
            # The `delegations` dict is shared with a reopened session: it hears this too.
            self.delegations.pop(delegation_id, None)

    async def _plan_turn(self, delegation_id: str, request: str) -> None:
        assert self._planner is not None
        transcript = list(self.transcript)
        if not transcript:
            log.warning("voice session %s: a planner turn started with an empty transcript "
                        "(delegation text: %s); sideband event types so far: %s", self.voice_id,
                        "yes" if request else "none", self._types_seen())
            if not request:
                # Nothing to plan from: a turn would read the inbox and report "nothing".
                await self._result(delegation_id, SAY_ASK_AGAIN)
                self._traced(transcript, request, None, SAY_ASK_AGAIN)
                return
        result = await self._planner.delegate(person=self.person, grant=self.grant,
                                              transcript=transcript, delegation_id=delegation_id,
                                              model=self.planner_model, request=request or None)
        await self._result(delegation_id, result.spoken)
        self._traced(transcript, request, result, result.spoken)

    async def _result(self, delegation_id: str, spoken: str) -> None:
        """The planner's result, through the update queue (updates.py) when one
        is bound: spoken at the next gap, with the updates waiting merged into
        its line, or held for the wake when paused; else at once, as before."""
        queue = self.updates
        if queue is None or queue.closed or not spoken:
            await self._say(delegation_id, spoken)
            return
        queue.note(vupdates.Update(vupdates.RESULT, f"result:{delegation_id}", spoken, delegation_id))
        await queue.pump()

    def at_gap(self) -> bool:
        """Whether an update may be spoken now: the Live session is open and not
        pausing, the person is not speaking (no utterance of theirs open, no
        speech started and not stopped) and the voice is not mid-sentence (its
        audio playing, or its text not ending a sentence for less than
        VOICE_SENTENCE_SECONDS)."""
        if self.ended or self._socket is None or self.pausing or self._playing or self._person_speaking:
            return False
        self._end_idle_utterances()
        if any(s.speaker == "person" and not s.ended for s in self._segments):
            return False
        for segment in reversed(self._segments):
            if segment.speaker == "voice":
                if (not segment.final and not ends_sentence(segment.text)
                        and self._clock() - self.spoken_at < VOICE_SENTENCE_SECONDS):
                    return False
                break
        return True

    async def say_update(self, line: str, *, wakes: bool, delegation_id: str | None = None) -> bool:
        """Say one update queue line (updates.py): as the result's commentary for
        `delegation_id`, else for the delegation in flight or the newest one.
        Paused, a line that `wakes` is held for the wake and asks for it (cause
        `result`, as a result does); one that does not is dropped. Whether it
        was sent or held."""
        target = delegation_id or next(iter(self.delegations), None) or self.last_delegation
        if target is None or (self.ended and not self.paused):
            return False
        if self.paused and not wakes:
            return False
        if delegation_id is not None:
            await self._say(target, line)
        else:
            self._update_lines += 1
            await self._say(target, line, prefix=f"update{self._update_lines}")
        return True

    def _traced(self, transcript: list[tuple[str, str]], request: str,
                result: "TurnResult | None", told: str) -> None:
        """With debug mode on, send this finished turn's trace beside it
        (trace.py); the turn never waits for it and never sees it fail."""
        if self.trace is None or self._planner is None:
            return
        body = self.trace.turn(transcript=transcript, handoff=request or None, result=result, told=told)
        # The tier beside the engine: which kind of account served the turn (§5.4).
        body["tier"] = self.tier
        vtrace.spawn(self._planner.write_trace(self.grant, body), self._trace_writes)

    async def _say(self, delegation_id: str, spoken: str, *, prefix: str = "planner") -> None:
        """Send `spoken` back as the delegation's commentary, unless the session
        ended. Paused, it is held for the wake, which it asks for (`cause:
        result`); reopened, the Live session open now says it (§3.12.7)."""
        if not spoken:
            return
        if self.successor is not None:
            current = self.successor
            while current.successor is not None:
                current = current.successor
            await current._say(delegation_id, spoken, prefix=prefix)
            return
        if self.paused and not self.ending:
            self.held_results.append(spoken)
            await self.wake(WAKE_CAUSES[0])
            return
        if self.ended or self._socket is None:
            return
        # Said aloud: the voice is speaking, so the silence counts from now.
        self.spoken_at = self._clock()
        try:
            await self._socket.send(json.dumps({
                "type": COMMENTARY_APPEND, "event_id": f"{prefix}_{delegation_id}"[:64],
                "delegation_id": delegation_id, "content": spoken}))
        except WebSocketException as exc:
            log.info("voice session %s: the planner's summary was not sent (%s)",
                     self.voice_id, type(exc).__name__)

    def _note_activity(self, kind: str, event: dict) -> None:
        """Either side speaking resets the pause's silence; audio playing holds it."""
        if kind == OUTPUT_AUDIO_STARTED:
            self._playing = True
            return
        if kind == OUTPUT_AUDIO_STOPPED:
            self._playing = False
            self._spoken()
            return
        if kind.endswith(SPEECH_STARTED_SUFFIX) and not kind.startswith("session.output"):
            self._person_speaking = True
            self.spoken_at = self._clock()
            if self.pausing:
                self.person_spoke = True
            return
        if kind.endswith(SPEECH_STOPPED_SUFFIX) and not kind.startswith("session.output"):
            self._person_speaking = False
            self.spoken_at = self._clock()
            return
        speaker = final_speaker(kind)
        final = speaker is not None
        if speaker is None:
            speaker = delta_speaker(kind)
        if speaker is None and kind != COMMENTARY_APPENDED:
            return
        if speaker is not None and not (_final_text(event) if final else _delta_text(event) or "").strip():
            return
        self.spoken_at = self._clock()
        if speaker == "person" and self.pausing:
            self.person_spoke = True
        if speaker == "voice" and (final or ends_sentence(self._voice_text())):
            self._spoken()

    def _voice_text(self) -> str:
        for segment in reversed(self._segments):
            if segment.speaker == "voice":
                return segment.text
        return ""

    def _spoken(self) -> None:
        if self._spoke is not None:
            self._spoke.set()

    async def say_and_wait(self, delegation_id: str, spoken: str, seconds: float) -> None:
        """Say `spoken` for `delegation_id` and wait until the voice has said a
        sentence after it (a voice final, a sentence's end, or the audio
        stopping), at most `seconds`."""
        self._spoke = asyncio.Event()
        try:
            await self._say(delegation_id, spoken, prefix="pause")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._spoke.wait(), seconds)
        finally:
            self._spoke = None

    def _stop_quiet_timer(self) -> None:
        if self._quiet_timer is not None:
            self._quiet_timer.cancel()
            self._quiet_timer = None

    def check_quiet(self) -> None:
        """Pause when neither side has spoken for `pause_after` with a delegation
        in flight, no read-back pending and nothing playing; else look again when
        it could be due. Checked on every sideband event (on the injected clock)
        and by a timer for when none comes."""
        self._stop_quiet_timer()
        if (self.pause_after is None or self.on_quiet is None or self.pausing or self.paused
                or self.ended or self.replaced or self.reopen_pending):
            return
        if not self.delegations or self._playing or self.readback_pending():
            return
        left = self.spoken_at + self.pause_after - self._clock()
        if left <= 0:
            self.pausing = True
            self.on_quiet(self)
            return
        self._quiet_timer = asyncio.get_running_loop().call_later(max(left, 0.05), self._quiet_due)

    def _quiet_due(self) -> None:
        self._quiet_timer = None
        self.check_quiet()

    async def emit(self, event: dict[str, Any]) -> None:
        """Hand `event` to the event sink; a sink's failure is logged, never raised."""
        if self._on_event is None:
            return
        try:
            await self._on_event(self.voice_id, event)
        except Exception as exc:  # noqa: BLE001 - a sink's failure never fails the session
            log.warning("voice session %s: event sink failed: %s", self.voice_id, type(exc).__name__)

    async def wake(self, cause: str) -> None:
        """Ask the holder to reopen the paused voice (`session.state: waking`), once."""
        if self.waking or not self.paused:
            return
        self.waking = True
        log.info("voice session %s: asking for the wake (%s)", self.voice_id, cause)
        await self.emit({"type": SESSION_STATE, "state": WAKING, "cause": cause})

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


class SessionEnded(LookupError):
    """No such voice session is held here: it ended, or never started here."""


class NotPaused(Exception):
    """A wake for a voice session whose Live session is open (or a tier reopen
    with no next tier held): there is nothing to reopen."""


class Sessions:
    """Every live session this process holds, by swarm-api's voice session id.

    One process (a one-replica StatefulSet, §3.4) so one voice session's GPT-Live
    session is held exactly once."""

    def __init__(self, api: LiveApi, *, leases: Leases, close_grace: float = 5.0,
                 on_event: EventSink | None = None, planner: "Planner | None" = None,
                 finals: vreadback.Finals | None = None, pause_after: float | None = None,
                 pause_say_seconds: float = PAUSE_SAY_SECONDS,
                 clock: Callable[[], float] | None = None,
                 updates: vupdates.Board | None = vupdates.BOARD) -> None:
        self.api = api
        #: THE UPDATES (updates.py): each voice session's queue, on the board the
        #: follower and the planner feed; None speaks each result at once.
        self._updates = updates
        #: THE PAUSE: seconds of silence with a delegation in flight that pause a
        #: session (PAUSE_AFTER_SECONDS when armed), or None: never paused.
        self._pause_after = pause_after
        self._pause_say = pause_say_seconds
        #: The sessions' clock for the pause and the utterance gaps (tests inject one).
        self._clock = clock
        self._pauses: set[asyncio.Task] = set()
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
        #: Sessions whose spent Live session ended before the reopen came, by
        #: voice id, each holding its next tier (`next_credential`).
        self._awaiting: dict[str, LiveSession] = {}
        self._expiries: dict[str, asyncio.TimerHandle] = {}
        self._moves: set[asyncio.Task] = set()

    def get(self, voice_id: str) -> LiveSession | None:
        return self._live.get(voice_id)

    def counts(self) -> dict[str, int]:
        return {"sessions": len(self._live), "attached": sum(1 for s in self._live.values() if s.attached)}

    def _forget(self, session: LiveSession) -> None:
        if session.paused and not session.replaced and not session.ending:
            # Closed for the pause: the voice session, its lease, its grant and
            # its planner turns live on until a wake or an end.
            return
        if session.forgotten:
            # Its sideband's close and the path that closed it both forget it: once.
            return
        session.forgotten = True
        session.cancel_turns()
        if self._live.get(session.voice_id) is session:
            del self._live[session.voice_id]
        # The Voice it pool account goes back with the session: an end, a
        # shutdown, or OpenAI's own `session.closed` on the sideband -- unless
        # a wake carried it on to the Live session open now.
        carried = session.successor is not None and session.successor._credential is session._credential
        if not carried:
            self._release(session._credential)
        if session.replaced:
            # A reopen took its place: the voice session lives on in the new one.
            session.grant = None
            return
        if session.next_credential is not None:
            # The spent session ended before the browser offered again: its
            # next tier waits for the reopen, for REOPEN_SECONDS at most.
            self._awaiting[session.voice_id] = session
            self._expiries[session.voice_id] = asyncio.get_running_loop().call_later(
                REOPEN_SECONDS, self._expire, session.voice_id)
            return
        self._ended(session)

    def _ended(self, session: LiveSession) -> None:
        # The grant dies with the session (§3.6): an ended session presents none.
        session.grant = None
        self.holds.forget(session.voice_id)
        if self._updates is not None and self._updates.get(session.voice_id) is session.updates:
            self._updates.drop(session.voice_id)
        if self._planner is not None:
            self._planner.forget(session.voice_id)

    def _expire(self, voice_id: str) -> None:
        """No reopen came: the next tier goes back and the voice session ends."""
        self._expiries.pop(voice_id, None)
        session = self._awaiting.pop(voice_id, None)
        if session is None:
            return
        log.info("voice session %s: no reopen within %.0f s; the next tier is given back", voice_id,
                 REOPEN_SECONDS)
        held, session.next_credential = session.next_credential, None
        if held is not None:
            self._release(held)
        self._ended(session)

    def _spent(self, session: LiveSession) -> None:
        """The session's key is spent: lease the next tier beside it (§5.3)."""
        if session.next_credential is not None or session.replaced or session.moving:
            return
        session.moving = True
        task = asyncio.get_running_loop().create_task(self._move(session), name="voice-tier-move")
        self._moves.add(task)
        task.add_done_callback(self._moves.discard)

    async def _move(self, session: LiveSession) -> None:
        try:
            moved = await self._leases.move(session._credential, person=session.person)
        except (VoicePoolRefused, CredentialError) as exc:
            log.info("voice session %s: no tier after %s: %s", session.voice_id, session.tier,
                     exc.reason if isinstance(exc, VoicePoolRefused) else type(exc).__name__)
            return
        finally:
            session.moving = False
        if session.ended and self._awaiting.get(session.voice_id) is not session:
            # Ended for good while the lease was taken: nothing to reopen.
            self._release(moved)
            return
        moved.start_renewing()
        session.next_credential = moved
        log.info("voice session %s: the %s tier is held for the reopen", session.voice_id, moved.tier)
        if self._on_event is not None:
            try:
                await self._on_event(session.voice_id, {"type": REOPEN_EVENT, "tier": moved.tier})
            except Exception as exc:  # noqa: BLE001 - a sink's failure never fails the move
                log.warning("voice session %s: event sink failed: %s", session.voice_id, type(exc).__name__)

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
                    grant: SessionGrant | None = None, planner_model: str | None = None,
                    debug: bool = False) -> LiveSession:
        """Lease a Voice it pool account for `person`, create the GPT-Live session for
        `offer` on it, attach its sideband, and hold it. Raises SessionExists,
        accountlease.VoicePoolRefused (nothing started), CredentialError or LiveError;
        on any failure after the lease, the lease's release is begun before raising.

        The whole start runs inside START_SECONDS, and a failure waits at most
        RELEASE_WAIT_SECONDS for the release: swarm-api must have this answer
        before its START_TIMEOUT_SECONDS, or it shows the person "could not be
        reached" in place of the refusal (see THE START HAS ONE DEADLINE)."""
        if voice_id in self._starting:
            raise SessionExists(voice_id)
        spent = self._awaiting.get(voice_id) or self._live.get(voice_id)
        if spent is not None and spent.next_credential is not None:
            return await self._reopen(spent, offer=offer, grant=grant, planner_model=planner_model)
        if voice_id in self._live:
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
                    stage = "the create"
                    session = await self._create(voice_id, person, credential, offer)
                    session.grant = grant
                    session.planner_model = planner_model
                    if debug:
                        session.trace = vtrace.SessionTrace(vtrace.ENGINE_LIVE, clock=session._clock)
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
            log.info("voice session %s: live session %s attached on the %s tier", voice_id, session.live_id,
                     session.tier)
            if self._updates is not None and self._planner is not None:
                # One queue for the voice session's life: it speaks through
                # whichever Live session holds it now (a reopen replaces it).
                session.updates = self._updates.bind(voice_id, lambda: self._live.get(voice_id),
                                                     clock=self._clock)
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

    def _base_instructions(self) -> str:
        if self._planner is None:
            return INSTRUCTIONS + TURN_INSTRUCTIONS
        return INSTRUCTIONS + DELEGATE_INSTRUCTIONS + TURN_INSTRUCTIONS

    def _config(self, instructions: str | None = None) -> dict[str, Any]:
        config: dict[str, Any] = {"model": MODEL, "instructions": instructions or self._base_instructions()}
        if self._planner is not None:
            config["delegation"] = {"type": "client"}
        return config

    async def _create(self, voice_id: str, person: str, credential: LeasedCredential, offer: str,
                      instructions: str | None = None) -> LiveSession:
        """The GPT-Live session for `offer` on `credential`, not yet attached."""
        live_id, answer = await self.api.create(credential, offer, self._config(instructions))
        session = LiveSession(voice_id, live_id, credential, self.api,
                              on_event=self._on_event, on_closed=self._forget,
                              person=person, planner=self._planner, finals=self._finals,
                              clock=self._clock)
        session.answer = answer
        session.on_spent = self._spent
        if self._planner is not None and self._pause_after is not None:
            # Without a planner nothing is ever delegated, so nothing waits to pause.
            session.pause_after = self._pause_after
            session.on_quiet = self._quiet
            session.readback_pending = lambda: self._readback_pending(voice_id)
        if hasattr(credential, "on_lost"):
            # A renewal the broker refuses is a spent key too (§5.3).
            credential.on_lost = lambda: self._spent(session)
        return session

    def _readback_pending(self, voice_id: str) -> bool:
        """Whether a call is held for the person's answer to a read-back (readback.py)."""
        held = getattr(self.holds, "_held", {}).get(voice_id)
        return held is not None and self.holds.clock() - held.at <= vreadback.HOLD_SECONDS

    def _quiet(self, session: LiveSession) -> None:
        task = asyncio.get_running_loop().create_task(self._pause(session), name="voice-pause")
        self._pauses.add(task)
        task.add_done_callback(self._pauses.discard)

    async def _pause(self, session: LiveSession) -> None:
        """THE PAUSE (§3.12.7): say the sentence, wait for it (at most
        `pause_say_seconds`), close the Live session keeping its usage seconds,
        and tell the sink `session.state: paused`. The voice session, its grant,
        its lease and its planner turns keep running. The person speaking, or the
        delegated work finishing, before the close leaves the session open."""
        try:
            delegation_id = next(iter(session.delegations), None)
            if delegation_id is None or session.ended:
                return
            session.person_spoke = False
            await session.say_and_wait(delegation_id, PAUSE_SENTENCE, self._pause_say)
            if (session.person_spoke or not session.delegations or session.ended or session.replaced
                    or session.reopen_pending or self._live.get(session.voice_id) is not session):
                log.info("voice session %s: the pause was called off", session.voice_id)
                return
            session.paused_at = _now_iso()
            try:
                await session.end(self._grace)
            except (LiveError, CredentialError) as exc:
                # Not closed: the session stays open (and billing), as before the pause.
                session.paused_at = None
                log.warning("voice session %s: the pause could not close the live session: %s",
                            session.voice_id, exc)
                return
        finally:
            session.pausing = False
            if not session.paused and not session.ended:
                session.spoken_at = session._clock()
                session.check_quiet()
        log.info("voice session %s: paused after %.0f s of silence (%s s talked)", session.voice_id,
                 session.pause_after or 0, f"{session.talk_seconds:g}")
        await session.emit({"type": SESSION_STATE, "state": PAUSED, "paused_at": session.paused_at,
                            "usage_seconds": session.talk_seconds})
        if not session.delegations and session.held_results:
            await session.wake(WAKE_CAUSES[0])

    async def reopen(self, voice_id: str, *, offer: str, cause: str, grant: SessionGrant | None = None,
                     planner_model: str | None = None,
                     context: vinstructions.ReopenContext | None = None) -> LiveSession:
        """Open the voice session's Live session again from the browser's fresh
        `offer`, by `cause` (REOPEN_CAUSES): a wake of a paused session on its
        held lease, or the tier move on its held next tier. Raises ValueError
        (an unknown cause), SessionEnded (not held here: ended), NotPaused
        (nothing to reopen), SessionExists (a start or reopen in flight),
        CredentialError or LiveError; a failed reopen leaves the session paused
        (or its next tier held) for a retry."""
        if cause not in REOPEN_CAUSES:
            raise ValueError("cause must be one of " + ", ".join(REOPEN_CAUSES))
        session = self._live.get(voice_id) or self._awaiting.get(voice_id)
        if session is None or session.ending:
            raise SessionEnded(voice_id)
        if voice_id in self._starting:
            raise SessionExists(voice_id)
        if cause == TIER_CAUSE:
            if session.next_credential is None:
                raise NotPaused(voice_id)
        elif not session.paused:
            raise NotPaused(voice_id)
        return await self._reopen(session, offer=offer, grant=grant, planner_model=planner_model,
                                  cause=cause, context=context)

    async def _reopen(self, spent: LiveSession, *, offer: str, grant: SessionGrant | None,
                      planner_model: str | None, cause: str = TIER_CAUSE,
                      context: vinstructions.ReopenContext | None = None) -> LiveSession:
        """Reopen Live for `spent`'s voice session, by `cause` (§3.12.7: one path):
        the new Live session takes the browser's fresh offer -- on the next
        tier's key for the tier move (§5.3), on the held lease for a wake -- with
        instructions rebuilt (instructions.py), and carries the transcript
        segments, the grant, the planner model, the trace, the planner turns in
        flight and the usage seconds over. A spent session still open is closed;
        its key is given back unless the new session carries it. A failed
        reopen keeps the next tier held, or the session paused, for a retry."""
        voice_id = spent.voice_id
        credential = spent.next_credential if spent.next_credential is not None else spent._credential
        spent.waking = True
        first_words = list(spent.held_results)
        known = (spent.grant.headers()[GRANT_HEADER],) if spent.grant is not None else ()
        instructions = vinstructions.build(self._base_instructions(), context, transcript=spent.transcript,
                                           cause=cause, first_words=first_words, secrets_=known)
        self._starting.add(voice_id)
        try:
            deadline = asyncio.get_running_loop().time() + START_SECONDS
            async with asyncio.timeout_at(deadline):
                session = await self._create(voice_id, spent.person, credential, offer, instructions)
                session.grant = grant if grant is not None else spent.grant
                session.planner_model = planner_model if planner_model is not None else spent.planner_model
                session.trace = spent.trace
                session._segments = list(spent._segments)
                await session.attach(deadline - ATTACH_RESERVE_SECONDS)
        except BaseException:
            spent.waking = False
            raise
        finally:
            self._starting.discard(voice_id)
        # The planner turns in flight run on: their results reach the voice open now.
        session._turns, spent._turns = spent._turns, set()
        session.delegations = spent.delegations
        session.updates = spent.updates
        session.last_delegation = spent.last_delegation
        spent.held_results = []
        if spent.next_credential is credential:
            spent.next_credential = None
        spent.replaced = True
        spent.successor = session
        timer = self._expiries.pop(voice_id, None)
        if timer is not None:
            timer.cancel()
        self._awaiting.pop(voice_id, None)
        self._live[voice_id] = session
        log.info("voice session %s: live reopened (%s) as %s on the %s tier", voice_id, cause, session.live_id,
                 session.tier)
        if not spent.ended:
            try:
                await spent.end(self._grace)
            except (LiveError, CredentialError) as exc:
                log.warning("voice session %s: the spent live session did not close: %s", voice_id, exc)
        session.earlier_seconds = spent.talk_seconds
        self._forget(spent)
        if cause in WAKE_CAUSES:
            await session.emit({"type": SESSION_STATE, "state": LISTENING, "cause": cause,
                                "woke_at": _now_iso()})
        return session

    async def end(self, voice_id: str) -> LiveSession | None:
        session = self._live.get(voice_id)
        if session is None:
            waiting = self._awaiting.get(voice_id)
            if waiting is not None:
                timer = self._expiries.get(voice_id)
                if timer is not None:
                    timer.cancel()
                self._expire(voice_id)
                await self.drain_releases()
            return waiting
        # An end is the person's: a next tier held for a reopen goes back with it,
        # and a paused session is forgotten now (its Live session closed already).
        session.ending = True
        held, session.next_credential = session.next_credential, None
        if held is not None:
            self._release(held)
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
        for task in list(self._moves):
            task.cancel()
        for voice_id in list(self._awaiting):
            await self.end(voice_id)
        for voice_id in list(self._live):
            try:
                await self.end(voice_id)
            except (LiveError, CredentialError) as exc:
                log.warning("voice session %s: could not end at shutdown: %s", voice_id, exc)
        await self.drain_releases()
