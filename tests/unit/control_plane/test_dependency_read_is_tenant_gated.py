"""The scheduler reads a task's parents through the tenant gate (#453 box 86).

The worker re-checks a step's parents before it runs anything
(`ControlPlane.fetch_parent_states`): a parent document of another tenant is
refused, and a `state` the contract does not name is "not SUCCEEDED". The
reconciler's promotion rule (`detect._dependencies_met`) treats another
tenant's parent as unsatisfied too. The scheduler's `parent_ends` did
neither: it read every parent id it was given, whoever's, and raised on an
unknown state.

So the two halves of the dependency rule disagreed, and a child whose parent's
task document had been rewritten -- a `tenant_id` forged onto a SUCCEEDED
parent -- was promoted by the scheduler, leased, refused by the worker, and
promoted again: promote -> lease -> park, every drain. And reading another
tenant's parent was a cross-tenant read in its own right: its FAILED state
cancelled this tenant's child and its `end_cause` was copied onto it
(invariant 9).

What is held here, on both of the scheduler's dependency paths (the sweep of
DEPENDENCY_INCOMPLETE parks and admission of a READY step):

    another tenant's SUCCEEDED parent does not promote or admit the child
    another tenant's FAILED parent does not cancel the child, and nothing of it is copied
    a parent with a state the contract does not name is "not SUCCEEDED", not a crash
    the same tenant's SUCCEEDED parent still promotes (the control)
"""

from __future__ import annotations

from .conftest import seed_pool, seed_task, seed_tenant


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_tenant(db, "research", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _parked_child(db, task_id: str = "task_child", parent: str = "task_parent") -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=(parent,),
    )


def test_the_same_tenants_succeeded_parent_promotes_the_child(db, make_scheduler, dispatcher):
    # The control: the refusals below are the tenant gate, not a fixture that
    # promotes nothing.
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _parked_child(db)

    report = make_scheduler().drain()

    assert report.promoted_dependencies == 1, report.to_dict()
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_child"]


def test_another_tenants_succeeded_parent_does_not_promote_the_child(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="research", state="SUCCEEDED")
    _parked_child(db)

    report = make_scheduler().drain()

    assert report.promoted_dependencies == 0, report.to_dict()
    assert report.leased == 0, report.to_dict()
    child = db.docs["tasks/task_child"]
    assert (child["state"], child["park_reason"]) == ("PARKED", "DEPENDENCY_INCOMPLETE")
    assert dispatcher.dispatched == []
    # Nothing leased, so no capacity was taken (invariant 1).
    assert db.docs["pools/global"]["active"] == 0


def test_another_tenants_failed_parent_does_not_cancel_the_child(db, make_scheduler, dispatcher):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="research", state="FAILED")
    db.docs["tasks/task_parent"]["end_cause"] = "research-only-cause"
    _parked_child(db)

    make_scheduler().drain()

    child = db.docs["tasks/task_child"]
    assert child["state"] == "PARKED", child
    assert "research-only-cause" not in repr(child)


def test_a_ready_step_with_another_tenants_succeeded_parent_is_parked_not_leased(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="research", state="SUCCEEDED")
    seed_task(db, task_id="task_child", tenant_id="eng", state="READY", depends_on=("task_parent",))

    report = make_scheduler().drain()

    assert report.leased == 0, report.to_dict()
    child = db.docs["tasks/task_child"]
    assert (child["state"], child["park_reason"]) == ("PARKED", "DEPENDENCY_INCOMPLETE")
    assert dispatcher.dispatched == []


def test_a_parent_with_an_unknown_state_is_not_succeeded_and_breaks_nothing(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_odd", tenant_id="eng", state="SUCCEEDED")
    db.docs["tasks/task_odd"]["state"] = "NOT_A_STATE"
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _parked_child(db, "task_child_odd", parent="task_odd")
    _parked_child(db)

    report = make_scheduler().drain()

    # The child of the unreadable parent waits; the sweep goes on past it.
    assert db.docs["tasks/task_child_odd"]["state"] == "PARKED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_child"]
    assert report.promoted_dependencies == 1, report.to_dict()
