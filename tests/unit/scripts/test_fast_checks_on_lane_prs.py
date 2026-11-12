"""A lane's pull request runs the fast checks only; the full suite runs on the
merge train's pull request, on main and wherever `full-ci` asks for it.

Owner decision 2026-11-11 ("skip pr testing and only run tests when merging to
main", "Fast checks only on PRs"; docs/ci.md "What runs where"). Every HEAVY
job of application.yml, terraform.yml and security.yml runs only when the run
is not a pull request, or the pull request's head is a `train/` branch
(scripts/merge-train.sh), or it carries the `full-ci` label.

The properties held here, against the real workflow files:

  * the rule is written ONCE, as the `changes` job's `full` output, and every
    heavy job that can run on a pull request reads it first in its `if:`;
  * a heavy job that never runs on any pull request (an image build, a
    Terraform plan, the published-image scan) keeps its own main-only `if:`;
  * no fast job reads it, and every job of the three files is classified, so a
    new job must be put on one side or the other;
  * the aggregate gates (`ci-gate`, `format / unit tests`,
    `swarm-ui typecheck / component tests`) read a heavy job skipped by the
    rule as a pass and a failed one as a failure, and ci-gate's reuse verdict
    (scripts/lib/ci_reuse.py) does not mistake the skip for an unsourced one.

The workflow lines ship as workflow-lines.patch, pushed by hand: until it is
applied the workflow assertions here fail with PATCH_NOTE.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_ci_reuse import reuse as ci_reuse

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"

PATCH_NOTE = ("-- the workflow change of 'fast checks only on lane PRs' is not applied: it ships "
              "as workflow-lines.patch, which the operator pushes by hand")

FULL_OUTPUT = ("${{ (github.event_name != 'pull_request' || startsWith(github.head_ref, 'train/') "
               "|| contains(github.event.pull_request.labels.*.name, 'full-ci')) && 'true' || 'false' }}")
FULL_IF = "needs.changes.outputs.full == 'true' && "

#: Heavy jobs that run on a pull request, so they carry the rule.
HEAVY_GATED = {
    "application.yml": {
        "python-unit": "unit tests (${{ matrix.shard }}/${{ strategy.job-total }})",
        "ui-build": "swarm-ui typecheck / build",
        "ui-tests": "swarm-ui component tests (${{ matrix.shard }}/${{ strategy.job-total }})",
        "integration": "integration tests (emulator)",
        "build-check": "build changed images without pushing",
    },
    "terraform.yml": {},
    "security.yml": {},
}
#: Heavy jobs that already never run on any pull request: they need no rule.
HEAVY_MAIN_ONLY = {
    "application.yml": {"build"},
    "terraform.yml": {"plan"},
    "security.yml": {"images"},
}
#: Fast checks: they run on every pull request and never read the rule.
FAST = {
    "application.yml": {"changes", "shell", "workflows", "ruff", "python-checks", "quick-checks", "manifests"},
    "terraform.yml": {"offline", "validate", "test", "checkov", "plan-not-run"},
    # deployment-values: the name keeper of the guard folded into `checks`,
    # which runs on every change (part of #2688), like `secrets` and `policy`.
    "security.yml": {"filesystem", "checks", "secrets", "policy", "iac", "deployment-values"},
}
#: Aggregates and bookkeeping: they read other jobs' results.
AGGREGATE = {
    "application.yml": {"python", "ui", "tested-tree", "ci-gate", "plugin-tag"},
    "terraform.yml": set(),
    "security.yml": set(),
}
# The event clauses a main-only job's every disjunct must carry one of.
NOT_A_PULL_REQUEST = ("github.event_name != 'pull_request'", "github.event_name == 'workflow_dispatch'",
                      "github.event_name == 'schedule'")


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def _if(job: dict) -> str:
    return str(job.get("if", "")).removeprefix("${{").removesuffix("}}").strip()


@pytest.mark.parametrize("name", sorted(FAST))
def test_every_job_is_classified(name):
    """MUTATION: add a job without saying whether it is heavy or fast."""
    classified = set(HEAVY_GATED[name]) | HEAVY_MAIN_ONLY[name] | FAST[name] | AGGREGATE[name]
    assert set(_jobs(name)) == classified, sorted(set(_jobs(name)) ^ classified)


def test_the_rule_is_written_once_as_the_changes_job_s_output():
    """MUTATION: drop `train/` or `full-ci` from it, or copy it into a job."""
    jobs = _jobs("application.yml")
    assert jobs["changes"]["outputs"].get("full") == FULL_OUTPUT, f"{jobs['changes']['outputs'].get('full')} {PATCH_NOTE}"
    for name in FAST:
        text = (WORKFLOWS / name).read_text()
        expected = 1 if name == "application.yml" else 0
        assert text.count("startsWith(github.head_ref, 'train/')") == expected, name
        assert text.count("labels.*.name, 'full-ci'") == expected, name


@pytest.mark.parametrize("name, job_id", [(n, j) for n, js in HEAVY_GATED.items() for j in js])
def test_each_heavy_job_reads_the_rule_first(name, job_id):
    """MUTATION: drop the rule from one heavy job's `if:`, or rename the job."""
    job = _jobs(name)[job_id]
    assert str(job["name"]) == HEAVY_GATED[name][job_id], job["name"]
    needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
    assert "changes" in needs, needs
    assert _if(job).startswith(FULL_IF), f"{job_id}: {_if(job)} {PATCH_NOTE}"
    assert _if(job).count("outputs.full") == 1, _if(job)


def test_ci_reuse_knows_every_gated_reusable_job():
    """ci-gate's reuse verdict reads REACH; a heavy job it does not know as
    heavy would fail the gate on every lane pull request. MUTATION: drop a key
    from ci_reuse.HEAVY."""
    gated = set(HEAVY_GATED["application.yml"])
    assert set(ci_reuse.HEAVY) == gated & set(ci_reuse.REUSABLE)
    assert ci_reuse.FULL_IF + " && " == FULL_IF


@pytest.mark.parametrize("name, job_id", [(n, j) for n, js in HEAVY_MAIN_ONLY.items() for j in sorted(js)])
def test_a_main_only_heavy_job_never_runs_on_a_pull_request(name, job_id):
    """MUTATION: let `build`, `plan` or the published-image scan run on a pull request."""
    condition = _if(_jobs(name)[job_id])
    assert condition, job_id
    for disjunct in condition.split("||"):
        assert any(clause in disjunct for clause in NOT_A_PULL_REQUEST), (job_id, disjunct)
    assert "outputs.full" not in condition, condition


@pytest.mark.parametrize("name, job_id", [(n, j) for n, js in FAST.items() for j in sorted(js)]
                         + [(n, j) for n, js in AGGREGATE.items() for j in sorted(js)])
def test_no_fast_or_aggregate_job_reads_the_rule(name, job_id):
    """MUTATION: put the rule on ruff, the secret scan or ci-gate."""
    condition = _if(_jobs(name)[job_id])
    for marker in ("outputs.full", "full-ci", "train/"):
        assert marker not in condition, (job_id, condition)


# --- the gates ----------------------------------------------------------------
def _needs(job_ids, *, full: str, failed: str = "") -> dict:
    outputs = {"app": "true", "python": "all", "ui": "true", "full": full, "reused": "[]", "reused_from": ""}
    needs = {"changes": {"result": "success", "outputs": outputs}}
    for job_id in job_ids:
        if job_id == "changes":
            continue
        heavy = job_id in HEAVY_GATED["application.yml"]
        result = "skipped" if heavy and full == "false" else "success"
        needs[job_id] = {"result": "failure" if job_id == failed else result, "outputs": {}}
    return needs


def _verdict(job_id: str, needs: dict) -> subprocess.CompletedProcess:
    step = _jobs("application.yml")[job_id]["steps"][0]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}", step["env"]
    return subprocess.run(["bash", "-c", step["run"]], env={**os.environ, "NEEDS": json.dumps(needs)},
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("job_id", ["ci-gate", "python", "ui"])
def test_an_aggregate_gate_passes_a_heavy_job_the_rule_skipped(job_id):
    """MUTATION: count `skipped` as a failure in the gate's first step."""
    needs = _needs(_jobs("application.yml")[job_id]["needs"], full="false")
    proc = _verdict(job_id, needs)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("job_id, failed", [("ci-gate", "python-unit"), ("ci-gate", "integration"),
                                             ("ci-gate", "build-check"), ("python", "python-unit"),
                                             ("ui", "ui-tests")])
def test_an_aggregate_gate_still_fails_a_heavy_job_that_ran_and_failed(job_id, failed):
    """MUTATION: pass every result the gate reads."""
    needs = _needs(_jobs("application.yml")[job_id]["needs"], full="true", failed=failed)
    proc = _verdict(job_id, needs)
    assert proc.returncode == 1 and failed in proc.stdout, proc.stdout + proc.stderr


def test_the_reuse_verdict_passes_a_heavy_skip_only_when_the_rule_is_off():
    """With `full` false a skipped heavy job was not reached; with `full`
    true (or absent) the same skip has no source and fails the gate.
    MUTATION: read every heavy skip as a pass, or ignore `full`."""
    jobs = _jobs("application.yml")["ci-gate"]["needs"]
    problems, _ = ci_reuse.gate(_needs(jobs, full="false"), "pull_request")
    assert problems == [], problems
    for full in ("true", ""):
        needs = _needs(jobs, full=full)
        needs["python-unit"]["result"] = "skipped"
        if not full:
            del needs["changes"]["outputs"]["full"]
        problems, _ = ci_reuse.gate(needs, "pull_request")
        assert len(problems) == 1 and "python-unit" in problems[0], (full, problems)


def test_docs_say_what_runs_where():
    """MUTATION: drop the section, or the label's name from it."""
    text = (ROOT / "docs" / "ci.md").read_text()
    start = text.index("## What runs where")
    section = text[start:text.index("\n## ", start + 1)]
    for needle in ("full-ci", "train/", "lane's pull request", "main"):
        assert needle in section, needle
