"""The publish fold keeps every branch the agent merged as a parent (#3359).

WHAT WENT WRONG (5 November 2026, reported by Product Oracle's session). A
direct-pr task pushing onto an existing pull request (`publish_to_pr`) ran
`git merge origin/train/an`, and the commit the worker pushed had ONE parent:
the fold (`gitops.fold_agent_commits`) rewinds the agent's commits to the
clone base and commits the tree as one worker commit, and it kept a second
parent only for the default branch's tip. train/an's TREE was pushed without
its HISTORY, so merging the pull request back raised again every conflict the
lane had resolved (6 files on one pull request, 16 on another).

WHAT IS PINNED: the folded commit's first parent is the base, and after it
comes the head of every branch the agent merged that the base does not
already contain; a merge of the default branch behaves as it did; a head the
clean repository cannot read fails the fold rather than fold without it.

Real git against local repositories; only the forge is faked.
"""

from __future__ import annotations

import io
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops, lifecycle

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

#: The identity Claude Code commits as where none is set: what the fold removes.
AGENT = ["-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com"]
PERSON = ["-c", "user.name=Someone", "-c", "user.email=someone@example.invalid"]


def _git(repo: Path, *args: str, who: list[str] = PERSON) -> str:
    done = subprocess.run(["git", *who, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def _commit(repo: Path, name: str, *, who: list[str] = PERSON, message: str = "") -> str:
    (repo / f"{name}.txt").write_text(f"{name}\n")
    _git(repo, "add", "-A", who=who)
    _git(repo, "commit", "-q", "-m", message or name, who=who)
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, ref: str, *extra: str) -> str:
    """The agent's `git merge --no-ff <ref>`, committed as Claude."""
    _git(repo, "merge", "-q", "--no-ff", *extra, "-m", f"merge {ref}", ref, who=AGENT)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """main with two commits, checked out on `work`; returns the repo and the base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "root")
    base = _commit(repo, "main-1")
    _git(repo, "checkout", "-q", "-b", "work")
    return repo, base


def _branch(repo: Path, name: str, start: str, files: list[str]) -> str:
    """A branch `name` from `start` with a commit per file, made by a person."""
    _git(repo, "checkout", "-q", "-b", name, start)
    for file in files:
        _commit(repo, file)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "work")
    return head


def _logger():
    from agent_worker.logs import build_logger

    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="acme",
                        generation=1, runner_profile="mock", stream=io.StringIO())


def _fold(tmp_path: Path, repo: Path, base: str, *, merged: str = "") -> int:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return gitops.fold_agent_commits(
        repo=repo, base=base, keep=None, message="the worker's commit",
        author_name="Swarm Worker", author_email="worker@example.invalid",
        private_dir=tmp_path / "private", logs_dir=logs, timeout_seconds=60,
        logger=_logger(), merged=merged,
    )


def _parents(repo: Path, sha: str = "HEAD") -> list[str]:
    return _git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def _is_ancestor(repo: Path, sha: str, of: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, of],
                          capture_output=True).returncode == 0


def _folded_by_the_worker(repo: Path) -> None:
    author = _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
    assert author == "Swarm Worker <worker@example.invalid>|Swarm Worker <worker@example.invalid>"


# -- 1. the fold --------------------------------------------------------------


def test_a_merge_of_a_non_default_branch_keeps_its_head_as_a_parent(tmp_path: Path):
    repo, base = _repo(tmp_path)
    train = _branch(repo, "train", base, ["t1", "t2"])
    _merge(repo, "train")
    _commit(repo, "mine", who=AGENT)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")

    assert _fold(tmp_path, repo, base) == 4
    # MUTATION: commit the fold with `-p base` alone (the code before #3359)
    # -> one parent, and train is no ancestor of what is pushed.
    assert _parents(repo) == [base, train]
    assert _is_ancestor(repo, train, "HEAD")
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree
    _folded_by_the_worker(repo)


def test_two_merges_keep_both_heads_in_the_order_merged(tmp_path: Path):
    repo, base = _repo(tmp_path)
    first = _branch(repo, "first", base, ["a1"])
    second = _branch(repo, "second", base, ["b1", "b2"])
    _merge(repo, "first")
    _commit(repo, "between", who=AGENT)
    _merge(repo, "second")

    _fold(tmp_path, repo, base)
    # MUTATION: keep only the newest merge's head -> `first` is lost.
    assert _parents(repo) == [base, first, second]
    assert _is_ancestor(repo, first, "HEAD") and _is_ancestor(repo, second, "HEAD")


def test_a_branch_merged_twice_is_kept_once_at_its_newer_head(tmp_path: Path):
    repo, base = _repo(tmp_path)
    older = _branch(repo, "train", base, ["t1"])
    _merge(repo, older)
    _git(repo, "checkout", "-q", "train")
    newer = _commit(repo, "t2")
    _git(repo, "checkout", "-q", "work")
    _merge(repo, "train")

    _fold(tmp_path, repo, base)
    # MUTATION: drop the "another kept head contains it" pass -> three parents.
    assert _parents(repo) == [base, newer]


def test_a_merge_of_the_default_branch_behaves_as_before(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _commit(repo, "mine", who=AGENT)
    _git(repo, "checkout", "-q", "main")
    main_tip = _commit(repo, "main-2")
    _git(repo, "checkout", "-q", "work")
    _merge(repo, "main")

    _fold(tmp_path, repo, base, merged=main_tip)
    assert _parents(repo) == [base, main_tip]
    _folded_by_the_worker(repo)


def test_a_merged_default_branch_proven_by_the_forge_but_not_merged_last_is_kept(
    tmp_path: Path,
):
    """The caller's `merged`, a commit HEAD holds, stays the fold's second
    parent whether or not it is a merge commit's parent itself."""
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-q", "main")
    main_1 = _commit(repo, "main-2")
    _commit(repo, "main-3")
    _git(repo, "checkout", "-q", "work")
    _merge(repo, "main")

    _fold(tmp_path, repo, base, merged=main_1)
    # main-3 contains main-2: one parent for the default branch, its newer head.
    assert _parents(repo) == [base, _git(repo, "rev-parse", "main")]


def test_a_merge_whose_head_is_already_in_the_base_adds_no_parent(tmp_path: Path):
    repo, base = _repo(tmp_path)
    old = _git(repo, "rev-parse", "HEAD~1")
    _commit(repo, "mine", who=AGENT)
    # A merge of a commit the base holds, made the one way git makes it.
    made = _git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-p", old,
                "-m", "merge old", who=AGENT)
    _git(repo, "reset", "-q", "--soft", made)
    assert len(_parents(repo)) == 2  # the control: the agent made a merge

    _fold(tmp_path, repo, base)
    # MUTATION: drop the `--is-ancestor <head> <base>` test -> two parents.
    assert _parents(repo) == [base]


def test_a_merged_side_branch_of_claudes_own_commits_is_folded_without_its_history(
    tmp_path: Path,
):
    """The fold exists to keep Claude's commits off the forge: a branch the
    agent committed on as Claude and merged is not kept as a parent."""
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "side", who=AGENT)
    _git(repo, "checkout", "-q", "work")
    _commit(repo, "mine", who=AGENT)
    _merge(repo, "side")

    _fold(tmp_path, repo, base)
    # MUTATION: drop the attribution read -> Claude's `side` commit is pushed.
    assert _parents(repo) == [base]
    assert {"side.txt", "mine.txt"} <= set(_git(repo, "ls-tree", "--name-only", "HEAD").split())


def test_a_persons_co_authored_by_on_a_merged_branch_does_not_drop_it(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "train")
    train = _commit(repo, "t1", message="t1\n\nCo-authored-by: A Person <a@example.invalid>")
    _git(repo, "checkout", "-q", "work")
    _merge(repo, "train")

    _fold(tmp_path, repo, base)
    assert _parents(repo) == [base, train]


def test_a_merged_head_the_repository_cannot_read_fails_the_fold():
    """A head `cat-file` cannot read is `MergedHeadUnreadable`, which the
    publish turns into a failure that keeps the bundle; never a fold that
    drops the branch's history in silence."""
    head = "c" * 40

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        if "rev-list" in argv:
            return 0, f"{'a' * 40} {'b' * 40} {head}\n"
        if "cat-file" in argv:
            return 128, ""
        return 0, ""

    with pytest.raises(gitops.MergedHeadUnreadable, match="cannot read") as caught:
        gitops._merged_heads(run, ["git"], base="b" * 40, proven="", logger=_logger())
    assert head[:12] in str(caught.value)
    assert isinstance(caught.value, gitops.GitError)


# -- 2. the issue's shape: publish_to_pr onto an existing pull request ------------


def test_a_lane_that_merges_a_train_branch_into_its_pull_request_pushes_the_merge(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    _no_agent_text(monkeypatch)
    train_work = tmp_path / "train-work"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(origin), str(train_work)],
                   check=True, capture_output=True)
    _commit(train_work, "train-1")
    train = _commit(train_work, "train-2")
    _git(train_work, "push", "-q", "origin", "HEAD:refs/heads/train/an")
    start = refs(origin)[PR_BRANCH]

    def edit(repo: Path) -> None:
        _git(repo, "fetch", "-q", f"file://{origin}", "train/an:refs/remotes/origin/train/an")
        # The clone is shallow, so git cannot see train/an's root in it.
        _merge(repo, "origin/train/an", "--allow-unrelated-histories")
        _commit(repo, "the-lanes-fix", who=AGENT)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    assert out["pushed_head"] == tip
    # The agent's commits are kept one by one here (the replay), so the merge
    # is the commit under the lane's fix; a fold makes it the tip itself.
    merges = [line.split()[1:] for line in
              _git(origin, "rev-list", "--merges", "--parents", f"{start}..{tip}").splitlines()]
    # MUTATION: drop every merge parent but the default branch's (the code
    # before #3359) -> no merge at all, and train/an's head is no ancestor of
    # the pull request's branch on the remote.
    assert merges == [[start, train]], merges
    assert _is_ancestor(origin, train, tip)
    assert _is_ancestor(origin, start, tip)  # still a fast-forward
    assert {"train-1.txt", "train-2.txt", "the-lanes-fix.txt"} <= tree_at(origin, tip)
    author = _git(origin, "log", "-1", "--format=%ae%n%B", tip)
    assert "anthropic.com" not in author.lower()


# -- 3. a merged branch's own commits are scanned (#3359 review, #259) ----------

#: Built at run time: a value only the fake leak test below knows.
SECRET = "swarm-review-" + secrets.token_hex(12)


def _flags_secret(_path: str, text: str):
    at = text.find(SECRET)
    return None if at < 0 else lifecycle.CredentialHit("registered_secret", at)


def _history_leaks(tmp_path: Path, repo: Path):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return lambda commits: lifecycle._commits_leak(
        commits, leaks=_flags_secret, git=["git"], repo=repo,
        private_dir=tmp_path / "private", logs_dir=logs, timeout_seconds=60,
        logger=_logger(),
    )


def _side_branch_that_adds_and_removes_the_secret(repo: Path, start: str) -> str:
    """A person's branch `tmp` whose first commit adds the secret and whose
    second removes it: its final tree is clean, its history is not."""
    _git(repo, "checkout", "-q", "-b", "tmp", start)
    (repo / "config.txt").write_text(f"token={SECRET}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add the config")
    (repo / "config.txt").write_text("token=read from the environment\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "stop committing the value")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "work")
    return head


def test_a_merged_branch_whose_history_adds_a_secret_is_folded_without_it(tmp_path: Path):
    repo, base = _repo(tmp_path)
    clean = _branch(repo, "train", base, ["t1"])
    tmp = _side_branch_that_adds_and_removes_the_secret(repo, base)
    _merge(repo, "train")
    _merge(repo, "tmp")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")

    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    gitops.fold_agent_commits(
        repo=repo, base=base, keep=None, message="the worker's commit",
        author_name="Swarm Worker", author_email="worker@example.invalid",
        private_dir=tmp_path / "private", logs_dir=logs, timeout_seconds=60,
        logger=_logger(), history_leaks=_history_leaks(tmp_path, repo),
    )
    # MUTATION: drop the `history_leaks` read in `merged_head_kept` -> tmp is
    # a parent, and the commit holding the secret is pushed with it.
    assert _parents(repo) == [base, clean]
    assert not _is_ancestor(repo, tmp, "HEAD")
    # The branch's changes are still in the tree; only its history is not.
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree


def test_the_replay_drops_a_merged_branch_whose_history_adds_a_secret(tmp_path: Path):
    repo, base = _repo(tmp_path)
    clean = _branch(repo, "train", base, ["t1"])
    tmp = _side_branch_that_adds_and_removes_the_secret(repo, base)
    _merge(repo, "train")
    _merge(repo, "tmp")
    _commit(repo, "mine", who=AGENT)

    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    kept = lifecycle.replay_agent_commits(
        repo=repo, base=base, keep=None, task_id="task_1", scrub=lambda text: text,
        author_name="Swarm Worker", author_email="worker@example.invalid",
        private_dir=tmp_path / "private", logs_dir=logs, timeout_seconds=60,
        logger=_logger(), leaks=_flags_secret,
    )
    assert kept == 3
    merges = [line.split()[2:] for line in
              _git(repo, "rev-list", "--merges", "--parents", f"{base}..HEAD").splitlines()]
    # MUTATION: pass no `history_leaks` to `_published_merge_parents` -> the
    # second merge keeps tmp, and the secret's commit is pushed. Rewritten
    # without tmp, that merge has one parent, so it is no merge at all.
    assert merges == [[clean]], merges
    assert _is_ancestor(repo, clean, "HEAD")
    assert not _is_ancestor(repo, tmp, "HEAD")


def test_a_lane_that_merges_a_branch_with_a_secret_in_its_history_never_pushes_it(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """The issue's shape with the review's failure: the agent merges a side
    branch that added a credential and removed it. The pull request's branch
    takes the merge's tree, never that branch's commits."""
    _no_agent_text(monkeypatch)
    monkeypatch.setattr(lifecycle.Worker, "_leaks_in_added_text",
                        lambda self, path, text: _flags_secret(path, text))
    start = refs(origin)[PR_BRANCH]
    heads: list[str] = []

    def edit(repo: Path) -> None:
        _git(repo, "checkout", "-q", "-b", "work")
        heads.append(_side_branch_that_adds_and_removes_the_secret(repo, "HEAD"))
        _merge(repo, "tmp")
        _commit(repo, "the-lanes-fix", who=AGENT)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359-leak",
                            dispatch=DIRECT, edit=edit)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    # MUTATION: drop the `history_leaks` read -> tmp's head, and the commit
    # that added the secret, are on the remote.
    assert not _is_ancestor(origin, heads[0], tip)
    assert _is_ancestor(origin, start, tip)
    assert "config.txt" in tree_at(origin, tip)
    assert SECRET.encode() not in subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"], cwd=str(origin),
        check=True, capture_output=True,
    ).stdout, "the secret reached a pushed object"


# -- 4. a merged head the clean repository cannot read fails the publish ---------


def test_a_publish_whose_merged_head_cannot_be_read_pushes_nothing_and_keeps_the_bundle(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    """The brief's rule: never a silent squash. Nothing is pushed, the
    attempt's error says why, and the agent's branch is kept as the bundle."""
    _no_agent_text(monkeypatch)
    start = refs(origin)[PR_BRANCH]

    def unreadable(run, g, head, **_kwargs):
        raise gitops.MergedHeadUnreadable(
            f"the agent merged commit {head[:12]}, which the clean publish repository "
            "cannot read"
        )

    monkeypatch.setattr(lifecycle, "merged_head_kept", unreadable)
    monkeypatch.setattr(gitops, "merged_head_kept", unreadable)
    heads: list[str] = []

    def edit(repo: Path) -> None:
        _git(repo, "checkout", "-q", "-b", "work")
        heads.append(_branch(repo, "side", "HEAD", ["s1"]))
        _merge(repo, "side")
        _commit(repo, "the-lanes-fix", who=AGENT)
        heads.append(_git(repo, "rev-parse", "HEAD"))

    worker, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id="t-3359-read",
                                 dispatch=DIRECT, edit=edit)
    # MUTATION: drop the `except MergedHeadUnreadable` in the publish -> no
    # bundle is kept or named.
    assert out.get("published") is not True, out
    assert refs(origin)[PR_BRANCH] == start
    assert out.get("publish_failed") is True, out
    reason = str(out.get("publish_reason"))
    assert "cannot read" in reason and lifecycle.WORK_BUNDLE_NAME in reason, out
    assert out.get("bundle") == lifecycle.WORK_BUNDLE_NAME, out
    bundle = worker.ws.artifacts / lifecycle.WORK_BUNDLE_NAME
    assert bundle.is_file()
    listed = _git(origin, "bundle", "list-heads", str(bundle)).split()
    assert heads[-1] in listed, listed
