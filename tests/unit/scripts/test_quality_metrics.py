"""The context-quality baseline: docs/design/context-quality.md §2.6 (CTXQ-M).

scripts/lib/quality_metrics.py is pure: scripts/quality-baseline.sh fetches
the PRs, the Actions runs, the PR heads' commits, the issues and (with an
operator's credential) swarm-api's runs, tasks and impact answers into one
directory, and the module computes M1-M12 from what was recorded there. So
every rule that moves a figure is held here over fixtures in the shapes the
GitHub API, `git log` and swarm-api serve, each with a control that moves the
figure the other way when only that rule's fact changes.

The three rules §7 names for this lane:
  * a cancelled (or skipped, or unfinished) run is not a red run;
  * a merge commit from main is not a hand fix-up, even with the operator's name;
  * a `## Continuation` section is counted once, however often it is repeated.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "scripts" / "lib" / "quality_metrics.py"
SCRIPT = REPO / "scripts" / "quality-baseline.sh"

_spec = importlib.util.spec_from_file_location("quality_metrics", HELPER)
qm = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["quality_metrics"] = qm
_spec.loader.exec_module(qm)

WORKER = "bogdan"
OPERATOR = "bogdan-alexandrescu"
MAIN_AUTHOR = "Bogdan Alexandrescu"


# --------------------------------------------------------------------------
# recorded shapes
# --------------------------------------------------------------------------

def _pr(number, *, head=None, merged_at="2026-10-12T10:00:00Z", created_at="2026-10-12T08:00:00Z",
        body="", state="closed", closed_at=None):
    return {
        "number": number,
        "state": state,
        "title": f"lane {number}",
        "body": body,
        "created_at": created_at,
        "merged_at": merged_at,
        "closed_at": closed_at or merged_at,
        "head": {"ref": head or f"swarm/task_{number:04d}"},
    }


def _run(run_id, branch, conclusion, *, workflow="application", created_at="2026-10-12T09:00:00Z",
         event="pull_request", status="completed"):
    return {
        "id": run_id,
        "name": workflow,
        "path": f".github/workflows/{workflow}.yml",
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "head_branch": branch,
        "created_at": created_at,
    }


def _log(*commits):
    """`git log --format=%x1e%H%x1f%P%x1f%an%x1f%cI --name-only` output."""
    out = []
    for sha, parents, author, date, files in commits:
        out.append(f"\x1e{sha}\x1f{' '.join(parents)}\x1f{author}\x1f{date}\n\n" + "".join(f"{f}\n" for f in files))
    return "".join(out)


VERDICT_NOT_YET = """Body of the lane.

Review verdict: **NOT_YET** (from `verdict.json`, task `task_r1`).
The verdict did not name this step, so the fix agent did not run: this branch is the reviewed work as it was.

Findings, as the review wrote them:

```text
- the list card still says 1,000 edges
- deleting the wiring leaves every test green
```
1 more finding(s) are in the verdict file and not shown here.
"""

VERDICT_MERGE = """Body.

Review verdict: **MERGE** (from `verdict.json`, task `task_r2`).
The verdict did not name this step, so the fix agent did not run: this branch is the reviewed work as it was.
"""


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

def test_the_window_is_the_newest_merged_swarm_lanes_and_nothing_else():
    prs = [
        _pr(10), _pr(11), _pr(12, merged_at=None),           # 12: closed unmerged
        _pr(13, head="feature/hand-made"),                     # 13: not a lane
        _pr(14), _pr(15),
    ]
    lanes = qm.select_lanes(prs, limit=3)
    assert [p["number"] for p in lanes] == [11, 14, 15]


def test_the_same_pr_on_two_pages_is_one_lane():
    lanes = qm.select_lanes([_pr(10), _pr(10), _pr(11)], limit=150)
    assert [p["number"] for p in lanes] == [10, 11]


# --------------------------------------------------------------------------
# M1-M3: CI
# --------------------------------------------------------------------------

def test_cancelled_skipped_and_unfinished_runs_are_not_red():
    lanes = [_pr(1)]
    branch = lanes[0]["head"]["ref"]
    runs = [
        _run(1, branch, "cancelled"),
        _run(2, branch, "skipped"),
        _run(3, branch, None, status="in_progress"),
        _run(4, branch, "success"),
    ]
    ci = qm.ci_metrics(lanes, runs)
    assert ci["m1_red_runs"] == 0
    assert ci["m2_red_prs"] == 0
    assert ci["runs_counted"] == 1
    # Control: the same branch with one failure is red.
    ci = qm.ci_metrics(lanes, runs + [_run(5, branch, "failure")])
    assert ci["m1_red_runs"] == 1 and ci["m2_red_prs"] == 1


def test_only_pull_request_runs_of_the_three_workflows_count():
    lanes = [_pr(1)]
    branch = lanes[0]["head"]["ref"]
    runs = [
        _run(1, branch, "failure", workflow="accept"),
        _run(2, branch, "failure", event="push"),
        _run(3, "swarm/task_other", "failure"),
    ]
    assert qm.ci_metrics(lanes, runs)["m1_red_runs"] == 0
    runs.append(_run(4, branch, "failure", workflow="terraform"))
    runs.append(_run(5, branch, "failure", workflow="security"))
    assert qm.ci_metrics(lanes, runs)["m1_red_runs"] == 2


def test_a_run_read_twice_across_created_at_ranges_counts_once():
    lanes = [_pr(1)]
    branch = lanes[0]["head"]["ref"]
    red = _run(7, branch, "failure")
    ci = qm.ci_metrics(lanes, [red, dict(red)])
    assert ci["m1_red_runs"] == 1


def test_first_pass_reads_the_earliest_completed_application_run():
    a, b = _pr(1), _pr(2)
    runs = [
        # a: red first, then green -> not first-pass, even though it ended green
        _run(1, a["head"]["ref"], "failure", created_at="2026-10-12T09:00:00Z"),
        _run(2, a["head"]["ref"], "success", created_at="2026-10-12T09:30:00Z"),
        # b: a cancelled run first is not "the earliest completed"; then green
        _run(3, b["head"]["ref"], "cancelled", created_at="2026-10-12T08:00:00Z"),
        _run(4, b["head"]["ref"], "success", created_at="2026-10-12T08:10:00Z"),
        # b's terraform failure does not decide application first-pass
        _run(5, b["head"]["ref"], "failure", workflow="terraform", created_at="2026-10-12T08:05:00Z"),
    ]
    ci = qm.ci_metrics([a, b], runs)
    per = {p["number"]: p for p in ci["per_pr"]}
    assert per[1]["first_pass"] is False
    assert per[2]["first_pass"] is True
    assert ci["m3_first_pass"] == 1
    assert ci["m3_measured"] == 2


def test_a_pr_with_no_application_run_is_unknown_not_green():
    ci = qm.ci_metrics([_pr(1)], [])
    assert ci["per_pr"][0]["first_pass"] is None
    assert ci["m3_first_pass"] == 0 and ci["m3_measured"] == 0


# --------------------------------------------------------------------------
# M4-M5: the review verdict
# --------------------------------------------------------------------------

def test_verdicts_and_their_findings_are_read_from_the_body():
    verdicts = qm.parse_verdicts(VERDICT_NOT_YET)
    assert verdicts == [{"verdict": "NOT_YET", "findings": 3}]
    assert qm.parse_verdicts(VERDICT_MERGE) == [{"verdict": "MERGE", "findings": 0}]
    assert qm.parse_verdicts("no review here") == []


def test_a_finding_line_outside_the_fence_is_not_a_finding():
    body = VERDICT_MERGE + "\n- a bullet in the agent's own body\n"
    assert qm.parse_verdicts(body) == [{"verdict": "MERGE", "findings": 0}]


def test_not_yet_rate_findings_and_red_after_merge():
    lanes = [_pr(1, body=VERDICT_NOT_YET), _pr(2, body=VERDICT_MERGE), _pr(3, body=VERDICT_MERGE)]
    runs = [_run(1, lanes[1]["head"]["ref"], "failure")]   # 2 went red after a MERGE
    report = qm.build_report({"lanes": lanes, "runs": runs})
    review = report["review"]
    assert review["m4_not_yet_prs"] == 1
    assert review["m4_findings"] == 3
    assert review["m5_red_after_merge"] == 1
    assert review["m5_red_prs"] == 1
    # Control: a red NOT_YET PR is not "red after MERGE".
    runs = [_run(1, lanes[0]["head"]["ref"], "failure")]
    review = qm.build_report({"lanes": lanes, "runs": runs})["review"]
    assert review["m5_red_after_merge"] == 0 and review["m5_red_prs"] == 1


# --------------------------------------------------------------------------
# M6: hand fix-up
# --------------------------------------------------------------------------

def test_git_log_is_parsed_with_parents_author_date_and_files():
    text = _log(
        ("a" * 40, ["b" * 40], WORKER, "2026-10-12T09:00:00+00:00", ["apps/x.py", "tests/unit/test_x.py"]),
        ("c" * 40, ["d" * 40, "e" * 40], MAIN_AUTHOR, "2026-10-12T09:10:00+00:00", []),
    )
    commits = qm.parse_git_log(text)
    assert [c["sha"] for c in commits] == ["a" * 40, "c" * 40]
    assert commits[0]["files"] == ["apps/x.py", "tests/unit/test_x.py"]
    assert commits[0]["author"] == WORKER
    assert len(commits[1]["parents"]) == 2


def test_a_merge_from_main_is_not_a_hand_fix_up_even_by_the_operator():
    merge = ("c" * 40, ["d" * 40, "e" * 40], OPERATOR, "2026-10-12T09:10:00+00:00", [])
    worker = ("a" * 40, ["b" * 40], WORKER, "2026-10-12T09:00:00+00:00", ["apps/x.py"])
    from_main = ("f" * 40, ["9" * 40], MAIN_AUTHOR, "2026-10-12T09:20:00+00:00", ["apps/y.py"])
    commits = qm.parse_git_log(_log(worker, merge, from_main))
    assert qm.hand_fixups(commits, operators=(OPERATOR,)) == []
    # Control: a non-merge commit by the operator's identity is one.
    hand = ("1" * 40, ["a" * 40], OPERATOR, "2026-10-12T09:30:00+00:00", ["apps/x.py"])
    commits = qm.parse_git_log(_log(worker, merge, from_main, hand))
    assert [c["sha"] for c in qm.hand_fixups(commits, operators=(OPERATOR,))] == ["1" * 40]


def test_m6_counts_prs_and_commits():
    lanes = [_pr(1), _pr(2)]
    hand = lambda sha: (sha, ["a" * 40], OPERATOR, "2026-10-12T09:30:00+00:00", ["x"])  # noqa: E731
    commits = {1: qm.parse_git_log(_log(hand("1" * 40), hand("2" * 40))), 2: []}
    report = qm.build_report({"lanes": lanes, "commits": commits}, operators=(OPERATOR,))
    assert report["hand_fixup"]["m6_prs"] == 1
    assert report["hand_fixup"]["m6_commits"] == 2
    assert report["hand_fixup"]["prs_read"] == 2


def test_a_lane_whose_head_was_not_read_is_not_counted_clean():
    report = qm.build_report({"lanes": [_pr(1), _pr(2)], "commits": {1: []}}, operators=(OPERATOR,))
    assert report["hand_fixup"]["prs_read"] == 1
    assert report["hand_fixup"]["prs_unread"] == [2]


# --------------------------------------------------------------------------
# M7: escaped defects
# --------------------------------------------------------------------------

def test_a_bug_citing_a_lane_within_14_days_is_an_escaped_defect():
    lanes = [_pr(1148, merged_at="2026-10-12T10:00:00Z"), _pr(1149, merged_at="2026-10-12T10:00:00Z")]
    issues = [
        {"number": 1151, "title": "CLAUDE.md says 5 groups after #1148", "body": "",
         "created_at": "2026-10-13T00:00:00Z", "labels": [{"name": "bug"}]},
        # a bug filed BEFORE the lane merged cannot have escaped from it
        {"number": 1100, "title": "old", "body": "see #1149", "created_at": "2026-10-11T00:00:00Z",
         "labels": [{"name": "bug"}]},
        # past 14 days
        {"number": 1400, "title": "late", "body": "see #1149", "created_at": "2026-10-30T00:00:00Z",
         "labels": [{"name": "bug"}]},
        # not labelled bug
        {"number": 1152, "title": "#1149", "body": "", "created_at": "2026-10-13T00:00:00Z",
         "labels": [{"name": "enhancement"}]},
        # a pull request in the issues list is not an issue
        {"number": 1153, "title": "#1149", "body": "", "created_at": "2026-10-13T00:00:00Z",
         "labels": [{"name": "bug"}], "pull_request": {"url": "x"}},
    ]
    epic_comments = [
        {"issue": 1194, "body": "- [ ] **stale comment** · `x.py` · found by #1149",
         "created_at": "2026-10-12T12:00:00Z"},
    ]
    escaped = qm.escaped_defects(lanes, issues, epic_comments)
    assert escaped["m7_escaped"] == 2
    assert sorted((e["source"], e["pr"]) for e in escaped["items"]) == [
        ("epic-comment #1194", 1149), ("issue #1151", 1148)]
    assert escaped["prs_with_escape"] == 2


# --------------------------------------------------------------------------
# M8: issue-run outcomes
# --------------------------------------------------------------------------

MARK = "<!-- swarmcloud-issue-run:run_1:pr -->\nBody."


def test_issue_run_outcomes_read_the_marker_and_the_closing_comment():
    prs = [
        _pr(970, body=MARK, merged_at="2026-10-09T10:00:00Z", created_at="2026-10-09T09:00:00Z"),
        _pr(1136, body=MARK, merged_at=None, closed_at="2026-10-11T23:00:00Z",
            created_at="2026-10-11T22:12:00Z"),
        _pr(1090, body=MARK, merged_at=None, closed_at="2026-10-11T23:00:00Z",
            created_at="2026-10-11T22:12:00Z"),
        _pr(1155, body=MARK, merged_at=None, state="open", created_at="2026-10-12T09:00:00Z"),
        _pr(1000, body="no marker", merged_at=None, created_at="2026-10-10T09:00:00Z"),
    ]
    comments = {
        1136: [{"body": "Duplicate of the lane already in flight.", "created_at": "2026-10-11T22:59:00Z"}],
        # the word in an EARLIER comment, then a closing comment saying something else
        1090: [{"body": "might be a duplicate?", "created_at": "2026-10-11T22:30:00Z"},
               {"body": "Closing: the plan was wrong.", "created_at": "2026-10-11T22:59:30Z"}],
    }
    out = qm.issue_run_outcomes(prs, comments, since="2026-10-01T00:00:00Z")
    assert out["m8_issue_run_prs"] == 4
    assert out["merged"] == 1
    assert out["closed_duplicate"] == 1
    assert out["closed_other"] == 1
    assert out["open"] == 1
    assert out["closed_comment_unread"] == []


def test_a_closed_issue_run_pr_whose_comments_were_not_read_says_so():
    prs = [_pr(1136, body=MARK, merged_at=None, closed_at="2026-10-11T23:00:00Z")]
    out = qm.issue_run_outcomes(prs, {}, since="2026-10-01T00:00:00Z")
    assert out["closed_comment_unread"] == [1136]
    assert out["closed_duplicate"] == 0


# --------------------------------------------------------------------------
# M9: plan approvals, edits, rejections, NOT_READY (swarm-api)
# --------------------------------------------------------------------------

def _hist(*moves):
    return [{"at": "2026-10-12T09:00:00Z", "from": a, "to": b, "by": "op"} for a, b in moves]


def test_plan_metrics_read_the_run_history():
    runs = [
        {"id": "r1", "state": "DONE", "created_at": "2026-10-12T09:00:00Z",
         "history": _hist((None, "PLANNING"), ("PLANNING", "PLANNED"), ("PLANNED", "PLANNED"),
                          ("PLANNED", "PLANNED"), ("PLANNED", "APPROVED"), ("APPROVED", "RUNNING"),
                          ("RUNNING", "DONE"))},
        {"id": "r2", "state": "REJECTED", "created_at": "2026-10-12T09:00:00Z",
         "history": _hist((None, "PLANNING"), ("PLANNING", "PLANNED"), ("PLANNED", "REJECTED"))},
        {"id": "r3", "state": "NOT_READY", "created_at": "2026-10-12T09:00:00Z",
         "history": _hist((None, "PLANNING"), ("PLANNING", "NOT_READY"))},
        # outside the window
        {"id": "r0", "state": "REJECTED", "created_at": "2026-09-01T09:00:00Z", "history": []},
    ]
    out = qm.plan_metrics(runs, since="2026-10-01T00:00:00Z")
    assert out == {"m9_runs": 3, "approved": 1, "edits": 2, "runs_edited": 1, "rejected": 1,
                   "not_ready": 1, "measured": True}


def test_plan_metrics_without_a_credential_are_not_measured_not_zero():
    out = qm.plan_metrics(None, since="2026-10-01T00:00:00Z")
    assert out["measured"] is False
    assert "m9_runs" not in out


# --------------------------------------------------------------------------
# M10: fix rounds
# --------------------------------------------------------------------------

def test_a_continuation_heading_is_counted_once():
    section = "## Continuation (attempt 2): `task_fix1`\n\nfixed it\n"
    body = "Body.\n\n" + section + "\n" + section + "\n## Continuation (attempt 3): `task_fix2`\n"
    assert qm.continuations(body) == 2
    # A Republished section is the same task's later attempt, not a fix round.
    assert qm.continuations("## Republished (attempt 2): `task_x`\n") == 0
    # A heading quoted inside a line is not a section.
    assert qm.continuations("see `## Continuation (attempt 2): x` in docs") == 0


def test_a_fix_that_repeats_an_earlier_prs_fix_outside_its_own_change_is_found():
    fixture = "tests/unit/control_plane/repo_index_fakes.py"
    a, b, c = _pr(1162), _pr(1167), _pr(1169)
    runs = [
        _run(1, a["head"]["ref"], "failure", created_at="2026-10-12T01:00:00Z"),
        _run(2, b["head"]["ref"], "failure", created_at="2026-10-12T02:00:00Z"),
        _run(3, c["head"]["ref"], "failure", created_at="2026-10-12T03:00:00Z"),
    ]
    own = lambda sha, f, t: (sha, ["0" * 40], WORKER, t, [f])  # noqa: E731
    commits = {
        # a: its own change, then the fixture fix after its red run
        1162: qm.parse_git_log(_log(own("a1" * 20, "apps/a.py", "2026-10-12T00:30:00+00:00"),
                                    own("a2" * 20, fixture, "2026-10-12T01:30:00+00:00"))),
        # b: the same fixture fixed again, after a's fix: a repeat
        1167: qm.parse_git_log(_log(own("b1" * 20, "apps/b.py", "2026-10-12T01:40:00+00:00"),
                                    own("b2" * 20, fixture, "2026-10-12T02:30:00+00:00"))),
        # c: fixes the fixture too, but c's own change already edited it: its own file, no repeat
        1169: qm.parse_git_log(_log(own("c1" * 20, fixture, "2026-10-12T02:40:00+00:00"),
                                    own("c2" * 20, fixture, "2026-10-12T03:30:00+00:00"))),
    }
    report = qm.build_report({"lanes": [a, b, c], "runs": runs, "commits": commits})
    rounds = report["fix_rounds"]
    assert rounds["prs_with_fix_commits"] == 3
    assert rounds["m10_repeats"] == [{"pr": 1167, "file": fixture, "first_fixed_in": 1162}]


def test_m10_counts_continuations_per_pr():
    body = "## Continuation (attempt 2): `t1`\n\n## Continuation (attempt 3): `t2`\n"
    report = qm.build_report({"lanes": [_pr(1, body=body), _pr(2)]})
    assert report["fix_rounds"]["m10_continuations"] == 2
    assert report["fix_rounds"]["prs_continued"] == 1


# --------------------------------------------------------------------------
# M11: graph test reach (swarm-api impact answers)
# --------------------------------------------------------------------------

def _impact(changed, unmapped_symbols, *, cut=False, stale=False):
    return {
        "index_sha": "f" * 40, "stale": stale,
        "changed": [{"id": s, "kind": k} for s, k in changed],
        "unmapped": [{"symbol": s, "path": s.split("#")[0], "reason": "no test reaches"}
                     for s in unmapped_symbols] + [{"path": "docs/x.md", "reason": "file level"}],
        "lists_cut": ["unmapped"] if cut else [],
    }


def test_test_reach_is_the_share_of_changed_non_test_symbols_a_test_reaches():
    impacts = {
        1: _impact([("a.py#f", "function"), ("a.py#g", "function"), ("t.py#test_f", "test")],
                   ["a.py#g"]),
        2: _impact([], []),
        3: _impact([("b.py#h", "function")], [], stale=True),
    }
    out = qm.test_reach(impacts)
    assert out["m11_symbols"] == 3          # the test symbol is not a changed symbol to reach
    assert out["m11_reached"] == 2
    assert out["prs_measured"] == 2
    assert out["prs_without_symbols"] == [2]
    assert out["stale_index"] == [3]
    assert out["measured"] is True


def test_a_cut_unmapped_list_is_a_floor_not_a_reading():
    out = qm.test_reach({1: _impact([("a.py#f", "function")], ["a.py#f"], cut=True)})
    assert out["prs_cut"] == [1]
    assert out["prs_measured"] == 0
    assert out["m11_symbols"] == 0


def test_test_reach_without_impact_answers_is_not_measured():
    assert qm.test_reach(None) == {"measured": False}


# --------------------------------------------------------------------------
# M12: cost and wall time (swarm-api tasks)
# --------------------------------------------------------------------------

def test_cost_null_is_not_zero_and_an_incomplete_total_is_a_floor():
    lanes = [_pr(1), _pr(2), _pr(3), _pr(4)]
    tasks = {
        "task_0001": {"cost_usd_total": 2.5, "cost_incomplete": False, "duration_s_total": 600},
        "task_0002": {"cost_usd_total": 1.5, "cost_incomplete": True, "duration_s_total": 300},
        "task_0003": {"cost_usd_total": None, "cost_incomplete": True, "duration_s_total": None},
    }
    out = qm.cost_metrics(lanes, tasks)
    assert out["m12_cost_usd"] == pytest.approx(4.0)
    assert out["cost_is_floor"] is True
    assert out["tasks_with_cost"] == 2
    assert out["tasks_without_cost"] == ["task_0003"]
    assert out["tasks_unread"] == ["task_0004"]
    assert out["m12_wall_s"] == 900
    assert out["median_cost_usd"] == pytest.approx(2.0)


def test_the_task_id_is_read_from_the_lane_branch():
    assert qm.task_id_of(_pr(1, head="swarm/task_ab12CD")) == "task_ab12CD"
    assert qm.task_id_of(_pr(1, head="swarm/task_ab12-fix")) == "task_ab12"
    assert qm.task_id_of(_pr(1, head="feature/x")) is None


def test_cost_without_a_credential_is_not_measured():
    assert qm.cost_metrics([_pr(1)], None) == {"measured": False}


# --------------------------------------------------------------------------
# the report, and the CLI the script calls
# --------------------------------------------------------------------------

def test_the_report_says_how_much_it_visited():
    lanes = [_pr(1), _pr(2)]
    runs = [_run(1, lanes[0]["head"]["ref"], "failure"), _run(2, lanes[0]["head"]["ref"], "cancelled")]
    report = qm.build_report({"lanes": lanes, "runs": runs})
    assert report["visited"]["lanes"] == 2
    assert report["visited"]["runs_read"] == 2
    assert report["visited"]["runs_counted"] == 1
    assert report["window"]["first_pr"] == 1 and report["window"]["last_pr"] == 2
    text = "\n".join(qm.render(report))
    assert "M1" in text and "M12" in text
    assert "not measured" in text       # M9, M11, M12 without swarm-api


def _write(dirpath: Path, name: str, data) -> None:
    (dirpath / name).write_text(json.dumps(data), encoding="utf-8")


def _cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True,
                          cwd=cwd, check=False)


def test_cli_report_reads_a_recorded_directory(tmp_path):
    lanes = [_pr(1, body=VERDICT_NOT_YET), _pr(2)]
    _write(tmp_path, "lanes.json", lanes)
    _write(tmp_path, "runs.json", [_run(1, lanes[0]["head"]["ref"], "failure")])
    (tmp_path / "commits").mkdir()
    (tmp_path / "commits" / "1.log").write_text(
        _log(("1" * 40, ["a" * 40], OPERATOR, "2026-10-12T09:30:00+00:00", ["x"])), encoding="utf-8")
    (tmp_path / "commits" / "2.log").write_text("", encoding="utf-8")
    proc = _cli("report", "--dir", str(tmp_path), "--json", "--operator-author", OPERATOR)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["schema"] == qm.SCHEMA
    assert report["ci"]["m2_red_prs"] == 1
    assert report["hand_fixup"]["m6_prs"] == 1
    assert report["review"]["m4_not_yet_prs"] == 1


def test_cli_report_over_no_lanes_exits_2(tmp_path):
    _write(tmp_path, "lanes.json", [])
    proc = _cli("report", "--dir", str(tmp_path))
    assert proc.returncode == 2
    assert "no lane" in proc.stderr


def test_cli_select_writes_the_window(tmp_path):
    pages = tmp_path / "pages.json"
    pages.write_text(json.dumps([_pr(1), _pr(2, merged_at=None)]) + "\n" + json.dumps([_pr(3)]),
                     encoding="utf-8")
    proc = _cli("select", "--limit", "150", "--pages", str(pages))
    assert proc.returncode == 0, proc.stderr
    assert [p["number"] for p in json.loads(proc.stdout)] == [1, 3]


def test_cli_windows_split_the_runs_read_into_bounded_ranges():
    proc = _cli("windows", "--since", "2026-10-11T12:41:00Z", "--until", "2026-10-12T00:00:00Z",
                "--hours", "6")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [
        "2026-10-11T12:41:00Z..2026-10-11T18:40:59Z",
        "2026-10-11T18:41:00Z..2026-10-12T00:00:00Z",
    ]


def test_cli_lists_the_issue_run_prs_whose_closing_comment_is_needed(tmp_path):
    prs = [_pr(1136, body=MARK, merged_at=None, closed_at="2026-10-11T23:00:00Z"),
           _pr(970, body=MARK), _pr(1000, body="none", merged_at=None)]
    pages = tmp_path / "prs.json"
    pages.write_text(json.dumps(prs), encoding="utf-8")
    proc = _cli("issue-run-prs", "--pages", str(pages), "--since", "2026-10-01T00:00:00Z")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["1136"]


# --------------------------------------------------------------------------
# the script's house rules (CLAUDE.md "Writing scripts")
# --------------------------------------------------------------------------

def _effective_lines(text):
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield stripped


def test_script_carries_set_euo_pipefail_first_and_sources_common():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert next(_effective_lines(text)) == "set -euo pipefail"
    assert re.search(r"^source .*lib/common\.sh\"?$", text, re.MULTILINE)
    assert SCRIPT.stat().st_mode & 0o111, "the script must be executable"
    assert "| redact" in text


def test_script_only_reads():
    text = SCRIPT.read_text()
    # The impact route is a POST that reads; nothing else may be one.
    posts = re.findall(r"api_request POST \S+", text)
    assert posts and all("/impact" in p for p in posts), posts
    for verb in ("PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{verb}\b", text), verb
    assert not re.search(r"gh (pr|issue) (close|edit|comment|merge|create)", text)
    assert "fs_" not in text


def test_script_avoids_bash4_only_constructs():
    text = SCRIPT.read_text()
    assert "mapfile" not in text and "readarray" not in text
    assert "declare -A" not in text
    assert ",,}" not in text and "^^}" not in text
