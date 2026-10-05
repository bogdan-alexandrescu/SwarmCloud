"""Checkpoints upload what changed, skip what a reinstall rebuilds, and back off (#637).

The 2026-10-05 history analysis: 247 GB of checkpoints uploaded, a median of
66 MB every 120 s, node_modules, virtualenvs and build caches included -- and
1.25% of those bytes were ever restored. Three changes, one property each:

1. the dependency and build directories (`.venv`, `__pycache__`, `dist`,
   `.test-build`, `coverage`, anywhere in `work/`) are left out, as the tool
   caches and `node_modules` already were -- unless a git checkout TRACKS a
   file under one, because a restore that dropped a tracked file would hand
   the agent a checkout with that file deleted;
2. after the attempt's first checkpoint, each archive holds only what changed
   since the previous one, names its base by digest in the archive itself, and
   a restore replays the chain into exactly the tree a full archive restores;
3. a PERIODIC checkpoint of an unchanged tree is not written, and the interval
   to the next one doubles to a cap; a change resets it. Every other
   checkpoint -- final, park, cancellation, SIGTERM -- is written whatever
   changed (invariant 8).

MUTATIONS: drop a name from `BUILD_DIRS` (test 1); exclude a tracked `dist`
(the tracked test); upload the whole tree every time (the size assertion in
test 2); stop removing deleted paths on restore (test 2's tree comparison);
take the base from the manifest rather than the digest-bound archive header
(the planted-base test); let `create` skip an unchanged tree (the final test);
stop doubling, or stop resetting, in `CheckpointBackoff` (test 3).
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import (
    BUILD_DIRS,
    CheckpointBackoff,
    CheckpointManager,
    checkpoint_prefix,
)
from agent_worker.errors import CheckpointError, ExitCode
from agent_worker.logs import build_logger

from conftest import TENANT, seed_attempt


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


def _settled(monkeypatch) -> None:
    """Shrink the racy window, and wait it out, so files written above count as settled."""
    from agent_worker import checkpoint as checkpoint_mod

    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 50_000_000)
    time.sleep(0.1)


# ---------------------------------------------------------------------------
# 1. rebuildable directories are left out
# ---------------------------------------------------------------------------

def test_dependency_and_build_directories_are_not_archived(store, tmp_path, log_stream):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    repo = ws.work / "repo"
    _write(repo / "src" / "app.py", "print('kept')\n")
    _write(repo / "notes" / "coverage.md", "a file NAMED like a cache is kept\n")
    _write(repo / "docs" / "dist", "a FILE named dist is kept\n")
    for name in BUILD_DIRS:
        _write(repo / name / "inner" / "blob.bin", "rebuildable\n")
        _write(repo / "apps" / "ui" / name / "blob.bin", "rebuildable\n")
    _write(ws.work / ".venv" / "bin" / "activate", "rebuildable\n")
    _write(repo / "pkg" / "__pycache__" / "mod.cpython-311.pyc", "rebuildable\n")
    # The caches uv and npm fill under HOME, already out since #286.
    _write(ws.work / ".cache" / "uv" / "wheel.whl", "rebuildable\n")
    _write(ws.work / ".npm" / "_cacache" / "index", "rebuildable\n")
    _write(repo / "node_modules" / "left-pad" / "index.js", "rebuildable\n")

    record = _manager(store, _logger(log_stream)).create(ws)
    names = _members(store, record)

    assert "repo/src/app.py" in names
    assert "repo/notes/coverage.md" in names
    assert "repo/docs/dist" in names
    for name in BUILD_DIRS:
        assert not any(n == f"repo/{name}" or n.startswith(f"repo/{name}/") for n in names), name
        assert not any(f"/{name}/" in f"/{n}" for n in names), name
    assert not any(n.startswith((".cache", ".npm", ".venv")) for n in names)
    assert not any("node_modules" in n for n in names)


def test_a_build_directory_git_tracks_is_kept(store, tmp_path, log_stream):
    """A restore must produce a working tree: a TRACKED `dist/` left out would
    come back as a checkout with every file under it deleted."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    repo = ws.work / "repo"
    _write(repo / "dist" / "bundle.js", "committed output\n")
    _write(repo / "coverage" / "report.txt", "never committed\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "dist/bundle.js"], check=True, env=env)

    record = _manager(store, _logger(log_stream)).create(ws)
    names = _members(store, record)

    assert "repo/dist/bundle.js" in names
    assert not any(n.startswith("repo/coverage") for n in names)


# ---------------------------------------------------------------------------
# 2. incremental after the first, and a restore replays the chain
# ---------------------------------------------------------------------------

def test_incremental_restore_equals_a_full_restore(store, tmp_path, log_stream, monkeypatch):
    logger = _logger(log_stream)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    work = ws.work
    _write(work / "repo" / "big.bin", "B" * 200_000)
    _write(work / "repo" / "edit.txt", "v1\n")
    _write(work / "repo" / "gone.txt", "deleted later\n")
    _write(work / "repo" / "olddir" / "a.txt", "dir deleted later\n")
    _write(work / "repo" / "becomes_dir", "a file that becomes a directory\n")
    _write(work / "repo" / "becomes_link" / "x.txt", "a directory that becomes a link\n")
    _write(work / "repo" / "script.sh", "#!/bin/sh\n")
    _settled(monkeypatch)
    manager = _manager(store, logger)
    first = manager.create(ws)
    assert first.base_checkpoint_id is None

    # Changes of every shape between checkpoints.
    _write(work / "repo" / "edit.txt", "v2, longer\n")
    (work / "repo" / "gone.txt").unlink()
    (work / "repo" / "olddir" / "a.txt").unlink()
    (work / "repo" / "olddir").rmdir()
    (work / "repo" / "becomes_dir").unlink()
    _write(work / "repo" / "becomes_dir" / "inside.txt", "now a directory\n")
    (work / "repo" / "becomes_link" / "x.txt").unlink()
    (work / "repo" / "becomes_link").rmdir()
    os.symlink("edit.txt", work / "repo" / "becomes_link")
    os.chmod(work / "repo" / "script.sh", 0o755)
    _write(work / "repo" / "new" / "file.txt", "new\n")
    time.sleep(0.1)  # settled, so the third archive holds only the third's changes
    second = manager.create(ws)

    _write(work / "repo" / "edit.txt", "v3\n")
    _write(work / "repo" / "third.txt", "third\n")
    third = manager.create(ws)

    # Only what changed travels: the 200 kB file is in the first archive only.
    assert "repo/big.bin" in _members(store, first)
    assert "repo/big.bin" not in _members(store, second)
    assert sorted(_members(store, third)) == ["repo/edit.txt", "repo/third.txt"]
    assert third.archive_bytes < first.archive_bytes
    assert second.base_checkpoint_id == first.checkpoint_id
    assert third.base_checkpoint_id == second.checkpoint_id
    assert third.base_archive_sha256 == second.archive_sha256

    # A full archive of the same tree, from a manager with no chain.
    full = _manager(store, logger, attempt_id="att_full").create(ws)
    assert full.base_checkpoint_id is None

    replayed = workspace_mod.create(tmp_path / "ws_chain", "att_2")
    _manager(store, logger, attempt_id="att_2").restore(third, replayed)
    reference = workspace_mod.create(tmp_path / "ws_full", "att_3")
    _manager(store, logger, attempt_id="att_3").restore(full, reference)

    assert _tree(replayed.work) == _tree(reference.work)
    assert _tree(replayed.work) == _tree(work)
    assert not any(replayed.restore.iterdir()), "every downloaded layer is removed"


def test_a_restore_refuses_a_base_whose_bytes_were_rewritten(store, tmp_path, log_stream):
    """The base is named by digest INSIDE the incremental archive, whose own
    digest the attempt document binds: rewriting the base in the bucket, or
    the manifest that describes it, does not get a planted tree restored."""
    logger = _logger(log_stream)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "a.txt", "one\n")
    manager = _manager(store, logger)
    first = manager.create(ws)
    _write(ws.work / "b.txt", "two\n")
    second = manager.create(ws)

    planted = workspace_mod.create(tmp_path / "planted", "att_x")
    _write(planted.work / "a.txt", "planted\n")
    _write(planted.work / ".claude" / "settings.json", "{}\n")
    forged = _manager(store, logger, attempt_id="att_x").create(planted)
    store.upload_bytes(first.archive_key, store.download_bytes(forged.archive_key))
    # And the manifest of the head claims the forged base too.
    manifest = json.loads(store.download_bytes(second.manifest_key))
    manifest["base_archive_sha256"] = forged.archive_sha256
    store.upload_bytes(second.manifest_key, json.dumps(manifest).encode())

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    with pytest.raises(CheckpointError, match="integrity"):
        _manager(store, logger, attempt_id="att_2").restore(second, resumed)
    assert not (resumed.work / ".claude").exists()


def test_the_chain_is_rebased_on_a_full_archive(store, tmp_path, log_stream, monkeypatch):
    from agent_worker import checkpoint as checkpoint_mod

    monkeypatch.setattr(checkpoint_mod, "CHECKPOINT_CHAIN_MAX", 2)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    # Incompressible, so the full archive outweighs the small ones on it and
    # only the length rule rebases.
    (ws.work / "weight.bin").write_bytes(os.urandom(256 * 1024))
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    bases = []
    for index in range(5):
        _write(ws.work / f"f{index}.txt", f"{index}\n")
        bases.append(manager.create(ws).base_checkpoint_id)
    assert bases == [None, "ckpt-00001", "ckpt-00002", None, "ckpt-00004"]


# ---------------------------------------------------------------------------
# 3. an unchanged tree backs off; the final checkpoint is always written
# ---------------------------------------------------------------------------

def test_an_unchanged_tree_is_not_rewritten_and_a_change_is(
    store, tmp_path, log_stream, monkeypatch
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "a.txt", "one\n")
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    announced: list[str] = []

    first = manager.create_if_changed(ws, before_upload=lambda: announced.append("1"))
    assert first is not None and manager.last_unchanged is False
    keys = sorted(store.list_keys("tenants/"))

    assert manager.create_if_changed(ws, before_upload=lambda: announced.append("2")) is None
    assert manager.last_unchanged is True
    assert sorted(store.list_keys("tenants/")) == keys, "nothing is uploaded"
    assert announced == ["1"], "an unchanged tree announces no checkpoint"
    assert "checkpoint unchanged" in log_stream.getvalue()

    _write(ws.work / "a.txt", "two\n")
    changed = manager.create_if_changed(ws)
    assert changed is not None and manager.last_unchanged is False
    assert changed.checkpoint_id == "ckpt-00002", "a skipped tick takes no id"


def test_a_rewrite_in_the_same_clock_tick_is_not_missed(store, tmp_path, log_stream):
    """Same size, and quite possibly the same mtime, ctime and inode: only the
    racy rule catches it, by archiving a just-written file again next time."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    target = ws.work / "a.txt"
    target.write_text("one\n")
    manager = _manager(store, _logger(log_stream))
    manager.create(ws)
    with target.open("r+") as handle:  # in place: the inode stays
        handle.write("two\n")
    record = manager.create_if_changed(ws)
    assert record is not None
    assert _members(store, record) == ["a.txt"]
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    _manager(store, _logger(log_stream), attempt_id="att_2").restore(record, resumed)
    assert (resumed.work / "a.txt").read_text() == "two\n"


def test_the_interval_backs_off_while_unchanged_and_resets_on_change():
    backoff = CheckpointBackoff(base_seconds=120, cap_seconds=600)
    assert backoff.current == 120
    unchanged = [backoff.next_interval(changed=False) for _ in range(5)]
    assert unchanged == [240, 480, 600, 600, 600]
    assert backoff.next_interval(changed=True) == 120
    assert backoff.next_interval(changed=False) == 240
    # A cap below the base never shortens the mandatory interval.
    assert CheckpointBackoff(base_seconds=900, cap_seconds=600).next_interval(changed=False) == 900


@pytest.mark.parametrize("label", ["final", "quota-park", "cancellation", "interrupted"])
def test_every_checkpoint_but_the_periodic_one_is_written_unchanged(
    store, tmp_path, log_stream, label, monkeypatch
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "a.txt", "one\n")
    _settled(monkeypatch)
    manager = _manager(store, _logger(log_stream))
    manager.create(ws)
    assert manager.create_if_changed(ws) is None

    record = manager.create(ws, label=label)
    assert store.exists(record.manifest_key)
    assert json.loads(store.download_bytes(record.manifest_key))["label"] == label
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    _manager(store, _logger(log_stream), attempt_id="att_2").restore(record, resumed)
    assert (resumed.work / "a.txt").read_text() == "one\n"


def test_a_worker_skips_unchanged_periodic_checkpoints_and_still_writes_the_final(
    db, store, tmp_path, log_stream, worker_factory, monkeypatch
):
    from agent_worker import checkpoint as checkpoint_mod

    # The racy window is a second; this test's interval is too.
    monkeypatch.setattr(checkpoint_mod, "RACY_WINDOW_NS", 50_000_000)
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 3.5})
    worker, _, _ = worker_factory(checkpoint_interval_seconds=1)
    assert worker.run() == ExitCode.OK

    prefix = checkpoint_prefix(
        tenant_id=TENANT, task_id="task_1", attempt_id="att_1", checkpoint_id=""
    )
    labels = [
        json.loads(store.download_bytes(key))["label"]
        for key in sorted(store.list_keys(prefix))
        if key.endswith("/manifest.json")
    ]
    assert labels[-1] == "final"
    assert "checkpoint unchanged" in log_stream.getvalue()
    assert labels.count("periodic") < 3, labels
