"""`GET /v1/tasks/{id}/startup`: an attempt's start-up steps and how long they usually take (STARTUP-API).

WHAT THIS IS FOR. The owner, 2026-10-13: "we should put a progress bar when
the browser agent is starting up the browser so you get a sense of progress as
it comes up" -- "Stages + real pod steps". The console (lane STARTUP-UI)
draws the bar in the Browser tab, the Agents thumbnails and the theater from
this one answer:

    {"attempt_id", "generation",
     "steps": [{"step", "at"}, ...],
     "estimate": {"<step>": median seconds from dispatched, ...},
     "sample_size": n,
     "done": bool}

THE STEPS are the current attempt's `lease_acquired`, `dispatched`,
`starting` and `running` events -- the bar's first stages, which already
existed -- and every `startup_progress` step (contract request 74): the pod's
four, written by the reconciler (`reconciler.startsteps`), and the browser's
three, written by the worker off the live gate
(`agent_worker.handoff.GATE_STARTUP_STEPS`). Each step once, at its earliest
time (the worker writes `running` again as start-up marks, with a `cause`), in
the order they happened. Only the CURRENT attempt's, at its current
generation: an earlier attempt's pod, or a fenced worker's late write, is not
this start-up (invariant 5).

THE ESTIMATE is, per step, the median seconds from `dispatched` over the last
`ESTIMATE_SAMPLE` completed start-ups -- attempts that reached `first_frame`
-- of the same runner profile IN THE CALLER'S TENANT. The tenant's own: a
figure computed from other tenants' tasks would tell this one when they ran
(invariant 9). Bounded reads -- the newest `ESTIMATE_TASKS` tasks of the
profile, one events query each -- cached for `ESTIMATE_TTL_SECONDS` per
tenant and profile, so a console polling the bar costs one task read and one
events query per poll. No history is `{}` and `sample_size` 0; the console
then shows the steps without a time.

`done` is true once `first_frame` is seen, or once the task is no longer
starting or running (it ended, or went back to wait).

A READ, NOTHING ELSE: no state, lease or pool is touched (invariants 1-3).
"""

from __future__ import annotations

import statistics
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Task
from swarm_common.states import CONCURRENCY_STATES, EventType

from .store import EVENTS, LEASES, TASKS

#: The `startup_progress` steps, in order: the pod's four, then the browser's
#: three. tests/unit/common/test_startup_progress_contract.py holds this equal
#: to the reconciler's and the worker's restatements.
STARTUP_STEPS: tuple[str, ...] = (
    "pod_scheduled",
    "image_pulling",
    "image_pulled",
    "container_started",
    "browser_launching",
    "browser_ready",
    "first_frame",
)

#: The existing events that are the bar's first stages, in order.
STAGE_EVENTS: tuple[str, ...] = (
    EventType.LEASE_ACQUIRED.value,
    EventType.DISPATCHED.value,
    EventType.STARTING.value,
    EventType.RUNNING.value,
)

#: Every step this route can serve, in its canonical order: the tie-break
#: between steps that share a timestamp.
ALL_STEPS: tuple[str, ...] = STAGE_EVENTS + STARTUP_STEPS
_RANK = {step: index for index, step in enumerate(ALL_STEPS)}

#: The step that ends a start-up: the live stream's first frame.
DONE_STEP = "first_frame"

#: Completed start-ups the estimate is the median of (the owner's "last 20").
ESTIMATE_SAMPLE = 20
#: Tasks of the profile read for the estimate, newest first: twice the sample,
#: because a task that never reached its first frame contributes nothing.
ESTIMATE_TASKS = 40
#: Events read per task for the estimate (stage and step events only).
ESTIMATE_EVENTS_PER_TASK = 200
#: How long one tenant's estimate for one profile is served from memory.
ESTIMATE_TTL_SECONDS = 300.0
#: Events of the current attempt read for its steps. An attempt writes a
#: heartbeat every 150 s and two checkpoint events every 120 s, so even a
#: long browser attempt is well under this.
ATTEMPT_EVENTS_LIMIT = 2000

_ESTIMATE_TYPES = [
    EventType.DISPATCHED.value,
    EventType.STARTING.value,
    EventType.RUNNING.value,
    EventType.STARTUP_PROGRESS.value,
]


def _step_of(event: dict[str, Any]) -> str | None:
    """The bar step an event marks, or None when it marks none."""
    kind = event.get("type")
    if kind == EventType.STARTUP_PROGRESS.value:
        detail = event.get("detail")
        step = detail.get("step") if isinstance(detail, dict) else None
        return step if step in STARTUP_STEPS else None
    return kind if kind in STAGE_EVENTS else None


def _of_generation(event: dict[str, Any], generation: int | None) -> bool:
    """An event of this generation, or one that names none (older writers)."""
    stamped = event.get("generation")
    return generation is None or stamped is None or stamped == generation


def first_times(events: Iterable[dict[str, Any]], *, generation: int | None) -> dict[str, datetime]:
    """Each step's EARLIEST time among one attempt's events of one generation."""
    found: dict[str, datetime] = {}
    for event in events:
        step = _step_of(event)
        at = event.get("at")
        if step is None or not isinstance(at, datetime) or not _of_generation(event, generation):
            continue
        if step not in found or at < found[step]:
            found[step] = at
    return found


def ordered_steps(times: dict[str, datetime]) -> list[dict[str, Any]]:
    """The steps as served: in the order they happened, ties in the bar's order."""
    return [
        {"step": step, "at": at}
        for step, at in sorted(times.items(), key=lambda item: (item[1], _RANK[item[0]]))
    ]


def estimate_from(startups: list[dict[str, datetime]]) -> dict[str, float]:
    """Per step, the median seconds from `dispatched` over the start-ups that reached it."""
    estimate: dict[str, float] = {}
    for step in ALL_STEPS:
        if step in (EventType.LEASE_ACQUIRED.value, EventType.DISPATCHED.value):
            continue
        seconds = [
            (times[step] - times[EventType.DISPATCHED.value]).total_seconds()
            for times in startups
            if step in times
        ]
        if seconds:
            estimate[step] = round(float(statistics.median(seconds)), 1)
    return estimate


class StartupEstimates:
    """The per-tenant, per-profile estimate, read boundedly and held for a few minutes.

    Per app (`AppContext.startup_estimates`), never at import, so a test gets
    its own cache over its own in-memory Firestore.
    """

    def __init__(self, *, ttl_seconds: float = ESTIMATE_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict[str, float], int]] = {}

    def estimate(self, db: Any, tenant_id: str, runner_profile: str) -> tuple[dict[str, float], int]:
        key = (tenant_id, runner_profile)
        now = self._clock()
        with self._lock:
            held = self._cache.get(key)
            if held is not None and held[0] > now:
                return dict(held[1]), held[2]
        startups = recent_startups(db, tenant_id, runner_profile)
        estimate = estimate_from(startups)
        with self._lock:
            self._cache[key] = (now + self._ttl, estimate, len(startups))
        return dict(estimate), len(startups)


def recent_startups(db: Any, tenant_id: str, runner_profile: str) -> list[dict[str, datetime]]:
    """The last `ESTIMATE_SAMPLE` completed start-ups of a profile in one tenant, newest first.

    One query for the newest `ESTIMATE_TASKS` tasks (the tasks-tenant-profile
    index, `tenant_id`, `runner_profile`, `created_at` desc), then one events
    query per task on `type` alone, which Firestore's automatic index serves.
    Each attempt that reached `first_frame` and has a `dispatched` is one
    start-up, read at the generation its `dispatched` was written under.
    """
    tasks = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("runner_profile", "==", runner_profile))
        .order_by("created_at", direction="DESCENDING")
        .limit(ESTIMATE_TASKS)
    )
    startups: list[dict[str, datetime]] = []
    for task in tasks.stream():
        events = (
            db.collection(TASKS)
            .document(task.id)
            .collection(EVENTS)
            .where(filter=FieldFilter("type", "in", _ESTIMATE_TYPES))
            .limit(ESTIMATE_EVENTS_PER_TASK)
        )
        by_attempt: dict[str, list[dict[str, Any]]] = {}
        for snap in events.stream():
            event = snap.to_dict() or {}
            attempt_id = event.get("attempt_id")
            if isinstance(attempt_id, str) and attempt_id:
                by_attempt.setdefault(attempt_id, []).append(event)
        for attempt_events in by_attempt.values():
            dispatched = [
                e for e in attempt_events
                if e.get("type") == EventType.DISPATCHED.value and isinstance(e.get("at"), datetime)
            ]
            if not dispatched:
                continue
            generation = min(dispatched, key=lambda e: e["at"]).get("generation")
            times = first_times(attempt_events, generation=generation)
            if DONE_STEP in times and EventType.DISPATCHED.value in times:
                startups.append(times)
    startups.sort(key=lambda times: times[EventType.DISPATCHED.value], reverse=True)
    return startups[:ESTIMATE_SAMPLE]


def current_attempt(db: Any, task: Task) -> tuple[str | None, int | None]:
    """The task's current attempt and generation, from its current lease; (None, None) without one."""
    if not task.current_lease_id:
        return None, None
    snap = db.collection(LEASES).document(task.current_lease_id).get()
    if not snap.exists:
        return None, None
    lease = snap.to_dict() or {}
    attempt_id = lease.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None, None
    try:
        generation = int(lease.get("generation", task.current_generation))
    except (TypeError, ValueError):
        generation = task.current_generation
    return attempt_id, generation


def attempt_steps(db: Any, task: Task, attempt_id: str, generation: int | None) -> dict[str, datetime]:
    """The current attempt's steps, each at its earliest time, at its generation."""
    events = (
        db.collection(TASKS)
        .document(task.id)
        .collection(EVENTS)
        .where(filter=FieldFilter("attempt_id", "==", attempt_id))
        .limit(ATTEMPT_EVENTS_LIMIT)
    )
    return first_times((snap.to_dict() or {} for snap in events.stream()), generation=generation)


def startup_of(db: Any, task: Task, estimates: StartupEstimates) -> dict[str, Any]:
    """The route's answer for one task the caller may read (tenant-checked by the caller)."""
    attempt_id, generation = current_attempt(db, task)
    times = attempt_steps(db, task, attempt_id, generation) if attempt_id else {}
    estimate, sample_size = estimates.estimate(db, task.tenant_id, task.runner_profile)
    return {
        "attempt_id": attempt_id,
        "generation": generation,
        "steps": ordered_steps(times),
        "estimate": estimate,
        "sample_size": sample_size,
        "done": DONE_STEP in times or task.state not in CONCURRENCY_STATES,
    }
