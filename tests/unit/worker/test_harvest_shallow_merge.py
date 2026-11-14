"""The worker's harvest of a two-parent merge in a depth-1 clone (#2901, the publish end).

WHAT WENT WRONG (a peer project, 2026-11-13). An `integrate` contributor with
`publish_to_pr` merged the pull request's base into its branch, and the
publish then failed: "could not fetch the agent's work into the worker's clean
repository ... Could not read <commit>". The clone is `--depth 1` at the pull
request's head H, the floor the worker recorded. The base's tip T came in
shallow -- a depth-1 tip, or a slice cut off above H's history -- so T's side
of the merge is cut at a commit that is NO ANCESTOR of H. The clean
repository's boundary held H, the lines proven under it and the lines a relay
confirmed; with no relay to ask, the walk from the merge crossed T's cut-off
into parents the clone never had.

WHAT IS PINNED: before the harvest fetch, the clean repository fetches each
cut-off nobody answered from the forge itself, at depth 1 (`fetch_on_origin`,
`gitops.fetch_shallow_lines_from_origin`). One that lands is origin's commit,
so the walk may stop there, and the merge publishes with both parents. A sha
origin lacks, a malformed line, an answer naming shas nobody asked about and
a fetcher that raises add nothing.

Local `file://` repositories; no network, no credentials.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops
from agent_worker.gitops import GitError, prepare_publish_repo
from agent_worker.logs import build_logger

_ENV = {k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG"))}
_ENV.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="acme", generation=1,
                        runner_profile="mock", stream=io.StringIO())


@pytest.fixture
def local_urls(monkeypatch, tmp_path: Path) -> None:
    """A `file://` remote under tmp_path passes; every other URL is validated as usual."""
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(gitops, "validate_repository_url",
                        lambda url: url if url.startswith(allowed) else real(url))


class MergedBase:
    """origin: `main` of 8 commits; `feature` = main~4 plus H. The clone is
    `--depth 1` of feature; the agent fetches main's tip T at depth 1 (what
    the platform's depth-1 fetch lands) and merges it into H."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.origin)], check=True,
                       env=_ENV)
        for i in range(8):
            (self.origin / "log.txt").write_text(f"line {i}\n")
            _git(self.origin, "add", "-A")
            _git(self.origin, "commit", "-q", "-m", f"main {i}")
        self.tip = _git(self.origin, "rev-parse", "main")
        _git(self.origin, "checkout", "-q", "-b", "feature", "main~4")
        (self.origin / "feature.txt").write_text("mine\n")
        _git(self.origin, "add", "-A")
        _git(self.origin, "commit", "-q", "-m", "the pull request")
        self.head = _git(self.origin, "rev-parse", "HEAD")
        self.url = f"file://{self.origin}"
        self.clone = tmp_path / "work" / "repo"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "feature", self.url,
                        str(self.clone)], check=True, capture_output=True, env=_ENV)
        _git(self.clone, "fetch", "-q", "--depth=1", "origin", "main:refs/remotes/origin/main")
        _git(self.clone, "merge", "-q", "--no-ff", "--allow-unrelated-histories", "-X", "theirs",
             "-m", "merge main", "origin/main")
        self.merge = _git(self.clone, "rev-parse", "HEAD")
        # The control: T's side is cut at T itself, which is no ancestor of H.
        assert sorted((self.clone / ".git" / "shallow").read_text().split()) \
            == sorted([self.head, self.tip])

    def prepare(self, **kwargs) -> Path:
        logs = self.tmp / "logs"
        logs.mkdir(exist_ok=True)
        return prepare_publish_repo(
            source_repo=self.clone, work_head=None, base=self.head,
            private_dir=self.tmp / "private", logs_dir=logs, timeout_seconds=60,
            logger=_logger(), **kwargs)

    def fetcher(self, asked: list[list[str]]):
        """The worker's origin fetch, as lifecycle wires it, against file://."""
        def fetch(repo: Path, shas: list[str]) -> list[str]:
            asked.append(list(shas))
            return gitops.fetch_shallow_lines_from_origin(
                repo=repo, url=self.url, token=None, shas=shas,
                private_dir=self.tmp / "private", logs_dir=self.tmp / "logs",
                timeout_seconds=60, logger=_logger())
        return fetch


def test_the_harvest_of_a_two_parent_merge_fails_with_no_relay_and_no_origin_fetch(
    tmp_path: Path,
):
    """The reported failure, kept as the control."""
    pr = MergedBase(tmp_path)
    with pytest.raises(GitError) as raised:
        pr.prepare()
    assert str(raised.value).startswith("could not fetch the agent's work into the worker's "
                                        "clean repository")


def test_the_harvest_of_a_two_parent_merge_succeeds_with_a_depth_1_clone(
    tmp_path: Path, local_urls,
):
    pr = MergedBase(tmp_path)
    asked: list[list[str]] = []
    repo = pr.prepare(fetch_on_origin=pr.fetcher(asked))
    # Only the cut-off nothing else answered: never the floor the worker recorded.
    assert asked == [[pr.tip]]
    assert _git(repo, "rev-parse", "HEAD") == pr.merge
    assert _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] \
        == [pr.head, pr.tip]
    boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
    assert boundary == [pr.head, pr.tip]
    # The relay answers first; the forge is asked only about what is left.
    asked.clear()
    pr.prepare(confirm_on_origin=lambda shas: list(shas), fetch_on_origin=pr.fetcher(asked))
    assert asked == []


def test_an_origin_fetch_that_lands_nothing_adds_nothing(tmp_path: Path, local_urls):
    pr = MergedBase(tmp_path)
    stranger = hashlib.sha1(os.urandom(8)).hexdigest()

    def lies(repo: Path, shas: list[str]) -> list[str]:
        return [stranger]                       # a sha nobody asked about

    def raises(repo: Path, shas: list[str]) -> list[str]:
        raise RuntimeError("the forge did not answer")

    for fetcher in (lies, raises):
        with pytest.raises(GitError):
            pr.prepare(fetch_on_origin=fetcher)


def test_fetch_shallow_lines_from_origin_keeps_only_commits_origin_has(
    tmp_path: Path, local_urls,
):
    pr = MergedBase(tmp_path)
    repo = tmp_path / "clean"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_ENV)
    (tmp_path / "logs").mkdir(exist_ok=True)
    stranger = hashlib.sha1(os.urandom(8)).hexdigest()
    tree = _git(pr.origin, "rev-parse", "main^{tree}")
    landed = gitops.fetch_shallow_lines_from_origin(
        repo=repo, url=pr.url, token=None,
        shas=[stranger, pr.tip[:12], "--upload-pack=x", tree, pr.tip, pr.tip],
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_logger())
    assert landed == [pr.tip]
    # Depth 1: the snapshot, never the history.
    assert _git(repo, "rev-list", "--count", pr.tip) == "1"
    assert not (tmp_path / "private" / ".git-credentials").exists()
