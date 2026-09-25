"""CPU (and the peak memory beside it) per attempt, from the worker's HEARTBEAT readings.

WHY FROM EVENTS (#184). The Details pane draws memory and workspace against
their limits; the owner asked for CPU beside them, as the PEAK and the MEAN of
the runtime's CPU limit. The worker measures both (`agent_worker.metrics`),
but the frozen `swarm_common.models.Attempt` has no CPU fields, so they cannot
ride on the attempt document the way `peak_rss_bytes` does -- contract request
#15 in docs/contract-change-requests.md asks for typed ones. Until it is
decided, the worker puts them on its HEARTBEAT events (flat keys in `detail`:
see `agent_worker.metrics.heartbeat_cpu_fields`), and this module reads them
back for `GET /v1/tasks/{id}/attempts?include=usage`.

ONE READ, BOUNDED. The task's events are read ONCE per request, newest first,
one page of `max_page_size`. Per attempt the newest HEARTBEAT whose detail
HAS the key `cpu_seconds` (present, possibly null, on every reading) is its
reading. A long run's final reading is at the END of its history, which is
why this is a descending read and why the UI must not use its own 200-event
oldest-first page for the bar.

THE STATUS OF EACH BLOCK, and what the UI prints for it:

  * `final`         -- the newest reading was taken after a runner was reaped
                       (`detail.final`): "at exit";
  * `live`          -- a periodic reading, and the attempt has not ended:
                       "latest heartbeat Ns ago";
  * `last_reading`  -- a periodic reading, and the attempt ended without a
                       final one (killed, reclaimed): "last heartbeat";
  * `never_ran`     -- no reading and `started_at` is null;
  * `absent`        -- no reading, and the page reached back past the
                       attempt's creation, so there is none to find;
  * `beyond_window` -- no reading, and the page was full without reaching the
                       attempt: there may be one further back;
  * `unread`        -- the events read failed. Every block says so, and the
                       route still answers 200: the attempt rows are real.

A reading whose figures are all null is still a reading -- "never measured"
is the UI's sentence for it, never zero.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from swarm_common.models import Attempt, Task
from swarm_common.states import CONCURRENCY_STATES, TERMINAL_STATES, EventType

#: Numeric figures copied from a reading, in the order the block lists them.
_FIGURES = (
    "cpu_seconds",
    "peak_cpu_cores",
    "mean_cpu_cores",
    "cpu_wall_seconds",
    "cpu_limit_cores",
    "peak_rss_bytes",
)
_WORDS = ("cpu_source", "cpu_limit_source")


def attempt_is_over(attempt: Attempt, task: Task, *, is_latest: bool) -> bool:
    """Whether this attempt has ended, on the evidence the UI's `attemptEnd` uses.

    Restated from `apps/swarm-ui/src/duration.ts` so the server's "live" and
    the drawer's "running" are the same judgement:

      * its document records `completed_at`;
      * it is not the task's newest attempt (a newer one superseded it);
      * the task is terminal;
      * the task no longer holds this attempt's lease -- on a task document
        written no earlier than the attempt existed, since an older read says
        nothing about an attempt admitted after it.
    """
    if attempt.completed_at is not None:
        return True
    if not is_latest:
        return True
    if task.state in TERMINAL_STATES:
        return True
    # A null pointer is NOT holding: the API always serves `current_lease_id`,
    # and null there means the task holds no lease -- the UI's `letGo` reads it
    # the same way (only a pointer the payload OMITS is read as holding).
    holds = task.state in CONCURRENCY_STATES and task.current_lease_id == attempt.lease_id
    if holds:
        return False
    return _utc(task.updated_at) >= _utc(attempt.created_at)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else _utc(moment).astimezone(timezone.utc).isoformat()


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _empty_block(status: str, detail: str) -> dict[str, Any]:
    block: dict[str, Any] = {
        "status": status,
        "detail": detail,
        "event_id": None,
        "measured_at": None,
        "age_seconds": None,
        "final": None,
    }
    for name in _FIGURES:
        block[name] = None
    for name in _WORDS:
        block[name] = None
    return block


def usage_blocks(
    attempts: list[Attempt],
    task: Task,
    events: Iterable[Any] | None,
    *,
    read_at: datetime,
    window_full: bool,
    read_error: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """`({attempt_id: usage block}, usage_read)` for one task's attempts.

    `events` is the one descending page (newest first), or None when the read
    failed -- then `read_error` says why and every block is `unread`.
    """
    read_at = _utc(read_at)
    if events is None:
        detail = read_error or "the task's events could not be read"
        blocks = {
            a.attempt_id: _empty_block("unread", "the events read failed, so no reading was looked for")
            for a in attempts
        }
        return blocks, {
            "read_at": _iso(read_at),
            "events_examined": 0,
            "window_full": False,
            "status": "unread",
            "detail": detail,
        }

    page = list(events)
    newest: dict[str, Any] = {}
    for event in page:
        kind = getattr(event, "type", None)
        kind = kind.value if hasattr(kind, "value") else kind
        if kind != EventType.HEARTBEAT.value:
            continue
        attempt_id = getattr(event, "attempt_id", None)
        detail = getattr(event, "detail", None)
        if not attempt_id or not isinstance(detail, dict) or "cpu_seconds" not in detail:
            continue
        if attempt_id not in newest:
            newest[attempt_id] = event  # descending: the first seen is the newest
    oldest_at = min((_utc(e.at) for e in page), default=None)

    latest_id = (
        max(attempts, key=lambda a: (_utc(a.created_at), a.attempt_id)).attempt_id
        if attempts
        else None
    )
    blocks: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        reading = newest.get(attempt.attempt_id)
        if reading is not None:
            blocks[attempt.attempt_id] = _reading_block(
                reading, attempt, task, read_at=read_at,
                is_latest=attempt.attempt_id == latest_id,
            )
            continue
        if attempt.started_at is None:
            blocks[attempt.attempt_id] = _empty_block(
                "never_ran", "this attempt never started a worker, so nothing was measured"
            )
            continue
        reached = (not window_full) or (
            oldest_at is not None and oldest_at < _utc(attempt.created_at)
        )
        if reached:
            blocks[attempt.attempt_id] = _empty_block(
                "absent",
                "the events read reached back past this attempt's start and found no "
                "resource reading for it",
            )
        else:
            blocks[attempt.attempt_id] = _empty_block(
                "beyond_window",
                "the newest events filled the read without reaching this attempt; its "
                "reading, if any, is further back",
            )
    return blocks, {
        "read_at": _iso(read_at),
        "events_examined": len(page),
        "window_full": window_full,
        "status": "ok",
        "detail": None,
    }


def _reading_block(
    event: Any, attempt: Attempt, task: Task, *, read_at: datetime, is_latest: bool
) -> dict[str, Any]:
    detail = event.detail
    final = detail.get("final") is True
    if final:
        status, why = "final", "taken when the runner was reaped"
    elif not attempt_is_over(attempt, task, is_latest=is_latest):
        status, why = "live", "the newest periodic reading of a running attempt"
    else:
        status, why = (
            "last_reading",
            "the attempt ended without a reading at exit; this is its last periodic one",
        )
    measured = _utc(event.at)
    block: dict[str, Any] = {
        "status": status,
        "detail": why,
        "event_id": event.event_id,
        "measured_at": _iso(measured),
        "age_seconds": round((read_at - measured).total_seconds(), 3),
        "final": final,
    }
    for name in _FIGURES:
        block[name] = _number(detail.get(name))
    for name in _WORDS:
        value = detail.get(name)
        block[name] = value if isinstance(value, str) else None
    return block
