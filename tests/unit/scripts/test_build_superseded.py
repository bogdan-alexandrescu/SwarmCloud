"""A release whose build a newer push cancelled ends superseded, even when it
reads the build run before the cancellation has concluded.

#2664 (owner's pick, 2026-10-31). The images stage fails the moment
`build images` is cancelled, but GitHub takes seconds to conclude the rest of
the application run `cancelled` (33 s measured on 2026-10-28). The check read
the run at once, found no conclusion, and failed a superseded release red with
"no newer commit superseded it". scripts/lib/build-superseded.sh makes that
judgement and waits, bounded, for the run's final conclusion before it judges.

Run here against a fake `gh` whose answers can change between reads, the way
GitHub's do while a run is being cancelled. WHAT THIS CANNOT PROVE: that
GitHub lists a run cancelled by concurrency as `cancelled` once it concludes,
and that the compare API says `ahead` for a commit main has moved past.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "build-superseded.sh"

REPO_NAME = "owner/repo"
SHA = "a" * 40
RUN_ID = 4242
COMPARE = f"repos/{REPO_NAME}/compare/{SHA}...main"
APP_RUNS = f"repos/{REPO_NAME}/actions/workflows/application.yml/runs?head_sha={SHA}&branch=main&event=push&per_page=1"
APP_JOBS = f"repos/{REPO_NAME}/actions/runs/{RUN_ID}/jobs?per_page=100"

# A response is either one JSON body, answered on every read, or
# {"reads": [body, body, ...]}: the n-th read gets the n-th body, and every
# read past the end gets the last one.
FAKE_GH = """\
import json, os, subprocess, sys
args = sys.argv[1:]
if not args or args[0] != "api":
    sys.exit(2)
path = args[1]
jq = args[args.index("--jq") + 1] if "--jq" in args else "."
with open(os.environ["FAKE_GH_RESPONSES"]) as f:
    responses = json.load(f)
log = os.environ["FAKE_GH_LOG"]
with open(log) as f:
    seen = sum(1 for line in f if line.rstrip("\\n") == path)
with open(log, "a") as f:
    f.write(path + "\\n")
if path not in responses:
    sys.stderr.write("HTTP 502: unreadable\\n")
    sys.exit(1)
body = responses[path]
if isinstance(body, dict) and "reads" in body:
    body = body["reads"][min(seen, len(body["reads"]) - 1)]
sys.exit(subprocess.run(["jq", "-r", jq], input=json.dumps(body), text=True).returncode)
"""


def _run(tmp_path: Path, responses: dict, *, wait: int = 30, poll: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    log = tmp_path / "gh.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GH_TOKEN": "unused-by-the-fake",
        "FAKE_GH_RESPONSES": str(tmp_path / "responses.json"),
        "FAKE_GH_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--sha", SHA, "--repo", REPO_NAME, "--wait", str(wait), "--poll", str(poll)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, log.read_text().splitlines()


def _run_reads(*runs: dict) -> dict:
    return {"reads": [{"workflow_runs": [run]} for run in runs]}


def _jobs(build_conclusion: str | None) -> dict:
    jobs = [{"name": "unit tests (1/4)", "conclusion": "cancelled"}]
    if build_conclusion is not None:
        jobs.append({"name": "build images", "conclusion": build_conclusion})
    return {"jobs": jobs}


IN_PROGRESS = {"id": RUN_ID, "status": "in_progress", "conclusion": None}
CANCELLED = {"id": RUN_ID, "status": "completed", "conclusion": "cancelled"}


def test_the_script_is_executable_and_strict():
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"
    lines = [line for line in SCRIPT.read_text().splitlines()[1:] if line.strip() and not line.lstrip().startswith("#")]
    assert lines[0] == "set -euo pipefail"


def test_a_run_read_in_progress_then_cancelled_ends_superseded(tmp_path):
    """#2664's case: the run reads in_progress first and cancelled later."""
    responses = {
        COMPARE: {"status": "ahead"},
        APP_RUNS: _run_reads(IN_PROGRESS, IN_PROGRESS, CANCELLED),
        APP_JOBS: _jobs("cancelled"),
    }
    proc, calls = _run(tmp_path, responses)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(RUN_ID)
    assert calls.count(APP_RUNS) == 3, "it judged before the run concluded"


def test_a_run_already_cancelled_is_superseded_without_waiting(tmp_path):
    responses = {COMPARE: {"status": "ahead"}, APP_RUNS: _run_reads(CANCELLED), APP_JOBS: _jobs(None)}
    proc, calls = _run(tmp_path, responses, poll=600)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(RUN_ID)
    assert calls.count(APP_RUNS) == 1


@pytest.mark.parametrize("build_conclusion", ["cancelled", "skipped", None])
def test_every_build_the_cancel_stopped_counts(tmp_path, build_conclusion):
    responses = {
        COMPARE: {"status": "ahead"},
        APP_RUNS: _run_reads(IN_PROGRESS, CANCELLED),
        APP_JOBS: _jobs(build_conclusion),
    }
    proc, _ = _run(tmp_path, responses)
    assert proc.returncode == 0, proc.stderr


def test_the_wait_is_bounded_and_an_unconcluded_run_stays_red(tmp_path):
    responses = {COMPARE: {"status": "ahead"}, APP_RUNS: _run_reads(IN_PROGRESS), APP_JOBS: _jobs("cancelled")}
    proc, _ = _run(tmp_path, responses, wait=0)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "had not concluded" in proc.stderr


@pytest.mark.parametrize("build_conclusion", ["success", "failure"])
def test_a_build_that_finished_stops_the_wait_red(tmp_path, build_conclusion):
    """A build that succeeded or failed is never a supersession: no point waiting."""
    responses = {COMPARE: {"status": "ahead"}, APP_RUNS: _run_reads(IN_PROGRESS), APP_JOBS: _jobs(build_conclusion)}
    proc, calls = _run(tmp_path, responses, wait=600, poll=600)
    assert proc.returncode == 1
    assert calls.count(APP_RUNS) == 1


@pytest.mark.parametrize(
    "case, responses",
    [
        ("main-not-moved", {COMPARE: {"status": "identical"}, APP_RUNS: _run_reads(CANCELLED), APP_JOBS: _jobs("cancelled")}),
        ("compare-unreadable", {APP_RUNS: _run_reads(CANCELLED), APP_JOBS: _jobs("cancelled")}),
        ("build-failed", {COMPARE: {"status": "ahead"}, APP_RUNS: _run_reads(CANCELLED), APP_JOBS: _jobs("failure")}),
        ("build-succeeded", {COMPARE: {"status": "ahead"}, APP_RUNS: _run_reads(CANCELLED), APP_JOBS: _jobs("success")}),
        (
            "run-failed",
            {
                COMPARE: {"status": "ahead"},
                APP_RUNS: _run_reads(IN_PROGRESS, {"id": RUN_ID, "status": "completed", "conclusion": "failure"}),
                APP_JOBS: _jobs("skipped"),
            },
        ),
        ("no-run", {COMPARE: {"status": "ahead"}, APP_RUNS: {"workflow_runs": []}}),
        ("runs-unreadable", {COMPARE: {"status": "ahead"}}),
        ("jobs-unreadable", {COMPARE: {"status": "ahead"}, APP_RUNS: _run_reads(CANCELLED)}),
    ],
)
def test_any_other_failure_stays_red(tmp_path, case, responses):
    proc, _ = _run(tmp_path, responses)
    assert proc.returncode == 1, case
    assert proc.stdout == "", case
