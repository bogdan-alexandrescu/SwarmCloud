"""Round-robin fairness across tenants.

The property: one tenant cannot starve another, no matter how much work it
submits or what priority it claims. Priority is a number the CALLER chooses, so
ordering the queue by priority alone hands the whole platform to whoever types
the biggest number.

These tests are deliberately split. The first half exercises the pure ordering
function, which is where the fairness property actually lives. The second half
runs the real drain loop against the in-memory Firestore and the real
transactional admission path, to prove the ordering survives contact with
admission, dispatch and pool limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_common.models import Task
from swarm_common.states import TaskState

from scheduler.fairness import AgingConfig, aging_bonus, effective_priority, round_robin_order

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
AGING = AgingConfig(interval_seconds=60, step=1, max_bonus=50)


def task(task_id: str, tenant: str, *, priority: int = 0, age_seconds: int = 0) -> Task:
    return Task(
        id=task_id,
        tenant_id=tenant,
        created_at=NOW - timedelta(seconds=age_seconds),
        updated_at=NOW,
        state=TaskState.READY,
        runner_profile="mock",
        resource_class="standard",
        input={},
        submitted_by=f"seed@{tenant}",
        priority=priority,
    )


def tenants_of(tasks) -> list[str]:
    return [t.tenant_id for t in tasks]


# -- the required case: fairness across tenants ---------------------------

def test_a_flood_from_one_tenant_cannot_starve_another():
    flood = [task(f"a{i}", "big", priority=0, age_seconds=100) for i in range(50)]
    small = [task("b0", "small", priority=0, age_seconds=10)]

    order = round_robin_order(flood + small, now=NOW, config=AGING)

    position = tenants_of(order).index("small")
    assert position <= 1, (
        f"the small tenant's only task waited behind {position} tasks from the "
        "flooding tenant; round-robin should have given it the second slot"
    )


def test_priority_cannot_buy_extra_turns():
    """A tenant claiming priority 100 still gets one slot per round."""
    loud = [task(f"l{i}", "loud", priority=100) for i in range(10)]
    quiet = [task(f"q{i}", "quiet", priority=0) for i in range(10)]

    order = tenants_of(round_robin_order(loud + quiet, now=NOW, config=AGING))

    assert order[0] == "loud", "priority should still decide who opens the rotation"
    # ...and then strict alternation, not ten louds in a row.
    assert order[:6] == ["loud", "quiet", "loud", "quiet", "loud", "quiet"]
    assert order.count("loud") == order.count("quiet") == 10


def test_every_tenant_is_served_once_before_anyone_is_served_twice():
    tasks = []
    for tenant, count in (("a", 8), ("b", 4), ("c", 2), ("d", 1)):
        tasks += [task(f"{tenant}{i}", tenant, priority=0) for i in range(count)]

    order = tenants_of(round_robin_order(tasks, now=NOW, config=AGING))

    assert set(order[:4]) == {"a", "b", "c", "d"}
    assert len(set(order[:4])) == 4


def test_rotation_order_is_fixed_and_does_not_re_favour_the_top_tenant():
    """Recomputing the rotation each round would be priority ordering in disguise."""
    high = [task(f"h{i}", "high", priority=90) for i in range(4)]
    low = [task(f"l{i}", "low", priority=0) for i in range(4)]

    order = tenants_of(round_robin_order(high + low, now=NOW, config=AGING))
    assert order == ["high", "low"] * 4


def test_a_tenant_with_nothing_running_opens_the_rotation():
    busy = [task("x0", "busy", priority=50)]
    idle = [task("y0", "idle", priority=0)]

    order = tenants_of(
        round_robin_order(
            busy + idle, now=NOW, config=AGING, active_by_tenant={"busy": 12, "idle": 0}
        )
    )
    assert order[0] == "idle"


def test_ordering_within_a_tenant_is_priority_then_age():
    tasks = [
        task("old-low", "t", priority=0, age_seconds=30),
        task("new-high", "t", priority=10, age_seconds=0),
        task("older-low", "t", priority=0, age_seconds=90),
    ]
    order = [t.id for t in round_robin_order(tasks, now=NOW, config=AGING)]
    assert order[0] == "new-high"
    assert order[1] == "older-low"


def test_ordering_is_deterministic_regardless_of_input_order():
    tasks = [
        task("a0", "a", priority=1),
        task("b0", "b", priority=1),
        task("a1", "a", priority=1),
        task("c0", "c", priority=1),
    ]
    forwards = [t.id for t in round_robin_order(tasks, now=NOW, config=AGING)]
    backwards = [t.id for t in round_robin_order(list(reversed(tasks)), now=NOW, config=AGING)]
    assert forwards == backwards


def test_empty_input_is_handled():
    assert round_robin_order([], now=NOW, config=AGING) == []


# -- starvation aging ------------------------------------------------------

def test_aging_lifts_an_old_low_priority_task_over_a_fresh_one():
    stale = task("stale", "t", priority=0, age_seconds=600)     # +10
    fresh = task("fresh", "t", priority=5, age_seconds=0)
    assert effective_priority(stale, NOW, AGING) == 10
    assert effective_priority(fresh, NOW, AGING) == 5
    assert [t.id for t in round_robin_order([fresh, stale], now=NOW, config=AGING)][0] == "stale"


def test_aging_is_capped_so_it_cannot_invert_priority_forever():
    ancient = task("ancient", "t", priority=0, age_seconds=60 * 60 * 24 * 30)
    assert aging_bonus(ancient, NOW, AGING) == AGING.max_bonus
    urgent = task("urgent", "t", priority=AGING.max_bonus + 1, age_seconds=0)
    assert effective_priority(urgent, NOW, AGING) > effective_priority(ancient, NOW, AGING)


def test_a_task_from_the_future_gets_no_bonus():
    future = task("future", "t", priority=0, age_seconds=-3600)
    assert aging_bonus(future, NOW, AGING) == 0


# -- the same property through the real drain loop -------------------------

def test_drain_loop_alternates_tenants_under_a_global_limit(db, make_scheduler, dispatcher):
    """Six slots, two tenants, one flooding: the small tenant must get served."""
    seed_tenant(db, "big", max_active=100)
    seed_tenant(db, "small", max_active=100)
    seed_pool(db, "global", hard_limit=6)

    for i in range(20):
        seed_task(
            db,
            task_id=f"task_big_{i:02d}",
            tenant_id="big",
            priority=100,
            created_at=NOW - timedelta(seconds=300),
        )
    for i in range(3):
        seed_task(
            db,
            task_id=f"task_small_{i:02d}",
            tenant_id="small",
            priority=0,
            created_at=NOW - timedelta(seconds=10),
        )

    report = make_scheduler().drain()

    assert report.leased == 6, report.to_dict()
    assert report.dispatched == 6
    served = [d["tenant_id"] for d in dispatcher.dispatched]
    assert served.count("small") == 3, (
        f"the flooding tenant took {served.count('big')} of 6 slots and left "
        f"{served.count('small')} for the small tenant: {served}"
    )
    # Everything else is still READY, costing nothing (invariant 1).
    still_ready = [d for d in db.docs.values() if d.get("state") == "READY"]
    assert len(still_ready) == 17


def test_drain_records_blockers_instead_of_stalling(db, make_scheduler, dispatcher):
    """A full pool for one tenant must not stop another tenant's work."""
    seed_tenant(db, "capped", max_active=1)
    seed_tenant(db, "open", max_active=10)
    seed_pool(db, "global", hard_limit=50)

    for i in range(4):
        seed_task(db, task_id=f"task_capped_{i}", tenant_id="capped", priority=50)
    for i in range(3):
        seed_task(db, task_id=f"task_open_{i}", tenant_id="open", priority=0)

    report = make_scheduler().drain()

    served = [d["tenant_id"] for d in dispatcher.dispatched]
    assert served.count("capped") == 1, "the tenant limit must hold"
    assert served.count("open") == 3, "a capped tenant must not block an uncapped one"
    assert report.denied >= 1

    blocked = db.docs["tasks/task_capped_1"]["blocked_by"]
    assert blocked and blocked[0]["reason"] == "TENANT_LIMIT"
    assert db.docs["tasks/task_capped_1"]["state"] == "READY", (
        "a busy platform is not a durable condition; the task must stay READY"
    )


def test_drain_respects_the_admin_pause(db, make_scheduler, dispatcher):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=10)
    seed_task(db, task_id="task_1", tenant_id="eng")
    db.docs["control/dispatch"] = {"dispatch_paused": True, "updated_by": "root@saga.xyz"}

    report = make_scheduler().drain()

    assert report.stop_reason == "dispatch_paused"
    assert report.leased == 0
    assert dispatcher.dispatched == []


def test_drain_is_bounded_by_max_leases(db, make_scheduler, dispatcher):
    seed_tenant(db, "eng", max_active=1000)
    seed_pool(db, "global", hard_limit=1000)
    for i in range(40):
        seed_task(db, task_id=f"task_{i:02d}", tenant_id="eng")

    report = make_scheduler(settings=scheduler_settings(max_leases_per_run=5)).drain()

    assert report.stop_reason == "max_leases"
    assert report.leased == 5
    assert len(dispatcher.dispatched) == 5


def test_lease_is_released_when_dispatch_fails(db, make_scheduler, dispatcher):
    seed_tenant(db, "eng", max_active=5)
    seed_pool(db, "global", hard_limit=5)
    seed_task(db, task_id="task_doomed", tenant_id="eng")
    dispatcher.fail_for = {"task_doomed"}

    report = make_scheduler().drain()

    assert report.dispatch_failures == 1
    # Capacity handed straight back: no slot held by a container that never was.
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["pools/tenant:eng"]["active"] == 0
    assert db.docs["tasks/task_doomed"]["state"] == "READY"
    # ...and backed off, so the run does not spend its whole lease budget
    # re-failing the same dispatch.
    assert db.docs["tasks/task_doomed"]["next_eligible_at"] is not None
    assert report.stop_reason == "no_admissible_work"
    lease_docs = [d for path, d in db.docs.items() if path.startswith("leases/")]
    assert lease_docs and all(d["released_at"] is not None for d in lease_docs)
