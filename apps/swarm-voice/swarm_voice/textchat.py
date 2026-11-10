"""Voice it's text conversation on the ChatGPT plan (docs/design/voice-it.md §3.2).

WHY IT EXISTS (owner, 4 November 2026: "both paths"). OpenAI refuses the
ChatGPT plan's token for GPT-Live sessions (403 on /v1/live/sessions, 4
November 2026), so a Live session needs an OpenAI developer API key in the
Voice it pool (live.py). The plan still serves text inference, the way AI
Studio runs on it, so with no key in the pool a session is a TEXT
conversation: the browser turns each spoken turn into text and posts it, and
this module answers it from the plan. There is no SDP and no WebRTC; the
captions are the turns themselves.

THE WIRE, AS A PEER APPLICATION MEASURED IT (2026-08-10, against a live ChatGPT Plus
token; copied, not re-derived):

* `POST https://chatgpt.com/backend-api/codex/responses` with
  the plan's access token as the bearer of the `Authorization` header, `OpenAI-Beta:
  responses=experimental`, `originator: codex_cli_rs`, and per request
  `chatgpt-account-id: <the plan's account id>` and `session_id: <the Voice it
  session id>` (the backend echoes it as the prompt cache key).
* The body is `{model, instructions, input, stream: true, store: false,
  include: ["reasoning.encrypted_content"]}`. `stream: false` is a 400, and so
  is `max_output_tokens`; there is no temperature, no top_p, no system role and
  no response_format (`build_body` refuses every one of them, so none is ever
  sent). The delegation tool rides beside them as the Codex CLI sends its own.
* The answer is SSE. The terminal `response.completed` carries
  `response.output == []`: the text arrives in `response.output_item.done`
  items, so those are what is read (reading only the terminal event returns an
  empty reply on success).
* `GET /models?client_version=0.147.0` (client_version required) lists what
  the plan serves under `models[].slug`; the first listed of `PREFERRED_MODELS`
  is used.
* Quota rides on every answer's headers: `x-codex-primary-used-percent`,
  `x-codex-primary-window-minutes`, `x-codex-primary-reset-at` and the same
  `secondary` set (minutes 300 is the five-hour window, 10080 the week; used is
  a percent). They are named the way the broker records the plan's windows
  (quota_broker/openai_plan.py: `five_hour`, `seven_day`, a 0-1 utilization and
  the reset instant) and reported to the broker for the session's hold. A
  refusal whose error type is `usage_limit_reached` carries `resets_at`, and
  becomes `UsageLimited` (swarm-api answers it 429 with when it resets).

THE ACCOUNT is the Voice it pool's ChatGPT plan, leased for the session with
`capability: text` (accountlease.CAPABILITY_TEXT) and renewed by the broker,
the plan's one writer: this module never refreshes a token, and a 401 only
asks the lease for a fresh one (`Credential.refused`).

DELEGATION works as on the Live path (§3.3). With a planner, the model is
offered one `delegate` tool; a turn that asks for a plan comes back as that
call, becomes `Planner.delegate` over the conversation so far with the
session's person and grant, and the planner's at most two sentences are the
turn's reply while its cards stream to the console as they do today.

NOTHING SECRET OR PERSONAL IS LOGGED. The token is held in a `Bearer` (masked
repr); the person's text and the model's reply are never logged or put in an
exception: a log line names the session, a status and counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Protocol

import httpx

from .accountlease import CAPABILITY_TEXT
from .credential import Bearer, Credential, CredentialError
from .grant import SessionGrant
from .live import (DELEGATE_INSTRUCTIONS, INSTRUCTIONS, LeasedCredential, LiveError, LiveRefused,
                   SessionExists, openai_error)

if TYPE_CHECKING:
    from .planner import Planner

log = logging.getLogger("swarm_voice.textchat")

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
#: The Codex CLI version the models route requires (`client_version`).
CLIENT_VERSION = "0.147.0"
#: Used in this order, the first the plan lists; a plan that lists none of
#: them is answered by the first model it does list.
PREFERRED_MODELS = ("gpt-5.5", "gpt-5.6-sol", "gpt-5.4-mini")
#: A turn's text, as the console sends it.
MAX_TURN_CHARS = 4000
#: Turns of both sides kept and sent back as the conversation.
HISTORY_TURNS = 200
#: The body fields the plan's Responses route refuses (400) or this path never
#: sends: `build_body` raises rather than build a request carrying one.
FORBIDDEN_FIELDS = frozenset({"max_output_tokens", "temperature", "top_p", "response_format"})
#: A text session no turn has reached for this long ends itself and gives its
#: plan back, so a closed tab never holds the Voice it pool's account.
IDLE_SECONDS = 900.0
REQUEST_SECONDS = 120.0
_MODELS_SECONDS = 10.0
#: The longest SSE answer read (a reply is a few sentences).
MAX_STREAM_BYTES = 4 * 1024 * 1024
#: Window lengths named the way the broker names a plan's windows.
_WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}

USAGE_LIMIT = "usage_limit_reached"
DELEGATE_TOOL_NAME = "delegate"

#: The text conversation's standing instructions: the Live voice's, adapted for
#: a reply that is read and spoken back from text.
TEXT_INSTRUCTIONS = INSTRUCTIONS + (
    " Each message you receive is what the person said aloud, transcribed, and your reply is "
    "shown as a caption and read out, so answer in plain sentences: no markdown, no lists, "
    "no code blocks, and at most a few sentences unless they ask for more."
)
#: Added when a planner is configured: the text path's way to delegate.
TEXT_DELEGATE_INSTRUCTIONS = DELEGATE_INSTRUCTIONS.replace(
    "delegate it, say a few words while you wait, then speak the result you are given",
    "call the delegate tool with what they asked for, and nothing else; the planner's "
    "answer is given to the person for you",
)


def delegate_tool() -> dict[str, Any]:
    """The one tool the text model is offered: hand the turn to the planner."""
    return {
        "type": "function",
        "name": DELEGATE_TOOL_NAME,
        "description": ("Hand the person's request to SwarmCloud's planner, which draws cards on "
                        "their screen and answers in at most two sentences."),
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string",
                                       "description": "What the person asked for, in a sentence."}},
            "required": ["request"],
            "additionalProperties": False,
        },
    }


class CodexRefused(LiveRefused):
    """The plan's Responses route refused a call: its status and error code and
    type, never the error's message."""


class UsageLimited(LiveError):
    """The plan's usage limit refused the turn; `resets_at` (ISO 8601, UTC) is
    when it lifts, None when the answer did not say."""

    def __init__(self, resets_at: str | None) -> None:
        self.resets_at = resets_at
        super().__init__("the ChatGPT plan's usage limit is reached"
                         + (f" until {resets_at}" if resets_at else ""))


class TextSessionEnded(Exception):
    """The text session has ended (or never existed here): no turn runs."""


def build_headers(bearer: Bearer, session_id: str) -> dict[str, str]:
    """The Responses wire's headers: the plan's bearer and account, the
    experimental beta, the Codex CLI's originator, and the session id."""
    if not bearer.account_id:
        # A key (or a plan lease without its account id) cannot talk to the plan's route.
        raise CredentialError("the Voice it text lease carried no ChatGPT plan account id")
    return {"Authorization": " ".join(("Bearer", bearer.reveal())), "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs", "chatgpt-account-id": bearer.account_id,
            "session_id": session_id}


def _message(side: str, text: str) -> dict[str, Any]:
    if side == "person":
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def build_body(*, model: str, instructions: str, turns: list[tuple[str, str]],
               tools: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """The Responses body. `stream` true and `store` false are fixed; a field the
    plan's route refuses (`FORBIDDEN_FIELDS`) or `stream`/`store` set otherwise
    raises ValueError, so no such request is ever built."""
    bad = FORBIDDEN_FIELDS.intersection(extra)
    if bad:
        raise ValueError(f"the plan's Responses route refuses {', '.join(sorted(bad))}")
    if extra.get("stream", True) is not True or extra.get("store", False) is not False:
        raise ValueError("the plan's Responses route needs stream true and store false")
    if extra.keys() - {"stream", "store"}:
        raise ValueError(f"no Responses field {sorted(extra.keys() - {'stream', 'store'})} is sent")
    body: dict[str, Any] = {"model": model, "instructions": instructions,
                            "input": [_message(side, text) for side, text in turns],
                            "stream": True, "store": False,
                            "include": ["reasoning.encrypted_content"]}
    if tools:
        body.update({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})
    return body


def _instant(value: Any) -> str | None:
    """An ISO 8601 UTC instant from unix seconds (a number or digits) or an ISO string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            seconds = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        value = seconds
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def quota_windows(headers: httpx.Headers | dict[str, str]) -> dict[str, dict[str, Any]]:
    """The plan's windows from a Responses answer's `x-codex-*` headers, named
    and scaled as the broker records a plan's (`five_hour`/`seven_day`, a 0-1
    utilization, the reset instant). A window missing any figure is left out."""
    get = headers.get
    windows: dict[str, dict[str, Any]] = {}
    for which in ("primary", "secondary"):
        used, minutes, reset = (get(f"x-codex-{which}-used-percent"),
                                get(f"x-codex-{which}-window-minutes"),
                                get(f"x-codex-{which}-reset-at"))
        try:
            pct = float(used) if used is not None else math.nan
            length = int(float(minutes)) if minutes is not None else 0
        except ValueError:
            continue
        resets = _instant(reset)
        if not math.isfinite(pct) or length <= 0 or resets is None:
            continue
        name = _WINDOW_NAMES.get(length, f"window_{length * 60}s")
        windows[name] = {"utilization": max(0.0, min(1.0, pct / 100.0)), "resets_at": resets}
    return windows


def _usage_limit(raw: Any, headers: httpx.Headers | dict[str, str] | None) -> UsageLimited | None:
    """`UsageLimited` when an error body (or an SSE failure's `error`) is the plan's
    usage limit, with when it resets: its `resets_at`, else `resets_in_seconds`,
    else the binding window's reset header."""
    error = raw.get("error") if isinstance(raw, dict) and isinstance(raw.get("error"), dict) else raw
    if not isinstance(error, dict) or USAGE_LIMIT not in (error.get("type"), error.get("code")):
        return None
    resets = _instant(error.get("resets_at"))
    after = error.get("resets_in_seconds")
    if resets is None and isinstance(after, (int, float)) and not isinstance(after, bool) and after >= 0:
        resets = _instant(time.time() + float(after))
    if resets is None and headers is not None:
        windows = quota_windows(headers)
        spent = [w["resets_at"] for w in windows.values() if w["utilization"] >= 1.0]
        resets = max(spent) if spent else None
    return UsageLimited(resets)


@dataclass
class CodexAnswer:
    """What one Responses call produced. Holds no secret."""

    text: str = ""
    #: The `delegate` call's request, when the model handed the turn over.
    delegation: str | None = None
    windows: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


async def _events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Each SSE event's JSON `data`, in order; a `[DONE]` or an unparsable line is skipped."""
    size = 0
    data: list[str] = []
    async for line in response.aiter_lines():
        size += len(line) + 1
        if size > MAX_STREAM_BYTES:
            raise LiveError("the ChatGPT plan's answer is longer than any reply")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line.strip() or not data:
            continue
        payload, data = "\n".join(data), []
        if payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event
    if data and data != ["[DONE]"]:
        with contextlib.suppress(ValueError):
            event = json.loads("\n".join(data))
            if isinstance(event, dict):
                yield event


def _item_text(item: dict[str, Any]) -> str:
    parts = item.get("content") if isinstance(item.get("content"), list) else []
    return "".join(p["text"] for p in parts
                   if isinstance(p, dict) and p.get("type") == "output_text" and isinstance(p.get("text"), str))


class CodexApi:
    """The plan's two Codex routes, over one shared HTTP client."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str = CODEX_BASE) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def models(self, bearer: Bearer, session_id: str) -> list[str]:
        """The slugs the plan serves, in the order it lists them."""
        try:
            response = await self._http.get(f"{self._base}/models", params={"client_version": CLIENT_VERSION},
                                            headers=build_headers(bearer, session_id), timeout=_MODELS_SECONDS)
        except httpx.HTTPError as exc:
            raise LiveError(f"the ChatGPT plan's models could not be listed ({type(exc).__name__})") from None
        if response.status_code != 200:
            code, kind = openai_error(response.content)
            raise CodexRefused("the plan's model list", response.status_code, code, kind)
        try:
            body = response.json()
        except ValueError:
            body = None
        listed = body.get("models") if isinstance(body, dict) else None
        return [m["slug"] for m in listed or () if isinstance(m, dict) and isinstance(m.get("slug"), str)]

    async def respond(self, bearer: Bearer, *, session_id: str, body: dict[str, Any]) -> CodexAnswer:
        """One streamed Responses call; the reply from its `output_item.done`
        items. Raises UsageLimited, CodexRefused (with the status; 401 among
        them) or LiveError."""
        headers = {**build_headers(bearer, session_id), "Accept": "text/event-stream"}
        answer = CodexAnswer()
        try:
            async with self._http.stream("POST", f"{self._base}/responses", json=body, headers=headers,
                                         timeout=REQUEST_SECONDS) as response:
                answer.windows = quota_windows(response.headers)
                if response.status_code != 200:
                    raw = await response.aread()
                    try:
                        parsed = json.loads(raw) if len(raw) <= 16 * 1024 else None
                    except ValueError:
                        parsed = None
                    limited = _usage_limit(parsed, response.headers)
                    if limited is not None:
                        raise limited
                    code, kind = openai_error(raw)
                    raise CodexRefused("the text turn", response.status_code, code, kind)
                texts: list[str] = []
                async for event in _events(response):
                    kind_ = event.get("type")
                    if kind_ == "response.output_item.done" and isinstance(event.get("item"), dict):
                        item = event["item"]
                        if item.get("type") == "message" and item.get("role", "assistant") == "assistant":
                            texts.append(_item_text(item))
                        elif item.get("type") == "function_call" and item.get("name") == DELEGATE_TOOL_NAME:
                            answer.delegation = _delegation_request(item.get("arguments"))
                    elif kind_ in ("response.failed", "error"):
                        failure = event.get("response", {}).get("error") if kind_ == "response.failed" \
                            and isinstance(event.get("response"), dict) else event.get("error", event)
                        limited = _usage_limit(failure, response.headers)
                        if limited is not None:
                            raise limited
                        code, kind = openai_error(json.dumps({"error": failure if isinstance(failure, dict) else {}}))
                        raise CodexRefused("the text turn", None, code, kind)
                    elif kind_ == "response.completed" and isinstance(event.get("response"), dict):
                        usage = event["response"].get("usage")
                        if isinstance(usage, dict):
                            answer.usage = {k: v for k, v in usage.items()
                                            if isinstance(v, (int, float)) and not isinstance(v, bool)}
                answer.text = "\n".join(t.strip() for t in texts if t.strip())
                return answer
        except httpx.HTTPError as exc:
            raise LiveError(f"the ChatGPT plan could not be reached ({type(exc).__name__})") from None


def _delegation_request(arguments: Any) -> str:
    """The `delegate` call's request; an unreadable argument is still a delegation."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        args = None
    request = args.get("request") if isinstance(args, dict) else None
    return request.strip() if isinstance(request, str) else ""


class TextLeases(Protocol):
    """The Voice it pool (accountlease.VoiceBroker)."""

    async def lease(self, *, person: str, voice_session_id: str,
                    capability: str = ...) -> LeasedCredential: ...

    async def call(self, path: str, payload: dict) -> tuple[int, Any]: ...


@dataclass
class TurnReply:
    """One answered turn: its id, the reply and the cards the planner drew."""

    turn_id: str
    reply: str
    cards: list[dict[str, Any]] = field(default_factory=list)


#: What the reply says when the plan answered nothing readable.
SAY_EMPTY = "I didn't catch an answer to that; could you say it again?"


class TextSession:
    """One person's text conversation on a leased ChatGPT plan."""

    mode = "text"

    def __init__(self, voice_id: str, *, person: str, credential: LeasedCredential,
                 grant: SessionGrant | None, clock: Callable[[], float],
                 planner_model: str | None = None) -> None:
        self.voice_id = voice_id
        self.person = person
        self.grant = grant
        #: The planner model read at this session's start; None keeps the pin.
        self.planner_model = planner_model
        self._credential = credential
        self._clock = clock
        self.started_at = clock()
        self.last_turn_at = self.started_at
        self.turns: list[tuple[str, str]] = []
        self.model: str | None = None
        self.ended = False
        self.reason: str | None = None
        self.lock = asyncio.Lock()
        self.idle_task: asyncio.Task | None = None

    def __repr__(self) -> str:
        return f"TextSession(voice_id={self.voice_id!r}, turns={len(self.turns)})"

    @property
    def usage_seconds(self) -> int:
        return int(max(0.0, self._clock() - self.started_at))


class TextSessions:
    """Every text conversation this process holds, by swarm-api's voice session id."""

    def __init__(self, api: CodexApi, *, leases: TextLeases, planner: "Planner | None" = None,
                 clock: Callable[[], float] = time.monotonic, idle_seconds: float = IDLE_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.api = api
        self._leases = leases
        self._planner = planner
        self._clock = clock
        self._idle = idle_seconds
        self._sleep = sleep
        self._held: dict[str, TextSession] = {}
        self._starting: set[str] = set()

    def get(self, voice_id: str) -> TextSession | None:
        return self._held.get(voice_id)

    def counts(self) -> dict[str, int]:
        return {"text_sessions": len(self._held)}

    @property
    def instructions(self) -> str:
        return TEXT_INSTRUCTIONS + (TEXT_DELEGATE_INSTRUCTIONS if self._planner is not None else "")

    async def start(self, voice_id: str, *, person: str, grant: SessionGrant | None = None,
                    exists: Callable[[str], bool] = lambda _id: False,
                    planner_model: str | None = None) -> TextSession:
        """Lease the Voice it pool's ChatGPT plan for `person` and hold the
        conversation. Raises SessionExists, accountlease.VoicePoolRefused
        (nothing started) or CredentialError."""
        if voice_id in self._held or voice_id in self._starting or exists(voice_id):
            raise SessionExists(voice_id)
        self._starting.add(voice_id)
        try:
            credential = await self._leases.lease(person=person, voice_session_id=voice_id,
                                                  capability=CAPABILITY_TEXT)
            credential.start_renewing()
            session = TextSession(voice_id, person=person, credential=credential, grant=grant,
                                  clock=self._clock, planner_model=planner_model)
            self._held[voice_id] = session
            session.idle_task = asyncio.create_task(self._watch_idle(session), name=f"voice-text-idle-{voice_id}")
            log.info("voice session %s: text conversation started on the ChatGPT plan", voice_id)
            return session
        finally:
            self._starting.discard(voice_id)

    async def _watch_idle(self, session: TextSession) -> None:
        while not session.ended:
            wait = session.last_turn_at + self._idle - self._clock()
            if wait <= 0 and not session.lock.locked():
                log.info("voice session %s: text conversation idle; ended", session.voice_id)
                await self._forget(session, "idle")
                return
            await self._sleep(max(wait, 1.0))

    async def _model(self, session: TextSession, bearer: Bearer) -> str:
        if session.model is None:
            try:
                listed = await self.api.models(bearer, session.voice_id)
            except LiveError as exc:
                log.warning("voice session %s: the plan's models were not listed (%s); using %s",
                            session.voice_id, type(exc).__name__, PREFERRED_MODELS[0])
                listed = []
            preferred = [m for m in PREFERRED_MODELS if m in listed]
            session.model = preferred[0] if preferred else (listed[0] if listed else PREFERRED_MODELS[0])
        return session.model

    async def _respond(self, session: TextSession, turns: list[tuple[str, str]]) -> CodexAnswer:
        """One Responses call; a 401 asks the lease for a fresh token and tries once more."""
        credential: Credential = session._credential
        for attempt in (1, 2):
            bearer = await credential.bearer()
            model = await self._model(session, bearer)
            body = build_body(model=model, instructions=self.instructions, turns=turns,
                              tools=[delegate_tool()] if self._planner is not None else None)
            try:
                return await self.api.respond(bearer, session_id=session.voice_id, body=body)
            except CodexRefused as exc:
                if exc.status != 401 or attempt == 2:
                    raise
                await credential.refused()
        raise AssertionError("unreachable")

    async def turn(self, voice_id: str, text: str) -> TurnReply:
        """Answer one turn of the person's. Raises TextSessionEnded, UsageLimited,
        CodexRefused, CredentialError or LiveError; the person's text is never logged."""
        session = self._held.get(voice_id)
        if session is None or session.ended:
            raise TextSessionEnded(voice_id)
        text = text.strip()
        if not 1 <= len(text) <= MAX_TURN_CHARS:
            raise ValueError(f"a turn is 1 to {MAX_TURN_CHARS} characters")
        async with session.lock:
            if session.ended:
                raise TextSessionEnded(voice_id)
            session.last_turn_at = self._clock()
            turns = [*session.turns, ("person", text)]
            answer = await self._respond(session, turns)
            await self._report(session, answer.windows)
            turn_id = "vt_" + secrets.token_hex(12)
            cards: list[dict[str, Any]] = []
            if answer.delegation is not None and self._planner is not None:
                result = await self._planner.delegate(person=session.person, grant=session.grant,
                                                      transcript=turns, delegation_id=turn_id,
                                                      model=session.planner_model)
                reply, cards = result.spoken, list(result.cards)
            else:
                reply = answer.text
            reply = reply.strip() or SAY_EMPTY
            if session.ended:
                raise TextSessionEnded(voice_id)
            session.turns = [*turns, ("voice", reply)][-HISTORY_TURNS:]
            session.last_turn_at = self._clock()
            log.info("voice session %s: text turn %s answered (%d chars, %d card(s), delegated %s)",
                     voice_id, turn_id, len(reply), len(cards), answer.delegation is not None)
            return TurnReply(turn_id, reply, cards)

    async def _report(self, session: TextSession, windows: dict[str, dict[str, Any]]) -> None:
        """Hand the plan's windows to the quota broker against the session's
        hold; a failure is logged and never fails the turn."""
        credential = session._credential
        account_id = getattr(credential, "account_id", None)
        assignment_id = getattr(credential, "assignment_id", None)
        if not windows or not account_id or not assignment_id:
            return
        from urllib.parse import quote

        try:
            status, _ = await self._leases.call(
                f"/v1/voice/lease/{quote(str(account_id), safe='')}/readings",
                {"assignment_id": assignment_id, "voice_session_id": session.voice_id, "windows": windows})
        except CredentialError as exc:
            log.warning("voice session %s: the plan's windows were not reported: %s", session.voice_id, exc)
            return
        if status != 200:
            log.warning("voice session %s: the quota broker did not take the plan's windows (HTTP %s)",
                        session.voice_id, status)

    async def _forget(self, session: TextSession, reason: str) -> None:
        if session.ended:
            return
        session.ended = True
        session.reason = reason
        # The grant dies with the session (§3.6).
        session.grant = None
        if self._planner is not None:
            self._planner.forget(session.voice_id)
        if self._held.get(session.voice_id) is session:
            del self._held[session.voice_id]
        task, session.idle_task = session.idle_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await session._credential.release()

    async def end(self, voice_id: str, reason: str = "client_close") -> TextSession | None:
        session = self._held.get(voice_id)
        if session is None:
            return None
        await self._forget(session, reason)
        log.info("voice session %s: text conversation ended (%s)", voice_id, reason)
        return session

    async def end_all(self) -> None:
        for voice_id in list(self._held):
            await self.end(voice_id, "shutdown")
