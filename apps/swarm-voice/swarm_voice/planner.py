"""The planner: each delegation becomes cards and at most two spoken sentences
(docs/design/voice-it.md §3.3, §3.4, §3.6, §3.9; section 10, phase two).

ONE TURN, as §3.3 lays it out. When the live voice delegates (live.py sees
`session.delegation.created` on the sideband), `Planner.delegate`:

  1. refuses at once when the session holds no grant (tools_read.NoGrant): the
     planner reads and writes nothing in swarm-api except as the session's
     person;
  2. reads the card catalogue from swarm-api's schema route WITH THE GRANT, and
     offers the model exactly its `draw_<kind>` tools, the read tools
     (tools_read.py), the visual tools (tools_visual.py: `show_file`,
     `show_diff`, `show_log`, `show_mockup`, `approve_mockup`,
     `approved_mockups`, `add_note`, `notes_to_work`) and `speak`, whose
     `point_at` reaches the page as a `focus` (voice-it-own-speech.md §8.5);
  3. runs on the session's warm account and child (plannerwarm.py; "KEPT WARM"
     below), or leases ONE Claude subscription account from the quota broker for
     the person's own workspace (plannerlease.py), released in a finally whatever
     the turn does; a full pool is a spoken refusal, never a wait;
  4. runs Claude Opus 5.5 as headless Claude Code on that account
     (claudeturn.py), its only tools those of step 2 through a local MCP
     server, executing each read through swarm-api with the grant and writing
     each card through swarm-api's card write path, which validates it against
     the catalogue and appends it to the session's stream;
  5. hands the live voice at most two sentences to say (prompts.spoken),
     followed by one that says what the turn put on screen (`drew`).

WHAT IT DREW IS SAID, NONE INCLUDED (owner, 10 November 2026: the voice said
"there is a card on screen" when the planner drew none). Every spoken result
ends by saying how many cards and mock-ups the turn put on screen, counted from
what swarm-api accepted rather than from what the model said, or that nothing
was drawn (NOTHING_DRAWN) and why: the voice's instructions (live.py) let it
point at a card only when the result says one was drawn.

WHAT IT DRAWS APPEARS AS IT IS DRAWN (owner, 12 November 2026: the session
looked frozen while the planner worked). A card is written the moment its
`draw_<kind>` call comes in, not at the end of the turn, and a mock-up's
frames are written one at a time (`draw_mockup`): frame one alone, then one
and two, and so on, each a new version of the same visual, so the stage draws
frame one before frame three is written; a card whose body carries frames
(`write_card`) is written the same way, each frame the next version of one
card. A visual's frame that cannot be drawn writes no version of its own; what was repaired and left out is reported for the
whole mock-up, as one call would.

AN UNKNOWN KIND IS REFUSED. A tool the model calls that is not a read tool,
not `speak` and not one of the catalogue's `draw_<kind>` tools as served this
turn is answered with an error and never reaches swarm-api; the refusal is
recorded on the turn's result. swarm-api refuses an unknown kind again on write
(cards.validate), so the console never sees one.

THE ACCOUNT (§3.6; the owner, 2 November 2026: "we have a pool of account for
anthropic, lets use one of the accounts there"). The turn runs on a pool
lease, as a worker's agent does, and counts one agent on that account while it
runs and while it is kept warm. swarm-voice reads no Anthropic secret and holds no accessor grant: the
broker hands the lease's token with the lease, and it goes to the Claude Code
child's environment alone (CLAUDE_CODE_OAUTH_TOKEN), never to the Messages API
from here. When the lease lands on an Anthropic developer key
(docs/design/llm-providers.md §5.3) the key goes to the child as
ANTHROPIC_API_KEY instead, never both. A turn that ends on a spent account
before it spoke, drew or acted (a plan at its limit, a key's credit gone,
`TurnResult.spent`) moves to the next tier and runs again inside the same
TURN_SECONDS; with no tier left the voice says the pool is full.

WHAT IT COSTS. A subscription turn has no measured price: `usage` carries the
token counts, the model and `billing: "subscription"`, and the ledger
(swarm_api.voice.ledger.turn_cost) counts the turn against the session's $100
and the day's $300 by its estimate at Opus prices, shown as not measured --
never as $0. A turn on a key is billed per token (`billing: "api_key"`), so the
ledger prices its usage. `usage.tier` names the tier either way.

KEPT WARM (plannerwarm.py). A session's delegated turns share one lease and one
child while it is active: the first starts them, and the session's end
(`forget`), a pause or a spend cap (`cool`), WARM_IDLE_SECONDS without a turn
and the shutdown (`aclose`) release both. The turn itself is the same: its own
message with the transcript, the request and the brief fenced, its own tools
and result, the same TURN_SECONDS and the same `usage`. A warm child that is
dead, silent, spent or on a lost hold leaves the turn to a lease and a child of
its own (`_cold`), within the same TURN_SECONDS; a turn that has put something
on screen, done an act or spoken is never run again.

THE PROJECT BRIEF (the plan item "voice, better answers"). Before the model
runs, the turn reads the project's status (tools_read `project_status`, the
grant's own project) and puts the brief written after the last turn into the
turn message, fenced as data (prompts.turn_message), so a later delegation or
session does not start cold; a read that fails leaves it out. `speak` carries
the turn's new `brief`; after the turn ends, and only when it spoke, the last
one is written ONCE (`POST .../sessions/{id}/brief`, BRIEF_ROUTE), scrubbed and
cut to prompts.BRIEF_MAX bytes. It is the turn's own output, so it costs no
model call and its tokens are in the turn's `usage`. A session at a spend cap
writes none: a capped delegation runs no turn, and swarm-api refuses the write
(409) when the caps say the session is capped.

NOTHING SECRET IN A PROMPT, A LOG, A TRANSCRIPT OR A CARD. The token and the
grant are scrubbed from the transcript, from every tool result, from the
child's captured streams and from every card before it is written
(prompts.scrub); errors carry an HTTP status, never a body; log lines name the
session, kinds, counts and statuses only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import actledger, plannerwarm, prompts, tools_act, tools_read, tools_visual
from . import updates as vupdates
from .accountlease import TIER_PLAN, VoiceBroker, build_broker
from .claudeturn import ChildGone, ChildOutcome, ClaudeCode
from .credential import CredentialError
from .grant import SessionGrant
from .plannerlease import NO_ACCOUNTS_REGISTERED, PlannerAccounts, PoolFull
from .worksink import CAP_SENTENCES, WORK_ROUTE

#: The caps' refusal a session already at a cap gives (worksink.CAP_REASONS).
CAPPED_REASON = "voice_budget_capped"

log = logging.getLogger("swarm_voice.planner")

#: Model calls in one turn: reads, draws and the closing `speak`.
MAX_ROUNDS = 8
#: Cards one turn may write: a delegation usually needs one (§3.3).
MAX_CARDS = 4
#: The whole turn, after which the voice is told the planner ran out of time.
#: Below the broker's ten-minute voice hold, so a lease outlives no turn.
TURN_SECONDS = 240.0
#: Where the turn's brief is written (swarm-api routes/voice_status.py).
BRIEF_ROUTE = "/v1/voice/sessions/{session_id}/brief"
#: The longest the brief's write may hold the delegation's answer.
BRIEF_WRITE_SECONDS = 5.0

#: How a subscription turn's cost is recorded (module docstring, "WHAT IT COSTS").
BILLING = "subscription"
#: How a turn on an Anthropic developer key is recorded: billed per token, so
#: swarm-api's ledger prices its usage like any measured turn (§5.3).
BILLING_KEY = "api_key"

DRAW_PREFIX = "draw_"

#: What the voice says when a turn could not run, by cause. Short, plain, and
#: never a status code or an id.
SAY_NO_GRANT = "I can't reach SwarmCloud for this conversation, so I couldn't draw anything."
SAY_POOL_FULL = ("Every Claude account in your workspace's pool is busy right now, "
                 "so I can't draw cards for this one.")
SAY_NO_POOL = "Your workspace has no Claude account in its pool, so I can't draw cards."
SAY_NO_LEASE = "I couldn't get a Claude account for the planner, so nothing new is on screen."
SAY_FAILED = "The planner couldn't finish that one, so nothing new is on screen."
SAY_REFUSED = "I can't plan that one."
#: Said after a turn's own words when it put nothing on screen: the voice reads
#: it as "no card" (live.DELEGATE_INSTRUCTIONS).
NOTHING_DRAWN = "I drew nothing, so nothing new is on screen."
#: What `drew` calls each kind of visual it counts; other visual tools (an
#: approval, a note) put nothing new on screen.
DRAWN_VISUALS = {tools_visual.SHOW_MOCKUP: ("mock-up", "mock-ups"),
                 tools_visual.SHOW_FILE: ("file view", "file views"),
                 tools_visual.SHOW_DIFF: ("diff view", "diff views"),
                 tools_visual.SHOW_LOG: ("log view", "log views")}


#: `TurnResult.error` for a turn that ran out of time (trace.py's `timeout`).
TIMED_OUT = "the turn ran out of time"
#: What debug mode's trace calls each drawn visual (trace.py; DRAWN_VISUALS' tools).
VISUAL_KINDS = {tools_visual.SHOW_MOCKUP: "mockup", tools_visual.SHOW_FILE: "file",
                tools_visual.SHOW_DIFF: "diff", tools_visual.SHOW_LOG: "log"}
#: A read's target: its arguments that name a thing, never what it returned.
READ_TARGET_KEYS = ("task_id", "workflow_id", "run_id", "schedule_id", "repo_id", "item_id",
                    "finding_id", "step", "status", "query")


def read_target(args: dict[str, Any]) -> str | None:
    """The read's target for the trace, e.g. "task_id=t_1"; None for a list."""
    named = [f"{k}={str(args[k])[:80]}" for k in READ_TARGET_KEYS
             if isinstance(args.get(k), (str, int)) and not isinstance(args.get(k), bool)]
    return ", ".join(named) or None


@dataclass
class TurnResult:
    """What one delegation produced. Holds no secret."""

    delegation_id: str | None
    spoken: str
    cards: list[dict[str, Any]] = field(default_factory=list)
    #: Tool calls refused: unknown kinds and tools, and cards swarm-api refused.
    refused: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    #: The acts this turn read back or did (tools_act.py), as the act ledger has them.
    acts: list[dict[str, Any]] = field(default_factory=list)
    #: The visuals this turn put on screen or annotated (tools_visual.py), by tool.
    visuals: list[dict[str, Any]] = field(default_factory=list)
    #: `speak`'s `point_at` (voice-it-own-speech.md §8.5) as the page got it, or None.
    point_at: dict[str, Any] | None = None
    #: Token counts, the model and `billing` (module docstring, "WHAT IT COSTS").
    usage: dict[str, Any] = field(default_factory=dict)
    #: The child's stream-json events, scrubbed (claudeturn.ChildOutcome.events).
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: Why the turn could not run or finish, for the log; None when it did.
    error: str | None = None
    #: Debug mode's trace (trace.py): each card or mock-up drawn, {kind, title,
    #: frames?}, or refused, {kind, title, refused: the reason}.
    drawn: list[dict[str, Any]] = field(default_factory=list)
    #: Debug mode's trace: each read, {tool, target}; never what it returned.
    read_targets: list[dict[str, Any]] = field(default_factory=list)
    #: Each mock-up some of whose frames were not drawn (tools_visual.frames_of):
    #: {title, asked, drawn, dropped: [{n, label, why}]}; said by `frames_not_drawn`.
    frames_dropped: list[dict[str, Any]] = field(default_factory=list)
    #: The tier the turn ran on (accountlease.TIERS; also `usage["tier"]`).
    tier: str | None = None
    #: claudeturn.SPENT_CREDIT or SPENT_RATE when the turn ended on a spent
    #: account before it spoke or wrote anything; the turn then moves (§5.3).
    spent: str | None = None
    #: The project brief this turn's `speak` gave, scrubbed and cut (prompts.brief_text).
    brief: str | None = None
    #: Whether swarm-api stored it (one write per turn; none at a cap).
    brief_written: bool = False
    #: Whether the brief written after the last turn was in this turn's message.
    brief_read: bool = False


def drew(result: "TurnResult") -> str:
    """One sentence of what `result` put on screen: its cards and its mock-ups
    (and file, diff or log views), each counted, or NOTHING_DRAWN."""
    counts: list[tuple[int, tuple[str, str]]] = []
    if result.cards:
        counts.append((len(result.cards), ("card", "cards")))
    for tool, names in DRAWN_VISUALS.items():
        shown = sum(1 for v in result.visuals if v.get("tool") == tool and v.get("visual_id"))
        if shown:
            counts.append((shown, names))
    if not counts:
        return NOTHING_DRAWN
    parts = [f"{n} {names[0] if n == 1 else names[1]}" for n, names in counts]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"I drew {listed} on screen."


_COUNTS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
           "eleven", "twelve")
_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
             "ninth", "tenth", "eleventh", "twelfth")


def _word(words: tuple[str, ...], n: int) -> str:
    return words[n - 1] if 1 <= n <= len(words) else str(n)


def frames_not_drawn(result: "TurnResult") -> str:
    """What the voice says about each mock-up frame that could not be drawn
    ("I drew two of the three versions; the third, Minimal, was refused
    because ..."), or "" when every frame asked for was drawn."""
    said: list[str] = []
    for mockup in result.frames_dropped:
        asked, shown = mockup["asked"], mockup["drawn"]
        lead = (f"I drew {_word(_COUNTS, shown)} of the {_word(_COUNTS, asked)} versions"
                if shown else "I couldn't draw the mock-up")
        why = "; ".join(f"the {_word(_ORDINALS, d['n'])}, {d['label']}, was refused because {d['why']}"
                        for d in mockup["dropped"])
        said.append(f"{lead}; {why}.")
    return " ".join(said)


def _same_mockup(mockup: dict[str, Any], args: dict[str, Any]) -> bool:
    """Whether a `show_mockup` call with `args` redraws the mock-up a
    `frames_dropped` entry records: the same visual_id, or the same title."""
    visual_id = args.get("visual_id")
    if visual_id and visual_id == mockup.get("visual_id"):
        return True
    title = _title(args)
    return title is not None and mockup.get("title") == title


def _redrawn(result: "TurnResult", args: dict[str, Any]) -> bool:
    """Whether an earlier call this turn already recorded what this mock-up
    left out; a refused redraw then changes nothing on screen, and the
    earlier entry still says which version is missing."""
    return any(_same_mockup(m, args) for m in result.frames_dropped)


def _failed(result: "TurnResult") -> str:
    """SAY_FAILED, unless the turn had put something on screen before it failed."""
    said = SAY_FAILED if drew(result) == NOTHING_DRAWN else f"The planner couldn't finish that one. {drew(result)}"
    return f"{said} {frames_not_drawn(result)}".strip()


def _scrub_all(value: Any, known: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return prompts.scrub(value, known)
    if isinstance(value, list):
        return [_scrub_all(v, known) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_all(v, known) for k, v in value.items()}
    return value


def _title(args: dict[str, Any]) -> str | None:
    title = args.get("title")
    return title[:200] if isinstance(title, str) else None


def _visual_drawn(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """A drawn visual for the trace: its kind, title and, for a mock-up, its
    frames' labels (never their HTML)."""
    drawn: dict[str, Any] = {"kind": VISUAL_KINDS[name], "title": _title(args)}
    if name == tools_visual.SHOW_MOCKUP and isinstance(args.get("frames"), list):
        drawn["frames"] = [str(f.get("label"))[:200] for f in args["frames"]
                           if isinstance(f, dict) and isinstance(f.get("label"), str)]
    return drawn


def _card_from(kind: str, args: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    """The card a `draw_<kind>` call asks for, as swarm-api's write path takes it.
    The project, session, version and time are swarm-api's to set."""
    card = {"card_id": args.get("card_id"), "kind": kind, "state": args.get("state"),
            "title": args.get("title"), "created_by": "planner",
            "references": args.get("references"), "summary": args.get("summary"),
            "body": args.get("body")}
    return _scrub_all(card, known)


#: The most frames a card's body carries (swarm_api/voice/cards.py `_FRAMES`).
CARD_FRAMES_MAX = 12


def _card_view(written: Any) -> dict[str, Any]:
    written = written if isinstance(written, dict) else {}
    return written["card"] if isinstance(written.get("card"), dict) else written


async def write_card(api: tools_read.SwarmApi, kind: str, args: dict[str, Any],
                     known: tuple[str, ...]) -> tuple[dict[str, Any], str | None]:
    """Write a `draw_<kind>` card now, while the turn goes on: (the card as
    last written, why a later frame was not written or None). A card whose
    body carries frames (a mock-up, a wireframe) is written frame by frame,
    each frame the next version of the same card, so the console draws frame
    one before frame three is written. Raises ApiRefused when the first write
    is refused; a later frame refused leaves the card at the frames before it."""
    body = args.get("body") if isinstance(args.get("body"), dict) else {}
    frames = body.get("frames")
    if not isinstance(frames, list) or not 1 < len(frames) <= CARD_FRAMES_MAX:
        return _card_view(await api.write_card(_card_from(kind, args, known))), None
    def upto(n: int, card_id: Any) -> dict[str, Any]:
        return _card_from(kind, {**args, "card_id": card_id, "body": {**body, "frames": frames[:n]}}, known)

    card = _card_view(await api.write_card(upto(1, args.get("card_id"))))
    for n in range(2, len(frames) + 1):
        try:
            card = _card_view(await api.write_card(upto(n, card.get("card_id"))))
        except tools_read.ApiRefused as exc:
            return card, f"frame {n} of {len(frames)} and after were not written: {exc}"
    return card, None


def _drawable(frame: Any) -> bool:
    """Whether one frame survives swarm-api's frame rules (after repair)."""
    try:
        tools_visual.frames_of([frame])
    except tools_read.BadInput:
        return False
    return True


async def draw_mockup(api: tools_read.SwarmApi, args: dict[str, Any]) -> Any:
    """`show_mockup`, written frame by frame: each drawable frame writes the
    frames so far as the next version of one visual, so the page shows frame
    one before frame three is written. The answer is the last write's, with
    what was repaired and left out counted over every frame asked for."""
    frames = args.get("frames")
    if not isinstance(frames, list) or not 1 < len(frames) <= tools_visual.FRAMES_MAX:
        return await tools_visual.run_visual_tool(api, tools_visual.SHOW_MOCKUP, args)
    # Raises FramesRefused when no frame is left, before anything is written.
    _, repaired, dropped = tools_visual.frames_of(frames)
    visual_id = args.get("visual_id")
    answer: dict[str, Any] = {}
    for n, frame in enumerate(frames, start=1):
        if not _drawable(frame):
            continue
        answer = await tools_visual.run_visual_tool(api, tools_visual.SHOW_MOCKUP, {
            **args, "frames": frames[:n], "visual_id": visual_id})
        visual_id = answer.get("visual_id") or visual_id
    answer = {k: v for k, v in answer.items() if k not in ("repaired", "dropped")}
    answer["asked"] = len(frames)
    if repaired:
        answer["repaired"] = repaired
    if dropped:
        answer["dropped"] = dropped
    return answer


class Planner:
    """Claude Opus, as Claude Code on a pool lease, over the catalogue's draw
    tools and the read tools."""

    def __init__(self, http: httpx.AsyncClient, *, api_base: str, id_tokens: tools_read.IdTokens,
                 accounts: PlannerAccounts, claude: ClaudeCode | None = None,
                 max_rounds: int = MAX_ROUNDS, turn_seconds: float = TURN_SECONDS,
                 updates: vupdates.Board | None = vupdates.BOARD,
                 warm: plannerwarm.WarmPlanners | None = None) -> None:
        self._http = http
        #: Each session's warm account and child; None runs every turn cold.
        self._warm = warm
        #: THE UPDATES (updates.py): told the tool each call makes (a check-in's
        #: phrase) and each card drawn (its kind and title); never its arguments.
        self._updates = updates
        self._api_base = api_base
        self._id_tokens = id_tokens
        self._accounts = accounts
        self._claude = claude or ClaudeCode(max_turns=max_rounds)
        self._turn_seconds = turn_seconds
        #: One turn at a time per voice session (§3.8: turns run one at a time).
        self._locks: dict[str, asyncio.Lock] = {}
        #: Each session's act ledger (actledger.py), for the session's life.
        self._acts: dict[str, actledger.ActLedger] = {}

    def forget(self, session_id: str) -> None:
        """The session ended: nothing of it is kept."""
        self._locks.pop(session_id, None)
        self._acts.pop(session_id, None)
        self.cool(session_id, "the session ended")

    def cool(self, session_id: str, why: str) -> None:
        """Let the session's warm account and child go (paused, at a spend cap):
        now, or after the turn it is running; its next turn starts afresh."""
        if self._warm is not None:
            self._warm.cool(session_id, why)

    async def aclose(self) -> None:
        """The shutdown: every warm account and child is given back."""
        if self._warm is not None:
            await self._warm.aclose()

    async def write_trace(self, grant: SessionGrant | None, body: dict[str, Any]) -> bool:
        """Send one debug turn's trace to swarm-api (trace.py); never raises."""
        from . import trace

        return await trace.send(self._http, base_url=self._api_base, id_tokens=self._id_tokens,
                                grant=grant, body=body)

    async def write_latency(self, grant: SessionGrant | None, record: dict[str, Any]) -> bool:
        """Send one hosted turn's latency record (latency.py) to swarm-api on
        the grant's trace write, as `{latency: record}`, for every session,
        debug or not; never raises (trace.send logs a failure by type)."""
        from . import trace

        return await trace.send(self._http, base_url=self._api_base, id_tokens=self._id_tokens,
                                grant=grant, body={"latency": record})

    def ledger(self, session_id: str) -> actledger.ActLedger:
        return self._acts.setdefault(session_id, actledger.ActLedger())

    async def delegate(self, *, person: str, grant: SessionGrant | None,
                       transcript: list[tuple[str, str]], delegation_id: str | None = None,
                       model: str | None = None, request: str | None = None,
                       admitted: bool = False) -> TurnResult:
        """Run one turn for a delegation; never raises for a refusal or a failure:
        the result's `spoken` always says what happened. `model` is the
        session's planner model, read at its start; None keeps the pin.
        `request` is the delegation's own text, when the voice gave it any
        (GPT-Live's delegation event, live.py), handed over beside the transcript.
        `admitted`: the caller already asked swarm-api's caps (the fast front's
        `started`, worksink.py); else this asks them first (`_capped`)."""
        try:
            api = tools_read.SwarmApi.for_session(self._http, base_url=self._api_base,
                                                  id_tokens=self._id_tokens, grant=grant)
        except tools_read.NoGrant as exc:
            log.warning("planner refused: %s", exc)
            return TurnResult(delegation_id, SAY_NO_GRANT, error=str(exc))
        if not admitted and await self._capped(api):
            self.cool(api.session_id, "a spend cap")
            return TurnResult(delegation_id, CAP_SENTENCES[CAPPED_REASON])
        lock = self._locks.setdefault(api.session_id, asyncio.Lock())
        async with lock:
            result = TurnResult(delegation_id, "")
            try:
                await asyncio.wait_for(
                    self._turn(api, result, person=person, transcript=transcript, model=model,
                               request=request),
                    self._turn_seconds)
            except asyncio.TimeoutError:
                log.warning("voice session %s: planner turn ran out of time", api.session_id)
                result.spoken, result.error = _failed(result), TIMED_OUT
                return result
            await self._write_brief(api, result)
            return result

    async def _read_brief(self, api: tools_read.SwarmApi) -> str | None:
        """The brief written after the project's last turn, or None (none yet,
        or the read failed: the turn runs without it)."""
        try:
            body = await api.project_status()
        except tools_read.ApiRefused as exc:
            log.warning("voice session %s: the project brief could not be read: %s",
                        api.session_id, exc)
            return None
        brief = body.get("brief")
        text = brief.get("text") if isinstance(brief, dict) else None
        return text if isinstance(text, str) and text.strip() else None

    async def _write_brief(self, api: tools_read.SwarmApi, result: TurnResult) -> None:
        """Store the turn's brief, once, when its `speak` gave one. A refusal
        (a cap's included) or a failure is logged and changes no answer."""
        if not result.brief or result.brief_written:
            return
        path = BRIEF_ROUTE.format(session_id=api.session_id)
        try:
            await asyncio.wait_for(api._call("POST", BRIEF_ROUTE, path,  # noqa: SLF001
                                             json_body={"text": result.brief}),
                                   BRIEF_WRITE_SECONDS)
        except tools_read.ApiRefused as exc:
            log.warning("voice session %s: the project brief was not written: %s",
                        api.session_id, exc)
            if exc.status == 409 and exc.reason in CAP_SENTENCES:
                self.cool(api.session_id, "a spend cap")
            return
        except asyncio.TimeoutError:
            log.warning("voice session %s: the project brief's write ran out of time",
                        api.session_id)
            return
        result.brief_written = True

    async def _capped(self, api: tools_read.SwarmApi) -> bool:
        """Whether the session has reached a spend cap (voice-it.md §3.8):
        swarm-api's caps' state, `POST .../work {budget: true}`. A read that
        fails is not a cap: the turn runs, and swarm-api's sweep still ends a
        session past its grace."""
        try:
            body = await api._call("POST", WORK_ROUTE, WORK_ROUTE.format(session_id=api.session_id),  # noqa: SLF001
                                   json_body={"budget": True})
        except tools_read.ApiRefused as exc:
            log.warning("voice session %s: the caps could not be read: %s", api.session_id, exc)
            return False
        state = body.get("budget") if isinstance(body, dict) else None
        capped = isinstance(state, dict) and state.get("capped") is not None
        if capped:
            log.info("voice session %s: a delegation refused at a cap", api.session_id)
        return capped

    async def read(self, *, grant: SessionGrant | None, name: str, args: Any,
                   known: tuple[str, ...] = ()) -> tuple[str, bool]:
        """Run one read tool for the fast front (claudefront.py): (the
        tool_result text, whether it is an error). The same swarm-api client,
        session grant, fence and scrub as a planner turn's read; no model, no
        lease and no turn lock, since a read changes nothing. Never raises for
        a refusal: its text says what was refused."""
        if not tools_read.is_read_tool(name):
            return f"refused: {name[:64]!r} is not a read tool", True
        try:
            api = tools_read.SwarmApi.for_session(self._http, base_url=self._api_base,
                                                  id_tokens=self._id_tokens, grant=grant)
        except tools_read.NoGrant:
            return "refused: SwarmCloud cannot be read in this conversation", True
        known = (*known, *api.known_secrets())
        try:
            body = await tools_read.run_read_tool(api, name, args)
        except (tools_read.BadInput, tools_read.ApiRefused) as exc:
            return prompts.scrub(str(exc), known), True
        return prompts.fence(name, prompts.scrub(prompts.as_text(body), known)), False

    async def _turn(self, api: tools_read.SwarmApi, result: TurnResult, *, person: str,
                    transcript: list[tuple[str, str]], model: str | None,
                    request: str | None = None) -> TurnResult:
        self.ledger(api.session_id).begin_turn()
        try:
            catalogue = await api.catalogue()
        except tools_read.ApiRefused as exc:
            log.warning("voice session %s: planner could not read the catalogue: %s", api.session_id, exc)
            result.spoken, result.error = SAY_FAILED, str(exc)
            return result
        draw_tools = [t for t in catalogue["tools"]
                      if isinstance(t, dict) and str(t.get("name", "")).startswith(DRAW_PREFIX)]
        kinds = {str(t["name"])[len(DRAW_PREFIX):] for t in draw_tools}
        tools = [*draw_tools, *tools_read.tool_schemas(), *tools_visual.tool_schemas(),
                 *tools_act.tool_schemas(), prompts.speak_tool()]
        brief = await self._read_brief(api)
        result.brief_read = brief is not None
        claude = self._claude.with_model(model) if model else self._claude
        #: The account the warm child left as spent, its tier and its reason.
        left: tuple[str, str | None, str] | None = None
        spare: dict[str, int] = {}
        try:
            if self._warm is not None:
                try:
                    async with self._warm.turn(
                            voice_session_id=api.session_id, person=person, claude=claude, system=prompts.SYSTEM,
                            tools=tools, known=api.known_secrets(),
                            key=plannerwarm.key_of(voice_session_id=api.session_id, person=person,
                                                   project_id=api.project_id, grant=api.known_secrets(),
                                                   model=claude.model, tools=tools)) as held:
                        if held is not None:
                            result.tier = held.lease.tier
                            await self._run(api, result, held.lease.bearer.reveal(), tools, kinds, transcript,
                                            claude=claude, request=request, brief=brief, warm=held)
                            if result.spent is None:
                                return result
                            held.failed = True
                            left = held.lease.account_id, held.lease.tier, result.spent
                except plannerwarm.ColdAgain as exc:
                    log.info("voice session %s: the warm planner failed; the turn runs on its own lease",
                             api.session_id)
                    spare = exc.usage
                # What the attempt noted and did not do: the cold turn's own follow.
                for noted in (result.reads, result.read_targets, result.refused, result.drawn,
                              result.frames_dropped):
                    noted.clear()
            await self._cold(api, result, tools, kinds, transcript, person=person, claude=claude,
                             request=request, brief=brief, left=left)
            for name, value in spare.items():
                # The failed attempt's tokens were used too: the ledger counts them.
                result.usage[name] = result.usage.get(name, 0) + value
            return result
        except PoolFull as exc:
            if left is not None:
                return self._no_tier_left(api, result, left[1], left[2], exc)
            log.warning("voice session %s: no planner account: %s", api.session_id, exc.reason)
            result.spoken = SAY_NO_POOL if exc.reason == NO_ACCOUNTS_REGISTERED else SAY_POOL_FULL
            result.error = f"the pool leased no account ({exc.reason})"
            return result
        except CredentialError as exc:
            log.warning("voice session %s: planner lease failed: %s", api.session_id, exc)
            result.spoken, result.error = SAY_NO_LEASE, str(exc)
            return result

    async def _cold(self, api: tools_read.SwarmApi, result: TurnResult, tools: list[dict], kinds: set[str],
                    transcript: list[tuple[str, str]], *, person: str, claude: ClaudeCode, request: str | None,
                    brief: str | None, left: tuple[str, str | None, str] | None) -> TurnResult:
        """Run the turn on a lease and a child of its own, both gone when it
        ends. Raises PoolFull or CredentialError when the lease is refused."""
        exclude: dict[str, Any] = {"exclude": (left[0],)} if left is not None else {}
        async with self._accounts.lease(person=person, voice_session_id=api.session_id, **exclude) as held:
            while True:
                result.tier, result.spent, result.error = held.tier, None, None
                await self._run(api, result, held.bearer.reveal(), tools, kinds, transcript,
                                claude=claude, request=request, brief=brief)
                if result.spent is None:
                    return result
                # The account is spent and the turn did nothing yet: the
                # next tier runs it again, within the same TURN_SECONDS
                # (§5.3). With no tier left the refusal below stands.
                spent_tier = held.tier
                try:
                    await held.move()
                except PoolFull as exc:
                    return self._no_tier_left(api, result, spent_tier, result.spent, exc)
                log.info("voice session %s: the planner's %s tier is spent (%s); running the turn on the "
                         "%s tier", api.session_id, spent_tier, result.spent, held.tier)

    @staticmethod
    def _no_tier_left(api: tools_read.SwarmApi, result: TurnResult, tier: str | None, spent: str,
                      exc: PoolFull) -> TurnResult:
        log.warning("voice session %s: the planner's %s tier is spent (%s) and no tier is left: %s",
                    api.session_id, tier, spent, exc.reason)
        result.spoken = SAY_POOL_FULL
        result.error = f"the {tier} tier is spent ({spent}) and no tier is left"
        return result

    async def _run(self, api: tools_read.SwarmApi, result: TurnResult, token: str, tools: list[dict],
                   kinds: set[str], transcript: list[tuple[str, str]], *, claude: ClaudeCode,
                   request: str | None = None, brief: str | None = None,
                   warm: plannerwarm.Held | None = None) -> TurnResult:
        """Run the model once for `result`: on `warm`'s child when given, else on
        a child of the turn's own. Raises ColdAgain when `warm`'s child failed
        before the turn did anything."""
        known = (token, *api.known_secrets())
        said: list[str] = []
        done = asyncio.Event()

        async def handler(name: str, args: dict) -> tuple[str, bool]:
            if self._updates is not None:
                self._updates.calling(api.session_id, name)
            text, is_error, spoken_now = await self._tool(api, {"name": name, "input": args}, kinds, known,
                                                          result, transcript=transcript)
            if spoken_now is not None:
                said.append(spoken_now)
                done.set()
            return text, is_error

        prompt = prompts.turn_message(transcript, request=request, secrets_=known, brief=brief)
        try:
            if warm is not None:
                outcome = await self._on_warm(warm, result, said, prompt, handler, done)
            else:
                outcome = await claude.run(prompt=prompt, system=prompts.SYSTEM, token=token,
                                           tools=tools, handler=handler, known=known, done=done)
        except OSError as exc:
            log.warning("voice session %s: the planner could not start Claude Code (%s)", api.session_id,
                        type(exc).__name__)
            result.spoken, result.error = SAY_FAILED, f"Claude Code could not start ({type(exc).__name__})"
            return result
        result.usage = {**outcome.usage, "model": claude.model, "tier": result.tier,
                        "billing": BILLING if result.tier in (None, TIER_PLAN) else BILLING_KEY}
        result.transcript = outcome.events
        if not said and (outcome.is_error or (outcome.exit_code not in (0, None) and not outcome.stopped)):
            log.warning("voice session %s: Claude Code ended without speaking (exit %s)", api.session_id,
                        outcome.exit_code)
            if not result.cards and not result.acts and not result.visuals:
                result.spent = outcome.spent
            result.error = f"Claude Code exited {outcome.exit_code} without speaking"
            result.spoken = _failed(result)
            return result
        text = said[-1] if said else outcome.result_text
        said_text = prompts.spoken(text or "", secrets_=known)
        if not said_text and drew(result) == NOTHING_DRAWN and not result.frames_dropped:
            result.spoken = SAY_FAILED
        else:
            result.spoken = f"{said_text} {drew(result)} {frames_not_drawn(result)}".strip()
        log.info("voice session %s: planner turn drew %d card(s) [%s], refused %d, read %d",
                 api.session_id, len(result.cards), ",".join(c.get("kind", "?") for c in result.cards),
                 len(result.refused), len(result.reads))
        return result

    @staticmethod
    async def _on_warm(warm: plannerwarm.Held, result: TurnResult, said: list[str], prompt: str, handler: Any,
                       done: asyncio.Event) -> ChildOutcome:
        """The turn on the session's warm child, which a turn that failed on it
        never leaves for the next."""
        gone = False
        try:
            outcome = await warm.say(prompt, handler=handler, done=done)
        except ChildGone as exc:
            gone, outcome = True, ChildOutcome(is_error=True, usage=exc.usage)
        if said:
            # It answered: its own words stand, whatever the child did next.
            warm.failed = gone
            return outcome
        if not outcome.is_error:
            return outcome
        warm.failed = True
        untouched = not result.cards and not result.acts and not result.visuals
        # A child just started that answered with an error is today's failure:
        # running it again would only buy the same one.
        if untouched and outcome.spent is None and (gone or warm.reused):
            raise plannerwarm.ColdAgain(outcome.usage)
        return outcome

    async def _tool(self, api: tools_read.SwarmApi, use: dict, kinds: set[str], known: tuple[str, ...],
                    result: TurnResult, *, transcript: list[tuple[str, str]] | None = None,
                    ) -> tuple[str, bool, str | None]:
        """(the tool_result text, whether it is an error, the spoken summary if `speak`)."""
        name = str(use.get("name") or "")
        args = use.get("input") if isinstance(use.get("input"), dict) else {}
        if name == prompts.SPEAK_TOOL:
            summary = args.get("summary")
            said = summary if isinstance(summary, str) else ""
            if args.get("point_at") is not None:
                await self._point(api, result, args["point_at"],
                                  prompts.spoken(said, secrets_=known))
            noted = prompts.brief_text(args.get("brief"), secrets_=known)
            if noted is not None:
                # The last `speak` wins; it is written once, after the turn.
                result.brief = noted
            return "said", False, said
        if tools_visual.is_visual_tool(name):
            try:
                if name == tools_visual.SHOW_MOCKUP:
                    body = await draw_mockup(api, _scrub_all(args, known))
                else:
                    body = await tools_visual.run_visual_tool(api, name, _scrub_all(args, known))
            except (tools_read.BadInput, tools_read.ApiRefused) as exc:
                result.refused.append(name)
                if name in VISUAL_KINDS:
                    result.drawn.append({"kind": VISUAL_KINDS[name], "title": _title(args),
                                         "refused": str(exc)})
                if isinstance(exc, tools_visual.FramesRefused) and not _redrawn(result, args):
                    self._frames_dropped(api, result, args, 0, exc.dropped)
                return str(exc), True, None
            if name in VISUAL_KINDS and isinstance(body, dict) and body.get("visual_id"):
                result.drawn.append(_visual_drawn(name, args))
                if isinstance(body.get("frames"), list) and "frames" in result.drawn[-1]:
                    # The frames swarm-api drew, so a frame left out is not traced as drawn.
                    result.drawn[-1]["frames"] = [str(f.get("label"))[:200] for f in body["frames"]
                                                  if isinstance(f, dict)]
                if name == tools_visual.SHOW_MOCKUP:
                    # A redraw that drew this mock-up supersedes what an earlier
                    # call left out, so the voice names only a version still missing.
                    result.frames_dropped = [m for m in result.frames_dropped
                                             if not _same_mockup(m, args)]
                if body.get("dropped"):
                    self._frames_dropped(api, result, args, len(body.get("frames") or []),
                                         body["dropped"], visual_id=body.get("visual_id"))
            result.visuals.append({"tool": name, **{k: body.get(k) for k in (
                "visual_id", "note_id", "work") if isinstance(body, dict) and body.get(k)}})
            return prompts.fence(name, prompts.scrub(prompts.as_text(body), known)), False, None
        if tools_act.is_act_tool(name):
            try:
                body = await tools_act.answer_question(api, self.ledger(api.session_id),
                                                       _scrub_all(args, known),
                                                       transcript=transcript or ())
            except (tools_read.BadInput, tools_read.ApiRefused, actledger.ActRefused) as exc:
                result.refused.append(name)
                return prompts.scrub(str(exc), known), True, None
            result.acts.append({"tool": name, **{k: body.get(k) for k in (
                "status", "task_id", "question_index") if body.get(k) is not None}})
            return prompts.fence(name, prompts.scrub(prompts.as_text(body), known)), False, None
        if tools_read.is_read_tool(name):
            result.reads.append(name)
            result.read_targets.append({"tool": name, "target": read_target(args)})
            try:
                body = await tools_read.run_read_tool(api, name, args)
            except tools_read.BadInput as exc:
                return str(exc), True, None
            except tools_read.ApiRefused as exc:
                return str(exc), True, None
            return prompts.fence(name, prompts.scrub(prompts.as_text(body), known)), False, None
        kind = name[len(DRAW_PREFIX):] if name.startswith(DRAW_PREFIX) else ""
        if not kind or kind not in kinds:
            result.refused.append(name)
            result.drawn.append({"kind": (kind or name)[:64], "title": _title(args),
                                 "refused": "not a card kind in the catalogue or a tool offered this turn"})
            log.warning("voice session %s: planner called an unknown kind or tool; refused",
                        api.session_id)
            return (f"refused: {name[:64]!r} is not a card kind in the catalogue or a tool offered "
                    "this turn; nothing was drawn"), True, None
        if len(result.cards) >= MAX_CARDS:
            result.refused.append(name)
            result.drawn.append({"kind": kind, "title": _title(args),
                                 "refused": f"a turn draws at most {MAX_CARDS} cards"})
            return f"refused: a turn draws at most {MAX_CARDS} cards", True, None
        try:
            card, short = await write_card(api, kind, args, known)
        except tools_read.ApiRefused as exc:
            result.refused.append(name)
            result.drawn.append({"kind": kind, "title": _title(args), "refused": str(exc)})
            log.warning("voice session %s: a %s card was not written: %s", api.session_id, kind, exc)
            return f"the card was not written: {exc}", True, None
        result.cards.append({"kind": kind, "card_id": card.get("card_id"), "title": card.get("title")})
        if self._updates is not None:
            # On screen as it is drawn; the voice names it at the next gap.
            self._updates.drawn(api.session_id, kind, card.get("title"), card.get("card_id"))
        result.drawn.append({"kind": kind, "title": card.get("title") if isinstance(card.get("title"), str)
                             else _title(args)})
        answer = {"written": True, "card_id": card.get("card_id"), "version": card.get("version")}
        if short is not None:
            log.warning("voice session %s: a %s card stopped short: %s", api.session_id, kind, short)
            return prompts.as_text({**answer, "not_written": short}), True, None
        return prompts.as_text(answer), False, None


    @staticmethod
    def _frames_dropped(api: tools_read.SwarmApi, result: TurnResult, args: dict[str, Any],
                        drawn: int, dropped: list[dict[str, Any]],
                        visual_id: Any = None) -> None:
        """Record the frames a mock-up left out, for the voice to say; log
        their numbers and reasons, never their HTML."""
        frames = args.get("frames")
        asked = len(frames) if isinstance(frames, list) else drawn + len(dropped)
        result.frames_dropped.append({"title": _title(args), "asked": asked, "drawn": drawn,
                                      "dropped": dropped, "visual_id": visual_id})
        log.warning("voice session %s: a mock-up drew %d of %d frames; left out: %s",
                    api.session_id, drawn, asked,
                    "; ".join(f"frame {d['n']} {d['why']}" for d in dropped))

    async def _point(self, api: tools_read.SwarmApi, result: TurnResult, point_at: Any,
                     caption: str) -> None:
        """Send `speak`'s `point_at` to the page as a `focus` (§8.5). A refusal
        is recorded and the speech still plays: pointing is never worth a
        silent turn."""
        try:
            result.point_at = await tools_visual.focus(api, point_at, caption=caption or None)
        except (tools_read.BadInput, tools_read.ApiRefused) as exc:
            result.refused.append(f"{prompts.SPEAK_TOOL}.point_at")
            log.warning("voice session %s: speak's point_at was not shown: %s", api.session_id, exc)


def build_planner(http: httpx.AsyncClient, *, broker: VoiceBroker | None = None) -> Planner | None:
    """The deployment's planner from the environment, or None when swarm-api's
    URL is not configured (then no session offers delegation).

    * `SWARM_API_URL` -- swarm-api's base URL;
    * `SWARM_API_AUDIENCE` -- the audience swarm-api verifies (defaults to the URL);
    * `QUOTA_BROKER_URL`, `QUOTA_BROKER_AUDIENCE` -- the broker each turn's
      account is leased from (accountlease.build_broker);
    * `CLAUDE_CODE_BIN` -- the CLI, `claude` on PATH by default (the image
      installs it at agent-runtime-base's pinned version)."""
    base = os.environ.get("SWARM_API_URL", "").strip()
    if not base:
        return None
    audience = os.environ.get("SWARM_API_AUDIENCE", "").strip() or base
    accounts = PlannerAccounts(broker or build_broker(http))
    return Planner(http, api_base=base, id_tokens=tools_read.google_id_tokens(audience),
                   accounts=accounts, warm=plannerwarm.WarmPlanners(accounts))
