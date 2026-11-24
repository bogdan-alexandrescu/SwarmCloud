"""An idempotency key on `POST /v1/tasks`, `/v1/tasks/batch` and `/v1/workflows`.

A submission re-sent with its key answers the first submission's ids, marked
`idempotent_replay`, and creates nothing; the same key with another payload is
refused; a key is scoped to its workspace AND its submitter; two concurrent
submits with one key create one submission; a key expires after
`idempotency.KEY_WINDOW`.
"""

from __future__ import annotations

import secrets
import threading
from datetime import timedelta
from typing import Any

import pytest

from swarm_api.store import Store

from .conftest import auth_header


#: The claims' collection and the key window (`swarm_api.idempotency`),
#: written out so this file states the contract rather than reading it back.
SUBMISSION_KEYS = "submission_keys"
KEY_WINDOW = timedelta(hours=24)


def _key() -> str:
    return secrets.token_urlsafe(24)


def _task(prompt: str = "build it") -> dict[str, Any]:
    return {"runner_profile": "mock", "input": {"prompt": prompt}}


def _workflow(prompt: str = "build it") -> dict[str, Any]:
    return {
        "steps": [
            {"step_id": "build", "runner_profile": "mock", "input": {"prompt": prompt}},
            {"step_id": "check", "runner_profile": "mock", "depends_on": ["build"],
             "input": {"prompt": "check it"}},
        ],
    }


def _task_ids(db) -> list[str]:
    return [p.split("/")[1] for p in db.paths("tasks/") if p.count("/") == 1]


def _workflow_ids(db) -> list[str]:
    return [p.split("/")[1] for p in db.paths("workflows/") if p.count("/") == 1]


def _claims(db) -> dict[str, dict[str, Any]]:
    return db.dump(f"{SUBMISSION_KEYS}/")


def _post(client, path: str, body: dict[str, Any], user: str = "alice"):
    return client.post(path, headers=auth_header(user), json=body)


# -- replay answers the original ids, and creates nothing --------------------

def test_a_task_resent_with_its_key_answers_the_original_task_and_creates_nothing(client, db):
    key = _key()
    first = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key})
    assert first.status_code == 201, first.text
    assert first.json()["idempotent_replay"] is False
    again = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key})
    assert again.status_code == 200, again.text
    assert again.json()["idempotent_replay"] is True
    assert again.json()["task"]["id"] == first.json()["task"]["id"]
    assert _task_ids(db) == [first.json()["task"]["id"]]


def test_a_batch_resent_with_its_key_answers_the_original_tasks(client, db):
    key = _key()
    body = {"tasks": [_task("one"), _task("two")], "idempotency_key": key}
    first = _post(client, "/v1/tasks/batch", body)
    assert first.status_code == 201, first.text
    again = _post(client, "/v1/tasks/batch", body)
    assert again.status_code == 200, again.text
    assert again.json()["idempotent_replay"] is True
    ids = [t["id"] for t in first.json()["tasks"]]
    assert [t["id"] for t in again.json()["tasks"]] == ids
    assert sorted(_task_ids(db)) == sorted(ids)


def test_a_workflow_resent_with_its_key_answers_the_original_workflow(client, db):
    key = _key()
    first = _post(client, "/v1/workflows", {**_workflow(), "idempotency_key": key})
    assert first.status_code == 201, first.text
    again = _post(client, "/v1/workflows", {**_workflow(), "idempotency_key": key})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["idempotent_replay"] is True
    assert body["workflow"]["workflow_id"] == first.json()["workflow"]["workflow_id"]
    assert ({s["task_id"] for s in body["workflow"]["steps"]}
            == {s["task_id"] for s in first.json()["workflow"]["steps"]})
    assert body["dispatch"] == first.json()["dispatch"]
    assert _workflow_ids(db) == [first.json()["workflow"]["workflow_id"]]
    assert len(_task_ids(db)) == 2


def test_without_a_key_two_identical_submissions_are_two(client, db):
    assert _post(client, "/v1/tasks", _task()).status_code == 201
    assert _post(client, "/v1/tasks", _task()).status_code == 201
    assert len(_task_ids(db)) == 2
    assert _claims(db) == {}


# -- the same key with a different payload is refused ------------------------

def test_the_same_key_with_a_different_payload_is_refused_and_creates_nothing(client, db):
    key = _key()
    assert _post(client, "/v1/tasks", {**_task("one"), "idempotency_key": key}).status_code == 201
    refused = _post(client, "/v1/tasks", {**_task("two"), "idempotency_key": key})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "idempotency_key_reused", refused.text
    assert len(_task_ids(db)) == 1


def test_the_same_key_on_another_route_is_refused(client, db):
    key = _key()
    assert _post(client, "/v1/tasks", {**_task(), "idempotency_key": key}).status_code == 201
    refused = _post(client, "/v1/tasks/batch", {"tasks": [_task()], "idempotency_key": key})
    assert refused.status_code == 409, refused.text
    refused = _post(client, "/v1/workflows", {**_workflow(), "idempotency_key": key})
    assert refused.status_code == 409, refused.text
    assert len(_task_ids(db)) == 1
    assert _workflow_ids(db) == []


def test_a_key_on_a_task_inside_a_batch_is_refused(client, db):
    body = {"tasks": [{**_task(), "idempotency_key": _key()}]}
    refused = _post(client, "/v1/tasks/batch", body)
    assert refused.status_code == 422, refused.text
    assert _task_ids(db) == []


def test_a_short_key_is_refused(client, db):
    refused = _post(client, "/v1/tasks", {**_task(), "idempotency_key": "abc"})
    assert refused.status_code == 422, refused.text
    assert _task_ids(db) == []


# -- a key is scoped to its workspace and its submitter ----------------------

def test_two_workspaces_with_one_key_get_two_submissions_and_never_each_others(client, db):
    """alice is in eng, bob in research: the same key and body are two
    submissions, neither a replay of the other, and bob is never answered
    alice's task."""
    key = _key()
    alice = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key}, user="alice")
    bob = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key}, user="bob")
    assert alice.status_code == 201 and bob.status_code == 201, (alice.text, bob.text)
    assert bob.json()["idempotent_replay"] is False
    assert bob.json()["task"]["id"] != alice.json()["task"]["id"]
    assert bob.json()["task"]["tenant_id"] != alice.json()["task"]["tenant_id"]
    # A different body in the other workspace is not a collision either.
    other = _post(client, "/v1/tasks", {**_task("else"), "idempotency_key": key}, user="bob")
    assert other.status_code == 409  # bob's OWN key, reused by bob
    again = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key}, user="bob")
    assert again.json()["task"]["id"] == bob.json()["task"]["id"]
    assert len(_task_ids(db)) == 2
    assert len(_claims(db)) == 2


def test_two_submitters_in_one_workspace_with_one_key_get_two_submissions(client, db):
    """alice and root are both in eng: a key is also the submitter's own."""
    key = _key()
    alice = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key}, user="alice")
    root = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key}, user="root")
    assert alice.status_code == 201 and root.status_code == 201, (alice.text, root.text)
    assert root.json()["task"]["tenant_id"] == alice.json()["task"]["tenant_id"]
    assert root.json()["idempotent_replay"] is False
    assert root.json()["task"]["id"] != alice.json()["task"]["id"]
    # root's different body under alice's key is root's own first use, not a refusal.
    assert _post(client, "/v1/tasks/batch", {"tasks": [_task()], "idempotency_key": _key()},
                 user="root").status_code == 201
    assert len(_task_ids(db)) == 3


def test_the_claim_stores_neither_the_key_nor_the_payload(client, db):
    key = _key()
    assert _post(client, "/v1/tasks", {**_task("a secret-free prompt"),
                                       "idempotency_key": key}).status_code == 201
    (path, claim), = _claims(db).items()
    assert key not in path and key not in repr(claim)
    assert "a secret-free prompt" not in repr(claim)
    assert claim["tenant_id"] == "eng" and claim["submitted_by"] == "alice@example.com"


# -- race-safe: two concurrent submits with one key create one submission ----

def test_two_concurrent_submits_with_one_key_create_exactly_one_submission(
        client, db, api_context, monkeypatch):
    """Both requests pass the claim read before either writes (the barrier
    holds each at `Store.create_tasks`), then race to the commit: one creates
    the claim and its task, the other's batch is refused whole and answers
    the winner's task."""
    barrier = threading.Barrier(2, timeout=30)
    original = Store.create_tasks

    def held(self, tasks, **kwargs):
        barrier.wait()
        return original(self, tasks, **kwargs)

    monkeypatch.setattr(Store, "create_tasks", held)
    key = _key()
    answers: list[Any] = []

    def send() -> None:
        answers.append(_post(client, "/v1/tasks", {**_task(), "idempotency_key": key}))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(answers) == 2
    assert sorted(a.status_code for a in answers) == [200, 201], [a.text for a in answers]
    assert sorted(a.json()["idempotent_replay"] for a in answers) == [False, True]
    assert answers[0].json()["task"]["id"] == answers[1].json()["task"]["id"]
    assert len(_task_ids(db)) == 1
    assert len(_claims(db)) == 1


def test_two_concurrent_workflow_submits_with_one_key_create_one_workflow(
        client, db, monkeypatch):
    barrier = threading.Barrier(2, timeout=30)
    original = Store.create_tasks

    def held(self, tasks, **kwargs):
        barrier.wait()
        return original(self, tasks, **kwargs)

    monkeypatch.setattr(Store, "create_tasks", held)
    key = _key()
    answers: list[Any] = []

    def send() -> None:
        answers.append(_post(client, "/v1/workflows", {**_workflow(), "idempotency_key": key}))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    # The loser's read of the winner's workflow may come before the winner
    # writes the workflow document: then it is told to re-send, never given a
    # second workflow.
    codes = sorted(a.status_code for a in answers)
    assert codes in ([200, 201], [201, 409]), [a.text for a in answers]
    if codes == [201, 409]:
        loser = next(a for a in answers if a.status_code == 409)
        assert loser.json()["code"] == "idempotency_replay_incomplete"
    assert len(_workflow_ids(db)) == 1
    assert len(_task_ids(db)) == 2


# -- expiry ------------------------------------------------------------------

def test_a_claim_expires_after_the_window_and_the_key_then_starts_a_new_submission(client, db):
    key = _key()
    first = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key})
    assert first.status_code == 201
    (path, claim), = _claims(db).items()
    assert claim["expires_at"] - claim["created_at"] == KEY_WINDOW
    db.docs[path]["expires_at"] = claim["created_at"] - timedelta(seconds=1)
    again = _post(client, "/v1/tasks", {**_task(), "idempotency_key": key})
    assert again.status_code == 201, again.text
    assert again.json()["idempotent_replay"] is False
    assert again.json()["task"]["id"] != first.json()["task"]["id"]
    assert len(_task_ids(db)) == 2
    # The fresh claim replaced the expired one and holds for a new window.
    (_, fresh), = _claims(db).items()
    assert fresh["task_ids"] == [again.json()["task"]["id"]]


@pytest.mark.parametrize("kind", ["task", "batch"])
def test_the_fingerprint_never_holds_a_credential_value(kind):
    from swarm_api import idempotency

    assert idempotency.KEY_WINDOW == KEY_WINDOW
    value = secrets.token_hex(16)
    body = {"input": {}, "mcp_credentials": {"srv": {"value": value, "expires_in": 60}}}
    other = {"input": {}, "mcp_credentials": {"srv": {"value": secrets.token_hex(16),
                                                      "expires_in": 60}}}
    assert idempotency.fingerprint(kind, body) == idempotency.fingerprint(kind, other)
