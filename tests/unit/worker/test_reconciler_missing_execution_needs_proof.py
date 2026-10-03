"""`missing_execution` is a claim of ABSENCE, and only proof of absence may fence on it (#372).

On 2026-09-30 the reconciler fenced task_4c63880eb31d421fa85f generation 1 as
`missing_execution` -- "no backend execution exists 400s after dispatch" --
while its Cloud Run execution swarm-job-eng-claude-code-s4s9z existed, had
started seven minutes earlier, and was heartbeating and checkpointing. The
Cloud Run listing had SUCCEEDED; it simply did not map that execution to its
attempt, and a listing that does not mention an attempt was read as proof that
nothing runs for it.

The owner's expected behaviour, recorded on the issue, and pinned here:

  M-1  a lease that heartbeats inside `heartbeat_grace_seconds` is never
       missing an execution: a live worker IS an execution;
  M-2  on Cloud Run, an absence finding is repaired only once the attempt's
       recorded execution, read BY NAME, is reported absent -- an ACTIVE answer
       repairs nothing, an UNREADABLE one holds the finding back;
  M-3  a listed execution under a `swarm-` job whose attempt id cannot be read
       is logged, once per execution, with its name and the identifiers that
       were missing (never an environment value), and does not by itself
       produce a fence.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore
from test_backend_identity import FakeExecutionsClient, FakeJobsClient, run_execution, run_job
from test_reconciler_safety import TENANT, build, seed_running_task

from reconciler.backends import CloudRunBackend, ProbeOutcome
from reconciler.config import ReconcilerConfig
from reconciler.detect import FindingKind, detect_missing_executions
from reconciler.logs import build_logger
from reconciler.store import ControlStore
from swarm_common.models import utcnow

PROJECT = "p"
REGION = "us-central1"
#: The job and execution names `seed_running_task` records on the attempt.
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-eng-mock"
EXECUTION = f"{JOB}/executions/x1"

#: Past `missing_execution_grace_seconds` (300 in the fixture config): the
#: attempt was created this long ago.
AGE = 400


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region=REGION,
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def seed(db: FakeFirestore, *, heartbeat_seconds_ago: int | None) -> None:
    """A RUNNING Cloud Run task whose attempt was created `AGE` seconds ago.

    `heartbeat_seconds_ago=None` is a lease that has not heartbeated yet: its
    dispatch deadline is still ahead, so the stale-lease rule leaves it alone
    and only the missing-execution rule can speak about it.
    """
    seed_running_task(db, generation=1, silent_seconds=10)
    now = utcnow()
    created = now - timedelta(seconds=AGE)
    lease = db.documents["leases/lease_1"]
    attempt = db.documents["attempts/att_1"]
    lease["created_at"] = created
    lease["dispatch_deadline"] = created + timedelta(seconds=600)
    attempt["created_at"] = created
    attempt["execution_name"] = EXECUTION
    if heartbeat_seconds_ago is None:
        lease["heartbeat_at"] = None
        lease["expires_at"] = created + timedelta(seconds=120)
        attempt["started_at"] = None
    else:
        lease["heartbeat_at"] = now - timedelta(seconds=heartbeat_seconds_ago)
        lease["expires_at"] = lease["heartbeat_at"] + timedelta(seconds=120)


def untouched(db: FakeFirestore, report: Any) -> None:
    assert report.outcomes == [], [o.as_dict() for o in report.outcomes]
    task = db.documents["tasks/task_1"]
    assert task["state"] == "RUNNING"
    assert task["current_generation"] == 1, "a live attempt was fenced"
    assert db.documents["leases/lease_1"]["released_at"] is None


class ProbedExecutions(FakeExecutionsClient):
    """Cloud Run's executions API, with `get_execution` scripted separately.

    `listed` is what `list_executions` returns per job; `by_name` is what a GET
    answers, so a test can model the #372 shape -- a listing that does not
    attribute an execution that a GET finds -- and a GET that fails.
    """

    def __init__(self, listed: dict[str, list[Any]], *, by_name: dict[str, Any] | None = None,
                 get_raises: Exception | None = None) -> None:
        super().__init__(listed)
        self.by_name = dict(by_name or {})
        self.get_raises = get_raises

    def get_execution(self, name: str) -> Any:
        from google.api_core import exceptions as gapi_exceptions

        self.read.append(name)
        if self.get_raises is not None:
            raise self.get_raises
        if name in self.by_name:
            return self.by_name[name]
        raise gapi_exceptions.NotFound(name)


def cloud_run(executions: ProbedExecutions, *, stream: io.StringIO | None = None) -> CloudRunBackend:
    job = run_job(name=JOB, labels={"managed-by": "swarm-scheduler", "swarm-tenant": TENANT})
    return CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([job]),
        executions_client=executions,
        logger=build_logger(stream=stream if stream is not None else io.StringIO()),
    )


def ids_env(**overrides: str) -> dict[str, str]:
    env = {"TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "TENANT_ID": TENANT, "GENERATION": "1"}
    env.update(overrides)
    return {k: v for k, v in env.items() if v}


# ---------------------------------------------------------------------------
# M-1: a heartbeating lease is proof an execution exists
# ---------------------------------------------------------------------------


def test_a_heartbeating_lease_with_no_listed_execution_yields_no_finding(db, config):
    """The listing succeeded and named nothing for this attempt; the worker
    heartbeated twenty seconds ago. Something is running it."""
    seed(db, heartbeat_seconds_ago=20)
    snapshot = ControlStore(db, logger=build_logger(stream=io.StringIO())).snapshot()

    assert detect_missing_executions(snapshot, {}, config) == []

    report = build(db, config, FakeBackend(executions=[])).run_once()
    untouched(db, report)


def test_a_lease_whose_heartbeat_is_older_than_the_grace_is_still_judged(db, config):
    """The control for the test above: the exemption is the heartbeat, inside
    the window, and nothing wider. A worker last heard from five minutes ago
    proves nothing about now."""
    seed(db, heartbeat_seconds_ago=300)
    snapshot = ControlStore(db, logger=build_logger(stream=io.StringIO())).snapshot()

    found = detect_missing_executions(snapshot, {}, config)

    assert [f.kind for f in found] == [FindingKind.MISSING_EXECUTION]


def test_the_372_shape_a_live_unattributed_execution_is_not_fenced(db, config):
    """The incident, end to end through the real Cloud Run backend: the
    execution is listed, carries no attempt id, and its worker heartbeats."""
    seed(db, heartbeat_seconds_ago=15)
    unattributed = run_execution(name=EXECUTION, env={}, age_seconds=AGE)
    executions = ProbedExecutions({JOB: [unattributed]}, by_name={EXECUTION: unattributed})

    report = build(db, config, cloud_run(executions)).run_once()

    untouched(db, report)


# ---------------------------------------------------------------------------
# M-2: on Cloud Run, absence is confirmed by name before it is repaired
# ---------------------------------------------------------------------------


def test_an_execution_active_by_name_is_not_repaired_as_missing(db, config):
    """No heartbeat yet, past the grace, and the listing attributes nothing to
    the attempt -- but a GET of the attempt's own execution finds it running."""
    seed(db, heartbeat_seconds_ago=None)
    running = run_execution(name=EXECUTION, env=ids_env(), age_seconds=AGE)
    executions = ProbedExecutions({JOB: []}, by_name={EXECUTION: running})

    report = build(db, config, cloud_run(executions)).run_once()

    assert executions.read == [EXECUTION], "the attempt's execution was not asked for by name"
    untouched(db, report)
    # Disproved, not merely held back: nothing is wrong, so nothing pages.
    assert report.suppressed == [], [s.as_dict() for s in report.suppressed]


def test_an_execution_absent_by_name_is_repaired(db, config):
    seed(db, heartbeat_seconds_ago=None)
    executions = ProbedExecutions({JOB: []})

    report = build(db, config, cloud_run(executions)).run_once()

    assert executions.read == [EXECUTION], "absence was not confirmed by name"
    assert [o.kind for o in report.outcomes] == ["missing_execution"], (
        [o.as_dict() for o in report.outcomes]
    )
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert db.documents["tasks/task_1"]["current_generation"] == 2
    assert db.documents["tasks/task_1"]["state"] == "READY"


def test_an_unreadable_probe_holds_the_finding_back(db, config):
    """The listing worked, the GET did not. Nothing is proven, nothing moves."""
    from google.api_core import exceptions as gapi_exceptions

    seed(db, heartbeat_seconds_ago=None)
    executions = ProbedExecutions(
        {JOB: []}, get_raises=gapi_exceptions.ServiceUnavailable("backend blip")
    )

    report = build(db, config, cloud_run(executions)).run_once()

    untouched(db, report)
    assert [s.kind for s in report.suppressed] == ["missing_execution"]


def test_an_attempt_with_no_recorded_execution_keeps_todays_rule(db, config):
    """Nothing to ask for by name: past the grace with an empty listing, the
    finding stands as it always has."""
    seed(db, heartbeat_seconds_ago=None)
    db.documents["attempts/att_1"]["execution_name"] = None
    executions = ProbedExecutions({JOB: []})

    report = build(db, config, cloud_run(executions)).run_once()

    assert executions.read == []
    assert [o.kind for o in report.outcomes] == ["missing_execution"]


def test_a_placeholder_execution_name_is_not_probed_into_a_false_absence(db, config):
    """The dispatcher records `<job>/executions/pending-<attempt>` when the
    run operation named no execution. A GET of that name answers 404 whether
    or not the real execution runs, so it proves nothing and is not asked."""
    seed(db, heartbeat_seconds_ago=None)
    db.documents["attempts/att_1"]["execution_name"] = f"{JOB}/executions/pending-att_1"
    executions = ProbedExecutions({JOB: []})

    report = build(db, config, cloud_run(executions)).run_once()

    assert executions.read == []
    assert [o.kind for o in report.outcomes] == ["missing_execution"]


def _backend(executions: ProbedExecutions) -> CloudRunBackend:
    return cloud_run(executions)


def test_cloud_run_probe_reads_one_execution_by_name():
    from google.api_core import exceptions as gapi_exceptions

    running = run_execution(name=EXECUTION, env=ids_env())
    done = run_execution(name=f"{JOB}/executions/x2", env=ids_env(), running=0,
                         succeeded=1, completed=True)
    backend = _backend(ProbedExecutions({}, by_name={EXECUTION: running, done.name: done}))

    active = backend.probe(EXECUTION)
    assert active.outcome is ProbeOutcome.ACTIVE
    assert active.execution is not None and active.execution.attempt_id == "att_1"
    assert active.execution.tenant_id == TENANT

    assert backend.probe(done.name).outcome is ProbeOutcome.FINISHED
    assert backend.probe(f"{JOB}/executions/gone").outcome is ProbeOutcome.ABSENT

    failing = _backend(ProbedExecutions({}, get_raises=gapi_exceptions.PermissionDenied("no")))
    assert failing.probe(EXECUTION).outcome is ProbeOutcome.UNREADABLE


def test_cloud_run_probe_refuses_names_that_are_not_a_platform_execution():
    backend = _backend(ProbedExecutions({}))
    other_team = f"projects/{PROJECT}/locations/{REGION}/jobs/their-job/executions/e1"

    assert backend.probe(other_team).outcome is ProbeOutcome.UNREADABLE
    assert backend.probe("not-a-name").outcome is ProbeOutcome.UNREADABLE
    assert backend.probe(f"{JOB}/executions/pending-att_1").outcome is ProbeOutcome.UNREADABLE


# ---------------------------------------------------------------------------
# M-3: an unattributed execution is logged, and is not a fence by itself
# ---------------------------------------------------------------------------


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


UNATTRIBUTED = "cloud run execution under a swarm- job carries no attempt id"


def test_a_listed_execution_with_no_attempt_id_is_logged_once_per_execution():
    stream = io.StringIO()
    unattributed = run_execution(
        name=EXECUTION,
        env={"TASK_ID": "task_1", "UNRELATED_SETTING": "value-that-must-not-be-logged"},
    )
    attributed = run_execution(name=f"{JOB}/executions/x2", env=ids_env())
    backend = cloud_run(
        ProbedExecutions({JOB: [unattributed, attributed]}), stream=stream
    )

    backend.list_executions()

    found = [line for line in _lines(stream) if line["message"] == UNATTRIBUTED]
    assert len(found) == 1, _lines(stream)
    line = found[0]
    assert line["execution"] == EXECUTION
    assert "ATTEMPT_ID" in line["missing"]
    assert "TASK_ID" not in line["missing"]
    assert line["task_id"] == "task_1"
    assert "value-that-must-not-be-logged" not in stream.getvalue()

    # Once per execution, not once per pass (2026-10-03: three CI executions
    # warned every minute for five hours beside a held lease).
    backend.list_executions()
    again = [line for line in _lines(stream) if line["message"] == UNATTRIBUTED]
    assert len(again) == 1, "logged again on the next pass"


def test_an_execution_with_a_task_but_no_attempt_id_does_not_fence_its_live_task(db, config):
    """The orphan rule used to read "no attempt id" as "no lease exists for
    this execution's attempt" and kill it -- and the task's live lease with it.
    An execution that names the task's CURRENT generation under a live lease
    cannot be attributed, and that is not the same as being an orphan."""
    seed(db, heartbeat_seconds_ago=15)
    stream = io.StringIO()
    half = run_execution(
        name=EXECUTION, env=ids_env(ATTEMPT_ID=""), age_seconds=AGE
    )
    backend = cloud_run(ProbedExecutions({JOB: [half]}, by_name={EXECUTION: half}), stream=stream)
    terminated: list[str] = []
    backend.terminate = lambda execution: terminated.append(execution.name) or True  # type: ignore[method-assign]

    report = build(db, config, backend).run_once()

    assert terminated == []
    untouched(db, report)
    assert any(line["message"] == UNATTRIBUTED for line in _lines(stream))


def test_an_unattributed_execution_of_an_older_generation_is_still_an_obsolete_one(db, config):
    """The control for the test above: the stand-aside is for an execution
    that may be the current attempt. One that names a superseded generation is
    still the obsolete-generation rule's, attempt id or not."""
    seed(db, heartbeat_seconds_ago=15)
    old = run_execution(
        name=f"{JOB}/executions/x0", env=ids_env(ATTEMPT_ID="", GENERATION="0"), age_seconds=AGE
    )
    current = run_execution(name=EXECUTION, env=ids_env(), age_seconds=AGE)
    backend = cloud_run(ProbedExecutions({JOB: [old, current]}, by_name={EXECUTION: current}))
    terminated: list[str] = []
    backend.terminate = lambda execution: terminated.append(execution.name) or True  # type: ignore[method-assign]

    build(db, config, backend).run_once()

    assert terminated == [old.name]
    assert db.documents["leases/lease_1"]["released_at"] is None


# ---------------------------------------------------------------------------
# M-2 for stale_lease: the other absence kind confirmed by name on Cloud Run
# ---------------------------------------------------------------------------
#
# `_CONFIRMED_BY_NAME` holds STALE_LEASE as well as MISSING_EXECUTION: a silent
# Cloud Run lease whose execution the listing did not attribute used to be
# fenced and released straight away. It now waits for a GET of the attempt's
# own execution, and what that GET answers decides the repair -- including the
# new case where a live execution is TERMINATED before the lease is released.


@pytest.fixture
def stale_only_config(config: ReconcilerConfig) -> ReconcilerConfig:
    """The missing-execution clock pushed out of the way, so the stale-lease
    rule is the only one that speaks about this lease."""
    from dataclasses import replace as dc_replace

    return dc_replace(config, missing_execution_grace_seconds=3600)


def test_a_stale_lease_whose_execution_is_absent_by_name_is_fenced_and_released(
    db, stale_only_config
):
    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions({JOB: []})

    report = build(db, stale_only_config, cloud_run(executions)).run_once()

    assert executions.read == [EXECUTION], "absence was not confirmed by name"
    assert [o.kind for o in report.outcomes] == ["stale_lease"], (
        [o.as_dict() for o in report.outcomes]
    )
    assert not [o for o in report.outcomes if o.terminated]
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert db.documents["tasks/task_1"]["current_generation"] == 2


def test_a_stale_lease_whose_execution_is_active_by_name_is_killed_before_its_release(
    db, stale_only_config
):
    """Silent for five minutes, execution running by name: a dead worker. The
    execution is terminated, and only after that is the capacity released."""
    seed(db, heartbeat_seconds_ago=300)
    running = run_execution(name=EXECUTION, env=ids_env(), age_seconds=AGE)
    executions = ProbedExecutions({JOB: []}, by_name={EXECUTION: running})
    backend = cloud_run(executions)
    released_when_terminated: list[Any] = []

    def terminate(execution: Any) -> bool:
        released_when_terminated.append(db.documents["leases/lease_1"]["released_at"])
        return True

    backend.terminate = terminate  # type: ignore[method-assign]

    report = build(db, stale_only_config, backend).run_once()

    assert executions.read == [EXECUTION]
    assert [o.kind for o in report.outcomes] == ["dead_worker"], (
        [o.as_dict() for o in report.outcomes]
    )
    assert released_when_terminated == [None], "capacity was released before the kill"
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert db.documents["tasks/task_1"]["current_generation"] == 2


def test_a_stale_lease_whose_probe_is_unreadable_is_held(db, stale_only_config):
    from google.api_core import exceptions as gapi_exceptions

    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions(
        {JOB: []}, get_raises=gapi_exceptions.ServiceUnavailable("backend blip")
    )

    report = build(db, stale_only_config, cloud_run(executions)).run_once()

    assert executions.read == [EXECUTION]
    assert report.outcomes == [], [o.as_dict() for o in report.outcomes]
    assert [s.kind for s in report.suppressed] == ["stale_lease"]
    assert db.documents["leases/lease_1"]["released_at"] is None
    assert db.documents["tasks/task_1"]["current_generation"] == 1
