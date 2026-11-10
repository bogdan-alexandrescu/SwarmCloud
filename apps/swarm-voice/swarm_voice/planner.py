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
     `show_diff`, `show_log`, `add_note`, `notes_to_work`) and `speak`, whose
     `point_at` reaches the page as a `focus` (voice-it-own-speech.md §8.5);
  3. leases ONE Claude subscription account from the quota broker for the
     person's own workspace (plannerlease.py), released in a finally whatever
     the turn does; a full pool is a spoken refusal, never a wait;
  4. runs Claude Opus 5.5 as headless Claude Code on that account
     (claudeturn.py), its only tools those of step 2 through a local MCP
     server, executing each read through swarm-api with the grant and writing
     each card through swarm-api's card write path, which validates it against
     the catalogue and appends it to the session's stream;
  5. hands the live voice at most two sentences to say (prompts.spoken).

AN UNKNOWN KIND IS REFUSED. A tool the model calls that is not a read tool,
not `speak` and not one of the catalogue's `draw_<kind>` tools as served this
turn is answered with an error and never reaches swarm-api; the refusal is
recorded on the turn's result. swarm-api refuses an unknown kind again on write
(cards.validate), so the console never sees one.

THE ACCOUNT (§3.6; the owner, 2 November 2026: "we have a pool of account for
anthropic, lets use one of the accounts there"). The turn runs on a pool
lease, as a worker's agent does, and counts one agent on that account while it
runs. swarm-voice reads no Anthropic secret and holds no accessor grant: the
broker hands the lease's token with the lease, and it goes to the Claude Code
child's environment alone (CLAUDE_CODE_OAUTH_TOKEN), never to the Messages API
from here and never as ANTHROPIC_API_KEY.

WHAT IT COSTS. A subscription turn has no measured price: `usage` carries the
token counts, the model and `billing: "subscription"`, and the ledger
(swarm_api.voice.ledger.turn_cost) counts the turn against the session's $100
and the day's $300 by its estimate at Opus prices, shown as not measured --
never as $0.

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

from . import actledger, prompts, tools_act, tools_read, tools_visual
from .accountlease import VoiceBroker, build_broker
from .claudeturn import ClaudeCode
from .credential import CredentialError
from .grant import SessionGrant
from .plannerlease import NO_ACCOUNTS_REGISTERED, PlannerAccounts, PoolFull

log = logging.getLogger("swarm_voice.planner")

#: Model calls in one turn: reads, draws and the closing `speak`.
MAX_ROUNDS = 8
#: Cards one turn may write: a delegation usually needs one (§3.3).
MAX_CARDS = 4
#: The whole turn, after which the voice is told the planner ran out of time.
#: Below the broker's ten-minute voice hold, so a lease outlives no turn.
TURN_SECONDS = 240.0
#: How a subscription turn's cost is recorded (module docstring, "WHAT IT COSTS").
BILLING = "subscription"

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


def _scrub_all(value: Any, known: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return prompts.scrub(value, known)
    if isinstance(value, list):
        return [_scrub_all(v, known) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_all(v, known) for k, v in value.items()}
    return value


def _card_from(kind: str, args: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    """The card a `draw_<kind>` call asks for, as swarm-api's write path takes it.
    The project, session, version and time are swarm-api's to set."""
    card = {"card_id": args.get("card_id"), "kind": kind, "state": args.get("state"),
            "title": args.get("title"), "created_by": "planner",
            "references": args.get("references"), "summary": args.get("summary"),
            "body": args.get("body")}
    return _scrub_all(card, known)


class Planner:
    """Claude Opus, as Claude Code on a pool lease, over the catalogue's draw
    tools and the read tools."""

    def __init__(self, http: httpx.AsyncClient, *, api_base: str, id_tokens: tools_read.IdTokens,
                 accounts: PlannerAccounts, claude: ClaudeCode | None = None,
                 max_rounds: int = MAX_ROUNDS, turn_seconds: float = TURN_SECONDS) -> None:
        self._http = http
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
        self._locks.pop(session_id, None)
        self._acts.pop(session_id, None)

    def ledger(self, session_id: str) -> actledger.ActLedger:
        return self._acts.setdefault(session_id, actledger.ActLedger())

    async def delegate(self, *, person: str, grant: SessionGrant | None,
                       transcript: list[tuple[str, str]], delegation_id: str | None = None,
                       model: str | None = None, request: str | None = None) -> TurnResult:
        """Run one turn for a delegation; never raises for a refusal or a failure:
        the result's `spoken` always says what happened. `model` is the
        session's planner model, read at its start; None keeps the pin.
        `request` is the delegation's own text, when the voice gave it any
        (GPT-Live's delegation event, live.py), handed over beside the transcript."""
        try:
            api = tools_read.SwarmApi.for_session(self._http, base_url=self._api_base,
                                                  id_tokens=self._id_tokens, grant=grant)
        except tools_read.NoGrant as exc:
            log.warning("planner refused: %s", exc)
            return TurnResult(delegation_id, SAY_NO_GRANT, error=str(exc))
        lock = self._locks.setdefault(api.session_id, asyncio.Lock())
        async with lock:
            result = TurnResult(delegation_id, "")
            try:
                return await asyncio.wait_for(
                    self._turn(api, result, person=person, transcript=transcript, model=model,
                               request=request),
                    self._turn_seconds)
            except asyncio.TimeoutError:
                log.warning("voice session %s: planner turn ran out of time", api.session_id)
                result.spoken, result.error = SAY_FAILED, "the turn ran out of time"
                return result

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
        try:
            async with self._accounts.lease(person=person, voice_session_id=api.session_id) as held:
                return await self._run(api, result, held.bearer.reveal(), tools, kinds, transcript,
                                       claude=self._claude.with_model(model) if model else self._claude,
                                       request=request)
        except PoolFull as exc:
            log.warning("voice session %s: no planner account: %s", api.session_id, exc.reason)
            result.spoken = SAY_NO_POOL if exc.reason == NO_ACCOUNTS_REGISTERED else SAY_POOL_FULL
            result.error = f"the pool leased no account ({exc.reason})"
            return result
        except CredentialError as exc:
            log.warning("voice session %s: planner lease failed: %s", api.session_id, exc)
            result.spoken, result.error = SAY_NO_LEASE, str(exc)
            return result

    async def _run(self, api: tools_read.SwarmApi, result: TurnResult, token: str, tools: list[dict],
                   kinds: set[str], transcript: list[tuple[str, str]], *, claude: ClaudeCode,
                   request: str | None = None) -> TurnResult:
        known = (token, *api.known_secrets())
        said: list[str] = []
        done = asyncio.Event()

        async def handler(name: str, args: dict) -> tuple[str, bool]:
            text, is_error, spoken_now = await self._tool(api, {"name": name, "input": args}, kinds, known,
                                                          result, transcript=transcript)
            if spoken_now is not None:
                said.append(spoken_now)
                done.set()
            return text, is_error

        try:
            outcome = await claude.run(
                prompt=prompts.turn_message(transcript, request=request, secrets_=known), system=prompts.SYSTEM, token=token,
                tools=tools, handler=handler, known=known, done=done)
        except OSError as exc:
            log.warning("voice session %s: the planner could not start Claude Code (%s)", api.session_id,
                        type(exc).__name__)
            result.spoken, result.error = SAY_FAILED, f"Claude Code could not start ({type(exc).__name__})"
            return result
        result.usage = {**outcome.usage, "model": claude.model, "billing": BILLING}
        result.transcript = outcome.events
        if not said and (outcome.is_error or (outcome.exit_code not in (0, None) and not outcome.stopped)):
            log.warning("voice session %s: Claude Code ended without speaking (exit %s)", api.session_id,
                        outcome.exit_code)
            result.error = f"Claude Code exited {outcome.exit_code} without speaking"
            result.spoken = SAY_FAILED
            return result
        text = said[-1] if said else outcome.result_text
        result.spoken = prompts.spoken(text or "", secrets_=known) or (
            "It's on screen." if result.cards else SAY_FAILED)
        log.info("voice session %s: planner turn drew %d card(s) [%s], refused %d, read %d",
                 api.session_id, len(result.cards), ",".join(c.get("kind", "?") for c in result.cards),
                 len(result.refused), len(result.reads))
        return result

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
            return "said", False, said
        if tools_visual.is_visual_tool(name):
            try:
                body = await tools_visual.run_visual_tool(api, name, _scrub_all(args, known))
            except (tools_read.BadInput, tools_read.ApiRefused) as exc:
                result.refused.append(name)
                return str(exc), True, None
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
            log.warning("voice session %s: planner called an unknown kind or tool; refused",
                        api.session_id)
            return (f"refused: {name[:64]!r} is not a card kind in the catalogue or a tool offered "
                    "this turn; nothing was drawn"), True, None
        if len(result.cards) >= MAX_CARDS:
            result.refused.append(name)
            return f"refused: a turn draws at most {MAX_CARDS} cards", True, None
        try:
            written = await api.write_card(_card_from(kind, args, known))
        except tools_read.ApiRefused as exc:
            result.refused.append(name)
            log.warning("voice session %s: a %s card was not written: %s", api.session_id, kind, exc)
            return f"the card was not written: {exc}", True, None
        card = written.get("card") if isinstance(written.get("card"), dict) else written
        result.cards.append({"kind": kind, "card_id": card.get("card_id"), "title": card.get("title")})
        return prompts.as_text({"written": True, "card_id": card.get("card_id"),
                                "version": card.get("version")}), False, None


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
    return Planner(http, api_base=base, id_tokens=tools_read.google_id_tokens(audience),
                   accounts=PlannerAccounts(broker or build_broker(http)))
