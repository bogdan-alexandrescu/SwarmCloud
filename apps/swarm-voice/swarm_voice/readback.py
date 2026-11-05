"""swarm-voice's half of the read-back gate (docs/design/voice-it-own-speech.md
§7.2, §7.3; swarm-api's half is swarm_api/voice/readback.py).

HELD CALLS. swarm-api answers a gated act (cancel, merge, deploy, delete,
approve) made under the session grant with 202 `readback_pending` and does not
run it. The act path hands that answer to `Holds.hold` with a `send` that
re-sends the very same request, and nothing else: the call is held here, in
memory, by its read-back id. One is held per session; a newer one replaces the
older, as swarm-api supersedes it. On a "go", `Holds.run` re-sends it once with
`X-Swarm-Voice-Readback: <id>`, and swarm-api runs it only if that id is
confirmed and the method, route and body match. A restart loses what is held:
swarm-api's `/readback` call then answers 404, the read-back is `expired` and
nothing runs. A held call older than `HOLD_SECONDS` is dropped (swarm-api has
expired it by then).

FINALS (§7.3). GPT-Live's final user transcripts reach swarm-voice on the
sideband; `Finals.post` sends each to swarm-api's
`POST /v1/voice/sessions/{id}/readbacks/answer` under the session grant,
before the planner, so a "go" or a refusal is told apart deterministically
there. On `answer: "go"` the held call is re-sent from here. Nothing logs what
was said: log lines name the session and a status.

THE INTERNAL ROUTE. `router` serves `POST /internal/sessions/{id}/readback`
`{readback_id}` beside `/internal/sessions`, `/turns` and `/end`, for a go
swarm-api heard itself (a tap, or the own-speech relay). It takes the same
caller check (`app.state.verify_caller`) and the holds of
`app.state.sessions.holds`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .grant import SessionGrant

log = logging.getLogger("swarm_voice.readback")

READBACK_HEADER = "X-Swarm-Voice-Readback"
ANSWER_ROUTE = "/v1/voice/sessions/{session_id}/readbacks/answer"
#: swarm-api's code for a held call (voice/grant.py's `ReadbackHeld`).
HELD_CODE = "readback_pending"
#: swarm-api's 45 s after asking plus the asking; anything older has expired there.
HOLD_SECONDS = 120.0

_READBACK_ID_RE = re.compile(r"^vr_[0-9a-f]{32}$")

#: Re-sends the held request with the extra headers given; returns its answer.
Send = Callable[[dict[str, str]], Awaitable[Any]]
IdTokens = Callable[[], Awaitable[str]]


class NotHeld(LookupError):
    """No call is held for this session under that read-back id."""


def held_id(status: int, body: Any) -> str | None:
    """The read-back id of a 202 `readback_pending` answer, else None."""
    if status != 202 or not isinstance(body, dict) or body.get("code") != HELD_CODE:
        return None
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    rid = detail.get("readback_id")
    return rid if isinstance(rid, str) and _READBACK_ID_RE.match(rid) else None


@dataclass
class _Held:
    readback_id: str
    send: Send
    at: float


@dataclass
class Holds:
    """The held calls, one per voice session."""

    clock: Callable[[], float] = time.monotonic
    _held: dict[str, _Held] = field(default_factory=dict)

    def hold(self, session_id: str, readback_id: str, send: Send) -> None:
        if not _READBACK_ID_RE.match(readback_id or ""):
            raise ValueError("not a read-back id")
        self._held[session_id] = _Held(readback_id, send, self.clock())

    def holds(self, session_id: str, readback_id: str) -> bool:
        held = self._held.get(session_id)
        if held is None or held.readback_id != readback_id:
            return False
        if self.clock() - held.at > HOLD_SECONDS:
            del self._held[session_id]
            return False
        return True

    async def run(self, session_id: str, readback_id: str) -> Any:
        """Re-send the held call once with its read-back id; `NotHeld` when
        it is not held (any more)."""
        if not self.holds(session_id, readback_id):
            raise NotHeld(readback_id)
        held = self._held.pop(session_id)
        return await held.send({READBACK_HEADER: readback_id})

    def forget(self, session_id: str) -> None:
        """An ended session's held call is dropped: nothing it held runs."""
        self._held.pop(session_id, None)


class Finals:
    """Posts each final utterance to swarm-api's read-back answer route, as
    the session's person through its grant."""

    def __init__(self, http: httpx.AsyncClient, *, api_base: str, id_tokens: IdTokens,
                 holds: Holds) -> None:
        self._http = http
        self._base = api_base.rstrip("/")
        self._id_tokens = id_tokens
        self.holds = holds

    @classmethod
    def for_planner(cls, planner: Any, holds: Holds) -> "Finals | None":
        """The planner's swarm-api client (its HTTP client, base URL and ID
        tokens), which already acts through the session grant; None when the
        planner carries none (a test's fake)."""
        http = getattr(planner, "_http", None)
        base = getattr(planner, "_api_base", None)
        tokens = getattr(planner, "_id_tokens", None)
        if not isinstance(http, httpx.AsyncClient) or not isinstance(base, str) or not callable(tokens):
            return None
        return cls(http, api_base=base, id_tokens=tokens, holds=holds)

    async def post(self, grant: SessionGrant | None, text: str) -> dict[str, Any] | None:
        """swarm-api's answer to one final, or None when it was not reached.
        On a "go", the session's held call is re-sent."""
        if grant is None or not text.strip():
            return None
        session_id = grant.session_id
        try:
            token = await self._id_tokens()
            response = await self._http.post(
                self._base + ANSWER_ROUTE.format(session_id=session_id),
                json={"text": text.strip()[:4000]},
                headers={"Authorization": f"Bearer {token}", **grant.headers()}, timeout=10)
        except Exception as exc:  # noqa: BLE001 - google-auth or httpx; neither message is logged
            log.warning("voice session %s: a final did not reach swarm-api (%s)",
                        session_id, type(exc).__name__)
            return None
        if response.status_code != 200:
            log.info("voice session %s: the read-back answer was refused (HTTP %s)",
                     session_id, response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        readback = body.get("readback") if isinstance(body.get("readback"), dict) else {}
        rid = readback.get("readback_id")
        if body.get("answer") == "go" and isinstance(rid, str):
            try:
                await self.holds.run(session_id, rid)
            except NotHeld:
                log.info("voice session %s: read-back %s holds no call here", session_id, rid)
        return body


class RunReadback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readback_id: str


router = APIRouter()


@router.post("/internal/sessions/{voice_session_id}/readback")
async def run_readback(voice_session_id: str, body: RunReadback, request: Request) -> dict:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "an ID token is required")
    verify = getattr(request.app.state, "verify_caller", None)
    if verify is None:
        raise HTTPException(401, "the caller is not admitted")
    try:
        await verify(token.strip())
    except PermissionError:
        raise HTTPException(401, "the caller is not admitted") from None
    sessions = getattr(request.app.state, "sessions", None)
    holds = getattr(sessions, "holds", None)
    if not isinstance(holds, Holds):
        raise HTTPException(404, "no call is held for that read-back")
    try:
        answer = await holds.run(voice_session_id, body.readback_id)
    except NotHeld:
        raise HTTPException(404, "no call is held for that read-back") from None
    status = getattr(answer, "status_code", None)
    return {"readback_id": body.readback_id, "ran": True,
            "status": status if isinstance(status, int) else None}
