"""A release wait wakes on a terminal release, and not before (lane AU-7).

docs/design/agent-autonomy.md §3.8. An agent that asked to see its change
deploy is PARKED(DEPENDENCY_INCOMPLETE) with a `release_wait` naming the
commit, holding nothing. The merge-wake tick reads the release of that
commit from GitHub, as the `release_status` git tool does, and returns the
task to READY only when the release is terminal: deployed, failed, held at
an environment past six hours, or the wait's own 24-hour bound. These tests
hold:

  1. not released, running, or waiting at an environment for less than six
     hours is recorded and NOT woken;
  2. deployed and failed wake it, READY with a `release_settled` event and
     nothing leased (invariant 1);
  3. a release held at `dev-iam` for six hours wakes it as
     `held_at_environment`, and the 24-hour bound as `release_wait_expired`;
  4. one read per five minutes; another tenant's wait is never read with
     this tenant's token (invariant 9); a hand-off or a dependency park
     without the record is not visited;
  5. a tenant with no release wait answers the tick exactly as before.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_api import mergewake

from .conftest import api_settings, seed_task, seed_tenant

TASK = "task_rel"
REPO = "https://github.com/example-org/widgets"


class Releases:
    """`GitHubWriter`'s five reads `release_reading` makes, answering one release."""

    def __init__(self, sha: str) -> None:
        self.sha = sha
        self.head = secrets.token_hex(20)
        self.run: dict[str, Any] | None = None
        self.jobs: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.reads: list[tuple[str, str]] = []

    def default_branch(self, ref, token):
        self.reads.append((ref.repository, token))
        return "main"

    def workflow_runs(self, ref, workflow, token, *, branch=None, limit=20):
        assert workflow == "release.yml" and branch == "main"
        return [self.run] if self.run else []

    def compare_status(self, ref, base, head, token):
        return "ahead" if base == self.sha and head == self.head else "diverged"

    def run_jobs(self, ref, run_id, token):
        return self.jobs

    def pending_deployments(self, ref, run_id, token):
        return self.pending

    def released(self, status: str, conclusion: str | None = None) -> None:
        self.run = {"id": 7, "head_sha": self.head, "status": status, "conclusion": conclusion,
                    "repository": {"full_name": "example-org/widgets"}}


class Tokens:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def token_for(self, tenant):
        self.asked.append(tenant.id if hasattr(tenant, "id") else str(tenant))
        return "tok-" + secrets.token_hex(8)


@pytest.fixture
def sha() -> str:
    return secrets.token_hex(20)


@pytest.fixture
def github(sha) -> Releases:
    return Releases(sha)


@pytest.fixture
def tokens() -> Tokens:
    return Tokens()


@pytest.fixture
def now() -> list[datetime]:
    return [datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)]


@pytest.fixture
def ctx(db, api_store, github, tokens, now):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return SimpleNamespace(store=api_store, now=lambda: now[0], forge_writer=github,
                           forge_tokens=tokens, settings=api_settings(), forge_installations=None)


def _park(db, sha: str, *, task_id: str = TASK, tenant_id: str = "eng", at: datetime,
          record: bool = True) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="PARKED",
                    park_reason="DEPENDENCY_INCOMPLETE", next_eligible_at=at)
    doc["repository_url"] = REPO
    doc["attempt_count"] = 1
    entry: dict[str, Any] = {"reason": "DEPENDENCY_INCOMPLETE", "park_phase": "release_wait"}
    if record:
        entry["release_wait"] = {"sha": sha, "attempt_id": "att_1", "asked_at": at.isoformat()}
    doc["blocked_by"] = [entry]
    return doc


def _tick(ctx, tenant_id: str = "eng") -> mergewake.WakeReport:
    return mergewake.wake_tenant(ctx, tenant_id, limit=50)


def _task(db, task_id: str = TASK) -> dict:
    return db.docs[f"tasks/{task_id}"]


def _events(db, task_id: str = TASK) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for path, doc in db.docs.items() if path.startswith(prefix)]


# -- 1. not before ------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["not_released", "in_progress", "waiting"])
def test_a_release_that_has_not_ended_is_recorded_and_not_woken(ctx, db, github, sha, now, status):
    _park(db, sha, at=now[0] - timedelta(hours=1))
    if status != "not_released":
        github.released(status)
    if status == "waiting":
        github.pending = [{"environment": {"name": "dev-iam"}}]

    report = _tick(ctx)

    # MUTATION: wake on any reading and the agent resumes before its change deployed.
    assert _task(db)["state"] == "PARKED"
    assert report.release_visited == 1 and report.release_woken == 0
    recorded = _task(db)["metadata"]["release_wait"]
    assert recorded["state"] == {"not_released": "not_released", "in_progress": "running",
                                 "waiting": "waiting"}[status]
    if status == "waiting":
        assert recorded["waiting_at"] == ["dev-iam"]


# -- 2. a terminal release ---------------------------------------------------------------

@pytest.mark.parametrize(("conclusion", "jobs", "why"), [
    ("success", [{"name": "deploy", "conclusion": "success"}], "deployed"),
    ("failure", [{"name": "deploy", "conclusion": "failure"}], "failed"),
])
def test_a_terminal_release_wakes_the_agent_to_ready(ctx, db, github, sha, now, conclusion, jobs, why):
    _park(db, sha, at=now[0] - timedelta(hours=1))
    github.released("completed", conclusion)
    github.jobs = jobs

    report = _tick(ctx)

    task = _task(db)
    # MUTATION: never write READY and the agent sleeps out its 24 hours.
    assert task["state"] == "READY", task
    assert task["park_reason"] is None and task["blocked_by"] == []
    assert task["current_lease_id"] is None
    assert report.release_woken == 1
    assert task["metadata"]["release_wait"]["wake_reason"] == why
    (ready,) = [e for e in _events(db) if e["type"] == "ready"]
    assert ready["detail"]["reason"] == mergewake.RELEASE_SETTLED
    assert ready["detail"]["release"] == why


# -- 3. held, and the bound ---------------------------------------------------------------

def test_a_release_held_at_an_environment_for_six_hours_wakes_the_agent(ctx, db, github, sha, now):
    _park(db, sha, at=now[0] - timedelta(hours=1))
    github.released("waiting")
    github.pending = [{"environment": {"name": "dev-iam"}}]
    _tick(ctx)
    assert _task(db)["state"] == "PARKED"

    now[0] += timedelta(hours=5, minutes=59)
    _tick(ctx)
    # MUTATION: measure the hold from the park and a release held for minutes ends the wait.
    assert _task(db)["state"] == "PARKED"

    now[0] += timedelta(minutes=6)
    _tick(ctx)
    # MUTATION: wait through any hold and an owner's day off holds the agent for 24 hours.
    assert _task(db)["state"] == "READY"
    assert _task(db)["metadata"]["release_wait"]["wake_reason"] == "held_at_environment"


def test_the_24_hour_bound_wakes_the_agent(ctx, db, github, sha, now):
    _park(db, sha, at=now[0] - timedelta(hours=24, seconds=1))

    _tick(ctx)

    # MUTATION: drop the bound and an unreleased commit holds the agent for ever.
    assert _task(db)["state"] == "READY"
    assert _task(db)["metadata"]["release_wait"]["wake_reason"] == "release_wait_expired"


# -- 4. bounded reads, the tenant's own -------------------------------------------------------

def test_a_wait_is_read_at_most_once_per_five_minutes(ctx, db, github, sha, now):
    _park(db, sha, at=now[0] - timedelta(hours=1))
    _tick(ctx)
    _tick(ctx)
    # MUTATION: skip the `checked_at` check and GitHub is asked every minute.
    assert len(github.reads) == 1
    now[0] += timedelta(seconds=mergewake.RELEASE_READ_SECONDS)
    _tick(ctx)
    assert len(github.reads) == 2


def test_another_tenants_wait_is_not_read_and_other_parks_are_not_visited(ctx, db, github, sha, now, tokens):
    _park(db, sha, task_id="task_theirs", tenant_id="research", at=now[0] - timedelta(hours=1))
    _park(db, sha, task_id="task_plain", at=now[0] - timedelta(hours=1), record=False)

    report = _tick(ctx)

    # MUTATION: drop the tenant filter and another tenant's commit is read with this token.
    assert report.release_visited == 0
    assert github.reads == [] and tokens.asked == []
    assert _task(db, "task_theirs")["state"] == "PARKED"


def test_a_tenant_with_no_release_wait_answers_as_before(ctx, db):
    # MUTATION: always add `releases` and the merge tick's answer changes for every tenant.
    assert "releases" not in _tick(ctx).to_api()
