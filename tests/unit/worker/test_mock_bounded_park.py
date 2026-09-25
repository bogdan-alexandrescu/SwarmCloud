"""A mock asked for a rate limit parks once, and the attempt after the park runs (#142).

THE DEFECT, from the review of PR #201. The mock raised its rate limit on EVERY
attempt: the check ran before any saved state and kept no count. A park does
not spend an attempt -- `admission.acquire_lease_in_transaction` counts the
lease, but neither `ControlPlane.park` nor the scheduler's promotion reads
`retries_exhausted` -- so a mock task sent `{"quota_exhausted": true}` was
leased, parked, promoted and leased again, a new Cloud Run execution each time,
until somebody cancelled it. That is why the bridge withheld the key.

THE OWNER'S DECISION (#142, 2026-09-25): a bounded park. The mock counts the
attempts it has parked in its own state file, `quota_exhausted_times` in
`mock_state.json`, the way `credential_revoked_times` counts its refusals in a
marker file, and parks one attempt only. The state file is in `work/`, so the
park's own checkpoint carries the count to the next attempt, which restores it
and runs.

WHAT IS NOT CHANGED: within ONE attempt the mock keeps refusing. A short
retry-after is retried in place by the worker, three times, and then parked
(`test_quota_park.py::test_short_wait_retries_in_place_then_parks_when_it_keeps_failing`);
a provider that keeps saying no must still end in a park, so a retry in place
is the same attempt and does not spend the count.

The property is asserted through the production lifecycle -- a real park, a
real checkpoint, a real restore into a fresh workspace -- and once more on the
runner directly, where the state file can be read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_worker.errors import ExitCode
from agent_worker.runners.base import QuotaExhaustedSignal, RunnerContext
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


def test_the_attempt_after_a_mock_park_finishes_instead_of_parking_again(db, worker_factory):
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_input=QUOTA_RUN)
    first, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)

    assert first.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    checkpoint = task["latest_checkpoint"]
    assert checkpoint, "a park checkpoints first; without it the count has nowhere to live"

    # What the scheduler does once `next_eligible_at` passes: a new attempt, a
    # new lease and a new generation, restoring the park's checkpoint.
    seed_attempt(
        db,
        attempt_id="att_2",
        lease_id="lease_2",
        generation=2,
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
    parks = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert len(parks) == 1, f"parked {len(parks)} times; the owner's bound is once"
    output = task["result_summary"]["runner"]["output"]
    assert output["completed_steps"] == 2
    assert output["was_resumed"] is True, "the second attempt ran from the park's checkpoint"


# -- the runner itself, where the state file can be read ---------------------------


def _ctx(work: Path, artifacts: Path, attempt_id: str) -> RunnerContext:
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        # `attempt_id` is what the lifecycle adds to every runner's input
        # (`payload.setdefault("attempt_id", ...)`), which is how the mock tells
        # a retry in place from the next attempt.
        payload={**QUOTA_RUN, "sleep_seconds": 0.0, "attempt_id": attempt_id},
    )


def test_the_park_count_is_kept_in_the_mock_state_file(tmp_path):
    from agent_worker.runners import mock

    work, artifacts = tmp_path / "work", tmp_path / "artifacts"

    with pytest.raises(QuotaExhaustedSignal) as caught:
        mock.body(_ctx(work, artifacts, "att_1"))
    assert caught.value.retry_after_seconds == 1800
    state = json.loads((work / mock.STATE_FILE).read_text())
    assert state["quota_exhausted_times"] == 1, state

    # The same attempt, retried in place: still refused, and not counted again.
    with pytest.raises(QuotaExhaustedSignal):
        mock.body(_ctx(work, artifacts, "att_1"))
    state = json.loads((work / mock.STATE_FILE).read_text())
    assert state["quota_exhausted_times"] == 1, state

    # The next attempt, in the workspace the checkpoint restored: it runs.
    out = mock.body(_ctx(work, artifacts, "att_2"))
    assert out["completed_steps"] == 2
    state = json.loads((work / mock.STATE_FILE).read_text())
    assert state["quota_exhausted_times"] == 1, state
