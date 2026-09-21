"""Step 4 of a repair must not reset a task that a LIVE lease is running.

`repair.py`'s step 4 sends the task back to READY and clears
`current_lease_id`. It identified the task by `finding.task_id` alone and never
checked which lease the task is currently under, so a finding about an OLD
attempt reset a task whose NEW attempt is running right now:

    1. task X runs at generation 1 under lease A
    2. the worker parks on quota, releases, and the scheduler legitimately
       re-admits: lease B, generation 2, worker B running and heartbeating
    3. the reconciler reaches a finding still describing generation 1 --
       `invalidate_generation(X, expected=1)` correctly no-ops, `release_lease(A)`
       correctly no-ops, and then step 4 forces X from RUNNING to READY and
       wipes `current_lease_id`, unhooking live lease B
    4. the next drain sees X READY and mints lease C

Two workers, one repository -- reached through the machinery invariant 5 exists
to enforce -- plus lease B orphaned and holding capacity nothing will return.

Reported as finding 1 of docs/audits/2026-09-18/10-reconciler-fencing-races.md.
The detection side already knows this state exists: `detect_orphan_leases` has a
"task points at a different lease" rule, and `detect_orphan_executions` calls a
superseded generation "the COMMON case" precisely because a newer attempt is
running the same task right now. Only the repair side did not look.

Note what these tests do NOT assert: that the stale execution is terminated or
that the stale lease's slots come back. Those are steps 1-3 and are covered by
test_reconciler_safety.py. Every case here is about step 4 leaving alone a task
it was not asked about.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner  # noqa: F401

from reconciler.config import ReconcilerConfig
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

from test_reconciler_safety import (
    TENANT,
    build,
    running_execution,
    seed_running_task,
)


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def seed_superseded_lease(
    db: FakeFirestore,
    *,
    lease_id: str = "lease_old",
    attempt_id: str = "att_old",
    task_id: str = "task_1",
    generation: int = 4,
    silent_seconds: int = 3000,
) -> None:
    """An unreleased lease from a PREVIOUS attempt of a task that moved on.

    Exactly the state observed live on 2026-09-16 and described in
    test_orphan_lease_after_partial_repair.py: `invalidate_generation` and
    `release_lease` are separate transactions, so an interruption between them
    strands the old lease at the old generation while the task is re-admitted.
    """
    now = utcnow()
    pools = pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )
    db.seed(
        f"leases/{lease_id}",
        {
            "lease_id": lease_id, "task_id": task_id, "attempt_id": attempt_id,
            "tenant_id": TENANT, "generation": generation, "pools": pools, "units": 1,
            "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(seconds=silent_seconds + 60),
            "dispatch_deadline": now - timedelta(seconds=silent_seconds - 240),
            "expires_at": now - timedelta(seconds=silent_seconds - 120),
            "heartbeat_at": now - timedelta(seconds=silent_seconds),
            "released_at": None,
        },
    )


def test_an_obsolete_execution_does_not_reset_the_task_its_successor_is_running(db, config):
    """A generation-4 execution is still alive; generation 5 is running fine.

    The finding carries no lease at all (the old lease is long released), so the
    only thing tying it to the task is `task_id` -- which is the live attempt's
    task too.
    """
    seed_running_task(db, generation=5, silent_seconds=0)
    stale = running_execution(attempt_id="att_old", generation=4)
    backend = FakeBackend(executions=[stale], journal=db.writes)

    report = build(db, config, backend).run_once()

    assert {o.kind for o in report.outcomes} == {"obsolete_generation"}
    assert backend.terminated == [stale.name], "the superseded execution must still be killed"

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value, (
        "the live generation-5 attempt was reset to READY by a finding about generation 4"
    )
    assert task["current_lease_id"] == "lease_1", (
        "the live lease was unhooked from its task; the next drain would admit a second worker"
    )
    assert task["current_generation"] == 5, "a no-op invalidation must not fence the live worker"
    assert db.doc("leases/lease_1")["released_at"] is None


def test_a_stranded_old_lease_is_released_without_disturbing_the_live_one(db, config):
    """Steps 1-3 still run for the stale lease; step 4 must not touch the task."""
    seed_running_task(db, generation=5, silent_seconds=0, pool_active=2)
    seed_superseded_lease(db, generation=4)
    backend = FakeBackend(executions=[], journal=db.writes)

    report = build(db, config, backend).run_once()

    outcomes = [o for o in report.outcomes if o.lease_id == "lease_old"]
    assert outcomes, f"the stranded lease produced no finding: {report.as_dict()}"
    assert outcomes[0].released is True, "the stranded lease's slots must come back"
    assert db.doc("leases/lease_old")["released_at"] is not None
    # One unit returned, from 2 to 1 -- the live lease's unit stays held.
    assert db.doc("pools/global")["active"] == 1

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value
    assert task["current_lease_id"] == "lease_1"
    assert db.doc("leases/lease_1")["released_at"] is None


def test_a_task_holding_no_lease_at_all_is_still_repaired(db, config):
    """"No expectation" must not become "refuse everything".

    A task whose `current_lease_id` is already None has nothing to strand -- it
    is the shape `detect_orphan_leases` explicitly tolerates (`task.lease_id not
    in (None, lease.lease_id)`), and it is a task stuck in a concurrency state
    with no live attempt, which is exactly what step 4 exists to free.
    """
    seed_running_task(db, pool_active=1, silent_seconds=600)
    db.doc("tasks/task_1")["current_lease_id"] = None
    backend = FakeBackend(executions=[], journal=db.writes)

    report = build(db, config, backend).run_once()

    assert report.outcomes[0].repaired_to == TaskState.READY.value
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value


def test_the_ordinary_repair_still_requeues_its_own_task(db, config):
    """The guard must not turn a real reclaim into a no-op.

    Here the finding IS about the lease the task points at, which is the whole
    population of ordinary stale-lease repairs. Without this the fix above would
    be indistinguishable from deleting step 4.
    """
    seed_running_task(db, pool_active=1, silent_seconds=600)
    backend = FakeBackend(executions=[], journal=db.writes)

    report = build(db, config, backend).run_once()

    assert report.outcomes[0].repaired_to == TaskState.READY.value
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value
    assert db.doc("tasks/task_1")["current_lease_id"] is None
    assert db.doc("pools/global")["active"] == 0
