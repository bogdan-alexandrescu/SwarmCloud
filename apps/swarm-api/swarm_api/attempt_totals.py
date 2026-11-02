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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_common.models import Attempt, Task

from .codec import attempt_from_dict
from .store import ATTEMPTS

log = logging.getLogger(__name__)

#: A creation time for an attempt document a merge-only write left without one.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Firestore's ceiling on the values of one `in` filter.
IN_LIMIT = 30

#: Attempt documents read per task before a chunk is called incomplete. A
#: task's attempts are bounded by its `max_attempts`, which is far below this;
#: a chunk that reaches it is reported unread rather than summed short.
PER_TASK_CAP = 50

#: Chunk queries in flight at once: seven covers a full 200-row page.
MAX_CONCURRENT_CHUNKS = 8


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


#: The figures `attempt_totals` returns, in order: what a stored copy must
#: carry, every one, to be served.
TOTAL_KEYS: tuple[str, ...] = (
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

    THE CHUNKS ARE READ AT ONCE (#2688, LOAD-SPEED): a 200-row page is seven
    independent queries, and run one after another they were the longest
    chain of round trips in `GET /v1/tasks`. Each chunk keeps its own failure
    rule, so one chunk that fails marks only its own tasks unread.
    """
    wanted = list(dict.fromkeys(t for t in task_ids if t))
    chunks = [wanted[start : start + IN_LIMIT] for start in range(0, len(wanted), IN_LIMIT)]
    out: dict[str, dict[str, Any]] = {}
    if len(chunks) <= 1:
        for chunk in chunks:
            out.update(_chunk_totals(db, tenant_id, chunk))
        return out
    with ThreadPoolExecutor(max_workers=min(len(chunks), MAX_CONCURRENT_CHUNKS)) as pool:
        for found in pool.map(lambda chunk: _chunk_totals(db, tenant_id, chunk), chunks):
            out.update(found)
    return out


def _chunk_totals(db: Any, tenant_id: str, chunk: list[str]) -> dict[str, dict[str, Any]]:
    """One chunk of at most 30 tasks: their totals, or every one unread."""
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
        return {t: unread_totals() for t in chunk}
    return {t: {**attempt_totals(rows), "attempts_read": "ok"} for t, rows in by_task.items()}


def with_totals(row: dict[str, Any], totals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`row` (a `task_to_api` row) with its task's totals added beside it."""
    row.update(totals.get(str(row.get("id")), unread_totals()))
    return row



# --------------------------------------------------------------------------
# The stored copy (contract request 132, accepted 2026-11-01)
# --------------------------------------------------------------------------
#
# `Task.attempt_totals` is a READ-SIDE VIEW ONLY: `attempt_totals()` over the
# task's attempt documents, plus `as_of`, the attempt whose write stored it.
# Rewritten by whoever owns the task's current attempt when that attempt
# starts, records its spend and ends (the worker's `control.py` on the direct
# path, `attemptstate.py` on the state API), each time from the documents
# themselves, so a lost write is corrected by the next. `GET /v1/tasks`
# serves it when it is the CURRENT attempt's (`served_totals`) and derives the
# rest as above. Nothing that admits, leases or counts capacity reads it.


def task_attempt_docs(db: Any, tenant_id: str, task_id: str) -> list[dict[str, Any]]:
    """Every attempt document of one task, in its tenant: one bounded query.

    Raises `OverflowError` at `PER_TASK_CAP`, so a writer never stores a total
    summed short; a writer that cannot read stores nothing, and the reader
    derives.
    """
    query = (
        db.collection(ATTEMPTS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("task_id", "==", task_id))
        .limit(PER_TASK_CAP)
    )
    docs = [snap.to_dict() or {} for snap in query.stream()]
    if len(docs) >= PER_TASK_CAP:
        raise OverflowError(f"{len(docs)} attempt documents for one task")
    return [d for d in docs if d.get("tenant_id") == tenant_id and d.get("task_id") == task_id]


def stored_totals(
    docs: Iterable[Mapping[str, Any]],
    *,
    attempt_id: str,
    generation: int,
    own: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`Task.attempt_totals` as a writer stores it.

    `docs` are the task's attempt documents as read; `own` is what THIS
    write puts on the writer's own attempt (its end, its spend), merged over
    the document read, because the write commits beside the stored copy.
    """
    rows: dict[str, dict[str, Any]] = {}
    for doc in docs:
        rows[str(doc.get("attempt_id"))] = dict(doc)
    if own is not None:
        rows[attempt_id] = {
            "attempt_id": attempt_id, "generation": generation,
            **rows.get(attempt_id, {}), **dict(own),
        }
    attempts = [attempt_from_dict(_with_defaults(d, attempt_id, generation)) for d in rows.values()]
    return {**attempt_totals(attempts),
            "as_of": {"attempt_id": attempt_id, "generation": generation}}


def _with_defaults(doc: Mapping[str, Any], attempt_id: str, generation: int) -> dict[str, Any]:
    """An attempt document `attempt_from_dict` can decode, however partial a
    merge-only write left it (the end landing before the start)."""
    out = dict(doc)
    out.setdefault("attempt_id", attempt_id)
    out.setdefault("generation", generation)
    out.setdefault("task_id", "")
    out.setdefault("tenant_id", "")
    out.setdefault("lease_id", "")
    out.setdefault("backend", "")
    if out.get("created_at") is None:
        out["created_at"] = out.get("started_at") or out.get("completed_at") or _EPOCH
    return out


def served_totals(task: Task) -> dict[str, Any] | None:
    """The stored copy, as a row serves it, or None when the reader must derive.

    Served only when it is the CURRENT attempt's (`as_of.generation ==
    current_generation`): a newer attempt the scheduler has leased since has a
    document the copy does not count. None too for an older document, one with
    no copy, or a malformed one.
    """
    stored = task.attempt_totals
    if not isinstance(stored, Mapping):
        return None
    as_of = stored.get("as_of")
    if not isinstance(as_of, Mapping) or as_of.get("generation") != task.current_generation:
        return None
    if any(key not in stored for key in TOTAL_KEYS):
        return None
    return {**{key: stored[key] for key in TOTAL_KEYS}, "attempts_read": "ok"}


def totals_for_page(db: Any, tenant_id: str, tasks: Sequence[Task]) -> dict[str, dict[str, Any]]:
    """Each task's totals: its stored copy where current, else `totals_for`'s
    batched read for only the tasks without one."""
    out: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for task in tasks:
        served = served_totals(task) if task.tenant_id == tenant_id else None
        if served is not None:
            out[task.id] = served
        else:
            pending.append(task.id)
    if pending:
        out.update(totals_for(db, tenant_id, pending))
    return out
