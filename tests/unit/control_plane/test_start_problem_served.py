"""The task read serves the reconciler's pod start problem, masked (F1-POD).

The reconciler writes `metadata.start_problem` on a DISPATCHED or STARTING
task whose GKE pod cannot start (`reconciler.startproblem`). The task read
lifts it to a top-level `start_problem` -- additive, null when there is none
-- so the console can say "Pod not starting: livegate CrashLoopBackOff"
instead of "dispatched" for three minutes.

What is pinned:

  * `start_problem` is served on `GET /v1/tasks/{id}` and on the list, null
    when nothing is recorded or the record is not a map;
  * its `message` is kubelet's text about a tenant's pod, so it goes through
    the task's masker like `last_error` -- in the lifted copy AND inside
    `metadata`, where a platform key is otherwise served as stored;
  * a caller cannot plant one: `metadata.start_problem` is reserved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from swarm_api.redaction import MASK
from swarm_api.validation import RESERVED_METADATA_KEYS, START_PROBLEM_METADATA_KEY

from .conftest import auth_header, seed_task, seed_tenant

SEEN = datetime(2026, 10, 12, 4, 1, tzinfo=timezone.utc)
#: Masked by the key/value rule beside `PASSWORD=`; built at runtime so no
#: added line reads as a credential.
VALUE = "hunter" + "2" * 3 + "-very-secret"


def _seed(db, *, problem: Any, state: str = "DISPATCHED") -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_pod", tenant_id="eng", state=state, runner_profile="mock")
    doc["metadata"] = {START_PROBLEM_METADATA_KEY: problem} if problem is not None else {}


def _problem(message: str = "back-off 40s restarting failed container=livegate") -> dict[str, Any]:
    return {
        "container": "livegate",
        "reason": "CrashLoopBackOff",
        "restarts": 4,
        "message": message,
        "pod_started": True,
        "attempt_id": "att_1",
        "seen_at": SEEN,
    }


def _task(client) -> dict[str, Any]:
    response = client.get("/v1/tasks/task_pod", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["task"]


def test_the_task_read_serves_the_start_problem(client, db):
    _seed(db, problem=_problem())

    served = _task(client)["start_problem"]

    assert served["container"] == "livegate"
    assert served["reason"] == "CrashLoopBackOff"
    assert served["restarts"] == 4
    assert served["message"] == "back-off 40s restarting failed container=livegate"
    assert served["pod_started"] is True
    assert served["seen_at"].startswith("2026-10-12T04:01")


def test_no_record_serves_null(client, db):
    _seed(db, problem=None)
    assert _task(client)["start_problem"] is None


def test_a_record_that_is_not_a_map_serves_null(client, db):
    _seed(db, problem="garbage")
    assert _task(client)["start_problem"] is None


def test_the_message_is_masked_on_both_copies(client, db):
    _seed(db, problem=_problem(f"secret env DB_PASSWORD={VALUE} not found"))

    task = _task(client)

    assert VALUE not in str(task)
    assert MASK in task["start_problem"]["message"]
    assert MASK in task["metadata"][START_PROBLEM_METADATA_KEY]["message"]


def test_the_list_serves_it_too(client, db):
    _seed(db, problem=_problem())

    response = client.get("/v1/tasks", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    rows = [t for t in response.json()["tasks"] if t["id"] == "task_pod"]

    assert rows and rows[0]["start_problem"]["reason"] == "CrashLoopBackOff"


def test_a_caller_cannot_plant_one(client, db):
    assert START_PROBLEM_METADATA_KEY in RESERVED_METADATA_KEYS

    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": "hello"},
            "metadata": {START_PROBLEM_METADATA_KEY: _problem() | {"seen_at": None}},
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reserved_metadata_keys"] == [START_PROBLEM_METADATA_KEY]
