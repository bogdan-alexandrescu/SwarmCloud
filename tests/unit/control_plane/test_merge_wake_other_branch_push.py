"""The merge wake and issueci read a pull request's checks with its head ref (#2946).

a9009a19, measured: swarm/task_18f2c11a's push run 41200297118 had a red
`ci-gate` (job 124538127127) and a red `unit tests (5/10)`; sibling
swarm/task_0a667b37's push run 41200133512, at the SAME sha, had a newer
green `ci-gate` (job 124538826396); the pull_request run 41200300965 was
green. `commits/{sha}/check-runs` lists all three suites, and
`forgechecks.latest_per_event` takes the larger id as the push result, so
the wake tick read the head green, never asked the free re-run of
41200297118, and issueci's auto-merge called green what the merge step
refused: the worker's `merge.checks_at` drops a push suite of another known
branch (`_other_branch_push`, #2365). `GitHubWriter.check_runs` drops it too
given `head_ref` (test_forgechecks_per_event.py); these tests hold that the
tick and `issueci._read_ci` pass the pull request's head ref, through the
real writer over a fake `send`.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import forgechecks, forgewrite, issueci, mergewake
from swarm_api.validation import IssueRef

from . import forge_fakes
from .test_forgechecks_per_event import (
    OWN_RED,
    OWN_RUN,
    PR_BRANCH,
    PR_GREEN,
    PR_RUN,
    SIBLING,
    SIBLING_GREEN,
    SIBLING_RUN,
    SUITE_A,
    SUITE_B,
    SUITE_C,
    _workflow_run,
)
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    HEAD,
    NUMBER,
    QueueGitHub,
    _fix_record,
    _fix_workflows,
    _tick,
    _wait,
    clock,
    fixable_merge,
    tenant_tokens,
    wake_client,
)

GATE = "ci-gate"
SHARD = "unit tests (5/10)"
SHARD_RED = 124538127001
REF = IssueRef(owner="example-org", repo="widgets", number=NUMBER)


class BranchGitHub(QueueGitHub):
    """`QueueGitHub`, plus `GET actions/runs?head_sha=`: the Actions runs at
    a sha, with the event and head branch of each run's check suite."""

    def __init__(self) -> None:
        super().__init__()
        self.workflow_runs: list[dict[str, Any]] = []

    def __call__(self, method, url, headers, body, timeout):
        if method == "GET" and urlparse(url).path.endswith("/actions/runs"):
            self.calls.append((method, url, dict(headers), body))
            return 200, json.dumps({"total_count": len(self.workflow_runs),
                                    "workflow_runs": self.workflow_runs}).encode()
        return super().__call__(method, url, headers, body, timeout)


def _run(github: BranchGitHub, check_id: int, name: str, conclusion: str, suite: int,
         run_id: int) -> None:
    github.check(HEAD, name, conclusion, check_id=check_id, run_id=run_id)
    github.check_runs[HEAD][-1]["check_suite"] = {"id": suite}


def _a9009a19(github: BranchGitHub) -> None:
    """The pull request on swarm/task_18f2c11a, requiring ci-gate, at a9009a19's answers."""
    github.open_pull(NUMBER, HEAD, ref=PR_BRANCH)
    github.require(GATE)
    github.check_runs[HEAD] = []
    _run(github, OWN_RED, GATE, "failure", SUITE_A, OWN_RUN)
    _run(github, SHARD_RED, SHARD, "failure", SUITE_A, OWN_RUN)
    _run(github, SIBLING_GREEN, GATE, "success", SUITE_B, SIBLING_RUN)
    _run(github, PR_GREEN, GATE, "success", SUITE_C, PR_RUN)
    github.workflow_runs = [
        _workflow_run(OWN_RUN, SUITE_A, "push", PR_BRANCH, "failure"),
        _workflow_run(SIBLING_RUN, SUITE_B, "push", SIBLING),
        _workflow_run(PR_RUN, SUITE_C, "pull_request", PR_BRANCH),
    ]


@pytest.fixture
def github() -> BranchGitHub:
    """test_merge_wake's `wake_client` reads through this fake (overrides its `github`)."""
    gh = BranchGitHub()
    _a9009a19(gh)
    return gh


def test_the_tick_reads_a9009a19_red_on_the_pull_request_s_own_push_run(github):
    read = mergewake._read(forgewrite.GitHubWriter(send=github), REF, HEAD,
                           forge_fakes.make_token())

    # MUTATION (mergewake.py `_read`): call check_runs without head_ref ->
    # the sibling's newer green push ci-gate decides push, and this is green.
    assert read.reason == "red", read
    assert read.pull.head_ref == PR_BRANCH
    failing = [r["id"] for r in read.checks.failing_runs]
    assert OWN_RED in failing and SIBLING_GREEN not in failing, failing
    assert {r["workflow_run_id"] for r in read.checks.failing_runs} == {OWN_RUN}


def test_the_tick_re_runs_the_failed_jobs_of_the_pull_request_s_push_run(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _a9009a19(github)  # fixable_merge opened the pull request on another branch
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    # MUTATION (mergewake.py `_read`): no head_ref -> green, the step woken
    # and nothing re-run.
    assert body["report"]["rerun"] == 1 and body["report"]["woken"] == 0, body
    assert github.run_reruns == [OWN_RUN]
    assert ("POST", f"/repos/example-org/widgets/actions/runs/{OWN_RUN}/rerun-failed-jobs") \
        in github.writes()
    assert SIBLING_RUN not in github.run_reruns and github.reruns == []
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD and rerun["runs"] == [OWN_RUN]
    assert OWN_RED in rerun["jobs"] and SIBLING_GREEN not in rerun["jobs"]
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert _fix_workflows(db) == []


def test_issueci_reads_a9009a19_red_so_auto_merge_does_not_call_it_green(github):
    run = SimpleNamespace(issue=REF, id="run_a9009a19")

    pull, reading = issueci._read_ci(forgewrite.GitHubWriter(send=github), run, NUMBER,
                                     forge_fakes.make_token())

    # MUTATION (issueci.py `_read_ci`): no head_ref -> GREEN, and auto_merge
    # reports the head green where the merge step refuses it.
    assert pull.head_ref == PR_BRANCH
    assert reading.state == forgechecks.RED, reading
    failing = [r["id"] for r in reading.failing_runs]
    assert OWN_RED in failing and SIBLING_GREEN not in failing, failing
