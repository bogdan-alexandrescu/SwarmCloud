"""A required check its head's finished, failed Actions run never reported reads red (#2999).

PR #2959 at 5a5d673b7, measured 2026-11-01: both `application` runs at the
head (pull_request and push) concluded `failure` at 09:12Z on a red unit
shard, and ended without a `ci-gate` check run (26 check runs, none named
ci-gate). The required check was absent, absent read as pending, and the
merge step parked 13 hours on CI_PENDING, re-checking 56 times, for a check
no run would produce. Now a required check that NO check run reports reads
red once every Actions run at the head that is not superseded has concluded
and one concluded failed: that run's failing jobs are what is red, so the
wake tick's free re-run and CI-fix round go to them, as for a red ci-gate.
A run still in progress, or a failed run superseded by a newer one of its
workflow, event and branch, keeps the check pending.

THE PARITY TABLE runs swarm-api's `forgechecks.unreported_failure` and the
worker's `merge.unreported_failure` over the same cases; the tick tests run
the real writer over a fake `send`.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import merge as worker_merge
from swarm_api import forgechecks, forgewrite, mergewake
from swarm_api.validation import IssueRef

from . import forge_fakes
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    HEAD,
    NUMBER,
    ROOT,
    _fix_record,
    _fix_workflows,
    _tick,
    _wait,
    clock,
    fixable_merge,
    tenant_tokens,
    wake_client,
)
from .test_merge_wake_other_branch_push import BranchGitHub

GATE = "ci-gate"
SHARD = "console component tests (4/4)"
LINT = "lint"
#: The pull request's head branch: the branch its root task pushed, which a
#: CI-fix round continues.
BRANCH = f"swarm/{ROOT}"
PR_RUN, PUSH_RUN, SECURITY_RUN, NEWER_PR_RUN = 41290837472, 41290837100, 41290837200, 41290999999
PR_SUITE, PUSH_SUITE, SECURITY_SUITE, NEWER_SUITE = 901, 902, 903, 904
SHARD_RED, PUSH_SHARD_RED = 124801931332, 124801931000
APPLICATION, SECURITY = 11, 12
REF = IssueRef(owner="example-org", repo="widgets", number=NUMBER)

# ---------------------------------------------------------------------------
# 1. The rule, both readers over one table
# ---------------------------------------------------------------------------

#: (case, check runs as (check id, name, conclusion or None while running,
#: Actions run id, its conclusion or None while running, superseded),
#: the failing check ids, or None while the absent check may still report)
PARITY = [
    ("#2999: the failed run ended without the gate",
     [(1, SHARD, "failure", PR_RUN, "failure", False),
      (2, LINT, "success", PR_RUN, "failure", False),
      (3, "trivy", "success", SECURITY_RUN, "success", False)], [1]),
    ("both events failed: each failing job is named",
     [(1, SHARD, "failure", PR_RUN, "failure", False),
      (2, SHARD, "failure", PUSH_RUN, "failure", False)], [1, 2]),
    ("one run still in progress: it may yet report the gate",
     [(1, SHARD, "failure", PUSH_RUN, "failure", False),
      (2, SHARD, None, PR_RUN, None, False)], None),
    ("another workflow still in progress",
     [(1, SHARD, "failure", PR_RUN, "failure", False),
      (2, "trivy", None, SECURITY_RUN, None, False)], None),
    ("the failed run superseded by a newer run of its workflow",
     [(1, SHARD, "failure", PR_RUN, "failure", True)], None),
    ("superseded, its newer run green",
     [(1, SHARD, "failure", PR_RUN, "failure", True),
      (2, SHARD, "success", NEWER_PR_RUN, "success", False)], None),
    ("every run passed: nothing failed that would have reported it",
     [(1, SHARD, "success", PR_RUN, "success", False)], None),
    ("a cancelled run waits for its re-run (#1778)",
     [(1, SHARD, "failure", PR_RUN, "cancelled", False)], None),
    ("the Actions runs were not read", [(1, SHARD, "failure", None, None, False)], None),
    ("a cancelled job in a failed run is not what failed",
     [(1, SHARD, "cancelled", PR_RUN, "failure", False),
      (2, LINT, "failure", PR_RUN, "failure", False)], [2]),
]


def _github_run(check_id: int, name: str, conclusion: str | None, run: int | None,
                run_conclusion: str | None, superseded: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"id": check_id, "name": name,
                           "status": "completed" if conclusion else "in_progress",
                           "conclusion": conclusion, "app": {"id": 1, "slug": "github-actions"}}
    if run is not None:
        row.update(workflow_run_id=run, workflow_conclusion=run_conclusion,
                   workflow_superseded=superseded)
    return row


def _worker_check(check_id: int, name: str, conclusion: str | None, run: int | None,
                  run_conclusion: str | None, superseded: bool) -> worker_merge.CheckFacts:
    return worker_merge.CheckFacts(
        name=name, status="completed" if conclusion else "in_progress", conclusion=conclusion,
        app_id=1, run_id=check_id, workflow_run_id=run, workflow_conclusion=run_conclusion,
        workflow_superseded=superseded)


@pytest.mark.parametrize(("case", "rows", "failing"), PARITY, ids=[c[0] for c in PARITY])
def test_swarm_api_and_the_merge_step_read_an_unreported_check_alike(case, rows, failing):
    api = forgechecks.unreported_failure([_github_run(*row) for row in rows])
    worker = worker_merge.unreported_failure([_worker_check(*row) for row in rows])

    # MUTATION (forgechecks.py / merge.py `unreported_failure`): ignore
    # `workflow_superseded` -> the superseded rows read failed; treat a run
    # with no conclusion as finished -> the in-progress rows read failed.
    assert (None if api is None else sorted(r["id"] for r in api)) == failing, case
    assert (None if worker is None else sorted(c.run_id for c in worker)) == failing, case


def _required(*names: str) -> list[forgechecks.RequiredCheck]:
    return [forgechecks.RequiredCheck(name, None) for name in names]


def test_a_head_whose_failed_run_never_reported_the_gate_reads_red_naming_its_jobs():
    runs = [_github_run(*row) for row in PARITY[0][1]]

    reading = forgechecks.evaluate(_required(GATE), runs, [])

    # MUTATION (forgechecks.py `evaluate`): read an absent required check as
    # pending, as before #2999 -> PENDING, "ci-gate" pending for 13 hours.
    assert reading.state == forgechecks.RED, reading
    assert reading.pending == []
    assert reading.unreported == [GATE]
    assert [r["id"] for r in reading.failing_runs] == [1]
    assert reading.failing_names() == [SHARD]
    assert {"name": GATE, "state": forgechecks.FAILED, "url": None} in reading.checks
    assert reading.counts[forgechecks.FAILED] == 1


@pytest.mark.parametrize("row", [2, 4], ids=["run in progress", "run superseded"])
def test_an_absent_gate_stays_pending_while_a_run_may_still_report_it(row):
    _case, rows, _failing = PARITY[row]

    reading = forgechecks.evaluate(_required(GATE), [_github_run(*r) for r in rows], [])

    assert reading.state == forgechecks.PENDING, reading
    assert reading.pending == [GATE] and reading.failing_runs == []


def test_a_reported_gate_is_judged_by_its_own_run_as_before():
    """The rule reads only a check NO run reports: a green ci-gate beside a
    red shard nothing requires is green, as it always was."""
    rows = [_github_run(1, SHARD, "failure", PR_RUN, "failure", False),
            _github_run(2, GATE, "success", PR_RUN, "failure", False)]

    reading = forgechecks.evaluate(_required(GATE), rows, [])

    assert reading.state == forgechecks.GREEN, reading


# ---------------------------------------------------------------------------
# 2. The wake tick at PR #2959's head
# ---------------------------------------------------------------------------

def _job(github: BranchGitHub, check_id: int, name: str, conclusion: str | None, suite: int,
         run_id: int) -> None:
    github.check(HEAD, name, conclusion, check_id=check_id, run_id=run_id,
                 status="completed" if conclusion else "in_progress",
                 output={"title": "1 failed", "summary": "voice/Talk.tsx and voice/talk.css collide"})
    github.check_runs[HEAD][-1]["check_suite"] = {"id": suite}


def _actions_run(run_id: int, suite: int, event: str, workflow: int,
                 conclusion: str | None) -> dict[str, Any]:
    return {"id": run_id, "check_suite_id": suite, "event": event, "head_branch": BRANCH,
            "workflow_id": workflow, "head_sha": HEAD,
            "status": "completed" if conclusion else "in_progress", "conclusion": conclusion}


def _talk_pane(github: BranchGitHub, *, pull_request_run: str | None = "failure") -> None:
    """5a5d673b7's answers: requiring ci-gate; the push run failed on the
    shard and reported no ci-gate; the pull_request run concluded
    `pull_request_run` (None: still running); security passed."""
    github.open_pull(NUMBER, HEAD, ref=BRANCH)
    github.require(GATE)
    github.check_runs[HEAD] = []
    _job(github, PUSH_SHARD_RED, SHARD, "failure", PUSH_SUITE, PUSH_RUN)
    _job(github, SHARD_RED, SHARD, pull_request_run, PR_SUITE, PR_RUN)
    _job(github, 124801939999, "trivy", "success", SECURITY_SUITE, SECURITY_RUN)
    github.workflow_runs = [
        _actions_run(PUSH_RUN, PUSH_SUITE, "push", APPLICATION, "failure"),
        _actions_run(PR_RUN, PR_SUITE, "pull_request", APPLICATION, pull_request_run),
        _actions_run(SECURITY_RUN, SECURITY_SUITE, "push", SECURITY, "success"),
    ]


@pytest.fixture
def github() -> BranchGitHub:
    """test_merge_wake's `wake_client` reads through this fake (overrides its `github`)."""
    return BranchGitHub()


def _read(github: BranchGitHub) -> mergewake._Reading:
    return mergewake._read(forgewrite.GitHubWriter(send=github), REF, HEAD,
                           forge_fakes.make_token())


def test_the_tick_reads_the_talk_pane_head_red_on_its_failed_runs(github):
    _talk_pane(github)

    read = _read(github)

    # MUTATION (forgechecks.py `evaluate`): absent reads pending -> reason None.
    assert read.reason == "red", read
    assert sorted(r["id"] for r in read.checks.failing_runs) == [PUSH_SHARD_RED, SHARD_RED]
    assert {r["workflow_run_id"] for r in read.checks.failing_runs} == {PR_RUN, PUSH_RUN}


def test_the_tick_claims_a_round_for_a_gate_its_failed_run_never_reported(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _talk_pane(github)

    body = _tick(wake_client).json()

    # MUTATION (forgechecks.py `evaluate`): absent reads pending -> the park
    # waits again, "1 checks pending (ci-gate)", and no round is claimed.
    assert body["report"]["fixing"] == 1 and body["report"]["woken"] == 0, body
    (claimed,) = _fix_record(db)["rounds"]
    assert claimed["round"] == 1 and claimed["head"] == HEAD
    (workflow,) = _fix_workflows(db)
    fix_task = db.docs[f"tasks/{workflow['steps'][0]['task_id']}"]
    assert SHARD in fix_task["input"]["prompt"]
    reading = _wait(db)[mergewake.READING_KEY]
    assert reading["pending"] == [] and reading["failed"] == [SHARD], reading


def test_the_tick_re_runs_the_failed_runs_first_when_github_takes_it(db, wake_client, github):
    """Flake re-runs included: the free re-run asks for the runs that failed."""
    fixable_merge(db, github, rounds=2)
    _talk_pane(github)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1 and body["report"]["fixing"] == 0, body
    assert sorted(github.run_reruns) == sorted([PR_RUN, PUSH_RUN])
    assert _fix_workflows(db) == []


def test_with_no_round_left_the_tick_wakes_the_step_red(db, wake_client, github):
    fixable_merge(db, github, rounds=0)
    _talk_pane(github)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "red"


def test_a_run_still_in_progress_keeps_the_gate_pending(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _talk_pane(github, pull_request_run=None)

    body = _tick(wake_client).json()

    # MUTATION (forgechecks.py `unreported_failure`): judge only the runs that
    # finished -> the push run's failure reads red and a round is claimed
    # while the pull_request run may still report ci-gate.
    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0, body
    assert body["report"]["waiting"] == 1
    assert _wait(db)[mergewake.READING_KEY]["pending"] == [GATE]
    assert _fix_workflows(db) == []


def test_a_superseded_failed_run_keeps_the_gate_pending(db, wake_client, github):
    """The failed pull_request run has a newer run of its workflow, event and
    branch at the head, queued with no job yet: that run decides."""
    fixable_merge(db, github, rounds=2)
    github.open_pull(NUMBER, HEAD, ref=BRANCH)
    github.require(GATE)
    github.check_runs[HEAD] = []
    _job(github, SHARD_RED, SHARD, "failure", PR_SUITE, PR_RUN)
    github.workflow_runs = [
        _actions_run(PR_RUN, PR_SUITE, "pull_request", APPLICATION, "failure"),
        {**_actions_run(NEWER_PR_RUN, NEWER_SUITE, "pull_request", APPLICATION, None),
         "status": "queued"},
    ]

    body = _tick(wake_client).json()

    # MUTATION (forgechecks.py `unreported_failure`): ignore
    # `workflow_superseded` -> red, and a round on a run already replaced.
    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0, body
    assert _wait(db)[mergewake.READING_KEY]["pending"] == [GATE]
    assert _fix_workflows(db) == []
