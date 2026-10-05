"""A task's spend and time across EVERY attempt, for the task the API serves.

WHY (owner decision, 2026-10-05, lane review P1). `result_summary` is written
by `finish()` for the attempt that ended last, and the task's `started_at` is
overwritten by every attempt's STARTING, so the task document only ever
describes its LAST attempt. `swarm result` and the follow outcome reported
exactly that: UR1's implement step served $0.51 while its two attempts cost
$9.64, and across 17 lanes $65.77 was served against $77.53 spent. The
per-attempt records (`GET /v1/tasks/{id}/attempts`) held the rest, and no
reader summed them.

So `GET /v1/tasks/{id}` and `GET /v1/workflows/{id}` serve, on each task:

  attempts                 how many attempt documents the task has;
  attempts_with_cost       how many of them recorded a `cost_usd`;
  cost_usd_total           every recorded `cost_usd`, summed;
  cost_incomplete          true when any attempt recorded no cost, so the
                           total is a FLOOR ("at least $X");
  duration_s_total         every ended attempt's started_at -> completed_at;
  duration_incomplete      true when any attempt has no start or no end;
  first_started_at         the earliest attempt start;
  last_attempt_cost_usd    the newest attempt's cost, for the secondary text;
  last_attempt_duration_s  the newest attempt's run time.

The last attempt's own fields on the task -- `started_at`, `completed_at`,
`result_summary` -- keep their names and meaning; these are added beside them.

NULL IS NOT ZERO. A total with no recorded figure is null, never 0: a task
whose runner reported nothing did not run for free. `attempts` is a COUNT and
is 0 for a task never attempted -- that was counted, not missed.

A FAILED READ IS NOT "NO ATTEMPTS". When the attempt query fails, or a chunk
comes back at its limit, the task gets every figure null and
`attempts_read: "failed"`, so no reader turns an unread task into a cheap one.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_common.models import Attempt

from .codec import attempt_from_dict
from .store import ATTEMPTS

log = logging.getLogger(__name__)

#: Firestore's ceiling on the values of one `in` filter.
IN_LIMIT = 30

#: Attempt documents read per task before a chunk is called incomplete. A
#: task's attempts are bounded by its `max_attempts`, which is far below this;
#: a chunk that reaches it is reported unread rather than summed short.
PER_TASK_CAP = 50


def _seconds(attempt: Attempt) -> float | None:
    """One attempt's run time, or None when it has no start or no end yet."""
    if attempt.started_at is None or attempt.completed_at is None:
        return None
    if attempt.completed_at < attempt.started_at:
        return None
    return (attempt.completed_at - attempt.started_at).total_seconds()


def _newest(attempts: Sequence[Attempt]) -> Attempt | None:
    """The last attempt: the highest generation, then the latest created.

    Ordered here, not by the query, because the batched read below has no
    ordering (an `in` filter with an order would need a composite index).
    """
    if not attempts:
        return None
    return max(attempts, key=lambda a: (a.generation, a.created_at))


def attempt_totals(attempts: Iterable[Attempt]) -> dict[str, Any]:
    """The totals one task's attempts add up to. Pure, so it is tested alone."""
    rows = list(attempts)
    costs = [a.cost_usd for a in rows if a.cost_usd is not None]
    seconds = [_seconds(a) for a in rows]
    measured = [s for s in seconds if s is not None]
    starts = [a.started_at for a in rows if a.started_at is not None]
    last = _newest(rows)
    last_seconds = _seconds(last) if last is not None else None
    return {
        "attempts": len(rows),
        "attempts_with_cost": len(costs),
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_incomplete": len(costs) < len(rows),
        "duration_s_total": round(sum(measured), 1) if measured else None,
        "duration_incomplete": len(measured) < len(rows),
        "first_started_at": min(starts) if starts else None,
        "last_attempt_cost_usd": last.cost_usd if last is not None else None,
        "last_attempt_duration_s": round(last_seconds, 1) if last_seconds is not None else None,
    }


def unread_totals() -> dict[str, Any]:
    """Every figure null, and why: the attempts could not be read."""
    return {
        "attempts": None,
        "attempts_with_cost": None,
        "cost_usd_total": None,
        "cost_incomplete": None,
        "duration_s_total": None,
        "duration_incomplete": None,
        "first_started_at": None,
        "last_attempt_cost_usd": None,
        "last_attempt_duration_s": None,
        "attempts_read": "failed",
    }


def totals_for(db: Any, tenant_id: str, task_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """`attempt_totals` for every task in `task_ids`, from one query per 30 tasks.

    Equality plus `in` and no ordering, which merged single-field indexes
    serve -- the read `outcomes.Outcomes._attempts` already makes. Every task
    asked for gets an entry: its totals, or `unread_totals()` when its chunk
    could not be read whole.
    """
    wanted = list(dict.fromkeys(t for t in task_ids if t))
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), IN_LIMIT):
        chunk = wanted[start : start + IN_LIMIT]
        limit = PER_TASK_CAP * len(chunk)
        try:
            query = (
                db.collection(ATTEMPTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("task_id", "in", chunk))
                .limit(limit)
            )
            docs = [snap.to_dict() or {} for snap in query.stream()]
            if len(docs) >= limit:
                raise OverflowError(f"{len(docs)} attempt documents for {len(chunk)} task(s)")
            by_task: dict[str, list[Attempt]] = {t: [] for t in chunk}
            members = set(chunk)
            for doc in docs:
                # The query filters both; restated where a fake or an index
                # mistake cannot move the tenant boundary.
                if doc.get("tenant_id") != tenant_id or doc.get("task_id") not in members:
                    continue
                by_task[doc["task_id"]].append(attempt_from_dict(doc))
        except Exception as exc:  # the tasks were read; only their attempts were not
            log.warning("attempt totals read failed: %s", type(exc).__name__)
            out.update({t: unread_totals() for t in chunk})
            continue
        out.update({t: {**attempt_totals(rows), "attempts_read": "ok"} for t, rows in by_task.items()})
    return out


def with_totals(row: dict[str, Any], totals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`row` (a `task_to_api` row) with its task's totals added beside it."""
    row.update(totals.get(str(row.get("id")), unread_totals()))
    return row

