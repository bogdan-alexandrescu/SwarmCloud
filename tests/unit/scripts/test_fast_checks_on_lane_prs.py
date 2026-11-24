"""The aggregate gates and ci-gate's reuse verdict under the fast-checks rule.

Owner decision 2026-11-11 ("skip pr testing and only run tests when merging to
main", "Fast checks only on PRs"; docs/ci.md "What runs where"). Every HEAVY
job of application.yml runs only when the run is not a pull request, or the
pull request's head is a `train/` branch (scripts/merge-train.sh), or it
carries the `full-ci` label; the rule is the `changes` job's `full` output.

Held here: the aggregate gates (`ci-gate`, `format / unit tests`,
`swarm-ui typecheck / component tests`), their first step run from its
published run block (ci_run_blocks), read a heavy job skipped by the rule as a
pass and a failed one as a failure, and ci-gate's reuse verdict
(scripts/lib/ci_reuse.py) does not mistake the skip for an unsourced one.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from .ci_run_blocks import run_block
from .test_ci_reuse import reuse as ci_reuse

FULL_IF = "needs.changes.outputs.full == 'true' && "

#: Heavy jobs of application.yml that run on a pull request, so they carry the rule.
HEAVY_GATED = {"python-unit", "ui-build", "ui-tests", "integration", "build-check"}

#: Each aggregate's `needs:` and the key of its first step, the verdict, whose
#: `env` is `NEEDS: ${{ toJSON(needs) }}`.
VERDICT = "every job this check stands for passed, or was skipped by its own if"
AGGREGATES = {
    "ci-gate": (["shell", "workflows", "ruff", "changes", "quick-checks", "python-checks", "python-unit",
                 "ui-build", "ui-tests", "integration", "manifests", "build-check", "build", "tested-tree",
                 "plugin-tag"],
                "every job of this workflow passed, or was skipped by its own if"),
    "python": (["changes", "python-checks", "python-unit"], VERDICT),
    "ui": (["changes", "ui-build", "ui-tests"], VERDICT),
}


def test_ci_reuse_knows_every_gated_reusable_job():
    """ci-gate's reuse verdict reads REACH; a heavy job it does not know as
    heavy would fail the gate on every lane pull request. MUTATION: drop a key
    from ci_reuse.HEAVY."""
    assert set(ci_reuse.HEAVY) == HEAVY_GATED & set(ci_reuse.REUSABLE)
    assert ci_reuse.FULL_IF + " && " == FULL_IF


# --- the gates ----------------------------------------------------------------
def _needs(job_ids, *, full: str, failed: str = "") -> dict:
    outputs = {"app": "true", "python": "all", "ui": "true", "full": full, "reused": "[]", "reused_from": ""}
    needs = {"changes": {"result": "success", "outputs": outputs}}
    for job_id in job_ids:
        if job_id == "changes":
            continue
        heavy = job_id in HEAVY_GATED
        result = "skipped" if heavy and full == "false" else "success"
        needs[job_id] = {"result": "failure" if job_id == failed else result, "outputs": {}}
    return needs


def _verdict(job_id: str, needs: dict) -> subprocess.CompletedProcess:
    script = run_block("application.yml", job_id, AGGREGATES[job_id][1])
    return subprocess.run(["bash", "-c", script], env={**os.environ, "NEEDS": json.dumps(needs)},
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("job_id", ["ci-gate", "python", "ui"])
def test_an_aggregate_gate_passes_a_heavy_job_the_rule_skipped(job_id):
    """MUTATION: count `skipped` as a failure in the gate's first step."""
    needs = _needs(AGGREGATES[job_id][0], full="false")
    proc = _verdict(job_id, needs)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("job_id, failed", [("ci-gate", "python-unit"), ("ci-gate", "integration"),
                                             ("ci-gate", "build-check"), ("python", "python-unit"),
                                             ("ui", "ui-tests")])
def test_an_aggregate_gate_still_fails_a_heavy_job_that_ran_and_failed(job_id, failed):
    """MUTATION: pass every result the gate reads."""
    needs = _needs(AGGREGATES[job_id][0], full="true", failed=failed)
    proc = _verdict(job_id, needs)
    assert proc.returncode == 1 and failed in proc.stdout, proc.stdout + proc.stderr


def test_the_reuse_verdict_passes_a_heavy_skip_only_when_the_rule_is_off():
    """With `full` false a skipped heavy job was not reached; with `full`
    true (or absent) the same skip has no source and fails the gate.
    MUTATION: read every heavy skip as a pass, or ignore `full`."""
    jobs = AGGREGATES["ci-gate"][0]
    problems, _ = ci_reuse.gate(_needs(jobs, full="false"), "pull_request")
    assert problems == [], problems
    for full in ("true", ""):
        needs = _needs(jobs, full=full)
        needs["python-unit"]["result"] = "skipped"
        if not full:
            del needs["changes"]["outputs"]["full"]
        problems, _ = ci_reuse.gate(needs, "pull_request")
        assert len(problems) == 1 and "python-unit" in problems[0], (full, problems)
