"""`merge_branches`' merge starts no background git maintenance (#2922).

git 2.56 (CI's runner) ends a `git merge` that commits with a detached `git
maintenance run --auto`, as it ends a fetch. The integrator merges contributor
after contributor into one publish repository, so that repack can still be
running when the next contributor's fetch (`--depth=2147483647`, which reads
and rewrites `.git/shallow`) starts over the same repository: the race the
fetches were closed against in `test_fetches_no_maintenance.py`. The merge
carries `-c maintenance.auto=false -c gc.auto=0` for the same reason.

Traced with real git against a `file://` forge, on the merges only (`GIT_TRACE`
set on the argv whose command is `merge`), with two contributors so a fetch
follows a merge; the argv is checked too, for a git too old to trace a
detached maintenance.
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

#: A git that starts automatic maintenance after a command, as `GIT_TRACE`
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


def test_the_integrator_merge_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop `_NO_AUTO_MAINTENANCE` from `merge_branches`' merge and
    the trace shows `git maintenance run --auto` after each merge commit."""
    work = tmp_path / "seed-work"
    work.mkdir()
    _git(work, "init", "--quiet", "--initial-branch=main")
    _commit(work, "one")
    _git(work, "checkout", "--quiet", "-b", "swarm/x")
    _commit(work, "two")
    _git(work, "checkout", "--quiet", "main")
    _git(work, "checkout", "--quiet", "-b", "swarm/y")
    _commit(work, "three")
    bare = tmp_path / "forge.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(work), str(bare))
    url = f"file://{bare}"
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(gitops, "validate_repository_url",
                        lambda u: u if u.startswith(allowed) else real(u))
    repo = tmp_path / "publish"
    _git(tmp_path, "clone", "--quiet", "--branch", "main", url, str(repo))

    trace = tmp_path / "git-trace.log"
    merges: list[list[str]] = []
    run_child = gitops.run_child

    def traced(argv, **kwargs):
        if "merge" in argv:
            merges.append(list(argv))
            kwargs["env"] = {**kwargs["env"], "GIT_TRACE": str(trace)}
        return run_child(argv, **kwargs)

    monkeypatch.setattr(gitops, "run_child", traced)
    outcome = gitops.merge_branches(
        repo=repo, url=url, branches=["swarm/x", "swarm/y"], token="",
        private_dir=tmp_path / "private", logs_dir=tmp_path, timeout_seconds=60,
        logger=_logger(), author_name="swarm", author_email="swarm@example.invalid",
    )
    assert list(outcome.merged) == ["swarm/x", "swarm/y"]
    assert len(merges) == 2, merges
    started = trace.read_text()
    assert started.count("built-in: git merge") >= 2  # the trace saw both merges
    assert not _AUTO_MAINTENANCE.findall(started)
    for argv, branch in zip(merges, ["swarm/x", "swarm/y"]):
        settings = argv[:argv.index("merge")]
        assert "maintenance.auto=false" in settings and "gc.auto=0" in settings, argv
        assert argv[argv.index("merge"):] == ["merge", "--no-ff", "--no-edit", "-m",
                                              f"swarm: integrate {branch}", "FETCH_HEAD"]
