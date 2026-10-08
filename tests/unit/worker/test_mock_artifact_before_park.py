"""The mock's `artifact_before_park` writes its artifact before the park, and never after it (contract request 56, part of #166).

WHY IT EXISTS. #166's carry -- a file the agent wrote before a quota park
reaches the attempt that succeeds, by reference (`parked_uploads`,
`carried_from`) -- is on main, but nothing could show it live: the only runner
that parks on demand is the mock, and it parked BEFORE writing its artifact.
Request 56 (accepted by the owner 2026-10-08) gives the mock an input that
writes the artifact first and then parks. The attempt after the park writes
NOTHING under that name, so a live run's file can only have arrived by carry,
never by a rewrite.

Held here on the runner directly; the carry through the production lifecycle
is `test_parked_uploads_carry.py::test_artifact_before_park_is_carried_to_the_finishing_attempt`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker.runners import mock
from agent_worker.runners.base import QuotaExhaustedSignal, RunnerContext

PARKED_TEXT = "written before the park\n"

#: The live-proof shape: a park, and the artifact written before it.
CARRY_RUN: dict[str, Any] = {
    "prompt": "write the notes, then meet a rate limit",
    "steps": 2,
    "sleep_seconds": 0.0,
    "quota_exhausted": True,
    "retry_after_seconds": 1800,
    "artifact_before_park": True,
    "artifact_name": "notes.md",
    "artifact_text": PARKED_TEXT,
}


def _ctx(tmp: Path, attempt_count: int, payload: dict[str, Any]) -> RunnerContext:
    """A fresh workspace for attempt `attempt_count`, as every attempt gets."""
    work, artifacts = tmp / f"work-{attempt_count}", tmp / f"artifacts-{attempt_count}"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={**payload, "attempt_id": f"att_{attempt_count}", "attempt_count": attempt_count},
    )


def _files(ctx: RunnerContext) -> dict[str, str]:
    return {p.name: p.read_text() for p in ctx.artifacts_dir.iterdir() if p.is_file()}


def test_the_parking_attempt_writes_the_artifact_then_parks(tmp_path):
    ctx = _ctx(tmp_path, 1, CARRY_RUN)

    with pytest.raises(QuotaExhaustedSignal) as caught:
        mock.body(ctx)

    assert caught.value.retry_after_seconds == 1800
    assert _files(ctx) == {"notes.md": PARKED_TEXT}, (
        "the artifact was not in the artifacts folder when the mock parked, so "
        "the park's upload had nothing to carry"
    )


def test_the_attempt_after_the_park_does_not_rewrite_it(tmp_path):
    with pytest.raises(QuotaExhaustedSignal):
        mock.body(_ctx(tmp_path, 1, CARRY_RUN))

    resumed = _ctx(tmp_path, 2, CARRY_RUN)
    out = mock.body(resumed)

    assert out["completed_steps"] == 2, out
    assert "notes.md" not in _files(resumed), (
        "the attempt after the park wrote notes.md again, so a live run could not "
        "tell the carried file from a rewrite"
    )
    assert _files(resumed) == {}, _files(resumed)


def test_with_the_flag_off_the_park_writes_nothing_and_the_finish_writes_it(tmp_path):
    run = {**CARRY_RUN, "artifact_before_park": False}
    parked = _ctx(tmp_path, 1, run)

    with pytest.raises(QuotaExhaustedSignal):
        mock.body(parked)
    assert _files(parked) == {}, "the mock wrote its artifact before a park nobody asked it to"

    finished = _ctx(tmp_path, 2, run)
    mock.body(finished)
    assert _files(finished) == {"notes.md": PARKED_TEXT}


def test_without_quota_exhausted_the_flag_changes_nothing(tmp_path):
    """No park, so nothing to carry: the one attempt writes its artifact as usual."""
    ctx = _ctx(tmp_path, 1, {**CARRY_RUN, "quota_exhausted": False})

    out = mock.body(ctx)

    assert out["completed_steps"] == 2, out
    assert _files(ctx) == {"notes.md": PARKED_TEXT}
