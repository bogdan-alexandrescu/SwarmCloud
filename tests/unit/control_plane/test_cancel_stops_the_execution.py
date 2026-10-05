"""The cancel route asks the backend to stop the execution, once (#627).

A cancel used to be a flag that the worker read at its next poll, or that the
reconciler acted on once the worker went silent; some executions ran 7-13 h
past a cancel (the 2026-10-05 history analysis). The route now names the
task's execution and asks its backend to stop it -- Cloud Run's cancel or a
GKE Job delete -- straight away. Pinned here:

  * the first cancel of a running task asks the backend ONCE, for the
    execution its attempt recorded; a second cancel asks nothing;
  * a task with no execution (QUEUED), or whose attempt has ended, asks
    nothing; a workflow cancel asks for each running step;
  * the route releases nothing itself: the lease and pools are untouched;
  * the REST canceller refuses any name that is not this platform's, never
    deletes a GKE Job it did not create or that is another tenant's, and
    calls the endpoints the reconciler calls.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from swarm_api.deps import AppContext, _execution_canceller
from swarm_api.executioncancel import (
    ExecutionTarget,
    NoExecutionCanceller,
    RestExecutionCanceller,
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


# -- the REST canceller ------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _Session:
    def __init__(self, *, post=200, get=200, delete=200, labels=None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._post, self._get, self._delete = post, get, delete
        self._labels = labels if labels is not None else {
            "managed-by": "swarm-scheduler", "swarm-tenant": "eng"}

    def post(self, url: str, **kw: Any) -> _Response:
        self.calls.append(("POST", url, kw))
        return _Response(self._post)

    def get(self, url: str, **kw: Any) -> _Response:
        self.calls.append(("GET", url, kw))
        return _Response(self._get, {"metadata": {"labels": self._labels}})

    def delete(self, url: str, **kw: Any) -> _Response:
        self.calls.append(("DELETE", url, kw))
        return _Response(self._delete)


def _rest(session: _Session, **kw: Any) -> RestExecutionCanceller:
    return RestExecutionCanceller(project_id=PROJECT, region=REGION, session=session, **kw)


def _run_target(name: str) -> ExecutionTarget:
    return ExecutionTarget("eng", "task_1", "att_1", "CLOUD_RUN_JOB", name)


def _gke_target(name: str, tenant: str = "eng") -> ExecutionTarget:
    return ExecutionTarget(tenant, "task_1", "att_1", "GKE_AUTOPILOT", name)


RUN_NAME = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-job-eng-mock/executions/swarm-job-eng-mock-abc12"
CA = base64.b64encode(b"not a real certificate").decode()


def test_cloud_run_cancel_posts_to_the_execution():
    session = _Session()
    assert _rest(session).cancel(_run_target(RUN_NAME)) == "requested"
    assert [(m, u) for m, u, _ in session.calls] == [
        ("POST", f"https://run.googleapis.com/v2/{RUN_NAME}:cancel")]


@pytest.mark.parametrize("status,outcome", [(404, "gone"), (400, "finished"), (403, "http_403")])
def test_cloud_run_answers_are_named(status, outcome):
    assert _rest(_Session(post=status)).cancel(_run_target(RUN_NAME)) == outcome


@pytest.mark.parametrize("name", [
    RUN_NAME.replace(PROJECT, "someone-elses-project"),
    RUN_NAME.replace("jobs/swarm-job", "jobs/their-job"),
    RUN_NAME.replace(REGION, "europe-west1"),
    "../" + RUN_NAME,
])
def test_a_name_that_is_not_this_platforms_execution_is_refused_without_a_call(name):
    session = _Session()
    assert _rest(session).cancel(_run_target(name)) == "refused_name"
    assert session.calls == []


def test_the_dispatchers_placeholder_is_not_cancelled():
    session = _Session()
    name = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-job-eng-mock/executions/pending-att_1"
    assert _rest(session).cancel(_run_target(name)) == "placeholder"
    assert session.calls == []


def test_gke_without_the_clusters_endpoint_asks_nothing():
    session = _Session()
    assert _rest(session).cancel(_gke_target("swarm-tenant-eng/swarm-job-1")) == "gke_unconfigured"
    assert session.calls == []


def test_gke_reads_the_job_then_deletes_it_in_the_background():
    session = _Session()
    canceller = _rest(session, gke_endpoint="10.0.0.2", gke_ca_cert_b64=CA)

    assert canceller.cancel(_gke_target("swarm-tenant-eng/swarm-job-1")) == "requested"

    url = "https://10.0.0.2/apis/batch/v1/namespaces/swarm-tenant-eng/jobs/swarm-job-1"
    assert [(m, u) for m, u, _ in session.calls] == [("GET", url), ("DELETE", url)]
    assert session.calls[1][2]["json"] == {"propagationPolicy": "Background",
                                          "gracePeriodSeconds": 30}


@pytest.mark.parametrize("labels,outcome", [
    ({"managed-by": "swarm-terraform", "swarm-tenant": "eng"}, "refused_unmanaged"),
    ({"swarm-tenant": "eng"}, "refused_unmanaged"),
    ({"managed-by": "swarm-scheduler", "swarm-tenant": "research"}, "refused_tenant"),
])
def test_gke_never_deletes_a_job_it_did_not_create_or_of_another_tenant(labels, outcome):
    session = _Session(labels=labels)
    canceller = _rest(session, gke_endpoint="10.0.0.2", gke_ca_cert_b64=CA)

    assert canceller.cancel(_gke_target("swarm-tenant-eng/swarm-job-1")) == outcome
    assert [m for m, _u, _ in session.calls] == ["GET"]


@pytest.mark.parametrize("name", ["kube-system/swarm-job-1", "agents-staging/x", "swarm-job-1"])
def test_gke_refuses_a_namespace_outside_the_tenant_prefix(name):
    session = _Session()
    canceller = _rest(session, gke_endpoint="10.0.0.2", gke_ca_cert_b64=CA)
    assert canceller.cancel(_gke_target(name)) == "refused_name"
    assert session.calls == []


def test_a_canceller_that_raises_is_an_outcome_not_an_error():
    class Broken(_Session):
        def post(self, url: str, **kw: Any) -> _Response:
            raise ConnectionError("no route")

    assert _rest(Broken()).cancel(_run_target(RUN_NAME)) == "error:ConnectionError"


def test_the_setting_chooses_the_canceller():
    assert isinstance(_execution_canceller(api_settings()), NoExecutionCanceller)
    assert isinstance(
        _execution_canceller(api_settings(execution_cancel_enabled=True)),
        RestExecutionCanceller,
    )


def test_a_deployment_turns_it_on_by_default(monkeypatch):
    from swarm_api.settings import ApiSettings

    monkeypatch.delenv("EXECUTION_CANCEL_ENABLED", raising=False)
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    try:
        settings = ApiSettings.from_env()
    except ValueError as exc:  # pragma: no cover - an environment this file did not set
        pytest.skip(f"ApiSettings.from_env needs more environment here: {exc}")
    assert settings.execution_cancel_enabled is True
