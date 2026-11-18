"""Debug mode's trace, swarm-voice's half (the owner, 10 November 2026:
"Trace on Telemetry"; swarm-api's half is swarm_api/voice/trace.py).

WHEN. Only for a session whose start body said `debug: true` (swarm-api
reads Voice it › Settings' debug mode once, at the start). Such a session
holds a `SessionTrace`; every other session holds None, and then nothing here
counts, sends or logs anything.

WHAT. For each planner turn, on both engines (GPT-Live's handoff in live.py,
the Own speech front's delegation in claudefront.py): the transcript lines the
planner was given and the handoff text; each tool read by name and target,
never what it returned; each card or mock-up drawn (kind, title, a mock-up's
frame labels) or refused (with the refusal's reason); when nothing was drawn,
why, as a fixed code (`NONE_CODES`); what the voice was told back; the seconds
from the turn's last transcript piece to the voice being told; and the
sideband's counts since the previous turn. With each turn go the session's
engine and its counts so far.

WHERE. swarm-voice holds no storage client (main.py), so each finished turn is
POSTed to swarm-api's `POST /v1/voice/sessions/{session_id}/trace` through the
session grant, the path the planner already reaches swarm-api on, which
masks every text before it stores it. THE TURN NEVER WAITS FOR IT: `send` runs
in its own task, and a failed write is logged once, with the fixed code
`WRITE_FAILED` and the exception's type, and dropped.

THE COUNTS. GPT-Live's sideband gives all of them: `pieces` (the person's
transcript deltas), `finals` (the person's finals, `empty_finals` those with
no words), `handoffs` (delegations), `barge_ins` (the person starting to speak
while the voice's own line is still arriving) and, per session, `replies`
(planner turns told back). The Own speech front hears finals only (the speech
machine's pieces and barge-ins stay in swarm-api's relay), so on that engine
`pieces` and `barge_ins` stay 0.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

import httpx

from . import tools_read
from .grant import SessionGrant

if TYPE_CHECKING:
    from .planner import TurnResult

log = logging.getLogger("swarm_voice.trace")

ENGINE_OWN = "own"
ENGINE_LIVE = "gpt-live"

TRACE_ROUTE = "/v1/voice/sessions/{session_id}/trace"

#: The one log code a failed trace write leaves (nothing else is logged).
WRITE_FAILED = "voice_trace_write_failed"

#: Why nothing was drawn (swarm_api/voice/trace.py's NONE_CODES).
NONE_EMPTY = "empty_transcript"
NONE_REFUSED = "refused"
NONE_ERROR = "planner_error"
NONE_TIMEOUT = "timeout"
NONE_NO_GRANT = "no_grant"
NONE_WORDS = "answered_in_words"

TURN_EVENTS = ("pieces", "finals", "empty_finals", "handoffs", "barge_ins")
SESSION_EVENTS = (*TURN_EVENTS, "replies")


def none_code(result: "TurnResult | None") -> str | None:
    """Why `result` drew nothing, or None when it drew something."""
    from . import planner as pl

    if result is None:
        return NONE_EMPTY
    if any("refused" not in d for d in result.drawn):
        return None
    if result.error == pl.TIMED_OUT:
        return NONE_TIMEOUT
    if result.spoken == pl.SAY_NO_GRANT:
        return NONE_NO_GRANT
    if result.error is not None:
        return NONE_ERROR
    if result.drawn:
        return NONE_REFUSED
    return NONE_WORDS


class SessionTrace:
    """One debug session's counts and turn numbers. Holds no secret: the text
    it is handed goes to swarm-api, which masks it before it is kept."""

    def __init__(self, engine: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.engine = engine
        self._clock = clock
        self.counts: dict[str, int] = dict.fromkeys(SESSION_EVENTS, 0)
        self._mark: dict[str, int] = dict(self.counts)
        self.turns = 0
        #: When the person's last transcript piece (a delta or a final) arrived.
        self.last_piece_at: float | None = None

    def note(self, name: str) -> None:
        self.counts[name] += 1
        if name in ("pieces", "finals", "empty_finals"):
            self.last_piece_at = self._clock()

    def turn(self, *, transcript: list[tuple[str, str]], handoff: str | None,
             result: "TurnResult | None", told: str, heard_at: float | None = None,
             none: str | None = None) -> dict[str, Any]:
        """The body of one finished turn, told back now; `result` None is a
        turn the planner never ran (an empty transcript, unless `none` names
        another reason)."""
        self.turns += 1
        if told:
            self.counts["replies"] += 1
        events = {k: self.counts[k] - self._mark[k] for k in TURN_EVENTS}
        self._mark = dict(self.counts)
        heard = heard_at if heard_at is not None else self.last_piece_at
        secs = round(max(0.0, self._clock() - heard), 3) if heard is not None else None
        return {
            "engine": self.engine,
            "session_events": dict(self.counts),
            "turn": {
                "n": self.turns,
                "secs": secs,
                "lines": [{"who": who if who in ("person", "voice") else "voice", "text": text}
                          for who, text in transcript],
                "handoff": handoff or None,
                "read": list(result.read_targets) if result is not None else [],
                "drawn": list(result.drawn) if result is not None else [],
                "none": none or none_code(result),
                "told": told,
                "events": events,
            },
        }


async def send(http: httpx.AsyncClient, *, base_url: str, id_tokens: tools_read.IdTokens,
               grant: SessionGrant | None, body: dict[str, Any]) -> bool:
    """POST one turn's trace through the session grant; never raises. A
    failure is logged once with WRITE_FAILED and the exception's type."""
    try:
        api = tools_read.SwarmApi.for_session(http, base_url=base_url, id_tokens=id_tokens, grant=grant)
        path = TRACE_ROUTE.format(session_id=api.session_id)
        # tools_read's one request path: the planner's own headers and refusals.
        await api._call("POST", TRACE_ROUTE, path, json_body=body)
        return True
    except Exception as exc:  # noqa: BLE001 -- a trace never fails a turn; the type only
        log.warning("%s session=%s (%s)", WRITE_FAILED,
                    grant.session_id if grant is not None else None, type(exc).__name__)
        return False


def spawn(coro: Any, tasks: set[asyncio.Task]) -> asyncio.Task:
    """Run a trace write beside the turn, held in `tasks` until it ends."""
    task = asyncio.create_task(coro, name="voice-trace")
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


__all__ = [
    "ENGINE_LIVE",
    "ENGINE_OWN",
    "NONE_EMPTY",
    "NONE_ERROR",
    "SESSION_EVENTS",
    "SessionTrace",
    "TRACE_ROUTE",
    "TURN_EVENTS",
    "WRITE_FAILED",
    "none_code",
    "send",
    "spawn",
]
