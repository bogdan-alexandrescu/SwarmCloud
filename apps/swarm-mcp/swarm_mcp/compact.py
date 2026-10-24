"""`swarm_follow` `format: "progress"`: a workflow row that costs bytes, not megabytes.

WHY THIS EXISTS (owner decision, 2026-10-01: "Keep live rows, slim them").
Measured that day: six wave-1 workflows of three steps each ran as eighteen
haiku `sc:step` rows, each looping `swarm_follow` with `format: "lines"` and
`wait_seconds: 90`. Every reply carried 5-16 KB of the remote agent's narrated
log, and a row re-reads its whole transcript on every turn, so one row used
4.0M tokens over 41 calls and the six workflows about 22M in all. Rows for
steps whose parents had not started polled the whole time. What the workflow
ACTS on is one terminal line per step.

So this format returns NO log line. Per task it returns one short progress
line -- state; elapsed; attempt n/m; last checkpoint age; tokens and cost so
far; for a waiting task, what it waits for -- and only when that line differs
from the one the previous call returned, plus the state transitions since
`since`. A reply for one unfinished task is a few hundred bytes, and nothing
in it repeats an earlier reply.

WHAT IS READ, AND WHEN. Each poll reads the task document only. The attempts
(spend, tokens) and the newest events (the last checkpoint) are read once, as
the call returns, and only for a task that has started: a QUEUED, READY or
PARKED task holds no capacity (invariant 1), has nothing to report but its
state and why it waits, and is re-read at most every
`progress.PENDING_POLL_CAP_SECONDS`. A figure that was not recorded is said in
one word (`unrecorded`, `none`), a read that failed in one word
(`unreadable`); none is ever estimated, and an unrecorded cost is never $0.

THE WINDOW (owner decision, 2026-10-01, #448: the largest row that day made
20 calls over 39 turns and re-read 830k cached tokens). The first call (no
`since`) answers at once. After that a call holds until a task's STATE changes,
every task has finished or been given up on, or `wait_seconds` (at most
`MAX_WAIT_SECONDS`) pass. Progress inside one state -- a checkpoint, spend,
elapsed time -- does not end a window; it shows in the line the window ends
with. "State" is what a row acts on (`_wake_key`): waiting for admission or
capacity, parked and why, holding capacity, or finished. LEASED, DISPATCHED,
STARTING and RUNNING are one state for the hold: start-up is nothing a row can
act on, and waking on each step of it cost a call apiece.

THE PARENT HOLD. A step whose parents have not finished cannot start, and a
row that follows it has nothing to say until they do. Given `parents` -- the
parents' task ids, from the workflow read -- a call holds, the first call too,
while the task is SUBMITTED, QUEUED or PARKED on DEPENDENCY_INCOMPLETE and any
parent is unfinished. So a dependent step's row makes ONE call while its
parents run, then follows as above.

GIVING UP is `progress.watch`'s rule, unchanged: a 403 or 404 at once,
`progress.READ_FAILURE_LIMIT` calls in a row that could not read the task, or
a task that is not the `step_id` the row expects. `format: "lines"` remains
for a caller that wants the log itself.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from swarm_common.states import CONCURRENCY_STATES

from . import details
from .client import TERMINAL, SwarmClient, SwarmError
from .follow import console_link, event_type, waiting_brief
from .progress import (
    _PENDING,
    PENDING_POLL_CAP_SECONDS,
    PERMANENT_READ_STATUSES,
    READ_FAILURE_LIMIT,
    SINCE_VERSION,
    _checksum,
    _payload,
    clip,
    outcome,
    questions_count,
    questions_words,
    waiting_key,
    waiting_words,
)
from .render import _DEPENDENCY_WAIT, describe_blocker, merge_row, parse_time, task_label

#: The longest one progress call may hold: thirty minutes (owner decision,
#: 2026-10-01, #448). A held call costs a task read per poll and NOTHING in the
#: row's context, while every reply it returns is re-read on each later turn --
#: so a row should spend a call only when its task's state changes, and a
#: twenty-minute RUNNING step should be one call, not ten.
#:
#: IT MUST STAY UNDER THE MCP TOOL-CALL TIMEOUT the plugin runs with, or Claude
#: Code abandons the call and the row reads an error instead of a reply. The
#: plugin sets no limit of its own (`plugin/.claude-plugin/plugin.json` passes
#: no timeout and no `MCP_TOOL_TIMEOUT`), so Claude Code's default applies:
#: `MCP_TOOL_TIMEOUT`, 100,000,000 ms (about 27.8 hours) unless the developer
#: lowers it. 1800 s is far under that, and under the 3600 s `swarm_wait`
#: already holds by default in the same plugin. A developer who sets
#: `MCP_TOOL_TIMEOUT` below 1,800,000 ms makes every held row call fail.
#:
#: THE IDLE TIMEOUT IS THE OTHER LIMIT, and the one that bit (lane U12,
#: 2026-10-04: rows ended UNKNOWN on "exceeded idle timeout while waiting for
#: parent tasks"). Claude Code also aborts a call that sent NOTHING for
#: `SILENT_MAX_WAIT_SECONDS`' idle timeout, below. This full hold is served
#: only to a call the bridge keeps alive with progress notifications
#: (`server.KEEPALIVE_SECONDS`), which reset that idle clock.
MAX_WAIT_SECONDS = 1800

#: The longest a progress call may hold when the host sent no `progressToken`,
#: so the bridge has no way to send a keepalive: twenty minutes.
#:
#: WHY (read 2026-10-04 from Claude Code 2.1.283, `bin/claude.exe`): every MCP
#: tool call has an idle watchdog that ticks every 30 s and aborts the call
#: when no response AND no progress notification arrived for
#: `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` ms -- else a per-server `timeout`, else
#: 1,800,000 ms for a stdio server like this plugin's (300,000 ms for http and
#: sse). A silent thirty-minute hold plus its last reads is cut at that edge,
#: which is what U12's rows met. Two thirds of the stdio default leaves ten
#: minutes for the reads after the window and the watchdog's 30 s tick; the
#: row re-issues the hold, so a parent wait costs at most three calls an hour.
#: A developer who lowers `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` below this AND
#: runs a host that sends no progress token gets cut calls, which a row
#: repeats (`plugin/agents/step.md`).
SILENT_MAX_WAIT_SECONDS = 1200

#: How often a running task is re-read inside a window. One small read.
POLL_SECONDS = 10.0

#: The status a reader that is being rate limited is answered with. A 429 is
#: NOT a failed read: the API will answer the same read once the reader slows
#: down. So it never counts toward `READ_FAILURE_LIMIT`, and a call that met
#: one waits and reads again -- RATE_LIMIT_BACKOFF_SECONDS, doubled each time,
#: up to RATE_LIMIT_BACKOFF_CAP_SECONDS -- inside its own window instead of
#: returning (epic #1334: a second attach of one workflow minutes after the
#: first hit a 429, and its rows ended UNKNOWN). A call whose window ends
#: while still limited says so: its task's `read` is `rate_limited`, which a
#: row repeats the call on and never counts as a failure.
RATE_LIMITED_STATUS = 429
RATE_LIMIT_BACKOFF_SECONDS = 15.0
RATE_LIMIT_BACKOFF_CAP_SECONDS = 120.0

#: The `since` token's format marker. A `lines` token carries none, and is
#: read here as no token: those positions are byte offsets this format does
#: not use.
_FORMAT = "progress"

#: How many of the newest events are read for the last checkpoint.
_EVENT_PAGE = 100

#: What a pending task waits for, per `park_reason`, in a word or two.
_WAITS_FOR = {
    "DEPENDENCY_INCOMPLETE": "dependency",
    "CHILDREN_INCOMPLETE": "child tasks",
    "CI_PENDING": "pull request checks",
    "PROVIDER_QUOTA_EXHAUSTED": "quota",
    "PROVIDER_COOLDOWN": "quota cooldown",
    "PROVIDER_OUTAGE": "provider outage",
    "SCHEDULED_RETRY": "scheduled retry",
    "MANUAL_PAUSE": "operator pause",
    "BUDGET_EXHAUSTED": "budget",
    "CREDENTIAL_MISSING": "credential",
    "HUMAN_REQUIRED": "a person",
    "AWAITING_INPUT": "a person's answer",   # contract request 92
}


# --------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------

def encode_since(
    states: dict[str, Any], keys: dict[str, str], failures: dict[str, int], llm_said: set[str] | None = None,
) -> str:
    """States last reported, each task's last line key, the read-failure
    streaks, and the tasks whose `LLM: <account>` was already said.

    The same envelope as `progress.encode_since` -- base64url JSON, a dot, a
    CRC-32 -- so a relay that changes one character is refused, not resumed
    from a position nobody issued.
    """
    payload = {
        "v": SINCE_VERSION,
        "fmt": _FORMAT,
        "s": {k: v for k, v in states.items() if v is not None},
        "k": keys,
        "f": {k: int(v) for k, v in failures.items() if v},
    }
    if llm_said:
        payload["l"] = sorted(llm_said)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{body}.{_checksum(body)}"


def decode_since(token: Any) -> tuple[dict[str, Any], dict[str, str], dict[str, int]]:
    """`(states, keys, failures)`; empty for no token or one of another format.

    Raises `SwarmError` for a token whose checksum fails (`progress._payload`).
    """
    payload = _payload(token)
    if payload is None or payload.get("fmt") != _FORMAT:
        return {}, {}, {}

    def _dict(name: str) -> dict[str, Any]:
        value = payload.get(name)
        return value if isinstance(value, dict) else {}

    failures = {
        str(k): int(v) for k, v in _dict("f").items()
        if isinstance(v, int) and not isinstance(v, bool) and v > 0
    }
    return dict(_dict("s")), {str(k): str(v) for k, v in _dict("k").items()}, failures


def llm_said(token: Any) -> set[str]:
    """The tasks whose `LLM: <account>` a row already wrote (SC-DESC-DETAILS);
    empty for no token, one of another format, or one from before it."""
    payload = _payload(token)
    if payload is None or payload.get("fmt") != _FORMAT or not isinstance(payload.get("l"), list):
        return set()
    return {str(t) for t in payload["l"]}


# --------------------------------------------------------------------------
# One task, as one line
# --------------------------------------------------------------------------

def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M tok"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k tok"
    return f"{count} tok"


def waits_for(task: dict[str, Any]) -> str:
    """What a pending task waits for: dependency, capacity, quota, ..."""
    reason = task.get("park_reason")
    if reason:
        return _WAITS_FOR.get(str(reason), str(reason).lower().replace("_", " "))
    blockers = task.get("blocked_by") or []
    if blockers:
        return "capacity (" + clip("; ".join(describe_blocker(b) for b in blockers[:2]), 60) + ")"
    state = task.get("state")
    if state == "READY":
        return "capacity"
    if state == "QUEUED":
        return "admission"
    return str(state).lower()


#: The states that hold capacity (invariant 3: concurrency counts from LEASED),
#: from the frozen contract. One state for the hold: see the module docstring.
_HOLDING = frozenset(state.value for state in CONCURRENCY_STATES)

def _wake_key(task: dict[str, Any]) -> str:
    """What ends a window early: the task's state, as a row acts on it.

    Waiting (SUBMITTED, QUEUED, READY) is one state: none of them holds
    capacity and the row can do nothing about any of them. PARKED is its own,
    with its reason -- a dependency wait and a quota park are different news.
    Holding capacity (LEASED through RUNNING) is one. A finished task is its
    own state. Progress inside any of these is not a change.
    """
    if task.get("read") != "ok":
        return "unread"
    state = task.get("state")
    if state == "PARKED":
        return f"PARKED:{task.get('park_reason') or ''}"
    if state in _PENDING:
        return "waiting"
    if state in _HOLDING:
        return "holding"
    return str(state)


def _waits_on_parents(task: dict[str, Any]) -> bool:
    """Whether a task is still in the wait its parents end."""
    if task.get("read") != "ok":
        return False
    state = task.get("state")
    return state in ("SUBMITTED", "QUEUED") or (state == "PARKED" and task.get("park_reason") == _DEPENDENCY_WAIT)


#: The four token counts `record_spend` writes on an attempt document.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _spend(client: SwarmClient, task_id: str) -> tuple[str, str]:
    """`(tokens, cost)` words: summed over attempts, or one word for unknown."""
    try:
        attempts = client.attempts(task_id)
    except SwarmError:
        return "tokens unreadable", "cost unreadable"
    costs = [
        a.get("cost_usd") for a in attempts
        if isinstance(a.get("cost_usd"), (int, float)) and not isinstance(a.get("cost_usd"), bool)
    ]
    # EVERY KIND THE ATTEMPT RECORDED, cache reads and writes included (#322):
    # input + output alone read 10,007 for a run that used 1.41M tokens. A
    # kind no attempt recorded adds nothing, and none at all is `unrecorded`.
    counts = [
        a.get(field) for a in attempts for field in _TOKEN_FIELDS
        if isinstance(a.get(field), int) and not isinstance(a.get(field), bool)
    ]
    tokens = _tokens(sum(counts)) if counts else "tokens unrecorded"
    cost = f"${sum(costs):.2f}" if costs else "cost unrecorded"
    return tokens, cost


def _last_checkpoint(client: SwarmClient, task_id: str, now: datetime) -> tuple[str, str]:
    """`(words, stamp)`: the last completed checkpoint's age, and its time as a key."""
    try:
        events, _token, paged = client.events_page(task_id, limit=_EVENT_PAGE, newest_first=True)
    except SwarmError:
        return "checkpoint unreadable", "?"
    found = [
        parse_time(e.get("at")) for e in events if event_type(e) == "checkpoint_completed"
    ]
    found = [t for t in found if t is not None]
    if found:
        latest = max(found)
        return f"checkpoint {_span((now - latest).total_seconds())} ago", latest.isoformat()
    if len(events) < _EVENT_PAGE:
        # The whole history, in either order: no checkpoint in it is none.
        return "checkpoint none", "-"
    if paged:
        # The newest page is full and holds none: there may be an older one,
        # past this page. Not "none", which would be a claim.
        return "checkpoint unknown", "?"
    # An API that cannot page served the OLDEST events; that says nothing.
    return "checkpoint unreadable", "?"


def progress_line(client: SwarmClient, task: dict[str, Any], now: datetime) -> tuple[str, str]:
    """`(line, key)` for one task. The key changes when the line means something new.

    The key is `<wake key>|<last checkpoint>`: elapsed time, checkpoint age and
    spend move on every call and are not, by themselves, news.
    """
    label = task_label(task.get("task_id"), task.get("step_id"))
    if task.get("read") != "ok":
        return f"[{label}] unreadable: {clip(task.get('error') or '', 120)}", "unread|"
    state = task.get("state")
    wake = _wake_key(task)
    if state in _PENDING:
        since = parse_time(task.get("updated_at")) or parse_time(task.get("created_at"))
        waited = f"waiting {_span((now - since).total_seconds())}" if since else "waiting"
        brief = waiting_brief(task)
        if brief is not None:
            # WHY IN THE API'S OWN WORDS (#1782), and a new line only when
            # those words change: they are the key's second half, where a
            # running task keeps its checkpoint. An older API serves none and
            # keeps the line and key below.
            words = waiting_words(brief, now)
            return (f"[{label}] {state} · {waited} · waits: {clip(words, 240)}",
                    f"{wake}|{waiting_key(task)}")
        return f"[{label}] {state} · {waited} · waits: {waits_for(task)}", f"{wake}|"
    started = parse_time(task.get("started_at"))
    ended = parse_time(task.get("completed_at")) if task.get("terminal") else None
    elapsed = _span(((ended or now) - started).total_seconds()) if started else "elapsed unrecorded"
    parts = [f"[{label}] {state}", elapsed]
    count, cap = task.get("attempt_count"), task.get("max_attempts")
    parts.append(f"attempt {count}/{cap}" if count is not None and cap is not None else "attempt unrecorded")
    stamp = ""
    if not task.get("terminal"):
        words, stamp = _last_checkpoint(client, str(task.get("task_id")), now)
        parts.append(words)
    tokens, cost = _spend(client, str(task.get("task_id")))
    parts += [tokens, cost]
    asked = questions_count(task) if task.get("terminal") else 0
    if asked:
        # WHAT THE AGENT ASKS THE OWNER (owner decision, 2026-10-05): on the
        # final line, so the row that writes one line per change says it.
        parts.append(questions_words(asked))
    merged = merge_row(task)
    if merged is not None:
        # A MERGE STEP'S VERDICT (lane MS5): merged and the issues closed, or
        # the refusal code -- what a workflow acts on, on the final line.
        parts.append(merged)
    return " · ".join(parts), f"{wake}|{stamp}"


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

def _read(client: SwarmClient, task_id: str) -> dict[str, Any]:
    try:
        task = client.task(task_id)
    except SwarmError as exc:
        return {"task_id": task_id, "read": "failed", "error": str(exc), "http_status": exc.status}
    state = task.get("state")
    return {
        **task,
        "task_id": task_id,
        "read": "ok",
        "terminal": state in TERMINAL,
    }


def _slim_outcome(full: dict[str, Any]) -> dict[str, Any]:
    """The step result's fields, from `progress.outcome` -- not the whole answer."""
    out = {
        "state": full.get("state"),
        "answer_excerpt": full.get("answer_excerpt"),
        "cost_usd": full.get("cost_usd"),
        # `cost_usd` is every attempt's; these say whether it is a floor and
        # what the last attempt alone cost (lane review P1).
        "cost_incomplete": full.get("cost_incomplete"),
        "attempts": full.get("attempts"),
        "last_attempt_cost_usd": full.get("last_attempt_cost_usd"),
        "duration_s": full.get("duration_s"),
        "pr_url": full.get("pr_url"),
        "artifacts": [a.get("name") for a in full.get("artifacts") or [] if isinstance(a, dict)],
        "last_error": full.get("last_error"),
        "end_cause": full.get("end_cause"),
        # The agent's questions for the owner, whole: they are the reason a
        # person reads this step's result (`progress.owner_questions`).
        "questions": list(full.get("questions") or []),
        # And the owner's answers to them, stored on the task in the console
        # (TASK-ANSWERS, `progress.owner_answers`).
        "owner_answers": list(full.get("owner_answers") or []),
    }
    if full.get("questions_unavailable_because"):
        out["questions_unavailable_because"] = clip(full["questions_unavailable_because"], 300)
    if full.get("answer_unavailable_because"):
        out["answer_unavailable_because"] = clip(full["answer_unavailable_because"], 200)
    if full.get("console"):
        # The API's link, so the row's answer can say where the whole of it is.
        out["console"] = full["console"]
    return out


def _text(value: Any) -> str:
    """A field as text for `step_result`: "" for none, a non-string as compact JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), default=str)


def step_result(row: dict[str, Any]) -> dict[str, Any]:
    """The step's answer, ready for an `sc:step` row to return as given.

    WHY (owner decision, 2026-10-05, lane review B2). Every one of the 14
    refused StructuredOutput calls that day was a bare null -- `"pr_url": ,`
    -- typed by the Haiku relay while retyping `outcome` field by field, and
    one review row lost a SUCCEEDED result that way. So this object holds NO
    null anywhere: a text with no value is "", a list with none is [], and a
    figure that was not recorded (`cost_usd`, `duration_s`) is LEFT OUT --
    never 0, which would say it cost nothing. `plugin/workflows/run.js` reads
    a missing figure back as null.

    A task given up on (`abandoned`) answers `UNKNOWN` with its reason as
    `last_error`, so every reply that stops carries a result to return.

    `last_error` is the relayed outcome's, else the one the row's own follow
    read of the task carried (`row["last_error"]`). Measured 2026-10-20, GKE
    admission outage: two step rows and a task row ended FAILED with an empty
    `last_error` while the task document held `gke_create_job_forbidden` and
    `swarm_workflow_status` served it, so the session reported a failure with
    no reason (#1334).
    """
    found = row.get("outcome") or {}
    result: dict[str, Any] = {"state": str(found.get("state") or row.get("state") or "UNKNOWN")}
    if row.get("abandoned"):
        result["state"] = "UNKNOWN"
    result["answer_excerpt"] = _text(found.get("answer_excerpt"))
    for figure in ("cost_usd", "duration_s"):
        value = found.get(figure)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[figure] = value
    result["pr_url"] = _text(found.get("pr_url"))
    result["artifacts"] = [str(name) for name in found.get("artifacts") or [] if name]
    if row.get("abandoned"):
        result["last_error"] = _text(row.get("abandoned_because"))
    else:
        result["last_error"] = _text(found.get("last_error") or row.get("last_error"))
    result["console"] = _text(found.get("console") or row.get("console"))
    # The agent's questions for the owner (2026-10-05), with "" for a text it
    # left out, so this object still holds no null. `[]` when it asked none.
    result["questions"] = [_no_null_question(q) for q in found.get("questions") or [] if isinstance(q, dict)]
    # And which of them the owner has answered (ANSWERS-429, part of #1904):
    # the follow payload carried `owner_answers` (TASK-ANSWERS) and this
    # result did not, so a step row could not tell an answered question from
    # an open one and the session asked it again. The index, choice and text
    # recorded on the task, never more; no key when nothing is answered, as
    # `run.js`'s `answered` has none.
    answers = [_no_null_answer(a) for a in found.get("owner_answers") or [] if _answer_index(a) is not None]
    if answers:
        result["owner_answers"] = answers
    return result


#: The park a merge step waits out on its pull request's CI
#: (`agent_worker.control.ControlPlane.park_ci_pending`).
_CI_PARK = "CI_PENDING"


def _parked_on_ci(task: dict[str, Any]) -> bool:
    """Whether a task read back PARKED on its pull request's CI."""
    return task.get("read") == "ok" and task.get("state") == "PARKED" and task.get("park_reason") == _CI_PARK


def ci_park_result(task_id: str, task: dict[str, Any], link: str | None) -> dict[str, Any]:
    """A step row's answer for a merge PARKED on CI: `step_result`'s shape, no null.

    WHY (ROW-SKIP, part of #1194; measured 2026-10-12). A merge step parked on
    `CI_PENDING` is woken and parked again for as long as the pull request's
    CI runs -- hours, on a slow pipeline -- so its `sc:step` row held call
    after call, wrote a line per wake, and ended at its turn budget with no
    StructuredOutput at all. The workflow was fine; the row was the defect.
    So a STEP row is told the park, with its reason, and stops: the task
    holds no capacity while it waits (invariant 1), it resumes on its own
    when CI settles, and `swarm follow` or `/sc attach` picks it up again.
    """
    wait = ((task.get("metadata") or {}).get("merge_wait")) or {}
    on: list[str] = []
    number = wait.get("pull_request")
    if isinstance(number, int) and not isinstance(number, bool):
        on.append(f"PR #{number}")
    pending = [str(name) for name in wait.get("pending") or [] if name]
    if pending:
        on.append("pending " + clip(", ".join(pending), 120))
    detail = f" ({'; '.join(on)})" if on else ""
    return {
        "state": "PARKED",
        "answer_excerpt": "",
        "pr_url": "",
        "artifacts": [],
        "last_error": (
            f"PARKED on {_CI_PARK}: waiting for the pull request's checks{detail}. It holds no "
            f"capacity and resumes on its own when they settle; nothing was cancelled. "
            f"Resume with: swarm follow {task_id}"
        ),
        "console": _text(link),
        "questions": [],
    }


def _no_null_question(question: dict[str, Any]) -> dict[str, Any]:
    """One of `progress.owner_questions`' questions, every missing text as ""."""
    return {
        "question": _text(question.get("question")),
        "options": [
            {"label": _text(option.get("label")), "description": _text(option.get("description"))}
            for option in question.get("options") or []
            if isinstance(option, dict)
        ],
        "recommended": _text(question.get("recommended")),
        "context": _text(question.get("context")),
    }


def _answer_index(answer: Any) -> int | None:
    """An owner's answer's `question_index`, or None when it names no question."""
    if not isinstance(answer, dict):
        return None
    index = answer.get("question_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    return index


def _no_null_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """One of `progress.owner_answers`' answers as `step_result` serves it: no null."""
    return {
        "question_index": _answer_index(answer),
        "choice": _text(answer.get("choice")),
        "text": _text(answer.get("text")),
    }


def _rate_limited(read: dict[str, Any]) -> bool:
    return read.get("read") == "failed" and read.get("http_status") == RATE_LIMITED_STATUS


def watch_progress(
    client: SwarmClient,
    task_ids: list[str],
    *,
    since: Any = None,
    wait_seconds: float = 0,
    step_id: str | None = None,
    parents: list[str] | None = None,
    max_wait: float = MAX_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """`{progress, changed, transitions, stop, since, tasks}` for these tasks.

    `progress` holds a line only for a task whose line changed since `since`;
    `changed` is whether it holds any. A finished task carries `outcome`, the
    step result's fields. `stop` is `progress.watch`'s: every task finished
    with its outcome, or given up on (`abandoned_because`).

    `parents` (one task only) holds the call -- the first one too -- while
    the task waits on them (`_waits_on_parents`) and any of them is
    unfinished; the reply's `parents` maps each to its state.

    `max_wait` is the longest this call may hold, whatever `wait_seconds`
    asks: `MAX_WAIT_SECONDS` for a call the bridge keeps alive, else
    `SILENT_MAX_WAIT_SECONDS`, inside the host's idle timeout.
    """
    if parents and len(task_ids) != 1:
        raise SwarmError(
            f"`parents` names the parents of ONE task, and {len(task_ids)} task ids were given"
        )
    if step_id is not None and len(task_ids) != 1:
        raise SwarmError(
            f"`step_id` names the step of ONE task, and {len(task_ids)} task ids were "
            "given; follow one task with it, or pass no `step_id`"
        )
    states, keys, failures = decode_since(since)
    said_llm = llm_said(since)
    first_call = since in (None, "")
    wait = max(0.0, min(float(wait_seconds or 0), max_wait, MAX_WAIT_SECONDS))
    deadline = clock() + wait
    interval = POLL_SECONDS
    latest: dict[str, dict[str, Any]] = {}
    read_ok: set[str] = set()
    wake_before = {t: (keys.get(t) or "").rsplit("|", 1)[0] for t in task_ids}
    parent_states: dict[str, Any] = {}
    backoff = RATE_LIMIT_BACKOFF_SECONDS

    while True:
        for task_id in task_ids:
            task = _read(client, task_id)
            latest[task_id] = task
            if task["read"] == "ok":
                read_ok.add(task_id)
        held_by_parents = False
        limited = any(_rate_limited(latest[t]) for t in task_ids)
        if parents:
            parent_states = {}
            unfinished = False
            for parent in parents:
                seen = _read(client, parent)
                limited = limited or (_rate_limited(seen) and _waits_on_parents(latest[task_ids[0]]))
                if seen["read"] == "ok":
                    parent_states[parent] = seen.get("state")
                    unfinished = unfinished or not seen["terminal"]
                else:
                    parent_states[parent] = None
                    # A parent this identity can never read cannot be waited
                    # for; any other failure is waited through, like the task's.
                    unfinished = unfinished or seen.get("http_status") not in PERMANENT_READ_STATUSES
            held_by_parents = unfinished and _waits_on_parents(latest[task_ids[0]])
        wrong = {
            t for t in task_ids
            if step_id is not None and latest[t]["read"] == "ok" and latest[t].get("step_id") != step_id
        }
        settled = all(
            (latest[t]["read"] == "ok" and latest[t]["terminal"]) or t in wrong
            or (latest[t]["read"] == "failed" and latest[t].get("http_status") in PERMANENT_READ_STATUSES)
            for t in task_ids
        )
        moved = any(
            latest[t]["read"] == "ok" and _wake_key(latest[t]) != wake_before[t] for t in task_ids
        )
        # A step row's merge parked on its pull request's CI ends the hold:
        # the row answers PARKED rather than waiting the CI out (`ci_park_result`).
        ci_parked = step_id is not None and _parked_on_ci(latest[task_ids[0]])
        if limited:
            # Rate limited: wait and read again, the first call too, rather
            # than hand the row a failed read (RATE_LIMITED_STATUS).
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(backoff, remaining))
            backoff = min(backoff * 2, RATE_LIMIT_BACKOFF_CAP_SECONDS)
            continue
        backoff = RATE_LIMIT_BACKOFF_SECONDS
        if parents:
            if settled or ci_parked or not held_by_parents:
                break
        elif first_call or settled or moved or ci_parked:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
        if all(latest[t].get("state") in _PENDING for t in task_ids):
            interval = min(interval * 2, PENDING_POLL_CAP_SECONDS)
        else:
            interval = POLL_SECONDS

    stamp = now()
    progress: list[str] = []
    transitions: list[str] = []
    rows: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = latest[task_id]
        label = task_label(task_id, task.get("step_id"))
        row: dict[str, Any] = {
            "task_id": task_id,
            "step_id": task.get("step_id"),
            "state": task.get("state"),
            "terminal": bool(task.get("terminal")),
            "read": task["read"],
        }
        link = console_link(task) if task["read"] == "ok" else None
        if link is not None:
            row["console"] = link
        if task["read"] == "ok" and task.get("state") == "PARKED" and task.get("park_reason"):
            row["park_reason"] = task["park_reason"]
        if task["read"] == "ok" and task.get("last_error"):
            # The follow read's own reason, for `step_result` when the
            # finished task's re-read (`outcome`) comes back without one.
            row["last_error"] = task["last_error"]
        abandoned = None
        if step_id is not None and task["read"] == "ok" and task.get("step_id") is None:
            # A single task handed to a step row (#830).
            abandoned = (
                f"task {task_id} is a single task, not workflow step {step_id!r}: it "
                "belongs to no workflow. Follow it WITHOUT `step_id` -- the sc:task row "
                "does. Nothing was cancelled"
            )
        elif step_id is not None and task["read"] == "ok" and task.get("step_id") != step_id:
            abandoned = (
                f"task {task_id} is workflow step {task.get('step_id')!r}, not {step_id!r}: this "
                "row was handed another step's task id. Nothing was cancelled"
            )
        elif task_id in read_ok:
            failures.pop(task_id, None)
        elif _rate_limited(task):
            # Not a failure: the streak stays where it was, and the row asks
            # again (RATE_LIMITED_STATUS).
            row["read"] = "rate_limited"
            row["read_error"] = clip(task.get("error") or "", 200)
        else:
            failures[task_id] = failures.get(task_id, 0) + 1
            row["read_error"] = clip(task.get("error") or "", 200)
            status = task.get("http_status")
            if status in PERMANENT_READ_STATUSES:
                abandoned = (
                    f"task {task_id} cannot be read: HTTP {status} -- {clip(task.get('error') or '', 160)}. "
                    "Asking again will not change it. Nothing was cancelled"
                )
            elif failures[task_id] >= READ_FAILURE_LIMIT:
                abandoned = (
                    f"task {task_id} could not be read on {failures[task_id]} calls in a row; whether "
                    "it still runs is UNKNOWN. Nothing was cancelled"
                )

        before = states.get(task_id)
        if task["read"] == "ok":
            now_state = task.get("state")
            if before is not None and now_state != before:
                transitions.append(f"[{label}] {before} → {now_state}")
            states[task_id] = now_state

        if abandoned:
            row["abandoned"] = True
            row["abandoned_because"] = abandoned
            progress.append(f"[{label}] stopped following: {clip(abandoned, 160)}")
        else:
            line, key = progress_line(client, task, stamp)
            if link is not None:
                # WHERE TO WATCH IT, on EVERY line the row writes: the link the
                # API served, as served (owner decisions 2026-10-01 and
                # 2026-10-04). Once only on the first line and the last, which
                # left a row parked or running for an hour showing no link where
                # the person looked. Not part of `key`, so a link never makes
                # an unchanged line news: a row still writes only on a change.
                line = f"{line} · console: {link}"
            # WHICH LLM, ONCE (SC-DESC-DETAILS): a run started before its
            # account was assigned said `LLM: not assigned yet` in its
            # details, so the first line after the assignment says which
            # account and model -- on that line, even when nothing else on
            # it is news, and never again for this task.
            held = details.llm_line(task) if task["read"] == "ok" else None
            fresh_llm = bool(held) and task_id not in said_llm
            if fresh_llm:
                line = f"{line} · LLM: {held}"
                said_llm.add(task_id)
            if key != keys.get(task_id) or first_call or fresh_llm:
                progress.append(line)
            keys[task_id] = key
            if task["read"] == "ok" and task.get("terminal"):
                try:
                    row["outcome"] = _slim_outcome(outcome(client, client.task(task_id)))
                except SwarmError as exc:
                    row["outcome"] = None
                    row["outcome_unavailable_because"] = clip(f"the finished task could not be re-read: {exc}", 200)
        if step_id is not None and not abandoned and _parked_on_ci(task):
            row["parked_on_ci"] = True
        rows.append(row)

    stop = bool(rows) and all(
        r.get("abandoned") or r.get("parked_on_ci") or (r["terminal"] and r.get("outcome")) for r in rows
    )
    reply = {
        "progress": progress,
        "changed": bool(progress),
        "transitions": transitions,
        "stop": stop,
        "since": encode_since(states, keys, failures, said_llm),
        "tasks": rows,
    }
    if parents:
        reply["parents"] = parent_states
    if stop and len(rows) == 1:
        # The step's answer, ready-made (`step_result`): the row returns it as
        # given rather than retyping `outcome` field by field.
        if rows[0].get("parked_on_ci"):
            reply["result"] = ci_park_result(rows[0]["task_id"], latest[rows[0]["task_id"]], rows[0].get("console"))
        else:
            reply["result"] = step_result(rows[0])
    return reply


# --------------------------------------------------------------------------
# `format: "summary"`: the sc:summary row's count line (PLUGIN-ROW-SPLIT-2)
# --------------------------------------------------------------------------

#: The buckets a summary line sorts a state into -- run.js's SUMMARY_RUNNING,
#: SUMMARY_DONE and SUMMARY_FAILED, and a unit test holds the three equal
#: (test_plugin_row_split_2.py). Capacity-holding states run (invariant 3:
#: concurrency counts from LEASED), a success is done, the other terminal
#: states failed, and every other state -- waiting, parked, ready, not read
#: yet -- is queued. The row used to count these itself, from a list in
#: agents/summary.md; a haiku row counting eighteen states is a line that can
#: be wrong, so the bridge counts and the row copies (owner, 2026-10-21).
SUMMARY_RUNNING = tuple(sorted(_HOLDING))
SUMMARY_DONE = ("SUCCEEDED",)
SUMMARY_FAILED = ("FAILED", "CANCELLED", "DEAD_LETTERED")
SUMMARY_BUCKETS = ("running", "queued", "done", "failed")
SUMMARY_NOUNS = ("step", "task")


def summary_bucket(state: Any) -> str:
    """run.js's `summaryBucket`: which count a state adds to."""
    if state in SUMMARY_RUNNING:
        return "running"
    if state in SUMMARY_DONE:
        return "done"
    if state in SUMMARY_FAILED:
        return "failed"
    return "queued"


def pull_request(url: Any) -> str | None:
    """run.js's `pullRequest`: `PR #<n>` for a `/pull/<n>` link, else `PR <url>`."""
    if not isinstance(url, str) or not url:
        return None
    at = url.rfind("/pull/")
    return "PR #" + url[at + 6:].split("/")[0] if at >= 0 else "PR " + url


def summary_line(noun: str, total: int, counts: dict[str, int], prs: list[str], console: Any) -> str:
    """run.js's `summaryLine`, from counts: `<N> steps: <r> running · <q>
    queued · <d> done · <f> failed`, then ` · PR #12, #14` and ` · console:
    <link>` when there are any."""
    head = f"{total} {noun}{'' if total == 1 else 's'}: " + " · ".join(
        f"{int(counts.get(bucket) or 0)} {bucket}" for bucket in SUMMARY_BUCKETS)
    if prs:
        head += " · " + ", ".join(pr[3:] if index and pr.startswith("PR ") else pr for index, pr in enumerate(prs))
    link = console.strip() if isinstance(console, str) else ""
    return head + (f" · console: {link}" if link else "")


def _count(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SwarmError(f"`summary_of.{where}` must be a whole number, 0 or more; it was {value!r}")
    return value


def read_summary_of(given: Any) -> dict[str, Any]:
    """`summary_of`, checked: what run.js's prompt hands the summary row and
    the row passes back unchanged -- `noun`, `total`, the `counted` buckets of
    the items it does NOT follow, their `prs`, the workflow's `console` and
    the `line` the workflow last wrote."""
    if not isinstance(given, dict):
        raise SwarmError(
            "`format: \"summary\"` needs `summary_of`: the object the row's prompt gives on its "
            "`summary_of:` line, passed unchanged. Without it the line would count only the "
            "tasks followed, which is not the workflow"
        )
    noun = given.get("noun")
    if noun not in SUMMARY_NOUNS:
        raise SwarmError(f"`summary_of.noun` is one of {list(SUMMARY_NOUNS)}; it was {noun!r}")
    counted = given.get("counted") if given.get("counted") is not None else {}
    if not isinstance(counted, dict):
        raise SwarmError("`summary_of.counted` maps running, queued, done and failed to counts")
    prs = given.get("prs") if given.get("prs") is not None else []
    if not isinstance(prs, list) or not all(isinstance(pr, str) for pr in prs):
        raise SwarmError("`summary_of.prs` is a list of `PR #<n>` strings")
    line = given.get("line")
    console = given.get("console")
    return {
        "noun": noun,
        "total": _count(given.get("total"), "total"),
        "counted": {bucket: _count(counted.get(bucket, 0), f"counted.{bucket}") for bucket in SUMMARY_BUCKETS},
        "prs": list(prs),
        "console": console if isinstance(console, str) else None,
        "line": line if isinstance(line, str) else "",
    }


def _summary_said(token: Any) -> tuple[dict[str, str], str | None]:
    """`(prs, line)` a summary token carries: each finished task's pull
    request, and the line the last reply returned; empty before the first."""
    payload = _payload(token)
    if payload is None or payload.get("fmt") != _FORMAT or not isinstance(payload.get("sm"), dict):
        return {}, None
    said = payload["sm"]
    prs = said.get("p") if isinstance(said.get("p"), dict) else {}
    line = said.get("ln") if isinstance(said.get("ln"), str) else None
    return {str(k): str(v) for k, v in prs.items()}, line


def watch_summary(
    client: SwarmClient,
    task_ids: list[str],
    summary_of: Any,
    *,
    since: Any = None,
    wait_seconds: float = 0,
    max_wait: float = MAX_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """`{line, line_changed, transitions, stop, since, tasks}`: the summary
    row's follow (PLUGIN-ROW-SPLIT-2, owner 2026-10-21).

    `task_ids` is EVERY task the row was handed, on every call: a task the
    token already saw finish is not read again and has no row in `tasks`
    (its row came back on the call it finished), and the rest are followed
    exactly as `watch_progress` follows them -- the same hold, the same
    giving up -- without the per-task `progress` lines, which the row does
    not write. `line` counts the `summary_of` items and every task of
    `task_ids` by its last state read, in run.js's buckets, with the
    `summary_of` pull requests then each finished task's; `line_changed`
    says whether it differs from the line the last reply returned -- the
    prompt's `summary_of.line` before the first.
    """
    given = read_summary_of(summary_of)
    task_ids = [str(t) for t in task_ids]
    states, _keys, _failures = decode_since(since)
    prs, last_line = _summary_said(since)
    open_ids = [t for t in task_ids if states.get(t) not in TERMINAL]
    if open_ids:
        reply = watch_progress(client, open_ids, since=since, wait_seconds=wait_seconds, max_wait=max_wait,
                               sleep=sleep, clock=clock, now=now)
    else:
        reply = {"progress": [], "transitions": [], "stop": True,
                 "since": since if since not in (None, "") else encode_since({}, {}, {}), "tasks": []}
    states, keys, failures = decode_since(reply["since"])
    for row in reply["tasks"]:
        made = pull_request((row.get("outcome") or {}).get("pr_url")) if row.get("terminal") else None
        if made:
            prs[row["task_id"]] = made
    counts = dict(given["counted"])
    for task_id in task_ids:
        counts[summary_bucket(states.get(task_id))] += 1
    shown = list(given["prs"])
    for task_id in task_ids:
        if prs.get(task_id) and prs[task_id] not in shown:
            shown.append(prs[task_id])
    line = summary_line(given["noun"], given["total"], counts, shown, given["console"])
    before = last_line if last_line is not None else given["line"]
    token = encode_since(states, keys, failures, llm_said(reply["since"]))
    # The summary's own memory rides in the same checked envelope: re-sealed
    # with it, so a relay that changes one character is still refused.
    payload = _payload(token) or {}
    payload["sm"] = {"p": {t: prs[t] for t in task_ids if t in prs}, "ln": line}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return {
        "line": line,
        "line_changed": line != before,
        "transitions": reply["transitions"],
        "stop": bool(reply["stop"]),
        "since": f"{body}.{_checksum(body)}",
        "tasks": reply["tasks"],
    }
