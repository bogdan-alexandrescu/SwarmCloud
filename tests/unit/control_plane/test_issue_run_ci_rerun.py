"""An issue run re-runs a red head's failed Actions jobs once before it spends
or refuses a CI-fix round (#3200).

run_7eddc5b3 (PR #3150) failed at its cap of 3 rounds on a 30 s vitest
timeout in one swarm-ui shard of a pull request that touched no swarm-ui
file, and nothing was ever re-run: the CHECKING read spent a round, or
refused at the cap, on the first red reading. The merge step already asks
GitHub to re-run first (`mergewake._rerun_first`); the issue run now shares
that rule (`cirerun`) and records its re-runs on the run's own
`pull_request.reruns`.

GitHub is the fake TRANSPORT under the shipped `forgewrite.GitHubWriter`
(`forge_fakes.GitHubWrites`: `rerun_answer`, `run_reruns`, `reruns`,
`runs`). No credentials, no network, no emulator.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from swarm_api import cirerun, issueci, issueruns

from .test_issue_run_ci import (  # noqa: F401 - the fixtures
    PR,
    SHA_A,
    SHA_B,
    SHA_C,
    SHA_D,
    _read,
    _red_rounds,
    _round_ends,
    _rounds,
    _stored,
    _to_checking,
    api_context,
    clock,
    forge_tokens,
    writes,
)

SHARD = "swarm-ui component tests (2/4)"
WORKFLOW_RUN = 4242
RERUN_POST = "/rerun-failed-jobs"


def _settled(clock) -> None:
    """Past the re-run's settle window (`_read` adds its own minute)."""
    clock.at += timedelta(seconds=cirerun.RERUN_SETTLE_SECONDS)


def _reruns(db, run_id: str) -> list[dict]:
    return list(_stored(db, run_id)["pull_request"].get("reruns") or [])


def _shard_red(writes, sha: str, run_id: int = WORKFLOW_RUN, **kwargs) -> int:
    writes.check(sha, "unit", "success")
    return writes.check(sha, SHARD, "failure", run_id=run_id,
                        output={"summary": f"ADM-5 timed out at {sha[:4]}"}, **kwargs)


def _rerun_posts(writes) -> list[str]:
    return [path for method, path in writes.writes()
            if method == "POST" and (path.endswith(RERUN_POST) or path.endswith("/rerun"))]


@pytest.fixture
def shard_required(writes):
    writes.require("unit", SHARD)
    return writes


def _at_the_cap(client, db, objects, writes, clock) -> dict:
    """Three rounds spent on "unit" (re-runs refused, the default), then
    round three pushed SHA_D where only the swarm-ui shard is red."""
    running = _to_checking(client, db, objects, writes, clock, fix_rounds=3)
    run = _red_rounds(client, db, writes, clock, running["id"], [SHA_A, SHA_B, SHA_C])
    assert run["state"] == "FIXING" and run["ci_fix_round"] == 3
    _round_ends(db, _rounds(db, running["id"])[-1])
    writes.open_pull(PR, SHA_D)
    _shard_red(writes, SHA_D)
    writes.rerun_answer = 201
    return running


# --------------------------------------------------------------------------
# 1. the issue's own case
# --------------------------------------------------------------------------

def test_a_red_shard_at_the_cap_is_rerun_once_and_the_run_stays_checking(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    running = _at_the_cap(client, db, objects, writes, clock)
    stored = _stored(db, running["id"])
    assert stored["ci_fix_round"] == stored["fix_rounds"] == 3
    assert stored["ci_round_sha"] != SHA_D

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert writes.run_reruns == [WORKFLOW_RUN] and writes.reruns == []
    assert len(_rounds(db, running["id"])) == 3
    (entry,) = [r for r in _reruns(db, running["id"]) if r["head"] == SHA_D]
    assert entry["runs"] == [WORKFLOW_RUN] and len(entry["jobs"]) == 1
    assert entry["requested_at"] is not None and "error" not in entry


def test_a_first_red_reading_reruns_instead_of_starting_a_round(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING" and run["ci_fix_round"] == 0
    assert _rounds(db, running["id"]) == []
    assert writes.run_reruns == [WORKFLOW_RUN]
    assert not _stored(db, running["id"]).get("failure_excerpt")


def test_a_settling_or_running_rerun_waits_and_is_not_asked_again(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)
    _read(client, clock, running["id"])

    # Inside the window, the same failed check run still the latest.
    inside = _read(client, clock, running["id"])
    assert inside["state"] == "CHECKING" and _rounds(db, running["id"]) == []

    # Past the window, while the re-run's workflow run is still running.
    writes.runs[WORKFLOW_RUN] = {"id": WORKFLOW_RUN, "status": "in_progress"}
    _settled(clock)
    running_still = _read(client, clock, running["id"])
    assert running_still["state"] == "CHECKING" and _rounds(db, running["id"]) == []

    assert writes.run_reruns == [WORKFLOW_RUN]
    assert len(_reruns(db, running["id"])) == 1


def test_a_rerun_that_answers_green_is_done_without_a_round(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    shard = _shard_red(writes, SHA_A)
    _read(client, clock, running["id"])

    # GitHub made the re-run's check run, and it passed.
    writes.check_runs[SHA_A] = []
    writes.check(SHA_A, "unit", "success")
    writes.check(SHA_A, SHARD, "success", check_id=shard + 100, run_id=WORKFLOW_RUN)
    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE" and run["green_sha"] == SHA_A
    assert run["ci_fix_round"] == 0 and _rounds(db, running["id"]) == []
    assert writes.run_reruns == [WORKFLOW_RUN]


def test_a_rerun_that_answers_red_spends_the_round_or_fails_at_the_cap(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    # Below the cap: the round is spent once the re-run answered red.
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)
    _read(client, clock, running["id"])
    writes.runs[WORKFLOW_RUN] = {"id": WORKFLOW_RUN, "status": "completed"}
    _settled(clock)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FIXING" and run["ci_fix_round"] == 1
    assert len(_rounds(db, running["id"])) == 1
    assert "ADM-5 timed out" in run["failure_excerpt"]
    assert writes.run_reruns == [WORKFLOW_RUN]


def test_a_rerun_that_answers_red_at_the_cap_fails_with_the_excerpt(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    running = _at_the_cap(client, db, objects, writes, clock)
    assert _read(client, clock, running["id"])["state"] == "CHECKING"
    _settled(clock)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "ADM-5 timed out at dddd" in run["failure_excerpt"]
    assert "3 fix rounds" in run["error"] and SHARD in run["error"]
    assert writes.run_reruns == [WORKFLOW_RUN] and len(_rounds(db, running["id"])) == 3


def test_a_new_head_gets_its_own_rerun(client, db, objects, shard_required, clock):
    writes = shard_required
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)
    _read(client, clock, running["id"])
    _settled(clock)
    assert _read(client, clock, running["id"])["state"] == "FIXING"

    _round_ends(db, _rounds(db, running["id"])[0])
    writes.open_pull(PR, SHA_B)
    _shard_red(writes, SHA_B, run_id=WORKFLOW_RUN + 1)
    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING" and run["ci_fix_round"] == 1
    assert writes.run_reruns == [WORKFLOW_RUN, WORKFLOW_RUN + 1]
    assert [r["head"] for r in _reruns(db, running["id"])] == [SHA_A, SHA_B]


@pytest.mark.parametrize("failing", ["commit status", "another app"])
def test_a_non_actions_failure_is_not_rerun(client, db, objects, writes, clock, failing):
    writes.rerun_answer = 201
    if failing == "commit status":
        writes.require("unit", app_id=None)
        running = _to_checking(client, db, objects, writes, clock)
        writes.statuses[SHA_A] = [{"context": "unit", "state": "failure"}]
    else:
        writes.require("unit", app_id=None)
        running = _to_checking(client, db, objects, writes, clock)
        writes.check(SHA_A, "unit", "failure", app_id=999, app_slug="buildkite", run_id=77)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FIXING" and run["ci_fix_round"] == 1
    assert _rerun_posts(writes) == [] and _reruns(db, running["id"]) == []


def test_a_non_actions_failure_at_the_cap_fails_on_that_read(
    client, db, objects, writes, clock,
):
    writes.rerun_answer = 201
    writes.require("unit", app_id=None)
    running = _to_checking(client, db, objects, writes, clock, fix_rounds=1)
    writes.statuses[SHA_A] = [{"context": "unit", "state": "failure"}]
    _read(client, clock, running["id"])
    _round_ends(db, _rounds(db, running["id"])[0])
    writes.open_pull(PR, SHA_B)
    writes.statuses[SHA_B] = [{"context": "unit", "state": "failure"}]

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and "1 fix round" in run["error"]
    assert _rerun_posts(writes) == []


def test_a_refused_rerun_costs_nothing(client, db, objects, shard_required, clock):
    writes = shard_required
    assert writes.rerun_answer == 403
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FIXING" and run["ci_fix_round"] == 1
    (entry,) = _reruns(db, running["id"])
    assert entry["head"] == SHA_A and entry["runs"] == [WORKFLOW_RUN]
    assert entry["error"] and "requested_at" not in entry
    assert len(_rerun_posts(writes)) == 1


def test_a_refused_rerun_at_the_cap_fails_on_that_read(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    running = _at_the_cap(client, db, objects, writes, clock)
    writes.rerun_answer = 403

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and "3 fix rounds" in run["error"]
    assert [r["head"] for r in _reruns(db, running["id"]) if r.get("error")][-1] == SHA_D


def test_a_refused_rerun_is_not_asked_past_its_bound_at_one_head(
    client, db, objects, shard_required, clock, api_context, monkeypatch,
):
    """A read that cannot start its round (the membership lookup failed)
    stays CHECKING at the same head; its refused re-run is asked again at
    most RERUN_REFUSALS_MAX times there."""
    from swarm_api.errors import UpstreamUnavailable

    writes = shard_required
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)

    def unresolved(email, tenant, **_):
        raise UpstreamUnavailable("group membership could not be resolved; retry shortly")

    monkeypatch.setattr(api_context.authenticator, "is_tenant_member", unresolved)
    for _ in range(cirerun.RERUN_REFUSALS_MAX + 2):
        assert _read(client, clock, running["id"])["state"] == "CHECKING"

    assert len(_rerun_posts(writes)) == cirerun.RERUN_REFUSALS_MAX
    assert len(_reruns(db, running["id"])) == cirerun.RERUN_REFUSALS_MAX
    monkeypatch.undo()
    assert _read(client, clock, running["id"])["state"] == "FIXING"
    assert len(_rerun_posts(writes)) == cirerun.RERUN_REFUSALS_MAX


def test_a_round_that_pushed_nothing_still_fails_after_its_rerun(
    client, db, objects, shard_required, clock,
):
    writes = shard_required
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    _shard_red(writes, SHA_A)
    _read(client, clock, running["id"])
    _settled(clock)
    assert _read(client, clock, running["id"])["state"] == "FIXING"
    _round_ends(db, _rounds(db, running["id"])[0])

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and "pushed nothing" in run["error"]
    assert writes.run_reruns == [WORKFLOW_RUN]


def test_concurrent_reads_ask_github_once(
    client, db, objects, shard_required, clock, api_context, monkeypatch,
):
    """A second read of the same head, made while the first is asking GitHub
    (from the run as it stood before either), finds the first's claim in
    its own transaction and asks nothing."""
    writes = shard_required
    writes.rerun_answer = 201
    running = _to_checking(client, db, objects, writes, clock)
    writes.check(SHA_A, "unit", None, status="queued")
    assert _read(client, clock, running["id"])["state"] == "CHECKING"
    writes.check_runs[SHA_A] = []
    _shard_red(writes, SHA_A)
    stale = issueruns.IssueRun.from_firestore(dict(_stored(db, running["id"])))
    assert not (stale.pull_request or {}).get("reruns")

    writer = api_context.forge_writer
    asked = writer.rerun_failed_jobs
    inner: list[issueruns.IssueRun] = []

    def racing(ref, run_id, token):
        if not inner:
            inner.append(issueci.from_checks(
                api_context, stale.tenant_id, stale, owner_auth=lambda ctx, run: None))
        return asked(ref, run_id, token)

    monkeypatch.setattr(writer, "rerun_failed_jobs", racing)
    run = _read(client, clock, running["id"])

    assert inner and inner[0].state == issueruns.RunState.CHECKING
    assert run["state"] == "CHECKING" and _rounds(db, running["id"]) == []
    assert writes.run_reruns == [WORKFLOW_RUN]
    (entry,) = _reruns(db, running["id"])
    assert entry["requested_at"] is not None
