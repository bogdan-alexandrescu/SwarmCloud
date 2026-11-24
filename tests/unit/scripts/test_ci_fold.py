"""Sub-minute checks run as steps of one job per workflow, and every check
name they had still reports (CI-FOLD, archived issue 1799; docs/ci.md "Sub-minute checks
share a job").

WHY. In the 24 h to 2026-10-21 06:00 UTC, 3,313 of 7,341 self-hosted VM jobs
(45%) did under 60 s of work, and each paid a VM create and boot (~52 s at
best) and a slot under the pool's cap. The owner approved folding them
(2026-10-21, FIX 2 of the ci-runners capacity review):

  * application.yml: `release workflow wiring (actionlint)`, `ruff check`,
    `kubernetes manifests` and `lock / contract / format / bridge install`
    run as steps of `quick-checks`;
  * security.yml: `secret scan` and `platform policy assertions` run as
    steps of `checks`; `trivy (repo)` stays its own job, the one that holds
    security-events: write (owner decision 2026-10-21 on PR 1813);
  * terraform.yml: `fmt / validate / tflint` and `terraform test` run as
    steps of `offline`.

Each old job id survives as a NAME-KEEPER on GitHub-hosted ubuntu-24.04 (no
VM) that passes only when its folded job's `verdict` step says that check's
steps succeeded. The two existing name-keepers (`format / unit tests`,
`swarm-ui typecheck / component tests`) and terraform.yml's `plan (not run
on a pull request)`, which read `needs` or write a fixed summary and run
nothing else, move to ubuntu-24.04 too.

WHAT THIS HOLDS. Each name-keeper's shell and each folded job's `verdict`
shell, run from the published run blocks (scripts/generated/ci-run-blocks.json)
with the env the workflow gives them written here. The pins on the layout
itself (job names, runs-on, needs, if:, step ids, the VM counts and their
docs/ci.md table) were deleted by issue 71.

WHAT THIS CANNOT PROVE: that GitHub fills `toJSON(steps)` with an entry per
step id carrying `outcome`, and a job's `outputs` after a failed step, in the
shape the fakes below use. That is GitHub's documented behaviour; the first
pull request carrying the workflow change is the live proof.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .ci_run_blocks import run_block

#: Per workflow: the job the sub-minute checks are folded into, and each old
#: job id kept as a name-keeper with the check name it reports (its CHECK env).
FOLDED: dict[str, tuple[str, dict[str, str]]] = {
    "application.yml": ("quick-checks", {
        "workflows": "release workflow wiring (actionlint)",
        "ruff": "ruff check",
        "manifests": "kubernetes manifests",
        "python-checks": "lock / contract / format / bridge install",
    }),
    "security.yml": ("checks", {
        "secrets": "secret scan",
        "policy": "platform policy assertions",
    }),
    "terraform.yml": ("offline", {
        "validate": "fmt / validate / tflint",
        "test": "terraform test",
    }),
}

needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None, reason="the verdict step needs bash and jq"
)


# ---------------------------------------------------------------------------
# 1. Each name-keeper fails when its folded check did not pass.
# ---------------------------------------------------------------------------
KEEPERS = [(file, job_id, check) for file, (_, keep) in FOLDED.items() for job_id, check in keep.items()]


def _run_keeper(file: str, job_id: str, verdict: str, folded: str) -> subprocess.CompletedProcess:
    host, keep = FOLDED[file]
    script = run_block(file, job_id, f"the folded check passed in `{host}`")
    # The step's env: CHECK is the keeper's own name, VERDICT its folded
    # job's output for it, FOLDED that job's result.
    env = {**os.environ, "CHECK": keep[job_id], "VERDICT": verdict, "FOLDED": folded}
    for leak in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        env.pop(leak, None)
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30,
                          check=False)


@pytest.mark.parametrize("file, job_id, check", KEEPERS)
@pytest.mark.parametrize("verdict, folded", [
    ("failure", "failure"),
    ("skipped", "success"),   # reached, but none of its steps ran
    ("", "failure"),           # the folded job died before its verdict step
    ("", "cancelled"),
    ("", "skipped"),           # the folded job never ran
    ("Success", "success"),    # exact, not case-folded
])
def test_a_name_keeper_fails_whenever_its_folded_check_did_not_pass(file, job_id, check, verdict, folded):
    """A name-keeper must never pass when its folded step failed.
    MUTATION: compare `!= "failure"` instead of `== "success"`, or read the
    folded job's result instead of the check's verdict."""
    proc = _run_keeper(file, job_id, verdict, folded)
    assert proc.returncode != 0, (verdict, folded, proc.stdout)
    assert f"'{check}' did not pass" in proc.stdout, proc.stdout


@pytest.mark.parametrize("file, job_id, check", KEEPERS)
def test_a_name_keeper_passes_when_its_folded_check_passed(file, job_id, check):
    """The control: the keeper can come out green. A sibling check's failure
    fails the folded job, and the run through ci-gate, but not this name.
    MUTATION: make the keeper fail on anything."""
    for folded in ("success", "failure"):
        proc = _run_keeper(file, job_id, "success", folded)
        assert proc.returncode == 0, (folded, proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# 2. The verdict step, run.
# ---------------------------------------------------------------------------
def _steps(file: str, outcome: str = "success") -> dict[str, str]:
    """A folded job's steps as the verdict reads them: a shared setup step,
    then two steps per check, keyed `<check>-<n>`."""
    steps = {"setup-tools": outcome}
    for job_id in FOLDED[file][1]:
        steps[f"{job_id}-one"] = outcome
        steps[f"{job_id}-two"] = outcome
    return steps


def _verdict(file: str, steps: dict[str, str], tmp_path: Path) -> dict[str, str]:
    host, keep = FOLDED[file]
    script = run_block(file, host, "verdict")
    out = tmp_path / "out"
    out.write_text("")
    # The step's env: STEPS is toJSON(steps), CHECKS the folded job ids.
    env = {**os.environ, "STEPS": json.dumps({k: {"outcome": v, "conclusion": v, "outputs": {}}
                                              for k, v in steps.items()}),
           "CHECKS": " ".join(keep), "GITHUB_OUTPUT": str(out)}
    env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


@needs_jq
@pytest.mark.parametrize("file", sorted(FOLDED))
def test_the_verdict_names_the_failed_check_and_only_it(file, tmp_path):
    """MUTATION: let a failed step read as `skipped`, or blame every check."""
    keep = FOLDED[file][1]
    assert _verdict(file, _steps(file), tmp_path) == {k: "success" for k in keep}
    for failed in keep:
        for bad in ("failure", "cancelled"):
            steps = _steps(file)
            steps[f"{failed}-two"] = bad
            got = _verdict(file, steps, tmp_path)
            assert got == {k: ("failure" if k == failed else "success") for k in keep}, (failed, bad, got)


@needs_jq
@pytest.mark.parametrize("file", sorted(FOLDED))
def test_a_failed_setup_fails_every_check_and_an_unreached_check_is_skipped(file, tmp_path):
    """MUTATION: ignore the setup steps, or call a check none of whose steps
    ran `success`."""
    keep = FOLDED[file][1]
    steps = _steps(file)
    steps["setup-tools"] = "failure"
    assert _verdict(file, steps, tmp_path) == {k: "failure" for k in keep}
    # A job lost before its setup reported anything is no pass either.
    assert _verdict(file, {}, tmp_path) == {k: "failure" for k in keep}
    for unreached in keep:
        steps = _steps(file)
        steps[f"{unreached}-one"] = steps[f"{unreached}-two"] = "skipped"
        got = _verdict(file, steps, tmp_path)
        assert got[unreached] == "skipped", got
    # A check with one step skipped by its own `if` (a cached download, the
    # contract check off a pull request) and the rest passed is a pass.
    steps = _steps(file)
    for job_id in keep:
        steps[f"{job_id}-one"] = "skipped"
    assert _verdict(file, steps, tmp_path) == {k: "success" for k in keep}
