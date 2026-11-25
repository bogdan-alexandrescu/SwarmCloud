"""`ci-gate` is the one check that stands for application.yml and terraform.yml.

SINCE 2026-10-07 IT IS THE LAST JOB OF application.yml, not a workflow of its
own (owner request: ci-gate.yml held a runner for the whole of CI while it
polled). It `needs:` every other job there but the two that only keep an old
check name, and judges their results itself
(test_unit_shards.py runs that step), then runs scripts/ci-gate.sh for
terraform.yml alone -- `CI_GATE_WORKFLOWS`, which the tests below set as the
gate job does, so the script is tested in the configuration CI runs it in.

WHY IT EXISTS. Owner decision, 2026-09-29: main's ruleset (main-protection,
looked up by name) requires only security.yml's four checks, because application.yml
and terraform.yml carry a `pull_request` path filter, and GitHub treats a
required check whose workflow was filtered out as PENDING FOREVER, not as
passed. So nothing required held a pull request on its unit tests. `ci-gate`
runs on every pull request and every push to main, waits for the runs of those
two workflows at the same head commit, and passes only when every one of them
that ran passed. It is what the ruleset can require in their place.

The properties asserted here, against the REAL script with a fake `gh` and
workflow files written under tmp_path (CI_GATE_WORKFLOW_DIR):

  * the path lists come from the workflow files themselves -- the script reads
    them, and what it reads equals what PyYAML reads -- so there is no second
    copy to drift (CLAUDE.md: the mirrored copy is the defect);
  * GitHub's glob semantics: `**` crosses directories, `*` does not, and a
    root-level name matches only at the root;
  * a workflow the changed paths should trigger is WAITED FOR, including
    before its run exists (the race at the start of every pull request), and
    one that never appears fails the gate by name;
  * a workflow the paths did not trigger counts as passed;
  * a failed job, a cancelled run and a run that failed to start each fail it;
  * a `skipped` job counts as passed only inside a run that succeeded -- a
    skip its own `if:` chose -- never inside a run that failed or never started;
  * a run the paths did not predict, which appears late and fails, still
    fails the gate (the settle window);
  * runs of another event at the same commit are not this gate's business;
  * an unreadable API is never read as a pass;
  * the gate job's own judgement of its `needs`, run from its published shell
    (scripts/generated/ci-run-blocks.json).

The pins on the workflow's shape and on docs/ci.md's ruleset body were
deleted by issue 71.

WHAT THIS CANNOT PROVE: that GitHub's API answers in the shape the fake serves
(workflow runs filtered by head_sha and event, each carrying its workflow
`path`; a run's jobs with `filter=latest`). Only a real pull request exercises
the real API -- this pull request's own `ci-gate` run is the first.

THE TESTS THAT READ .github/ hold CI security decisions the owner kept on
2026-11-25: that ci-gate needs every other job of its workflow, holds read
permissions only, and gates every path-filtered workflow. This file is on
scripts/check_test_reads.py's SECURITY_GUARDS list for those tests alone
(issue 71).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .ci_run_blocks import run_block

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "ci-gate.sh"
OWNER_REPO = "owner/swarm"
SHA = "c0ffee" * 6 + "abcd"
#: What the gate job hands the script (its `wait` step's env).
GATED_IN_CI = "terraform.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the gate needs bash and jq",
)

#: Workflow files the script reads, in the shapes it models: a path-filtered
#: pull_request (with a comment between items, the case most parsers get
#: wrong), an unfiltered one, and one with no push trigger.
FIXTURE_WORKFLOWS = {
    "terraform.yml": """name: terraform
on:
  push:
    branches: [main]
  pull_request:
    paths:
      - "terraform/**"
      - "tests/terraform/**"
      # the plan guard terraform.yml's checks run
      - "scripts/lib/plan-guard.sh"
      - "scripts/lib/destroy-guard.jq"
      - ".github/workflows/terraform.yml"
  merge_group:
    types: [checks_requested]
  workflow_dispatch:
jobs:
  offline:
    runs-on: ubuntu-24.04
    steps:
      - run: "true"
""",
    "application.yml": """name: application
on:
  pull_request:
  push:
    branches: [main]
  merge_group:
jobs:
  ci-gate:
    runs-on: ubuntu-24.04
    steps:
      - run: "true"
""",
    "security.yml": """name: security
on:
  pull_request:
  schedule:
    - cron: "0 5 * * *"
jobs:
  checks:
    runs-on: ubuntu-24.04
    steps:
      - run: "true"
""",
}

#: terraform.yml's jobs as a pull request runs them: `plan` refuses
#: pull_request by its `if:` and is skipped.
TF_JOBS = {
    "offline checks (fmt / validate / tflint, terraform test)": "success",
    "fmt / validate / tflint": "success",
    "terraform test": "success",
    "plan (not run on a pull request)": "success",
    "checkov": "success",
    "plan (dev)": "skipped",
}


def _workflow_dir(tmp_path: Path) -> Path:
    root = tmp_path / "workflows"
    if not root.exists():
        root.mkdir(parents=True)
        for name, body in FIXTURE_WORKFLOWS.items():
            (root / name).write_text(body)
    return root


def _base_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "NO_COLOR": "1",
        "GITHUB_REPOSITORY": OWNER_REPO,
        # As the gate job runs it.
        "CI_GATE_WORKFLOWS": GATED_IN_CI,
        "CI_GATE_WORKFLOW_DIR": str(_workflow_dir(tmp_path)),
    }
    # This suite runs INSIDE GitHub Actions in CI; nothing here may annotate or
    # write to the real job's summary, or reach the real API with its token.
    for leak in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(leak, None)
    return env


def gate(tmp_path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env or _base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ---------------------------------------------------------------------------
# The path lists are read from the workflow files, and read correctly.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(FIXTURE_WORKFLOWS))
@pytest.mark.parametrize("event", ["pull_request", "push", "schedule", "no_such_event"])
def test_the_script_reads_each_workflows_paths_exactly_as_yaml_does(tmp_path, name, event):
    """MUTATION: hard-code a path list in the script, or drop a pattern while
    parsing (a comment between items is the case most parsers get wrong)."""
    path = _workflow_dir(tmp_path) / name
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    on = data.get("on", data.get(True))
    proc = gate(tmp_path, "paths", "--workflow", str(path), "--event", event)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    if event not in on:
        assert lines == ["none"], lines
        return
    trigger = on[event] or {}
    if "paths" not in trigger:
        assert lines == ["all"], lines
        return
    assert lines[0] == "paths", lines
    assert lines[1:] == [str(p) for p in trigger["paths"]], lines
    # The control: the pull_request list is long; an empty read is a bug.
    assert len(lines) > 3


def test_the_script_waits_on_the_workflows_it_is_handed(tmp_path):
    """MUTATION: ignore CI_GATE_WORKFLOWS for a built-in list."""
    proc = gate(tmp_path, "workflows")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["terraform.yml"], proc.stdout


@pytest.mark.parametrize(
    "body, why",
    [
        ("on: [push, pull_request]\njobs: {}\n", "not a block"),
        ("on:\n  pull_request:\n    paths-ignore:\n      - docs/**\njobs: {}\n", "paths-ignore"),
        ("on:\n  pull_request:\n    paths: [\"a/**\"]\njobs: {}\n", "inline"),
        ("on:\n  pull_request:\n    paths:\n      - \"!docs/**\"\njobs: {}\n", "negat"),
    ],
)
def test_a_trigger_shape_the_script_does_not_model_is_refused_not_guessed(tmp_path, body, why):
    """Guessing would make the gate wait for a run that never comes, or skip one
    that does. MUTATION: fall through to `all` or `none` on an unknown shape."""
    wf = tmp_path / "odd.yml"
    wf.write_text(body)
    proc = gate(tmp_path, "paths", "--workflow", str(wf), "--event", "pull_request")
    assert proc.returncode != 0, proc.stdout
    assert why in proc.stderr.lower(), proc.stderr


# ---------------------------------------------------------------------------
# Which workflows a set of changed files should trigger.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "changed, expected",
    [
        # application.yml is not this script's: it carries the gate, which
        # judges its jobs through `needs`.
        (["docs/ci.md"], set()),
        (["apps/README.md"], set()),
        (["terraform/infra/main.tf"], {"terraform.yml"}),
        (["terraform/modules/monitoring/alerts.tf"], {"terraform.yml"}),
        (["scripts/lib/plan-guard.sh"], {"terraform.yml"}),
        (["scripts/lib/plan-guard.sh.orig"], set()),
        (["scripts/lib/other.sh"], set()),
        # A root-level pattern matches only at the root.
        (["docs/.github/workflows/terraform.yml"], set()),
        # `terraform/**` needs something under terraform/; a file called
        # `terraform` is not, and neither is a sibling sharing the prefix.
        (["terraform"], set()),
        (["terraformx/y.tf"], set()),
        # `**` crosses directories.
        (["tests/terraform/a/b/main.tftest.hcl"], {"terraform.yml"}),
        (["LICENSE"], set()),
        ([".github/workflows/security.yml"], set()),
        ([".github/workflows/terraform.yml"], {"terraform.yml"}),
        (["README.md", "terraform/infra/main.tf"], {"terraform.yml"}),
        ([], set()),
    ],
)
def test_the_changed_paths_decide_which_workflows_are_expected(tmp_path, changed, expected):
    listing = tmp_path / "changed.txt"
    listing.write_text("".join(f"{p}\n" for p in changed or []))
    proc = gate(tmp_path, "expected", "--event", "pull_request", "--changed", str(listing))
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == expected, proc.stdout


def test_on_push_to_main_terraform_is_expected_whatever_changed(tmp_path):
    """terraform.yml runs on every push to main (no `push` path filter)."""
    listing = tmp_path / "changed.txt"
    listing.write_text("LICENSE\n")
    proc = gate(tmp_path, "expected", "--event", "push", "--changed", str(listing))
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == {"terraform.yml"}, proc.stdout


# ---------------------------------------------------------------------------
# A fake `gh`: workflow runs and their jobs from a JSON world. A run can appear
# only after N looks, and stay in progress for N looks.
# ---------------------------------------------------------------------------
FAKE_GH = r'''#!{python}
import json, os, sys, urllib.parse

args = sys.argv[1:]
with open(os.environ["FAKE_WORLD"]) as fh:
    world = json.load(fh)


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


def bump(key):
    path = os.environ["FAKE_COUNTS"]
    counts = {}
    if os.path.exists(path):
        with open(path) as fh:
            counts = json.load(fh)
    counts[key] = counts.get(key, 0) + 1
    with open(path, "w") as fh:
        json.dump(counts, fh)
    return counts[key]


if args[:1] != ["api"]:
    print("fake gh: unhandled " + " ".join(args), file=sys.stderr)
    sys.exit(2)

path = next(a for a in args[1:] if a.startswith("repos/"))
parsed = urllib.parse.urlparse(path)
query = dict(urllib.parse.parse_qsl(parsed.query))
parts = parsed.path.split("/")
if os.environ.get("FAKE_GH_FAIL") == "always":
    record(event="api-failed", path=parsed.path)
    print(f"HTTP 502: Bad Gateway (https://api.github.com/{parsed.path})", file=sys.stderr)
    sys.exit(1)

if parts[3:5] == ["actions", "runs"] and len(parts) == 5:
    n = bump("runs")
    record(event="runs", query=query)
    out = []
    for run in world["runs"]:
        if run["head_sha"] != query.get("head_sha"):
            continue
        if "event" in query and run.get("event", "pull_request") != query["event"]:
            continue
        if n <= run.get("appears_after", 0):
            continue
        status, conclusion = run.get("status", "completed"), run.get("conclusion", "success")
        if n <= run.get("pending_polls", 0):
            status, conclusion = "in_progress", None
        out.append({
            "id": run["id"], "head_sha": run["head_sha"], "event": run.get("event", "pull_request"),
            "path": ".github/workflows/" + run["workflow"], "name": run["workflow"].split(".")[0],
            "status": status, "conclusion": conclusion, "created_at": run.get("created_at", "2026-09-29T00:00:00Z"),
            "html_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run['id']}",
        })
    print(json.dumps({"total_count": len(out), "workflow_runs": out}))
    sys.exit(0)

if parts[3:5] == ["actions", "runs"] and len(parts) == 7 and parts[6] == "jobs":
    run = next(r for r in world["runs"] if r["id"] == int(parts[5]))
    record(event="jobs", run=run["id"], query=query)
    out = []
    for i, job in enumerate(run.get("jobs", [])):
        out.append({
            "name": job["name"], "status": job.get("status", "completed"),
            "conclusion": job.get("conclusion", "success"),
            "html_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run['id']}/job/{run['id'] * 10 + i}",
        })
    print(json.dumps({"total_count": len(out), "jobs": out}))
    sys.exit(0)

print("fake gh: unhandled api " + path, file=sys.stderr)
sys.exit(2)
'''


def tf_jobs(**override: str) -> list[dict]:
    return [{"name": name, "conclusion": override.get(name, conclusion)} for name, conclusion in TF_JOBS.items()]


def run(run_id: int, workflow: str = "terraform.yml", **extra) -> dict:
    return {"id": run_id, "workflow": workflow, "head_sha": SHA, "event": "pull_request",
            "jobs": tf_jobs() if workflow == "terraform.yml" else [{"name": "x"}], **extra}


def wait(tmp_path: Path, world: dict, changed: list[str] | None, *, event: str = "pull_request",
         base: str | None = None, **knobs: str):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "gh"
    fake.write_text(FAKE_GH.replace("{python}", sys.executable))
    fake.chmod(0o755)
    (tmp_path / "world.json").write_text(json.dumps(world))
    listing = tmp_path / "changed.txt"
    listing.write_text("".join(f"{p}\n" for p in changed or []))
    env = _base_env(tmp_path)
    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_WORLD": str(tmp_path / "world.json"),
        "FAKE_EVENTS": str(tmp_path / "events.jsonl"),
        "FAKE_COUNTS": str(tmp_path / "counts.json"),
        "CI_GATE_POLL": "0.05",
        "CI_GATE_WAIT": "20",
        "CI_GATE_APPEAR": "10",
        "CI_GATE_SETTLE": "0",
        "CI_GATE_API_TRIES": "3",
        **knobs,
    })
    where = ["--changed", str(listing)] if changed is not None else ["--base", base or ""]
    proc = gate(tmp_path, "wait", "--event", event, "--sha", SHA, *where, env=env)
    events_file = tmp_path / "events.jsonl"
    events = [json.loads(l) for l in events_file.read_text().splitlines() if l] if events_file.exists() else []
    return proc, events


def _last_line(proc: subprocess.CompletedProcess) -> str:
    lines = [l for l in proc.stderr.splitlines() if l.strip()]
    assert lines, "the gate said nothing at all"
    return lines[-1]


def test_every_expected_workflow_passing_with_if_skipped_jobs_passes(tmp_path):
    world = {"runs": [run(2)]}
    proc, events = wait(tmp_path, world, ["terraform/infra/main.tf"])
    assert proc.returncode == 0, proc.stderr
    looked = [e for e in events if e["event"] == "runs"]
    assert looked and all(e["query"].get("head_sha") == SHA and e["query"].get("event") == "pull_request"
                          for e in looked), looked
    assert {e["run"] for e in events if e["event"] == "jobs"} == {2}, "every run's jobs are judged"


#: A commit id no clone has: a push's `before` after a force-push (archived pull request 1948).
GONE = "c3427310d7670168b23bf2b01578cccc729f10cd"


def test_a_push_whose_before_is_gone_expects_every_workflow_rather_than_failing(tmp_path):
    """A force-push to a swarm/** branch leaves `before` fetchable from nowhere,
    so no fetch depth can diff it. The changed files are unknown, which expects
    terraform.yml: its passing run passes, and its absence fails by name."""
    proc, _ = wait(tmp_path, {"runs": [run(2, event="push")]}, None, event="push", base=GONE)
    assert proc.returncode == 0, proc.stderr
    assert "changed files unknown" in proc.stderr, proc.stderr
    proc, _ = wait(tmp_path, {"runs": []}, None, event="push", base=GONE, CI_GATE_APPEAR="1")
    assert proc.returncode != 0, proc.stderr
    assert "terraform.yml" in _last_line(proc), proc.stderr


def test_a_pull_requests_base_missing_from_the_clone_still_fails(tmp_path):
    """A pull request's base is always fetchable, so missing it is a broken checkout."""
    proc, _ = wait(tmp_path, {"runs": [run(2)]}, None, event="pull_request", base=GONE)
    assert proc.returncode != 0, proc.stderr
    assert "fetch-depth: 0" in proc.stderr, proc.stderr


def test_a_workflow_the_paths_did_not_trigger_counts_as_passed(tmp_path):
    proc, _ = wait(tmp_path, {"runs": []}, ["docs/ci.md"])
    assert proc.returncode == 0, proc.stderr
    assert "terraform.yml" in proc.stderr and "not triggered" in proc.stderr.lower(), proc.stderr


def test_nothing_triggered_passes(tmp_path):
    proc, _ = wait(tmp_path, {"runs": []}, [".github/workflows/security.yml"])
    assert proc.returncode == 0, proc.stderr


def test_application_yml_runs_are_not_this_scripts_to_judge(tmp_path):
    """The gate job judges application.yml's jobs through `needs`; its run is
    still in progress while the gate runs, and waiting for it would wait for
    the gate itself. MUTATION: put application.yml back in CI_GATE_WORKFLOWS."""
    world = {"runs": [run(1, "application.yml", status="in_progress", conclusion=None), run(2)]}
    proc, events = wait(tmp_path, world, ["terraform/infra/main.tf", "apps/api/x.py"], CI_GATE_WAIT="2")
    assert proc.returncode == 0, proc.stderr
    assert {e["run"] for e in events if e["event"] == "jobs"} == {2}, events


def test_a_run_still_in_progress_is_waited_for(tmp_path):
    """MUTATION: pass on the first look, or treat in_progress as passed."""
    proc, events = wait(tmp_path, {"runs": [run(2, pending_polls=4)]}, ["terraform/infra/main.tf"])
    assert proc.returncode == 0, proc.stderr
    assert len([e for e in events if e["event"] == "runs"]) >= 5, events


def test_an_expected_run_not_created_yet_is_waited_for(tmp_path):
    """"No run yet" and "not triggered" look alike in the API, so a predicted
    run is waited for. MUTATION: read "no run" as "not triggered"."""
    proc, events = wait(tmp_path, {"runs": [run(2, appears_after=4)]}, ["terraform/infra/main.tf"])
    assert proc.returncode == 0, proc.stderr
    assert len([e for e in events if e["event"] == "runs"]) >= 5, events


def test_an_expected_run_that_never_appears_fails_by_name(tmp_path):
    proc, _ = wait(tmp_path, {"runs": []}, ["terraform/infra/main.tf"], CI_GATE_APPEAR="1")
    assert proc.returncode != 0, proc.stderr
    assert "terraform.yml" in _last_line(proc), proc.stderr


def test_a_failed_job_fails_the_gate_naming_it_and_its_url(tmp_path):
    jobs = tf_jobs(**{"terraform test": "failure"})
    proc, _ = wait(tmp_path, {"runs": [run(2, conclusion="failure", jobs=jobs)]}, ["terraform/infra/main.tf"])
    assert proc.returncode != 0
    assert "terraform test" in proc.stderr and "/actions/runs/2/job/" in proc.stderr, proc.stderr


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required"])
def test_a_run_that_did_not_succeed_fails_the_gate(tmp_path, conclusion):
    proc, _ = wait(tmp_path, {"runs": [run(2, conclusion=conclusion)]}, ["terraform/infra/main.tf"])
    assert proc.returncode != 0, proc.stderr
    assert conclusion in proc.stderr, proc.stderr


def test_a_run_that_failed_to_start_fails_the_gate(tmp_path):
    """No jobs, conclusion startup_failure: nothing ran, so nothing passed."""
    proc, _ = wait(tmp_path, {"runs": [run(2, conclusion="startup_failure", jobs=[])]}, ["terraform/infra/main.tf"])
    assert proc.returncode != 0, proc.stderr
    assert "failed to start" in proc.stderr.lower(), proc.stderr


def test_skipped_jobs_do_not_pass_inside_a_run_that_did_not_succeed(tmp_path):
    """Every job skipped and the run a failure: a workflow that never really
    started. MUTATION: judge jobs alone, counting `skipped` as passed."""
    jobs = [{"name": j["name"], "conclusion": "skipped"} for j in tf_jobs()]
    proc, _ = wait(tmp_path, {"runs": [run(2, conclusion="failure", jobs=jobs)]}, ["terraform/infra/main.tf"])
    assert proc.returncode != 0, proc.stderr


def test_a_run_the_paths_did_not_predict_still_counts_when_it_fails(tmp_path):
    """terraform.yml ran although the paths said it would not, and failed."""
    world = {"runs": [run(2, conclusion="failure", jobs=tf_jobs(**{"terraform test": "failure"}))]}
    proc, _ = wait(tmp_path, world, ["docs/ci.md"])
    assert proc.returncode != 0, proc.stderr
    assert "terraform.yml" in proc.stderr, proc.stderr


def test_a_late_unpredicted_failing_run_is_caught_inside_the_settle_window(tmp_path):
    """Nothing expected, so without a settle window the gate would pass on its
    first look. The gate job sets the window to zero because it starts after
    the event's runs exist; the script's window still works when asked for. MUTATION: ignore CI_GATE_SETTLE."""
    world = {"runs": [run(2, appears_after=3, conclusion="failure",
                          jobs=tf_jobs(**{"terraform test": "failure"}))]}
    proc, _ = wait(tmp_path, world, ["LICENSE"], CI_GATE_SETTLE="2")
    assert proc.returncode != 0, proc.stderr


def test_runs_of_another_event_at_the_same_commit_are_not_judged(tmp_path):
    """A failing push run at this sha is main's business, not this pull request's."""
    world = {"runs": [run(2), run(9, conclusion="failure", event="push",
                                  jobs=[{"name": "terraform test", "conclusion": "failure"}])]}
    proc, _ = wait(tmp_path, world, ["terraform/infra/main.tf"])
    assert proc.returncode == 0, proc.stderr


def test_the_newest_run_of_a_workflow_decides(tmp_path):
    world = {"runs": [
        run(1, conclusion="failure", created_at="2026-09-29T00:00:00Z",
            jobs=tf_jobs(**{"checkov": "failure"})),
        run(5, created_at="2026-09-29T01:00:00Z"),
    ]}
    proc, _ = wait(tmp_path, world, ["terraform/infra/main.tf"])
    assert proc.returncode == 0, proc.stderr


def test_an_unreadable_api_is_never_a_pass(tmp_path):
    proc, _ = wait(tmp_path, {"runs": [run(2)]}, ["terraform/infra/main.tf"], FAKE_GH_FAIL="always")
    assert proc.returncode != 0, proc.stderr
    assert "could not read" in _last_line(proc).lower(), proc.stderr


def test_a_run_still_going_past_the_wait_fails(tmp_path):
    proc, _ = wait(tmp_path, {"runs": [run(2, pending_polls=10_000)]}, ["terraform/infra/main.tf"], CI_GATE_WAIT="1")
    assert proc.returncode != 0, proc.stderr
    assert "terraform.yml" in _last_line(proc), proc.stderr


# ---------------------------------------------------------------------------
# The gate's own judgement of application.yml's jobs, run as the runner runs it.
# ---------------------------------------------------------------------------
#: Each job that judges its `needs`, and the key of its judging step.
JUDGES = {
    "ci-gate": "every job of this workflow passed, or was skipped by its own if",
    "python": "every job this check stands for passed, or was skipped by its own if",
    "ui": "every job this check stands for passed, or was skipped by its own if",
}


def judge(tmp_path: Path, job_id: str, results: dict[str, str]) -> subprocess.CompletedProcess:
    """Run `job_id`'s needs-reading step with `needs.<job>.result` = results
    (the step's env: NEEDS is `toJSON(needs)`)."""
    body = tmp_path / f"judge-{job_id}.sh"
    body.write_text(run_block("application.yml", job_id, JUDGES[job_id]))
    needs = {name: {"result": result, "outputs": {}} for name, result in results.items()}
    env = {**_base_env(tmp_path), "NEEDS": json.dumps(needs)}
    return subprocess.run(["bash", "-e", str(body)], env=env, capture_output=True, text=True, timeout=60, check=False)


def _all_needs(job_id: str) -> dict[str, str]:
    """The jobs a judge needs, each concluded `success`: `changes` and the
    checks below it."""
    return {name: "success" for name in ("changes", "python-unit", "ui-tests", "build-check")}


@pytest.mark.parametrize("job_id", sorted(JUDGES))
def test_every_need_passing_or_skipped_by_its_if_passes(tmp_path, job_id):
    proc = judge(tmp_path, job_id, _all_needs(job_id))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # A docs-only pull request: everything but `changes` skipped.
    skipped = {name: ("success" if name == "changes" else "skipped") for name in _all_needs(job_id)}
    proc = judge(tmp_path, job_id, skipped)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("job_id", sorted(JUDGES))
@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_any_need_failed_or_cancelled_fails_by_name(tmp_path, job_id, result):
    """MUTATION: count only `failure`, or read `cancelled` as passed."""
    needs = _all_needs(job_id)
    victim = sorted(n for n in needs if n != "changes")[-1]
    needs[victim] = result
    proc = judge(tmp_path, job_id, needs)
    assert proc.returncode != 0, proc.stdout
    assert f"{victim} ({result})" in proc.stdout, proc.stdout


@pytest.mark.parametrize("job_id", sorted(JUDGES))
def test_a_skip_under_a_changes_job_that_did_not_succeed_fails(tmp_path, job_id):
    """Everything skipped because `changes` never ran is not a pass.
    MUTATION: drop the `changes` check from the step."""
    needs = {name: "skipped" for name in _all_needs(job_id)}
    proc = judge(tmp_path, job_id, needs)
    assert proc.returncode != 0, proc.stdout
    assert "changes" in proc.stdout


def test_the_script_follows_the_house_rules():
    text = SCRIPT.read_text()
    first = next(
        line for i, line in enumerate(text.splitlines())
        if not (i == 0 and line.startswith("#!")) and line.strip() and not line.lstrip().startswith("#")
    )
    assert first == "set -euo pipefail", first
    assert text.startswith("#!/usr/bin/env bash\n")
    assert 'lib/common.sh"' in text
    assert os.access(SCRIPT, os.X_OK), "scripts/ci-gate.sh is not executable"


WORKFLOWS = REPO / ".github" / "workflows"


GATE_WORKFLOW = WORKFLOWS / "application.yml"


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def gate_job() -> dict:
    """application.yml's `ci-gate` job."""
    return _workflow(GATE_WORKFLOW)["jobs"]["ci-gate"]


def test_every_path_filtered_workflow_is_gated_and_security_is_not(tmp_path):
    """A new path-filtered workflow must come under the gate, or its failures
    hold nothing. security.yml is required directly and is not gated, and
    application.yml -- which carries the gate and runs on every pull request --
    is judged by the gate job's `needs`, never waited on (it would wait for
    itself). MUTATION: put application.yml back in CI_GATE_WORKFLOWS, or give
    application.yml's pull_request trigger a path filter again."""
    proc = gate(tmp_path, "workflows")
    assert proc.returncode == 0, proc.stderr
    gated = set(proc.stdout.split())
    filtered = set()
    for path in WORKFLOWS.glob("*.yml"):
        trigger = _workflow(path)["on"]
        if isinstance(trigger, dict) and isinstance(trigger.get("pull_request"), dict):
            if "paths" in trigger["pull_request"] or "paths-ignore" in trigger["pull_request"]:
                filtered.add(path.name)
    assert filtered == {"terraform.yml"}, filtered  # the control
    assert gated == filtered, gated
    assert "security.yml" not in gated
    assert "application.yml" not in gated


NAME_KEEPERS = {"python", "ui"}


def test_the_gate_needs_every_other_job_of_its_workflow():
    """A job left out of `needs` is one the gate passes without waiting for.
    MUTATION: drop any job from the gate's `needs`, e.g. `build-check`."""
    jobs = _workflow(GATE_WORKFLOW)["jobs"]
    needs = gate_job()["needs"]
    assert isinstance(needs, list), needs
    expected = set(jobs) - {"ci-gate"} - NAME_KEEPERS
    assert set(needs) == expected, expected ^ set(needs)
    assert len(needs) == len(set(needs)), needs


def test_the_gate_holds_read_permissions_only():
    job = gate_job()
    scope = job.get("permissions")
    assert isinstance(scope, dict), "the gate job must declare its permissions, not inherit the workflow's"
    assert set(scope) <= {"actions", "checks", "contents"} and set(scope.values()) == {"read"}, scope
    assert scope.get("actions") == "read", "the script lists terraform.yml's runs"
