"""An unattributed execution is warned about once, and only while it matters (#1822).

MEASURED 2026-10-21: "cloud run execution under a swarm- job carries no attempt
id" was logged about 70k times a day; 95% of those executions were already
finished (mostly swarm-verify acceptance runs). The once-per-execution memory
lived in the process, so every new reconciler instance said it all again.

Pinned here:

  U-1  A RUNNING unattributed execution is logged once.
  U-2  A FINISHED one is never logged, and is counted in the pass summary.
  U-3  A new instance does not log again an execution older than the horizon
       an earlier pass already reported it within.
  U-4  A refused cancel over the wedged shape (no running task, no completion
       time) is marked for the reconciler and is a WARNING in the backend, the
       error being the reconciler's call; a running one stays an ERROR.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from test_backend_identity import FakeJobsClient, _wedged_clients, run_execution, run_job

from reconciler.backends import (
    UNATTRIBUTED_REPORT_HORIZON_SECONDS,
    CloudRunBackend,
    cancel_refused_as_not_running,
)
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore

UNATTRIBUTED = "cloud run execution under a swarm- job carries no attempt id"
STILL_RUNNING = "cloud run still reports this execution as running; NOT releasing the slot"
PROJECT = "saga-agents-staging"
REGION = "us-central1"
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-job-eng-mock"


def lines(stream: io.StringIO, message: str) -> list[dict[str, Any]]:
    found = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return [line for line in found if line["message"] == message]


class Listed:
    def __init__(self, executions: list[Any]) -> None:
        self.executions = executions

    def list_executions(self, parent: str) -> list[Any]:
        return list(self.executions) if parent == JOB else []


def backend(executions: list[Any], stream: io.StringIO) -> CloudRunBackend:
    job = run_job(name=JOB, labels={"managed-by": "swarm-scheduler", "swarm-tenant": "eng"})
    return CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([job]),
        executions_client=Listed(executions),
        logger=build_logger(stream=stream),
    )


def ci_run(name: str, **shape: Any) -> Any:
    """A `gcloud run jobs execute` run under the platform's job: no ids at all."""
    return run_execution(name=f"{JOB}/executions/{name}", env={}, **shape)


def test_a_running_unattributed_execution_is_logged_once():
    """U-1."""
    stream = io.StringIO()
    cloud_run = backend([ci_run("ci-running", age_seconds=60)], stream)

    cloud_run.list_executions()
    cloud_run.list_executions()

    found = lines(stream, UNATTRIBUTED)
    # MUTATION: skipping running ones too (logging nothing) makes this 0; no
    # once-memory at all makes it 2.
    assert [line["execution"] for line in found] == [f"{JOB}/executions/ci-running"]


def test_a_finished_unattributed_execution_is_never_logged_and_is_counted():
    """U-2."""
    stream = io.StringIO()
    finished = [
        ci_run("verify-a", running=0, succeeded=1, completed=True, age_seconds=60),
        ci_run("verify-b", running=0, failed=1, completed=True, age_seconds=60),
    ]
    cloud_run = backend(finished, stream)

    assert cloud_run.list_executions() == []
    # MUTATION: dropping the `ended` check logs both, as before #1822.
    assert lines(stream, UNATTRIBUTED) == []
    # MUTATION: not counting them leaves this at 0.
    assert cloud_run.unattributed_finished == 2


def test_the_pass_summary_counts_finished_unattributed_executions(db):
    """U-2: the count reaches the pass's own record."""
    stream = io.StringIO()
    cloud_run = backend(
        [ci_run("verify-a", running=0, succeeded=1, completed=True, age_seconds=60)], stream
    )
    logger = build_logger(stream=io.StringIO())
    config = ReconcilerConfig(
        project_id=PROJECT,
        region=REGION,
        firestore_database="swarm",
        enable_gke=False,
        enable_checkpoint_gc=False,
    )
    reconciler = Reconciler(
        store=ControlStore(db, logger=logger), backends=[cloud_run], config=config,
        logger=logger, hold_releaser=None, finish_announcer=None,
    )

    report = reconciler.run_once()

    # MUTATION: not adding the backend's count to the report leaves this at 0.
    assert report.as_dict()["unattributed_finished"] == 1


def test_a_new_instance_does_not_log_again_what_an_earlier_pass_reported():
    """U-3: the memory that holds across instances is the execution's age."""
    young = ci_run("ci-young", age_seconds=60)
    old = ci_run("ci-old", age_seconds=UNATTRIBUTED_REPORT_HORIZON_SECONDS + 60)
    stream = io.StringIO()

    backend([young, old], stream).list_executions()

    # MUTATION: dropping the horizon (in-process memory alone) logs ci-old too,
    # on every new instance, which is what reset the memory before #1822.
    assert [line["execution"] for line in lines(stream, UNATTRIBUTED)] == [
        f"{JOB}/executions/ci-young"
    ]


@pytest.mark.parametrize(
    ("current", "wedged", "severity"),
    [
        (dict(running=0), True, "WARNING"),
        (dict(running=1), False, "ERROR"),
    ],
)
def test_a_refused_cancel_over_the_wedged_shape_is_marked(current, wedged, severity):
    """U-4. The original error is still re-raised: nothing about the slot changes."""
    from google.api_core import exceptions as gapi_exceptions

    stream = io.StringIO()
    jobs, executions = _wedged_clients(
        cancel_error=gapi_exceptions.Unknown("Unspecified error."),
        current=run_execution(**current),
    )
    cloud_run = CloudRunBackend(
        PROJECT, REGION, jobs_client=jobs, executions_client=executions,
        logger=build_logger(stream=stream),
    )
    view = ExecutionView(
        name=f"{JOB}/executions/x1", backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING, created_at=None,
    )

    with pytest.raises(gapi_exceptions.Unknown) as raised:
        cloud_run.terminate(view)

    # MUTATION: not noting the wedged shape leaves this False for running=0;
    # noting every unproven stop makes it True for running=1.
    assert cancel_refused_as_not_running(raised.value) is wedged
    assert [line["severity"] for line in lines(stream, STILL_RUNNING)] == [severity]


def test_cloud_runs_own_not_running_refusal_is_recognised():
    from google.api_core import exceptions as gapi_exceptions

    refusal = gapi_exceptions.BadRequest(
        "Execution 'swarm-job-eng-mock-5lrt8' cannot be cancelled because it is not running."
    )
    # MUTATION: matching only the note misses Cloud Run's own words.
    assert cancel_refused_as_not_running(refusal) is True
    assert cancel_refused_as_not_running(RuntimeError("Cloud Run said no")) is False
