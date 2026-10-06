"""The plugin's rows for a merge step (lane MS5, docs/merge-step.md "Revised 2026-10-06" §6).

A merge step that waits for its pull request's checks is PARKED on
`CI_PENDING` (contract request 49, applied by MS2). It holds no capacity
(invariant 1), so the rows say what it waits for in plain words, and
`swarm_trouble` leaves it out exactly as it leaves out a dependency park: a
merge waiting for CI is the ordinary shape of a workflow whose last step has
not merged yet, not something wrong.

A finished merge step gets one compact row from its `result_summary.merge`:
merged (the commit and the issues closed), or refused with the worker's code.

Every `ParkReason` member must have a sentence: a reason added to the frozen
enum without one would print as its own lower-cased name.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_common.states import ParkReason
from swarm_mcp import compact, progress, render
from swarm_mcp.render import PLAIN, Snapshot, Style, find_trouble, render_task

WIDE = Style(width=200, color=False, unicode=False)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[3]
MERGE_PY = REPO / "apps" / "agent-worker" / "agent_worker" / "merge.py"

#: The refusal codes lane MS3 splits out of today's (docs/merge-step.md §6 MS3),
#: beside every code the worker's merge action refuses with today.
MS3_CODES = (
    "from_fork", "base_not_default", "protection_refused", "merge_conflict",
    "checks_timeout", "behind_too_often",
)


def _worker_codes() -> list[str]:
    """Every refusal code `agent_worker.merge` writes, read from its source."""
    source = MERGE_PY.read_text(encoding="utf-8")
    codes = set(re.findall(r'refuse\(\s*"([a-z_]+)"', source))
    codes |= set(re.findall(r'MERGE_(?:REFUSED|FAILED),\s*"([a-z_]+)"', source))
    codes |= set(re.findall(r'"code":\s*"([a-z_]+)"', source))
    return sorted(codes)


REFUSAL_CODES = sorted(set(_worker_codes()) | set(MS3_CODES))


def test_the_worker_source_yields_its_refusal_codes():
    # The control: a regex that matched nothing would make every case below
    # vacuous. These are codes the worker refuses with on main; MS3 split
    # `not_mergeable` into `protection_refused` and `merge_conflict`, and
    # test_merge_action.py holds that `not_mergeable` is gone.
    found = _worker_codes()
    for code in ("checks_failed", "head_moved", "merge_conflict", "token_lacks_rights",
                 "merge_unanswered", "credential_unreadable", "upstream_unreadable"):
        assert code in found, (code, found)


# --------------------------------------------------------------------------
# CI_PENDING's plain-language rows
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [r.value for r in ParkReason])
def test_every_park_reason_has_a_sentence(reason):
    assert reason in progress._PARKED_BECAUSE, reason
    assert reason in compact._WAITS_FOR, reason


def test_ci_pending_reads_as_waiting_for_the_checks():
    task = {
        "task_id": "task_merge01", "step_id": "merge", "state": "PARKED",
        "park_reason": "CI_PENDING", "blocked_by": ["python (unit)"],
    }
    line = progress.state_line(task)
    assert "waiting for the pull request's checks" in line
    assert "(CI_PENDING)" in line
    assert "holds no capacity" in line
    assert "stalled" not in line and "blocked" not in line.split("--")[0]


def test_ci_pending_progress_row_says_what_it_waits_for():
    task = {"task_id": "task_merge01", "step_id": "merge", "state": "PARKED",
            "park_reason": "CI_PENDING", "read": "ok"}
    assert compact.waits_for(task) == "pull request checks"


# --------------------------------------------------------------------------
# swarm_trouble
# --------------------------------------------------------------------------

def _parked(task_id: str, reason: str) -> dict:
    return {"id": task_id, "state": "PARKED", "park_reason": reason}


def test_trouble_omits_ci_pending_and_keeps_every_other_reason():
    snap = Snapshot(tasks=[
        _parked("t1", "CI_PENDING"),
        _parked("t2", "CI_PENDING"),
        _parked("t3", "DEPENDENCY_INCOMPLETE"),
        _parked("t4", "CREDENTIAL_MISSING"),
    ])
    parked = [f.what for f in find_trouble(snap, WIDE) if f.where == "parked"]
    assert len(parked) == 1, parked
    assert "credential missing" in parked[0]
    assert not any("ci pending" in d for d in parked)


def test_only_ci_pending_parks_are_no_trouble():
    snap = Snapshot(tasks=[_parked("t1", "CI_PENDING")])
    assert [f for f in find_trouble(snap, WIDE) if f.where == "parked"] == []


# --------------------------------------------------------------------------
# The merge result's compact row
# --------------------------------------------------------------------------

def _merge_task(state: str, merge: dict) -> dict:
    return {"task_id": "task_merge01", "step_id": "merge", "state": state,
            "runner_profile": "merge", "result_summary": {"merge": merge}}


def test_a_merge_row_names_the_commit_and_the_issues_closed():
    sha = "ab" * 20
    row = render.merge_row(_merge_task("SUCCEEDED", {
        "action": "merge", "merged_by_this_task": True, "pull_request": 719,
        "merge_commit": sha, "issues_closed": [352, 295],
    }))
    assert row == f"merged #719 at {sha[:12]} · issues closed #352, #295"


def test_a_merge_with_no_issue_to_close_says_none():
    row = render.merge_row(_merge_task("SUCCEEDED", {
        "merged_by_this_task": True, "pull_request": 7, "merge_commit": "c" * 40,
        "issues_closed": [],
    }))
    assert row.endswith("issues closed none")


def test_issues_left_open_are_said():
    row = render.merge_row(_merge_task("SUCCEEDED", {
        "merged_by_this_task": True, "pull_request": 7, "merge_commit": "c" * 40,
        "issues_closed": [1], "issues_not_closed": [{"number": 2, "reason": "the forge refused"}],
    }))
    assert "issues closed #1" in row
    assert "not closed #2" in row


def test_issues_unread_is_not_none_closed():
    row = render.merge_row(_merge_task("SUCCEEDED", {
        "merged_by_this_task": True, "pull_request": 7, "merge_commit": "c" * 40,
        "issues_unread": "GitHub answered 502",
    }))
    assert "issues unread" in row
    assert "closed none" not in row


def test_an_earlier_attempts_merge_is_already_merged():
    row = render.merge_row(_merge_task("SUCCEEDED", {
        "merged_by_this_task": False, "already_merged": True, "pull_request": 7,
        "merge_commit": "d" * 40, "issues_closed": [],
    }))
    assert row.startswith("already merged #7 at dddddddddddd")


@pytest.mark.parametrize("code", REFUSAL_CODES)
def test_the_merge_row_renders_each_refusal_code(code):
    row = render.merge_row(_merge_task("FAILED", {
        "merged_by_this_task": False, "pull_request": 7,
        "refusal": {"code": code, "message": "x" * 400},
    }))
    assert row == f"refused {code}"


def test_a_parked_merge_has_no_result_row():
    task = _merge_task("PARKED", {"wait": {"code": "checks_pending", "message": "m"}})
    assert render.merge_row(task) is None


def test_a_task_that_is_not_a_merge_has_no_row():
    assert render.merge_row({"state": "SUCCEEDED", "result_summary": {"git": {}}}) is None
    assert render.merge_row({"state": "SUCCEEDED"}) is None


class _Client:
    def attempts(self, task_id):
        return [{"cost_usd": 0.0}]


def test_the_progress_row_carries_the_merge_row():
    task = {**_merge_task("FAILED", {"refusal": {"code": "checks_failed", "message": "m"}}),
            "read": "ok", "terminal": True, "attempt_count": 1, "max_attempts": 3}
    line, _key = compact.progress_line(_Client(), task, NOW)
    assert line.endswith("· refused checks_failed"), line


def test_the_task_view_carries_the_merge_row():
    task = _merge_task("SUCCEEDED", {"merged_by_this_task": True, "pull_request": 7,
                                     "merge_commit": "e" * 40, "issues_closed": [3]})
    lines = render_task(task, PLAIN, NOW)
    assert any("merge" in line and "merged #7 at eeeeeeeeeeee · issues closed #3" in line
               for line in lines), lines
