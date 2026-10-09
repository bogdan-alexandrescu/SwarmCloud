"""application.yml's `changes` job decides which jobs a pull request runs.

WHY THIS EXISTS (#605, owner decision 2026-10-05). application.yml's `ui` job
(typecheck, ~9 minutes of jsdom component tests, the production build) ran on
every pull request, so #601 -- a scheduler-only change -- went red on a UI load
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
  * a pattern shape the step does not model is refused, not guessed;
  * the reading of APP_PATHS the other tests use (application_paths.py) agrees
    with the step on every pattern in the list.

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
import yaml

from .application_paths import app_paths, diff_step, reaches

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"

EVERYTHING = {"app": "true", "python": "all", "areas": "", "ui": "true"}
NOTHING = {"app": "false", "python": "none", "areas": "", "ui": "false"}


def _jobs() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs")
    return [needs] if isinstance(needs, str) else list(needs or [])


def test_each_job_runs_only_on_the_gates_output():
    """MUTATION: drop a job's `if:`, or point it at the wrong output."""
    jobs = _jobs()
    gate = jobs["changes"]
    for name in ("app", "python", "areas", "ui"):
        assert gate["outputs"][name].replace(" ", "") == "${{steps.diff.outputs.%s}}" % name, gate["outputs"]
    # The gate itself is never filtered: if it were skipped, so would everything be.
    assert "if" not in gate
    # `base...head` needs the history; a depth-1 clone has no merge base.
    checkout = next(s for s in gate["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0

    expected_if = {
        "ui-build": "needs.changes.outputs.ui == 'true'",
        "ui-tests": "needs.changes.outputs.ui == 'true'",
        "python-checks": "needs.changes.outputs.python == 'all'",
        "python-unit": "needs.changes.outputs.python != 'none'",
        "shell": "needs.changes.outputs.app == 'true'",
        "workflows": "needs.changes.outputs.app == 'true'",
        "integration": "needs.changes.outputs.app == 'true'",
        "manifests": "needs.changes.outputs.app == 'true'",
        # `images` is false when app is (the images step says so first), and
        # test_build_images_local.py runs that step against the plan's answer.
        "build-check": "github.event_name == 'pull_request' && needs.changes.outputs.images == 'true'",
    }
    for job_id, condition in expected_if.items():
        assert "changes" in _needs(jobs[job_id]), f"{job_id} does not need changes"
        assert jobs[job_id].get("if") == condition, (job_id, jobs[job_id].get("if"))
    # Every job but the gate's own reads, the aggregators, ci-gate and main's
    # build is listed above: a new job must decide what reaches it.
    rest = set(jobs) - set(expected_if) - {"changes", "python", "ui", "ci-gate", "build"}
    assert not rest, f"jobs that do not read the changes gate: {sorted(rest)}"


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
    step = diff_step()
    body = tmp_path / "step.sh"
    body.write_text(step["run"])
    # The step's literal env (APP_PATHS); the expressions are the caller's.
    literal = {k: str(v) for k, v in (step.get("env") or {}).items() if "${{" not in str(v)}
    merged = {**os.environ, **literal, "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(runner_temp), **env}
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
    # #601's shape: the scheduler alone -- every Python test, no UI.
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


def test_the_list_carries_every_pattern_the_old_filter_did():
    """The patterns are those of `on.pull_request.paths` as of 2026-10-07,
    minus ci-gate.yml, which is gone, plus the hotfix lane and the release
    stages both lanes run (2026-10-08, observer proposal H), which run only on
    main and are read by test_hotfix_release.py, plus accept.yml (2026-10-08,
    release timing cuts A and C), which runs only after a release completes on
    main and is read by test_release_acceptance_job.py. MUTATION: drop any one."""
    assert set(app_paths()) == {
        "apps/**", "images/**", "kubernetes/**", "scripts/**", "tests/**", "docs/**",
        "terraform/modules/monitoring/alerts.tf", "plugin/**", ".claude-plugin/**",
        "pyproject.toml", "uv.lock", "Makefile", "README.md", "CLAUDE.md", "CONTRACT.md",
        ".github/workflows/application.yml", ".github/workflows/release.yml",
        ".github/ISSUE_TEMPLATE/**", ".github/labels.yml", ".github/workflows/iam-refusal-probe.yml",
        ".github/workflows/ci-fix.yml", ".github/workflows/auto-merge.yml", "scripts/ci-gate.sh",
        ".dockerignore", ".gcloudignore", ".github/workflows/hotfix.yml", ".github/actions/**",
        ".github/workflows/accept.yml",
    }


def test_the_shared_reading_of_the_list_agrees_with_the_step(tmp_path):
    """application_paths.reaches() is what other tests ask; it must say what
    the step says, for a sample under each pattern and a near miss of each."""
    samples = []
    for pattern in app_paths():
        if pattern.endswith("/**"):
            samples += [pattern[:-2] + "a/b.txt", pattern[:-3] + "x/b.txt"]
        else:
            samples += [pattern, pattern + ".orig"]
    for i, sample in enumerate(samples):
        sub = tmp_path / str(i)
        sub.mkdir()
        outputs = _pull_request(sub, sample)
        assert (outputs["app"] == "true") == reaches(sample), (sample, outputs)
