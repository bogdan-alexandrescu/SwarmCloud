"""The gate runs what the merge train ejects for (owner's improvement plan, 2026-11-23).

A lane's pull request runs no CI before the merge train, so the pre-publish
gate is the only check before it. Two of the train's ejection causes had no
check in the gate:

* the deployment-values guard on the lane's added lines
  (`scripts/guards/deployment-values.sh --diff <base> --mode enforce`). The
  train runs the BASE commit's copy of scripts/guards/ -- its
  placeholders.json allowlist included -- over the change as data, so the
  gate does the same: a public host added to the allowlist in the same pull
  request as the file naming it is still a finding, and the fix turn is told
  that the host has to land on main first in a pull request of its own. Exit
  3 (no pattern list here: the name layer did not run) is the shape layer
  passing, the names left to CI;
* the publish credential scan (`scripts/publish-scan.sh`), which otherwise
  fails the attempt at the worker's final leak scan, after the fix turn.

The deployment-values tests run the REAL guard in a scratch repository.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish

REPO = Path(__file__).resolve().parents[3]
_IDENT = {"GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
          "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}
#: A public host no allowlist holds, built at run time so that this file's own
#: added lines carry no URL for the guard to find.
HOST = ".".join(["widgets", "sample-vendor", "io"])
URL = "https" + "://" + HOST + "/docs"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git runs the guard's diff")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_IDENT}).stdout.strip()


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "DEPLOYMENT_PATTERNS")}
    return {**env, **_IDENT, "HOME": str(tmp_path), "SWARM_ENV_FILE": str(tmp_path / "no.env"),
            "TMPDIR": str(tmp_path)}


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copytree(REPO / "scripts" / "guards", repo / "scripts" / "guards")
    shutil.copy(REPO / "scripts" / "lib" / "common.sh", repo / "scripts" / "lib" / "common.sh")
    (repo / "README.md").write_text("a repository\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return {"repo": repo, "base": _git(repo, "rev-parse", "HEAD"), "env": _env(tmp_path)}


def _check(lane: dict[str, Any], name: str, files: list[str]) -> prepublish.Check:
    checks = prepublish.select_checks(lane["repo"], files, base=lane["base"], env=lane["env"])
    (check,) = [c for c in checks if c.name == name]
    return check


def _deployment_values(lane: dict[str, Any], files: list[str]) -> tuple[str, str]:
    check = _check(lane, prepublish.DEPLOYMENT_VALUES_CHECK, files)
    assert check.run is not None, check.skipped
    return check.run(prepublish.run_command, lane["repo"], lane["env"], 120.0)


def _add(lane: dict[str, Any], path: str, text: str, *, commit: bool = True) -> None:
    target = lane["repo"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if commit:
        _git(lane["repo"], "add", "-A")
        _git(lane["repo"], "commit", "-q", "-m", f"add {path}")


# ---------------------------------------------------------------------------
# The check set
# ---------------------------------------------------------------------------


def test_both_checks_are_chosen_for_any_change_and_run_before_the_guard_script(tmp_path):
    repo = tmp_path / "repo"
    for script in (prepublish.GUARDS_SCRIPT, prepublish.DEPLOYMENT_VALUES_SCRIPT,
                   prepublish.PUBLISH_SCAN_SCRIPT):
        (repo / script).parent.mkdir(parents=True, exist_ok=True)
        (repo / script).write_text("")
    names = [c.name for c in prepublish.select_checks(repo, ["docs/a.md"], base="b", env={})]
    # MUTATION: dropping either check leaves its train ejection to the train.
    # They take about a second each: ahead of the guard script, its budget
    # never skips them.
    assert names == [prepublish.DEPLOYMENT_VALUES_CHECK, prepublish.PUBLISH_SCAN_CHECK,
                     prepublish.GUARDS_CHECK]
    assert prepublish.select_checks(repo, [], base="b", env={}) == []


# ---------------------------------------------------------------------------
# The deployment-values guard, from the base commit's copy
# ---------------------------------------------------------------------------


@needs_git
def test_a_clean_change_passes_the_shape_layer_and_leaves_the_names_to_ci(lane):
    _add(lane, "docs/new.md", "a note with nothing in it\n")
    status, output = _deployment_values(lane, ["docs/new.md"])
    # MUTATION: reading the guard's exit 3 (no pattern list) as a failure
    # fails every lane, since no worker holds the list.
    assert status == prepublish.PASSED, output
    assert "left to CI" in output


@needs_git
def test_a_public_host_is_a_finding_the_fix_turn_is_told_to_land_on_main_first(lane):
    _add(lane, "docs/new.md", f"read {URL} for more\n")
    status, output = _deployment_values(lane, ["docs/new.md"])
    assert status == prepublish.FAILED
    assert "docs/new.md:1" in output and "url-host" in output
    assert HOST not in output  # the guard never prints the value
    # MUTATION: leaving out the url-host advice sends the agent to add the
    # host to placeholders.json here, which the train ignores.
    assert "land on main first" in output and "placeholders.json" in output


@needs_git
def test_adding_the_host_to_the_allowlist_in_the_same_change_does_not_help(lane):
    allow = lane["repo"] / "scripts" / "guards" / "placeholders.json"
    data = json.loads(allow.read_text())
    data["allowlist"]["public_hosts"].append(HOST)
    allow.write_text(json.dumps(data, indent=2) + "\n")
    _add(lane, "docs/new.md", f"read {URL} for more\n")
    status, output = _deployment_values(lane, ["docs/new.md",
                                               "scripts/guards/placeholders.json"])
    # MUTATION: running the tree's own guard reads the lane's allowlist and
    # passes what the train ejects.
    assert status == prepublish.FAILED, output
    assert "land on main first" in output


@needs_git
def test_lines_not_yet_committed_are_scanned_and_leave_no_worktree(lane):
    _add(lane, "docs/new.md", f"read {URL} for more\n", commit=False)
    head = _git(lane["repo"], "rev-parse", "HEAD")
    status, output = _deployment_values(lane, ["docs/new.md"])
    # MUTATION: diffing base...HEAD alone misses what the worker commits at
    # publish -- the agent's uncommitted and untracked files.
    assert status == prepublish.FAILED, output
    assert "docs/new.md:1" in output
    assert _git(lane["repo"], "rev-parse", "HEAD") == head
    assert len(_git(lane["repo"], "worktree", "list").splitlines()) == 1
    assert _git(lane["repo"], "status", "--porcelain") == "?? docs/"


@needs_git
def test_a_base_without_the_guard_skips_the_check(lane):
    shutil.rmtree(lane["repo"] / "scripts" / "guards")
    _git(lane["repo"], "add", "-A")
    _git(lane["repo"], "commit", "-q", "-m", "no guard")
    base = _git(lane["repo"], "rev-parse", "HEAD")
    shutil.copytree(REPO / "scripts" / "guards", lane["repo"] / "scripts" / "guards")
    _add(lane, "docs/new.md", "a note\n")
    checks = prepublish.select_checks(lane["repo"], ["docs/new.md"], base=base, env=lane["env"])
    (check,) = [c for c in checks if c.name == prepublish.DEPLOYMENT_VALUES_CHECK]
    assert check.run is not None
    status, output = check.run(prepublish.run_command, lane["repo"], lane["env"], 120.0)
    assert status == prepublish.SKIPPED and "base" in output


# ---------------------------------------------------------------------------
# The publish credential scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "status"), [(0, prepublish.PASSED), (1, prepublish.FAILED),
                                              (2, prepublish.ERROR)])
def test_the_publish_scan_reads_its_three_exits(tmp_path, code, status):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / prepublish.PUBLISH_SCAN_SCRIPT).write_text("")
    calls: list[list[str]] = []

    def runner(argv: Any, cwd: Path, env: Any, timeout: float) -> prepublish.Ran:
        calls.append(list(argv))
        return prepublish.Ran(code, "docs/x.md:3 generic-token\n" if code == 1 else "")

    (check,) = [c for c in prepublish.select_checks(repo, ["docs/x.md"], base="b", env={})
                if c.name == prepublish.PUBLISH_SCAN_CHECK]
    assert check.run is not None
    got, output = check.run(runner, repo, {}, 60.0)
    assert got == status
    assert calls == [["bash", prepublish.PUBLISH_SCAN_SCRIPT, "--base", "b"]]
    if code == 1:
        assert "docs/x.md:3" in output
