"""Tearing the workspace down never recurses and never sets the exit code (#737).

WHAT WENT WRONG. `workspace.destroy()` was `shutil.rmtree(ws.root,
ignore_errors=True)`. On Python 3.11 `rmtree` recurses one frame per folder
level, and `ignore_errors` swallows only `OSError`, so a tree about 1,000
folders deep raised `RecursionError` out of `Worker._cleanup()` -- the
`finally` of `run()` -- AFTER the task had SUCCEEDED and given its lease back.
The container exited 1 and Cloud Run counted a failed execution for a
successful task: 93 executions in the 7 days to 2026-10-06.

WHAT IS PINNED.

* `destroy` removes a tree 2,000 folders deep with the recursion limit
  lowered well below that depth, so no per-level frame can be hiding in it;
* a link inside the tree to a folder outside it is removed as a link: the
  folder it points at, and the file in it, are untouched;
* a `destroy` that raises is logged as one line and changes neither a
  SUCCEEDED attempt's exit code (0) nor a FAILED one's (1), and does not
  stop the steps before it (the account and the lease are still given back).

MUTATION. Put `shutil.rmtree(ws.root, ignore_errors=True)` back in `destroy`:
the depth test raises `RecursionError`. Follow links in `_remove_tree`
(`entry.is_dir()` with no `follow_symlinks=False`): the outside folder's file
is deleted. Drop the guard around `workspace_mod.destroy` in `_cleanup`:
`run()` raises instead of returning 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt

DEPTH = 2000


@pytest.fixture(autouse=True)
def _no_deep_tree_left_behind(tmp_path):
    """Remove what each test built, pass or fail, without recursion.

    pytest keeps the last three runs' `tmp_path`s under TMPDIR, which in a
    SwarmCloud run is the attempt's own `tmp/`: a tree this deep left there
    was what `workspace.destroy` met at the end of the attempt (#737), and
    pytest's own pruning of old runs is a recursive `rmtree` that raises
    `RecursionError` on it."""
    yield
    for entry in list(tmp_path.iterdir()):
        workspace_mod._remove_tree(entry)


def _deep_tree(root: Path, depth: int = DEPTH) -> None:
    """`root/d/d/.../d/bottom.txt`, built relative to each parent's
    descriptor: the full path is longer than PATH_MAX, so no single path the
    test builds is longer than one name, and `os.makedirs` would recurse."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open("bottom.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
        os.write(leaf, b"at the bottom\n")
        os.close(leaf)
    finally:
        os.close(fd)


def test_a_tree_2000_folders_deep_is_destroyed_with_the_recursion_limit_lowered(tmp_path):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _deep_tree(ws.work)
    (ws.artifacts / "report.md").write_text("an artifact\n")

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        workspace_mod.destroy(ws)
    finally:
        sys.setrecursionlimit(limit)

    assert DEPTH > 200 * 5, "the tree must be far deeper than the lowered limit"
    assert not ws.root.exists(), sorted(os.listdir(ws.root))[:5]
    # The parent of the workspace is not the workspace's to remove.
    assert (tmp_path / "ws").is_dir()


def test_a_link_inside_the_tree_to_a_folder_outside_it_is_removed_and_the_outside_is_not(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the workspace's\n")
    (outside / "sub").mkdir()
    (outside / "sub" / "deeper.txt").write_text("nor this\n")
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "nested").mkdir()
    (ws.work / "nested" / "out").symlink_to(outside, target_is_directory=True)
    (ws.work / "file-link").symlink_to(outside / "keep.txt")
    (ws.tmp / "out").symlink_to(outside, target_is_directory=True)

    workspace_mod.destroy(ws)

    assert not ws.root.exists()
    assert (outside / "keep.txt").read_text() == "not the workspace's\n"
    assert (outside / "sub" / "deeper.txt").read_text() == "nor this\n"


def test_a_workspace_root_that_is_a_link_is_unlinked_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the workspace's\n")
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    real = tmp_path / "moved"
    ws.root.rename(real)
    ws.root.symlink_to(outside, target_is_directory=True)

    workspace_mod.destroy(ws)

    assert not os.path.lexists(ws.root)
    assert (outside / "keep.txt").read_text() == "not the workspace's\n"


def test_destroy_of_a_workspace_already_gone_is_a_no_op(tmp_path):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    workspace_mod.destroy(ws)
    workspace_mod.destroy(ws)
    assert not ws.root.exists()


def _destroy_raises(monkeypatch) -> list[int]:
    calls: list[int] = []

    def destroy(ws):
        calls.append(1)
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(workspace_mod, "destroy", destroy)
    return calls


def test_a_destroy_that_raises_does_not_change_a_succeeded_attempts_exit_code(
    db, worker_factory, log_stream, monkeypatch
):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    calls = _destroy_raises(monkeypatch)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK

    assert calls, "destroy was never called, so nothing was tested"
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    lines = [line for line in log_stream.getvalue().splitlines() if "cleanup step failed" in line]
    assert len(lines) == 1, lines
    assert '"step": "destroy_workspace"' in lines[0], lines[0]
    assert '"error": "RecursionError"' in lines[0], lines[0]
    assert "Traceback" not in lines[0], lines[0]


def test_a_destroy_that_raises_keeps_a_failed_attempts_exit_code(
    db, worker_factory, monkeypatch
):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05,
                                 "fail": True, "fail_message": "the agent gave up"})
    calls = _destroy_raises(monkeypatch)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.FAILED

    assert calls, "destroy was never called, so nothing was tested"
    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value


@pytest.mark.parametrize("step", ["_collect_spend", "_record_spend", "_record_cpu",
                                  "_release_account"])
def test_an_earlier_cleanup_step_that_raises_still_lets_the_workspace_be_destroyed(
    db, worker_factory, tmp_path, monkeypatch, step
):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker.ws = ws = workspace_mod.create(tmp_path / "ws", "att_1")

    def boom(*args, **kwargs):
        raise RuntimeError("the step broke")

    monkeypatch.setattr(worker, step, boom)

    worker._cleanup()

    assert not ws.root.exists()
