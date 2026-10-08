"""A checkpoint never archives what `work/` points at when `work/` is a link.

The #227 finding (wave 2026-09-26, from the standalone-outputs lane): "A
checkpoint archives the target of `work/` when `work/` itself has been replaced
by a link". `_write_archive` walked `Path(source).rglob("*")`, and `rglob` does
not descend into a link BELOW the root but lists the target of a linked ROOT,
so an agent that swapped its working directory for a link to any directory its
uid can read had that directory uploaded as the checkpoint, up to
`max_checkpoint_bytes`, and served back by every checkpoint route.

The worker and the agent share a uid, so this is not a read the agent could not
make itself. It is the worker PUBLISHING it: into the bucket, and as the newest
checkpoint, the one a resume restores.

A REFUSAL, NOT AN EMPTY CHECKPOINT. An empty archive would commit as the newest
checkpoint and a resume would restore nothing, discarding the last real one.
Refusing leaves that one the newest.

MUTATIONS: open the root without `O_NOFOLLOW` (the refusal tests go red); make
the refusal a path-level `is_symlink()` check (the lying-check test goes red,
because that check is exactly what a swap after it defeats); descend into a
linked subdirectory (the below-the-root test goes red); re-raise a
`PermissionError` from the walk (the unreadable-folder test fails the
checkpoint); say "a link" for every root-open error (the EMFILE test).
"""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import CheckpointError
from agent_worker.logs import build_logger

from worker_seeds import TENANT

OUTSIDE_SECRET = "outside-the-work-tree-8812"


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
    (target / "secret.txt").write_text(OUTSIDE_SECRET)
    return target


def _swap_work_for_a_link(ws, target: Path) -> None:
    shutil.rmtree(ws.work)
    ws.work.symlink_to(target, target_is_directory=True)


def _checkpoint_keys(store) -> list[str]:
    return [k for k in store.list_keys(f"tenants/{TENANT}/tasks/task_1/") if "/checkpoints/" in k]


def test_a_linked_work_is_refused_and_nothing_is_uploaded(store, tmp_path, log_stream):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _swap_work_for_a_link(ws, _outside(tmp_path))
    manager = _manager(store, _logger(log_stream))

    with pytest.raises(CheckpointError, match="link"):
        manager.create(ws)
    assert _checkpoint_keys(store) == [], "a refused checkpoint uploaded something"
    assert manager.seq == 0, "a refused checkpoint is not one this attempt wrote"


def test_the_last_real_checkpoint_stays_the_newest(store, tmp_path, log_stream):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "state.json").write_text('{"completed_steps": 2}')
    manager = _manager(store, _logger(log_stream))
    good = manager.create(ws)

    _swap_work_for_a_link(ws, _outside(tmp_path))
    with pytest.raises(CheckpointError):
        manager.create(ws)

    latest = manager.find_latest()
    assert latest is not None and latest.checkpoint_id == good.checkpoint_id
    data = store.download_bytes(latest.archive_key)
    assert OUTSIDE_SECRET.encode() not in data


def test_the_refusal_does_not_rest_on_a_check_made_before_the_walk(
    store, tmp_path, log_stream, monkeypatch
):
    """`work/` can be swapped between any path-level check and the walk. The
    refusal must come from opening the root without following it, so a check
    that reports "not a link" changes nothing."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _swap_work_for_a_link(ws, _outside(tmp_path))
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: False if self == ws.work else real_is_symlink(self)
    )
    monkeypatch.setattr(os.path, "islink", lambda path: False)

    with pytest.raises(CheckpointError, match="link"):
        _manager(store, _logger(log_stream)).create(ws)
    assert _checkpoint_keys(store) == []


def test_below_the_root_a_link_is_archived_as_a_link_and_never_followed(
    store, tmp_path, log_stream
):
    """The rule that already held, kept through the no-follow walk: a link
    inside the tree is one member, and what it points at is not in the archive.
    The CLI's own session transcripts under `work/.claude` stay (owner decision
    on #244), and `input.json` stays out (owner decision 2026-09-27)."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    outside = _outside(tmp_path)
    (ws.work / "notes").mkdir()
    (ws.work / "notes" / "a.txt").write_text("mine")
    (ws.work / "notes" / "elsewhere").symlink_to(outside, target_is_directory=True)
    (ws.work / "notes" / "to-a").symlink_to("a.txt")
    transcript = ws.work / ".claude" / "projects" / "p" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type": "user"}\n')
    ws.input_path.write_text('{"prompt": "x"}')

    record = _manager(store, _logger(log_stream)).create(ws)

    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert members["notes/elsewhere"].issym()
    assert members["notes/elsewhere"].linkname == str(outside)
    assert members["notes/to-a"].issym() and members["notes/to-a"].linkname == "a.txt"
    assert not any(name.startswith("notes/elsewhere/") for name in members), sorted(members)
    assert OUTSIDE_SECRET.encode() not in data
    assert ".claude/projects/p/session.jsonl" in members, "the CLI's transcript was dropped"
    assert "input.json" not in members
    assert members["notes"].isdir() and members["notes/a.txt"].isfile()
    # Every member that is not a directory, the way the API's listing counts.
    assert record.file_count == sum(1 for m in members.values() if not m.isdir())
    assert record.file_count == 4


# ---------------------------------------------------------------------------
# #227: an entry the worker cannot read, and a root it cannot open
# ---------------------------------------------------------------------------


def _records(log_stream) -> list[dict]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 folder; CI is not root")
def test_an_unreadable_folder_is_left_out_and_named_and_the_rest_is_archived(
    store, tmp_path, log_stream
):
    """A folder (and a file) under `work/` with mode 000 made `os.open` raise
    `PermissionError`, which `_walk` re-raised, failing the whole checkpoint
    and every later one; `rglob` used to pass it by. It is skipped and named,
    path only, in one warning with a count."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "state.json").write_text('{"completed_steps": 3}')
    locked = ws.work / "locked"
    locked.mkdir()
    (locked / "inside.txt").write_text("unreachable")
    sealed = ws.work / "sealed.txt"
    sealed.write_text("unreadable")
    locked.chmod(0o000)
    sealed.chmod(0o000)
    try:
        record = _manager(store, _logger(log_stream)).create(ws)
    finally:
        locked.chmod(0o755)
        sealed.chmod(0o644)

    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "state.json" in names
    assert not any(name.startswith(("locked", "sealed")) for name in names), names
    warnings = [r for r in _records(log_stream) if "unreadable" in r]
    assert len(warnings) == 1, warnings
    assert warnings[0]["unreadable"] == 2
    assert sorted(warnings[0]["named"]) == ["locked", "sealed.txt"]
    assert "Permission denied" not in json.dumps(warnings[0]), "the OS error is not the path"


def test_a_root_that_cannot_be_opened_for_another_reason_is_not_called_a_link(
    store, tmp_path, log_stream, monkeypatch
):
    """The root-open refusal said "it is a link or not a directory" whatever
    the error was. That is said only for ELOOP and ENOTDIR; anything else --
    here EMFILE, out of descriptors -- is named for what it is."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "state.json").write_text("{}")
    real_open = os.open

    def open_without_descriptors(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is None and os.fspath(path) == os.fspath(ws.work):
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_without_descriptors)
    with pytest.raises(CheckpointError) as refused:
        _manager(store, _logger(log_stream)).create(ws)
    message = str(refused.value)
    assert "EMFILE" in message, message
    assert "is a link or not a directory" not in message, message
    assert _checkpoint_keys(store) == []
