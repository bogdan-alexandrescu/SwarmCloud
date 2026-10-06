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

from datetime import datetime, timedelta, timezone
from typing import Any

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


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    gh = forge_fakes.GitHubWrites()
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
        forge_writer=forgewrite.GitHubWriter(send=github),
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
                              "failed": 0, "truncated": False}
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
