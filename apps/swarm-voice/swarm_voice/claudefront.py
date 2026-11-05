"""Own speech's fast Claude front: answers each spoken sentence at once and
hands planning and actions to the planner (docs/design/voice-it-own-speech.md
§3.3 step 4, §10.1, §14 L12 and §14.1).

THE SHAPE OF textchat.TextSessions: `start`, `turn`, `end`, `end_all` and
`counts`, so swarm-voice's main.py picks one or the other by the session's mode
(`speech` here, `text` there) and swaps nothing else.

THE MODEL. The haiku class (claudeturn.front_model(), VOICE_FRONT_MODEL from
Terraform's `local.runner_models.haiku`), picked by first-reply latency: the
front says one or two sentences and makes one choice, answer or `delegate`.
The planner keeps the planning class (claudeturn.planner_model(),
`local.runner_models.fable`).

ONE WARM CHILD PER SESSION. `ClaudeCode.converse` starts `claude -p
--input-format stream-json` at the session's start, with claudeturn.py's
rules (built environment, `--tools ""`, `--strict-mcp-config` offering only
`delegate`, a private HOME removed at the end, scrubbed output); each final is
one user message on its stdin. The child never sees the grant: `delegate`
runs in THIS process, on `Planner.delegate`, with the session's person and
grant.

ONE ACCOUNT FOR THE SESSION. `PlannerAccounts(broker, holder="voice-front")`
assigns one Claude account of the person's own workspace pool at the start,
renews it every 240 s while the session lives and releases it on end, idle,
tab close (swarm-api's end) and `end_all`; else it lapses on the broker's TTL.
A warm child keeps its token, so a per-turn release would run a model on an
account the pool counts free: the hold counts one agent while the session
lives (CONTRACT.md invariant 3). A renewal the broker refuses as not held
stops the child at once. Delegations lease their own account per turn, as
today (plannerlease.HOLDER).

A REFUSAL AT START (pool full, broker refusal, no CLI) is `FrontRefused`,
nothing held: main.py falls back to text mode (§4.1). A dead child or a lost
hold mid-session re-leases ONCE, excluding that account, with the last turns
as the first message; if that fails too the turn raises `FrontDown`, the
page's `engine down` (§4.5).

DELEGATIONS RUN IN THE BACKGROUND, one at a time per session, at most two
waiting behind it; a third is refused to the model, which says so. The turn
answers with the front's sentence and the delegation's id at once; swarm-api
waits on `GET /delegations/{id}` (`delegations_router`) and speaks the
planner's two sentences at the next silence.

NO OPENAI. Nothing here, nor the planner, calls an OpenAI host; the only
network the front's own code reaches is the quota broker (and the planner's
swarm-api). NOTHING SECRET IN A REPR OR A LOG: the token lives in the lease's
`Bearer` and the child's built environment; reprs name ids and counts, logs
name ids, counts and exception types, never text the person said.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from . import claudeturn, prompts
from .credential import CredentialError
from .grant import SessionGrant
from .plannerlease import FRONT_HOLDER, LeaseFailed, PlannerAccounts, PlannerLease, PoolFull

if TYPE_CHECKING:
    from .planner import Planner, TurnResult

log = logging.getLogger("swarm_voice.claudefront")

MODE = "speech"
DELEGATE_TOOL = "delegate"
#: A turn's text, as textchat.MAX_TURN_CHARS.
MAX_TURN_CHARS = 4000
#: Turns kept for the planner's transcript and a re-lease's first message.
HISTORY_TURNS = 200
#: The last turns a re-leased child is told.
RESUME_TURNS = 12
#: The front's own answer to one sentence: far below the planner's 240 s.
TURN_SECONDS = 60.0
#: No sentence for this long ends the session (textchat.IDLE_SECONDS).
IDLE_SECONDS = 900.0
#: Delegations waiting behind the running one, per session.
MAX_WAITING = 2
#: Finished delegations kept for `GET /delegations/{id}`, per session.
KEEP_DONE = 16
#: The longest `GET /delegations/{id}?wait=` holds (planner.TURN_SECONDS).
MAX_WAIT_SECONDS = 240.0
#: The child's model rounds per sentence: an answer, or `delegate` then an answer.
MAX_ROUNDS = 3
BILLING = "subscription"

SAY_EMPTY = "I didn't catch an answer to that; could you say it again?"
SAY_BUSY = "refused: two requests are already waiting for the planner; tell the person to wait for them"

FRONT_INSTRUCTIONS = (
    "You are the voice of Voice it, a planning partner for the person talking to you; what you "
    "write is spoken aloud. Answer every message at once in one or two short spoken sentences: "
    "no lists, no markdown, no code. "
    "When the person asks for something to be planned, drawn, shown, looked up, explained from "
    "SwarmCloud, or done (a task, a merge, a cancel, an approval), call the `delegate` tool with "
    "what they asked for in one sentence, then say in a few words that you are on it, for example "
    "\"Looking at that now.\"; the planner's answer is spoken to them later, so never invent a "
    "plan, a status or a result yourself, and never say an action is done. "
    "Anything else, small talk or a question about what was already said, answer yourself. "
    "Never ask for, repeat or read out a password, key, token or any other credential; if the "
    "person starts to read one aloud, stop them and tell them to use the console's secret form."
)


def delegate_tool() -> dict[str, Any]:
    """The front's one tool, as claudeturn.ToolBridge offers it."""
    return {
        "name": DELEGATE_TOOL,
        "description": ("Hand the person's request to SwarmCloud's planner, which draws cards on "
                        "their screen, acts for them and answers in at most two sentences, later."),
        "input_schema": {
            "type": "object",
            "properties": {"request": {"type": "string",
                                       "description": "What the person asked for, in a sentence."}},
            "required": ["request"],
            "additionalProperties": False,
        },
    }


class FrontRefused(Exception):
    """No front could start: `reason` is the pool's refusal or the cause.
    Nothing is held; the session falls back to text mode."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FrontDown(Exception):
    """The session's child or hold was lost and one re-lease could not
    replace it: the page's `engine down`."""


class FrontSessionEnded(Exception):
    """No live front session by that id."""


class SessionExists(Exception):
    """A front session by that id is already held or starting."""


@dataclass
class Delegation:
    """One request handed to the planner. Holds no secret."""

    delegation_id: str
    voice_id: str
    request: str
    state: str = "waiting"
    spoken: str = ""
    cards: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def __repr__(self) -> str:
        return f"Delegation(id={self.delegation_id!r}, state={self.state!r})"

    def to_api(self) -> dict[str, Any]:
        return {"delegation_id": self.delegation_id, "voice_session_id": self.voice_id, "state": self.state,
                "spoken": self.spoken, "cards": list(self.cards)}


@dataclass
class FrontAnswer:
    """One answered sentence: the front's reply, and the delegation it started."""

    turn_id: str
    reply: str
    delegation_id: str | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)


class FrontSession:
    """One person's spoken conversation on a session-long Claude child."""

    mode = MODE

    def __init__(self, voice_id: str, *, person: str, grant: SessionGrant | None,
                 clock: Callable[[], float]) -> None:
        self.voice_id = voice_id
        self.person = person
        self.grant = grant
        self._clock = clock
        self.started_at = clock()
        self.last_turn_at = self.started_at
        self.turns: list[tuple[str, str]] = []
        self.lease: PlannerLease | None = None
        self.conversation: Any = None
        self.lost = False
        self.releases = 0
        self.ended = False
        self.reason: str | None = None
        self.lock = asyncio.Lock()
        self.renew_task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.runner: asyncio.Task | None = None
        self.queue: deque[Delegation] = deque()
        self.running: Delegation | None = None
        self.delegations: dict[str, Delegation] = {}
        #: This turn's delegation id, set by the `delegate` handler.
        self.delegated: str | None = None
        #: Token counts by model: the front's pin and the planner's.
        self.usage: dict[str, dict[str, int]] = {}

    def __repr__(self) -> str:
        account = self.lease.account_id if self.lease is not None else None
        return f"FrontSession(voice_id={self.voice_id!r}, account_id={account!r}, turns={len(self.turns)})"

    @property
    def usage_seconds(self) -> int:
        return int(max(0.0, self._clock() - self.started_at))

    def add_usage(self, model: str, counts: dict[str, Any]) -> None:
        line = self.usage.setdefault(model, {})
        for name, value in counts.items():
            if isinstance(value, int) and not isinstance(value, bool):
                line[name] = line.get(name, 0) + value

    def usage_report(self) -> dict[str, Any]:
        """The ledger's line: counts `by_model` (both pins), billed as a subscription."""
        return {"by_model": {m: dict(c) for m, c in self.usage.items()}, "billing": BILLING}


class FrontSessions:
    """Every own-speech front this process holds, by swarm-api's voice session id."""

    def __init__(self, *, accounts: PlannerAccounts, planner: "Planner | None" = None,
                 claude: claudeturn.ClaudeCode | None = None, clock: Callable[[], float] = time.monotonic,
                 idle_seconds: float = IDLE_SECONDS, turn_seconds: float = TURN_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if accounts.holder != FRONT_HOLDER:
            raise ValueError(f"the front's accounts are held as {FRONT_HOLDER!r}")
        self._accounts = accounts
        self._planner = planner
        self._claude = claude or claudeturn.ClaudeCode(model=claudeturn.front_model(), max_turns=MAX_ROUNDS)
        self._clock = clock
        self._idle = idle_seconds
        self._turn_seconds = turn_seconds
        self._sleep = sleep
        self._held: dict[str, FrontSession] = {}
        self._starting: set[str] = set()

    @property
    def model(self) -> str:
        return self._claude.model

    def get(self, voice_id: str) -> FrontSession | None:
        return self._held.get(voice_id)

    def counts(self) -> dict[str, int]:
        return {"front_sessions": len(self._held),
                "front_delegations": sum(len(s.queue) + (s.running is not None) for s in self._held.values())}

    def delegation(self, delegation_id: str) -> Delegation | None:
        for session in self._held.values():
            found = session.delegations.get(delegation_id)
            if found is not None:
                return found
        return None

    # -- start --------------------------------------------------------------

    async def start(self, voice_id: str, *, person: str, grant: SessionGrant | None = None,
                    exists: Callable[[str], bool] = lambda _id: False) -> FrontSession:
        """Hold one Claude account for the session and start its warm child.
        Raises SessionExists, or FrontRefused with nothing held."""
        if voice_id in self._held or voice_id in self._starting or exists(voice_id):
            raise SessionExists(voice_id)
        self._starting.add(voice_id)
        try:
            session = FrontSession(voice_id, person=person, grant=grant, clock=self._clock)
            try:
                await self._attach(session, exclude=())
            except (PoolFull, CredentialError, OSError) as exc:
                reason = exc.reason if isinstance(exc, PoolFull) else type(exc).__name__
                log.info("voice session %s: no fast front (%s); text mode", voice_id, reason)
                raise FrontRefused(reason) from None
            self._held[voice_id] = session
            session.idle_task = asyncio.create_task(self._watch_idle(session), name=f"voice-front-idle-{voice_id}")
            log.info("voice session %s: fast front started on account %s (%s)", voice_id,
                     session.lease.account_id if session.lease else None, self.model)
            return session
        finally:
            self._starting.discard(voice_id)

    async def _attach(self, session: FrontSession, *, exclude: tuple[str, ...]) -> None:
        """Lease an account and start the child on it; on any failure nothing
        stays held. Raises PoolFull, CredentialError or OSError."""
        lease = await self._accounts.assign(person=session.person, voice_session_id=session.voice_id,
                                            exclude=exclude)
        try:
            token = lease.bearer.reveal()
            conversation = await self._claude.converse(
                system=FRONT_INSTRUCTIONS, token=token, tools=[delegate_tool()],
                handler=self._handler(session), known=(token,))
        except BaseException:
            await self._accounts.release(lease)
            raise
        session.lease, session.conversation, session.lost = lease, conversation, False
        session.renew_task = asyncio.create_task(self._renew(session, lease),
                                                 name=f"voice-front-lease-{session.voice_id}")

    async def _detach(self, session: FrontSession) -> None:
        """Stop the child and give its account back."""
        task, session.renew_task = session.renew_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        conversation, session.conversation = session.conversation, None
        if conversation is not None:
            with contextlib.suppress(Exception):
                await conversation.close()
        lease, session.lease = session.lease, None
        if lease is not None:
            await self._accounts.release(lease)

    async def _renew(self, session: FrontSession, lease: PlannerLease) -> None:
        while not lease.released and not session.ended:
            await self._sleep(self._accounts.renew_seconds)
            if lease.released or session.ended:
                return
            try:
                await self._accounts.renew(lease)
            except LeaseFailed as exc:
                if exc.status == 409:
                    # Not held any more: the account counts free, so the
                    # child on its token stops now; the next sentence re-leases.
                    log.warning("voice session %s: the front's hold was lost; child stopped", session.voice_id)
                    session.lost = True
                    conversation = session.conversation
                    if conversation is not None:
                        with contextlib.suppress(Exception):
                            await conversation.close()
                    return
                log.warning("voice session %s: front lease renewal failed: %s", session.voice_id, exc)
            except CredentialError as exc:
                log.warning("voice session %s: front lease renewal failed: %s", session.voice_id, exc)

    async def _watch_idle(self, session: FrontSession) -> None:
        while not session.ended:
            wait = session.last_turn_at + self._idle - self._clock()
            if wait <= 0 and not session.lock.locked() and session.running is None:
                log.info("voice session %s: fast front idle; ended", session.voice_id)
                await self._forget(session, "idle")
                return
            await self._sleep(max(wait, 1.0))

    # -- one sentence -------------------------------------------------------

    async def turn(self, voice_id: str, text: str) -> FrontAnswer:
        """Answer one final at once. Raises FrontSessionEnded, ValueError or
        FrontDown; the person's text is never logged."""
        session = self._held.get(voice_id)
        if session is None or session.ended:
            raise FrontSessionEnded(voice_id)
        text = text.strip()
        if not 1 <= len(text) <= MAX_TURN_CHARS:
            raise ValueError(f"a turn is 1 to {MAX_TURN_CHARS} characters")
        async with session.lock:
            if session.ended:
                raise FrontSessionEnded(voice_id)
            session.last_turn_at = self._clock()
            session.delegated = None
            outcome = await self._say(session, text)
            if session.ended:
                raise FrontSessionEnded(voice_id)
            known = self._known(session)
            reply = prompts.spoken(outcome.result_text, secrets_=known) or SAY_EMPTY
            session.add_usage(self.model, outcome.usage)
            session.turns = [*session.turns, ("person", text), ("voice", reply)][-HISTORY_TURNS:]
            session.last_turn_at = self._clock()
            turn_id = "vf_" + secrets.token_hex(12)
            log.info("voice session %s: front turn %s answered (%d chars, delegated %s)", voice_id, turn_id,
                     len(reply), session.delegated is not None)
            return FrontAnswer(turn_id, reply, session.delegated)

    async def _say(self, session: FrontSession, text: str) -> claudeturn.ChildOutcome:
        """The child's answer to `text`; a dead child or a lost hold re-leases
        once, excluding that account, with the last turns first."""
        conversation = session.conversation
        if conversation is not None and conversation.alive and not session.lost:
            try:
                return await asyncio.wait_for(conversation.say(text), self._turn_seconds)
            except (claudeturn.ChildGone, asyncio.TimeoutError) as exc:
                log.warning("voice session %s: the front's child failed (%s)", session.voice_id,
                            type(exc).__name__)
        if session.releases >= 1:
            raise FrontDown(session.voice_id)
        session.releases += 1
        excluded = (session.lease.account_id,) if session.lease is not None else ()
        await self._detach(session)
        try:
            await self._attach(session, exclude=excluded)
        except (PoolFull, CredentialError, OSError) as exc:
            log.warning("voice session %s: the front could not be re-leased (%s)", session.voice_id,
                        type(exc).__name__)
            raise FrontDown(session.voice_id) from None
        try:
            return await asyncio.wait_for(session.conversation.say(self._resume(session, text)),
                                          self._turn_seconds)
        except (claudeturn.ChildGone, asyncio.TimeoutError):
            raise FrontDown(session.voice_id) from None

    @staticmethod
    def _resume(session: FrontSession, text: str) -> str:
        lines = [f"{who}: {said}" for who, said in session.turns[-RESUME_TURNS:]]
        return ("The conversation so far, newest last (you are resuming it):\n"
                + prompts.fence("transcript", "\n".join(lines) or "(nothing said yet)")
                + "\nThe person now says: " + text)

    def _known(self, session: FrontSession) -> tuple[str, ...]:
        lease = session.lease
        return (lease.bearer.reveal(),) if lease is not None else ()

    # -- delegations --------------------------------------------------------

    def _handler(self, session: FrontSession) -> claudeturn.ToolHandler:
        async def handler(name: str, args: dict) -> tuple[str, bool]:
            if name != DELEGATE_TOOL:
                return f"refused: {name[:64]!r} is not a tool offered", True
            request = args.get("request")
            request = request.strip() if isinstance(request, str) else ""
            if self._planner is None:
                return "refused: no planner is configured; tell the person you cannot plan this", True
            if session.delegated is not None:
                return "already delegated this turn; just tell the person you are on it", False
            if len(session.queue) >= MAX_WAITING:
                return SAY_BUSY, True
            delegation = Delegation("vd_" + secrets.token_hex(12), session.voice_id, request[:MAX_TURN_CHARS])
            session.delegations[delegation.delegation_id] = delegation
            session.queue.append(delegation)
            session.delegated = delegation.delegation_id
            if session.runner is None or session.runner.done():
                session.runner = asyncio.create_task(self._run_delegations(session),
                                                     name=f"voice-front-delegations-{session.voice_id}")
            return "delegated; the planner's answer is spoken later. Say in a few words that you are on it.", False
        return handler

    async def _run_delegations(self, session: FrontSession) -> None:
        """The session's delegations, one at a time, oldest first."""
        while session.queue and not session.ended:
            delegation = session.queue.popleft()
            session.running, delegation.state = delegation, "running"
            try:
                result = await self._delegate(session, delegation)
                delegation.spoken, delegation.cards = result.spoken, list(result.cards)
                delegation.error = result.error
                model = str(result.usage.get("model") or claudeturn.planner_model())
                session.add_usage(model, result.usage)
                session.turns = [*session.turns, ("voice", result.spoken)][-HISTORY_TURNS:]
                delegation.state = "done"
            except asyncio.CancelledError:
                delegation.state, delegation.error = "cancelled", "the session ended"
                raise
            except Exception as exc:  # noqa: BLE001 -- the type only, never the message
                log.warning("voice session %s: delegation %s failed (%s)", session.voice_id,
                            delegation.delegation_id, type(exc).__name__)
                delegation.state, delegation.error = "failed", type(exc).__name__
            finally:
                session.running = None
                delegation.done.set()
                self._trim(session)
            log.info("voice session %s: delegation %s %s (%d card(s))", session.voice_id,
                     delegation.delegation_id, delegation.state, len(delegation.cards))

    async def _delegate(self, session: FrontSession, delegation: Delegation) -> "TurnResult":
        assert self._planner is not None
        transcript = [*session.turns, ("person", delegation.request)] if delegation.request else list(session.turns)
        return await self._planner.delegate(person=session.person, grant=session.grant, transcript=transcript,
                                            delegation_id=delegation.delegation_id)

    @staticmethod
    def _trim(session: FrontSession) -> None:
        done = [d for d in session.delegations.values() if d.done.is_set()]
        for old in done[:-KEEP_DONE]:
            session.delegations.pop(old.delegation_id, None)

    # -- the end ------------------------------------------------------------

    async def _forget(self, session: FrontSession, reason: str) -> None:
        if session.ended:
            return
        session.ended = True
        session.reason = reason
        # The grant dies with the session (voice-it.md §3.6).
        session.grant = None
        if self._planner is not None:
            self._planner.forget(session.voice_id)
        if self._held.get(session.voice_id) is session:
            del self._held[session.voice_id]
        for delegation in session.queue:
            delegation.state, delegation.error = "cancelled", "the session ended"
            delegation.done.set()
        session.queue.clear()
        for name in ("idle_task", "runner"):
            task = getattr(session, name)
            setattr(session, name, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._detach(session)

    async def end(self, voice_id: str, reason: str = "client_close") -> FrontSession | None:
        session = self._held.get(voice_id)
        if session is None:
            return None
        await self._forget(session, reason)
        log.info("voice session %s: fast front ended (%s)", voice_id, reason)
        return session

    async def end_all(self) -> None:
        for voice_id in list(self._held):
            await self.end(voice_id, "shutdown")


def delegations_router(front: FrontSessions | Callable[[], FrontSessions | None],
                       caller: Callable[..., Any]) -> APIRouter:
    """`GET /delegations/{id}`: a delegation's state, and once done its spoken
    sentences and cards. `wait` (seconds, at most MAX_WAIT_SECONDS) holds the
    answer until the delegation is done. `caller` is main.py's ID-token check;
    `front` the sessions, or a function returning them (main.py's app state)."""
    router = APIRouter()

    def sessions() -> FrontSessions | None:
        return front if isinstance(front, FrontSessions) else front()

    @router.get("/delegations/{delegation_id}")
    async def get_delegation(delegation_id: str, wait: float = Query(default=0.0, ge=0.0, le=MAX_WAIT_SECONDS),
                             _caller: Any = Depends(caller)) -> dict[str, Any]:
        held = sessions()
        delegation = held.delegation(delegation_id) if held is not None else None
        if delegation is None:
            raise HTTPException(404, "no such delegation")
        if wait and not delegation.done.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(delegation.done.wait(), wait)
        return delegation.to_api()

    return router

