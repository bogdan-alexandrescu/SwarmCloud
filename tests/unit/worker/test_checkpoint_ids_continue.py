"""A resumed attempt's checkpoint ids continue from the one it restored (#174).

`restore()` said so in a comment and did not: its one line was
`self._seq = max(self._seq, 0)`, which never read `record.seq`, so every
resumed attempt started again at `ckpt-00001`. Nothing was overwritten --
`checkpoint_prefix` puts the attempt id in the key -- but two attempts of one
task each had a `ckpt-00001`, and the id alone no longer said which came first.

TWO NUMBERS, KEPT APART. The id sequence continues across attempts; the count
`CheckpointManager.seq` reports stays PER ATTEMPT, because the heartbeat sends
it as "checkpoints" (`lifecycle._progress`) and #174 asked that it keep meaning
"checkpoints this attempt wrote". The per-attempt assertions below are that
check: a fix that set `seq` from the restored record would move the heartbeat
too, and goes red here.

MUTATIONS: restore the old `max(self._seq, 0)` line (the continuation tests go
red); return the id sequence from `seq` (the per-attempt assertions go red);
drop the type check on `record.seq` (the tampered-manifest test raises in
`create`).
"""

from __future__ import annotations

import json

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger

from conftest import TENANT, seed_attempt


def _logger(log_stream):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=log_stream,
    )


def _manager(store, logger, *, attempt_id="att_1", generation=1):
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id,
        generation=generation, logger=logger,
    )


def _first_attempt(store, tmp_path, logger, *, count=3):
    ws = workspace_mod.create(tmp_path / "ws1", "att_1")
    manager = _manager(store, logger)
    records = []
    for step in range(1, count + 1):
        (ws.work / f"step-{step}.txt").write_text(f"step {step}\n")
        records.append(manager.create(ws))
    assert [r.checkpoint_id for r in records] == [
        f"ckpt-{n:05d}" for n in range(1, count + 1)
    ]
    return records


def test_a_resumed_attempt_continues_the_ids_from_the_restored_checkpoint(
    store, tmp_path, log_stream
):
    logger = _logger(log_stream)
    _first_attempt(store, tmp_path, logger)

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, logger, attempt_id="att_2", generation=2)
    found = manager.find_latest()
    assert found is not None and found.seq == 3
    manager.restore(found, resumed)
    assert manager.seq == 0, "the restore wrote no checkpoint of THIS attempt"

    record = manager.create(resumed)
    assert record.checkpoint_id == "ckpt-00004", record.checkpoint_id
    assert record.seq == 4
    assert record.archive_key.startswith(
        f"tenants/{TENANT}/tasks/task_1/attempts/att_2/checkpoints/ckpt-00004/"
    )
    assert manager.seq == 1, "the heartbeat's count is this attempt's, not the task's"

    second = manager.create(resumed)
    assert second.checkpoint_id == "ckpt-00005"
    assert manager.seq == 2


def test_the_ids_continue_from_the_checkpoint_restored_not_the_newest_in_the_bucket(
    store, tmp_path, log_stream
):
    """The pointer can name an older checkpoint than `find_latest` would pick.
    The ids continue from the one restored; the attempt's own prefix is what
    keeps them from colliding with the newer ones, as it always has."""
    logger = _logger(log_stream)
    records = _first_attempt(store, tmp_path, logger)

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, logger, attempt_id="att_2", generation=2)
    found = manager.find_by_uri(records[1].uri)
    assert found is not None and found.checkpoint_id == "ckpt-00002"
    manager.restore(found, resumed)

    record = manager.create(resumed)
    assert record.checkpoint_id == "ckpt-00003"
    assert "/attempts/att_2/" in record.archive_key
    assert store.exists(records[2].manifest_key), "attempt 1's ckpt-00003 is untouched"


@pytest.mark.parametrize("bad_seq", ["7", -1, True, None, 2.5])
def test_a_manifest_seq_that_is_not_a_count_is_not_continued_from(
    store, tmp_path, log_stream, bad_seq
):
    """The manifest is data read from a bucket. A `seq` that is not a
    non-negative integer must not stop this attempt checkpointing: formatting
    `"7"` into `ckpt-{:05d}` raises in every later `create`, and invariant 8
    says checkpointing is mandatory. The ids start again at 1 instead."""
    logger = _logger(log_stream)
    records = _first_attempt(store, tmp_path, logger, count=1)
    manifest = json.loads(store.download_bytes(records[0].manifest_key))
    manifest["seq"] = bad_seq
    store.upload_bytes(records[0].manifest_key, json.dumps(manifest).encode("utf-8"))

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, logger, attempt_id="att_2", generation=2)
    found = manager.find_by_uri(records[0].uri)
    assert found is not None
    manager.restore(found, resumed)

    record = manager.create(resumed)
    assert record.checkpoint_id == "ckpt-00001"
    assert manager.seq == 1


def test_through_the_whole_worker_the_second_attempts_ids_follow_the_first(
    db, store, tmp_path, worker_factory
):
    """The attempt documents are where a person reads the ids from."""
    seed_attempt(
        db, attempt_id="att_1", lease_id="lease_1", generation=1,
        task_input={"prompt": "resume me", "steps": 2, "sleep_seconds": 0.05, "fail": True},
    )
    worker1, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)
    assert worker1.run() == ExitCode.FAILED
    pointer = db.doc("tasks/task_1")["latest_checkpoint"]
    first = db.doc("attempts/att_1")["checkpoints"]
    assert first, "attempt 1 wrote no checkpoint"
    restored_id = pointer.rstrip("/").rsplit("/", 1)[-1]

    seed_attempt(
        db, attempt_id="att_2", lease_id="lease_2", generation=2, attempt_count=2,
        latest_checkpoint=pointer,
        task_input={"prompt": "resume me", "steps": 4, "sleep_seconds": 0.05},
    )
    worker2, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2)
    assert worker2.run() == ExitCode.OK

    second = db.doc("attempts/att_2")["checkpoints"]
    assert second, "attempt 2 wrote no checkpoint"
    restored_seq = int(restored_id.removeprefix("ckpt-"))
    assert second[0] == f"ckpt-{restored_seq + 1:05d}", (first, restored_id, second)
