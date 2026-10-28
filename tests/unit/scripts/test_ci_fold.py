"""Sub-minute checks run as steps of one job per workflow, and every check
name they had still reports (CI-FOLD, #1799; docs/ci.md "Sub-minute checks
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

WHERE THE REQUIRED NAMES WERE READ. `gh api` is not available to the lane
that wrote this, so the required names come from this repository:
test_merge_queue_workflows.REQUIRED_CONTEXTS (the ruleset 24160219 read of
2026-10-01, unchanged at 2026-10-06, and the body docs/ci.md gives the
owner), the check names scripts/lib/ci_reuse.py reuses by (REUSABLE), and
ci-gate's `needs`.

WHAT THIS CANNOT PROVE: that GitHub fills `toJSON(steps)` with an entry per
step id carrying `outcome`, and a job's `outputs` after a failed step, in the
shape the fakes below use. That is GitHub's documented behaviour; the first
pull request carrying the workflow change is the live proof.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from .ci_fold_layout import FOLDED, FOLDED_HOSTS, PATCH_NOTE, check_steps, host_job
from .test_ci_reuse import reuse as ci_reuse
from .test_merge_queue_workflows import REQUIRED_CONTEXTS

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
CI_DOC = REPO / "docs" / "ci.md"
HOSTED = "ubuntu-24.04"

#: Jobs that do no work of their own (they read `needs`, or write a fixed
#: summary) and moved to a hosted runner without folding anything.
MOVED_HOSTED = {
    "application.yml": {"python", "ui"},
    "terraform.yml": {"plan-not-run"},
}

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

#: Self-hosted job definitions a same-repository pull request reaching every
#: job runs, per workflow (a matrix counts once: the unit and vitest shards
#: are untouched, so they add the same number before and after).
VMS_BEFORE = {"application.yml": 15, "security.yml": 4, "terraform.yml": 4}
VMS_AFTER = {"application.yml": 10, "security.yml": 3, "terraform.yml": 2}
#: The claim in docs/ci.md and the pull request: this many fewer VMs a run.
FEWER_VMS = 8

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


def _keeper_condition(name: str, job_id: str) -> str:
    """The old job's own condition: the keeper's `if` after `always() && `."""
    condition = str(_jobs(name)[job_id].get("if") or "")
    assert condition == "always()" or condition.startswith("always() && "), f"{job_id}: {condition} {PATCH_NOTE}"
    return condition.removeprefix("always()").removeprefix(" && ")


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
# 2. Each name-keeper runs hosted and fails when its folded step failed.
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
        # never posts a `failure` for it (#2582).
        assert job.get("if") == "${{ !cancelled() }}", (job_id, job.get("if"))
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


def _run_keeper(file: str, job_id: str, verdict: str, folded: str) -> subprocess.CompletedProcess:
    host_job(file)  # names the unapplied patch, rather than failing on the old job's many steps
    (step,) = _jobs(file)[job_id]["steps"]
    env = {**os.environ, "CHECK": step["env"]["CHECK"], "VERDICT": verdict, "FOLDED": folded}
    for leak in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        env.pop(leak, None)
    return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30,
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


@pytest.mark.parametrize("file, job_id", sorted((f, j) for f, ids in MOVED_HOSTED.items() for j in ids))
def test_the_jobs_that_only_read_needs_run_hosted(file, job_id):
    """MUTATION: put `format / unit tests` back on the pool expression, or
    give a moved job a checkout."""
    job = _jobs(file)[job_id]
    assert job.get("runs-on") == HOSTED, f"{file}:{job_id} runs on {job.get('runs-on')!r} {PATCH_NOTE}"
    for step in job.get("steps") or []:
        assert "uses" not in step, (job_id, step)


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


def test_each_application_check_runs_on_its_old_condition_only():
    """A check skipped by reuse, or not reached by the change, must not run
    in the folded job either. MUTATION: drop the `ruff` reuse clause from its
    step's `if`."""
    for job_id in FOLDED["application.yml"][1]:
        condition = _keeper_condition("application.yml", job_id)
        own = f"{ci_reuse.reach_if(job_id)} && !contains(fromJSON(needs.changes.outputs.reused || '[]'), '{job_id}')"
        assert condition == own, (job_id, condition)
        for step in check_steps("application.yml", job_id):
            assert condition in str(step.get("if")), (step["id"], step.get("if"))


def _evaluate(expression: str, app: str, python: str, reused: set[str]) -> bool:
    """The subset of GitHub's expression language these `if:`s use."""
    py = expression.removeprefix("always() && ")
    py = re.sub(r"!contains\(fromJSON\(needs\.changes\.outputs\.reused \|\| '\[\]'\), '([\w-]+)'\)",
                lambda m: repr(m.group(1) not in reused), py)
    py = py.replace("needs.changes.outputs.app", repr(app)).replace("needs.changes.outputs.python", repr(python))
    py = py.replace("&&", " and ").replace("||", " or ")
    assert re.fullmatch(r"[\s()'\w=!.-]+", py), py
    return bool(eval(py))  # noqa: S307 -- built above from literals only


def test_the_folded_job_runs_exactly_when_one_of_its_checks_would():
    """Over every reach and reuse combination. MUTATION: drop the
    `python-checks` arm from `quick-checks`' `if`."""
    host_if = host_job("application.yml")["if"]
    keep = list(FOLDED["application.yml"][1])
    conditions = [_keeper_condition("application.yml", k) for k in keep]
    seen = set()
    for app, python in itertools.product(("true", "false"), ("all", "selected", "subset", "none")):
        for n in range(len(keep) + 1):
            for reused in itertools.combinations(keep, n):
                wanted = any(_evaluate(c, app, python, set(reused)) for c in conditions)
                assert _evaluate(host_if, app, python, set(reused)) == wanted, (app, python, reused)
                seen.add(wanted)
    assert seen == {True, False}, "the control: the job is both run and skipped somewhere"


# --- the verdict step, run -------------------------------------------------
def _verdict(file: str, steps: dict[str, str], tmp_path: Path) -> dict[str, str]:
    step = host_job(file)["steps"][-1]
    out = tmp_path / "out"
    out.write_text("")
    env = {**os.environ, "STEPS": json.dumps({k: {"outcome": v, "conclusion": v, "outputs": {}}
                                              for k, v in steps.items()}),
           "CHECKS": step["env"]["CHECKS"], "GITHUB_OUTPUT": str(out)}
    env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def _all(file: str, outcome: str = "success") -> dict[str, str]:
    return {str(s["id"]): outcome for s in host_job(file)["steps"] if s.get("id") and s["id"] != "verdict"}


def test_the_verdict_rule_is_written_once_in_three_places():
    """MUTATION: fix the jq in one workflow only."""
    runs = {file: host_job(file)["steps"][-1]["run"] for file in FOLDED}
    assert len(set(runs.values())) == 1, runs


@needs_jq
@pytest.mark.parametrize("file", sorted(FOLDED))
def test_the_verdict_names_the_failed_check_and_only_it(file, tmp_path):
    """MUTATION: let a failed step read as `skipped`, or blame every check."""
    keep = FOLDED[file][1]
    assert _verdict(file, _all(file), tmp_path) == {k: "success" for k in keep}
    for failed in keep:
        for bad in ("failure", "cancelled"):
            steps = _all(file)
            steps[str(check_steps(file, failed)[-1]["id"])] = bad
            got = _verdict(file, steps, tmp_path)
            assert got == {k: ("failure" if k == failed else "success") for k in keep}, (failed, bad, got)


@needs_jq
@pytest.mark.parametrize("file", sorted(FOLDED))
def test_a_failed_setup_fails_every_check_and_an_unreached_check_is_skipped(file, tmp_path):
    """MUTATION: ignore the setup steps, or call a check none of whose steps
    ran `success`."""
    keep = FOLDED[file][1]
    steps = _all(file)
    setup = next(k for k in steps if k.startswith("setup-"))
    steps[setup] = "failure"
    assert _verdict(file, steps, tmp_path) == {k: "failure" for k in keep}
    # A job lost before its setup reported anything is no pass either.
    assert _verdict(file, {}, tmp_path) == {k: "failure" for k in keep}
    for unreached in keep:
        steps = _all(file)
        for step in check_steps(file, unreached):
            steps[str(step["id"])] = "skipped"
        got = _verdict(file, steps, tmp_path)
        assert got[unreached] == "skipped", got
    # A check with one step skipped by its own `if` (a cached download, the
    # contract check off a pull request) and the rest passed is a pass.
    if file == "application.yml":
        steps = _all(file)
        steps["workflows-download"] = "skipped"
        steps["python-checks-contract"] = "skipped"
        assert _verdict(file, steps, tmp_path) == {k: "success" for k in keep}


# ---------------------------------------------------------------------------
# 4. The self-hosted job count per pull-request run drops by the claim.
# ---------------------------------------------------------------------------
def _on_the_pool(job: dict) -> bool:
    runs_on = job.get("runs-on")
    return not (isinstance(runs_on, str) and runs_on == HOSTED)


def _pool_jobs_on_a_pull_request(file: str) -> list[str]:
    """Self-hosted jobs a same-repository pull request can start. A job whose
    `if` rules a pull request out is left out; none of today's are on the pool."""
    found = []
    for job_id, job in _jobs(file).items():
        condition = str(job.get("if") or "")
        if "!= 'pull_request'" in condition or ("pull_request" not in condition and "schedule" in condition):
            continue
        if _on_the_pool(job):
            found.append(job_id)
    return found


def test_a_pull_request_run_uses_the_claimed_number_fewer_vms():
    """MUTATION: put a name-keeper back on the pool, or unfold a check."""
    now = {file: len(_pool_jobs_on_a_pull_request(file)) for file in VMS_BEFORE}
    assert now == VMS_AFTER, f"{now} {PATCH_NOTE}"
    assert sum(VMS_BEFORE.values()) - sum(now.values()) == FEWER_VMS, now
    # The heavy jobs are untouched: still on the pool.
    app = _jobs("application.yml")
    for job_id in ("shell", "changes", "python-unit", "ui-build", "ui-tests", "integration", "build-check",
                   "tested-tree", "ci-gate"):
        assert _on_the_pool(app[job_id]), job_id
    assert _on_the_pool(_jobs("security.yml")["iac"]) and _on_the_pool(_jobs("terraform.yml")["checkov"])


def test_the_doc_states_the_counts_this_file_holds():
    """MUTATION: change a count in docs/ci.md, or VMS_AFTER, alone."""
    text = CI_DOC.read_text()
    match = re.search(r"^### Sub-minute checks share a job\n(.*?)(?=^#{2,3} )", text, re.DOTALL | re.MULTILINE)
    assert match, "docs/ci.md has no section `Sub-minute checks share a job`"
    section = match.group(1)
    for file in VMS_BEFORE:
        assert re.search(rf"`{re.escape(file)}`\s*\|\s*{VMS_BEFORE[file]}\s*\|\s*{VMS_AFTER[file]}\s*\|", section), file
    total = rf"\*\*{sum(VMS_BEFORE.values())}\*\*\s*\|\s*\*\*{sum(VMS_AFTER.values())}\*\*"
    assert re.search(total, section), "the totals row"
    assert f"{FEWER_VMS} fewer" in section
    assert "name-keeper" in section and HOSTED in section
