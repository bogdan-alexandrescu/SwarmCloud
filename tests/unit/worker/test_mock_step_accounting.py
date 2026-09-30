"""The mock runner's own accounting: resumes and completed steps (#361).

Two defects found by the 2026-09-30 acceptance run, both in figures the smoke
and checkpoint tests read to decide whether a restore worked:

* `resumed_count` counted every run that found `mock_state.json`, so an
  in-place restart of the SAME attempt (a short provider wait, a credential
  reload) read as a resume. Row 9: three in-attempt retries and one restore
  reported 4. It must equal the number of `checkpoint_restored` events: 1.
* A stop that landed INSIDE a step counted that step as completed, wrote its
  progress file and saved it, so the attempt that restored the checkpoint
  skipped work nobody did.

WRITTEN TO FAIL ON THE CODE BEFORE THE CHANGE, on their assertions.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_worker.runners import mock
from agent_worker.runners.base import RunnerContext


def _ctx(tmp_path: Path, payload: dict) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


def _run(tmp_path: Path, attempt_id: str, steps: int = 2) -> dict:
    return mock.body(
        _ctx(tmp_path, {"steps": steps, "sleep_seconds": 0.0, "attempt_id": attempt_id})
    )


def test_in_place_restarts_of_one_attempt_are_not_resumes(tmp_path):
    first = _run(tmp_path, "att_1")
    for _ in range(3):
        again = _run(tmp_path, "att_1")
    assert first["resumed_count"] == 0
    assert again["resumed_count"] == 0, (
        f"three in-place restarts of att_1 reported {again['resumed_count']} resumes; "
        "no checkpoint was restored"
    )
    assert again["was_resumed"] is False


def test_three_retries_and_one_restore_report_one_resume(tmp_path):
    """Row 9 of the 2026-09-30 acceptance run, in miniature: att_1 runs and
    checkpoints, att_2 restores that workspace and is then restarted in place
    three times. Each run has one more step to do than the last, as the real
    retries did -- each one ran and saved work before the next restart, which
    is what made the old count climb to 4."""
    _run(tmp_path, "att_1", steps=1)
    restored = _run(tmp_path, "att_2", steps=2)
    assert restored["resumed_count"] == 1
    for steps in (3, 4, 5):
        last = _run(tmp_path, "att_2", steps=steps)
    assert last["completed_steps"] == 5
    assert last["resumed_count"] == 1, (
        f"one restore and three in-attempt retries reported {last['resumed_count']}; "
        "resumed_count must equal the checkpoint_restored events, 1"
    )
    assert last["was_resumed"] is True


def test_a_restart_that_finds_all_the_work_done_is_still_not_a_resume(tmp_path):
    """A restored workspace that holds every step: the restored run completes no
    step, so the attempt's id must be saved before the loop, or the restart
    after it reads the previous attempt's id and counts the restore twice."""
    _run(tmp_path, "att_1")
    _run(tmp_path, "att_2")
    last = _run(tmp_path, "att_2")
    assert last["resumed_count"] == 1


def test_each_restore_counts_once(tmp_path):
    _run(tmp_path, "att_1")
    _run(tmp_path, "att_2")
    third = _run(tmp_path, "att_3")
    assert third["resumed_count"] == 2


def test_a_step_interrupted_by_a_stop_is_not_counted(tmp_path, monkeypatch):
    """The stop lands during step 2's sleep. Step 1 finished; step 2 did not."""
    ctx = _ctx(tmp_path, {"steps": 4, "sleep_seconds": 4.0, "attempt_id": "att_1"})
    first_step = ctx.work_dir / mock.PROGRESS_DIR / "step-0001.txt"

    def sleep(_: float) -> None:
        # The first nap after step 1 was recorded is inside step 2.
        if first_step.exists():
            ctx.stop_requested = True

    monkeypatch.setattr(mock.time, "sleep", sleep)
    out = mock.body(ctx)

    assert out["completed_steps"] == 1, (
        f"a stop inside step 2 reported {out['completed_steps']} completed steps; "
        "only step 1 finished"
    )
    progress = sorted(p.name for p in (ctx.work_dir / mock.PROGRESS_DIR).iterdir())
    assert progress == ["step-0001.txt"], progress
    saved = json.loads((ctx.work_dir / mock.STATE_FILE).read_text())
    assert saved["completed_steps"] == 1, "the checkpointed state skips the unfinished step"


def test_a_stop_before_any_step_finishes_counts_none(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"steps": 2, "sleep_seconds": 2.0, "attempt_id": "att_1"})

    def sleep(_: float) -> None:
        ctx.stop_requested = True

    monkeypatch.setattr(mock.time, "sleep", sleep)
    out = mock.body(ctx)
    assert out["completed_steps"] == 0
    assert not list((ctx.work_dir / mock.PROGRESS_DIR).iterdir())
