"""A merge step waiting on CI holds nothing until CI has an answer.

The owner's pick of 6 November 2026, after the load analysis: 5,303 merge-step
attempts for 439 merges in 7 days, because the scheduler woke every CI_PENDING
park at its fallback instant (the park plus MERGE_CI_FALLBACK_SECONDS) even
while swarm-api's wake tick was reading its checks every CI_READ_SECONDS and
finding them still running -- a lease, a worker start and a release for a
re-check whose answer the control plane already had. Now, while the tick's
own reading of the park's head is fresh, the fallback waits: the park becomes
READY when the tick marks it (green, red with no round left, head moved), or
at the wait's own deadlines (`loop.merge_wait_due`), never once per check.

Driven end to end over the in-memory Firestore: swarm-api's tick
(`POST /v1/admin/merges/wake`, the deployment's scheduler clock, which needs no
webhook) and `Scheduler.drain()` on one clock, with the global pool at 0 so
admission leases nothing and what is measured is the park and its wake.
"""

from __future__ import annotations

from datetime import timedelta

from swarm_common.states import EventType, ParkReason

from scheduler import loop as loop_mod
from swarm_api import issueci, mergewake

from .conftest import seed_pool
from .test_merge_wake import (  # noqa: F401 - the fixtures are used by name
    CHECK,
    HEAD,
    NOW,
    _fix_record,
    _fix_workflows,
    _tick,
    _wait,
    clock,
    fixable_merge,
    github,
    parked_merge,
    tenant_tokens,
    wake_client,
)

TASK = "task_merge"


def _ready_events(db) -> list[dict]:
    return [d for path, d in db.docs.items()
            if path.startswith(f"tasks/{TASK}/events/") and d.get("type") == EventType.READY.value]


def _no_capacity(db) -> None:
    seed_pool(db, "global", hard_limit=0)


def _parked(db) -> bool:
    task = db.docs[f"tasks/{TASK}"]
    return task["state"] == "PARKED" and task["park_reason"] == ParkReason.CI_PENDING.value


def _held_nothing(db) -> None:
    task = db.docs[f"tasks/{TASK}"]
    assert task["current_lease_id"] is None
    assert not [p for p in db.docs if p.startswith("leases/")]
    assert db.docs["pools/global"].get("active", 0) == 0


def _every_minute(db, wake_client, scheduler, clock, minutes: int) -> int:
    """Run the tick and a drain once a minute; the parks promoted in all."""
    promoted = 0
    for _ in range(minutes):
        clock.now += timedelta(minutes=1)
        assert _tick(wake_client).status_code == 200
        promoted += scheduler.drain().promoted_ci_waits
    return promoted


def test_pending_ci_makes_no_attempt_per_check(db, wake_client, github, clock, make_scheduler):
    """An hour of pending CI, read by the tick every minute: no READY, no lease."""
    _no_capacity(db)
    parked_merge(db)
    github.check(HEAD, CHECK, None, status="in_progress")
    scheduler = make_scheduler(now=clock)

    promoted = _every_minute(db, wake_client, scheduler, clock, 60)

    assert promoted == 0
    assert _parked(db)
    assert _ready_events(db) == []
    assert db.docs[f"tasks/{TASK}"]["attempt_count"] == 0
    _held_nothing(db)
    # The tick did read it, and found no answer: that is what held the fallback.
    reading = _wait(db)[mergewake.READING_KEY]
    assert reading["head"] == HEAD and reading["pending"] == [CHECK]


def test_green_ci_makes_the_step_ready_once(db, wake_client, github, clock, make_scheduler):
    _no_capacity(db)
    parked_merge(db)
    github.check(HEAD, CHECK, None, status="in_progress")
    scheduler = make_scheduler(now=clock)
    assert _every_minute(db, wake_client, scheduler, clock, 20) == 0

    github.check(HEAD, CHECK, "success")
    promoted = _every_minute(db, wake_client, scheduler, clock, 10)

    assert promoted == 1
    # Left the CI wait; with no capacity, admission holds it where it would
    # hold any READY task, never on CI_PENDING again.
    assert db.docs[f"tasks/{TASK}"]["park_reason"] != ParkReason.CI_PENDING.value, \
        db.docs[f"tasks/{TASK}"]["park_reason"]
    (ready,) = _ready_events(db)
    assert ready["detail"]["reason"] == "wake_requested"
    assert db.docs[f"tasks/{TASK}"]["attempt_count"] == 0
    _held_nothing(db)


def test_red_ci_starts_a_repair_round_once_and_the_step_stays_parked(
        db, wake_client, github, clock, make_scheduler):
    _no_capacity(db)
    fixable_merge(db, github, rounds=2)
    scheduler = make_scheduler(now=clock)

    promoted = _every_minute(db, wake_client, scheduler, clock, 45)

    assert promoted == 0
    assert _parked(db)
    assert _ready_events(db) == []
    assert len(_fix_workflows(db)) == 1
    assert len(_fix_record(db)["rounds"]) == 1
    _held_nothing(db)


def test_a_silent_tick_still_falls_back_to_the_park_instant(db, make_scheduler, clock):
    """No reading at all -- a dead tick, a token swarm-api cannot use: the
    worker's fallback instant wakes it, as before."""
    _no_capacity(db)
    parked_merge(db)
    clock.now = NOW + timedelta(minutes=15)

    report = make_scheduler(now=clock).drain()

    assert report.promoted_ci_waits == 1
    (ready,) = _ready_events(db)
    assert ready["detail"]["reason"] == "fallback_due"


def test_a_tick_that_falls_silent_hands_back_to_the_fallback(
        db, wake_client, github, clock, make_scheduler):
    _no_capacity(db)
    parked_merge(db)
    github.check(HEAD, CHECK, None, status="in_progress")
    scheduler = make_scheduler(now=clock)
    assert _every_minute(db, wake_client, scheduler, clock, 20) == 0

    clock.now += timedelta(seconds=loop_mod.MERGE_CI_FALLBACK_SECONDS)
    report = scheduler.drain()

    assert report.promoted_ci_waits == 1
    (ready,) = _ready_events(db)
    assert ready["detail"]["reason"] == "fallback_due"


def test_a_wait_the_tick_holds_is_woken_at_its_stuck_deadline(
        db, wake_client, github, clock, make_scheduler):
    """The stuck-wait rules (`merge.ci_stuck`, `merge._ci_wait_age`) are read
    by the worker, so the wait is woken once when the earliest of them is due:
    here the wait began 80 minutes before this park, so CI_STUCK_SECONDS from
    its start comes 10 minutes in -- held to the fallback instant, never
    earlier than the wake it replaces."""
    _no_capacity(db)
    started = NOW - timedelta(minutes=80)
    parked_merge(db, wait_started_at=started, parked_at=NOW, first_parked_at=started)
    github.check(HEAD, CHECK, None, status="in_progress")
    scheduler = make_scheduler(now=clock)

    assert _every_minute(db, wake_client, scheduler, clock, 14) == 0
    assert _every_minute(db, wake_client, scheduler, clock, 1) == 1

    (ready,) = _ready_events(db)
    assert ready["detail"]["reason"] == "wait_deadline"


def test_a_wait_the_tick_holds_is_re_read_by_the_worker_at_least_every_recheck_bound(
        db, wake_client, github, clock, make_scheduler):
    _no_capacity(db)
    parked_merge(db, wait_started_at=NOW, parked_at=NOW, first_parked_at=NOW)
    github.check(HEAD, CHECK, None, status="in_progress")
    scheduler = make_scheduler(now=clock)
    bound = loop_mod.MERGE_CI_RECHECK_SECONDS // 60

    assert _every_minute(db, wake_client, scheduler, clock, bound - 1) == 0
    assert _every_minute(db, wake_client, scheduler, clock, 1) == 1


def test_merge_wait_due_reads_only_a_reading_of_this_park(clock):
    """A reading at another head, or older than the park, answers nothing for
    it; one in the future is not trusted (the document is tenant-writable)."""
    parked = NOW
    fallback = NOW + timedelta(seconds=loop_mod.MERGE_CI_FALLBACK_SECONDS)
    now = fallback + timedelta(seconds=1)

    def wait(**reading):
        return {"head": HEAD, "parked_at": parked, "first_parked_at": parked,
                mergewake.READING_KEY: {"head": HEAD, "read_at": now - timedelta(seconds=30),
                                        **reading}}

    assert loop_mod.merge_wait_due(wait(), fallback, now) is None
    assert loop_mod.merge_wait_due(wait(head="b" * 40), fallback, now) == "fallback_due"
    assert loop_mod.merge_wait_due(
        wait(read_at=parked - timedelta(seconds=1)), fallback, now) == "fallback_due"
    assert loop_mod.merge_wait_due(
        wait(read_at=now + timedelta(minutes=5)), fallback, now) == "fallback_due"
    assert loop_mod.merge_wait_due({}, fallback, now) == "fallback_due"
    assert loop_mod.merge_wait_due({}, fallback, fallback - timedelta(seconds=1)) is None


def test_the_scheduler_restates_the_workers_bounds():
    from agent_worker import merge as merge_mod

    assert loop_mod.MERGE_CI_FALLBACK_SECONDS == merge_mod.MERGE_CI_FALLBACK_SECONDS
    assert loop_mod.MERGE_CI_STUCK_SECONDS == merge_mod.CI_STUCK_SECONDS
    assert loop_mod.MERGE_CI_MAX_SECONDS == merge_mod.MERGE_CI_MAX_SECONDS
    assert loop_mod.MERGE_READING_KEY == mergewake.READING_KEY
    # A reading is fresh while the tick could have read it again many times.
    assert loop_mod.MERGE_CI_FALLBACK_SECONDS > 10 * issueci.CI_READ_SECONDS
