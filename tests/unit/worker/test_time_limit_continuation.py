"""A step stopped at its time limit with its checkpoint uploaded continues (BGD, 2026-11-02).

MEASURED (2 November 2026, the 14 days before, Engineering workspace): 75 hours
went to runs killed at their 1 or 2 hour limit, 46 tasks, ending 37 workflows.
An attempt that reached `timeout_seconds` was killed and the task ended FAILED
(`EndCause.TIMEOUT`), and the workflow's on-step-failure rule ended the
workflow, though the final checkpoint held the work.

NOW: the attempt parks SCHEDULED_RETRY at once (one fenced transaction: PARKED,
the lease and pools released, the attempt ended), its attempt refunded and
counted in `metadata.time_limit_continuations`; the scheduler promotes it and
admission leases a NEW attempt at a new generation, which restores the
checkpoint and resumes the agent's session, told it is continuing after a time
limit. At most `TIME_LIMIT_MAX_CONTINUATIONS` (2) times: the third stop at the
limit fails as before, saying it ran out of continuations. An attempt whose
final checkpoint did not upload fails as before.
"""

from __future__ import annotations

import copy
import io
import json
import secrets
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker import lifecycle as lifecycle_mod
from agent_worker.control import ControlPlane
from agent_worker.errors import CheckpointError, ExitCode
from agent_worker.logs import build_logger
from agent_worker.runners import cliagent
from swarm_common.models import EndCause, utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from fakes import FakeFirestore, FakeTransactionRunner
from worker_seeds import TENANT, seed_attempt

KEY = "time_limit_continuations"
#: BGD, 2026-11-02: two. Stated here rather than imported, so a module that
#: lost its constant fails on the behaviour and not on an import.
CAP = 2
#: Four steps over six seconds: a two-second limit stops it after one or two.
LONG_RUN = {"prompt": "run past the limit", "steps": 4, "sleep_seconds": 6.0}


def _parks(db: FakeFirestore) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]


def _admit(db: FakeFirestore, *, attempt: int, attempt_count: int, checkpoint: Any) -> None:
    """What admission writes for the next attempt: a new lease at a new
    generation, one more `attempt_count`; the task's metadata is carried."""
    metadata = copy.deepcopy((db.documents.get("tasks/task_1") or {}).get("metadata"))
    seed_attempt(db, attempt_id=f"att_{attempt}", lease_id=f"lease_{attempt}",
                 generation=attempt, attempt_count=attempt_count,
                 latest_checkpoint=checkpoint, task_input=LONG_RUN)
    if metadata is not None:
        db.doc("tasks/task_1")["metadata"] = metadata


# ---------------------------------------------------------------------------
# the bound
# ---------------------------------------------------------------------------


def test_the_cap_is_named_once_and_swarm_api_restates_it():
    from swarm_api import attemptstate, validation

    assert control_mod.TIME_LIMIT_CONTINUATIONS_METADATA_KEY == KEY
    assert attemptstate.TIME_LIMIT_CONTINUATIONS == KEY
    assert validation.TIME_LIMIT_CONTINUATIONS_METADATA_KEY == KEY
    assert KEY in validation.RESERVED_METADATA_KEYS
    assert control_mod.TIME_LIMIT_MAX_CONTINUATIONS == CAP
    assert attemptstate.TIME_LIMIT_MAX_CONTINUATIONS == CAP
    assert attemptstate.TIME_LIMIT == control_mod.TIME_LIMIT_DETAIL_KEY
    # A continued attempt is a refunded one: the restore counts it back.
    assert KEY in lifecycle_mod.REFUNDED_RESUME_METADATA_KEYS


# ---------------------------------------------------------------------------
# the lifecycle: continue once and finish; the third stop fails; no checkpoint fails
# ---------------------------------------------------------------------------


def test_a_stop_at_the_limit_with_a_checkpoint_continues_once_and_finishes(db, worker_factory):
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_input=LONG_RUN)
    first, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1,
                                 timeout_seconds=2)

    # MUTATION: end the timed-out branch FAILED as before and this is ExitCode.TIMEOUT.
    assert first.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.SCHEDULED_RETRY.value
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None, "a park holds no capacity"
    assert task["metadata"][KEY] == 1
    assert task["attempt_count"] == 0, "a continuation refunds its attempt"
    checkpoint = task["latest_checkpoint"]
    assert checkpoint
    [parked] = _parks(db)
    assert parked["detail"]["cause"] == "time_limit_continuation"
    assert parked["detail"]["time_limit"]["continuation"] == 1
    assert parked["detail"]["time_limit"]["max_continuations"] == CAP
    assert parked["detail"]["attempt_refunded"] is True
    # The attempt list says it continued at the time limit.
    assert db.doc("attempts/att_1")["error"] == (
        "SCHEDULED_RETRY: continued at the time limit (1 of 2)"
    )

    # The scheduler's promotion and admission: a new attempt, generation 2.
    _admit(db, attempt=2, attempt_count=task["attempt_count"] + 1, checkpoint=checkpoint)
    second, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2,
                                  timeout_seconds=60)

    assert second.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert len(_parks(db)) == 1
    output = task["result_summary"]["runner"]["output"]
    assert output["was_resumed"] is True, "the continuation did not restore the checkpoint"
    assert output["completed_steps"] == 4
    # The checkpoint carried the stopped attempt's note, and the restore read it.
    # MUTATION: write the note after the final checkpoint and this is 0.
    assert second._continued_at_time_limit == 1


def test_a_third_stop_at_the_limit_with_a_cap_of_two_fails_out_of_continuations(
    db, worker_factory
):
    seed_attempt(db, attempt_id="att_3", lease_id="lease_3", generation=3, attempt_count=1,
                 task_input=LONG_RUN)
    db.doc("tasks/task_1")["metadata"] = {KEY: CAP}
    third, _, _ = worker_factory(attempt_id="att_3", lease_id="lease_3", generation=3,
                                 timeout_seconds=2)

    # MUTATION: drop the cap from `_time_limit_refusal` and this parks a third time.
    assert third.run() == ExitCode.TIMEOUT
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == EndCause.TIMEOUT.value
    assert "ran out of continuations" in task["last_error"]
    assert "2 of 2" in task["last_error"]
    assert _parks(db) == []


def test_a_stop_at_the_limit_whose_checkpoint_did_not_upload_fails_as_today(
    db, worker_factory, monkeypatch
):
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_input=LONG_RUN)
    first, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1,
                                 timeout_seconds=2, checkpoint_interval_seconds=600)

    def upload_refused(*_: object, **__: object) -> None:
        raise CheckpointError("the bucket refused the upload")

    monkeypatch.setattr(first.checkpoints, "create", upload_refused)

    # MUTATION: continue without checking the final checkpoint and this parks.
    assert first.run() == ExitCode.TIMEOUT
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == EndCause.TIMEOUT.value
    assert task["last_error"].startswith("runner exceeded its 2s timeout and was killed")
    assert _parks(db) == []
    assert KEY not in (task.get("metadata") or {})


# ---------------------------------------------------------------------------
# the next attempt resumes the session, told why
# ---------------------------------------------------------------------------


def _bare_worker(worker_factory, tmp_path, *, used: int) -> Any:
    worker, _, _ = worker_factory()
    work = tmp_path / "restored"
    work.mkdir()
    worker.ws = SimpleNamespace(work=work)
    worker._task = {"metadata": {KEY: used}}
    return worker


def test_the_continuation_resumes_the_session_the_stopped_attempt_left(
    db, worker_factory, tmp_path
):
    seed_attempt(db)
    worker = _bare_worker(worker_factory, tmp_path, used=1)
    session = f"{secrets.token_hex(4)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
    note = worker.ws.work / lifecycle_mod.CONTINUATION_NOTE_NAME
    note.write_text(json.dumps({"continuation": 1, "session_id": session}))

    worker._read_continuation_note()

    assert worker._resume_session == session
    assert worker._resume_reason == cliagent.RESUME_TIME_LIMIT
    assert not note.exists(), "the note stays out of the agent's tree"
    assert worker._continuation_env() == {}, "a resumed session is told by its channel"


def test_a_continuation_with_no_session_tells_a_fresh_start(db, worker_factory, tmp_path):
    seed_attempt(db)
    worker = _bare_worker(worker_factory, tmp_path, used=1)
    note = worker.ws.work / lifecycle_mod.CONTINUATION_NOTE_NAME
    note.write_text(json.dumps({"continuation": 1, "session_id": None}))

    worker._read_continuation_note()

    assert worker._resume_session is None
    assert worker._continuation_env() == {
        cliagent.RESUME_REASON_ENV: cliagent.RESUME_TIME_LIMIT
    }


def test_a_note_from_a_stop_that_did_not_continue_is_ignored(db, worker_factory, tmp_path):
    seed_attempt(db)
    worker = _bare_worker(worker_factory, tmp_path, used=0)
    note = worker.ws.work / lifecycle_mod.CONTINUATION_NOTE_NAME
    note.write_text(json.dumps({"continuation": 1, "session_id": "s"}))

    worker._read_continuation_note()

    assert worker._resume_session is None
    assert worker._continuation_env() == {}
    assert not note.exists()


def test_the_resumed_cli_is_told_it_continues_after_a_time_limit():
    prompt = cliagent.resume_prompt(cliagent.RESUME_TIME_LIMIT)
    assert prompt == cliagent.TIME_LIMIT_RESUME_PROMPT
    assert "continuing after a time limit" in prompt
    assert cliagent.resume_prompt(cliagent.RESUME_MOVED) == cliagent.RESUME_PROMPT


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------


class _Guard:
    def __init__(self, *, tokens: int, limit: int, exceeded: bool = False) -> None:
        self.limits = SimpleNamespace(tokens=limit, usd=None)
        self.tokens = tokens
        self.usd = None
        self.exceeded = exceeded


@pytest.mark.parametrize(
    ("used", "tokens", "limit", "exceeded", "refused"),
    [
        (0, 400, 1000, False, None),           # this attempt and the next: 800
        (0, 600, 1000, False, "budget"),       # the next would take it to 1200
        (1, 300, 1000, False, None),           # three attempts of 300
        (1, 400, 1000, False, "budget"),       # three of 400 is 1200
        (0, 100, 1000, True, "budget"),        # stopped by its budget
    ],
)
def test_a_continuation_the_budget_would_not_hold_is_refused(
    db, worker_factory, tmp_path, used, tokens, limit, exceeded, refused
):
    seed_attempt(db)
    worker = _bare_worker(worker_factory, tmp_path, used=used)
    worker.budget = _Guard(tokens=tokens, limit=limit, exceeded=exceeded)
    worker._cancel_requested_now = lambda: False

    assert worker._time_limit_refusal(object()) == refused


# ---------------------------------------------------------------------------
# the direct park and the routes' park count the same way
# ---------------------------------------------------------------------------


def _control(db: FakeFirestore) -> ControlPlane:
    return ControlPlane(
        db, task_id="task_1", attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
                            generation=1, runner_profile="mock", stream=io.StringIO()),
        txn_runner=FakeTransactionRunner(db),
    )


def _time_limit_park(control: Any, continuation: int) -> Any:
    return control.park(
        reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow(),
        detail={"cause": "time_limit_continuation",
                "time_limit": {"continuation": continuation, "max_continuations": CAP}},
    )


def test_direct_time_limit_parks_refund_within_the_cap_and_count():
    db = FakeFirestore()
    count = 1
    for park in range(1, CAP + 2):
        metadata = copy.deepcopy((db.documents.get("tasks/task_1") or {}).get("metadata"))
        seed_attempt(db, state=TaskState.RUNNING, attempt_count=count)
        if metadata is not None:
            db.doc("tasks/task_1")["metadata"] = metadata
        refunded = _time_limit_park(_control(db), park)
        task = db.doc("tasks/task_1")
        assert task["state"] == TaskState.PARKED.value
        if park <= CAP:
            assert refunded is True
            assert task["attempt_count"] == count - 1
            assert task["metadata"][KEY] == park
        else:
            assert refunded is False
            assert task["attempt_count"] == count
            assert task["metadata"][KEY] == CAP
        count = task["attempt_count"] + 1


def test_an_ordinary_scheduled_retry_park_counts_nothing():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=2)

    _control(db).park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow(),
                      detail={"cause": "worker_interrupted"})

    task = db.doc("tasks/task_1")
    assert task["attempt_count"] == 2
    assert KEY not in (task.get("metadata") or {})
    assert db.doc("attempts/att_1")["error"] == ParkReason.SCHEDULED_RETRY.value


def test_the_routes_park_counts_a_continuation_as_the_direct_one_does():
    from state_routes_harness import RoutesOpener, api_client, events, remote_control
    from state_routes_harness import TASK as ROUTE_TASK
    from state_routes_harness import seed_attempt as seed_route_attempt

    from control_plane.fakes import FakeFirestore as ApiFirestore

    api_db = ApiFirestore()
    opener = RoutesOpener(api_client(api_db))
    count = 1
    for park in range(1, CAP + 2):
        previous = api_db.docs.get(f"tasks/{ROUTE_TASK}") or {}
        seed_route_attempt(api_db, attempt_id=f"att_t{park}", lease_id=f"lease_t{park}",
                           generation=park, attempt_count=count,
                           metadata=copy.deepcopy(previous.get("metadata") or {}))
        control = remote_control(opener, attempt_id=f"att_t{park}", lease_id=f"lease_t{park}",
                                 generation=park)
        control.validate_generation()
        control.advance_to_running()
        refunded = _time_limit_park(control, park)
        task = api_db.docs[f"tasks/{ROUTE_TASK}"]
        assert task["state"] == TaskState.PARKED.value
        parked = events(api_db, kind="parked")[-1]
        if park <= CAP:
            assert refunded is True
            assert task["metadata"][KEY] == park
            assert parked["detail"]["attempt_refunded"] is True
            assert api_db.docs[f"attempts/att_t{park}"]["error"] == (
                f"SCHEDULED_RETRY: continued at the time limit ({park} of {CAP})"
            )
        else:
            assert refunded is False
            assert task["metadata"][KEY] == CAP
        count = task["attempt_count"] + 1
