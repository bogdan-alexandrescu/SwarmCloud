"""A `single-pr` step clones, pushes and opens by its `pr_role` (#295, merge-step.md §3).

| pr_role | clones                    | publishes                                         |
|---------|---------------------------|---------------------------------------------------|
| author  | the workflow's ref        | pushes `swarm/<own id>`, opens THE pull request    |
| reader  | `swarm/<pr_author>`       | nothing at all, whatever it changed               |
| amender | `swarm/<pr_author>`       | fast-forward pushes the AUTHOR's branch, opens nothing |

The branch a reader or amender uses is DERIVED from the author's task id in
the signed dispatch block, never read as a name. Every repository step
records the commit it cloned as `result_summary.git.clone_commit`: the only
record of which head a reader actually saw, and what the merge pins (§4.2).

Real git against a bare repository on disk; only HTTP is faked
(test_strategy_end_to_end.py says why). Assertions are made against the
REMOTE, which is what actually happened.

MUTATIONS: clone `repository_ref` for a reader -- its HEAD is main's. Let a
reader reach the push -- the remote moves. Push the amender to its own
branch -- `swarm/t-fix` appears. Open a pull request for the amender -- the
forge records one. Read `pr_author` without the task-id check -- the bad
name clones. Drop `clone_commit` from the harvest -- the reader's record is
missing it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.errors import WorkerError

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def _titled(monkeypatch):
    monkeypatch.setattr(
        lifecycle.Worker, "_generated_pull_request_title", lambda self: "The author's title"
    )


AUTHOR = {"strategy": "single-pr", "carrier": "checkpoints", "pr_role": "author"}
READER = {"strategy": "single-pr", "carrier": "checkpoints", "pr_role": "reader",
          "pr_author": "t-author"}
AMENDER = {"strategy": "single-pr", "carrier": "checkpoints", "pr_role": "amender",
           "pr_author": "t-author"}


def _edit(name: str):
    def edit(repo: Path) -> None:
        (repo / f"{name}.txt").write_text(f"work from {name}\n")
    return edit


def _author(worker_factory, monkeypatch, origin, forge) -> str:
    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-author",
                            dispatch=AUTHOR, edit=_edit("author"))
    assert out["published"] is True, out
    assert out["pr_role"] == "author"
    assert len(forge.pulls) == 1 and forge.pulls[0]["head"] == "swarm/t-author"
    return refs(origin)["swarm/t-author"]


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout.strip()


def test_the_author_pushes_its_own_branch_and_opens_the_one_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    tip = _author(worker_factory, monkeypatch, origin, forge)
    assert "author.txt" in tree_at(origin, tip)


def test_a_reader_clones_the_authors_branch_records_it_and_pushes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    tip = _author(worker_factory, monkeypatch, origin, forge)
    before = refs(origin)
    seen: dict[str, str] = {}

    def look_then_edit(repo: Path) -> None:
        seen["head"] = _head(repo)
        _edit("review")(repo)

    worker, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-review",
                                 dispatch=READER, edit=look_then_edit)
    assert seen["head"] == tip, "the reader did not clone swarm/t-author"
    assert out["clone_commit"] == tip
    assert out["published"] is False and out["pr_role"] == "reader"
    assert refs(origin) == before, "a reader pushed"
    assert len(forge.pulls) == 1, "a reader opened a pull request"


def test_the_amender_fast_forwards_the_authors_branch_and_opens_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    tip = _author(worker_factory, monkeypatch, origin, forge)
    worker, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-fix",
                                 dispatch=AMENDER, edit=_edit("fix"))
    after = refs(origin)
    assert out["clone_commit"] == tip
    assert out["published"] is True and out["branch"] == "swarm/t-author", out
    assert "swarm/t-fix" not in after, "the amender pushed a branch of its own"
    new_tip = after["swarm/t-author"]
    assert new_tip != tip and out["pushed_head"] == new_tip
    # A fast-forward: the author's commit is an ancestor of the new tip.
    subprocess.run(["git", "merge-base", "--is-ancestor", tip, new_tip], cwd=str(origin), check=True)
    assert {"author.txt", "fix.txt"} <= tree_at(origin, new_tip)
    assert len(forge.pulls) == 1, "the amender opened a pull request"
    assert "fast-forward" in out["publish_reason"]


def test_the_amender_never_forces_over_a_branch_that_moved(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The push is never forced: a branch that moved after the clone refuses it."""
    _author(worker_factory, monkeypatch, origin, forge)

    def edit_then_move_the_branch(repo: Path) -> None:
        _edit("fix")(repo)
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "--quiet", "--branch", "swarm/t-author", str(origin),
                        str(other)], check=True, capture_output=True)
        (other / "elsewhere.txt").write_text("moved\n")
        for argv in (["add", "-A"], ["commit", "--quiet", "-m", "moved"],
                     ["push", "--quiet", "origin", "HEAD:swarm/t-author"]):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            *argv], cwd=str(other), check=True, capture_output=True)

    worker, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-fix",
                                 dispatch=AMENDER, edit=edit_then_move_the_branch)
    moved_before = refs(origin)["swarm/t-author"]
    assert out["published"] is False, out
    assert "fix.txt" not in tree_at(origin, moved_before)


@pytest.mark.parametrize("bad", ["../main", "main; rm -rf", "", None, 7])
def test_a_pr_author_that_is_not_a_task_id_clones_nothing(
    worker_factory, origin, local_urls, bad
):
    worker, config, _ = worker_factory(task_id="t-review", repository_url=f"file://{origin}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-review", "metadata": {"dispatch": {**READER, "pr_author": bad}}}
    worker._task = task
    with pytest.raises(WorkerError, match="pr_author"):
        worker._maybe_clone(task)
    assert not (worker.ws.work / lifecycle.REPO_DIR_NAME / ".git").exists()


def test_an_unknown_pr_role_reads_as_a_reader_and_pushes_nothing(worker_factory):
    worker, _, _ = worker_factory()
    worker._task = {"metadata": {"dispatch": {"strategy": "single-pr", "pr_role": "boss"}}}
    assert worker._pr_role() == "reader"
    worker._task = {"metadata": {"dispatch": {"strategy": "direct-pr", "pr_role": "author"}}}
    assert worker._pr_role() == ""


def test_every_repository_step_records_its_clone_commit(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """§3: not only single-pr steps -- a `collect` step's record carries it too."""
    main = refs(origin)["main"]
    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-direct",
                            dispatch={"strategy": "collect"}, edit=_edit("x"))
    assert out["clone_commit"] == main
