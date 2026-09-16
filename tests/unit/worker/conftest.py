"""Fixtures that assemble a real worker against in-memory infrastructure.

The worker under test is the production `Worker`, driving the production
`ControlPlane`, the production `CheckpointManager` and the production process
manager. Only three things are substituted, and each is a real implementation
rather than a mock: Firestore becomes an in-memory document store, GCS becomes a
directory, and Cloud Monitoring becomes a list. The runner child is a genuine
subprocess running the genuine mock runner.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_worker.config import WorkerConfig
from agent_worker.control import ControlPlane
from agent_worker.lifecycle import Worker, WorkerDeps
from agent_worker.logs import build_logger
from agent_worker.objectstore import LocalObjectStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import TaskState

from fakes import FakeFirestore, FakeSecretClient, FakeTransactionRunner, RecordingExporter

TENANT = "eng"
PROJECT = "saga-agents-staging"
BUCKET = "saga-agents-staging-swarm-artifacts"


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "gcs", bucket=BUCKET)


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


def seed_tenant(db: FakeFirestore, *, credentials: list[str] | None = None) -> None:
    db.seed(
        f"tenants/{TENANT}",
        {
            "tenant_id": TENANT,
            "kind": "group",
            "principal": "eng@saga.xyz",
            "created_at": utcnow(),
            "enabled": True,
            "credentials": credentials or [],
            "service_account": f"swarm-tenant-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
            "gcs_prefix": f"tenants/{TENANT}",
            "namespace": f"swarm-{TENANT}",
        },
    )


def seed_attempt(
    db: FakeFirestore,
    *,
    task_id: str = "task_1",
    attempt_id: str = "att_1",
    lease_id: str = "lease_1",
    generation: int = 1,
    task_generation: int | None = None,
    state: TaskState = TaskState.LEASED,
    runner_profile: str = "mock",
    task_input: dict[str, Any] | None = None,
    cancel_requested: bool = False,
    lease_released: bool = False,
    latest_checkpoint: str | None = None,
    pool_active: int = 1,
) -> dict[str, str]:
    """Create the documents a dispatched attempt would find in Firestore."""
    profile = RUNNER_PROFILES[runner_profile]
    backend = resolve_backend(profile).value
    units = RESOURCE_CLASSES[profile.resource_class].units
    pools = pool_names_for(
        tenant_id=TENANT,
        provider=profile.provider,
        resource_class=profile.resource_class,
        runner_profile=runner_profile,
        backend=backend,
    )
    now = utcnow()
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id,
            "tenant_id": TENANT,
            "state": state.value,
            "runner_profile": runner_profile,
            "resource_class": profile.resource_class,
            "provider": profile.provider,
            "input": task_input or {"prompt": "hello", "steps": 2, "sleep_seconds": 0.1},
            "submitted_by": "alice@saga.xyz",
            "created_at": now,
            "updated_at": now,
            "attempt_count": 1,
            "max_attempts": 3,
            "current_lease_id": lease_id,
            "current_generation": task_generation if task_generation is not None else generation,
            "cancel_requested": cancel_requested,
            "latest_checkpoint": latest_checkpoint,
            "timeout_seconds": profile.timeout_seconds,
        },
    )
    db.seed(
        f"leases/{lease_id}",
        {
            "lease_id": lease_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "tenant_id": TENANT,
            "generation": generation,
            "pools": pools,
            "units": units,
            "state": state.value,
            "created_at": now,
            "dispatch_deadline": now + timedelta(seconds=300),
            "expires_at": now + timedelta(seconds=120),
            "heartbeat_at": None,
            "released_at": now if lease_released else None,
        },
    )
    for name in pools:
        db.seed(
            name if name.startswith("pools/") else f"pools/{name}",
            {"name": name, "hard_limit": 10, "active": pool_active, "enabled": True,
             "updated_at": now},
        )
    seed_tenant(db)
    return {"task_id": task_id, "attempt_id": attempt_id, "lease_id": lease_id}


def build_worker(
    db: FakeFirestore,
    store: LocalObjectStore,
    tmp_path: Path,
    log_stream: io.StringIO,
    *,
    task_id: str = "task_1",
    attempt_id: str = "att_1",
    lease_id: str = "lease_1",
    generation: int = 1,
    runner_profile: str = "mock",
    timeout_seconds: int = 60,
    checkpoint_interval_seconds: int = 2,
    heartbeat_interval_seconds: int = 1,
    control_poll_seconds: int = 1,
    termination_grace_seconds: int = 2,
    max_in_worker_retry_delay_seconds: int = 45,
    secret_client: Any | None = None,
    **overrides: Any,
) -> tuple[Worker, WorkerConfig, RecordingExporter]:
    profile = RUNNER_PROFILES[runner_profile]
    config = WorkerConfig(
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=TENANT,
        generation=generation,
        runner_profile=runner_profile,
        project_id=PROJECT,
        region="us-central1",
        firestore_database="swarm",
        artifact_bucket=BUCKET,
        workspace_root=tmp_path / "workspace",
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
        max_in_worker_retry_delay_seconds=max_in_worker_retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
        control_poll_seconds=control_poll_seconds,
        provider=profile.provider,
        **overrides,
    )
    logger = build_logger(
        task_id=task_id,
        attempt_id=attempt_id,
        tenant_id=TENANT,
        generation=generation,
        runner_profile=runner_profile,
        stream=log_stream,
    )
    control = ControlPlane(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=TENANT,
        generation=generation,
        logger=logger,
        txn_runner=FakeTransactionRunner(db),
    )
    exporter = RecordingExporter()
    deps = WorkerDeps(
        control=control,
        store=store,
        logger=logger,
        db=db,
        metrics_exporter=exporter,
        secret_client=secret_client or FakeSecretClient(),
    )
    return Worker(config, deps), config, exporter


@pytest.fixture
def worker_factory(db, store, tmp_path, log_stream):
    def _factory(**kwargs: Any):
        return build_worker(db, store, tmp_path, log_stream, **kwargs)

    return _factory
