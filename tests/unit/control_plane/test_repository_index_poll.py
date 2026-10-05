"""The repository index poll: `POST /v1/admin/repositories/poll` (repo-index.md §3.3, lane RI4).

A per-tenant Cloud Scheduler job (`repo_index_poll`, terraform/modules/
scheduler/jobs.tf) calls this route every five minutes as the rollup
sweeper's OIDC identity. What is held here:

  * an unchanged default branch is read with the last response's ETag, GitHub
    answers `304 Not Modified`, and nothing is submitted;
  * a head that moved is indexed ONCE: a second poll finds the run in flight
    and submits nothing more;
  * the minimum change interval holds a busy repository back, and the
    interval trigger (`last_indexed_at + interval_hours`) queues a run even
    when the head never moved;
  * a run in flight is never duplicated: a newer head is recorded as pending
    and indexed once, after the running one ends;
  * the poll reads only the named tenant's registrations, with that tenant's
    token, and submits only into that tenant, as the registration's creator;
  * only the scheduler's identity may call it.

No credentials, no network: GitHub is a fake transport under the real client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant
from .repo_fakes import TenantTokens, repo_entry
from .repo_index_fakes import (
    REPOSITORY,
    HeadPolls,
    IndexGitHub,
    etag_of,
    finish_index_task,
    fixture_index,
    sha,
)

ONE, TWO, THREE = sha("one"), sha("two"), sha("three")
OTHER = "saga-xyz/gadgets"

#: The identity terraform/modules/scheduler/jobs.tf gives the poll job: the
#: rollup sweeper, admitted by being on ROLLUP_SWEEPER_USERS.
SWEEPER = "swarm-rollup-sweeper@example-project.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def github():
    return IndexGitHub(
        heads={"main": ONE},
        repos={
            REPOSITORY: repo_entry(REPOSITORY, default_branch="main"),
            OTHER: repo_entry(OTHER, default_branch="main"),
        },
    )


@pytest.fixture
def polls(github):
    return HeadPolls(github)


@pytest.fixture
def secrets_reader():
    return TenantTokens()


@pytest.fixture
def client(db, tokens, group_map, objects, github, polls, secrets_reader, clock):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    ctx = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=secrets_reader,
        forge=forge.GitHubIssues(send=github, probe_send=polls),
        now=clock,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _register(client, repository=REPOSITORY, user="alice") -> str:
    created = client.post(
        "/v1/repositories", json={"repository": repository}, headers=auth_header(user)
    )
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


@pytest.fixture
def repo_id(client) -> str:
    return _register(client)


def _poll(client, tenant_id="eng", headers=None):
    return client.post(
        f"/v1/admin/repositories/poll?tenant_id={tenant_id}",
        headers=SWEEPER_HEADERS if headers is None else headers,
    )


def _tasks(db, tenant_id=None) -> list[dict]:
    return [doc for path, doc in db.docs.items()
            if path.startswith("tasks/") and path.count("/") == 1
            and (tenant_id is None or doc.get("tenant_id") == tenant_id)]


def _index(db, repo_id) -> dict:
    return db.docs[f"repositories/{repo_id}"]["index"]


def _indexed(db, repo_id, commit, at, *, head=None, etag=True) -> None:
    """The registration as a promoted index of `commit` at `at` leaves it."""
    index = _index(db, repo_id)
    index.update(current_sha=commit, last_indexed_at=at, head_sha=head or commit,
                 head_read_at=at, etag=etag_of(head or commit) if etag else None,
                 etag_sha=(head or commit) if etag else None)


# --------------------------------------------------------------------------
# the conditional read
# --------------------------------------------------------------------------

def test_an_unchanged_branch_answers_304_and_submits_nothing(client, db, repo_id, polls, clock):
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=2))
    clock.advance(minutes=5)

    response = _poll(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"]["not_modified"] == 1
    assert body["report"]["submitted"] == 0
    assert _tasks(db) == []
    [(url, headers, status)] = polls.calls
    assert url == "https://api.github.com/repos/saga-xyz/widgets/commits/main"
    # The last response's ETag, so GitHub can answer 304 without spending
    # the token's rate limit; the sha media type keeps a 200 to 40 bytes.
    assert headers["If-None-Match"] == etag_of(ONE)
    assert headers["Accept"] == "application/vnd.github.sha"
    assert status == 304
    index = _index(db, repo_id)
    assert index["head_sha"] == ONE
    assert index["head_read_at"] == clock()


def test_an_etag_is_sent_only_for_the_head_it_described(
    client, db, objects, repo_id, github, polls, clock
):
    """"Index now" moves `head_sha` with no ETag. The tag of the earlier head
    must not be sent for the new one: a branch force-pushed back to the
    earlier head would answer 304 and vouch for the wrong commit."""
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=2))
    github.heads["main"] = TWO
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    assert _index(db, repo_id)["head_sha"] == TWO
    github.heads["main"] = ONE  # force-pushed back

    _poll(client)

    [(_url, headers, status)] = polls.calls
    assert "If-None-Match" not in headers and status == 200
    assert _index(db, repo_id)["head_sha"] == ONE


def test_the_first_read_sends_no_etag_and_stores_the_one_it_gets(client, db, repo_id, polls):
    _poll(client)
    [(_url, headers, status)] = polls.calls
    assert "If-None-Match" not in headers and status == 200
    index = _index(db, repo_id)
    assert index["etag"] == etag_of(ONE) and index["head_sha"] == ONE


# --------------------------------------------------------------------------
# the change trigger
# --------------------------------------------------------------------------

def test_a_changed_head_submits_one_index_run(client, db, repo_id, github, polls, clock):
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=2))
    github.heads["main"] = TWO

    first = _poll(client)

    assert first.status_code == 200, first.text
    assert first.json()["report"]["submitted"] == 1
    [task] = _tasks(db)
    assert task["repository_ref"] == TWO
    assert task["runner_profile"] == "claude-code"  # by name (invariant 10)
    assert task["tenant_id"] == "eng"
    # As the registration's creator, never as the scheduler's identity.
    assert task["submitted_by"] == "alice@saga.xyz"
    assert task["state"] in ("QUEUED", "READY")  # waits for admission (invariant 1)
    run = db.docs[f"repo_index_runs/{task['id']}"]
    assert run["trigger"] == "change" and run["commit_sha"] == TWO
    index = _index(db, repo_id)
    assert index["head_sha"] == TWO and index["etag"] == etag_of(TWO)
    assert index["in_flight_task_id"] == task["id"]

    clock.advance(minutes=5)
    second = _poll(client)
    assert second.json()["report"]["submitted"] == 0
    assert len(_tasks(db)) == 1, "the same head is indexed once"
    assert polls.calls[-1][2] == 304


def test_a_new_registration_is_indexed_on_its_first_poll(client, db, repo_id):
    report = _poll(client).json()["report"]
    assert report["submitted"] == 1
    [task] = _tasks(db)
    assert task["repository_ref"] == ONE


def test_the_minimum_change_interval_holds_a_busy_branch_back(
    client, db, objects, repo_id, github, clock
):
    _poll(client)
    [task] = _tasks(db)
    finish_index_task(db, objects, task["id"], fixture_index(ONE))
    clock.advance(minutes=10)
    github.heads["main"] = TWO

    early = _poll(client).json()["report"]
    assert early["submitted"] == 0
    assert len(_tasks(db)) == 1
    assert _index(db, repo_id)["current_sha"] == ONE  # the finished run was settled
    assert _index(db, repo_id)["head_sha"] == TWO  # the head is recorded all the same

    clock.advance(minutes=21)  # 31 minutes since the last run was queued
    later = _poll(client).json()["report"]
    assert later["submitted"] == 1
    assert sorted(t["repository_ref"] for t in _tasks(db)) == sorted([ONE, TWO])


def test_on_change_off_reads_no_head_until_the_interval_is_due(client, db, repo_id, polls, clock):
    patched = client.patch(
        f"/v1/repositories/{repo_id}", json={"index": {"on_change": "off"}},
        headers=auth_header("alice"),
    )
    assert patched.status_code == 200, patched.text
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=1))
    assert _poll(client).json()["report"]["submitted"] == 0
    assert polls.calls == []


# --------------------------------------------------------------------------
# the interval trigger
# --------------------------------------------------------------------------

def test_an_elapsed_interval_submits_a_run_of_the_unchanged_head(
    client, db, repo_id, polls, clock
):
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=25))

    report = _poll(client).json()["report"]

    assert polls.calls[-1][2] == 304
    assert report["submitted"] == 1
    [task] = _tasks(db)
    assert task["repository_ref"] == ONE
    assert db.docs[f"repo_index_runs/{task['id']}"]["trigger"] == "interval"


def test_an_interval_not_yet_elapsed_submits_nothing(client, db, repo_id, clock):
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=23))
    assert _poll(client).json()["report"]["submitted"] == 0
    assert _tasks(db) == []


def test_a_failed_run_is_not_retried_every_tick(client, db, objects, repo_id, clock):
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=25))
    _poll(client)
    [task] = _tasks(db)
    finish_index_task(db, objects, task["id"], None, state="FAILED")
    for _ in range(3):
        clock.advance(minutes=5)
        assert _poll(client).json()["report"]["submitted"] == 0
    assert len(_tasks(db)) == 1
    # The interval counts from the failed run's queueing: a day later, again.
    clock.advance(hours=24)
    assert _poll(client).json()["report"]["submitted"] == 1


# --------------------------------------------------------------------------
# coalescing with RI2's in-flight rule
# --------------------------------------------------------------------------

def test_a_run_in_flight_is_not_duplicated(client, db, objects, repo_id, github, clock):
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    manual = started.json()["run"]["task_id"]

    clock.advance(minutes=5)
    same = _poll(client).json()["report"]
    assert same["submitted"] == 0 and same["coalesced"] == 0
    assert _index(db, repo_id).get("pending_sha") is None

    clock.advance(minutes=30)
    github.heads["main"] = TWO
    moved = _poll(client).json()["report"]
    assert moved["submitted"] == 0 and moved["coalesced"] == 1
    assert len(_tasks(db)) == 1
    assert _index(db, repo_id)["pending_sha"] == TWO

    clock.advance(minutes=5)
    _poll(client)
    assert len(_tasks(db)) == 1, "a pending head is recorded once, not resubmitted"

    finish_index_task(db, objects, manual, fixture_index(ONE))
    clock.advance(minutes=5)
    after = _poll(client).json()["report"]
    assert after["submitted"] == 1
    refs = sorted(t["repository_ref"] for t in _tasks(db))
    assert refs == sorted([ONE, TWO])
    assert _index(db, repo_id)["current_sha"] == ONE
    assert _index(db, repo_id).get("pending_sha") is None

    clock.advance(minutes=5)
    assert _poll(client).json()["report"]["submitted"] == 0
    assert len(_tasks(db)) == 2


def test_a_paused_registration_is_neither_read_nor_indexed(client, db, repo_id, polls, clock):
    client.patch(f"/v1/repositories/{repo_id}", json={"paused": True},
                 headers=auth_header("alice"))
    _indexed(db, repo_id, ONE, clock() - timedelta(days=3))
    report = _poll(client).json()["report"]
    assert report["skipped"] == 1 and report["submitted"] == 0
    assert polls.calls == [] and _tasks(db) == []


# --------------------------------------------------------------------------
# tenant scoping
# --------------------------------------------------------------------------

def test_the_poll_reads_and_submits_only_the_named_tenant(
    client, db, repo_id, polls, secrets_reader
):
    other = _register(client, OTHER, user="bob")
    secrets_reader.asked.clear()

    response = _poll(client, "eng")

    assert response.status_code == 200, response.text
    assert response.json()["report"]["registrations"] == 1
    assert [url for url, _h, _s in polls.calls] == [
        "https://api.github.com/repos/saga-xyz/widgets/commits/main"
    ]
    assert set(secrets_reader.asked) == {"swarm-tenant-eng-git"}
    assert [t["tenant_id"] for t in _tasks(db)] == ["eng"]
    untouched = _index(db, other)
    assert untouched.get("head_read_at") is None and untouched.get("in_flight_task_id") is None

    _poll(client, "research")
    assert {t["tenant_id"] for t in _tasks(db)} == {"eng", "research"}
    [research] = _tasks(db, "research")
    assert research["submitted_by"] == "bob@saga.xyz"


def test_a_creator_no_longer_in_the_tenant_submits_nothing(client, db, repo_id):
    # carol is in no tenant group: the creator as they would read once removed.
    db.docs[f"repositories/{repo_id}"]["created_by"] = "carol@saga.xyz"
    response = _poll(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"]["submitted"] == 0 and _tasks(db) == []
    assert body["failures"] == [{"repo_id": repo_id, "code": "owner_not_member"}]


def test_a_refused_read_is_reported_and_the_pass_goes_on(client, db, repo_id, polls):
    other = _register(client, "saga-xyz/gadgets")
    polls.status[REPOSITORY] = 403
    body = _poll(client).json()
    assert body["failures"] == [{"repo_id": repo_id, "code": "no_access"}]
    assert body["report"]["submitted"] == 1
    [task] = _tasks(db)
    assert task["metadata"]["repo_index"] == other


# --------------------------------------------------------------------------
# who may call it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("user", ["alice", "root"])
def test_only_the_scheduler_identity_may_poll(client, db, repo_id, user):
    response = _poll(client, headers=auth_header(user))
    assert response.status_code == 403, response.text
    assert _tasks(db) == []


def test_the_tenant_must_be_named(client):
    response = client.post("/v1/admin/repositories/poll", headers=SWEEPER_HEADERS)
    assert response.status_code == 422
