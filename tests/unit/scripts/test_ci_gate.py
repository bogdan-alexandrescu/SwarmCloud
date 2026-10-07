"""`ci-gate` is the one check that stands for application.yml and terraform.yml.

SINCE 2026-10-07 IT IS THE LAST JOB OF application.yml, not a workflow of its
own (owner request: ci-gate.yml held a runner for the whole of CI while it
polled). It `needs:` every other job there and judges their results itself
(test_unit_shards.py runs that step), then runs scripts/ci-gate.sh for
terraform.yml alone -- `CI_GATE_WORKFLOWS`, read below from the workflow, so
the script is tested here in the configuration CI runs it in.

WHY IT EXISTS. Owner decision, 2026-09-29: main's ruleset (main-protection,
id 24160219) requires only security.yml's four checks, because application.yml
and terraform.yml carry a `pull_request` path filter, and GitHub treats a
required check whose workflow was filtered out as PENDING FOREVER, not as
passed. So nothing required held a pull request on its unit tests. `ci-gate`
runs on every pull request and every push to main, waits for the runs of those
two workflows at the same head commit, and passes only when every one of them
that ran passed. It is what the ruleset can require in their place.

The properties asserted here, against the REAL script with a fake `gh`:

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
  * the workflow's shape: its job is named exactly `ci-gate`, it runs on every
    pull request (no filter of its own) and on push to main, it holds only
    read permissions, and no `${{ }}` reaches a `run:`;
  * docs/ci.md carries the exact ruleset PUT that adds `ci-gate`, keeping the
    four security checks and the ruleset's other rules, with no check pinned.

WHAT THIS CANNOT PROVE: that GitHub's API answers in the shape the fake serves
(workflow runs filtered by head_sha and event, each carrying its workflow
`path`; a run's jobs with `filter=latest`). Only a real pull request exercises
the real API -- this pull request's own `ci-gate` run is the first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .application_paths import app_paths, reaches

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
SCRIPT = REPO / "scripts" / "ci-gate.sh"
GATE_WORKFLOW = WORKFLOWS / "application.yml"
CI_DOC = REPO / "docs" / "ci.md"
OWNER_REPO = "owner/swarm"
SHA = "c0ffee" * 6 + "abcd"
RULESET_ID = "24160219"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the gate needs bash and jq",
)


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def gate_job() -> dict:
    """application.yml's `ci-gate` job."""
    return _workflow(GATE_WORKFLOW)["jobs"]["ci-gate"]


def wait_step() -> dict:
    (step,) = [s for s in gate_job()["steps"] if "ci-gate.sh wait" in str(s.get("run", ""))]
    return step


def gated_in_ci() -> str:
    """The CI_GATE_WORKFLOWS the gate job hands the script."""
    value = str((wait_step().get("env") or {}).get("CI_GATE_WORKFLOWS") or "")
    assert value.split(), "the gate job does not say which workflows the script waits on"
    return value


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
        "CI_GATE_WORKFLOWS": gated_in_ci(),
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
@pytest.mark.parametrize("name", ["application.yml", "terraform.yml", "security.yml"])
@pytest.mark.parametrize("event", ["pull_request", "push", "schedule", "no_such_event"])
def test_the_script_reads_each_workflows_paths_exactly_as_yaml_does(tmp_path, name, event):
    """MUTATION: hard-code a path list in the script, or drop a pattern while
    parsing (a comment between items is the case most parsers get wrong)."""
    on = _workflow(WORKFLOWS / name)["on"]
    proc = gate(tmp_path, "paths", "--workflow", str(WORKFLOWS / name), "--event", event)
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
    # The control: the real pull_request lists are long; an empty read is a bug.
    assert len(lines) > 3


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
    listing.write_text("".join(f"{p}\n" for p in changed))
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


def workflow_jobs(name: str, **override: str) -> list[dict]:
    """A workflow's jobs as a pull request runs them: a job whose `if:` refuses
    pull_request (terraform.yml's `plan`) is skipped by it."""
    jobs = []
    for job_id, job in _workflow(WORKFLOWS / name)["jobs"].items():
        label = str(job.get("name") or job_id)
        condition = str(job.get("if") or "")
        conclusion = "skipped" if "!= 'pull_request'" in condition else "success"
        jobs.append({"name": label, "conclusion": override.get(label, conclusion)})
    assert any(j["conclusion"] == "skipped" for j in jobs), "the control: a pull request skips a job by its if:"
    return jobs


def tf_jobs(**override: str) -> list[dict]:
    return workflow_jobs("terraform.yml", **override)


def run(run_id: int, workflow: str = "terraform.yml", **extra) -> dict:
    return {"id": run_id, "workflow": workflow, "head_sha": SHA, "event": "pull_request",
            "jobs": tf_jobs() if workflow == "terraform.yml" else [{"name": "x"}], **extra}


def wait(tmp_path: Path, world: dict, changed: list[str], *, event: str = "pull_request", **knobs: str):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "gh"
    fake.write_text(FAKE_GH.replace("{python}", sys.executable))
    fake.chmod(0o755)
    (tmp_path / "world.json").write_text(json.dumps(world))
    listing = tmp_path / "changed.txt"
    listing.write_text("".join(f"{p}\n" for p in changed))
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
    proc = gate(tmp_path, "wait", "--event", event, "--sha", SHA, "--changed", str(listing), env=env)
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
    the event's runs exist (test_unit_shards.py holds that reason); the
    script's window still works when asked for. MUTATION: ignore CI_GATE_SETTLE."""
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
# The workflow's shape.
# ---------------------------------------------------------------------------
def test_the_gate_job_is_named_ci_gate_and_always_runs():
    """The required context is the job's name, and the job must report on
    every pull request, push to main and queue entry.
    MUTATION: rename the job, give application.yml's pull_request trigger a
    path filter again, drop `if: always()`, or leave ci-gate.yml beside it."""
    wf = _workflow(GATE_WORKFLOW)
    on = wf["on"]
    # Every pull request, with no filter: a filtered gate is the problem it
    # exists to solve.
    assert "pull_request" in on and not (on["pull_request"] or {}), on
    assert on["push"] == {"branches": ["main"]}, on
    job = gate_job()
    assert job.get("name") == "ci-gate", job
    # Skipped would read as passed; always() runs it when something it needs
    # failed, so it can say so.
    assert job.get("if") == "always()", job.get("if")
    # Exactly one producer of the context, so two checks of one name never
    # disagree. ci-gate.yml is gone.
    assert not (WORKFLOWS / "ci-gate.yml").exists()
    producers = [
        (path.name, job_id)
        for path in WORKFLOWS.glob("*.yml")
        for job_id, other in (_workflow(path).get("jobs") or {}).items()
        if str(other.get("name") or job_id) == "ci-gate"
    ]
    assert producers == [("application.yml", "ci-gate")], producers


def test_the_gate_needs_every_other_job_of_its_workflow():
    """A job left out of `needs` is one the gate passes without waiting for.
    MUTATION: drop any job from the gate's `needs`, e.g. `build-check`."""
    jobs = _workflow(GATE_WORKFLOW)["jobs"]
    needs = gate_job()["needs"]
    assert isinstance(needs, list), needs
    assert set(needs) == set(jobs) - {"ci-gate"}, set(jobs) - {"ci-gate"} ^ set(needs)
    assert len(needs) == len(set(needs)), needs


def test_the_gate_holds_read_permissions_only():
    job = gate_job()
    scope = job.get("permissions")
    assert isinstance(scope, dict), "the gate job must declare its permissions, not inherit the workflow's"
    assert set(scope) <= {"actions", "checks", "contents"} and set(scope.values()) == {"read"}, scope
    assert scope.get("actions") == "read", "the script lists terraform.yml's runs"


def test_no_expression_is_interpolated_into_a_run_block():
    """Values reach the shell through env:, never through `${{ }}` in run:."""
    steps = gate_job()["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert any("scripts/ci-gate.sh wait" in r for r in runs), runs
    for r in runs:
        assert "${{" not in r, r
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    # The changed files come from `git diff base...head`; a shallow clone has neither.
    assert (checkout.get("with") or {}).get("fetch-depth") == 0, checkout
    assert (checkout.get("with") or {}).get("persist-credentials") is False, checkout


def test_the_gate_waits_without_a_settle_window_only_because_it_starts_late():
    """CI_GATE_SETTLE is zero in the gate job: it starts only after `changes`
    -- a job of a run the same event created -- has finished, so terraform.yml's
    run already exists. That holds only while the gate needs `changes`.
    MUTATION: drop `changes` from the gate's needs while keeping the zero."""
    env = wait_step().get("env") or {}
    if str(env.get("CI_GATE_SETTLE", "60")) == "0":
        assert "changes" in gate_job()["needs"]


# ---------------------------------------------------------------------------
# The gate's own judgement of application.yml's jobs, run as the runner runs it.
# ---------------------------------------------------------------------------
def judge_step(job_id: str) -> dict:
    job = _workflow(GATE_WORKFLOW)["jobs"][job_id]
    steps = [s for s in job["steps"] if "NEEDS" in (s.get("env") or {})]
    assert len(steps) == 1, f"{job_id} has no single step reading `needs`"
    assert steps[0]["env"]["NEEDS"].replace(" ", "") == "${{toJSON(needs)}}", steps[0]["env"]
    return steps[0]


def judge(tmp_path: Path, job_id: str, results: dict[str, str]) -> subprocess.CompletedProcess:
    """Run `job_id`'s needs-reading step with `needs.<job>.result` = results."""
    body = tmp_path / f"judge-{job_id}.sh"
    body.write_text(str(judge_step(job_id)["run"]))
    needs = {name: {"result": result, "outputs": {}} for name, result in results.items()}
    env = {**_base_env(tmp_path), "NEEDS": json.dumps(needs)}
    return subprocess.run(["bash", "-e", str(body)], env=env, capture_output=True, text=True, timeout=60, check=False)


def _all_needs(job_id: str) -> dict[str, str]:
    """Every job `job_id` needs, each concluded `success`."""
    needs = _workflow(GATE_WORKFLOW)["jobs"][job_id]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    return {name: "success" for name in needs}


@pytest.mark.parametrize("job_id", ["ci-gate", "python", "ui"])
def test_every_need_passing_or_skipped_by_its_if_passes(tmp_path, job_id):
    proc = judge(tmp_path, job_id, _all_needs(job_id))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # A docs-only pull request: everything but `changes` skipped.
    skipped = {name: ("success" if name == "changes" else "skipped") for name in _all_needs(job_id)}
    proc = judge(tmp_path, job_id, skipped)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("job_id", ["ci-gate", "python", "ui"])
@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_any_need_failed_or_cancelled_fails_by_name(tmp_path, job_id, result):
    """MUTATION: count only `failure`, or read `cancelled` as passed."""
    needs = _all_needs(job_id)
    victim = sorted(n for n in needs if n != "changes")[-1]
    needs[victim] = result
    proc = judge(tmp_path, job_id, needs)
    assert proc.returncode != 0, proc.stdout
    assert f"{victim} ({result})" in proc.stdout, proc.stdout


@pytest.mark.parametrize("job_id", ["ci-gate", "python", "ui"])
def test_a_skip_under_a_changes_job_that_did_not_succeed_fails(tmp_path, job_id):
    """Everything skipped because `changes` never ran is not a pass.
    MUTATION: drop the `changes` check from the step."""
    needs = {name: "skipped" for name in _all_needs(job_id)}
    proc = judge(tmp_path, job_id, needs)
    assert proc.returncode != 0, proc.stdout
    assert "changes" in proc.stdout


def test_the_three_judgements_are_one_text():
    """Three copies of one rule are kept identical, so one is never fixed alone."""
    runs = {job_id: judge_step(job_id)["run"] for job_id in ("ci-gate", "python", "ui")}
    assert len(set(runs.values())) == 1, runs


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


# ---------------------------------------------------------------------------
# The owner's step: the ruleset PUT in docs/ci.md.
# ---------------------------------------------------------------------------
def _always_run_checks() -> set[str]:
    names = set()
    for job_id, job in (_workflow(WORKFLOWS / "security.yml").get("jobs") or {}).items():
        condition = str(job.get("if") or "")
        if "!= 'pull_request'" in condition or ("pull_request" not in condition and "schedule" in condition):
            continue
        names.add(str(job.get("name") or job_id))
    return names


def test_docs_carry_the_ruleset_put_that_adds_ci_gate():
    """MUTATION: drop a security check from the body, drop a rule the live
    ruleset has, or pin a check the owner decided to leave unpinned."""
    text = CI_DOC.read_text()
    match = re.search(
        r"```bash\n(gh api -X PUT repos/bogdan-alexandrescu/SwarmCloud/rulesets/" + RULESET_ID + r".*?)```",
        text,
        re.DOTALL,
    )
    assert match, "docs/ci.md does not carry the ruleset `gh api -X PUT` command"
    payload_match = re.search(r"<<'JSON'\n(.*?)\nJSON", match.group(1), re.DOTALL)
    assert payload_match, match.group(1)
    body = json.loads(payload_match.group(1))
    assert body["name"] == "main-protection" and body["target"] == "branch" and body["enforcement"] == "active", body
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"], body["conditions"]
    rules = {rule["type"]: rule for rule in body["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= set(rules), rules
    checks = rules["required_status_checks"]["parameters"]["required_status_checks"]
    contexts = {c["context"] for c in checks}
    always = _always_run_checks()
    assert always, "the control: security.yml has always-run jobs"
    assert contexts == always | {"ci-gate"}, contexts
    # Owner decision, 2026-09-29: no check is pinned to an integration_id;
    # the body restates the live ruleset and adds only ci-gate.
    assert all(set(c) == {"context"} for c in checks), checks
    assert rules["required_status_checks"]["parameters"]["strict_required_status_checks_policy"] is False


def test_a_pull_request_touching_only_the_gate_runs_its_tests_and_actionlint():
    """Owner decision, 2026-09-29: a pull request changing only the gate runs
    its tests and lints its workflow. The gate is application.yml's own job
    now, so actionlint reads it with application.yml, and the script is under
    scripts/**. MUTATION: drop either path, or application.yml from actionlint."""
    application = _workflow(WORKFLOWS / "application.yml")
    assert reaches("scripts/ci-gate.sh"), app_paths()
    assert reaches(".github/workflows/application.yml"), app_paths()
    lint = [
        step.get("run") or ""
        for step in application["jobs"]["workflows"]["steps"]
        if "actionlint" in (step.get("run") or "")
    ]
    assert any(".github/workflows/application.yml" in run for run in lint), lint
