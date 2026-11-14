"""swarm-voice: GPT-Live sessions for Voice it (docs/design/voice-it.md §3.2, §3.4;
section 10, phase one).

A one-replica StatefulSet in `swarm-system` on the GKE cluster (§3.4), so a
voice session's sideband is held by exactly one process for the length of the
conversation, which no request-shaped service can do. It holds no data
authority: no Firestore client, no storage client. The browser never reaches it;
swarm-api calls it inside the VPC.

Routes:

* `POST /internal/sessions` -- swarm-api starts a voice session. Body:
  `voice_session_id` (swarm-api's session id), `person` (the signed-in person's
  email) and `offer` (the browser's SDP offer). swarm-voice leases a ChatGPT
  plan account from the quota broker's Voice it pool for `person`
  (accountlease.py; owner, 2026-11-02: "pool only, wait for it"), creates the
  GPT-Live session on that lease, attaches its sideband and answers
  `201 {voice_session_id, live_session_id, answer}`. When the broker leases
  nothing, nothing starts: 403 `{"code": "voice_pool_refused", "message"}` for
  a person outside the pool, 409 `{"code": "voice_pool_busy" |
  "voice_pool_refused", "message"}` when its account is in use or none is
  registered, `message` being one sentence the voice can say. 409 also when
  the voice session already has a live one; 502 when OpenAI or the broker
  failed. When OpenAI itself refused, the 502 says why: `{"code":
  "openai_refused", "message", "openai_status", "openai_code", "openai_type"}`,
  OpenAI's error code and type held to a closed pattern (live.openai_error;
  never its message, which may quote the request), `openai_status` null for a
  refusal sent on the sideband. The lease is released when the session ends, when
  its start fails and when OpenAI closes it; if swarm-voice dies, the broker's
  hold expires on its own (ten-minute TTL, renewed every four minutes).
  No person's own ChatGPT sign-in is read: there is no Secret Manager path for
  the OpenAI credential any more.
  Optionally, together, `project_id` and `grant`: the session grant
  (grant.py, docs/design/voice-it.md §3.6), held in memory on the live session
  until it ends and presented only to swarm-api. A grant without a project, a
  grant for another session or one swarm-api did not mint is 422, and the
  refusal never carries it.
  `mode` (default `live`) is what swarm-api picked from the pool: `text` when
  the Voice it pool holds no OpenAI API key (owner, 4 November 2026: "both
  paths"). A text start carries no offer: swarm-voice leases the pool's
  ChatGPT plan (`capability: text`), holds the conversation (textchat.py) and
  answers `201 {voice_session_id, mode: "text"}`, with no SDP; the pool
  refusals have the same status and codes, but name the plan, never the key
  (TEXT_POOL_REFUSALS). Every pool refusal also carries the broker's
  `reason` and the `capability` leased (`live` or `text`). A Live start answers with `mode: "live"` beside the
  fields above.
  `mode: "speech"` is our own speech engine (docs/design/voice-it-own-speech.md
  §4.1, §10.2): no offer either (one is 422), and its conversation is the fast
  Claude front (claudefront.FrontSessions), never textchat.py's: a speech
  start builds no text session. The front holds one Claude account of the
  person's workspace pool for the session and runs on `conversation_model`
  (Voice it › Settings', else VOICE_FRONT_MODEL); it answers
  `201 {voice_session_id, mode: "speech"}`. When no account is free, nothing
  is held and the answer is 409 `{"code": "voice_front_refused", "message"}`:
  swarm-api then falls the session back to text mode and starts it as text.
* `POST /internal/sessions/{voice_session_id}/turns` -- one turn, routed by the
  session's mode: `{"text": <1..4000 chars>}` -> `200 {"turn_id", "reply",
  "cards"}`. A speech session's turn is its fast front's, never textchat's,
  and adds `delegation_id` when the front handed it to the planner (whose
  answer is read at `/delegations/{id}`); a text session's `cards` are what
  the planner drew when the turn delegated. 404 for a session this process
  holds no text or speech conversation for (a Live one, an ended one, one lost
  to a restart); 429 `{"code": "usage_limit_reached", "message",
  "resets_at"}` when the plan's usage limit refused it; 502 as a start's, and
  when a front's child is down past its one re-lease. The text is never
  logged.
* `POST /internal/sessions/{voice_session_id}/end` -- closes the GPT-Live
  session and its sideband, which stops OpenAI's per-second billing, or ends
  the text conversation and gives its plan back, or ends the fast front and
  releases its Claude account.
* `POST /internal/sessions/{voice_session_id}/readback` -- readback.py's: a
  go swarm-api heard re-sends the call held for that read-back.
* `GET /delegations/{delegation_id}[?wait=<seconds>]` -- claudefront.py's: a
  front's delegation, and once done the planner's spoken sentences and cards.
  `200 {voice_session_id, ended, usage_seconds, reason}`, 404 for a session
  this process does not hold.
* `GET /healthz` -- counts only.

THE CALLER. Each `/internal/` call carries a Google ID token minted for
`SWARM_VOICE_AUDIENCE`; only the service accounts in `SWARM_VOICE_CALLERS`
(swarm-api's) are admitted. The audience is required: google-auth skips the
`aud` check when it is None (swarm_api/auth.py, GoogleTokenVerifier).

NOTHING SECRET LEAVES. A response carries the SDP answer and ids, never a
credential; an error carries a status, never a body; the leased token is held
in memory only (accountlease.PoolCredential) and never logged.

Configuration, read once at start (never at import):

* `PROJECT_ID` -- this deployment's project.
* `QUOTA_BROKER_URL` (and `QUOTA_BROKER_AUDIENCE`) -- the quota broker, whose
  Voice it pool leases each session's account and whose workspace pool leases
  each planner turn's Claude account (plannerlease.py). Required: without it
  no session could start.
* `SWARM_VOICE_AUDIENCE` -- the audience swarm-api mints its ID token for.
* `SWARM_VOICE_CALLERS` -- comma-separated service account emails admitted.
* `SWARM_API_URL` (and optionally `SWARM_API_AUDIENCE`) -- swarm-api's
  in-cluster address, where the planner (planner.build_planner) reads and
  draws cards as the session's person through the session grant. Unset, no
  session offers delegation. Each turn runs Claude Code (`CLAUDE_CODE_BIN`,
  `claude` on PATH) on a pool lease; a full pool is a spoken refusal.
  The same address is where a Live session's event sink (`ReopenSink`) tells
  swarm-api the next tier a spent key's reopen holds; unset, no sink is wired
  and a spent Live key ends the talk after live.REOPEN_SECONDS.

THE REOPEN REACHES THE CONSOLE (docs/design/llm-providers.md §5.3, the owner,
11 November 2026). live.py emits `voice.tier.reopen` ({tier}) through the
session's event sink when a spent key's next tier is held. `ReopenSink` is
that sink: it posts the tier to swarm-api's
`POST /v1/voice/sessions/{session_id}/reopen` exactly as the planner reaches
swarm-api (tools_read.SwarmApi: this service's ID token and the session's
grant, a route on the grant's write list), and swarm-api puts it on the
session's stream. Every other event the sink is handed -- the sideband's
transcripts among them -- is dropped here unread: nothing else leaves, and the
tier is a name, never a key.

THE VOICE'S STATE REACHES SWARM-API (the owner, 12 November 2026). The same
sink posts each `session.state` live.py emits -- `paused`, `waking`,
`listening` -- to that route as `{state, cause?}` through the same grant
(swarm-api's report_voice_state), the cause only when the event names one and
nothing else of the event. A report never holds the voice: it is posted in the
background, behind the session's previous report so swarm-api hears them in
order. A refused or failed post is retried once, then dropped with a log line
naming the session, the state and the route's status, never the grant, a token
or anything said. The tier's post is unchanged: inline, once.

THE PAUSE AND THE REOPEN (docs/design/voice-it.md §3.12.7, the design's lane
V2; the owner picked the automatic pause on 11 November 2026). live.py pauses
a GPT-Live session after live.PAUSE_AFTER_SECONDS of silence with a delegation
in flight, and `POST /internal/sessions/{voice_session_id}/reopen` opens it
again by cause. Body: `{"offer": <SDP>, "cause": "result"|"speech"|"resume"|
"tier", "brief": {...}?, "cards": [<one-liner>]?, "planner_model": <id>?}`;
the brief (the project's, as the planner last saved it) and the open cards'
one-liners are quoted data for the refreshed instructions (instructions.py),
never configuration: the caller names a cause and nothing else that shapes the
session. Answered `{voice_session_id, mode: "live", live_session_id, answer,
cause}`; 404 for a session not held here (ended), 409 for one with nothing to
reopen (open, or a reopen in flight), and the start's refusals otherwise.
Authenticated as every `/internal/` route.

The pause is armed only with `VOICE_PAUSE=on` (PAUSE_SWITCH): it closes the
voice, and until swarm-api forwards the reopen and writes the paused state
(lane V3) and the console reopens on `session.state` (lane V7), nothing could
wake a paused session. Off, nothing pauses and nothing else changes.

THE FOLLOWER HOOK. Each session's start and end runs `Followers.start` and
`Followers.stop`, every registered pair in turn. With nothing registered they
do nothing; a follower's failure is logged and never fails the start or the
end. The app built from the environment registers the roster's follower
(follow.py, lane V6) when SWARM_API_URL is set, and stops every one at
shutdown; it is `app.state.roster` (None without it).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, field_validator

from . import readback
from .follow import build_roster
from .accountlease import (CAPABILITY, CAPABILITY_TEXT, NONE_AVAILABLE, NOT_IN_POOL, VoicePoolRefused,
                           build_broker)
from .claudefront import (FRONT_MODELS, FrontDown, FrontRefused, FrontSessionEnded, FrontSessions,
                          delegations_router)
from .claudefront import SessionExists as FrontExists
from .claudeturn import admitted_model
from .credential import CredentialError
from .grant import GrantInvalid, SessionGrant
from . import tools_read
from .instructions import ReopenContext
from .live import (LISTENING, MAX_OFFER_BYTES, PAUSE_AFTER_SECONDS, PAUSED, REOPEN_EVENT, SESSION_STATE, WAKING,
                   LiveApi, LiveError, LiveRefused, NotPaused, SessionEnded, SessionExists, Sessions)
from .planner import build_planner
from .plannerlease import FRONT_HOLDER, PlannerAccounts
from .textchat import MAX_TURN_CHARS, CodexApi, TextSessionEnded, TextSessions, UsageLimited
from .worksink import build_work_sink

log = logging.getLogger("swarm_voice")

VOICE_ID = r"^[A-Za-z0-9_-]{8,128}$"

CallerCheck = Callable[[str], Awaitable[str]]
FollowerStart = Callable[[str, "SessionGrant | None"], Awaitable[None]]
FollowerStop = Callable[[str], Awaitable[None]]

#: The environment switch that arms the pause (see THE PAUSE AND THE REOPEN).
PAUSE_SWITCH = "VOICE_PAUSE"
#: The largest brief a reopen carries, as JSON (swarm-api keeps it under 64 KiB).
REOPEN_BRIEF_MAX_BYTES = 64 * 1024
#: The most open cards' one-liners a reopen carries, and the longest one.
REOPEN_CARDS_MAX = 200
REOPEN_CARD_MAX_CHARS = 600


class Followers:
    """The hook that starts and stops a voice session's follower (THE
    FOLLOWER HOOK): a registry, empty until lane V6 registers its follower."""

    def __init__(self) -> None:
        self._pairs: list[tuple[FollowerStart, FollowerStop]] = []

    def register(self, start: FollowerStart, stop: FollowerStop) -> None:
        self._pairs.append((start, stop))

    async def start(self, voice_id: str, grant: SessionGrant | None) -> None:
        for begin, _ in list(self._pairs):
            try:
                await begin(voice_id, grant)
            except Exception as exc:  # noqa: BLE001 - a follower never fails the session
                log.warning("voice session %s: a follower did not start: %s", voice_id, type(exc).__name__)

    async def stop(self, voice_id: str) -> None:
        for _, halt in list(self._pairs):
            try:
                await halt(voice_id)
            except Exception as exc:  # noqa: BLE001 - a follower never fails the end
                log.warning("voice session %s: a follower did not stop: %s", voice_id, type(exc).__name__)


class StartRequest(BaseModel):
    voice_session_id: str = Field(pattern=VOICE_ID)
    person: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    #: `live` (a GPT-Live session from `offer`), `text` (the plan's text
    #: conversation, no offer) or `speech` (own speech's fast Claude front, no
    #: offer). swarm-api picks it from Voice it › Settings' engine.
    mode: Literal["live", "text", "speech"] = "live"
    offer: str | None = Field(default=None, min_length=4, max_length=MAX_OFFER_BYTES, pattern=r"^v=0\r?\n")
    #: The session grant and its project (grant.py). A SecretStr with no
    #: constraint, so neither its repr nor a validation error carries it: its
    #: shape is checked by `SessionGrant`, whose refusal names no value.
    project_id: str | None = Field(default=None, max_length=64)
    grant: SecretStr | None = None
    #: The planner model for this session: Voice it › Settings'
    #: `planner_model`, its class's pin, read by swarm-api at the start. Only
    #: an id of one of MODEL_CLASSES is accepted; absent, the planner keeps
    #: VOICE_PLANNER_MODEL.
    planner_model: str | None = Field(default=None, max_length=64)

    @field_validator("planner_model")
    @classmethod
    def _a_class_pin(cls, value: str | None) -> str | None:
        if value is not None and not admitted_model(value):
            raise ValueError("planner_model must be the id of one of the pool's model classes")
        return value

    #: The fast front's model for this session: Voice it › Settings'
    #: `conversation_model`, read by swarm-api at the start. Only an id of the
    #: curated list (claudefront.FRONT_MODELS) is accepted; absent, the front
    #: keeps VOICE_FRONT_MODEL.
    conversation_model: str | None = Field(default=None, max_length=64)

    #: Voice it › Settings' debug mode, read by swarm-api at the start: True
    #: traces each planner turn of this session to swarm-api (trace.py);
    #: absent or False, nothing is traced.
    debug: bool = False

    @field_validator("conversation_model")
    @classmethod
    def _a_curated_model(cls, value: str | None) -> str | None:
        if value is not None and value not in FRONT_MODELS:
            raise ValueError("conversation_model must be one of the curated conversation models")
        return value


class ReopenRequest(BaseModel):
    """A reopen of a held voice session (THE PAUSE AND THE REOPEN)."""

    offer: str = Field(min_length=4, max_length=MAX_OFFER_BYTES, pattern=r"^v=0\r?\n")
    cause: Literal["result", "speech", "resume", "tier"]
    #: Quoted data for the refreshed instructions, never configuration.
    brief: dict[str, Any] | None = None
    cards: list[str] = Field(default_factory=list, max_length=REOPEN_CARDS_MAX)
    planner_model: str | None = Field(default=None, max_length=64)

    @field_validator("brief")
    @classmethod
    def _a_small_brief(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value, default=str)) > REOPEN_BRIEF_MAX_BYTES:
            raise ValueError("brief is an object of at most 64 KiB")
        return value

    @field_validator("cards")
    @classmethod
    def _short_cards(cls, value: list[str]) -> list[str]:
        if any(len(card) > REOPEN_CARD_MAX_CHARS for card in value):
            raise ValueError(f"each card's one-liner is at most {REOPEN_CARD_MAX_CHARS} characters")
        return value

    @field_validator("planner_model")
    @classmethod
    def _a_class_pin(cls, value: str | None) -> str | None:
        if value is not None and not admitted_model(value):
            raise ValueError("planner_model must be the id of one of the pool's model classes")
        return value


class TurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TURN_CHARS)


def session_grant(body: StartRequest) -> SessionGrant | None:
    """The start call's grant, or None when swarm-api sent none; 422 when it is
    incomplete, for another session, or not one swarm-api mints."""
    if body.grant is None and body.project_id is None:
        return None
    if body.grant is None or body.project_id is None:
        raise HTTPException(422, "a session grant needs both project_id and grant")
    try:
        grant = SessionGrant(project_id=body.project_id, session_id=body.voice_session_id,
                             value=body.grant.get_secret_value())
    except GrantInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    return grant


#: What swarm-api is told when the Voice it pool leases no account: (status, code,
#: one sentence the voice or the page can say). Nothing starts on any of them.
#: A Live session runs on an OpenAI developer API key alone (OpenAI refused the
#: ChatGPT plan, 4 November 2026), so each sentence names the key, never a plan.
POOL_REFUSALS: dict[str, tuple[int, str, str]] = {
    NOT_IN_POOL: (403, "voice_pool_refused",
                  "Voice it's OpenAI API key isn't available to you, so this conversation can't start."),
    NONE_AVAILABLE: (409, "voice_pool_busy",
                     "Voice it's OpenAI API key is in use by another session; try again when it ends."),
}
POOL_REFUSED_DEFAULT = (409, "voice_pool_refused",
                        "Voice it has no OpenAI API key in its pool yet, so this conversation can't start.")
#: A text start leases the Voice it pool's ChatGPT plan (accountlease
#: CAPABILITY_TEXT), never the OpenAI key, so its refusals name the plan
#: (live QA, 9 November 2026: a text start was told the key was in use).
TEXT_POOL_REFUSALS: dict[str, tuple[int, str, str]] = {
    NOT_IN_POOL: (403, "voice_pool_refused",
                  "Voice it's plan account isn't available to you, so this conversation can't start."),
    NONE_AVAILABLE: (409, "voice_pool_busy",
                     "No Voice it plan account is free for a text session; try again in a few minutes."),
}
TEXT_POOL_REFUSED_DEFAULT = (409, "voice_pool_refused",
                             "Voice it has no plan account in its pool yet, so this conversation can't start.")
#: A speech start whose fast front found no Claude account (claudefront.FrontRefused):
#: swarm-api's text fallback (swarm_api.voice.sessions.FRONT_REFUSED_CODE).
FRONT_REFUSED = "voice_front_refused"


def openai_refusal(exc: LiveRefused) -> JSONResponse:
    """swarm-voice's 502 for a Live call OpenAI refused: its status, code and type."""
    return JSONResponse(status_code=502, content={
        "code": "openai_refused", "message": str(exc), "openai_status": exc.status,
        "openai_code": exc.code, "openai_type": exc.type})


def pool_refusal(reason: str, *, capability: str = CAPABILITY) -> JSONResponse:
    """The refusal for a lease the Voice it pool refused: `reason` is the
    broker's, `capability` what was leased (`live`, the key; `text`, the plan)."""
    if capability == CAPABILITY_TEXT:
        status, code, message = TEXT_POOL_REFUSALS.get(reason, TEXT_POOL_REFUSED_DEFAULT)
    else:
        status, code, message = POOL_REFUSALS.get(reason, POOL_REFUSED_DEFAULT)
    return JSONResponse(status_code=status, content={"code": code, "message": message,
                                                     "reason": reason, "capability": capability})


def google_caller_check(audience: str, callers: frozenset[str]) -> CallerCheck:
    """An ID-token check pinned to `audience` that admits only `callers`."""
    if not audience:
        raise RuntimeError("SWARM_VOICE_AUDIENCE is required: without it the token's audience is not checked")
    if not callers:
        raise RuntimeError("SWARM_VOICE_CALLERS is empty: nothing could call swarm-voice")
    request_holder: list[Any] = []

    def verify(token: str) -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        if not request_holder:
            request_holder.append(google_requests.Request())
        try:
            claims = id_token.verify_oauth2_token(token, request_holder[0], audience=audience)
        except ValueError:
            # google-auth's message may echo token content: never propagated.
            raise PermissionError("id token verification failed") from None
        email = claims.get("email")
        if not email or claims.get("email_verified") is False or email not in callers:
            raise PermissionError("the caller is not admitted")
        return str(email)

    async def check(token: str) -> str:
        return await asyncio.to_thread(verify, token)

    return check


#: swarm-api's route for a held next tier (swarm_api/routes/voice.py
#: `tier_reopen`), on the session grant's write list.
REOPEN_ROUTE = "/v1/voice/sessions/{session_id}/reopen"


#: The voice states live.py's `session.state` reports that swarm-api takes
#: through the grant (swarm-api's voice/sessions.py VOICE_REPORTS).
STATE_REPORTS = (PAUSED, WAKING, LISTENING)
#: Tries per state report: the post and one retry, then it is dropped.
STATE_REPORT_TRIES = 2


class ReopenSink:
    """A Live session's event sink: tells swarm-api, through the session's
    grant, the tier a spent key's reopen holds and the voice's state as each
    `session.state` names it (see THE REOPEN REACHES THE CONSOLE and THE VOICE'S
    STATE REACHES SWARM-API). Any other event is dropped unread."""

    def __init__(self, http: Any, *, api_base: str, id_tokens: tools_read.IdTokens) -> None:
        self._http = http
        self._base = api_base
        self._id_tokens = id_tokens
        #: The sessions whose events this sink is handed (`bind`).
        self._sessions: Sessions | None = None
        #: Each voice session's last state report in flight: the next one waits
        #: for it, so swarm-api hears them in the order live.py emitted them.
        self._reports: dict[str, asyncio.Task[None]] = {}

    def bind(self, sessions: Sessions) -> None:
        self._sessions = sessions

    def _grant(self, voice_id: str) -> SessionGrant | None:
        if self._sessions is None:
            return None
        session = self._sessions.get(voice_id) \
            or self._sessions._awaiting.get(voice_id)  # noqa: SLF001 -- a spent session that already ended
        return session.grant if session is not None else None

    async def __call__(self, voice_id: str, event: dict[str, Any]) -> None:
        if event.get("type") == SESSION_STATE:
            self._report_state(voice_id, event)
            return
        if event.get("type") != REOPEN_EVENT:
            return
        tier = event.get("tier")
        grant = self._grant(voice_id)
        if not isinstance(tier, str) or grant is None:
            log.info("voice session %s: no grant, so swarm-api is not told of the next tier", voice_id)
            return
        api = tools_read.SwarmApi.for_session(self._http, base_url=self._base, id_tokens=self._id_tokens,
                                              grant=grant)
        try:
            await api._call("POST", REOPEN_ROUTE, REOPEN_ROUTE.format(session_id=grant.session_id),  # noqa: SLF001
                            json_body={"tier": tier})
        except tools_read.ApiRefused as exc:
            log.warning("voice session %s: swarm-api was not told of the %s tier: %s", voice_id, tier, exc)
            return
        log.info("voice session %s: swarm-api was told of the %s tier", voice_id, tier)

    def _report_state(self, voice_id: str, event: dict[str, Any]) -> None:
        """Queue `{state, cause?}` for swarm-api behind the session's previous
        report and return at once: the voice never waits on swarm-api."""
        state = event.get("state")
        if state not in STATE_REPORTS:
            return
        grant = self._grant(voice_id)
        if grant is None:
            log.info("voice session %s: no grant, so swarm-api is not told the voice is %s", voice_id, state)
            return
        body: dict[str, Any] = {"state": state}
        cause = event.get("cause")
        if isinstance(cause, str) and cause:
            body["cause"] = cause
        previous = self._reports.get(voice_id)
        task = asyncio.create_task(self._post_state(voice_id, grant, body, previous))
        self._reports[voice_id] = task
        task.add_done_callback(lambda done: self._reports.pop(voice_id, None)
                               if self._reports.get(voice_id) is done else None)

    async def _post_state(self, voice_id: str, grant: SessionGrant, body: dict[str, Any],
                          previous: asyncio.Task[None] | None) -> None:
        if previous is not None:
            # Its own failure is already logged; only its order matters here.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await previous
        api = tools_read.SwarmApi.for_session(self._http, base_url=self._base, id_tokens=self._id_tokens,
                                              grant=grant)
        path = REOPEN_ROUTE.format(session_id=grant.session_id)
        state = body["state"]
        for attempt in range(1, STATE_REPORT_TRIES + 1):
            try:
                await api._call("POST", REOPEN_ROUTE, path, json_body=body)  # noqa: SLF001
            except tools_read.ApiRefused as exc:
                # The route and status only: never the grant, a token or what was said.
                if attempt < STATE_REPORT_TRIES:
                    log.info("voice session %s: swarm-api was not told the voice is %s (%s); trying once more",
                             voice_id, state, exc)
                    continue
                log.warning("voice session %s: the %s report was dropped: %s", voice_id, state, exc)
                return
            log.info("voice session %s: swarm-api was told the voice is %s", voice_id, state)
            return

    async def drain(self) -> None:
        """Wait for every state report in flight."""
        while self._reports:
            await asyncio.gather(*list(self._reports.values()), return_exceptions=True)


def build_reopen_sink(http: Any) -> ReopenSink | None:
    """The sink from SWARM_API_URL (and SWARM_API_AUDIENCE, else the URL), the
    planner's own address; None when it is unset."""
    base = os.environ.get("SWARM_API_URL", "").strip()
    if not base:
        return None
    audience = os.environ.get("SWARM_API_AUDIENCE", "").strip() or base
    return ReopenSink(http, api_base=base, id_tokens=tools_read.google_id_tokens(audience))


def build_sessions(http: Any) -> Sessions:
    # Every session's account is leased from the quota broker's Voice it pool;
    # without QUOTA_BROKER_URL this raises and the process does not start.
    leases = build_broker(http)
    # The planner (§3.3) reaches swarm-api at SWARM_API_URL through the session
    # grant; without that address it is None and no session offers delegation.
    # Each planner turn leases its Claude account through the same broker client.
    # A spent key's next tier reaches swarm-api the same way (ReopenSink).
    sink = build_reopen_sink(http)
    # THE PAUSE is armed only with VOICE_PAUSE=on, until the console can wake it.
    pause = PAUSE_AFTER_SECONDS if os.environ.get(PAUSE_SWITCH, "").strip().lower() == "on" else None
    sessions = Sessions(LiveApi(http), leases=leases, planner=build_planner(http, broker=leases),
                        on_event=sink, pause_after=pause)
    if sink is not None:
        sink.bind(sessions)
    return sessions


def build_texts(http: Any, sessions: Sessions) -> TextSessions:
    """The text conversations (textchat.py), on the Live sessions' broker client
    and planner: a text turn delegates exactly as a Live one does."""
    return TextSessions(CodexApi(http), leases=sessions._leases,  # noqa: SLF001 -- one broker client
                        planner=sessions._planner)  # noqa: SLF001 -- one planner


def build_fronts(sessions: Sessions, http: Any = None) -> FrontSessions:
    """Own speech's fast fronts (claudefront.py), on the Live sessions' broker
    client, holding each session's Claude account as the front, and on their
    planner: a front's delegation reaches the same planner. With `http` and
    SWARM_API_URL, each delegation's lifecycle reaches swarm-api's work route
    through the session's grant (worksink.py); else nothing is posted."""
    return FrontSessions(accounts=PlannerAccounts(sessions._leases, holder=FRONT_HOLDER),  # noqa: SLF001
                         planner=sessions._planner,  # noqa: SLF001 -- one planner
                         work=build_work_sink(http) if http is not None else None)


def build_caller_check() -> CallerCheck:
    callers = frozenset(c.strip() for c in os.environ.get("SWARM_VOICE_CALLERS", "").split(",") if c.strip())
    return google_caller_check(os.environ.get("SWARM_VOICE_AUDIENCE", "").strip(), callers)


def usage_limited(exc: UsageLimited) -> JSONResponse:
    """swarm-voice's 429 for a turn the ChatGPT plan's usage limit refused."""
    return JSONResponse(status_code=429, content={
        "code": "usage_limit_reached", "resets_at": exc.resets_at,
        "message": "Voice it's ChatGPT plan has reached its usage limit"
                   + (f"; it resets at {exc.resets_at}." if exc.resets_at else "; try again later.")})


def create_app(*, sessions: Sessions | None = None, verify_caller: CallerCheck | None = None,
               texts: TextSessions | None = None, fronts: FrontSessions | None = None,
               followers: Followers | None = None) -> FastAPI:
    """The app. With no arguments (uvicorn's `--factory`), everything is built from
    the environment when the app starts, never at import."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = None
        if app.state.sessions is None:
            import httpx

            http = httpx.AsyncClient()
            app.state.sessions = build_sessions(http)
            if app.state.texts is None:
                app.state.texts = build_texts(http, app.state.sessions)
            if app.state.fronts is None:
                app.state.fronts = build_fronts(app.state.sessions, http)
            if followers is None:
                app.state.roster = build_roster(http)
                if app.state.roster is not None:
                    app.state.followers.register(app.state.roster.start, app.state.roster.stop)
        if app.state.verify_caller is None:
            app.state.verify_caller = build_caller_check()
        try:
            yield
        finally:
            if app.state.roster is not None:
                await app.state.roster.stop_all()
            # A shutdown closes every session: one left open would bill with no sideband.
            await app.state.sessions.end_all()
            if app.state.texts is not None:
                await app.state.texts.end_all()
            if app.state.fronts is not None:
                # Each front holds a Claude account: released, not left to the TTL.
                await app.state.fronts.end_all()
            if http is not None:
                await http.aclose()

    app = FastAPI(title="swarm-voice", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    app.state.verify_caller = verify_caller
    app.state.texts = texts
    app.state.fronts = fronts
    app.state.followers = followers if followers is not None else Followers()
    app.state.roster = None

    def held_elsewhere(voice_id: str, *, but: str) -> bool:
        """Whether another of the three conversations holds this voice id."""
        held = {"live": app.state.sessions, "text": app.state.texts, "speech": app.state.fronts}
        return any(kind != but and found is not None and found.get(voice_id) is not None
                   for kind, found in held.items())

    async def caller(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "an ID token is required")
        try:
            return await app.state.verify_caller(token.strip())
        except PermissionError:
            raise HTTPException(401, "the caller is not admitted") from None

    @app.post("/internal/sessions", status_code=201)
    async def start(body: StartRequest, _caller: str = Depends(caller)) -> Any:
        grant = session_grant(body)
        answer = await start_mode(body, grant)
        if getattr(answer, "status_code", 201) == 201:
            await app.state.followers.start(body.voice_session_id, grant)
        return answer

    async def start_mode(body: StartRequest, grant: Any) -> Any:
        if body.mode == "speech":
            return await start_speech(body, grant)
        if body.mode == "text":
            return await start_text(body, grant)
        if body.offer is None:
            raise HTTPException(422, "a live session needs the browser's SDP offer")
        if held_elsewhere(body.voice_session_id, but="live"):
            raise HTTPException(409, "this voice session already has a live session")
        try:
            session = await app.state.sessions.start(
                body.voice_session_id, person=body.person, offer=body.offer, grant=grant,
                planner_model=body.planner_model, debug=body.debug)
        except SessionExists:
            raise HTTPException(409, "this voice session already has a live session") from None
        except VoicePoolRefused as exc:
            log.info("voice session %s: the Voice it pool leased no account (%s)",
                     body.voice_session_id, exc.reason)
            return pool_refusal(exc.reason)
        except LiveRefused as exc:
            log.warning("voice session %s: start failed: %s", body.voice_session_id, exc)
            return openai_refusal(exc)
        except (CredentialError, LiveError) as exc:
            log.warning("voice session %s: start failed: %s", body.voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        return {"voice_session_id": session.voice_id, "mode": "live", "live_session_id": session.live_id,
                "answer": session.answer}

    async def start_text(body: StartRequest, grant: Any) -> Any:
        texts = app.state.texts
        if texts is None:
            raise HTTPException(503, "this swarm-voice holds no text conversations")
        try:
            session = await texts.start(body.voice_session_id, person=body.person, grant=grant,
                                        exists=lambda voice_id: held_elsewhere(voice_id, but="text"),
                                        planner_model=body.planner_model)
        except SessionExists:
            raise HTTPException(409, "this voice session already has a live session") from None
        except VoicePoolRefused as exc:
            log.info("voice session %s: the Voice it pool leased no plan (%s)", body.voice_session_id, exc.reason)
            return pool_refusal(exc.reason, capability=CAPABILITY_TEXT)
        except CredentialError as exc:
            log.warning("voice session %s: text start failed: %s", body.voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        return JSONResponse(status_code=201, content={"voice_session_id": session.voice_id, "mode": "text"})

    async def start_speech(body: StartRequest, grant: Any) -> Any:
        """The fast front, never textchat: a speech start builds no text session."""
        fronts_ = app.state.fronts
        if fronts_ is None:
            raise HTTPException(503, "this swarm-voice holds no speech conversations")
        if body.offer is not None:
            raise HTTPException(422, "a speech session takes no SDP offer")
        try:
            session = await fronts_.start(body.voice_session_id, person=body.person, grant=grant,
                                          exists=lambda voice_id: held_elsewhere(voice_id, but="speech"),
                                          planner_model=body.planner_model,
                                          conversation_model=body.conversation_model,
                                          debug=body.debug)
        except FrontExists:
            raise HTTPException(409, "this voice session already has a live session") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except FrontRefused as exc:
            log.info("voice session %s: no fast front (%s); swarm-api falls back to text",
                     body.voice_session_id, exc.reason)
            return JSONResponse(status_code=409, content={
                "code": FRONT_REFUSED,
                "message": "No Claude account is free for Voice it's voice, so this talk continues as text."})
        return JSONResponse(status_code=201, content={"voice_session_id": session.voice_id, "mode": "speech"})

    async def speech_turn(voice_session_id: str, text: str) -> Any:
        try:
            answered = await app.state.fronts.turn(voice_session_id, text)
        except FrontSessionEnded:
            raise HTTPException(404, "no such speech conversation") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except FrontDown:
            log.warning("voice session %s: the fast front is down", voice_session_id)
            raise HTTPException(502, "the fast front is down") from None
        said = {"turn_id": answered.turn_id, "reply": answered.reply, "cards": list(answered.cards)}
        if answered.delegation_id:
            said["delegation_id"] = answered.delegation_id
        return said

    @app.post("/internal/sessions/{voice_session_id}/turns")
    async def turn(voice_session_id: str, body: TurnRequest, _caller: str = Depends(caller)) -> Any:
        # By the session's mode: a speech session's turn is its front's alone.
        if app.state.fronts is not None and app.state.fronts.get(voice_session_id) is not None:
            return await speech_turn(voice_session_id, body.text)
        texts = app.state.texts
        if texts is None:
            raise HTTPException(404, "no such text conversation")
        try:
            answered = await texts.turn(voice_session_id, body.text)
        except TextSessionEnded:
            raise HTTPException(404, "no such text conversation") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except UsageLimited as exc:
            log.info("voice session %s: the plan's usage limit refused a turn (resets %s)",
                     voice_session_id, exc.resets_at)
            return usage_limited(exc)
        except LiveRefused as exc:
            return openai_refusal(exc)
        except (CredentialError, LiveError) as exc:
            log.warning("voice session %s: text turn failed: %s", voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        return {"turn_id": answered.turn_id, "reply": answered.reply, "cards": answered.cards}

    @app.post("/internal/sessions/{voice_session_id}/reopen")
    async def reopen(voice_session_id: str, body: ReopenRequest, _caller: str = Depends(caller)) -> Any:
        try:
            session = await app.state.sessions.reopen(
                voice_session_id, offer=body.offer, cause=body.cause, planner_model=body.planner_model,
                context=ReopenContext(brief=body.brief, cards=list(body.cards)))
        except SessionEnded:
            raise HTTPException(404, "no such live session: it has ended") from None
        except NotPaused:
            raise HTTPException(409, "this voice session has nothing to reopen") from None
        except SessionExists:
            raise HTTPException(409, "this voice session is already starting or reopening") from None
        except VoicePoolRefused as exc:
            return pool_refusal(exc.reason)
        except LiveRefused as exc:
            log.warning("voice session %s: reopen failed: %s", voice_session_id, exc)
            return openai_refusal(exc)
        except (CredentialError, LiveError) as exc:
            log.warning("voice session %s: reopen failed: %s", voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        return {"voice_session_id": session.voice_id, "mode": "live", "live_session_id": session.live_id,
                "answer": session.answer, "cause": body.cause}

    @app.post("/internal/sessions/{voice_session_id}/end")
    async def end(voice_session_id: str, _caller: str = Depends(caller)) -> dict:
        ended = await end_mode(voice_session_id)
        await app.state.followers.stop(voice_session_id)
        return ended

    async def end_mode(voice_session_id: str) -> dict:
        fronts_ = app.state.fronts
        if fronts_ is not None and fronts_.get(voice_session_id) is not None:
            front = await fronts_.end(voice_session_id)
            if front is not None:
                return {"voice_session_id": voice_session_id, "ended": True,
                        "usage_seconds": front.usage_seconds, "reason": front.reason}
        texts = app.state.texts
        if texts is not None and texts.get(voice_session_id) is not None:
            ended = await texts.end(voice_session_id)
            if ended is not None:
                return {"voice_session_id": voice_session_id, "ended": True,
                        "usage_seconds": ended.usage_seconds, "reason": ended.reason}
        try:
            session = await app.state.sessions.end(voice_session_id)
        except (CredentialError, LiveError) as exc:
            log.warning("voice session %s: end failed: %s", voice_session_id, exc)
            raise HTTPException(502, str(exc)) from None
        if session is None:
            raise HTTPException(404, "no such live session")
        # Every Live session's seconds: a pause and each reopen add one (§3.12.8).
        return {"voice_session_id": voice_session_id, "ended": session.ended,
                "usage_seconds": session.talk_seconds if session.usage_seconds is not None
                or session.earlier_seconds else None, "reason": session.reason}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        sessions_ = app.state.sessions
        counts = sessions_.counts() if sessions_ is not None else {"sessions": 0, "attached": 0}
        texts_ = app.state.texts
        fronts_ = app.state.fronts
        return JSONResponse({**counts, **(texts_.counts() if texts_ is not None else {"text_sessions": 0}),
                             **(fronts_.counts() if fronts_ is not None
                                else {"front_sessions": 0, "front_delegations": 0})})

    # The read-back gate's go (readback.py) and the fronts' delegations
    # (claudefront.py), both behind the same caller check.
    app.include_router(readback.router)
    app.include_router(delegations_router(lambda: app.state.fronts, caller))

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run("swarm_voice.main:create_app", factory=True, host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
