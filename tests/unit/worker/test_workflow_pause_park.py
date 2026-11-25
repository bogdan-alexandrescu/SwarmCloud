"""The worker's pause park (docs/design/workflow-pause.md section 5, lane P3).

A paused workflow's step carries a raw `pause_requested_at` mirror. The
worker's control poll reads it (`ControlSignals.pause_requested`, on the
direct poll and on swarm-api's fence view), and `_apply_control_signals`
answers it after the fence and the cancel and before the quota:

  terminate the runner -> checkpoint("workflow-pause") -> upload ->
  park WORKFLOW_PAUSED (one fenced transaction: PARKED, the attempt's end,
  the lease release) -> exit ExitCode.PARKED

and never a sleep: resume is the scheduler's promotion and a fresh lease,
never a worker waiting (invariant 4). What is held here:

* the order above, end to end over the in-memory Firestore, with no sleep on
  the pause path and the pools back down;
* a cancel outranks a pause: the step ends CANCELLED, never PARKED;
* a fenced worker writes nothing: no park, no release, no checkpoint pointer
  (invariant 5);
* a worker that reads the mirror before its runner starts walks to RUNNING
  and parks, without starting the agent (section 5.3);
* both polls carry the flag, and a cleared mirror reads False;
* the direct park refunds its attempt within the bound of five, on
  `metadata.pause_refunds`, as swarm-api's `AttemptState.park` does (Q1).

No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import io
import threading
import time
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode, FencedWriteRefused
from agent_worker.logs import build_logger
from agent_worker.procman import ChildProcess
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import FakeFirestore, FakeTransactionRunner
from state_routes_harness import RoutesOpener, api_client, remote_control
from state_routes_harness import TASK as ROUTE_TASK
from state_routes_harness import seed_attempt as seed_route_attempt
from worker_seeds import TENANT, seed_attempt

PAUSED = ParkReason.WORKFLOW_PAUSED
#: The owner's Q1, 2026-11-24: five, the quota park's number. Stated here so a
#: module that lost its constant fails on the refund, not on an import.
BOUND = 5
KEY = "pause_refunds"
#: A long run: 40 steps of 8 s, so only a stop ends it inside the test.
LONG = {"prompt": "pause me", "steps": 40, "sleep_seconds": 8.0}


def _record(worker: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """The pause path's steps, in the order they happen, and every sleep the
    main thread takes once the pause path has begun."""
    calls: list[tuple[str, Any]] = []
    main = threading.get_ident()
    pausing = {"on": False}

    real_terminate = ChildProcess.terminate

    def terminate(self: ChildProcess, grace_seconds: float, *, reason: str = "terminated") -> None:
        calls.append(("terminate", reason))
        if reason == "workflow paused":
            pausing["on"] = True
        return real_terminate(self, grace_seconds, reason=reason)

    monkeypatch.setattr(ChildProcess, "terminate", terminate)

    real_checkpoint = worker._checkpoint

    def checkpoint(label: str, **kw: Any) -> Any:
        calls.append(("checkpoint", label))
        if label == "workflow-pause":
            pausing["on"] = True
        return real_checkpoint(label, **kw)

    worker._checkpoint = checkpoint  # type: ignore[method-assign]

    real_park = worker.control.park

    def park(**kw: Any) -> Any:
        calls.append(("park", kw["reason"]))
        return real_park(**kw)

    worker.control.park = park  # type: ignore[method-assign]

    real_sleep = time.sleep

    def sleep(seconds: float) -> None:
        if pausing["on"] and threading.get_ident() == main:
            calls.append(("sleep", seconds))
        real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    return calls


def _pause_once_running(worker: Any, db: FakeFirestore, **also: Any) -> None:
    """Write the mirror once the agent process exists, so the loop meets it
    mid-run at its next control poll; never on a wall-clock guess."""
    real_mark = worker._mark_agent_started

    def mark() -> None:
        real_mark()
        task = db.doc("tasks/task_1")
        task["pause_requested_at"] = utcnow()
        task.update(also)

    worker._mark_agent_started = mark  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# the running worker
# ---------------------------------------------------------------------------


def test_the_poll_flag_terminates_checkpoints_parks_and_exits_without_a_sleep(
        db, store, worker_factory, monkeypatch):
    seed_attempt(db, pool_active=4, task_input=dict(LONG))
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=60)
    calls = _record(worker, monkeypatch)
    _pause_once_running(worker, db)

    started = time.monotonic()
    exit_code = worker.run()

    assert exit_code == ExitCode.PARKED
    # Well inside the run's own 320 s and the 60 s limit: the worker did not
    # wait for a resume that nobody sent (invariant 4).
    assert time.monotonic() - started < 45
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == PAUSED.value
    assert task["current_lease_id"] is None
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None
    assert lease["release_reason"] == f"parked:{PAUSED.value}"
    for pool in lease["pools"]:
        assert db.doc(f"pools/{pool}")["active"] == 3, pool

    # MUTATION: park before the checkpoint, or checkpoint under "periodic"
    # (which an unchanged workspace may skip), and resume loses the work.
    steps = [c for c in calls if c[0] in ("terminate", "checkpoint", "park")
             and c != ("checkpoint", "periodic")]
    assert steps == [("terminate", "workflow paused"), ("checkpoint", "workflow-pause"),
                     ("park", PAUSED)], calls
    # MUTATION: wait for the resume (or any backoff) on the pause path.
    assert [c for c in calls if c[0] == "sleep"] == [], calls

    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert len(parked) == 1
    assert parked[0]["detail"]["reason"] == PAUSED.value
    assert parked[0]["detail"]["park_phase"] == "workflow_pause"
    assert EventType.LEASE_RELEASED.value in db.event_types("task_1")
    # The checkpoint the next attempt restores from.
    manifests = [k for k in store.list_keys("tenants/") if k.endswith("manifest.json")]
    assert manifests, "a paused attempt must leave a checkpoint behind"


def test_cancel_outranks_pause(db, worker_factory, monkeypatch):
    seed_attempt(db, task_input=dict(LONG))
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=60)
    calls = _record(worker, monkeypatch)
    _pause_once_running(worker, db, cancel_requested=True)

    # MUTATION: check the pause before the cancel and a cancelled step is
    # parked, to be promoted again on resume.
    assert worker.run() == ExitCode.CANCELLED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value
    assert task.get("park_reason") != PAUSED.value
    assert ("park", PAUSED) not in calls
    assert ("checkpoint", "workflow-pause") not in calls
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_a_fenced_worker_writes_nothing_for_a_pause(db, worker_factory, monkeypatch):
    seed_attempt(db, task_input=dict(LONG))
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=60)
    calls = _record(worker, monkeypatch)
    # A newer generation owns the task, and its step is paused too.
    _pause_once_running(worker, db, current_generation=2)

    # MUTATION: check the pause before the fence and a superseded worker
    # parks (or tries to) a task that is no longer its own.
    assert worker.run() == ExitCode.GENERATION_FENCED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value
    assert task.get("park_reason") is None
    assert task["current_lease_id"] == "lease_1"
    assert db.doc("leases/lease_1")["released_at"] is None
    assert ("park", PAUSED) not in calls
    assert ("checkpoint", "workflow-pause") not in calls
    assert EventType.PARKED.value not in db.event_types("task_1")


def test_a_pre_running_worker_walks_to_running_and_parks(db, store, worker_factory,
                                                         monkeypatch):
    """The mirror was on the step before the worker opened it (a LEASED step,
    section 5.3): it walks to RUNNING, since a worker has no pre-running edge
    into PARKED, and parks with nothing to terminate. The agent never starts."""
    seed_attempt(db, state=TaskState.LEASED, task_input=dict(LONG))
    db.doc("tasks/task_1")["pause_requested_at"] = utcnow()
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=60)
    calls = _record(worker, monkeypatch)
    started: list[bool] = []
    real_mark = worker._mark_agent_started

    def mark() -> None:
        started.append(True)
        real_mark()

    worker._mark_agent_started = mark  # type: ignore[method-assign]

    assert worker.run() == ExitCode.PARKED
    # MUTATION: start the agent and park only at the loop's first poll.
    assert started == []
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == PAUSED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    types = db.event_types("task_1")
    # It walked to RUNNING before it parked: the only worker edge into PARKED.
    assert types.index(EventType.RUNNING.value) < types.index(EventType.PARKED.value), types
    assert ("checkpoint", "workflow-pause") in calls
    assert calls.index(("checkpoint", "workflow-pause")) < calls.index(("park", PAUSED))
    assert [c for c in calls if c[0] == "sleep"] == [], calls
    manifests = [k for k in store.list_keys("tenants/") if k.endswith("manifest.json")]
    assert manifests, "the restored workspace is checkpointed before the park"


# ---------------------------------------------------------------------------
# the signal, on both polls
# ---------------------------------------------------------------------------


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


def test_the_direct_poll_and_open_read_the_mirror():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    # MUTATION: a default of True, or a read of the wrong key.
    assert control.poll().pause_requested is False
    assert control.validate_generation().pause_requested is False

    db.doc("tasks/task_1")["pause_requested_at"] = utcnow()
    assert control.poll().pause_requested is True
    assert control.validate_generation().pause_requested is True

    # A resume clears the mirror; the next poll no longer asks for a park.
    db.doc("tasks/task_1")["pause_requested_at"] = None
    assert control.poll().pause_requested is False


def test_the_routes_poll_and_open_read_the_fence_views_flag():
    db = ApiFirestore()
    opener = RoutesOpener(api_client(db))
    seed_route_attempt(db, pause_requested_at=utcnow())
    control = remote_control(opener)
    assert control.validate_generation().pause_requested is True
    control.advance_to_running()
    assert control.poll().pause_requested is True

    db.docs[f"tasks/{ROUTE_TASK}"]["pause_requested_at"] = None
    assert control.poll().pause_requested is False


# ---------------------------------------------------------------------------
# the direct park's refund (Q1), as swarm-api's AttemptState.park refunds it
# ---------------------------------------------------------------------------


def _admit_direct(db: FakeFirestore, *, attempt_count: int) -> ControlPlane:
    metadata = copy.deepcopy((db.documents.get("tasks/task_1") or {}).get("metadata"))
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=attempt_count)
    if metadata is not None:
        db.doc("tasks/task_1")["metadata"] = metadata
    return _control(db)


def _pause_park(control: ControlPlane) -> Any:
    return control.park(reason=PAUSED, next_eligible_at=utcnow(),
                        detail={"park_phase": "workflow_pause"})


def test_the_bound_matches_swarm_apis():
    from swarm_api import attemptstate

    assert control_mod.PAUSE_REFUNDS_METADATA_KEY == KEY == attemptstate.PAUSE_REFUNDS
    assert control_mod.PAUSE_MAX_REFUNDS == BOUND == attemptstate.PAUSE_MAX_REFUNDS


def test_direct_pause_parks_one_to_five_are_refunded_and_the_sixth_spends_an_attempt():
    db = FakeFirestore()
    count = 3
    for park in range(1, BOUND + 2):
        control = _admit_direct(db, attempt_count=count)
        refunded = _pause_park(control)
        task = db.doc("tasks/task_1")
        assert task["state"] == TaskState.PARKED.value
        assert task["park_reason"] == PAUSED.value
        parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value][-1]
        if park <= BOUND:
            # MUTATION: no refund and two pauses fail a three-attempt step.
            assert refunded is True, park
            assert task["attempt_count"] == count - 1, park
            assert task["metadata"][KEY] == park
            assert parked["detail"]["attempt_refunded"] is True
            assert parked["detail"][KEY] == park
        else:
            # MUTATION: no bound and a step that only parks never ends.
            assert refunded is False
            assert task["attempt_count"] == count
            assert task["metadata"][KEY] == BOUND
            assert parked["detail"]["attempt_refunded"] is False
        count = task["attempt_count"] + 1


def test_a_pause_park_does_not_spend_the_quota_counter():
    db = FakeFirestore()
    control = _admit_direct(db, attempt_count=2)
    _pause_park(control)
    metadata = db.doc("tasks/task_1")["metadata"]
    # Its own counter, never another reason's.
    assert metadata.get(control_mod.QUOTA_PARK_RESUMES_METADATA_KEY) is None
    assert metadata[KEY] == 1


def test_a_stale_generations_pause_park_is_refused_and_writes_nothing():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, generation=1, task_generation=2,
                 attempt_count=3)
    db.doc("tasks/task_1")["pause_requested_at"] = utcnow()
    control = _control(db)
    before = copy.deepcopy(db.documents)
    db.writes.clear()

    with pytest.raises(FencedWriteRefused):
        _pause_park(control)

    assert db.writes == []
    assert db.documents == before

