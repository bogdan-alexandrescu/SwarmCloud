"""A dispatch the platform refuses parks PLATFORM_REFUSED; it never fails the task.

THE INCIDENT. 2026-10-28 ~09:30-11:15Z: swarm-tenant-eng's Job quota
(`count/jobs.batch` 240) filled with FINISHED Jobs, each holding its slot for
`ttlSecondsAfterFinished` (then 3600 s). Every dispatch past it was refused
`gke_job_quota_exceeded`, `return_to_ready_after_failed_dispatch` retried it
on a fixed 30 s and ended it FAILED at `max_attempts`: 28 steps FAILED (3
attempts; merge steps 10) without running once. On 2026-10-20 an admission
policy did the same to 11-13 agents (#1820).

BGD's standing rule (2026-09-14): "never fail workflows when quotas are
reached. just pause them and resume them when new quota is refreshed." BGD
accepted contract request 123 on 2026-10-28, and these tests hold what it
built (docs/contract-change-requests.md request 123):

  * a refusal coded `gke_job_quota_exceeded` or `gke_admission_refused`
    (`dispatch.PLATFORM_REFUSAL_CODES`) releases the lease, parks the task
    PLATFORM_REFUSED with `next_eligible_at` backed off (30 s doubling, at
    most ten minutes), increments `platform_refusals` and spends NO attempt;
  * the scheduler promotes it once `next_eligible_at` passes;
  * a dispatch that succeeds resets `platform_refusals` to 0;
  * past `PLATFORM_REFUSED_MAX_PARKS` it stays PARKED, never FAILED;
  * any other 403 (`gke_create_job_forbidden`: RBAC, a missing namespace)
    still goes the old way: READY on 30 s, FAILED at the cap.

A PARKED task holds no lease and no pool count (invariant 1), so every test
that parks also checks the pool came back to zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from swarm_common.models import PLATFORM_REFUSED_MAX_PARKS
from swarm_common.states import ParkReason, TaskState

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant

START = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
TASK = "task_quota"


class _Refusing:
    """A backend that refuses with `code` while `refusing[0]` is true, and
    otherwise succeeds, recording each generation it was asked to start."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.refusing = [True]
        self.generations: list[int] = []

    def dispatch(self, *, task, lease, profile, tenant):
        from scheduler.dispatch import DispatchError

        self.generations.append(lease.generation)
        if self.refusing[0]:
            raise DispatchError(
                "jobs.batch \"swarm-x\" is forbidden: refused (injected)", code=self.code
            )
        return f"execution-{lease.attempt_id}"


def _world(db, code: str):
    from scheduler.dispatch import BackendRouter
    from scheduler.loop import Scheduler
    from scheduler.metrics import SchedulerMetrics
    from scheduler.store import SchedulerStore

    seed_tenant(db, "eng", max_active=5)
    seed_pool(db, "global", hard_limit=5)
    seed_task(db, task_id=TASK, tenant_id="eng", created_at=START)
    clock = [START]
    backend = _Refusing(code)
    settings = scheduler_settings()
    scheduler = Scheduler(
        settings=settings,
        store=SchedulerStore(db, now=lambda: clock[0]),
        router=BackendRouter(cloud_run=backend, gke=backend, settings=settings),
        metrics=SchedulerMetrics(),
        now=lambda: clock[0],
    )
    return scheduler, backend, clock


def _doc(db) -> dict:
    return db.docs[f"tasks/{TASK}"]


def _unreleased_leases(db) -> list[str]:
    return [
        path for path, doc in db.docs.items()
        if path.startswith("leases/") and doc.get("released_at") is None
    ]


@pytest.mark.parametrize("code", ["gke_job_quota_exceeded", "gke_admission_refused"])
def test_a_platform_refusal_parks_spends_no_attempt_and_releases_its_lease(db, code):
    # MUTATION: pass `platform_refusal=False` in loop.py's DispatchError branch
    # (or drop the `parks` branch in store.py `_returned`) and the task is READY
    # with attempt_count 1 -- the path that FAILED 28 steps on 2026-10-28.
    scheduler, _backend, _clock = _world(db, code)

    report = scheduler.drain()

    assert report.dispatch_failures == 1, report.to_dict()
    stored = _doc(db)
    assert stored["state"] == TaskState.PARKED.value
    assert stored["park_reason"] == ParkReason.PLATFORM_REFUSED.value
    assert stored["attempt_count"] == 0, "a refusal by the platform spends no attempt"
    assert stored["platform_refusals"] == 1
    assert stored["next_eligible_at"] == START + timedelta(seconds=30)
    assert stored["current_lease_id"] is None
    assert stored.get("completed_at") is None
    assert code in stored["last_error"]
    assert _unreleased_leases(db) == [], "a parked task holds no lease (invariant 1)"
    assert db.docs["pools/global"]["active"] == 0, "the slot came back in the same write"
    assert report.platform_refused == 1


@pytest.mark.parametrize("code", ["gke_job_quota_exceeded", "gke_admission_refused"])
def test_a_platform_refused_park_is_promoted_after_next_eligible_at(db, code):
    # MUTATION: drop `report.promoted_platform_refusals = ...` from `drain` and
    # the task stays PARKED for ever: nothing else promotes this reason.
    scheduler, backend, clock = _world(db, code)
    scheduler.drain()
    assert _doc(db)["state"] == TaskState.PARKED.value

    # Not due yet: a drain inside the backoff leaves it parked and leases nothing.
    clock[0] = START + timedelta(seconds=29)
    report = scheduler.drain()
    assert report.leased == 0
    assert _doc(db)["state"] == TaskState.PARKED.value

    # Due, and the quota has freed: promoted, admitted and dispatched.
    backend.refusing[0] = False
    clock[0] = START + timedelta(seconds=31)
    report = scheduler.drain()
    assert report.promoted_platform_refusals == 1, report.to_dict()
    assert report.dispatched == 1, report.to_dict()
    stored = _doc(db)
    assert stored["state"] == TaskState.DISPATCHED.value
    assert stored["attempt_count"] == 1, "the one attempt that ran is the only one counted"


def test_a_successful_dispatch_resets_the_refusal_count(db):
    # MUTATION: drop the `platform_refusals = 0` reset in store.py
    # `mark_dispatched` and the count stays 2 after the Job is created.
    scheduler, backend, clock = _world(db, "gke_job_quota_exceeded")
    scheduler.drain()
    clock[0] = clock[0] + timedelta(seconds=31)
    scheduler.drain()
    assert _doc(db)["platform_refusals"] == 2
    assert _doc(db)["next_eligible_at"] == clock[0] + timedelta(seconds=60), "30 s, doubling"

    backend.refusing[0] = False
    clock[0] = clock[0] + timedelta(seconds=61)
    report = scheduler.drain()
    assert report.dispatched == 1, report.to_dict()
    assert _doc(db)["platform_refusals"] == 0


def test_the_sixth_consecutive_refusal_leaves_it_parked_not_failed(db):
    # MUTATION: end the task FAILED once `platform_refusals` passes
    # PLATFORM_REFUSED_MAX_PARKS (request 123 as first proposed) and the sixth
    # drain reads FAILED -- what BGD's rule of 2026-09-14 forbids.
    scheduler, backend, clock = _world(db, "gke_job_quota_exceeded")
    waits = []
    for _ in range(PLATFORM_REFUSED_MAX_PARKS + 3):
        report = scheduler.drain()
        assert report.dispatch_failures == 1, report.to_dict()
        stored = _doc(db)
        assert stored["state"] == TaskState.PARKED.value, stored.get("last_error")
        assert stored["park_reason"] == ParkReason.PLATFORM_REFUSED.value
        waits.append((stored["next_eligible_at"] - clock[0]).total_seconds())
        clock[0] = stored["next_eligible_at"] + timedelta(seconds=1)

    stored = _doc(db)
    assert stored["platform_refusals"] == PLATFORM_REFUSED_MAX_PARKS + 3
    assert stored["attempt_count"] == 0, "eight refusals, no attempt spent"
    assert stored.get("end_cause") is None
    assert waits == [30, 60, 120, 240, 480, 600, 600, 600], "30 s doubling, held at 10 min"
    assert len(backend.generations) == PLATFORM_REFUSED_MAX_PARKS + 3, (
        "past the bound it is still retried: a quota frees, and the work resumes"
    )
    assert db.docs["pools/global"]["active"] == 0


def test_a_403_that_is_not_the_platforms_still_goes_the_old_way(db):
    # MUTATION: treat every DispatchError as a platform refusal and an RBAC
    # fault parks for ever instead of failing at the cap.
    scheduler, _backend, clock = _world(db, "gke_create_job_forbidden")
    states = []
    for _ in range(3):
        scheduler.drain()
        states.append(_doc(db)["state"])
        clock[0] = clock[0] + timedelta(seconds=31)

    stored = _doc(db)
    assert states == ["READY", "READY", "FAILED"]
    assert stored["attempt_count"] == 3
    assert stored.get("park_reason") != ParkReason.PLATFORM_REFUSED.value
    assert not stored.get("platform_refusals")


def test_a_cancel_requested_while_leased_still_ends_cancelled_on_a_refusal(db):
    # MUTATION: test `parks` before `cancel_requested` in store.py `_returned`
    # and a refused task the user cancelled is parked and later run.
    scheduler, _backend, _clock = _world(db, "gke_job_quota_exceeded")
    original = scheduler._router.dispatch

    def cancel_then_dispatch(**kwargs):
        db.docs[f"tasks/{TASK}"]["cancel_requested"] = True
        return original(**kwargs)

    scheduler._router.dispatch = cancel_then_dispatch
    scheduler.drain()
    assert _doc(db)["state"] == TaskState.CANCELLED.value
    assert db.docs["pools/global"]["active"] == 0


def test_the_backoff_doubles_from_30_seconds_and_holds_at_ten_minutes():
    # MUTATION: drop the cap in `platform_refusal_backoff` and the sixth park
    # waits 16 minutes, past the Job TTL that frees the quota.
    from scheduler.store import platform_refusal_backoff

    got = [platform_refusal_backoff(n).total_seconds() for n in range(1, 9)]
    assert got == [30, 60, 120, 240, 480, 600, 600, 600]
    assert platform_refusal_backoff(10_000).total_seconds() == 600
