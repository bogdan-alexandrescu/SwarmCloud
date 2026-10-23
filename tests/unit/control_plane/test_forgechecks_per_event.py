"""swarm-api decides a required check by the latest run of EACH event, as the merge step does (CHECK-RULE; part of #1999 and #1778).

Owner decision, BGD 2026-10-22: (1) a required check is decided by the
LATEST run of each event that ran it (push, pull_request, any other); a
re-run replaces the earlier runs of its own event, and every event's latest
run must be green, so a red push run is never outvoted by a green
pull_request run (CI-EARLY, #1694). (2) EVERYWHERE: the worker's merge step,
`forgechecks.required_check_state` and its readers (issue runs, the merge
train, the merge wake, `pr_checks`, failure-review), and auto-merge.yml.

swarm-api cannot import the worker and the shared rule cannot live in the
frozen contract, so the rule is written twice; THE PARITY TABLE below runs
both over the same cases, so the console, the train and the merge never
disagree.

No cloud: GitHub is an in-memory answer under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from agent_worker import merge as worker_merge
from swarm_api import forgechecks, forgewrite
from swarm_api.validation import IssueRef

from . import forge_fakes

NAME = "ci-gate"
REF = IssueRef(owner="example-org", repo="widgets", number=0)
SHA = "a" * 40

#: (case, [(run id, status, conclusion, event)], forgechecks state)
#: The worker's state is the same with `red` spelt `failed`.
PARITY = [
    ("one event, failed then re-run green", [(1, "completed", "failure", "push"),
                                             (2, "completed", "success", "push")], "green"),
    ("push green, pull_request red", [(1, "completed", "success", "push"),
                                      (2, "completed", "failure", "pull_request")], "red"),
    ("push red, pull_request green", [(1, "completed", "failure", "push"),
                                      (2, "completed", "success", "pull_request")], "red"),
    ("one event, green then red", [(1, "completed", "success", "push"),
                                   (2, "completed", "failure", "push")], "red"),
    ("#1778: cancelled then green", [(1, "completed", "cancelled", "push"),
                                     (2, "completed", "success", "push")], "green"),
    ("listed newest first", [(2, "completed", "success", "push"),
                             (1, "completed", "failure", "push")], "green"),
    ("an event's latest still running", [(1, "completed", "success", "push"),
                                         (2, "in_progress", None, "pull_request")], "pending"),
    ("a red event beside a running one", [(1, "completed", "failure", "push"),
                                          (2, "queued", None, "pull_request")], "pending"),
    ("a running run superseded in its event", [(1, "in_progress", None, "push"),
                                               (2, "completed", "success", "push")], "green"),
    ("every event green", [(1, "completed", "success", "push"),
                           (2, "completed", "skipped", "pull_request")], "green"),
    ("no runs", [], "pending"),
]


def _github_run(run_id: int, status: str, conclusion: str | None, event: str | None) -> dict[str, Any]:
    """A check run as `GitHubWriter.check_runs` returns it: GitHub's fields,
    and the `event` it adds from the check suite's workflow run."""
    run: dict[str, Any] = {"id": run_id, "name": NAME, "status": status, "conclusion": conclusion,
                           "app": {"id": 1}, "check_suite": {"id": 500 + run_id}}
    if event is not None:
        run["event"] = event
    return run


def _worker_run(run_id: int, status: str, conclusion: str | None, event: str | None) -> dict[str, Any]:
    """The same run as the worker's `CheckFacts.as_run` hands it over."""
    return worker_merge.CheckFacts(name=NAME, status=status, conclusion=conclusion, app_id=1,
                                   run_id=run_id, event=event, check_suite_id=500 + run_id).as_run()


@pytest.mark.parametrize(("case", "runs", "state"), PARITY, ids=[c[0] for c in PARITY])
def test_swarm_api_and_the_merge_step_decide_every_case_alike(case, runs, state):
    api = forgechecks.required_check_state([_github_run(*r) for r in runs])
    worker = worker_merge.required_check_state([_worker_run(*r) for r in runs])
    # MUTATION (forgechecks.py): keep the every-run rule -> the re-run and
    # #1778 cases read "red".
    # MUTATION (either side): drop the per-event grouping and take the newest
    # run overall -> "push red, pull_request green" reads "green".
    assert api == state, case
    # MUTATION (merge.py): change one side only -> the two disagree here.
    assert {"failed": "red"}.get(worker, worker) == api, (case, worker, api)


def test_the_reading_fails_only_the_latest_red_run_of_each_event():
    """What `issueci` builds the excerpt from, and the run page counts."""
    required = [forgechecks.RequiredCheck(NAME, 1)]
    flaky, rerun = _github_run(1, "completed", "failure", "push"), _github_run(2, "completed", "success", "push")
    reading = forgechecks.evaluate(required, [flaky, rerun], [])
    # MUTATION: keep the every-run rule in `evaluate` -> "red", the flake in failing_runs.
    assert reading.state == forgechecks.GREEN, reading
    assert reading.failing_runs == []
    # MUTATION: count every run on the page -> one failed beside one passed.
    assert reading.counts[forgechecks.FAILED] == 0 and reading.counts[forgechecks.PASSED] == 1

    red_pr = _github_run(3, "completed", "failure", "pull_request")
    reading = forgechecks.evaluate(required, [flaky, rerun, red_pr], [])
    assert reading.state == forgechecks.RED
    # MUTATION: fail every non-green run of the check -> the superseded flake too.
    assert reading.failing_runs == [red_pr]


# ---------------------------------------------------------------------------
# The forge read: `check_runs` adds the event of each run's check suite
# ---------------------------------------------------------------------------


class _GitHub:
    """`GET commits/{sha}/check-runs` and `GET actions/runs?head_sha=`."""

    def __init__(self, runs: list[dict[str, Any]], workflow_runs: list[dict[str, Any]],
                 actions_status: int = 200) -> None:
        self.runs = runs
        self.workflow_runs = workflow_runs
        self.actions_status = actions_status
        self.calls: list[str] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/check-runs"):
            return 200, json.dumps({"total_count": len(self.runs), "check_runs": self.runs}).encode()
        if parsed.path.endswith("/actions/runs"):
            if self.actions_status != 200:
                return self.actions_status, json.dumps({"message": "refused"}).encode()
            assert parse_qs(parsed.query).get("head_sha") == [SHA], url
            return 200, json.dumps({"total_count": len(self.workflow_runs),
                                    "workflow_runs": self.workflow_runs}).encode()
        return 404, json.dumps({"message": "no route"}).encode()


def _raw(run_id: int, conclusion: str, suite: int) -> dict[str, Any]:
    return {"id": run_id, "name": NAME, "status": "completed", "conclusion": conclusion,
            "app": {"id": 1}, "check_suite": {"id": suite}}


def test_check_runs_adds_each_run_s_event_from_its_check_suite():
    github = _GitHub([_raw(1, "failure", 71), _raw(2, "success", 72)],
                     [{"id": 9, "check_suite_id": 71, "event": "push"},
                      {"id": 10, "check_suite_id": 72, "event": "pull_request"}])

    runs = forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: never read actions/runs -> no event, and the two suites
    # would still be told apart only by their suite ids.
    assert [r.get("event") for r in runs] == ["push", "pull_request"], runs
    assert forgechecks.required_check_state(runs) == forgechecks.RED


def test_check_runs_reads_no_workflow_runs_when_each_check_ran_once():
    github = _GitHub([_raw(1, "success", 71), {**_raw(2, "success", 71), "name": "lint"}], [])

    forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: read actions/runs at every head -> one more request each time.
    assert not any("/actions/runs" in url for url in github.calls), github.calls


@pytest.mark.parametrize("status", [403, 404])
def test_without_actions_read_runs_are_grouped_by_check_suite(status):
    """A credential without `actions: read`: no event, and no error. Each
    check suite is then its own group, so a green pull_request suite still
    never outvotes a red push suite."""
    github = _GitHub([_raw(1, "failure", 71), _raw(2, "success", 72)], [], actions_status=status)

    runs = forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: let the refusal escape -> ChecksForbidden / ForgeWriteNotFound.
    assert all("event" not in r for r in runs), runs
    # MUTATION: read every event-less run as one group -> "green".
    assert forgechecks.required_check_state(runs) == forgechecks.RED


# ---------------------------------------------------------------------------
# No required check: every check, by the same rule (CHECK-RULE-2)
# ---------------------------------------------------------------------------
# BGD, 2026-10-22: "Yes, one rule for every check." `evaluate` with no
# required set judged EVERY run, so a superseded failure, or a superseded run
# still in progress, held the reading. Now each check (name and App) is judged
# by the latest run of each event, with `other_check_state`; what it lists
# and fails are those deciding runs only, as the required branch does.


def _other(run_id: int, status: str, conclusion: str | None, event: str) -> dict[str, Any]:
    return {**_github_run(run_id, status, conclusion, event), "name": "lint"}


def test_no_required_a_failure_re_run_green_is_green():
    flaky, rerun = _other(1, "completed", "failure", "push"), _other(2, "completed", "success", "push")
    reading = forgechecks.evaluate([], [flaky, rerun], [])
    # MUTATION: judge every run instead of each event's latest -> "red".
    assert reading.state == forgechecks.GREEN, reading
    assert reading.failing_runs == []
    # MUTATION: list every run, not the deciding ones -> two checks counted.
    assert reading.checks == [forgechecks.run_entry(rerun)]


def test_no_required_a_run_in_progress_superseded_by_a_green_one_is_green():
    reading = forgechecks.evaluate(
        [], [_other(1, "in_progress", None, "push"), _other(2, "completed", "success", "push")], [])
    # MUTATION: judge every run instead of each event's latest -> "pending".
    assert reading.state == forgechecks.GREEN, reading
    assert reading.pending == []


def test_no_required_a_red_pull_request_run_beside_a_green_push_run_is_red():
    push, red_pr = _other(1, "completed", "success", "push"), _other(2, "completed", "failure", "pull_request")
    reading = forgechecks.evaluate([], [push, red_pr], [])
    # MUTATION: take the newest run of the check whatever its event -> still
    # red here, but "push red, pull_request green" goes green; judging only
    # the first event -> "green".
    assert reading.state == forgechecks.RED, reading
    assert reading.failing_runs == [red_pr]


def test_no_required_green_then_red_in_one_event_is_red():
    green, red = _other(1, "completed", "success", "push"), _other(2, "completed", "failure", "push")
    reading = forgechecks.evaluate([], [green, red], [])
    # MUTATION: take the OLDEST run of each event -> "green".
    assert reading.state == forgechecks.RED, reading
    assert reading.failing_runs == [red]


def test_no_required_checks_of_two_apps_with_one_name_are_judged_apart():
    """The control on the grouping: one App's green run never supersedes
    another App's red one."""
    red = {**_other(1, "completed", "failure", "push"), "app": {"id": 1}}
    green = {**_other(2, "completed", "success", "push"), "app": {"id": 2}}
    reading = forgechecks.evaluate([], [red, green], [])
    # MUTATION: group by name alone -> "green".
    assert reading.state == forgechecks.RED, reading


# ---------------------------------------------------------------------------
# A run GitHub concluded but left `in_progress` (CHECK-STUCK, part of #2031)
# ---------------------------------------------------------------------------
#
# BGD, 2026-10-23: a check run that carries a conclusion AND a completed_at
# is judged by its conclusion, whatever `status` says. PR 2006's `secret
# scan` read `in_progress` with `conclusion: success` and a `completed_at`
# while its workflow run was completed/success, and the merge step waited
# 6 h on it. A run with no conclusion stays pending; `completed` with no
# conclusion is not green -- red here, as before (`failed` in the worker).

STUCK_AT = "2026-10-22T19:05:52Z"

#: (case, status, conclusion, completed_at, required state, other state, bucket)
STUCK = [
    ("in_progress, success, completed_at", "in_progress", "success", STUCK_AT,
     forgechecks.GREEN, forgechecks.GREEN, forgechecks.PASSED),
    ("in_progress, failure, completed_at", "in_progress", "failure", STUCK_AT,
     forgechecks.RED, forgechecks.RED, forgechecks.FAILED),
    ("in_progress, no conclusion", "in_progress", None, None,
     forgechecks.PENDING, forgechecks.PENDING, forgechecks.PENDING),
    ("in_progress, success, no completed_at", "in_progress", "success", None,
     forgechecks.PENDING, forgechecks.PENDING, forgechecks.PENDING),
    ("completed, no conclusion", "completed", None, STUCK_AT,
     forgechecks.RED, forgechecks.RED, forgechecks.FAILED),
]


@pytest.mark.parametrize(("case", "status", "conclusion", "completed_at", "required", "other", "bucket"),
                         STUCK, ids=[row[0] for row in STUCK])
def test_a_run_with_a_conclusion_and_completed_at_is_judged_by_its_conclusion(
    case, status, conclusion, completed_at, required, other, bucket
):
    run = {**_github_run(1, status, conclusion, None), "completed_at": completed_at}
    # MUTATION: read status alone in `required_check_state` -> the first two
    # rows read "pending" (PR 2006's 6 h wait).
    assert forgechecks.required_check_state([run]) == required, case
    # MUTATION: read status alone in `other_check_state` -> the same rows
    # read "pending" for a check nothing requires.
    assert forgechecks.other_check_state(run) == other, case
    # MUTATION: read status alone in `run_bucket` -> the run page counts a
    # concluded run as pending.
    assert forgechecks.run_bucket(run) == bucket, case
    # MUTATION: accept a conclusion without a completed_at -> the fourth row
    # reads green.
    # The worker decides the same row alike (red spelt failed).
    facts = worker_merge.CheckFacts(name=NAME, status=status, conclusion=conclusion, app_id=1,
                                    run_id=1, completed_at=completed_at).as_run()
    worker = worker_merge.required_check_state([facts])
    # MUTATION (merge.py): change one side only -> the two disagree here.
    assert {"failed": "red"}.get(worker, worker) == required, (case, worker)


@pytest.mark.parametrize("required", [True, False], ids=["required", "nothing-required"])
def test_the_reading_fails_and_excerpts_a_stuck_failure(required):
    """What `issueci` builds the excerpt from: a run concluded `failure` but
    left `in_progress` is a failing run, not a pending one."""
    run = {**_github_run(1, "in_progress", "failure", None), "completed_at": STUCK_AT}
    reading = forgechecks.evaluate([forgechecks.RequiredCheck(NAME, 1)] if required else [], [run], [])
    # MUTATION: read status alone in `evaluate`'s failing-run filter -> the
    # state is red with no failing run to excerpt.
    assert reading.state == forgechecks.RED, reading
    assert reading.failing_runs == [run], reading
    assert reading.pending == []


@pytest.mark.parametrize("required", [True, False], ids=["required", "nothing-required"])
def test_the_reading_is_green_on_a_stuck_success(required):
    """PR 2006's `secret scan`, as swarm-api reads it."""
    run = {**_github_run(1, "in_progress", "success", None), "completed_at": STUCK_AT}
    reading = forgechecks.evaluate([forgechecks.RequiredCheck(NAME, 1)] if required else [], [run], [])
    # MUTATION: read status alone -> "pending", naming ci-gate.
    assert reading.state == forgechecks.GREEN, reading
    assert reading.counts[forgechecks.PASSED] == 1, reading.counts
