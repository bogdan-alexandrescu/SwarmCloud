"""The row view: what a Claude Code workflow agent shows while SwarmCloud runs.

WHY THIS EXISTS (owner decision, 2026-09-26). A Claude Code workflow can run a
step in SwarmCloud and still show it in `/workflows` as a running agent: the
plugin's `sc:remote` and `sc:step` agents dispatch or adopt a task and then
FOLLOW it, and each follow call's result is what that row's transcript shows.
Claude Code has no executor hook for `agent()`, so a local agent that follows
the remote task IS the row -- and this module is what makes that row readable
and affordable.

`follow.follow` already answers "what is new since this cursor". Three things
it did not do are the difference between a usable row and an unusable one:

1. A TOKEN, NOT A STRUCTURE. The cursor is a nested object the caller passes
   back verbatim. A small model copying it by hand between calls is a
   transcription on every poll, and one dropped field re-reads the run from
   the start. `since` is the same cursor, plus the state last reported and the
   one-off lines already said, as one opaque string.

2. NARRATION, NOT NDJSON. A claude-code agent's own stdout (`agent_stdout`;
   the runner's `stdout` is its JSON log) is `stream-json`: one
   JSON object per line, many of them tool results holding whole files. Shown
   raw, twenty kilobytes of it is five thousand tokens of braces in the row's
   context on every call. Each object is narrated here as one short line --
   what the agent said, which tool it called, how the tool answered, what the
   run cost -- and every line is capped. The record itself is untouched: the
   task's log objects hold every byte, `swarm tail` prints them, and a row that
   leaves lines out SAYS how many and where they are.

3. WAITING IN THE TOOL. An MCP tool returns once, and an agent whose only tools
   are the swarmcloud ones cannot sleep. Polling as fast as it can answer
   spends a turn every few seconds and grows its context with each one. So a
   call may GATHER for up to `wait_seconds` and return what arrived in that
   window: early when every task has finished, when a task leaves the queue and
   starts, or when the read budget is spent; and at once on the first call, so
   a row says straight away whether it is running or waiting. It still returns
   once, and it still holds no connection: it re-reads the same routes every
   few seconds, exactly as a caller would.

4. KNOWING WHEN TO STOP. `follow` reports a task it cannot read as
   `read: failed`, not as an error, and rightly: a transport blip is not the
   end of a task. But a row that polls a task it can NEVER read -- a
   mis-copied id (404), a task of another tenant or another deployment (404),
   a read this identity is refused (403) -- would otherwise follow it until
   its turn limit: hundreds of windows, hours of a row that looks "running",
   thousands of requests. So each task's failed-read streak rides in the
   `since` token; a 403 or 404 ends it at once, and `READ_FAILURE_LIMIT` calls
   in a row that could not read it at all end it too. The reply then says
   `stop: true`, and the task's row says why (`abandoned_because`). The same
   `stop` ends a row handed ANOTHER step's task: given the `step_id` it
   expects, a task that is a different step is not followed at all.

The outcome block (`outcome`) is what a finished row hands back to its
workflow: the agent's answer, the attempt's spend, the duration, the pull
request, the artifacts and -- on a failure -- the error and the per-attempt
record. None of it is invented: a figure that was not recorded is null with a
sentence saying so, never 0.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import threading
import time
import zlib
from collections import OrderedDict
from typing import Any, Callable

from swarm_common.states import PENDING_STATES

from .client import SwarmClient, SwarmError, task_id_of
from .follow import AGENT_STREAMS, DEFAULT_EVENT_PAGE, console_link, follow, follow_command, render
from .follow import STREAMS as RUNNER_STREAMS
from .patches import describe_task, explain_failure
from .render import describe_blocker, parse_time, task_label

#: The `since` token's format version. A token of another version is not
#: guessed at: it is read as no token, which re-reads rather than skips.
SINCE_VERSION = 1

#: How much raw log a `lines` call may READ. Larger than `follow`'s default on
#: purpose: in `lines` format raw bytes do not reach the caller's context --
#: narrated lines do, and those are bounded by `DEFAULT_MAX_LINES` -- so the
#: byte budget here governs how far behind a busy agent the row can fall, not
#: how many tokens it costs. The live tail object is 256KB; reading less than
#: that per call while an agent writes more is how bytes fall out of the window.
DEFAULT_LINES_LOG_BUDGET = 256 * 1024

#: How many lines one `lines` call returns. The row's transcript keeps every
#: call's result, so this is what one call adds to the following agent's
#: context. Past it, the EARLIEST lines of the call are left out and a line
#: says how many.
DEFAULT_MAX_LINES = 40

#: The longest one narrated line may be. A tool result that is a whole file is
#: one line of its first characters and an ellipsis.
MAX_LINE_CHARS = 200

#: The longest one call may gather for. An MCP call that blocks is ordinary in
#: this bridge -- `swarm_wait` blocks for up to an hour -- but a row that polls
#: every five minutes is a row that looks stuck.
MAX_WAIT_SECONDS = 300

#: How often a gathering call re-reads the routes.
POLL_SECONDS = 5.0

#: The longest a gathering call backs off to while every task it follows stays
#: PENDING (owner decision, 2026-09-26, proxy cost bound). A QUEUED, PARKED or
#: READY task holds no capacity (invariant 1) and cannot produce a new line
#: until it leaves that state -- which `started` below already ends the call
#: for, at once -- so polling it every `POLL_SECONDS` for a whole window is
#: four routes' worth of reads that can only ever answer "still pending". The
#: interval doubles each poll while that holds, capped here, and resets to
#: `POLL_SECONDS` the moment it does not.
PENDING_POLL_CAP_SECONDS = 30.0

#: How much of the answer `answer_excerpt` carries -- what a workflow step's
#: row returns in place of the whole answer (`sc:step`).
EXCERPT_CHARS = 500

#: The longest answer handed back in the outcome. The whole answer stays the
#: task's, readable in the console and from the answer route.
MAX_ANSWER_CHARS = 16_000

#: How many CALLS in a row may fail to read a task before a row stops following
#: it. A call gathers for up to `MAX_WAIT_SECONDS` and re-reads every
#: `POLL_SECONDS`, so this is several minutes of every read failing -- long
#: enough for a token refresh or a deploy to pass, short enough that a row does
#: not sit "running" for hours on a task it will never read. One successful
#: read in a call resets it.
READ_FAILURE_LIMIT = 3

#: The HTTP statuses of a read that will fail the same way next time: 404 is a
#: task this deployment does not have -- a mis-copied id, another deployment,
#: or another tenant's task, which the API answers as absent (invariant 9) --
#: and 403 a read this identity is refused. The row stops on the first one.
PERMANENT_READ_STATUSES = frozenset({403, 404})

#: The frozen vocabulary's waiting states, as strings.
_PENDING = frozenset(state.value for state in PENDING_STATES)

#: Why a task in each pending state is not running yet, as a sentence. Every one
#: of them holds no capacity (invariant 1), which is the fact a reader watching
#: a row that "does nothing" most needs.
_WAITING = {
    "SUBMITTED": "submitted, not yet queued",
    "QUEUED": "queued for admission",
    "READY": "ready, waiting for capacity",
    "PARKED": "parked",
}

#: Why a PARKED or READY task is held, per `park_reason`.
_PARKED_BECAUSE = {
    "DEPENDENCY_INCOMPLETE": "a step it depends on has not finished yet",
    # Contract request 40 (docs/design/child-tasks.md): the agent awaits the
    # child tasks it submitted, holding no capacity.
    "CHILDREN_INCOMPLETE": "it is waiting for the child tasks its agent submitted",
    "PROVIDER_QUOTA_EXHAUSTED": "the provider's quota window is exhausted; it resumes on its own",
    "PROVIDER_COOLDOWN": "the provider is cooling down; it resumes on its own",
    "PROVIDER_OUTAGE": "the provider is failing; it resumes on its own",
    "SCHEDULED_RETRY": "a retry is scheduled",
    "MANUAL_PAUSE": "dispatch is paused by an operator",
    "BUDGET_EXHAUSTED": "the tenant's budget is exhausted",
    "CREDENTIAL_MISSING": "the tenant has no credential for this profile's provider",
}


# --------------------------------------------------------------------------
# The `since` token
# --------------------------------------------------------------------------

def encode_since(
    cursor: dict[str, Any],
    states: dict[str, Any],
    said: dict[str, Any],
    groups: dict[str, str] | None = None,
    failures: dict[str, int] | None = None,
) -> str:
    """The cursor, the states last reported, the one-off lines already said,
    which tasks are read from the runner's streams rather than the agent's,
    and how many calls in a row could not read each task.

    base64url of compact JSON: opaque to the caller, readable by anyone
    debugging a row, and carrying nothing the follow report did not already
    show -- byte positions, event counts, attempt ids, state names and counts.

    THEN A DOT AND A CHECKSUM (epic #227): eight hex digits of the CRC-32 of
    the base64 text. A haiku relay copies this token by hand on every call,
    and base64 JSON with nothing to check it against can decode, after a
    one-character slip, to a DIFFERENT position -- one that moved forward
    loses output with nothing said. CRC-32 detects every single-character
    change, and every burst of up to 32 bits; `decode_since` refuses a token
    whose checksum does not match. `.` is outside the base64url alphabet, so
    the split is unambiguous.
    """
    payload = {
        "v": SINCE_VERSION,
        "c": cursor,
        "s": {k: v for k, v in states.items() if v is not None},
        "m": {k: sorted(v) for k, v in said.items() if v},
        "g": {k: v for k, v in (groups or {}).items() if v == "runner"},
        "f": {k: int(v) for k, v in (failures or {}).items() if v},
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{body}.{_checksum(body)}"


def _checksum(body: str) -> str:
    return f"{zlib.crc32(body.encode('ascii')):08x}"


def _payload(token: Any) -> dict[str, Any] | None:
    """A token's JSON, or None when it is absent, unreadable or of another version.

    A token that HAS a checksum and fails it raises: that is a token which was
    issued and then changed on the way back, and reading it would resume from
    a position nobody issued. A token with no checksum at all -- garbage, or
    one minted by a bridge older than the checksum -- is unreadable, and
    `decode_since` starts over from it and says so.
    """
    if not isinstance(token, str) or not token:
        return None
    body, dot, checksum = token.strip().rpartition(".")
    if not dot:
        return None
    if checksum != _checksum(body):
        # NEVER "or omit it" (owner, 2026-10-05): rows that took that advice
        # dropped `since`, every call then answered at once, and three rows
        # polled 56-60 times in minutes.
        raise SwarmError(
            "the `since` token fails its checksum: it is not the token a previous "
            "call returned, and was changed on its way back here -- a single "
            "character is enough. Nothing was read. Pass `since` back unchanged: copy "
            "it again from the previous reply, character for character, and call "
            "again with it"
        )
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != SINCE_VERSION:
        return None
    return payload


#: How many tokens one bridge process keeps behind handles. A row makes at most
#: ~60 calls and a session runs a few dozen rows; past this the OLDEST handles
#: go first, and a handle that has gone is read as described in `Handles.resolve`.
HANDLE_LIMIT = 4096

#: A handle: `r`, four hex digits, and a Luhn mod 16 check digit.
_HANDLE_DIGITS = 4


def _luhn16(digits: str) -> str:
    """The Luhn mod 16 check digit of these hex digits.

    Luhn mod N catches every single-character substitution and every swap of
    two adjacent different characters -- the two slips a hand copy makes.
    """
    total = 0
    factor = 2
    for ch in reversed(digits):
        addend = factor * int(ch, 16)
        total += addend // 16 + addend % 16
        factor = 1 if factor == 2 else 2
    return f"{(16 - total % 16) % 16:x}"


class Handles:
    """Each follower's `since` token, kept HERE and handed out as a short handle.

    WHY (owner decision, 2026-10-05, lane review B1). Haiku `sc:step` rows
    copied the ~175-character token by hand and corrupted it on 88 of 192
    calls (46%). The checksum refused every one, so nothing was lost -- but
    the refused rows then dropped `since`, every call without one answered at
    once, and three rows polled 56-60 times in minutes: 14.9M tokens, 51% of
    all row tokens. A handle such as `r7f3a2` is six characters to copy.

    A handle is an immutable alias of ONE token: every reply issues a new one,
    and an older handle still resumes from where IT was issued. So a call the
    host cut, repeated with the same `since`, re-reads rather than skipping
    what the cut reply carried -- exactly as the full token behaves.

    A follower is `(format, task ids)`. A follow WITHOUT `since` for a follower
    this process already answered resumes from the LAST token it issued that
    follower, and therefore holds like any follow instead of answering at once.
    The last one issued, not the last one passed back: a row that keeps
    dropping `since` would otherwise resume, on every call, from a position
    whose state has since moved, and answer at once each time.

    In memory, per bridge process: a restarted bridge knows no handle, and
    `resolve` says what it does then. The tokens hold byte positions, state
    names and counts -- what the follow report already showed -- and nothing
    secret.
    """

    def __init__(self, limit: int = HANDLE_LIMIT) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        #: handle -> (follower, token), oldest first.
        self._tokens: OrderedDict[str, tuple[tuple[Any, ...], str]] = OrderedDict()
        #: follower -> the last token issued to it, oldest first.
        self._latest: OrderedDict[tuple[Any, ...], str] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._latest.clear()

    @staticmethod
    def follower(fmt: str, task_ids: list[str]) -> tuple[Any, ...]:
        return (fmt, tuple(str(t) for t in task_ids))

    @staticmethod
    def is_handle(since: Any) -> bool:
        """Whether `since` is SHAPED like a handle -- its check digit aside."""
        if not isinstance(since, str):
            return False
        text = since.strip().lower()
        return (
            len(text) == _HANDLE_DIGITS + 2
            and text[0] == "r"
            and all(ch in "0123456789abcdef" for ch in text[1:])
        )

    def issue(self, follower: tuple[Any, ...], token: str) -> str:
        """A new handle for `token`, which is now `follower`'s latest."""
        with self._lock:
            while True:
                body = f"{secrets.randbelow(16 ** _HANDLE_DIGITS):0{_HANDLE_DIGITS}x}"
                handle = f"r{body}{_luhn16(body)}"
                if handle not in self._tokens:
                    break
            self._tokens[handle] = (follower, token)
            self._latest[follower] = token
            self._latest.move_to_end(follower)
            while len(self._tokens) > self._limit:
                self._tokens.popitem(last=False)
            while len(self._latest) > self._limit:
                self._latest.popitem(last=False)
            return handle

    def resolve(
        self, since: Any, follower: tuple[Any, ...], *, resume: bool = True,
    ) -> tuple[Any, str | None]:
        """`(token, note)`: the full token to read from, and a sentence when it
        is not the one the caller passed.

        * no `since`: `follower`'s last token when `resume` and this process
          issued one (the call then holds); else None, a first call;
        * a handle whose check digit fails: refused -- it was changed on the
          way back, and the refusal says to copy it again, never to omit it;
        * a handle issued for OTHER tasks: refused, for the same reason;
        * a handle this process does not have (it restarted, or the handle
          is older than `HANDLE_LIMIT` handles): `follower`'s last token when
          there is one, else a fresh start, and the note says so;
        * anything else -- an old full token included -- is the token itself.
        """
        if since is None or since == "":
            if not resume:
                return None, None
            with self._lock:
                latest = self._latest.get(follower)
            if latest is None:
                return None, None
            return latest, (
                "no `since` was passed; this bridge resumed from the position it last "
                "returned for these tasks, so the call held like any follow"
            )
        if not self.is_handle(since):
            return since, None
        handle = since.strip().lower()
        if _luhn16(handle[1:-1]) != handle[-1]:
            raise SwarmError(
                f"the `since` handle {since.strip()!r} fails its check digit: it is not "
                "the handle a previous call returned, and was changed on its way back "
                "here. Nothing was read. Copy `since` again from the previous reply, "
                "character for character -- six characters, `r` and five hex digits -- "
                "and call again with it"
            )
        with self._lock:
            known = self._tokens.get(handle)
            latest = self._latest.get(follower)
        if known is not None:
            owner, token = known
            if owner != follower:
                raise SwarmError(
                    f"the `since` handle {handle!r} was returned by a follow of "
                    f"{list(owner[1])} in format {owner[0]!r}, not of these tasks in this "
                    "format. Nothing was read. Copy `since` again from the previous reply "
                    "for these tasks, character for character, and call again with it"
                )
            return token, None
        if latest is not None:
            return latest, (
                f"this bridge no longer holds the position behind `since` {handle!r}, so it "
                "resumed from the last position it returned for these tasks"
            )
        return None, (
            f"this bridge holds no position behind `since` {handle!r} -- it was restarted "
            "since that handle was issued -- so this call started each task from the "
            "beginning; it may repeat what an earlier call showed"
        )


#: This bridge process's handles. One process serves one Claude Code session.
HANDLES = Handles()


def read_failures(token: Any) -> dict[str, int]:
    """Each task's count of calls in a row that could not read it, from a token.

    Zero for a token that cannot be read: a lost streak costs a few more
    windows before a row stops, which is recoverable; a streak that appeared
    from nowhere would stop a row that can still read its task.
    """
    payload = _payload(token)
    raw = payload.get("f") if payload is not None else None
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): int(v)
        for k, v in raw.items()
        if isinstance(v, int) and not isinstance(v, bool) and v > 0
    }


def decode_since(
    token: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, set[str]], dict[str, str], str | None]:
    """`(cursor, states, said, groups, note)` from a token, or a fresh start and why.

    A token that cannot be read starts from the BEGINNING and says so -- the
    rule `follow._normalise` states: a position that quietly became zero costs
    a repeat, a position that quietly became large loses output, and only one
    of those is recoverable. The failed-read streak is `read_failures`.

    A token whose checksum does not match is NOT unreadable: it is a changed
    token, and it raises `SwarmError` rather than starting over (see
    `_payload`), so the relay that changed it is told to pass it back as given.
    """
    if token is None or token == "":
        return {}, {}, {}, {}, None
    fresh = (
        {},
        {},
        {},
        {},
        "the `since` token could not be read, so this call started each task from "
        "the beginning; what follows may repeat lines an earlier call showed",
    )
    payload = _payload(token)
    if payload is None:
        return fresh
    cursor = payload.get("c") if isinstance(payload.get("c"), dict) else {}
    states = payload.get("s") if isinstance(payload.get("s"), dict) else {}
    raw_said = payload.get("m") if isinstance(payload.get("m"), dict) else {}
    said = {str(k): set(v) for k, v in raw_said.items() if isinstance(v, list)}
    raw_groups = payload.get("g") if isinstance(payload.get("g"), dict) else {}
    groups = {str(k): "runner" for k, v in raw_groups.items() if v == "runner"}
    return cursor, states, said, groups, None


# --------------------------------------------------------------------------
# Narrating a stream-json line
# --------------------------------------------------------------------------

def clip(text: Any, limit: int = MAX_LINE_CHARS) -> str:
    """One line, at most `limit` characters, with an ellipsis where it was cut."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _tool_input(block: dict[str, Any]) -> str:
    """The part of a tool call's input that says what it does."""
    given = block.get("input")
    if not isinstance(given, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        value = given.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(given, separators=(",", ":"), default=str)


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
        return " ".join(parts)
    return ""


def _money(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}"


def narrate(line: str) -> list[str]:
    """One line of an agent's stdout as the short lines a row shows.

    A `stream-json` object becomes what it MEANS -- `assistant: …`,
    `tool Bash: git status`, `tool result: …`, `finished: success · 7 turns ·
    $0.21`. Anything that is not one (a runner's plain output, or a line cut
    by the read window) passes through, capped. Nothing is dropped except
    extended-thinking blocks, whose text is not the agent's output; a thinking
    block is narrated as the word, so the row shows the agent was thinking.
    """
    text = line.rstrip("\n")
    if not text.strip():
        return []
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return [clip(text)]
    try:
        event = json.loads(stripped)
    except ValueError:
        return [clip(text)]
    if not isinstance(event, dict):
        return [clip(text)]

    kind = event.get("type")
    if kind == "system":
        if event.get("subtype") == "init":
            model = event.get("model") or "an unreported model"
            tools = event.get("tools")
            count = f" · {len(tools)} tools" if isinstance(tools, list) else ""
            return [clip(f"session started · {model}{count}")]
        return [clip(f"system: {event.get('subtype') or 'event'}")]
    if kind == "assistant":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        out: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                out.append(clip(f"assistant: {block['text']}"))
            elif block.get("type") == "tool_use":
                out.append(clip(f"tool {block.get('name') or '?'}: {_tool_input(block)}"))
            elif block.get("type") in ("thinking", "redacted_thinking"):
                out.append("thinking…")
        return out
    if kind == "user":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        out = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                label = "tool error" if block.get("is_error") else "tool result"
                out.append(clip(f"{label}: {_result_text(block) or '(empty)'}"))
        return out
    if kind == "result":
        parts = [f"finished: {event.get('subtype') or ('error' if event.get('is_error') else 'done')}"]
        if isinstance(event.get("num_turns"), int):
            parts.append(f"{event['num_turns']} turns")
        cost = _money(event.get("total_cost_usd"))
        if cost:
            parts.append(cost)
        return [clip(" · ".join(parts))]
    if kind == "rate_limit_event":
        return ["rate-limit reading"]
    return [clip(f"{kind or 'event'}: {stripped}")]


# --------------------------------------------------------------------------
# What a task is doing, as one line
# --------------------------------------------------------------------------

def state_line(task: dict[str, Any]) -> str:
    """Where one task is, in words -- above all, WHY a waiting task waits.

    A row that shows nothing for ten minutes reads as a hung agent. A task in
    QUEUED, READY or PARKED is not hung and holds no capacity (invariant 1);
    this line says which of those it is and what it is waiting for.
    """
    label = task_label(task.get("task_id"), task.get("step_id"))
    state = task.get("state")
    if task.get("read") == "failed":
        return f"[{label}] ! the task could not be read: {clip(task.get('error') or '', 160)}"
    if state in _PENDING:
        # The park reason, when there is one, IS the answer to "why": a READY
        # step held on DEPENDENCY_INCOMPLETE is waiting for a parent, not for
        # capacity, and saying both would send the reader to the wrong one.
        reason = task.get("park_reason")
        if reason:
            why = f"{_PARKED_BECAUSE.get(str(reason), str(reason).lower())} ({reason})"
        else:
            why = _WAITING.get(str(state), str(state).lower())
        blockers = task.get("blocked_by") or []
        if blockers:
            why += " -- blocked by " + "; ".join(describe_blocker(b) for b in blockers[:3])
        return f"[{label}] waiting · {state} · {why}; holds no capacity"
    if state in ("LEASED", "DISPATCHED", "STARTING"):
        return f"[{label}] starting · {state} · admitted, capacity reserved"
    if state == "RUNNING":
        return f"[{label}] running"
    return f"[{label}] {state}"


# --------------------------------------------------------------------------
# Gathering over a window
# --------------------------------------------------------------------------

def watch(
    client: SwarmClient,
    task_ids: list[str],
    *,
    since: Any = None,
    wait_seconds: float = 0,
    max_log_bytes: int = DEFAULT_LINES_LOG_BUDGET,
    max_new_events: int = DEFAULT_EVENT_PAGE,
    max_lines: int = DEFAULT_MAX_LINES,
    include_heartbeats: bool = False,
    step_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Everything these tasks produced in one window, as short lines.

    Returns `{lines, since, tasks, all_finished, stop, truncated, truncation,
    ...}`, `lines` first. `since` is what to pass next time. A task that has
    finished carries its `outcome` (see `outcome`). `stop` is true when calling again cannot
    change anything: every task has finished, or has been given up on -- it
    could not be read (see `READ_FAILURE_LIMIT`), or it is not the `step_id`
    this row expects -- and a task given up on carries `abandoned_because`.

    `step_id`, with exactly one task: the workflow step that task must be. A
    row of `/sc:swarmcloud` is handed its task id through a relay that retypes it; a
    task that turns out to be ANOTHER step is not followed, because its
    answer, cost and pull request would be reported under this step's name.

    THE WINDOW. Without `since` this reads once and returns, so a new row says
    at once what its task is doing. With it, it re-reads every
    `POLL_SECONDS` until `wait_seconds` pass, and returns earlier when every
    task has finished, when a task leaves a waiting state, or when this call's
    read budget is spent -- whatever was not read is still owed and the next
    call continues from exactly there.

    WHOSE OUTPUT. The agent CLI's own streams (`AGENT_STREAMS`) -- a
    claude-code agent's `stream-json` -- not the runner's JSON log lines. A
    task whose runner starts no agent CLI answers `not_applicable` for those;
    it is switched to the runner's streams in the same poll, and stays
    switched (the token remembers it).
    """
    if step_id is not None and len(task_ids) != 1:
        raise SwarmError(
            f"`step_id` names the step of ONE task, and {len(task_ids)} task ids were "
            "given; follow one task with it, or pass no `step_id`"
        )
    cursor, states, said, groups, note = decode_since(since)
    failures = read_failures(since)
    first_call = since in (None, "")
    wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
    budget = max(0, int(max_log_bytes))
    deadline = clock() + wait
    lines: list[str] = []
    notes: list[str] = [note] if note else []
    spent = 0
    lost = 0
    polls = 0
    started = False
    latest: dict[str, dict[str, Any]] = {}
    truncation: list[str] = []
    #: Tasks read successfully at least once in THIS call; their streak resets.
    read_ok: set[str] = set()
    #: Tasks that are not the step this row expects: task id -> their step id.
    wrong_step: dict[str, Any] = {}
    #: The next sleep, backed off while every known task stays PENDING (below).
    interval = POLL_SECONDS

    def given_up(task_id: str) -> bool:
        task = latest.get(task_id) or {}
        if task_id in wrong_step:
            return True
        return (
            task_id not in read_ok
            and task.get("read") == "failed"
            and task.get("http_status") in PERMANENT_READ_STATUSES
        )

    while True:
        polls += 1
        truncation = []
        for report in _poll(
            client, list(task_ids), cursor, groups,
            room=lambda: max(0, budget - spent),
            max_new_events=max_new_events,
            include_heartbeats=include_heartbeats,
        ):
            spent += int(report.get("log_bytes_returned") or 0)
            truncation.extend(report.get("truncation") or [])
            cursor.update(report["cursor"])
            for task in report["tasks"]:
                task_id = task["task_id"]
                latest[task_id] = task
                if task.get("read") == "ok":
                    read_ok.add(task_id)
                    if step_id is not None and task.get("step_id") != step_id:
                        wrong_step[task_id] = task.get("step_id")
                before = states.get(task_id)
                now = task.get("state") if task.get("read") == "ok" else None
                # A failed read is `render`'s line (`! <error>`); a finished
                # task's closing line is too. This says only where a task MOVED.
                if now is not None and now != before and not task.get("terminal"):
                    lines.append(state_line(task))
                if before in _PENDING and now is not None and now not in _PENDING:
                    started = True
                if now is not None:
                    states[task_id] = now
                for row in (task.get("logs") or {}).get("streams") or []:
                    lost += int(row.get("gap_bytes") or 0)
            for line in render(report, memo=said, narrate=narrate):
                if line.startswith("… "):
                    continue  # the withheld sentences; this call reports its own
                if " ! " in line and lines and lines[-1] == line:
                    continue  # the same failed read, polled again inside one window
                lines.append(line)
        # Settled: nothing another poll could change -- each task has finished,
        # or is one this row will not follow (unreadable for good, or the
        # wrong step). A 404 must not be polled for the rest of a window.
        settled = bool(latest) and all(
            (latest[t].get("read") == "ok" and latest[t].get("terminal")) or given_up(t)
            for t in latest
        )
        if settled or spent >= budget or first_call or started:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
        # BACK OFF while every task known so far is still PENDING: nothing has
        # happened that a poll could report, and `started` above already ends
        # the call the moment one leaves that state. Reset the instant a known
        # task is not pending (already terminal, running, or unreadable) --
        # that case wants the ordinary cadence, not this one.
        if latest and all(task.get("state") in _PENDING for task in latest.values()):
            interval = min(interval * 2, PENDING_POLL_CAP_SECONDS)
        else:
            interval = POLL_SECONDS

    # THE STREAK, per task, carried to the next call in the token: one good
    # read in this call clears it; a call in which every read failed adds one.
    abandoned: dict[str, str] = {}
    for task_id in task_ids:
        task = latest.get(task_id) or {}
        if task_id in wrong_step:
            abandoned[task_id] = (
                f"task {task_id} is workflow step {wrong_step[task_id]!r}, not "
                f"{step_id!r}: this row was handed another step's task id, and following "
                "it would report that step's answer, cost and pull request under this "
                "step's name. Nothing was cancelled; SwarmCloud runs both steps as before"
            )
            continue
        if task_id in read_ok:
            failures.pop(task_id, None)
            continue
        if task.get("read") != "failed":
            continue
        failures[task_id] = failures.get(task_id, 0) + 1
        status = task.get("http_status")
        error = clip(task.get("error") or "no error text", 240)
        if status in PERMANENT_READ_STATUSES:
            abandoned[task_id] = (
                f"task {task_id} cannot be read: HTTP {status} -- {error}. A 404 is a task "
                "this deployment does not have (a mis-copied id, another deployment, or "
                "another tenant's task); a 403 is a read this identity is refused. Neither "
                "changes by asking again, so this row stopped following it. Nothing was "
                "cancelled"
            )
        elif failures[task_id] >= READ_FAILURE_LIMIT:
            abandoned[task_id] = (
                f"task {task_id} could not be read on {failures[task_id]} calls in a row "
                f"(the last: {error}), so this row stopped following it. Whether the task "
                "is still running is UNKNOWN: nothing was cancelled"
            )
    for task_id, why in abandoned.items():
        task = latest.get(task_id) or {}
        label = task_label(task_id, task.get("step_id"))
        lines.append(f"[{label}] ! stopped following: {why}")

    shown, left_out = _bounded(lines, max(1, int(max_lines)))
    if left_out:
        # Spelled by `follow_command`, for the install this bridge runs from,
        # like every command the bridge hands back.
        shown.insert(
            0,
            f"… {left_out} earlier line(s) of this window are not shown here; every byte "
            f"is in the task's log, which `{follow_command(list(task_ids))}` prints",
        )
    if lost:
        notes.append(
            f"{lost} byte(s) of live output were produced faster than this row read them "
            "and the 256KB live window moved past them; the completed log holds them once "
            "the task finishes"
        )
    tasks = []
    all_finished = bool(task_ids)
    for task_id in task_ids:
        task = latest.get(task_id) or {"task_id": task_id, "read": "failed"}
        row: dict[str, Any] = {
            "task_id": task_id,
            "step_id": task.get("step_id"),
            "state": task.get("state"),
            "terminal": bool(task.get("terminal")),
            "read": task.get("read"),
            "streams": "runner" if groups.get(task_id) == "runner" else "agent",
        }
        if task.get("console"):
            # The link `follow` copied from the API's task document; absent
            # when the API served none.
            row["console"] = task["console"]
        if task.get("park_reason"):
            row["park_reason"] = task["park_reason"]
        if task.get("read") == "failed":
            row["read_error"] = task.get("error")
            row["http_status"] = task.get("http_status")
        if failures.get(task_id):
            row["read_failures"] = failures[task_id]
        if task_id in abandoned:
            # Given up on: no outcome, even for a finished task -- a wrong
            # step's outcome is exactly what must not be handed back.
            row["abandoned"] = True
            row["abandoned_because"] = abandoned[task_id]
            all_finished = False
        elif task.get("read") == "ok" and task.get("terminal"):
            try:
                row["outcome"] = outcome(client, client.task(task_id))
                asked = len(row["outcome"].get("questions") or [])
                if asked:
                    # WHAT THE AGENT ASKS THE OWNER (2026-10-05), as a line of
                    # its own after the window's: the outcome holds the text.
                    label = task_label(task_id, task.get("step_id"))
                    shown.append(f"[{label}] {questions_words(asked)}")
            except SwarmError as exc:
                # NOT all finished, then: a caller that stopped here would stop
                # with no outcome to return. The next call re-reads it.
                row["outcome"] = None
                row["outcome_unavailable_because"] = f"the finished task could not be re-read: {exc}"
                all_finished = False
        else:
            all_finished = False
        tasks.append(row)

    # STOP when another call cannot change the answer: every task finished with
    # its outcome, or given up on. Not when a finished task's outcome could not
    # be re-read -- the next call tries again, and a persistent failure there
    # is a failed read, which the streak ends.
    stop = bool(tasks) and all(
        t.get("abandoned") or (t["terminal"] and t.get("outcome") is not None) for t in tasks
    )
    reply: dict[str, Any] = {
        # LINES FIRST (epic #227). The /workflows row detail shows the START of
        # each result, and with `since` first it showed a cursor instead of
        # what the task was doing. JSON keeps this order through the tool.
        "lines": shown,
        "since": encode_since(cursor, states, said, groups, failures),
        "tasks": tasks,
        "all_finished": all_finished,
        "stop": stop,
        "still_running": [t["task_id"] for t in tasks if not t["terminal"] and not t.get("abandoned")],
        "polls": polls,
        "log_bytes_read": spent,
        "truncated": bool(truncation) or bool(left_out),
        "truncation": truncation,
        "notes": notes,
        "next": (
            "Pass `since` back unchanged on the next call; nothing above is returned "
            "again. Stop when `stop` is true: then read each task's `outcome`, or its "
            "`abandoned_because` when this row gave up on it."
        ),
    }
    if abandoned:
        reply["stop_because"] = "; ".join(abandoned.values())
    return reply


def _poll(
    client: SwarmClient,
    task_ids: list[str],
    cursor: dict[str, Any],
    groups: dict[str, str],
    *,
    room: Callable[[], int],
    max_new_events: int,
    include_heartbeats: bool,
) -> list[dict[str, Any]]:
    """One read of every task: agent streams first, the runner's where there is no agent.

    A task the route answers `not_applicable` for is re-read, in the SAME
    poll, from the runner's streams, from its PRE-POLL cursor -- so its events
    are delivered once, by the runner read, and its report is not also
    rendered from the agent read, which had nothing to say about it.
    """
    reports: list[dict[str, Any]] = []
    agent_ids = [t for t in task_ids if groups.get(t) != "runner"]
    if agent_ids:
        read = follow(
            client,
            agent_ids,
            cursor={t: cursor[t] for t in agent_ids if t in cursor},
            max_log_bytes=room(),
            max_new_events=max_new_events,
            include_heartbeats=include_heartbeats,
            streams=AGENT_STREAMS,
        )
        switched = {
            task["task_id"]
            for task in read["tasks"]
            if (task.get("logs") or {}).get("status") == "not_applicable"
        }
        for task_id in switched:
            groups[task_id] = "runner"
        reports.append(
            {
                **read,
                "tasks": [t for t in read["tasks"] if t["task_id"] not in switched],
                "cursor": {t: c for t, c in read["cursor"].items() if t not in switched},
            }
        )
    runner_ids = [t for t in task_ids if groups.get(t) == "runner"]
    if runner_ids:
        spent_so_far = sum(int(r.get("log_bytes_returned") or 0) for r in reports)
        reports.append(
            follow(
                client,
                runner_ids,
                cursor={t: cursor[t] for t in runner_ids if t in cursor},
                max_log_bytes=max(0, room() - spent_so_far),
                max_new_events=max_new_events,
                include_heartbeats=include_heartbeats,
                streams=RUNNER_STREAMS,
            )
        )
    return reports


def _bounded(lines: list[str], limit: int) -> tuple[list[str], int]:
    """The last `limit` lines, capped in width, and how many were left out.

    The LATEST lines are kept: a row is read to see where the agent is now,
    and the state and closing lines of a window come last.
    """
    capped = [clip(line, MAX_LINE_CHARS + 24) for line in lines]
    if len(capped) <= limit:
        return capped, 0
    return capped[-limit:], len(capped) - limit


# --------------------------------------------------------------------------
# What a finished task hands back
# --------------------------------------------------------------------------

def outcome(client: SwarmClient, task: dict[str, Any]) -> dict[str, Any]:
    """A finished task, as a workflow step's return value.

    `answer` is the agent's final text; `answer_json` is the last JSON object
    that text ends with, parsed, for a caller that asked the agent for one.
    `cost_usd` is the sum of every attempt's recorded spend, and null -- never
    0 -- when no attempt recorded one; `cost_incomplete` says it is a floor.
    `spend_totals`' fields sit beside it: `attempts`, `cost_usd_total`,
    `duration_s_total`, `first_started_at` and the last attempt's
    `last_attempt_cost_usd` and `last_attempt_duration_s`. On a failure, `last_error` and `failure`
    carry what the per-attempt record says, and `answer` is whatever the agent
    printed, which is not a success.
    """
    described = describe_task(task)
    out: dict[str, Any] = {
        "task_id": task_id_of(task),
        "state": task.get("state"),
        "runner_profile": task.get("runner_profile"),
    }
    out.update(final_answer(client, task))
    totals = spend_totals(client, task)
    out.update(_cost(totals))
    out.update(_duration(task))
    # EVERY ATTEMPT, with the last one beside it (lane review P1): `cost_usd`
    # above is the total; `duration_s` is the last attempt's.
    out.update(totals)
    out["pr_url"] = described.get("pull_request")
    out["artifacts"] = _artifacts(task)
    # The agent's questions for the owner, read back from its questions.json
    # (`owner_questions`); `[]` and no extra read when it asked none.
    out.update(owner_questions(client, task))
    out["last_error"] = task.get("last_error")
    # Contract request 23 (#217): the outcome ledger's own classification,
    # read first and typed, rather than a workflow row sorting free-text
    # `last_error` itself. Null on SUCCEEDED and on a task that ended before
    # this field existed -- never a reason to guess one.
    out["end_cause"] = task.get("end_cause")
    out["commits"] = described.get("commits")
    out["patch"] = described.get("patch")
    if described.get("no_patch_because"):
        out["no_patch_because"] = described["no_patch_because"]
    if described.get("no_pull_request_because"):
        out["no_pull_request_because"] = described["no_pull_request_because"]
    failure = explain_failure(client, task)
    if failure is not None:
        out["failure"] = failure
    # Where the whole answer can be read: the link the API served, as served,
    # and no key at all when it served none (`follow.console_link`).
    link = console_link(task)
    if link is not None:
        out["console"] = link
    return out


# --------------------------------------------------------------------------
# What the agent asks the owner
# --------------------------------------------------------------------------

#: The artifact an agent writes its questions for the owner into, and the
#: `result_summary` key the worker counts them under (`agent_worker.questions`,
#: owner decision 2026-10-05). Spelled again here: the bridge does not import
#: the worker.
QUESTIONS_NAME = "questions.json"
QUESTIONS_KEY = "questions"

#: The worker takes no file larger than this, so one window of the artifacts
#: route reads it whole; one byte more says whether it was cut.
QUESTIONS_MAX_BYTES = 64 * 1024


def questions_count(task: dict[str, Any]) -> int:
    """How many questions the worker counted in the task's `questions.json`, else 0.

    The worker counts only a file it validated AND uploaded; a rejected file
    is 0 here, with `questions_rejected` beside it in the summary.
    """
    count = (task.get("result_summary") or {}).get(QUESTIONS_KEY)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def questions_words(count: int) -> str:
    """The progress line's words for `count` questions (owner decision, 2026-10-05)."""
    return f"? {count} question(s) for the owner"


def _question_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def owner_questions(client: SwarmClient, task: dict[str, Any]) -> dict[str, Any]:
    """`{"questions": [...]}`: what the agent asked the owner, read from its artifact.

    WHY (owner decision, 2026-10-05). An agent that meets a decision that is
    the owner's writes it into `questions.json` instead of guessing (I310,
    W532), and the worker counts a valid one in `result_summary.questions`.
    This reads the file back through the artifacts route -- by NAME, from the
    task's own manifest, redacted by the API -- so `swarm_result` and the
    follow outcome hand the questions to whoever reads the result.

    DATA, NEVER ACTED ON. The questions are returned for a person to read;
    nothing in the bridge answers, dispatches or decides on them.

    A task that asked none reads nothing more and answers `[]`. A counted file
    that cannot be read answers `[]` with `questions_unavailable_because`, so
    "asked none" and "asked some we could not read" are never the same.
    """
    count = questions_count(task)
    if count == 0:
        return {"questions": []}
    task_id = task_id_of(task)
    why = None
    try:
        window = client.artifact_content(
            task_id, QUESTIONS_NAME, limit_bytes=QUESTIONS_MAX_BYTES + 1
        )
    except SwarmError as exc:
        window, why = None, f"the artifact route could not be read: {exc}"
    if window is not None:
        if window.get("status") != "ok" or not isinstance(window.get("content"), str):
            why = f"the artifact route says {window.get('status')}"
        elif window.get("truncated"):
            why = f"the file is larger than the {QUESTIONS_MAX_BYTES} bytes the worker takes"
    if why is None:
        try:
            parsed = json.loads(window["content"])
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return {"questions": [_question(entry) for entry in parsed if isinstance(entry, dict)]}
        why = "the file the API served is not a JSON list"
    return {
        "questions": [],
        "questions_unavailable_because": (
            f"the worker counted {count} question(s) in {QUESTIONS_NAME}, and {why}; "
            "it is in the task's artifacts"
        ),
    }


def _question(entry: dict[str, Any]) -> dict[str, Any]:
    """One question with exactly its four keys, as the worker checked them."""
    options = []
    for option in entry.get("options") or []:
        if isinstance(option, dict):
            options.append({
                "label": _question_text(option.get("label")),
                "description": _question_text(option.get("description")),
            })
    return {
        "question": _question_text(entry.get("question")),
        "options": options,
        "recommended": _question_text(entry.get("recommended")),
        "context": _question_text(entry.get("context")),
    }


def final_answer(client: SwarmClient, task: dict[str, Any]) -> dict[str, Any]:
    """The agent's final text, as `GET /v1/tasks/{id}/answer` finds it.

    That route is the one implementation of "what did the agent answer": the
    LAST `result` event of the agent's own stdout, whole, redacted after JSON
    decoding -- and only when there is none, the runner's summary, which the
    runner cuts at 2,000 characters and the route marks `complete: false`
    when it may have been cut. So a JSON object the agent was asked to END
    with survives, where the summary would have cut it off. Its four statuses
    are carried: anything but `ok` is no answer, with the route's reason.
    """
    task_id = task_id_of(task)
    if not task_id:
        return {
            "answer": None,
            "answer_excerpt": None,
            "answer_json": None,
            "answer_unavailable_because": "the task carries no id, so its answer cannot be read",
        }
    try:
        served = client.answer(task_id)
    except SwarmError as exc:
        return {
            "answer": None,
            "answer_excerpt": None,
            "answer_json": None,
            "answer_unavailable_because": f"the answer route could not be read: {exc}",
        }
    status = served.get("status")
    text = served.get("content")
    if status != "ok" or not isinstance(text, str) or not text.strip():
        why = served.get("detail") or "the route gave no reason"
        return {
            "answer": None,
            "answer_excerpt": None,
            "answer_json": None,
            "answer_unavailable_because": f"the answer route says {status}: {why}",
        }

    out: dict[str, Any] = {"answer_source": served.get("source")}
    if served.get("complete") is False:
        out["answer_truncated"] = served.get("detail") or (
            "this is the runner's summary, which is cut at 2,000 characters"
        )
    if served.get("is_error") is True:
        out["answer_is_error"] = True
    out["answer_json"] = last_json_object(text)
    if len(text) > MAX_ANSWER_CHARS:
        out["answer_truncated"] = (
            f"the answer is {len(text)} characters; the first {MAX_ANSWER_CHARS} are "
            "here and the whole of it is the task's answer in the console"
        )
        text = text[:MAX_ANSWER_CHARS]
    out["answer"] = text
    out["answer_excerpt"] = json_safe_excerpt(text)
    return out


#: What `json_safe_excerpt` writes in place of the characters a JSON string
#: would have to escape. A backslash becomes a forward slash so a Windows path
#: still reads as a path; a double quote or a backtick becomes a single quote.
_EXCERPT_REPLACEMENTS = {"\\": "/", '"': "'", "`": "'"}


def json_safe_excerpt(text: str) -> str:
    """The first `EXCERPT_CHARS` of `text`, in characters that need no escaping
    inside a JSON string: `json.dumps(x, ensure_ascii=False) == '"' + x + '"'`.

    WHY (#285). An `sc:step` row is a Haiku relay that RETYPES the bridge's
    outcome into its `StructuredOutput` JSON. Served the raw answer's first 500
    characters, it failed validation five times on one step -- backticks,
    double quotes, `********` masks, paths and backslashes -- and a step whose
    task had SUCCEEDED reported `state: null`. A field that needs no escaping
    can be copied verbatim, so there is nothing for the relay to get wrong.

    Control characters (newlines and tabs included, and the C1 range and the
    Unicode line/paragraph separators, which some JSON readers reject) become
    a space and runs of whitespace collapse to one; a backslash, double quote
    or backtick is replaced as `_EXCERPT_REPLACEMENTS` says. The replacing is
    done BEFORE the cut, so the limit is measured on the text served: the
    result, ellipsis included, is at most `EXCERPT_CHARS` characters. `****`
    masks and path text pass through. This is the excerpt only: `answer`, the
    whole answer, stays exactly as the task wrote it.
    """
    chars = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F or ch in ("\u2028", "\u2029"):
            chars.append(" ")
        else:
            chars.append(_EXCERPT_REPLACEMENTS.get(ch, ch))
    flat = " ".join("".join(chars).split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[: EXCERPT_CHARS - 1] + "…"


def last_json_object(text: Any) -> dict[str, Any] | None:
    """The JSON object `text` ENDS with, or None.

    "Ends with" is the contract `sc:remote` asks a remote agent for: nothing
    after the object but whitespace or a closing code fence. An object in the
    middle of the answer is not the answer's object, so it is not taken.
    """
    if not isinstance(text, str):
        return None
    body = text.rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    if not body.endswith("}"):
        return None
    decoder = json.JSONDecoder()
    position = len(body)
    for _ in range(400):
        position = body.rfind("{", 0, position)
        if position < 0:
            return None
        try:
            value, end = decoder.raw_decode(body, position)
        except ValueError:
            continue
        if isinstance(value, dict) and not body[end:].strip():
            return value
    return None


#: What `swarm_api.attempt_totals` serves on a task, read back by these names.
TOTAL_FIELDS = (
    "attempts",
    "attempts_with_cost",
    "cost_usd_total",
    "cost_incomplete",
    "duration_s_total",
    "duration_incomplete",
    "first_started_at",
    "last_attempt_cost_usd",
    "last_attempt_duration_s",
)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _attempt_seconds(attempt: dict[str, Any]) -> float | None:
    started = parse_time(attempt.get("started_at"))
    completed = parse_time(attempt.get("completed_at"))
    if started is None or completed is None or completed < started:
        return None
    return (completed - started).total_seconds()


def _derived_totals(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """`attempt_totals`' rules over the attempts route's rows, for an API that serves none."""
    costs = [c for c in (_number(a.get("cost_usd")) for a in attempts) if c is not None]
    seconds = [s for s in (_attempt_seconds(a) for a in attempts) if s is not None]
    starts = [(parse_time(a.get("started_at")), a.get("started_at")) for a in attempts]
    starts = [(moment, raw) for moment, raw in starts if moment is not None]
    last = max(
        attempts,
        key=lambda a: (a.get("generation") or 0, str(a.get("created_at") or "")),
        default=None,
    )
    last_seconds = _attempt_seconds(last) if last is not None else None
    return {
        "attempts": len(attempts),
        "attempts_with_cost": len(costs),
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_incomplete": len(costs) < len(attempts),
        "duration_s_total": round(sum(seconds), 1) if seconds else None,
        "duration_incomplete": len(seconds) < len(attempts),
        "first_started_at": min(starts)[1] if starts else None,
        "last_attempt_cost_usd": _number(last.get("cost_usd")) if last is not None else None,
        "last_attempt_duration_s": round(last_seconds, 1) if last_seconds is not None else None,
    }


def spend_totals(client: SwarmClient, task: dict[str, Any], *, read_attempts: bool = True) -> dict[str, Any]:
    """The task's cost and time over EVERY attempt, with the last attempt's beside them.

    WHY (owner decision, 2026-10-05, lane review P1): `swarm result` and this
    outcome reported only the final attempt -- UR1's implement step served
    $0.51 while its two attempts cost $9.64. The API serves the totals on the
    task (`swarm_api.attempt_totals`) and they are read from there, as served.
    An API from before them, or one whose attempt read failed, is totalled
    here from `GET /v1/tasks/{id}/attempts` by the same rules: a missing
    attempt cost makes the total a floor (`cost_incomplete`), and nothing
    recorded is null -- never 0.

    `read_attempts=False` is for a reader that promises no extra round trip
    (`swarm_result` on a success): with no served totals it says so instead.
    """
    served = task.get("attempts")
    if isinstance(served, int) and not isinstance(served, bool) and task.get("attempts_read") != "failed":
        out = {field: task.get(field) for field in TOTAL_FIELDS}
        if out["attempts_with_cost"] is None:
            out.pop("attempts_with_cost")
        return out
    if not read_attempts:
        why = (
            "the API could not read this task's attempts"
            if task.get("attempts_read") == "failed"
            else "this API serves no attempt totals"
        )
        return {**{field: None for field in TOTAL_FIELDS},
                "totals_unavailable_because": f"{why}; swarm_follow's outcome reads them per attempt"}
    task_id = task_id_of(task)
    if not task_id:
        return {**{field: None for field in TOTAL_FIELDS},
                "totals_unavailable_because": "the task carries no id, so its attempts cannot be read"}
    try:
        attempts = client.attempts(task_id)
    except SwarmError as exc:
        return {**{field: None for field in TOTAL_FIELDS},
                "totals_unavailable_because": f"the attempts could not be read: {exc}"}
    return _derived_totals(attempts)


def cost_words(totals: dict[str, Any]) -> str:
    """The total as one line, the last attempt in brackets: what a reader is shown."""
    if totals.get("totals_unavailable_because"):
        return f"cost unreadable: {totals['totals_unavailable_because']}"
    count = totals.get("attempts")
    total = _number(totals.get("cost_usd_total"))
    if total is None:
        return "no attempts yet" if count == 0 else "cost not recorded -- NOT MEASURED, not $0"
    words = f"${total:.2f}"
    if totals.get("cost_incomplete"):
        words = f"at least {words}"
    if not isinstance(count, int) or count <= 1:
        return words
    notes = []
    with_cost = totals.get("attempts_with_cost")
    if totals.get("cost_incomplete") and isinstance(with_cost, int):
        notes.append(f"{count - with_cost} of {count} attempts recorded no cost")
    last = _number(totals.get("last_attempt_cost_usd"))
    notes.append("last attempt " + (f"${last:.2f}" if last is not None else "not recorded"))
    return f"{words} over {count} attempts ({'; '.join(notes)})"


def _cost(totals: dict[str, Any]) -> dict[str, Any]:
    """`cost_usd`, the task's spend over every attempt, and why it is null or a floor."""
    out: dict[str, Any] = {"cost_usd": totals.get("cost_usd_total")}
    if totals.get("totals_unavailable_because"):
        out["cost_note"] = totals["totals_unavailable_because"]
    elif out["cost_usd"] is None:
        out["cost_note"] = (
            "no attempt recorded a cost" if totals.get("attempts") else "the task has no attempts"
        ) + " -- this is NOT MEASURED, not $0"
    elif totals.get("cost_incomplete"):
        out["cost_note"] = f"{cost_words(totals)}: an attempt recorded no cost, so this is a floor"
    return out


def _duration(task: dict[str, Any]) -> dict[str, Any]:
    started = parse_time(task.get("started_at"))
    completed = parse_time(task.get("completed_at"))
    if started and completed and completed >= started:
        return {
            "duration_s": round((completed - started).total_seconds(), 1),
            # The task's `started_at` is rewritten by every attempt's STARTING,
            # so this span is the LAST attempt's; `duration_s_total` is all of them.
            "duration_basis": "the last attempt's started_at to completed_at",
        }
    seconds = (task.get("result_summary") or {}).get("duration_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return {"duration_s": round(float(seconds), 1), "duration_basis": "the last attempt's runner"}
    return {"duration_s": None, "duration_basis": "not recorded"}


def _artifacts(task: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for artifact in (task.get("result_summary") or {}).get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("name"):
            out.append({k: artifact.get(k) for k in ("name", "uri", "bytes") if k in artifact})
    return out
