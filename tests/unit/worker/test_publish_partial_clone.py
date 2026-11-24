"""A step whose agent merged branches it fetched as a partial clone publishes (issue 367).

WHAT WENT WRONG (2026-11-24). The fix step of an `integrate` workflow fetched
the other contributors' branches with `--filter=blob:none` and merged them.
Its clone became a partial clone (`remote.origin.promisor=true`) whose
history reaches blobs it never downloaded. The clean repository's
object-checked fetch from the clone runs its upload side with lazy fetching
disabled -- rightly: a lazy fetch would run a program the agent's config
names -- so `pack-objects` died on the first missing object: "could not fetch
the agent's work into the worker's clean repository ... could not fetch <sha>
from promisor remote".

WHAT IS PINNED. When that first fetch fails, the clean repository reads the
commit graph through the same hardened upload-pack (commits only, no blob
read), fetches the heads the agent merged FROM ORIGIN with the worker's own
fetch (`fetch_on_origin`), points a ref at each one that landed so the
negotiation sends it as a `have`, stops the walk there and retries once with
no thin-pack bases. The merge keeps the contributor tip as its second parent
and nothing is missing in the clean repository. Without an origin fetch, or
when origin does not serve the merged head, the failure is today's. A
publish whose first fetch succeeds does neither the skeleton fetch nor any
origin fetch. A program the clone's config names never runs.

Local repositories; no network, no credentials. Each is a handful of commits.
"""
from __future__ import annotations

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

FIXED = "could not fetch the agent's work into the worker's clean repository"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="acme", generation=1,
                        runner_profile="mock", stream=io.StringIO())


def _write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)


@pytest.fixture
def local_urls(monkeypatch, tmp_path: Path) -> None:
    """A `file://` remote under tmp_path passes; every other URL is validated as usual."""
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(gitops, "validate_repository_url",
                        lambda url: url if url.startswith(allowed) else real(url))


class PartialMerge:
    """origin: main b0 (f1, f2, f3); `nominated` = b0 + a change to f1;
    `contrib` = b0 + two commits that each rewrite f2. The worker's clone is
    `--depth=1 --branch nominated`, its HEAD the recorded base. The agent
    then unshallows and fetches contrib with `--filter=blob:none`, merges
    contrib and commits once more: a partial clone missing two blobs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.origin)], check=True,
                       env=_ENV)
        _git(self.origin, "config", "uploadpack.allowFilter", "true")
        _git(self.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        for name in ("f1", "f2", "f3"):
            _write(self.origin, name, f"{name}\n")
        _git(self.origin, "add", "-A")
        _git(self.origin, "commit", "-q", "-m", "b0")
        _git(self.origin, "checkout", "-q", "-b", "contrib")
        for i in (1, 2):
            _write(self.origin, "f2", f"contrib {i}\n")
            _git(self.origin, "commit", "-q", "-am", f"contrib {i}")
        self.contrib = _git(self.origin, "rev-parse", "HEAD")
        _git(self.origin, "checkout", "-q", "-b", "nominated", "main")
        _write(self.origin, "f1", "nominated\n")
        _git(self.origin, "commit", "-q", "-am", "nominated")
        self.base = _git(self.origin, "rev-parse", "HEAD")
        self.url = f"file://{self.origin}"
        self.clone = tmp_path / "work" / "repo"
        subprocess.run(["git", "clone", "-q", "--depth=1", "--branch", "nominated", self.url,
                        str(self.clone)], check=True, capture_output=True, env=_ENV)
        _git(self.clone, "fetch", "-q", "--filter=blob:none", "--unshallow", "origin",
             "nominated")
        _git(self.clone, "fetch", "-q", "--filter=blob:none", "origin",
             "contrib:refs/remotes/origin/contrib")
        _git(self.clone, "merge", "-q", "--no-edit", "origin/contrib")
        self.merge = _git(self.clone, "rev-parse", "HEAD")
        _write(self.clone, "f3", "the agent's\n")
        _git(self.clone, "commit", "-q", "-am", "the agent's work")
        self.head = _git(self.clone, "rev-parse", "HEAD")
        # The control: a partial clone whose HEAD reaches objects only origin has.
        assert _git(self.clone, "config", "remote.origin.promisor") == "true"
        self.missing = [line[1:] for line in _git(
            self.clone, "rev-list", "--objects", "--missing=print", "HEAD").splitlines() if line.startswith("?")]
        assert self.missing, "the clone was meant to lack an object only origin has"
        self.logs = tmp_path / "logs"
        self.logs.mkdir(exist_ok=True)

    def prepare(self, **kwargs) -> Path:
        return prepare_publish_repo(
            source_repo=self.clone, work_head=None, base=self.base,
            private_dir=self.tmp / "private", logs_dir=self.logs, timeout_seconds=60,
            logger=_logger(), **kwargs)

    def fetcher(self, asked: list[list[str]], url: str | None = None):
        """The worker's origin fetch, as lifecycle wires it, against file://."""
        def fetch(repo: Path, shas: list[str]) -> list[str]:
            asked.append(list(shas))
            return gitops.fetch_shallow_lines_from_origin(
                repo=repo, url=url or self.url, token=None, shas=shas,
                private_dir=self.tmp / "private", logs_dir=self.logs,
                timeout_seconds=60, logger=_logger())
        return fetch


def test_the_publish_fetch_of_a_partial_clone_merge_succeeds_from_origin(
    tmp_path: Path, local_urls,
):
    pr = PartialMerge(tmp_path)
    asked: list[list[str]] = []
    repo = pr.prepare(fetch_on_origin=pr.fetcher(asked))
    assert _git(repo, "rev-parse", "HEAD") == pr.head
    # Nothing the history needs is missing in the clean repository.
    assert not [line for line in _git(repo, "rev-list", "--objects", "--missing=print",
                                      "HEAD").splitlines() if line.startswith("?")]
    # The merge keeps the contributor tip as its second parent.
    assert _git(repo, "rev-list", "--parents", "-n", "1", pr.merge).split()[1:] \
        == [pr.base, pr.contrib]
    # Only the merged head was put to origin, and it joined the boundary.
    assert [pr.contrib] in asked
    boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
    assert boundary == [pr.base, pr.contrib]
    # The agent's own commits are in the clean repository, with their trees.
    assert _git(repo, "show", f"{pr.head}:f3") == "the agent's"


def test_without_an_origin_fetch_the_failure_is_todays(tmp_path: Path):
    pr = PartialMerge(tmp_path)
    with pytest.raises(GitError) as raised:
        pr.prepare()
    assert str(raised.value).startswith(FIXED)


def test_a_merged_head_origin_does_not_serve_adds_nothing(tmp_path: Path, local_urls):
    """origin is asked, but answers nothing (here: a fetcher pointed at an
    origin without the contributor branch): the boundary stays the base and
    the failure is today's."""
    pr = PartialMerge(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "--bare", "--single-branch", "--branch", "nominated",
                    pr.url, str(other)], check=True, capture_output=True, env=_ENV)
    asked: list[list[str]] = []
    with pytest.raises(GitError) as raised:
        pr.prepare(fetch_on_origin=pr.fetcher(asked, url=f"file://{other}"))
    assert str(raised.value).startswith(FIXED)
    assert [pr.contrib] in asked


def test_an_answer_naming_a_sha_nobody_asked_about_adds_nothing(tmp_path: Path):
    pr = PartialMerge(tmp_path)
    with pytest.raises(GitError) as raised:
        pr.prepare(fetch_on_origin=lambda repo, shas: ["0" * 40])
    assert str(raised.value).startswith(FIXED)


def test_a_fetcher_that_raises_adds_nothing(tmp_path: Path):
    pr = PartialMerge(tmp_path)

    def boom(repo: Path, shas: list[str]) -> list[str]:
        raise RuntimeError("the forge is down")

    with pytest.raises(GitError) as raised:
        pr.prepare(fetch_on_origin=boom)
    assert str(raised.value).startswith(FIXED)


def test_a_program_the_clones_config_names_never_runs(tmp_path: Path, local_urls):
    """The agent plants an upload-pack program, an ssh command and a
    credential helper in its clone's config; the completed publish runs none."""
    pr = PartialMerge(tmp_path)
    marker = tmp_path / "planted-ran"
    planted = tmp_path / "planted.sh"
    planted.write_text(f"#!/bin/sh\ntouch {marker}\nexec git-upload-pack \"$@\"\n")
    planted.chmod(0o755)
    for key in ("remote.origin.uploadpack", "core.sshCommand", "credential.helper",
                "core.alternateRefsCommand", "core.fsmonitor"):
        _git(pr.clone, "config", key, str(planted))
    _git(pr.clone, "config", "uploadpack.packObjectsHook", str(planted))
    hooks = pr.clone / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("pre-upload-pack", "post-checkout", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / hook).chmod(0o755)
    asked: list[list[str]] = []
    repo = pr.prepare(fetch_on_origin=pr.fetcher(asked))
    assert _git(repo, "rev-parse", "HEAD") == pr.head
    assert not marker.exists()


def test_a_first_fetch_that_succeeds_makes_no_skeleton_and_no_origin_fetch(
    tmp_path: Path, local_urls,
):
    """A full clone whose agent merged contrib: the first publish fetch
    succeeds, so neither completion step runs."""
    pr = PartialMerge(tmp_path)
    full = tmp_path / "work" / "full"
    subprocess.run(["git", "clone", "-q", "--branch", "nominated", pr.url, str(full)],
                   check=True, capture_output=True, env=_ENV)
    _git(full, "merge", "-q", "--no-edit", "origin/contrib")
    head = _git(full, "rev-parse", "HEAD")
    asked: list[list[str]] = []
    repo = prepare_publish_repo(
        source_repo=full, work_head=None, base=pr.base, private_dir=tmp_path / "private",
        logs_dir=pr.logs, timeout_seconds=60, logger=_logger(),
        fetch_on_origin=pr.fetcher(asked))
    assert _git(repo, "rev-parse", "HEAD") == head
    assert asked == []
    assert not list(pr.logs.glob("git-publish-skeleton*"))
    assert not list(pr.logs.glob("git-publish-fetch-completed*"))
    assert not list((tmp_path / "private").glob("skeleton-*"))


def test_the_completion_makes_no_lazy_fetch_from_the_clone(
    tmp_path: Path, local_urls, monkeypatch,
):
    """The commits-only fetch makes its scratch repository a partial clone
    whose promisor is the agent's clone; git 2.39.5 lazily fetched from it
    mid-fetch, running a plain `git-upload-pack` in the clone without the
    hardened environment. Every git the publish runs is traced: no lazy
    fetch, and every upload-pack in the clone is the hardened one."""
    pr = PartialMerge(tmp_path)
    trace = tmp_path / "git-trace.log"
    real = gitops._git_env

    def traced(private_dir: Path) -> dict[str, str]:
        env = real(private_dir)
        env["GIT_TRACE"] = str(trace)
        return env

    monkeypatch.setattr(gitops, "_git_env", traced)
    repo = pr.prepare(fetch_on_origin=pr.fetcher([]))
    assert _git(repo, "rev-parse", "HEAD") == pr.head
    text = trace.read_text()
    assert "fetch.negotiationAlgorithm=noop" not in text
    spawned = [line for line in text.splitlines()
               if "run_command:" in line and "upload-pack" in line and str(pr.clone) in line]
    assert spawned
    assert all("GIT_NO_LAZY_FETCH=1" in line for line in spawned)
