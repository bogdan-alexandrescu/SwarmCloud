"""`GitHubWriter.rerun_failed_jobs` and the workflow run id each Actions check carries (#2841).

The merge wake tick's free re-run asks GitHub to re-run the failed jobs of
the failing check's workflow run (`POST actions/runs/<id>/rerun-failed-jobs`):
a job re-run of a fan-in gate re-runs its dependents, never its needs. The
tick finds the run by `workflow_run_id`, which `mark_workflow_runs` and
`actions_check_runs` stamp from the Actions runs they already read.

No credentials, no network. Every token is built at runtime.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import forgechecks, forgewrite
from swarm_api.validation import IssueRef

from . import forge_fakes

REF = IssueRef(owner="example-org", repo="widgets", number=0)
SHA = "c" * 40
RUN = 18_552_431_907
ROW = {"id": RUN, "check_suite_id": 71, "workflow_id": 501, "event": "pull_request",
       "head_branch": "swarm/task_x", "head_sha": SHA, "status": "completed",
       "conclusion": "failure"}


class _Send:
    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        path = urlparse(url).path
        if path.endswith("/actions/runs"):
            return 200, json.dumps({"total_count": 1, "workflow_runs": [ROW]}).encode()
        if path.endswith(f"/actions/runs/{RUN}/jobs"):
            jobs = [{"id": 301, "name": "ci-gate", "status": "completed", "conclusion": "failure"}]
            return 200, json.dumps({"total_count": 1, "jobs": jobs}).encode()
        if method == "POST":
            return self.status, (b"{}" if self.status < 300 else b'{"message": "refused"}')
        return 404, b"{}"


def test_rerun_failed_jobs_posts_the_runs_rerun_failed_jobs_route_with_the_token():
    send = _Send()
    token = forge_fakes.make_token()

    forgewrite.GitHubWriter(send=send).rerun_failed_jobs(REF, RUN, token)

    ((method, url, headers, body),) = send.calls
    assert method == "POST"
    assert urlparse(url).path == \
        f"/repos/example-org/widgets/actions/runs/{RUN}/rerun-failed-jobs"
    assert token in headers.get("Authorization", "")
    assert json.loads(body) == {}


def test_a_refused_rerun_failed_jobs_names_the_actions_write_permission():
    send = _Send(status=403)

    with pytest.raises(forgewrite.ForgeWriteForbidden) as refused:
        forgewrite.GitHubWriter(send=send).rerun_failed_jobs(REF, RUN, forge_fakes.make_token())

    assert forgewrite.ACTIONS_WRITE in refused.value.message


def test_mark_workflow_runs_stamps_the_actions_run_id():
    runs = [{"id": 301, "name": "ci-gate", "check_suite": {"id": 71}},
            {"id": 302, "name": "lint", "check_suite": {"id": 99}}]

    marked = forgechecks.mark_workflow_runs(runs, [ROW])

    assert marked[0]["workflow_run_id"] == RUN
    assert "workflow_run_id" not in marked[1], "no Actions run made suite 99"


def test_actions_check_runs_stamps_each_job_with_its_run_id():
    runs: list[dict[str, Any]] = forgewrite.GitHubWriter(send=_Send()).actions_check_runs(
        REF, SHA, forge_fakes.make_token())

    assert [(r["id"], r["workflow_run_id"]) for r in runs] == [(301, RUN)]
