"""Nothing in an additional checkout is ever carried back (MR-WORKER, #1434 check 2).

docs/design/multi-repository-steps.md §4.5: the primary repository stays the
only one whose changes leave the attempt. `Worker._harvest_git` reads
`work/repo` only, and `work/repos/` is beside it, never under it, so
`collect`'s `swarm-work.patch`, `direct-pr`'s push and `integrate`'s merge
carry nothing from an additional checkout. And a task with no primary
repository, whose working folder is uploaded (`standalone_outputs`), never
walks the links in `work/repos/`.

Pinned here:

* **#1434 check 2**: an edit made inside `work/repos/web` leaves
  `swarm-work.patch` byte-identical to the patch of the same attempt without
  that edit;
* a no-repository task's working-folder upload skips `repos/`, while a task
  with no additional repositories still uploads an agent's own `repos/`.

MUTATIONS: clone the additional checkouts under `work/repo/` -- the patch
differs. Drop `repos` from `_workdir_reserved` -- the standalone scan lists
the links.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_worker import lifecycle, standalone_outputs
from agent_worker.extrarepos import release_tree

from test_additional_repositories_clone import (
    entry,
    make_bare,
    make_worker,
    redirect_clones,
)
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _patch_after(worker_factory, monkeypatch, tmp_path: Path, origin: Path, web: Path,
                 commit: str, *, task_id: str, edit_extra: bool) -> bytes:
    redirect_clones(monkeypatch, {"web": web})
    task = {
        "task_id": task_id,
        "metadata": {"dispatch": {"strategy": "collect"}},
        "additional_repositories": [entry("web", commit)],
    }
    worker, _ = make_worker(
        worker_factory, tmp_path, task=task, task_id=task_id,
        attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    monkeypatch.setattr(worker, "_git_token", lambda: None)
    assert worker._maybe_clone(task) is not None
    worker._clone_additional_keeping_lease(task)

    # The "agent": the same change to the primary in both attempts.
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "README.md").write_text("changed by the agent\n")
    if edit_extra:
        # It can undo the read-only signal (same uid) and edit, commit and all.
        extra = worker.ws.work / "repos" / "web"
        release_tree(extra.resolve())
        for path in extra.resolve().rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                path.chmod(0o644)
        (extra / "README.md").write_text("an edit in the additional checkout\n")
        (extra / "new.py").write_text("LEAK = 1\n")

    summary = worker._harvest_git(publish=False)
    assert summary is not None and "error" not in summary, summary
    return (worker.ws.artifacts / lifecycle.PATCH_NAME).read_bytes()


def test_an_edit_in_an_additional_checkout_leaves_the_patch_byte_identical(
    worker_factory, monkeypatch, tmp_path, origin, local_urls
):
    web, web_shas = make_bare(tmp_path, "web")

    control = _patch_after(worker_factory, monkeypatch, tmp_path, origin, web, web_shas[-1],
                           task_id="task_a", edit_extra=False)
    edited = _patch_after(worker_factory, monkeypatch, tmp_path, origin, web, web_shas[-1],
                          task_id="task_b", edit_extra=True)

    assert b"changed by the agent" in control, "the control patch carries no work"
    assert edited == control
    assert b"LEAK" not in edited and b"additional checkout" not in edited


def test_a_no_repository_tasks_upload_skips_the_links(
    worker_factory, monkeypatch, tmp_path, local_urls
):
    web, web_shas = make_bare(tmp_path, "web")
    redirect_clones(monkeypatch, {"web": web})
    task = {"task_id": "task_1", "additional_repositories": [entry("web", web_shas[-1])]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task, runner_profile="claude-code")
    assert worker._uploads_working_folder(task) is True
    worker._clone_additional_keeping_lease(task)
    (worker.ws.work / "answer.md").write_text("the answer\n")

    reserved = worker._workdir_reserved()
    scan = standalone_outputs.scan(worker.ws.work, reserved=reserved)

    assert "repos" in reserved
    assert "answer.md" in scan.files
    assert not [p for p in scan.files if p == "repos" or p.startswith("repos/")], scan.files
    assert not [p for p in scan.symlinks if p.startswith("repos/")], scan.symlinks


def test_a_task_without_additional_repositories_still_uploads_its_own_repos_folder(
    worker_factory, tmp_path
):
    task = {"task_id": "task_1"}
    worker, _ = make_worker(worker_factory, tmp_path, task=task, runner_profile="claude-code")
    worker._clone_additional_keeping_lease(task)
    (worker.ws.work / "repos").mkdir()
    (worker.ws.work / "repos" / "notes.md").write_text("the agent's own folder\n")

    scan = standalone_outputs.scan(worker.ws.work, reserved=worker._workdir_reserved())

    assert "repos/notes.md" in scan.files
