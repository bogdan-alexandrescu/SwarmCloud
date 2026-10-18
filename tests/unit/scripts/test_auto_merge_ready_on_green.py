"""`ready-on-green` becomes `ready` in CI once a pull request is green at its head.

Owner decision, 2026-10-17. An operator's laptop ran ~20 watcher loops
(`gh pr view` every 60-90 s per pull request) only to add `ready` when a pull
request's checks went green, on the owner's personal GitHub quota. The
`ready-on-green` job in `.github/workflows/auto-merge.yml` does that instead,
and this file holds it:

* it runs on the events `requeue` re-evaluates on -- a pull request's
  `workflow_run` finishing, any `check_suite` -- and on a 15-minute schedule;
* it judges each `ready-on-green` pull request at the head GitHub reports for
  it, with main's `scripts/merge-train.sh judge`, and re-reads that head
  before it labels, so a head that moved is not promoted;
* green adds `ready` and removes `ready-on-green` with the merge App's token:
  a label the GITHUB_TOKEN adds raises no `labeled` event, and `enable` would
  never run;
* red leaves the pull request alone and comments once per head, naming the
  failing checks; a `[swarm] task_` title is skipped.

The job's two shells are RUN against a fake `gh` on PATH, and the judge
against the real `scripts/merge-train.sh`. Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
AUTO_MERGE = REPO / ".github" / "workflows" / "auto-merge.yml"
LABELS = REPO / ".github" / "labels.yml"
CI_DOC = REPO / "docs" / "ci.md"
GH_REPO = "bogdan-alexandrescu/SwarmCloud"

JOB = "ready-on-green"
LABEL = "ready-on-green"
GREEN_HEAD = "ab" * 20
RED_HEAD = "cd" * 20
PENDING_HEAD = "ef" * 20
REQUIRED = ["shellcheck", "trivy"]


def _workflow() -> dict:
    data = yaml.safe_load(AUTO_MERGE.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _workflow()


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert JOB in jobs, f"expected a job keyed {JOB!r} in auto-merge.yml, found {sorted(jobs)}"
    return jobs[JOB]


def _step(job: dict, step_id: str) -> dict:
    matches = [step for step in job.get("steps") or [] if step.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}, found {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_the_workflow_runs_on_a_finishing_run_a_finishing_suite_and_every_15_minutes(workflow: dict):
    """MUTATION: drop the schedule, change its cadence, or drop the
    `workflow_run`/`check_suite` triggers `requeue` already uses."""
    on = workflow["on"]
    assert on["workflow_run"]["types"] == ["completed"], on["workflow_run"]
    assert {"application", "security"} <= set(on["workflow_run"]["workflows"]), on["workflow_run"]
    assert (on.get("check_suite") or {}).get("types") == ["completed"], on.get("check_suite")
    assert on.get("schedule") == [{"cron": "*/15 * * * *"}], on.get("schedule")


def test_the_job_runs_on_the_requeue_events_and_the_schedule_only_with_the_merge_app(workflow: dict, job: dict):
    """The same events as `requeue`, plus the schedule, and nothing without
    the App. MUTATION: drop the `workflow_run.event == 'pull_request'` guard
    (every push to main would judge for nothing), the schedule clause, or the
    MERGE_APP_ID guard."""
    condition = re.sub(r"\s+", " ", str(job.get("if") or ""))
    requeue = re.sub(r"\s+", " ", str(workflow["jobs"]["requeue"].get("if") or ""))
    for clause in (
        "github.event_name == 'workflow_run' && github.event.workflow_run.event == 'pull_request'",
        "github.event_name == 'check_suite'",
        "vars.MERGE_APP_ID != ''",
    ):
        assert clause in requeue, f"control: requeue's if no longer says {clause!r}: {requeue}"
        assert clause in condition, condition
    assert "github.event_name == 'schedule'" in condition, condition


def test_only_this_job_runs_on_the_schedule(workflow: dict):
    """Every other job names its own event, so the schedule starts nothing
    else. MUTATION: give another job a condition the schedule satisfies."""
    for name, other in (workflow.get("jobs") or {}).items():
        if name == JOB:
            continue
        condition = str(other.get("if") or "")
        assert condition, f"{name} has no `if`, so it would run on the schedule"
        assert "schedule" not in condition, f"{name}: {condition}"
        assert re.search(r"github\.event_name == '|github\.event\.action == '", condition), f"{name}: {condition}"


def test_the_finished_runs_head_is_read_from_both_events(job: dict):
    """MUTATION: read only `workflow_run.head_sha`, and a check suite judges
    nothing."""
    expression = str((job.get("env") or {}).get("RUN_HEAD_SHA") or "")
    assert "github.event.workflow_run.head_sha" in expression, expression
    assert "github.event.check_suite.head_sha" in expression, expression


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_the_label_is_added_with_the_merge_apps_token(job: dict):
    """A `ready` the GITHUB_TOKEN adds raises no `labeled` event, so `enable`
    would never run. MUTATION: label with `github.token`, or mint the App
    token with more than pull requests, or from another App."""
    mint = _step(job, "app-token")
    assert mint.get("uses", "").startswith("actions/create-github-app-token@"), mint
    inputs = mint.get("with") or {}
    assert inputs.get("app-id") == "${{ vars.MERGE_APP_ID }}", inputs
    assert inputs.get("private-key") == "${{ secrets.MERGE_APP_PRIVATE_KEY }}", inputs
    permissions = {k: v for k, v in inputs.items() if k.startswith("permission-")}
    assert permissions == {"permission-pull-requests": "write"}, permissions
    label = _step(job, "label")
    assert (label.get("env") or {}).get("APP_TOKEN") == "${{ steps.app-token.outputs.token }}", label.get("env")
    run = label["run"]
    for line in run.splitlines():
        if "/labels" in line:
            assert 'GH_TOKEN="${APP_TOKEN}"' in line, line


def test_the_github_token_asks_for_the_least_it_needs(job: dict):
    """Reads (branches, check runs, statuses), and pull-requests: write for
    the one comment. MUTATION: widen it, or add actions/contents write."""
    assert job.get("permissions") == {
        "contents": "read",
        "checks": "read",
        "statuses": "read",
        "pull-requests": "write",
    }, job.get("permissions")
    assert (_step(job, "judge").get("env") or {}).get("GH_TOKEN") == "${{ github.token }}"


def test_the_checkout_is_the_default_branchs_scripts_without_credentials(job: dict):
    """The judge is main's merge-train.sh, never a pull request's code."""
    checkouts = [s for s in job.get("steps") or [] if str(s.get("uses") or "").startswith("actions/checkout@")]
    assert len(checkouts) == 1, checkouts
    inputs = checkouts[0].get("with") or {}
    assert inputs.get("ref") == "${{ github.event.repository.default_branch }}", inputs
    assert inputs.get("persist-credentials") is False, inputs
    assert inputs.get("sparse-checkout") == "scripts", inputs
    for step in job.get("steps") or []:
        assert "github.event" not in str(step.get("run") or ""), step


# ---------------------------------------------------------------------------
# The judge, run against a fake `gh`
# ---------------------------------------------------------------------------

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'GH_TOKEN=%s %s\n' "${GH_TOKEN:-}" "$*" >> "${FAKE_GH_LOG}"
args="$*"
case "${args}" in
  "pr list "*) cat "${FAKE_DIR}/prs.json" ;;
  "api repos/${GH_REPO}/branches/"*) echo '{"protection": {"enabled": false}}' ;;
  "api --paginate repos/${GH_REPO}/rules/branches/"*) cat "${FAKE_DIR}/rules.json" ;;
  "api repos/${GH_REPO}/commits/"*"/check-runs"*)
    sha="${args#api repos/${GH_REPO}/commits/}"; sha="${sha%%/*}"
    cat "${FAKE_DIR}/check-runs-${sha}.json" ;;
  "api repos/${GH_REPO}/commits/"*"/status"*)
    sha="${args#api repos/${GH_REPO}/commits/}"; sha="${sha%%/*}"
    cat "${FAKE_DIR}/status-${sha}.json" ;;
  "api --paginate repos/${GH_REPO}/issues/"*"/comments"*)
    number="${args#api --paginate repos/${GH_REPO}/issues/}"; number="${number%%/*}"
    jq -r '.[].body' "${FAKE_DIR}/comments-${number}.json" 2>/dev/null || true ;;
  "pr comment "*)
    body=""; prev=""
    for arg in "$@"; do [[ "${prev}" == "--body-file" ]] && body="${arg}"; prev="${arg}"; done
    [[ -n "${body}" ]] || { echo "fake gh: pr comment without --body-file: $*" >&2; exit 3; }
    cat "${body}" >> "${FAKE_DIR}/commented-$3.md" ;;
  "api repos/${GH_REPO}/pulls/"*)
    number="${args#api repos/${GH_REPO}/pulls/}"
    cat "${FAKE_DIR}/pull-${number}.json" ;;
  "api -X POST repos/${GH_REPO}/issues/"*"/labels -f labels[]="*) ;;
  "api -X DELETE repos/${GH_REPO}/issues/"*"/labels/"*) ;;
  *) echo "fake gh: unexpected call: $*" >&2; exit 3 ;;
esac
"""


def _outputs(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file: `k=v` lines and `k<<DELIM` blocks."""
    outputs: dict[str, str] = {}
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            key, delimiter = line.split("<<", 1)
            block = []
            i += 1
            while lines[i] != delimiter:
                block.append(lines[i])
                i += 1
            outputs[key] = "\n".join(block)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
        i += 1
    return outputs


def _check_run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": f"https://example.test/{name}"}


def _pr(number: int, head: str, title: str = "The release reads the digest it built") -> dict:
    return {"number": number, "title": title, "headRefOid": head, "baseRefName": "main"}


@pytest.fixture
def fake(tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "fake"
    data.mkdir()
    rules = [{"type": "required_status_checks",
              "parameters": {"required_status_checks": [{"context": n} for n in REQUIRED]}}]
    (data / "rules.json").write_text(json.dumps(rules))
    heads = {
        GREEN_HEAD: [_check_run(n) for n in REQUIRED] + [_check_run("queue for auto-merge", "in_progress", None)],
        RED_HEAD: [_check_run("shellcheck"), _check_run("trivy", conclusion="failure"),
                   _check_run("python (unit)", conclusion="failure")],
        PENDING_HEAD: [_check_run("shellcheck"), _check_run("trivy", "in_progress", None)],
    }
    for sha, runs in heads.items():
        (data / f"check-runs-{sha}.json").write_text(json.dumps({"total_count": len(runs), "check_runs": runs}))
        (data / f"status-{sha}.json").write_text(json.dumps({"state": "pending", "statuses": []}))
    return tmp_path


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    for name in ("gh.log", "summary.md", "output.txt"):
        (tmp_path / name).write_text("")
    return {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GH_REPO": GH_REPO,
        "FAKE_DIR": str(tmp_path / "fake"),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        **extra,
    }


def run_judge(job: dict, tmp_path: Path, prs: list[dict], event: str = "schedule", head: str = ""):
    (tmp_path / "fake" / "prs.json").write_text(json.dumps(prs))
    env = _env(tmp_path, EVENT_NAME=event, RUN_HEAD_SHA=head, GH_TOKEN="workflow-" + secrets.token_hex(8))
    proc = subprocess.run(
        ["bash", "-c", _step(job, "judge")["run"]],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return _outputs(tmp_path / "output.txt"), (tmp_path / "gh.log").read_text(), (tmp_path / "summary.md").read_text()


def test_the_judge_lists_only_ready_on_green_pull_requests(job: dict, fake: Path):
    """MUTATION: list `ready` pull requests, or every open one."""
    _, calls, _ = run_judge(job, fake, [])
    listing = [c for c in calls.splitlines() if " pr list " in c]
    assert len(listing) == 1, calls
    assert "--state open" in listing[0] and f"--label {LABEL} " in listing[0] + " ", listing


def test_a_green_head_is_promoted_and_judged_at_that_head(job: dict, fake: Path):
    """Every required check passed at the head the list reported, and the
    gate's own running check run does not count. MUTATION: read the check
    runs at another ref, or drop the `queue for auto-merge` exclusion."""
    outputs, calls, summary = run_judge(job, fake, [_pr(1500, GREEN_HEAD)])
    assert outputs.get("promote") == f"1500 {GREEN_HEAD}", (outputs, summary)
    assert f"commits/{GREEN_HEAD}/check-runs" in calls, calls
    assert f"commits/{GREEN_HEAD}/status" in calls, calls
    assert "judged 1 ready-on-green pull request(s); 1 green" in summary, summary


def test_a_pending_head_is_left_alone_and_said_nothing_about(job: dict, fake: Path):
    outputs, calls, summary = run_judge(job, fake, [_pr(1501, PENDING_HEAD)])
    assert outputs.get("promote") == "", outputs
    assert "pr comment" not in calls, calls
    assert "still waiting" in summary, summary


def test_a_red_head_is_left_alone_and_commented_on_once_with_the_failing_checks(job: dict, fake: Path):
    """MUTATION: promote a red head, comment on every run, or drop the names."""
    outputs, calls, _ = run_judge(job, fake, [_pr(1502, RED_HEAD)])
    assert outputs.get("promote") == "", outputs
    body = (fake / "fake" / "commented-1502.md").read_text()
    assert "python (unit), trivy" in body, body
    assert RED_HEAD in body, body
    marker = f"<!-- ready-on-green: red at {RED_HEAD} -->"
    assert marker in body, body
    # The second run finds its marker and says nothing more.
    (fake / "fake" / "comments-1502.json").write_text(json.dumps([{"body": "unrelated"}, {"body": body}]))
    (fake / "fake" / "commented-1502.md").unlink()
    outputs, calls, _ = run_judge(job, fake, [_pr(1502, RED_HEAD)])
    assert outputs.get("promote") == "", outputs
    assert not (fake / "fake" / "commented-1502.md").exists(), calls
    assert "pr comment" not in calls, calls


def test_a_red_comment_on_an_older_head_does_not_silence_a_new_red_head(job: dict, fake: Path):
    """Once per HEAD: a fix pushed that is red again is told again."""
    old = f"<!-- ready-on-green: red at {'00' * 20} -->\nold"
    (fake / "fake" / "comments-1503.json").write_text(json.dumps([{"body": old}]))
    run_judge(job, fake, [_pr(1503, RED_HEAD)])
    assert (fake / "fake" / "commented-1503.md").is_file()


@pytest.mark.parametrize("title", ["[swarm] task_0123", "  [Swarm] task_abc"])
def test_a_swarm_placeholder_title_is_skipped(job: dict, fake: Path, title: str):
    """The gate refuses it; promoting it would only earn that refusal.
    MUTATION: drop the title check, or make it case-sensitive."""
    outputs, calls, summary = run_judge(job, fake, [_pr(1504, GREEN_HEAD, title)])
    assert outputs.get("promote") == "", outputs
    assert "check-runs" not in calls, calls
    assert "[swarm] task_" in summary, summary


def test_a_finishing_run_judges_only_the_pull_requests_at_its_head(job: dict, fake: Path):
    """The schedule judges all; a finishing run only its own head, so a busy
    hour of CI does not read every pull request on every finish. MUTATION:
    ignore RUN_HEAD_SHA, or apply it on the schedule too."""
    prs = [_pr(1505, GREEN_HEAD), _pr(1506, PENDING_HEAD)]
    outputs, calls, _ = run_judge(job, fake, prs, event="workflow_run", head=PENDING_HEAD)
    assert outputs.get("promote") == "", outputs
    assert GREEN_HEAD not in calls, calls
    outputs, _, _ = run_judge(job, fake, prs, event="check_suite", head=GREEN_HEAD)
    assert outputs.get("promote") == f"1505 {GREEN_HEAD}", outputs
    outputs, _, summary = run_judge(job, fake, prs, event="schedule", head="")
    assert outputs.get("promote") == f"1505 {GREEN_HEAD}", outputs
    assert "judged 2 ready-on-green" in summary, summary


def test_a_base_with_no_required_checks_promotes_nothing(job: dict, fake: Path):
    """"Every required check passed" over none is true of anything."""
    (fake / "fake" / "rules.json").write_text("[]")
    outputs, _, summary = run_judge(job, fake, [_pr(1507, GREEN_HEAD)])
    assert outputs.get("promote") == "", outputs
    assert "no required checks" in summary, summary


# ---------------------------------------------------------------------------
# The label step, run against a fake `gh`
# ---------------------------------------------------------------------------


def run_label(job: dict, tmp_path: Path, promote: str, pulls: dict[int, dict]):
    for number, pull in pulls.items():
        (tmp_path / "fake" / f"pull-{number}.json").write_text(json.dumps(pull))
    app_token = "app-" + secrets.token_hex(8)
    workflow_token = "workflow-" + secrets.token_hex(8)
    env = _env(tmp_path, GH_TOKEN=workflow_token, APP_TOKEN=app_token, PROMOTE=promote)
    proc = subprocess.run(
        ["bash", "-c", _step(job, "label")["run"]],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    return proc, (tmp_path / "gh.log").read_text(), app_token, workflow_token


def _pull(head: str, labels: tuple[str, ...] = (LABEL,), state: str = "open") -> dict:
    return {"state": state, "head": {"sha": head}, "labels": [{"name": n} for n in labels]}


def test_a_green_head_gets_ready_and_loses_ready_on_green_with_the_app_token(job: dict, fake: Path):
    """MUTATION: add another label, keep `ready-on-green`, or label with the
    GITHUB_TOKEN."""
    proc, calls, app_token, workflow_token = run_label(job, fake, f"1500 {GREEN_HEAD}", {1500: _pull(GREEN_HEAD)})
    assert proc.returncode == 0, proc.stderr
    writes = [c for c in calls.splitlines() if " -X " in c]
    assert writes == [
        f"GH_TOKEN={app_token} api -X POST repos/{GH_REPO}/issues/1500/labels -f labels[]=ready",
        f"GH_TOKEN={app_token} api -X DELETE repos/{GH_REPO}/issues/1500/labels/{LABEL}",
    ], calls
    assert f"GH_TOKEN={workflow_token} api repos/{GH_REPO}/pulls/1500" in calls, calls


@pytest.mark.parametrize(
    "pull",
    [
        _pull("99" * 20),  # the head moved since the judge
        _pull(GREEN_HEAD, labels=("ready",)),  # someone already took it off
        _pull(GREEN_HEAD, state="closed"),
    ],
    ids=["head-moved", "label-gone", "closed"],
)
def test_a_pull_request_that_changed_since_the_judge_is_not_labelled(job: dict, fake: Path, pull: dict):
    """The head-sha check: `ready` names the head that was judged green, not
    whatever was pushed after. MUTATION: drop the re-read, or compare
    anything but `.head.sha`."""
    proc, calls, _, _ = run_label(job, fake, f"1500 {GREEN_HEAD}", {1500: pull})
    assert proc.returncode == 0, proc.stderr
    assert f"api repos/{GH_REPO}/pulls/1500" in calls, calls  # the control: it did look
    assert " -X " not in calls, calls


def test_a_malformed_promote_line_fails_the_step(job: dict, fake: Path):
    proc, calls, _, _ = run_label(job, fake, "1500 not-a-sha", {})
    assert proc.returncode != 0
    assert " -X " not in calls, calls


# ---------------------------------------------------------------------------
# The label is declared, and the doc says what it does
# ---------------------------------------------------------------------------


def test_the_label_is_declared():
    declared = {entry["name"]: entry for entry in yaml.safe_load(LABELS.read_text())}
    assert LABEL in declared, sorted(declared)
    assert declared[LABEL]["description"].strip(), declared[LABEL]


def test_docs_ci_describes_ready_on_green():
    """The section the auto-merge doc points at names the label, the job, the
    schedule and the token."""
    text = CI_DOC.read_text()
    match = re.search(r"\n#+ [^\n]*ready-on-green[^\n]*\n(.*?)(?=\n#+ )", text, re.DOTALL | re.IGNORECASE)
    assert match, "docs/ci.md has no heading naming ready-on-green"
    section = match.group(1)
    for phrase in ("`ready-on-green`", "`ready`", "15 minutes", "merge App", "GITHUB_TOKEN", "head"):
        assert phrase in section, phrase
