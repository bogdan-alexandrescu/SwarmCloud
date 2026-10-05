"""The swarm-ui job runs on a pull request only when the change can reach swarm-ui.

WHY THIS EXISTS (#605, owner decision 2026-10-05). application.yml's `ui` job
(typecheck, ~9 minutes of jsdom component tests, the production build) ran on
every pull request, so #601 -- a scheduler-only change -- went red on a UI load
flake in code it never touched. The job now `needs:` a gate job,
`ui-changes`, whose output it reads in its `if:`. The gate diffs the pull
request against its base and says `ui=true` only when a changed path is under
`apps/swarm-ui/`, the API's route surface (`apps/swarm-api/swarm_api/routes/`,
`apps/swarm-api/swarm_api/main.py`) or the workflow file itself.

The rule is one regex inside a `run:` block, which nothing else reads, so a
prefix dropped from it, an always-run on push lost, or a failed diff read as
"no UI change" would pass every other check and silently stop testing the UI.

WHAT IS ASSERTED, BY RUNNING THE STEP rather than matching its text:

  * `ui` needs `ui-changes` and runs only on its `ui == 'true'` output;
  * on a push or a dispatch the step says `ui=true` without diffing;
  * on a pull request it says `ui=true` for a change under each of the four
    paths, and `ui=false` for a change outside them (the #601 shape);
  * a base it cannot diff against says `ui=true`: doubt runs the job.

WHY A SKIPPED `ui` IS STILL SAFE FOR THE GATE. A job skipped by its own `if:`
is listed `skipped` in a run that concluded `success`, which scripts/ci-gate.sh
counts as a pass ("WHAT `skipped` MEANS"); test_ci_gate.py holds that rule. If
`ui-changes` itself fails, `ui` is skipped as a dependant and the run concludes
`failure`, which the gate fails on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"


def _jobs() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]


def _diff_step() -> dict:
    steps = _jobs()["ui-changes"]["steps"]
    found = [s for s in steps if s.get("id") == "diff"]
    assert len(found) == 1, "ui-changes has no single step with id `diff`"
    return found[0]


def test_the_ui_job_runs_only_on_the_gates_output():
    jobs = _jobs()
    ui = jobs["ui"]
    needs = ui.get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "ui-changes" in needs, "the ui job does not need ui-changes"
    assert ui.get("if", "").replace(" ", "") == "needs.ui-changes.outputs.ui=='true'"
    gate = jobs["ui-changes"]
    assert gate["outputs"]["ui"].replace(" ", "") == "${{steps.diff.outputs.ui}}"
    # The gate itself is never filtered: if it were skipped, so would ui be.
    assert "if" not in gate
    # `base...head` needs the history; a depth-1 clone has no merge base.
    checkout = next(s for s in gate["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _repo_changing(tmp_path: Path, changed: str) -> tuple[Path, str, str]:
    """A scratch repository with a base commit and a head commit that changes
    `changed` alone. Returns (repo, base sha, head sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    path = repo / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _run(tmp_path: Path, cwd: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out = tmp_path / "github-output"
    out.write_text("")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    body = tmp_path / "step.sh"
    body.write_text(_diff_step()["run"])
    merged = {**os.environ, "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(runner_temp), **env}
    result = subprocess.run(["bash", "-e", str(body)], cwd=cwd, env=merged, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return result, outputs


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_a_push_or_a_dispatch_always_runs_the_ui_job(tmp_path, event):
    result, outputs = _run(tmp_path, tmp_path, {"EVENT": event, "BASE_SHA": "", "HEAD_SHA": ""})
    assert result.returncode == 0, result.stderr
    assert outputs == {"ui": "true"}


@pytest.mark.parametrize(
    "changed",
    [
        "apps/swarm-ui/src/AgentDetail.tsx",
        "apps/swarm-api/swarm_api/routes/tasks.py",
        "apps/swarm-api/swarm_api/main.py",
        ".github/workflows/application.yml",
    ],
)
def test_a_pull_request_that_reaches_swarm_ui_runs_the_ui_job(tmp_path, changed):
    repo, base, head = _repo_changing(tmp_path, changed)
    result, outputs = _run(tmp_path, repo, {"EVENT": "pull_request", "BASE_SHA": base, "HEAD_SHA": head})
    assert result.returncode == 0, result.stderr
    assert outputs == {"ui": "true"}, result.stdout


@pytest.mark.parametrize(
    "changed",
    [
        # #601's shape: the scheduler alone.
        "apps/scheduler/scheduler/store.py",
        # Near misses of the four prefixes.
        "apps/swarm-api/swarm_api/store.py",
        "apps/swarm-api/swarm_api/main.py.orig",
        ".github/workflows/terraform.yml",
        "docs/apps/swarm-ui/notes.md",
    ],
)
def test_a_pull_request_outside_swarm_ui_skips_the_ui_job(tmp_path, changed):
    repo, base, head = _repo_changing(tmp_path, changed)
    result, outputs = _run(tmp_path, repo, {"EVENT": "pull_request", "BASE_SHA": base, "HEAD_SHA": head})
    assert result.returncode == 0, result.stderr
    assert outputs == {"ui": "false"}, result.stdout


def test_a_base_it_cannot_diff_against_runs_the_ui_job(tmp_path):
    repo, _, head = _repo_changing(tmp_path, "apps/scheduler/scheduler/store.py")
    missing = "0" * 40
    result, outputs = _run(tmp_path, repo, {"EVENT": "pull_request", "BASE_SHA": missing, "HEAD_SHA": head})
    assert result.returncode == 0, result.stderr
    assert outputs == {"ui": "true"}
