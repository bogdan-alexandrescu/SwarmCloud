"""After a merge of main, the gate selects by the diff the pull request shows (#1694).

A fix or conflict-fix step clones its branch at the pre-merge head
(`SWARM_CLONE_BASE`), then merges origin/main in. The gate diffed from that
clone base, so every file main had changed since the branch left it counted
as the lane's: on PR 1696 59 files of main's selected 154 test files, and the
guard script hit its limit in part 3 with no test failing. Now, when no
pre-publish rebase moved the base and HEAD holds commits of the default branch
the clone base lacks, the base is `git merge-base <default branch> HEAD`.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish
from agent_worker.logs import build_logger

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}
MAIN_FILES = [f"apps/other/m{i}.py" for i in range(5)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


def _log() -> Any:
    return build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                        runner_profile="claude-code", stream=io.StringIO())


def _commit(repo: Path, files: list[str], message: str) -> None:
    for name in files:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(f"{message}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    """main: base; the lane branch: own.py, pushed (its head is the clone base);
    then main advances with five other files, origin/main tracks it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, ["README.md"], "base")
    _git(repo, "checkout", "-q", "-b", "lane")
    _commit(repo, ["apps/w/own.py"], "the lane's own change")
    clone_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, MAIN_FILES, "main moves on")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-q", "lane")
    return {"repo": repo, "clone_base": clone_base, "tmp": tmp_path}


def _selected(lane: dict[str, Any], monkeypatch) -> tuple[list[str], dict[str, Any]]:
    seen: list[list[str]] = []

    def select(repo, files, **kwargs):
        seen.append(list(files))
        return []

    monkeypatch.setattr(prepublish, "select_checks", select)
    artifacts = lane["tmp"] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(lane["tmp"]),
           **{k: v for k, v in _ENV.items() if k.startswith("GIT_")},
           prepublish.CLONE_BASE_ENV: lane["clone_base"]}
    gate = prepublish.Gate(repo=lane["repo"], env=env, secrets=(), resume=lambda t: True,
                           remaining=lambda: 3000.0, log=_log(), artifacts_dir=artifacts)
    record = gate.run()
    assert seen, "the gate selected no checks"
    return seen[0], record


def test_a_merge_of_main_selects_only_the_branchs_own_files(lane, monkeypatch):
    repo = lane["repo"]
    _git(repo, "merge", "-q", "--no-edit", "origin/main")
    _commit(repo, ["apps/w/fix.py"], "the fix step's change")
    files, record = _selected(lane, monkeypatch)
    # MUTATION: diffing from the pre-merge clone base lists main's five files too.
    assert files == ["apps/w/fix.py", "apps/w/own.py"], files
    merge_base = _git(repo, "merge-base", "origin/main", "HEAD")
    assert record["base"] == merge_base


def test_without_a_merge_the_clone_base_stays(lane, monkeypatch):
    _commit(lane["repo"], ["apps/w/fix.py"], "the fix step's change")
    files, record = _selected(lane, monkeypatch)
    assert files == ["apps/w/fix.py"], files
    assert record["base"] == lane["clone_base"]


def test_no_default_branch_ref_keeps_the_clone_base(lane, monkeypatch):
    repo = lane["repo"]
    _git(repo, "merge", "-q", "--no-edit", "origin/main")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    files, record = _selected(lane, monkeypatch)
    assert record["base"] == lane["clone_base"]
    assert sorted(files) == MAIN_FILES
