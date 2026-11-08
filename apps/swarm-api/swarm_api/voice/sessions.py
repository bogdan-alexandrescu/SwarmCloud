"""Voice it sessions: the project and session documents, and swarm-voice's
internal start and end calls (docs/design/voice-it.md §3.6, §3.7, §10 phase 1).

THE DOCUMENTS (§3.7). Everything is under the workspace the console has
selected, and every read also requires the caller to be the document's person:

  voice_projects/{project}                      tenant_id, person, title, status
  voice_projects/{project}/sessions/{session}   tenant_id, person, state, grant_hash
  voice_live/{marker}                           the person's one live session
  voice_audit/{row}                             one row per start and end

A talk with no project names gets a new untitled project. Another tenant's
project or session, or another person's, is the same 404 as a missing one
(invariant 9): nothing in a refusal says the document exists.

ENDING BY SESSION ID. The Talk pane ends a session by its id alone, so `end`
finds the project: it lists the projects whose `tenant_id` and `person` are
the caller's (two equality filters, served by the single-field indexes) and
reads that session id under each. A session under anyone else's project is
never read, so it is the same 404 as a missing one.

ONE LIVE SESSION PER PERSON (§3.7). `voice_live/{marker}` names the person's
live session and is read and written in the same transaction as the session
it names, so two starts racing each other cannot both pass: the second is 409
`conflict`. The marker's id is an HMAC of the email under the
deployment's settings key when one is configured, so no worker can compute
it; a session left `starting` by a crash longer than STARTING_STALE_SECONDS
ago no longer holds it.

THE GRANT (§3.6). Each session gets a random 256-bit grant. Only its SHA-256
is stored, on the session document; the grant itself goes to swarm-voice in
the start call and nowhere else, and is never logged or returned to the
browser. `swarm_api/voice/grant.py` accepts it from swarm-voice on the
allowlisted routes while the session is live; ending a session removes the
hash, so the grant dies with it.

SWARM-VOICE'S INTERNAL CALLS. swarm-api POSTs, with a Google ID token for
`voice_audience` (else the URL) as its bearer:

  {SWARM_VOICE_URL}/internal/sessions
      {"voice_session_id", "project_id", "person", "offer", "grant"}
      -> 201 {"voice_session_id", "live_session_id", "answer": "<SDP>"}
  {SWARM_VOICE_URL}/internal/sessions/{session_id}/end
      {"session_id", "cause"} -> 200

The start body is swarm-voice's `StartRequest` (apps/swarm-voice/swarm_voice/
main.py) field for field (owner, 2026-11-02: "swarm-api follows swarm-voice").
An answer for another session id is read as a refusal; the live session's id
is kept on the session document.

THE ACCOUNT (owner, 2026-11-02: the ChatGPT plan account is reserved for
Voice it through the quota broker's "Voice it pool"). swarm-api sends no
credential: swarm-voice leases the pool account from the broker itself for
each session (`POST /v1/voice/lease`), so the start body is
{voice_session_id, person, offer, project_id, grant} and nothing here reads a
person's secret. swarm-voice's refusals of the pool reach the browser with
swarm-voice's own sentence, under the established codes:

  403 (the person is not in the Voice it pool)        -> 403 `forbidden`,
      detail.reason `voice_pool_refused`
  409 `voice_pool_busy` / `voice_pool_refused`        -> 409 `conflict`,
      detail.reason the code swarm-voice sent

Either way the session is ended at once (cause `voice_pool_refused`) and the
person's marker freed, so the next try is not a 409 of ours.

THE MODELS (the owner, 6 November 2026: "wire-in-follow-up", "front-reads-it").
Voice it › Settings is read once, before the start transaction, and both
start bodies (live and text) carry what it names:

  planner_model       the planner class's pin
                      (`VoiceSettings.session_planner_model` over swarm-api's
                      `worker_class_models`)
  conversation_model  the fast front's model: the curated entry's id
                      (`speech_catalogue.CONVERSATION_MODELS`)

Each is omitted when the setting is unset, or names an entry the catalogue no
longer lists or a class with no pin of its own, so swarm-voice keeps its pin
(VOICE_PLANNER_MODEL, VOICE_FRONT_MODEL). swarm-voice keeps both for the
session, so a change reaches the next session's start, never a running one.

THE ENGINE (docs/design/voice-it-own-speech.md §4.1, §10; pick 7: "Start
uses exactly the engine in Settings"). The browser's Start names no engine and
no mode. `pick_engine` reads Voice it › Settings' `engine`:

  own        `speech`: the session opens, swarm-voice starts the fast Claude
             front (no offer, `mode: "speech"`), then the speech machine's
             lease is taken and the instance brought to RUNNING
             (speech_machine.SpeechMachine.start, swarm-api's four-minute
             wait). The OpenAI pool is never asked.
  gpt-live   `live`, today's GPT-Live path, when the broker's Voice it pool
             holds an OpenAI API key. With none (`GET /v1/voice/pool` answers
             `no_voice_account_registered`) the start is refused, 409
             `conflict` with `detail.reason` `gpt_live_no_openai_key`, before
             anything is written: no other account, plan or person's key is
             tried. A broker that cannot be asked keeps the live path, and
             swarm-voice's own lease then says why.

TEXT MODE IS THE FALLBACK, never an engine Settings choose (§10.1). An own
speech session becomes text when its engine cannot start, with the reason as
a fixed code in `fallback: {from: "own", reason}` on the session document and
in the start's answer:

  speech_not_deployed   the deployment runs no speech machine (SWARM_SPEECH_*
                        empty), so no session is opened as speech at all
  front_refused         swarm-voice's 409 `voice_front_refused`: no Claude
                        account free for the front (a refusal, never a wait)
  no_l4                 the zone has no L4 free; no other zone is tried (Q1)
  not_ready             the machine was not RUNNING inside the four minutes

The session is not re-opened: its document turns `mode: "text"`, the front
(if it started) is ended at swarm-voice, and swarm-voice's text start runs on
the same session id. A speech start never builds swarm-voice's text
conversation; only this fallback's own text start does. A machine another
session holds is a refusal (409, `speech_machine_in_use`), not a fallback:
the session is ended and the person freed. While its machine starts, a
`speech` session holds the person for SPEECH_STARTING_STALE_SECONDS.

A text or speech start sends `{voice_session_id, project_id, person, grant,
mode}` with no offer; swarm-voice answers `201 {voice_session_id, mode}`. The
browser then posts each turn as text (a speech session's come from the audio
relay too):

  {SWARM_VOICE_URL}/internal/sessions/{session_id}/turns
      {"text"} -> 200 {"turn_id", "reply", "cards", "delegation_id"?}

`delegation_id` is set when the fast front handed the turn to the planner;
`VoiceSessions.delegation` reads the planner's spoken line from
`{SWARM_VOICE_URL}/delegations/{id}?wait=<seconds>`, only for that session.
Ending a speech session also stops its machine, under the lease's generation.

swarm-voice's 404 (it holds no text conversation for the session: ended
there, or lost to a restart) reads as 409 `conflict`, reason
`voice_session_not_live`; its 429 `usage_limit_reached` as 429
`rate_limited` with `detail.resets_at`. Neither the text nor the reply is
logged or stored here.

A start swarm-voice refuses or does not answer ends the session at once with
the cause `voice_unreachable` and frees the person's marker, so the next try
is not a 409. An end swarm-voice does not hear is logged and the session
still ends: what stops OpenAI's billing then is swarm-voice's own idle stop
(§3.10).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from ..errors import ApiError, Conflict, Forbidden, NotFound, RateLimited, UpstreamUnavailable
from . import speech_catalogue as cat

log = logging.getLogger(__name__)

PROJECTS = "voice_projects"
SESSIONS = "sessions"
LIVE_MARKERS = "voice_live"
AUDIT = "voice_audit"

#: A new talk's project title until the planner names it (§3.11).
UNTITLED = "Untitled"

STARTING = "starting"
LIVE = "live"
ENDED = "ended"
#: The states that hold the person's one live session.
HOLDING = frozenset({STARTING, LIVE})

#: A session still `starting` this long after it was created was left by a
#: crash between the start transaction and swarm-voice's answer, and no
#: longer blocks the person's next start. swarm-voice's start call times out
#: well inside it.
STARTING_STALE_SECONDS = 120
#: The same for a `speech` session, which stays `starting` while swarm-api
#: brings the speech machine to RUNNING: speech_machine.READY_SECONDS (240),
#: plus swarm-voice's start call (START_TIMEOUT_SECONDS) and a margin.
SPEECH_STARTING_STALE_SECONDS = 330

END_BY_PERSON = "ended_by_person"
END_VOICE_UNREACHABLE = "voice_unreachable"
END_VOICE_POOL_REFUSED = "voice_pool_refused"
#: Another session holds the speech machine (speech_machine.MachineInUse).
END_MACHINE_IN_USE = "speech_machine_in_use"

#: A session's modes: GPT-Live over WebRTC on an OpenAI API key, or the ChatGPT
#: plan's text conversation (owner, 4 November 2026: "both paths").
MODE_LIVE = "live"
MODE_TEXT = "text"
#: Our own speech: the speech machine's ears and mouth, the fast Claude front's
#: conversation (docs/design/voice-it-own-speech.md §10.1).
MODE_SPEECH = "speech"
MODES = (MODE_LIVE, MODE_TEXT, MODE_SPEECH)
#: The modes whose turns are posted as text to swarm-voice's `/turns`.
TURN_MODES = frozenset({MODE_TEXT, MODE_SPEECH})
#: Voice it › Settings' engines (speech_catalogue.ENGINE_OWN / ENGINE_GPT_LIVE).
ENGINE_MODES = {cat.ENGINE_OWN: MODE_SPEECH, cat.ENGINE_GPT_LIVE: MODE_LIVE}
#: Why an own-speech session became text: fixed codes, written to the session's
#: `fallback.reason` and the start's answer (§4.1). `no_l4` and `not_ready` are
#: speech_machine's FALLBACK_NO_L4 and FALLBACK_NOT_READY.
FALLBACK_NOT_DEPLOYED = "speech_not_deployed"
FALLBACK_FRONT_REFUSED = "front_refused"
FALLBACK_NO_L4 = "no_l4"
FALLBACK_NOT_READY = "not_ready"
FALLBACK_REASONS = frozenset({FALLBACK_NOT_DEPLOYED, FALLBACK_FRONT_REFUSED, FALLBACK_NO_L4,
                              FALLBACK_NOT_READY})
#: swarm-voice's 409 code for a speech start whose fast front could not lease
#: a Claude account (swarm_voice.main.FRONT_REFUSED).
FRONT_REFUSED_CODE = "voice_front_refused"
#: `detail.reason` of the 409 for a GPT-Live start with no OpenAI API key.
NO_OPENAI_KEY = "gpt_live_no_openai_key"
#: The broker's reason for a Voice it pool with no account that can open a Live
#: session, i.e. no OpenAI API key: the one answer that makes a session text.
NO_LIVE_ACCOUNT = "no_voice_account_registered"
#: A text turn, as the console sends it (swarm_voice.textchat.MAX_TURN_CHARS).
MAX_TURN_CHARS = 4000
TURN_TIMEOUT_SECONDS = 300.0
#: `detail.reason` of the 409 for a turn on a session that takes none.
NOT_LIVE = "voice_session_not_live"
USAGE_LIMIT = "usage_limit_reached"

#: swarm-voice's 409 codes for the Voice it pool, carried to the browser as
#: `detail.reason` (a 403 from swarm-voice is always `voice_pool_refused`).
POOL_BUSY = "voice_pool_busy"
POOL_REFUSED = "voice_pool_refused"
POOL_REASONS = frozenset({POOL_BUSY, POOL_REFUSED})
#: What a start is told when the Voice it pool holds no OpenAI API key. Every
#: Voice it session is a GPT-Live session, which only a developer API key can
#: open (OpenAI refused the ChatGPT plan's token, 403 on /v1/live/sessions, 4
#: November 2026), so the broker refuses the lease BEFORE taking any hold and
#: swarm-voice answers 409 `voice_pool_refused`. That code means exactly this,
#: so the person reads where to add one, whatever older sentence swarm-voice
#: sent, and never a 502.
NO_API_KEY_SENTENCE = (
    "Voice it needs an OpenAI API key in the Voice it pool: add one on Admin \u203a Accounts")
#: A refusal sentence longer than this is cut: it is shown, not parsed.
MAX_REFUSAL_MESSAGE = 500
#: swarm-voice's 502 when OpenAI refused the Live session: it carries OpenAI's
#: status and its error's code and type, never the error's message.
OPENAI_REFUSED = "openai_refused"
#: The pattern swarm-voice holds OpenAI's code and type to (swarm_voice.live), held
#: again here: anything else reads as "unreadable".
OPENAI_ERROR_TOKEN = re.compile(r"[a-z0-9_.-]{1,64}")

AUDIT_STARTED = "voice_session_started"
AUDIT_ENDED = "voice_session_ended"

#: An SDP offer or answer larger than this is not one a browser made.
MAX_SDP_BYTES = 64 * 1024

START_TIMEOUT_SECONDS = 20.0
#: The longest a delegation is waited on: swarm-voice's
#: claudefront.MAX_WAIT_SECONDS, the planner's turn.
DELEGATION_WAIT_SECONDS = 240.0
END_TIMEOUT_SECONDS = 5.0

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)

_MARKER_PURPOSE = "voice-live:"


#: The two refusals keep the established codes, `forbidden` and `conflict`
#: (tests/unit/control_plane/test_new_refusals_ship_off.py): a new code would
#: have to ship behind a switch, and a switch that lets a stranger start a
#: session, or a person hold two, is the breach itself.
class VoiceNotAllowed(Forbidden):
    pass


class VoiceUnavailable(UpstreamUnavailable):
    code = "voice_unavailable"


class VoicePoolRefused(Forbidden):
    """swarm-voice's 403: the person is not in the Voice it pool. Keeps the
    established `forbidden` code; `detail.reason` is `voice_pool_refused`."""


class VoicePoolBusy(Conflict):
    """swarm-voice's 409: the Voice it pool account is busy or refused the
    lease. Keeps the established `conflict` code; `detail.reason` is the code
    swarm-voice sent (`voice_pool_busy` or `voice_pool_refused`)."""


#: Either pool refusal; `start` ends the session on both.
VoicePoolUnavailable = (VoicePoolRefused, VoicePoolBusy)


class VoiceSessionLive(Conflict):
    pass


class VoiceNoOpenAIKey(Conflict):
    """Settings name GPT-Live and the Voice it pool holds no OpenAI API key.
    Keeps the established `conflict` code; `detail.reason` is
    `gpt_live_no_openai_key`. Nothing was written or started."""


class SpeechFrontRefused(Exception):
    """swarm-voice's 409 `voice_front_refused`: no Claude account for the fast
    front. Never reaches the browser: the session falls back to text."""


class VoiceUnreachable(ApiError):
    status_code = 502
    code = "voice_unreachable"


class VoiceSessionNotLive(Conflict):
    """A turn on a session that is not a live text conversation. Keeps the
    established `conflict` code; `detail.reason` is `voice_session_not_live`."""


class VoiceUsageLimited(RateLimited):
    """The ChatGPT plan's usage limit refused a turn. Keeps the established
    `rate_limited` code; `detail` adds `reason` and `resets_at`."""

    def __init__(self, resets_at: str | None, *, now: datetime) -> None:
        when = _instant(resets_at)
        wait = max(0.0, (when - now).total_seconds()) if when is not None and now.tzinfo else 0.0
        super().__init__(
            "Voice it's ChatGPT plan has reached its usage limit"
            + (f"; it resets at {when.isoformat()}." if when is not None else "; try again later."),
            retry_after_seconds=wait)
        self.detail.update({"reason": USAGE_LIMIT, "resets_at": when.isoformat() if when else None})


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def not_allowed() -> VoiceNotAllowed:
    return VoiceNotAllowed(
        "Voice it is open only to the people this deployment names "
        "(VOICE_IT_PEOPLE, else the platform owner). Nothing was started.")


def unavailable() -> VoiceUnavailable:
    return VoiceUnavailable(
        "Voice it is unavailable: swarm-voice is not deployed here "
        "(SWARM_VOICE_URL is unset). Nothing was started.")


def _refusal_body(raw: bytes) -> tuple[str, str]:
    """(code, message) from swarm-voice's refusal body: `{code, message}`, or
    FastAPI's `{detail: {code, message}}` / `{detail: "<sentence>"}`."""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = None
    if isinstance(body, dict) and "detail" in body and "message" not in body:
        body = body["detail"]
    if isinstance(body, str):
        return "", body.strip()[:MAX_REFUSAL_MESSAGE]
    if not isinstance(body, dict):
        return "", ""
    code = body.get("code") if isinstance(body.get("code"), str) else ""
    message = body.get("message") if isinstance(body.get("message"), str) else ""
    return code.strip(), message.strip()[:MAX_REFUSAL_MESSAGE]


def pool_refusal(status: int, raw: bytes) -> VoicePoolRefused | VoicePoolBusy | None:
    """The browser's refusal for swarm-voice's 403, or its 409 naming a pool
    code, carrying its sentence. None for any other answer (a 409 for the
    voice session already being live is not the pool's, and reads as the
    generic refusal)."""
    code, message = _refusal_body(raw)
    if status == 403:
        return VoicePoolRefused(
            message or "You are not in the Voice it pool, so no ChatGPT account can be "
            "leased for you. Nothing was started.", detail={"reason": POOL_REFUSED})
    if status != 409 or code not in POOL_REASONS:
        return None
    reason = code
    if code == POOL_REFUSED:
        return VoicePoolBusy(NO_API_KEY_SENTENCE, detail={"reason": reason})
    return VoicePoolBusy(
        message or "The Voice it pool's ChatGPT account is busy; try again shortly. "
        "Nothing was started.", detail={"reason": reason})


def openai_refusal(raw: bytes) -> VoiceUnreachable | None:
    """The browser's refusal for swarm-voice's 502 `openai_refused`, saying what
    OpenAI answered: its status and its error's code and type. None for any other
    502 body."""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        body = body["detail"]
    if not isinstance(body, dict) or body.get("code") != OPENAI_REFUSED:
        return None

    def kept(value: Any) -> str:
        return value if isinstance(value, str) and OPENAI_ERROR_TOKEN.fullmatch(value) else "unreadable"

    status = body.get("openai_status")
    status = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None
    code, kind = kept(body.get("openai_code")), kept(body.get("openai_type"))
    said = [str(status) if status is not None else "on the sideband", code]
    if kind not in ("unreadable", code):
        said.append(kind)
    return VoiceUnreachable(
        f"OpenAI refused the Live session for the Voice it account ({', '.join(said)}); "
        "the session was ended.",
        detail={"reason": OPENAI_REFUSED, "openai_status": status, "openai_code": code, "openai_type": kind})


def is_allowed(people: tuple[str, ...], email: str) -> bool:
    who = (email or "").strip().lower()
    return bool(who) and who in {p.strip().lower() for p in people if p.strip()}


def grant_hash(grant: str) -> str:
    return hashlib.sha256(grant.encode("ascii")).hexdigest()


def marker_id(key: str, email: str) -> str:
    """`vl_` + 32 hex: an HMAC of the email under the settings key, or its
    SHA-256 where none is configured (a test, a local run)."""
    message = (_MARKER_PURPOSE + email.strip().lower()).encode("utf-8")
    if key:
        digest = hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(message).hexdigest()
    return "vl_" + digest[:32]


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# swarm-voice
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StartCall:
    session_id: str
    project_id: str
    #: Kept for the caller's own record; swarm-voice's body has no tenant.
    tenant_id: str
    person: str
    #: The browser's SDP offer; empty for a text session, which has none.
    offer: str
    #: Excluded from the repr: it is the session's credential.
    grant: str
    mode: str = MODE_LIVE
    #: Voice it › Settings' models, read at the start; None leaves swarm-voice
    #: on its pin, and the field out of the body.
    planner_model: str | None = None
    conversation_model: str | None = None

    def __repr__(self) -> str:
        return (f"StartCall(session_id={self.session_id!r}, project_id={self.project_id!r}, "
                f"tenant_id={self.tenant_id!r})")


@dataclass(frozen=True)
class StartAnswer:
    """What swarm-voice's 201 carries besides the session id it echoes."""

    answer: str
    live_session_id: str
    mode: str = MODE_LIVE


@dataclass(frozen=True)
class TurnAnswer:
    """swarm-voice's answer to one text turn."""

    turn_id: str
    reply: str
    cards: list[dict[str, Any]]
    #: The fast front's delegation to the planner, on a speech session.
    delegation_id: str | None = None


@dataclass(frozen=True)
class EnginePick:
    """What a Start runs: Settings' engine, the session's mode, and the
    fallback's fixed code when the mode is text."""

    engine: str
    mode: str
    fallback: str | None = None


def no_openai_key() -> VoiceNoOpenAIKey:
    return VoiceNoOpenAIKey(
        "Voice it's Settings name GPT-Live, and its pool holds no OpenAI API key: add one in "
        "Voice it \u203a Settings. Nothing was started.", detail={"reason": NO_OPENAI_KEY})


def pick_engine(chosen: Mapping[str, Any], pool: Any, person: str, *,
                speech_deployed: bool) -> EnginePick:
    """The engine Voice it › Settings name (`chosen`, settings.effective's
    view, whose default is own speech), with no choice at Start.

    own speech is `speech`, or `text` with `speech_not_deployed` when the
    deployment runs no speech machine; the OpenAI pool is never asked.
    GPT-Live is `live`, or `VoiceNoOpenAIKey` when the broker says the pool
    holds no OpenAI API key; a broker that cannot be asked keeps `live`, and
    swarm-voice's own lease refuses properly."""
    engine = chosen.get("engine")
    engine = engine if engine in ENGINE_MODES else cat.DEFAULTS["engine"]
    if engine == cat.ENGINE_OWN:
        if not speech_deployed:
            return EnginePick(engine, MODE_TEXT, FALLBACK_NOT_DEPLOYED)
        return EnginePick(engine, MODE_SPEECH)
    try:
        answer = pool.voice_pool(person)
    except Exception as exc:  # noqa: BLE001 -- the class name only; the Live path refuses properly
        log.warning("the Voice it pool could not be read for GPT-Live (%s); live", type(exc).__name__)
        return EnginePick(engine, MODE_LIVE)
    reason = answer.get("reason") if isinstance(answer, dict) else None
    if reason == NO_LIVE_ACCOUNT:
        raise no_openai_key()
    return EnginePick(engine, MODE_LIVE)


class VoiceClient(Protocol):
    def start(self, call: StartCall) -> StartAnswer:
        """swarm-voice's answer, or `VoiceUnreachable`, or a pool refusal
        (`VoicePoolRefused`/`VoicePoolBusy`)."""
        ...

    def end(self, session_id: str, *, cause: str) -> bool:
        """True when swarm-voice took the end. Never raises."""
        ...

    def turn(self, session_id: str, text: str) -> TurnAnswer:
        """swarm-voice's answer to a text turn, or `VoiceSessionNotLive`,
        `VoiceUsageLimited` or `VoiceUnreachable`."""
        ...

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        """The planner's spoken line once the front's delegation is done, or
        None (not done in `timeout`, unknown, another session's). Never raises."""
        ...


def _metadata_id_token(audience: str, timeout: float) -> str:
    url = f"{METADATA_IDENTITY_URL}?{urllib.parse.urlencode({'audience': audience, 'format': 'full'})}"
    request = urllib.request.Request(url, method="GET", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the metadata server
        token = response.read().decode("ascii").strip()
    if not token:
        raise OSError("the metadata server returned no identity token")
    return token


class HttpVoiceClient:
    """swarm-voice's internal routes over HTTP. What is logged is the session
    id and a status, never a body: the start call carries the grant and the
    browser's offer."""

    def __init__(self, url: str, *, audience: str = "",
                 token: Callable[[str, float], str] = _metadata_id_token) -> None:
        self._base = url.strip().rstrip("/")
        self._audience = audience.strip() or self._base
        self._token = token

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, bytes]:
        bearer = self._token(self._audience, timeout)
        request = urllib.request.Request(
            self._base + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - our own internal URL
                return response.status, response.read(MAX_SDP_BYTES + 1024)
        except urllib.error.HTTPError as exc:
            # A refusal's body is swarm-voice's {code, message}: read so the
            # pool's sentence reaches the browser; never logged.
            try:
                raw = exc.read(16 * 1024) if exc.fp is not None else b""
            except OSError:
                raw = b""
            return exc.code, raw or b""

    def _get(self, path: str, timeout: float) -> tuple[int, bytes]:
        bearer = self._token(self._audience, timeout)
        request = urllib.request.Request(self._base + path, method="GET",
                                         headers={"Authorization": f"Bearer {bearer}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - our own internal URL
                return response.status, response.read(256 * 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    def start(self, call: StartCall) -> StartAnswer:
        body = {"voice_session_id": call.session_id, "project_id": call.project_id,
                "person": call.person, "offer": call.offer, "grant": call.grant}
        if call.mode in TURN_MODES:
            # No offer: a text conversation and the fast front have no WebRTC.
            # A Live body is today's, field for field (swarm-voice's `mode`
            # defaults to live).
            body = {"voice_session_id": call.session_id, "project_id": call.project_id,
                    "person": call.person, "grant": call.grant, "mode": call.mode}
        if call.planner_model is not None:
            body["planner_model"] = call.planner_model
        if call.conversation_model is not None:
            body["conversation_model"] = call.conversation_model
        try:
            status, raw = self._post("/internal/sessions", body, START_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice start for session=%s did not reach swarm-voice (%s)",
                        call.session_id, type(exc).__name__)
            raise VoiceUnreachable("swarm-voice could not be reached; the session was ended.") from None
        if call.mode == MODE_SPEECH and status == 409 and _refusal_body(raw)[0] == FRONT_REFUSED_CODE:
            log.info("swarm-voice found no Claude account for the front of session=%s", call.session_id)
            raise SpeechFrontRefused("swarm-voice could not start the fast front")
        refusal = pool_refusal(status, raw) if status in (403, 409) else None
        if refusal is not None:
            log.warning("swarm-voice refused the Voice it pool for session=%s (HTTP %s, %s)",
                        call.session_id, status, refusal.detail.get("reason"))
            raise refusal
        refused = openai_refusal(raw) if status == 502 else None
        if refused is not None:
            log.warning("OpenAI refused the Live session for session=%s (OpenAI HTTP %s, %s, %s)",
                        call.session_id, refused.detail["openai_status"], refused.detail["openai_code"],
                        refused.detail["openai_type"])
            raise refused
        if status != 201:
            log.warning("swarm-voice refused the start for session=%s (HTTP %s)", call.session_id, status)
            raise VoiceUnreachable(f"swarm-voice refused the session (HTTP {status}); it was ended.")
        try:
            started = json.loads(raw.decode("utf-8"))
        except ValueError:
            started = None
        if not isinstance(started, dict) or started.get("voice_session_id") != call.session_id:
            log.warning("swarm-voice answered the start for session=%s for another session", call.session_id)
            raise VoiceUnreachable("swarm-voice answered for another session; the session was ended.")
        if call.mode in TURN_MODES:
            if started.get("mode") != call.mode:
                raise VoiceUnreachable(f"swarm-voice did not start a {call.mode} conversation; "
                                       "the session was ended.")
            return StartAnswer(answer="", live_session_id="", mode=call.mode)
        answer, live_id = started.get("answer"), started.get("live_session_id")
        if not isinstance(answer, str) or not isinstance(live_id, str) or not live_id:
            raise VoiceUnreachable("swarm-voice answered without an SDP answer; the session was ended.")
        return StartAnswer(answer=answer, live_session_id=live_id)

    def turn(self, session_id: str, text: str) -> TurnAnswer:
        path = "/internal/sessions/" + urllib.parse.quote(session_id, safe="") + "/turns"
        try:
            status, raw = self._post(path, {"text": text}, TURN_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice turn for session=%s did not reach swarm-voice (%s)", session_id, type(exc).__name__)
            raise VoiceUnreachable("swarm-voice could not be reached for this turn.") from None
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("detail"), dict):
            body = body["detail"]
        if status == 404:
            raise VoiceSessionNotLive("This Voice it conversation is no longer live; start a new one.",
                                      detail={"reason": NOT_LIVE})
        if status == 429 and isinstance(body, dict) and body.get("code") == USAGE_LIMIT:
            log.info("the ChatGPT plan's usage limit refused a turn for session=%s", session_id)
            resets = body.get("resets_at")
            raise VoiceUsageLimited(resets if isinstance(resets, str) else None, now=datetime.now(timezone.utc))
        refused = openai_refusal(raw) if status == 502 else None
        if refused is not None:
            log.warning("OpenAI refused a text turn for session=%s (OpenAI HTTP %s, %s)", session_id,
                        refused.detail["openai_status"], refused.detail["openai_code"])
            raise VoiceUnreachable(
                "OpenAI refused this turn on Voice it's ChatGPT plan "
                f"({refused.detail['openai_status'] or 'in the stream'}, {refused.detail['openai_code']}).",
                detail=refused.detail)
        if status != 200 or not isinstance(body, dict):
            log.warning("swarm-voice refused a turn for session=%s (HTTP %s)", session_id, status)
            raise VoiceUnreachable(f"swarm-voice could not answer this turn (HTTP {status}).")
        turn_id, reply, cards = body.get("turn_id"), body.get("reply"), body.get("cards")
        if not isinstance(turn_id, str) or not isinstance(reply, str):
            raise VoiceUnreachable("swarm-voice answered the turn without a reply.")
        delegation = body.get("delegation_id")
        return TurnAnswer(turn_id=turn_id, reply=reply,
                          cards=[c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else [],
                          delegation_id=delegation if isinstance(delegation, str) and delegation else None)

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        wait = max(0.0, min(float(timeout), DELEGATION_WAIT_SECONDS))
        path = ("/delegations/" + urllib.parse.quote(delegation_id, safe="") + "?"
                + urllib.parse.urlencode({"wait": wait}))
        try:
            status, raw = self._get(path, wait + 10.0)
        except (OSError, ValueError) as exc:
            log.warning("voice delegation %s for session=%s did not reach swarm-voice (%s)",
                        delegation_id, session_id, type(exc).__name__)
            return None
        try:
            body = json.loads(raw.decode("utf-8")) if status == 200 and raw else None
        except ValueError:
            body = None
        # Another session's delegation is never spoken here, whatever its id.
        if not isinstance(body, dict) or body.get("voice_session_id") != session_id \
                or body.get("state") != "done":
            return None
        spoken = body.get("spoken")
        return spoken if isinstance(spoken, str) and spoken.strip() else None

    def end(self, session_id: str, *, cause: str) -> bool:
        path = "/internal/sessions/" + urllib.parse.quote(session_id, safe="") + "/end"
        try:
            status, _ = self._post(path, {"session_id": session_id, "cause": cause},
                                   END_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice end for session=%s did not reach swarm-voice (%s)",
                        session_id, type(exc).__name__)
            return False
        if status != 200:
            log.warning("swarm-voice refused the end for session=%s (HTTP %s)", session_id, status)
        return status == 200


def client_for(settings: Any) -> VoiceClient | None:
    """The deployment's client: none without SWARM_VOICE_URL."""
    url = str(getattr(settings, "voice_url", "") or "").strip()
    if not url:
        return None
    return HttpVoiceClient(url, audience=str(getattr(settings, "voice_audience", "") or ""))


# --------------------------------------------------------------------------
# The documents
# --------------------------------------------------------------------------


def _live_detail(marker: dict[str, Any] | None, tenant_id: str) -> dict[str, Any]:
    """Which session holds the person, so the console can end it; named only
    when it is in the tenant asking (invariant 9)."""
    if not marker or marker.get("tenant_id") != tenant_id:
        return {}
    return {"project_id": marker.get("project_id"), "session_id": marker.get("session_id")}


def _valid_answer(answer: str) -> bool:
    return answer.startswith("v=0") and len(answer.encode("utf-8")) <= MAX_SDP_BYTES


class VoiceSessions:
    """Starts and ends one person's sessions in one tenant."""

    def __init__(self, db: Any, client: VoiceClient, *, marker_key: str = "",
                 now: Callable[[], datetime],
                 class_models: Mapping[str, str] | None = None,
                 machine: Any = None) -> None:
        self._db = db
        self._client = client
        self._marker_key = marker_key
        self._now = now
        #: swarm-api's `worker_class_models`; None reads WORKER_CLASS_MODELS,
        #: the variable `Settings.worker_class_models` is parsed from.
        self._class_models = class_models
        #: The speech machine's lifecycle (speech_machine.SpeechMachine), or
        #: None where the deployment runs none: a speech start then falls back.
        self._machine = machine

    def _session_models(self) -> tuple[str | None, str | None]:
        """(planner model, conversation model) for a session starting now,
        each None where the setting leaves swarm-voice on its pin."""
        from .settings import COLLECTION, DOCUMENT, VoiceSettings  # settings imports this module

        pins = cat.configured_class_models() if self._class_models is None else self._class_models
        planner = VoiceSettings(self._db, now=self._now).session_planner_model(pins)
        snap = self._db.collection(COLLECTION).document(DOCUMENT).get()
        stored = (snap.to_dict() or {}).get("conversation_model") if snap.exists else None
        conversation = next((m.model for m in cat.CONVERSATION_MODELS if m.name == stored), None)
        return planner, conversation

    def _project_ref(self, project_id: str) -> Any:
        return self._db.collection(PROJECTS).document(project_id)

    def _session_ref(self, project_id: str, session_id: str) -> Any:
        return self._project_ref(project_id).collection(SESSIONS).document(session_id)

    def _audit(self, txn: Any, *, action: str, tenant_id: str, person: str,
               project_id: str, session_id: str, detail: dict[str, Any]) -> None:
        txn.set(self._db.collection(AUDIT).document(), {
            "action": action, "tenant_id": tenant_id, "by": person, "at": self._now(),
            "project_id": project_id, "session_id": session_id, "detail": detail,
        })

    @staticmethod
    def _mine(data: dict[str, Any] | None, tenant_id: str, person: str) -> bool:
        return bool(data) and data.get("tenant_id") == tenant_id and data.get("person") == person

    def _holds(self, txn: Any, marker: dict[str, Any] | None) -> bool:
        """Whether the marker still names a live (or freshly starting) session."""
        if not marker or not marker.get("project_id") or not marker.get("session_id"):
            return False
        snap = _snapshot(txn.get(self._session_ref(marker["project_id"], marker["session_id"])))
        if not snap.exists:
            return False
        session = snap.to_dict() or {}
        state = session.get("state")
        if state == LIVE:
            return True
        if state == STARTING:
            created = _aware(session.get("created_at"))
            stale = SPEECH_STARTING_STALE_SECONDS if session.get("mode") == MODE_SPEECH \
                else STARTING_STALE_SECONDS
            return created is None or self._now() - created < timedelta(seconds=stale)
        return False

    def start(self, *, tenant_id: str, person: str, offer: str,
              project_id: str | None, mode: str = MODE_LIVE, fallback: str | None = None,
              chosen: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """No credential: swarm-voice leases the pool accounts itself. `mode`
        and `fallback` are `pick_engine`'s; `chosen` is Settings' view, which
        the speech machine's start applies. A text or speech session sends no
        offer. A speech session that cannot start becomes text, with its
        reason (`fallback` in the answer)."""
        if mode not in MODES:
            raise ValueError(f"no Voice it session mode {mode!r}")
        if fallback is not None and (mode != MODE_TEXT or fallback not in FALLBACK_REASONS):
            raise ValueError(f"no Voice it fallback {fallback!r} for mode {mode!r}")
        if mode == MODE_SPEECH and self._machine is None:
            mode, fallback = MODE_TEXT, FALLBACK_NOT_DEPLOYED
        # Read before anything is written, so a failed read leaves nothing held.
        planner_model, conversation_model = self._session_models()
        person = person.strip().lower()
        session_id = "vs_" + secrets.token_hex(16)
        new_project = project_id is None
        project_id = project_id or "vp_" + secrets.token_hex(16)
        grant = secrets.token_urlsafe(32)
        marker_ref = self._db.collection(LIVE_MARKERS).document(marker_id(self._marker_key, person))
        project_ref = self._project_ref(project_id)
        session_ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _open(txn: Any) -> None:
            marker_snap = _snapshot(txn.get(marker_ref))
            project_snap = None if new_project else _snapshot(txn.get(project_ref))
            if project_snap is not None and not (
                    project_snap.exists and self._mine(project_snap.to_dict(), tenant_id, person)):
                raise NotFound(f"voice project {project_id!r} not found")
            marker = marker_snap.to_dict() if marker_snap.exists else None
            if self._holds(txn, marker):
                raise VoiceSessionLive(
                    "You already have a live Voice it session; end it before starting another.",
                    detail=_live_detail(marker, tenant_id))
            now = self._now()
            if new_project:
                txn.set(project_ref, {
                    "tenant_id": tenant_id, "person": person, "title": UNTITLED,
                    "status": "active", "created_at": now, "updated_at": now})
            else:
                txn.update(project_ref, {"updated_at": now})
            fields = {
                "tenant_id": tenant_id, "person": person, "project_id": project_id,
                "state": STARTING, "created_at": now, "grant_hash": grant_hash(grant),
                "mode": mode}
            if fallback is not None:
                fields["fallback"] = {"from": cat.ENGINE_OWN, "reason": fallback, "at": now}
            txn.set(session_ref, fields)
            txn.set(marker_ref, {
                "tenant_id": tenant_id, "project_id": project_id,
                "session_id": session_id, "at": now})
            self._audit(txn, action=AUDIT_STARTED, tenant_id=tenant_id, person=person,
                        project_id=project_id, session_id=session_id,
                        detail={"new_project": new_project})

        _open(self._db.transaction())

        def call_for(this_mode: str) -> StartCall:
            return StartCall(session_id=session_id, project_id=project_id, tenant_id=tenant_id,
                             person=person, offer=offer if this_mode == MODE_LIVE else "", grant=grant,
                             mode=this_mode, planner_model=planner_model,
                             conversation_model=conversation_model)

        def close(cause: str) -> None:
            self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                        session_id=session_id, cause=cause)

        try:
            if mode == MODE_SPEECH:
                fallback = self._start_speech(
                    call_for(MODE_SPEECH), chosen=chosen or {}, tenant_id=tenant_id, close=close)
                if fallback is not None:
                    mode = MODE_TEXT
                    self._session_ref(project_id, session_id).update(
                        {"mode": MODE_TEXT,
                         "fallback": {"from": cat.ENGINE_OWN, "reason": fallback, "at": self._now()}})
                    log.info("voice session %s falls back to text (%s)", session_id, fallback)
            if mode != MODE_SPEECH:
                started = self._client.start(call_for(mode))
                if not isinstance(started, StartAnswer):
                    raise VoiceUnreachable("swarm-voice did not answer the start; the session was ended.")
                if mode == MODE_TEXT and started.mode != MODE_TEXT:
                    raise VoiceUnreachable("swarm-voice did not start a text conversation; the session was ended.")
                if mode == MODE_LIVE and not _valid_answer(started.answer):
                    raise VoiceUnreachable("swarm-voice's answer is not an SDP answer; the session was ended.")
        except VoiceUnreachable:
            close(END_VOICE_UNREACHABLE)
            raise
        except VoicePoolUnavailable:
            close(END_VOICE_POOL_REFUSED)
            raise
        live_fields: dict[str, Any] = {"state": LIVE, "live_at": self._now()}
        if mode == MODE_LIVE:
            live_fields["live_session_id"] = started.live_session_id
        self._session_ref(project_id, session_id).update(live_fields)
        log.info("voice session started tenant=%s project=%s session=%s new_project=%s mode=%s",
                 tenant_id, project_id, session_id, new_project, mode)
        answer = {"project_id": project_id, "session_id": session_id, "state": LIVE,
                  "new_project": new_project, "mode": mode}
        if mode == MODE_LIVE:
            answer["answer_sdp"] = started.answer
        if fallback is not None:
            answer["fallback"] = {"from": cat.ENGINE_OWN, "reason": fallback}
        return answer

    def _start_speech(self, call: StartCall, *, chosen: Mapping[str, Any], tenant_id: str,
                      close: Callable[[str], None]) -> str | None:
        """The front at swarm-voice, then the speech machine to RUNNING. None
        when both started; else the fallback's fixed code, with the front
        ended and the machine's lease released. A machine another session
        holds, or a failure that is no fallback, ends the session and raises."""
        from .speech_machine import MachineInUse, SessionRef, SpeechFallback  # it imports this module

        try:
            started = self._client.start(call)
        except SpeechFrontRefused:
            return FALLBACK_FRONT_REFUSED
        if not isinstance(started, StartAnswer) or started.mode != MODE_SPEECH:
            raise VoiceUnreachable("swarm-voice did not start the speech conversation; the session was ended.")
        session = SessionRef(tenant_id=tenant_id, person=call.person, project_id=call.project_id,
                             session_id=call.session_id)
        try:
            self._machine.start(session, chosen=chosen)
        except SpeechFallback as exc:
            reason = exc.reason if exc.reason in FALLBACK_REASONS else FALLBACK_NOT_READY
            # The front holds a Claude account: it ends before the text start.
            self._client.end(call.session_id, cause=reason)
            return reason
        except MachineInUse:
            close(END_MACHINE_IN_USE)
            self._client.end(call.session_id, cause=END_MACHINE_IN_USE)
            raise
        except Exception:
            close(END_VOICE_UNREACHABLE)
            self._client.end(call.session_id, cause=END_VOICE_UNREACHABLE)
            raise
        return None

    def turn(self, *, tenant_id: str, person: str, session_id: str, text: str) -> dict[str, Any]:
        """One spoken turn of a text session, as text: swarm-voice's reply and
        the cards the planner drew. 404 for a session that is not this
        person's in this tenant; 409 (`voice_session_not_live`) for one that is
        ended, still starting, or a Live (WebRTC) one. A speech session's turn
        is the fast front's at swarm-voice, never the text conversation's, and
        carries `delegation_id` when the front delegated. The text and the
        reply are neither logged nor stored here."""
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        snap = _snapshot(self._session_ref(project_id, session_id).get())
        data = (snap.to_dict() or {}) if snap.exists else {}
        if not self._mine(data, tenant_id, person):
            raise NotFound(f"voice session {session_id!r} not found")
        if data.get("state") != LIVE or data.get("mode") not in TURN_MODES:
            raise VoiceSessionNotLive(
                "This Voice it session takes no text turns: it is not a live text or speech conversation.",
                detail={"reason": NOT_LIVE, "state": data.get("state"), "mode": data.get("mode") or MODE_LIVE})
        answered = self._client.turn(session_id, text)
        log.info("voice turn answered tenant=%s project=%s session=%s turn=%s cards=%d",
                 tenant_id, project_id, session_id, answered.turn_id, len(answered.cards))
        said: dict[str, Any] = {"turn_id": answered.turn_id, "reply": answered.reply, "cards": answered.cards}
        delegation = getattr(answered, "delegation_id", None)
        if delegation:
            said["delegation_id"] = delegation
        return said

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        """The planner's spoken line for the fast front's delegation, read
        from swarm-voice's `GET /delegations/{id}` for this session only
        (speech_relay.Turns' `delegation`); None when there is none yet."""
        ask = getattr(self._client, "delegation", None)
        if not callable(ask):
            return None
        line = ask(session_id, delegation_id, timeout=timeout)
        return line if isinstance(line, str) and line.strip() else None

    def _close(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
               cause: str) -> dict[str, Any] | None:
        """Mark the session ended and free the person's marker when it names
        it; the session's data before, or None when it was already ended.
        `NotFound` when it is not this person's in this tenant."""
        session_ref = self._session_ref(project_id, session_id)
        marker_ref = self._db.collection(LIVE_MARKERS).document(marker_id(self._marker_key, person))

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(session_ref))
            marker_snap = _snapshot(txn.get(marker_ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person):
                raise NotFound(f"voice session {session_id!r} not found")
            if data.get("state") == ENDED:
                return None
            now = self._now()
            # The grant dies with the session (§3.6): its hash goes, so no
            # presented grant can match it again, whatever the state reads.
            txn.update(session_ref, {"state": ENDED, "ended_at": now, "end_cause": cause,
                                     "grant_hash": None})
            marker = marker_snap.to_dict() if marker_snap.exists else None
            if marker and marker.get("session_id") == session_id \
                    and marker.get("project_id") == project_id:
                txn.delete(marker_ref)
            self._audit(txn, action=AUDIT_ENDED, tenant_id=tenant_id, person=person,
                        project_id=project_id, session_id=session_id, detail={"cause": cause})
            return data

        return _apply(self._db.transaction())

    def _project_of(self, *, tenant_id: str, person: str, session_id: str) -> str:
        """The project of this person's session in this tenant; `NotFound`
        when no project of theirs holds it."""
        projects = (self._db.collection(PROJECTS)
                    .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    .where(filter=FieldFilter("person", "==", person))
                    .stream())
        # get_all does not keep the order it was given: match by path.
        refs = {project.id: self._session_ref(project.id, session_id) for project in projects}
        owner = {ref.path: project_id for project_id, ref in refs.items()}
        for snap in (self._db.get_all(list(refs.values())) if refs else ()):
            if snap.exists and self._mine(snap.to_dict(), tenant_id, person):
                return owner[snap.reference.path]
        raise NotFound(f"voice session {session_id!r} not found")

    def end(self, *, tenant_id: str, person: str, session_id: str) -> dict[str, Any]:
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        before = self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                             session_id=session_id, cause=END_BY_PERSON)
        told = None
        if before is not None:
            told = self._client.end(session_id, cause=END_BY_PERSON)
            if before.get("mode") == MODE_SPEECH and self._machine is not None:
                # Stop (§4.5): the machine stops with the session, fenced by
                # the lease's generation; none held is nothing to stop.
                holder = self._machine.holder_of(session_id)
                if holder is not None:
                    self._machine.stop(holder, cause=END_BY_PERSON)
            log.info("voice session ended tenant=%s project=%s session=%s voice_told=%s",
                     tenant_id, project_id, session_id, told)
        return {"project_id": project_id, "session_id": session_id, "state": ENDED,
                "changed": before is not None, "voice_told": told}
