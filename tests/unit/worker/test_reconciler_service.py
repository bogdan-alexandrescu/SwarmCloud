"""The reconciler's HTTP surface and its termination confirmation.

`reconciler/service.py` had 0% coverage: `create_app`, both probes, the invoker
allowlist and the pass lock were asserted in prose and verified by nothing. Its
own docstring calls the allowlist "defence in depth for a service whose job is to
terminate other people's work", and explains that two concurrent passes would
both see the same stale lease, both release it, and the second release is the one
that decrements a pool below its true active count.

`CloudRunBackend.terminate` had none either, and the unit tests that exercise the
repair ordering use `FakeBackend`, whose `terminate` returns a scripted bool --
so the real implementation's `return True if cancelled else bool(result)` was
never run. A protobuf is truthy whenever it carries any field, so that fallback
reported success for effectively any completed operation, including the exact
"returned a different execution" case the name comparison exists to catch, and
`repair.py`'s `if not outcome.terminated` became unreachable.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_backend_identity import JOB, PROJECT, REGION, FakeExecutionsClient, FakeJobsClient

from reconciler.backends import CloudRunBackend
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import ReconcileReport
from reconciler.service import _PASS_LOCK, _verify_invoker, create_app
from swarm_common.models import utcnow


class _StubReconciler:
    """A reconciler whose pass is observable and can be made to block."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.passes = 0
        self._gate = gate

    def run_once(self) -> ReconcileReport:
        self.passes += 1
        if self._gate is not None:
            self._gate.wait(timeout=5)
        report = ReconcileReport(started_at=utcnow())
        report.finished_at = utcnow()
        return report


def _client(reconciler=None) -> TestClient:
    return TestClient(create_app(reconciler or _StubReconciler(), logger=build_logger()))


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def test_the_probes_need_no_token_and_build_no_client():
    """A readiness probe that authenticated, or that built a Firestore client,
    would make the container fail to come up for reasons unrelated to its job."""
    client = _client()
    health = client.get("/healthz")
    ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["service"] == "swarm-reconciler"
    assert ready.status_code == 200
    assert ready.json()["last_pass_at"] is None


def test_readyz_reports_the_last_pass_once_one_has_run():
    reconciler = _StubReconciler()
    client = _client(reconciler)

    assert client.post("/reconcile").status_code == 200
    body = client.get("/readyz").json()

    assert body["last_pass_at"] is not None
    assert body["last_pass_findings"] == 0
    assert reconciler.passes == 1


def test_create_app_builds_no_firestore_client_at_import_or_construction():
    """`create_app()` with no reconciler must not authenticate to anything --
    otherwise every test that imports this module needs credentials, and the
    container's own smoke check cannot run at build time."""
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# the invoker allowlist
# ---------------------------------------------------------------------------


def test_an_unset_allowlist_is_a_no_op_and_says_so(monkeypatch):
    """Cloud Run IAM is then the only gate. This is the configuration in which a
    misconfiguration is indistinguishable from a working allowlist, which is
    exactly why it is pinned by a test."""
    monkeypatch.delenv("RECONCILER_ALLOWED_INVOKERS", raising=False)
    _verify_invoker(None, build_logger())          # does not raise
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "   ,  ")
    _verify_invoker(None, build_logger())


def test_a_configured_allowlist_rejects_a_caller_with_no_bearer_token(monkeypatch):
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "tick@saga-agents-staging.iam.gserviceaccount.com")
    for header in (None, "", "Basic abc", "bearer"):
        with pytest.raises(HTTPException) as exc:
            _verify_invoker(header, build_logger())
        assert exc.value.status_code == 401


def test_a_token_that_cannot_be_verified_is_401_not_500(monkeypatch):
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "tick@saga-agents-staging.iam.gserviceaccount.com")
    with pytest.raises(HTTPException) as exc:
        _verify_invoker("Bearer not-a-real-token", build_logger())
    assert exc.value.status_code == 401


def test_a_verified_caller_off_the_allowlist_is_403(monkeypatch):
    """403 rather than 401: the token was good, the identity was not. Telling
    those apart is the difference between "fix your auth" and "you are not
    allowed to terminate other people's work"."""
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "tick@saga-agents-staging.iam.gserviceaccount.com")
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request: {"email": "someone-else@saga.xyz"},
    )
    with pytest.raises(HTTPException) as exc:
        _verify_invoker("Bearer good-token", build_logger())
    assert exc.value.status_code == 403


def test_a_verified_caller_on_the_allowlist_passes(monkeypatch):
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "a@saga.xyz, tick@saga.xyz ")
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request: {"email": "tick@saga.xyz"},
    )
    _verify_invoker("Bearer good-token", build_logger())


def test_the_reconcile_endpoint_enforces_the_allowlist(monkeypatch):
    monkeypatch.setenv("RECONCILER_ALLOWED_INVOKERS", "tick@saga.xyz")
    reconciler = _StubReconciler()
    response = _client(reconciler).post("/reconcile")
    assert response.status_code == 401
    assert reconciler.passes == 0, "a rejected caller still ran a pass"


# ---------------------------------------------------------------------------
# one pass at a time
# ---------------------------------------------------------------------------


def test_a_concurrent_pass_gets_409_and_does_not_run(monkeypatch):
    """Two passes would both see the same stale lease, both invalidate, both
    terminate and both release -- and the second release decrements a pool below
    its true active count, silently inflating capacity."""
    monkeypatch.delenv("RECONCILER_ALLOWED_INVOKERS", raising=False)
    gate = threading.Event()
    reconciler = _StubReconciler(gate)
    client = _client(reconciler)
    results: list[int] = []

    def _first() -> None:
        results.append(client.post("/reconcile").status_code)

    thread = threading.Thread(target=_first, daemon=True)
    thread.start()
    # Wait until the first pass is definitely inside run_once and holding the lock.
    for _ in range(500):
        if reconciler.passes == 1 and _PASS_LOCK.locked():
            break
        threading.Event().wait(0.01)

    second = client.post("/reconcile")
    gate.set()
    thread.join(timeout=10)

    assert second.status_code == 409
    assert second.json() == {"status": "already_running"}
    assert results == [200]
    assert reconciler.passes == 1, "the concurrent request ran a second pass"


def test_the_lock_is_released_even_when_a_pass_raises(monkeypatch):
    """A pass that throws must not wedge the service: Cloud Scheduler would keep
    getting 409 forever and nothing would ever be reconciled again."""
    monkeypatch.delenv("RECONCILER_ALLOWED_INVOKERS", raising=False)

    class _Exploding:
        def run_once(self):
            raise RuntimeError("firestore is unreachable")

    client = TestClient(create_app(_Exploding(), logger=build_logger()))
    with pytest.raises(RuntimeError):
        client.post("/reconcile")
    assert not _PASS_LOCK.locked()


# ---------------------------------------------------------------------------
# termination is confirmed, not requested
# ---------------------------------------------------------------------------


def _view(name: str = f"{JOB}/executions/x1") -> ExecutionView:
    return ExecutionView(
        name=name,
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow(),
        task_id="task_1",
        attempt_id="att_1",
        tenant_id="eng",
        generation=3,
    )


class _CancelClient(FakeExecutionsClient):
    def __init__(self, result) -> None:
        super().__init__({})
        self.result = result
        self.cancelled: list[str] = []

    def cancel_execution(self, request):
        self.cancelled.append(request.name)
        outcome = self.result
        return SimpleNamespace(result=lambda timeout=None: outcome)


def _backend(client) -> CloudRunBackend:
    return CloudRunBackend(
        PROJECT, REGION, executions_client=client, jobs_client=FakeJobsClient([]),
        logger=build_logger(),
    )


def test_a_cancellation_naming_this_execution_is_confirmed():
    view = _view()
    client = _CancelClient(SimpleNamespace(name=view.name))
    assert _backend(client).terminate(view) is True
    assert client.cancelled == [view.name]


def test_a_cancellation_that_returns_a_different_execution_is_not_confirmed():
    """The case the name comparison was written to catch, and the case
    `bool(result)` reported as success. `repair.py` gates the lease release on
    this boolean, so a false True releases a slot while the first agent may still
    be running."""
    view = _view()
    client = _CancelClient(SimpleNamespace(name=f"{JOB}/executions/someone-else"))
    assert _backend(client).terminate(view) is False


def test_a_truthy_but_unrelated_operation_result_is_not_confirmed():
    """A protobuf is truthy whenever it carries any field at all."""
    view = _view()
    client = _CancelClient(SimpleNamespace(name="", generation=7, uid="abc"))
    assert _backend(client).terminate(view) is False


def test_an_execution_that_is_already_gone_counts_as_confirmed():
    from google.api_core import exceptions as gapi_exceptions

    view = _view()

    class _NotFound(FakeExecutionsClient):
        def __init__(self) -> None:
            super().__init__({})

        def cancel_execution(self, request):
            raise gapi_exceptions.NotFound("no such execution")

    assert _backend(_NotFound()).terminate(view) is True
