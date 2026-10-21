"""A big tree's checkpoints archive what changed, not the whole tree again (CHECKPOINT-FAST, #1822).

Measured 2026-10-21: a checkpoint takes about 300-560 files/s, so a tree of
110k-240k files took 371-561 s. Four causes, one property each here:

1. the chain was rebased (a full re-read) on the PERIODIC checkpoint right
   after a big incremental, because its bytes outweighed the small full
   archive under it -- task_8cff3654's 561 s full of 240,122 files. A large
   tree's periodic checkpoint now stays incremental; the final, park and
   cancellation checkpoints, and the chain-length bound, still rebase;
2. the first checkpoint after a restore was always full: the index was set
   only by `create`, so each retry re-archived the ~102k files it had just
   restored. The restore now seeds the index, and continues the chain under
   this attempt's own prefix, so the first checkpoint holds only what changed;
3. compression is zlib level 6, not 9, and a level-9 archive still restores;
4. "checkpoint written" carries `walk_s`, `archive_s`, `upload_s` and `entries`.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import tarfile
import time
import zlib
from pathlib import Path

from agent_worker import checkpoint as checkpoint_mod
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import ARCHIVE_NAME, CheckpointManager, checkpoint_prefix
from agent_worker.logs import build_logger

from worker_seeds import TENANT


def _logger(stream):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=stream,
    )


def _manager(store, logger, attempt_id="att_1"):
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id,
        generation=1, logger=logger,
    )


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _members(store, record) -> list[str]:
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return archive.getnames()


def _tree(root: Path) -> dict[str, tuple]:
    """Every entry under `root`: its type, mode, and content or link target."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                out[rel] = ("file", stat.S_IMODE(st.st_mode), path.read_bytes())
    return out


def _records(log_stream) -> list[dict]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


def _settled(monkeypatch) -> None:
    """Shrink the racy window, and wait it out, so files written above count as settled."""
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 50_000_000)
    time.sleep(0.1)


def _small_full_then_big_incremental(store, tmp_path, log_stream, monkeypatch):
    """task_8cff3654's shape: a small full archive, then an incremental that outweighs it."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    for index in range(8):
        _write(ws.work / "src" / f"f{index}.py", f"print({index})\n")
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    full = manager.create_if_changed(ws)
    assert full is not None and full.base_checkpoint_id is None
    # Incompressible, so this incremental's bytes are past the full's.
    (ws.work / "node_like" / "blob.bin").parent.mkdir()
    (ws.work / "node_like" / "blob.bin").write_bytes(os.urandom(256 * 1024))
    time.sleep(0.1)
    big = manager.create_if_changed(ws)
    assert big is not None and big.base_checkpoint_id == full.checkpoint_id
    assert big.archive_bytes >= full.archive_bytes
    _write(ws.work / "src" / "f0.py", "print('edited')\n")
    time.sleep(0.1)
    return ws, manager, big


# ---------------------------------------------------------------------------
# 1. a large tree's periodic checkpoint does not rebase on the bytes rule
# ---------------------------------------------------------------------------

def test_a_large_trees_periodic_checkpoint_after_a_big_incremental_is_incremental(
    store, tmp_path, log_stream, monkeypatch
):
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_LARGE_TREE_ENTRIES", 5)
    ws, manager, big = _small_full_then_big_incremental(store, tmp_path, log_stream, monkeypatch)

    periodic = manager.create_if_changed(ws)

    # MUTATION: rebase a periodic checkpoint whatever the tree's size (the
    # bytes rule as it was) -> this is a full archive and its base is None.
    assert periodic is not None
    assert periodic.base_checkpoint_id == big.checkpoint_id
    # MUTATION: archive the whole tree on the periodic -> f1..f7 travel too.
    assert _members(store, periodic) == ["src/f0.py"]

    # The chain it left restores into exactly the tree.
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    _manager(store, _logger(log_stream), attempt_id="att_2").restore(periodic, resumed)
    assert _tree(resumed.work) == _tree(ws.work)


def test_a_small_trees_periodic_checkpoint_keeps_the_bytes_rule(
    store, tmp_path, log_stream, monkeypatch
):
    """The control: below the threshold, today's rule rebases as it did."""
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_LARGE_TREE_ENTRIES", 10_000)
    ws, manager, _big = _small_full_then_big_incremental(store, tmp_path, log_stream, monkeypatch)

    periodic = manager.create_if_changed(ws)

    # MUTATION: never rebase on the bytes rule -> this stays incremental.
    assert periodic is not None and periodic.base_checkpoint_id is None


def test_the_final_checkpoint_still_rebases(store, tmp_path, log_stream, monkeypatch):
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_LARGE_TREE_ENTRIES", 5)
    ws, manager, _big = _small_full_then_big_incremental(store, tmp_path, log_stream, monkeypatch)

    final = manager.create(ws, label="final")

    # MUTATION: exempt every checkpoint of a large tree from the bytes rule,
    # not only the periodic one -> the final stays incremental.
    assert final.base_checkpoint_id is None
    assert len(_members(store, final)) >= 9


def test_a_large_trees_periodic_chain_still_ends_at_the_length_bound(
    store, tmp_path, log_stream, monkeypatch
):
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_LARGE_TREE_ENTRIES", 2)
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_CHAIN_MAX", 2)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    for index in range(4):
        _write(ws.work / f"seed{index}.txt", f"{index}\n")
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    bases = []
    for index in range(5):
        _write(ws.work / f"f{index}.txt", "y" * 50_000 + str(index))
        time.sleep(0.1)
        record = manager.create_if_changed(ws)
        assert record is not None
        bases.append(record.base_checkpoint_id)
    # MUTATION: skip the length bound on a large tree's periodic checkpoint
    # -> the chain grows past CHECKPOINT_CHAIN_MAX and there is no second None.
    assert bases == [None, "ckpt-00001", "ckpt-00002", None, "ckpt-00004"]


# ---------------------------------------------------------------------------
# 2. the first checkpoint after a restore archives only what changed
# ---------------------------------------------------------------------------

def _earlier_attempt(store, tmp_path, log_stream, monkeypatch):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    # Incompressible, so the full archive outweighs the incremental on it and
    # only a seeded index explains a small first checkpoint after the restore.
    (ws.work / "repo").mkdir(parents=True)
    (ws.work / "repo" / "big.bin").write_bytes(os.urandom(200_000))
    _write(ws.work / "repo" / "edit.txt", "v1\n")
    _write(ws.work / "repo" / "gone.txt", "deleted after the restore\n")
    os.symlink("edit.txt", ws.work / "repo" / "alias")
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    manager.create(ws)
    _write(ws.work / "repo" / "second.txt", "from the second checkpoint\n")
    time.sleep(0.1)
    head = manager.create(ws)
    assert head.base_checkpoint_id == "ckpt-00001"
    return head


def test_the_first_checkpoint_after_a_restore_archives_only_changed_files(
    store, tmp_path, log_stream, monkeypatch
):
    head = _earlier_attempt(store, tmp_path, log_stream, monkeypatch)
    # No window: every file the restore wrote is settled before its scan reads it.
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 0)

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, _logger(log_stream), attempt_id="att_2")
    manager.restore(head, resumed)
    work = resumed.work
    _write(work / "repo" / "edit.txt", "v2, changed after the restore\n")
    _write(work / "repo" / "new.txt", "new after the restore\n")
    (work / "repo" / "gone.txt").unlink()

    first = manager.create_if_changed(resumed)

    # MUTATION: leave `_index` unset by `restore` -> a full archive, base None.
    assert first is not None
    assert first.checkpoint_id == "ckpt-00003"
    assert first.base_checkpoint_id == head.checkpoint_id
    assert first.base_archive_sha256 == head.archive_sha256
    # MUTATION: seed the index from the archive's signatures rather than the
    # restored tree's -> every restored file reads as changed and travels.
    # MUTATION: never archive a restored file that changed -> edit.txt is missing.
    assert sorted(_members(store, first)) == ["repo/edit.txt", "repo/new.txt"]

    # The chain stays inside this attempt's own prefix: the restored layers
    # are under att_2, so a reclaim of att_1 cannot take a base away.
    for checkpoint_id in ("ckpt-00001", "ckpt-00002"):
        key = checkpoint_prefix(
            tenant_id=TENANT, task_id="task_1", attempt_id="att_2", checkpoint_id=checkpoint_id
        )
        # MUTATION: skip re-uploading the restored layers -> the base is
        # missing under att_2 and the restore below fails.
        assert store.exists(f"{key}/{ARCHIVE_NAME}"), checkpoint_id
        # ... each with a manifest that says what it is, so the console lists
        # a restored copy rather than a checkpoint that was never committed.
        manifest = json.loads(store.download_bytes(f"{key}/manifest.json"))
        # MUTATION: copy the archive alone -> no manifest to read.
        assert manifest["label"] == "restored"
        assert manifest["restored_from_attempt"] == "att_1"
        assert manifest["attempt_id"] == "att_2"
    # ... and it restores the changed tree, deletion included, byte-identical.
    again = workspace_mod.create(tmp_path / "ws3", "att_3")
    _manager(store, _logger(log_stream), attempt_id="att_3").restore(first, again)
    assert _tree(again.work) == _tree(work)
    assert not (again.work / "repo" / "gone.txt").exists()


def test_an_unchanged_tree_after_a_restore_writes_no_periodic_checkpoint(
    store, tmp_path, log_stream, monkeypatch
):
    head = _earlier_attempt(store, tmp_path, log_stream, monkeypatch)
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 0)
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, _logger(log_stream), attempt_id="att_2")
    manager.restore(head, resumed)

    # MUTATION: leave `_index` unset by `restore` -> a full archive is written.
    assert manager.create_if_changed(resumed) is None
    assert manager.last_unchanged is True


def test_a_restored_checkpoint_whose_ids_restart_starts_a_new_chain(
    store, tmp_path, log_stream, monkeypatch
):
    """A manifest `seq` the restore will not continue from restarts the ids at 1,
    which would overwrite the restored layers' copies; the chain starts again."""
    head = _earlier_attempt(store, tmp_path, log_stream, monkeypatch)
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 0)
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, _logger(log_stream), attempt_id="att_2")
    manager.restore(dataclasses.replace(head, seq=True), resumed)
    _write(resumed.work / "repo" / "edit.txt", "v2\n")

    first = manager.create_if_changed(resumed)

    # MUTATION: seed the chain whatever the restored seq -> ckpt-00001 is
    # written over the copy of the full layer and names ckpt-00002 its base.
    assert first is not None and first.checkpoint_id == "ckpt-00001"
    assert first.base_checkpoint_id is None


# ---------------------------------------------------------------------------
# 3. level 6, and old level-9 archives still restore
# ---------------------------------------------------------------------------

def _spied_levels(monkeypatch) -> list[int]:
    levels: list[int] = []
    real = zlib.compressobj

    def spy(level=-1, *args, **kwargs):
        levels.append(level)
        return real(level, *args, **kwargs)

    monkeypatch.setattr(zlib, "compressobj", spy)
    return levels


def test_an_archive_written_at_level_6_restores_byte_identical(
    store, tmp_path, log_stream, monkeypatch
):
    levels = _spied_levels(monkeypatch)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "repo" / "text.txt", "the same line again\n" * 5_000)
    (ws.work / "repo" / "random.bin").write_bytes(os.urandom(100_000))
    os.symlink("text.txt", ws.work / "repo" / "alias")
    os.chmod(ws.work / "repo" / "text.txt", 0o750)
    record = _manager(store, _logger(log_stream)).create(ws)

    # MUTATION: compress at level 9 (as before) -> the spy reads 9.
    assert levels == [6]
    # The header's XFL is 0 at level 6 (RFC 1952: 2 is "maximum compression").
    assert store.download_bytes(record.archive_key)[8] == 0

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    _manager(store, _logger(log_stream), attempt_id="att_2").restore(record, resumed)
    # MUTATION: switch the archive format -> the r:gz restore cannot read it.
    assert _tree(resumed.work) == _tree(ws.work)


def test_a_level_9_archive_an_older_worker_wrote_still_restores(
    store, tmp_path, log_stream, monkeypatch
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "a.txt", "written by an older worker\n" * 100)
    _settled(monkeypatch)
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_GZIP_LEVEL", 9)
    manager = _manager(store, _logger(log_stream))
    old_full = manager.create(ws)
    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_GZIP_LEVEL", 6)
    _write(ws.work / "b.txt", "written by this worker\n")
    head = manager.create(ws)
    assert head.base_checkpoint_id == old_full.checkpoint_id

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    # MUTATION: make the restore read only level-6 streams -> this raises.
    _manager(store, _logger(log_stream), attempt_id="att_2").restore(head, resumed)
    assert _tree(resumed.work) == _tree(ws.work)


# ---------------------------------------------------------------------------
# 4. the written line carries the durations
# ---------------------------------------------------------------------------

def test_the_written_line_carries_the_walk_archive_and_upload_durations(
    store, tmp_path, log_stream, monkeypatch
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "repo" / "a.txt", "a\n")
    _write(ws.work / "repo" / "b.txt", "b\n")
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    manager.create(ws)
    _write(ws.work / "repo" / "c.txt", "c\n")
    time.sleep(0.1)
    manager.create(ws)

    written = [r for r in _records(log_stream) if r["message"] == "checkpoint written"]
    assert [r["kind"] for r in written] == ["full", "incremental"]
    for record in written:
        # MUTATION: drop any one of the four fields from the line -> KeyError.
        for field in ("walk_s", "archive_s", "upload_s"):
            assert isinstance(record[field], float), (field, record)
            assert record[field] >= 0.0, (field, record)
    # MUTATION: log the archived member count as `entries` -> 3 and 1.
    assert [r["entries"] for r in written] == [3, 4]
    # A full archive has no separate stat-only walk; an incremental has one.
    assert written[0]["walk_s"] == 0.0
