"""A `publish_to_pr` step and the workflow paths (issue 472, task_3162ecbd, 2026-11-25).

A step that publishes onto an existing pull request's branch is refused at
publish (`publish_to_pr_workflow_files`) when its change touches
`.github/workflows` or `.github/actions`: the tenant token has no `workflow`
scope and swarm-api's `git_push_branch` pushes only a task's own branch. That
refusal stays. What changed: `_workflow_paths` lists a path for EVERY new
commit that touches it, and the replay keeps each agent commit (archived issue
242), so an agent that touched a workflow file and then restored it was still
refused. task_3162ecbd worked 40 minutes on pull request 356, deleted one line
of `.github/workflows/terraform.yml`, and the publish refused the whole branch.
Such a step's commits are now folded into one when any of them touches those
paths, so only a NET workflow change is refused.

Real git against a bare repository on disk; only HTTP is faked, with the
fixtures of test_publish_to_pr.py and test_publish_workflow_files.py.

MUTATIONS: drop the fold -- the restored lanes (b, c) are refused. Fold every
onto-PR step -- (d) records no `agent_commits_kept`. Fold every step that
touches a workflow path -- (e) records no `agent_commits_kept`. Drop the
refusal -- (a) pushes the workflow change.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle

from test_publish_to_pr import (  # noqa: F401 -- fixtures and helpers
    DIRECT,
    PR_BRANCH,
    _git,
    _no_agent_text,
    _titled,
    pull,
)
from test_publish_workflow_files import FakeBroker, _with_broker, own_pushes  # noqa: F401
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WORKFLOW = ".github/workflows/terraform.yml"
WORKFLOW_TEXT = "name: terraform\non: [push]\njobs:\n  plan: {}\n  apply: {}\n"
ACTION = ".github/actions/setup/action.yml"
WORKFLOW_PREFIXES = (".github/workflows/", ".github/actions/")


@pytest.fixture
def pr_with_workflow(pull, origin, tmp_path) -> str:
    """The pull request's branch, holding a workflow file as pull request
    356's did; returns its head."""
    work = tmp_path / "seed-workflow"
    subprocess.run(["git", "clone", "--quiet", "--branch", PR_BRANCH, str(origin), str(work)],
                   check=True, capture_output=True)
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / WORKFLOW).write_text(WORKFLOW_TEXT)
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "the pull request's workflow")
    _git(work, "push", "--quiet", "origin", f"HEAD:refs/heads/{PR_BRANCH}")
    return _git(work, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)


def _changes_the_workflow(repo: Path) -> None:
    """The measured agent: one line of the workflow deleted, committed."""
    (repo / WORKFLOW).write_text(WORKFLOW_TEXT.replace("  apply: {}\n", ""))
    (repo / "fix.txt").write_text("the fix\n")
    _commit(repo, "fix, and a workflow line")


def _changes_then_restores_the_workflow(repo: Path) -> None:
    _changes_the_workflow(repo)
    (repo / WORKFLOW).write_text(WORKFLOW_TEXT)  # the start head's content
    (repo / "other.txt").write_text("a real change beside the restoration\n")
    _commit(repo, "restore the workflow")


def _adds_then_deletes_an_action(repo: Path) -> None:
    (repo / ".github" / "actions" / "setup").mkdir(parents=True)
    (repo / ACTION).write_text("name: setup\nruns:\n  using: composite\n  steps: []\n")
    (repo / "fix.txt").write_text("the fix\n")
    _commit(repo, "fix, and an action")
    (repo / ACTION).unlink()
    (repo / "other.txt").write_text("a real change beside the deletion\n")
    _commit(repo, "drop the action again")


def _two_plain_commits(repo: Path) -> None:
    (repo / "first.txt").write_text("first\n")
    _commit(repo, "tests")
    (repo / "second.txt").write_text("second\n")
    _commit(repo, "fix")


def _two_workflow_commits(repo: Path) -> None:
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / WORKFLOW).write_text(WORKFLOW_TEXT)
    _commit(repo, "add a workflow")
    (repo / "fix.txt").write_text("the fix\n")
    _commit(repo, "fix")


def _pushed_paths(origin: Path, start: str, tip: str) -> list[str]:
    """Every path any commit between `start` and `tip` on the remote lists."""
    text = subprocess.run(
        ["git", "log", "--format=", "--name-only", "--no-renames",
         "--diff-merges=combined", tip, "--not", start],
        cwd=str(origin), check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in text.splitlines() if line.strip()]


# -- a: a net workflow change is refused, as before ---------------------------


def test_a_net_workflow_change_onto_a_pull_request_is_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, pr_with_workflow, own_pushes,
    tmp_path,
):
    _no_agent_text(monkeypatch)
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    worker, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-wf-net",
                                 dispatch=DIRECT, edit=_changes_the_workflow)
    assert out["published"] is False, out
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert out["workflow_refused"]["code"] == "publish_to_pr_workflow_files"
    assert "publish_to_pr_workflow_files" in out["publish_reason"]
    assert out["workflow_paths"] == [WORKFLOW]
    patch = worker.ws.artifacts / "workflow-lines.patch"
    assert patch.is_file() and f"+++ b/{WORKFLOW}" in patch.read_text()
    assert refs(origin)[PR_BRANCH] == pr_with_workflow, "the pull request's branch moved"
    assert own_pushes == [] and broker.calls == [] and forge.pulls == []


# -- b, c: a change the agent restored publishes (RED before the fold) ---------


@pytest.mark.parametrize("edit", [_changes_then_restores_the_workflow,
                                  _adds_then_deletes_an_action],
                         ids=["workflow-restored", "action-added-then-deleted"])
def test_a_workflow_touch_the_agent_restored_publishes_folded(
    worker_factory, monkeypatch, origin, local_urls, forge, pr_with_workflow, own_pushes,
    tmp_path, edit,
):
    _no_agent_text(monkeypatch)
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    start = pr_with_workflow
    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-wf-undone",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    assert out.get("workflow_touch_folded") is True, out
    assert "agent_commits_kept" not in out
    tip = refs(origin)[PR_BRANCH]
    assert tip != start
    _git(origin, "merge-base", "--is-ancestor", start, tip)  # a fast-forward
    pushed = _pushed_paths(origin, start, tip)
    assert [p for p in pushed if p.startswith(WORKFLOW_PREFIXES)] == [], pushed
    assert {"fix.txt", "other.txt"} <= set(pushed)
    assert {WORKFLOW, "fix.txt", "other.txt"} <= tree_at(origin, tip)
    assert ACTION not in tree_at(origin, tip)
    assert _git(origin, "show", f"{tip}:{WORKFLOW}") + "\n" == WORKFLOW_TEXT
    # The worker's own push, leased on the pull request's head; never the broker.
    assert own_pushes == [PR_BRANCH] and broker.calls == [] and forge.pulls == []


# -- d, e: the controls --------------------------------------------------------


def test_an_onto_pr_lane_touching_no_workflow_path_keeps_each_commit(
    worker_factory, monkeypatch, origin, local_urls, forge, pr_with_workflow
):
    _no_agent_text(monkeypatch)
    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-plain-kept",
                            dispatch=DIRECT, edit=_two_plain_commits)
    assert out["published"] is True, out
    assert out["agent_commits_kept"] == 2
    assert "workflow_touch_folded" not in out


def test_a_lane_without_publish_to_pr_is_still_replayed_and_brokered(
    worker_factory, monkeypatch, origin, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    _with_broker(monkeypatch, broker)
    dispatch = {"strategy": "direct-pr", "carrier": "checkpoints"}
    _, config, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-wf-own",
                                 dispatch=dispatch, edit=_two_workflow_commits)
    assert out["published"] is True, out
    assert out["agent_commits_kept"] == 2
    assert "workflow_touch_folded" not in out
    assert out["workflow_publish"] == "broker"
    assert [c[0] for c in broker.calls] == ["git_push_branch"] and own_pushes == []
    assert WORKFLOW in tree_at(origin, f"{config.git_branch_prefix}{config.task_id}")
