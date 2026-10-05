"""The reconciler stops a cancelled task's execution when swarm-api asks (#627).

swarm-api holds no compute permission, so the cancel route publishes the
attempt (ids only) and Pub/Sub pushes it to `/stop-execution` here, where the
stop permissions and backend clients already are. Pinned here:

  S-1  A cancelled task's live execution is stopped ONCE, by the backend's own
       `terminate`, after a by-name `probe`; a redelivery of the same message
       stops nothing more (the execution is over by then).
  S-2  The message is trusted for ids only: a task of another tenant, a task
       not cancelled, an attempt of another task, an ended attempt, or an
       execution the backend says belongs to another task stops nothing.
  S-3  Something the backend cannot read or will not call ours (the probe's
       UNREADABLE: outside the prefix, unmanaged, a placeholder) is not
       stopped; the pass remains the backstop.
  S-4  Nothing is released or written: no lease, pool or task change.
  S-5  The route acks every decision and a malformed message, and answers 503
       only when a read or the stop raised, so Pub/Sub retries just that.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fakes import FakeFirestore
from fastapi.testclient import TestClient

from reconciler.backends import Probe, ProbeOutcome
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.service import create_app
from reconciler.stopexec import ExecutionStopper, parse_push
from reconciler.store import ControlStore
from swarm_common.models import utcnow

TENANT = "eng"
TASK = "task_1"
ATTEMPT = "att_1"
EXECUTION = "projects/p/locations/us-central1/jobs/swarm-job-eng-mock/executions/swarm-job-eng-mock-x1"


class StopBackend:
    """A backend whose execution runs until `terminate` is called once."""

    name = "CLOUD_RUN_JOB"

    def __init__(self, *, outcome: ProbeOutcome = ProbeOutcome.ACTIVE, view: Any = None) -> None:
        self.outcome = outcome
        self.view = view or ExecutionView(
            name=EXECUTION,
            backend=self.name,
            phase=ExecutionPhase.RUNNING,
            created_at=utcnow(),
            task_id=TASK,
            attempt_id=ATTEMPT,
            tenant_id=TENANT,
        )
        self.probed: list[str] = []
        self.terminated: list[ExecutionView] = []

    def probe(self, execution_name: str) -> Probe:
        self.probed.append(execution_name)
        return Probe(self.outcome, execution=self.view)

    def terminate(self, execution: ExecutionView) -> bool:
        self.terminated.append(execution)
        # Cancelled: a second probe reads it as over.
        self.outcome = ProbeOutcome.FINISHED
        return True


def seed(db: FakeFirestore, **task_overrides: Any) -> None:
    db.seed(
        f"tasks/{TASK}",
        {"tenant_id": TENANT, "state": "RUNNING", "cancel_requested": True,
         "current_lease_id": "lease_1", **task_overrides},
    )
    db.seed(
        f"attempts/{ATTEMPT}",
        {"task_id": TASK, "tenant_id": TENANT, "backend": "CLOUD_RUN_JOB",
         "execution_name": EXECUTION, "completed_at": None},
    )
    db.seed("leases/lease_1", {"task_id": TASK, "attempt_id": ATTEMPT, "released_at": None})


def stopper(db: FakeFirestore, backend: StopBackend) -> ExecutionStopper:
    logger = build_logger()
    return ExecutionStopper(
        store=ControlStore(db, logger=logger), backends={backend.name: backend}, logger=logger
    )


def test_a_cancelled_tasks_execution_is_stopped_once(db):
    """S-1, S-4."""
    seed(db)
    before = {path: dict(doc) for path, doc in db.documents.items()}
    backend = StopBackend()
    stop = stopper(db, backend)

    first = stop.stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT)
    again = stop.stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT)

    assert first == "stopped"
    assert again == "finished"
    assert [view.name for view in backend.terminated] == [EXECUTION]
    assert backend.probed == [EXECUTION, EXECUTION]
    # Nothing released, nothing written: the worker or the pass ends the task.
    assert db.documents == before
    assert db.writes == []


@pytest.mark.parametrize(
    "task_overrides,attempt_overrides,outcome",
    [
        ({"tenant_id": "research"}, {}, "refused_tenant"),
        ({"cancel_requested": False}, {}, "not_cancelled"),
        ({}, {"task_id": "task_other"}, "refused_attempt"),
        ({}, {"tenant_id": "research"}, "refused_tenant"),
        ({}, {"completed_at": utcnow()}, "attempt_ended"),
        ({}, {"execution_name": ""}, "no_execution"),
        ({}, {"backend": "SOMETHING_ELSE"}, "backend_unavailable"),
    ],
)
def test_the_message_is_trusted_for_ids_only(db, task_overrides, attempt_overrides, outcome):
    """S-2."""
    seed(db, **task_overrides)
    db.documents[f"attempts/{ATTEMPT}"].update(attempt_overrides)
    backend = StopBackend()

    assert stopper(db, backend).stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT) == outcome
    assert backend.terminated == []


def test_an_unknown_task_or_attempt_stops_nothing(db):
    backend = StopBackend()
    assert stopper(db, backend).stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT) == "no_task"
    seed(db)
    del db.documents[f"attempts/{ATTEMPT}"]
    assert stopper(db, backend).stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT) == "no_attempt"
    assert backend.probed == [] and backend.terminated == []


@pytest.mark.parametrize("field,value", [("task_id", "task_other"), ("attempt_id", "att_other"),
                                         ("tenant_id", "research")])
def test_an_execution_the_backend_says_is_someone_elses_is_not_stopped(db, field, value):
    """S-2: the attempt's recorded name, read back, must still be this attempt."""
    seed(db)
    backend = StopBackend()
    backend.view = ExecutionView(**{**backend.view.__dict__, field: value})

    outcome = stopper(db, backend).stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT)

    assert outcome in {"refused_mismatch", "refused_tenant"}
    assert backend.terminated == []


@pytest.mark.parametrize("probe,outcome", [(ProbeOutcome.UNREADABLE, "unreadable"),
                                           (ProbeOutcome.ABSENT, "gone"),
                                           (ProbeOutcome.FINISHED, "finished")])
def test_what_the_backend_cannot_call_ours_or_running_is_not_stopped(db, probe, outcome):
    """S-3."""
    seed(db)
    backend = StopBackend(outcome=probe)
    assert stopper(db, backend).stop(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT) == outcome
    assert backend.terminated == []


# -- the route ---------------------------------------------------------------


def push(body: dict[str, Any]) -> dict[str, Any]:
    data = base64.b64encode(json.dumps(body).encode()).decode()
    return {"message": {"data": data, "attributes": {}, "messageId": "1"}, "subscription": "s"}


IDS = {"tenant_id": TENANT, "task_id": TASK, "attempt_id": ATTEMPT}


def test_the_route_stops_the_execution_and_acks(db, monkeypatch):
    """S-5."""
    monkeypatch.delenv("RECONCILER_ALLOWED_INVOKERS", raising=False)
    seed(db)
    backend = StopBackend()
    client = TestClient(create_app(object(), logger=build_logger(), stopper=stopper(db, backend)))

    first = client.post("/stop-execution", json=push(IDS))
    redelivered = client.post("/stop-execution", json=push(IDS))

    assert (first.status_code, first.json()) == (200, {"status": "stopped"})
    assert (redelivered.status_code, redelivered.json()) == (200, {"status": "finished"})
    assert len(backend.terminated) == 1


@pytest.mark.parametrize("envelope", [{}, {"message": {}}, push({"task_id": TASK}),
                                      {"message": {"data": "not base64!"}}])
def test_a_malformed_message_is_acked_and_stops_nothing(db, monkeypatch, envelope):
    """S-5: a malformed message is malformed on every retry."""
    monkeypatch.delenv("RECONCILER_ALLOWED_INVOKERS", raising=False)
    backend = StopBackend()
    client = TestClient(create_app(object(), logger=build_logger(), stopper=stopper(db, backend)))

    r = client.post("/stop-execution", json=envelope)

    assert (r.status_code, r.json()) == (200, {"status": "malformed"})
    assert backend.probed == []


def test_a_stop_that_raised_is_retried(db, monkeypatch):
    """S-5: only a failure is a 503."""
    monkeypatch.delenv("RECONCILER_ALLOWED_INVOKERS", raising=False)
    seed(db)

    class Broken(StopBackend):
        def terminate(self, execution: ExecutionView) -> bool:
            raise ConnectionError("no route")

    client = TestClient(create_app(object(), logger=build_logger(), stopper=stopper(db, Broken())))

    r = client.post("/stop-execution", json=push(IDS))

    assert (r.status_code, r.json()) == (503, {"status": "retry"})


def test_the_route_needs_an_allowed_invoker(db, monkeypatch):
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "tick@p.iam.gserviceaccount.com")
    backend = StopBackend()
    client = TestClient(create_app(object(), logger=build_logger(), stopper=stopper(db, backend)))

    r = client.post("/stop-execution", json=push(IDS))

    assert r.status_code == 401
    assert backend.probed == []


def test_parse_push_reads_the_attributes_when_there_is_no_body():
    envelope = {"message": {"attributes": dict(IDS)}}
    assert parse_push(envelope) == IDS
