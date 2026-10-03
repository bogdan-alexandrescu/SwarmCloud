"""A held lease must be held for a reason, and never silently (2026-10-03).

MEASURED ON DEV, ~03:35Z: task_8fce64316ad14fc981fb (attempt
att_848c6b79e6e74e6d949a, lease lease_fb40d28612aa4be9be80). Its Cloud Run
execution swarm-job-eng-claude-code-6c98m FAILED at 22:35:23Z ("Container
terminated on signal 7"); the last heartbeat was 22:33:15Z. Five hours later
the task was still RUNNING and its lease still held capacity, while every pass
logged `stale_lease` and `missing_execution` as `not repairing: the backend
that would hold this execution was unreadable`, beside GKE's `namespace
unreadable` lines and one `carries no attempt id` warning per CI execution per
minute.

What is pinned here:

  H-1  POSITIVE PROOF. A lease past its TTL whose execution the listing shows
       ENDED is a lost worker, and is repaired by the ordinary path -- fenced,
       released, requeued (or FAILED with LOST_WORKER once attempts are spent)
       -- with no probe by name needed.
  H-2  An execution under a `swarm-` job that the scheduler did not create (no
       task id, no attempt id) is excluded, logged once per execution rather
       than once per pass, and can never make Cloud Run unreadable.
  H-3  An unreadable GKE namespace, or a blind GKE backend, holds only what
       could be on GKE.
  H-4  #450 stands: an execution that is MISSING is repaired only on proof of
       its absence.
  H-5  A lease held past its TTL for longer than `held_lease_alert_minutes` is
       logged at ERROR once per lease per hour, and listed in the pass.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace as dc_replace
from datetime import timedelta
from typing import Any, Iterable

import pytest
from fakes import FakeFirestore, FakeTransactionRunner
from test_backend_identity import FakeJobsClient, run_execution, run_job
from test_reconciler_missing_execution_needs_proof import (
    EXECUTION,
    JOB,
    PROJECT,
    REGION,
    ProbedExecutions,
    ids_env,
    seed,
)
from test_reconciler_safety import TENANT

from reconciler.backends import CloudRunBackend, NamespacedListing, Probe, ProbeOutcome
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.repair import HELD_PAST_TTL, NOT_REPAIRING, Reconciler
from reconciler.store import ControlStore
from swarm_common.models import EndCause
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

UNATTRIBUTED = "cloud run execution under a swarm- job carries no attempt id"
#: A CI run under the same job: `gcloud run jobs execute` sets none of the ids.
CI_EXECUTION = f"{JOB}/executions/ci-acceptance"


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
        enable_checkpoint_gc=False,
    )


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def reconciler(
    db: FakeFirestore, config: ReconcilerConfig, *backends: Any
) -> tuple[Reconciler, io.StringIO]:
    stream = io.StringIO()
    logger = build_logger(stream=stream)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=list(backends), config=config, logger=logger), stream


def cloud_run(executions: ProbedExecutions, *, logger: Any | None = None) -> CloudRunBackend:
    job = run_job(name=JOB, labels={"managed-by": "swarm-scheduler", "swarm-tenant": TENANT})
    return CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([job]),
        executions_client=executions,
        logger=logger if logger is not None else build_logger(stream=io.StringIO()),
    )


def unreadable_by_name() -> Exception:
    """The probe's GET answers 403, as it may have on 2026-10-03."""
    from google.api_core import exceptions as gapi_exceptions

    return gapi_exceptions.PermissionDenied("run.executions.get denied")


def failed_execution(**overrides: Any) -> Any:
    """swarm-job-eng-claude-code-6c98m as Cloud Run listed it: over, failed."""
    shape: dict[str, Any] = dict(
        name=EXECUTION, env=ids_env(), running=0, failed=1, completed=True, age_seconds=2400
    )
    shape.update(overrides)
    return run_execution(**shape)


def ci_execution(**overrides: Any) -> Any:
    shape: dict[str, Any] = dict(name=CI_EXECUTION, env={}, age_seconds=600)
    shape.update(overrides)
    return run_execution(**shape)


def held(db: FakeFirestore, report: Any) -> None:
    released = [o.as_dict() for o in report.outcomes if o.released]
    assert released == [], released
    assert db.documents["leases/lease_1"]["released_at"] is None
    assert db.documents["tasks/task_1"]["current_generation"] == 1
    assert db.documents["tasks/task_1"]["state"] == "RUNNING"


# ---------------------------------------------------------------------------
# H-1: an ended execution is positive proof the worker is gone
# ---------------------------------------------------------------------------


def test_a_lease_past_its_ttl_whose_execution_failed_is_repaired_as_a_lost_worker(db, config):
    """The incident: silent five minutes, its execution listed as FAILED, and
    the GET by name refused. The listing alone proves the worker is gone."""
    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions({JOB: [failed_execution()]}, get_raises=unreadable_by_name())
    backend = cloud_run(executions)
    terminated: list[str] = []
    backend.terminate = lambda execution: terminated.append(execution.name) or True  # type: ignore[method-assign]
    before = db.documents["pools/global"]["active"]
    rec, _ = reconciler(db, config, backend)

    report = rec.run_once()

    assert [o.kind for o in report.outcomes] == ["stale_lease"], (
        [o.as_dict() for o in report.outcomes]
    )
    outcome = report.outcomes[0]
    assert outcome.released is True
    assert outcome.invalidated_to == 2
    assert outcome.repaired_to == "READY"
    assert "has ended" in outcome.reason
    assert report.suppressed == [], [s.as_dict() for s in report.suppressed]
    assert terminated == [], "an ended execution has nothing left to kill"
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert db.documents["pools/global"]["active"] == before - 1, "capacity was not returned"
    task = db.documents["tasks/task_1"]
    assert task["current_generation"] == 2
    assert task["state"] == "READY"


def test_a_lost_worker_with_its_attempts_spent_ends_failed_as_lost_worker(db, config):
    """The same path, all the way: the existing retry rules decide, and spent
    attempts end FAILED with the lost-worker cause."""
    seed(db, heartbeat_seconds_ago=300)
    db.documents["tasks/task_1"]["attempt_count"] = 3
    db.documents["tasks/task_1"]["max_attempts"] = 3
    executions = ProbedExecutions({JOB: [failed_execution()]}, get_raises=unreadable_by_name())
    rec, _ = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    assert [o.repaired_to for o in report.outcomes] == ["FAILED"]
    task = db.documents["tasks/task_1"]
    assert task["state"] == "FAILED"
    assert task["end_cause"] == EndCause.LOST_WORKER.value
    assert db.documents["leases/lease_1"]["released_at"] is not None


def test_an_execution_still_reconciling_is_not_proof(db, config):
    """The control: completion time set but Cloud Run still acting on it is
    not over (`execution_is_finished`). With the GET refused, nothing moves."""
    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions(
        {JOB: [failed_execution(reconciling=True)]}, get_raises=unreadable_by_name()
    )
    rec, _ = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    held(db, report)


def test_an_ended_execution_of_another_generation_is_not_proof(db, config):
    """The control: an ended execution that names another generation is not
    this lease's, whatever attempt id it carries."""
    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions(
        {JOB: [failed_execution(env=ids_env(GENERATION="7"))]}, get_raises=unreadable_by_name()
    )
    rec, _ = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    held(db, report)


def test_a_heartbeating_lease_over_an_ended_execution_is_left_alone(db, config):
    """The control: proof the execution ended is acted on only for a lease
    already past its clocks. A live heartbeat is not a stale lease."""
    seed(db, heartbeat_seconds_ago=15)
    executions = ProbedExecutions({JOB: [failed_execution()]}, get_raises=unreadable_by_name())
    rec, _ = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    held(db, report)
    assert report.outcomes == [], [o.as_dict() for o in report.outcomes]


# ---------------------------------------------------------------------------
# H-2: an execution the scheduler did not create is not ours
# ---------------------------------------------------------------------------


def test_an_unattributed_execution_is_logged_once_and_excluded():
    stream = io.StringIO()
    attributed = run_execution(name=f"{JOB}/executions/x2", env=ids_env())
    backend = cloud_run(
        ProbedExecutions({JOB: [ci_execution(), attributed]}), logger=build_logger(stream=stream)
    )

    first = backend.list_executions()
    second = backend.list_executions()

    assert [v.name for v in first] == [attributed.name]
    assert [v.name for v in second] == [attributed.name]
    found = [line for line in lines(stream) if line["message"] == UNATTRIBUTED]
    assert len(found) == 1, "logged on every pass instead of once per execution"
    assert found[0]["execution"] == CI_EXECUTION


class RaisingLogger:
    """A logger that cannot describe an unattributed execution."""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        self._inner = build_logger(stream=self.stream)

    def warning(self, message: str, **fields: Any) -> None:
        if message == UNATTRIBUTED:
            raise ValueError("cannot serialise this execution")
        self._inner.warning(message, **fields)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_an_unattributed_execution_does_not_make_cloud_run_unreadable(db, config):
    """A CI execution beside the incident's failed one. Describing it fails;
    the listing must still stand, and the lost worker must still be repaired."""
    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions(
        {JOB: [ci_execution(), failed_execution()]}, get_raises=unreadable_by_name()
    )
    rec, stream = reconciler(db, config, cloud_run(executions, logger=RaisingLogger()))

    report = rec.run_once()

    assert not [e for e in report.errors if "listing executions failed" in e], report.errors
    assert not [line for line in lines(stream) if line["message"] == NOT_REPAIRING]
    assert [o.kind for o in report.outcomes] == ["stale_lease"]
    assert db.documents["leases/lease_1"]["released_at"] is not None


# ---------------------------------------------------------------------------
# H-3: GKE's blindness holds only GKE's findings
# ---------------------------------------------------------------------------


class UnreadableGke:
    """A namespaced backend whose tenant namespace answers 403.

    `blind=True` reads nothing at all, which `_look` reports as the whole
    backend being unavailable; otherwise one other namespace reads.
    """

    name = Backend.GKE_AUTOPILOT.value

    def __init__(self, *, blind: bool) -> None:
        self.blind = blind
        self.probed: list[str] = []

    def namespace_for(self, tenant_id: str, recorded: str | None = None) -> str:
        return recorded or f"{ReconcilerConfig.namespace_prefix}{tenant_id}"

    def namespace_of(self, execution_name: str | None) -> str | None:
        head, sep, _ = str(execution_name or "").partition("/")
        return head if sep and not head.startswith("projects") else None

    def list_executions_in(self, namespaces: Iterable[str]) -> NamespacedListing:
        mine = f"{ReconcilerConfig.namespace_prefix}{TENANT}"
        return NamespacedListing(
            executions=[],
            readable=frozenset() if self.blind else frozenset({"swarm-tenant-smoke"}),
            unreadable={mine: "403 Forbidden"},
        )

    def probe(self, execution_name: str) -> Probe:
        self.probed.append(execution_name)
        return Probe(ProbeOutcome.UNREADABLE, detail="403")

    def terminate(self, execution: Any) -> bool:
        raise AssertionError("nothing on GKE may be terminated here")

    def termination(self, execution: Any) -> None:
        return None

    def list_job_resources(self) -> list[Any]:
        return []

    def delete_job_resource(self, resource: Any) -> bool:
        return False


def test_the_seeded_profile_runs_on_cloud_run():
    """The premise of the two tests below, read from the frozen contract."""
    assert resolve_backend(RUNNER_PROFILES["mock"]) is Backend.CLOUD_RUN_JOB


@pytest.mark.parametrize("blind", [True, False], ids=["gke-blind", "gke-namespace-403"])
def test_gke_unreadability_does_not_hold_a_cloud_run_lease_with_no_attempt(db, config, blind):
    """A Cloud Run task whose attempt document is not in the snapshot: the
    rule falls back to "every backend that could hold it must be readable",
    and GKE could never have held a `mock` task."""
    seed(db, heartbeat_seconds_ago=300)
    del db.documents["attempts/att_1"]
    gke = UnreadableGke(blind=blind)
    rec, stream = reconciler(
        db, dc_replace(config, enable_gke=True), cloud_run(ProbedExecutions({JOB: []})), gke
    )

    report = rec.run_once()

    assert "stale_lease" in [o.kind for o in report.outcomes], (
        [o.as_dict() for o in report.outcomes],
        [s.as_dict() for s in report.suppressed],
    )
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert gke.probed == []


@pytest.mark.parametrize("blind", [True, False], ids=["gke-blind", "gke-namespace-403"])
def test_gke_unreadability_does_not_hold_a_cloud_run_finding(db, config, blind):
    """The incident's shape with its attempt present: a Cloud Run finding
    (namespace None) beside an unreadable GKE is repaired on Cloud Run's
    evidence alone."""
    seed(db, heartbeat_seconds_ago=300)
    executions = ProbedExecutions({JOB: [failed_execution()]}, get_raises=unreadable_by_name())
    gke = UnreadableGke(blind=blind)
    rec, stream = reconciler(db, dc_replace(config, enable_gke=True), cloud_run(executions), gke)

    report = rec.run_once()

    assert [o.kind for o in report.outcomes] == ["stale_lease"]
    assert db.documents["leases/lease_1"]["released_at"] is not None
    assert not [line for line in lines(stream) if line["message"] == NOT_REPAIRING]


def test_gke_unreadability_still_holds_a_task_whose_backend_is_unknown(db, config):
    """The control: a task whose profile this service does not know could be
    anywhere, so every backend must be readable before absence is believed."""
    seed(db, heartbeat_seconds_ago=300)
    del db.documents["attempts/att_1"]
    db.documents["tasks/task_1"]["runner_profile"] = "no-such-profile"
    rec, _ = reconciler(
        db,
        dc_replace(config, enable_gke=True),
        cloud_run(ProbedExecutions({JOB: []})),
        UnreadableGke(blind=True),
    )

    report = rec.run_once()

    held(db, report)
    assert "stale_lease" in [s.kind for s in report.suppressed]


# ---------------------------------------------------------------------------
# H-4: a MISSING execution still needs proof of absence (#450)
# ---------------------------------------------------------------------------


def test_a_missing_execution_with_an_unreadable_listing_is_still_held(db, config):
    """Nothing listed (the listing itself failed), nothing readable by name:
    no proof either way, so nothing moves."""
    seed(db, heartbeat_seconds_ago=None)

    class FailingList(ProbedExecutions):
        def list_executions(self, parent: str) -> list[Any]:
            raise RuntimeError("listing refused")

    executions = FailingList({JOB: []}, get_raises=unreadable_by_name())
    rec, stream = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    held(db, report)
    assert [s.kind for s in report.suppressed] == ["missing_execution"]
    assert any(line["message"] == NOT_REPAIRING for line in lines(stream))


def test_a_missing_execution_with_a_readable_listing_and_an_unreadable_get_is_held(db, config):
    seed(db, heartbeat_seconds_ago=None)
    executions = ProbedExecutions({JOB: [ci_execution()]}, get_raises=unreadable_by_name())
    rec, _ = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    held(db, report)
    assert [s.kind for s in report.suppressed] == ["missing_execution"]


# ---------------------------------------------------------------------------
# H-5: a hold is never silent
# ---------------------------------------------------------------------------


def _held_long(db: FakeFirestore, minutes_past_ttl: int) -> None:
    """A lease past its TTL by `minutes_past_ttl`, whose execution is missing
    and cannot be read by name -- so it is held, correctly."""
    seed(db, heartbeat_seconds_ago=120 + minutes_past_ttl * 60)


def _shift(rec: Reconciler, seconds: int) -> None:
    """Every later snapshot is taken `seconds` later than the wall clock."""
    real = rec._store.snapshot

    def later() -> Any:
        snapshot = real()
        snapshot.taken_at = snapshot.taken_at + timedelta(seconds=seconds)
        return snapshot

    rec._store.snapshot = later  # type: ignore[method-assign]


def test_a_lease_held_past_its_ttl_too_long_is_an_error_once_an_hour(db, config):
    _held_long(db, minutes_past_ttl=30)
    executions = ProbedExecutions({JOB: []}, get_raises=unreadable_by_name())
    rec, stream = reconciler(db, config, cloud_run(executions))

    def errors() -> list[dict[str, Any]]:
        return [line for line in lines(stream) if line["message"] == HELD_PAST_TTL]

    report = rec.run_once()

    held(db, report)
    assert len(errors()) == 1
    line = errors()[0]
    assert line["severity"] == "ERROR"
    assert line["lease_id"] == "lease_1"
    assert line["task_id"] == "task_1"
    assert any("unreadable" in why for why in line["why"]), line["why"]
    surfaced = report.as_dict()["held_past_ttl"]
    assert [entry["lease_id"] for entry in surfaced] == ["lease_1"]
    assert surfaced[0]["past_ttl_seconds"] > 15 * 60

    rec.run_once()
    rec.run_once()
    assert len(errors()) == 1, "repeated inside the hour"

    _shift(rec, 3601)
    later = rec.run_once()
    assert len(errors()) == 2, "not repeated after an hour"
    assert [entry["lease_id"] for entry in later.held_past_ttl] == ["lease_1"]


def test_a_lease_held_briefly_past_its_ttl_is_not_an_error(db, config):
    """The control: five minutes past the TTL is inside the fifteen."""
    _held_long(db, minutes_past_ttl=5)
    executions = ProbedExecutions({JOB: []}, get_raises=unreadable_by_name())
    rec, stream = reconciler(db, config, cloud_run(executions))

    report = rec.run_once()

    held(db, report)
    assert report.held_past_ttl == []
    assert not [line for line in lines(stream) if line["message"] == HELD_PAST_TTL]


def test_the_held_alert_threshold_is_a_setting(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setenv("HELD_LEASE_ALERT_MINUTES", "40")
    assert ReconcilerConfig.from_env().held_lease_alert_minutes == 40
    monkeypatch.delenv("HELD_LEASE_ALERT_MINUTES")
    assert ReconcilerConfig.from_env().held_lease_alert_minutes == 15
