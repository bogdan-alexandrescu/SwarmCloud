"""application.yml's `changes` job decides which jobs a pull request runs.

WHY THIS EXISTS (archived issue 605, owner decision 2026-10-05). application.yml's `ui` job
(typecheck, ~9 minutes of jsdom component tests, the production build) ran on
every pull request, so archived pull request 601 -- a scheduler-only change -- went red on a UI load
flake in code it never touched. The UI jobs `needs:` a gate job and read its
`ui` output in their `if:`.

THE SAME GATE NOW DECIDES THE REST (owner request 2026-10-07). application.yml
runs on every pull request -- its `ci-gate` job is the required check, and a
workflow filtered out by `on.pull_request.paths` reports nothing -- so that
path list moved into this job as APP_PATHS, and it writes:

  * `app`: a changed file matches APP_PATHS; every other job is skipped
    without it, as the whole workflow used to be;
  * `python`: `all`, or `subset` when every such file is under docs/ or
    apps/swarm-ui/ (the unit shards then run only the tests that read those),
    or `none`;
  * `areas`: which of `docs` and `swarm-ui` the subset is for;
  * `ui`: as before -- apps/swarm-ui/, the API's route surface
    (`apps/swarm-api/swarm_api/routes/`, `apps/swarm-api/swarm_api/main.py`)
    or the workflow file itself.

The rules are a `run:` block nothing else reads, so a prefix dropped from it,
an always-run on push lost, or a failed diff read as "nothing changed" would
pass every other check and silently stop testing. So, BY RUNNING THE STEP
rather than matching its text, over the path-filter matrix:

  * docs-only: no UI, the Python subset for docs; UI-only: the UI, the Python
    subset for swarm-ui; both: the subset for both; any other file in the list:
    everything Python; nothing in the list: nothing but the gate;
  * on a push or a dispatch everything runs without diffing;
  * a base it cannot diff against runs everything: doubt runs the jobs;
  * a pattern shape the step does not model is refused, not guessed.

The step's shell is read from scripts/generated/ci-run-blocks.json; its
APP_PATHS env is written below (the list as of 2026-10-30). The pins on the
job's outputs and `if:`s and on the list itself were deleted by issue 71.

WHY A SKIPPED JOB IS STILL SAFE. A job skipped by its own `if:` reports
`skipped`, which the aggregating jobs and ci-gate count as a pass only while
`changes` itself succeeded (test_ci_gate.py runs that judgement). If `changes`
fails, everything reading it is skipped and the judgement fails.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from .ci_run_blocks import run_block

EVERYTHING = {"app": "true", "python": "all", "areas": "", "ui": "true"}
NOTHING = {"app": "false", "python": "none", "areas": "", "ui": "false"}

#: The `diff` step's APP_PATHS env: a path, or `<dir>/**` for everything
#: under `<dir>/`; `#` lines are commentary.
APP_PATHS = """apps/**
images/**
kubernetes/**
scripts/**
tests/**
docs/**
# a comment line is skipped
terraform/modules/monitoring/alerts.tf
terraform/modules/service_account_ids/main.tf
plugin/**
.claude-plugin/**
pyproject.toml
uv.lock
Makefile
README.md
CLAUDE.md
CONTRACT.md
.github/workflows/application.yml
.github/workflows/release.yml
.github/workflows/hotfix.yml
.github/actions/**
.github/workflows/accept.yml
.github/ISSUE_TEMPLATE/**
.github/labels.yml
.github/workflows/iam-refusal-probe.yml
.github/workflows/ci-fix.yml
.github/workflows/auto-merge.yml
scripts/ci-gate.sh
.dockerignore
.gcloudignore
install/**
"""


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


def _repo_changing(tmp_path: Path, *changed: str) -> tuple[Path, str, str]:
    """A scratch repository with a base commit and a head commit that changes
    `changed` alone. Returns (repo, base sha, head sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for name in changed:
        path = repo / name
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
    body.write_text(run_block("application.yml", "changes", "diff"))
    # The step's literal env (APP_PATHS); the expressions are the caller's.
    merged = {**os.environ, "APP_PATHS": APP_PATHS, "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(runner_temp),
              **env}
    result = subprocess.run(["bash", "-e", str(body)], cwd=cwd, env=merged, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return result, outputs


def _pull_request(tmp_path: Path, *changed: str) -> dict[str, str]:
    repo, base, head = _repo_changing(tmp_path, *changed)
    result, outputs = _run(tmp_path, repo, {"EVENT": "pull_request", "BASE_SHA": base, "HEAD_SHA": head})
    assert result.returncode == 0, result.stdout + result.stderr
    return outputs


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "merge_group"])
def test_a_push_a_dispatch_or_a_queue_entry_runs_everything(tmp_path, event):
    result, outputs = _run(tmp_path, tmp_path, {"EVENT": event, "BASE_SHA": "", "HEAD_SHA": ""})
    assert result.returncode == 0, result.stderr
    assert outputs == EVERYTHING


# The path-filter matrix. Each row: the files a pull request changes, and what
# the gate must say.
MATRIX = [
    # docs-only: no UI; the Python tests that read docs/ still run.
    (["docs/ci.md"], {"app": "true", "python": "subset", "areas": "docs", "ui": "false"}),
    (["docs/runbooks/merge-app.md", "docs/ci.md"], {"app": "true", "python": "subset", "areas": "docs", "ui": "false"}),
    # A docs file under a swarm-ui-looking path is still docs.
    (["docs/apps/swarm-ui/notes.md"], {"app": "true", "python": "subset", "areas": "docs", "ui": "false"}),
    # UI-only: the UI, and the Python tests that read apps/swarm-ui.
    (["apps/swarm-ui/src/AgentDetail.tsx"], {"app": "true", "python": "subset", "areas": "swarm-ui", "ui": "true"}),
    (["apps/swarm-ui/package.json"], {"app": "true", "python": "subset", "areas": "swarm-ui", "ui": "true"}),
    # Both.
    (["docs/ci.md", "apps/swarm-ui/src/App.tsx"],
     {"app": "true", "python": "subset", "areas": "docs swarm-ui", "ui": "true"}),
    # The API's route surface reaches the UI AND is Python.
    (["apps/swarm-api/swarm_api/routes/tasks.py"], {**EVERYTHING}),
    (["apps/swarm-api/swarm_api/main.py"], {**EVERYTHING}),
    # This workflow reaches everything.
    ([".github/workflows/application.yml"], {**EVERYTHING}),
    # archived pull request 601's shape: the scheduler alone -- every Python test, no UI.
    (["apps/scheduler/scheduler/store.py"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    # Near misses of the UI prefixes.
    (["apps/swarm-api/swarm_api/store.py"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    (["apps/swarm-api/swarm_api/main.py.orig"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    (["apps/swarm-uix/a.ts"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    # docs plus anything else Python: everything Python.
    (["docs/ci.md", "scripts/ci-gate.sh"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    (["apps/swarm-ui/src/App.tsx", "tests/unit/scripts/test_ci_gate.py"],
     {"app": "true", "python": "all", "areas": "", "ui": "true"}),
    # Named one by one in APP_PATHS.
    (["README.md"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    (["terraform/modules/monitoring/alerts.tf"], {"app": "true", "python": "all", "areas": "", "ui": "false"}),
    # Outside the list: nothing but the gate runs, as when the workflow was
    # filtered out. A file outside the list beside docs changes nothing.
    ([".github/workflows/terraform.yml"], NOTHING),
    (["terraform/infra/main.tf"], NOTHING),
    (["LICENSE"], NOTHING),
    # `docs/**` needs something under docs/; a root file named like it is not.
    (["docs"], NOTHING),
    (["docsx/a.md"], NOTHING),
    (["README.md.orig"], NOTHING),
    (["terraform/infra/main.tf", "docs/ci.md"], {"app": "true", "python": "subset", "areas": "docs", "ui": "false"}),
]


@pytest.mark.parametrize("changed, expected", MATRIX, ids=[" + ".join(c) for c, _ in MATRIX])
def test_the_path_filter_matrix(tmp_path, changed, expected):
    """MUTATION: drop docs/ from the subset rule (docs-only would run every
    Python test), treat apps/swarm-ui/ as `others` (UI-only would), skip the
    Python tests entirely on a docs-only change, or match a prefix without its
    slash (docsx/ would count as docs/)."""
    assert _pull_request(tmp_path, *changed) == expected


def test_a_base_it_cannot_diff_against_runs_everything(tmp_path):
    repo, _, head = _repo_changing(tmp_path, "docs/ci.md")
    missing = "0" * 40
    result, outputs = _run(tmp_path, repo, {"EVENT": "pull_request", "BASE_SHA": missing, "HEAD_SHA": head})
    assert result.returncode == 0, result.stderr
    assert outputs == EVERYTHING


@pytest.mark.parametrize("pattern", ["apps/*.py", "**/*.md", "docs/[ab]/x", "a?c"])
def test_a_pattern_shape_the_step_does_not_model_is_refused(tmp_path, pattern):
    """GitHub's `*` stops at `/` and bash's does not: a glob the step matched
    by its own rules would disagree with the filter it replaced. So it fails,
    which fails every job reading it and ci-gate.
    MUTATION: drop the refusal and match the pattern anyway."""
    repo, base, head = _repo_changing(tmp_path, "apps/x.py")
    result, outputs = _run(tmp_path, repo, {"EVENT": "pull_request", "BASE_SHA": base, "HEAD_SHA": head,
                                            "APP_PATHS": f"apps/**\n{pattern}\n"})
    assert result.returncode != 0, result.stdout
    assert pattern in result.stdout, result.stdout
    assert outputs == {}, outputs
