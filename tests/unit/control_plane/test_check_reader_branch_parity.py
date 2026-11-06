"""swarm-api's check reader drops another branch's push suites exactly as the merge step does (#2946).

The worker's `merge.GitHubMerger.checks_at(sha, head_ref=)` drops a check run
whose suite was triggered by a push to a KNOWN branch other than the pull
request's head branch (`_other_branch_push`, #2365). swarm-api's
`forgewrite.GitHubWriter.check_runs(..., head_ref=)` feeds the merge wake,
issueci's auto-merge, the train, `pr_checks` and the shepherd; at a9009a19 it
kept a sibling branch's newer green push ci-gate and read green what the merge
step refused (#2946). swarm-api cannot import the worker, so the rule is
written twice; this table runs both readers over the SAME GitHub answers.

Commit statuses are left out of the comparison: swarm-api reads them in a
separate call (`commit_statuses`), the worker in `checks_at`.

No cloud: GitHub is an in-memory answer under both real readers.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import urlparse

import pytest

from agent_worker import forge as worker_forge
from agent_worker import merge as worker_merge
from swarm_api import forgewrite
from swarm_api.validation import IssueRef

NAME = "ci-gate"
REF = IssueRef(owner="example-org", repo="widgets", number=0)
SHA = "b" * 40
PR_BRANCH = "swarm/task_18f2c11a"
SIBLING = "swarm/task_0a667b37"


class _GitHub:
    """check-runs, actions/runs and the combined status at one sha, served to
    both readers: swarm-api's `send` and the worker's `transport`."""

    def __init__(self, runs: list[dict[str, Any]], workflow_runs: list[dict[str, Any]]) -> None:
        self.runs = runs
        self.workflow_runs = workflow_runs

    def answer(self, url: str) -> tuple[int, bytes]:
        path = urlparse(url).path
        if path.endswith("/check-runs"):
            body = {"total_count": len(self.runs), "check_runs": self.runs}
        elif path.endswith("/actions/runs"):
            body = {"total_count": len(self.workflow_runs), "workflow_runs": self.workflow_runs}
        elif path.endswith("/status"):
            body = {"state": "success", "statuses": [{"context": "legacy", "state": "success"}]}
        else:
            return 404, json.dumps({"message": "no route"}).encode()
        return 200, json.dumps(body).encode()

    def send(self, method, url, headers, body, timeout):
        return self.answer(url)

    def transport(self, request):
        status, body = self.answer(request.full_url)
        return status, {"content-type": "application/json"}, body


def _check(run_id: int, conclusion: str, suite: int, name: str = NAME) -> dict[str, Any]:
    return {"id": run_id, "name": name, "status": "completed", "conclusion": conclusion,
            "app": {"id": 15368}, "check_suite": {"id": suite}}


def _workflow_run(run_id: int, suite: int, event: str | None, branch: str | None,
                  conclusion: str = "success") -> dict[str, Any]:
    return {"id": run_id, "check_suite_id": suite, "event": event, "head_branch": branch,
            "workflow_id": 7, "head_sha": SHA, "status": "completed", "conclusion": conclusion}


#: (case, check runs, Actions runs, head_ref)
CASES = [
    ("sibling push green beside own push red",
     [_check(10, "failure", 71), _check(20, "success", 72), _check(30, "success", 73)],
     [_workflow_run(1, 71, "push", PR_BRANCH, "failure"), _workflow_run(2, 72, "push", SIBLING),
      _workflow_run(3, 73, "pull_request", PR_BRANCH)],
     PR_BRANCH),
    ("sibling push red beside own push green",
     [_check(10, "success", 71), _check(20, "failure", 72)],
     [_workflow_run(1, 71, "push", PR_BRANCH), _workflow_run(2, 72, "push", SIBLING, "failure")],
     PR_BRANCH),
    ("own push red, nothing else",
     [_check(10, "failure", 71)],
     [_workflow_run(1, 71, "push", PR_BRANCH, "failure")],
     PR_BRANCH),
    ("pull_request suites only, one from a fork's branch",
     [_check(10, "failure", 71), _check(20, "success", 72)],
     [_workflow_run(1, 71, "pull_request", PR_BRANCH, "failure"),
      _workflow_run(2, 72, "pull_request", SIBLING)],
     PR_BRANCH),
    ("a push suite of unknown branch, and one with no Actions run",
     [_check(10, "failure", 71), _check(20, "success", 72), _check(30, "success", 73)],
     [_workflow_run(1, 71, "push", None, "failure"), _workflow_run(2, 72, "push", SIBLING)],
     PR_BRANCH),
    ("no head_ref",
     [_check(10, "failure", 71), _check(20, "success", 72)],
     [_workflow_run(1, 71, "push", PR_BRANCH, "failure"), _workflow_run(2, 72, "push", SIBLING)],
     None),
    ("every check once and green: no Actions read, nothing dropped",
     [_check(10, "success", 72), _check(20, "success", 72, name="lint")],
     [_workflow_run(2, 72, "push", SIBLING)],
     PR_BRANCH),
]


def _token() -> str:
    return "ghp_" + secrets.token_hex(18)


@pytest.mark.parametrize(("case", "runs", "workflow_runs", "head_ref"), CASES,
                         ids=[c[0] for c in CASES])
def test_swarm_api_and_the_merge_step_keep_the_same_check_runs(case, runs, workflow_runs, head_ref):
    github = _GitHub(runs, workflow_runs)
    client = worker_forge.PinnedForgeClient(token=_token(), transport=github.transport)
    merger = worker_merge.GitHubMerger(client, owner=REF.owner, repo=REF.repo)

    worker = {(c.name, c.run_id, c.event) for c in merger.checks_at(SHA, head_ref=head_ref)
              if c.run_id is not None}
    api = {(r["name"], r["id"], r.get("event"))
           for r in forgewrite.GitHubWriter(send=github.send).check_runs(
               REF, SHA, _token(), head_ref=head_ref)}

    # MUTATION (forgewrite.py): ignore head_ref, or drop pull_request /
    # unknown-branch suites -> the two sets differ.
    # MUTATION (merge.py `_other_branch_push`): change the worker's rule
    # alone -> the two sets differ.
    assert api == worker, (case, api, worker)
