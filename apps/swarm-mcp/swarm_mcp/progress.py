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
import time
from typing import Any, Callable

from swarm_common.states import PENDING_STATES

from .client import SwarmClient, SwarmError, task_id_of
from .follow import AGENT_STREAMS, DEFAULT_EVENT_PAGE, follow, follow_command, render
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
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _payload(token: Any) -> dict[str, Any] | None:
    """A token's JSON, or None when it is absent, unreadable or of another version."""
    if not isinstance(token, str) or not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != SINCE_VERSION:
        return None
    return payload


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

    Returns `{since, tasks, lines, all_finished, stop, truncated, truncation,
    ...}`. `since` is what to pass next time. A task that has finished carries
    its `outcome` (see `outcome`). `stop` is true when calling again cannot
    change anything: every task has finished, or has been given up on -- it
    could not be read (see `READ_FAILURE_LIMIT`), or it is not the `step_id`
    this row expects -- and a task given up on carries `abandoned_because`.

    `step_id`, with exactly one task: the workflow step that task must be. A
    row of `/sc:run` is handed its task id through a relay that retypes it; a
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
        sleep(min(POLL_SECONDS, remaining))

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
        "since": encode_since(cursor, states, said, groups, failures),
        "tasks": tasks,
        "lines": shown,
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
    0 -- when no attempt recorded one. On a failure, `last_error` and `failure`
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
    out.update(_cost(client, task))
    out.update(_duration(task))
    out["pr_url"] = described.get("pull_request")
    out["artifacts"] = _artifacts(task)
    out["last_error"] = task.get("last_error")
    out["commits"] = described.get("commits")
    out["patch"] = described.get("patch")
    if described.get("no_patch_because"):
        out["no_patch_because"] = described["no_patch_because"]
    if described.get("no_pull_request_because"):
        out["no_pull_request_because"] = described["no_pull_request_because"]
    failure = explain_failure(client, task)
    if failure is not None:
        out["failure"] = failure
    return out


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
    out["answer_excerpt"] = text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1] + "…"
    return out


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


def _cost(client: SwarmClient, task: dict[str, Any]) -> dict[str, Any]:
    """The task's spend: every attempt's `cost_usd`, summed. Null is not zero."""
    task_id = task_id_of(task)
    if not task_id:
        return {"cost_usd": None, "cost_note": "the task carries no id, so its attempts cannot be read"}
    try:
        attempts = client.attempts(task_id)
    except SwarmError as exc:
        return {"cost_usd": None, "cost_note": f"the attempts could not be read: {exc}"}
    recorded = [
        a.get("cost_usd") for a in attempts
        if isinstance(a.get("cost_usd"), (int, float)) and not isinstance(a.get("cost_usd"), bool)
    ]
    if not recorded:
        return {
            "cost_usd": None,
            "cost_note": (
                "no attempt recorded a cost" if attempts else "the task has no attempts"
            ) + " -- this is NOT MEASURED, not $0",
        }
    out: dict[str, Any] = {"cost_usd": round(sum(recorded), 6)}
    if len(recorded) < len(attempts):
        out["cost_note"] = (
            f"{len(attempts) - len(recorded)} of {len(attempts)} attempt(s) recorded no "
            "cost, so this is a floor"
        )
    return out


def _duration(task: dict[str, Any]) -> dict[str, Any]:
    started = parse_time(task.get("started_at"))
    completed = parse_time(task.get("completed_at"))
    if started and completed and completed >= started:
        return {
            "duration_s": round((completed - started).total_seconds(), 1),
            "duration_basis": "started_at to completed_at, across every attempt",
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
