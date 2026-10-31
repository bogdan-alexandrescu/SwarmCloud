"""Every worker fetch that can run back to back over one clone starts no
background git maintenance (#2922).

git 2.56 (CI's runner) ends every `git fetch` with a detached `git maintenance
run --auto`, whose repack can hold `.git/shallow.lock` or rewrite
`.git/shallow` while the next git command over the same clone runs ("shallow
file has changed since we read it"). `fetch_pull_base` and `deepen_history`
already run their fetches with `-c maintenance.auto=false -c gc.auto=0`
(`test_publish_to_pr_pull_base.py`, `test_deepen_history_no_maintenance.py`);
pinned here, with real git against a `file://` forge, are the rest:

* `shallow_clone`'s sha path (`fetch --depth 1 origin <sha>`), which
  `deepen_history` and `fetch_pull_base` follow;
* `clone_at_commit`'s fetch by sha and its fetch by branch, which
  `fetch_pull_base` follows;
* `fetch_base_history`, which an agent may run again over the same checkout;
* the publish fetch with `--update-shallow` (`prepare_publish_repo`);
* `merge_branches`' per-branch fetch and `fetch_branch_tip`'s carrier fetch.

Each test traces only the fetches (`GIT_TRACE` set on the argv whose command
is `fetch`), so a checkout or a merge that starts its own maintenance is not
what is measured; each also checks the argv itself, for a git too old to
trace a detached maintenance.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops
from agent_worker.logs import build_logger

#: A git that starts automatic maintenance after a fetch, as `GIT_TRACE`
#: writes it: `maintenance run --auto` (git 2.29 on) or `gc --auto` before.
_AUTO_MAINTENANCE = re.compile(r"run_command: git (maintenance run|gc) --auto")


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="tenant-a",
                        generation=1, runner_profile="mock", stream=io.StringIO())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                          "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / f"{name}.txt").write_text(f"{name}\n")
    _git(work, "add", f"{name}.txt")
    _git(work, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet",
         "-m", name)
    return _git(work, "rev-parse", "HEAD")


class _Forge:
    """A bare `file://` forge: `main` with two commits, `swarm/x` one past it."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = tmp_path / "seed-work"
        work.mkdir()
        _git(work, "init", "--quiet", "--initial-branch=main")
        self.first = _commit(work, "one")
        self.main = _commit(work, "two")
        _git(work, "checkout", "--quiet", "-b", "swarm/x")
        self.branch = _commit(work, "three")
        self.work = work
        bare = tmp_path / "forge.git"
        _git(tmp_path, "clone", "--quiet", "--bare", str(work), str(bare))
        self.url = f"file://{bare}"
        real = gitops.validate_repository_url
        allowed = f"file://{tmp_path}"
        monkeypatch.setattr(gitops, "validate_repository_url",
                            lambda url: url if url.startswith(allowed) else real(url))


class _FetchTrace:
    """`GIT_TRACE` on every fetch the worker starts, and their argv."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = tmp_path / "git-trace.log"
        self.fetches: list[list[str]] = []
        run_child = gitops.run_child

        def traced(argv, **kwargs):
            if "fetch" in argv:
                self.fetches.append(list(argv))
                kwargs["env"] = {**kwargs["env"], "GIT_TRACE": str(self.path)}
            return run_child(argv, **kwargs)

        monkeypatch.setattr(gitops, "run_child", traced)

    def assert_no_maintenance(self, fetches: int = 1) -> None:
        assert len(self.fetches) >= fetches, self.fetches
        for argv in self.fetches:
            settings = argv[:argv.index("fetch")]
            assert "maintenance.auto=false" in settings and "gc.auto=0" in settings, argv
        started = self.path.read_text()
        assert started.count("built-in: git fetch") >= fetches  # the trace saw the fetches
        assert not _AUTO_MAINTENANCE.findall(started)


def test_a_clone_by_sha_fetches_with_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`shallow_clone`'s sha path: its maintenance could overlap the first
    fetch of `deepen_history` or `fetch_pull_base`. MUTATION: drop
    `_NO_AUTO_MAINTENANCE` from that fetch and the trace shows `git
    maintenance run --auto`."""
    forge = _Forge(tmp_path, monkeypatch)
    trace = _FetchTrace(tmp_path, monkeypatch)
    (tmp_path / "logs").mkdir()
    clone = gitops.shallow_clone(
        url=forge.url, ref=forge.main, destination=tmp_path / "repo",
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_logger(),
    )
    assert _git(clone.path, "rev-parse", "HEAD") == forge.main
    trace.assert_no_maintenance()
    (argv,) = trace.fetches
    assert argv[argv.index("fetch"):] == ["fetch", "--depth", "1", "--no-tags", "origin",
                                          forge.main]


def test_a_pinned_clone_fetches_with_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`clone_at_commit`'s fetch by sha, which `fetch_pull_base` follows."""
    forge = _Forge(tmp_path, monkeypatch)
    trace = _FetchTrace(tmp_path, monkeypatch)
    (tmp_path / "logs").mkdir()
    clone = gitops.clone_at_commit(
        url=forge.url, branch="main", commit=forge.first, destination=tmp_path / "repo",
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_logger(),
    )
    assert _git(clone.path, "rev-parse", "HEAD") == forge.first
    trace.assert_no_maintenance()
    (argv,) = trace.fetches
    assert argv[argv.index("fetch"):] == ["fetch", "--depth", "1", "--no-tags", "origin",
                                          forge.first]


def test_a_pinned_clone_by_branch_fetches_with_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`clone_at_commit`'s fallback when the forge refuses a fetch by sha."""
    forge = _Forge(tmp_path, monkeypatch)
    trace = _FetchTrace(tmp_path, monkeypatch)
    traced = gitops.run_child

    def refused(argv, **kwargs):
        # The forge refuses the sha: asked for one it does not hold instead.
        if "fetch" in argv and argv[-1] == forge.first:
            argv = [*argv[:-1], "f" * 40]
        return traced(argv, **kwargs)

    monkeypatch.setattr(gitops, "run_child", refused)
    (tmp_path / "logs").mkdir()
    clone = gitops.clone_at_commit(
        url=forge.url, branch="main", commit=forge.first, destination=tmp_path / "repo",
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_logger(),
    )
    assert _git(clone.path, "rev-parse", "HEAD") == forge.first
    by_branch = [argv for argv in trace.fetches if "refs/heads/main" in argv]
    assert by_branch, trace.fetches                  # the fallback ran
    trace.assert_no_maintenance(fetches=2)
    assert by_branch[0][by_branch[0].index("fetch"):] == [
        "fetch", "--no-tags", "origin", "refs/heads/main"]


def test_the_base_history_fetch_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch_base_history` carried `gc.auto=0` alone, which since git 2.29
    gates only maintenance's gc task, not `maintenance run --auto` itself."""
    forge = _Forge(tmp_path, monkeypatch)
    (tmp_path / "logs").mkdir()
    clone = gitops.shallow_clone(
        url=forge.url, ref="main", destination=tmp_path / "repo",
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_logger(),
    )
    trace = _FetchTrace(tmp_path, monkeypatch)
    reason = gitops.fetch_base_history(clone.path, url=forge.url, base_sha=forge.main,
                                       private_dir=tmp_path / "private")
    assert reason is None
    assert _git(clone.path, "rev-list", "--count", "HEAD") == "2"
    trace.assert_no_maintenance()
    (argv,) = trace.fetches
    assert argv[argv.index("fetch"):] == [
        "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
        f"--depth={gitops.ANCESTRY_FETCH_DEPTH}", "--", forge.url, forge.main]


def test_the_publish_fetch_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `--update-shallow` fetch into the clean publish repository, which
    the fold, the scans and the integrator's merges then run over."""
    forge = _Forge(tmp_path, monkeypatch)
    trace = _FetchTrace(tmp_path, monkeypatch)
    (tmp_path / "logs").mkdir()
    publish_dir = gitops.prepare_publish_repo(
        source_repo=forge.work, work_head=forge.branch, base=forge.main,
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_logger(),
    )
    assert _git(publish_dir, "rev-parse", "refs/swarm/head") == forge.branch
    trace.assert_no_maintenance()
    (argv,) = trace.fetches
    assert "--update-shallow" in argv and "--no-recurse-submodules" in argv


def test_the_integrator_fetch_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`merge_branches` fetches branch after branch into one publish repository,
    merging each between them."""
    forge = _Forge(tmp_path, monkeypatch)
    repo = tmp_path / "publish"
    _git(tmp_path, "clone", "--quiet", "--branch", "main", forge.url, str(repo))
    trace = _FetchTrace(tmp_path, monkeypatch)
    outcome = gitops.merge_branches(
        repo=repo, url=forge.url, branches=["swarm/x"], token="", private_dir=tmp_path / "private",
        logs_dir=tmp_path, timeout_seconds=60, logger=_logger(), author_name="swarm",
        author_email="swarm@example.invalid",
    )
    assert outcome.merged == ("swarm/x",) or list(outcome.merged) == ["swarm/x"]
    trace.assert_no_maintenance()
    (argv,) = trace.fetches
    assert argv[argv.index("fetch"):] == ["fetch", "--no-tags", "--depth=2147483647", "--",
                                          forge.url, "refs/heads/swarm/x"]


def test_the_carrier_fetch_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch_branch_tip`, the carrier fetch into the publish repository."""
    forge = _Forge(tmp_path, monkeypatch)
    repo = tmp_path / "publish"
    _git(tmp_path, "clone", "--quiet", "--branch", "main", forge.url, str(repo))
    trace = _FetchTrace(tmp_path, monkeypatch)
    tip = gitops.fetch_branch_tip(
        repo=repo, url=forge.url, branch="swarm/x", token="", private_dir=tmp_path / "private",
        logs_dir=tmp_path, timeout_seconds=60, logger=_logger(),
    )
    assert tip == forge.branch
    trace.assert_no_maintenance()
    (argv,) = trace.fetches
    assert argv[argv.index("fetch"):] == ["fetch", "--no-tags", "--depth=2147483647", "--",
                                          forge.url, "refs/heads/swarm/x"]
