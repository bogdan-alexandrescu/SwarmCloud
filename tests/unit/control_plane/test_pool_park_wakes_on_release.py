"""A step parked on a full account pool wakes when an account is released (#3042).

Measured on 2026-11-02 (the issue's last comment): three real steps in seven
days found no free pool account and parked for up to five minutes
(`STALE_READING_RETRY_SECONDS`), or until the prewarm sweep reached them two
minutes before that, even when another agent gave its account back sooner.
The finish event (#1885) woke them only when the agent that let go had ENDED
its task; an attempt that parks, or fails back to READY, releases its account
and rings no `task_finished`.

Every release, whatever ends the hold, closes the hold's record in
`account_holds` with `released_at` (`quota_broker.accounts.hold_log_entry`).
Each scheduler pass reads the records closed since the last one and wakes the
pool parks the freed accounts can serve, so the wake reaches the step within
one pass of the release. What is held here:

    two steps parked for a full pool and one released account wake exactly the oldest one
    ... and one release wakes both when the account has room for two
    ... and the finish event wakes the oldest one too
    a paused pool's park is not woken
    another tenant's release wakes nothing here
    a wake that races the fallback does not dispatch twice
    ... nor does a finish event's wake followed by the pass that reads the same release
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quota_broker.accountstore import AccountStore
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import measure, scheduler_settings, seed_pool, seed_task, seed_tenant

BROKER = "https://swarm-quota-broker.example.run.app"
OWNER = "eng"
STRANGER = "research"
NOW = datetime.now(timezone.utc)
#: Outside the prewarm horizon, so only the wake can promote a park.
LATER = NOW + timedelta(minutes=5)


def _world(db) -> None:
    seed_pool(db, "global", hard_limit=10)
    for tenant in (OWNER, STRANGER):
        seed_tenant(db, tenant, credentials=("anthropic",), max_active=10)


def _released(db, assignment: str, *, account_id: str, tenant_id: str, task_id: str,
              ago: timedelta = timedelta(seconds=10)) -> None:
    """The broker's closed hold record: an agent gave `account_id` back."""
    db.docs[f"account_holds/{assignment}"] = {
        "account_id": account_id,
        "tenant_id": tenant_id,
        "task_id": task_id,
        "attempt_id": "att_1",
        "assigned_at": NOW - timedelta(minutes=30),
        "released_at": NOW - ago,
        "end": "released",
    }


def _pool_park(db, task_id: str, tenant_id: str, *, reason: str = "accounts_full",
               parked_at: datetime, next_eligible_at: datetime = LATER) -> None:
    profile = RUNNER_PROFILES["claude-code"]
    seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant_id,
        state="PARKED",
        park_reason="PROVIDER_QUOTA_EXHAUSTED",
        runner_profile=profile.name,
        resource_class=profile.resource_class,
        provider=profile.provider,
        created_at=parked_at,
        next_eligible_at=next_eligible_at,
    )
    db.docs[f"tasks/{task_id}"]["blocked_by"] = [
        {
            "reason": "PROVIDER_QUOTA_EXHAUSTED",
            "provider": profile.provider,
            "account_pool_reason": reason,
            "park_phase": "account_assign",
        }
    ]


def _scheduler(make_scheduler):
    return make_scheduler(settings=scheduler_settings(quota_broker_url=BROKER))


def _two_full_parks(db) -> None:
    # The OLDER park has the LATER document id: the store pages in id order,
    # so a wake that takes the first one it reads wakes the newer one.
    _pool_park(db, "task_z_old", OWNER, parked_at=NOW - timedelta(minutes=4))
    _pool_park(db, "task_a_new", OWNER, parked_at=NOW - timedelta(minutes=1))


def test_one_released_account_wakes_exactly_the_oldest_of_two_full_pool_parks(
    db, make_scheduler, dispatcher
):
    _world(db)
    AccountStore(db).register(OWNER, "team")
    _two_full_parks(db)
    # Given back by an attempt that PARKED: no `task_finished` was rung.
    seed_task(db, task_id="task_gave_back", tenant_id=OWNER, state="PARKED",
              park_reason="HUMAN_REQUIRED")
    _released(db, "asg_1", account_id=f"{OWNER}:team", tenant_id=OWNER,
              task_id="task_gave_back")

    report = _scheduler(make_scheduler).drain()

    # MUTATION: a pass that does not read released holds leaves both PARKED
    # until their five-minute fallback.
    assert db.docs["tasks/task_z_old"]["state"] != "PARKED", report.to_dict()
    # MUTATION: waking in document-id order wakes the newer park instead.
    assert db.docs["tasks/task_a_new"]["state"] == "PARKED"
    assert report.promoted_account_waits == 1, report.to_dict()
    # Woken into the ordinary admission pass, which took the lease (invariant 2).
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_z_old"]


def test_one_release_wakes_both_parks_when_the_account_has_room_for_two(
    db, make_scheduler, dispatcher
):
    _world(db)
    store = AccountStore(db)
    store.register(OWNER, "team")
    store.set_max_agents(f"{OWNER}:team", 2)
    measure(store, f"{OWNER}:team")
    _two_full_parks(db)
    seed_task(db, task_id="task_gave_back", tenant_id=OWNER, state="SUCCEEDED")
    _released(db, "asg_1", account_id=f"{OWNER}:team", tenant_id=OWNER,
              task_id="task_gave_back")

    report = _scheduler(make_scheduler).drain()

    # BGD's pick on PR #3044, "free slots only": a release wakes as many parks
    # as the pool has free slots, each gated by `choose()` and a claim.
    # MUTATION: a one-park-per-release budget leaves the newer one PARKED.
    assert db.docs["tasks/task_z_old"]["state"] != "PARKED", report.to_dict()
    assert db.docs["tasks/task_a_new"]["state"] != "PARKED", report.to_dict()
    assert report.promoted_account_waits == 2, report.to_dict()


def test_the_finish_event_wakes_the_oldest_full_pool_park(db, make_scheduler, dispatcher):
    _world(db)
    AccountStore(db).register(OWNER, "team")
    _two_full_parks(db)
    seed_task(db, task_id="task_done", tenant_id=OWNER, state="SUCCEEDED")
    _released(db, "asg_done", account_id=f"{OWNER}:team", tenant_id=OWNER,
              task_id="task_done")

    _scheduler(make_scheduler).release_dependants("task_done")

    # MUTATION: the finish event's wake in document-id order wakes the newer.
    assert db.docs["tasks/task_z_old"]["state"] != "PARKED"
    assert db.docs["tasks/task_a_new"]["state"] == "PARKED"


def test_a_paused_pools_park_is_not_woken(db, make_scheduler, dispatcher):
    """Waiting on a person: an account coming free says nothing about that."""
    _world(db)
    AccountStore(db).register(OWNER, "team")
    _pool_park(db, "task_paused", OWNER, reason="pool_paused",
               parked_at=NOW - timedelta(minutes=4))
    seed_task(db, task_id="task_done", tenant_id=OWNER, state="SUCCEEDED")
    _released(db, "asg_done", account_id=f"{OWNER}:team", tenant_id=OWNER,
              task_id="task_done")

    scheduler = _scheduler(make_scheduler)
    finish = scheduler.release_dependants("task_done")
    drained = scheduler.drain()

    # MUTATION: dropping the park-reason filter wakes it -- the account is
    # free now, so `choose()` alone says yes.
    assert db.docs["tasks/task_paused"]["state"] == "PARKED"
    assert finish.promoted_account_waits == 0
    assert drained.promoted_account_waits == 0
    assert dispatcher.dispatched == []
    # Invariant 1: nothing was reserved for a task that stayed parked.
    assert db.docs["pools/global"]["active"] == 0


def test_another_tenants_release_wakes_nothing_here(db, make_scheduler, dispatcher):
    _world(db)
    store = AccountStore(db)
    # The owner's own account has room: only WHICH account was released
    # keeps this park where it is.
    store.register(OWNER, "team")
    store.register(STRANGER, "solo")
    _pool_park(db, "task_owner", OWNER, parked_at=NOW - timedelta(minutes=4))
    seed_task(db, task_id="task_theirs", tenant_id=STRANGER, state="SUCCEEDED")
    _released(db, "asg_theirs", account_id=f"{STRANGER}:solo", tenant_id=STRANGER,
              task_id="task_theirs")

    report = _scheduler(make_scheduler).drain()

    # MUTATION: waking every pool park `choose()` would serve, whichever
    # account came free, wakes it.
    assert db.docs["tasks/task_owner"]["state"] == "PARKED", report.to_dict()
    assert report.promoted_account_waits == 0
    assert dispatcher.dispatched == []


def test_a_wake_that_races_the_fallback_does_not_dispatch_twice(
    db, make_scheduler, dispatcher
):
    """The park's own instant has passed (the prewarm sweep promotes it) AND an
    account was released for it (the wake promotes it): one lease, one dispatch."""
    _world(db)
    AccountStore(db).register(OWNER, "team")
    _pool_park(db, "task_due", OWNER, parked_at=NOW - timedelta(minutes=6),
               next_eligible_at=NOW - timedelta(seconds=1))
    seed_task(db, task_id="task_gave_back", tenant_id=OWNER, state="SUCCEEDED")
    _released(db, "asg_1", account_id=f"{OWNER}:team", tenant_id=OWNER,
              task_id="task_gave_back")

    scheduler = _scheduler(make_scheduler)
    scheduler.drain()
    scheduler.drain()

    # Each promotion is the guarded PARKED -> READY write, and only a READY
    # task is leased: whichever of the two gets there second writes nothing.
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_due"]
    assert db.docs["pools/global"]["active"] == 1


def test_a_finish_wake_and_the_pass_after_it_do_not_dispatch_twice(
    db, make_scheduler, dispatcher
):
    _world(db)
    AccountStore(db).register(OWNER, "team")
    _two_full_parks(db)
    seed_task(db, task_id="task_done", tenant_id=OWNER, state="SUCCEEDED")
    _released(db, "asg_done", account_id=f"{OWNER}:team", tenant_id=OWNER,
              task_id="task_done")

    scheduler = _scheduler(make_scheduler)
    scheduler.release_dependants("task_done")
    report = scheduler.drain()

    # MUTATION: the pass counting the release the finish event already spent
    # wakes the second park for the same freed slot.
    assert db.docs["tasks/task_a_new"]["state"] == "PARKED", report.to_dict()
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_z_old"]
