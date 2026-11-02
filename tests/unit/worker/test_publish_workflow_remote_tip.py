"""Commits origin's publish branch already holds are not the round's to push (#2506).

MEASURED (ci-fix task_1297d8f4, PR #2446): the round cloned the pull
request's branch at fba856; the owner then pushed 89507cf to it, a commit
that touched `.github/workflows/`, and the agent's clone ended on exactly
that commit with nothing of its own. The publish read the workflow range as
`HEAD --not <clone base> <merged default-branch commit>`, never where origin's
branch stood, so it counted the owner's workflow file as this round's, sent a
no-op push through swarm-api's `git_push_branch`, whose one call answered
api_unavailable, and the step ended publish_failed with nothing to push.

The property, against a real bare repository on disk (only the forge's HTTP
and the broker's signed call are faked, as in test_publish_workflow_files.py):

  * a HEAD that IS origin's tip of the branch pushes nothing, calls no
    broker, and ends as the `no_change` result naming the tip;
  * a workflow commit already on origin does not route the round's new,
    ordinary commit through the broker: the worker's own push carries it,
    as a fast-forward over origin's tip;
  * a round that adds its own workflow commit on top of origin's tip still
    goes to the broker, which is told the tip is a commit origin has.

WRITTEN TO FAIL BEFORE THE CHANGE: there, the first two called the broker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import expected_outputs as expected_mod
from agent_worker import lifecycle

from test_publish_workflow_files import FakeBroker, _with_broker
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures are used by name
    _the_fence_holds,
    _attempt,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ROOT = "task_0123456789abcdef2506"
FIX = "task_fedcba9876543210f506"
CONTINUES = {"strategy": "direct-pr", "continues": ROOT}
WORKFLOW = ".github/workflows/ci.yml"
OWNER = ("the owner", "owner@example.invalid")


def _sh(cwd: Path, *args: str, who: tuple[str, str] = OWNER) -> str:
    name, email = who
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _writer(origin: Path, tmp: Path) -> Path:
    clone = tmp / f"writer-{len(list(tmp.glob('writer-*')))}"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True,
                   capture_output=True)
    return clone


@pytest.fixture
def red(origin: Path, tmp_path: Path) -> str:
    """`swarm/<ROOT>` on the remote: the pull request's branch the round continues."""
    branch = f"swarm/{ROOT}"
    clone = _writer(origin, tmp_path)
    _sh(clone, "checkout", "--quiet", "-b", branch)
    (clone / "feature.py").write_text("def broken():\n    return 1\n")
    _sh(clone, "add", "feature.py")
    _sh(clone, "commit", "--quiet", "-m", "the work that went red")
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return branch


def _owner_pushes_a_workflow_commit(origin: Path, tmp: Path, branch: str) -> str:
    """The owner's commit on the pull request's branch after the round cloned
    it, touching a workflow file: 89507cf in the measured case."""
    clone = _writer(origin, tmp)
    _sh(clone, "checkout", "--quiet", "-b", "pr", f"origin/{branch}")
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / WORKFLOW).write_text("name: ci\non: [push]\njobs: {}\n")
    _sh(clone, "add", WORKFLOW)
    _sh(clone, "commit", "--quiet", "-m", "ci: the owner's workflow fix")
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return refs(origin)[branch]


def _agent_moves_onto(repo: Path, origin: Path, branch: str) -> None:
    """The agent pulls the pull request's branch into its clone."""
    _sh(repo, "fetch", "--quiet", str(origin), f"refs/heads/{branch}")
    _sh(repo, "reset", "--quiet", "--hard", "FETCH_HEAD")


def _fixer(worker_factory, monkeypatch, origin: Path, broker: FakeBroker):
    worker, config, task = _attempt(
        worker_factory, monkeypatch, origin, task_id=FIX, attempt=1, dispatch=CONTINUES
    )
    cloned = worker._maybe_clone(task)
    assert cloned["commit"], "the clone did not land"
    _with_broker(monkeypatch, broker)
    return worker, config, worker.ws.work / lifecycle.REPO_DIR_NAME


@pytest.fixture
def own_pushes(monkeypatch) -> list[dict[str, Any]]:
    """Every push the worker made with the tenant token (`gitops.push_branch`)."""
    calls: list[dict[str, Any]] = []
    real = lifecycle.push_branch

    def spy(**kwargs: Any) -> str:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", spy)
    return calls


def _is_ancestor(bare: Path, old: str, new: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", old, new], cwd=str(bare),
                          capture_output=True).returncode == 0


def test_head_at_the_remote_tip_with_a_workflow_commit_ends_no_change_without_the_broker(
    worker_factory, monkeypatch, origin, red, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin, broker)
    tip = _owner_pushes_a_workflow_commit(origin, tmp_path, red)
    _agent_moves_onto(repo, origin, red)
    before = refs(origin)

    out = worker._harvest_git(publish=True)

    # MUTATION: drop the remote-tip check in `_publish_git` -> the broker is
    # called with the owner's commit, the measured api_unavailable call.
    assert broker.calls == [], "a HEAD origin already has went to the broker"
    # MUTATION: push anyway with the token -> a push is recorded.
    assert own_pushes == []
    assert refs(origin) == before, "something was pushed"
    assert out["published"] is False
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out.get("publish_reason")
    assert "workflow_paths" not in out
    reason = out[lifecycle.BASE_MERGE_NO_CHANGE_FIELD]
    # MUTATION: name the clone base instead -> the tip is not in the reason.
    assert reason.startswith("no_change:") and tip[:12] in reason and red in reason
    assert out["publish_reason"] == reason
    assert forge.pulls == []
    # The finish reads it as the attempt's no_change result, not a failure.
    summary: dict[str, Any] = {"git": out}
    assert worker._no_change_after_publish(summary) is True
    assert summary[expected_mod.NO_CHANGE_SUMMARY_KEY] is True
    assert tip[:12] in summary[lifecycle.NO_CHANGE_REASON_SUMMARY_KEY]
    assert worker._publish_failed(summary) is None


def test_a_workflow_commit_already_on_origin_does_not_route_new_plain_work_through_the_broker(
    worker_factory, monkeypatch, origin, red, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    worker, config, repo = _fixer(worker_factory, monkeypatch, origin, broker)
    tip = _owner_pushes_a_workflow_commit(origin, tmp_path, red)
    _agent_moves_onto(repo, origin, red)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")

    out = worker._harvest_git(publish=True)

    # MUTATION: leave origin's tip out of `_workflow_negatives` -> the owner's
    # workflow commit is counted as the round's and the broker is called.
    assert broker.calls == []
    assert "workflow_paths" not in out
    assert [call["branch"] for call in own_pushes] == [red]
    assert out["published"] is True, out.get("publish_reason")
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD)
    landed = refs(origin)[red]
    # MUTATION: replay from the clone base -> the owner's commit is rewritten
    # and the push is not a fast-forward of origin's tip.
    assert _is_ancestor(origin, tip, landed), "origin's tip was replaced"
    assert _sh(origin, "rev-parse", f"{landed}^") == tip
    assert {WORKFLOW, "feature.py"} <= tree_at(origin, red)
    assert "return 2" in _sh(origin, "show", f"{red}:feature.py")


def test_a_new_workflow_commit_on_the_remote_tip_still_goes_to_the_broker_with_the_tip_as_a_have(
    worker_factory, monkeypatch, origin, red, local_urls, forge, own_pushes, tmp_path
):
    broker = FakeBroker(origin, tmp_path)
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin, broker)
    tip = _owner_pushes_a_workflow_commit(origin, tmp_path, red)
    _agent_moves_onto(repo, origin, red)
    (repo / ".github" / "workflows" / "lint.yml").write_text("name: lint\non: [push]\njobs: {}\n")

    out = worker._harvest_git(publish=True)

    # MUTATION: end every publish on a branch with a remote tip as no_change
    # -> the round's own workflow file is never pushed.
    assert [c[0] for c in broker.calls] == ["git_push_branch"]
    assert own_pushes == []
    fields = broker.calls[0][1]
    # MUTATION: send only the clone base as haves -> the tip is missing.
    assert tip in fields["haves"]
    assert fields["commits"] == 1
    # Only the round's own workflow file; the owner's is already on origin.
    assert out["workflow_paths"] == [".github/workflows/lint.yml"]
    assert out["workflow_publish"] == "broker" and out["published"] is True
    landed = refs(origin)[red]
    assert landed == fields["new_sha"] and _is_ancestor(origin, tip, landed)
