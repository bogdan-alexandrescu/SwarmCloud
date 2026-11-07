"""Every branch the agent merged reaches the pull request as a parent, whatever it is (#3359).

WHAT WENT WRONG AGAIN (6 November 2026, reported by a peer application's session).
After #3363, commit 6a4538246 "merge train AO head ebac1ff2" on Product
Oracle's pull request 1292 still had ONE parent and the train's tree, so the
pull request's next merge would replay the train's 88 files as conflicts.

THE CAUSES. `merged_head_kept` drops a merged head whose own commits carry
Claude attribution, so that a side branch the agent committed on as Claude
never reaches the forge. A merge train's branch is no such side branch: it is
on the forge already, built by whoever built it, and one commit on it with a
`Co-Authored-By: Claude` trailer dropped the whole train. And a merge the
agent left IN PROGRESS (`git merge --no-commit`, or a resolved conflict it
staged and never committed) was committed by the worker in the clean publish
repository, which had no `MERGE_HEAD`, so the worker's own commit squashed it.

WHAT IS PINNED: a merged head the forge holds stays a parent, fetched by
refspec into FETCH_HEAD or by name, train or any branch; the worker's commit
of a merge in progress is a merge whose second parent is the merged head; a
side branch the agent made as Claude, which the forge does not hold, is still
dropped; the fold does not leave a worker commit that is a merge unread.

Real git against local repositories; only the forge is faked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops

from test_fold_keeps_merge_parents import (
    AGENT,
    _commit,
    _git,
    _is_ancestor,
    _logger,
    _repo,
)
from test_publish_to_pr import (  # noqa: F401 -- fixtures are used by name
    DIRECT,
    PR_BRANCH,
    _no_agent_text,
    _titled,
    pull,
)
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures are used by name
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: A trailer Claude Code adds, on a commit made OUTSIDE this platform (a
#: person's own Claude Code session), as on a peer application's train.
TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _push_branch(tmp_path: Path, origin: Path, name: str) -> str:
    """`name` on the forge: main plus three commits, the middle one with
    Claude's trailer, as a train someone built in their own session."""
    work = tmp_path / f"build-{name.replace('/', '-')}"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(origin), str(work)],
                   check=True, capture_output=True)
    _commit(work, f"{name.replace('/', '-')}-1")
    _commit(work, f"{name.replace('/', '-')}-2", message=f"lane two\n\n{TRAILER}")
    head = _commit(work, f"{name.replace('/', '-')}-3")
    _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
    return head


def _merges_pushed(origin: Path, start: str, tip: str) -> list[list[str]]:
    return [line.split()[1:] for line in
            _git(origin, "rev-list", "--merges", "--parents", f"{start}..{tip}").splitlines()]


# -- 1. a branch the forge holds stays a parent ----------------------------------


def test_a_train_fetched_by_refspec_and_merged_from_fetch_head_is_a_parent(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """The report's shape: `git fetch origin refs/heads/train/AO`, then
    `git merge FETCH_HEAD`, on a train with one attributed commit."""
    _no_agent_text(monkeypatch)
    train = _push_branch(tmp_path, origin, "train/AO")
    start = refs(origin)[PR_BRANCH]

    def edit(repo: Path) -> None:
        _git(repo, "fetch", "-q", f"file://{origin}", "refs/heads/train/AO")
        _git(repo, "merge", "-q", "--no-ff", "--allow-unrelated-histories",
             "-m", f"merge train AO head {train[:8]}", "FETCH_HEAD", who=AGENT)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359-train",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    # MUTATION: drop the `on_forge` answer in `merged_head_kept` (the code
    # before this change) -> the attributed commit drops the train, and the
    # merge is pushed with one parent: no merge at all.
    assert _merges_pushed(origin, start, tip) == [[start, train]]
    assert _is_ancestor(origin, train, tip)
    assert _is_ancestor(origin, start, tip)


def test_any_non_default_branch_merged_by_name_is_a_parent(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    _no_agent_text(monkeypatch)
    feature = _push_branch(tmp_path, origin, "feature/search")
    start = refs(origin)[PR_BRANCH]

    def edit(repo: Path) -> None:
        _git(repo, "fetch", "-q", f"file://{origin}",
             "feature/search:refs/remotes/origin/feature/search")
        _git(repo, "merge", "-q", "--no-ff", "--allow-unrelated-histories",
             "-m", "merge feature/search", "origin/feature/search", who=AGENT)
        _commit(repo, "the-lanes-fix", who=AGENT)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359-any",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    assert _merges_pushed(origin, start, tip) == [[start, feature]]
    assert {"feature-search-1.txt", "the-lanes-fix.txt"} <= tree_at(origin, tip)
    assert "anthropic.com" not in _git(origin, "log", "-1", "--format=%ae%n%ce", tip)


def test_a_side_branch_the_agent_made_as_claude_is_still_dropped(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """The forge does not hold it, so asking the forge changes nothing."""
    _no_agent_text(monkeypatch)
    start = refs(origin)[PR_BRANCH]
    heads: list[str] = []

    def edit(repo: Path) -> None:
        _git(repo, "checkout", "-q", "-b", "work")
        _git(repo, "checkout", "-q", "-b", "side")
        heads.append(_commit(repo, "side-1", who=AGENT))
        _git(repo, "checkout", "-q", "work")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side", who=AGENT)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359-side",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    assert not _is_ancestor(origin, heads[0], tip)
    assert _is_ancestor(origin, start, tip)  # still a fast-forward
    assert "side-1.txt" in tree_at(origin, tip)


# -- 2. the worker's own commit never squashes a merge ----------------------------


def test_a_merge_left_in_progress_is_committed_by_the_worker_as_a_merge(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """`git merge --no-commit FETCH_HEAD`, never committed: the worker's
    commit of the uncommitted work concludes the merge."""
    _no_agent_text(monkeypatch)
    train = _push_branch(tmp_path, origin, "train/AP")
    start = refs(origin)[PR_BRANCH]

    def edit(repo: Path) -> None:
        _git(repo, "fetch", "-q", f"file://{origin}", "refs/heads/train/AP")
        _git(repo, "merge", "-q", "--no-ff", "--no-commit", "--allow-unrelated-histories",
             "FETCH_HEAD", who=AGENT)
        (repo / "resolved.txt").write_text("the agent's edit on top of the merge\n")

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359-nocommit",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    # MUTATION: drop `carry_merge` in `_build_clean_repo` -> the worker's
    # commit has one parent and the train's history is not on the remote.
    assert _merges_pushed(origin, start, tip) == [[start, train]]
    assert {"train-AP-1.txt", "resolved.txt"} <= tree_at(origin, tip)


def test_the_fold_keeps_a_merged_head_the_forge_holds_whatever_its_commits_say(
    tmp_path: Path,
):
    """The fold (a replay that cannot keep the commits one by one) asks the
    forge too: an attributed train it holds stays a parent, a side branch
    it does not hold is still left out."""
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "train", base)
    _commit(repo, "t1", message=f"lane one\n\n{TRAILER}")
    train = _commit(repo, "t2")
    _git(repo, "checkout", "-q", "-b", "side", base)
    side = _commit(repo, "s1", who=AGENT)
    _git(repo, "checkout", "-q", "work")
    for ref in ("train", "side"):
        _git(repo, "merge", "-q", "--no-ff", "-m", f"merge {ref}", ref, who=AGENT)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    asked: list[str] = []

    def on_forge(sha: str) -> bool:
        asked.append(sha)
        return sha == train

    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    gitops.fold_agent_commits(
        repo=repo, base=base, keep=None, message="the worker's commit",
        author_name="Swarm Worker", author_email="worker@example.invalid",
        private_dir=tmp_path / "private", logs_dir=logs, timeout_seconds=60,
        logger=_logger(), on_forge=on_forge,
    )
    # MUTATION: drop `on_forge` from `_merged_heads` -> [base] alone.
    assert _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [base, train]
    assert not _is_ancestor(repo, side, "HEAD")
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree
    assert sorted(asked) == sorted([train, side])


# -- 3. the forge's answer ---------------------------------------------------------


def test_the_forge_holds_a_commit_it_has_and_not_one_only_this_clone_has(
    local_urls, tmp_path: Path
):
    forge_repo = tmp_path / "forge.git"
    _git(tmp_path, "init", "-q", "--bare", str(forge_repo))
    repo, base = _repo(tmp_path)
    _git(repo, "push", "-q", str(forge_repo), "main")
    local_only = _commit(repo, "local")
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)

    def holds(sha: str) -> bool:
        return gitops.forge_holds_commit(
            url=f"file://{forge_repo}", sha=sha, token="", private_dir=tmp_path / "private",
            logs_dir=logs, timeout_seconds=60, logger=_logger(),
        )

    assert holds(base) is True
    assert holds(local_only) is False
    assert holds("not-a-sha") is False
    # The probe repository is removed: nothing is left under private/.
    assert not list((tmp_path / "private").glob("forge-probe-*"))
