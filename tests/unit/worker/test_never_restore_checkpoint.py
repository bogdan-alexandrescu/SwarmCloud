"""A profile with `never_restore_checkpoint` restores nothing, on ANY attempt (CR 36).

docs/merge-step.md §10 item 4a: `claude-code-review` judges the head it checks
out, so a workspace an earlier attempt -- or a planter -- left behind is never
brought back, not on attempt 1 and not on a retry whose checkpoint every
#347 check accepts. Next to test_no_planted_restore.py, which holds the
general rule this narrows further.

The control is the same retry on `claude-code`, the profile identical to
`claude-code-review` but for this flag: it restores, so the setup is one a
restore would accept.

MUTATIONS: drop the guard in `_restore_checkpoint` -- the review retry
restores. Read `never_restore_checkpoint` only on attempt 1 -- the same.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import EventType

from conftest import TENANT, record_as_earlier_attempt, seed_attempt


class _Quiet:
    def info(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...


def _recorded_retry(db, store, tmp_path, worker_factory, profile: str):
    """Attempt 2 of a task whose attempt 1 recorded a checkpoint, as #347 accepts."""
    earlier = workspace_mod.create(tmp_path / f"earlier-{profile}", "att_1")
    (earlier.work / "left-by-attempt-1.txt").write_text("an earlier attempt's tree\n")
    record = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=_Quiet(),
    ).create(earlier, label="periodic")
    seed_attempt(db, attempt_id="att_2", lease_id="lease_2", generation=2,
                 runner_profile=profile, attempt_count=2, latest_checkpoint=record.uri)
    record_as_earlier_attempt(db, record)
    worker, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2,
                                  runner_profile=profile)
    worker.ws = workspace_mod.create(tmp_path / f"now-{profile}", "att_2")
    return worker, db.doc("tasks/task_1")


def test_the_catalogue_sets_the_flag_on_the_review_profile_only_among_agents():
    assert RUNNER_PROFILES["claude-code-review"].never_restore_checkpoint is True
    assert RUNNER_PROFILES["claude-code"].never_restore_checkpoint is False


def test_a_review_retry_restores_nothing_even_a_recorded_checkpoint(
    db, store, tmp_path, worker_factory
):
    worker, task = _recorded_retry(db, store, tmp_path, worker_factory, "claude-code-review")
    worker._restore_checkpoint(task)
    assert worker._restored_from is None
    assert not (worker.ws.work / "left-by-attempt-1.txt").exists()
    assert EventType.CHECKPOINT_RESTORED.value not in db.event_types("task_1")


def test_the_same_retry_on_claude_code_restores(db, store, tmp_path, worker_factory):
    """The control: the setup is one `_recorded_checkpoint` accepts."""
    worker, task = _recorded_retry(db, store, tmp_path, worker_factory, "claude-code")
    worker._restore_checkpoint(task)
    assert worker._restored_from is not None
    assert (worker.ws.work / "left-by-attempt-1.txt").exists()
    assert EventType.CHECKPOINT_RESTORED.value in db.event_types("task_1")


@pytest.mark.parametrize("attempt_count", [1, 2, 5])
def test_the_guard_does_not_even_read_the_pointer(
    db, store, tmp_path, worker_factory, monkeypatch, attempt_count
):
    worker, task = _recorded_retry(db, store, tmp_path, worker_factory, "claude-code-review")
    task["attempt_count"] = attempt_count
    monkeypatch.setattr(worker, "_recorded_checkpoint",
                        lambda _task: pytest.fail("the review followed a checkpoint pointer"))
    worker._restore_checkpoint(task)
    assert worker._restored_from is None
