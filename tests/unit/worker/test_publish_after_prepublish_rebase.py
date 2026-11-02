"""The worker publishes a pre-publish-rebased lane from where it landed (SPEED-D1).

The runner rebases a lane onto the base's CURRENT tip before the worker
publishes it (`agent_worker.prepublish`, owner decision 2026-10-14). The
publish replays every commit in `<base>..HEAD` as the worker's, so with the
CLONE base as `<base>` the commits main gained since the clone would be
replayed and pushed as the lane's -- and main's lines scanned as the lane's
additions. `Worker._adopt_rebase_base` moves the publish base to the sha the
git tools' relay rebased onto (`GitTools.rebased_onto`) when, in the worker's
own clean repository, HEAD still descends from it.

Also here: which steps the worker offers the rebase to
(`Worker._prepublish_rebase_ref`), and the runner's "Pre-publish checks
failed" section reaching the pull request body.

Real repositories, a real bare origin, the fake forge of
test_strategy_end_to_end. The credential-shaped fixture main gains is built at
runtime.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_worker import lifecycle, prepublish, workspace as workspace_mod

from test_publish_scan_after_merging_main import _advance_main
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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _cloned(worker_factory, monkeypatch, origin: Path, *, task_id: str, dispatch: dict):
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": dispatch}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    return worker, config


def _rebase_lane(worker, origin: Path, *, record: bool = True) -> str:
    """What the runner's gate does through the relay: the lane commits, then
    is rebased onto origin's newer main, and the relay records where."""
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    (repo / "lane.txt").write_text("the lane's own work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the lane's own work")
    _git(repo, "fetch", "--quiet", f"file://{origin}", "main")
    onto = _git(repo, "rev-parse", "FETCH_HEAD")
    _git(repo, "rebase", "--quiet", onto)
    if record:
        worker.children._git = SimpleNamespace(rebased_onto=onto)
    return onto


def test_a_rebased_lane_is_published_from_where_it_landed(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, config = _cloned(worker_factory, monkeypatch, origin, task_id="t-rebased",
                             dispatch={"strategy": "direct-pr"})
    clone_base = worker._publish_base
    main_tip = _advance_main(origin, tmp_path)
    onto = _rebase_lane(worker, origin)
    assert onto == main_tip

    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out.get("publish_reason")
    # main's commit is not the lane's: not replayed, not scanned as an addition.
    assert "final_tree_leak" not in out, out
    assert out["rebased_from"] == clone_base and out["rebased_onto"] == onto
    assert out.get("agent_commits_kept") == 1, out
    branch = f"{config.git_branch_prefix}t-rebased"
    _git(origin, "merge-base", "--is-ancestor", main_tip, branch)
    assert _git(origin, "rev-parse", f"{branch}~1") == main_tip
    assert worker.checkpoints.clone_base == onto


def test_without_the_relays_record_the_clone_base_stands(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The control: a rebase the worker did not make itself moves nothing."""
    worker, _ = _cloned(worker_factory, monkeypatch, origin, task_id="t-unrecorded",
                        dispatch={"strategy": "direct-pr"})
    _advance_main(origin, tmp_path)
    _rebase_lane(worker, origin, record=False)
    out = worker._harvest_git(publish=True)
    assert "rebased_onto" not in out
    # main's fixture is read as the lane's addition, exactly the failure the
    # adoption exists to prevent.
    assert out["published"] is False and "final_tree_leak" in out, out


def test_a_rebase_the_agent_undid_keeps_the_clone_base(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, _ = _cloned(worker_factory, monkeypatch, origin, task_id="t-undone",
                        dispatch={"strategy": "direct-pr"})
    clone_base = worker._publish_base
    _advance_main(origin, tmp_path)
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    _rebase_lane(worker, origin)
    _git(repo, "reset", "--quiet", "--hard", clone_base)
    (repo / "again.txt").write_text("redone on the old base\n")
    out = worker._harvest_git(publish=True)
    assert "rebased_onto" not in out
    assert worker._publish_base == clone_base
    assert out["published"] is True, out.get("publish_reason")


@pytest.mark.parametrize("dispatch, offered", [
    ({"strategy": "direct-pr"}, True),
    ({"strategy": "single-pr", "pr_role": "author"}, True),
    ({"strategy": "collect"}, False),
    ({"strategy": "integrate", "role": "contributor"}, False),
    ({"strategy": "direct-pr", "carrier": "branches"}, False),
])
def test_only_an_author_cloned_from_the_branch_is_offered_the_rebase(
    worker_factory, monkeypatch, origin, local_urls, dispatch, offered
):
    worker, _ = _cloned(worker_factory, monkeypatch, origin,
                        task_id=f"t-offer-{dispatch['strategy']}", dispatch=dispatch)
    assert worker._prepublish_rebase == ("main" if offered else None)


def test_the_pre_publish_section_reaches_the_pull_request_body(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, _ = _cloned(worker_factory, monkeypatch, origin, task_id="t-red",
                        dispatch={"strategy": "direct-pr"})
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    (repo / "lane.txt").write_text("work\n")
    (worker.ws.artifacts / prepublish.PR_SECTION_NAME).write_text(
        f"{prepublish.PR_SECTION_HEADING}\n\n### shellcheck failed (1.0 s)\nSC2086 here\n")
    out = worker._harvest_git(publish=True)
    assert out["published"] is True, out.get("publish_reason")
    assert out.get("prepublish_checks_failed") is True
    (pull,) = forge.pulls
    assert prepublish.PR_SECTION_HEADING in pull["body"] and "SC2086 here" in pull["body"]


def test_a_file_that_is_not_the_runners_section_is_not_appended(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, _ = _cloned(worker_factory, monkeypatch, origin, task_id="t-forged",
                        dispatch={"strategy": "direct-pr"})
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "lane.txt").write_text("work\n")
    (worker.ws.artifacts / prepublish.PR_SECTION_NAME).write_text("anything else\n")
    worker._harvest_git(publish=True)
    (pull,) = forge.pulls
    assert "anything else" not in pull["body"]
