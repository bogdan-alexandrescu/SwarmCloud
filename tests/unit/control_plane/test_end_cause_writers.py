"""Every terminal writer in the scheduler and the API records why the task ended.

Contract request 23, accepted by the owner on 2026-09-25 (#185, decision 9):
`Task.end_cause` is written BESIDE `completed_at` by whichever component ends
the task, so the outcome ledger never again has to read a writer's words to
learn why. The worker's writes are held by tests/unit/worker/
test_end_cause_worker.py and the reconciler's by tests/unit/worker/
test_end_cause_reconciler.py; this file holds the other two:

  * the SCHEDULER, which ends a task four ways: a cancel requested before
    admission, a dependant of a parent that did not succeed (split into
    `failed_parent` and `cancelled_parent`, decision 2), the fail_workflow
    sweep, and a dispatch that kept failing (or was cancelled meanwhile);
  * the API, whose cancel ends a task only when that task holds no capacity.

Each case drives the shipped code over the in-memory Firestore and reads the
task document the write left, so nothing here restates a writer.

WRITTEN TO FAIL ON THE CODE BEFORE THE CHANGE, on its assertions: nothing new is
imported, and the document simply had no `end_cause` to read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task
from swarm_common.states import TaskState

from .conftest import auth_header, seed_pool, seed_task, seed_tenant
from .test_on_step_failure import (
    CONTINUE,
    FAIL,
    FIVE_STEPS,
    step,
    submit,
    worker_finished,
    worker_is_running,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _world(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _doc(db, task_id: str) -> dict:
    return db.docs[f"tasks/{task_id}"]


def _ended(db, task_id: str, state: str, cause: str) -> None:
    doc = _doc(db, task_id)
    assert doc["state"] == state, (task_id, doc["state"])
    assert doc["completed_at"] is not None, task_id
    assert doc.get("end_cause") == cause, (
        f"{task_id} ended {state} with end_cause={doc.get('end_cause')!r}; "
        f"its writer knew it was {cause!r}"
    )


# --------------------------------------------------------------------------
# The scheduler: a dependant of a parent that did not succeed
# --------------------------------------------------------------------------

def _parent(db, task_id: str, state: str, cause: str | None, requested: bool) -> None:
    """A parent as its terminal writer left it: its state, why, and the cancel flag."""
    seed_task(db, task_id=task_id, tenant_id="eng", state=state, cancel_requested=requested)
    _doc(db, task_id).update({"completed_at": NOW, "end_cause": cause})


@pytest.mark.parametrize(
    "parents, cause",
    [
        ({"task_p1": ("FAILED", "runner_error", False)}, "failed_parent"),
        ({"task_p1": ("DEAD_LETTERED", None, False)}, "failed_parent"),
        ({"task_p1": ("CANCELLED", "cancel_requested", True)}, "cancelled_parent"),
        # THE PARENT'S OWN END, NOT ITS STATE (the review of #217). A parent
        # that is itself a failure's cascade -- or a step the fail_workflow
        # sweep took -- passes the failure on: CANCELLED is only its state.
        ({"task_p1": ("CANCELLED", "failed_parent", False)}, "failed_parent"),
        ({"task_p1": ("CANCELLED", "workflow_sweep", False)}, "failed_parent"),
        ({"task_p1": ("CANCELLED", "cancelled_parent", False)}, "cancelled_parent"),
        # A cancel requested before the field existed: the flag says so.
        ({"task_p1": ("CANCELLED", None, True)}, "cancelled_parent"),
        # A parent cancelled before the field existed, with no flag, ended in a
        # way this read cannot tell. The scheduler writes no cause rather than
        # guess, and the ledger splits the step by its chain of parents.
        ({"task_p1": ("CANCELLED", None, False)}, None),
        # A failure wins over a stop by hand beside it: the failure is what the
        # dependant could not have survived. A failure's cascade is a failure.
        (
            {"task_p1": ("CANCELLED", "cancel_requested", True), "task_p2": ("FAILED", "timeout", False)},
            "failed_parent",
        ),
        (
            {
                "task_p1": ("CANCELLED", "cancel_requested", True),
                "task_p2": ("CANCELLED", "failed_parent", False),
            },
            "failed_parent",
        ),
    ],
    ids=[
        "failed", "dead-lettered", "cancelled-on-request", "a-failure-s-cascade", "swept",
        "a-cancel-s-cascade", "requested-before-the-field", "unknown-end", "both",
        "a-cancel-beside-a-failure-s-cascade",
    ],
)
def test_a_dependant_is_cancelled_with_the_cause_its_parents_gave_it(db, make_scheduler, parents, cause):
    """Both of the scheduler's cascade paths: admission meets a READY dependant,
    the dependency sweep a PARKED one. They write the same words -- "an upstream
    workflow step did not succeed" -- and now different causes (#185, decision 2)."""
    _world(db)
    for task_id, (state, parent_cause, requested) in parents.items():
        _parent(db, task_id, state, parent_cause, requested)
    seed_task(db, task_id="task_ready_child", tenant_id="eng", depends_on=tuple(parents))
    seed_task(
        db, task_id="task_parked_child", tenant_id="eng", state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE", depends_on=tuple(parents),
    )

    make_scheduler().drain()

    for child in ("task_ready_child", "task_parked_child"):
        _ended(db, child, "CANCELLED", cause)
        assert _doc(db, child)["last_error"] == "an upstream workflow step did not succeed"


#: a -> b -> c -> d. Each step waits on the one before, so an end at `a`
#: reaches `d` three cascades down.
CHAIN = [step("a"), step("b", "a"), step("c", "b"), step("d", "c")]


def _drain_the_chain(make_scheduler) -> None:
    # The dependency sweep reads its PARKED slice once, so a grandchild sees
    # its parent's cancel on this drain or the next. Four drains reach `d`
    # whatever order the slice comes back in.
    scheduler = make_scheduler()
    for _ in range(4):
        scheduler.drain()


def test_a_failure_s_cascade_is_a_failure_s_however_far_down_it_reaches(client, db, make_scheduler):
    """THE REVIEW OF #217. `_FAILED_PARENT_STATES` holds CANCELLED, so the
    dependency rule is transitive: `b` is cancelled after `a` FAILED, then `c`
    after `b`, then `d` after `c`. Read from the parents' STATES alone, `c`
    and `d` were "after a cancel" -- a cancel nobody made -- and the
    workflow's failure cascade counted one step where it took three."""
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, CHAIN, on_step_failure=CONTINUE)
    worker_is_running(db, steps["a"])
    worker_finished(db, steps["a"], "FAILED")
    _doc(db, steps["a"])["end_cause"] = "runner_error"

    _drain_the_chain(make_scheduler)

    for name in ("b", "c", "d"):
        _ended(db, steps[name], "CANCELLED", "failed_parent")


def test_a_cancel_s_cascade_is_a_cancel_s_however_far_down_it_reaches(client, db, make_scheduler):
    """The other side of the same rule: a stop by hand at `a` is "after a
    cancel" all the way down, never promoted to a failure. A PIN: the code
    before the review's fix already got this side right."""
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, CHAIN, on_step_failure=CONTINUE)
    worker_is_running(db, steps["a"])
    worker_finished(db, steps["a"], "CANCELLED")
    _doc(db, steps["a"]).update({"end_cause": "cancel_requested", "cancel_requested": True})

    _drain_the_chain(make_scheduler)

    for name in ("b", "c", "d"):
        _ended(db, steps[name], "CANCELLED", "cancelled_parent")


def test_a_cancel_requested_before_admission_is_ended_as_requested(db, make_scheduler):
    _world(db)
    seed_task(db, task_id="task_flagged", tenant_id="eng", cancel_requested=True)

    make_scheduler().drain()

    _ended(db, "task_flagged", "CANCELLED", "cancel_requested")


def test_the_fail_workflow_sweep_ends_every_step_it_takes_as_a_sweep(client, db, make_scheduler):
    """`waiting` shares no edge with the failure; `after_broken` and `after_busy`
    wait behind steps. The sweep takes all three, and says so on each."""
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")

    make_scheduler().drain()

    for name in ("waiting", "after_busy", "after_broken"):
        _ended(db, steps[name], "CANCELLED", "workflow_sweep")


def test_under_continue_only_the_failed_step_s_dependant_is_cancelled_and_says_why(
    client, db, make_scheduler
):
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, FIVE_STEPS, on_step_failure=CONTINUE)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")

    make_scheduler().drain()

    _ended(db, steps["after_broken"], "CANCELLED", "failed_parent")


# --------------------------------------------------------------------------
# The scheduler: a dispatch that failed on the last attempt
# --------------------------------------------------------------------------

def _leased(db, *, attempts: int = 3, cancel_requested: bool = False) -> tuple[Task, Lease]:
    """What `return_to_ready_after_failed_dispatch` is handed: the READY snapshot
    the drain read, and the document admission has since made LEASED."""
    snapshot = Task(
        id="task_x", tenant_id="eng", runner_profile="mock", resource_class="standard",
        state=TaskState.READY, created_at=NOW, updated_at=NOW, input={},
        submitted_by="alice@saga.xyz", attempt_count=attempts - 1, max_attempts=3,
    )
    lease = Lease(
        lease_id="lease_x", task_id="task_x", attempt_id="att_x", tenant_id="eng",
        generation=attempts, units=1, pools=["global"], state=TaskState.LEASED,
        created_at=NOW, dispatch_deadline=NOW + timedelta(seconds=300),
        expires_at=NOW + timedelta(seconds=120),
    )
    stored = snapshot.to_firestore()
    stored.update(
        {
            "state": TaskState.LEASED.value,
            "attempt_count": attempts,
            "current_lease_id": lease.lease_id,
            "current_generation": attempts,
            "cancel_requested": cancel_requested,
        }
    )
    db.collection("tasks").document("task_x").set(stored)
    return snapshot, lease


@pytest.mark.parametrize(
    "cancel_requested, state, cause",
    [(False, "FAILED", "dispatch_failed"), (True, "CANCELLED", "cancel_requested")],
    ids=["spent", "cancelled-meanwhile"],
)
def test_a_dispatch_that_failed_on_the_last_attempt_ends_with_its_cause(db, cancel_requested, state, cause):
    from scheduler.store import SchedulerStore

    snapshot, lease = _leased(db, cancel_requested=cancel_requested)
    SchedulerStore(db, now=lambda: NOW).return_to_ready_after_failed_dispatch(
        snapshot, lease, "gke_create_job_failed"
    )

    _ended(db, "task_x", state, cause)


def test_a_dispatch_that_will_be_retried_ends_nothing_and_records_no_cause(db):
    from scheduler.store import SchedulerStore

    snapshot, lease = _leased(db, attempts=1)
    SchedulerStore(db, now=lambda: NOW).return_to_ready_after_failed_dispatch(
        snapshot, lease, "gke_create_job_failed"
    )

    doc = _doc(db, "task_x")
    assert doc["state"] == "READY"
    assert doc.get("end_cause") is None, "a task that has not ended has no end cause"


# --------------------------------------------------------------------------
# The API's cancel
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["QUEUED", "READY", "PARKED"])
def test_the_api_ends_a_task_that_holds_nothing_as_requested(client, db, state):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_idle", tenant_id="eng", state=state)

    response = client.post("/v1/tasks/task_idle/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    _ended(db, "task_idle", "CANCELLED", "cancel_requested")


def test_the_api_only_flags_a_task_that_holds_capacity_and_writes_no_cause(client, db):
    """The worker or the reconciler ends it, and writes the cause then; the API
    has ended nothing, so it records no end."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_held", tenant_id="eng", state="DISPATCHED")

    response = client.post("/v1/tasks/task_held/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    doc = _doc(db, "task_held")
    assert doc["state"] == "DISPATCHED" and doc["cancel_requested"] is True
    assert doc.get("end_cause") is None
