"""The scheduler's park writer names the budget a BUDGET_EXHAUSTED park waits on (WAIT-RECORD; part of #1782).

NOTHING PARKS BUDGET_EXHAUSTED TODAY: there are no per-tenant budgets, built
or planned (owner decision, 2026-10-01; `test_every_park_has_an_unparker`
holds that no sweep reads it). The owner's 2026-10-21 decision is that such
a park records `budget`, by name, so the "Why it's waiting" panel can say
which. The scheduler's parks all go through one writer (`Scheduler._park`),
so the field is required THERE: a future BUDGET_EXHAUSTED park that does
not name its budget is refused before anything is written, rather than
landing as a park the panel can only call "Budget exhausted".

Offline: in-memory Firestore. Each assertion names the mutation it catches.
"""

from __future__ import annotations

import pytest

from swarm_common.states import ParkReason

from scheduler.loop import DrainReport

from .conftest import seed_task, seed_tenant

TENANT = "eng"


def _parked_events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for path, doc in sorted(db.docs.items())
            if path.startswith(prefix) and doc.get("type") == "parked"]


@pytest.mark.parametrize("detail", [{}, {"budget": ""}, {"budget": None}, {"budget": 5}])
def test_a_budget_park_that_names_no_budget_is_refused_and_writes_nothing(
    db, make_scheduler, detail
):
    seed_tenant(db, TENANT)
    seed_task(db, task_id="task_b", tenant_id=TENANT)
    scheduler = make_scheduler()
    task = scheduler.store.get_task("task_b")

    # MUTATION: `_park` writes a BUDGET_EXHAUSTED park whatever its detail -> no raise.
    with pytest.raises(ValueError, match="budget"):
        scheduler._park(task, ParkReason.BUDGET_EXHAUSTED, DrainReport(), detail=detail)

    # MUTATION: the check runs after the write -> the task is PARKED -> fails.
    assert db.docs["tasks/task_b"]["state"] == "READY"
    assert _parked_events(db, "task_b") == []


def test_a_budget_park_records_its_budget_by_name(db, make_scheduler):
    """The control: the same park, naming its budget, is written with it."""
    seed_tenant(db, TENANT)
    seed_task(db, task_id="task_b", tenant_id=TENANT)
    scheduler = make_scheduler()
    task = scheduler.store.get_task("task_b")

    scheduler._park(task, ParkReason.BUDGET_EXHAUSTED, DrainReport(),
                    detail={"budget": "monthly_spend"})

    assert db.docs["tasks/task_b"]["park_reason"] == ParkReason.BUDGET_EXHAUSTED.value
    (event,) = _parked_events(db, "task_b")
    # MUTATION: the guard strips or renames the field -> fails.
    assert event["detail"]["budget"] == "monthly_spend"


def test_every_other_park_is_written_as_before(db, make_scheduler):
    """The guard is BUDGET_EXHAUSTED's alone: a MANUAL_PAUSE park needs no budget."""
    seed_tenant(db, TENANT)
    seed_task(db, task_id="task_p", tenant_id=TENANT)
    scheduler = make_scheduler()
    task = scheduler.store.get_task("task_p")

    scheduler._park(task, ParkReason.MANUAL_PAUSE, DrainReport(),
                    detail={"error": "tenant is missing or disabled"})

    (event,) = _parked_events(db, "task_p")
    # MUTATION: the guard applies to every reason -> raises -> fails.
    # MUTATION: the guard adds a `budget` key to other parks -> fails.
    assert event["detail"] == {"reason": ParkReason.MANUAL_PAUSE.value,
                               "error": "tenant is missing or disabled"}
