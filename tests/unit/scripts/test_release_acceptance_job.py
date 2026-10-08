"""Post-deploy acceptance: .github/workflows/accept.yml, and the suite's wiring.

Owner decision, 2026-09-29: after every dev deploy, scripts/acceptance/run.sh
dispatches real tasks and asserts what they produced. Owner decision,
2026-10-08 (cuts A and C of the release timing report): that happens in a
workflow of its own, after release.yml completes, outside the `release-dev`
lock, with its groups in parallel. These pin the parts a reviewer cannot see
running:

* release.yml no longer warms, smokes or accepts on dev, and has no
  `acceptance` job; prod still warms and smokes inside its own deploy;
* accept.yml is triggered by `workflow_run` of release (completed, on main)
  and by dispatch. `target` (decide) has no concurrency; only the accepting
  jobs carry `accept-dev-<job>` groups that cancel the run in progress, so a
  trigger that can accept nothing (a failed, superseded or prod release)
  cannot cancel one. A pre-wave job cancels the swarm-verify executions an
  earlier run left running;
* it judges what dev RUNS: the SHA from releases/dev/applied.json, which every
  later job checks out, and on a completed release only when that release is
  the one that applied it;
* every group runs exactly once, through verify-remote.sh, as a matrix with
  fail-fast false, in waves whose peaks fit the smoke tenant's ceiling;
* a red run opens or updates ONE issue labelled bug naming the SHA and the
  failing groups (scripts/accept-report.sh, run here against a fake `gh`);
* every acceptance script is a script this repository accepts: `set -euo
  pipefail` first, executable, and shellcheck-clean where shellcheck exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from .test_acceptance_target import _tenants, _unquote
from .test_hotfix_release import _order, world  # noqa: F401 -- `world` is a fixture
from .test_release_reuses_ci_images import _code, _needs, _workflow

ROOT = Path(__file__).resolve().parents[3]
ACCEPTANCE = ROOT / "scripts" / "acceptance"
REPORT_SH = ROOT / "scripts" / "accept-report.sh"
GROUPS = ("mock", "generic", "claude-code", "workflow", "browser")
#: What is run rather than sourced. lib.sh and the group files are sourced by
#: run.sh and checked through it (-x); on their own they would report every
#: variable common.sh and lib.sh set.
ENTRY_SCRIPTS = ("run.sh", *(f"{g}.sh" for g in GROUPS), "github-cleanup.sh", "github-verify.sh", "sandbox-sync.sh", "parsers.sh")

#: Each group's peak of tasks in flight at once, read from its group file on
#: 2026-10-08: mock submits twelve tasks and a two-step workflow before it
#: waits; generic one task per command row (8); workflow four workflow roots;
#: claude-code and browser three tasks each. accept.yml's header states the
#: same numbers; a group that grows past them should move waves.
PEAK = {"mock": 13, "generic": 8, "workflow": 4, "claude-code": 3, "browser": 3}

ACCEPT_JOBS = ("acceptance", "acceptance-2")


def _accept() -> dict:
    return _workflow("accept.yml")


def _starts_with(a, b) -> bool:
    # GitHub's startsWith is case-insensitive.
    return str(a or "").lower().startswith(str(b or "").lower())


def _gh(expression, **context) -> object:
    """A GitHub expression with `startsWith` as well as the family
    test_release_reuses_ci_images._evaluate reads."""
    body = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", str(expression), re.S)
    text = body.group(1) if body else str(expression)
    for name, value in context.items():
        text = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w.])", repr(value), text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    return eval(  # noqa: S307 -- repository file, test only
        text, {"__builtins__": {}}, {"format": lambda t, *a: str(t).format(*a), "startsWith": _starts_with}
    )


def _run_context(event: str, conclusion: str = "success", run_event: str = "push", title: str = "a commit") -> dict:
    return {
        "github.event_name": event,
        "github.ref": "refs/heads/main",
        "github.run_id": "7",
        "github.event.workflow_run.conclusion": conclusion if event == "workflow_run" else None,
        "github.event.workflow_run.event": run_event if event == "workflow_run" else None,
        "github.event.workflow_run.display_title": title if event == "workflow_run" else None,
    }


# ---------------------------------------------------------------------------
# release.yml: nothing of acceptance is left on dev
# ---------------------------------------------------------------------------


def test_the_release_has_no_acceptance_job_and_does_not_smoke_dev():
    jobs = _workflow("release.yml")["jobs"]
    assert "acceptance" not in jobs, "acceptance moved to accept.yml; release.yml must not hold the lock through it"
    deploy = jobs["deploy"]["steps"]
    for marker in ("warm-jobs.sh", "verify-remote.sh smoke-test", "prove-gke-dispatch"):
        steps = [s for s in deploy if marker in _code(s.get("run", ""))]
        assert len(steps) == 1, f"release.yml's deploy runs {marker!r} {len(steps)} times; prod still needs it once"
        assert "github.event.inputs.environment == 'prod'" in str(steps[0].get("if", "")), (
            f"release.yml's deploy runs {marker!r} on dev; on dev it is accept.yml's"
        )
    code = "\n".join(_code(s.get("run", "")) for job in jobs.values() for s in job.get("steps") or [])
    assert "acceptance/" not in code


def test_a_prod_release_says_so_in_its_run_name():
    """accept.yml tells a prod release from a dev one by this alone."""
    name = str(_workflow("release.yml").get("run-name", ""))
    assert "format('release {0} {1}', inputs.environment" in name, name
    assert "github.event_name == 'workflow_dispatch'" in name, name


# ---------------------------------------------------------------------------
# accept.yml: when it runs, and what it cancels
# ---------------------------------------------------------------------------


def test_it_runs_after_release_completes_on_main_and_by_dispatch():
    on = _accept()["on"]
    assert set(on) == {"workflow_run", "workflow_dispatch"}, on
    assert on["workflow_run"]["workflows"] == ["release"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert on["workflow_run"]["branches"] == ["main"]


ACCEPTING_JOBS = ("quiesce", "smoke", "sandbox", "acceptance", "acceptance-2", "sandbox-results", "report")


def test_the_decide_job_has_no_concurrency_and_the_workflow_has_none_either():
    """MUTATION: put a concurrency block back on the workflow or on `target`.
    A completion that accepts nothing would then cancel a real in-flight
    acceptance and decide accept=false, leaving the deployed SHA unaccepted."""
    accept = _accept()
    assert "concurrency" not in accept, "a workflow-level group is joined by every completion, even one that accepts nothing"
    assert "concurrency" not in accept["jobs"]["target"]


def test_every_accepting_job_cancels_the_run_in_progress_in_a_group_of_its_own():
    jobs = _accept()["jobs"]
    groups = []
    for job_id in ACCEPTING_JOBS:
        concurrency = jobs[job_id].get("concurrency")
        assert concurrency, f"{job_id} is an accepting job and must cancel an older acceptance"
        assert concurrency["cancel-in-progress"] is True
        group = str(concurrency["group"])
        assert group.startswith("accept-dev-"), group
        groups.append(group)
    # Per job, not one shared name: a shared job-level group would make this
    # run's own smoke, sandbox and matrix jobs cancel one another.
    assert len(set(groups)) == len(groups), groups
    assert "matrix.group" in str(jobs["acceptance"]["concurrency"]["group"])
    assert "matrix.group" in str(jobs["acceptance-2"]["concurrency"]["group"])


@pytest.mark.parametrize(
    "context",
    [
        _run_context("workflow_run", conclusion="failure"),
        _run_context("workflow_run", conclusion="cancelled"),
        _run_context("workflow_run", run_event="workflow_dispatch", title="release prod main"),
    ],
    ids=["failed-release", "cancelled-release", "prod-release"],
)
def test_a_trigger_that_accepts_nothing_runs_no_accepting_job(context):
    assert not _gh(_accept()["jobs"]["target"]["if"], **context)


def test_every_accepting_job_runs_only_when_the_decision_is_true():
    jobs = _accept()["jobs"]
    for job_id in ("quiesce", "smoke", "sandbox", *ACCEPT_JOBS):
        assert "needs.target.outputs.accept == 'true'" in str(jobs[job_id]["if"]), job_id


def test_a_push_whose_headline_reads_like_a_prod_dispatch_is_still_accepted():
    context = _run_context("workflow_run", run_event="push", title="release prod notes for the runbook")
    assert _gh(_accept()["jobs"]["target"]["if"], **context)


@pytest.mark.parametrize("event", ["workflow_run", "workflow_dispatch"])
def test_it_runs_only_on_main(event):
    context = {**_run_context(event), "github.ref": "refs/heads/feature"}
    assert not _gh(_accept()["jobs"]["target"]["if"], **context)
    assert _gh(_accept()["jobs"]["target"]["if"], **_run_context(event))


def test_it_names_no_environment_and_never_prod():
    for job_id, job in _accept()["jobs"].items():
        assert "environment" not in job, f"accept.yml's {job_id} names an environment"
    assert "prod" not in str(_accept().get("env")), "accept.yml is dev only"
    assert _accept()["env"]["ENVIRONMENT"] == "dev"


# ---------------------------------------------------------------------------
# accept.yml: it judges exactly what is deployed
# ---------------------------------------------------------------------------


def test_it_reads_the_deployed_sha_from_applied_json_and_checks_it_out():
    jobs = _accept()["jobs"]
    target = jobs["target"]
    runs = [_code(s.get("run", "")) for s in target["steps"]]
    assert any(re.search(r"release-order\.sh deployed --environment", r) for r in runs), runs
    assert "steps.deployed.outputs.sha" in str(target["outputs"]["sha"])
    for job_id in ("smoke", "sandbox", *ACCEPT_JOBS, "sandbox-results"):
        checkout = next(s for s in jobs[job_id]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        assert checkout.get("with", {}).get("ref") == "${{ needs.target.outputs.sha }}", (
            f"accept.yml's {job_id} does not check out the deployed commit"
        )
        assert "target" in _upstream_ids(jobs, job_id)


def _upstream_ids(jobs: dict, job_id: str) -> set[str]:
    seen, todo = set(), list(_needs(jobs[job_id]))
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(_needs(jobs[n]))
    return seen


def _decide(
    tmp_path: Path, event: str, applied_run: str, trigger_run: str, trigger_sha: str = "b" * 40
) -> tuple[str, str]:
    step = next(s for s in _accept()["jobs"]["target"]["steps"] if s.get("id") == "decide")
    out, summary = tmp_path / "out", tmp_path / "summary"
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_EVENT_NAME": event,
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "SHA": "a" * 40,
        "APPLIED_RUN": applied_run,
        "APPLIED_BY": "release",
        "TRIGGER_RUN": trigger_run,
        "TRIGGER_SHA": trigger_sha,
    }
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return out.read_text(), summary.read_text()


def test_a_completed_release_is_accepted_only_when_it_applied_what_dev_runs(tmp_path):
    """A release a newer commit superseded applied nothing, and a prod release
    records nothing for dev: neither may accept the deployment another run
    made. MUTATION: drop the APPLIED_RUN comparison from the decide step."""
    out, summary = _decide(tmp_path, "workflow_run", "41", "41")
    assert out == "accept=true\n"
    assert "a" * 40 in summary, "the summary must name the SHA it accepts"
    out, _ = _decide(tmp_path, "workflow_run", "40", "41")
    assert out.endswith("accept=false\n")


def test_a_hotfix_re_applying_the_same_sha_is_accepted_though_the_run_id_differs(tmp_path):
    """applied.json names the hotfix run (40), the completed release run (41)
    built the very commit dev runs. MUTATION: drop the SHA comparison from the
    decide step; the deployed SHA would then be accepted by nobody."""
    out, _ = _decide(tmp_path, "workflow_run", "40", "41", trigger_sha="a" * 40)
    assert out == "accept=true\n"
    out, _ = _decide(tmp_path, "workflow_run", "40", "41", trigger_sha="c" * 40)
    assert out.endswith("accept=false\n")


def test_a_dispatch_accepts_whatever_dev_runs(tmp_path):
    out, _ = _decide(tmp_path, "workflow_dispatch", "40", "")
    assert out == "accept=true\n"


def test_release_order_deployed_prints_the_applied_record(world):  # noqa: F811 -- the fixture
    proc = _order(world, "deployed", "--environment", "dev")
    assert proc.returncode != 0, "no record must be an error: acceptance of nothing would be green"
    assert "does not exist" in proc.stderr
    sha = world.shas["B"]
    assert _order(world, "record", "--environment", "dev", "--what", "applied", "--sha", sha).returncode == 0
    proc = _order(world, "deployed", "--environment", "dev")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"sha={sha}\nrun_id=42\nworkflow=release\n"


# ---------------------------------------------------------------------------
# accept.yml: warm, smoke and the suite
# ---------------------------------------------------------------------------


def test_it_warms_before_it_smokes_and_proves_gke():
    steps = _accept()["jobs"]["smoke"]["steps"]
    runs = [_code(s.get("run", "")) for s in steps]
    warm = next(i for i, r in enumerate(runs) if "warm-jobs.sh" in r)
    smoke = next(i for i, r in enumerate(runs) if "verify-remote.sh smoke-test" in r)
    gke = next(i for i, r in enumerate(runs) if "verify-remote.sh prove-gke-dispatch" in r)
    assert warm < smoke < gke
    assert steps[warm].get("continue-on-error") is True, "a failed warm run is a slow first task, not a red acceptance"
    assert "!cancelled()" in str(steps[gke].get("if", "")), "the GKE proof runs after a failed smoke, not after a cancel"


def _groups(job: dict) -> list[str]:
    return list(job["strategy"]["matrix"]["group"])


def test_every_group_runs_exactly_once_through_verify_remote_with_fail_fast_off():
    jobs = _accept()["jobs"]
    seen: list[str] = []
    for job_id in ACCEPT_JOBS:
        job = jobs[job_id]
        assert job["strategy"]["fail-fast"] is False, f"{job_id}: one red group must not cancel the others"
        runs = [_code(s.get("run", "")) for s in job["steps"] if "verify-remote.sh" in _code(s.get("run", ""))]
        assert runs == ['./scripts/verify-remote.sh "acceptance/${GROUP}"'], runs
        assert any(s.get("env", {}).get("GROUP") == "${{ matrix.group }}" for s in job["steps"])
        seen += _groups(job)
    assert sorted(seen) == sorted(GROUPS), f"accept.yml runs {seen}, the suite has {GROUPS}"
    for target in seen:
        wrapper = ACCEPTANCE / f"{target}.sh"
        assert wrapper.is_file(), f"verify-remote.sh would run scripts/acceptance/{target}.sh, which does not exist"
        assert f"--only {target}" in wrapper.read_text()


def test_each_wave_fits_the_smoke_tenant_s_ceiling():
    """Two waves, because five at once would peak at ~31 tasks against a
    ceiling of 20 (owner decision 2026-10-08, cut C: "if they do not fit, run
    them in two waves"). MUTATION: move `generic` into the first wave."""
    smoke = _tenants()["smoke"]
    ceiling = min(int(_unquote(smoke["max_active"])), int(_unquote(smoke["capacity_units"])))
    assert sum(PEAK.values()) > ceiling, "all five fit at once now: run them in one wave"
    for job_id in ACCEPT_JOBS:
        groups = _groups(_accept()["jobs"][job_id])
        peak = sum(PEAK[g] for g in groups)
        assert peak <= ceiling, f"{job_id} runs {groups}, a peak of {peak} tasks against smoke's ceiling of {ceiling}"


def test_the_second_wave_follows_the_first_whatever_it_concluded():
    jobs = _accept()["jobs"]
    second = jobs["acceptance-2"]
    assert "acceptance" in _needs(second)
    condition = str(second["if"])
    assert "!cancelled()" in condition
    assert "needs.acceptance.result" not in condition, "a red first wave must not hide the second wave's own result"


@pytest.mark.parametrize("job_id", ACCEPT_JOBS)
def test_acceptance_starts_only_after_smoke_and_the_sandbox_sync_succeeded(job_id):
    job = _accept()["jobs"][job_id]
    assert {"smoke", "sandbox"} <= set(_needs(job))
    condition = str(job["if"])
    assert "needs.smoke.result == 'success'" in condition
    assert "needs.sandbox.result == 'success'" in condition
    assert "needs.target.outputs.accept == 'true'" in condition


def test_the_sandbox_is_synced_once_and_read_back_and_swept_after_both_waves():
    jobs = _accept()["jobs"]
    sync = [j for j, job in jobs.items() if any("sandbox-sync.sh" in _code(s.get("run", "")) for s in job["steps"])]
    assert sync == ["sandbox"], f"the fixtures are written by {sync}; two writers race on one branch"
    results = jobs["sandbox-results"]
    assert set(ACCEPT_JOBS) <= set(_needs(results))
    assert "!cancelled()" in str(results["if"]), "the sweep must run after a red group"
    steps = results["steps"]
    verify = next(i for i, s in enumerate(steps) if "github-verify.sh" in _code(s.get("run", "")))
    sweep = next(i for i, s in enumerate(steps) if "github-cleanup.sh" in _code(s.get("run", "")))
    assert verify < sweep
    assert "!cancelled()" in str(steps[sweep].get("if", "")), "the sweep must run after a failed read-back"
    for step in (steps[verify], steps[sweep]):
        assert "secrets.SWARM_SANDBOX_GITHUB_TOKEN" in str(step.get("env", {}).get("SWARM_ACCEPTANCE_GITHUB_TOKEN", ""))
    assert "needs.sandbox.outputs.since" in str(steps[verify].get("env", {}).get("SWARM_ACCEPTANCE_SINCE", ""))


def test_the_sandbox_token_never_reaches_the_suite():
    jobs = _accept()["jobs"]
    for job_id in ("smoke", *ACCEPT_JOBS):
        assert "SWARM_SANDBOX_GITHUB_TOKEN" not in json.dumps(jobs[job_id]), f"{job_id} is handed the sandbox token"


# ---------------------------------------------------------------------------
# accept.yml: a red run is an issue
# ---------------------------------------------------------------------------


def test_the_report_needs_every_job_and_files_an_issue_on_red():
    jobs = _accept()["jobs"]
    report = jobs["report"]
    assert set(_needs(report)) == set(jobs) - {"report"}, "the report must see every job's result"
    condition = str(report["if"])
    assert "!cancelled()" in condition and "always()" not in condition
    assert "needs.target.result == 'failure'" in condition, "an identity that cannot authenticate is a red acceptance too"
    assert report["permissions"].get("issues") == "write"
    assert report["permissions"].get("actions") == "read"
    step = next(s for s in report["steps"] if "accept-report.sh" in _code(s.get("run", "")))
    assert step["env"]["ACCEPT_SHA"] == "${{ needs.target.outputs.sha }}"
    assert step["env"]["ACCEPT_NEEDS"] == "${{ toJSON(needs) }}"
    writers = [j for j, job in jobs.items() if (job.get("permissions") or {}).get("issues") == "write"]
    assert writers == ["report"], writers


FAKE_GH = r"""#!{python}
import json, os, sys
log = os.environ["FAKE_GH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[0] == "api":
    if os.environ.get("FAKE_GH_API_FAIL"):
        sys.exit(1)
    sys.stdout.write(os.environ.get("FAKE_GH_FAILED_JOBS", ""))
elif args[:2] == ["issue", "list"]:
    if os.environ.get("FAKE_GH_LIST_FAIL"):
        sys.stderr.write("HTTP 502\n"); sys.exit(1)
    sys.stdout.write(os.environ.get("FAKE_GH_OPEN", ""))
elif args[:2] == ["issue", "create"]:
    body = open(args[args.index("--body-file") + 1]).read()
    open(os.environ["FAKE_GH_LOG"] + ".body", "w").write(body)
    sys.stdout.write("https://github.com/o/r/issues/900\n")
elif args[:2] in (["issue", "edit"], ["issue", "comment"]):
    if "--body-file" in args:
        open(os.environ["FAKE_GH_LOG"] + ".comment", "w").write(open(args[args.index("--body-file") + 1]).read())
else:
    sys.stderr.write(f"fake gh: {args} is not modelled\n"); sys.exit(2)
"""

SHA = "c0ffee" + "0" * 34
GREEN = {"target": {"result": "success"}, "smoke": {"result": "success"}, "acceptance": {"result": "success"},
         "acceptance-2": {"result": "success"}, "sandbox": {"result": "success"}, "sandbox-results": {"result": "success"}}
RED = {**GREEN, "acceptance": {"result": "failure"}}


def _report(tmp_path: Path, needs: dict, sha: str = SHA, now: str = "", **env: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "gh.log"
    summary = tmp_path / "summary.md"
    proc = subprocess.run(
        [str(REPORT_SH)],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "ACCEPT_", "GH_"))},
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "NO_COLOR": "1",
            "FAKE_GH_LOG": str(log),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "77",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_STEP_SUMMARY": str(summary),
            "ACCEPT_SHA": sha,
            "ACCEPT_NOW_SHA": now,
            "ACCEPT_NEEDS": json.dumps(needs),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return proc, calls, summary.read_text() if summary.exists() else "", log


def _issue_calls(calls):
    return [c for c in calls if c[0] == "issue" and c[1] in ("create", "edit", "comment")]


def test_a_green_run_names_its_sha_and_files_nothing(tmp_path):
    proc, calls, summary, _ = _report(tmp_path, GREEN)
    assert proc.returncode == 0, proc.stderr
    assert SHA in summary
    assert _issue_calls(calls) == []


def test_a_red_run_opens_one_bug_issue_naming_the_sha_and_the_failing_group(tmp_path):
    """MUTATION: drop `--label bug` from the create, or name the wave rather
    than the group the job list gives."""
    proc, calls, summary, log = _report(tmp_path, RED, FAKE_GH_FAILED_JOBS="acceptance (mock)\nacceptance (browser)\n")
    assert proc.returncode == 0, proc.stderr
    created = [c for c in calls if c[:2] == ["issue", "create"]]
    assert len(created) == 1, calls
    create = created[0]
    assert create[create.index("--label") + 1] == "bug"
    title = create[create.index("--title") + 1]
    assert SHA[:12] in title and "acceptance (mock), acceptance (browser)" in title, title
    body = Path(str(log) + ".body").read_text()
    assert "<!-- swarm-accept: dev acceptance is red -->" in body
    assert SHA in body and "acceptance (mock)" in body
    # The bug form's sections, in its order (CLAUDE.md, "Issues").
    sections = re.findall(r"(?m)^### (.+)$", body)
    assert sections == ["What happened", "Severity", "Where you saw it", "Environment", "Steps to reproduce",
                        "What you expected instead", "Identifiers", "Screenshots, errors, logs", "Anything else"]
    assert "opened" in summary


def test_a_red_run_updates_the_open_issue_rather_than_opening_another(tmp_path):
    proc, calls, _, log = _report(tmp_path, RED, FAKE_GH_OPEN="512\n", FAKE_GH_FAILED_JOBS="acceptance (generic)\n")
    assert proc.returncode == 0, proc.stderr
    kinds = [c[1] for c in _issue_calls(calls)]
    assert kinds == ["edit", "comment"], calls
    assert all(c[2] == "512" for c in _issue_calls(calls))
    edit = next(c for c in calls if c[:2] == ["issue", "edit"])
    assert "acceptance (generic)" in edit[edit.index("--title") + 1]
    assert SHA in Path(str(log) + ".comment").read_text()
    listing = next(c for c in calls if c[:2] == ["issue", "list"])
    assert listing[listing.index("--label") + 1] == "bug" and listing[listing.index("--state") + 1] == "open"


def test_the_failing_jobs_come_from_needs_when_the_job_list_cannot_be_read(tmp_path):
    proc, calls, _, _ = _report(tmp_path, RED, FAKE_GH_API_FAIL="1")
    assert proc.returncode == 0, proc.stderr
    create = next(c for c in calls if c[:2] == ["issue", "create"])
    assert create[create.index("--title") + 1].endswith(": acceptance")


def test_a_red_run_on_a_deployment_that_is_gone_is_not_filed(tmp_path):
    proc, calls, summary, _ = _report(tmp_path, RED, now="d" * 40)
    assert proc.returncode == 0, proc.stderr
    assert _issue_calls(calls) == []
    assert "d" * 40 in summary


def test_a_target_that_could_not_read_what_dev_runs_is_filed_too(tmp_path):
    needs = {"target": {"result": "failure"}, **{k: {"result": "skipped"} for k in GREEN if k != "target"}}
    proc, calls, _, log = _report(tmp_path, needs, sha="", FAKE_GH_FAILED_JOBS="what dev runs\n")
    assert proc.returncode == 0, proc.stderr
    create = next(c for c in calls if c[:2] == ["issue", "create"])
    assert "what dev runs" in create[create.index("--title") + 1]


def test_a_red_run_that_cannot_be_filed_fails_loudly(tmp_path):
    proc, calls, _, _ = _report(tmp_path, RED, FAKE_GH_LIST_FAIL="1")
    assert proc.returncode != 0
    assert "NOT filed" in proc.stderr
    assert _issue_calls(calls) == []


def test_the_report_script_is_a_script_this_repository_accepts():
    text = REPORT_SH.read_text()
    first = next(l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    assert first == "set -euo pipefail"
    assert os.access(REPORT_SH, os.X_OK)


def test_list_names_every_group_and_its_checks_without_touching_anything():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "SWARM_"))}
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        ["bash", str(ACCEPTANCE / "run.sh"), "--list"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    listed = [line for line in result.stdout.splitlines() if line and not line.startswith(" ")]
    assert tuple(listed) == GROUPS
    checks = [line.strip() for line in result.stdout.splitlines() if line.startswith("  ")]
    for group in GROUPS:
        assert any(c.startswith(f"{group}: ") for c in checks), f"{group} lists no checks"
    assert len(checks) == len(set(checks)), "two checks share a name"


def test_an_unknown_group_is_refused():
    result = subprocess.run(
        ["bash", str(ACCEPTANCE / "run.sh"), "--only", "nonsense", "--list"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "unknown group" in result.stderr


def test_a_mid_run_crash_still_exits_nonzero_and_reports_how_far_it_got():
    """PR #358's first live run against dev crashed on an unbound variable
    mid-suite and still exited 0: acc_cleanup (cancel_all + rm -rf), which the
    EXIT trap ran on the way out, itself succeeded, and nothing captured the
    crash's own exit status before that -- so the trap's LAST command's status
    (0) became the whole script's. `--only _selftest_crash` runs a group that
    exists for exactly this: it deliberately references an unset, required
    variable (`${var:?...}`) after recording one check, and does no platform
    work, so this needs no deployment and no credentials.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "SWARM_"))}
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        ["bash", str(ACCEPTANCE / "run.sh"), "--only", "_selftest_crash"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0, "a crashed run must not exit 0: " + result.stderr
    assert "did not finish" in result.stderr, result.stderr
    assert "1 of 1 checks visited" in result.stderr, result.stderr


def _scripts() -> list[Path]:
    return sorted(ACCEPTANCE.glob("*.sh")) + sorted((ACCEPTANCE / "groups").glob("*.sh"))


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_acceptance_script_starts_with_set_euo_pipefail(script):
    first = next(
        line.strip()
        for line in script.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert first == "set -euo pipefail", f"{script}: first effective line is {first!r}"


@pytest.mark.parametrize("script", [ACCEPTANCE / n for n in ENTRY_SCRIPTS], ids=lambda p: p.name)
def test_every_entry_script_is_executable(script):
    assert os.access(script, os.X_OK), f"{script} is not executable; verify-remote.sh runs it directly"


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_script_defaults_a_jq_argument_to_the_escaped_brace_literal(script):
    """`"${1:-{\\}}"` is what PR #358's first live run hit: inside double
    quotes, bash 3.2 (macOS, the version these scripts must support) does not
    strip the backslash, so the "default empty object" is the literal,
    invalid JSON text `{\\}` rather than `{}`. jq then refuses it with
    "invalid JSON text passed to --argjson", the caller's argument silently
    turns into nothing, and the API 422s with "Field required". Every
    default must instead come from a variable (`local empty='{}'` then
    `"${1:-$empty}"`) or an explicit `[ -n "$1" ] || set -- '{}'`.
    """
    text = script.read_text()
    assert ":-{\\" not in text, f"{script}: bash 3.2 will not strip the backslash in \"${{1:-{{\\}}}}\" -- use a variable default instead"


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_acceptance_script_is_syntactically_valid_bash(script):
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"{script}: bash -n failed:\n{result.stderr}"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed here")
def test_the_acceptance_scripts_are_shellcheck_clean():
    result = subprocess.run(
        # The ENTRY scripts, with -x: they source lib.sh, parsers.sh and every
        # group, so all of it is checked in the context it runs in. A group
        # file checked on its own would report every variable lib.sh sets.
        ["shellcheck", "-x", *[str(ACCEPTANCE / name) for name in ENTRY_SCRIPTS]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# accept.yml: a GitHub cancel does not stop Cloud Run
# ---------------------------------------------------------------------------


def _step_run(job_id: str, name_part: str) -> str:
    step = next(s for s in _accept()["jobs"][job_id]["steps"] if name_part in str(s.get("name", "")))
    return _code(step["run"])


def test_the_pre_wave_step_cancels_the_running_swarm_verify_executions():
    """MUTATION: remove the cancel. A replacement run's wave 1 would overlap
    the cancelled run's tasks in the smoke tenant (ceiling 20)."""
    run = _step_run("quiesce", "swarm-verify executions still running")
    assert "gcloud run jobs executions list --job swarm-verify" in run
    assert "status.runningCount>0" in run
    assert "gcloud run jobs executions cancel" in run


def test_the_pre_wave_job_also_cancels_the_other_accept_runs_and_gates_the_rest():
    jobs = _accept()["jobs"]
    run = _step_run("quiesce", "other in-progress accept runs")
    assert "gh run cancel" in run and "accept.yml" in run
    assert jobs["quiesce"]["permissions"].get("actions") == "write"
    for job_id in ("smoke", "sandbox"):
        assert "quiesce" in _needs(jobs[job_id]), f"{job_id} can start before the old executions are stopped"


@pytest.mark.parametrize("job_id", ["smoke", "acceptance", "acceptance-2"])
def test_a_job_that_runs_the_suite_cancels_its_executions_when_cancelled(job_id):
    steps = _accept()["jobs"][job_id]["steps"]
    cleanup = [s for s in steps if "gcloud run jobs executions cancel" in _code(s.get("run", ""))]
    assert len(cleanup) == 1, job_id
    assert "cancelled()" in str(cleanup[0]["if"])


def test_the_acceptance_account_may_cancel_executions():
    tf = (ROOT / "terraform" / "bootstrap" / "acceptance.tf").read_text()
    runner = tf[tf.index('resource "google_project_iam_custom_role" "acceptance_runner"') : tf.index('"acceptance_lister"')]
    assert '"run.executions.cancel"' in runner
