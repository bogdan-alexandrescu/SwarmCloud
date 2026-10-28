"""swarm-api's GitHub wire reads a red check in a cancelled Actions run as the merge step does (FORGEWRITE-2582; part of #2582).

#2582 (PR #2565 at 968d9949): a double push of one sha started two push runs
of application.yml, concurrency cancelled the first, and its `ci-gate`,
under `if: always()`, concluded `failure` over needs that were only
cancelled, while the pull_request run's `ci-gate` passed. The worker's
`merge.checks_at` attaches each check run's Actions run conclusion, so the
merge step read that as pending (`forgechecks.cancelled_by_its_workflow`).
`forgewrite.GitHubWriter.check_runs` and `actions_check_runs` did not, so
swarm-api's issue-run, wake and train reading of the same head said red.

No cloud: GitHub is an in-memory answer under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

from swarm_api import forgechecks, forgewrite
from swarm_api.validation import IssueRef

from . import forge_fakes

NAME = "ci-gate"
REF = IssueRef(owner="example-org", repo="widgets", number=0)
SHA = "b" * 40
WORKFLOW = 501

#: The Actions runs at SHA, #2582's shape: push run 9 cancelled by
#: concurrency, the pull_request run 10 passed.
CANCELLED_PUSH = {"id": 9, "check_suite_id": 71, "workflow_id": WORKFLOW, "event": "push",
                  "head_branch": "swarm/task_x", "head_sha": SHA, "status": "completed",
                  "conclusion": "cancelled"}
GREEN_PR = {"id": 10, "check_suite_id": 72, "workflow_id": WORKFLOW, "event": "pull_request",
            "head_branch": "swarm/task_x", "head_sha": SHA, "status": "completed",
            "conclusion": "success"}


class _GitHub:
    """`commits/{sha}/check-runs`, `actions/runs?head_sha=` and each run's jobs."""

    def __init__(self, runs: list[dict[str, Any]], workflow_runs: list[dict[str, Any]],
                 jobs: dict[int, list[dict[str, Any]]] | None = None) -> None:
        self.runs = runs
        self.workflow_runs = workflow_runs
        self.jobs = jobs or {}
        self.calls: list[str] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/check-runs"):
            return 200, json.dumps({"total_count": len(self.runs), "check_runs": self.runs}).encode()
        if parsed.path.endswith("/actions/runs"):
            assert parse_qs(parsed.query).get("head_sha") == [SHA], url
            return 200, json.dumps({"total_count": len(self.workflow_runs),
                                    "workflow_runs": self.workflow_runs}).encode()
        if parsed.path.endswith("/jobs"):
            run_id = int(parsed.path.split("/")[-2])
            jobs = self.jobs.get(run_id, [])
            return 200, json.dumps({"total_count": len(jobs), "jobs": jobs}).encode()
        return 404, json.dumps({"message": "no route"}).encode()


def _raw(run_id: int, conclusion: str | None, suite: int, name: str = NAME) -> dict[str, Any]:
    return {"id": run_id, "name": name, "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "app": {"id": 1}, "check_suite": {"id": suite}}


def _job(job_id: int, conclusion: str | None, name: str = NAME) -> dict[str, Any]:
    return {"id": job_id, "name": name, "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion}


def _state(runs: list[dict[str, Any]], name: str = NAME) -> str:
    return forgechecks.required_check_state([run for run in runs if run.get("name") == name])


# ---------------------------------------------------------------------------
# check_runs
# ---------------------------------------------------------------------------


def test_check_runs_reads_2582s_cancelled_push_gate_as_pending():
    github = _GitHub([_raw(1, "failure", 71), _raw(2, "success", 72)],
                     [CANCELLED_PUSH, GREEN_PR])

    runs = forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: never pass the runs through `mark_workflow_runs` -> "red",
    # the reading #2582 measured while the merge step said pending.
    assert _state(runs) == forgechecks.PENDING, runs
    assert sum("/actions/runs" in url for url in github.calls) == 1, github.calls


def test_check_runs_reads_a_red_gate_in_a_superseded_run_as_pending_with_no_repeated_name():
    """The cancelled push run's gate is the only `ci-gate` yet: the newer push
    run has not made one. The name does not repeat, so only the red run
    makes the Actions runs worth reading (the worker's `checks_at`)."""
    newer_push = {**CANCELLED_PUSH, "id": 11, "check_suite_id": 73, "status": "in_progress",
                  "conclusion": None}
    github = _GitHub([_raw(1, "failure", 71)], [CANCELLED_PUSH, newer_push])

    runs = forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: read the Actions runs only when a name repeats -> "red".
    assert _state(runs) == forgechecks.PENDING, runs
    assert runs[0].get("event") == "push", runs


def test_check_runs_keeps_a_failure_in_an_uncancelled_run_red():
    failed_push = {**CANCELLED_PUSH, "conclusion": "failure"}
    github = _GitHub([_raw(1, "failure", 71), _raw(2, "success", 72)], [failed_push, GREEN_PR])

    runs = forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: read every red run with an Actions run as cancelled -> "pending".
    assert _state(runs) == forgechecks.RED, runs


def test_check_runs_makes_no_actions_call_when_nothing_is_red():
    github = _GitHub([_raw(1, "success", 71), _raw(2, "skipped", 71, name="lint"),
                      _raw(3, None, 71, name="trivy")],
                     [CANCELLED_PUSH])

    forgewrite.GitHubWriter(send=github).check_runs(REF, SHA, forge_fakes.make_token())

    # MUTATION: read the Actions runs at every head -> one more request each time.
    assert not any("/actions/runs" in url for url in github.calls), github.calls


# ---------------------------------------------------------------------------
# actions_check_runs (a token with no "Checks" permission, #1188)
# ---------------------------------------------------------------------------


def test_actions_check_runs_reads_2582s_cancelled_push_gate_as_pending():
    github = _GitHub([], [CANCELLED_PUSH, GREEN_PR],
                     jobs={9: [_job(101, "failure")], 10: [_job(102, "success")]})

    runs = forgewrite.GitHubWriter(send=github).actions_check_runs(
        REF, SHA, forge_fakes.make_token())

    # MUTATION: never pass the jobs through `mark_workflow_runs` -> "red".
    assert _state(runs) == forgechecks.PENDING, runs
    # The Actions runs it already read are the ones it marks by: no second read.
    assert sum(urlparse(url).path.endswith("/actions/runs") for url in github.calls) == 1


def test_actions_check_runs_keeps_a_failure_in_an_uncancelled_run_red():
    failed_push = {**CANCELLED_PUSH, "conclusion": "failure"}
    github = _GitHub([], [failed_push, GREEN_PR],
                     jobs={9: [_job(101, "failure")], 10: [_job(102, "success")]})

    runs = forgewrite.GitHubWriter(send=github).actions_check_runs(
        REF, SHA, forge_fakes.make_token())

    assert _state(runs) == forgechecks.RED, runs
