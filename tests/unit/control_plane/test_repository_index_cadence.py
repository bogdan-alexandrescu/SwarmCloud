"""The indexer's model and cadence (#2524, INDEXER-MODEL-CADENCE; BGD 2026-10-27:
"Sonnet, 120 min, skip unchanged").

What is held here:

  * an index task names the `sonnet` model class where the deployment pins it
    (WORKER_CLASS_MODELS), and names none -- the profile's default -- where it
    pins nothing, so the local emulator loop is not refused
    `model_class_not_yet_available`;
  * a head equal to the promoted index's commit starts NO run: the poll's
    interval tick holds it as `unchanged` and records `index.last_skip`, and
    does not try again on every tick; an incremental "Index now" answers
    `skipped`; only an explicit full request rebuilds it;
  * the default change interval is 120 minutes; a value a caller set wins,
    even one equal to the old default of 30, while the 30 every registration
    stored at creation before #2524 reads as the new default.

No credentials, no network: GitHub is a fake transport under the real client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, repoindex
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import (
    MIN_CHANGE_INTERVAL_DEFAULT,
    min_change_interval_of,
)
from swarm_api.waker import NullWaker
from swarm_common.profiles import RUNNER_PROFILES

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

ONE, TWO = sha("one"), sha("two")
SWEEPER = "rollup-sweeper@swarm.example.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
#: terraform/infra/locals.tf `runner_models`, as WORKER_CLASS_MODELS carries it.
CLASS_MODELS = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def github():
    return IndexGitHub(heads={"main": ONE},
                       repos={REPOSITORY: repo_entry(REPOSITORY, default_branch="main")})


def _client(db, tokens, group_map, objects, github, clock, *, class_models):
    seed_tenant(db, "eng")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    ctx = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,),
                              worker_class_models=dict(class_models)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=TenantTokens(),
        forge=forge.GitHubIssues(send=github, probe_send=HeadPolls(github)),
        now=clock,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def client(db, tokens, group_map, objects, github, clock):
    return _client(db, tokens, group_map, objects, github, clock, class_models=CLASS_MODELS)


def _register(client, **index) -> str:
    body = {"repository": REPOSITORY, **({"index": index} if index else {})}
    created = client.post("/v1/repositories", json=body, headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


def _tasks(db) -> list[dict]:
    return [doc for path, doc in db.docs.items()
            if path.startswith("tasks/") and path.count("/") == 1]


def _index(db, repo_id) -> dict:
    return db.docs[f"repositories/{repo_id}"]["index"]


def _indexed(db, repo_id, commit, at) -> None:
    """The registration as a promoted index of `commit` at `at` leaves it."""
    _index(db, repo_id).update(current_sha=commit, last_indexed_at=at, head_sha=commit,
                               head_read_at=at, etag=etag_of(commit), etag_sha=commit)


def _poll(client):
    response = client.post("/v1/admin/repositories/poll?tenant_id=eng",
                           headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["report"]


def _index_now(client, repo_id, **body):
    return client.post(f"/v1/repositories/{repo_id}/index", json=body,
                       headers=auth_header("alice"))


# --------------------------------------------------------------------------
# the model class
# --------------------------------------------------------------------------

def test_the_indexer_profile_accepts_the_class_index_runs_name():
    # A class the profile refused would be a 422 on every index run.
    assert repoindex.INDEXER_MODEL_CLASS == "sonnet"
    assert "sonnet" in RUNNER_PROFILES[repoindex.INDEXER_PROFILE].model_classes


def test_an_index_task_names_the_sonnet_class():
    record = {"repo_id": "r1", "owner": "example-org", "repo": "widgets",
              "default_branch": "main", "tenant_id": "eng",
              "repository_url": "https://github.com/example-org/widgets"}
    assert repoindex.indexer_task(record, ONE).model_class == "sonnet"
    assert repoindex.index_model_class(CLASS_MODELS) == "sonnet"
    assert repoindex.index_model_class({}) is None


def test_index_now_submits_a_sonnet_task_where_the_deployment_pins_it(client, db):
    repo_id = _register(client)
    started = _index_now(client, repo_id)
    assert started.status_code == 202, started.text
    [task] = _tasks(db)
    assert task["runner_profile"] == "indexer"
    assert task["model_class"] == "sonnet"
    assert task["model"] == CLASS_MODELS["sonnet"]


def test_an_index_run_where_nothing_is_pinned_takes_the_default_and_is_not_refused(
    db, tokens, group_map, objects, github, clock
):
    unpinned = _client(db, tokens, group_map, objects, github, clock, class_models={})
    repo_id = _register(unpinned)
    started = _index_now(unpinned, repo_id)
    assert started.status_code == 202, started.text
    [task] = _tasks(db)
    assert task.get("model_class") is None


# --------------------------------------------------------------------------
# an unchanged head starts nothing
# --------------------------------------------------------------------------

def test_choose_kind_starts_no_run_for_the_promoted_head():
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    index = {"current_sha": ONE, "last_kind": "full", "last_indexed_at": now}
    choice = repoindex.choose_kind(index, ONE, requested="incremental",
                                   version=None, changes=None, now=now)
    assert choice.kind == repoindex.NO_RUN
    assert "the head is the promoted index" in choice.reason
    # A person asking for a full run still gets one.
    asked = repoindex.choose_kind(index, ONE, requested="full",
                                  version=None, changes=None, now=now)
    assert asked.kind == "full"


def test_an_elapsed_interval_on_the_promoted_head_starts_nothing_and_says_why(
    client, db, clock
):
    repo_id = _register(client)
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=25))

    report = _poll(client)

    assert report["submitted"] == 0 and report["skipped"] == 1
    assert report["held"] == {repoindex.UNCHANGED: 1}
    assert _tasks(db) == []
    index = _index(db, repo_id)
    assert index["in_flight_task_id"] is None, "the claim is released"
    poll = index["last_poll"]
    assert (poll["outcome"], poll["reason"], poll["head_sha"]) == (
        "held", repoindex.UNCHANGED, ONE)
    skip = index["last_skip"]
    assert skip["head_sha"] == ONE and skip["trigger"] == "interval"
    assert skip["at"] == clock()
    assert "the head is the promoted index" in skip["reason"]


def test_a_skipped_interval_is_not_tried_again_on_every_tick(client, db, clock):
    repo_id = _register(client)
    _indexed(db, repo_id, ONE, clock() - timedelta(hours=25))
    _poll(client)
    for _ in range(3):
        clock.advance(minutes=5)
        report = _poll(client)
        assert report["held"] == {"current": 1}, report
    assert _index(db, repo_id)["last_skip"]["at"] == clock() - timedelta(minutes=15)
    assert _tasks(db) == []


def test_an_incremental_index_now_on_the_promoted_head_is_skipped(client, db, objects):
    repo_id = _register(client)
    started = _index_now(client, repo_id, kind="full")
    finish_index_task(db, objects, started.json()["run"]["task_id"], fixture_index(ONE))
    assert client.get(f"/v1/repositories/{repo_id}/index",
                      headers=auth_header("alice")).status_code == 200
    assert _index(db, repo_id)["current_sha"] == ONE

    again = _index_now(client, repo_id, kind="incremental")

    assert again.status_code == 202, again.text
    body = again.json()
    assert body["run"] is None and body["coalesced"] is False
    assert body["skipped"] == repoindex.UNCHANGED
    assert len(_tasks(db)) == 1
    # A full request is a person's to make, and it rebuilds.
    rebuilt = _index_now(client, repo_id, kind="full")
    assert rebuilt.status_code == 202 and rebuilt.json()["run"]["kind"] == "full"
    assert len(_tasks(db)) == 2


# --------------------------------------------------------------------------
# the change interval
# --------------------------------------------------------------------------

def test_the_default_change_interval_is_120_minutes(client, db):
    assert MIN_CHANGE_INTERVAL_DEFAULT == 120
    repo_id = _register(client)
    assert min_change_interval_of(_index(db, repo_id)) == 120
    served = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice"))
    assert served.json()["repository"]["index"]["min_change_interval_minutes"] == 120


def test_the_old_default_stored_at_registration_reads_as_the_new_one():
    # Every registration before #2524 stored 30 at creation (`_index_defaults`).
    assert min_change_interval_of({"min_change_interval_minutes": 30}) == 120
    assert min_change_interval_of({}) == 120
    # A value nobody would have got by default was a choice, and stays.
    assert min_change_interval_of({"min_change_interval_minutes": 60}) == 60


@pytest.mark.parametrize("minutes", [30, 45, 240])
def test_an_interval_a_caller_sets_wins(client, db, minutes):
    repo_id = _register(client)
    patched = client.patch(f"/v1/repositories/{repo_id}",
                           json={"index": {"min_change_interval_minutes": minutes}},
                           headers=auth_header("alice"))
    assert patched.status_code == 200, patched.text
    assert patched.json()["repository"]["index"]["min_change_interval_minutes"] == minutes
    assert min_change_interval_of(_index(db, repo_id)) == minutes


def test_an_interval_named_at_registration_wins(client, db):
    repo_id = _register(client, min_change_interval_minutes=30)
    assert min_change_interval_of(_index(db, repo_id)) == 30


def test_the_poll_throttles_a_moved_head_for_120_minutes_by_default(
    client, db, objects, github, clock
):
    _register(client)
    _poll(client)
    [task] = _tasks(db)
    finish_index_task(db, objects, task["id"], fixture_index(ONE))
    github.heads["main"] = TWO

    clock.advance(minutes=31)
    assert _poll(client)["submitted"] == 0, "30 minutes no longer clears the throttle"
    clock.advance(minutes=90)  # 121 minutes since the last run was queued
    assert _poll(client)["submitted"] == 1
    assert sorted(t["repository_ref"] for t in _tasks(db)) == sorted([ONE, TWO])


def test_the_poll_applies_an_override_shorter_than_the_default(
    client, db, objects, github, clock
):
    repo_id = _register(client, min_change_interval_minutes=30)
    _poll(client)
    [task] = _tasks(db)
    finish_index_task(db, objects, task["id"], fixture_index(ONE))
    github.heads["main"] = TWO
    clock.advance(minutes=31)
    assert _poll(client)["submitted"] == 1
    assert _index(db, repo_id)["min_change_interval_minutes"] == 30
