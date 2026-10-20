"""`GET /v1/tasks/{id}/utilisation`: a task's CPU, memory and throttling over time (LIVE-UTIL-API).

WHAT THIS IS FOR. The owner, 2026-10-20 (#1689): Details showed an attempt's
CPU limit, the peak of its 2-second samples and its mean, and "those will
always show maximum when in fact the pods do not run at max cpu the entire
time". The console polls this route while a task runs and draws use over time,
with CPU throttling MEASURED rather than inferred from a peak at the limit:

    {"task_id", "read_at",
     "attempts": [{"attempt_id", "generation",
                   "cpu_limit_cores", "memory_limit_bytes",
                   "throttled_seconds", "peak_throttled_share", "mean_throttled_share",
                   "points": [{"at", "cpu_cores", "memory_bytes", "throttled_share"}]}]}

WHERE THE POINTS COME FROM. The worker takes one point every 5 seconds from
the container's cgroup (`agent_worker.metrics.ResourceSampler.sample_point`)
and puts the ones taken since the previous beat on each HEARTBEAT event's
`detail.series`, as `{"t", "cpu", "mem", "thr"}`, beside `series_clock` --
"now" on the same clock. A point is dated `event.at - (series_clock - t)`: the
worker's monotonic clock never meets the server's wall clock, only the
difference between two readings of it does. HEARTBEAT is the one event series
the platform keeps per task; no new collection, no new write.

THE ATTEMPT'S FIGURES. `cpu_limit_cores` is the attempt document's typed field
(contract request #15). `memory_limit_bytes` and the three throttling figures
are the attempt's newest HEARTBEAT's: `Attempt` is frozen and has no field for
them, and contract change request 107 asks for typed ones. Attempts newest
first, as `GET /v1/tasks/{id}/attempts` serves them; an attempt that has not
beaten yet is served with no points. Only events of the attempt's own
generation (or none, for older writers) are read into it (invariant 5).

None means not measured, never zero, throughout: a point the worker could not
read is served null, and so is a figure no heartbeat carried.

BOUNDED READS. One task read (the tenant and submitter check), one attempts
query of at most `MAX_ATTEMPTS`, and one events query on `type` alone, which
Firestore's automatic single-field index serves, of at most
`HEARTBEAT_EVENTS_LIMIT` -- at one HEARTBEAT event per 60-150 s, more than a
day of beats. A poll costs that much and nothing else.

A READ, NOTHING ELSE: no state, lease or pool is touched (invariants 1-3).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Attempt, Task
from swarm_common.states import EventType

from .store import EVENTS, TASKS

#: Attempts served, newest first. A task retried more often than this has its
#: oldest attempts left out; each one served costs nothing beyond its row.
MAX_ATTEMPTS = 20

#: HEARTBEAT events read for one task. See the module docstring.
HEARTBEAT_EVENTS_LIMIT = 2000

#: The attempt figures read off the newest HEARTBEAT that carries each.
BEAT_FIGURES = (
    "memory_limit_bytes",
    "throttled_seconds",
    "peak_throttled_share",
    "mean_throttled_share",
)


def _number(value: Any) -> float | None:
    """A JSON number as a float; None for null, a bool or anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _whole(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def points_of(event: dict[str, Any]) -> list[dict[str, Any]]:
    """One HEARTBEAT event's points, dated; [] for a beat from before the series."""
    at = event.get("at")
    detail = event.get("detail")
    if not isinstance(at, datetime) or not isinstance(detail, dict):
        return []
    series = detail.get("series")
    clock = _number(detail.get("series_clock"))
    if not isinstance(series, list) or clock is None:
        return []
    out: list[dict[str, Any]] = []
    for point in series:
        if not isinstance(point, dict):
            continue
        t = _number(point.get("t"))
        if t is None:
            continue
        out.append({
            "at": at - timedelta(seconds=clock - t),
            "cpu_cores": _number(point.get("cpu")),
            "memory_bytes": _whole(point.get("mem")),
            "throttled_share": _number(point.get("thr")),
        })
    return out


def _of_attempt(event: dict[str, Any], attempt: Attempt) -> bool:
    if event.get("attempt_id") != attempt.attempt_id:
        return False
    stamped = event.get("generation")
    return stamped is None or stamped == attempt.generation


def attempt_series(attempt: Attempt, events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """One attempt's row: its limits, its throttling figures and its points, oldest first."""
    beats = sorted(
        (e for e in events if _of_attempt(e, attempt) and isinstance(e.get("at"), datetime)),
        key=lambda e: e["at"],
    )
    figures: dict[str, Any] = {name: None for name in BEAT_FIGURES}
    for beat in reversed(beats):
        detail = beat.get("detail")
        if not isinstance(detail, dict):
            continue
        for name in BEAT_FIGURES:
            if figures[name] is None and detail.get(name) is not None:
                figures[name] = _number(detail.get(name))
        if all(value is not None for value in figures.values()):
            break
    points = [point for beat in beats for point in points_of(beat)]
    points.sort(key=lambda point: point["at"])
    memory_limit = figures.pop("memory_limit_bytes")
    return {
        "attempt_id": attempt.attempt_id,
        "generation": attempt.generation,
        "cpu_limit_cores": attempt.cpu_limit_cores,
        "memory_limit_bytes": None if memory_limit is None else int(memory_limit),
        **figures,
        "points": points,
    }


def heartbeat_events(db: Any, task: Task) -> list[dict[str, Any]]:
    """The task's HEARTBEAT events, at most `HEARTBEAT_EVENTS_LIMIT`, unordered."""
    query = (
        db.collection(TASKS)
        .document(task.id)
        .collection(EVENTS)
        .where(filter=FieldFilter("type", "==", EventType.HEARTBEAT.value))
        .limit(HEARTBEAT_EVENTS_LIMIT)
    )
    return [snap.to_dict() or {} for snap in query.stream()]


def utilisation_of(db: Any, task: Task, attempts: list[Attempt], *, read_at: datetime) -> dict[str, Any]:
    """The route's answer for one task the caller may read (tenant-checked by the caller).

    `attempts` are the task's, newest first (`Store.list_attempts`).
    """
    events = heartbeat_events(db, task) if attempts else []
    return {
        "task_id": task.id,
        "read_at": read_at,
        "attempts": [attempt_series(attempt, events) for attempt in attempts],
    }
