"""Worker entrypoint.

    python -m agent_worker

Everything that decides WHAT to run comes from the frozen catalogue keyed by
`RUNNER_PROFILE`; everything the environment supplies is an identifier. The exit
code is the contract with the dispatcher and the reconciler:

    0   terminal state persisted, lease released
    1   the attempt failed, its state persisted, lease released. The state is
        terminal, except for an attempt whose runner finished cleanly without
        writing an expected output (#149): that task goes back to READY while
        it has attempts left, and to FAILED once they are spent
    69  a dependency was UNAVAILABLE before the runner existed: a spent startup
        budget, UNAVAILABLE, any 5xx or gRPC UNKNOWN, DATA_LOSS or
        UNIMPLEMENTED, CANCELLED, a google-auth transport error, at the
        generation check or after it. At the generation check it is also
        every API error that is not a refusal named under 78. The task, the
        lease and the event stream were not written. After the generation
        check, the attempt's own document records the phase and the error,
        when Firestore took it. The next attempt may not meet the outage, so
        it is RETRIED
    70  fenced: a newer generation owns this task; the task and the lease
        were not written, and the agent was never started or was stopped
    71  cancelled
    75  parked (quota, backpressure, missing credential, interruption)
    76  the runner exceeded its timeout and was killed
    78  the worker CANNOT START, and another attempt would fail the same way:
        bad configuration, a DNS preflight that still could not resolve what
        the first Firestore call needs after its retries, clients that could
        not be built, or a generation check that Firestore REFUSED
        (PERMISSION_DENIED, UNAUTHENTICATED, NOT_FOUND, INVALID_ARGUMENT,
        FAILED_PRECONDITION, a credential google-auth would not refresh or
        could not find). Nothing else at the generation check is a 78: an
        error that is not one of these named refusals is 69. Nothing was
        written to Firestore, not even the attempt. The
        cause is written to the Kubernetes termination message
        (`startup.write_termination_message`). NOT retried: see below.
        A refusal AFTER the generation check is not a 78: the attempt owns
        its task by then, and fails it with the reason, exit 1
    143 a SIGTERM or SIGINT arrived before the runner child existed. The task,
        the lease and the event stream were not written. If the attempt had
        already recorded its start, its own document records the phase. That
        holds when the signal lands inside a Firestore transaction too, where
        the library's rollback can raise something else in its place

THE STARTUP IS LOUD ON PURPOSE (see `startup.py` for the incident behind it).
The first line is written before the configuration is read. Every phase after
that gets its own line. Library warnings reach stdout. A DNS failure is
retried on a fixed schedule, one warning per failed attempt: the last lookup
is asked 30 s after the first however fast each one fails, and the verdict is
reported within `DNS_PREFLIGHT_WINDOW_SECONDS`. A SIGTERM names the phase it
arrived in.

WHAT HAPPENS TO EACH EXIT AFTERWARDS. The reconciler reads the exit code of a
FINISHED execution whose attempt still holds its task's current lease
(`reconciler.detect.detect_cannot_start`): a Cloud Run task's
`last_attempt_result.exit_code`, or the worker container's
`state.terminated.exitCode` on a GKE pod.

  * 78 is NON-RETRYABLE (owner, 2026-09-25). The reconciler fails the task in
    the pass that sees the finished execution, without waiting for any
    deadline and whatever attempts remain. It fences the generation first,
    releases the lease through the frozen `release_lease_in_transaction`, and
    writes `last_error` "worker could not start: <cause>". The cause is the
    worker's own, from the termination message on GKE. Where there is none
    (Cloud Run keeps no termination message), `last_error` is "worker exited
    78: could not start (see execution logs: <execution>)". A requested
    cancel still ends CANCELLED.
  * Every other code keeps the rules it had before any exit code was read.
    The reconciler judges the lease, not the process (`detect_stale_leases`).
    A 69 or a 143 before the first heartbeat is judged by the lease's
    dispatch deadline, 300 s after admission. After the first heartbeat the
    lease is reclaimed once it has been silent for `heartbeat_grace_seconds`.
    Either way it is fenced, released, and the task goes back to READY, or to
    FAILED once `max_attempts` is spent. The lifecycle does not park a 143. A
    SCHEDULED_RETRY park is promoted by nothing
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
        message = f"worker configuration failed: {exc}"
        phases.log.error(message, phase=phases.current, exit_code=ExitCode.CONFIG)
        startup.write_termination_message(
            message=message,
            cause=f"configuration: {exc}",
            phase=phases.current,
            exit_code=ExitCode.CONFIG,
            execution=_execution_name(),
        )
        return ExitCode.CONFIG

    hosts = startup.preflight_hosts()
    phases.enter(
        "dns_preflight",
        hosts=hosts,
        budget_seconds=startup.DNS_PREFLIGHT_BUDGET_SECONDS,
        attempts=startup.DNS_PREFLIGHT_ATTEMPTS,
        schedule_seconds=list(startup.DNS_PREFLIGHT_SCHEDULE_SECONDS),
        window_seconds=startup.DNS_PREFLIGHT_WINDOW_SECONDS,
    )

    def _attempt_failed(attempt: int, unreachable: list[dict[str, Any]], retry_in: float) -> None:
        # One line per failed attempt, so that a DNS that is flapping shows
        # as flapping, and one that recovered shows that it needed to.
        phases.log.warning(
            f"DNS preflight attempt {attempt} of {startup.DNS_PREFLIGHT_ATTEMPTS} failed: "
            f"could not resolve {', '.join(r['host'] for r in unreachable)}; "
            f"retrying in {retry_in:g}s",
            phase=phases.current,
            attempt=attempt,
            attempts=startup.DNS_PREFLIGHT_ATTEMPTS,
            unreachable=unreachable,
            retry_in_seconds=retry_in,
            budget_seconds=startup.DNS_PREFLIGHT_BUDGET_SECONDS,
        )

    preflight = startup.dns_preflight_with_retries(hosts, on_failed_attempt=_attempt_failed)
    if not preflight.ok:
        unreachable = preflight.unreachable
        names = ", ".join(r["host"] for r in unreachable)
        message = (
            f"DNS unreachable: could not resolve {names} after {preflight.attempts} "
            f"attempts over {preflight.seconds:g}s; exiting {ExitCode.CONFIG} before "
            "building any client"
        )
        phases.log.error(
            message,
            phase=phases.current,
            unreachable=unreachable,
            resolved=[r for r in preflight.results if r["ok"]],
            attempts=preflight.attempts,
            seconds=preflight.seconds,
            budget_seconds=startup.DNS_PREFLIGHT_BUDGET_SECONDS,
            schedule_seconds=list(startup.DNS_PREFLIGHT_SCHEDULE_SECONDS),
            window_seconds=startup.DNS_PREFLIGHT_WINDOW_SECONDS,
            nameservers=startup.resolver_nameservers(),
            hint=(
                "every Google API call this worker makes starts with these names. "
                "On GKE, check that the tenant namespace's egress NetworkPolicy "
                "allows UDP and TCP 53 to the nameservers listed here. On a cluster "
                "with NodeLocal DNSCache that is the node-local cache, not only kube-dns"
            ),
            exit_code=ExitCode.CONFIG,
        )
        startup.write_termination_message(
            message=message,
            cause=f"DNS unreachable ({names})",
            phase=phases.current,
            exit_code=ExitCode.CONFIG,
            execution=_execution_name(),
            attempts=preflight.attempts,
        )
        return ExitCode.CONFIG
    phases.log.info(
        "DNS preflight passed",
        phase=phases.current,
        resolved=preflight.results,
        attempts=preflight.attempts,
    )

    phases.enter("build_worker")
    try:
        worker = build_worker(config, settings, phases=phases)
    except Exception as exc:
        message = "the worker could not be built; exiting before touching the control plane"
        phases.log.exception(message, exc, phase=phases.current, exit_code=ExitCode.CONFIG)
        startup.write_termination_message(
            message=message,
            cause=f"its clients could not be built: {type(exc).__name__}: {exc}",
            phase=phases.current,
            exit_code=ExitCode.CONFIG,
            execution=_execution_name(),
        )
        return ExitCode.CONFIG
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(main())
