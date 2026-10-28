"""A repository whose index runs fail says why, and the poll stops re-running it (INDEX-FAIL).

Operator QA p14 (2026-10-13): in tenant u-bogdan the poll re-queued an index
run of sagaxyz/ai-studio every 15 minutes, every one ending "repository clone
failed ... remote: Repository not found. fatal: Authentication failed",
while the Repositories card showed only "no index" / "indexing now". What is
held here:

  * a failed run's cause is classified from the clone error the worker wrote
    (`access` for not-found / refused credentials, `transient` for a forge or
    network that did not answer) and stored on the registration as
    `index.last_failure`, which the API serves;
  * after 2 consecutive access failures the poll stops queueing that
    repository (`index.backoff`, the held reason `backoff`), and resumes on
    Index now or when the tenant's git credentials change;
  * a transient failure never stops the poll;
  * a success clears the failure, the streak and the back-off;
  * no token, and no URL user-info, reaches anything stored or served.

No credentials, no network: the fixtures are the poll suite's.
"""

from __future__ import annotations

import json

import pytest

from swarm_api import indexfailure
from swarm_api.gittokens import GitTokens, Scope, record_for_slot

from .conftest import auth_header
from .repo_index_fakes import finish_index_task, fixture_index
from .test_repository_index_poll import (  # noqa: F401 -- the poll suite's fixtures
    ONE,
    _index,
    _poll,
    _tasks,
    client,
    clock,
    github,
    polls,
    repo_id,
    secrets_reader,
)

#: Built at runtime, never one literal: nothing added may look like a credential.
TOKEN = "ghp_" + "Q7" * 18
NOT_FOUND = (
    "repository clone failed: git clone exited 128: remote: Repository not found. "
    f"fatal: Authentication failed for 'https://x-access-token:{TOKEN}@github.com/"
    "example-org/widgets/'"
)
UNREACHABLE = (
    "repository clone failed: fatal: unable to access 'https://github.com/example-org/widgets/': "
    "Could not resolve host: github.com"
)


def _fail_newest(db, objects, error: str, *, end_cause: str = "runner_error") -> str:
    """The newest index task ends FAILED with `error` as the worker writes it."""
    task = max(_tasks(db), key=lambda t: t["created_at"])
    finish_index_task(db, objects, task["id"], None, state="FAILED")
    doc = db.docs[f"tasks/{task['id']}"]
    doc["last_error"] = error
    doc["end_cause"] = end_cause
    return task["id"]


def _queued_then_failed(client, db, objects, clock, error: str, **kw) -> str:
    """One poll tick queues a run (the head is not the built index), the run fails,
    and the change throttle is let pass."""
    report = _poll(client).json()["report"]
    assert report["submitted"] == 1, report
    task_id = _fail_newest(db, objects, error, **kw)
    clock.advance(minutes=121)  # past the 120-minute change throttle (#2524)
    return task_id


# --------------------------------------------------------------------------
# the classification: pure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("error, end_cause, expected", [
    (NOT_FOUND, "runner_error", "access"),
    ("repository clone failed: fatal: could not read Username for 'https://github.com': "
     "terminal prompts disabled", "runner_error", "access"),
    ("remote: Permission to example-org/widgets.git denied to someone.", "runner_error", "access"),
    (UNREACHABLE, "runner_error", "transient"),
    ("repository clone failed: the forge did not answer after 3 tries", "runner_error",
     "transient"),
    (None, "lost_worker", "transient"),
    ("the indexer exited 1", "runner_error", "other"),
])
def test_the_cause_is_classified_from_the_clone_error(error, end_cause, expected):
    assert indexfailure.classify(error, end_cause) == expected


def test_a_stored_failure_carries_no_token_and_no_url_credentials():
    record = indexfailure.failure_record(
        task_id="task_1", commit_sha=ONE, at="now", last_error=NOT_FOUND,
        end_cause="runner_error",
    )
    stored = json.dumps(record)
    assert TOKEN not in stored
    assert "x-access-token" not in stored
    assert record["class"] == "access"
    assert record["detail"].startswith("remote: Repository not found.")
    assert "github.com/example-org/widgets" in record["detail"], "the host and path are kept"


# --------------------------------------------------------------------------
# the registration says why
# --------------------------------------------------------------------------

def test_a_failed_run_is_recorded_on_the_registration_and_served(
    client, db, objects, repo_id, clock
):
    task_id = _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _poll(client)

    failure = _index(db, repo_id)["last_failure"]
    assert failure["task_id"] == task_id
    assert failure["class"] == "access"
    assert failure["cause"] == indexfailure.CAUSES["access"]

    served = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice"))
    index = served.json()["repository"]["index"]
    assert index["last_failure"]["task_id"] == task_id
    assert index["last_failure"]["class"] == "access"
    assert isinstance(index["last_failure"]["at"], str)
    listed = client.get("/v1/repositories", headers=auth_header("alice"))
    assert listed.json()["repositories"][0]["index"]["last_failure"]["task_id"] == task_id
    for text in (served.text, listed.text, json.dumps(db.docs[f"repositories/{repo_id}"],
                                                     default=str)):
        assert TOKEN not in text
        assert "x-access-token" not in text


# --------------------------------------------------------------------------
# the back-off
# --------------------------------------------------------------------------

def test_the_poll_stops_after_two_access_failures(client, db, objects, repo_id, clock):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)

    for _ in range(4):
        report = _poll(client).json()["report"]
        assert report["submitted"] == 0, "a repository it cannot read is not re-run"
        assert report["held"] == {"backoff": 1}
        clock.advance(minutes=121)  # past the 120-minute change throttle (#2524)
    assert len(_tasks(db)) == 2

    index = _index(db, repo_id)
    assert index["failure_streak"] == {"class": "access", "count": 2}
    assert index["backoff"]["failures"] == 2
    assert "Index now" in index["backoff"]["reason"]
    poll = index["last_poll"]
    assert (poll["outcome"], poll["reason"]) == ("held", "backoff")
    served = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice"))
    assert isinstance(served.json()["repository"]["index"]["backoff"]["since"], str)


def test_index_now_lifts_the_backoff(client, db, objects, repo_id, clock):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    assert _poll(client).json()["report"]["held"] == {"backoff": 1}

    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    assert _index(db, repo_id).get("backoff") is None
    assert len(_tasks(db)) == 3

    # The manual run succeeds: the failure and its streak are gone.
    finish_index_task(db, objects, started.json()["run"]["task_id"], fixture_index(ONE))
    _poll(client)
    index = _index(db, repo_id)
    assert index.get("last_failure") is None
    assert index.get("failure_streak") is None
    assert index["current_sha"] == ONE


def test_a_failure_after_index_now_stops_the_poll_again_at_once(
    client, db, objects, repo_id, clock
):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    client.post(f"/v1/repositories/{repo_id}/index", json={}, headers=auth_header("alice"))
    _fail_newest(db, objects, NOT_FOUND)
    clock.advance(minutes=121)  # past the 120-minute change throttle (#2524)

    report = _poll(client).json()["report"]
    assert report["submitted"] == 0 and report["held"] == {"backoff": 1}
    assert _index(db, repo_id)["failure_streak"]["count"] == 3


def test_a_credential_change_lifts_the_backoff(client, db, objects, repo_id, clock):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    assert _poll(client).json()["report"]["held"] == {"backoff": 1}
    clock.advance(minutes=121)  # past the 120-minute change throttle (#2524)

    # Setup > Git tokens: a token for this repository is registered.
    record = record_for_slot("eng", Scope.REPOSITORY, repo_id=repo_id,
                             repository="example-org/widgets", registered_by="alice@example.com",
                             now=clock())
    GitTokens(db, now=clock).register(record)

    report = _poll(client).json()["report"]
    assert report["submitted"] == 1, "the poll resumes once the credentials changed"
    assert _index(db, repo_id).get("backoff") is None


def test_an_unchanged_credential_keeps_the_backoff(client, db, objects, repo_id, clock):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    assert _poll(client).json()["report"]["held"] == {"backoff": 1}
    mark = _index(db, repo_id)["backoff"]["credential"]
    assert mark, "the back-off records the credentials it was set under"
    for _ in range(3):
        assert _poll(client).json()["report"]["submitted"] == 0
        clock.advance(hours=25)  # the interval and the daily re-verification pass
    assert _index(db, repo_id)["backoff"]["credential"] == mark


def test_a_transient_failure_keeps_polling(client, db, objects, repo_id, clock):
    for _ in range(4):
        _queued_then_failed(client, db, objects, clock, UNREACHABLE)
    # The tick that settles the fourth failure queues a fifth run.
    assert _poll(client).json()["report"]["submitted"] == 1
    assert len(_tasks(db)) == 5
    index = _index(db, repo_id)
    assert index.get("backoff") is None
    assert index["last_failure"]["class"] == "transient"
    assert index["failure_streak"] == {"class": "transient", "count": 4}


def test_a_different_cause_restarts_the_streak(client, db, objects, repo_id, clock):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _queued_then_failed(client, db, objects, clock, UNREACHABLE)
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    assert _poll(client).json()["report"]["submitted"] == 1, "never two access failures in a row"
    assert _index(db, repo_id).get("backoff") is None


def test_a_success_resets_the_streak(client, db, objects, repo_id, clock):
    _queued_then_failed(client, db, objects, clock, NOT_FOUND)
    _poll(client)
    task = max(_tasks(db), key=lambda t: t["created_at"])
    finish_index_task(db, objects, task["id"], fixture_index(ONE))
    _poll(client)
    index = _index(db, repo_id)
    assert index.get("last_failure") is None and index.get("failure_streak") is None
    assert index.get("backoff") is None

