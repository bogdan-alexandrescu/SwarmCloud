"""The merge step's wake tick: POST /v1/admin/merges/wake?tenant_id=<t> (lane MS2).

docs/merge-step.md "Revised 2026-10-06" §1: a merge step that finds its pull
request's checks still running parks CI_PENDING, holding nothing. The
platform has no webhook receiver, so the signal that CI has settled is a
cheap re-read on a per-tenant Cloud Scheduler tick (`merge_wake`,
terraform/modules/scheduler/jobs.tf) as the rollup sweeper. For each of the
tenant's CI_PENDING parks the route reads the pull request and its checks at
the recorded head with THAT tenant's `-git` token and, when nothing it waits
on is still pending, writes `metadata.merge_wait.wake_requested_at`. The
scheduler promotes on that marker (test_scheduler_ci_wait.py); the worker
re-reads every fact on wake and trusts nothing the tick saw.

What these tests hold:

  1. The marker is written only when the reading has settled: green, red,
     the head moved, the pull request closed or merged. Still pending, it is
     not, and the task is never moved by the route -- READY is the
     scheduler's write.
  2. A task is read at most once per `issueci.CI_READ_SECONDS`.
  3. Only the named tenant's parks are read, with the named tenant's token;
     another tenant's park is neither read nor marked (invariant 9).
  4. A read that fails is not a reading: nothing is marked, and the failure
     is reported.
  5. The route is the rollup sweeper's (test_rollup_sweeper_is_narrow.py
     holds the allow-list).

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, issueci, mergewake
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.states import ParkReason

from . import forge_fakes
from .conftest import api_settings, seed_task, seed_tenant

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
MOVED = "b" * 40
NUMBER = 41
CHECK = "ci / unit"
REPO_URL = "https://github.com/saga-xyz/widgets"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


class QueueGitHub(forge_fakes.GitHubWrites):
    """`GitHubWrites`, plus the one GraphQL read a `merge_queued` park makes
    (lane C3H): whether the pull request is still in its base's merge queue,
    and the last reason GitHub gave for removing it. Every REST route is the
    parent's, unchanged."""

    def __init__(self) -> None:
        super().__init__()
        self.in_queue = True
        self.removed_reason: str | None = None
        self.graphql_errors: list[dict[str, str]] = []

    def __call__(self, method, url, headers, body, timeout):
        if urlparse(url).path != "/graphql":
            return super().__call__(method, url, headers, body, timeout)
        self.calls.append((method, url, dict(headers), body))
        if self.graphql_errors:
            return 200, json.dumps({"data": None, "errors": self.graphql_errors}).encode()
        removals = ([{"reason": self.removed_reason, "createdAt": "2026-10-06T12:00:00Z"}]
                    if self.removed_reason else [])
        return 200, json.dumps({"data": {"repository": {"pullRequest": {
            "isInMergeQueue": self.in_queue,
            "mergeQueueEntry": {"id": "MQE_1", "position": 1, "state": "QUEUED"}
            if self.in_queue else None,
            "timelineItems": {"nodes": removals}}}}}).encode()

    def graphql_calls(self) -> list[tuple[str, str, dict[str, str], bytes | None]]:
        return [c for c in self.calls if urlparse(c[1]).path == "/graphql"]


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    gh = QueueGitHub()
    gh.open_pull(NUMBER, HEAD, ref="swarm/task_int")
    gh.require(CHECK)
    return gh


@pytest.fixture
def tenant_tokens() -> forge_fakes.AnyTenantTokens:
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def wake_client(db, tokens, group_map, objects, github, tenant_tokens, clock) -> TestClient:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=tenant_tokens,
        forge_writer=forgewrite.GitHubWriter(send=github, locate=github.locate, fetch=github.fetch),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def parked_merge(db, task_id: str = "task_merge", *, tenant_id: str = "eng",
                 head: str | None = HEAD, **wait: Any) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="PARKED",
                    runner_profile="merge", park_reason=ParkReason.CI_PENDING.value,
                    next_eligible_at=NOW + timedelta(minutes=15))
    record: dict[str, Any] = {"pull_request": NUMBER, "code": "checks_pending",
                              "pending": [CHECK], "wakes": 1, "updates": 0,
                              "first_parked_at": NOW - timedelta(minutes=5),
                              "parked_at": NOW - timedelta(minutes=5), **wait}
    if head is not None:
        record["head"] = head
    doc["repository_url"] = REPO_URL
    doc["metadata"] = {"dispatch": {"merge_target": {"pull_request": "task_int"}},
                       mergewake.MERGE_WAIT_METADATA_KEY: record}
    return doc


def _tick(client: TestClient, tenant_id: str = "eng"):
    return client.post(f"/v1/admin/merges/wake?tenant_id={tenant_id}", headers=SWEEPER_HEADERS)


def _wait(db, task_id: str = "task_merge") -> dict:
    return db.docs[f"tasks/{task_id}"]["metadata"][mergewake.MERGE_WAIT_METADATA_KEY]


def _reads(github: forge_fakes.GitHubWrites) -> list[str]:
    return [url for method, url, _, _ in github.calls if method == "GET"]


# --------------------------------------------------------------------------
# 1. the marker, only when settled
# --------------------------------------------------------------------------

def test_green_at_the_recorded_head_writes_the_marker_and_nothing_else(db, wake_client, github):
    parked_merge(db)
    github.check(HEAD, CHECK, "success")
    before = {k: v for k, v in db.docs["tasks/task_merge"].items() if k != "metadata"}

    response = _tick(wake_client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng"
    assert body["report"]["woken"] == 1 and body["report"]["visited"] == 1
    assert body["failures"] == []
    assert _wait(db)[mergewake.WAKE_MARKER] == NOW
    # The route never moves the task: READY is the scheduler's write, and
    # only admission takes capacity (invariant 1).
    after = {k: v for k, v in db.docs["tasks/task_merge"].items() if k != "metadata"}
    assert after == before
    assert after["state"] == "PARKED" and after["park_reason"] == ParkReason.CI_PENDING.value


def test_still_pending_writes_no_marker(db, wake_client, github):
    parked_merge(db)
    github.check(HEAD, CHECK, None, status="in_progress")

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 0 and body["report"]["waiting"] == 1
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert _wait(db)["checked_at"] == NOW


def test_a_required_check_nobody_reported_yet_is_still_pending(db, wake_client, github):
    parked_merge(db)
    _tick(wake_client)
    assert mergewake.WAKE_MARKER not in _wait(db)


@pytest.mark.parametrize("case", ["red", "head_moved", "closed", "merged"])
def test_every_settled_reading_wakes_the_step(db, wake_client, github, case):
    """Red too: the worker refuses `checks_failed` itself, with every name."""
    parked_merge(db)
    github.check(HEAD, CHECK, None, status="in_progress")
    if case == "red":
        github.check_runs[HEAD] = []
        github.check(HEAD, CHECK, "failure")
    elif case == "head_moved":
        github.pulls[NUMBER]["head"]["sha"] = MOVED
    elif case == "closed":
        github.pulls[NUMBER]["state"] = "closed"
    else:
        github.pulls[NUMBER].update(state="closed", merged=True)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)[mergewake.WAKE_MARKER] == NOW
    assert _wait(db)["wake_reason"] == case


def test_a_marked_park_is_not_read_again(db, wake_client, github):
    parked_merge(db, wake_requested_at=NOW - timedelta(minutes=1))
    body = _tick(wake_client).json()
    assert body["report"]["visited"] == 0
    assert _reads(github) == []


# --------------------------------------------------------------------------
# 2. at most once per CI_READ_SECONDS
# --------------------------------------------------------------------------

def test_a_task_is_read_at_most_once_per_ci_read_seconds(db, wake_client, github, clock):
    parked_merge(db)
    github.check(HEAD, CHECK, None, status="in_progress")

    _tick(wake_client)
    reads = len(_reads(github))
    assert reads > 0

    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS - 1)
    body = _tick(wake_client).json()
    assert body["report"]["skipped"] == 1
    assert len(_reads(github)) == reads, "read again inside CI_READ_SECONDS"

    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)
    _tick(wake_client)
    assert len(_reads(github)) > reads, "never read again after CI_READ_SECONDS"


def test_a_checked_at_in_the_future_does_not_stop_the_reads(db, wake_client, github):
    """The task document is tenant-writable: a forged instant only delays to
    the next read, never past it."""
    parked_merge(db, checked_at=NOW + timedelta(days=30))
    github.check(HEAD, CHECK, "success")
    assert _tick(wake_client).json()["report"]["woken"] == 1


# --------------------------------------------------------------------------
# 3. the named tenant's parks only, with the named tenant's token
# --------------------------------------------------------------------------

def test_only_the_named_tenants_parks_are_read_with_its_own_token(
    db, wake_client, github, tenant_tokens
):
    parked_merge(db, "task_eng")
    parked_merge(db, "task_research", tenant_id="research")
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client, "eng").json()

    assert body["report"]["visited"] == 1 and body["report"]["woken"] == 1
    assert mergewake.WAKE_MARKER in _wait(db, "task_eng")
    assert mergewake.WAKE_MARKER not in _wait(db, "task_research")
    assert tenant_tokens.asked == ["swarm-tenant-eng-git"]
    eng_token = tenant_tokens.issued["swarm-tenant-eng-git"]
    for _, url, headers, _ in github.calls:
        assert eng_token in headers["Authorization"], url
        assert eng_token not in url
    # And the token is in no response.
    assert eng_token not in str(body)


def test_other_states_and_park_reasons_are_not_visited(db, wake_client, github):
    parked_merge(db)
    db.docs["tasks/task_merge"]["park_reason"] = ParkReason.SCHEDULED_RETRY.value
    other = parked_merge(db, "task_ready")
    other["state"] = "READY"
    other["park_reason"] = None
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["visited"] == 0
    assert _reads(github) == []


def test_a_tenant_with_no_ci_wait_reads_no_token(db, wake_client, tenant_tokens):
    body = _tick(wake_client).json()
    assert body["report"] == {"visited": 0, "woken": 0, "waiting": 0, "skipped": 0,
                              "failed": 0, "fixing": 0, "truncated": False}
    assert tenant_tokens.asked == []


def test_an_unknown_tenant_is_not_found(wake_client):
    assert _tick(wake_client, "nobody").status_code == 404


# --------------------------------------------------------------------------
# 4. a failed read is not a reading
# --------------------------------------------------------------------------

def test_a_read_github_refuses_marks_nothing_and_is_reported(db, wake_client, github):
    parked_merge(db)
    github.status["GET"] = 403

    body = _tick(wake_client).json()

    assert body["report"]["failed"] == 1 and body["report"]["woken"] == 0
    assert body["failures"][0]["task_id"] == "task_merge"
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_a_park_with_no_recorded_head_is_left_to_the_fallback(db, wake_client, github):
    parked_merge(db, head=None)
    body = _tick(wake_client).json()
    assert body["report"]["failed"] == 1 and body["failures"][0]["code"] == "wait_unrecorded"
    assert _reads(github) == []
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_a_repository_off_github_is_never_sent_the_token(db, wake_client, github, tenant_tokens):
    doc = parked_merge(db)
    doc["repository_url"] = "https://gitlab.com/saga-xyz/widgets"
    body = _tick(wake_client).json()
    assert body["failures"][0]["code"] == "wait_unrecorded"
    assert github.calls == []


# --------------------------------------------------------------------------
# 5. the route
# --------------------------------------------------------------------------

def test_an_ordinary_member_may_not_tick(db, wake_client):
    response = wake_client.post(
        "/v1/admin/merges/wake?tenant_id=eng", headers={"Authorization": "Bearer token-alice"}
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# 6. the CI-fix hand-off for a workflow that is not an issue run (lane MS7)
# --------------------------------------------------------------------------
#
# docs/merge-step.md "Revised 2026-10-06" §6 MS7. A workflow asks for up to
# `metadata.merge_fix_rounds` CI-fix rounds. When the tick reads a red
# required check on a CI_PENDING merge with rounds left, it claims the round
# in the merge task's `metadata.merge_fix` in a guarded transaction, then
# submits the continuation the issue-run loop builds
# (`issueci.ci_fix_continuation`) as the merge task's submitter, while they
# are still a member. The merge stays parked: no marker. Red with no round
# left wakes the step, which refuses `checks_failed`.

ROOT = "task_" + "1" * 20
ROUND_TASK_STATES = ("PARKED", "READY", "RUNNING")


def fixable_merge(db, github, *, rounds: int = 1, submitter: str = "alice@saga.xyz",
                  **wait: Any) -> dict:
    """A CI_PENDING merge of ROOT's pull request, red at HEAD, asking for `rounds`."""
    root = seed_task(db, task_id=ROOT, tenant_id="eng", state="SUCCEEDED",
                     runner_profile="claude-code")
    root["repository_url"] = REPO_URL
    root["submitted_by"] = submitter
    root["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    root["result_summary"] = {"git": {"pushed_head": HEAD, "pull_request": {"number": NUMBER}}}
    doc = parked_merge(db, **wait)
    doc["submitted_by"] = submitter
    doc["metadata"]["dispatch"]["merge_target"] = {"pull_request": ROOT}
    doc["metadata"]["merge_fix_rounds"] = rounds
    github.open_pull(NUMBER, HEAD, ref=f"swarm/{ROOT}")
    github.check(HEAD, CHECK, "failure", output={"title": "2 failed",
                                                 "summary": "test_price_sort failed"})
    return doc


def _fix_workflows(db) -> list[dict]:
    """Every workflow holding a CI-fix step, whose task continues ROOT."""
    found = []
    for key, workflow in db.docs.items():
        if not key.startswith("workflows/"):
            continue
        steps = [s for s in workflow.get("steps") or [] if s.get("step_id") == issueci.CI_FIX_STEP]
        for step in steps:
            task = db.docs[f"tasks/{step['task_id']}"]
            assert task["metadata"]["dispatch"]["continues"] == ROOT
        if steps:
            found.append(workflow)
    return found


def _fix_record(db, task_id: str = "task_merge") -> dict:
    return db.docs[f"tasks/{task_id}"]["metadata"].get(mergewake.MERGE_FIX_METADATA_KEY) or {}


def test_red_with_rounds_left_claims_a_round_and_submits_one_continuation(db, wake_client, github):
    fixable_merge(db, github, rounds=2)

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1 and body["report"]["woken"] == 0, body
    assert body["failures"] == []
    # The merge stays parked, and is not woken: the round is what runs now.
    task = db.docs["tasks/task_merge"]
    assert task["state"] == "PARKED" and task["park_reason"] == ParkReason.CI_PENDING.value
    assert mergewake.WAKE_MARKER not in _wait(db)

    (workflow,) = _fix_workflows(db)
    (step,) = workflow["steps"]
    assert step["step_id"] == issueci.CI_FIX_STEP
    fix_task = db.docs[f"tasks/{step['task_id']}"]
    assert fix_task["submitted_by"] == "alice@saga.xyz"
    assert fix_task["runner_profile"] == "claude-code"
    assert fix_task["metadata"]["merge"] == "off", "a fix round never carries its own merge"
    assert fix_task["metadata"]["dispatch"]["continues"] == ROOT
    assert fix_task["metadata"]["dispatch"]["strategy"] == "direct-pr"
    assert fix_task["metadata"]["merge_fix_round"] == {
        "merge_task": "task_merge", "round": 1, "head_sha": HEAD, "pull_request": NUMBER}
    prompt = fix_task["input"]["prompt"]
    assert f"pull request #{NUMBER}" in prompt and "round 1 of at most 2" in prompt
    assert "test_price_sort failed" in prompt and CHECK in prompt

    (claimed,) = _fix_record(db)["rounds"]
    assert claimed == {"round": 1, "head": HEAD, "claimed_at": NOW,
                       "workflow_id": workflow["workflow_id"], "task_id": step["task_id"]}


def test_a_round_still_running_is_neither_claimed_again_nor_woken(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=3)
    _tick(wake_client)
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0, body
    assert body["report"]["waiting"] == 1
    assert len(_fix_workflows(db)) == 1
    assert len(_fix_record(db)["rounds"]) == 1
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_two_racing_ticks_claim_once_and_submit_once(db, wake_client, github, monkeypatch):
    """Both ticks read red with a round left; the inner one claims first, and
    the outer one's guarded claim is refused, so it submits nothing."""
    fixable_merge(db, github, rounds=2)
    claim = mergewake._claim_round
    raced = {"done": False}

    def racing(*args, **kwargs):
        if not raced["done"]:
            raced["done"] = True
            inner = _tick(wake_client).json()
            assert inner["report"]["fixing"] == 1, inner
        return claim(*args, **kwargs)

    monkeypatch.setattr(mergewake, "_claim_round", racing)

    outer = _tick(wake_client).json()

    assert raced["done"]
    assert outer["report"]["fixing"] == 0, outer
    assert len(_fix_workflows(db)) == 1
    assert len(_fix_record(db)["rounds"]) == 1


def test_the_claim_guard_refuses_a_stale_reading(db, github):
    """The claim itself, directly: a reading made before another tick's
    claim (one round seen where there are now two) writes nothing."""
    fixable_merge(db, github, rounds=3)
    assert mergewake._claim_round(db, "eng", "task_merge", HEAD, seen=0, now=NOW) == 1
    assert mergewake._claim_round(db, "eng", "task_merge", HEAD, seen=0, now=NOW) is None
    assert len(_fix_record(db)["rounds"]) == 1


def test_red_with_no_round_left_wakes_the_step_to_checks_failed(db, wake_client, github):
    fixable_merge(db, github, rounds=1)
    db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = {
        "rounds": [{"round": 1, "head": MOVED, "task_id": "task_" + "2" * 20}]}

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1 and body["report"]["fixing"] == 0
    assert _wait(db)["wake_reason"] == "red"
    assert _fix_workflows(db) == []


@pytest.mark.parametrize("rounds", [0, None])
def test_red_on_a_workflow_that_asked_for_no_rounds_wakes_the_step(db, wake_client, github, rounds):
    doc = fixable_merge(db, github)
    if rounds is None:
        doc["metadata"].pop("merge_fix_rounds")
    else:
        doc["metadata"]["merge_fix_rounds"] = rounds
    _tick(wake_client)
    assert _wait(db)["wake_reason"] == "red"
    assert _fix_workflows(db) == []


def test_a_round_that_ended_at_the_red_head_wakes_the_step(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=2)
    _tick(wake_client)
    (claimed,) = _fix_record(db)["rounds"]
    db.docs[f"tasks/{claimed['task_id']}"]["state"] = "SUCCEEDED"
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "fix_round_ended"
    assert len(_fix_workflows(db)) == 1, "a second round at the head the first could not fix"


def test_a_round_claimed_and_never_recorded_wakes_the_step_once_lost(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=2)
    db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = {
        "rounds": [{"round": 1, "head": HEAD, "claimed_at": NOW}]}
    assert _tick(wake_client).json()["report"]["waiting"] == 1
    clock.now = NOW + timedelta(seconds=issueci.LOST_ROUND_SECONDS)
    _tick(wake_client)
    assert _wait(db)["wake_reason"] == "fix_round_lost"
    assert _fix_workflows(db) == []


def test_a_submitter_who_left_the_tenant_gets_no_round(db, wake_client, github):
    fixable_merge(db, github, rounds=2, submitter="carol@saga.xyz")

    body = _tick(wake_client).json()

    assert _fix_workflows(db) == []
    assert body["report"]["woken"] == 1
    assert _wait(db)["wake_reason"] == "fix_round_refused"
    (claimed,) = _fix_record(db)["rounds"]
    assert claimed["head"] == HEAD and "no longer a member" in claimed["error"]
    assert "task_id" not in claimed


def test_a_pull_request_not_on_the_roots_branch_gets_no_round(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    github.pulls[NUMBER]["head"]["ref"] = "swarm/task_" + "9" * 20

    _tick(wake_client)

    assert _wait(db)["wake_reason"] == "red"
    assert _fix_workflows(db) == []


def test_the_head_a_round_pushed_wakes_the_step_naming_the_round(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=2)
    _tick(wake_client)
    (claimed,) = _fix_record(db)["rounds"]
    fix_task = db.docs[f"tasks/{claimed['task_id']}"]
    fix_task["state"] = "SUCCEEDED"
    fix_task["result_summary"] = {"git": {"pushed_head": MOVED}}
    github.pulls[NUMBER]["head"]["sha"] = MOVED
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1
    assert _wait(db)["wake_reason"] == "head_moved"
    assert _wait(db)["head_pushed_by"] == claimed["task_id"]


def test_a_forged_round_budget_is_capped(db, wake_client, github):
    fixable_merge(db, github, rounds=99)
    db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = {"rounds": [
        {"round": n, "head": f"{n:040x}"} for n in range(1, mergewake.MERGE_FIX_ROUNDS_MAX + 1)]}
    _tick(wake_client)
    assert _wait(db)["wake_reason"] == "red"
    assert _fix_workflows(db) == []


# --------------------------------------------------------------------------
# 7. update-branch is asynchronous (MS3 review finding, 2026-10-06)
# --------------------------------------------------------------------------

def test_a_park_on_a_pending_update_waits_for_the_head_to_move(db, wake_client, github, clock):
    """The worker asked GitHub to update the branch and GitHub has not moved
    the head yet. The old head's checks are green -- that is why it was only
    behind -- so reading them would wake the step for nothing. The tick
    waits for the head to move, and wakes the step then."""
    parked_merge(db, code=mergewake.BRANCH_UPDATE_PENDING, pending=[])
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1 and body["report"]["woken"] == 0, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert not [u for u in _reads(github) if "/check-runs" in u or "/status" in u]

    github.pulls[NUMBER]["head"]["sha"] = MOVED
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)
    _tick(wake_client)
    assert _wait(db)["wake_reason"] == "head_moved"


def test_a_park_at_the_updated_head_reads_its_checks_there(db, wake_client, github):
    """The worker saw the update land and parked at the new head: the tick
    reads the checks at that head, and they are still running there."""
    parked_merge(db, head=MOVED, code="branch_updated", pending=[])
    github.pulls[NUMBER]["head"]["sha"] = MOVED
    github.check(HEAD, CHECK, "success")
    github.check(MOVED, CHECK, None, status="queued")

    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1, body
    assert any(f"/commits/{MOVED}/check-runs" in u for u in _reads(github))
    assert not any(f"/commits/{HEAD}/" in u for u in _reads(github))


# --------------------------------------------------------------------------
# A park in the base's merge queue (lane C3H)
# --------------------------------------------------------------------------
# The worker found the base merges only through a merge queue, enqueued the
# pull request and parked `merge_queued`. Its checks were green when it did,
# so reading them would wake it for nothing; what it waits on is the queue.
# The tick reads the pull request, and, while it is open at the recorded head,
# whether it is still in the queue: merged wakes it (the worker succeeds),
# out of the queue wakes it (the worker refuses with GitHub's reason).

def test_a_pull_request_still_in_the_queue_is_a_wait_and_its_checks_are_not_read(
    db, wake_client, github, tenant_tokens
):
    parked_merge(db, code=mergewake.MERGE_QUEUED, pending=[])
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1 and body["report"]["woken"] == 0, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert _wait(db)["checked_at"] == NOW
    assert not [u for u in _reads(github) if "/check-runs" in u or "/status" in u]
    (call,) = github.graphql_calls()
    method, url, headers, payload = call
    assert method == "POST" and url == "https://api.github.com/graphql"
    variables = json.loads(payload)["variables"]
    assert variables == {"owner": "saga-xyz", "name": "widgets", "number": NUMBER}
    (token,) = tenant_tokens.issued.values()
    assert [k for k, v in headers.items() if token in str(v)] == ["Authorization"]
    assert token not in payload.decode()


def test_a_pull_request_removed_from_the_queue_wakes_the_step(db, wake_client, github):
    """MUTATION: treat out-of-the-queue as a wait, and the step sleeps until
    its fallback every 15 minutes, for MERGE_CI_MAX_SECONDS."""
    parked_merge(db, code=mergewake.MERGE_QUEUED, pending=[])
    github.in_queue = False
    github.removed_reason = "The merge group failed a required status check"

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)[mergewake.WAKE_MARKER] == NOW
    assert _wait(db)["wake_reason"] == "dequeued"


@pytest.mark.parametrize("case", ["merged", "closed", "head_moved"])
def test_a_queued_pull_request_merged_closed_or_moved_wakes_without_a_queue_read(
    db, wake_client, github, case
):
    parked_merge(db, code=mergewake.MERGE_QUEUED, pending=[])
    if case == "merged":
        github.pulls[NUMBER].update(state="closed", merged=True)
    elif case == "closed":
        github.pulls[NUMBER]["state"] = "closed"
    else:
        github.pulls[NUMBER]["head"]["sha"] = MOVED

    _tick(wake_client)

    assert _wait(db)["wake_reason"] == case
    assert github.graphql_calls() == []


def test_a_queue_read_github_refuses_is_not_a_reading(db, wake_client, github):
    parked_merge(db, code=mergewake.MERGE_QUEUED, pending=[])
    github.graphql_errors = [{"message": "Resource not accessible by personal access token"}]

    body = _tick(wake_client).json()

    assert body["report"]["failed"] == 1 and body["report"]["woken"] == 0, body
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_a_park_that_is_not_in_a_queue_never_reads_one(db, wake_client, github):
    """The control: every other park is read exactly as before."""
    parked_merge(db)
    github.check(HEAD, CHECK, "success")
    _tick(wake_client)
    assert _wait(db)["wake_reason"] == "green"
    assert github.graphql_calls() == []


def test_the_queued_park_code_is_the_workers():
    from agent_worker import merge as worker_merge

    assert mergewake.MERGE_QUEUED == worker_merge.MERGE_QUEUED == "merge_queued"



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


def test_a_park_waiting_for_the_merge_slot_is_not_read(db, wake_client, github):
    """Merge race (#295): a step parked for its repository's merge slot is
    green -- that is why it asked for the slot -- so reading it would wake it
    for nothing every tick. The holder's release marks it; the tick skips it.
    The control is the first test here: the same park as `checks_pending` is
    read and marked."""
    parked_merge(db, code=mergewake.MERGE_SLOT_WAIT, pending=[])
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["skipped"] == 1 and body["report"]["woken"] == 0, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert _reads(github) == []
