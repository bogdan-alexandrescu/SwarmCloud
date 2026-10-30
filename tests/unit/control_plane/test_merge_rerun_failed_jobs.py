"""The free re-run re-runs the failed jobs of the failing check's workflow run (#2841).

The merge wake tick's one free re-run per head (SPEED-D2) used to ask
`POST actions/jobs/<id>/rerun` of the failing REQUIRED check's own job. For
application.yml that job is the fan-in `ci-gate` (`needs:` every other job),
and a job re-run re-runs the job and its DEPENDENTS, never its needs: the
flaky `unit tests (1/1)` job it aggregates never re-ran (PR 2790 at
d6548f96), the gate read red again, and the re-run was spent. The tick now
asks `POST actions/runs/<run_id>/rerun-failed-jobs` of the workflow run each
failing Actions check belongs to, once per distinct run, and falls back to
the job re-run only when no run can be resolved.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import timedelta

from swarm_api import issueci, mergewake

from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    HEAD,
    _fix_record,
    _fix_workflows,
    _tick,
    _wait,
    clock,
    fixable_merge,
    github,
    tenant_tokens,
    wake_client,
)

GATE = "ci-gate"
LINT = "ci / lint"
RUN = 18_552_431_907


def _red_gate(github, *, run_id: int | None = RUN, name: str = GATE) -> int:
    """A fresh failing `name` at HEAD, a job of Actions run `run_id`."""
    return github.check(HEAD, name, "failure", run_id=run_id,
                        output={"title": "needs failed", "summary": "unit tests (1/1) failed"})


def _require_gate(github, *names: str) -> None:
    github.require(*(names or (GATE,)))
    github.check_runs[HEAD] = []


def test_a_red_ci_gate_reruns_the_failed_jobs_of_its_workflow_run(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _require_gate(github)
    gate = _red_gate(github)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1, body
    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0
    assert github.run_reruns == [RUN]
    assert github.reruns == [], "the fan-in job's own re-run never re-runs its needs"
    assert ("POST", f"/repos/example-org/widgets/actions/runs/{RUN}/rerun-failed-jobs") \
        in github.writes()
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD
    assert rerun["jobs"] == [gate] and rerun["runs"] == [RUN]
    assert "error" not in rerun
    assert "rounds" not in _fix_record(db)
    assert _fix_workflows(db) == []
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_two_failing_checks_of_one_run_ask_one_rerun(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _require_gate(github, GATE, LINT)
    gate = _red_gate(github)
    lint = _red_gate(github, name=LINT)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1, body
    assert github.run_reruns == [RUN]
    assert github.reruns == []
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["jobs"] == sorted([gate, lint]) and rerun["runs"] == [RUN]


def test_a_check_with_no_resolvable_run_falls_back_to_the_job_rerun(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _require_gate(github)
    gate = _red_gate(github, run_id=None)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1, body
    assert github.run_reruns == []
    assert github.reruns == [gate]
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["jobs"] == [gate] and rerun["runs"] == []


def test_a_refused_run_rerun_goes_to_a_round_in_the_same_tick(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _require_gate(github)
    gate = _red_gate(github)  # rerun_answer stays 403: no `actions: write`

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1 and body["report"]["rerun"] == 0, body
    assert github.run_reruns == [] and github.reruns == []
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD and "error" in rerun
    assert rerun["jobs"] == [gate] and rerun["runs"] == [RUN]
    assert len(_fix_record(db)["rounds"]) == 1


def test_a_settling_run_rerun_is_waited_on_not_asked_again(db, wake_client, github, clock):
    """GitHub may still serve the failed ci-gate as the latest right after
    the request: the tick waits on it, it never asks a second re-run."""
    fixable_merge(db, github, rounds=2)
    _require_gate(github)
    _red_gate(github)
    github.rerun_answer = 201
    _tick(wake_client)

    clock.now = clock.now + timedelta(seconds=issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1 and body["report"]["fixing"] == 0, body
    assert github.run_reruns == [RUN], "a head is re-run once, never per tick"
    assert _fix_workflows(db) == []
