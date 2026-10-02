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

from .client import TERMINAL, SwarmClient, SwarmError
from .follow import console_link, event_type
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
)
from .render import describe_blocker, parse_time, task_label

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
MAX_WAIT_SECONDS = 1800

#: How often a running task is re-read inside a window. One small read.
POLL_SECONDS = 10.0

#: The `since` token's format marker. A `lines` token carries none, and is
#: read here as no token: those positions are byte offsets this format does
#: not use.
_FORMAT = "progress"

#: How many of the newest events are read for the last checkpoint.
_EVENT_PAGE = 100

#: What a pending task waits for, per `park_reason`, in a word or two.
_WAITS_FOR = {
    "DEPENDENCY_INCOMPLETE": "dependency",
    "PROVIDER_QUOTA_EXHAUSTED": "quota",
    "PROVIDER_COOLDOWN": "quota cooldown",
    "PROVIDER_OUTAGE": "provider outage",
    "SCHEDULED_RETRY": "scheduled retry",
    "MANUAL_PAUSE": "operator pause",
    "BUDGET_EXHAUSTED": "budget",
    "CREDENTIAL_MISSING": "credential",
}


# --------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------

def encode_since(states: dict[str, Any], keys: dict[str, str], failures: dict[str, int]) -> str:
    """States last reported, each task's last line key, and the read-failure streaks.

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

#: What a dependent step waits in before it can start.
_DEPENDENCY_WAIT = "DEPENDENCY_INCOMPLETE"


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
        "duration_s": full.get("duration_s"),
        "pr_url": full.get("pr_url"),
        "artifacts": [a.get("name") for a in full.get("artifacts") or [] if isinstance(a, dict)],
        "last_error": full.get("last_error"),
        "end_cause": full.get("end_cause"),
    }
    if full.get("answer_unavailable_because"):
        out["answer_unavailable_because"] = clip(full["answer_unavailable_because"], 200)
    if full.get("console"):
        # The API's link, so the row's answer can say where the whole of it is.
        out["console"] = full["console"]
    return out


def watch_progress(
    client: SwarmClient,
    task_ids: list[str],
    *,
    since: Any = None,
    wait_seconds: float = 0,
    step_id: str | None = None,
    parents: list[str] | None = None,
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
    first_call = since in (None, "")
    wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
    deadline = clock() + wait
    interval = POLL_SECONDS
    latest: dict[str, dict[str, Any]] = {}
    read_ok: set[str] = set()
    wake_before = {t: (keys.get(t) or "").rsplit("|", 1)[0] for t in task_ids}
    parent_states: dict[str, Any] = {}

    while True:
        for task_id in task_ids:
            task = _read(client, task_id)
            latest[task_id] = task
            if task["read"] == "ok":
                read_ok.add(task_id)
        held_by_parents = False
        if parents:
            parent_states = {}
            unfinished = False
            for parent in parents:
                seen = _read(client, parent)
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
        if parents:
            if settled or not held_by_parents:
                break
        elif first_call or settled or moved:
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
        abandoned = None
        if step_id is not None and task["read"] == "ok" and task.get("step_id") != step_id:
            abandoned = (
                f"task {task_id} is workflow step {task.get('step_id')!r}, not {step_id!r}: this "
                "row was handed another step's task id. Nothing was cancelled"
            )
        elif task_id in read_ok:
            failures.pop(task_id, None)
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
            if link is not None and (first_call or task.get("terminal")):
                # WHERE TO WATCH IT, on the row's FIRST line and its LAST: the
                # link the API served, as served (owner decision 2026-10-01).
                # Not on every line between -- each written line is re-read on
                # every later turn of the row -- and not part of `key`, so a
                # link never makes an unchanged line news.
                line = f"{line} · console: {link}"
            if key != keys.get(task_id) or first_call:
                progress.append(line)
            keys[task_id] = key
            if task["read"] == "ok" and task.get("terminal"):
                try:
                    row["outcome"] = _slim_outcome(outcome(client, client.task(task_id)))
                except SwarmError as exc:
                    row["outcome"] = None
                    row["outcome_unavailable_because"] = clip(f"the finished task could not be re-read: {exc}", 200)
        rows.append(row)

    stop = bool(rows) and all(r.get("abandoned") or (r["terminal"] and r.get("outcome")) for r in rows)
    reply = {
        "progress": progress,
        "changed": bool(progress),
        "transitions": transitions,
        "stop": stop,
        "since": encode_since(states, keys, failures),
        "tasks": rows,
    }
    if parents:
        reply["parents"] = parent_states
    return reply
