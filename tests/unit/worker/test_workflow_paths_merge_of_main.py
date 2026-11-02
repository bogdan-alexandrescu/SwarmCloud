"""A lane that merges main publishes through its own push unless the merge changed a workflow file (#2452).

MEASURED (task_1297d8f4, task_f2b72856, 2026-10-26): after PR 2446 changed
`application.yml` and `release.yml` on main, two steps that merged main --
one with no change of its own, one resolving conflicts in `merge.py` and a
doc -- were routed to swarm-api's `git_push_branch` as workflow pushes and
ended FAILED with an empty `workflow-lines.patch`. `Worker._workflow_paths`
listed with `git log -m`, which diffs a merge against EACH parent, so main's
own workflow files read as the branch's through the merge.

The listing is `--diff-merges=combined`, swarm-api's too: a merge names a
path only where its result differs from every parent. Real git against a
bare origin, the forge's HTTP and the broker's signed call faked, as in
test_publish_scan_after_merging_main.py and test_publish_workflow_files.py:

  * a lane whose only workflow "change" is main's, merged in, is published by
    the worker's own push and the broker is never called;
  * a merge that resolves the workflow file to something neither parent has
    still goes to the broker, naming it;
  * a commit of the lane's own that renames a workflow file away still does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_publish_workflow_files import FakeBroker, _with_broker, own_pushes  # noqa: F401
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WORKFLOW = ".github/workflows/ci.yml"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=check, capture_output=True, text=True,
    ).stdout.strip()


def _main_changes_a_workflow(origin: Path, tmp_path: Path) -> None:
    """`main` gains a workflow file after the lane's clone: PR 2446's merge."""
    other = tmp_path / "upstream-dev"
    _git(tmp_path, "clone", "--quiet", str(origin), str(other))
    (other / ".github" / "workflows").mkdir(parents=True)
    (other / WORKFLOW).write_text("name: ci\non: [push]\njobs: {}\n")
    _git(other, "add", "-A")
    _git(other, "commit", "--quiet", "-m", "main changes ci")
    _git(other, "push", "--quiet", "origin", "main")


def _run(worker_factory, monkeypatch, origin: Path, tmp_path: Path, broker: FakeBroker, *,
         task_id: str, edit):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    _with_broker(monkeypatch, broker)
    _main_changes_a_workflow(origin, tmp_path)
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME, origin)
    return f"{config.git_branch_prefix}{task_id}", worker._harvest_git(publish=True)


def _lane_work(repo: Path) -> None:
    (repo / "lane.txt").write_text("the lane's own work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the lane's own work")


def _merge_main(repo: Path, origin: Path, *, resolve: str | None = None) -> None:
    _git(repo, "fetch", "--quiet", f"file://{origin}", "main")
    if resolve is None:
        _git(repo, "merge", "--quiet", "--no-ff", "--no-edit", "FETCH_HEAD")
        return
    _git(repo, "merge", "--quiet", "--no-ff", "--no-commit", "FETCH_HEAD", check=False)
    (repo / WORKFLOW).write_text(resolve)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "--no-edit")


def test_a_lane_that_merges_mains_workflow_change_publishes_through_its_own_push(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)

    def edit(repo: Path, origin: Path) -> None:
        _lane_work(repo)
        _merge_main(repo, origin)

    branch, out = _run(worker_factory, monkeypatch, origin, tmp_path, broker,
                       task_id="t-merge-main-wf", edit=edit)

    # MUTATION: list with `-m` -> main's ci.yml is the lane's and the broker is called.
    assert broker.calls == [], [c[1].get("branch") for c in broker.calls]
    assert own_pushes == [branch]
    assert out["published"] is True, out.get("publish_reason")
    assert "workflow_paths" not in out and "workflow_publish" not in out
    assert {"lane.txt", WORKFLOW} <= tree_at(origin, branch)


def test_a_merge_that_rewrites_mains_workflow_file_still_goes_to_the_broker(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)

    def edit(repo: Path, origin: Path) -> None:
        _lane_work(repo)
        _merge_main(repo, origin, resolve="name: ci\non: [push, pull_request]\njobs: {}\n")

    branch, out = _run(worker_factory, monkeypatch, origin, tmp_path, broker,
                       task_id="t-evil-merge-wf", edit=edit)

    # MUTATION: skip merge commits in the listing -> the token pushes a workflow change.
    assert own_pushes == []
    assert [c[0] for c in broker.calls] == ["git_push_branch"]
    assert out["workflow_paths"] == [WORKFLOW] and out["workflow_publish"] == "broker"
    assert refs(origin)[branch] == broker.calls[0][1]["new_sha"]


def test_a_lane_commit_that_renames_a_workflow_away_still_goes_to_the_broker(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)

    def edit(repo: Path, origin: Path) -> None:
        _merge_main(repo, origin)
        _git(repo, "mv", WORKFLOW, "ci.yml")
        _git(repo, "commit", "--quiet", "-m", "move ci out of workflows")

    _, out = _run(worker_factory, monkeypatch, origin, tmp_path, broker,
                  task_id="t-rename-wf", edit=edit)

    # MUTATION: list with renames detected -> the deletion is unseen, the token pushes.
    assert own_pushes == []
    assert out["workflow_paths"] == [WORKFLOW] and out["workflow_publish"] == "broker"
