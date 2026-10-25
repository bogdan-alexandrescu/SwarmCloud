"""A continued branch moved only by a default-branch merge takes the round's fix as a fast-forward (#2179).

MEASURED 2026-10-23 (failure-review `sch_54e9828e6772`, agent
`task_045fda9399ba47289b3a`): a CI-fix round for PR #2093 cloned the pull
request's branch at bfdc51ad and made one commit; meanwhile GitHub's "Update
branch" put a merge of main on that branch (c903caca, parents bfdc51ad and
main's 3c6bba9b). The round's push, never forced, was a non-fast-forward,
and the PushRejected handler only recovered a branch holding this task's own
earlier attempt (#1438), which a continued branch is not. The task ended
publish_failed, although the moved tip was only main merged in -- and already
carried the same fix.

The property, against a real bare repository on disk (only HTTP to the forge
is faked, as in test_strategy_end_to_end.py):

  * a branch that gained only a merge of the default branch since the clone
    takes the round's commits replayed onto that merge, pushed as a plain
    fast-forward (no lease, never forced), every added commit the worker's;
  * a moved tip that already makes the round's diff empty pushes nothing and
    ends no_change, not publish_failed;
  * a branch moved by anything else -- someone's ordinary commit, a merge
    whose second parent is not on the default branch -- is left exactly as
    it was, and the attempt fails publish_failed as before;
  * a round whose file the base merge also rewrote -- every conflicted path
    of the replay is one the merge changed, clone base..moved tip -- takes
    the base's version of it (#2367): the round ends no_change with a
    `superseded_by_base` reason naming the paths and the tip, not
    publish_failed, and what is left of the round over the tip still lands
    as the worker's fast-forward, with the superseded paths on the record;
  * a replay conflict in a path the base merge did not change pushes nothing
    and fails publish_failed, saying so.

MEASURED 2026-10-25 (#2367): four CI-fix rounds had hand-patched
tests/unit/scripts/unit_test_durations.json, main's #2312 rewrote that file
and was merged into their branches, and each replay conflicted on it and
ended publish_failed.

The own-branch recovery (#1438) is pinned by
test_retry_publishes_onto_its_own_branch.py, which this change leaves as it
was.

WRITTEN TO FAIL BEFORE THE CHANGE: there, every push below was refused and
`published` was False with `publish_failed` set.
The two superseded cases were written to fail before #2367: there the
replay raised 'conflicted' and the round ended publish_failed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import expected_outputs as expected_mod
from agent_worker import gitops, lifecycle, merge as merge_mod

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _attempt,
    _the_agent_titles_its_pull_request,
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    pushed_commits,
    refs,
    tree_at,
)

ROOT = "task_0123456789abcdef0123"
FIX = "task_fedcba9876543210fedc"
CONTINUES = {"strategy": "direct-pr", "continues": ROOT}
#: GitHub's own identity on an "Update branch" merge.
GITHUB = ("GitHub", "noreply@github.com")


def _sh(cwd: Path, *args: str, who: tuple[str, str] = ("t", "t@example.invalid")) -> str:
    # In the environment, which outranks any `user.*` an ambient config of the
    # machine running the test would otherwise supply.
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


def _commit(clone: Path, name: str, text: str, message: str, **kw: Any) -> str:
    (clone / name).write_text(text)
    _sh(clone, "add", name, **kw)
    _sh(clone, "commit", "--quiet", "-m", message, **kw)
    return _sh(clone, "rev-parse", "HEAD")


@pytest.fixture
def red(origin: Path, tmp_path: Path) -> str:
    """`swarm/<ROOT>` on the remote: the pull request's branch the round continues."""
    branch = f"swarm/{ROOT}"
    clone = _writer(origin, tmp_path)
    _sh(clone, "checkout", "--quiet", "-b", branch)
    _commit(clone, "feature.py", "def broken():\n    return 1\n", "the work that went red")
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return branch


def _update_branch(origin: Path, tmp: Path, branch: str, *, main_file: str, main_text: str):
    """What GitHub's "Update branch" does: a commit lands on main, and a
    two-parent merge of main goes on `branch` (first parent its old tip)."""
    clone = _writer(origin, tmp)
    on_main = _commit(clone, main_file, main_text, "a change on main (#2092)")
    _sh(clone, "push", "--quiet", "origin", "HEAD:main")
    _sh(clone, "checkout", "--quiet", "-b", "pr", f"origin/{branch}")
    _sh(clone, "merge", "--quiet", "--no-ff", "-m", f"Merge branch 'main' into {branch}",
        on_main, who=GITHUB)
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return refs(origin)[branch], on_main


def _fixer(worker_factory, monkeypatch, origin: Path):
    worker, config, task = _attempt(
        worker_factory, monkeypatch, origin, task_id=FIX, attempt=1, dispatch=CONTINUES
    )
    cloned = worker._maybe_clone(task)
    assert cloned["commit"], "the clone did not land"
    return worker, config, worker.ws.work / lifecycle.REPO_DIR_NAME


@pytest.fixture
def pushes(monkeypatch) -> list[dict[str, Any]]:
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


def test_a_fix_lands_on_a_branch_github_updated_with_main_as_a_fast_forward(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    worker, config, repo = _fixer(worker_factory, monkeypatch, origin)
    cloned_at = refs(origin)[red]
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    moved, on_main = _update_branch(origin, tmp_path, red, main_file="shards.py",
                                    main_text="MAX_SHARDS = 9\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out.get("publish_reason")
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD)
    landed = refs(origin)[red]
    assert out["branch"] == red and out["pushed_head"] == landed
    # Onto the moved tip, never over it: GitHub's merge and the clone base
    # are both still under the new head.
    assert _is_ancestor(origin, moved, landed), "the base merge was replaced"
    assert _is_ancestor(origin, cloned_at, landed)
    added = pushed_commits(origin, red, since=moved)
    assert len(added) == 1 and added[0]["parents"] == [moved]
    assert_only_the_worker_wrote(added, config)
    # Both main's change and the fix.
    assert {"shards.py", "feature.py"} <= tree_at(origin, red)
    assert "return 2" in _sh(origin, "show", f"{red}:feature.py")
    assert refs(origin)["main"] == on_main
    # Never a lease and never a force: the first push and the replay's both
    # plain pushes, the second a fast-forward over the fetched tip.
    assert [call.get("lease") for call in pushes] == [None, None]
    assert out["base_merge_tip"] == moved
    assert [p["head"] for p in forge.pulls] == [red]


def test_a_moved_tip_that_already_holds_the_fix_ends_no_change(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    """The #2093 case: main already carried `MAX_SHARDS = 9`, the very change
    the round made."""
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "shards.py").write_text("MAX_SHARDS = 9\n")
    moved, _ = _update_branch(origin, tmp_path, red, main_file="shards.py",
                              main_text="MAX_SHARDS = 9\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out.get("publish_reason")
    assert refs(origin)[red] == moved, "something was pushed over the moved tip"
    assert moved[:12] in out[lifecycle.BASE_MERGE_NO_CHANGE_FIELD]
    assert [call.get("lease") for call in pushes] == [None]
    assert forge.pulls == []
    # The finish reads it as the attempt's no_change result, not a failure.
    summary: dict[str, Any] = {"git": out}
    assert worker._no_change_after_publish(summary) is True
    assert summary[expected_mod.NO_CHANGE_SUMMARY_KEY] is True
    assert moved[:12] in summary[lifecycle.NO_CHANGE_REASON_SUMMARY_KEY]
    assert worker._publish_failed(summary) is None
    assert worker._published_nothing(summary) is None


def _ordinary_commit(origin: Path, tmp: Path, branch: str) -> str:
    clone = _writer(origin, tmp)
    _sh(clone, "checkout", "--quiet", "-b", "pr", f"origin/{branch}")
    _commit(clone, "theirs.txt", "a human got there first\n", "theirs",
            who=("a person", "person@example.invalid"))
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return refs(origin)[branch]


def _merge_of_a_side_branch(origin: Path, tmp: Path, branch: str) -> str:
    """A two-parent merge whose second parent is NOT on the default branch."""
    clone = _writer(origin, tmp)
    _sh(clone, "checkout", "--quiet", "-b", "side", "origin/main")
    side = _commit(clone, "side.txt", "never on main\n", "a side branch")
    _sh(clone, "checkout", "--quiet", "-b", "pr", f"origin/{branch}")
    _sh(clone, "merge", "--quiet", "--no-ff", "-m", "Merge branch 'side'", side, who=GITHUB)
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return refs(origin)[branch]


@pytest.mark.parametrize("mover", [_ordinary_commit, _merge_of_a_side_branch],
                         ids=["ordinary_commit", "merge_not_of_the_default_branch"])
def test_a_branch_moved_by_anything_else_is_left_as_it_was(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path, mover
):
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    theirs = mover(origin, tmp_path, red)

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "rejected" in out["publish_reason"]
    assert refs(origin)[red] == theirs, "the branch someone else moved was changed"
    assert [call.get("lease") for call in pushes] == [None]
    assert forge.pulls == []


def test_a_round_whose_file_the_base_merge_rewrote_ends_superseded_by_base(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    """The #2367 case: the round hand-patched a file main then rewrote and
    merged into the branch. Main's version stands; the round is no_change."""
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "shards.py").write_text("MAX_SHARDS = 7\n")
    moved, _ = _update_branch(origin, tmp_path, red, main_file="shards.py",
                              main_text="MAX_SHARDS = 9\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out.get("publish_reason")
    assert refs(origin)[red] == moved, "something was pushed over the moved tip"
    assert [call.get("lease") for call in pushes] == [None]
    assert forge.pulls == []
    assert out[lifecycle.BASE_MERGE_SUPERSEDED_FIELD] == ["shards.py"]
    reason = out[lifecycle.BASE_MERGE_NO_CHANGE_FIELD]
    assert reason.startswith("superseded_by_base"), reason
    assert moved[:12] in reason and "shards.py" in reason
    summary: dict[str, Any] = {"git": out}
    assert worker._no_change_after_publish(summary) is True
    assert summary[expected_mod.NO_CHANGE_SUMMARY_KEY] is True
    assert summary[lifecycle.NO_CHANGE_REASON_SUMMARY_KEY].startswith("superseded_by_base")
    assert worker._publish_failed(summary) is None
    assert worker._published_nothing(summary) is None


def test_a_partly_superseded_round_pushes_the_rest_as_a_fast_forward(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    worker, config, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "shards.py").write_text("MAX_SHARDS = 7\n")
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    moved, _ = _update_branch(origin, tmp_path, red, main_file="shards.py",
                              main_text="MAX_SHARDS = 9\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out.get("publish_reason")
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD)
    landed = refs(origin)[red]
    assert out["pushed_head"] == landed
    added = pushed_commits(origin, red, since=moved)
    assert len(added) == 1 and added[0]["parents"] == [moved]
    assert_only_the_worker_wrote(added, config)
    assert "return 2" in _sh(origin, "show", f"{red}:feature.py")
    assert _sh(origin, "show", f"{red}:shards.py") == "MAX_SHARDS = 9"
    assert out[lifecycle.BASE_MERGE_SUPERSEDED_FIELD] == ["shards.py"]
    assert not out.get(lifecycle.BASE_MERGE_NO_CHANGE_FIELD)
    assert [call.get("lease") for call in pushes] == [None, None]


def test_a_conflict_outside_the_base_merges_files_still_fails_publish(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    """The conflicted path is not one the base merge changed (the read of
    what it changed is stubbed to exclude it), so the round's version is
    not known to be stale: nothing is taken, nothing is pushed."""
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "shards.py").write_text("MAX_SHARDS = 7\n")
    moved, _ = _update_branch(origin, tmp_path, red, main_file="shards.py",
                              main_text="MAX_SHARDS = 9\n")
    monkeypatch.setattr(gitops, "base_merge_changed_paths", lambda **_: frozenset())

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    reason = out["publish_reason"]
    assert "base merge" in reason and "conflict" in reason, reason
    assert refs(origin)[red] == moved
    assert [call.get("lease") for call in pushes] == [None]
    assert not out.get(lifecycle.BASE_MERGE_NO_CHANGE_FIELD)


def test_the_walk_is_bounded_as_the_merge_steps_is():
    """At most as many base merges as the merge step accepts from GitHub
    (`merge._updates_onto`): a longer line is not an update, it is a branch
    someone keeps writing to."""
    assert gitops.BASE_MERGE_MAX_STEPS == merge_mod.MERGE_MAX_HEAD_UPDATES


def test_a_branch_outside_the_prefix_is_never_fetched(tmp_path):
    with pytest.raises(gitops.GitError, match="outside"):
        gitops.base_merge_tip(
            repo=tmp_path / "no-such-repository", url="https://github.com/acme/widgets.git",
            branch="feature/x", default_branch="main", token="unused", base="0" * 40,
            private_dir=tmp_path / "p", logs_dir=tmp_path / "l", timeout_seconds=5,
            logger=None,
        )
