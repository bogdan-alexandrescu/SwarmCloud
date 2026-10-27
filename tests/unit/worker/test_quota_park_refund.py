"""A provider-quota park refunds its attempt, within a server-side bound (#2350).

A PROVIDER_QUOTA_EXHAUSTED park-and-exit is invariant 4's designed path, yet
it spent one of `max_attempts`: four `accounts_full` parks took
task_c61ee787 to attempt_count 5 of max_attempts 3, and its first real
infrastructure loss was then final ("no attempts left"). Decided 2026-10-27
(BGD, epic #2525): "Refund, bounded" -- refunded like a CI_PENDING park, up
to `QUOTA_MAX_RESUMES` (5) counted in `metadata.quota_park_resumes`; past the
bound the park spends an attempt as it always did.

What is held here, on both control planes (the direct one and the routes'):

* parks 1-5 are refunded and park 6 spends an attempt, on a task parked on
  its last attempt -- the incident's shape;
* the bound is swarm-api's: a worker has no way to send a larger one;
* a stale generation's quota park is refused and refunds nothing
  (invariant 5);
* the counter survives a retry: a failed attempt between parks does not
  reset it;
* CREDENTIAL_MISSING, the park that needs an admin, refunds nothing;
* a caller cannot set the counter at submission.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import io
from datetime import timedelta
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker.control import ControlPlane
from agent_worker.errors import FencedWriteRefused
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import FakeFirestore, FakeTransactionRunner
from state_routes_harness import RoutesOpener, api_client, events, remote_control
from state_routes_harness import TASK as ROUTE_TASK
from state_routes_harness import seed_attempt as seed_route_attempt
from worker_seeds import TENANT, seed_attempt

KEY = "quota_park_resumes"
QUOTA = ParkReason.PROVIDER_QUOTA_EXHAUSTED
#: The incident's park: the account pool was full when the worker asked.
DETAIL = {"park_phase": "account_assign", "account_pool_reason": "accounts_full"}
#: BGD, 2026-10-27: five. Stated here rather than imported, so a module that
#: lost its constant fails on the refund and not on an import.
BOUND = 5


def _control(db: FakeFirestore, *, generation: int = 1) -> ControlPlane:
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=generation,
        logger=build_logger(
            task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
            generation=generation, runner_profile="mock", stream=io.StringIO(),
        ),
        txn_runner=FakeTransactionRunner(db),
    )


def _quota_park(control: ControlPlane) -> Any:
    return control.park(reason=QUOTA, next_eligible_at=utcnow() + timedelta(minutes=5),
                        detail=dict(DETAIL))


def _admit_direct(db: FakeFirestore, *, attempt_count: int) -> ControlPlane:
    """What admission does for the next attempt: one more `attempt_count`, a
    live lease, RUNNING. The task's metadata is carried, as it is in Firestore."""
    metadata = copy.deepcopy((db.documents.get("tasks/task_1") or {}).get("metadata"))
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=attempt_count)
    if metadata is not None:
        db.doc("tasks/task_1")["metadata"] = metadata
    return _control(db)


# ---------------------------------------------------------------------------
# the bound
# ---------------------------------------------------------------------------


def test_the_bound_is_named_once_and_swarm_api_restates_it():
    from swarm_api import attemptstate, validation

    assert control_mod.QUOTA_PARK_RESUMES_METADATA_KEY == KEY
    assert attemptstate.QUOTA_PARK_RESUMES == KEY
    assert validation.QUOTA_PARK_RESUMES_METADATA_KEY == KEY
    assert attemptstate.QUOTA_MAX_RESUMES == BOUND
    # MUTATION: let the two bounds drift and a direct park and a route park
    # refund a different number of quota parks.
    assert control_mod.QUOTA_MAX_RESUMES == attemptstate.QUOTA_MAX_RESUMES


# ---------------------------------------------------------------------------
# the direct park
# ---------------------------------------------------------------------------


def test_direct_quota_parks_one_to_five_are_refunded_and_the_sixth_spends_an_attempt():
    bound = BOUND
    db = FakeFirestore()
    count = 3  # the LAST of max_attempts 3, as task_c61ee787's parks were
    for park in range(1, bound + 2):
        control = _admit_direct(db, attempt_count=count)
        refunded = _quota_park(control)
        task = db.doc("tasks/task_1")
        assert task["state"] == TaskState.PARKED.value
        assert task["park_reason"] == QUOTA.value
        assert db.doc("leases/lease_1")["released_at"] is not None
        parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value][-1]
        if park <= bound:
            # MUTATION: park without the refund and attempt_count climbs to
            # max_attempts on the first park.
            assert task["attempt_count"] == count - 1, park
            assert refunded is True, park
            assert task["metadata"][KEY] == park
            assert parked["detail"]["attempt_refunded"] is True
            assert parked["detail"][KEY] == park
        else:
            # MUTATION: refund without the bound and a task that parks for
            # ever never ends at max_attempts.
            assert refunded is False
            assert task["attempt_count"] == count
            assert task["metadata"][KEY] == bound
            assert parked["detail"]["attempt_refunded"] is False
        count = task["attempt_count"] + 1
    assert park == bound + 1


def test_a_direct_credential_missing_park_refunds_nothing():
    db = FakeFirestore()
    control = _admit_direct(db, attempt_count=2)

    control.park(reason=ParkReason.CREDENTIAL_MISSING,
                 next_eligible_at=utcnow() + timedelta(hours=1), detail=dict(DETAIL))

    task = db.doc("tasks/task_1")
    # MUTATION: refund every park reason and an admin's park is free for ever.
    assert task["attempt_count"] == 2
    assert KEY not in (task.get("metadata") or {})


def test_a_stale_generations_direct_quota_park_is_refused_and_refunds_nothing():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, generation=1, task_generation=2,
                 attempt_count=3)
    control = _control(db)
    before = copy.deepcopy(db.documents)
    db.writes.clear()

    # MUTATION: read the task without `_fenced_task` and the stale attempt
    # parks a task that is no longer its own, and refunds it.
    with pytest.raises(FencedWriteRefused):
        _quota_park(control)

    assert db.writes == []
    assert db.documents == before
    assert db.doc("tasks/task_1")["attempt_count"] == 3


def test_the_direct_counter_survives_a_retry():
    bound = BOUND
    db = FakeFirestore()
    control = _admit_direct(db, attempt_count=1)
    for _ in range(bound - 1):
        assert _quota_park(control) is True
        control = _admit_direct(db, attempt_count=db.doc("tasks/task_1")["attempt_count"] + 1)
    # An ordinary failure between parks: READY, an attempt spent, metadata kept.
    assert control.fail_retryably(exit_code=1, error="boom", cause="crash") is TaskState.READY
    assert db.doc("tasks/task_1")["metadata"][KEY] == bound - 1

    control = _admit_direct(db, attempt_count=db.doc("tasks/task_1")["attempt_count"] + 1)
    assert _quota_park(control) is True
    assert db.doc("tasks/task_1")["metadata"][KEY] == bound
    control = _admit_direct(db, attempt_count=db.doc("tasks/task_1")["attempt_count"] + 1)
    before = db.doc("tasks/task_1")["attempt_count"]
    # MUTATION: reset the counter on a retry and this sixth park is refunded.
    assert _quota_park(control) is False
    assert db.doc("tasks/task_1")["attempt_count"] == before


# ---------------------------------------------------------------------------
# the routes' park (RemoteControlPlane -> swarm-api's attemptstate)
# ---------------------------------------------------------------------------


class _Routes:
    """One task's attempts over swarm-api's routes, each a new generation."""

    def __init__(self) -> None:
        self.db = ApiFirestore()
        self.opener = RoutesOpener(api_client(self.db))
        self.generation = 0

    def admit(self, *, attempt_count: int) -> Any:
        self.generation += 1
        g = self.generation
        previous = self.db.docs.get(f"tasks/{ROUTE_TASK}") or {}
        metadata = copy.deepcopy(previous.get("metadata") or {})
        seed_route_attempt(self.db, attempt_id=f"att_q{g}", lease_id=f"lease_q{g}",
                           generation=g, attempt_count=attempt_count, metadata=metadata)
        control = remote_control(self.opener, attempt_id=f"att_q{g}", lease_id=f"lease_q{g}",
                                 generation=g)
        control.validate_generation()
        control.advance_to_running()
        return control

    @property
    def task(self) -> dict[str, Any]:
        return self.db.docs[f"tasks/{ROUTE_TASK}"]


def test_route_quota_parks_one_to_five_are_refunded_and_the_sixth_spends_an_attempt():
    bound = BOUND
    routes = _Routes()
    count = 3
    for park in range(1, bound + 2):
        control = routes.admit(attempt_count=count)
        refunded = _quota_park(control)
        task = routes.task
        assert task["state"] == TaskState.PARKED.value
        assert routes.db.docs[f"leases/lease_q{routes.generation}"]["released_at"] is not None
        parked = events(routes.db, kind="parked")[-1]
        if park <= bound:
            # MUTATION: leave PROVIDER_QUOTA_EXHAUSTED out of `_park_payload`'s
            # refunds and attempt_count climbs past max_attempts, #2350's shape.
            assert task["attempt_count"] == count - 1, park
            assert refunded is True, park
            assert task["metadata"][KEY] == park
            assert parked["detail"]["attempt_refunded"] is True
        else:
            # MUTATION: refund without the bound and this sixth park is free.
            assert refunded is False
            assert task["attempt_count"] == count
            assert task["metadata"][KEY] == bound
            assert parked["detail"]["attempt_refunded"] is False
        count = task["attempt_count"] + 1


def test_a_worker_cannot_raise_the_routes_bound(monkeypatch):
    """The park body has no number in it: a worker whose own restatement of
    the bound says a million is still held to swarm-api's."""
    monkeypatch.setattr(control_mod, "QUOTA_MAX_RESUMES", 1_000_000, raising=False)
    routes = _Routes()
    routes.admit(attempt_count=1)
    routes.task["metadata"] = {KEY: BOUND}
    control = routes.admit(attempt_count=2)
    refunded = _quota_park(control)

    # MUTATION: take the bound from the worker and this refunds.
    assert refunded is False
    assert routes.task["attempt_count"] == 2


def test_a_stale_generations_route_quota_park_is_refused_and_refunds_nothing():
    routes = _Routes()
    control = routes.admit(attempt_count=3)
    routes.task["current_generation"] = routes.generation + 1
    before = copy.deepcopy(routes.db.docs)

    # MUTATION: skip `_fenced` in the park route and the stale park refunds.
    with pytest.raises(FencedWriteRefused):
        _quota_park(control)

    assert routes.db.docs == before
    assert routes.task["attempt_count"] == 3


def test_the_route_counter_survives_a_retry():
    bound = BOUND
    routes = _Routes()
    control = routes.admit(attempt_count=1)
    for _ in range(bound - 1):
        assert _quota_park(control) is True
        control = routes.admit(attempt_count=routes.task["attempt_count"] + 1)
    assert control.fail_retryably(exit_code=1, error="boom", cause="crash") is TaskState.READY
    assert routes.task["metadata"][KEY] == bound - 1

    control = routes.admit(attempt_count=routes.task["attempt_count"] + 1)
    assert _quota_park(control) is True
    control = routes.admit(attempt_count=routes.task["attempt_count"] + 1)
    before = routes.task["attempt_count"]
    # MUTATION: reset the counter on a retry and this sixth park is refunded.
    assert _quota_park(control) is False
    assert routes.task["attempt_count"] == before
    assert routes.task["metadata"][KEY] == bound


def test_a_route_credential_missing_park_refunds_nothing():
    routes = _Routes()
    control = routes.admit(attempt_count=2)

    control.park(reason=ParkReason.CREDENTIAL_MISSING,
                 next_eligible_at=utcnow() + timedelta(hours=1), detail=dict(DETAIL))

    assert routes.task["attempt_count"] == 2
    assert KEY not in (routes.task.get("metadata") or {})
