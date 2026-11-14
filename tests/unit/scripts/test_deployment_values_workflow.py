"""The `deployment-values` check: the guard, enforcing, on every change.

docs/design/generic-repository.md, section 4.3: security.yml's
`deployment-values` job runs scripts/guards/deployment-values.sh in enforce
mode over a pull request's added lines (`--diff` against its base) and over
every tracked file on main, reading the pattern list from the
DEPLOYMENT_PATTERNS secret. A fork's pull request gets no secrets: it runs
the shape layer alone, says so, and prints only pass or fail and a count.

The step's script runs here for real, with the real guard, in a scratch git
repository whose `scripts` is this checkout's, against a pattern list built
at run time: a planted value turns the check red, and the list's value never
reaches the output.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SECURITY = REPO / ".github" / "workflows" / "security.yml"
STEP = "deployment-values · enforce"

_spec = importlib.util.spec_from_file_location("deployment_values", REPO / "scripts" / "guards" / "deployment_values.py")
guard = importlib.util.module_from_spec(_spec)
# Registered before it runs: its dataclasses look their module up by name.
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)


def _jobs() -> dict:
    return yaml.safe_load(SECURITY.read_text(encoding="utf-8"))["jobs"]


def _job() -> dict:
    """The folded job the guard runs in (CI-FOLD, #1799)."""
    return _jobs()["checks"]


def _step() -> dict:
    return next(s for s in _job()["steps"] if s.get("name") == STEP)


def _script() -> str:
    return _step()["run"]


def test_the_check_keeps_its_required_name_and_holds_no_credential():
    """The ruleset requires the check by name: its name-keeper reports the
    folded step's verdict. The job can never ask for cloud credentials (no
    id-token, no auth step), and --diff gets the full history it reads."""
    keeper = _jobs()["deployment-values"]
    assert keeper["name"] == "deployment-values"
    assert keeper["needs"] == "checks"
    assert keeper["steps"][0]["env"]["VERDICT"] == "${{ needs.checks.outputs.deployment-values }}"
    job = _job()
    assert job["permissions"] == {"contents": "read"}
    assert not [s for s in job["steps"] if "google-github-actions/auth" in str(s.get("uses", ""))]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    assert _step()["id"] == "deployment-values-enforce"
    assert _step()["env"]["DEPLOYMENT_PATTERNS"] == "${{ secrets.DEPLOYMENT_PATTERNS }}"
    assert job["outputs"]["deployment-values"] == "${{ steps.verdict.outputs.deployment-values }}"


def test_it_runs_on_every_pull_request_and_on_main():
    on = yaml.safe_load(SECURITY.read_text(encoding="utf-8"))[True]
    assert "pull_request" in on and "merge_group" in on
    assert on["push"] == {"branches": ["main"]}
    # Every pull request but a lane's: the owner's lane rule of 12 November
    # (tests/unit/scripts/test_fast_checks_on_lane_prs.py) skips a lane's
    # pull request and nothing else, so the only condition is that clause.
    assert _job()["if"] == (
        "${{ !(github.event_name == 'pull_request' && startsWith(github.head_ref, 'swarm/') "
        "&& github.event.pull_request.head.repo.full_name == github.repository) }}"
    )


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
    return {"root": root, "word": word, "list": listed, "tmp": tmp_path}


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
        "BASE_REF": "main" if event == "pull_request" else "",
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
