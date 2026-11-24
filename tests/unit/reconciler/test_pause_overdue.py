"""The pause-overdue rule (issue 7, docs/design/workflow-pause.md section 6).

A step of a paused workflow whose worker never reaches its control poll keeps
running and keeps its lease. Past `pause_enforce_after_seconds` the reconciler
stops the execution, and only on a confirmed kill fences the generation,
releases the lease and writes READY in one transaction; the scheduler's
admission gate then parks the step WORKFLOW_PAUSED.

Each test is one line of the design's lane P4:

* past the bound: terminated, generation bumped, lease released, READY, the
  finding's detail;
* inside the bound: untouched;
* a parked step: no finding;
* `cancel_requested`: only CANCEL_OVERDUE;
* the reference is `lease.created_at` for a lease newer than the pause;
* the snapshot reads paused workflows by `paused == true` alone;
* a resume between the kill and the repair: still released, still READY;
* the worker parked first: the repair refuses and writes nothing.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import FindingKind, detect_all
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

TENANT = "eng"
WORKFLOW = "wf_1"
EXECUTION = "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1"
BOUND = 600


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="example-project",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
        cancel_enforce_after_seconds=600,
        pause_enforce_after_seconds=BOUND,
    )


def _pools() -> list[str]:
    return pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )


def seed_paused_running(
    db: FakeFirestore,
    *,
    pause_age: float,
    lease_age: float = 13 * 3600,
    generation: int = 3,
    attempt_count: int = 1,
    metadata: dict[str, Any] | None = None,
    workflow_tenant: str = TENANT,
    cancel_age: float | None = None,
) -> None:
    """A RUNNING step of a paused workflow whose worker heartbeats but never polls."""
    now = utcnow()
    paused_at = now - timedelta(seconds=pause_age)
    pools = _pools()
    task: dict[str, Any] = {
        "id": "task_1", "tenant_id": TENANT, "state": TaskState.RUNNING.value,
        "runner_profile": "mock", "resource_class": "standard",
        "workflow_id": WORKFLOW,
        "current_generation": generation, "current_lease_id": "lease_1",
        "attempt_count": attempt_count, "max_attempts": 3,
        "updated_at": now - timedelta(seconds=5),
        # The raw mirror the pause route writes onto every non-terminal step.
        "pause_requested_at": paused_at,
    }
    if metadata is not None:
        task["metadata"] = metadata
    if cancel_age is not None:
        task["cancel_requested"] = True
        task["cancel_requested_at"] = now - timedelta(seconds=cancel_age)
    db.seed("tasks/task_1", task)
    db.seed(
        f"workflows/{WORKFLOW}",
        {
            "workflow_id": WORKFLOW, "tenant_id": workflow_tenant,
            "state": TaskState.RUNNING.value, "steps": [],
            "paused": True, "paused_at": paused_at, "paused_by": "member@example.com",
        },
    )
    db.seed(
        "leases/lease_1",
        {
            "lease_id": "lease_1", "task_id": "task_1", "attempt_id": "att_1",
            "tenant_id": TENANT, "generation": generation, "pools": pools, "units": 1,
            "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(seconds=lease_age),
            "dispatch_deadline": now - timedelta(seconds=lease_age) + timedelta(hours=1),
            # Alive: the heartbeat runs on its own thread, poll or no poll.
            "expires_at": now + timedelta(seconds=120),
            "heartbeat_at": now - timedelta(seconds=10),
            "released_at": None,
        },
    )
    db.seed(
        "attempts/att_1",
        {
            "attempt_id": "att_1", "task_id": "task_1", "tenant_id": TENANT,
            "generation": generation, "lease_id": "lease_1", "backend": "CLOUD_RUN_JOB",
            "created_at": now - timedelta(seconds=lease_age), "execution_name": EXECUTION,
            "started_at": now - timedelta(seconds=lease_age), "completed_at": None,
            "exit_code": None, "error": None,
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
        created_at=utcnow() - timedelta(hours=13),
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=generation,
        parent="projects/p/locations/us-central1/jobs/swarm-eng-mock",
    )


class HookedBackend(FakeBackend):
    """A backend that runs `on_terminate` once the kill is confirmed: what
    another writer commits between the reconciler's kill and its repair."""

    def __init__(self, *args: Any, on_terminate: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_terminate = on_terminate

    def terminate(self, execution: Any) -> bool:
        confirmed = super().terminate(execution)
        self._on_terminate()
        return confirmed


def build(db, config, *backends) -> Reconciler:
    logger = build_logger(stream=io.StringIO())
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=list(backends), config=config, logger=logger)


def store_for(db) -> ControlStore:
    return ControlStore(db, logger=build_logger(stream=io.StringIO()),
                        txn_runner=FakeTransactionRunner(db))


def findings_of(db, config, executions=None):
    snapshot = store_for(db).snapshot()
    return detect_all(
        snapshot,
        executions if executions is not None else [running_execution()],
        config,
        now=snapshot.taken_at,
    )


def _untouched(db: FakeFirestore, backend: FakeBackend) -> None:
    assert backend.terminated == [], "an execution was stopped inside the pause bound"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value
    assert task["current_generation"] == 3, "the generation was fenced"
    assert db.doc("leases/lease_1")["released_at"] is None, "the lease was released"
    for pool in _pools():
        assert db.doc(f"pools/{pool}")["active"] == 2


def _ready_event(db: FakeFirestore) -> dict[str, Any]:
    ready = [e for e in db.events("task_1") if e["type"] == EventType.READY.value]
    assert len(ready) == 1, db.event_types("task_1")
    return ready[0]


# --------------------------------------------------------------------------
# P-1  past the bound: kill, fence, release, READY
# --------------------------------------------------------------------------

def test_past_the_bound_the_step_is_stopped_released_and_made_ready(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend).run_once()

    kinds = [outcome.kind for outcome in report.outcomes]
    assert FindingKind.PAUSE_OVERDUE.value in kinds, kinds
    assert backend.terminated == [EXECUTION], "the paused step's execution was not stopped"

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task["state"]
    assert task["current_generation"] == 4, "the stopped attempt's generation was not fenced"
    assert task["current_lease_id"] is None
    # Not a failure: no requeue backoff (section 6.3).
    assert "next_eligible_at" not in task, task.get("next_eligible_at")
    assert db.doc("leases/lease_1")["released_at"] is not None, "the lease was kept"
    assert db.doc("leases/lease_1")["release_reason"] == "reconciler:pause_overdue"
    for pool in _pools():
        assert db.doc(f"pools/{pool}")["active"] == 1, f"{pool} was not given back"
    assert db.doc("attempts/att_1")["completed_at"] is not None

    # The kill comes BEFORE the release (invariants 1-3).
    ops = [(entry[0], entry[1]) for entry in db.writes]
    terminate_at = ops.index(("terminate", EXECUTION))
    lease_written_at = next(i for i, (_, path) in enumerate(ops) if path == "leases/lease_1")
    assert terminate_at < lease_written_at

    event = _ready_event(db)
    assert event["detail"]["finding"] == "pause_overdue"
    assert event["detail"]["paused_at"] is not None
    assert event["detail"]["pause_lifted"] is False


def test_the_finding_names_the_step_the_pause_and_the_overrun(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)

    overdue = [f for f in findings_of(db, config) if f.kind is FindingKind.PAUSE_OVERDUE]

    assert len(overdue) == 1, overdue
    finding = overdue[0]
    assert (finding.task_id, finding.lease_id, finding.attempt_id, finding.tenant_id,
            finding.generation) == ("task_1", "lease_1", "att_1", TENANT, 3)
    assert finding.requires_termination
    assert finding.execution is not None and finding.execution.name == EXECUTION
    detail = finding.detail
    assert set(detail) >= {
        "workflow_id", "task_state", "paused_at", "pause_age_seconds",
        "overdue_by_seconds", "measured_from", "namespace",
    }, detail
    assert detail["workflow_id"] == WORKFLOW
    assert detail["task_state"] == TaskState.RUNNING.value
    assert detail["measured_from"] == "paused_at"
    assert detail["pause_age_seconds"] == pytest.approx(BOUND + 300, abs=5)
    assert detail["overdue_by_seconds"] == pytest.approx(300, abs=5)
    assert f"workflow {WORKFLOW} paused" in finding.reason
    assert EXECUTION in finding.reason
    assert f"({BOUND}s)" in finding.reason


def test_the_requeue_is_refunded_up_to_five_times(db, config):
    seed_paused_running(db, pause_age=BOUND + 300, attempt_count=3)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, config, backend).run_once()

    task = db.doc("tasks/task_1")
    # Three of three attempts used: without the refund this would be FAILED.
    assert task["state"] == TaskState.READY.value, task.get("last_error")
    assert task["attempt_count"] == 2
    assert task["metadata"]["pause_refunds"] == 1


def test_past_five_refunds_the_requeue_counts(db, config):
    seed_paused_running(
        db, pause_age=BOUND + 300, attempt_count=3, metadata={"pause_refunds": 5}
    )
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, config, backend).run_once()

    task = db.doc("tasks/task_1")
    assert task["attempt_count"] == 3
    assert task["metadata"]["pause_refunds"] == 5
    assert task["state"] == TaskState.FAILED.value, "spent attempts past the refund bound"


# --------------------------------------------------------------------------
# P-2  inside the bound: the worker's own pause path is left alone
# --------------------------------------------------------------------------

def test_inside_the_bound_the_step_is_untouched(db, config):
    seed_paused_running(db, pause_age=BOUND - 120)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend).run_once()

    assert FindingKind.PAUSE_OVERDUE.value not in [o.kind for o in report.outcomes]
    _untouched(db, backend)


def test_a_dry_run_changes_nothing(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, replace(config, dry_run=True), backend).run_once()

    outcome = next(o for o in report.outcomes if o.kind == FindingKind.PAUSE_OVERDUE.value)
    assert outcome.skipped == "dry_run"
    _untouched(db, backend)


# --------------------------------------------------------------------------
# P-3  scope
# --------------------------------------------------------------------------

def test_a_parked_step_has_no_finding(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)
    # The worker honoured the pause: PARKED, lease released, off the lease.
    db.documents["tasks/task_1"].update(
        state=TaskState.PARKED.value, current_lease_id=None, park_reason="WORKFLOW_PAUSED"
    )
    db.documents["leases/lease_1"]["released_at"] = utcnow()

    kinds = [f.kind for f in findings_of(db, config)]

    assert FindingKind.PAUSE_OVERDUE not in kinds, kinds


def test_a_cancelled_step_gets_only_the_cancel_finding(db, config):
    seed_paused_running(db, pause_age=BOUND + 300, cancel_age=3600)

    kinds = [f.kind for f in findings_of(db, config)]

    assert FindingKind.CANCEL_OVERDUE in kinds, kinds
    assert FindingKind.PAUSE_OVERDUE not in kinds, kinds


def test_a_workflow_of_another_tenant_pauses_nothing(db, config):
    seed_paused_running(db, pause_age=BOUND + 300, workflow_tenant="other")

    kinds = [f.kind for f in findings_of(db, config)]

    assert FindingKind.PAUSE_OVERDUE not in kinds, kinds


def test_an_older_generations_execution_is_not_this_rules(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)

    kinds = [f.kind for f in findings_of(db, config, [running_execution(generation=2)])]

    assert FindingKind.PAUSE_OVERDUE not in kinds, kinds


def test_a_lifted_pause_has_no_finding(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)
    db.documents[f"workflows/{WORKFLOW}"].update(paused=False, paused_at=None)

    kinds = [f.kind for f in findings_of(db, config)]

    assert FindingKind.PAUSE_OVERDUE not in kinds, kinds


# --------------------------------------------------------------------------
# P-4  the reference time
# --------------------------------------------------------------------------

def test_a_lease_newer_than_the_pause_gets_the_full_bound_from_its_creation(db, config):
    # Paused two hours ago; leased in the admission race two minutes ago.
    seed_paused_running(db, pause_age=7200, lease_age=120)

    kinds = [f.kind for f in findings_of(db, config)]

    assert FindingKind.PAUSE_OVERDUE not in kinds, kinds


def test_past_the_bound_from_the_lease_the_finding_says_so(db, config):
    seed_paused_running(db, pause_age=7200, lease_age=BOUND + 60)

    overdue = [f for f in findings_of(db, config) if f.kind is FindingKind.PAUSE_OVERDUE]

    assert len(overdue) == 1, overdue
    detail = overdue[0].detail
    assert detail["measured_from"] == "lease_created_at"
    assert detail["overdue_by_seconds"] == pytest.approx(60, abs=5)
    assert detail["pause_age_seconds"] == pytest.approx(7200, abs=5)


# --------------------------------------------------------------------------
# P-5  the snapshot reads paused workflows by equality, never a scan
# --------------------------------------------------------------------------

class _RecordingQuery:
    """Delegates to the fake's query and records each `workflows` read's filters."""

    def __init__(self, inner: Any, reads: list, filters: tuple = ()) -> None:
        self._inner = inner
        self._reads = reads
        self._filters = filters

    def where(self, *args: Any, filter: Any = None, **kwargs: Any) -> "_RecordingQuery":
        recorded = (filter.field_path, filter.op_string, filter.value)
        return _RecordingQuery(
            self._inner.where(*args, filter=filter, **kwargs),
            self._reads,
            (*self._filters, recorded),
        )

    def order_by(self, *args: Any, **kwargs: Any) -> "_RecordingQuery":
        return _RecordingQuery(self._inner.order_by(*args, **kwargs), self._reads, self._filters)

    def start_after(self, *args: Any, **kwargs: Any) -> "_RecordingQuery":
        return _RecordingQuery(self._inner.start_after(*args, **kwargs), self._reads,
                               self._filters)

    def limit(self, *args: Any, **kwargs: Any) -> "_RecordingQuery":
        return _RecordingQuery(self._inner.limit(*args, **kwargs), self._reads, self._filters)

    def stream(self, **kwargs: Any):
        self._reads.append(self._filters)
        return self._inner.stream(**kwargs)

    def get(self, **kwargs: Any):
        self._reads.append(self._filters)
        return list(self._inner.stream(**kwargs))

    def document(self, *args: Any, **kwargs: Any):
        return self._inner.document(*args, **kwargs)


class RecordingFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_reads: list[tuple] = []

    def collection(self, name: str):
        inner = super().collection(name)
        if name == "workflows":
            return _RecordingQuery(inner, self.workflow_reads)
        return inner


def test_the_snapshot_reads_paused_workflows_by_paused_equal_true_alone(config):
    db = RecordingFirestore()
    seed_paused_running(db, pause_age=BOUND + 300)
    db.seed("workflows/wf_2", {"workflow_id": "wf_2", "tenant_id": TENANT,
                               "state": TaskState.RUNNING.value, "steps": [],
                               "paused": False, "paused_at": None})
    store = store_for(db)

    full = store.snapshot()
    incremental = store.incremental_snapshot(
        changed_since=utcnow() - timedelta(minutes=5), due_by=utcnow() + timedelta(minutes=1)
    )

    assert db.workflow_reads, "the snapshot never read the paused workflows"
    for filters in db.workflow_reads:
        assert filters == (("paused", "==", True),), filters
    paused_at = db.doc(f"workflows/{WORKFLOW}")["paused_at"]
    for snapshot in (full, incremental):
        assert snapshot.paused_workflows == {WORKFLOW: (TENANT, paused_at)}


def test_the_step_task_carries_its_workflow_id(db, config):
    seed_paused_running(db, pause_age=60)

    snapshot = store_for(db).snapshot()

    assert snapshot.tasks["task_1"].workflow_id == WORKFLOW


class _UnreadablePauses(FakeFirestore):
    def collection(self, name: str):
        if name == "workflows":
            raise RuntimeError("503 the workflows query timed out")
        return super().collection(name)


def test_an_unreadable_pause_read_stands_the_rule_down_and_says_so(config):
    """A failed pause read withholds only this rule's kill, never the pass."""
    db = _UnreadablePauses()
    seed_paused_running(db, pause_age=BOUND + 300)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend).run_once()

    assert any(e.startswith("paused_workflows:") for e in report.errors), report.errors
    assert FindingKind.PAUSE_OVERDUE.value not in [o.kind for o in report.outcomes]
    _untouched(db, backend)


# --------------------------------------------------------------------------
# P-6  races between the kill and the repair (section 6.3, 8.1)
# --------------------------------------------------------------------------

def test_a_resume_between_the_kill_and_the_repair_still_releases_and_requeues(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)

    def resume() -> None:
        db.documents[f"workflows/{WORKFLOW}"].update(paused=False, paused_at=None)
        db.documents["tasks/task_1"].pop("pause_requested_at", None)

    backend = HookedBackend(
        executions=[running_execution()], journal=db.writes, on_terminate=resume
    )

    build(db, config, backend).run_once()

    assert backend.terminated == [EXECUTION]
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task["state"]
    assert task["current_generation"] == 4
    assert db.doc("leases/lease_1")["released_at"] is not None, "a killed step kept its lease"
    for pool in _pools():
        assert db.doc(f"pools/{pool}")["active"] == 1
    assert _ready_event(db)["detail"]["pause_lifted"] is True


def test_a_worker_that_parked_first_makes_the_repair_refuse_and_write_nothing(db, config):
    seed_paused_running(db, pause_age=BOUND + 300)
    marks: dict[str, int] = {}

    def worker_parks() -> None:
        # `ControlPlane.park`'s one transaction: PARKED, the attempt's end and
        # the lease's release, with its pool units returned.
        now = utcnow()
        db.documents["tasks/task_1"].update(
            state=TaskState.PARKED.value, current_lease_id=None,
            park_reason="WORKFLOW_PAUSED", updated_at=now,
        )
        db.documents["leases/lease_1"].update(released_at=now, release_reason="parked")
        db.documents["attempts/att_1"].update(completed_at=now, exit_code=0)
        for pool in _pools():
            db.documents[f"pools/{pool}"]["active"] = 1
        marks["writes"] = len(db.writes)

    backend = HookedBackend(
        executions=[running_execution()], journal=db.writes, on_terminate=worker_parks
    )

    build(db, config, backend).run_once()

    assert backend.terminated == [EXECUTION]
    after = [path for _, path, _ in db.writes[marks["writes"]:]]
    touched = [
        path for path in after
        if path.startswith(("tasks/task_1", "leases/", "pools/", "attempts/"))
    ]
    assert touched == [], touched
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["current_generation"] == 3
    for pool in _pools():
        assert db.doc(f"pools/{pool}")["active"] == 1, "a pool was given back twice"


# --------------------------------------------------------------------------
# the bound itself (decision Q2)
# --------------------------------------------------------------------------

def test_the_bound_is_its_own_setting_defaulting_to_ten_minutes(monkeypatch):
    monkeypatch.delenv("PAUSE_ENFORCE_AFTER_SECONDS", raising=False)
    monkeypatch.delenv("CANCEL_ENFORCE_AFTER_SECONDS", raising=False)
    assert ReconcilerConfig(project_id="p", region="r", firestore_database="d") \
        .pause_enforce_after_seconds == 600
    monkeypatch.setenv("PROJECT_ID", "swarm-unit-test")
    monkeypatch.setenv("PAUSE_ENFORCE_AFTER_SECONDS", "1800")
    loaded = ReconcilerConfig.from_env()
    assert loaded.pause_enforce_after_seconds == 1800
    assert loaded.cancel_enforce_after_seconds == 600, "the pause bound moved the cancel bound"
