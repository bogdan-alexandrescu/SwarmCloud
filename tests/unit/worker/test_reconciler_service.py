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
from test_backend_identity import (
    JOB,
    PROJECT,
    REGION,
    FakeExecutionsClient,
    FakeJobsClient,
    run_execution,
)

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
    """A cancel whose acknowledgement is scripted, over a readable execution.

    `current` is what a re-read of the execution returns, and it defaults to the
    still-running shape: an unacknowledged cancel over an execution Cloud Run
    still reports as running is the one case that must keep the slot held.
    """

    def __init__(self, result, current=None) -> None:
        name = f"{JOB}/executions/x1"
        super().__init__({JOB: [current if current is not None else run_execution(name=name)]})
        self.result = result
        self.cancelled: list[str] = []

    def cancel_execution(self, request):
        self.cancelled.append(request.name)
        outcome = self.result
        return SimpleNamespace(result=lambda timeout=None: outcome)


class _RaisingCancelClient(FakeExecutionsClient):
    """A cancel that fails, over an execution whose own state is readable.

    This is the production shape. Cloud Run does not report "the cancellation
    failed" and "the execution did not succeed" differently: all three failures
    seen on saga-agents-staging arrive as an exception out of
    `cancel_execution`/`operation.result()` while the execution itself is
    already terminal.
    """

    def __init__(self, error: Exception, current=None, *, raise_on_get=None) -> None:
        name = f"{JOB}/executions/x1"
        super().__init__({JOB: [current if current is not None else run_execution(name=name)]})
        self.error = error
        self.raise_on_get = raise_on_get
        self.cancelled: list[str] = []

    def cancel_execution(self, request):
        self.cancelled.append(request.name)
        raise self.error

    def get_execution(self, name: str):
        if self.raise_on_get is not None:
            self.read.append(name)
            raise self.raise_on_get
        return super().get_execution(name)


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


# ---------------------------------------------------------------------------
# ... and an execution Cloud Run reports as FINISHED is confirmed too
#
# Three shapes of "the cancel did not ack but the execution is over" have been
# observed on saga-agents-staging, and each stranded the slot for at least a
# full pass:
#
#   400 Execution 'swarm-job-u-bogdan-mock-w8g2g' cannot be cancelled because
#       it is not running.                        (16 leases held, 2026-09-20)
#   409 Task swarm-verify-dc9fl-task0 failed with exit code: 1 ...
#                                                       (LRO error code 10)
#   None Unspecified error. 2: Unspecified error.       (LRO error code 2)
#
# The last one is swarm-job-eng-claude-code-sq2l6. Its CancelExecution audit
# entry carries `status { code: 2, message: "Execution
# swarm-job-eng-claude-code-sq2l6 has failed to complete, 0/1 tasks were a
# success." }` and, in the SAME payload, the execution with cancelledCount 1 and
# completionTime set. The cancellation worked; Cloud Run failed the operation
# because the execution did not SUCCEED. There is no way to tell those apart
# from the ack, which is why the execution's own state has to be read.
# ---------------------------------------------------------------------------


def _finished(name: str = f"{JOB}/executions/x1"):
    """The shape a real `get_execution` returned for sq2l6 after the cancel."""
    return run_execution(name=name, running=0, cancelled=1, completed=True)


def test_an_unspecified_cancel_error_over_a_finished_execution_is_confirmed():
    """The sq2l6 case: the slot must come back."""
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    client = _RaisingCancelClient(gapi_exceptions.Unknown("Unspecified error."), _finished())
    assert _backend(client).terminate(view) is True
    assert client.read == [view.name], "the execution's own state must be read"


def test_a_cancel_refused_because_the_execution_is_not_running_is_confirmed():
    """`400 ... cannot be cancelled because it is not running.`

    A read-then-act race: 16 of these arrived in 1.997s on 2026-09-20 because
    the executions finished between the list and the cancel. The reconciler's
    own record of that pass, pass_0bd239d41f79479385cf, reads findings 17,
    skipped 16, slots_released 1.
    """
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    client = _RaisingCancelClient(
        gapi_exceptions.BadRequest(
            f"Execution '{view.name}' cannot be cancelled because it is not running."
        ),
        _finished(),
    )
    assert _backend(client).terminate(view) is True


def test_a_cancel_error_over_a_still_running_execution_keeps_the_slot_held():
    """The refusal this whole module exists for, and it must not weaken.

    The execution reads as running, so the cancel outcome is genuinely unknown.
    The original error is re-raised so `repair.py` logs the real cause rather
    than a bare False.
    """
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    client = _RaisingCancelClient(gapi_exceptions.Unknown("Unspecified error."))
    with pytest.raises(gapi_exceptions.Unknown):
        _backend(client).terminate(view)
    assert client.read == [view.name]


def test_a_cancel_error_whose_re_read_finds_nothing_is_confirmed():
    """A GET that 404s is the strongest proof there is: the execution is gone."""
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    client = _RaisingCancelClient(
        gapi_exceptions.Unknown("Unspecified error."),
        raise_on_get=gapi_exceptions.NotFound(view.name),
    )
    assert _backend(client).terminate(view) is True


def test_a_cancel_error_whose_re_read_also_fails_keeps_the_slot_held():
    """No proof either way is the same as no proof of a stop."""
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    client = _RaisingCancelClient(
        gapi_exceptions.Unknown("Unspecified error."),
        raise_on_get=gapi_exceptions.ServiceUnavailable("backend is down"),
    )
    with pytest.raises(gapi_exceptions.Unknown):
        _backend(client).terminate(view)


def test_a_cancel_error_over_an_execution_reporting_nothing_yet_keeps_the_slot_held():
    """No counts and no completion time is a COLD START, not a stop.

    This is the shape sq2l6 itself had for the three minutes before the cancel
    ("Started deployed execution in 2m54.83s"), and the shape
    `_execution_view` deliberately reads as RUNNING. Zero counts are the same
    bytes whether the container has not started or the API is answering
    partially, so "nothing is reported" must never read as "nothing is running"
    -- that is a release straight into a live agent, and image pulls here take
    minutes.
    """
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    not_yet_reporting = run_execution(name=view.name, running=0)
    assert not_yet_reporting.completion_time is None
    client = _RaisingCancelClient(
        gapi_exceptions.Unknown("Unspecified error."), not_yet_reporting
    )
    with pytest.raises(gapi_exceptions.Unknown):
        _backend(client).terminate(view)


def test_only_a_completed_execution_counts_as_finished():
    """The predicate itself, one condition at a time."""
    from reconciler.backends import execution_is_finished

    assert execution_is_finished(run_execution(running=0, cancelled=1, completed=True))
    assert execution_is_finished(run_execution(running=0, succeeded=1, completed=True))
    assert execution_is_finished(run_execution(running=0, failed=1, completed=True))
    # No completion time: every other field can look terminal and it still is not.
    assert not execution_is_finished(run_execution(running=0, cancelled=1))
    assert not execution_is_finished(run_execution(running=0))
    assert not execution_is_finished(run_execution(running=1, completed=True))
    assert not execution_is_finished(
        run_execution(running=0, cancelled=1, completed=True, reconciling=True)
    )
    # An object carrying none of the fields holds the slot rather than freeing it.
    assert not execution_is_finished(SimpleNamespace())


def test_a_cancel_error_over_a_reconciling_execution_keeps_the_slot_held():
    """`reconciling` means Cloud Run has not settled this resource yet.

    `completion_time` can already be set while the service is still acting on
    the execution -- sq2l6 grew an `ImmediateRetry` condition five seconds after
    its completionTime -- so a resource still reconciling is not proof.
    """
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    still_settling = run_execution(
        name=view.name, running=0, cancelled=1, completed=True, reconciling=True
    )
    client = _RaisingCancelClient(gapi_exceptions.Unknown("Unspecified error."), still_settling)
    with pytest.raises(gapi_exceptions.Unknown):
        _backend(client).terminate(view)


def test_a_cancel_error_over_an_execution_with_a_live_task_keeps_the_slot_held():
    """A terminal count on a multi-task execution is not the whole execution.

    `completion_time` plus `running_count > 0` is the shape that matters: one
    task finished, another is still going, and the agent is still running.
    """
    from google.api_core import exceptions as gapi_exceptions

    view = _view()
    partly_done = run_execution(name=view.name, running=1, succeeded=1, completed=True)
    client = _RaisingCancelClient(gapi_exceptions.Unknown("Unspecified error."), partly_done)
    with pytest.raises(gapi_exceptions.Unknown):
        _backend(client).terminate(view)


def test_a_mismatched_ack_over_a_finished_execution_is_confirmed():
    """The name comparison stays, but it is no longer the only evidence."""
    view = _view()
    client = _CancelClient(
        SimpleNamespace(name=f"{JOB}/executions/someone-else"), current=_finished()
    )
    assert _backend(client).terminate(view) is True
