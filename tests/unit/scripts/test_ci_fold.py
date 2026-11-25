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
that guard the required checks and the verdict (job names, the keepers' runs-on,
needs and `if:`, step ids) stay; the VM counts, the reach and reuse conditions
and their docs/ci.md table were deleted by issue 71.

WHAT THIS CANNOT PROVE: that GitHub fills `toJSON(steps)` with an entry per
step id carrying `outcome`, and a job's `outputs` after a failed step, in the
shape the fakes below use. That is GitHub's documented behaviour; the first
pull request carrying the workflow change is the live proof.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from .ci_fold_layout import PATCH_NOTE
from .ci_run_blocks import run_block
from .test_ci_reuse import reuse as ci_reuse
from .test_merge_queue_workflows import REQUIRED_CONTEXTS, without_lane_clause

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
HOSTED = "ubuntu-24.04"

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

#: The folded job of each workflow, by file name.
FOLDED_HOSTS = {name: host for name, (host, _) in FOLDED.items()}

#: Every check name these three workflows reported on a pull request before
#: CI-FOLD, read from main at a5831dd. Each must still be a job's name.
NAMES_BEFORE = {
    "application.yml": {
        "shellcheck", "release workflow wiring (actionlint)", "ruff check", "what this change reaches",
        "lock / contract / format / bridge install", "format / unit tests", "swarm-ui typecheck / build",
        "swarm-ui typecheck / component tests", "integration tests (emulator)", "kubernetes manifests",
        "build changed images without pushing", "record the tested tree", "ci-gate",
    },
    "security.yml": {
        "trivy (repo)", "secret scan", "checkov (terraform + kubernetes)", "platform policy assertions",
    },
    "terraform.yml": {
        "fmt / validate / tflint", "terraform test", "plan (not run on a pull request)", "checkov",
    },
}

needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None, reason="the verdict step needs bash and jq"
)


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    if True in data:
        data["on"] = data.pop(True)
    return data


def _jobs(name: str) -> dict:
    return _workflow(name)["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def host_job(name: str) -> dict:
    host = FOLDED_HOSTS[name]
    jobs = _jobs(name)
    assert host in jobs, f"{name} has no `{host}` job {PATCH_NOTE}"
    return jobs[host]


def check_steps(name: str, job_id: str) -> list[dict]:
    """The steps that run old job `job_id`'s check, in its folded job."""
    steps = [s for s in host_job(name).get("steps") or [] if str(s.get("id", "")).startswith(f"{job_id}-")]
    assert steps, f"{name}: no `{job_id}-*` step in `{FOLDED_HOSTS[name]}` {PATCH_NOTE}"
    return steps


# ---------------------------------------------------------------------------
# 0. Every required check name still exists as a job name.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 1. Every required check name still exists as a job name.
# ---------------------------------------------------------------------------
def test_every_required_check_name_is_still_a_job_name():
    """The ruleset's five contexts, every name ci_reuse.py reuses by, and the
    name of every job ci-gate needs. MUTATION: rename `secrets` (`secret
    scan`) or drop the `ruff` name-keeper."""
    names = {
        file: {str(job.get("name") or job_id) for job_id, job in _jobs(file).items()}
        for file in ("application.yml", "security.yml", "terraform.yml")
    }
    everywhere = set().union(*names.values())
    missing = set(REQUIRED_CONTEXTS) - everywhere
    assert not missing, f"required by ruleset 24160219 and reported by no job: {sorted(missing)}"
    # Each required context is reported by exactly one job, so no second job
    # of the same name can pass it while the real one fails.
    for context in REQUIRED_CONTEXTS:
        count = sum(str(j.get("name")) == context for f in names for j in _jobs(f).values())
        assert count == 1, (context, count)
    app = _jobs("application.yml")
    for key, reused_name in ci_reuse.REUSABLE.items():
        assert key in app, key
        assert str(app[key]["name"]).startswith(reused_name), (key, app[key]["name"])
    for need in _needs(app["ci-gate"]):
        assert need in app, f"ci-gate needs `{need}`, which is not a job"


@pytest.mark.parametrize("name", sorted(NAMES_BEFORE))
def test_every_check_name_from_before_the_fold_still_reports(name):
    """MUTATION: drop the `test` name-keeper from terraform.yml."""
    names = {str(job.get("name") or job_id) for job_id, job in _jobs(name).items()}
    assert NAMES_BEFORE[name] <= names, f"{sorted(NAMES_BEFORE[name] - names)} {PATCH_NOTE}"


# ---------------------------------------------------------------------------
# 1. Each name-keeper fails when its folded check did not pass.
# ---------------------------------------------------------------------------
KEEPERS = [(file, job_id, check) for file, (_, keep) in FOLDED.items() for job_id, check in keep.items()]


@pytest.mark.parametrize("file, job_id, check", KEEPERS)
def test_a_name_keeper_runs_hosted_and_reads_only_its_own_verdict(file, job_id, check):
    """MUTATION: put a keeper back on the pool expression, drop `always()`
    (skipped because the folded job failed reads as passed), point it at
    another check's verdict, or give it a checkout."""
    job = _jobs(file)[job_id]
    host = FOLDED_HOSTS[file]
    host_job(file)
    assert job.get("name") == check, (job_id, job.get("name"))
    assert job.get("runs-on") == HOSTED, f"{file}:{job_id} runs on {job.get('runs-on')!r} {PATCH_NOTE}"
    assert host in _needs(job), (job_id, _needs(job))
    if file == "security.yml":
        # A required check: `!cancelled()`, so a run concurrency cancelled
        # never posts a `failure` for it (archived issue 2582).
        # (Then the lane clause, test_fast_checks_on_lane_prs.py.)
        assert without_lane_clause(job.get("if")) == "${{ !cancelled() }}", (job_id, job.get("if"))
    else:
        assert str(job.get("if") or "").startswith("always()"), (job_id, job.get("if"))
    (step,) = job["steps"]
    assert "uses" not in step, step
    env = step.get("env") or {}
    assert env.get("VERDICT") == "${{ needs.%s.outputs.%s }}" % (host, job_id), env
    assert env.get("CHECK") == check, env
    assert "${{" not in step["run"], "values reach the shell through env:, never inside run:"
    # The folded job publishes that verdict under the keeper's own id.
    outputs = host_job(file).get("outputs") or {}
    assert outputs.get(job_id) == "${{ steps.verdict.outputs.%s }}" % job_id, outputs


# ---------------------------------------------------------------------------
# 3. The folded job carries a step for each folded check.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("file, job_id, check", KEEPERS)
def test_the_folded_job_has_steps_named_after_each_old_job(file, job_id, check):
    """MUTATION: drop the `ruff check · ruff check` step, or name a step
    without its old job's name."""
    for step in check_steps(file, job_id):
        assert str(step.get("name") or "").startswith(f"{check} · "), (job_id, step.get("name"))
        # One check failing never hides another's answer.
        condition = str(step.get("if") or "")
        assert condition == "always()" or condition.startswith("${{ !cancelled()"), (step["id"], condition)


@pytest.mark.parametrize("file", sorted(FOLDED))
def test_every_folded_step_belongs_to_one_check_or_the_shared_setup(file):
    """The verdict reads step ids; a step outside them would fail the job
    with no name-keeper saying which. MUTATION: add a step with no id."""
    host, keep = FOLDED[file]
    prefixes = tuple(f"{job_id}-" for job_id in keep)
    steps = host_job(file)["steps"]
    for step in steps:
        sid = str(step.get("id") or "")
        assert sid.startswith("setup-") or sid.startswith(prefixes) or sid == "verdict", (file, step)
    # No id is a prefix of another check's, so a step is read by one verdict.
    for a, b in itertools.permutations(keep, 2):
        assert not f"{b}-".startswith(f"{a}-"), (a, b)
    assert steps[-1].get("id") == "verdict" and steps[-1].get("if") == "always()", steps[-1]
    assert set(steps[-1]["env"]["CHECKS"].split()) == set(keep), steps[-1]["env"]
    # The shared setup comes first, before any check's step.
    kinds = ["setup" if str(s.get("id", "")).startswith("setup-") else "other" for s in steps]
    assert kinds[0] == "setup" and "setup" not in kinds[kinds.index("other"):], kinds


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


def test_the_verdict_rule_is_written_once_in_three_places():
    """MUTATION: fix the jq in one workflow only."""
    runs = {file: host_job(file)["steps"][-1]["run"] for file in FOLDED}
    assert len(set(runs.values())) == 1, runs


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
