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
    78  the worker could not start at all: bad configuration, a DNS preflight
        that could not resolve what the first Firestore call needs, clients
        that could not be built, or a control plane it could not read at the
        generation check. Nothing was written, not even the attempt.
        Also: a dependency that was UNAVAILABLE after the generation check and
        before the runner (a spent startup budget, UNAVAILABLE, a 5xx). The
        task, the lease and the event stream were not written; the attempt's
        own document records the phase and the error, when Firestore took it.
        A refusal there (PERMISSION_DENIED, NOT_FOUND) is not a 78: the
        attempt fails the task with the reason, exit 1
    143 a SIGTERM or SIGINT arrived before the runner child existed. The task,
        the lease and the event stream were not written. If the attempt had
        already recorded its start, its own document records the phase. That
        holds when the signal lands inside a Firestore transaction too, where
        the library's rollback can raise something else in its place

THE STARTUP IS LOUD ON PURPOSE (see `startup.py` for the incident behind it).
The first line is written before the configuration is read. Every phase after
that gets its own line. Library warnings reach stdout. A DNS failure is
reported within `DNS_PREFLIGHT_BUDGET_SECONDS`. A SIGTERM names the phase it
arrived in.

WHAT HAPPENS TO A 78 OR A 143 AFTERWARDS. No component reads the exit code.
The reconciler judges the lease, not the process (`reconciler/detect.py`,
`detect_stale_leases`):

  * A 78 from the configuration, the DNS preflight, the generation check or
    `advance_to_running` comes before the first heartbeat, so the lease is
    judged by its dispatch deadline alone, 300 s after admission. (A 78 from
    later in the startup window comes after it, and is judged like a 143
    after it, below.) It is then reclaimed as
    `stale_lease`: fenced, released, and the task goes back to READY with
    `last_error` "reconciled: lease silent for Ns ...". It is retried like any
    other lost attempt. Once `max_attempts` is spent it goes to FAILED with
    that same `last_error`. The DNS or configuration cause is in the container
    log and nowhere in Firestore. A worker that cannot reach Firestore cannot
    write it there. Making 78 non-retryable, with its cause as `last_error`,
    needs the reconciler to read each execution's exit code from the backend.
    It does not do that today.
  * A 143 before the first heartbeat is judged the same way. After the first
    heartbeat, the lease is reclaimed once it has been silent for
    `heartbeat_grace_seconds`, and the task is requeued. The lifecycle does
    not park it. A SCHEDULED_RETRY park is promoted by nothing
    (docs/incidents/2026-09-24-gke-dispatch.md), so parking would strand the
    task where the reconciler's requeue does not.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from swarm_common.config import Settings
from swarm_common.logging_setup import configure_logging

from . import startup
from .config import WorkerConfig
from .control import ControlPlane
from .errors import ConfigError, ExitCode
from .lifecycle import Worker, WorkerDeps, _execution_name
from .logs import build_logger
from .metrics import build_metrics_exporter
from .objectstore import LocalObjectStore, build_object_store
from .secrets import SecretManagerClient


def _firestore_client(settings: Settings):
    from google.cloud import firestore  # lazy so unit tests never touch grpc

    # The named `swarm` database, never `(default)`: this is a shared project.
    return firestore.Client(project=settings.project_id, database=settings.firestore_database)


def firestore_startup_call_options() -> dict[str, Any]:
    """`retry` and `timeout` for every Firestore call before the runner exists.

    The control plane applies them inside `ControlPlane.startup_budget()`:
    reads, writes, and a transaction's own begin, commit and rollback.

    The predicate is the set of errors Firestore's own default retries for
    `batch_get_documents`, which is what `DocumentReference.get` calls:
    DEADLINE_EXCEEDED, INTERNAL and UNAVAILABLE. Only the budget is shorter
    (see `startup.FIRESTORE_STARTUP_RETRY_SECONDS`). The same options go on
    the window's plain writes: `record_attempt_start`'s `set`, the events and
    the heartbeat's `update`. Each writes a payload built before the call, so
    a retry after a DEADLINE_EXCEEDED whose write had in fact landed writes the
    same bytes again. A transaction's commit is the exception, and it gets a
    narrower predicate from `control._commit_retry`.
    """
    from google.api_core import exceptions as core_exceptions  # lazy: grpc
    from google.api_core.retry import Retry, if_exception_type

    return {
        "retry": Retry(
            initial=0.1,
            maximum=5.0,
            multiplier=1.3,
            predicate=if_exception_type(
                core_exceptions.DeadlineExceeded,
                core_exceptions.InternalServerError,
                core_exceptions.ServiceUnavailable,
            ),
            timeout=startup.FIRESTORE_STARTUP_RETRY_SECONDS,
        ),
        "timeout": startup.FIRESTORE_STARTUP_CALL_SECONDS,
    }


def build_worker(
    config: WorkerConfig, settings: Settings, *, phases: startup.Phases | None = None
) -> Worker:
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
        startup_call_options=firestore_startup_call_options(),
    )
    if phases is not None:
        phases.rebind(logger)
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
        phases=phases,
    )
    return Worker(config, deps)


def main() -> int:
    # FIRST, before anything that can fail or hang: one flushed line that says
    # this process exists. A pod whose log is empty never ran this line.
    phases = startup.Phases(startup.bootstrap_logger())
    phases.started(execution=_execution_name())
    # google-auth and grpc attach NullHandlers to their loggers. Without a
    # root handler their warnings go nowhere, including "Compute Engine
    # Metadata server unavailable" and every failed project-id lookup.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    # Nothing has been written yet, so a SIGTERM from here until the lifecycle
    # installs its own handler names the phase and exits at once. The same
    # call arms the SIGTERM stack dump.
    armed = startup.route_signals(phases.exit_on_signal)

    phases.enter("configuration", stack_dump_on_sigterm=armed)
    try:
        settings = Settings.from_env()
        config = WorkerConfig.from_env(settings)
    except (ConfigError, ValueError) as exc:
        # No task identity has been validated yet. The line carries whatever
        # the environment claimed, and nothing else from it.
        phases.log.error(
            f"worker configuration failed: {exc}",
            phase=phases.current,
            exit_code=ExitCode.CONFIG,
        )
        return ExitCode.CONFIG

    hosts = startup.preflight_hosts()
    phases.enter(
        "dns_preflight", hosts=hosts, budget_seconds=startup.DNS_PREFLIGHT_BUDGET_SECONDS
    )
    results = startup.dns_preflight(hosts, budget_seconds=startup.DNS_PREFLIGHT_BUDGET_SECONDS)
    unreachable = [r for r in results if not r["ok"]]
    if unreachable:
        names = ", ".join(r["host"] for r in unreachable)
        phases.log.error(
            f"DNS unreachable: could not resolve {names} within "
            f"{startup.DNS_PREFLIGHT_BUDGET_SECONDS:g}s; exiting {ExitCode.CONFIG} "
            "before building any client",
            phase=phases.current,
            unreachable=unreachable,
            resolved=[r for r in results if r["ok"]],
            budget_seconds=startup.DNS_PREFLIGHT_BUDGET_SECONDS,
            nameservers=startup.resolver_nameservers(),
            hint=(
                "every Google API call this worker makes starts with these names. "
                "On GKE, check that the tenant namespace's egress NetworkPolicy "
                "allows UDP and TCP 53 to the nameservers listed here. On a cluster "
                "with NodeLocal DNSCache that is the node-local cache, not only kube-dns"
            ),
            exit_code=ExitCode.CONFIG,
        )
        return ExitCode.CONFIG
    phases.log.info("DNS preflight passed", phase=phases.current, resolved=results)

    phases.enter("build_worker")
    try:
        worker = build_worker(config, settings, phases=phases)
    except Exception as exc:
        phases.log.exception(
            "the worker could not be built; exiting before touching the control plane",
            exc,
            phase=phases.current,
            exit_code=ExitCode.CONFIG,
        )
        return ExitCode.CONFIG
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(main())
