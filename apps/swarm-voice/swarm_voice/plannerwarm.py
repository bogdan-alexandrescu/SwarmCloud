"""The planner kept warm: one account lease and one Claude Code child per voice
session, across its delegated turns (docs/design/voice-it.md §3.6; planner.py
runs the turns, plannerlease.py leases, claudeturn.py is the child).

WHY. A cold turn pays the broker's assign and Claude Code's start before its
first model call, and both again at the next delegation. A warm turn sends one
user message to the child the session's last turn left running.

WHAT RELEASES BOTH, the child first and then its lease: the session's end, a
pause, a spend cap (`cool`), WARM_IDLE_SECONDS with no delegated turn, a lost
hold, and the process's shutdown (`aclose`). A turn that is running is never
cut for it: the release follows the turn, and a child no turn used again is
gone by the idle period whatever else was missed.

IT NEVER FAILS A TURN. `turn` yields None when the turn must run as it did
before (its own lease and its own child): the session's warm child was found
dead, its hold was lost, another turn of the session has it, or the child could
not start. A child that fails in the middle of a turn that has done nothing yet
raises `ColdAgain` through planner.py, which then runs that turn cold. A
refused lease is not retried: PoolFull and CredentialError reach the planner,
whose spoken refusals are unchanged.

NOTHING CROSSES A SESSION. A slot is found by the voice session's id, and its
child is used only when the whole `key_of` matches: the session, the person,
the project, the grant, the model and the tools offered. Anything else stops
the child and gives its lease back before a new one is leased. The child's
conversation lives in that process and its private HOME alone
(`--no-session-persistence`), which `Conversation.close` removes, so a later
child of any session starts with none of it. Its tool calls reach the running
turn's handler through a `Gate`, which is shut between turns: a late call is
refused and nothing is written.

THE HOLD is renewed every `PlannerAccounts.renew_seconds` while it is warm. A
renewal refused as gone (404, 409), or two failed in a row (the hold's ten
minutes would lapse before a third), stops the child: none may run on an
account the pool counts free.

NOTHING SECRET IS KEPT BUT THE LEASE'S OWN `Bearer`: the key holds a digest of
the grant, and no repr names a token, a grant or a person.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from typing import Any, AsyncIterator, Callable

from . import prompts
from .claudeturn import ChildGone, ChildOutcome, ClaudeCode, Conversation, ToolHandler
from .credential import CredentialError
from .plannerlease import LeaseFailed, PlannerAccounts, PlannerLease

log = logging.getLogger("swarm_voice.plannerwarm")

#: How long a session keeps its planner's account and child after its last
#: delegated turn. Two minutes: long enough for the person to look at what was
#: drawn and ask for the next thing, short enough that a conversation gone
#: quiet gives its slot back soon. An account holds six agents by default
#: (quota_broker.accounts.DEFAULT_MAX_AGENTS) and exactly one until its first
#: usage reading, so an idle hold on a small pool can be the only slot there
#: is. Below RENEW_SECONDS, so an idle hold is renewed at most once.
WARM_IDLE_SECONDS = 120.0
#: How long a reused child may write nothing after a message before its turn
#: falls back. Claude Code writes its `init` event before the model is called,
#: so ten seconds of nothing is a hung process and not a slow answer; it leaves
#: the cold turn nearly all of planner.TURN_SECONDS.
SIGN_SECONDS = 10.0
#: The most sessions whose idle child is kept, over the whole process: past it
#: the longest idle one lets go. A child can take several hundred MiB of the
#: pod's 2Gi (kubernetes/swarm-system/swarm-voice.yaml), which the live
#: sidebands and the fronts' children share, and an OOM kill would end every
#: session. A child whose turn is running is never counted or cut.
MAX_IDLE = 2
#: Consecutive failed renewals after which the hold is taken as lost.
RENEWALS_LOST = 2

#: Put before a reused child's message: Claude Code notes the interrupt that
#: ended the turn before it in the conversation, as if the person had made it.
REUSED_NOTE = ("Your previous turn ended when `speak` was taken; an interruption noted there was not the "
               "person's. What follows is a new turn, and replaces the transcript and brief you had.\n\n")
#: What a tool call outside a running turn is answered.
REFUSED = "refused: no turn is running"


class ColdAgain(Exception):
    """A warm child failed before its turn did anything: the turn runs cold.
    `usage` is what the failed attempt had used."""

    def __init__(self, usage: dict[str, int] | None = None) -> None:
        super().__init__("the planner's warm child failed")
        self.usage = dict(usage or {})


def key_of(*, voice_session_id: str, person: str, project_id: str, grant: tuple[str, ...], model: str,
           tools: list[dict[str, Any]]) -> str:
    """Whose child it is: a digest of everything a reused child must match."""
    parts = [voice_session_id, person, project_id, list(grant), model, tools]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Gate:
    """The child's tool handler: the running turn's while one runs, else a refusal."""

    def __init__(self) -> None:
        self._handler: ToolHandler | None = None
        self._running = 0
        self._still = asyncio.Event()
        self._still.set()

    async def __call__(self, name: str, args: dict) -> tuple[str, bool]:
        handler = self._handler
        if handler is None:
            return REFUSED, True
        self._running += 1
        self._still.clear()
        try:
            return await handler(name, args)
        finally:
            self._running -= 1
            if not self._running:
                self._still.set()

    def open(self, handler: ToolHandler) -> None:
        self._handler = handler

    def shut(self) -> None:
        self._handler = None

    async def still(self) -> None:
        """Wait for the calls already running: what they write is the turn's."""
        await self._still.wait()


class Held:
    """One session's lease and child."""

    def __init__(self, accounts: PlannerAccounts, *, key: str, lease: PlannerLease,
                 sign_seconds: float) -> None:
        self._accounts = accounts
        self.key = key
        self.lease = lease
        self.gate = Gate()
        self.conversation: Conversation | None = None
        self.turns = 0
        #: The turn left the child unfit for another.
        self.failed = False
        #: The hold is gone from under it.
        self.lost = False
        self._sign_seconds = sign_seconds
        self._renewer: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"Held({self.lease!r}, alive={self.alive}, turns={self.turns})"

    @property
    def alive(self) -> bool:
        return self.conversation is not None and self.conversation.alive

    @property
    def reused(self) -> bool:
        """Whether the turn now running is not the child's first."""
        return self.turns > 1

    def start_renewing(self, on_lost: Callable[["Held"], None]) -> None:
        self._renewer = asyncio.create_task(self._renew(on_lost),
                                            name=f"planner-warm-lease-{self.lease.voice_session_id}")

    async def _renew(self, on_lost: Callable[["Held"], None]) -> None:
        failures = 0
        while True:
            await asyncio.sleep(self._accounts.renew_seconds)
            try:
                await self._accounts.renew(self.lease)
                failures = 0
                continue
            except CredentialError as exc:
                failures += 1
                gone = isinstance(exc, LeaseFailed) and exc.status in (404, 409)
                log.warning("voice session %s: the warm planner's renewal failed: %s",
                            self.lease.voice_session_id, exc)
            if gone or failures >= RENEWALS_LOST:
                self.lost = True
                on_lost(self)
                return

    async def say(self, text: str, *, handler: ToolHandler, done: asyncio.Event) -> ChildOutcome:
        """One turn on the child, its tools `handler`'s while it runs; the turn
        is over once its closing `speak` is (claudeturn.Conversation.say).
        A turn that did not close, or whose child is gone (ChildGone), ends
        only when no call of it still runs, so the planner never runs one again
        that is yet to write something."""
        assert self.conversation is not None
        self.turns += 1
        if self.reused:
            text = REUSED_NOTE + text
        self.gate.open(handler)
        try:
            outcome = await self.conversation.say(text, done=done, closes=prompts.SPEAK_TOOL,
                                                  sign_seconds=self._sign_seconds if self.reused else None)
        except ChildGone:
            self.gate.shut()
            await self.gate.still()
            raise
        finally:
            self.gate.shut()
        if not outcome.stopped:
            await self.gate.still()
        return outcome

    async def close(self) -> None:
        """The child, then its lease. Never raises for either."""
        self.gate.shut()
        renewer, self._renewer = self._renewer, None
        if renewer is not None and renewer is not asyncio.current_task():
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewer
        try:
            if self.conversation is not None:
                await self.conversation.close()
        except Exception as exc:  # noqa: BLE001 -- the lease still goes back
            log.warning("voice session %s: the warm planner's child did not close (%s)",
                        self.lease.voice_session_id, type(exc).__name__)
        finally:
            await self._accounts.release(self.lease)


class Slot:
    """One voice session's place: what it holds, and whether a turn has it."""

    def __init__(self) -> None:
        self.held: Held | None = None
        self.busy = False
        #: Told to let go while a turn runs: the release follows the turn.
        self.let_go: str | None = None
        #: The hold was lost since the last turn: the next one runs cold.
        self.cold_next = False
        self.idle: asyncio.TimerHandle | None = None
        #: When its last turn ended, by the loop's clock.
        self.idle_at = 0.0

    def __repr__(self) -> str:
        return f"Slot({self.held!r}, busy={self.busy})"

    def take(self) -> Held | None:
        """What it held, no longer held by it; its idle timer stopped."""
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        held, self.held = self.held, None
        return held


class WarmPlanners:
    """Every voice session's warm planner (module docstring)."""

    def __init__(self, accounts: PlannerAccounts, *, idle_seconds: float = WARM_IDLE_SECONDS,
                 sign_seconds: float = SIGN_SECONDS, max_idle: int = MAX_IDLE) -> None:
        self._accounts = accounts
        self._idle_seconds = idle_seconds
        self._sign_seconds = sign_seconds
        self._max_idle = max_idle
        self._slots: dict[str, Slot] = {}
        #: Every lease and child not yet given back, whichever slot had it.
        self._held: set[Held] = set()
        self._closing: dict[Held, asyncio.Task[None]] = {}

    def __repr__(self) -> str:
        return f"WarmPlanners(sessions={len(self._slots)}, held={len(self._held)})"

    # -- a turn ---------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def turn(self, *, voice_session_id: str, key: str, person: str, claude: ClaudeCode, system: str,
                   tools: list[dict[str, Any]], known: tuple[str, ...]) -> AsyncIterator[Held | None]:
        """What this turn runs on: the session's warm child, a new one when it
        has none that matches `key`, or None (run it cold). A turn that fails,
        raises or is cancelled gives both back before this returns; otherwise
        both stay for `idle_seconds`. Raises PoolFull or CredentialError when a
        new child's lease is refused."""
        slot = self._slots.setdefault(voice_session_id, Slot())
        if slot.busy:
            yield None
            return
        slot.busy, slot.let_go = True, None
        finished = False
        try:
            held = await self._ready(slot, key, person=person, voice_session_id=voice_session_id, claude=claude,
                                     system=system, tools=tools, known=known)
            yield held
            finished = held is not None and not held.failed
        finally:
            slot.busy = False
            await self._after(voice_session_id, slot, finished)

    async def _ready(self, slot: Slot, key: str, *, person: str, voice_session_id: str, claude: ClaudeCode,
                     system: str, tools: list[dict[str, Any]], known: tuple[str, ...]) -> Held | None:
        if slot.cold_next:
            slot.cold_next = False
            return None
        if slot.held is not None:
            if slot.held.key == key and slot.held.alive and not slot.held.lost:
                if slot.idle is not None:
                    slot.idle.cancel()
                    slot.idle = None
                return slot.held
            dead = slot.held.key == key
            old = slot.take()
            assert old is not None
            log.info("voice session %s: the warm planner is replaced (%s)", voice_session_id,
                     "its child is gone" if dead else "it is another's")
            await self._close(old)
            if dead:
                return None
        # Released however the start ends: a hold never outlives a child that did not start.
        held = Held(self._accounts, key=key, lease=await self._accounts.assign(
            person=person, voice_session_id=voice_session_id), sign_seconds=self._sign_seconds)
        self._held.add(held)
        slot.held = held
        try:
            held.conversation = await claude.converse(
                system=system, token=held.lease.bearer.reveal(), tools=tools, handler=held.gate,
                known=(held.lease.bearer.reveal(), *known))
        except OSError as exc:
            log.warning("voice session %s: the warm planner could not start Claude Code (%s)", voice_session_id,
                        type(exc).__name__)
            slot.take()
            await self._close(held)
            return None
        held.start_renewing(lambda lost: self._lost(voice_session_id, lost))
        return held

    async def _after(self, voice_session_id: str, slot: Slot, finished: bool) -> None:
        """The turn is over: give both back, or keep them for the idle period."""
        held = slot.held
        if held is None:
            self._drop(voice_session_id, slot)
            return
        if not finished:
            slot.take()
            self._drop(voice_session_id, slot)
            await self._close(held)
            return
        why = slot.let_go or ("its hold was lost" if held.lost else None) or (
            None if held.alive else "its child did not settle")
        if why is not None:
            # The answer does not wait for it.
            self._release(voice_session_id, slot, why)
            return
        loop = asyncio.get_running_loop()
        slot.idle_at = loop.time()
        slot.idle = loop.call_later(self._idle_seconds, self._release, voice_session_id, slot, "idle")
        idle = sorted(((s.idle_at, v) for v, s in self._slots.items() if s.held is not None and not s.busy))
        for _, longest in idle[:max(0, len(idle) - self._max_idle)]:
            self._release(longest, self._slots[longest], "another session's planner needs the memory")

    # -- letting go -----------------------------------------------------------

    def cool(self, voice_session_id: str, why: str) -> None:
        """Let the session's account and child go: now, or after the turn it is
        running. Never waits and never raises."""
        slot = self._slots.get(voice_session_id)
        if slot is None:
            return
        if slot.busy:
            slot.let_go = why
        else:
            self._release(voice_session_id, slot, why)

    def _lost(self, voice_session_id: str, held: Held) -> None:
        slot = self._slots.get(voice_session_id)
        if slot is None or slot.held is not held or slot.busy:
            return
        # The slot stays: its next turn leases and starts afresh, as before.
        slot.cold_next = True
        slot.take()
        log.warning("voice session %s: the warm planner's hold is lost; its child is stopped", voice_session_id)
        self._close_soon(held)

    def _release(self, voice_session_id: str, slot: Slot, why: str) -> None:
        held = slot.take()
        # A session told to let go owes nothing to its next turn.
        slot.cold_next = False
        self._drop(voice_session_id, slot)
        if held is not None:
            log.info("voice session %s: the warm planner lets its account and child go (%s) after %d turn(s)",
                     voice_session_id, why, held.turns)
            self._close_soon(held)

    def _drop(self, voice_session_id: str, slot: Slot) -> None:
        if slot.held is None and not slot.busy and not slot.cold_next \
                and self._slots.get(voice_session_id) is slot:
            del self._slots[voice_session_id]

    def _close_soon(self, held: Held) -> asyncio.Task[None]:
        task = self._closing.get(held)
        if task is None:
            task = asyncio.create_task(self._closed(held), name="planner-warm-close")
            self._closing[held] = task
        return task

    async def _closed(self, held: Held) -> None:
        try:
            await held.close()
        except Exception as exc:  # noqa: BLE001 -- the type only; the hold lapses on the broker's TTL
            log.warning("voice session %s: the warm planner was not fully released (%s)",
                        held.lease.voice_session_id, type(exc).__name__)
        finally:
            self._held.discard(held)
            self._closing.pop(held, None)

    async def _close(self, held: Held) -> None:
        # Shielded: a turn cancelled twice still gives both back.
        await asyncio.shield(self._close_soon(held))

    async def drain(self) -> None:
        """Wait for every release under way."""
        while self._closing:
            await asyncio.gather(*list(self._closing.values()), return_exceptions=True)

    async def aclose(self) -> None:
        """The shutdown: let every session's go, and wait for it."""
        for voice_session_id, slot in list(self._slots.items()):
            self._release(voice_session_id, slot, "shutdown")
        for held in list(self._held):
            self._close_soon(held)
        await self.drain()
