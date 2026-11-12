"""Voice it's update queue: the voice speaks up while work runs (the owner's
picks of 12 November 2026, after three talks on the development deployment in
which the session looked frozen while the planner worked).

ONE QUEUE PER VOICE SESSION, bound by live.Sessions at a GPT-Live session's
start (when a planner is configured) and dropped when the voice session ends.
It is fed by:

* the planner's result (live.LiveSession._plan_turn), as before: spoken at once
  when the voice is at a gap, held for the wake when paused;
* the roster follower's changes that matter (follow.py): an agent, a workflow
  or a run started, a run opened its pull request, something finished, failed,
  was cancelled, or is waiting on the owner;
* a card drawn by the planner (planner.py), by its kind and title;
* a CHECK-IN about every CHECKIN_SECONDS while a delegation runs and nothing
  else was said, phrased from the tool the planner is calling now
  (`TOOL_PHRASES`), never from its text or its arguments.

WHAT IT SAYS. One short line per update, from a fixed phrase, an item's id
(follow.ID_RE) and, for a card, its kind and title: no prompt, no tool
argument and no model text beyond a card's title. The planner's result is the
one exception, as today: it is the planner's spoken summary. Updates that
pile up inside MERGE_SECONDS speak as ONE line (at most LINE_PHRASES phrases,
the rest counted), and an update is never spoken twice (each has a key, kept
for the session's life).

WHEN IT SPEAKS: ONLY AT A GAP (`LiveSession.at_gap`): never while the person
speaks (an utterance of theirs still open, or their speech started and not
stopped), never while the voice's audio plays or its current sentence is
unfinished (at most VOICE_SENTENCE_SECONDS), and never during the pause's own
sentence. A held line is looked at again on every sideband event and every
TICK_SECONDS.

PAUSED, an update other than a check-in is held as the result is
(`held_results`) and wakes the voice with the result's cause, so the reopen
says it as its first words (swarm-api's reopen causes are fixed: `result`,
`speech`, `resume`). A check-in never wakes a paused voice and is dropped
while paused: with the pause armed, the pause and the check-in fall due on the
same 45 seconds of silence, and the pause wins (see questions.json of this
lane: the owner may pick otherwise).

NOTHING WAITS ON THE VOICE. Every feed is a synchronous note that returns at
once; the follower, the planner and the delegated work run whether the voice
is open, paused or ended, and a queue's failure is logged, never raised into
them. Nothing here admits, reserves or runs work, and nothing here reads
anything: the follower's and the planner's reads are the session's tenant's,
through its grant, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

log = logging.getLogger("swarm_voice.updates")

#: A check-in while a delegation runs and nothing was said for this long (the
#: owner's "about every 45 seconds"); the same as the pause's silence.
CHECKIN_SECONDS = 45.0
#: Updates noted within this long of the first one waiting speak as one line
#: (the owner's "three updates in one second speak as one line").
MERGE_SECONDS = 1.0
#: How often a queue looks for a gap or a check-in when no sideband event comes.
TICK_SECONDS = 0.25
#: The most phrases one line names; the rest are counted ("and 2 more updates").
LINE_PHRASES = 3
#: The longest card title spoken, and the longest card kind.
TITLE_MAX = 80
KIND_MAX = 32

#: Kinds of update, in the order a merged line names them.
RESULT, OWNER, MILESTONE, CHECKIN = "result", "owner", "milestone", "checkin"
_ORDER = {RESULT: 0, OWNER: 1, MILESTONE: 2, CHECKIN: 3}

#: An item's id as a path names one (follow.ID_RE; repeated here so this module
#: imports nothing that reads).
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,%d}$" % (KIND_MAX - 1))
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

#: What a roster item is called aloud, by the follower's row kind.
ITEM_WORDS = {"agent": "Agent", "workflow": "Workflow", "run": "Run"}
#: The roster's states (follow.STATES' values) that are spoken, and how.
STARTED = "{item} {id} started."
STATE_PHRASES = {
    "waiting": (OWNER, "{item} {id} is waiting on you."),
    "failed": (OWNER, "{item} {id} failed."),
    "done": (MILESTONE, "{item} {id} finished."),
    "cancelled": (MILESTONE, "{item} {id} was cancelled."),
}
#: A run's own phases (swarm_api.issueruns) that say more than its roster state.
RUN_PHASES = {
    "CHECKING": (MILESTONE, "Run {id} opened its pull request."),
    "FIXING": (MILESTONE, "Run {id} is fixing its pull request's checks."),
    "DONE": (MILESTONE, "Run {id} is done: its pull request is through."),
    "PLANNED": (OWNER, "Run {id} has a plan waiting for your approval."),
}
#: The planner's tools, as a check-in says what it is doing now. A tool not
#: named here is "working on it"; a `draw_<kind>` tool names its kind.
TOOL_PHRASES = {
    "list_agents": "reading the running agents",
    "get_agent": "reading an agent",
    "agent_events": "reading an agent's events",
    "agent_questions": "reading an agent's questions",
    "open_questions": "reading the open questions",
    "list_workflows": "reading the workflows",
    "get_workflow": "reading a workflow",
    "list_runs": "reading the runs",
    "get_run": "reading a run",
    "list_findings": "reading the findings",
    "get_finding": "reading a finding",
    "list_approvals": "reading the approvals",
    "list_repositories": "reading the repositories",
    "list_schedules": "reading the schedules",
    "search": "searching SwarmCloud",
    "show_file": "putting a file on screen",
    "show_diff": "putting a diff on screen",
    "show_log": "putting a log on screen",
    "show_mockup": "drawing a mock-up",
    "approve_mockup": "recording a mock-up's approval",
    "approved_mockups": "reading the approved mock-ups",
    "add_note": "adding a note",
    "notes_to_work": "turning the notes into work",
    "answer_question": "answering an agent's question",
    "speak": "putting the answer together",
}
DRAW_PREFIX = "draw_"
#: The check-in's line, with what the planner is doing now.
CHECKIN_LINE = "Still working: {doing}."
CHECKIN_IDLE = "Still working on it."
DRAWN_LINE = "I drew a {kind} card: {title}."
MORE_LINE = "And {n} more {updates}."


def tool_phrase(name: str) -> str | None:
    """What a check-in says the planner is doing while it calls `name`: a fixed
    phrase, or None for a tool this table does not know."""
    if name in TOOL_PHRASES:
        return TOOL_PHRASES[name]
    if name.startswith(DRAW_PREFIX):
        kind = name[len(DRAW_PREFIX):]
        if _KIND_RE.match(kind):
            return f"drawing a {kind.replace('_', ' ')} card"
    return None


def _title(value: Any) -> str:
    text = _CONTROL.sub(" ", value if isinstance(value, str) else "")
    text = " ".join(text.split())
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1].rstrip() + "…"


@dataclass(frozen=True)
class Update:
    """One thing worth saying: its kind, its key (never said twice) and its
    line; a result names its delegation, a roster update its item."""

    kind: str
    key: str
    line: str
    delegation_id: str | None = None
    item: str | None = None


@dataclass(frozen=True)
class Change:
    """One roster row the follower wrote: its kind, id and roster state, the
    state it had before (None the first time), and a run's own phase."""

    kind: str
    id: str
    state: str
    before: str | None = None
    phase: str | None = None


def roster_updates(changes: Iterable[Change]) -> list[Update]:
    """The updates the follower's changes make: starts, a run's pull request,
    what finished, failed or waits on the owner. Ids that do not look like an
    id are never spoken."""
    found: list[Update] = []
    for change in changes:
        item = ITEM_WORDS.get(change.kind)
        if item is None or not ID_RE.match(change.id or ""):
            continue
        base = f"{change.kind}:{change.id}"
        if change.before is None and change.state in ("queued", "running"):
            found.append(Update(MILESTONE, f"{base}:started", STARTED.format(item=item, id=change.id),
                                item=base))
        phase = (change.phase or "").upper()
        if change.kind == "run" and phase in RUN_PHASES:
            kind, line = RUN_PHASES[phase]
            found.append(Update(kind, f"{base}:{phase.lower()}", line.format(id=change.id), item=base))
            continue
        if change.state in STATE_PHRASES and change.state != change.before:
            kind, line = STATE_PHRASES[change.state]
            found.append(Update(kind, f"{base}:{change.state}", line.format(item=item, id=change.id),
                                item=base))
    return found


def merge(updates: list[Update]) -> str:
    """One line for `updates`: the result first, then what needs the owner,
    then milestones and a check-in, at most LINE_PHRASES phrases. Of one
    item's updates only the newest is named: it says where the item is now."""
    newest = {u.item: at for at, u in enumerate(updates) if u.item is not None}
    updates = [u for at, u in enumerate(updates) if u.item is None or newest[u.item] == at]
    ranked = sorted(updates, key=lambda u: _ORDER.get(u.kind, 9))
    if any(u.kind != CHECKIN for u in ranked):
        # A check-in says nothing another update does not say better.
        ranked = [u for u in ranked if u.kind != CHECKIN]
    named = [u.line for u in ranked[:LINE_PHRASES]]
    rest = len(ranked) - len(named)
    if rest:
        named.append(MORE_LINE.format(n=rest, updates="update" if rest == 1 else "updates"))
    return " ".join(named)


class Speaker(Protocol):
    """What the queue needs of the voice session's Live session (live.LiveSession)."""

    @property
    def paused(self) -> bool: ...

    @property
    def ended(self) -> bool: ...

    ending: bool
    pause_after: float | None
    spoken_at: float
    delegations: dict[str, None]

    def at_gap(self) -> bool: ...

    async def say_update(self, line: str, *, wakes: bool, delegation_id: str | None = None) -> bool: ...


class UpdateQueue:
    """One voice session's updates."""

    def __init__(self, voice_id: str, speaker: Callable[[], Speaker | None], *,
                 clock: Callable[[], float] | None = None, tick: float | None = TICK_SECONDS) -> None:
        self.voice_id = voice_id
        self._speaker = speaker
        self._clock = clock or time.monotonic
        self.pending: list[Update] = []
        #: Every key spoken (or held for the wake): never spoken again.
        self.said: set[str] = set()
        #: Every line handed to the voice, in order (what the tests read).
        self.lines: list[str] = []
        #: When the oldest pending update was noted.
        self._first_at: float | None = None
        #: When the last line was spoken, or the delegation began: a check-in
        #: counts its silence from the latest of this and the voice's own speech.
        self._quiet_from: float | None = None
        #: What the planner is doing now (`tool_phrase`), while a delegation runs.
        self.doing: str | None = None
        self._checkins = 0
        self._lock = asyncio.Lock()
        self._tick = tick
        self._task: asyncio.Task | None = None
        self._pumps: set[asyncio.Task] = set()
        self.closed = False

    # -- the feeds: synchronous, never raise, never wait --------------------------

    def note(self, update: Update) -> None:
        if self.closed or update.key in self.said or any(u.key == update.key for u in self.pending):
            return
        if not self.pending:
            self._first_at = self._clock()
        self.pending.append(update)
        self.poke()

    def note_all(self, updates: Iterable[Update]) -> None:
        for update in updates:
            self.note(update)

    def drawn(self, kind: Any, title: Any, card_id: Any) -> None:
        """The planner drew a card: its kind and title, once per card id."""
        kind_text = str(kind or "")
        if not _KIND_RE.match(kind_text):
            return
        shown = _title(title)
        if not shown:
            return
        key = f"card:{card_id}" if isinstance(card_id, str) and ID_RE.match(card_id) else f"card:{kind_text}:{shown}"
        self.note(Update(MILESTONE, key, DRAWN_LINE.format(kind=kind_text.replace("_", " "), title=shown)))

    def calling(self, tool: str) -> None:
        """The planner is calling `tool`: what the next check-in says it is doing."""
        phrase = tool_phrase(tool)
        if phrase is not None:
            self.doing = phrase

    def roster(self, changes: Iterable[Change]) -> None:
        self.note_all(roster_updates(changes))

    # -- speaking -----------------------------------------------------------------

    def poke(self) -> None:
        """Look for a gap now, on the running loop, without waiting for it."""
        if self.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.pump(), name=f"updates-{self.voice_id}")
        self._pumps.add(task)
        task.add_done_callback(self._pumps.discard)

    def start(self) -> None:
        """Look every TICK_SECONDS (for a gap, a merge window's end, a check-in)."""
        if self._tick is None or self._task is not None or self.closed:
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name=f"updates-tick-{self.voice_id}")

    async def _run(self) -> None:
        assert self._tick is not None
        while not self.closed:
            await asyncio.sleep(self._tick)
            await self.pump()

    def close(self) -> None:
        self.closed = True
        self.pending = []
        for task in [self._task, *self._pumps]:
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self._task = None

    async def pump(self) -> list[str]:
        """Speak what is due, if it can: the lines spoken. Never raises."""
        try:
            async with self._lock:
                return await self._pump()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the queue never fails the session
            log.warning("voice session %s: an update was not spoken: %s", self.voice_id, type(exc).__name__)
            return []

    async def _pump(self) -> list[str]:
        if self.closed:
            return []
        speaker = self._speaker()
        if speaker is None or (speaker.ended and not speaker.paused) or speaker.ending:
            return []
        now = self._clock()
        self._checkin(speaker, now)
        if not self.pending:
            return []
        if speaker.paused:
            waking = [u for u in self.pending if u.kind != CHECKIN]
            self.pending = []
            self._first_at = None
            if not waking:
                return []
            return await self._speak(speaker, waking, now, wakes=True)
        due = any(u.kind == RESULT for u in self.pending) or (
            self._first_at is not None and now - self._first_at >= MERGE_SECONDS)
        if not due or not speaker.at_gap():
            return []
        updates, self.pending, self._first_at = self.pending, [], None
        return await self._speak(speaker, updates, now, wakes=False)

    async def _speak(self, speaker: Speaker, updates: list[Update], now: float, *, wakes: bool) -> list[str]:
        line = merge(updates)
        for update in updates:
            self.said.add(update.key)
        if not line:
            return []
        delegation_id = next((u.delegation_id for u in updates if u.kind == RESULT), None)
        if not await speaker.say_update(line, wakes=wakes, delegation_id=delegation_id):
            return []
        self.lines.append(line)
        self._quiet_from = now
        return [line]

    def _checkin(self, speaker: Speaker, now: float) -> None:
        """While a delegation runs: a check-in once nothing was said for
        CHECKIN_SECONDS, unless the pause, armed on the same silence, comes
        first, or the voice is paused. Its end stops them."""
        if not speaker.delegations:
            self._quiet_from = None
            self.doing = None
            self.pending = [u for u in self.pending if u.kind != CHECKIN]
            return
        if self._quiet_from is None:
            self._quiet_from = now
        if speaker.paused or self.pending:
            return
        if speaker.pause_after is not None and speaker.pause_after <= CHECKIN_SECONDS:
            # The pause falls due on the same silence and wins (questions.json).
            return
        quiet_from = max(self._quiet_from, speaker.spoken_at)
        if now - quiet_from < CHECKIN_SECONDS:
            return
        self._checkins += 1
        line = CHECKIN_LINE.format(doing=self.doing) if self.doing else CHECKIN_IDLE
        self.pending.append(Update(CHECKIN, f"checkin:{self._checkins}", line))
        # A check-in is due as soon as there is a gap: nothing merges into it.
        self._first_at = now - MERGE_SECONDS


class Board:
    """Every voice session's queue, by voice session id: the one place the
    follower and the planner reach a session's queue without holding it."""

    def __init__(self) -> None:
        self._queues: dict[str, UpdateQueue] = {}

    def bind(self, voice_id: str, speaker: Callable[[], Speaker | None], *,
             clock: Callable[[], float] | None = None, tick: float | None = TICK_SECONDS) -> UpdateQueue:
        queue = self._queues.get(voice_id)
        if queue is None or queue.closed:
            queue = UpdateQueue(voice_id, speaker, clock=clock, tick=tick)
            self._queues[voice_id] = queue
            with contextlib.suppress(RuntimeError):
                queue.start()
        return queue

    def get(self, voice_id: str) -> UpdateQueue | None:
        return self._queues.get(voice_id)

    def drop(self, voice_id: str) -> None:
        queue = self._queues.pop(voice_id, None)
        if queue is not None:
            queue.close()

    # The feeds. A session with no queue (a text or own-speech session, or one
    # already ended) hears nothing; a feed never raises into its caller.

    def drawn(self, voice_id: str, kind: Any, title: Any, card_id: Any) -> None:
        queue = self._queues.get(voice_id)
        if queue is not None:
            _safely(queue.drawn, kind, title, card_id)

    def calling(self, voice_id: str, tool: str) -> None:
        queue = self._queues.get(voice_id)
        if queue is not None:
            _safely(queue.calling, tool)

    def roster(self, voice_id: str, changes: Iterable[Change]) -> None:
        queue = self._queues.get(voice_id)
        if queue is not None:
            _safely(queue.roster, list(changes))


def _safely(call: Callable[..., None], *args: Any) -> None:
    try:
        call(*args)
    except Exception as exc:  # noqa: BLE001 - an update never fails the work that made it
        log.warning("an update was not noted: %s", type(exc).__name__)


#: The process's board: live.Sessions binds to it, the follower and the planner feed it.
BOARD = Board()
