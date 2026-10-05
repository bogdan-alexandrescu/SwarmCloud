"""The reconciler notices a workflow that stops making progress (#616).

Before this, nothing asked "has this workflow moved?". The reconciler judged
tasks at the lease and execution level and the scheduler's safety tick promoted
parked steps, but RI10 (`wf_e39b3371d1984e31a655`, 2026-10-05) read PARKED with
its stored state wrong while its implement step had SUCCEEDED, and only an
operator's laptop watchdog would have seen it stay that way.

Owner decision, 2026-10-05: the check runs IN THE RECONCILER, on its existing
pass. Pinned here:

  (a) a step PARKED on DEPENDENCY_INCOMPLETE whose parents all SUCCEEDED is
      found, and promoted to READY once -- one guarded write, never twice;
  (b) a step DISPATCHED or STARTING past the start budget is found;
  (c) a RUNNING workflow with no step change for N minutes is found, and a
      step RUNNING with a fresh heartbeat is progress, not a stall;
  (d) a stored workflow state that disagrees with the derived one is found and
      corrected once;
  a terminal workflow is ignored, and an unreadable workflows read is a
  finding on the pass, never silence.

Offline: the worker suite's in-memory Firestore, no backend executions.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import WorkflowStallKind, detect_stalled_workflows
from reconciler.logs import build_logger
from reconciler.model import LeaseView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

TENANT = "eng"
WF = "wf_e39b3371d1984e31a655"


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        enable_gke=False,
        enable_checkpoint_gc=False,
        enable_gc=False,
    )


def seed_task(
    db: FakeFirestore,
    task_id: str,
    state: TaskState,
    *,
    ago: timedelta,
    park_reason: ParkReason | None = None,
    depends_on: tuple[str, ...] = (),
    generation: int = 1,
    lease_id: str | None = None,
) -> None:
    at = utcnow() - ago
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id, "tenant_id": TENANT, "state": state.value,
            "park_reason": park_reason.value if park_reason else None,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": generation, "current_lease_id": lease_id,
            "attempt_count": 1, "max_attempts": 3, "updated_at": at,
            "completed_at": at if state in (TaskState.SUCCEEDED, TaskState.FAILED) else None,
            "depends_on": list(depends_on), "workflow_id": WF,
            "cancel_requested": False, "last_error": None,
        },
    )


def seed_workflow(
    db: FakeFirestore,
    steps: list[tuple[str, str, tuple[str, ...]]],
    *,
    state: TaskState = TaskState.RUNNING,
    workflow_id: str = WF,
    on_step_failure: str = "fail_workflow",
) -> None:
    """`steps` is [(step_id, task_id, depends_on step ids)]."""
    db.seed(
        f"workflows/{workflow_id}",
        {
            "workflow_id": workflow_id, "tenant_id": TENANT, "state": state.value,
            "submitted_by": "alice@saga.xyz",
            "created_at": utcnow() - timedelta(hours=2),
            "updated_at": utcnow() - timedelta(hours=1),
            "steps": [
                {"step_id": s, "runner_profile": "mock", "input": {},
                 "depends_on": list(deps), "task_id": t}
                for s, t, deps in steps
            ],
            "on_step_failure": on_step_failure, "priority": 0,
            "cancel_requested": False,
        },
    )


def store_for(db: FakeFirestore) -> ControlStore:
    return ControlStore(
        db, logger=build_logger(stream=io.StringIO()), txn_runner=FakeTransactionRunner(db)
    )


def reconciler(db: FakeFirestore, config: ReconcilerConfig) -> Reconciler:
    logger = build_logger(stream=io.StringIO())
    return Reconciler(
        store=ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db)),
        backends=[FakeBackend(journal=db.writes)],
        config=config,
        logger=logger,
        hold_releaser=None,
    )


def detect(db: FakeFirestore, config: ReconcilerConfig, leases: dict[str, LeaseView] | None = None):
    read = store_for(db).workflows_for_stall_check(limit=config.workflow_scan_limit)
    return detect_stalled_workflows(read, leases or {}, config, utcnow())


def kinds(stalls: list[Any]) -> list[str]:
    return [s.kind.value for s in stalls]


def ri10(db: FakeFirestore, *, finished_ago: timedelta = timedelta(minutes=10)) -> None:
    """RI10's shape: implement SUCCEEDED, review PARKED on its dependency."""
    seed_workflow(
        db,
        [("implement", "task_impl", ()), ("review", "task_rev", ("implement",))],
        state=TaskState.PARKED,
    )
    seed_task(db, "task_impl", TaskState.SUCCEEDED, ago=finished_ago)
    seed_task(
        db, "task_rev", TaskState.PARKED, ago=timedelta(hours=1),
        park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=("task_impl",),
    )


# ---------------------------------------------------------------------------
# (a) a step parked on dependencies that are all satisfied
# ---------------------------------------------------------------------------


def test_a_step_parked_on_succeeded_parents_is_found(db, config):
    ri10(db)

    stalls = [s for s in detect(db, config) if s.kind is WorkflowStallKind.DEPENDENCIES_MET]

    assert len(stalls) == 1
    stall = stalls[0]
    assert (stall.workflow_id, stall.step_id, stall.task_id) == (WF, "review", "task_rev")
    assert stall.age_seconds is not None and stall.age_seconds >= 9 * 60
    assert "implement" in stall.reason


def test_a_step_whose_parent_has_not_finished_is_not_found(db, config):
    seed_workflow(
        db, [("implement", "task_impl", ()), ("review", "task_rev", ("implement",))],
    )
    seed_task(db, "task_impl", TaskState.RUNNING, ago=timedelta(minutes=1))
    seed_task(
        db, "task_rev", TaskState.PARKED, ago=timedelta(hours=1),
        park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=("task_impl",),
    )

    assert WorkflowStallKind.DEPENDENCIES_MET.value not in kinds(detect(db, config))


def test_a_parent_that_just_finished_is_the_schedulers_first(db, config):
    """Inside the promote grace the scheduler's one-minute tick gets there first."""
    ri10(db, finished_ago=timedelta(seconds=10))

    assert WorkflowStallKind.DEPENDENCIES_MET.value not in kinds(detect(db, config))


def test_the_parked_step_is_promoted_once_and_never_twice(db, config):
    ri10(db)
    rec = reconciler(db, config)

    first = rec.run_once()

    assert db.doc("tasks/task_rev")["state"] == TaskState.READY.value
    assert db.doc("tasks/task_rev")["park_reason"] is None
    entries = [e for e in first.stalled_workflows if e["kind"] == "dependencies_met"]
    assert len(entries) == 1
    assert entries[0]["repaired"] is True
    assert entries[0]["workflow_id"] == WF and entries[0]["task_id"] == "task_rev"
    assert db.event_types("task_rev").count(EventType.READY.value) == 1

    second = rec.run_once()

    assert [e for e in second.stalled_workflows if e["kind"] == "dependencies_met"] == []
    assert db.event_types("task_rev").count(EventType.READY.value) == 1


def test_the_promotion_is_guarded_on_the_state_it_read(db, config):
    """A step another writer moved after the read is left as that writer left it."""
    ri10(db)
    store = store_for(db)
    read = store.workflows_for_stall_check(limit=10)
    stall = next(
        s for s in detect_stalled_workflows(read, {}, config, utcnow())
        if s.kind is WorkflowStallKind.DEPENDENCIES_MET
    )
    # The scheduler's sweep promotes it first, and admission leases it.
    db.documents["tasks/task_rev"]["state"] = TaskState.LEASED.value

    skipped = store.promote_workflow_step(
        stall.task_id, generation=stall.generation, parent_task_ids=stall.parents
    )

    assert skipped == "state_changed"
    assert db.doc("tasks/task_rev")["state"] == TaskState.LEASED.value


def test_a_newer_generation_is_not_promoted(db, config):
    """Invariant 5: a write decided at one generation never lands on another."""
    ri10(db)
    store = store_for(db)

    skipped = store.promote_workflow_step("task_rev", generation=0, parent_task_ids=("task_impl",))

    assert skipped == "generation_changed"
    assert db.doc("tasks/task_rev")["state"] == TaskState.PARKED.value


def test_a_failed_fail_workflow_workflow_is_not_promoted(db, config):
    """The scheduler cancels its unstarted steps; promoting one would run it."""
    seed_workflow(
        db,
        [("a", "task_a", ()), ("b", "task_b", ()), ("c", "task_c", ("a",))],
        state=TaskState.PARKED,
    )
    seed_task(db, "task_a", TaskState.SUCCEEDED, ago=timedelta(minutes=10))
    seed_task(db, "task_b", TaskState.FAILED, ago=timedelta(minutes=10))
    seed_task(
        db, "task_c", TaskState.PARKED, ago=timedelta(hours=1),
        park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=("task_a",),
    )

    reconciler(db, config).run_once()

    assert db.doc("tasks/task_c")["state"] == TaskState.PARKED.value


# ---------------------------------------------------------------------------
# (b) a step DISPATCHED or STARTING past the start budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [TaskState.DISPATCHED, TaskState.STARTING])
def test_a_step_past_the_start_budget_is_found(db, config, state):
    seed_workflow(db, [("implement", "task_impl", ())])
    seed_task(db, "task_impl", state, ago=timedelta(minutes=15))

    stalls = [s for s in detect(db, config) if s.kind is WorkflowStallKind.START_OVERDUE]

    assert len(stalls) == 1
    assert stalls[0].task_id == "task_impl"
    assert stalls[0].age_seconds >= 15 * 60 - 5
    assert state.value in stalls[0].reason


def test_a_step_inside_the_start_budget_is_not_found(db, config):
    seed_workflow(db, [("implement", "task_impl", ())])
    seed_task(db, "task_impl", TaskState.DISPATCHED, ago=timedelta(minutes=3))

    assert WorkflowStallKind.START_OVERDUE.value not in kinds(detect(db, config))


def test_the_start_budget_defaults_to_ten_minutes():
    assert ReconcilerConfig.workflow_start_budget_seconds == 600


# ---------------------------------------------------------------------------
# (c) no step change for N minutes on a RUNNING workflow
# ---------------------------------------------------------------------------


def _lease(task_id: str, heartbeat_ago: timedelta | None) -> LeaseView:
    now = utcnow()
    return LeaseView(
        lease_id=f"lease-{task_id}", task_id=task_id, attempt_id="att_1",
        tenant_id=TENANT, generation=1, pools=(), units=1, state=TaskState.RUNNING,
        created_at=now - timedelta(hours=2), dispatch_deadline=None,
        expires_at=now + timedelta(minutes=2),
        heartbeat_at=None if heartbeat_ago is None else now - heartbeat_ago,
    )


def test_a_running_workflow_with_no_change_for_n_minutes_is_found(db, config):
    seed_workflow(db, [("implement", "task_impl", ())])
    seed_task(db, "task_impl", TaskState.RUNNING, ago=timedelta(minutes=50), lease_id="lease-task_impl")
    leases = {"lease-task_impl": _lease("task_impl", heartbeat_ago=timedelta(minutes=20))}

    stalls = [s for s in detect(db, config, leases) if s.kind is WorkflowStallKind.NO_PROGRESS]

    assert len(stalls) == 1
    assert stalls[0].workflow_id == WF and stalls[0].task_id == "task_impl"
    assert stalls[0].age_seconds >= 50 * 60 - 5


def test_a_running_step_with_a_fresh_heartbeat_is_progress(db, config):
    seed_workflow(db, [("implement", "task_impl", ())])
    seed_task(db, "task_impl", TaskState.RUNNING, ago=timedelta(minutes=50), lease_id="lease-task_impl")
    leases = {"lease-task_impl": _lease("task_impl", heartbeat_ago=timedelta(seconds=20))}

    assert WorkflowStallKind.NO_PROGRESS.value not in kinds(detect(db, config, leases))


def test_a_recent_step_change_is_progress(db, config):
    seed_workflow(db, [("implement", "task_impl", ())])
    seed_task(db, "task_impl", TaskState.RUNNING, ago=timedelta(minutes=5))

    assert WorkflowStallKind.NO_PROGRESS.value not in kinds(detect(db, config))


def test_the_no_progress_window_defaults_to_forty_five_minutes():
    assert ReconcilerConfig.workflow_no_progress_minutes == 45


# ---------------------------------------------------------------------------
# (d) a stored state that disagrees with the derived one
# ---------------------------------------------------------------------------


def test_a_drifted_stored_state_is_found_and_corrected_once(db, config):
    # RI10's other half: stored RUNNING, steps say PARKED. The parked step's
    # parents are not finished, so (a) does not fire and only the drift is left.
    seed_workflow(
        db, [("implement", "task_impl", ()), ("review", "task_rev", ("implement",))],
        state=TaskState.RUNNING,
    )
    seed_task(
        db, "task_impl", TaskState.PARKED, ago=timedelta(minutes=5),
        park_reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
    )
    seed_task(
        db, "task_rev", TaskState.PARKED, ago=timedelta(hours=1),
        park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=("task_impl",),
    )
    rec = reconciler(db, config)

    first = rec.run_once()

    drift = [e for e in first.stalled_workflows if e["kind"] == "state_drift"]
    assert len(drift) == 1
    assert drift[0]["repaired"] is True
    assert "RUNNING" in drift[0]["reason"] and "PARKED" in drift[0]["reason"]
    assert db.doc(f"workflows/{WF}")["state"] == TaskState.PARKED.value
    writes = [w for w in db.writes if w[1] == f"workflows/{WF}"]

    second = rec.run_once()

    assert [e for e in second.stalled_workflows if e["kind"] == "state_drift"] == []
    assert [w for w in db.writes if w[1] == f"workflows/{WF}"] == writes


def test_the_drift_write_is_guarded_on_the_stored_state_it_read(db, config):
    seed_workflow(db, [("implement", "task_impl", ())], state=TaskState.QUEUED)
    store = store_for(db)

    skipped = store.write_derived_workflow_state(
        WF, expected=TaskState.RUNNING, to=TaskState.SUCCEEDED
    )

    assert skipped == "state_changed"
    assert db.doc(f"workflows/{WF}")["state"] == TaskState.QUEUED.value


def test_a_workflow_whose_step_cannot_be_read_is_not_rewritten(db, config):
    """A partial read is not evidence: it is a finding, and nothing is written."""
    seed_workflow(db, [("a", "task_a", ()), ("b", "task_gone", ())], state=TaskState.QUEUED)
    seed_task(db, "task_a", TaskState.SUCCEEDED, ago=timedelta(minutes=10))

    report = reconciler(db, config).run_once()

    assert db.doc(f"workflows/{WF}")["state"] == TaskState.QUEUED.value
    unreadable = [e for e in report.stalled_workflows if e["kind"] == "unreadable"]
    assert len(unreadable) == 1 and unreadable[0]["step_id"] == "b"


# ---------------------------------------------------------------------------
# Scope, the report, and a read that fails
# ---------------------------------------------------------------------------


def test_a_terminal_workflow_is_ignored(db, config):
    seed_workflow(
        db, [("implement", "task_impl", ()), ("review", "task_rev", ("implement",))],
        state=TaskState.SUCCEEDED,
    )
    seed_task(db, "task_impl", TaskState.SUCCEEDED, ago=timedelta(hours=3))
    # A step that would be rule (a)'s finding, and a stored state that (d)
    # would call drift, on a workflow whose stored state is terminal.
    seed_task(
        db, "task_rev", TaskState.PARKED, ago=timedelta(hours=3),
        park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=("task_impl",),
    )

    report = reconciler(db, config).run_once()

    assert report.stalled_workflows == []
    assert report.workflow_check["examined"] == 0
    assert db.doc("tasks/task_rev")["state"] == TaskState.PARKED.value
    assert db.doc(f"workflows/{WF}")["state"] == TaskState.SUCCEEDED.value


def test_every_finding_is_on_the_persisted_pass(db, config):
    ri10(db)

    reconciler(db, config).run_once()

    passes = [d for p, d in db.documents.items() if p.startswith("reconciler_passes/")]
    assert len(passes) == 1
    stored = passes[0]
    assert stored["workflow_check"]["read_error"] is None
    assert stored["workflow_check"]["examined"] == 1
    entry = next(e for e in stored["stalled_workflows"] if e["kind"] == "dependencies_met")
    assert set(entry) >= {
        "workflow_id", "tenant_id", "step_id", "task_id", "kind", "age_seconds",
        "reason", "repaired",
    }
    assert entry["tenant_id"] == TENANT


def test_a_dry_run_reports_and_writes_nothing(db, config):
    ri10(db)
    from dataclasses import replace

    report = reconciler(db, replace(config, dry_run=True)).run_once()

    assert db.doc("tasks/task_rev")["state"] == TaskState.PARKED.value
    entry = next(e for e in report.stalled_workflows if e["kind"] == "dependencies_met")
    assert entry["repaired"] is False
    assert "dry run" in entry["repair"]


class _UnreadableWorkflows(FakeFirestore):
    def collection(self, name: str) -> Any:
        if name == "workflows":
            raise RuntimeError("503 the workflows query timed out")
        return super().collection(name)


def test_an_unreadable_workflows_read_is_a_finding_not_silence(config):
    db = _UnreadableWorkflows()

    report = reconciler(db, config).run_once()

    assert report.workflow_check["read_error"] is not None
    assert "timed out" in report.workflow_check["read_error"]
    assert any(e.startswith("workflow_stall_check") for e in report.errors)
    stored = next(d for p, d in db.documents.items() if p.startswith("reconciler_passes/"))
    assert "timed out" in stored["workflow_check"]["read_error"]


def test_findings_are_worst_first(db, config):
    seed_workflow(db, [("implement", "task_impl", ())], workflow_id="wf_slow")
    seed_task(db, "task_impl", TaskState.STARTING, ago=timedelta(minutes=30))
    seed_workflow(db, [("x", "task_x", ())], workflow_id="wf_drift", state=TaskState.QUEUED)
    seed_task(db, "task_x", TaskState.READY, ago=timedelta(minutes=1))
    db.documents["tasks/task_x"]["workflow_id"] = "wf_drift"

    order = kinds(detect(db, config))

    assert order.index("start_overdue") < order.index("state_drift"), order
