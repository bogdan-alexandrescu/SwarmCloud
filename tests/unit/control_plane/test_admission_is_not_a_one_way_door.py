"""Everything after `acquire_lease` commits must give the capacity back if it fails.

`acquire_lease_in_transaction` is atomic and correct. What follows it in
`Scheduler._admit_one` is not transactional with it and, until now, was not even
guarded: `create_attempt` and `append_event` sat OUTSIDE the `try`, and the
`try` that did exist caught only `DispatchError`. So any other exception --

  * `google.auth.exceptions.DefaultCredentialsError` from building the Cloud Run
    client, which `dispatch.py` constructs BETWEEN its two
    `except GoogleAPICallError` blocks and therefore does not translate;
  * a `DeadlineExceeded`/`ServiceUnavailable` on the bare `.set()` in
    `create_attempt` or `append_event`;

-- propagated straight out of `drain()` with the pools already incremented, the
lease written and the task at LEASED. Nothing in `apps/scheduler/` ever looks at
a LEASED task again (`ready_tasks()` selects `state == "READY"` only), so the
slot was gone until the reconciler's deadline sweep noticed, minutes later, in a
different service.

Reported as findings 1 and 2 of
docs/audits/2026-09-18/05-scheduler-capacity-leaks.md.

The exception is still raised. A scheduler that swallowed a credential failure
would admit, leak, release and retry the whole queue in silence; the point here
is that the capacity comes back BEFORE it propagates. Invariant 3 is what makes
this matter: concurrency counts from LEASED, so a leaked lease is a slot the
platform refuses to reuse and cannot explain.
"""

from __future__ import annotations

import pytest

from .conftest import seed_pool, seed_task, seed_tenant


def base_pools(db) -> None:
    seed_pool(db, "global", hard_limit=20)
    seed_pool(db, "resource:standard", hard_limit=20)
    seed_pool(db, "backend:CLOUD_RUN_JOB", hard_limit=20)
    seed_tenant(db, "eng")


def held_units(db) -> int:
    return db.docs["pools/global"]["active"]


def test_a_dispatcher_that_raises_something_other_than_DispatchError_returns_the_slot(
    db, make_scheduler, dispatcher
):
    """The credential-failure shape: not a DispatchError, so nothing caught it."""
    base_pools(db)
    seed_task(db, task_id="task_leak", tenant_id="eng")

    def explode(*, task, lease, profile, tenant):
        # The real one is google.auth.exceptions.DefaultCredentialsError, raised
        # by `self._jobs()` in dispatch.py's `ensure_job` -- which sits between
        # the two blocks that translate Google errors into DispatchError.
        raise RuntimeError("could not refresh the metadata-server credential")

    dispatcher.dispatch = explode

    with pytest.raises(RuntimeError):
        make_scheduler().drain()

    assert held_units(db) == 0, "the lease was admitted and its capacity never returned"
    stored = db.docs["tasks/task_leak"]
    assert stored["state"] == "READY", "a LEASED task is one no drain will ever look at again"
    assert stored["current_lease_id"] is None
    leases = [doc for path, doc in db.docs.items() if path.startswith("leases/")]
    assert leases and all(lease["released_at"] is not None for lease in leases)


def test_a_failing_create_attempt_returns_the_slot_too(db, make_scheduler, dispatcher):
    """`create_attempt` is a bare `.set()` and used to sit outside every guard."""
    base_pools(db)
    seed_task(db, task_id="task_leak2", tenant_id="eng")

    scheduler = make_scheduler()
    original = scheduler.store.create_attempt

    def explode(task, lease, backend):  # noqa: ANN001
        raise TimeoutError("504 Deadline Exceeded writing the attempt")

    scheduler.store.create_attempt = explode

    with pytest.raises(TimeoutError):
        scheduler.drain()

    assert original is not None
    assert held_units(db) == 0
    assert db.docs["tasks/task_leak2"]["state"] == "READY"
    assert dispatcher.dispatched == [], "nothing should have been dispatched"


def test_the_capacity_comes_back_even_when_the_rollback_cannot_finish(
    db, make_scheduler, dispatcher
):
    """A broken control plane breaks the cleanup too; the pools still come back.

    `return_to_ready_after_failed_dispatch` releases the lease and returns the
    task in one transaction and writes the event only after it commits, so an
    event write that fails still returns the slot. The original exception must
    survive that -- masking
    it with the cleanup's own failure would send the operator after the wrong
    fault.
    """
    base_pools(db)
    seed_task(db, task_id="task_leak3", tenant_id="eng")

    scheduler = make_scheduler()
    original_append = scheduler.store.append_event
    calls = {"n": 0}

    def explode_dispatch(*, task, lease, profile, tenant):
        raise RuntimeError("the original fault")

    def flaky_append(*args, **kwargs):  # noqa: ANN002, ANN003
        # LEASE_ACQUIRED goes through; the one the ROLLBACK writes does not.
        calls["n"] += 1
        if calls["n"] == 1:
            return original_append(*args, **kwargs)
        raise RuntimeError("the cleanup's own fault")

    dispatcher.dispatch = explode_dispatch
    scheduler.store.append_event = flaky_append

    with pytest.raises(RuntimeError, match="the original fault"):
        scheduler.drain()

    assert held_units(db) == 0


def test_a_healthy_dispatch_is_unaffected(db, make_scheduler, dispatcher):
    """The guard must not change the ordinary path."""
    base_pools(db)
    seed_task(db, task_id="task_ok", tenant_id="eng")

    report = make_scheduler().drain()

    assert report.dispatched == 1
    assert len(dispatcher.dispatched) == 1
    assert held_units(db) == 1, "a running task still holds its slot"
    assert db.docs["tasks/task_ok"]["state"] == "DISPATCHED"
