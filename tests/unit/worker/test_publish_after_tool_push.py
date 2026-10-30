"""A branch the agent pushed through `git_push_branch` is already published (#1188).

MEASURED 2026-10-23 (acceptance run 39926360548, rebase task
`task_32f2c6b021444aa6a495`): a `direct-pr` continuation was granted the
lane's branch, rebased it onto main through the git tools and pushed it
through swarm-api (a lease-guarded rewrite). The end-of-task publish then
diffed from the CLONE base -- the lane's old tip, which the rebased head no
longer stands on -- folded the tree into one worker commit on that old tip,
pushed it without a lease, was refused as behind its remote, and refused
itself the own-earlier-attempt lease ("not this task's own branch"). The task
ended FAILED publish_failed with its work already on the branch.

The property, against a real bare repository on disk (only HTTP to the
forge is faked, as in test_continue_swarm_branch.py):

  * a head equal to the tip this attempt's own tool push left publishes as
    it is: nothing more lands on the branch, and the pull request step runs
    as it does today;
  * commits the agent made after its tool push are published over that tip,
    with `--force-with-lease` to exactly it;
  * a branch someone else moved past that tip is refused by the lease, and
    nothing is pushed.

The relay's half -- that the tip is the one swarm-api answered a push with,
and only for a branch this task may push -- is in test_gittools.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import lifecycle

from test_strategy_end_to_end import _the_fence_holds  # noqa: F401 -- fixture
from test_continue_swarm_branch import (  # noqa: F401 -- fixtures are used by name
    FIX,
    ROOT,
    _the_agent_titles_its_pull_request,
    _worker,
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

RED = f"swarm/{ROOT}"
CONTINUES = {"strategy": "direct-pr", "continues": ROOT}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _push_as_someone(origin: Path, tmp: Path, branch: str, name: str) -> str:
    """One commit on `branch` on the remote, written outside this attempt."""
    clone = tmp / f"someone-{name}"
    subprocess.run(["git", "clone", "--quiet", "--branch", branch, str(origin), str(clone)],
                   check=True, capture_output=True)
    (clone / f"{name}.txt").write_text(f"{name}\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", name)
    _git(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return refs(origin)[branch]


@pytest.fixture
def pushes(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real = lifecycle.push_branch

    def spy(**kwargs: Any) -> str:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", spy)
    return calls


def _rebased_and_tool_pushed(worker_factory, monkeypatch, origin: Path, tmp: Path):
    """What the rebase task did on 2026-10-23: main moves, the agent rebases
    the lane's branch onto it, and swarm-api pushes the result over the branch
    (its lease-guarded rewrite, stood in for by a forced push here). The relay
    remembers the tip that push answered with."""
    worker, config, task = _worker(worker_factory, monkeypatch, origin, CONTINUES)
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    _push_as_someone(origin, tmp, "main", "main-moved")
    _git(repo, "fetch", "--quiet", f"file://{origin}", "main")
    onto = _git(repo, "rev-parse", "FETCH_HEAD")
    _git(repo, "rebase", "--quiet", onto)
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "--quiet", "--force", f"file://{origin}", f"HEAD:refs/heads/{RED}")
    assert refs(origin)[RED] == tip
    worker.children._git = SimpleNamespace(
        rebased_onto=onto, pushed_tip=lambda branch: tip if branch == RED else None,
    )
    return worker, config, repo, tip


def test_a_head_the_tool_already_pushed_publishes_as_it_is(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path, log_stream
):
    worker, _, _, tip = _rebased_and_tool_pushed(worker_factory, monkeypatch, origin, tmp_path)

    out = worker._harvest_git(publish=True)

    # MUTATION: drop `_adopt_tool_pushed_tip` from `_publish_git` and the
    # publish folds onto the clone base and is refused: published False,
    # publish_failed, exactly the 2026-10-23 failure.
    assert out["published"] is True, out.get("publish_reason")
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out
    # MUTATION: replay or fold the tool-pushed head as before and the branch
    # moves to a worker commit on the old tip, or the push is refused.
    assert refs(origin)[RED] == tip, "something more was pushed over the tool's push"
    assert out["pushed_head"] == tip and out["tool_pushed_tip"] == tip
    assert out.get("agent_commits_kept") == 0 and not out.get("agent_commits_folded"), out
    # MUTATION: push without the lease and a branch someone moved since would
    # be overwritten or refused for the wrong reason; here the lease is the tip.
    assert [call.get("lease") for call in pushes] == [tip]
    # The pull request step ran exactly as for any continuation.
    assert [pull["head"] for pull in forge.pulls] == [RED]
    assert f"swarm/{FIX}" not in refs(origin)
    assert "already pushed through git_push_branch" in log_stream.getvalue()


def test_commits_after_the_tool_push_are_published_with_a_lease_on_its_tip(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    worker, _, repo, tip = _rebased_and_tool_pushed(worker_factory, monkeypatch, origin, tmp_path)
    (repo / "after.txt").write_text("committed after the tool push\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "after the tool push")
    (repo / "left.txt").write_text("left uncommitted\n")

    out = worker._harvest_git(publish=True)

    # MUTATION: drop the adoption and this folds onto the clone base and is
    # refused as behind its remote (published False).
    assert out["published"] is True, out.get("publish_reason")
    landed = refs(origin)[RED]
    assert landed != tip and out["pushed_head"] == landed
    # MUTATION: diff from the clone base instead of the tip and the rebased
    # commits are replayed or folded again: more than these two commits.
    added = _git(origin, "rev-list", f"{tip}..{RED}").split()
    assert len(added) == 2, added
    _git(origin, "merge-base", "--is-ancestor", tip, RED)
    tree = _git(origin, "ls-tree", "-r", "--name-only", RED).split()
    assert "after.txt" in tree and "left.txt" in tree
    # MUTATION: push with `lease=None` (a plain push) and this list reads [None].
    assert [call.get("lease") for call in pushes] == [tip]
    assert [pull["head"] for pull in forge.pulls] == [RED]


@pytest.mark.parametrize("extra", [True, False], ids=["with-commits", "head-at-tip"])
def test_a_branch_someone_else_moved_past_the_tool_push_is_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path, extra
):
    worker, _, repo, tip = _rebased_and_tool_pushed(worker_factory, monkeypatch, origin, tmp_path)
    if extra:
        (repo / "after.txt").write_text("committed after the tool push\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", "after the tool push")
    theirs = _push_as_someone(origin, tmp_path, RED, "a-person")

    out = worker._harvest_git(publish=True)

    assert out["published"] is False, out
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    # MUTATION: a bare force (or a lease on anything but the tool's tip)
    # replaces their commit; the lease refuses it.
    assert refs(origin)[RED] == theirs, "a commit someone else pushed was overwritten"
    # MUTATION: skip the push when the head equals the tip and the
    # head-at-tip case reads published True over a branch that moved.
    assert [call.get("lease") for call in pushes] == [tip]
    assert forge.pulls == [], "a pull request was asked for after a refused push"


def test_a_tip_the_head_does_not_descend_from_is_not_adopted(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    """The control: an agent that reset away from its own tool push publishes
    exactly as before -- no lease, and the never-forced push is refused."""
    worker, _, repo, tip = _rebased_and_tool_pushed(worker_factory, monkeypatch, origin, tmp_path)
    _git(repo, "reset", "--quiet", "--hard", worker._publish_base)
    (repo / "again.txt").write_text("redone on the old base\n")

    out = worker._harvest_git(publish=True)

    assert "tool_pushed_tip" not in out
    # MUTATION: adopt a tip the head does not descend from and this push
    # carries a lease that replaces the tool's push with an unrelated line.
    assert [call.get("lease") for call in pushes] == [None]
    assert out["published"] is False and refs(origin)[RED] == tip


def test_the_publish_reads_the_tip_through_child_path_pushed_tip(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    """`_adopt_tool_pushed_tip` asks ChildPath's public read, not the relay
    behind it (GITTOOLS-FOLLOW): the relay here has no `pushed_tip` at all,
    and the stubbed `ChildPath.pushed_tip` is the only place the tip lives."""
    worker, _, repo, tip = _rebased_and_tool_pushed(worker_factory, monkeypatch, origin, tmp_path)
    worker.children._git = SimpleNamespace(rebased_onto=worker.children._git.rebased_onto)
    asked: list[str] = []

    def pushed_tip(branch: str) -> str | None:
        asked.append(branch)
        return tip if branch == RED else None

    # raising=False: before GITTOOLS-FOLLOW there was no such read, and the
    # red run fails on the assertion below rather than on this line.
    monkeypatch.setattr(worker.children, "pushed_tip", pushed_tip, raising=False)

    # MUTATION: read `getattr(self.children, "_git", None).pushed_tip` again
    # and this relay has none: the tip is not adopted and this reads None.
    assert worker._adopt_tool_pushed_tip(repo, RED) == tip
    # MUTATION: read the tip for any branch but the one being published and
    # this list names another branch, or is empty.
    assert asked == [RED]


def test_a_tool_pushed_head_off_the_start_sha_is_not_a_moved_head(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    """#2094's guard and this path, together: the rebased head no longer
    descends from the commit the step started on, but it stands on the tip
    this attempt's own tool push left on the task's branch. That is this
    task's work, already published, and not a HEAD that moved off it."""
    worker, _, repo, tip = _rebased_and_tool_pushed(worker_factory, monkeypatch, origin, tmp_path)
    start = worker._publish_base
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", start, "HEAD"], cwd=str(repo),
    ).returncode == 1, "the scenario needs a head off the start sha"

    out = worker._harvest_git(publish=True)

    # MUTATION: anchor the moved-HEAD check on the start sha alone and this
    # publishes nothing with "HEAD moved off this task's branch".
    assert "head_moved" not in out, out.get("publish_reason")
    assert out["published"] is True, out.get("publish_reason")
    assert out["tool_pushed_tip"] == tip and refs(origin)[RED] == tip
    assert [pull["head"] for pull in forge.pulls] == [RED]
