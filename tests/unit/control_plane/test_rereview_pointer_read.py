"""A scheduler-ended re-review's verdict reads through swarm-api as its review's (request 157).

The scheduler ends a re-review whose gate is shut with a manifest entry that
points at the review's object (`republished_from`). `inspect.read_artifact` --
which issueci reads an issue run's verdict through -- follows it under the
worker's checks and serves the bytes from the REVIEW's own prefix. A pointer
to anything else -- another tenant, another workflow, a task that is not the
review the re-review republishes, a missing object, no usable size -- is the
404 any unreadable artifact gets (the owner, 2026-11-24). The listing and the
raw route agree with the content route.
Offline: FakeFirestore and the in-memory object reader.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"
VERDICT = json.dumps({"verdict": "MERGE"})


def _seed(db, objects, *, mutate=None, review_workflow="wf_1") -> None:
    seed_tenant(db, "eng")
    review = seed_task(db, task_id="task_review", tenant_id="eng", state="SUCCEEDED",
                       workflow_id=review_workflow)
    key = "tenants/eng/tasks/task_review/attempts/att_1/artifacts/verdict.json"
    objects.put(key, VERDICT.encode())
    review["result_summary"] = {
        "artifacts": [{"name": "verdict.json", "bytes": len(VERDICT),
                       "uri": f"gs://{BUCKET}/{key}"}],
        "logs": {},
    }
    review["completed_at"] = datetime.now(timezone.utc)
    rerun = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED",
                      depends_on=("task_review",), workflow_id="wf_1")
    rerun["metadata"] = {"dispatch": {"verdict_gate": {"task_id": "task_review"}}}
    rerun["result_summary"] = {
        "published_by": "control_plane",
        "verdict_gate": {"task_id": "task_review", "file": "verdict.json",
                         "agent_ran": False},
        "artifacts": [{"name": "verdict.json", "written_by": "control_plane",
                       "republished_from": "task_review", "bytes": len(VERDICT),
                       "uri": f"gs://{BUCKET}/{key}"}],
        "logs": {},
    }
    rerun["completed_at"] = datetime.now(timezone.utc)
    if mutate:
        mutate(rerun)


def _get(client) -> Any:
    return client.get("/v1/tasks/task_a/artifacts/content",
                      params={"name": "verdict.json"}, headers=auth_header("alice"))


def test_the_re_review_reads_as_its_reviews_verdict(client, db, objects):
    _seed(db, objects)
    response = _get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert json.loads(body["content"]) == {"verdict": "MERGE"}


def test_a_pointer_to_a_task_that_is_not_a_parent_is_not_followed(client, db, objects):
    _seed(db, objects, mutate=lambda doc: doc.update(depends_on=[]))
    assert _get(client).status_code != 200


def test_a_pointer_without_the_control_plane_record_is_not_followed(client, db, objects):
    def mutate(doc):
        doc["result_summary"]["verdict_gate"]["agent_ran"] = True
    _seed(db, objects, mutate=mutate)
    assert _get(client).status_code != 200


def _not_found(response) -> None:
    assert response.status_code == 404, response.text


def test_a_pointer_whose_uri_lies_in_another_tenant_is_not_found(client, db, objects):
    seed_tenant(db, "research")
    other = "tenants/research/tasks/task_review/attempts/att_1/artifacts/verdict.json"
    objects.put(other, json.dumps({"verdict": "REWORK"}).encode())

    def mutate(doc):
        doc["result_summary"]["artifacts"][0]["uri"] = f"gs://{BUCKET}/{other}"
    _seed(db, objects, mutate=mutate)
    _not_found(_get(client))


def test_a_pointer_to_a_review_in_another_tenant_is_not_found(client, db, objects):
    _seed(db, objects)
    db.docs["tasks/task_review"]["tenant_id"] = "research"
    _not_found(_get(client))


def test_a_pointer_into_another_workflow_is_not_found(client, db, objects):
    _seed(db, objects, review_workflow="wf_2")
    _not_found(_get(client))


def test_a_pointer_to_a_task_that_is_not_the_republished_review_is_not_found(
        client, db, objects):
    def mutate(doc):
        seed_task(db, task_id="task_other", tenant_id="eng", state="SUCCEEDED",
                  workflow_id="wf_1")
        doc["depends_on"] = ["task_review", "task_other"]
        doc["result_summary"]["artifacts"][0]["republished_from"] = "task_other"
    _seed(db, objects, mutate=mutate)
    _not_found(_get(client))


def test_a_pointer_that_is_not_a_parent_is_not_found(client, db, objects):
    _seed(db, objects, mutate=lambda doc: doc.update(depends_on=[]))
    _not_found(_get(client))


def test_a_pointer_to_a_missing_object_is_not_found(client, db, objects):
    _seed(db, objects)
    del objects.objects["tenants/eng/tasks/task_review/attempts/att_1/artifacts/verdict.json"]
    _not_found(_get(client))


def test_a_pointer_without_a_usable_size_is_not_found(client, db, objects):
    def mutate(doc):
        doc["result_summary"]["artifacts"][0]["bytes"] = None
    _seed(db, objects, mutate=mutate)
    _not_found(_get(client))


def test_a_pointer_over_the_size_cap_is_not_found(client, db, objects):
    def mutate(doc):
        doc["result_summary"]["artifacts"][0]["bytes"] = 1 << 40
    _seed(db, objects, mutate=mutate)
    _not_found(_get(client))


def test_the_pointed_to_verdict_is_redacted_like_any_read(client, db, objects):
    secret = "ghp_" + secrets.token_hex(18)
    text = json.dumps({"verdict": "MERGE", "reason": f"token {secret}"})
    key = "tenants/eng/tasks/task_review/attempts/att_1/artifacts/verdict.json"

    def mutate(doc):
        objects.put(key, text.encode())
        for row in (doc["result_summary"]["artifacts"][0],
                    db.docs["tasks/task_review"]["result_summary"]["artifacts"][0]):
            row["bytes"] = len(text)
    _seed(db, objects, mutate=mutate)
    response = _get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert secret not in body["content"]
    assert body["redacted"] is True


def test_the_listing_shows_the_pointer_entry_as_readable(client, db, objects):
    _seed(db, objects)
    response = client.get("/v1/tasks/task_a/artifacts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["artifacts"] if r["name"] == "verdict.json")
    assert row["attempt_id"] == "att_1"
    assert row["republished_from"] == "task_review"


def test_the_listing_does_not_offer_a_pointer_into_another_workflow(client, db, objects):
    _seed(db, objects, review_workflow="wf_2")
    response = client.get("/v1/tasks/task_a/artifacts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["artifacts"] if r["name"] == "verdict.json")
    assert row["attempt_id"] is None


def test_the_raw_route_serves_the_pointed_to_bytes(client, db, objects):
    _seed(db, objects)
    response = client.get("/v1/tasks/task_a/artifacts/raw",
                          params={"name": "verdict.json"}, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert json.loads(response.content) == {"verdict": "MERGE"}


def test_the_raw_route_refuses_a_pointer_into_another_workflow(client, db, objects):
    _seed(db, objects, review_workflow="wf_2")
    response = client.get("/v1/tasks/task_a/artifacts/raw",
                          params={"name": "verdict.json"}, headers=auth_header("alice"))
    _not_found(response)


def test_an_ordinary_artifact_reads_as_before(client, db, objects):
    seed_tenant(db, "eng")
    task = seed_task(db, task_id="task_plain", tenant_id="eng", state="SUCCEEDED",
                     workflow_id="wf_1")
    key = "tenants/eng/tasks/task_plain/attempts/att_9/artifacts/notes.txt"
    objects.put(key, b"hello\n")
    task["result_summary"] = {
        "artifacts": [{"name": "notes.txt", "bytes": 6, "uri": f"gs://{BUCKET}/{key}"}],
        "logs": {},
    }
    task["completed_at"] = datetime.now(timezone.utc)
    response = client.get("/v1/tasks/task_plain/artifacts/content",
                          params={"name": "notes.txt"}, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["content"] == "hello\n"
    assert body["attempt_id"] == "att_9"
