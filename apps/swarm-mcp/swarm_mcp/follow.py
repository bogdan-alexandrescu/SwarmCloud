"""What a remote agent has produced SINCE a cursor.

WHY A CURSOR AND NOT A STREAM. `server.py` explains that an MCP tool call
returns exactly once, so a tool cannot print a log line while an agent is still
writing it. `client.py:25` adds the other half: a long-lived `tail` inside a
subprocess dies, and the 401 it dies with looks like a permission problem. Both
rule out a held connection. What is left, and what this module is, is a
RESUMABLE CURSOR: each call answers "what is new since this cursor" and hands
back the next one, so a session can poll between other work and narrate
progress without holding anything open. It is not streaming and does not
pretend to be; it is the shape that survives a process that may be killed
between any two calls.

THE FOUR PROPERTIES THIS OWES ITS CALLER, all of which are load-bearing:

1. INCREMENTAL. Nothing is ever handed back twice. The cursor is a byte
   position in each STREAM (not in the object holding it) plus a count of
   events already delivered, so two consecutive calls partition the output
   rather than overlapping it. Where an exact slice is impossible -- see
   `_deliver_from_window` on redaction -- the code repeats a bounded, NAMED
   number of bytes rather than dropping any, because a silent drop is
   indistinguishable from an agent that went quiet.

2. BOUNDED, AND SAYING SO. A run that produced 50MB cannot be allowed to
   arrive in one answer. Every call carries a log-byte budget; when it is
   spent, the streams and tasks that were not read say `skipped_budget` and
   `truncation` names each one and the cursor that resumes it. A silent
   truncation reads as "that was all the output", which is the exact class of
   defect this repository keeps finding, so truncation is never a field a
   reader has to go looking for: it is a list of sentences.

3. HONEST ABOUT ABSENCE. "No events yet", "this task does not exist" and "the
   read failed" are three different facts and are reported as three different
   statuses. Collapsing any of them into an empty list is how a broken read
   gets reported as a quiet agent.

4. FAN-OUT. Several tasks in one call, because the case this exists for is
   five or six agents running together and a session that polls each one
   separately spends its whole turn on bookkeeping.

WHAT THE TWO ROUTES UNDERNEATH ACTUALLY OFFER, because the design is shaped by
their limits rather than by what would have been convenient:

  * `GET /v1/tasks/{id}/events` orders ASCENDING by `at` and applies `limit` as
    a HEAD -- the first N, not the last N -- and offers no page token. So the
    cursor into it is a COUNT of rows already consumed, and the route's own
    `max_page_size` is a ceiling past which later events are not reachable at
    all. `_events` reports when a page came back exactly full, because that is
    the only signal a client gets that the route may be holding more.
  * `GET /v1/tasks/{id}/logs` pages by BYTE OFFSET INTO AN OBJECT and reports
    `total_bytes`, `next_offset` and `truncated` in the RAW object's
    coordinates. There are two objects per stream: `logs/<stream>.log`, the
    complete record written once at exit, and `logs/live/<stream>.tail.log`, a
    bounded 256KB window republished every few seconds while the agent runs.
    They do not share a coordinate system -- the live object is
    `#swarm-tail offset=W size=S\\n` followed by stream bytes `[W, S)` -- so the
    cursor is kept in STREAM coordinates and translated on the way in. That
    translation is what makes the live-to-final handover free: the completed
    record IS the stream from byte zero, so the same cursor addresses it.
"""

from __future__ import annotations

import json
from typing import Any

from .client import TERMINAL, SwarmClient, SwarmError

#: Both streams, in the order they are read and reported. Matches
#: `swarm_api.inspect.STREAMS`; asking for both in one request is not possible
#: because the route takes ONE offset for every stream it returns and the two
#: streams are at different positions.
STREAMS = ("stdout", "stderr")

#: How many bytes of log text one call may hand back, across every task and
#: stream in it. Roughly 5k tokens -- enough to narrate real progress, small
#: enough that six agents polled every few seconds do not fill a session.
DEFAULT_LOG_BUDGET = 20_000

#: How many NEW events one call reports per task. Events are small and are the
#: skeleton of the narration, so this is generous; anything past it is withheld
#: and SAID, never dropped.
DEFAULT_EVENT_PAGE = 50

#: An event `detail` is a small dict in every writer in this repository
#: (`error_code`, `correlation_id`, a park reason). It is passed through, but a
#: blob that turned out not to be small would blow the budget the log half is
#: careful about, so it is cut here -- and the cut is reported in the event.
MAX_EVENT_DETAIL_CHARS = 1_000

#: The live tail object is at most `live_log_tail_bytes` (256KB by default).
#: A read of it is sized from the cursor rather than fixed, but never above
#: this, so a stream that somehow published more cannot turn one poll into a
#: multi-megabyte download.
MAX_LIVE_READ = 320 * 1024

#: HOW THE TAILER IS SPELLED, in one place, because three places had spelled it
#: `swarm tail` and `swarm` IS NOT ON ANYONE'S PATH. The console script is
#: installed into the uv-managed environment, never onto the shell's, so a fresh
#: checkout answers `command not found: swarm`. That string was handed to a
#: model as `follow_live_with` -- a field whose whole job is to be runnable --
#: and the model runs what it is given, gets a shell error at the moment it is
#: trying to report progress, and concludes the platform is broken.
#:
#: `test_the_tool_is_registered_and_its_schema_names_the_cursor` already asserts
#: no tool DESCRIPTION says `swarm tail`. It stopped one field short of the
#: place the model actually reads.
#:
#: `uv run` is the spelling that works in a fresh checkout with nothing
#: installed and also works when the environment IS active, which is why it wins
#: over teaching people to activate a venv first.
#: The prefix, on its own, because `swarm tail` was never the only place this
#: package hands a reader something to type. Three more were found on
#: 2026-09-24, all in PRINTED OUTPUT rather than in a tool response, and the
#: worst of them fires at precisely the wrong moment:
#:
#:   sc.py         "sc: run `swarm doctor` to see which auth tier this machine
#:                 is on" -- printed ONLY when `sc` could not connect. A session
#:                 that has just failed to read the cluster is told to run a
#:                 binary that does not exist, so it collects a second,
#:                 unrelated failure and reports SwarmCloud as broken twice.
#:   cli.py        "deploy it first, then run `swarm init` again"
#:   cli.py        "Try:  swarm dispatch \"say hello\" --profile mock" -- the
#:                 last line of `swarm init`, which is the first command a new
#:                 operator ever runs and therefore the first thing they copy.
#:
#: `plugin/skills/sc/SKILL.md` says `uv run swarm doctor`, correctly. The
#: program contradicted the skill, and the program is what the reader sees.
RUN_PREFIX = "uv run "

TAIL_COMMAND = f"{RUN_PREFIX}swarm tail"


def terminal_command(words: str) -> str:
    """A `swarm`/`sc` command spelled so it runs in a fresh checkout.

    ONE FUNCTION so there is one spelling. `swarm` and `sc` are console scripts
    of this package, installed into the uv-managed environment and never onto
    the shell's PATH, so every command this package hands back or prints has to
    carry `uv run`. Anything already carrying it is returned unchanged, so
    passing `TAIL_COMMAND` through is not a double prefix.
    """
    words = words.strip()
    if not words or words.startswith(RUN_PREFIX):
        return words
    return f"{RUN_PREFIX}{words}"


def follow_command(task_ids: list[str]) -> str:
    """The terminal command that tails these tasks live, or "" for none.

    EMPTY RATHER THAN A BARE COMMAND when there is nothing to follow. `swarm
    tail ` with the ids missing is worse than absent, because it still looks
    runnable; `swarm_dispatch` already refuses to emit that shape and this is
    where the refusal belongs so every caller inherits it.
    """
    followable = [str(t) for t in task_ids if t]
    if not followable:
        return ""
    return f"{TAIL_COMMAND} {' '.join(followable)}"


def new_cursor() -> dict[str, Any]:
    """The cursor for a task nothing has been read from yet.

    Written out in full rather than defaulted field by field, so that a caller
    handing back a cursor from an older version of this tool gets the missing
    pieces filled in by `_normalise` instead of a KeyError mid-poll.
    """
    return {
        "events": 0,
        "attempt_id": None,
        "streams": {
            name: {"pos": 0, "source": None, "complete": False, "window_start": 0}
            for name in STREAMS
        },
    }


def _normalise(raw: Any) -> dict[str, Any]:
    """A cursor that came back from a model, made safe to use.

    The cursor crosses a tool boundary, which means it arrives as whatever the
    caller sent: a string where an int belongs, a missing stream, a negative
    offset. None of those may raise in the middle of a poll, and none of them
    may silently read as zero either -- a position that quietly became zero
    would re-deliver a whole run's output as if it were new. So each field is
    coerced, and anything uncoercible falls back to the fresh value, which
    re-reads rather than skips.
    """
    cur = new_cursor()
    if not isinstance(raw, dict):
        return cur
    cur["events"] = _non_negative_int(raw.get("events"))
    attempt = raw.get("attempt_id")
    cur["attempt_id"] = attempt if isinstance(attempt, str) and attempt else None
    streams = raw.get("streams")
    if isinstance(streams, dict):
        for name in STREAMS:
            got = streams.get(name)
            if not isinstance(got, dict):
                continue
            cur["streams"][name] = {
                "pos": _non_negative_int(got.get("pos")),
                "source": got.get("source") if got.get("source") in ("live", "final") else None,
                "complete": bool(got.get("complete")),
                "window_start": _non_negative_int(got.get("window_start")),
            }
    return cur


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


class _Budget:
    """The call's log-byte allowance, and the record of what it cost.

    A class rather than a running integer because the REPORTING is the point.
    Every place that declines to read something, or reads only part of it,
    calls `withheld` -- and `truncation` is a list of sentences naming what was
    left behind and how to get it. A boolean nobody reads would be worth
    nothing: the failure mode being designed against is a reader concluding
    "that was all the output", and only a sentence prevents that.

    IT IS A FLOOR ON WHAT IS READ, NOT A CEILING ON ONE WINDOW. The API clamps
    `limit_bytes` UP to its own `min_log_bytes` (4KB by default), because a
    window too small to hold a line can only ever be withheld -- so the last
    stream read in a call can overshoot the remaining allowance by one of
    those. Nothing is cut mid-window to hide that: a window arrives whole or is
    not started, since a partial one cannot be turned back into a byte position
    once read-time redaction has shortened it. `spent` is therefore what really
    came back, and the answer reports it as `log_bytes_returned`.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.spent = 0
        self.truncation: list[str] = []

    def room(self) -> int:
        return max(0, self.limit - self.spent)

    def charge(self, text: str) -> int:
        size = len(text.encode("utf-8"))
        self.spent += size
        return size

    def withheld(self, message: str) -> None:
        self.truncation.append(message)

    def mark(self) -> tuple[int, int]:
        """A point to come back to, for a read whose answer gets discarded.

        There is exactly one of those: the read that discovers, from its own
        response, that the attempt underneath it changed. Its bytes are thrown
        away, so charging them would make `log_bytes_returned` overstate what
        the caller got -- and any sentence it added to `truncation` would name
        output that was never withheld, which is the same lie in the other
        direction.
        """
        return self.spent, len(self.truncation)

    def rollback(self, mark: tuple[int, int]) -> None:
        self.spent, cut = mark
        del self.truncation[cut:]

    @property
    def truncated(self) -> bool:
        return bool(self.truncation)


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------

def follow(
    client: SwarmClient,
    task_ids: list[str],
    *,
    cursor: dict[str, Any] | None = None,
    max_log_bytes: int = DEFAULT_LOG_BUDGET,
    max_new_events: int = DEFAULT_EVENT_PAGE,
    include_heartbeats: bool = False,
) -> dict[str, Any]:
    """Everything these tasks have produced since `cursor`, and the next cursor.

    `cursor` is `{task_id: {...}}` exactly as a previous call returned it. An
    absent or unknown task id starts from the beginning of that task, which is
    what a caller following a task it has just dispatched wants.

    The return is JSON-serialisable throughout, because it crosses a tool
    boundary and the caller has to hand `cursor` back verbatim.
    """
    incoming = cursor if isinstance(cursor, dict) else {}
    budget = _Budget(max_log_bytes)
    page = max(1, int(max_new_events))

    reports: list[dict[str, Any]] = []
    next_cursor: dict[str, Any] = {}
    running: list[str] = []

    for task_id in task_ids:
        cur = _normalise(incoming.get(task_id))
        report, cur = _follow_one(
            client,
            task_id,
            cur,
            budget=budget,
            page=page,
            include_heartbeats=include_heartbeats,
        )
        reports.append(report)
        next_cursor[task_id] = cur
        if not report["terminal"]:
            running.append(task_id)

    return {
        "tasks": reports,
        "cursor": next_cursor,
        "still_running": running,
        "all_finished": not running,
        "log_bytes_returned": budget.spent,
        "log_budget": budget.limit,
        "truncated": budget.truncated,
        "truncation": budget.truncation,
        "note": (
            "Call again with `cursor` to get only what is new; nothing above is "
            "returned twice. `truncated` being true means this answer is NOT the "
            "whole output -- read `truncation`, which names what was withheld."
        ),
    }


def _follow_one(
    client: SwarmClient,
    task_id: str,
    cur: dict[str, Any],
    *,
    budget: _Budget,
    page: int,
    include_heartbeats: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One task. The state read comes first because everything else depends on it.

    A task that cannot be READ is not a task with no output. `client.task`
    raises for a 404 (no such task, or someone else's), for an edge refusal and
    for a transport failure, and all three are reported as `read: failed` with
    the message and, where there was one, the HTTP status -- never as an empty
    events list, which is what a caller would otherwise see and believe.
    """
    try:
        task = client.task(task_id)
    except SwarmError as exc:
        return (
            {
                "task_id": task_id,
                "read": "failed",
                "error": str(exc),
                "http_status": exc.status,
                "state": None,
                # NOT true: a task whose state could not be read has not been
                # shown to have finished, and a caller that stopped polling on
                # it would stop on a task that is still running.
                "terminal": False,
                "events": {"status": "unknown", "detail": "the task itself could not be read"},
                "logs": {"status": "unknown", "detail": "the task itself could not be read"},
            },
            cur,
        )

    state = task.get("state")
    terminal = state in TERMINAL

    events, cur = _events(client, task_id, cur, page=page, include_heartbeats=include_heartbeats)
    logs, cur = _logs(client, task_id, cur, terminal=terminal, budget=budget)

    return (
        {
            "task_id": task_id,
            "read": "ok",
            "state": state,
            "terminal": terminal,
            "runner_profile": task.get("runner_profile"),
            "park_reason": task.get("park_reason"),
            "last_error": task.get("last_error"),
            "events": events,
            "logs": logs,
        },
        cur,
    )


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

def _events(
    client: SwarmClient,
    task_id: str,
    cur: dict[str, Any],
    *,
    page: int,
    include_heartbeats: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """New events, and only new ones.

    THE CURSOR IS A COUNT, NOT A TIMESTAMP, and that is forced by the route.
    `store.list_events` orders ascending by `at` and takes `limit` as a head,
    so row N of the answer is row N of the task's history for every limit that
    reaches it. A count therefore indexes it exactly, and two events written in
    the same microsecond -- which a timestamp cursor would either duplicate or
    drop -- are just two rows.

    WHAT THE COUNT CANNOT DO is reach past the route's `max_page_size`. There
    is no page token, so once a task has produced more events than the route
    will return, the later ones are not reachable through this API at all. The
    only signal a client gets is a page that came back EXACTLY full, so that is
    reported as `page_was_full` with the sentence a reader needs, rather than
    being left to look like a task that went quiet.
    """
    delivered = cur["events"]
    want = delivered + page
    try:
        rows = client.events(task_id, limit=want)
    except SwarmError as exc:
        # A failed read is never an empty list. `cli._latest_attempt` states
        # this rule for attempts; it is the same rule here.
        return (
            {
                "status": "unreadable",
                "detail": str(exc),
                "new": [],
                "delivered_total": delivered,
                "page_was_full": False,
            },
            cur,
        )

    fresh = rows[delivered:]
    cur = dict(cur)
    cur["events"] = delivered + len(fresh)

    shown = [_event_row(e) for e in fresh]
    if not include_heartbeats:
        shown = [row for row in shown if row["type"] != "heartbeat"]

    if not rows:
        status = "none_yet"
        detail = (
            "this task has no events yet. It exists and was read successfully -- "
            "this is an empty history, not a failed read."
        )
    elif not fresh:
        status = "up_to_date"
        detail = None
    else:
        status = "ok"
        detail = None

    out: dict[str, Any] = {
        "status": status,
        "detail": detail,
        "new": shown,
        "delivered_total": cur["events"],
        "page_was_full": len(rows) == want,
    }
    if out["page_was_full"]:
        out["page_detail"] = (
            f"the events route returned exactly the {want} rows asked for, so it may "
            "be holding more; call again. Note the route has NO page token and clamps "
            "at the API's max_page_size, so once this count stops growing it may mean "
            "the ceiling was reached rather than that the task went quiet."
        )
    if not include_heartbeats and len(shown) < len(fresh):
        out["heartbeats_hidden"] = len(fresh) - len(shown)
    return out, cur


def _event_row(event: dict[str, Any]) -> dict[str, Any]:
    row = {
        "at": event.get("at"),
        "type": event.get("type"),
        "attempt_id": event.get("attempt_id"),
        "generation": event.get("generation"),
        "detail": event.get("detail"),
    }
    encoded = json.dumps(row.get("detail"), default=str)
    if len(encoded) > MAX_EVENT_DETAIL_CHARS:
        row["detail"] = encoded[:MAX_EVENT_DETAIL_CHARS]
        row["detail_truncated"] = (
            f"this event's detail was {len(encoded)} characters and was cut to "
            f"{MAX_EVENT_DETAIL_CHARS}; read it in full with `swarm_status` or the "
            "events route"
        )
    return row


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------

def _logs(
    client: SwarmClient,
    task_id: str,
    cur: dict[str, Any],
    *,
    terminal: bool,
    budget: _Budget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both streams, each from where this cursor left off.

    THE ATTEMPT IS PART OF THE CURSOR. A retry is a new attempt with a new
    fencing generation and a new set of objects, so a byte position from the
    previous attempt addresses nothing in the new one. When the route reports a
    different attempt from the one the cursor was built against, the positions
    are reset to zero and the reset is SAID -- silently continuing would splice
    the second attempt's output onto the first at an arbitrary byte.
    """
    cur = {**cur, "streams": {k: dict(v) for k, v in cur["streams"].items()}}
    rows: list[dict[str, Any]] = []
    attempt_id: str | None = None
    attempt_status: str | None = None
    notes: list[str] = []
    already_reset = False
    no_attempt_yet = False

    for name in STREAMS:
        state = cur["streams"][name]

        if state["complete"]:
            rows.append(
                _row(
                    name,
                    "complete",
                    source="final",
                    detail=(
                        "the completed log has been delivered in full; nothing further "
                        "will ever be written to it"
                    ),
                    to=state["pos"],
                )
            )
            continue

        if no_attempt_yet:
            # The first stream already established that this task has never
            # run. Asking the same question about the other stream -- and
            # about the other object behind it -- is three more requests for an
            # answer already in hand, and a task polled every few seconds
            # before it is admitted is the commonest poll there is.
            rows.append(
                _row(
                    name,
                    "absent",
                    detail="this task has no attempt yet, so no log object can exist",
                    to=state["pos"],
                )
            )
            continue

        if budget.room() <= 0:
            budget.withheld(
                f"{task_id} {name}: not read at all -- this call's {budget.limit}-byte log "
                f"budget was already spent. Call again with the returned cursor, or raise "
                f"max_log_bytes."
            )
            rows.append(
                _row(
                    name,
                    "skipped_budget",
                    detail="not read: the call's log budget was spent before this stream",
                    to=state["pos"],
                )
            )
            continue

        mark = budget.mark()
        row, state, body = _read_stream(
            client, task_id, name, state, terminal=terminal, budget=budget
        )

        if body is not None:
            attempt = body.get("attempt") or {}
            attempt_status = attempt.get("status") or attempt_status
            no_attempt_yet = attempt_status == "no_attempt_yet"
            seen = body.get("attempt_id")
            if isinstance(seen, str) and seen:
                attempt_id = seen
                if cur["attempt_id"] and seen != cur["attempt_id"] and not already_reset:
                    # A RETRY. Every position this cursor holds was measured
                    # against the previous attempt's objects, and the new
                    # attempt's streams start at zero -- so continuing from
                    # them would splice one attempt's output onto the other at
                    # an arbitrary byte. Both streams are reset, this one is
                    # re-read from the start of the new attempt, and the reset
                    # is stated rather than inferred from a jump in the output.
                    already_reset = True
                    # The read above addressed the OLD attempt's byte
                    # positions, so whatever it returned is not this attempt's
                    # output and must not be charged or reported.
                    budget.rollback(mark)
                    cur["attempt_id"] = seen
                    cur["streams"] = {
                        key: {"pos": 0, "source": None, "complete": False, "window_start": 0}
                        for key in STREAMS
                    }
                    notes.append(
                        f"attempt {seen} is now the latest one and is not the attempt this "
                        "cursor was built against, so every stream position was reset to "
                        "the start of the new attempt. What follows is the new attempt's "
                        "output, not a continuation of the previous one."
                    )
                    state = cur["streams"][name]
                    row, state, body = _read_stream(
                        client, task_id, name, state, terminal=terminal, budget=budget
                    )

        cur["streams"][name] = state
        rows.append(row)

    if attempt_id:
        cur["attempt_id"] = attempt_id

    status = "ok"
    detail = None
    statuses = {row["status"] for row in rows}
    if statuses == {"absent"} and attempt_status == "no_attempt_yet":
        status = "no_attempt_yet"
        detail = (
            "this task has no attempt yet, so no log object can exist. It is queued, "
            "parked or waiting on capacity -- this is not a failed read."
        )
    elif statuses == {"unreadable"}:
        status = "unreadable"
        detail = "neither stream could be read; see each stream's detail"
    elif statuses == {"absent"}:
        status = "absent"
        detail = "an attempt exists but has published no log objects yet"

    return (
        {
            "status": status,
            "detail": detail,
            "attempt_id": attempt_id,
            "attempt_status": attempt_status,
            "notes": notes,
            "streams": rows,
        },
        cur,
    )


def _row(
    stream: str,
    status: str,
    *,
    source: str | None = None,
    text: str | None = None,
    detail: str | None = None,
    frm: int | None = None,
    to: int | None = None,
) -> dict[str, Any]:
    """The constant shape every stream row has.

    `text` is None -- never "" -- in every non-ok case, for the reason
    `swarm_api.inspect._entry` gives: a reader that renders text without
    reading status must see nothing rather than an empty log, which looks like
    a successful read of a silent agent.
    """
    return {
        "stream": stream,
        "status": status,
        "source": source,
        "text": text,
        "bytes": 0,
        "from": frm,
        "to": to,
        "gap_bytes": 0,
        "repeat_bytes": 0,
        "more_bytes": 0,
        "detail": detail,
    }


def _read_stream(
    client: SwarmClient,
    task_id: str,
    stream: str,
    state: dict[str, Any],
    *,
    terminal: bool,
    budget: _Budget,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """One stream, from `state["pos"]` onwards.

    WHICH OBJECT IS TRIED FIRST depends on the task, not on a preference. While
    the task runs, only the live tail exists. Once it is terminal the completed
    record is the better answer -- it is the whole stream and it never moves --
    but it is not guaranteed: a worker killed before its upload leaves only the
    tail. So terminal reads try `final` then `live`, and running reads try
    `live` then `final`, and an `absent` from the first is the only thing that
    moves on to the second. An UNREADABLE is returned as-is, exactly as the
    route refuses to substitute one object for another after a failed read.
    """
    order = ("final", "live") if terminal else ("live", "final")
    last_absent: dict[str, Any] | None = None
    last_body: dict[str, Any] | None = None

    for source in order:
        if source == "final":
            row, new_state, body = _read_final(client, task_id, stream, state, budget=budget)
        else:
            row, new_state, body = _read_live(client, task_id, stream, state, budget=budget)
        if body is not None:
            last_body = body
        if row["status"] == "absent":
            last_absent = row
            if ((body or {}).get("attempt") or {}).get("status") == "no_attempt_yet":
                # Not "this object has not appeared yet" but "there has never
                # been an attempt to write one". The other object cannot exist
                # either, so asking for it is a request whose answer is known.
                return row, new_state, body
            continue
        return row, new_state, body

    assert last_absent is not None
    return last_absent, dict(state), last_body


def _read_final(
    client: SwarmClient,
    task_id: str,
    stream: str,
    state: dict[str, Any],
    *,
    budget: _Budget,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """The completed record, paged by byte offset.

    NO TRANSLATION IS NEEDED HERE, and that is the whole reason the cursor is
    kept in stream coordinates. The completed log IS the stream from byte zero,
    so a position built while watching the live tail addresses the completed
    record exactly -- the handover from one object to the other costs nothing
    and loses nothing.
    """
    state = dict(state)
    pos = state["pos"]
    body, row = _fetch(client, task_id, stream, source="final", offset=pos, limit=budget.room())
    if row["status"] != "ok":
        return row, state, body

    served = body["_served"]
    total = served.get("total_bytes") or 0
    frm = int(served.get("offset") or 0)
    returned = int(served.get("returned_bytes") or 0)
    text = served.get("content") or ""

    if total < pos:
        # The live tail showed more than the completed record holds. The
        # worker caps what it uploads, so this is a real state, and reporting
        # it as "nothing new" would say the agent stopped talking.
        row = _row(
            stream,
            "ok",
            source="final",
            text="",
            detail=(
                f"the completed log is {total} bytes but this cursor had already been "
                f"shown {pos} bytes from the live tail; the record is shorter than the "
                "window was. Nothing further is available."
            ),
            frm=pos,
            to=pos,
        )
        state["complete"] = True
        state["source"] = "final"
        return row, state, body

    gap = frm - pos if frm > pos else 0
    out = _row(stream, "ok", source="final", text=text, frm=frm, to=frm + returned)
    out["bytes"] = budget.charge(text)
    out["gap_bytes"] = gap
    if served.get("detail"):
        out["detail"] = served["detail"]

    nxt = served.get("next_offset")
    if nxt is None:
        state["complete"] = True
        state["pos"] = frm + returned
    else:
        state["complete"] = False
        state["pos"] = int(nxt)
        out["more_bytes"] = max(0, total - int(nxt))
        budget.withheld(
            f"{task_id} {stream}: {out['more_bytes']} bytes of the completed log were not "
            f"returned (this call's budget or the route's window ended at byte {nxt}). "
            "Call again with the returned cursor to continue from exactly there."
        )
    state["source"] = "final"
    if gap:
        budget.withheld(
            f"{task_id} {stream}: {gap} bytes were skipped by the route's token-boundary "
            f"alignment between byte {pos} and byte {frm}; they are not recoverable "
            "through this route."
        )
    return out, state, body


def _read_live(
    client: SwarmClient,
    task_id: str,
    stream: str,
    state: dict[str, Any],
    *,
    budget: _Budget,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """The live tail, translated out of object coordinates into stream ones.

    THE OBJECT MOVES UNDER THE READER. The worker republishes the whole object
    every few seconds and it holds only the last 256KB, so an offset into it
    means a different stream byte after each flush. The header it writes --
    `#swarm-tail offset=W size=S` -- is what makes that tractable: W is the
    stream byte the window starts at and S is how long the stream is. The API
    parses it out and serves it as `tail_window`.

    ONE READ, FROM OFFSET ZERO, so the header and the bytes come from the SAME
    object generation. Reading the header in one request and the wanted bytes
    in another would be a race that silently stitches two non-adjacent windows
    together -- which is precisely the lie `cli.cmd_tail`'s gap header exists
    to prevent, so it is not a race this code is allowed to run.

    The read is SIZED rather than fixed: the window start seen last time bounds
    how far into it the cursor now is, so the request asks for the overlap plus
    the budget instead of pulling all 256KB every poll.
    """
    state = dict(state)
    pos = state["pos"]
    overlap = max(0, pos - state.get("window_start", 0))
    want = min(MAX_LIVE_READ, overlap + max(budget.room(), 1))

    body, row = _fetch(client, task_id, stream, source="live", offset=0, limit=want)
    if row["status"] != "ok":
        return row, state, body

    served = body["_served"]
    window = served.get("tail_window") or {}
    start = int(window.get("object_offset") or 0)
    size = int(window.get("stream_size") or 0)
    returned = int(served.get("returned_bytes") or 0)
    text = served.get("content") or ""

    if not window:
        # No header: an object written by something other than this platform's
        # worker, or a worker old enough to predate the header. The only safe
        # reading is that the object IS the stream, which is true for a file
        # that never exceeded the window.
        size = int(served.get("total_bytes") or returned)

    state["window_start"] = start
    state["source"] = "live"

    if pos >= size:
        return (
            _row(
                stream,
                "up_to_date",
                source="live",
                detail=f"nothing new; the stream is {size} bytes and this cursor is at {pos}",
                frm=pos,
                to=pos,
            ),
            state,
            body,
        )

    if pos > start + returned and size > pos:
        # The cursor is past everything this window read, which can only happen
        # when the window slid further than the sizing estimate allowed for.
        # Saying so is the honest move: delivering the head of the window
        # instead would repeat output already shown and hide the real bytes.
        budget.withheld(
            f"{task_id} {stream}: this call read {returned} bytes of a live window that "
            f"starts at stream byte {start}, but the cursor is at {pos}; the new output "
            f"between {pos} and {size} was not reached. Raise max_log_bytes."
        )
        return (
            _row(
                stream,
                "behind",
                source="live",
                detail=(
                    f"the live window is {size - start} bytes and this call could not read "
                    f"far enough into it to reach byte {pos}; raise max_log_bytes"
                ),
                frm=pos,
                to=pos,
            ),
            state,
            body,
        )

    out = _deliver_from_window(
        stream, text, start=start, returned=returned, pos=pos, budget=budget
    )
    state["pos"] = start + returned
    if out["gap_bytes"]:
        budget.withheld(
            f"{task_id} {stream}: {out['gap_bytes']} bytes were produced between polls and "
            f"the 256KB live window moved past them before this call read it. They are "
            "gone; poll more often, or read the completed log once the task finishes."
        )
    more = max(0, size - (start + returned))
    if more:
        out["more_bytes"] = more
        budget.withheld(
            f"{task_id} {stream}: {more} bytes of live output past byte {start + returned} "
            "were not returned by this call. Call again with the returned cursor."
        )
    return out, state, body


def _deliver_from_window(
    stream: str,
    text: str,
    *,
    start: int,
    returned: int,
    pos: int,
    budget: _Budget,
) -> dict[str, Any]:
    """The part of one live window the caller has not already been given.

    THE SLICE IS OVER REDACTED TEXT, WHICH IS SHORTER THAN THE RAW BYTES IT
    STANDS FOR. The API redacts at read time and states that its byte
    accounting refers to the RAW object for exactly this reason, so cutting the
    text at `pos - start` would cut too far in and DROP output -- silently, and
    only on runs that printed something matching a redaction rule, which is the
    worst possible distribution for a bug.

    The shortfall is knowable in total (`returned` minus the redacted text's
    length) even though its distribution inside the window is not, so the cut
    is moved back by the whole shortfall. The result never drops a byte and at
    worst REPEATS up to `shortfall` bytes, which is reported as
    `repeat_bytes`. Choosing repetition over loss is deliberate: a repeat is
    visible to the reader, a drop is not.
    """
    if pos <= start:
        gap = start - pos if pos > 0 else 0
        out = _row(stream, "ok", source="live", text=text, frm=start, to=start + returned)
        out["bytes"] = budget.charge(text)
        out["gap_bytes"] = gap
        if gap:
            out["detail"] = (
                f"{gap} bytes were missed: the live window now starts at stream byte "
                f"{start} and this cursor was at {pos}"
            )
        return out

    shortfall = max(0, returned - len(text.encode("utf-8")))
    cut = max(0, (pos - start) - shortfall)
    fresh = text.encode("utf-8")[cut:].decode("utf-8", errors="replace")
    out = _row(stream, "ok", source="live", text=fresh, frm=pos, to=start + returned)
    out["bytes"] = budget.charge(fresh)
    if shortfall:
        out["repeat_bytes"] = shortfall
        out["detail"] = (
            f"up to {shortfall} bytes of this window may repeat output already shown: "
            "read-time redaction shortened the text, so the cut was moved back by the "
            "whole shortfall rather than risk dropping new output"
        )
    return out


def _fetch(
    client: SwarmClient,
    task_id: str,
    stream: str,
    *,
    source: str,
    offset: int,
    limit: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One call to the logs route, with the stream entry lifted out of it.

    Returns `(body, row)`. `body` carries `_served`, the entry for this stream,
    so a caller does not search the list twice. The row is already the answer
    for every case except `ok`.
    """
    try:
        body = client.logs(
            task_id,
            stream=stream,
            source=source,
            offset=max(0, offset),
            limit_bytes=max(1, limit),
        )
    except SwarmError as exc:
        return None, _row(
            stream,
            "unreadable",
            source=source,
            detail=f"the logs route failed: {exc}",
        )

    entries = body.get("streams") or []
    served = next((e for e in entries if e.get("stream") == stream), None)
    if served is None:
        return body, _row(
            stream,
            "unreadable",
            source=source,
            detail=(
                f"the logs route answered without a `{stream}` entry; it returned "
                f"{[e.get('stream') for e in entries]}"
            ),
        )

    body = {**body, "_served": served}
    status = served.get("status")
    if status == "ok":
        return body, _row(stream, "ok", source=served.get("source"))
    if status == "absent":
        return body, _row(
            stream, "absent", source=served.get("source"), detail=served.get("detail")
        )
    return body, _row(
        stream,
        "unreadable",
        source=served.get("source"),
        detail=served.get("detail") or "the logs route reported an unreadable object",
    )


# --------------------------------------------------------------------------
# Rendering, for the terminal
# --------------------------------------------------------------------------

def render(report: dict[str, Any]) -> list[str]:
    """The same answer as lines, so a human sees exactly what the model sees.

    Deliberately not a second implementation of anything: it reads the report
    `follow` already built. A renderer that went back to the client would be a
    second definition of "what is new", which is how a CLI and a tool start
    disagreeing.
    """
    lines: list[str] = []
    for task in report["tasks"]:
        short = task["task_id"][-8:]
        if task["read"] == "failed":
            lines.append(f"[{short}] ! {task['error']}")
            continue
        for event in task["events"].get("new") or []:
            lines.append(f"[{short}] · {event['type']}")
        if task["events"]["status"] == "unreadable":
            lines.append(f"[{short}] ! events unreadable: {task['events']['detail']}")
        for row in task["logs"].get("streams") or []:
            if row["status"] == "ok" and row["text"]:
                for line in row["text"].splitlines():
                    lines.append(f"[{short}] {line}")
            elif row["status"] in ("unreadable", "behind", "skipped_budget"):
                lines.append(f"[{short}] ! {row['stream']}: {row['detail']}")
        for note in task["logs"].get("notes") or []:
            lines.append(f"[{short}] ⟲ {note}")
        if task["terminal"]:
            lines.append(f"[{short}] {task['state']}")
    for message in report["truncation"]:
        lines.append(f"… {message}")
    return lines
