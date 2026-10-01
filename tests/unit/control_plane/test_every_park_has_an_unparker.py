"""Every park reason something writes has something that ends it.

THE GAP (functionality wave 1, D3). docs/quota-management.md section 4 lists
what ends each park reason. Two of them had no code behind the table:

  * SCHEDULED_RETRY. The worker writes it on SIGTERM
    (`lifecycle._handle_interruption`) with `next_eligible_at=now`. The
    scheduler swept DEPENDENCY_INCOMPLETE, CREDENTIAL_MISSING and the three
    PROVIDER_* reasons, `ready_tasks` reads READY only and the reconciler has
    no promoter, so an interrupted task -- and the workflow it belonged to --
    sat PARKED for ever.
  * MANUAL_PAUSE. The drain writes it for a tenant that is missing or
    disabled and for a profile the catalogue no longer has. Nothing ever read
    it back, so re-enabling the tenant did not start its work.

BUDGET_EXHAUSTED is the third reason with no un-parker, and it stays that way
on purpose: the owner decided on 2026-10-01 there are no budgets, so nothing
writes it. The enum is frozen and keeps the value; the test at the bottom
holds that no sweep reads it.

Every promotion here is the same guarded transition the other sweeps use
(`SchedulerStore.promote_to_ready`): PARKED -> READY, re-read in a transaction,
skipped when the task or its park reason has moved on. READY costs nothing
(invariant 1); only admission, later, can take a lease.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_common.states import ParkReason

from scheduler.store import SchedulerStore

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant

TENANT = "eng"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def at_now() -> datetime:
    return NOW


def world(db, *, global_limit: int = 10) -> None:
    seed_pool(db, "global", hard_limit=global_limit)
    seed_tenant(db, TENANT)


def ready_events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [
        doc for path, doc in sorted(db.docs.items())
        if path.startswith(prefix) and doc.get("type") == "ready"
    ]


def lease_docs(db) -> list[str]:
    return [path for path in db.docs if path.startswith("leases/")]


def pool_actives(db) -> dict[str, int]:
    return {
        path: doc.get("active")
        for path, doc in db.docs.items()
        if path.startswith("pools/")
    }


# --------------------------------------------------------------------------
# SCHEDULED_RETRY
# --------------------------------------------------------------------------


def test_a_scheduled_retry_park_past_its_time_is_promoted_exactly_once(db, make_scheduler):
    # Global at 0 so admission refuses after the promotion: what is measured is
    # the promotion alone, and the task stays READY between the two drains.
    world(db, global_limit=0)
    seed_task(
        db,
        task_id="task_retry",
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.SCHEDULED_RETRY.value,
        next_eligible_at=NOW - timedelta(seconds=1),
    )
    scheduler = make_scheduler(now=at_now)

    first = scheduler.drain()
    second = scheduler.drain()

    doc = db.docs["tasks/task_retry"]
    assert doc["state"] == "READY", doc
    assert doc["park_reason"] is None
    assert doc["next_eligible_at"] is None
    assert first.promoted_scheduled_retries == 1
    assert second.promoted_scheduled_retries == 0
    events = ready_events(db, "task_retry")
    assert len(events) == 1, events
    assert events[0]["detail"]["reason"] == "retry_due"


def test_a_scheduled_retry_park_is_promoted_once_when_two_drains_race(db, make_scheduler):
    """Both schedulers list the park; the one that writes second finds it READY."""
    world(db, global_limit=0)
    seed_task(
        db,
        task_id="task_race",
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.SCHEDULED_RETRY.value,
        next_eligible_at=NOW - timedelta(minutes=5),
    )
    stale = SchedulerStore(db).parked_page(ParkReason.SCHEDULED_RETRY, 10, None)

    winner = make_scheduler(now=at_now).drain()
    loser_scheduler = make_scheduler(now=at_now)
    loser_scheduler.store.parked_page = lambda reason, limit, after: (
        list(stale) if reason is ParkReason.SCHEDULED_RETRY else []
    )
    loser = loser_scheduler.drain()

    assert winner.promoted_scheduled_retries == 1
    assert loser.promoted_scheduled_retries == 0
    assert loser.stale_writes >= 1
    assert len(ready_events(db, "task_race")) == 1


def test_a_scheduled_retry_park_in_the_future_is_not_promoted(db, make_scheduler):
    world(db)
    seed_task(
        db,
        task_id="task_later",
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.SCHEDULED_RETRY.value,
        next_eligible_at=NOW + timedelta(minutes=10),
    )

    report = make_scheduler(now=at_now).drain()

    doc = db.docs["tasks/task_later"]
    assert doc["state"] == "PARKED"
    assert doc["park_reason"] == ParkReason.SCHEDULED_RETRY.value
    assert report.promoted_scheduled_retries == 0
    assert ready_events(db, "task_later") == []


def test_promoting_a_scheduled_retry_never_touches_a_lease(db, make_scheduler):
    """Invariant 1: the promotion creates no lease and moves no pool count."""
    world(db, global_limit=0)
    seed_pool(db, "resource:standard", hard_limit=4, active=1)
    db.docs["leases/lease_other"] = {"lease_id": "lease_other", "state": "RUNNING", "units": 1}
    seed_task(
        db,
        task_id="task_free",
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.SCHEDULED_RETRY.value,
        next_eligible_at=NOW,
    )
    before_pools = pool_actives(db)
    before_leases = {p: dict(db.docs[p]) for p in lease_docs(db)}

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_scheduled_retries == 1
    assert db.docs["tasks/task_free"]["state"] == "READY"
    assert db.docs["tasks/task_free"]["current_lease_id"] is None
    assert pool_actives(db) == before_pools
    assert {p: db.docs[p] for p in lease_docs(db)} == before_leases


def test_an_interrupted_last_attempt_is_dead_lettered_not_retried(db, make_scheduler):
    """Admission does not check the retry cap, so READY would lease attempt 4 of 3."""
    world(db)
    seed_task(
        db,
        task_id="task_spent",
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.SCHEDULED_RETRY.value,
        next_eligible_at=NOW - timedelta(seconds=1),
    )
    db.docs["tasks/task_spent"]["attempt_count"] = 3
    db.docs["tasks/task_spent"]["max_attempts"] = 3

    report = make_scheduler(now=at_now).drain()

    doc = db.docs["tasks/task_spent"]
    assert doc["state"] == "DEAD_LETTERED", doc
    assert doc["completed_at"] is not None
    assert doc["next_eligible_at"] is None
    assert "3 of 3" in doc["last_error"]
    assert report.promoted_scheduled_retries == 0
    assert report.dead_lettered == 1
    assert lease_docs(db) == []


# --------------------------------------------------------------------------
# MANUAL_PAUSE
# --------------------------------------------------------------------------


def manual_pause(db, task_id: str = "task_paused") -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.MANUAL_PAUSE.value,
    )


def test_a_manual_pause_park_waits_while_its_pool_is_disabled(db, make_scheduler):
    world(db)
    seed_pool(db, f"tenant:{TENANT}", hard_limit=20, enabled=False)
    manual_pause(db)

    report = make_scheduler(now=at_now).drain()

    assert db.docs["tasks/task_paused"]["state"] == "PARKED"
    assert report.promoted_manual_pauses == 0


def test_a_manual_pause_park_is_promoted_when_its_pool_is_enabled_again(db, make_scheduler):
    world(db, global_limit=0)
    seed_pool(db, f"tenant:{TENANT}", hard_limit=20, enabled=False)
    manual_pause(db)
    scheduler = make_scheduler(now=at_now)

    before = scheduler.drain()
    db.docs[f"pools/tenant:{TENANT}"]["enabled"] = True
    after = scheduler.drain()

    assert before.promoted_manual_pauses == 0
    assert after.promoted_manual_pauses == 1
    assert db.docs["tasks/task_paused"]["state"] == "READY"
    assert lease_docs(db) == []


def test_a_pool_with_no_enabled_field_is_not_a_paused_pool(db, make_scheduler):
    """Admission reads a missing `enabled` as on; so does this sweep."""
    world(db, global_limit=0)
    del db.docs["pools/global"]["enabled"]
    manual_pause(db)

    report = make_scheduler(now=at_now).drain()

    assert report.promoted_manual_pauses == 1


def test_a_manual_pause_park_waits_while_its_tenant_is_disabled(db, make_scheduler):
    world(db, global_limit=0)
    db.docs[f"tenants/{TENANT}"]["enabled"] = False
    manual_pause(db)
    scheduler = make_scheduler(now=at_now)

    before = scheduler.drain()
    db.docs[f"tenants/{TENANT}"]["enabled"] = True
    after = scheduler.drain()

    assert before.promoted_manual_pauses == 0
    assert after.promoted_manual_pauses == 1
    assert db.docs["tasks/task_paused"]["state"] == "READY"


def test_a_manual_pause_park_on_a_profile_the_catalogue_lacks_stays_parked(db, make_scheduler):
    world(db)
    seed_task(
        db,
        task_id="task_gone",
        tenant_id=TENANT,
        state="PARKED",
        runner_profile="no-such-profile",
        park_reason=ParkReason.MANUAL_PAUSE.value,
    )

    report = make_scheduler(now=at_now).drain()

    assert db.docs["tasks/task_gone"]["state"] == "PARKED"
    assert report.promoted_manual_pauses == 0


# --------------------------------------------------------------------------
# The window moves: parks that must stay parked never hide one that is due
# --------------------------------------------------------------------------

SWEEP = 3


def test_a_due_retry_behind_a_full_window_of_future_retries_is_promoted(db, make_scheduler):
    """SWEEP+1 retries not yet due sort ahead of the due one; it is still reached."""
    world(db, global_limit=0)
    for i in range(SWEEP + 1):
        seed_task(
            db,
            task_id=f"task_a{i}",
            tenant_id=TENANT,
            state="PARKED",
            park_reason=ParkReason.SCHEDULED_RETRY.value,
            next_eligible_at=NOW + timedelta(hours=1),
        )
    seed_task(
        db,
        task_id="task_z_due",
        tenant_id=TENANT,
        state="PARKED",
        park_reason=ParkReason.SCHEDULED_RETRY.value,
        next_eligible_at=NOW - timedelta(seconds=1),
    )
    scheduler = make_scheduler(
        now=at_now, settings=scheduler_settings(dependency_sweep_size=SWEEP)
    )

    reports = [scheduler.drain() for _ in range(2)]

    assert db.docs["tasks/task_z_due"]["state"] == "READY"
    assert sum(r.promoted_scheduled_retries for r in reports) == 1
    for i in range(SWEEP + 1):
        assert db.docs[f"tasks/task_a{i}"]["state"] == "PARKED"
    assert len(ready_events(db, "task_z_due")) == 1


def test_a_re_enabled_tenants_pause_behind_a_full_window_of_disabled_ones_is_promoted(
    db, make_scheduler
):
    world(db, global_limit=0)
    seed_tenant(db, "aaa_off")
    db.docs["tenants/aaa_off"]["enabled"] = False
    for i in range(SWEEP + 1):
        seed_task(
            db,
            task_id=f"task_a{i}",
            tenant_id="aaa_off",
            state="PARKED",
            park_reason=ParkReason.MANUAL_PAUSE.value,
        )
    manual_pause(db, "task_z_paused")
    scheduler = make_scheduler(
        now=at_now, settings=scheduler_settings(dependency_sweep_size=SWEEP)
    )

    reports = [scheduler.drain() for _ in range(2)]

    assert db.docs["tasks/task_z_paused"]["state"] == "READY"
    assert sum(r.promoted_manual_pauses for r in reports) == 1
    for i in range(SWEEP + 1):
        assert db.docs[f"tasks/task_a{i}"]["state"] == "PARKED"


def test_the_window_returns_to_the_top_after_a_short_page(db, make_scheduler):
    """A park the cursor has passed is reached again once the window wraps."""
    world(db, global_limit=0)
    for i in range(SWEEP):
        seed_task(
            db,
            task_id=f"task_a{i}",
            tenant_id=TENANT,
            state="PARKED",
            park_reason=ParkReason.SCHEDULED_RETRY.value,
            next_eligible_at=NOW + timedelta(hours=1),
        )
    scheduler = make_scheduler(
        now=at_now, settings=scheduler_settings(dependency_sweep_size=SWEEP)
    )
    scheduler.drain()  # full page: the cursor now sits after task_a2
    scheduler.drain()  # short (empty) page: back to the top
    db.docs["tasks/task_a0"]["next_eligible_at"] = NOW - timedelta(seconds=1)

    report = scheduler.drain()

    assert report.promoted_scheduled_retries == 1
    assert db.docs["tasks/task_a0"]["state"] == "READY"


# --------------------------------------------------------------------------
# BUDGET_EXHAUSTED
# --------------------------------------------------------------------------


def test_no_sweep_reads_budget_exhausted(db, make_scheduler):
    """No budgets exist (owner, 2026-10-01): a sweep keyed on it would be dead code."""
    world(db)
    scheduler = make_scheduler(now=at_now, settings=scheduler_settings())
    asked: list[ParkReason] = []
    real_tasks = scheduler.store.parked_tasks
    real_page = scheduler.store.parked_page

    def parked_tasks(reason, limit):
        asked.append(reason)
        return real_tasks(reason, limit)

    def parked_page(reason, limit, after):
        asked.append(reason)
        return real_page(reason, limit, after)

    scheduler.store.parked_tasks = parked_tasks
    scheduler.store.parked_page = parked_page
    scheduler.drain()

    assert ParkReason.SCHEDULED_RETRY in asked
    assert ParkReason.MANUAL_PAUSE in asked
    assert ParkReason.BUDGET_EXHAUSTED not in asked
