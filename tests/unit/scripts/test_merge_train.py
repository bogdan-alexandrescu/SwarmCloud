"""The merge train (owner decision 2026-10-14, "A1 Merge train": keep main a
tested tree) -- scripts/merge-train.sh and .github/workflows/merge-train.yml.

WHY. Ruleset 24160219 (`main-protection`) has strict up-to-date on, so a green
`ready` pull request waits for its branch to be brought up to date and CI to
run again: a median 17 minutes after green, p90 173, and 2.93 CI runs per
pull request. GitHub's merge queue is refused on a user-owned repository. The
train tests several `ready` pull requests together, on main plus each one
squashed in order, and lands them one squash per pull request.

What is held here, all offline (no network, no credentials, bash + jq + git):

* `select`: which open pull requests may board -- green at head, `ready`,
  mergeable, not a draft, not a fork, aimed at main, not a train itself, not
  the worker's `[swarm] task_` placeholder title;
* `judge`: a train head's checks read as green, red (with the failing job's
  link) or pending;
* `plan`: bisection by halves, and ejection of a lone culprit;
* `compose`: the train is main plus one squash commit per pull request, in
  order, titled `<title> (#N)`, and a pull request that conflicts is dropped
  rather than failing the train;
* the state block a train pull request carries, and its refusal of a value
  that is not what the train wrote;
* the workflow's triggers, concurrency, permissions and credential;
* auto-merge.yml's gate leaves a `ready` pull request to the train when
  vars.MERGE_TRAIN is `on`, and is unchanged otherwise;
* the runbook carries the ruleset change and says it is not applied.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from .test_auto_merge_workflow import FAKE_GH, PROTECTED, _outputs, _step

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
WORKFLOW = REPO / ".github" / "workflows" / "merge-train.yml"
AUTO_MERGE = REPO / ".github" / "workflows" / "auto-merge.yml"
RUNBOOK = REPO / "docs" / "runbooks" / "merge-train.md"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "merge_train"

REQUIRED = ["ci-gate", "secret scan"]


def _need(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required")


def _run(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    _need("bash", "jq")
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, check=False, cwd=cwd, env=full_env,
    )


def _required_file(tmp_path: Path, names: list[str] = REQUIRED) -> Path:
    path = tmp_path / "required.txt"
    path.write_text("".join(f"{n}\n" for n in names))
    return path


# --------------------------------------------------------------------------
# the script's shape
# --------------------------------------------------------------------------


def test_the_script_is_executable_strict_and_sources_common():
    text = SCRIPT.read_text()
    assert os.access(SCRIPT, os.X_OK), "scripts/merge-train.sh is not executable"
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [
        line for line in text.splitlines()[1:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert effective[0] == "set -euo pipefail", effective[0]
    assert re.search(r'^source "\$\(cd -- "\$\(dirname -- "\$\{BASH_SOURCE\[0\]\}"\)" && pwd\)/lib/common\.sh"$',
                     text, flags=re.M)


def test_the_script_uses_nothing_bash_3_2_lacks():
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    for pattern in (r"\bmapfile\b", r"\breadarray\b", r"\bdeclare -A\b", r"\blocal -A\b",
                    r"\$\{[A-Za-z_]+,,\}", r"\$\{[A-Za-z_]+\^\^\}", r"\bcoproc\b"):
        assert not re.search(pattern, code), f"bash 3.2 has no {pattern}"


# --------------------------------------------------------------------------
# select: who may board
# --------------------------------------------------------------------------


def _select(tmp_path: Path, prs: Path = FIXTURES / "prs.json", required: list[str] = REQUIRED):
    proc = _run("select", "--prs", str(prs), "--required", str(_required_file(tmp_path, required)))
    assert proc.returncode == 0, proc.stderr
    return [line.split("\t") for line in proc.stdout.splitlines() if line]


def test_select_boards_only_green_ready_mergeable_same_repo_pull_requests_oldest_first(tmp_path):
    rows = _select(tmp_path)
    assert [r[0] for r in rows] == ["101", "114", "120"], rows


def test_select_boards_a_train_first_pull_request_ahead_of_older_ones(tmp_path):
    prs = json.loads((FIXTURES / "prs.json").read_text())
    for pr in prs:
        if pr["number"] == 120:
            pr["labels"].append({"name": "train-first"})
    path = tmp_path / "prs-first.json"
    path.write_text(json.dumps(prs))
    proc = _run("select", "--prs", str(path), "--required", str(_required_file(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    # MUTATION: sort by number alone -> 120 boards last, behind every older one.
    assert [line.split("\t")[0] for line in proc.stdout.splitlines() if line] == ["120", "101", "114"]


def test_select_carries_the_head_it_judged_and_a_one_line_title(tmp_path):
    rows = {r[0]: r for r in _select(tmp_path)}
    assert rows["101"][1] == "1010000000000000000000000000000000000001"
    assert rows["101"][2] == "The docs say why the reconciler parks"
    # A tab in a title would split the record: it is flattened to a space.
    assert rows["114"][2] == "Tabs in a title are flattened"
    assert all(len(r) == 3 for r in rows.values())


@pytest.mark.parametrize("number, why", [
    ("102", "a draft"),
    ("103", "a fork"),
    ("104", "no ready label (a label that merely contains the word does not count)"),
    ("105", "conflicting"),
    ("106", "mergeability unknown"),
    ("107", "a check that is not required failed"),
    ("108", "a required check never reported"),
    ("109", "a required check still running"),
    ("110", "the worker's placeholder title, any case, leading whitespace"),
    ("111", "a train branch"),
    ("112", "another base"),
    ("113", "a required status context errored"),
])
def test_select_leaves_out(tmp_path, number: str, why: str):
    assert number not in [r[0] for r in _select(tmp_path)], why


def test_select_judges_against_the_required_list_it_is_given(tmp_path):
    """The control for 108: with only ci-gate required, a pull request that never
    reported `secret scan` boards -- so 108's absence above is the missing check."""
    assert "108" in [r[0] for r in _select(tmp_path, required=["ci-gate"])]


def test_select_can_board_a_bisection_s_pull_request_github_has_not_rechecked(tmp_path):
    """Right after a train lands, GitHub recomputes every open pull request's
    mergeability and reports UNKNOWN meanwhile. A bisection's held-back half is
    boarded then, by name, and compose finds a real conflict anyway; a
    CONFLICTING one still stays out."""
    proc = _run("select", "--prs", str(FIXTURES / "prs.json"),
                "--required", str(_required_file(tmp_path)), "--allow-unknown-mergeable")
    assert proc.returncode == 0, proc.stderr
    numbers = [line.split("\t")[0] for line in proc.stdout.splitlines() if line]
    assert numbers == ["101", "106", "114", "120"]


# TRAIN-BOARDS-UNKNOWN (part of #2688; epic #3179, third comment): right
# after a landing every open pull request reads `mergeable: UNKNOWN` in the
# list, so a tick that boards only MERGEABLE boards nothing. With
# --recheck-unknown, select reads each otherwise eligible UNKNOWN pull request
# once more with the single-pull-request read (which makes GitHub compute it),
# waits briefly while it is still computing, and boards it only on MERGEABLE.

FAKE_PR_VIEW = """#!/usr/bin/env bash
set -euo pipefail
# gh pr view <N> --repo <R> --json mergeable --jq .mergeable, answered from
# $FAKE_MERGEABLE_DIR/<N>: one answer per line, one line per read, the last
# one repeated once they run out. An answer of 504 or 404 fails the read so.
printf '%s\\n' "$*" >>"${FAKE_GH_LOG}"
[[ "$1 $2" == "pr view" ]] || { echo "fake gh: unexpected: $*" >&2; exit 64; }
n="$3"
answers="${FAKE_MERGEABLE_DIR}/${n}"
count="${FAKE_MERGEABLE_DIR}/${n}.count"
reads=0
[[ -f "${count}" ]] && reads="$(cat "${count}")"
reads=$((reads + 1))
printf '%s' "${reads}" >"${count}"
total="$(wc -l <"${answers}" | tr -d ' ')"
[[ "${reads}" -le "${total}" ]] || reads="${total}"
answer="$(sed -n "${reads}p" "${answers}")"
case "${answer}" in
  504) echo "HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)" >&2; exit 1 ;;
  404) echo "GraphQL: Could not resolve to a PullRequest with the number of ${n}. (repository.pullRequest)" >&2; exit 1 ;;
esac
printf '%s\\n' "${answer}"
"""


def _recheck(tmp_path: Path, answers: dict[int, list[str]], prs: Path = FIXTURES / "prs.json"):
    _need("bash", "jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gh"
    fake.write_text(FAKE_PR_VIEW)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    mergeable_dir = tmp_path / "mergeable"
    mergeable_dir.mkdir(exist_ok=True)
    for number, lines in answers.items():
        (mergeable_dir / str(number)).write_text("".join(f"{line}\n" for line in lines))
    log = tmp_path / "gh.log"
    log.write_text("")
    proc = _run("select", "--prs", str(prs), "--required", str(_required_file(tmp_path)),
                "--recheck-unknown", env={
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "GH_REPO": "example-org/example-repo",
                    "FAKE_GH_LOG": str(log),
                    "FAKE_MERGEABLE_DIR": str(mergeable_dir),
                    "MERGE_TRAIN_MERGEABLE_WAIT_SECONDS": "0",
                    "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0",
                })
    assert proc.returncode == 0, proc.stderr
    numbers = [line.split("\t")[0] for line in proc.stdout.splitlines() if line]
    return numbers, log.read_text().splitlines(), proc


def test_select_boards_an_unknown_pull_request_that_reads_mergeable_on_a_second_read(tmp_path):
    numbers, reads, proc = _recheck(tmp_path, {106: ["MERGEABLE"]})
    # MUTATION: board only the list's MERGEABLE -> 106 is left out, and a tick
    # right after a landing boards nothing.
    assert numbers == ["101", "106", "114", "120"], proc.stderr
    assert reads == ["pr view 106 --repo example-org/example-repo --json mergeable --jq .mergeable"]


def test_select_carries_the_head_and_title_of_a_rechecked_pull_request(tmp_path):
    _, _, proc = _recheck(tmp_path, {106: ["MERGEABLE"]})
    rows = {line.split("\t")[0]: line.split("\t") for line in proc.stdout.splitlines() if line}
    assert len(rows["106"]) == 3
    assert re.fullmatch(r"[0-9a-f]{40}", rows["106"][1])


def test_select_leaves_out_an_unknown_pull_request_that_reads_conflicting_on_a_second_read(tmp_path):
    numbers, reads, _ = _recheck(tmp_path, {106: ["CONFLICTING"]})
    # MUTATION: board an UNKNOWN on any second answer -> 106 boards conflicting.
    assert numbers == ["101", "114", "120"]
    assert len(reads) == 1


def test_select_waits_briefly_while_github_is_still_computing(tmp_path):
    numbers, reads, _ = _recheck(tmp_path, {106: ["UNKNOWN", "UNKNOWN", "MERGEABLE"]})
    assert numbers == ["101", "106", "114", "120"]
    assert len(reads) == 3


def test_select_leaves_a_still_unknown_pull_request_for_the_next_tick(tmp_path):
    numbers, reads, _ = _recheck(tmp_path, {106: ["UNKNOWN"]})
    assert numbers == ["101", "114", "120"]
    # The wait is bounded: a handful of reads, never a poll until it answers.
    assert 1 <= len(reads) <= 5


def test_select_rereads_only_an_otherwise_eligible_unknown_pull_request(tmp_path):
    """A draft, a fork, one without `ready` or one not green is never re-read:
    the second read is for a candidate the train would board."""
    rows = json.loads((FIXTURES / "prs.json").read_text())
    for row in rows:
        if row["number"] in (102, 103, 104, 107):
            row["mergeable"] = "UNKNOWN"
    prs = tmp_path / "unknown.json"
    prs.write_text(json.dumps(rows))
    answers = {n: ["MERGEABLE"] for n in (102, 103, 104, 106, 107)}
    numbers, reads, _ = _recheck(tmp_path, answers, prs=prs)
    assert numbers == ["101", "106", "114", "120"]
    assert [r.split()[2] for r in reads] == ["106"]


def test_select_without_the_flag_never_reads_github(tmp_path):
    """Plain select stays pure: files in, lines out (106 stays out, above)."""
    assert "106" not in [r[0] for r in _select(tmp_path)]


def test_the_tick_rechecks_unknown_mergeability_when_it_boards_from_the_pool():
    code = SCRIPT.read_text()
    start = code[code.index("start_train() {"):]
    start = start[:start.index("\n}\n")]
    assert "--recheck-unknown" in start


# CHECK-STUCK-2 (the owner, 2026-10-23, task_9e9080121b104370b079 Q0; part of
# #2031): select reads a rollup check run as done the way `judge` does --
# status COMPLETED, OR a conclusion together with a completedAt. GitHub left
# PR 2006's `secret scan` IN_PROGRESS after giving it both; GitHub's own
# rollup shows it SUCCESS. A run with no conclusion stays pending.
#: (case, conclusion, completedAt, boards)
SELECT_STUCK = [
    ("IN_PROGRESS, SUCCESS, completedAt", "SUCCESS", "2026-10-22T19:05:52Z", True),
    ("IN_PROGRESS, no conclusion", None, None, False),
]


@pytest.mark.parametrize(("case", "conclusion", "completed_at", "boards"), SELECT_STUCK,
                         ids=[row[0] for row in SELECT_STUCK])
def test_select_reads_a_run_with_a_conclusion_and_completed_at_as_done(
    tmp_path, case, conclusion, completed_at, boards
):
    pr = {
        "number": 130, "title": "A check GitHub left in progress", "headRefOid": "13" + "0" * 37 + "1",
        "headRefName": "lane/stuck", "baseRefName": "main", "isDraft": False,
        "isCrossRepository": False, "mergeable": "MERGEABLE", "labels": [{"name": "ready"}],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "ci-gate", "status": "COMPLETED", "conclusion": "SUCCESS",
             "completedAt": "2026-10-22T19:01:00Z"},
            {"__typename": "CheckRun", "name": "secret scan", "status": "IN_PROGRESS",
             "conclusion": conclusion, "completedAt": completed_at},
        ],
    }
    prs = tmp_path / "stuck.json"
    prs.write_text(json.dumps([pr]))
    # MUTATION: read status alone (`done: (.status == "COMPLETED")`) -> the
    # first row never boards, PR 2006's wait in the train.
    assert (["130"] == [r[0] for r in _select(tmp_path, prs=prs)]) is boards, case


def test_select_with_no_open_pull_requests_prints_nothing(tmp_path):
    empty = tmp_path / "none.json"
    empty.write_text("[]")
    assert _select(tmp_path, prs=empty) == []


def test_select_refuses_an_empty_required_list(tmp_path):
    """No required check means `green` would mean nothing: fail closed."""
    proc = _run("select", "--prs", str(FIXTURES / "prs.json"),
                "--required", str(_required_file(tmp_path, [])))
    assert proc.returncode != 0


# --------------------------------------------------------------------------
# judge: a train head is green, red or pending
# --------------------------------------------------------------------------


def _judge(tmp_path: Path, check_runs: list[dict], statuses: list[dict] | None = None,
           required: list[str] = REQUIRED, workflow_runs: list[dict] | None = None):
    cr = tmp_path / "check-runs.json"
    cr.write_text(json.dumps({"total_count": len(check_runs), "check_runs": check_runs}))
    st = tmp_path / "status.json"
    st.write_text(json.dumps({"state": "pending", "statuses": statuses or []}))
    extra: list[str] = []
    if workflow_runs is not None:
        wf = tmp_path / "workflow-runs.json"
        wf.write_text(json.dumps({"total_count": len(workflow_runs), "workflow_runs": workflow_runs}))
        extra = ["--workflow-runs", str(wf)]
    proc = _run("judge", "--check-runs", str(cr), "--statuses", str(st),
                "--required", str(_required_file(tmp_path, required)), *extra)
    assert proc.returncode == 0, proc.stderr
    lines = [line.split("\t") for line in proc.stdout.splitlines() if line]
    verdict = [line[1] for line in lines if line[0] == "verdict"]
    assert len(verdict) == 1, proc.stdout
    failed = [(line[1], line[2]) for line in lines if line[0] == "failed"]
    return verdict[0], failed


def _cr(name: str, status: str = "completed", conclusion: str | None = "success",
        url: str = "https://github.com/o/r/actions/runs/1/job/2") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": url}


def test_judge_green_when_every_required_check_passed(tmp_path):
    verdict, failed = _judge(tmp_path, [_cr("ci-gate"), _cr("secret scan", conclusion="skipped"),
                                        _cr("Trivy", status="in_progress", conclusion=None)])
    assert (verdict, failed) == ("green", [])


def test_judge_counts_a_commit_status_as_a_check(tmp_path):
    verdict, _ = _judge(tmp_path, [_cr("ci-gate")],
                        statuses=[{"context": "secret scan", "state": "success", "target_url": "u"}])
    assert verdict == "green"


def test_judge_pending_while_a_required_check_runs_or_has_not_reported(tmp_path):
    assert _judge(tmp_path, [_cr("ci-gate", status="queued", conclusion=None),
                             _cr("secret scan")])[0] == "pending"
    assert _judge(tmp_path, [_cr("ci-gate")])[0] == "pending"


def test_judge_red_names_the_failing_job_with_its_link(tmp_path):
    url = "https://github.com/example-org/example-repo/actions/runs/77/job/88"
    verdict, failed = _judge(tmp_path, [
        _cr("ci-gate", status="in_progress", conclusion=None),
        _cr("secret scan"),
        _cr("python-unit (3)", conclusion="failure", url=url),
    ])
    assert verdict == "red"
    assert failed == [("python-unit (3)", url)]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "action_required", "startup_failure"])
def test_judge_red_on_any_failed_conclusion_required_or_not(tmp_path, conclusion: str):
    assert _judge(tmp_path, [_cr("ci-gate"), _cr("secret scan"), _cr("Trivy", conclusion=conclusion)])[0] == "red"


def test_judge_reads_a_cancelled_latest_run_as_neither_red_nor_green(tmp_path):
    """#1778: a cancelled run waits for its re-run. Required, it leaves the
    verdict pending; nothing requiring it, it holds nothing."""
    assert _judge(tmp_path, [_cr("ci-gate"), _cr("secret scan"), _cr("Trivy", conclusion="cancelled")])[0] == "green"
    assert _judge(tmp_path, [_cr("ci-gate", conclusion="cancelled"), _cr("secret scan")])[0] == "pending"


def test_judge_red_on_a_failed_commit_status(tmp_path):
    verdict, failed = _judge(tmp_path, [_cr("ci-gate")],
                             statuses=[{"context": "secret scan", "state": "error", "target_url": "https://x/y"}])
    assert verdict == "red"
    assert failed == [("secret scan", "https://x/y")]


# CHECK-RULE-2 (the owner, 2026-10-22, both answers): the judge decides a check by
# the latest run (highest id) of EACH event that ran it, as the worker's
# merge step, swarm-api and auto-merge.yml's gate 5 do. The event is the one
# that made the run's check suite, from `--workflow-runs` (the Actions runs at
# the head); without it each check suite is its own group, as gate 5 groups.


def _run_of(run_id: int, name: str, suite: int, conclusion: str | None = "success",
            status: str = "completed") -> dict:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion,
            "check_suite": {"id": suite},
            "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{run_id}"}


def _events(**suites: str) -> list[dict]:
    return [{"id": 900 + int(suite), "check_suite_id": int(suite), "event": event}
            for suite, event in suites.items()]


def test_judge_a_required_check_re_run_green_in_its_event_is_green(tmp_path):
    runs = [_run_of(1, "ci-gate", 101, "failure"), _run_of(2, "ci-gate", 103), _run_of(3, "secret scan", 101)]
    verdict, failed = _judge(tmp_path, runs, workflow_runs=_events(**{"101": "push", "103": "push"}))
    # MUTATION: judge every run instead of each event's latest -> "red".
    assert (verdict, failed) == ("green", [])


def test_judge_without_workflow_runs_judges_each_check_suite_on_its_own(tmp_path):
    runs = [_run_of(1, "ci-gate", 101, "failure"), _run_of(2, "ci-gate", 102), _run_of(3, "secret scan", 101)]
    verdict, failed = _judge(tmp_path, runs)
    # MUTATION: group by name alone when no event is known -> "green".
    assert verdict == "red"
    assert failed == [("ci-gate", runs[0]["html_url"])]
    # The control: the same suites, told they are one event, go green.
    assert _judge(tmp_path, runs, workflow_runs=_events(**{"101": "push", "102": "push"}))[0] == "green"


#: A check nothing requires, by (run id, suite, status, conclusion); suites
#: 101 and 103 are push, 102 pull_request. (case, runs, verdict)
JUDGE_OTHER = [
    ("push failure then success", [(1, 101, "completed", "failure"), (2, 103, "completed", "success")], "green"),
    ("push in_progress then success", [(1, 101, "in_progress", None), (2, 103, "completed", "success")], "green"),
    ("push success, pull_request failure", [(1, 101, "completed", "success"), (2, 102, "completed", "failure")], "red"),
    ("push success then failure", [(1, 101, "completed", "success"), (2, 103, "completed", "failure")], "red"),
]


@pytest.mark.parametrize(("case", "runs", "verdict"), JUDGE_OTHER, ids=[c[0] for c in JUDGE_OTHER])
def test_judge_a_check_nothing_requires_by_the_latest_run_of_each_event(tmp_path, case, runs, verdict):
    checks = [_run_of(9, "ci-gate", 101), _run_of(8, "secret scan", 101)]
    checks += [_run_of(i, "terraform", suite, conclusion, status) for i, suite, status, conclusion in runs]
    got, _failed = _judge(tmp_path, checks, workflow_runs=_events(
        **{"101": "push", "102": "pull_request", "103": "push"}))
    # MUTATION: judge every run instead of each event's latest -> the first
    # case is "red". MUTATION: take the newest run whatever its event -> the
    # pull_request case's push run could outvote it.
    assert got == verdict, case


# CHECK-STUCK (the owner, 2026-10-23, part of #2031): a check run that carries a
# conclusion AND a completed_at is judged by its conclusion, whatever
# `status` says. GitHub left PR 2006's `secret scan` at `in_progress` with
# `conclusion: success` and a `completed_at`; its workflow run was completed.
# A run with no conclusion stays pending; `completed` with no conclusion is
# not green (the judge reads it pending: done, but neither good nor bad).
STUCK_AT = "2026-10-22T19:05:52Z"


#: (case, status, conclusion, completed_at, verdict)
STUCK = [
    ("in_progress, success, completed_at", "in_progress", "success", STUCK_AT, "green"),
    ("in_progress, failure, completed_at", "in_progress", "failure", STUCK_AT, "red"),
    ("in_progress, no conclusion", "in_progress", None, None, "pending"),
    ("in_progress, success, no completed_at", "in_progress", "success", None, "pending"),
    ("completed, no conclusion", "completed", None, STUCK_AT, "pending"),
]


@pytest.mark.parametrize(("case", "status", "conclusion", "completed_at", "verdict"), STUCK,
                         ids=[row[0] for row in STUCK])
def test_judge_a_run_with_a_conclusion_and_completed_at_is_finished(
    tmp_path, case, status, conclusion, completed_at, verdict
):
    stuck = {**_cr("secret scan", status=status, conclusion=conclusion), "completed_at": completed_at}
    got, failed = _judge(tmp_path, [_cr("ci-gate"), stuck])
    # MUTATION: read status alone (`done: (.status == "completed")`) -> the
    # first row is "pending" (PR 2006's 6 h wait) and the second not "red".
    # MUTATION: accept a conclusion without a completed_at -> the fourth row
    # reads "green".
    assert got == verdict, (case, got, failed)
    # MUTATION: read status alone -> a run GitHub concluded `failure` is not
    # named among the failed.
    assert ([name for name, _url in failed] == ["secret scan"]) is (verdict == "red"), failed


def test_judge_a_stuck_failure_that_nothing_requires_is_red(tmp_path):
    stuck = {**_cr("Trivy", status="in_progress", conclusion="failure"), "completed_at": STUCK_AT}
    # MUTATION: read status alone -> a check nothing requires, still "in
    # progress", holds nothing and the head reads "green".
    assert _judge(tmp_path, [_cr("ci-gate"), _cr("secret scan"), stuck])[0] == "red"


def test_the_tick_passes_the_actions_runs_when_a_check_ran_twice():
    """MUTATION: keep calling the judge with --check-runs alone -> a train's
    head is judged each check suite on its own, and a push re-run green in a
    new suite still reads red."""
    text = SCRIPT.read_text()
    start = text.index("\nhandle_train() {")
    tick = text[start:text.index("\n}\n", start)]
    assert "actions/runs?head_sha=${head}" in tick
    assert re.search(r"cmd_judge --check-runs \"\$\{cr\}\".*--workflow-runs", tick, re.S), tick[:200]


# --------------------------------------------------------------------------
# plan: bisect by halves, eject a lone culprit
# --------------------------------------------------------------------------


def _plan(verdict: str, train: str, suspects: str = "") -> dict[str, str]:
    proc = _run("plan", "--verdict", verdict, "--train", train, "--suspects", suspects)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert set(out) == {"eject", "next", "suspects"}, proc.stdout
    return out


def test_a_red_train_of_four_is_halved():
    assert _plan("red", "101 102 103 104") == {"eject": "", "next": "101 102", "suspects": "103 104"}


def test_a_red_train_of_three_keeps_the_larger_half_first():
    assert _plan("red", "101 102 103") == {"eject": "", "next": "101 102", "suspects": "103"}


def test_halving_keeps_the_suspects_held_back_by_an_earlier_halving():
    assert _plan("red", "101 102", "103 104") == {"eject": "", "next": "101", "suspects": "102 103 104"}


def test_a_red_train_of_one_ejects_it_and_tries_the_suspects_next():
    assert _plan("red", "101", "102 103 104") == {"eject": "101", "next": "102 103 104", "suspects": ""}


def test_a_red_train_of_one_with_no_suspects_ejects_it_and_ends_the_bisection():
    assert _plan("red", "101") == {"eject": "101", "next": "", "suspects": ""}


def test_a_green_bisection_train_hands_on_to_the_held_back_half():
    assert _plan("green", "101 102", "103 104") == {"eject": "", "next": "103 104", "suspects": ""}


def test_a_green_train_with_no_suspects_plans_nothing():
    assert _plan("green", "101 102 103 104") == {"eject": "", "next": "", "suspects": ""}


@pytest.mark.parametrize("bad", ["101 x", "101;rm", "-1", ""])
def test_plan_refuses_a_train_that_is_not_pull_request_numbers(bad: str):
    assert _run("plan", "--verdict", "red", "--train", bad, "--suspects", "").returncode != 0


# --------------------------------------------------------------------------
# compose: main plus one squash per pull request, in order
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": os.environ["PATH"], "HOME": str(repo),
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def history(tmp_path: Path):
    """main with one file, and four pull request heads branched from it:
    101 adds a.txt; 102 rewrites shared.txt; 103 rewrites shared.txt the other
    way (conflicts with 102, not with main); 104 adds d.txt in two commits."""
    _need("git", "bash", "jq")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    heads = {}

    def branch(n: int, edits: list[dict[str, str]]):
        _git(repo, "checkout", "-q", "-b", f"pr{n}", base)
        for i, files in enumerate(edits):
            for name, content in files.items():
                (repo / name).write_text(content)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", f"pr{n} commit {i}")
        heads[n] = _git(repo, "rev-parse", "HEAD")

    branch(101, [{"a.txt": "a\n"}])
    branch(102, [{"shared.txt": "from 102\n"}])
    branch(103, [{"shared.txt": "from 103\n"}])
    branch(104, [{"d.txt": "d1\n"}, {"d.txt": "d2\n"}])
    _git(repo, "checkout", "-q", "main")
    return repo, base, heads


def _compose(tmp_path: Path, repo: Path, base: str, rows: list[tuple[int, str, str]]):
    prs = tmp_path / "train.tsv"
    prs.write_text("".join(f"{n}\t{sha}\t{title}\n" for n, sha, title in rows))
    proc = _run("compose", "--repo", str(repo), "--base", base, "--prs", str(prs))
    assert proc.returncode == 0, proc.stderr
    lines = [line.split("\t") for line in proc.stdout.splitlines() if line]
    included = [int(line[1]) for line in lines if line[0] == "included"]
    dropped = [int(line[1]) for line in lines if line[0] == "dropped"]
    head = [line[1] for line in lines if line[0] == "head"]
    assert len(head) == 1, proc.stdout
    return included, dropped, head[0]


def test_compose_squashes_each_pull_request_in_order_under_its_title(tmp_path, history):
    repo, base, heads = history
    included, dropped, head = _compose(tmp_path, repo, base, [
        (101, heads[101], "The first change"),
        (104, heads[104], "The fourth change, in two commits"),
    ])
    assert (included, dropped) == ([101, 104], [])
    subjects = _git(repo, "log", "--format=%s", f"{base}..{head}").splitlines()
    assert subjects == ["The fourth change, in two commits (#104)", "The first change (#101)"]
    # One commit per pull request, each with ONE parent: squashed, not merged.
    for sha in _git(repo, "rev-list", f"{base}..{head}").splitlines():
        assert len(_git(repo, "rev-list", "--parents", "-n", "1", sha).split()) == 2
    assert _git(repo, "show", f"{head}:a.txt") == "a"
    assert _git(repo, "show", f"{head}:d.txt") == "d2"
    assert _git(repo, "merge-base", "--is-ancestor", base, head) == ""


def test_compose_drops_a_pull_request_that_conflicts_with_one_ahead_of_it(tmp_path, history):
    repo, base, heads = history
    included, dropped, head = _compose(tmp_path, repo, base, [
        (102, heads[102], "Two"),
        (103, heads[103], "Three"),
        (101, heads[101], "One"),
    ])
    assert (included, dropped) == ([102, 101], [103])
    assert _git(repo, "show", f"{head}:shared.txt") == "from 102"
    assert _git(repo, "log", "--format=%s", f"{base}..{head}").splitlines() == ["One (#101)", "Two (#102)"]


def test_compose_leaves_the_repository_s_own_checkout_untouched(tmp_path, history):
    repo, base, heads = history
    before = _git(repo, "rev-parse", "HEAD")
    _compose(tmp_path, repo, base, [(102, heads[102], "Two"), (103, heads[103], "Three")])
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "shared.txt").read_text() == "base\n"


def test_compose_with_every_pull_request_dropped_reports_the_base_as_head(tmp_path, history):
    repo, base, heads = history
    included, dropped, head = _compose(tmp_path, repo, base, [(101, "f" * 40, "Not fetched")])
    assert (included, dropped, head) == ([], [101], base)


def test_compose_takes_a_title_with_shell_metacharacters_as_text(tmp_path, history):
    repo, base, heads = history
    title = 'It handles $(touch pwned) and `x` and "quotes"'
    _, _, head = _compose(tmp_path, repo, base, [(101, heads[101], title)])
    assert _git(repo, "log", "-1", "--format=%s", head) == f"{title} (#101)"
    assert not (tmp_path / "pwned").exists()


# --------------------------------------------------------------------------
# the state a train pull request carries
# --------------------------------------------------------------------------

STATE = {
    "id": "18000000001-1",
    "base": "a" * 40,
    "head": "b" * 40,
    "started": "1760400000",
    "dry_run": "false",
    "prs": f"101:{'1' * 40} 104:{'4' * 40}",
    "suspects": "102 103",
}


def _render(state: dict[str, str]) -> subprocess.CompletedProcess:
    return _run("state-render", *[f"{k}={v}" for k, v in state.items()])


def test_state_round_trips_through_a_pull_request_body(tmp_path):
    proc = _render(STATE)
    assert proc.returncode == 0, proc.stderr
    body = tmp_path / "body.md"
    body.write_text("A train.\n\nSome prose a person wrote.\n\n" + proc.stdout)
    for key, value in STATE.items():
        got = _run("state-get", "--body", str(body), "--key", key)
        assert got.returncode == 0, got.stderr
        assert got.stdout.rstrip("\n") == value, key


def test_state_allows_no_suspects(tmp_path):
    proc = _render({**STATE, "suspects": ""})
    body = tmp_path / "body.md"
    body.write_text(proc.stdout)
    got = _run("state-get", "--body", str(body), "--key", "suspects")
    assert (got.returncode, got.stdout.strip()) == (0, "")


@pytest.mark.parametrize("key, value", [
    ("base", "main"),
    ("head", "b" * 39),
    ("started", "yesterday"),
    ("dry_run", "maybe"),
    ("prs", "101"),
    ("prs", f"101:{'1' * 40};curl x"),
    ("suspects", "102,$(id)"),
    ("id", "../main"),
])
def test_state_get_refuses_a_value_the_train_never_writes(tmp_path, key: str, value: str):
    rendered = _render(STATE).stdout
    body = tmp_path / "body.md"
    body.write_text(re.sub(rf"^{key}=.*$", f"{key}={value}", rendered, flags=re.M))
    assert _run("state-get", "--body", str(body), "--key", key).returncode != 0


def test_state_get_refuses_a_body_with_no_state(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Just prose.\n")
    assert _run("state-get", "--body", str(body), "--key", "head").returncode != 0


# --------------------------------------------------------------------------
# the workflow
# --------------------------------------------------------------------------


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def train_workflow() -> dict:
    return _workflow(WORKFLOW)


def test_the_train_runs_every_five_minutes_and_on_demand(train_workflow):
    on = train_workflow["on"]
    assert on["schedule"] == [{"cron": "*/5 * * * *"}]
    assert "workflow_dispatch" in on
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"


def test_a_finished_train_ci_run_wakes_the_train_without_waiting_for_the_schedule(train_workflow):
    assert set(train_workflow["on"]["workflow_run"]["workflows"]) == {"application", "security", "auto-merge"}
    assert train_workflow["on"]["workflow_run"]["types"] == ["completed"]
    job = train_workflow["jobs"]["tick"]
    assert "startsWith(github.event.workflow_run.head_branch, 'train/')" in job["if"]
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in job["if"]


def test_the_schedule_does_nothing_unless_the_train_is_on(train_workflow):
    cond = train_workflow["jobs"]["tick"]["if"]
    assert "github.event_name == 'schedule' && vars.MERGE_TRAIN == 'on'" in cond


def test_the_train_never_starts_on_a_pull_request_event(train_workflow):
    """A `pull_request` trigger runs the pull request's copy of this file, on a
    ref the `repo-secrets` environment refuses; `pull_request_target` would run
    a write token on an event its author shapes. The `ready` label reaches the
    train through auto-merge.yml's completion instead."""
    assert not {"pull_request", "pull_request_target"} & set(train_workflow["on"])
    assert "auto-merge" in train_workflow["on"]["workflow_run"]["workflows"]


#: The context paths the tick's `if:` may read. Anything else is refused by
#: _tick_runs() rather than guessed at.
_IF_PATH = re.compile(r"\b(?:vars|github)(?:\.[A-Za-z_][\w-]*|\[\d+\])+")
#: The placeholder repository (docs/design/generic-repository.md, section 3.1).
THIS_REPO = "example-org/example-repo"


def _tick_runs(
    cond: str,
    event: str,
    *,
    train: str = "",
    name: str = "application",
    run_event: str = "pull_request",
    conclusion: str = "success",
    branch: str = "swarm/task_lane",
    head_repo: str = THIS_REPO,
    repository: str = THIS_REPO,
    listed: bool | None = None,
) -> bool:
    """Whether GitHub starts the tick job: evaluates the `if:` with `&&`,
    `||`, `==`, `!=` and startsWith(), as GitHub does. `train` is
    vars.MERGE_TRAIN (unset reads as ''); the workflow_run fields are those of
    the run that completed, and absent (null) on any other event. `listed` is
    whether the run's `pull_requests` names one: GitHub leaves it empty for a
    fork's pull request, so by default it follows `head_repo`."""
    is_run = event == "workflow_run"
    if listed is None:
        listed = head_repo == repository
    values = {
        "github.repository": repository,
        # GitHub's own flag: a copy of THIS_REPO under another name is a fork.
        "github.event.repository.fork": repository != THIS_REPO,
        "github.event_name": event,
        "vars.MERGE_TRAIN": train,
        "github.event.workflow_run.name": name if is_run else None,
        "github.event.workflow_run.event": run_event if is_run else None,
        "github.event.workflow_run.conclusion": conclusion if is_run else None,
        "github.event.workflow_run.head_branch": branch if is_run else None,
        "github.event.workflow_run.head_repository.full_name": head_repo if is_run else None,
        "github.event.workflow_run.pull_requests[0]": {"number": 4242} if is_run and listed else None,
    }
    names: dict[str, object] = {}

    def bind(match: re.Match) -> str:
        path = match.group(0)
        assert path in values, f"_tick_runs() does not model {path!r}"
        names[f"_v{len(names)}"] = values[path]
        return f"_v{len(names) - 1}"

    py = _IF_PATH.sub(bind, cond.strip())
    py = py.replace("&&", " and ").replace("||", " or ").replace("startsWith(", "_starts(")
    py = re.sub(r"\bnull\b", "None", py)
    py = re.sub(r"\bfalse\b", "False", re.sub(r"\btrue\b", "True", py))
    helpers = {"_starts": lambda value, prefix: str(value or "").lower().startswith(prefix.lower())}
    return bool(eval(py, {"__builtins__": {}}, {**names, **helpers}))  # noqa: S307


@pytest.mark.parametrize(
    ("why", "event", "kwargs", "runs"),
    [
        # The fallback and the hand-started run, as before.
        ("schedule, train on", "schedule", {"train": "on"}, True),
        ("schedule, train off", "schedule", {"train": "off"}, False),
        ("schedule, train unset", "schedule", {}, False),
        ("dispatch, train off: a dry run", "workflow_dispatch", {}, True),
        ("dispatch, train on", "workflow_dispatch", {"train": "on"}, True),
        # A train's CI: judged whatever MERGE_TRAIN says, and on any conclusion.
        ("train CI red, train off", "workflow_run", {"branch": "train/7", "conclusion": "failure"}, True),
        ("train CI green, train on", "workflow_run", {"branch": "train/7", "train": "on"}, True),
        ("security on a train", "workflow_run", {"branch": "train/7", "name": "security"}, True),
        (
            "a fork's train/ branch",
            "workflow_run",
            {"branch": "train/7", "head_repo": "someone/example-repo"},
            False,
        ),
        # Any branch's pull request run finishing green, with the train on.
        ("lane CI green, train on", "workflow_run", {"train": "on"}, True),
        ("lane security green, train on", "workflow_run", {"train": "on", "name": "security"}, True),
        ("lane CI green, train off", "workflow_run", {"train": "off"}, False),
        ("lane CI green, train unset", "workflow_run", {}, False),
        ("lane CI red, train on", "workflow_run", {"train": "on", "conclusion": "failure"}, False),
        ("lane CI cancelled, train on", "workflow_run", {"train": "on", "conclusion": "cancelled"}, False),
        (
            "main's push run, train on",
            "workflow_run",
            {"train": "on", "run_event": "push", "branch": "main"},
            False,
        ),
        (
            "a fork's pull request run, train on",
            "workflow_run",
            {"train": "on", "head_repo": "someone/example-repo"},
            False,
        ),
        # `ready` added: auto-merge.yml's pull_request_target run completes.
        (
            "ready added, train on",
            "workflow_run",
            {"train": "on", "name": "auto-merge", "run_event": "pull_request_target", "branch": "main"},
            True,
        ),
        # A fork's pull request raises auto-merge runs too (an edited title is
        # one): none of them may start a self-hosted tick that mints the App's
        # token, nor take the pending slot from an owner's dispatch.
        (
            "a fork's pull request's auto-merge run, train on",
            "workflow_run",
            {
                "train": "on",
                "name": "auto-merge",
                "run_event": "pull_request_target",
                "branch": "main",
                "head_repo": "someone/example-repo",
            },
            False,
        ),
        (
            "auto-merge run naming no pull request, train on",
            "workflow_run",
            {"train": "on", "name": "auto-merge", "run_event": "pull_request_target", "listed": False},
            False,
        ),
        (
            "a fork's head named in pull_requests, train on",
            "workflow_run",
            {
                "train": "on",
                "name": "auto-merge",
                "run_event": "pull_request_target",
                "head_repo": "someone/example-repo",
                "listed": True,
            },
            False,
        ),
        (
            "ready added, train off",
            "workflow_run",
            {"train": "off", "name": "auto-merge", "run_event": "pull_request_target", "branch": "main"},
            False,
        ),
        (
            "auto-merge's requeue dispatch, train on",
            "workflow_run",
            {"train": "on", "name": "auto-merge", "run_event": "workflow_dispatch", "branch": "main"},
            False,
        ),
        (
            "auto-merge's schedule, train on",
            "workflow_run",
            {"train": "on", "name": "auto-merge", "run_event": "schedule", "branch": "main"},
            False,
        ),
        (
            "auto-merge's own workflow_run, train on",
            "workflow_run",
            {"train": "on", "name": "auto-merge", "run_event": "workflow_run", "branch": "main"},
            False,
        ),
        (
            "auto-merge on a train branch, train off: not a train's CI",
            "workflow_run",
            {"name": "auto-merge", "run_event": "pull_request_target", "branch": "train/7"},
            False,
        ),
        # Anywhere but this repository, nothing.
        ("a fork of the repository", "schedule", {"train": "on", "repository": "someone/example-repo"}, False),
        (
            "a fork of the repository, its own train",
            "workflow_run",
            {"branch": "train/7", "repository": "someone/example-repo", "head_repo": "someone/example-repo"},
            False,
        ),
    ],
)
def test_when_a_tick_runs(train_workflow, why: str, event: str, kwargs: dict, runs: bool):
    """The WHEN block of merge-train.yml, row by row (part of #2688): the train
    ticks on the events that can change what it does, because GitHub ran the
    five-minute schedule only every four to seven hours. MUTATION: drop a
    clause, or its MERGE_TRAIN or conclusion guard, and a row goes red."""
    assert event in train_workflow["on"], why
    assert _tick_runs(train_workflow["jobs"]["tick"]["if"], event, **kwargs) is runs, why


def test_the_tick_names_no_repository_by_a_literal(train_workflow):
    """The repository is `github.repository` and GitHub's `fork` flag, never a
    written address (docs/design/generic-repository.md, section 2.2): a literal
    would tie the workflow to one deployment's repository, and the tick would
    never run in a copy under another name. MUTATION: compare
    `github.repository` with a quoted owner/name and this goes red."""
    cond = train_workflow["jobs"]["tick"]["if"]
    assert not re.search(r"github\.repository\s*[!=]=\s*'", cond), cond
    assert "github.event.repository.fork == false" in cond, cond
    # The address of a fork is irrelevant; the flag alone keeps it out.
    assert _tick_runs(cond, "workflow_dispatch", repository="someone/example-repo") is False
    assert _tick_runs(cond, "workflow_dispatch") is True


def test_one_train_at_a_time_and_a_running_one_is_never_cancelled(train_workflow):
    # On the job: a workflow-level group would let every skipped
    # `workflow_run` run (any branch's CI finishing) cancel a pending tick.
    assert "concurrency" not in train_workflow
    assert train_workflow["jobs"]["tick"]["concurrency"] == {"group": "merge-train", "cancel-in-progress": False}


def test_the_train_lands_with_the_merge_app_token_and_never_a_pat(train_workflow):
    text = WORKFLOW.read_text()
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == {"MERGE_APP_PRIVATE_KEY"}
    steps = train_workflow["jobs"]["tick"]["steps"]
    mint = [s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    assert len(mint) == 1
    assert mint[0]["with"]["app-id"] == "${{ vars.MERGE_APP_ID }}"
    assert mint[0]["with"]["private-key"] == "${{ secrets.MERGE_APP_PRIVATE_KEY }}"
    run = [s for s in steps if "merge-train.sh tick" in str(s.get("run", ""))]
    assert len(run) == 1
    assert run[0]["env"]["APP_TOKEN"] == f"${{{{ steps.{mint[0]['id']}.outputs.token }}}}"
    assert run[0]["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_the_train_runs_main_s_scripts_and_no_pull_request_s_code(train_workflow):
    steps = train_workflow["jobs"]["tick"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_the_github_token_asks_only_for_what_the_train_does_with_it(train_workflow):
    assert train_workflow["permissions"] == {}
    # actions: write -- read: the Actions runs at a train's head, whose events
    # the judge groups a check's runs by (CHECK-RULE-2); write: rule 1
    # re-runs a red train's failed jobs with this token
    # (MERGE_TRAIN_RERUN_BY=workflow, the owner's pick). MUTATION: set it to
    # read -> GitHub refuses every re-run and a red train bisects at once.
    permissions = dict(train_workflow["jobs"]["tick"]["permissions"])
    assert permissions.pop("actions") == "write"
    assert permissions == {
        "contents": "read",
        "checks": "read",
        "statuses": "write",
        "pull-requests": "write",
    }


def test_the_tick_learns_the_finished_run_s_branch_through_env(train_workflow):
    """The script tells a train's own CI (judge only) from any other
    `workflow_run` (start a train when none is open) by the finished run's head
    branch -- a value a pull request's author picks, so it reaches the script
    through env:, never interpolated into the shell line."""
    (step,) = [s for s in train_workflow["jobs"]["tick"]["steps"] if s.get("name") == "one tick of the train"]
    assert step["env"]["WORKFLOW_RUN_HEAD_BRANCH"] == "${{ github.event.workflow_run.head_branch }}"
    assert "head_branch" not in step["run"]


def test_the_workflow_never_expands_an_expression_inside_run(train_workflow):
    for step in train_workflow["jobs"]["tick"]["steps"]:
        assert "${{" not in str(step.get("run", "")), step.get("name")


def test_application_yml_is_not_touched_by_the_train():
    """The train is a draft pull request from `train/<id>`, so application.yml,
    security.yml and terraform.yml run their `pull_request` jobs on it: every
    check the ruleset requires, and never `build`, which mints a Google identity
    the pool refuses to any ref but main. A `push: train/**` trigger would run
    `build` on the train ref and fail every train (terraform/bootstrap/wif.tf)."""
    on = _workflow(REPO / ".github" / "workflows" / "application.yml")["on"]
    # Main only: a lane head runs CI once, on its pull request (owner,
    # 2026-11-02, test_ci_once_per_head.py).
    assert on["push"]["branches"] == ["main"]
    assert not any(b.startswith("train/") for b in on["push"]["branches"])
    assert "pull_request" in on


# --------------------------------------------------------------------------
# tick: which `workflow_run` starts a train
# --------------------------------------------------------------------------

#: gh for a tick that finds no train open: main's required checks, and the
#: open pull requests from FAKE_TICK_PRS. Any other call fails the tick.
FAKE_TICK_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
case "$*" in
  "api repos/${GH_REPO}/rules/branches/main --paginate") echo '[]' ;;
  "api repos/${GH_REPO}/branches/main") cat "${FAKE_GH_BRANCH}" ;;
  "pr list "*) cat "${FAKE_TICK_PRS}" ;;
  *) echo "fake gh: unexpected call: $*" >&2; exit 3 ;;
esac
"""


@pytest.fixture
def tick(tmp_path: Path):
    """Runs `merge-train.sh tick` with no train open and no pull request green
    and `ready`: a tick that starts a train says "no ready pull request is
    green at its head: no train"; one that only judges, "no train in flight"."""
    _need("bash", "jq", "git")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_TICK_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "branch.json").write_text(json.dumps(PROTECTED))
    (tmp_path / "prs.json").write_text("[]")

    def run(event: str, *, train: str = "on", branch: str | None = None, dry: str = "false"):
        for name in ("gh.log", "summary.md"):
            (tmp_path / name).write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": THIS_REPO,
            "GH_TOKEN": "fake-" + secrets.token_hex(4),
            "APP_TOKEN": "fake-" + secrets.token_hex(4),
            "REPO_DIR": str(tmp_path),
            "RUN_ID": "1",
            "RUN_ATTEMPT": "1",
            "RUN_URL": "https://example.invalid/run/1",
            "EVENT_NAME": event,
            "MERGE_TRAIN": train,
            "DRY_RUN_INPUT": dry,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "FAKE_GH_BRANCH": str(tmp_path / "branch.json"),
            "FAKE_TICK_PRS": str(tmp_path / "prs.json"),
        }
        if branch is not None:
            env["WORKFLOW_RUN_HEAD_BRANCH"] = branch
        proc = subprocess.run([str(SCRIPT), "tick"], env=env, capture_output=True, text=True,
                              timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        return (tmp_path / "summary.md").read_text()

    return run


STARTS = "no ready pull request is green at its head: no train"
JUDGES_ONLY = "no train in flight"


@pytest.mark.parametrize(
    ("why", "branch"),
    [
        ("a lane's CI finished green", "swarm/task_lane"),
        ("ready added: auto-merge's run on a lane", "swarm/task_lane"),
        ("a run whose branch is not given", None),
    ],
)
def test_a_pull_request_s_workflow_run_with_no_train_open_starts_one(tick, why: str, branch):
    """The event triggers exist for exactly this moment: no train open, and a
    pull request just became boardable. A tick that only looked for a train in
    flight would board nothing until the schedule, which GitHub ran every four
    to seven hours (security review of #3183, finding 1)."""
    out = tick("workflow_run", branch=branch)
    assert STARTS in out, why
    assert JUDGES_ONLY not in out, why


def test_a_train_branch_s_own_run_with_no_train_open_only_judges(tick):
    out = tick("workflow_run", branch="train/41-1")
    assert JUDGES_ONLY in out
    assert STARTS not in out


def test_a_pull_request_s_workflow_run_with_the_train_off_starts_nothing(tick):
    """The `if:` admits a non-train `workflow_run` only while the train is on;
    should one run anyway, it starts no train, dry or not, as a schedule tick."""
    out = tick("workflow_run", train="off", branch="swarm/task_lane")
    assert "MERGE_TRAIN is not on: no train started" in out
    assert STARTS not in out


@pytest.mark.parametrize(("event", "train", "starts"), [
    ("schedule", "on", True),
    ("schedule", "off", False),
    ("workflow_dispatch", "off", True),
])
def test_schedule_and_dispatch_ticks_start_a_train_as_before(tick, event: str, train: str, starts: bool):
    assert (STARTS in tick(event, train=train)) is starts


# --------------------------------------------------------------------------
# auto-merge.yml leaves `ready` to the train when it is on
# --------------------------------------------------------------------------


@pytest.fixture
def gate(tmp_path: Path):
    _need("bash", "jq")
    script = _step(_workflow(AUTO_MERGE)["jobs"]["enable"], "gate")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(merge_train: str | None, event_name: str = "pull_request_target"):
        files = {
            "branch": PROTECTED,
            "rules": [],
            "check-runs": {"check_runs": [{"name": "shellcheck", "status": "completed", "conclusion": "success"}]},
        }
        paths = {}
        for name, content in files.items():
            paths[name] = tmp_path / f"{name}.json"
            paths[name].write_text(json.dumps(content))
        for name in ("gh.log", "comments.md", "summary.md", "output.txt"):
            (tmp_path / name).write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "example-org/example-repo",
            "EVENT_NAME": event_name,
            "PR_NUMBER": "4242",
            "PR_TITLE": "A fact-style headline",
            "BASE_REF": "main",
            "HEAD_SHA": "0000000000000000000000000000000000abcd",
            "MERGE_APP_ID": "123456",
            "HAS_MERGE_APP_KEY": "true",
            "DEPENDENCY_REFUSAL": "",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "FAKE_GH_BRANCH": str(paths["branch"]),
            "FAKE_GH_CHECK_RUNS": str(paths["check-runs"]),
            "FAKE_GH_RULES": str(paths["rules"]),
            "FAKE_GH_COMMENTS": str(tmp_path / "comments.md"),
        }
        if merge_train is not None:
            env["MERGE_TRAIN"] = merge_train
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                              timeout=30, check=False)
        return proc, _outputs(tmp_path / "output.txt"), (tmp_path / "comments.md").read_text()

    return run


@pytest.mark.parametrize("value", [None, "", "off", "ON ", "true"])
def test_with_the_train_off_a_green_gate_still_says_merge(gate, value):
    proc, outputs, _ = gate(value)
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("decision") == "merge"


def test_with_the_train_on_a_green_gate_leaves_the_pull_request_to_the_train(gate):
    proc, outputs, comments = gate("on")
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("decision") == "train"
    assert "merge train" in comments.lower()


def test_with_the_train_on_a_re_evaluation_says_nothing_more_on_the_pull_request(gate):
    proc, outputs, comments = gate("on", event_name="workflow_dispatch")
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("decision") == "train"
    assert comments == ""


def test_the_gate_reads_the_train_switch_from_a_repository_variable():
    step = _step(_workflow(AUTO_MERGE)["jobs"]["enable"], "gate")
    assert step["env"]["MERGE_TRAIN"] == "${{ vars.MERGE_TRAIN }}"


def test_a_train_decision_arms_nothing():
    job = _workflow(AUTO_MERGE)["jobs"]["enable"]
    assert _step(job, "merge")["if"] == "steps.gate.outputs.decision == 'merge'"


# --------------------------------------------------------------------------
# the ruleset change: prepared, not applied
# --------------------------------------------------------------------------


def test_the_runbook_carries_the_ruleset_change_and_says_it_is_not_applied():
    text = RUNBOOK.read_text()
    assert "select(.name==\"main-protection\") | .id" in text
    assert 'gh api -X PUT repos/example-org/example-repo/rulesets/"$RULESET_ID"' in text
    assert '"strict_required_status_checks_policy": false' in text
    assert '{"context": "merge-train"}' in text
    assert re.search(r"not applied", text, flags=re.I)
    assert "MERGE_TRAIN" in text


def test_the_runbook_put_keeps_every_rule_the_ruleset_has_today():
    text = RUNBOOK.read_text()
    for rule in ("deletion", "non_fast_forward", "pull_request", "required_status_checks"):
        assert f'"type": "{rule}"' in text, rule
    for context in ("secret scan", "trivy (repo)", "checkov (terraform + kubernetes)",
                    "platform policy assertions", "ci-gate"):
        assert f'{{"context": "{context}"}}' in text, context


def test_the_runbook_says_how_to_roll_back():
    text = RUNBOOK.read_text()
    assert '"strict_required_status_checks_policy": true' in text
    assert re.search(r"^#+ .*roll(ing)? back", text, flags=re.M | re.I)


# --------------------------------------------------------------------------
# a red train: retry once, hold while main is red, else bisect
# --------------------------------------------------------------------------
#
# The owner, 6 November 2026, after CLD measured the train (4 to 6 November): 74 of
# 139 trains were red, mostly on checks known to fail at random ('format /
# unit tests', 'swarm-ui typecheck / component tests', single shards), and 6
# of 18 ejected pull requests later landed with no change. Keep the train and
# fix red trains:
#   rule 1, retry once: a first red at a train head re-runs only the failed
#     jobs of the failing runs, once, and the train stays open;
#   rule 2, hold while main is red: a red train whose failing checks are red
#     on main's head too closes "Held: main is red on <names>", ejects nobody
#     and keeps every `ready` label; no train boards until main's head is
#     green on those checks (or MERGE_TRAIN_MAIN_RED_WAIT_MINUTES pass);
#   rule 3, bisect, as before; a train of one is ejected.
#
# A whole tick, offline: bash, jq, git, and a fake `gh` (Python) serving a
# JSON world and logging every call.

RED_FAKE_GH = r'''#!{python}
import json, os, sys, urllib.parse

args = sys.argv[1:]
world_path = os.environ["FAKE_WORLD"]
with open(world_path) as fh:
    world = json.load(fh)
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")


def save():
    with open(world_path, "w") as fh:
        json.dump(world, fh)


def arg(flag):
    return args[args.index(flag) + 1]


if args[:2] == ["pr", "list"]:
    print(json.dumps(world["closed"] if arg("--state") == "closed" else world["open"]))
    sys.exit(0)
if args[:2] == ["pr", "comment"]:
    with open(arg("--body-file")) as src:
        world.setdefault("comments", {}).setdefault(args[2], []).append(src.read())
    save()
    sys.exit(0)
if args[:2] == ["pr", "close"]:
    world.setdefault("closed_now", []).append(int(args[2]))
    save()
    sys.exit(0)
if args[:2] == ["pr", "view"]:
    if "statusCheckRollup" in arg("--json"):
        # list_open_prs's own read of one `ready` pull request or train, beside the light list.
        print(json.dumps(next(p for p in world["open"] if str(p["number"]) == args[2])))
    else:
        print(json.dumps(world["members"][args[2]]))
    sys.exit(0)
if args[:2] == ["pr", "merge"]:
    world.setdefault("merged", []).append(int(args[2]))
    save()
    sys.exit(0)
if args[:1] == ["api"]:
    method = arg("-X") if "-X" in args else "GET"
    path = next(a for a in args[1:] if a.startswith("repos/"))
    parsed = urllib.parse.urlparse(path)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    parts = parsed.path.split("/")[3:]
    if method == "PATCH":
        world.setdefault("bodies", {})[parts[1]] = json.load(sys.stdin)["body"]
        save()
        sys.exit(0)
    if method == "POST" and parts[:2] == ["actions", "runs"] and parts[3] == "rerun-failed-jobs":
        if world.get("rerun_refused"):
            print("HTTP 403: Resource not accessible by integration", file=sys.stderr)
            sys.exit(1)
        world.setdefault("reruns", []).append(int(parts[2]))
        world["rerun_by"] = "app" if os.environ["GH_TOKEN"] == os.environ["APP_TOKEN"] else "workflow"
        save()
        sys.exit(0)
    if method == "POST" and parts[0] == "statuses":
        sys.exit(0)
    if method == "DELETE":
        world.setdefault("deleted", []).append("/".join(parts))
        save()
        sys.exit(0)
    if parts == ["rules", "branches", "main"]:
        print("[]")
        sys.exit(0)
    if parts == ["branches", "main"]:
        print(json.dumps(world["branch"]))
        sys.exit(0)
    if parts == ["commits", "main"]:
        print(json.dumps({"sha": world["main"]}))
        sys.exit(0)
    if parts[0] == "commits":
        at = world["checks"].get(parts[1], {})
        if parts[2] == "check-runs":
            print(json.dumps({"check_runs": at.get("check_runs", [])}))
        else:
            print(json.dumps({"statuses": at.get("statuses", [])}))
        sys.exit(0)
    if parts == ["actions", "runs"]:
        print(json.dumps({"workflow_runs": world["checks"].get(query["head_sha"], {}).get("runs", [])}))
        sys.exit(0)
    if parts[:2] == ["actions", "workflows"]:
        print(json.dumps({"workflow_runs": []}))
        sys.exit(0)
print("fake gh: unexpected: " + " ".join(args), file=sys.stderr)
sys.exit(64)
'''

#: The required checks the fake serves (PROTECTED's), and the two flaky ones.
UNIT = "format / unit tests"
SHARD = "unit tests (1/10)"
TRAIN_PR = 3500


def _at(passing: list[str], failing: list[str] = (), *, suite_base: int = 900,
        running: bool = False) -> dict:
    """One commit's checks: each name its own Actions run (id = suite id),
    `failing` ones concluded failure, `running` leaves every run queued."""
    runs, check_runs = [], []
    for i, name in enumerate([*passing, *failing]):
        suite = suite_base + i
        bad = name in failing
        check_runs.append({"id": 10 * suite, "name": name, "status": "completed",
                           "conclusion": "failure" if bad else "success",
                           "html_url": f"https://example.invalid/job/{suite}",
                           "check_suite": {"id": suite}})
        runs.append({"id": suite, "check_suite_id": suite, "event": "pull_request",
                     "status": "queued" if running else "completed",
                     "conclusion": None if running else ("failure" if bad else "success")})
    return {"check_runs": check_runs, "statuses": [], "runs": runs}


REQUIRED_TICK = ["shellcheck", "trivy (repo)", "fmt / validate / tflint"]


class RedTick:
    """A tick with train #3500 in flight: main plus `members` squashed."""

    def __init__(self, tmp_path: Path, members: list[int], suspects: str = ""):
        _need("bash", "jq", "git", "awk")
        self.tmp = tmp_path
        origin = tmp_path / "origin"
        origin.mkdir()
        _git(origin, "init", "-q", "-b", "main")
        _git(origin, "config", "user.email", "t@example.invalid")
        _git(origin, "config", "user.name", "t")
        (origin / "a.txt").write_text("a\n")
        _git(origin, "add", "a.txt")
        _git(origin, "commit", "-q", "-m", "base")
        self.base = _git(origin, "rev-parse", "HEAD")
        self.repo = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True)
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "b.txt").write_text("b\n")
        _git(self.repo, "add", "b.txt")
        _git(self.repo, "commit", "-q", "-m", "train")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.main = secrets.token_hex(20)
        self.members = members
        pairs = " ".join(f"{n}:{secrets.token_hex(20)}" for n in members)
        self.state = {"id": "41-1", "base": self.base, "head": self.head,
                      "started": str(int(time.time()) - 1800), "dry_run": "false",
                      "prs": pairs, "suspects": suspects}
        self.extra: list[str] = []
        self.env: dict[str, str] = {}
        self.world: dict = {
            "branch": PROTECTED, "main": self.main, "closed": [], "checks": {},
            "members": {str(n): {"state": "OPEN", "headRefOid": pair.split(":")[1],
                                 "labels": [{"name": "ready"}], "title": f"Change {n}"}
                        for n, pair in zip(members, pairs.split())},
        }
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(RED_FAKE_GH.replace("{python}", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def body(self) -> str:
        rendered = _run("state-render", *[f"{k}={v}" for k, v in self.state.items()], *self.extra)
        assert rendered.returncode == 0, rendered.stderr
        return "A merge train.\n\n" + rendered.stdout

    def run(self, *, train_open: bool = True) -> str:
        world = dict(self.world)
        world["open"] = [{
            "number": TRAIN_PR, "title": "Merge train 41-1", "body": self.body(),
            "headRefOid": self.head, "headRefName": "train/41-1", "baseRefName": "main",
            "isDraft": True, "isCrossRepository": False, "mergeable": "MERGEABLE",
            "labels": [], "statusCheckRollup": [],
        }] if train_open else []
        self.path = self.tmp / "world.json"
        self.path.write_text(json.dumps(world))
        for name in ("gh.log", "summary.md"):
            (self.tmp / name).write_text("")
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "GH_REPO": THIS_REPO,
            "GH_TOKEN": "fake-" + secrets.token_hex(4),
            "APP_TOKEN": "fake-" + secrets.token_hex(4),
            "REPO_DIR": str(self.repo),
            "RUN_ID": "42", "RUN_ATTEMPT": "1",
            "RUN_URL": "https://example.invalid/run/42",
            "EVENT_NAME": "schedule", "MERGE_TRAIN": "on", "DRY_RUN_INPUT": "false",
            "GITHUB_STEP_SUMMARY": str(self.tmp / "summary.md"),
            "FAKE_WORLD": str(self.path), "FAKE_GH_LOG": str(self.tmp / "gh.log"),
            "GIT_CONFIG_NOSYSTEM": "1",
            **self.env,
        }
        proc = subprocess.run([str(SCRIPT), "tick"], env=env, capture_output=True, text=True,
                              timeout=120, check=False)
        assert proc.returncode == 0, proc.stderr
        self.after = json.loads(self.path.read_text())
        return (self.tmp / "summary.md").read_text()

    def comments(self, n: int = TRAIN_PR) -> str:
        return "\n".join(self.after.get("comments", {}).get(str(n), []))

    def recorded(self, key: str) -> str | None:
        m = re.search(rf"^{key}=(.*)$", self.after.get("bodies", {}).get(str(TRAIN_PR), ""), re.M)
        return m.group(1) if m else None

    def ejected(self) -> list[str]:
        return [d for d in self.after.get("deleted", []) if d.endswith("/labels/ready")]


@pytest.fixture
def red_tick(tmp_path: Path):
    return lambda members, suspects="": RedTick(tmp_path, members, suspects)


def _red_at_head(t: RedTick, failing: list[str] = (UNIT,), *, running: bool = False) -> None:
    t.world["checks"][t.head] = _at(REQUIRED_TICK, list(failing), running=running)


def _main(t: RedTick, passing: list[str], failing: list[str] = ()) -> None:
    t.world["checks"][t.main] = _at(passing, list(failing), suite_base=700)


def test_a_first_red_re_runs_only_the_failed_jobs_and_keeps_the_train_open(red_tick):
    t = red_tick([101, 102, 103])
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])
    out = t.run()
    failing_run = next(r["id"] for r in t.world["checks"][t.head]["runs"]
                       if r["conclusion"] == "failure")
    # Only the failing run, through the rerun-failed-jobs endpoint.
    assert t.after.get("reruns") == [failing_run]
    assert t.after.get("rerun_by") == "workflow", "the workflow's own token, by default"
    assert "/rerun\n" not in (t.tmp / "gh.log").read_text() + "\n"
    assert TRAIN_PR not in t.after.get("closed_now", []), "the train stays open"
    assert t.recorded("retry") is not None and t.recorded("retry").endswith(":" + t.head)
    assert not t.ejected()
    assert "rule 1 (retry once)" in out
    assert "rule 1 (retry once)" in t.comments().lower()


def test_a_green_after_the_retry_lands_every_member(red_tick):
    t = red_tick([101, 102, 103])
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    t.world["checks"][t.head] = _at(REQUIRED_TICK)
    t.run()
    assert t.after.get("merged") == [101, 102, 103]
    assert TRAIN_PR in t.after.get("closed_now", [])
    assert "Green, and landed: #101, #102, #103" in t.comments()


def test_a_second_red_on_the_same_head_bisects(red_tick):
    t = red_tick([101, 102, 103, 104])
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])
    out = t.run()
    assert not t.after.get("reruns"), "the retry is spent"
    assert TRAIN_PR in t.after.get("closed_now", [])
    assert "Bisecting" in t.comments() and "rule 3 (bisect)" in t.comments().lower()
    assert "rule 3 (bisect)" in out
    assert not t.ejected()


def test_a_red_while_the_retry_still_runs_waits(red_tick):
    """GitHub queues the re-run's runs when it accepts the call; a tick that
    reads the head before the new check runs exist must not bisect."""
    t = red_tick([101, 102])
    t.extra = [f"retry={int(time.time()) - 60}:{t.head}"]
    _red_at_head(t)
    t.world["checks"][t.head]["runs"][-1]["status"] = "in_progress"
    out = t.run()
    assert TRAIN_PR not in t.after.get("closed_now", [])
    assert "rule 1 (retry once)" in out


def test_a_first_red_while_its_run_still_runs_waits_to_retry(red_tick):
    """A shard fails while the rest of its workflow run is still going: the
    head is red, but GitHub re-runs failed jobs only of a finished run. The
    train waits for it rather than finding nothing to re-run and bisecting."""
    t = red_tick([101, 102])
    _red_at_head(t)
    failing = t.world["checks"][t.head]["runs"][-1]
    failing.update({"status": "in_progress", "conclusion": None})
    out = t.run()
    assert not t.after.get("reruns")
    assert TRAIN_PR not in t.after.get("closed_now", [])
    assert t.recorded("retry") is None
    assert "rule 1 (retry once)" in out


def _stuck_run(t: RedTick) -> None:
    """A run at the head left queued (a runner outage, an approval wait)."""
    t.world["checks"][t.head]["runs"].append(
        {"id": 990, "check_suite_id": 990, "event": "pull_request",
         "status": "queued", "conclusion": None})


def test_a_first_red_past_the_age_limit_retries_without_waiting_for_a_stuck_run(red_tick):
    t = red_tick([101, 102])
    t.state["started"] = str(int(time.time()) - 91 * 60)
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])
    _stuck_run(t)
    out = t.run()
    failing_run = next(r["id"] for r in t.world["checks"][t.head]["runs"]
                       if r["conclusion"] == "failure")
    assert t.after.get("reruns") == [failing_run]
    assert t.recorded("retry") is not None
    assert "past the 90-minute limit: not waiting" in out
    assert "re-running the failed jobs" in out


def test_a_red_after_the_retry_past_the_age_limit_does_not_wait(red_tick):
    t = red_tick([101, 102, 103, 104])
    t.extra = [f"retry={int(time.time()) - 91 * 60}:{t.head}"]
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])
    _stuck_run(t)
    out = t.run()
    assert not t.after.get("reruns"), "the retry is spent"
    assert TRAIN_PR in t.after.get("closed_now", [])
    assert "past the 90-minute limit: not waiting" in out
    assert "rule 3 (bisect)" in out


def test_a_train_of_one_is_ejected_after_its_retry(red_tick):
    t = red_tick([101])
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])
    t.run()
    assert t.ejected() == ["issues/101/labels/ready"]
    assert "rule 3" in t.comments(101).lower()


def test_a_red_train_while_main_is_red_on_the_same_check_is_held(red_tick):
    t = red_tick([101, 102, 103], suspects="104")
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_at_head(t, [UNIT, SHARD])
    _main(t, REQUIRED_TICK, [UNIT])
    out = t.run()
    assert TRAIN_PR in t.after.get("closed_now", [])
    assert t.comments().startswith(f"Held: main is red on {UNIT}\n")
    assert not t.ejected(), "every ready label stays"
    assert set(t.after.get("comments", {})) == {str(TRAIN_PR)}, "no member is told anything"
    assert t.recorded("main_red") is not None
    assert json.loads(t.recorded("main_red_checks")) == [UNIT]
    assert "rule 2 (hold while main is red)" in out
    # Nothing boards behind it: no train branch pushed, no bisection's half.
    assert "pr create" not in (t.tmp / "gh.log").read_text()


def test_a_held_train_of_one_is_not_ejected(red_tick):
    t = red_tick([101])
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_at_head(t)
    _main(t, REQUIRED_TICK, [UNIT])
    t.run()
    assert not t.ejected()
    assert t.comments().startswith("Held: main is red on ")


def test_a_red_on_a_check_main_has_green_bisects(red_tick):
    t = red_tick([101, 102])
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_at_head(t, [UNIT])
    _main(t, [*REQUIRED_TICK, UNIT], [SHARD])
    t.run()
    assert "Bisecting" in t.comments()
    assert not t.comments().startswith("Held")


def test_the_app_token_re_runs_when_the_owner_picks_it(red_tick):
    t = red_tick([101, 102])
    _red_at_head(t)
    t.env = {"MERGE_TRAIN_RERUN_BY": "app"}
    t.run()
    assert t.after.get("rerun_by") == "app"
    assert len(t.after.get("reruns", [])) == 1
    assert TRAIN_PR not in t.after.get("closed_now", [])


def test_a_refused_re_run_falls_back_to_bisect(red_tick):
    t = red_tick([101, 102])
    t.world["rerun_refused"] = True
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])
    out = t.run()
    assert TRAIN_PR in t.after.get("closed_now", [])
    assert "Bisecting" in t.comments()
    assert "the retry could not run" in out


def _held_train(t: RedTick, ago_minutes: float, checks: list[str]) -> None:
    rendered = _run("state-render", *[f"{k}={v}" for k, v in t.state.items()],
                    f"main_red={int(time.time() - ago_minutes * 60)}",
                    f"main_red_checks={json.dumps(checks)}")
    t.world["closed"] = [
        {"number": TRAIN_PR - 7, "headRefName": "swarm/task_x", "body": ""},
        {"number": TRAIN_PR, "headRefName": "train/41-1", "body": rendered.stdout},
        {"number": TRAIN_PR - 1, "headRefName": "train/40-1", "body": ""},
    ]


def test_after_a_hold_nothing_boards_while_main_is_still_red(red_tick):
    t = red_tick([101])
    _held_train(t, 10, [UNIT])
    _main(t, REQUIRED_TICK, [UNIT])
    out = t.run(train_open=False)
    assert "rule 2 (hold while main is red)" in out
    assert STARTS not in out


def test_after_a_hold_nothing_boards_while_main_s_check_is_pending(red_tick):
    t = red_tick([101])
    _held_train(t, 10, [UNIT])
    t.world["checks"][t.main] = _at(REQUIRED_TICK)
    out = t.run(train_open=False)
    assert STARTS not in out


def test_after_a_hold_a_train_boards_once_main_is_green_on_those_checks(red_tick):
    t = red_tick([101])
    _held_train(t, 10, [UNIT])
    _main(t, [*REQUIRED_TICK, UNIT], [SHARD])
    out = t.run(train_open=False)
    assert STARTS in out


def test_after_a_hold_a_train_boards_once_the_wait_is_past_its_bound(red_tick):
    """A main that stays red would otherwise hold the fix itself out."""
    t = red_tick([101])
    _held_train(t, 61, [UNIT])
    _main(t, REQUIRED_TICK, [UNIT])
    out = t.run(train_open=False)
    assert STARTS in out


def test_state_carries_the_retry_and_the_hold_while_main_is_red(tmp_path):
    extra = {"retry": f"1760400000:{'b' * 40}", "main_red": "1760400000",
             "main_red_checks": json.dumps([UNIT, "swarm-ui component tests (3/8)"])}
    proc = _render({**STATE, **extra})
    assert proc.returncode == 0, proc.stderr
    body = tmp_path / "body.md"
    body.write_text(proc.stdout)
    for key, value in extra.items():
        got = _run("state-get", "--body", str(body), "--key", key)
        assert (got.returncode, got.stdout.rstrip("\n")) == (0, value), key


@pytest.mark.parametrize("key, value", [
    ("retry", "b" * 40),
    ("main_red", "soon"),
    ("main_red_checks", "[]"),
    ("main_red_checks", '["a -->"]'),
    ("main_red_checks", "unit tests"),
])
def test_state_refuses_a_retry_or_hold_the_train_never_writes(key: str, value: str):
    assert _render({**STATE, key: value}).returncode != 0


# --------------------------------------------------------------------------
# the open pull request list: light for all, heavy one at a time, retried
# --------------------------------------------------------------------------
# On 6 November 2026 one `gh pr list` of every open pull request with its body
# and rollup took 10.6 s and 1.4 MB at 64 open, at GitHub's 10-second GraphQL
# limit: four ticks in a row died on a 504, an i/o timeout or an empty reply,
# and the train stood still for 75 minutes (THE OPEN PULL REQUEST LIST).

FAKE_LIST_GH = """#!/usr/bin/env bash
set -euo pipefail
# gh pr list (key `list`) and gh pr view <N> (key <N>), answered from
# $FAKE_GH_DIR/<key>.json. $FAKE_GH_DIR/<key>.fails holds one line per read,
# in order, for reads that fail first: 504, empty, notjson or 404.
printf '%s\\n' "$*" >>"${FAKE_GH_LOG}"
case "$1 $2" in
  "pr list") key=list ;;
  "pr view") key="$3" ;;
  *) echo "fake gh: unexpected: $*" >&2; exit 64 ;;
esac
count="${FAKE_GH_DIR}/${key}.count"
reads=0
[[ -f "${count}" ]] && reads="$(cat "${count}")"
reads=$((reads + 1))
printf '%s' "${reads}" >"${count}"
how=""
[[ -f "${FAKE_GH_DIR}/${key}.fails" ]] && how="$(sed -n "${reads}p" "${FAKE_GH_DIR}/${key}.fails")"
case "${how}" in
  504) echo "HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)" >&2; exit 1 ;;
  empty) echo "unexpected end of JSON input" >&2; exit 1 ;;
  notjson) echo "<html>busy</html>"; exit 0 ;;
  404) echo "HTTP 404: Not Found (https://api.github.com/graphql)" >&2; exit 1 ;;
esac
cat "${FAKE_GH_DIR}/${key}.json"
"""

_LIGHT = ["number", "title", "headRefOid", "headRefName", "baseRefName", "isDraft",
          "isCrossRepository", "mergeable", "labels"]


def _open_pr(number: int, *, ready: bool, branch: str | None = None) -> dict:
    return {
        "number": number, "title": f"Change {number}", "headRefOid": secrets.token_hex(20),
        "headRefName": branch or f"swarm/task_{number}", "baseRefName": "main", "isDraft": False,
        "isCrossRepository": False, "mergeable": "MERGEABLE",
        "labels": [{"name": "ready"}] if ready else [],
        "body": f"body of {number}",
        "statusCheckRollup": [{"__typename": "CheckRun", "name": "ci-gate", "status": "COMPLETED",
                               "conclusion": "SUCCESS", "completedAt": "2026-11-06T12:00:00Z",
                               "detailsUrl": f"https://example.test/{number}"}],
    }


def _list_prs(tmp_path: Path, prs: list[dict], fails: dict[str, list[str]] | None = None):
    _need("bash", "jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gh"
    fake.write_text(FAKE_LIST_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    answers = tmp_path / "answers"
    answers.mkdir(exist_ok=True)
    (answers / "list.json").write_text(json.dumps([{k: p[k] for k in _LIGHT} for p in prs]))
    for p in prs:
        (answers / f"{p['number']}.json").write_text(json.dumps(p))
    for key, lines in (fails or {}).items():
        (answers / f"{key}.fails").write_text("".join(f"{line}\n" for line in lines))
    log = tmp_path / "gh.log"
    log.write_text("")
    out = tmp_path / "prs.json"
    proc = _run("list-prs", "--out", str(out), env={
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_REPO": "example-org/example-repo",
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_DIR": str(answers),
        "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0",
    })
    listed = json.loads(out.read_text()) if proc.returncode == 0 else None
    return proc, listed, log.read_text().splitlines()


def test_the_list_reads_no_body_or_rollup_and_each_ready_or_train_pull_request_alone(tmp_path):
    prs = [_open_pr(201, ready=True), _open_pr(202, ready=False),
           _open_pr(203, ready=False, branch="train/abc"), _open_pr(204, ready=True)]
    proc, listed, reads = _list_prs(tmp_path, prs)
    assert proc.returncode == 0, proc.stderr
    list_reads = [r for r in reads if r.startswith("pr list")]
    # MUTATION: read body and statusCheckRollup in the list -> one 1.4 MB
    # GraphQL reply that times out at 64 open pull requests.
    assert len(list_reads) == 1
    assert "statusCheckRollup" not in list_reads[0] and "body" not in list_reads[0]
    viewed = sorted(r.split()[2] for r in reads if r.startswith("pr view"))
    assert viewed == ["201", "203", "204"]
    by_number = {p["number"]: p for p in listed}
    assert sorted(by_number) == [201, 202, 203, 204]
    assert by_number[201]["statusCheckRollup"] == prs[0]["statusCheckRollup"]
    assert by_number[203]["body"] == "body of 203"
    assert by_number[202]["body"] == "" and by_number[202]["statusCheckRollup"] == []


def test_the_list_selects_the_same_pull_requests_as_the_one_big_read_did(tmp_path):
    prs = [_open_pr(201, ready=True), _open_pr(202, ready=False), _open_pr(204, ready=True)]
    proc, listed, _ = _list_prs(tmp_path, prs)
    assert proc.returncode == 0, proc.stderr
    light = tmp_path / "light.json"
    light.write_text(json.dumps(listed))
    big = tmp_path / "big.json"
    big.write_text(json.dumps(prs))
    required = _required_file(tmp_path, ["ci-gate"])
    one = _run("select", "--prs", str(light), "--required", str(required))
    other = _run("select", "--prs", str(big), "--required", str(required))
    assert one.returncode == 0 and other.returncode == 0, (one.stderr, other.stderr)
    assert one.stdout == other.stdout
    assert [line.split("\t")[0] for line in one.stdout.splitlines()] == ["201", "204"]


def test_a_list_read_that_answers_504_once_is_retried(tmp_path):
    proc, listed, reads = _list_prs(tmp_path, [_open_pr(201, ready=True)], {"list": ["504"]})
    # MUTATION: no retry on the list -> the tick dies, as four did at 12:45Z.
    assert proc.returncode == 0, proc.stderr
    assert len([r for r in reads if r.startswith("pr list")]) == 2
    assert [p["number"] for p in listed] == [201]
    assert "failed transiently" in proc.stderr


def test_an_empty_or_non_json_reply_is_retried(tmp_path):
    proc, listed, reads = _list_prs(tmp_path, [_open_pr(201, ready=True), _open_pr(204, ready=True)],
                                    {"201": ["empty"], "204": ["notjson"]})
    assert proc.returncode == 0, proc.stderr
    assert sorted(p["number"] for p in listed) == [201, 204]
    assert len([r for r in reads if r.startswith("pr view 201 ")]) == 2
    assert len([r for r in reads if r.startswith("pr view 204 ")]) == 2


def test_a_refused_read_is_not_retried_and_that_pull_request_sits_the_tick_out(tmp_path):
    proc, listed, reads = _list_prs(tmp_path, [_open_pr(201, ready=True), _open_pr(204, ready=True)],
                                    {"204": ["404", "404", "404"]})
    assert proc.returncode == 0, proc.stderr
    # MUTATION: retry a 404 -> three reads of 204.
    assert len([r for r in reads if r.startswith("pr view 204 ")]) == 1
    # MUTATION: keep 204 with an empty rollup -> `skips` would tell it a check
    # is missing that it never read.
    assert [p["number"] for p in listed] == [201]
    assert "#204 sits this tick out" in proc.stderr


def test_a_ready_pull_request_failing_every_attempt_sits_the_tick_out(tmp_path):
    proc, listed, reads = _list_prs(tmp_path, [_open_pr(201, ready=True), _open_pr(204, ready=True)],
                                    {"204": ["504", "504", "504"]})
    assert proc.returncode == 0, proc.stderr
    assert len([r for r in reads if r.startswith("pr view 204 ")]) == 3
    assert [p["number"] for p in listed] == [201]


def test_a_train_that_cannot_be_read_stops_the_tick(tmp_path):
    proc, _, _ = _list_prs(tmp_path, [_open_pr(203, ready=False, branch="train/abc")],
                           {"203": ["504", "504", "504"]})
    # MUTATION: let a train sit out like a `ready` pull request -> the tick
    # starts a second train while the first is still open.
    assert proc.returncode != 0
    assert "could not read train #203" in proc.stderr


# ---------------------------------------------------------------------------
# Every other read a tick makes is retried like the list (gh_read).
# ---------------------------------------------------------------------------
# 6 November 2026, 12:45Z to 13:00Z: four ticks in a row died on a GitHub read
# (a 504, an i/o timeout, twice an empty reply), and nothing re-ticked for 75
# minutes. Each read below answers wrongly once, then well: it is read once
# more and the tick goes on; a not-found answer is read once and not again.
# A fake `gh` in front of RED_FAKE_GH answers the faults FAKE_FAULTS lists.

FAULTY_GH = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
path = os.environ["FAKE_FAULTS"]
with open(path) as fh:
    faults = json.load(fh)
with open(path + ".log", "a") as fh:
    fh.write(json.dumps(args) + "\n")
SAID = {
    "504": "HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)",
    "timeout": 'Post "https://api.github.com/graphql": dial tcp 192.0.2.1:443: i/o timeout',
    "eof": "unexpected end of JSON input",
    "404": "HTTP 404: Not Found (https://api.github.com/repos/example)",
    "merged": "GraphQL: Pull request is already merged (mergePullRequest)",
}
inner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh-inner")
# The first fault whose words are all among the arguments: `skip` calls pass
# first, then each `fail` answer once, then `serve` (JSON) or the fake behind.
fault = next((f for f in faults if all(w in args for w in f["words"])), None)
if fault is not None:
    if fault.get("skip", 0) > 0:
        fault["skip"] -= 1
    elif fault.get("fail"):
        how = fault["fail"].pop(0)
        with open(path, "w") as fh:
            json.dump(faults, fh)
        if how == "lost":
            import subprocess
            subprocess.run([inner, *args], stdout=subprocess.DEVNULL, check=True)
            how = "504"
        print(SAID[how], file=sys.stderr)
        sys.exit(1)
    elif "serve" in fault:
        print(json.dumps(fault["serve"]))
        sys.exit(0)
    with open(path, "w") as fh:
        json.dump(faults, fh)
os.execv(inner, [inner, *args])
'''


def _faulty(t: RedTick, faults: list[dict]) -> Path:
    """Puts FAULTY_GH in front of t's fake; returns the faults file (its
    `.log` holds every call, the faulted ones too)."""
    inner = t.bin / "gh-inner"
    if not inner.exists():
        (t.bin / "gh").rename(inner)
    wrapper = t.bin / "gh"
    wrapper.write_text(FAULTY_GH.replace("{python}", sys.executable))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    path = t.tmp / "faults.json"
    path.write_text(json.dumps(faults))
    t.env = {**t.env, "FAKE_FAULTS": str(path), "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0"}
    return path


def _calls(faults: Path, words: list[str]) -> int:
    log = Path(str(faults) + ".log")
    lines = log.read_text().splitlines() if log.exists() else []
    return sum(1 for line in lines if all(w in json.loads(line) for w in words))


def _api(path: str) -> str:
    return f"repos/{THIS_REPO}/{path}"


def _green_train(t: RedTick) -> None:
    t.world["checks"][t.head] = _at(REQUIRED_TICK)


def _green_train_ran_twice(t: RedTick) -> None:
    """A check that ran twice at the head: the tick reads the Actions runs."""
    _green_train(t)
    again = dict(t.world["checks"][t.head]["check_runs"][0], id=1, check_suite={"id": 1})
    t.world["checks"][t.head]["check_runs"].append(again)


def _first_red(t: RedTick) -> None:
    _red_at_head(t)
    _main(t, [*REQUIRED_TICK, UNIT])


def _red_past_the_age_limit(t: RedTick) -> None:
    t.state["started"] = str(int(time.time()) - 6 * 3600)
    _first_red(t)


def _red_while_main_is_red(t: RedTick) -> None:
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_at_head(t, [UNIT, SHARD])
    _main(t, REQUIRED_TICK, [UNIT])


def _after_a_hold(t: RedTick) -> None:
    _held_train(t, 10, [UNIT])
    _main(t, [*REQUIRED_TICK, UNIT], [SHARD])


#: The deploy hold's four reads, answered for a build in progress on main and
#: no deploy since long ago, so a green train holds.
BUILD_RUN, RELEASE_RUN = 7001, 7002


def _hold_serves(t: RedTick) -> list[dict]:
    return [
        {"words": [_api("actions/workflows/application.yml/runs?branch=main&event=push&per_page=1")],
         "serve": {"workflow_runs": [{"id": BUILD_RUN, "head_sha": t.main}]}},
        {"words": [_api(f"actions/runs/{BUILD_RUN}/jobs?per_page=100")],
         "serve": {"jobs": [{"name": "build images", "status": "in_progress"}]}},
        {"words": [_api("actions/workflows/release.yml/runs?branch=main&per_page=10")],
         "serve": {"workflow_runs": [{"id": RELEASE_RUN, "status": "completed",
                                      "created_at": "2026-11-01T00:00:00Z"}]}},
        {"words": [_api(f"actions/runs/{RELEASE_RUN}/jobs?per_page=100")],
         "serve": {"jobs": [{"name": "deploy (prod)", "conclusion": "success",
                             "completed_at": "2026-11-01T01:00:00Z"}]}},
    ]


def _faults_for(t: RedTick, words: list[str], fail: list[str], skip: int = 0) -> list[dict]:
    """The fault on `words`; a deploy hold read also gets the hold's other
    reads answered, so the read faulted is made at all."""
    serves = _hold_serves(t) if any("jobs" in w or ".yml" in w for w in words) else []
    mine = [f for f in serves if f["words"] == words]
    rest = [f for f in serves if f["words"] != words]
    fault = {"words": words, "fail": fail, "skip": skip}
    if mine:
        fault["serve"] = mine[0]["serve"]
    return [fault, *rest]


def _head_runs(t: RedTick) -> list[str]:
    return [_api(f"actions/runs?head_sha={t.head}&per_page=100")]


#: (read, scenario, its words given the tick, calls it makes with no fault,
#: calls skipped before the fault).
TICK_READS = [
    ("main's rules", _green_train, lambda t: [_api("rules/branches/main")], 1, 0),
    ("main's branch protection", _green_train, lambda t: [_api("branches/main")], 1, 0),
    ("the check runs at the train head", _green_train,
     lambda t: [_api(f"commits/{t.head}/check-runs?per_page=100")], 1, 0),
    ("the statuses at the train head", _green_train,
     lambda t: [_api(f"commits/{t.head}/status?per_page=100")], 1, 0),
    ("the Actions runs at the train head", _green_train_ran_twice, _head_runs, 1, 0),
    ("main's application.yml runs", _green_train,
     lambda t: [_api("actions/workflows/application.yml/runs?branch=main&event=push&per_page=1")], 1, 0),
    ("the jobs of main's build", _green_train,
     lambda t: [_api(f"actions/runs/{BUILD_RUN}/jobs?per_page=100")], 1, 0),
    ("main's release.yml runs", _green_train,
     lambda t: [_api("actions/workflows/release.yml/runs?branch=main&per_page=10")], 1, 0),
    ("the jobs of main's release", _green_train,
     lambda t: [_api(f"actions/runs/{RELEASE_RUN}/jobs?per_page=100")], 1, 0),
    ("rule 1's runs still running", _first_red, _head_runs, 2, 0),
    ("rule 1's runs past the age limit", _red_past_the_age_limit, _head_runs, 2, 0),
    ("rule 1's runs to re-run", _first_red, _head_runs, 2, 1),
    ("main's head commit", _red_while_main_is_red, lambda t: [_api("commits/main")], 1, 0),
    ("the check runs on main", _red_while_main_is_red,
     lambda t: [_api(f"commits/{t.main}/check-runs?per_page=100")], 1, 0),
    ("the statuses on main", _red_while_main_is_red,
     lambda t: [_api(f"commits/{t.main}/status?per_page=100")], 1, 0),
    ("the Actions runs on main", _red_while_main_is_red,
     lambda t: [_api(f"actions/runs?head_sha={t.main}&per_page=100")], 1, 0),
    ("the closed trains", _after_a_hold, lambda t: ["pr", "list", "closed"], 1, 0),
]


def _tick_reads_ids(rows):
    return [row[0] for row in rows]


def _set_up_read(red_tick, scenario, words_of, fail: list[str], skip: int):
    t = red_tick([101, 102])
    scenario(t)
    words = words_of(t)
    return t, words, _faulty(t, _faults_for(t, words, fail, skip))


@pytest.mark.parametrize(("read", "scenario", "words_of", "normal", "skip"), TICK_READS,
                         ids=_tick_reads_ids(TICK_READS))
def test_a_tick_read_that_answers_504_once_is_read_once_more_and_the_tick_goes_on(
        red_tick, read, scenario, words_of, normal, skip):
    t, words, faults = _set_up_read(red_tick, scenario, words_of, ["504"], skip)
    t.run(train_open=scenario is not _after_a_hold)
    # MUTATION: read it with a bare `gh api` / `gh pr list` -> read once, and
    # the tick dies or decides on nothing read, as four did at 12:45Z.
    assert _calls(faults, words) == normal + 1, read


@pytest.mark.parametrize(("read", "scenario", "words_of", "normal", "skip"), TICK_READS,
                         ids=_tick_reads_ids(TICK_READS))
def test_a_tick_read_that_answers_not_found_is_not_read_again(
        red_tick, read, scenario, words_of, normal, skip):
    t, words, faults = _set_up_read(red_tick, scenario, words_of, ["404", "404", "404"], skip)
    try:
        t.run(train_open=scenario is not _after_a_hold)
    except AssertionError:
        pass  # Main's rules, protection and a train head's checks must be read.
    # MUTATION: retry every failure -> a refusal is asked three times.
    assert _calls(faults, words) == normal, read


def test_the_tick_s_outcome_survives_one_timeout_on_each_read_it_decides_on(red_tick):
    """Each read through gh_read decides as if it never failed."""
    t = red_tick([101, 102])
    _green_train(t)
    faults = _faulty(t, [{"words": [_api(f"commits/{t.head}/check-runs?per_page=100")],
                          "fail": ["timeout"]},
                         {"words": [_api(f"commits/{t.head}/status?per_page=100")],
                          "fail": ["eof"]},
                         {"words": [_api("rules/branches/main")], "fail": ["504"]}])
    t.run()
    assert t.after.get("merged") == [101, 102]
    assert _calls(faults, [_api("rules/branches/main")]) == 2


def test_a_green_train_holds_for_main_s_build_after_a_504_on_each_hold_read(red_tick):
    t = red_tick([101, 102])
    _green_train(t)
    faults = _hold_serves(t)
    for fault in faults:
        fault["fail"] = ["504"]
    path = _faulty(t, faults)
    out = t.run()
    # MUTATION: read the hold's runs with a bare `gh api` -> "could not read
    # ...: not holding", and the merge cancels main's build.
    assert "holding the merge so main's build" in out, out
    assert not t.after.get("merged")
    assert all(_calls(path, f["words"]) == 2 for f in faults)


def test_a_red_train_is_held_while_main_is_red_after_a_504_on_main_s_commit(red_tick):
    t = red_tick([101, 102])
    _red_while_main_is_red(t)
    _faulty(t, [{"words": [_api("commits/main")], "fail": ["504"]}])
    t.run()
    # MUTATION: a bare read of main's commit -> "could not read main's
    # checks", and a train red only because main is bisects and ejects.
    assert t.comments().startswith(f"Held: main is red on {UNIT}\n")
    assert not t.ejected()


def test_a_merge_github_refuses_after_a_lost_reply_is_read_again_through_a_504(red_tick):
    t = red_tick([101])
    _green_train(t)
    view = ["pr", "view", "101", "state"]
    faults = _faulty(t, [{"words": ["pr", "merge", "101"], "fail": ["504", "merged"]},
                         {"words": view, "fail": ["504"], "serve": {"state": "MERGED"}}])
    out = t.run()
    # MUTATION: a bare read after the refusal -> "GitHub refused to merge
    # #101", and a merge that landed is reported as a train stopped part-way.
    assert "had merged although GitHub's reply to an earlier attempt was lost" in out
    assert _calls(faults, view) == 2


def _tell_with_faults(tmp_path: Path, fail: list[str]):
    t = RedTick(tmp_path, [101])
    comments = {n: _api(f"issues/{n}/comments") for n in (105, 107, 113)}
    faults = _faulty(t, [{"words": [w], "fail": fail if n == 105 else [], "serve": []}
                         for n, w in comments.items()])
    world = {**t.world, "open": []}
    (t.tmp / "world.json").write_text(json.dumps(world))
    (t.tmp / "gh.log").write_text("")
    env = {"PATH": f"{t.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(t.tmp),
           "GH_REPO": THIS_REPO, "GH_TOKEN": "fake-" + secrets.token_hex(4),
           "APP_TOKEN": "fake-" + secrets.token_hex(4), "RUN_URL": "https://example.invalid/run/42",
           "FAKE_WORLD": str(t.tmp / "world.json"), "FAKE_GH_LOG": str(t.tmp / "gh.log"), **t.env}
    proc = subprocess.run([str(SCRIPT), "tell", "--prs", str(FIXTURES / "prs.json")], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    told = json.loads((t.tmp / "world.json").read_text()).get("comments", {})
    return told, _calls(faults, [comments[105]]), proc


def test_a_thread_s_comments_that_answer_504_once_are_read_again_and_it_is_told(tmp_path):
    told, reads, proc = _tell_with_faults(tmp_path, ["504"])
    # MUTATION: a bare comments read -> "could not read #105's comments", and
    # 105 is never told why it does not board.
    assert "105" in told, proc.stderr
    assert reads == 2
    assert "reading #105's comments failed transiently" in proc.stderr


def test_a_thread_s_comments_that_are_not_found_are_not_read_again(tmp_path):
    told, reads, proc = _tell_with_faults(tmp_path, ["404", "404", "404"])
    assert "105" not in told
    assert reads == 1
    assert "could not read #105's comments" in proc.stderr


def test_a_mergeability_read_that_answers_504_once_is_read_again(tmp_path):
    numbers, reads, proc = _recheck(tmp_path, {106: ["504", "MERGEABLE"]})
    # MUTATION: a bare read -> a 504 counts as UNKNOWN, read again after a
    # wait; with one read left it would be left out of the train.
    assert numbers == ["101", "106", "114", "120"], proc.stderr
    assert len(reads) == 2
    assert "reading the mergeability of #106 failed transiently" in proc.stderr


def test_a_mergeability_read_that_is_not_found_is_not_retried_by_gh_read(tmp_path):
    numbers, reads, proc = _recheck(tmp_path, {106: ["404"]})
    assert numbers == ["101", "114", "120"]
    # One read per MERGEABLE_READS round (three), never gh_read's retries too.
    assert len(reads) == 3
    assert "failed transiently" not in proc.stderr


def test_the_gather_defaults_are_8_or_15_minutes_up_to_40():
    """The owner's direction, 12 November 2026: a lane's pull request runs no
    checks, so the train's run is the only test a change gets, and trains are
    large -- "15 pull requests or 30 minutes, up to 40", then for speed on
    13 November, "8 ready or 15 minutes, up to 40". The minimum and the
    window live in the gather file swarm-api reads too; the maximum is the
    script's own; the workflow says all three where the variables override
    them. MUTATION: put any default back (4, 6, 15, 20 or 30)."""
    code = SCRIPT.read_text()
    gather = json.loads((REPO / "apps" / "swarm-api" / "swarm_api" / "merge_train_gather.json").read_text())
    assert (gather["min_prs"], gather["max_wait_minutes"]) == (8, 15)
    assert 'MAX_PRS="${MERGE_TRAIN_MAX_PRS:-40}"' in code.splitlines()
    for knob, key in (("MERGE_TRAIN_MIN_PRS", "min_prs"), ("MERGE_TRAIN_MAX_WAIT_MINUTES", "max_wait_minutes")):
        assert f"${{{knob}:-$(gather_default {key})}}" in code, knob
    workflow = WORKFLOW.read_text()
    assert "the script's defaults: 40 pull requests" in workflow
    assert "the script's 8 and 15" in workflow
    env = yaml.safe_load(workflow)["jobs"]["tick"]["steps"]
    tick_env = next(step["env"] for step in env if step.get("name") == "one tick of the train")
    for knob in ("MERGE_TRAIN_MAX_PRS", "MERGE_TRAIN_MIN_PRS", "MERGE_TRAIN_MAX_WAIT_MINUTES"):
        assert tick_env[knob] == "${{ vars.%s }}" % knob, knob
