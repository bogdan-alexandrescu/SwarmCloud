"""The reconciler gives back a fenced attempt's account holds, after the fence (#380).

A fenced worker exits without running its own release, so its subscription
account hold stayed counted for the hold's whole TTL (3 h) and `choose()` handed
out less of the account. The reconciler is the component that fences, so once
its fence has COMMITTED it asks the broker to release every hold stamped with
that attempt (`POST /v1/holds/release-attempt`, platform-only; the broker's side
is tests/unit/control_plane/test_fenced_attempt_holds.py).

ONLY AFTER THE FENCE. A fence that is refused or loses its race (#372) returns
no new generation, and then nothing is released: the attempt may be a live
worker still using its account. These tests read the task document at the
moment of the release call, so "after" is measured, not inferred from order of
lines.

The worker's own `account_released` path is a different lane (lifecycle.py).
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import Finding, FindingKind
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import BrokerHoldReleaser, Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

TENANT = "eng"
EXECUTION = "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1"


class RecordingReleaser:
    """Records each release, with the task's generation at the moment of the call."""

    def __init__(self, db: FakeFirestore, *, fail: Exception | None = None) -> None:
        self._db = db
        self._fail = fail
        self.calls: list[tuple[str, str, int]] = []

    def release_attempt(self, *, task_id: str, attempt_id: str) -> int:
        generation = int(self._db.doc(f"tasks/{task_id}")["current_generation"])
        self.calls.append((task_id, attempt_id, generation))
        if self._fail is not None:
            raise self._fail
        return 1


class FenceLosesItsRace(ControlStore):
    """The fence refused inside its transaction (#372).

    The fence's decision, not `invalidate_generation`: since #560 a repair
    fences in the same transaction as it releases (`fence_release_repair`),
    and both paths make the fence through this one judgement.
    """

    def _fence_decision(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def seed_dead_worker(db: FakeFirestore, *, generation: int = 3) -> None:
    """A RUNNING task whose lease went silent ten minutes ago."""
    now = utcnow()
    silent = 600
    pools = pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )
    db.seed(
        "tasks/task_1",
        {
            "id": "task_1", "tenant_id": TENANT, "state": TaskState.RUNNING.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": generation, "current_lease_id": "lease_1",
            "attempt_count": 1, "max_attempts": 3, "updated_at": now,
            "cancel_requested": False,
        },
    )
    db.seed(
        "leases/lease_1",
        {
            "lease_id": "lease_1", "task_id": "task_1", "attempt_id": "att_1",
            "tenant_id": TENANT, "generation": generation, "pools": pools, "units": 1,
            "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(seconds=silent + 60),
            "dispatch_deadline": now - timedelta(seconds=silent - 240),
            "expires_at": now - timedelta(seconds=silent - 120),
            "heartbeat_at": now - timedelta(seconds=silent),
            "released_at": None,
        },
    )
    db.seed(
        "attempts/att_1",
        {
            "attempt_id": "att_1", "task_id": "task_1", "tenant_id": TENANT,
            "generation": generation, "lease_id": "lease_1", "backend": "CLOUD_RUN_JOB",
            "created_at": now - timedelta(seconds=silent + 60),
            "execution_name": EXECUTION,
            "started_at": now - timedelta(seconds=silent + 30),
        },
    )
    for pool in pools:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 2,
                                  "enabled": True, "updated_at": now})


def running_execution(generation: int = 3) -> ExecutionView:
    return ExecutionView(
        name=EXECUTION,
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(seconds=600),
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=generation,
        parent="projects/p/locations/us-central1/jobs/swarm-eng-mock",
    )


def build(db, config, releaser, *backends, store_class=ControlStore) -> Reconciler:
    logger = build_logger(stream=io.StringIO())
    store = store_class(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(
        store=store, backends=list(backends), config=config, logger=logger,
        hold_releaser=releaser,
    )


def test_a_fenced_attempts_holds_are_released_after_the_fence(db, config):
    seed_dead_worker(db, generation=3)
    releaser = RecordingReleaser(db)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, releaser, backend).run_once()

    outcome = report.outcomes[0]
    assert outcome.invalidated_to == 4
    # Exactly this attempt, once, and the task already at the new generation
    # when the broker was asked.
    assert releaser.calls == [("task_1", "att_1", 4)]
    assert "released 1 account hold(s) of att_1" in outcome.actions


def test_a_fence_that_lost_its_race_releases_nothing(db, config):
    seed_dead_worker(db, generation=3)
    releaser = RecordingReleaser(db)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(
        db, config, releaser, backend, store_class=FenceLosesItsRace
    ).run_once()

    assert report.outcomes[0].invalidated_to is None
    assert releaser.calls == []


def test_a_dry_run_releases_nothing(db, config):
    seed_dead_worker(db)
    releaser = RecordingReleaser(db)
    dry = replace(config, dry_run=True)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, dry, releaser, backend).run_once()

    assert releaser.calls == []


def test_a_broker_failure_does_not_stop_the_repair(db, config):
    seed_dead_worker(db)
    releaser = RecordingReleaser(db, fail=OSError("broker unreachable"))
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, releaser, backend).run_once()

    outcome = report.outcomes[0]
    assert releaser.calls, "the release was never attempted"
    # Terminate, release the slot and requeue still happened.
    assert outcome.terminated is True
    assert outcome.released is True
    assert outcome.repaired_to == TaskState.READY.value
    assert any("did NOT release the account holds" in a for a in outcome.actions)


def test_a_stuck_attempt_fenced_alone_releases_its_holds(db, config):
    seed_dead_worker(db, generation=3)
    releaser = RecordingReleaser(db)
    reconciler = build(db, config, releaser)
    finding = Finding(
        kind=FindingKind.STUCK_NO_PROGRESS, reason="no progress", task_id="task_1",
        lease_id="lease_1", attempt_id="att_1", tenant_id=TENANT, generation=3,
    )

    outcome = reconciler._repair(finding, None, {})  # type: ignore[arg-type]

    assert outcome.invalidated_to == 4
    assert releaser.calls == [("task_1", "att_1", 4)]


def test_without_a_broker_url_there_is_no_releaser(monkeypatch):
    monkeypatch.delenv("QUOTA_BROKER_URL", raising=False)
    assert BrokerHoldReleaser.from_env() is None
    monkeypatch.setenv("QUOTA_BROKER_URL", "https://broker.example.run.app/")
    monkeypatch.setenv("QUOTA_BROKER_AUDIENCE", "https://broker.example")
    assert isinstance(BrokerHoldReleaser.from_env(), BrokerHoldReleaser)


def _built_with(db, config, **kwargs) -> str:
    stream = io.StringIO()
    logger = build_logger(stream=stream)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    Reconciler(store=store, backends=[], config=config, logger=logger, **kwargs)
    return stream.getvalue()


def test_a_reconciler_with_no_broker_says_so_once_at_start(db, config, monkeypatch):
    # The per-fence path returns silently when there is no releaser, so a
    # deployment missing QUOTA_BROKER_URL is visible only through this line.
    monkeypatch.delenv("QUOTA_BROKER_URL", raising=False)
    assert "no quota broker configured" in _built_with(db, config)

    assert "no quota broker configured" not in _built_with(
        db, config, hold_releaser=RecordingReleaser(db)
    )


def _left_running(db: FakeFirestore, *, terminate_returns: bool):
    """A GKE Job still active after its task reached CANCELLED.

    No fence: a terminal task has no generation left to fence. What ends this
    attempt is the confirmed kill, and nothing else will ever release for it
    -- the worker that would have died with its Job.
    """
    seed_dead_worker(db, generation=3)
    db.doc("tasks/task_1")["state"] = TaskState.CANCELLED.value
    execution = replace(running_execution(), backend="GKE_AUTOPILOT", namespace="swarm-tenant-eng")
    backend = FakeBackend(
        "GKE_AUTOPILOT", executions=[execution], journal=db.writes,
        terminate_returns=terminate_returns,
    )
    finding = Finding(
        kind=FindingKind.LEFT_RUNNING, reason="task is CANCELLED but its job is active",
        task_id="task_1", lease_id="lease_1", attempt_id="att_1", tenant_id=TENANT,
        generation=3, execution=execution,
    )
    return backend, finding


def test_a_left_running_job_killed_gives_back_its_holds(db, config):
    releaser = RecordingReleaser(db)
    backend, finding = _left_running(db, terminate_returns=True)
    reconciler = build(db, config, releaser, backend)

    outcome = reconciler._repair(finding, None, {"GKE_AUTOPILOT": backend})  # type: ignore[arg-type]

    assert outcome.terminated is True
    assert releaser.calls == [("task_1", "att_1", 3)]
    assert "released 1 account hold(s) of att_1" in outcome.actions


def test_a_left_running_job_whose_kill_is_unconfirmed_keeps_its_holds(db, config):
    # The control: the same Job, its kill not confirmed. Its worker may still
    # be running the agent on the account, so nothing is given back.
    releaser = RecordingReleaser(db)
    backend, finding = _left_running(db, terminate_returns=False)
    reconciler = build(db, config, releaser, backend)

    outcome = reconciler._repair(finding, None, {"GKE_AUTOPILOT": backend})  # type: ignore[arg-type]

    assert outcome.terminated is False
    assert releaser.calls == []
