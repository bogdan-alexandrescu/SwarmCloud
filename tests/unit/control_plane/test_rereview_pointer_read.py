"""A scheduler-ended re-review's verdict reads through swarm-api as its review's (request 157).

The scheduler ends a re-review whose gate is shut with a manifest entry that
points at the review's object (`republished_from`). `inspect.read_artifact` --
which issueci reads an issue run's verdict through -- follows it under the
worker's checks and serves the bytes from the REVIEW's own prefix.
Offline: FakeFirestore and the in-memory object reader.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"
VERDICT = json.dumps({"verdict": "MERGE"})


def _seed(db, objects, *, mutate=None) -> None:
    seed_tenant(db, "eng")
    review = seed_task(db, task_id="task_review", tenant_id="eng", state="SUCCEEDED")
    key = "tenants/eng/tasks/task_review/attempts/att_1/artifacts/verdict.json"
    objects.put(key, VERDICT.encode())
    review["result_summary"] = {
        "artifacts": [{"name": "verdict.json", "bytes": len(VERDICT),
                       "uri": f"gs://{BUCKET}/{key}"}],
        "logs": {},
    }
    review["completed_at"] = datetime.now(timezone.utc)
    rerun = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED",
                      depends_on=("task_review",))
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
