"""Worker entrypoint.

    python -m agent_worker

Everything that decides WHAT to run comes from the frozen catalogue keyed by
`RUNNER_PROFILE`; everything the environment supplies is an identifier. The exit
code is the contract with the dispatcher and the reconciler:

    0   terminal state persisted, lease released
    1   the attempt failed, terminal state persisted, lease released
    70  fenced: a newer generation owns this task; the task and the lease
        were not written, and the agent was never started or was stopped
    71  cancelled
    75  parked (quota, backpressure, missing credential, interruption)
    76  the runner exceeded its timeout and was killed
    78  the worker could not start at all
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from swarm_common.config import Settings

from .config import WorkerConfig
from .control import ControlPlane
from .errors import ConfigError, ExitCode
from .lifecycle import Worker, WorkerDeps
from .logs import build_logger
from .metrics import build_metrics_exporter
from .objectstore import LocalObjectStore, build_object_store
from .secrets import SecretManagerClient


def _firestore_client(settings: Settings):
    from google.cloud import firestore  # lazy so unit tests never touch grpc

    # The named `swarm` database, never `(default)`: this is a shared project.
    return firestore.Client(project=settings.project_id, database=settings.firestore_database)


def build_worker(config: WorkerConfig, settings: Settings) -> Worker:
    logger = build_logger(
        task_id=config.task_id,
        attempt_id=config.attempt_id,
        tenant_id=config.tenant_id,
        generation=config.generation,
        runner_profile=config.runner_profile,
    )
    local_root = os.environ.get("LOCAL_ARTIFACT_ROOT", "").strip()
    if local_root:
        # Local smoke runs: same code path, filesystem instead of GCS.
        store = LocalObjectStore(Path(local_root), bucket=config.artifact_bucket or "local")
    else:
        store = build_object_store(
            bucket=config.artifact_bucket, project_id=config.project_id
        )
    db = _firestore_client(settings)
    control = ControlPlane(
        db,
        task_id=config.task_id,
        attempt_id=config.attempt_id,
        lease_id=config.lease_id,
        tenant_id=config.tenant_id,
        generation=config.generation,
        logger=logger,
        heartbeat_extension_seconds=settings.lease_timeout_seconds,
    )
    deps = WorkerDeps(
        control=control,
        store=store,
        logger=logger,
        db=db,
        metrics_exporter=build_metrics_exporter(
            project_id=config.project_id,
            region=config.region,
            logger=logger,
            enable_cloud_monitoring=os.environ.get("DISABLE_CLOUD_MONITORING", "") == "",
        ),
        secret_client=SecretManagerClient(config.project_id),
    )
    return Worker(config, deps)


def main() -> int:
    try:
        settings = Settings.from_env()
        config = WorkerConfig.from_env(settings)
    except (ConfigError, ValueError) as exc:
        # No logger yet: there is no task identity to bind to.
        print(f'{{"severity":"ERROR","message":"worker configuration failed: {exc}"}}',
              file=sys.stderr, flush=True)
        return ExitCode.CONFIG
    worker = build_worker(config, settings)
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(main())
