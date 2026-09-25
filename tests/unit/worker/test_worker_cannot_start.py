"""A worker that cannot start (exit 78) fails its task at once, with its cause, and is not retried.

WHAT WAS WRONG. PR #57 made a worker that cannot start say so within seconds
and exit 78. Nothing read the 78. The reconciler judges leases, not processes,
so a 78 before the first heartbeat waited out the lease's 300 s dispatch
deadline. It was then reclaimed as `stale_lease`, and the task went back to
READY with `last_error` "reconciled: lease silent for Ns ...". The next attempt
met the same broken DNS, and so did the one after, until `max_attempts` was
spent and the task went to FAILED with that same error. The DNS cause was in
the container log and nowhere in Firestore.

THE OWNER'S DECISION, 2026-09-25 (follow-up to #57): a worker that cannot start
exits 78, and 78 is NON-RETRYABLE at the platform level. The reconciler reads
the finished execution's exit code (a Cloud Run task's `last_attempt_result`, a
GKE pod's `state.terminated`). On 78 it fails the task immediately: the
generation fenced first, the lease released through the frozen
`release_lease_in_transaction`, no retry. `last_error` is the worker's real
cause when the worker could leave one, else a generic line naming the
execution. Every other exit code keeps today's rules.

WHAT EACH TEST PINS, against the real `Reconciler`, `ControlStore` and frozen
admission code over the in-memory Firestore. Only the backend is scripted, and
it reports exactly what Cloud Run or the kubelet would.

  * an exit-78 execution fails its task in the SAME pass it is seen, before
    any dispatch deadline, with retries left, with the worker's cause;
  * fenced before released, pools returned, events in that order;
  * the cause comes from the worker's own account when there is one (its
    attempt document, then its termination message), else the generic line;
  * a non-78 failure keeps today's rules: nothing before the deadline, READY
    (a retry) after it;
  * nothing is concluded from a termination the backend could not read, from
    an attempt that is no longer the task's current one, or in a dry run;
  * a requested cancel still ends CANCELLED, as every other repair ends it;
  * both backends read the exit code from the field the platform writes it
    to, and the reconciler's 78 is the worker's 78.

HOW THE TESTS WERE MADE RED FIRST. They use only names that exist on main,
except where a test says otherwise: the scripted backend grows a `termination`
method the old reconciler never calls, so on main every behavioural test here
fails on what the task and the lease look like after the pass.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger as build_reconciler_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_tenant

#: The worker's "cannot start" exit, restated: the number is the contract
#: between the worker's process and whoever reads its exit status.
CANNOT_START = 78

GENERATION = 2
TASK, LEASE, ATTEMPT = "task_1", "lease_1", "att_1"
JOB = "projects/p/locations/us-central1/jobs/swarm-eng-mock"
EXECUTION = f"{JOB}/executions/swarm-eng-mock-x78"
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider=None,
    resource_class="standard",
    runner_profile="mock",
    backend="CLOUD_RUN_JOB",
)

#: What the worker leaves as its termination message after a DNS failure: its
#: last structured log line, with the one-line cause beside the message.
DNS_TERMINATION_MESSAGE = json.dumps(
    {
        "severity": "ERROR",
        "message": (
            "DNS unreachable: could not resolve firestore.googleapis.com after 3 attempts "
            "over 30s; exiting 78 before building any client"
        ),
        "cause": "DNS unreachable (firestore.googleapis.com)",
        "phase": "dns_preflight",
        "exit_code": 78,
    }
)


@pytest.fixture
def reconciler_config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def seed_an_attempt_that_never_started(
    db: FakeFirestore,
    *,
    age_seconds: int = 60,
    attempt_count: int = 1,
    max_attempts: int = 3,
    cancel_requested: bool = False,
    task_generation: int = GENERATION,
    attempt_fields: dict[str, Any] | None = None,
) -> None:
    """What a worker that exited before its first control-plane write leaves behind.

    The task is DISPATCHED (the worker never moved it to STARTING), the lease
    has never heartbeated, and its dispatch deadline is 300 s after admission.
    `age_seconds` is how long ago admission was.
    """
    now = utcnow()
    admitted = now - timedelta(seconds=age_seconds)
    db.seed(
        f"tasks/{TASK}",
        {
            "id": TASK, "tenant_id": TENANT, "state": TaskState.DISPATCHED.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": task_generation, "current_lease_id": LEASE,
            "attempt_count": attempt_count, "max_attempts": max_attempts,
            "updated_at": admitted, "cancel_requested": cancel_requested,
            "last_error": None,
        },
    )
    db.seed(
        f"leases/{LEASE}",
        {
            "lease_id": LEASE, "task_id": TASK, "attempt_id": ATTEMPT,
            "tenant_id": TENANT, "generation": GENERATION,
            "pools": POOLS, "units": 1, "state": TaskState.DISPATCHED.value,
            "created_at": admitted,
            "dispatch_deadline": admitted + timedelta(seconds=300),
            "expires_at": admitted + timedelta(seconds=120),
            "heartbeat_at": None,
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{ATTEMPT}",
        {
            "attempt_id": ATTEMPT, "task_id": TASK, "tenant_id": TENANT,
            "generation": GENERATION, "lease_id": LEASE, "backend": "CLOUD_RUN_JOB",
            "created_at": admitted, "execution_name": EXECUTION,
            **(attempt_fields or {}),
        },
    )
    for pool in POOLS:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 1,
                                  "enabled": True, "updated_at": now})
    seed_tenant(db)


def finished_execution(
    *, phase: ExecutionPhase = ExecutionPhase.FAILED, generation: int = GENERATION
) -> ExecutionView:
    """The Cloud Run execution of that attempt, as `CloudRunBackend` lists it."""
    return ExecutionView(
        name=EXECUTION,
        backend="CLOUD_RUN_JOB",
        phase=phase,
        created_at=utcnow() - timedelta(seconds=55),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=generation,
        parent=JOB,
    )


def ended(exit_code: int | None, message: str | None = None) -> SimpleNamespace:
    """How the backend says the execution ended."""
    return SimpleNamespace(
        exit_code=exit_code,
        message=message,
        detail=f"task 0 exited {exit_code}",
    )


def reconcile(
    db: FakeFirestore,
    config: ReconcilerConfig,
    *,
    executions: list[ExecutionView],
    terminations: dict[str, Any] | None = None,
    termination_raises: Exception | None = None,
) -> tuple[Any, FakeBackend]:
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(
        executions=executions,
        journal=db.writes,
        terminations=terminations,
        termination_raises=termination_raises,
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()
    return report, backend


def _write_index(db: FakeFirestore, path: str, field: str) -> int:
    for index, (_op, written, data) in enumerate(db.writes):
        if written == path and field in data:
            return index
    raise AssertionError(f"no write of {field} to {path}: {db.writes}")


# ---------------------------------------------------------------------------
# exit 78: failed at once, with the cause, fenced and released
# ---------------------------------------------------------------------------


def test_an_execution_that_exited_78_fails_its_task_at_once_with_the_workers_cause(
    db, reconciler_config
):
    """60 s after admission, two retries left: today's rules would do nothing yet.

    The dispatch deadline is 240 s away and the task has spent one of three
    attempts. The execution has finished with 78, so waiting cannot change
    the answer and retrying cannot either.
    """
    seed_an_attempt_that_never_started(db, age_seconds=60, attempt_count=1, max_attempts=3)

    report, backend = reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)},
    )

    assert backend.termination_reads == [EXECUTION], "the exit code was never read"
    kinds = [o.kind for o in report.outcomes]
    assert kinds == ["worker_cannot_start"], report.as_dict()
    [outcome] = report.outcomes
    assert outcome.released is True
    assert outcome.repaired_to == TaskState.FAILED.value

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    assert task["last_error"] == "worker could not start: DNS unreachable (firestore.googleapis.com)"
    assert task["current_lease_id"] is None
    assert task["completed_at"] is not None
    # NOT retried, though attempts remained: the task is terminal, and it was
    # never put back in the queue on the way there.
    assert task["attempt_count"] < task["max_attempts"]
    assert EventType.READY.value not in db.event_types(TASK), db.event_types(TASK)


def test_the_generation_is_fenced_before_the_lease_is_released_and_every_pool_comes_back(
    db, reconciler_config
):
    """Invariants 2 and 5, the same order every other repair keeps."""
    seed_an_attempt_that_never_started(db)

    reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)},
    )

    fenced = _write_index(db, f"tasks/{TASK}", "current_generation")
    released = _write_index(db, f"leases/{LEASE}", "released_at")
    failed = _write_index(db, f"tasks/{TASK}", "state")
    assert fenced < released < failed, (fenced, released, failed)
    assert db.doc(f"tasks/{TASK}")["current_generation"] == GENERATION + 1
    lease = db.doc(f"leases/{LEASE}")
    assert lease["released_at"] is not None
    assert lease["release_reason"] == "reconciler:worker_cannot_start"
    for pool in POOLS:
        assert db.doc(f"pools/{pool}")["active"] == 0, pool

    types = db.event_types(TASK)
    assert types == [
        EventType.GENERATION_FENCED.value,
        EventType.LEASE_RELEASED.value,
        EventType.FAILED.value,
    ], types
    failed_event = db.events(TASK)[-1]
    assert failed_event["detail"]["reason"] == "worker_cannot_start"
    assert failed_event["detail"]["exit_code"] == CANNOT_START
    assert failed_event["detail"]["execution"] == EXECUTION
    assert failed_event["detail"]["detail"] == (
        "worker could not start: DNS unreachable (firestore.googleapis.com)"
    )


def test_with_no_cause_to_read_the_error_names_the_exit_code_and_the_execution(
    db, reconciler_config
):
    """Cloud Run keeps no termination message, and a DNS failure writes nothing to Firestore."""
    seed_an_attempt_that_never_started(db)

    reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, None)},
    )

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value
    assert task["last_error"] == (
        f"worker exited 78: could not start (see execution logs: {EXECUTION})"
    )


def test_a_cause_the_worker_recorded_on_its_own_attempt_is_preferred(db, reconciler_config):
    """If the worker reached Firestore, its own record is the best account there is."""
    seed_an_attempt_that_never_started(
        db,
        attempt_fields={
            "exit_code": CANNOT_START,
            "error": "the control plane refused the generation check: PermissionDenied: 403",
        },
    )

    reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)},
    )

    assert db.doc(f"tasks/{TASK}")["last_error"] == (
        "worker could not start: the control plane refused the generation check: "
        "PermissionDenied: 403"
    )


def test_a_requested_cancel_still_ends_cancelled(db, reconciler_config):
    """The person who pressed cancel decided how this task ends; the 78 does not overrule them."""
    seed_an_attempt_that_never_started(db, cancel_requested=True)

    reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)},
    )

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.CANCELLED.value, task
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None


# ---------------------------------------------------------------------------
# every other exit keeps today's rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [1, 69, 137, None], ids=["failed", "unavailable", "killed", "unknown"])
def test_a_non_78_exit_before_the_dispatch_deadline_changes_nothing(
    db, reconciler_config, exit_code
):
    seed_an_attempt_that_never_started(db, age_seconds=60)

    report, _ = reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(exit_code)},
    )

    assert report.outcomes == [], report.as_dict()
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.DISPATCHED.value
    assert db.doc(f"leases/{LEASE}")["released_at"] is None


@pytest.mark.parametrize("exit_code", [1, 69, 137, None], ids=["failed", "unavailable", "killed", "unknown"])
def test_a_non_78_exit_after_the_dispatch_deadline_is_retried_as_before(
    db, reconciler_config, exit_code
):
    """Today's rules, unchanged: fenced, released, READY with retries left.

    400 s after admission both of today's absence rules see this lease (the
    stale lease past its dispatch deadline, and the missing execution past its
    grace), and the first of them to be repaired requeues the task.
    """
    seed_an_attempt_that_never_started(db, age_seconds=400, attempt_count=1, max_attempts=3)

    report, _ = reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(exit_code)},
    )

    kinds = {o.kind for o in report.outcomes}
    assert kinds and kinds <= {"stale_lease", "missing_execution"}, report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value, task
    assert task["last_error"].startswith("reconciled: "), task["last_error"]
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None


def test_a_termination_that_cannot_be_read_concludes_nothing(db, reconciler_config):
    """No exit code, no 78. The lease is judged by today's rules, which here do nothing yet."""
    seed_an_attempt_that_never_started(db, age_seconds=60)

    report, backend = reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        termination_raises=PermissionError("403 run.tasks.list"),
    )

    assert backend.termination_reads == [EXECUTION]
    assert report.outcomes == [], report.as_dict()
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.DISPATCHED.value
    assert any("403 run.tasks.list" in e for e in report.errors), report.errors


def test_a_78_from_an_attempt_the_task_has_moved_past_fails_nothing(db, reconciler_config):
    """A superseded attempt's 78 says nothing about the attempt that replaced it."""
    seed_an_attempt_that_never_started(db, age_seconds=60, task_generation=GENERATION + 1)

    reconcile(
        db,
        reconciler_config,
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)},
    )

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] != TaskState.FAILED.value, task
    assert "could not start" not in str(task.get("last_error")), task


def test_a_dry_run_reads_the_exit_code_and_writes_nothing(db, reconciler_config):
    from dataclasses import replace

    seed_an_attempt_that_never_started(db)
    before = len(db.writes)

    report, _ = reconcile(
        db,
        replace(reconciler_config, dry_run=True),
        executions=[finished_execution()],
        terminations={EXECUTION: ended(78, DNS_TERMINATION_MESSAGE)},
    )

    assert [o.kind for o in report.outcomes] == ["worker_cannot_start"]
    assert report.outcomes[0].skipped == "dry_run"
    assert db.writes[before:] == []


# ---------------------------------------------------------------------------
# the worker's words, read defensively
# ---------------------------------------------------------------------------


def test_the_cause_is_read_from_the_last_json_line_and_held_to_one_short_line():
    """The termination message is text a pod wrote. It reaches `last_error`, so it is bounded.

    New name, so red on main for its absence; the behavioural red is above.
    """
    from reconciler.detect import worker_cause

    assert worker_cause(DNS_TERMINATION_MESSAGE) == "DNS unreachable (firestore.googleapis.com)"
    assert worker_cause(json.dumps({"message": "only a message"})) == "only a message"
    # Not JSON (FallbackToLogsOnError, or a crash): the last non-empty line.
    assert worker_cause("Traceback ...\n  boom\nRuntimeError: boom\n\n") == "RuntimeError: boom"
    assert worker_cause("") is None
    assert worker_cause(None) is None
    noisy = worker_cause(json.dumps({"cause": "a\x1b[31m\nb\tc" + "x" * 5000}))
    assert noisy is not None
    assert "\n" not in noisy and "\x1b" not in noisy and "\t" not in noisy
    assert len(noisy) <= 500


def test_the_reconcilers_78_is_the_workers_78():
    """Two images, one number. Stated twice because nothing shared can hold it yet.

    The reconciler's image carries `apps/common` and `apps/reconciler` only, and
    the frozen contract has no worker exit codes (contract request 21 asks for
    them). Until then this is what stops the two drifting apart.
    """
    from agent_worker.errors import ExitCode
    from reconciler.detect import WORKER_EXIT_CANNOT_START

    assert WORKER_EXIT_CANNOT_START == ExitCode.CONFIG == CANNOT_START


# ---------------------------------------------------------------------------
# where each backend keeps the exit code
# ---------------------------------------------------------------------------


class _TasksClient:
    def __init__(self, tasks: list[Any]) -> None:
        self.tasks = tasks
        self.parents: list[str] = []

    def list_tasks(self, *, parent: str) -> list[Any]:
        self.parents.append(parent)
        return list(self.tasks)


def _cloud_run_task(exit_code: int, message: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        name=f"{EXECUTION}/tasks/{EXECUTION.rsplit('/', 1)[-1]}-task0",
        last_attempt_result=SimpleNamespace(
            exit_code=exit_code, status=SimpleNamespace(code=2, message=message)
        ),
    )


def test_cloud_run_reads_the_exit_code_from_its_task_attempt():
    """An execution carries counts only. The exit code is on the TASK's last attempt.

    `run.tasks.list` is already in swarmJobReaper (terraform/modules/iam/custom_roles.tf).
    """
    from reconciler.backends import CloudRunBackend

    tasks = _TasksClient([_cloud_run_task(78, "Task swarm-eng-mock-x78-task0 failed with exit code: 78")])
    backend = CloudRunBackend("p", "us-central1", tasks_client=tasks)

    found = backend.termination(finished_execution())

    assert tasks.parents == [EXECUTION]
    assert found is not None and found.exit_code == 78
    # Cloud Run's own words are kept as detail, never as the worker's cause.
    assert found.message is None
    assert "exit code: 78" in found.detail


def test_cloud_run_reports_no_exit_code_when_its_task_recorded_none():
    from reconciler.backends import CloudRunBackend

    backend = CloudRunBackend(
        "p", "us-central1", tasks_client=_TasksClient([_cloud_run_task(0, "")])
    )
    found = backend.termination(finished_execution())
    assert found is None or found.exit_code is None


class _CoreApi:
    """The namespaced pod list the `swarm-reaper` Role grants, and nothing else."""

    def __init__(self, pods_by_selector: dict[str, list[Any]]) -> None:
        self.pods_by_selector = pods_by_selector
        self.calls: list[tuple[str, str]] = []

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any:
        self.calls.append((namespace, label_selector))
        return SimpleNamespace(items=list(self.pods_by_selector.get(label_selector, [])))


def _pod(exit_code: int, message: str | None, *, container: str = "worker") -> SimpleNamespace:
    terminated = SimpleNamespace(exit_code=exit_code, message=message, reason="Error")
    return SimpleNamespace(
        metadata=SimpleNamespace(name="swarm-task-1-2-abcde", namespace="swarm-tenant-eng"),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    name=container,
                    state=SimpleNamespace(terminated=terminated),
                    last_state=SimpleNamespace(terminated=None),
                )
            ]
        ),
    )


def _gke_execution() -> ExecutionView:
    return ExecutionView(
        name="swarm-task-1-2",
        backend="GKE_AUTOPILOT",
        phase=ExecutionPhase.FAILED,
        created_at=utcnow() - timedelta(seconds=55),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=GENERATION,
        namespace="swarm-tenant-eng",
    )


def test_gke_reads_the_exit_code_and_the_termination_message_from_the_workers_pod():
    """`state.terminated` on the pod the Job created, read with `pods list` (already granted)."""
    from reconciler.backends import GkeBackend

    core = _CoreApi(
        {"batch.kubernetes.io/job-name=swarm-task-1-2": [_pod(78, DNS_TERMINATION_MESSAGE)]}
    )
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    found = backend.termination(_gke_execution())

    assert core.calls[0] == ("swarm-tenant-eng", "batch.kubernetes.io/job-name=swarm-task-1-2")
    assert found is not None and found.exit_code == 78
    assert found.message == DNS_TERMINATION_MESSAGE


def test_gke_falls_back_to_the_legacy_job_name_label():
    from reconciler.backends import GkeBackend

    core = _CoreApi({"job-name=swarm-task-1-2": [_pod(78, None)]})
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    found = backend.termination(_gke_execution())

    assert [selector for _, selector in core.calls] == [
        "batch.kubernetes.io/job-name=swarm-task-1-2",
        "job-name=swarm-task-1-2",
    ]
    assert found is not None and found.exit_code == 78 and found.message is None


def test_gke_reads_nothing_outside_the_tenant_namespace_prefix():
    """The same rule the Job list keeps: a namespace it cannot attribute is not read."""
    from dataclasses import replace

    from reconciler.backends import GkeBackend

    core = _CoreApi({})
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    assert backend.termination(replace(_gke_execution(), namespace="kube-system")) is None
    assert core.calls == []
