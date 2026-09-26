"""A mock asked for a rate limit parks once, and the attempt after the park runs (#142).

THE DEFECT, from the review of PR #201. The mock raised its rate limit on EVERY
attempt: the check ran before any saved state and kept no count. A park does
not spend an attempt -- `admission.acquire_lease_in_transaction` counts the
lease, but neither `ControlPlane.park` nor the scheduler's promotion reads
`retries_exhausted` -- so a mock task sent `{"quota_exhausted": true}` was
leased, parked, promoted and leased again, a new Cloud Run execution each time,
until somebody cancelled it. That is why the bridge withheld the key.

THE OWNER'S DECISION (#142, 2026-09-25): a bounded park, one attempt.

WHERE THE COUNT LIVES (the review of #213). The first version counted parks in
the mock's own state file, `mock_state.json` in `work/`, and relied on the
park's checkpoint to carry it to the next attempt. `Worker._checkpoint` logs a
failed upload and parks anyway, as it must for a real provider, so a park whose
checkpoint failed lost the count, and the next attempt parked again: the bound
held only while every park's checkpoint succeeded. The count is now the task's
`attempt_count`, which admission increments in the lease's own transaction and
the lifecycle hands to every runner beside `attempt_id`. Nothing a checkpoint
does can lose it, so the mock parks the task's first attempt and no other.

WHAT IS NOT CHANGED: within ONE attempt the mock keeps refusing. A short
retry-after is retried in place by the worker, three times, and then parked
(`test_quota_park.py::test_short_wait_retries_in_place_then_parks_when_it_keeps_failing`);
a provider that keeps saying no must still end in a park, so a retry in place
is the same attempt, with the same count, and is refused again.

The property is asserted through the production lifecycle -- a real park, a
real checkpoint (or a failed one), a new attempt in a fresh workspace -- and
once more on the runner directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker.errors import CheckpointError, ExitCode
from agent_worker.runners.base import QuotaExhaustedSignal, RunnerContext, RunnerFailure
from swarm_common.states import EventType, ParkReason, TaskState

from conftest import seed_attempt

#: Thirty minutes is far past `max_in_worker_retry_delay_seconds`, so the worker
#: parks at once rather than retrying in place. No `provider` key: the mock's
#: own default names whose quota document the park writes, and a caller cannot
#: send one (the catalogue does not declare it).
QUOTA_RUN = {
    "prompt": "park once, then finish",
    "steps": 2,
    "sleep_seconds": 0.05,
    "quota_exhausted": True,
    "retry_after_seconds": 1800,
}


def _parks(db) -> list[dict]:
    return [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]


def test_the_attempt_after_a_mock_park_finishes_instead_of_parking_again(db, worker_factory):
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_input=QUOTA_RUN)
    first, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)

    assert first.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    checkpoint = task["latest_checkpoint"]
    assert checkpoint, "a park checkpoints first (invariant 4), whatever the mock counts"

    # What the scheduler does once `next_eligible_at` passes: a new lease, a
    # new generation, and -- in the same transaction -- `attempt_count` 2.
    seed_attempt(
        db,
        attempt_id="att_2",
        lease_id="lease_2",
        generation=2,
        attempt_count=2,
        latest_checkpoint=checkpoint,
        task_input=QUOTA_RUN,
    )
    second, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2)

    assert second.run() == ExitCode.OK, (
        "the attempt after the park parked again: the mock's rate limit is not "
        "bounded, and this task would park and resume until cancelled"
    )
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert len(_parks(db)) == 1, f"parked {len(_parks(db))} times; the owner's bound is once"
    output = task["result_summary"]["runner"]["output"]
    assert output["completed_steps"] == 2
    assert output["was_resumed"] is True, "the second attempt ran from the park's checkpoint"


def test_the_bound_holds_when_the_parks_checkpoint_fails(db, worker_factory, monkeypatch):
    """THE REVIEW OF #213. A checkpoint that fails to upload is logged and the
    park goes ahead -- the provider really is refusing, so parking is still
    right for every other runner. With the count in `work/`, the next attempt
    started from nothing, counted no park, and parked again, and so on for as
    long as uploads failed. The count is the task's own now."""
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_input=QUOTA_RUN)
    first, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)

    def upload_refused(*_: object, **__: object) -> None:
        raise CheckpointError("the bucket refused the upload")

    monkeypatch.setattr(first.checkpoints, "create", upload_refused)

    assert first.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert not task.get("latest_checkpoint"), (
        "a checkpoint was recorded; this test is about a park whose checkpoint failed"
    )

    # The next lease: nothing to restore, a fresh workspace, attempt 2.
    seed_attempt(
        db,
        attempt_id="att_2",
        lease_id="lease_2",
        generation=2,
        attempt_count=2,
        latest_checkpoint=None,
        task_input=QUOTA_RUN,
    )
    second, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2)

    assert second.run() == ExitCode.OK, (
        "the attempt after a park whose checkpoint failed parked again: the "
        "bound depends on the checkpoint, and a run of failed uploads is a task "
        "that parks until cancelled"
    )
    assert len(_parks(db)) == 1, f"parked {len(_parks(db))} times; the owner's bound is once"
    output = db.doc("tasks/task_1")["result_summary"]["runner"]["output"]
    assert output["completed_steps"] == 2
    assert output["was_resumed"] is False, "there was no checkpoint to resume from"


def test_a_callers_own_attempt_count_never_reaches_the_runner(db, worker_factory):
    """The lifecycle ASSIGNS `attempt_count` from the task document; it does not
    `setdefault` it. swarm-api refuses the key for the mock (it is not
    declared), but a runner's input is written by more than the API, and a
    count a caller could set is a bound a caller could lift."""
    seed_attempt(
        db,
        attempt_id="att_1",
        lease_id="lease_1",
        generation=1,
        attempt_count=1,
        task_input={**QUOTA_RUN, "attempt_count": 7},
    )
    worker, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)

    assert worker.run() == ExitCode.PARKED, (
        "the task's first attempt did not park: the runner read the caller's "
        "attempt_count instead of the task's"
    )


# -- the runner itself -------------------------------------------------------------


def _ctx(work: Path, artifacts: Path, attempt_count: int | None) -> RunnerContext:
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    payload = {**QUOTA_RUN, "sleep_seconds": 0.0, "attempt_id": f"att_{attempt_count}"}
    if attempt_count is not None:
        # What the lifecycle writes into every runner's input, from the task
        # document: this attempt's number, 1 for the task's first lease.
        payload["attempt_count"] = attempt_count
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


def test_the_mock_parks_the_first_attempt_and_no_other(tmp_path):
    from agent_worker.runners import mock

    work, artifacts = tmp_path / "work", tmp_path / "artifacts"

    with pytest.raises(QuotaExhaustedSignal) as caught:
        mock.body(_ctx(work, artifacts, 1))
    assert caught.value.retry_after_seconds == 1800

    # The same attempt, retried in place: still refused.
    with pytest.raises(QuotaExhaustedSignal):
        mock.body(_ctx(work, artifacts, 1))

    # The next attempt in a FRESH workspace -- the park's checkpoint never
    # arrived -- runs all the same.
    out = mock.body(_ctx(tmp_path / "work-2", tmp_path / "artifacts-2", 2))
    assert out["completed_steps"] == 2, out


def test_without_an_attempt_count_the_mock_refuses_to_simulate_a_park(tmp_path):
    """A mock started without the count cannot tell its first attempt from any
    other, and a park it cannot bound is the defect this file is about. So it
    fails -- which spends an attempt, and `max_attempts` bounds -- and says why."""
    from agent_worker.runners import mock

    with pytest.raises(RunnerFailure) as caught:
        mock.body(_ctx(tmp_path / "work", tmp_path / "artifacts", None))
    assert "attempt_count" in str(caught.value), str(caught.value)
