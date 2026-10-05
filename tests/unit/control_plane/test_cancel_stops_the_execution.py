"""The cancel route asks the backend to stop the execution, once (#627).

A cancel used to be a flag that the worker read at its next poll, or that the
reconciler acted on once the worker went silent; some executions ran 7-13 h
past a cancel (the 2026-10-05 history analysis). The route now names the
task's attempt and asks the reconciler to stop its execution -- Cloud Run's
cancel or a GKE Job delete -- straight away. Pinned here:

  * the first cancel of a running task asks the backend ONCE, for the
    execution its attempt recorded; a second cancel asks nothing;
  * a task with no execution (QUEUED), or whose attempt has ended, asks
    nothing; a workflow cancel asks for each running step;
  * the route releases nothing itself: the lease and pools are untouched;
  * the request is published for the reconciler, which holds the stop
    permissions swarm-api does not, and carries ids only, never a resource
    name (the reconciler's side: tests/unit/reconciler/test_stop_execution.py).
"""

from __future__ import annotations

import json

import pytest

from swarm_api.deps import AppContext, _execution_canceller
from swarm_api.executioncancel import (
    ExecutionTarget,
    NoExecutionCanceller,
    PubSubExecutionCanceller,
)

from .conftest import api_settings, auth_header, seed_task, seed_tenant
from .test_cancel_without_a_worker import active_pools, dispatched, lease_of, worker_started

PROJECT = "proj-x"
REGION = "us-central1"


class Recorder:
    def __init__(self) -> None:
        self.targets: list[ExecutionTarget] = []

    def cancel(self, target: ExecutionTarget) -> str:
        self.targets.append(target)
        return "requested"


@pytest.fixture
def recorder(api_context: AppContext) -> Recorder:
    rec = Recorder()
    api_context.executions = rec
    return rec


def running(client, db, make_scheduler) -> str:
    task_id = dispatched(client, db, make_scheduler)
    worker_started(db, task_id)
    db.docs[f"tasks/{task_id}"]["state"] = "RUNNING"
    return task_id


def test_the_first_cancel_asks_the_backend_once_and_a_second_asks_nothing(
    client, db, make_scheduler, recorder
):
    task_id = running(client, db, make_scheduler)
    lease_id, lease = lease_of(db, task_id)
    attempt = db.docs[f"attempts/{lease['attempt_id']}"]
    pools_before = active_pools(db)

    first = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))
    second = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["execution_cancel_requested"] is True
    assert second.json()["execution_cancel_requested"] is False
    assert len(recorder.targets) == 1
    target = recorder.targets[0]
    assert target.task_id == task_id
    assert target.attempt_id == lease["attempt_id"]
    assert target.execution_name == attempt["execution_name"]
    assert target.backend == attempt["backend"]
    assert target.tenant_id == db.docs[f"tasks/{task_id}"]["tenant_id"]
    # Nothing released from here: the worker, SIGTERMed, ends it.
    assert db.docs[f"tasks/{task_id}"]["state"] == "RUNNING"
    assert db.docs[f"leases/{lease_id}"].get("released_at") is None
    assert active_pools(db) == pools_before


def test_a_task_with_no_execution_asks_nothing(client, db, recorder):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_idle", tenant_id="eng", state="QUEUED")

    body = client.post("/v1/tasks/task_idle/cancel", headers=auth_header("alice")).json()

    assert body["execution_cancel_requested"] is False
    assert recorder.targets == []


def test_an_attempt_that_has_ended_asks_nothing(client, db, make_scheduler, recorder):
    task_id = running(client, db, make_scheduler)
    _lease_id, lease = lease_of(db, task_id)
    db.docs[f"attempts/{lease['attempt_id']}"]["completed_at"] = lease.get("created_at") or 1

    client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_header("alice"))

    assert recorder.targets == []


def test_a_workflow_cancel_asks_for_each_running_step(client, db, make_scheduler, recorder):
    from .conftest import seed_pool

    seed_pool(db, "global", hard_limit=10)
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"steps": [{"step_id": "a", "runner_profile": "mock"},
                        {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]}]},
    )
    assert created.status_code in (200, 201), created.text
    workflow = created.json()["workflow"]
    workflow_id = workflow["workflow_id"]
    steps = {step["step_id"]: step["task_id"] for step in workflow["steps"]}
    make_scheduler().drain()
    running_id = steps["a"]
    running_doc = db.docs[f"tasks/{running_id}"]
    assert running_doc["state"] == "DISPATCHED", "nothing was admitted"

    r = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice"))

    assert r.status_code == 200, r.text
    assert [t.task_id for t in recorder.targets] == [running_id]
    assert "targets" not in r.json()


def test_a_child_cancelled_with_its_parent_is_stopped_too(client, db, make_scheduler, recorder):
    """A child's execution is asked to stop with its parent's, once."""
    child_id = running(client, db, make_scheduler)
    _lease_id, lease = lease_of(db, child_id)
    tenant = db.docs[f"tasks/{child_id}"]["tenant_id"]
    seed_task(db, task_id="task_parent", tenant_id=tenant, state="QUEUED")
    db.docs[f"tasks/{child_id}"]["parent_task_id"] = "task_parent"

    first = client.post("/v1/tasks/task_parent/cancel", headers=auth_header("alice"))

    assert first.status_code == 200, first.text
    assert first.json()["children_cancelled"] == 1
    assert [(t.task_id, t.attempt_id) for t in recorder.targets] == [
        (child_id, lease["attempt_id"])
    ]
    assert db.docs[f"tasks/{child_id}"]["cancel_requested"] is True
    # Cancelling the child itself now asks nothing more: it was already asked.
    client.post(f"/v1/tasks/{child_id}/cancel", headers=auth_header("alice"))
    assert len(recorder.targets) == 1


# -- the publisher -----------------------------------------------------------


class _Future:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def result(self, timeout: float | None = None) -> str:
        if self._error is not None:
            raise self._error
        return "msg-1"


class _Publisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []
        self._error = error

    def publish(self, topic: str, data: bytes, **attributes: str) -> _Future:
        self.calls.append((topic, data, attributes))
        return _Future(self._error)


RUN_NAME = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-job-eng-mock/executions/swarm-job-eng-mock-abc12"
TOPIC = "projects/proj-x/topics/swarm-execution-cancel"


def _target() -> ExecutionTarget:
    return ExecutionTarget("eng", "task_1", "att_1", "CLOUD_RUN_JOB", RUN_NAME)


def test_the_stop_request_carries_ids_and_never_a_resource_name():
    """The reconciler stops what the ATTEMPT recorded; nothing here can name
    a resource for it, least of all one of the other team's."""
    publisher = _Publisher()

    assert PubSubExecutionCanceller(TOPIC, publisher=publisher).cancel(_target()) == "requested"

    [(topic, data, attributes)] = publisher.calls
    assert topic == TOPIC
    sent = json.loads(data)
    assert sent == {"tenant_id": "eng", "task_id": "task_1", "attempt_id": "att_1"}
    assert attributes == sent
    assert RUN_NAME not in data.decode() and "CLOUD_RUN_JOB" not in data.decode()


def test_a_publish_that_fails_is_an_outcome_not_an_error():
    publisher = _Publisher(error=TimeoutError("no answer"))
    assert PubSubExecutionCanceller(TOPIC, publisher=publisher).cancel(_target()) == (
        "error:TimeoutError"
    )


def test_the_setting_chooses_the_canceller():
    assert isinstance(_execution_canceller(api_settings()), NoExecutionCanceller)
    # On, but no topic: nothing to publish to, so nothing is asked.
    assert isinstance(
        _execution_canceller(api_settings(execution_cancel_enabled=True)),
        NoExecutionCanceller,
    )
    assert isinstance(
        _execution_canceller(
            api_settings(execution_cancel_enabled=True, execution_cancel_topic=TOPIC)
        ),
        PubSubExecutionCanceller,
    )


def test_a_deployment_turns_it_on_by_default(monkeypatch):
    from swarm_api.settings import ApiSettings

    monkeypatch.delenv("EXECUTION_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("EXECUTION_CANCEL_TOPIC", "swarm-execution-cancel")
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    try:
        settings = ApiSettings.from_env()
    except ValueError as exc:  # pragma: no cover - an environment this file did not set
        pytest.skip(f"ApiSettings.from_env needs more environment here: {exc}")
    assert settings.execution_cancel_enabled is True
    assert settings.execution_cancel_topic == "swarm-execution-cancel"
