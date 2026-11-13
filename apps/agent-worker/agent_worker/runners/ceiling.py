"""The context ceiling: a session is ended by its size, and work goes on in a fresh one (TOKEN-CEILING, part of #2524).

The owner, 2026-10-27 (#2524 item 3, "200k tokens or 150 tool calls"): nothing
ended a session as its context grew. In the measured week the longest lanes
ran 300-640 turns at a 250-390k mean context, the spend in the 60-150 and
150+ turn buckets was most of the week's total, and one resume
(task_98d001a3, an account swap) started at 452,706 tokens and wrote 1.33M
tokens to cache in 8 turns. Every turn re-reads the whole context, so a long
session costs the square of its length; a fresh session that starts from a
short handoff costs a few thousand tokens to get back to work.

So the runner reads the CLI's stream as it is written -- through the stdout
capture's tap, as `cliagent.AccountStreamWatcher` and
`budgetwatch.BudgetStreamWatcher` do -- and:

  * a turn's CONTEXT is its top-level assistant message's
    `input_tokens + cache_read_input_tokens` (the last copy of each message:
    the stream repeats a message once per content block). A subagent's
    messages (`parent_tool_use_id` set) are its own context, not the
    session's, and are not counted;
  * the session's TOOL CALLS are its top-level `tool_use` blocks, each id once;
  * when either passes its ceiling, the CLI is stopped at the NEXT TURN
    BOUNDARY -- a top-level `user` event, the tool results of the turn
    complete -- and never mid-tool-call;
  * the runner (`cliagent.run_cli_agent`) then commits the tree as work in
    progress, writes a handoff (`handoff_prompt`) and starts a NEW session
    with it as the prompt's prefix -- never `--resume`, which would re-read
    the very context the stop was for. A resume the worker asks for (an
    account swap, a credential reload) or the runner's own finish, repair
    and gate passes follow the same rule when the session they would resume
    ended over the ceiling.

The number of fresh sessions per attempt is bounded (`MAX_FRESH_SESSIONS`):
once spent, the ceiling stops nothing more and the last session runs to its
end inside the step's timeout, so a lane that loops cannot restart forever.

It never raises on what the stream contains; a line that is not JSON is not
an event.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

#: The context, in tokens, past which a session is stopped at its next turn
#: boundary: the owner, 2026-10-27, #2524 item 3. Read as `cache_read + input` of
#: one turn, which is what each turn re-reads.
CONTEXT_CEILING_TOKENS = 200_000
#: The tool calls past which a session is stopped (the owner, #2524 item 3): the
#: lean-lanes rule in CLAUDE.md said ~150 per agent and nothing enforced it.
TOOL_CALL_CEILING = 150
#: The most a profile or a step can set either limit to. A profile or step may
#: LOWER the ceiling; a value above these is clamped to them, because the
#: decision was the owner's and a caller's input is not a way round it.
CONTEXT_CEILING_HARD_CAP = CONTEXT_CEILING_TOKENS
TOOL_CALL_HARD_CAP = TOOL_CALL_CEILING
#: Fresh sessions one attempt may start after a ceiling stop. Four: a lane
#: that needs a fifth start from a handoff is looping, not progressing, and
#: from then on the last session runs to its end inside the step's timeout.
MAX_FRESH_SESSIONS = 4

#: The step's own, lower limits: `input.context_ceiling = {"tokens": N,
#: "tool_calls": M}`, either key optional.
PAYLOAD_KEY = "context_ceiling"
#: A profile's own, lower limits, by runner profile name. A profile not named
#: here runs at the owner's ceiling.
PROFILE_LIMITS: Mapping[str, Mapping[str, int]] = {}

#: The note the agent may write for the session after it, in
#: $SWARM_ARTIFACTS_DIR, and the most of it a handoff carries.
AGENT_HANDOFF_NAME = "handoff.md"
AGENT_HANDOFF_MAX_BYTES = 16 * 1024
#: The file the runner keeps beside the worker's session channel, in the
#: attempt's private directory: the last session's context and tool calls and
#: the fresh sessions spent, so a restarted runner (a swap, a reload) knows
#: whether the session it was asked to resume ended over the ceiling.
STATE_NAME = "context-ceiling.json"

#: Why a session was stopped, and where.
WHY_CONTEXT = "context_tokens"
WHY_TOOL_CALLS = "tool_calls"
AT_TURN_BOUNDARY = "turn_boundary"
AT_RESUME = "resume"

#: The usage fields a stopped start's spend is summed from.
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: The key a stopped start's spend carries its usage under, per model, for
#: the worker to have priced (HANDOFF-COST-NOTES, #2896). A start stopped at
#: the ceiling prints no `result` event, so nothing reports its cost; the
#: session after it does print one, and its `total_cost_usd` is that
#: session's alone. Summing tokens and keeping that cost read a 30-minute,
#: four-session attempt as its last 5-turn session ($0.20, qa4-docs,
#: 2026-10-31). The worker prices these counts (`lifecycle._collect_spend`)
#: and adds them to the reported cost, so the attempt's cost is every
#: session's. The shape is `lifecycle._stream_usage`'s `usage_by_model`.
UNPRICED_KEY = "unpriced_usage_by_model"
#: What a spend says when one of its sessions' usage was not read
#: (COST-ACROSS-SESSIONS, part of #2688): measured in experiment batches one
#: and two, 17 steps restarted at the ceiling and their recorded cost left the
#: stopped session out ($17.04 recorded against about $85 read off the
#: streams). A session whose stream showed no usage is not a free one, so the
#: runner never reports such a cost as complete: it drops the reported
#: `total_cost_usd`, which the worker then prices from the whole capture and
#: marks estimated (`lifecycle._collect_spend`), and says so under this key.
COST_INCOMPLETE_KEY = "cost_incomplete"
#: Where the runner reports the attempt's sessions, in its `metrics` (copied
#: whole into `result_summary.runner.metrics`): `count`, `usage_unread` (the
#: session numbers whose usage was not read) and `cost_complete`.
SESSIONS_METRIC = "sessions"
#: The state key the tool calls per family of this attempt's ENDED runner
#: processes are carried under, so a runner restarted inside the attempt (an
#: account swap, a credential reload, a resume over the ceiling) reports every
#: session's calls, not only those of its own capture.
EARLIER_TOOL_CALLS_KEY = "earlier_tool_calls"

#: The width, in prompt tokens, of a `prompt_buckets` row: equal to
#: `lifecycle._PROMPT_BUCKET_TOKENS` (a test holds the two equal).
PROMPT_BUCKET_TOKENS = 1_000
#: A message that named no model: priced by nobody, so the worker reads the
#: whole figure as "not reported" rather than one that left it out.
UNNAMED_MODEL = "<unnamed>"
#: The counts of a `prompt_buckets` row, after its bucket, in this order.
BUCKET_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: The most of the stopped session's own last words a handoff carries
#: (#2897): what it said last, newest kept. qa4-company (2026-10-31) kept its
#: findings "in my context" and the session after it rebuilt them from
#: screenshots, because the handoff carried only the note file.
SESSION_TAIL_MAX_CHARS = 12 * 1024
#: Whether a handoff carries that tail at all: OFF unless this is "1"/"true"
#: (the owner, 2026-11-06). The tail is the previous session's own assistant text
#: fed back in as user input, and a fresh session started from it was refused
#: on its FIRST call -- `model_refusal_no_fallback`, "safeguards flagged this
#: message ... [reasoning_extraction]" (task_a27c0b2241d1492492ee,
#: task_56ad2dba41f041aa835f) -- which failed every long task at the
#: ceiling. The note file, the todos, the notes files, the diff summary and
#: the commits still go in; the agent is told to keep its findings in files.
SESSION_TAIL_ENV = "SWARM_CEILING_SESSION_TAIL"
#: What a fresh session's first call ending in a refusal looks like in the
#: stream: the CLI's own system event, or the provider's refusal text.
REFUSAL_SUBTYPE = "model_refusal_no_fallback"
_REFUSAL_MARKERS = (REFUSAL_SUBTYPE, "safeguards flagged")
#: The most files of $SWARM_ARTIFACTS_DIR a handoff lists by name (#2897).
NOTES_LIST_MAX = 40
#: What a handoff lists of $SWARM_ARTIFACTS_DIR: the files an agent keeps
#: notes and findings in. Its captures (`*.log`) and images are not notes.
NOTES_SUFFIXES = (".md", ".json", ".txt", ".yml", ".yaml", ".csv", ".jsonl")
#: The most of ONE notes file a handoff inlines (#2897: named, not inlined, was
#: the owner's open point). 8 KiB is a page or two of findings -- the head of
#: a long file says what it holds, and the file is named for the rest -- and
#: keeps one huge capture from taking the whole budget.
NOTES_INLINE_FILE_MAX_BYTES = 8 * 1024
#: The most of ALL notes files one handoff inlines, newest-modified first. 32
#: KiB is ~8-10k tokens: with the 16 KiB note file it keeps a fresh session's
#: first context a small fraction of the 200k ceiling it was started to
#: escape; the files past it are still named.
NOTES_INLINE_TOTAL_MAX_BYTES = 32 * 1024
#: Whether a handoff inlines the notes files: ON unless this is
#: "0"/"false"/"no"/"off". On by default because the notes are the handoff's
#: point (#2897); a first-call refusal already falls back to the minimal
#: handoff, which leaves them out; this setting is the rollback.
NOTES_INLINE_ENV = "SWARM_CEILING_NOTES_INLINE"
#: How far past a cap a notes file is read before its secrets are scrubbed,
#: so a secret that straddles the cut is replaced whole, not left as a prefix.
_NOTES_SCRUB_MARGIN_BYTES = 4 * 1024

#: The most lines of `git diff --stat` and of `git log` a handoff carries.
DIFF_STAT_LINES = 60
LOG_LINES = 15
_GIT_TIMEOUT_SECONDS = 60.0

#: What the agent is told up front (claude_code appends it to the prompt).
HANDOFF_NOTE = (
    "This session may be stopped at a turn boundary once its context passes "
    f"{CONTEXT_CEILING_TOKENS:,} tokens or it has made {TOOL_CALL_CEILING} tool "
    "calls, and the task continued by a fresh session that sees only a handoff. "
    f"As you work, keep a short note for it in $SWARM_ARTIFACTS_DIR/{AGENT_HANDOFF_NAME} "
    "(what is done, what is left, what you learnt) and your findings in files "
    "under $SWARM_ARTIFACTS_DIR, never only in your context."
)


@dataclass(frozen=True)
class CeilingLimits:
    tokens: int = CONTEXT_CEILING_TOKENS
    tool_calls: int = TOOL_CALL_CEILING

    def as_dict(self) -> dict[str, int]:
        return {"tokens": self.tokens, "tool_calls": self.tool_calls}


def _lowered(value: Any, cap: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return min(value, cap)


def session_tail_enabled(environ: Mapping[str, str]) -> bool:
    """Whether a handoff carries the previous session's last words
    (`SESSION_TAIL_ENV`, off by default)."""
    return environ.get(SESSION_TAIL_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def notes_inline_enabled(environ: Mapping[str, str]) -> bool:
    """Whether a handoff inlines the notes files (`NOTES_INLINE_ENV`, on by default)."""
    return environ.get(NOTES_INLINE_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def first_call_refusal(parsed: Any) -> str | None:
    """The refusal a session's stream ended in on its FIRST model call, or None.

    `parsed` is one start's events (`cliagent._parse_cli_output`). A start
    that got an answer -- a top-level assistant message that is not the
    CLI's own API-error message -- did not end on its first call, whatever
    followed. Otherwise a `system` event `model_refusal_no_fallback`, or a
    failed `result` naming a safeguards refusal, is what it ended in; the
    marker is returned so the failure can name it."""
    events = [e for e in parsed if isinstance(e, Mapping)] if isinstance(parsed, list) else []
    found: str | None = None
    for event in events:
        kind = event.get("type")
        if kind == "assistant" and not event.get("parent_tool_use_id") and not (
                event.get("isApiErrorMessage") or event.get("error")):
            return None
        if kind == "system" and event.get("subtype") == REFUSAL_SUBTYPE:
            found = found or REFUSAL_SUBTYPE
        elif kind in ("result", "assistant", "system"):
            text = json.dumps(event.get("result") if kind == "result" else event.get("message"))
            lowered = text.lower()
            for marker in _REFUSAL_MARKERS:
                if marker in lowered:
                    found = found or ("safeguards refusal" if marker == "safeguards flagged"
                                      else marker)
    return found


def resolve_limits(payload: Mapping[str, Any] | None, profile: str | None) -> CeilingLimits:
    """The ceiling for this run: the owner's values, lowered by the profile and the
    step, never raised past the hard caps. A value that is not a positive
    integer is ignored."""
    tokens, calls = CONTEXT_CEILING_TOKENS, TOOL_CALL_CEILING
    asked = payload.get(PAYLOAD_KEY) if isinstance(payload, Mapping) else None
    for source in (PROFILE_LIMITS.get(profile or ""), asked):
        if not isinstance(source, Mapping):
            continue
        lower = _lowered(source.get("tokens"), CONTEXT_CEILING_HARD_CAP)
        if lower is not None:
            tokens = min(tokens, lower)
        lower = _lowered(source.get("tool_calls"), TOOL_CALL_HARD_CAP)
        if lower is not None:
            calls = min(calls, lower)
    return CeilingLimits(tokens=tokens, tool_calls=calls)


class CeilingWatcher:
    """The context and tool calls of the runner's CLI sessions; the stop at the ceiling.

    ONE WATCHER PER RUNNER, across every start of the CLI: `begin(fresh=...)`
    starts counting a new session from zero, or carries on counting the one a
    start resumes.
    """

    def __init__(
        self,
        limits: CeilingLimits,
        *,
        fresh_sessions: int = 0,
        max_fresh_sessions: int = MAX_FRESH_SESSIONS,
    ) -> None:
        self.limits = limits
        self.fresh_sessions = fresh_sessions
        self.max_fresh_sessions = max_fresh_sessions
        self.stops: list[dict[str, Any]] = []
        #: Why a ceiling stop was called for but no fresh session followed.
        self.skipped: str | None = None
        #: Fresh sessions refused on their first call and started once more
        #: from the minimal handoff (`minimal_handoff_prompt`).
        self.refusal_restarts: list[dict[str, Any]] = []
        #: What each handoff inlined of the notes files (#2897): the session
        #: it started, and each file's name, bytes and whether it was cut --
        #: never the content -- so a deployed handoff can be checked.
        self.notes_inlined: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._fed: list[bytes] = []
        self._partial = b""
        self._start_usage = {}
        self._start_spend_messages: dict[str, tuple[str, dict[str, int]]] = {}
        self._start_turns = 0
        #: The sessions this attempt has run, an earlier runner process's
        #: included (`carry`), and the numbers of those whose usage was not
        #: read; `_unread_here` is whether one of THIS process's was.
        self.sessions = 0
        self.unread_sessions: list[int] = []
        self._unread_here = False
        self.earlier_tool_calls: dict[str, int] | None = None
        self._reset()

    def carry(self, state: Mapping[str, Any] | None) -> None:
        """Carry on the attempt's session record from the state an earlier
        runner process of the same attempt left (`STATE_NAME`)."""
        state = state or {}
        self.sessions = max(self.sessions, _count(state.get("sessions")))
        unread = state.get("unread_sessions")
        for number in unread if isinstance(unread, list) else []:
            if _count(number) and number not in self.unread_sessions:
                self.unread_sessions.append(number)
        self.earlier_tool_calls = family_counts(state.get(EARLIER_TOOL_CALLS_KEY))

    def _reset(self) -> None:
        self.session_id: str | None = None
        self.context_tokens = 0
        self.peak_context_tokens = 0
        self.turns = 0
        self._tool_ids: set[str] = set()
        self._anonymous_calls = 0
        #: Why the limits were passed, once they were, until the next boundary.
        self.passed: str | None = None
        #: Set when this start was stopped at a boundary for the ceiling.
        self.stopped_for: str | None = None
        #: The session's own last words and its last todo list (#2897): what
        #: a handoff carries of a session that kept its notes in its context.
        self._texts: list[str] = []
        self._texts_chars = 0
        self.todos: list[dict[str, Any]] | None = None

    @property
    def tool_calls(self) -> int:
        return len(self._tool_ids) + self._anonymous_calls

    @property
    def armed(self) -> bool:
        """Whether a fresh session can still follow a stop."""
        return self.fresh_sessions < self.max_fresh_sessions

    def over(self) -> str | None:
        """Why the current session is over its ceiling, or None."""
        if self.context_tokens > self.limits.tokens:
            return WHY_CONTEXT
        if self.tool_calls > self.limits.tool_calls:
            return WHY_TOOL_CALLS
        return None

    def begin(self, *, fresh: bool, seed: Mapping[str, Any] | None = None) -> None:
        """One start of the CLI begins. A fresh session counts from zero; a
        resumed one carries on (from `seed`, the state an earlier runner
        process left, when this process never saw the session)."""
        with self._lock:
            self._fed = []
        self._partial = b""
        self._start_usage: dict[str, dict[str, int]] = {}
        self._start_spend_messages = {}
        self._start_turns = 0
        if fresh:
            self._reset()
            self.sessions += 1
        else:
            # A resume carries on a session already counted -- by this
            # process, or by the earlier one whose state was carried; one
            # that left no state is still a session.
            self.sessions = max(self.sessions, 1)
            self.stopped_for = None
            self.passed = None
            if seed:
                self.session_id = _str_or_none(seed.get("session_id")) or self.session_id
                self.context_tokens = _count(seed.get("context_tokens"))
                self.peak_context_tokens = max(self.peak_context_tokens, self.context_tokens)
                self._anonymous_calls += _count(seed.get("tool_calls"))
                self.turns = _count(seed.get("turns"))

    def tap(self) -> Callable[[bytes], None]:
        """The callable the stdout capture feeds with every chunk."""
        return self._feed

    def _feed(self, chunk: bytes) -> None:
        with self._lock:
            self._fed.append(chunk)

    def poll(self, *, honour: bool = True) -> bool:
        """Read what was fed; True when the CLI must stop now, at a turn boundary.

        `honour=False` counts the rest of a stream whose CLI has exited."""
        with self._lock:
            chunk = b"".join(self._fed)
            self._fed = []
        if not chunk:
            return False
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        for raw in lines:
            event = _event(raw)
            if event is None:
                continue
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            if event.get("type") == "assistant":
                # Every message is paid for, a subagent's too (#2896).
                self._note_spend(event)
            if event.get("parent_tool_use_id"):
                continue
            kind = event.get("type")
            if kind == "assistant":
                self._note_assistant(event)
                if self.passed is None:
                    self.passed = self.over()
            elif kind == "user":
                self.turns += 1
                self._start_turns += 1
                if honour and self.passed is not None and self.armed and self.stopped_for is None:
                    self.stopped_for = self.passed
                    # Nothing after this boundary is read: the CLI stops here.
                    self._partial = b""
                    return True
        return False

    def _note_assistant(self, event: Mapping[str, Any]) -> None:
        message = event.get("message")
        if not isinstance(message, Mapping):
            return
        key = message.get("id")
        usage = message.get("usage")
        if isinstance(usage, Mapping):
            context = _count(usage.get("input_tokens")) + _count(
                usage.get("cache_read_input_tokens"))
            # The last copy of each message: the stream repeats one per block.
            slot = key if isinstance(key, str) and key else f"\x00{len(self._start_usage)}"
            self._start_usage[slot] = {f: _count(usage.get(f)) for f in USAGE_FIELDS}
            self.context_tokens = context
            self.peak_context_tokens = max(self.peak_context_tokens, context)
        content = message.get("content")
        texts = []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
            if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                continue
            if block.get("name") == "TodoWrite":
                todos = (block.get("input") or {}).get("todos") if isinstance(
                    block.get("input"), Mapping) else None
                if isinstance(todos, list):
                    self.todos = [t for t in todos if isinstance(t, Mapping)]
            block_id = block.get("id")
            if isinstance(block_id, str) and block_id:
                self._tool_ids.add(block_id)
            else:
                self._anonymous_calls += 1
        if texts:
            self._note_text("\n\n".join(texts))

    def _note_text(self, text: str) -> None:
        """Keep the session's last words, newest first to stay (#2897).

        The stream repeats a message once per content block, so a text equal
        to the last one kept is that copy, not new words."""
        if self._texts and self._texts[-1] == text:
            return
        self._texts.append(text)
        self._texts_chars += len(text)
        while len(self._texts) > 1 and self._texts_chars > SESSION_TAIL_MAX_CHARS:
            self._texts_chars -= len(self._texts.pop(0))

    def session_tail(self) -> str | None:
        """The session's own last words, at most `SESSION_TAIL_MAX_CHARS`."""
        if not self._texts:
            return None
        text = "\n\n---\n\n".join(self._texts)
        if len(text) > SESSION_TAIL_MAX_CHARS:
            text = "... (earlier cut)\n" + text[-SESSION_TAIL_MAX_CHARS:]
        return text

    def _note_spend(self, event: Mapping[str, Any]) -> None:
        message = event.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("usage"), Mapping):
            return
        usage = message["usage"]
        model = message.get("model")
        key = message.get("id")
        # The last copy of each message: the stream repeats one per block.
        slot = key if isinstance(key, str) and key else f"\x00{len(self._start_spend_messages)}"
        self._start_spend_messages[slot] = (
            model if isinstance(model, str) and model else UNNAMED_MODEL,
            {f: _count(usage.get(f)) for f in USAGE_FIELDS},
        )

    def start_spend(self) -> dict[str, Any]:
        """This start's spend as read off its stream: for a start stopped at
        the ceiling, whose CLI never printed the `result` event that totals
        it. Tokens only; the cost is priced from them downstream: every
        message's counts, a subagent's included, per model under
        `UNPRICED_KEY` (#2896)."""
        usage = dict.fromkeys(USAGE_FIELDS, 0)
        for message in self._start_usage.values():
            for field_name in USAGE_FIELDS:
                usage[field_name] += message.get(field_name, 0)
        spend: dict[str, Any] = {"usage": usage, "num_turns": self._start_turns}
        by_model = usage_by_model(self._start_spend_messages.values())
        if by_model:
            spend[UNPRICED_KEY] = by_model
        return spend

    @property
    def start_usage_read(self) -> bool:
        """Whether this start's stream showed the usage of any message."""
        return bool(self._start_usage or self._start_spend_messages)

    def note_unread(self) -> None:
        """This start's usage was not read: the current session's cost is
        unknown, so the attempt's is never reported complete."""
        number = max(self.sessions, 1)
        if number not in self.unread_sessions:
            self.unread_sessions.append(number)
        self._unread_here = True

    @property
    def unread_here(self) -> bool:
        """Whether a session THIS process ran had its usage unread."""
        return self._unread_here

    def sessions_record(self) -> dict[str, Any]:
        """`SESSIONS_METRIC`: how many sessions the attempt ran, and whether
        every one's usage was read."""
        return {
            "count": max(self.sessions, 1),
            "usage_unread": sorted(self.unread_sessions),
            "cost_complete": not self.unread_sessions,
        }

    def record_stop(self, *, at: str, why: str, **extra: Any) -> dict[str, Any]:
        """Note a ceiling stop the runner acts on: a fresh session follows."""
        self.fresh_sessions += 1
        entry = {
            "at": at,
            "why": why,
            "turn": self.turns,
            "context_tokens": self.context_tokens,
            "peak_context_tokens": self.peak_context_tokens,
            "tool_calls": self.tool_calls,
            "fresh_session": self.fresh_sessions,
            **extra,
        }
        self.stops.append(entry)
        return entry

    def state(self) -> dict[str, Any]:
        """What the next runner process of this attempt reads (`STATE_NAME`)."""
        return {
            "session_id": self.session_id,
            "context_tokens": self.context_tokens,
            "tool_calls": self.tool_calls,
            "turns": self.turns,
            "fresh_sessions": self.fresh_sessions,
            "sessions": self.sessions,
            "unread_sessions": sorted(self.unread_sessions),
            **({EARLIER_TOOL_CALLS_KEY: dict(self.earlier_tool_calls)}
               if self.earlier_tool_calls is not None else {}),
        }

    def summary(self) -> dict[str, Any]:
        """`result_summary`'s record: the limits, and every stop with its numbers."""
        return {
            "limits": self.limits.as_dict(),
            "max_fresh_sessions": self.max_fresh_sessions,
            "fresh_sessions": self.fresh_sessions,
            "stops": list(self.stops),
            "skipped": self.skipped,
            "refusal_restarts": list(self.refusal_restarts),
            "notes_inlined": list(self.notes_inlined),
            **self.sessions_record(),
        }


def usage_by_model(
    messages: Any,
) -> dict[str, dict[str, Any]]:
    """`(model, counts)` pairs as `lifecycle._stream_usage`'s `usage_by_model`:
    each model's sums, and its `prompt_buckets` rows -- `[bucket, input,
    output, cache written, cache read]`, a request whose prompt is `p` tokens
    in bucket `ceil(p / PROMPT_BUCKET_TOKENS)`, rising."""
    out: dict[str, dict[str, Any]] = {}
    for model, counts in messages:
        mine = out.setdefault(model, {**dict.fromkeys(USAGE_FIELDS, 0), "prompt_buckets": []})
        for field_name in USAGE_FIELDS:
            mine[field_name] += counts.get(field_name, 0)
        prompt = (counts.get("input_tokens", 0) + counts.get("cache_creation_input_tokens", 0)
                  + counts.get("cache_read_input_tokens", 0))
        row = [-(-prompt // PROMPT_BUCKET_TOKENS), *(counts.get(f, 0) for f in BUCKET_FIELDS)]
        mine["prompt_buckets"] = _merged_buckets(mine["prompt_buckets"], [row])
    return out


def _merged_buckets(first: Any, second: Any) -> list[list[int]]:
    rows: dict[int, list[int]] = {}
    for row in [*(first or []), *(second or [])]:
        if not isinstance(row, list) or len(row) != 1 + len(BUCKET_FIELDS):
            continue
        into = rows.setdefault(_count(row[0]), [0] * len(BUCKET_FIELDS))
        for at, value in enumerate(row[1:]):
            into[at] += _count(value)
    return [[bucket, *rows[bucket]] for bucket in sorted(rows)]


def merged_usage_by_model(first: Any, second: Any) -> dict[str, dict[str, Any]]:
    """Two `usage_by_model` maps as one: counts summed and bucket rows merged
    per model, so two stopped sessions are priced as the requests they made."""
    out: dict[str, dict[str, Any]] = {}
    for source in (first, second):
        if not isinstance(source, Mapping):
            continue
        for model, counts in source.items():
            if not isinstance(counts, Mapping):
                continue
            mine = out.setdefault(str(model), {**dict.fromkeys(USAGE_FIELDS, 0), "prompt_buckets": []})
            for field_name in USAGE_FIELDS:
                mine[field_name] += _count(counts.get(field_name))
            mine["prompt_buckets"] = _merged_buckets(
                mine["prompt_buckets"], counts.get("prompt_buckets"))
    return out


def family_counts(value: Any) -> dict[str, int] | None:
    """A `{family: calls}` map with only non-negative integer counts, or None."""
    if not isinstance(value, Mapping):
        return None
    return {str(k): _count(v) for k, v in value.items()}


def carry_tool_calls(
    path: Path | None, families: Mapping[str, int] | None
) -> dict[str, int] | None:
    """This runner process's tool calls per family, plus those of the
    attempt's runner processes before it (`EARLIER_TOOL_CALLS_KEY`); the sum
    is written back for the next one. `families` None (the capture could not
    be read) carries the earlier counts on unchanged and returns None."""
    state = read_state(path) or {}
    earlier = family_counts(state.get(EARLIER_TOOL_CALLS_KEY)) or {}
    if families is None:
        return None
    total = dict(earlier)
    for family, calls in families.items():
        total[family] = total.get(family, 0) + _count(calls)
    if path is not None:
        write_state(path, {**state, EARLIER_TOOL_CALLS_KEY: total})
    return total


def over_state(state: Mapping[str, Any] | None, session: str, limits: CeilingLimits) -> str | None:
    """Why `session`, as an earlier runner process left it, is over the ceiling, or None."""
    if not state or state.get("session_id") != session:
        return None
    if _count(state.get("context_tokens")) > limits.tokens:
        return WHY_CONTEXT
    if _count(state.get("tool_calls")) > limits.tool_calls:
        return WHY_TOOL_CALLS
    return None


def spent_sessions(state: Mapping[str, Any] | None) -> int:
    """The fresh sessions an earlier runner process of this attempt started."""
    return _count((state or {}).get("fresh_sessions"))


def seed_numbers(state: Mapping[str, Any] | None) -> dict[str, int]:
    """A stop's numbers, from the state an earlier runner process left."""
    state = state or {}
    context = _count(state.get("context_tokens"))
    return {
        "turn": _count(state.get("turns")),
        "context_tokens": context,
        "peak_context_tokens": context,
        "tool_calls": _count(state.get("tool_calls")),
    }


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _event(raw: bytes) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw.startswith(b"{"):
        return None
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


# ---------------------------------------------------------------------------
# The state file, beside the worker's channel
# ---------------------------------------------------------------------------


def state_path(channel: str | None) -> Path | None:
    """The state file beside the worker's channel or session file, or None."""
    return Path(channel).with_name(STATE_NAME) if channel else None


def read_state(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(path: Path | None, state: Mapping[str, Any]) -> None:
    """Replaced atomically, mode 0600; advisory, like the channel it sits beside."""
    if path is None:
        return
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(dict(state)))
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The handoff
# ---------------------------------------------------------------------------


def _git(repo: Path, env: Mapping[str, str], *args: str) -> tuple[int, str]:
    try:
        ran = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, type(exc).__name__
    return ran.returncode, (ran.stdout if ran.returncode == 0 else ran.stderr).strip()


def commit_wip(repo: Path, env: Mapping[str, str], message: str) -> dict[str, Any]:
    """Commit everything in the tree as work in progress, as the agent's own
    commits are: the publish reads the branch either way. Nothing to commit is
    not a failure, and a commit that fails leaves the tree exactly as it was --
    the fresh session works in the same checkout."""
    code, out = _git(repo, env, "add", "--all")
    if code != 0:
        return {"status": "failed", "detail": out[-300:]}
    code, _ = _git(repo, env, "diff", "--cached", "--quiet")
    if code == 0:
        return {"status": "clean"}
    code, out = _git(repo, env, "commit", "--no-verify", "--quiet", "-m", message)
    if code != 0:
        return {"status": "failed", "detail": out[-300:]}
    code, sha = _git(repo, env, "rev-parse", "--short", "HEAD")
    return {"status": "committed", "commit": sha if code == 0 else None}


def _lines(text: str, cap: int) -> str:
    lines = text.splitlines()
    if len(lines) <= cap:
        return text
    return "\n".join([*lines[: cap - 1], f"... and {len(lines) - cap + 1} more lines"])


def branch_summary(repo: Path, env: Mapping[str, str], base: str | None) -> tuple[str, str]:
    """(diff stat against the base, recent commits), as text for the handoff."""
    if base:
        code, stat = _git(repo, env, "diff", "--stat", base, "HEAD")
        if code != 0:
            stat = f"(git diff --stat against {base} failed: {stat[-200:]})"
        code, log = _git(repo, env, "log", "--oneline", f"-n{LOG_LINES}", f"{base}..HEAD")
    else:
        stat = "(the step's base is not known, so there is no diff stat)"
        code, log = _git(repo, env, "log", "--oneline", f"-n{LOG_LINES}")
    if code != 0:
        log = f"(git log failed: {log[-200:]})"
    return _lines(stat or "(no change against the base)", DIFF_STAT_LINES), _lines(
        log or "(no commits)", LOG_LINES)


def agent_note(artifacts_dir: Path) -> str | None:
    """The agent's own handoff note, capped, or None when it wrote none."""
    path = Path(artifacts_dir) / AGENT_HANDOFF_NAME
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(AGENT_HANDOFF_MAX_BYTES + 1)
    except OSError:
        return None
    text = data[:AGENT_HANDOFF_MAX_BYTES].decode("utf-8", errors="replace").strip()
    if len(data) > AGENT_HANDOFF_MAX_BYTES:
        text += "\n... (cut at 16 KiB)"
    return text or None


def _notes_paths(artifacts_dir: Path) -> list[tuple[str, Path]]:
    """The files the agent keeps notes and findings in, as `(name, path)`
    sorted by name: $SWARM_ARTIFACTS_DIR's own text files, one directory
    deep (#2897). The attempt's own directory and nothing else: a link is not
    followed, so no other tenant's or attempt's file is named or read. The
    note file itself is in the handoff already. `notes_files` and
    `notes_contents` both walk this, so what is named and what is inlined
    are filtered alike."""
    root = Path(artifacts_dir)
    found: list[tuple[str, Path]] = []
    try:
        if root.is_symlink() or not root.is_dir():
            return []
        entries = sorted(root.iterdir())
        for entry in entries:
            if entry.is_symlink():
                continue
            children = sorted(entry.iterdir()) if entry.is_dir() else [entry]
            for path in children:
                if (path.is_symlink() or not path.is_file()
                        or path.suffix.lower() not in NOTES_SUFFIXES
                        or path.relative_to(root).as_posix() == AGENT_HANDOFF_NAME):
                    continue
                found.append((path.relative_to(root).as_posix(), path))
    except OSError:
        pass
    return found


def notes_files(artifacts_dir: Path) -> list[str]:
    """The notes files (`_notes_paths`) as `name (N bytes)`, at most
    `NOTES_LIST_MAX`."""
    found: list[str] = []
    for name, path in _notes_paths(artifacts_dir):
        try:
            found.append(f"{name} ({path.stat().st_size} bytes)")
        except OSError:
            continue
    if len(found) > NOTES_LIST_MAX:
        return [*found[:NOTES_LIST_MAX], f"... and {len(found) - NOTES_LIST_MAX} more"]
    return found


@dataclass(frozen=True)
class NoteContent:
    """One notes file as a handoff inlines it: its name under
    $SWARM_ARTIFACTS_DIR, its text (None when the budget was spent before
    it), the bytes of it inlined, and whether it was cut."""

    name: str
    text: str | None
    bytes: int
    cut: bool


def _head(text: str, limit: int) -> str:
    """`text`'s first `limit` bytes of UTF-8, a character split at the cut dropped."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def notes_contents(
    artifacts_dir: Path, scrub: Callable[[str], str] | None = None,
) -> list[NoteContent]:
    """The notes files' contents (#2897), newest-modified first: each cut at
    `NOTES_INLINE_FILE_MAX_BYTES`, all of them at
    `NOTES_INLINE_TOTAL_MAX_BYTES`; a file past the budget comes back with
    no text, to be named as not inlined. The same files as `notes_files`.

    `scrub` is applied to what was read BEFORE the cut, read a margin past
    it, so a secret at the cut is replaced whole rather than left as a
    prefix; the caps count the scrubbed text."""
    dated: list[tuple[float, str, Path]] = []
    for name, path in _notes_paths(artifacts_dir):
        try:
            dated.append((path.stat().st_mtime, name, path))
        except OSError:
            continue
    dated.sort(key=lambda item: (-item[0], item[1]))
    left = NOTES_INLINE_TOTAL_MAX_BYTES
    out: list[NoteContent] = []
    for _, name, path in dated:
        limit = min(NOTES_INLINE_FILE_MAX_BYTES, left)
        if limit <= 0:
            out.append(NoteContent(name, None, 0, True))
            continue
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + _NOTES_SCRUB_MARGIN_BYTES + 1)
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        if scrub is not None:
            text = scrub(text)
        head = _head(text, limit)
        cut = head != text
        size = len(head.encode("utf-8"))
        left -= size
        out.append(NoteContent(name, head, size, cut))
    return out


def _fenced(text: str) -> str:
    """`text` in a code fence longer than any run of backticks in it."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text.rstrip(chr(10))}\n{fence}"


#: What follows a notes file the caps cut short, outside its fence.
NOTES_CUT_MARKER = (
    f"\n... (cut: a handoff inlines at most {NOTES_INLINE_FILE_MAX_BYTES // 1024} KiB of "
    f"one file and {NOTES_INLINE_TOTAL_MAX_BYTES // 1024} KiB of all of them; "
    "the file holds the rest)"
)


def _notes_section(notes: list[NoteContent]) -> str | None:
    blocks: list[str] = []
    left_out: list[str] = []
    for item in notes:
        if item.text is None:
            left_out.append(item.name)
            continue
        marker = NOTES_CUT_MARKER if item.cut else ""
        blocks.append(f"### {item.name}\n\n{_fenced(item.text)}{marker}")
    if not blocks and not left_out:
        return None
    if left_out:
        blocks.append(
            f"Not inlined (the {NOTES_INLINE_TOTAL_MAX_BYTES // 1024} KiB budget for the "
            "notes was spent on newer files); read them if you need them: "
            + ", ".join(left_out))
    return ("## Its notes and findings, as it last wrote them\n\n"
            "The files above, newest-modified first, as they were when it stopped. "
            "Continue from them; do not rebuild what they hold.\n\n"
            + "\n\n".join(blocks))


def _todo_lines(todos: Any) -> str | None:
    lines = []
    for todo in todos or []:
        if not isinstance(todo, Mapping):
            continue
        content = todo.get("content")
        if isinstance(content, str) and content.strip():
            status = todo.get("status")
            lines.append(f"- [{status if isinstance(status, str) else '?'}] {content.strip()}")
    return "\n".join(lines) or None


def handoff_prompt(
    *,
    stop: Mapping[str, Any],
    task_prompt: str,
    diff_stat: str | None,
    commits: str | None,
    wip: Mapping[str, Any] | None,
    note: str | None,
    then: str | None = None,
    session_tail: str | None = None,
    todos: Any = None,
    files: list[str] | None = None,
    notes: list[NoteContent] | None = None,
) -> str:
    """The prompt a fresh session starts from: the handoff, then the task.

    It carries the previous session's notes (#2897): its note file, the
    names of the notes files it kept in $SWARM_ARTIFACTS_DIR (`files`) and
    their contents (`notes`, from `notes_contents`: capped, newest first,
    already scrubbed; left out when `NOTES_INLINE_ENV` is off), its last todo
    list (`todos`) and, only when `SESSION_TAIL_ENV` turns it on, its own
    last words (`session_tail`) -- off by default, because that text fed back
    as user input got the fresh session's first call refused."""
    why = (
        f"its context reached {stop.get('context_tokens', 0):,} tokens"
        if stop.get("why") == WHY_CONTEXT
        else f"it had made {stop.get('tool_calls', 0)} tool calls"
    )
    committed = ""
    if wip and wip.get("status") == "committed":
        committed = f" The previous session's changes are committed on the branch ({wip.get('commit')})."
    elif wip and wip.get("status") == "failed":
        committed = " The previous session's changes are in the working tree, uncommitted."
    parts = [
        "CONTEXT CEILING HANDOFF (#2524). You are a fresh session continuing a task "
        f"another session started. It was stopped at a turn boundary because {why}; "
        "the platform starts a new session at "
        f"{CONTEXT_CEILING_TOKENS:,} tokens of context or {TOOL_CALL_CEILING} tool calls "
        "rather than resuming one whose every turn re-reads all of it. Nothing in the "
        f"workspace was undone.{committed} Read what follows, check the tree, and carry "
        "the task on from where it was left; do not redo finished work.",
    ]
    if diff_stat is not None:
        parts.append(f"## The branch against its base\n\n{diff_stat}")
    if commits is not None:
        parts.append(f"## Recent commits\n\n{commits}")
    parts.append(
        f"## The previous session's note ($SWARM_ARTIFACTS_DIR/{AGENT_HANDOFF_NAME})\n\n"
        + (note or "(it wrote none)")
    )
    if files:
        parts.append(
            "## Its notes and findings files ($SWARM_ARTIFACTS_DIR)\n\n"
            "Read the ones you need before you redo any of their work.\n\n"
            + "\n".join(f"- {name}" for name in files)
        )
    notes_text = _notes_section(notes or [])
    if notes_text:
        parts.append(notes_text)
    todo_text = _todo_lines(todos)
    if todo_text:
        parts.append(f"## Its todo list, as it last wrote it\n\n{todo_text}")
    if session_tail:
        parts.append(
            "## Its last messages (newest last)\n\n"
            "What it said as it worked, including anything it had found and "
            f"not yet written down.\n\n{session_tail}"
        )
    parts.append(f"## The task, as the first session was given it\n\n{task_prompt}")
    if then:
        parts.append(f"## And now\n\n{then}")
    return "\n\n".join(parts)


def minimal_handoff_prompt(
    *, task_prompt: str, note: str | None, todos: Any = None, then: str | None = None,
) -> str:
    """The handoff a fresh session refused on its first call starts from once
    more: the task, the note file and the todo list, nothing else of the
    previous session's (no branch summary, file list or last words). `then`
    is what this pass was to do -- the time left, or a finish, repair or gate
    turn's prompt -- which is the platform's, not the session's."""
    parts = [
        "CONTEXT CEILING HANDOFF (#2524). You are a fresh session continuing a task "
        "another session started; nothing in the workspace was undone and its "
        "changes are on the branch or in the tree. Check the tree, read "
        "$SWARM_ARTIFACTS_DIR, and carry the task on; do not redo finished work.",
        f"## The previous session's note ($SWARM_ARTIFACTS_DIR/{AGENT_HANDOFF_NAME})\n\n"
        + (note or "(it wrote none)"),
    ]
    todo_text = _todo_lines(todos)
    if todo_text:
        parts.append(f"## Its todo list, as it last wrote it\n\n{todo_text}")
    parts.append(f"## The task, as the first session was given it\n\n{task_prompt}")
    if then:
        parts.append(f"## And now\n\n{then}")
    return "\n\n".join(parts)
