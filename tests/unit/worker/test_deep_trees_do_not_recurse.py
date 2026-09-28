"""A tree deeper than 1,000 folders is walked without recursion (#259 review).

WHAT WENT WRONG. On Python 3.11 `os.walk` and `Path.rglob` recurse, one frame
per folder level, and the default recursion limit is 1,000. Every tree the
worker walks is in the agent's reach, so an agent that made
`work/d/d/d/.../d` 1,100 folders deep raised `RecursionError` out of the disk
check (`Workspace.disk_bytes`), the output scan (`standalone_outputs.scan`) and
the restore's file count (`CheckpointManager.restore`). The checkpoint archive
walk was already iterative, but it holds one open descriptor per level, so its
depth has to be bounded too, and past the bound it refuses rather than
running out of descriptors or archiving a tree missing its deepest part.

WHAT IS PINNED. Each of the four walks finishes on a 1,100-deep tree, and
none follows a link: a link halfway down points at a folder holding a large
file, and that file is never counted, listed or archived.

MUTATION. Put `os.walk(..., followlinks=False)` back in `disk_bytes` or
`scan`, or `rglob` back in `restore`: that test raises `RecursionError` on
3.11. Drop the depth check in `_add_entry`: the checkpoint test no longer
sees a `CheckpointError`.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from agent_worker import standalone_outputs, workspace as workspace_mod
from agent_worker.checkpoint import CHECKPOINT_MAX_DEPTH, CheckpointManager, CheckpointRecord
from agent_worker.errors import CheckpointError
from agent_worker.logs import build_logger

from conftest import TENANT

DEPTH = 1100
LINK_AT = 600
OUTSIDE_BYTES = 1024 * 1024


def _logger(log_stream):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=log_stream,
    )


def _manager(store, logger):
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=logger,
    )


def _outside(tmp_path: Path) -> Path:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "big.bin").write_bytes(b"x" * OUTSIDE_BYTES)
    return target


def _deep_tree(root: Path, outside: Path, depth: int = DEPTH) -> str:
    """`root/d/d/.../d/bottom.txt`, `depth` folders deep, with a link to
    `outside` at level `LINK_AT`. Made relative to each parent's descriptor,
    so no single path the test builds is longer than one name.

    Returns the bottom file's path relative to `root`."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for level in range(1, depth + 1):
            os.mkdir("d", dir_fd=fd)
            if level == LINK_AT:
                os.symlink(str(outside), "out", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open("bottom.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
        os.write(leaf, b"at the bottom\n")
        os.close(leaf)
    finally:
        os.close(fd)
    return "/".join(["d"] * depth + ["bottom.txt"])


def test_walk_tree_reaches_the_bottom_and_never_enters_a_link(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _deep_tree(root, _outside(tmp_path))

    seen = 0
    bottom = False
    for dirpath, dirnames, filenames in workspace_mod.walk_tree(root):
        seen += 1
        assert "elsewhere" not in dirpath.parts, dirpath
        if "bottom.txt" in filenames:
            bottom = True
        assert "big.bin" not in filenames, dirpath
    # The root and every one of the DEPTH folders, and not the linked one.
    assert seen == DEPTH + 1, seen
    assert bottom, "the walk never reached the bottom folder"


def test_disk_bytes_counts_a_deep_tree_and_not_a_link_target(tmp_path):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _deep_tree(ws.work, _outside(tmp_path))

    total = ws.disk_bytes()

    assert total >= len(b"at the bottom\n"), total
    assert total < OUTSIDE_BYTES, f"{total} bytes: the link's target was counted"


def test_the_output_scan_lists_a_deep_file_and_only_names_the_link(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    bottom = _deep_tree(work, _outside(tmp_path))

    found = standalone_outputs.scan(work)

    assert bottom in found.files, sorted(found.files)[:3]
    assert not any(path.endswith("big.bin") for path in found.files), found.files
    link = "/".join(["d"] * (LINK_AT - 1) + ["out"])
    assert link in found.symlinks, found.symlinks


def test_a_checkpoint_of_a_tree_past_the_bound_is_refused_not_exhausted(
    store, tmp_path, log_stream
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _deep_tree(ws.work, _outside(tmp_path))
    manager = _manager(store, _logger(log_stream))

    with pytest.raises(CheckpointError, match="folders deep"):
        manager.create(ws, label="periodic")
    keys = [k for k in store.list_keys(f"tenants/{TENANT}/tasks/task_1/") if "/checkpoints/" in k]
    assert keys == [], keys


def test_a_checkpoint_just_inside_the_bound_is_written(store, tmp_path, log_stream):
    """The control for the refusal above: depth alone below the bound is fine."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _deep_tree(ws.work, _outside(tmp_path), depth=CHECKPOINT_MAX_DEPTH - 2)
    manager = _manager(store, _logger(log_stream))

    record = manager.create(ws, label="periodic")

    assert record is not None and record.file_count >= 1, record


def test_a_restored_tree_deeper_than_1000_folders_is_counted(store, tmp_path, log_stream):
    """An archive this deep is never WRITTEN by this worker (the bound above),
    but one in the bucket is data, and the count after a successful restore
    must not be what fails it."""
    manager = _manager(store, _logger(log_stream))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for level in range(1, DEPTH + 1):
            info = tarfile.TarInfo("/".join(["d"] * level))
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        data = b"at the bottom\n"
        info = tarfile.TarInfo("/".join(["d"] * DEPTH + ["bottom.txt"]))
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    archive = buffer.getvalue()
    prefix = f"{manager.own_prefix}att_0/checkpoints/ckpt-00001"
    archive_key = f"{prefix}/archive.tar.gz"
    store.upload_bytes(archive_key, archive, content_type="application/gzip")
    record = CheckpointRecord(
        checkpoint_id="ckpt-00001", seq=1, task_id="task_1", attempt_id="att_0",
        tenant_id=TENANT, generation=1, created_at="2026-09-28T00:00:00Z",
        archive_key=archive_key, manifest_key=f"{prefix}/manifest.json",
        archive_bytes=len(archive), archive_sha256=hashlib.sha256(archive).hexdigest(),
        file_count=1, uri=store.uri(f"{prefix}/"),
    )
    ws = workspace_mod.create(tmp_path / "ws", "att_1")

    assert manager.restore(record, ws) == 1
