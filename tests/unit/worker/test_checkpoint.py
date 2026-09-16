"""Checkpointing: an interruption must cost minutes, not the whole attempt.

Cloud Run's ephemeral disk disables live migration, so these paths are the only
thing standing between an infrastructure event and a lost two-hour agent run.
The round trip is tested end to end -- archive, upload, discover, download,
verify, extract -- against a real object store, and then again through the whole
worker, because "the checkpoint uploaded" and "a later attempt actually resumed
from it" are different claims.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import CheckpointError, ExitCode
from agent_worker.logs import build_logger
from swarm_common.states import EventType, TaskState

from conftest import BUCKET, TENANT, seed_attempt


def _manager(store, logger, *, attempt_id="att_1", generation=1):
    return CheckpointManager(
        store=store,
        tenant_id=TENANT,
        task_id="task_1",
        attempt_id=attempt_id,
        generation=generation,
        logger=logger,
    )


def test_checkpoint_round_trip_restores_into_a_fresh_workspace(store, tmp_path, log_stream):
    logger = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=log_stream,
    )
    original = workspace_mod.create(tmp_path / "ws", "att_1")
    (original.work / "progress").mkdir()
    (original.work / "progress" / "step-0001.txt").write_text("step one\n")
    (original.work / "state.json").write_text(json.dumps({"completed_steps": 1}))
    (original.work / "nested" / "deep").mkdir(parents=True)
    (original.work / "nested" / "deep" / "file.bin").write_bytes(b"\x00\x01\x02" * 1000)

    manager = _manager(store, logger)
    record = manager.create(original)

    # Deterministic, identifier-derived path, and the manifest is the commit.
    expected = (
        f"tenants/{TENANT}/tasks/task_1/attempts/att_1/checkpoints/{record.checkpoint_id}"
    )
    assert record.archive_key == f"{expected}/archive.tar.gz"
    assert store.exists(f"{expected}/manifest.json")
    assert record.file_count == 3

    # A resumed worker: brand new attempt, brand new empty workspace.
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    assert not any(resumed.work.iterdir())

    manager2 = _manager(store, logger, attempt_id="att_2", generation=2)
    found = manager2.find_latest()
    assert found is not None and found.checkpoint_id == record.checkpoint_id

    restored = manager2.restore(found, resumed)
    assert restored == 3
    assert (resumed.work / "progress" / "step-0001.txt").read_text() == "step one\n"
    assert json.loads((resumed.work / "state.json").read_text())["completed_steps"] == 1
    assert (resumed.work / "nested" / "deep" / "file.bin").read_bytes() == b"\x00\x01\x02" * 1000
    # The archive staging area is not left behind to be re-checkpointed.
    assert not (resumed.restore / "archive.tar.gz").exists()


def test_restore_refuses_a_dirty_workspace(store, tmp_path, log_stream):
    logger = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=log_stream,
    )
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "a.txt").write_text("a")
    manager = _manager(store, logger)
    record = manager.create(ws)

    dirty = workspace_mod.create(tmp_path / "ws2", "att_2")
    (dirty.work / "leftover.txt").write_text("from a previous attempt")
    manager2 = _manager(store, logger, attempt_id="att_2")
    with pytest.raises(CheckpointError, match="fresh ephemeral runtime"):
        manager2.restore(record, dirty)


def test_corrupt_archive_is_detected(store, tmp_path, log_stream):
    logger = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=log_stream,
    )
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "a.txt").write_text("a")
    manager = _manager(store, logger)
    record = manager.create(ws)
    store.upload_bytes(record.archive_key, b"not a tarball at all")

    fresh = workspace_mod.create(tmp_path / "ws2", "att_2")
    with pytest.raises(CheckpointError, match="integrity"):
        _manager(store, logger, attempt_id="att_2").restore(record, fresh)


def test_workspace_create_wipes_a_leftover_tree(tmp_path):
    first = workspace_mod.create(tmp_path / "ws", "att_1")
    (first.work / "stale.txt").write_text("previous execution")
    second = workspace_mod.create(tmp_path / "ws", "att_1")
    assert second.root == first.root
    assert not any(second.work.iterdir())


def test_worker_resumes_from_the_previous_attempts_checkpoint(
    db, store, tmp_path, log_stream, worker_factory
):
    """Attempt 1 does half the work and fails; attempt 2 finishes it."""
    seed_attempt(
        db,
        attempt_id="att_1",
        lease_id="lease_1",
        generation=1,
        task_input={"prompt": "resume me", "steps": 2, "sleep_seconds": 0.05, "fail": True},
    )
    worker1, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)
    assert worker1.run() == ExitCode.FAILED
    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value
    checkpoint_uri = db.doc("tasks/task_1")["latest_checkpoint"]
    assert checkpoint_uri

    # The scheduler retries: a new attempt, a new lease, a new generation.
    seed_attempt(
        db,
        attempt_id="att_2",
        lease_id="lease_2",
        generation=2,
        latest_checkpoint=checkpoint_uri,
        task_input={"prompt": "resume me", "steps": 4, "sleep_seconds": 0.05},
    )
    worker2, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2)
    assert worker2.run() == ExitCode.OK

    summary = db.doc("tasks/task_1")["result_summary"]
    assert summary["restored_from"]["attempt_id"] == "att_1"
    runner_output = summary["runner"]["output"]
    # The two steps from attempt 1 were restored, not redone.
    assert runner_output["was_resumed"] is True
    assert runner_output["completed_steps"] == 4
    assert EventType.CHECKPOINT_RESTORED.value in db.event_types("task_1")


def test_checkpoints_are_recorded_on_the_attempt_document(db, store, tmp_path, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    attempt = db.doc("attempts/att_1")
    assert attempt["checkpoints"], "the final checkpoint must be recorded on the attempt"
    assert EventType.CHECKPOINT_COMPLETED.value in db.event_types("task_1")
