"""The `deployment-values` check: the guard, enforcing, on every change.

docs/design/generic-repository.md, section 4.3: security.yml's
`deployment-values` job runs scripts/guards/deployment-values.sh in enforce
mode over a pull request's added lines (`--diff` against its base) and over
every tracked file on main, reading the pattern list from the
DEPLOYMENT_PATTERNS secret. A fork's pull request gets no secrets: it runs
the shape layer alone, says so, and prints only pass or fail and a count.

The step's script (its published run block) runs here for real, with the
real guard, in a scratch git repository whose `scripts` is this checkout's,
against a pattern list built at run time: a planted value turns the check
red, and the list's value never reaches the output.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from .ci_run_blocks import run_block

REPO = Path(__file__).resolve().parents[3]
STEP = "deployment-values-enforce"

_spec = importlib.util.spec_from_file_location("deployment_values", REPO / "scripts" / "guards" / "deployment_values.py")
guard = importlib.util.module_from_spec(_spec)
# Registered before it runs: its dataclasses look their module up by name.
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)


def _script() -> str:
    """The guard's step, run from its published run block (ci_run_blocks)."""
    return run_block("security.yml", "deployment-values", STEP)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def lane(tmp_path: Path) -> dict:
    """A repository with main at one commit and a pull request's head one
    commit later, `scripts` linked to this checkout's and never tracked."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "scripts").symlink_to(REPO / "scripts")
    (root / ".git" / "info" / "exclude").write_text("scripts\n", encoding="utf-8")
    (root / "README.md").write_text("A clean file.\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "update-ref", "refs/remotes/origin/main", _git(root, "rev-parse", "HEAD"))
    _git(root, "checkout", "-q", "-b", "lane")
    # A name the pattern list holds, built at run time so no file holds it.
    word = "zq" + secrets.token_hex(5)
    _, listed = guard.stamp(f"C9\tliteral\t{word}\n")
    # The runner's workspace: guard/ is the base commit's checkout (this
    # checkout stands in for it), and the lane's repository is pr/.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "guard").symlink_to(REPO)
    return {"root": root, "word": word, "list": listed, "tmp": tmp_path, "workspace": workspace}


def _add(lane: dict, line: str) -> None:
    path = lane["root"] / "notes.md"
    path.write_text(line + "\n", encoding="utf-8")
    _git(lane["root"], "add", "notes.md")
    _git(lane["root"], "commit", "-q", "-m", "lane")


def _run(lane: dict, *, event: str = "pull_request", fork: bool = False, patterns: str | None = None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(lane["tmp"]),
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "SWARM_ENV_FILE": str(lane["tmp"] / "no.env"),
        "EVENT": event,
        # The branch the pull request targets now, never a recorded base commit.
        "PR_BASE_REF": "main" if event == "pull_request" else "",
        "GITHUB_WORKSPACE": str(lane["workspace"]),
        "QUEUE_BASE": "",
        "FORK": "true" if fork else "false",
        "DEPLOYMENT_PATTERNS": lane["list"] if patterns is None else patterns,
    }
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _script()],
        env=env, cwd=lane["root"], capture_output=True, text=True, timeout=120, check=False,
    )


def test_a_pull_request_adding_a_listed_value_is_red_and_never_prints_it(lane):
    """MUTATION: run the guard in report mode, or not at all, and this is
    green."""
    _add(lane, f"Deployed for {lane['word']} last week.")
    proc = _run(lane)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "notes.md:1 C9 list:" in proc.stdout
    assert lane["word"] not in proc.stdout + proc.stderr


def test_marking_files_binary_never_hides_an_added_line(lane):
    """The re-review of 14 November: a change that adds `* -diff` to
    .gitattributes made git print "Binary files differ" for every modified
    file, so the guard saw none of its added lines. The guard diffs as text."""
    (lane["root"] / ".gitattributes").write_text("* -diff\n", encoding="utf-8")
    (lane["root"] / "README.md").write_text(f"A clean file.\nnow {lane['word']}\n", encoding="utf-8")
    _git(lane["root"], "add", ".gitattributes", "README.md")
    _git(lane["root"], "commit", "-q", "-m", "hide")
    proc = _run(lane)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "README.md:2 C9" in proc.stdout
    assert lane["word"] not in proc.stdout + proc.stderr


def test_a_clean_pull_request_is_green(lane):
    _add(lane, "Nothing here names anybody.")
    proc = _run(lane)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_main_is_judged_over_every_tracked_file(lane):
    """A push reads the tree, so a value already on the base is found too."""
    _add(lane, f"Deployed for {lane['word']}.")
    _git(lane["root"], "update-ref", "refs/remotes/origin/main", _git(lane["root"], "rev-parse", "HEAD"))
    assert _run(lane).returncode == 0  # the pull request's diff is now empty
    proc = _run(lane, event="push")
    assert proc.returncode == 1 and "notes.md:1 C9" in proc.stdout


def test_a_same_repository_run_without_the_list_is_red(lane):
    """Fails closed: an unset secret is never a pass."""
    _add(lane, "Nothing here names anybody.")
    proc = _run(lane, patterns="")
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_a_fork_runs_the_shape_layer_alone_and_says_so(lane):
    _add(lane, f"Deployed for {lane['word']}.")
    proc = _run(lane, fork=True, patterns="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "shape layer passed" in proc.stdout
    assert "notes.md" not in proc.stdout


def test_a_fork_adding_a_shaped_value_is_red_with_a_count_only(lane):
    account = f"deployer@{secrets.token_hex(4)}-prod.iam.gserviceaccount.com"
    _add(lane, f"Runs as {account}.")
    proc = _run(lane, fork=True, patterns="")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "fail: 1 findings" in proc.stdout
    assert "notes.md" not in proc.stdout and account not in proc.stdout


def test_a_pull_requests_diff_is_against_the_branch_it_targets_now():
    """A retargeted pull request's recorded base commit is the old branch's:
    the added lines are read against the branch it targets now, while the
    guard itself still comes from the base commit. MUTATION: hand --diff the
    recorded pull_request.base.sha."""
    env = _step().get("env") or {}
    assert env.get("PR_BASE_REF") == "${{ github.event.pull_request.base.ref }}", env
    assert "pull_request.base.sha" not in str(env), env
    assert '--diff "refs/remotes/origin/${PR_BASE_REF:?' in _script(), _script()
