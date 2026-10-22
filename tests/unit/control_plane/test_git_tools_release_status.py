"""`release_status`, seeing a change deploy (docs/design/agent-autonomy.md §3.7; lane AU-1, part of #1933).

The answer is read from GitHub alone: the newest `release.yml` run on the
default branch whose head contains the sha (`compare` says `ahead` or
`identical`), its jobs, whether it is waiting at an environment
(`pending_deployments`), and the `deploy` job's conclusion, which is the
deployed-digest check. swarm-api's own `GIT_SHA` is reported beside it, with
`/v1/version`'s caution. No Cloud Run read, no IAM row.

What each case pins, from §6.1's row AU-1: `release_status` reports a run
waiting at `dev-iam` as a state the agent can read and report, never a
timeout. And: a sha no release contains is `not_released`; a run whose deploy
job succeeded is `deployed`; a failed one is `failed` naming the job.
"""

from __future__ import annotations

from .test_git_tools_api import (  # noqa: F401  (fixtures)
    app,
    call,
    client,
    origin,
    running_task,
    slots,
    worker_tokens,
)
from .test_git_tools_reads import forge  # noqa: F401  (a fixture)

SHA = "1" * 40
LATER = "2" * 40
LATEST = "3" * 40


def release_run(forge, run_id: int, head: str, *, status: str = "completed",
                conclusion: str | None = "success") -> None:
    forge.run(run_id, workflow="release.yml", head_sha=head, branch="main", status=status,
              conclusion=conclusion)


def test_release_status_reports_a_run_waiting_at_dev_iam(client, db, forge):
    attempt, _ = running_task(client, db)
    release_run(forge, 900, LATER, status="waiting", conclusion=None)
    forge.compare[(SHA, LATER)] = "ahead"
    forge.job(900, 9001, "verify", "success")
    forge.job(900, 9002, "infrastructure", None, status="waiting")
    forge.pending[900] = [{"environment": {"id": 1, "name": "dev-iam"},
                           "wait_timer": 0, "current_user_can_approve": False}]
    response = call(client, attempt, "release_status", {"sha": SHA})
    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: skip `pending_deployments` and the state reads "running".
    assert body["state"] == "waiting"
    assert body["waiting_at"] == ["dev-iam"]
    assert body["run"]["id"] == 900 and body["run"]["head_sha"] == LATER
    assert {j["name"]: j["conclusion"] for j in body["jobs"]} == {
        "verify": "success", "infrastructure": None}
    assert body["deployed"] is False


def test_the_newest_run_that_contains_the_sha_is_the_one_read(client, db, forge):
    attempt, _ = running_task(client, db)
    release_run(forge, 901, LATER, conclusion="failure")
    release_run(forge, 902, LATEST)  # newest
    forge.compare[(SHA, LATER)] = "ahead"
    forge.compare[(SHA, LATEST)] = "ahead"
    forge.job(902, 9021, "deploy", "success")
    response = call(client, attempt, "release_status", {"sha": SHA})
    body = response.json()
    # MUTATION: read the oldest containing run and this is 901, failed.
    assert body["run"]["id"] == 902
    assert body["state"] == "deployed" and body["deployed"] is True


def test_a_run_whose_head_does_not_contain_the_sha_is_skipped(client, db, forge):
    attempt, _ = running_task(client, db)
    release_run(forge, 903, LATER)
    release_run(forge, 904, LATEST)
    forge.compare[(SHA, LATER)] = "ahead"
    forge.compare[(SHA, LATEST)] = "diverged"  # a newer run built from elsewhere
    forge.job(903, 9031, "deploy", "success")
    response = call(client, attempt, "release_status", {"sha": SHA})
    # MUTATION: take the newest run without the compare and this is 904.
    assert response.json()["run"]["id"] == 903


def test_a_sha_no_release_contains_is_not_released(client, db, forge):
    attempt, _ = running_task(client, db)
    release_run(forge, 905, LATER)
    forge.compare[(SHA, LATER)] = "behind"
    response = call(client, attempt, "release_status", {"sha": SHA})
    assert response.status_code == 200, response.text
    # MUTATION: count `behind` as containing and this reads a run.
    assert response.json()["state"] == "not_released"
    assert response.json()["run"] is None


def test_a_failed_release_names_its_failing_job(client, db, forge):
    attempt, _ = running_task(client, db)
    release_run(forge, 906, SHA, conclusion="failure")
    forge.compare[(SHA, SHA)] = "identical"
    forge.job(906, 9061, "verify", "success")
    forge.job(906, 9062, "deploy", "failure")
    response = call(client, attempt, "release_status", {"sha": SHA})
    body = response.json()
    # MUTATION: report every completed run as deployed and this is "deployed".
    assert body["state"] == "failed"
    assert body["failed_jobs"] == ["deploy"]
    assert body["deployed"] is False


def test_a_running_release_is_running(client, db, forge):
    attempt, _ = running_task(client, db)
    release_run(forge, 907, SHA, status="in_progress", conclusion=None)
    forge.compare[(SHA, SHA)] = "identical"
    forge.job(907, 9071, "build", None, status="in_progress")
    response = call(client, attempt, "release_status", {"sha": SHA})
    assert response.json()["state"] == "running"


def test_swarm_apis_own_git_sha_is_reported_beside_it(client, db, forge, monkeypatch):
    monkeypatch.setenv("GIT_SHA", LATEST)
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "release_status", {"sha": SHA})
    body = response.json()
    # MUTATION: drop the version read and this key is missing.
    assert body["api_version"]["git_sha"] == LATEST
    assert "built" in body["api_version"]["note"]


def test_a_sha_that_is_not_forty_hex_is_bad_ref(client, db, forge):
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "release_status", {"sha": "main"})
    assert response.status_code == 422, response.text
    # MUTATION: pass any string to compare and a branch name reaches GitHub's URL.
    assert response.json()["code"] == "bad_ref"
