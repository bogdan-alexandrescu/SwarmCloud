"""The scheduler's CI-wait sweep: `Scheduler._promote_ci_waits` (lane MS2).

docs/merge-step.md "Revised 2026-10-06" §1: a merge step waiting for its pull
request's checks is PARKED on CI_PENDING and holds nothing. The scheduler
promotes it -- it never reads GitHub -- in either of two cases:

  * `metadata.merge_wait.wake_requested_at` is set, by swarm-api's wake tick
    (test_merge_wake.py) once the checks have settled;
  * `next_eligible_at` has passed: the worker set it to the park instant
    plus MERGE_CI_FALLBACK_SECONDS, so a dead tick or a broken token never
    strands a merge.

The promotion writes READY alone (invariant 1): no lease, no pool count. A
park on a task that has used its last attempt is dead-lettered, as an
exhausted SCHEDULED_RETRY is, because admission does not check the cap.
Driven through `Scheduler.drain()` over the in-memory Firestore, with the
global pool at 0 so admission leases nothing and what is measured is the
sweep alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_common.states import EventType, ParkReason

from scheduler import loop as loop_mod

from .conftest import seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
TASK = "task_merge"


def at_now() -> datetime:
    return NOW


def world(db) -> None:
    seed_pool(db, "global", hard_limit=0)
    seed_tenant(db, "eng")


def ci_park(db, *, due: datetime = NOW + timedelta(minutes=15), marker=None,
            task_id: str = TASK, **extra) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="PARKED",
                    runner_profile="merge", park_reason=ParkReason.CI_PENDING.value,
                    next_eligible_at=due)
    wait = {"head": "a" * 40, "pull_request": 41, "wakes": 1, "updates": 0,
            "first_parked_at": NOW - timedelta(minutes=5)}
    if marker is not None:
        wait[loop_mod.MERGE_WAKE_MARKER] = marker
    doc.update({"attempt_count": 0, "max_attempts": 10,
                "blocked_by": [{"reason": ParkReason.CI_PENDING.value, "pending": ["ci"]}],
                "metadata": {loop_mod.MERGE_WAIT_METADATA_KEY: wait}, **extra})
    return doc


def _events(db, task_id: str = TASK) -> list[dict]:
    return [d for path, d in db.docs.items()
            if path.startswith(f"tasks/{task_id}/events/")]


def test_the_wake_marker_promotes_the_park_to_ready_alone(db, make_scheduler):
    world(db)
    ci_park(db, marker=NOW - timedelta(seconds=10))

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_ci_waits == 1
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "READY"
    assert task["park_reason"] is None and task["blocked_by"] == []
    # READY alone: no lease was taken, nothing was counted (invariant 1).
    assert task["current_lease_id"] is None
    assert task["attempt_count"] == 0
    assert not [p for p in db.docs if p.startswith("leases/")]
    assert db.docs["pools/global"].get("active", 0) == 0
    ready = [e for e in _events(db) if e.get("type") == EventType.READY.value]
    assert ready and ready[-1]["detail"]["reason"] == "wake_requested"


def test_the_fallback_instant_promotes_a_park_the_tick_never_marked(db, make_scheduler):
    world(db)
    ci_park(db, due=NOW - timedelta(seconds=1))

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_ci_waits == 1
    assert db.docs[f"tasks/{TASK}"]["state"] == "READY"
    ready = [e for e in _events(db) if e.get("type") == EventType.READY.value]
    assert ready[-1]["detail"]["reason"] == "fallback_due"


def test_neither_marker_nor_fallback_leaves_the_park(db, make_scheduler):
    """The control: the same park, unmarked and not yet due, stays PARKED."""
    world(db)
    ci_park(db)

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_ci_waits == 0
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "PARKED" and task["park_reason"] == ParkReason.CI_PENDING.value


def test_an_exhausted_park_is_dead_lettered_not_promoted(db, make_scheduler):
    """Past MERGE_CI_MAX_WAKES a wake counts; on the last attempt READY would
    lease one attempt too many, because admission does not check the cap."""
    world(db)
    ci_park(db, marker=NOW, attempt_count=10, max_attempts=10)

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_ci_waits == 0
    assert report.dead_lettered == 1
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "DEAD_LETTERED"
    assert task["current_lease_id"] is None


def test_an_exhausted_park_with_a_cancel_requested_is_cancelled(db, make_scheduler):
    world(db)
    ci_park(db, attempt_count=10, max_attempts=10, cancel_requested=True)

    make_scheduler(now=at_now).drain()

    assert db.docs[f"tasks/{TASK}"]["state"] == "CANCELLED"


def test_other_park_reasons_are_not_this_sweeps(db, make_scheduler):
    world(db)
    ci_park(db, marker=NOW)
    db.docs[f"tasks/{TASK}"]["park_reason"] = ParkReason.BUDGET_EXHAUSTED.value

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_ci_waits == 0
    assert db.docs[f"tasks/{TASK}"]["state"] == "PARKED"
