"""A release wait refunds its attempt, within a bound (lane AU7-REFUND, part of #1933).

Lane AU-7 parked an agent's release wait with a plain `control.park`
(DEPENDENCY_INCOMPLETE), which refunds nothing: every release wait spent one
of the task's `max_attempts`, and a wait parked on the last attempt was never
woken, because the merge-wake tick does not wake a task whose retries are
exhausted. Decided 2026-10-22 (AU-7 question 5, the recommended option):
"Refund within a bound, like the CI wait".

What is held here:

* the worker's await-release exit refunds the attempt, so a wait on the last
  attempt leaves the task with an attempt to resume on;
* the direct park refunds while `metadata.release_wait_resumes` is under its
  bound and parks without a refund past it;
* a stale generation's release-wait park is refused and writes nothing
  (invariant 5);
* the routes' park refunds by swarm-api's bound, which the worker cannot send.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import io
import json
import secrets
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker import questions as questions_mod
from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode, FencedWriteRefused
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import FakeFirestore, FakeTransactionRunner
from state_routes_harness import LEASE, POOLS, RoutesOpener, api_client, events, remote_control
from state_routes_harness import TASK as ROUTE_TASK
from state_routes_harness import seed_attempt as seed_route_attempt
from test_standalone_outputs import _run, _seed, agent_cli  # noqa: F401 - agent_cli is a fixture
from worker_seeds import TENANT, seed_attempt

KEY = "release_wait_resumes"


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


def _detail(sha: str) -> dict[str, Any]:
    return {"park_phase": "release_wait",
            questions_mod.RELEASE_WAIT_KEY: {"sha": sha, "attempt_id": "att_1",
                                             "asked_at": utcnow().isoformat()}}


# ---------------------------------------------------------------------------
# the bound
# ---------------------------------------------------------------------------


def test_the_bound_is_named_once_and_swarm_api_restates_it():
    from swarm_api import attemptstate, validation

    assert control_mod.RELEASE_WAIT_RESUMES_METADATA_KEY == KEY
    assert attemptstate.RELEASE_WAIT_RESUMES == KEY
    assert validation.RELEASE_WAIT_RESUMES_METADATA_KEY == KEY
    # MUTATION: let the two bounds drift and a direct park and a route park
    # refund a different number of waits.
    assert control_mod.RELEASE_WAIT_MAX_RESUMES == attemptstate.RELEASE_WAIT_MAX_RESUMES
    assert control_mod.RELEASE_WAIT_MAX_RESUMES >= 1


# ---------------------------------------------------------------------------
# the direct park
# ---------------------------------------------------------------------------


def test_a_release_wait_refunds_its_attempt_up_to_the_bound_and_not_after():
    bound = control_mod.RELEASE_WAIT_MAX_RESUMES
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=2)
    db.doc("tasks/task_1")["metadata"] = {KEY: bound - 1}
    control = _control(db)

    refunded = control.park_release_wait(detail=_detail(secrets.token_hex(20)),
                                         max_resumes=bound)

    task = db.doc("tasks/task_1")
    # MUTATION: park without the refund (`refund = False`) and this is red.
    assert refunded is True
    assert task["attempt_count"] == 1
    # MUTATION: refund without counting it and the bound never bites.
    assert task["metadata"][KEY] == bound
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert task["current_lease_id"] is None
    (entry,) = task["blocked_by"]
    # MUTATION: drop the detail from `blocked_by` and the tick has no wait to read.
    assert entry[questions_mod.RELEASE_WAIT_KEY]["attempt_id"] == "att_1"
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("attempts/att_1")["exit_code"] == ExitCode.PARKED
    (parked,) = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked["detail"]["attempt_refunded"] is True
    assert parked["detail"][KEY] == bound


def test_past_the_bound_a_release_wait_parks_without_a_refund():
    bound = control_mod.RELEASE_WAIT_MAX_RESUMES
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=2)
    db.doc("tasks/task_1")["metadata"] = {KEY: bound}
    control = _control(db)

    refunded = control.park_release_wait(detail=_detail(secrets.token_hex(20)),
                                         max_resumes=bound)

    task = db.doc("tasks/task_1")
    # MUTATION: refund unconditionally (`refund = attempt_count > 0`) and a
    # task that waits for ever never ends at max_attempts.
    assert refunded is False
    assert task["attempt_count"] == 2
    assert task["metadata"][KEY] == bound
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value


def test_a_stale_generations_release_wait_park_is_refused_and_touches_nothing():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, generation=1, task_generation=2,
                 attempt_count=2)
    control = _control(db)
    before = copy.deepcopy(db.documents)
    db.writes.clear()

    # MUTATION: read the task without `_fenced_task` and the stale attempt
    # parks a task that is no longer its own, and refunds it.
    with pytest.raises(FencedWriteRefused):
        control.park_release_wait(detail=_detail(secrets.token_hex(20)),
                                  max_resumes=control_mod.RELEASE_WAIT_MAX_RESUMES)

    assert db.writes == []
    task = db.doc("tasks/task_1")
    assert task["attempt_count"] == 2 and task["state"] == TaskState.RUNNING.value
    assert KEY not in (task.get("metadata") or {})
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.documents == before


# ---------------------------------------------------------------------------
# the worker's await-release exit
# ---------------------------------------------------------------------------


def test_the_await_release_exit_refunds_a_wait_on_the_last_attempt(db, worker_factory, agent_cli):
    sha = secrets.token_hex(20)
    _seed(db, {"write_artifact": {"await-release.json": json.dumps({"sha": sha})}})
    task = db.doc("tasks/task_1")
    # The last attempt: before this lane the wait spent it and was never woken.
    task["attempt_count"] = task["max_attempts"]

    assert _run(worker_factory) == ExitCode.PARKED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    # MUTATION: keep the exit on `control.park` and the attempt is spent.
    assert task["attempt_count"] == task["max_attempts"] - 1
    assert task["metadata"][KEY] == 1
    (entry,) = task["blocked_by"]
    assert entry[questions_mod.RELEASE_WAIT_KEY]["sha"] == sha
    assert entry["park_phase"] == "release_wait"


# ---------------------------------------------------------------------------
# the routes' park (RemoteControlPlane -> swarm-api's attemptstate)
# ---------------------------------------------------------------------------


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


@pytest.fixture
def opener(api_db) -> RoutesOpener:
    return RoutesOpener(api_client(api_db))


def _running(api_db, opener, **extra):
    seed_route_attempt(api_db, **extra)
    control = remote_control(opener)
    control.validate_generation()
    control.advance_to_running()
    return control


def test_a_route_release_wait_refunds_by_swarm_apis_bound(api_db, opener):
    from swarm_api import attemptstate

    control = _running(api_db, opener)

    refunded = control.park_release_wait(detail=_detail(secrets.token_hex(20)),
                                         max_resumes=1_000_000)

    task = api_db.docs[f"tasks/{ROUTE_TASK}"]
    # MUTATION: leave the DEPENDENCY_INCOMPLETE branch out of `_park_payload`
    # and the route park refunds nothing.
    assert refunded is True
    assert task["attempt_count"] == 0 and task["metadata"][KEY] == 1
    assert task["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert api_db.docs[f"leases/{LEASE}"]["released_at"] is not None

    # Past swarm-api's bound, whatever the worker passed: no refund.
    api_db2 = ApiFirestore()
    control2 = _running(api_db2, RoutesOpener(api_client(api_db2)), attempt_count=2,
                        metadata={KEY: attemptstate.RELEASE_WAIT_MAX_RESUMES})
    # MUTATION: take the bound from the worker and this refunds.
    assert control2.park_release_wait(detail=_detail(secrets.token_hex(20)),
                                      max_resumes=1_000_000) is False
    assert api_db2.docs[f"tasks/{ROUTE_TASK}"]["attempt_count"] == 2


def test_a_dependency_park_without_a_release_wait_refunds_nothing(api_db, opener):
    control = _running(api_db, opener, attempt_count=2)

    control.park(reason=ParkReason.DEPENDENCY_INCOMPLETE, next_eligible_at=utcnow(),
                 detail={"park_phase": "something_else"})

    task = api_db.docs[f"tasks/{ROUTE_TASK}"]
    # MUTATION: refund every DEPENDENCY_INCOMPLETE park and this is red.
    assert task["attempt_count"] == 2
    assert KEY not in (task.get("metadata") or {})


def test_a_stale_generations_route_release_wait_is_refused_and_writes_nothing(api_db, opener):
    control = _running(api_db, opener, attempt_count=2)
    api_db.docs[f"tasks/{ROUTE_TASK}"]["current_generation"] = 2
    before = copy.deepcopy(api_db.docs)
    events_before = len(events(api_db))

    # MUTATION: skip `_fenced` in the park route and the stale park lands.
    with pytest.raises(FencedWriteRefused):
        control.park_release_wait(detail=_detail(secrets.token_hex(20)),
                                  max_resumes=control_mod.RELEASE_WAIT_MAX_RESUMES)

    assert api_db.docs == before
    assert len(events(api_db)) == events_before
    assert {n: api_db.docs[f"pools/{n}"]["active"] for n in POOLS} == {
        n: before[f"pools/{n}"]["active"] for n in POOLS}
