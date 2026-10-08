"""A fenced generation never sits behind a held lease (#560).

MEASURED ON DEV, 2026-10-04. Four mock tasks swarm-verify created at ~10:24Z
(task_59d1cff58cf8435fb9b0, task_d29b6d6ad4f44cbd9e4b,
task_8659e839438e470dbda5, task_316f072424a34354acaf) were still DISPATCHED
and still holding their leases more than ten hours later. For
task_59d1cff58cf8435fb9b0 the whole timeline was:

    10:24:08  lease_acquired, dispatched (swarm-job-eng-mock-r5877)
              -- attempt att_2b25ed9f3f2e41fcb37e never recorded a start
    10:33:07  generation_fenced  dead_worker, generation 1 -> 2,
              "lease silent for 536s (grace 90s, expired=True,
              dispatch_overdue=True)"
    10:53:58  cancel_requested   (from DISPATCHED, by swarm-verify)
    then nothing.

WHY. The repair fenced in its own transaction BEFORE it asked Cloud Run to
stop the execution, and returned without releasing when that stop was not
confirmed. Every later pass saw the lease superseded with its execution still
listed, left it to the obsolete-generation rule, whose fence was a no-op and
whose kill failed the same way, and so wrote nothing again. Its slots were
never returned and its cancel was never honoured.

What is pinned here:

  F-1  FENCE AND REPAIR ARE ONE. A repair that cannot stop the execution
       writes nothing at all, and one that can fences, releases and moves the
       task on in ONE transaction.
  F-2  A WORKER THAT NEVER STARTED is its own evidence once its lease is
       `never_started_release_seconds` old: the fence is written in the same
       transaction as the release, so if its container ever does start it
       exits at its generation check without running the agent (invariant 5).
  F-3  #450 STANDS for a worker that DID start: a missing execution whose
       backend cannot be read is held while its lease has ever heartbeated.
  F-4  THE EXISTING STATE -- fenced generation 2, a held generation-1 lease,
       cancel_requested, an attempt that never started -- is cleaned up by one
       pass, with no manual Firestore edit, whatever the backend says.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_reconciler_safety import TENANT, running_execution, seed_running_task

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.repair import HELD_PAST_TTL, Reconciler
from reconciler.store import ControlStore
from swarm_common.models import EndCause, utcnow
from swarm_common.states import EventType, TaskState

EXECUTION = "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1"


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=480,
        orphan_execution_grace_seconds=120,
        never_started_release_seconds=1200,
        enable_gke=False,
    )


class CountingRunner(FakeTransactionRunner):
    """Records which writes each transaction made, so a test can say "one"."""

    def __init__(self, db: FakeFirestore) -> None:
        super().__init__(db)
        self.spans: list[tuple[int, int]] = []

    def run(self, fn: Any, *, call_options: Any = None) -> Any:
        start = len(self._db.writes)
        try:
            return super().run(fn, call_options=call_options)
        finally:
            self.spans.append((start, len(self._db.writes)))

    def transaction_of(self, predicate: Any) -> int:
        """The index of the transaction that made the first write matching `predicate`."""
        for index, (start, end) in enumerate(self.spans):
            if any(predicate(entry) for entry in self._db.writes[start:end]):
                return index
        raise AssertionError("no transaction made that write")


def build(
    db: FakeFirestore, config: ReconcilerConfig, *backends: FakeBackend
) -> tuple[Reconciler, CountingRunner, io.StringIO]:
    stream = io.StringIO()
    logger = build_logger(stream=stream)
    runner = CountingRunner(db)
    store = ControlStore(db, logger=logger, txn_runner=runner)
    reconciler = Reconciler(
        store=store, backends=list(backends), config=config, logger=logger, hold_releaser=None
    )
    return reconciler, runner, stream


def never_started(
    db: FakeFirestore,
    *,
    age_seconds: int,
    task_generation: int = 1,
    cancel_requested: bool = False,
    state: TaskState = TaskState.DISPATCHED,
) -> None:
    """A Cloud Run attempt at generation 1 whose worker never wrote anything.

    The shape admission and dispatch leave behind: the lease has never
    heartbeated, its dispatch deadline (480 s) and its expiry (120 s) are
    measured from its creation, and the attempt records its execution and no
    start. `task_generation=2` is the task after the 10:33 fence.
    """
    seed_running_task(db, generation=1, state=state, pool_active=1)
    now = utcnow()
    created = now - timedelta(seconds=age_seconds)
    task = db.documents["tasks/task_1"]
    task["current_generation"] = task_generation
    task["cancel_requested"] = cancel_requested
    lease = db.documents["leases/lease_1"]
    lease.update(
        created_at=created,
        dispatch_deadline=created + timedelta(seconds=480),
        expires_at=created + timedelta(seconds=120),
        heartbeat_at=None,
        state=state.value,
    )
    attempt = db.documents["attempts/att_1"]
    attempt.update(created_at=created, started_at=None, execution_name=EXECUTION)


def pending_execution(age_seconds: int) -> Any:
    """Cloud Run's "created, not yet reporting": listed as active, generation 1."""
    return running_execution(attempt_id="att_1", generation=1, age_seconds=age_seconds)


def pools(db: FakeFirestore) -> dict[str, int]:
    return {
        path: int(doc.get("active", 0))
        for path, doc in db.documents.items()
        if path.startswith("pools/")
    }


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def assert_held_and_unfenced(db: FakeFirestore, generation: int = 1) -> None:
    assert db.doc("leases/lease_1")["released_at"] is None
    assert set(pools(db).values()) == {1}, pools(db)
    assert db.doc("tasks/task_1")["current_generation"] == generation
    assert db.doc("tasks/task_1")["state"] == TaskState.DISPATCHED.value


def assert_fenced_released_and_moved_on(
    db: FakeFirestore, runner: CountingRunner, *, to: TaskState
) -> None:
    """The lease is released, every pool is back at zero, the task has left
    DISPATCHED -- and the fence, the release and the move were ONE write."""
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert set(pools(db).values()) == {0}, pools(db)
    task = db.doc("tasks/task_1")
    assert task["state"] == to.value
    assert task["current_lease_id"] is None
    fenced = runner.transaction_of(
        lambda e: e[1] == "tasks/task_1" and "current_generation" in e[2]
    )
    released = runner.transaction_of(
        lambda e: e[1] == "leases/lease_1" and e[2].get("released_at") is not None
    )
    pooled = runner.transaction_of(lambda e: e[1].startswith("pools/") and "active" in e[2])
    moved = runner.transaction_of(lambda e: e[1] == "tasks/task_1" and "state" in e[2])
    assert fenced == released == pooled == moved, (
        "the fence, the release and the repair must commit together or not at all",
        fenced, released, pooled, moved,
    )


# ---------------------------------------------------------------------------
# F-1: no fence that the same transaction does not also repair
# ---------------------------------------------------------------------------


def test_an_unconfirmed_kill_leaves_no_fence_behind(db, config):
    """The 10:33 pass, reproduced: dispatch overdue, the execution listed, the
    kill refused. It used to commit the fence and return, which is how four
    leases were held for ten hours. Now it writes nothing it cannot finish."""
    never_started(db, age_seconds=600)
    backend = FakeBackend(
        executions=[pending_execution(600)],
        journal=db.writes,
        terminate_raises=RuntimeError("cannot be cancelled"),
    )
    reconciler, _, _ = build(db, config, backend)

    report = reconciler.run_once()

    assert [o.kind for o in report.outcomes] == ["dead_worker"]
    assert report.outcomes[0].invalidated_to is None
    assert report.outcomes[0].released is False
    assert_held_and_unfenced(db)
    assert EventType.GENERATION_FENCED.value not in db.event_types("task_1")


def test_a_confirmed_kill_fences_releases_and_requeues_in_one_transaction(db, config):
    seed_running_task(db, generation=1, pool_active=1)
    backend = FakeBackend(executions=[running_execution(generation=1)], journal=db.writes)
    reconciler, runner, _ = build(db, config, backend)

    report = reconciler.run_once()

    outcome = report.outcomes[0]
    assert (outcome.kind, outcome.terminated, outcome.released) == ("dead_worker", True, True)
    assert outcome.invalidated_to == 2
    assert_fenced_released_and_moved_on(db, runner, to=TaskState.READY)
    # The kill comes first now: nothing is fenced that cannot be finished.
    killed = next(i for i, e in enumerate(db.writes) if e[0] == "terminate")
    fenced = next(
        i for i, e in enumerate(db.writes)
        if e[1] == "tasks/task_1" and "current_generation" in e[2]
    )
    assert killed < fenced
    assert db.event_types("task_1")[-3:] == [
        EventType.GENERATION_FENCED.value,
        EventType.LEASE_RELEASED.value,
        EventType.READY.value,
    ]


def test_a_stale_lease_with_nothing_to_kill_is_one_transaction(db, config):
    seed_running_task(db, generation=1, pool_active=1)
    reconciler, runner, _ = build(db, config, FakeBackend(executions=[], journal=db.writes))

    reconciler.run_once()

    assert_fenced_released_and_moved_on(db, runner, to=TaskState.READY)


# ---------------------------------------------------------------------------
# F-2: a worker that never started, after the bounded wait
# ---------------------------------------------------------------------------


def test_a_never_started_worker_is_repaired_once_the_wait_is_over(db, config):
    """Same refusal to cancel as above, twenty-five minutes after dispatch."""
    never_started(db, age_seconds=1500)
    backend = FakeBackend(
        executions=[pending_execution(1500)],
        journal=db.writes,
        terminate_raises=RuntimeError("cannot be cancelled"),
    )
    reconciler, runner, _ = build(db, config, backend)

    report = reconciler.run_once()

    outcome = report.outcomes[0]
    assert outcome.terminated is False
    assert outcome.released is True
    assert outcome.invalidated_to == 2
    assert any("never started" in action for action in outcome.actions), outcome.actions
    assert_fenced_released_and_moved_on(db, runner, to=TaskState.READY)
    assert db.doc("tasks/task_1")["current_generation"] == 2


def test_a_never_started_worker_on_an_unreadable_backend_is_repaired_after_the_wait(db, config):
    """An execution that cannot be read must not hold capacity for ever."""
    never_started(db, age_seconds=1500)
    blind = FakeBackend(
        executions=[], journal=db.writes, list_raises=RuntimeError("Cloud Run unavailable")
    )
    reconciler, runner, _ = build(db, config, blind)

    reconciler.run_once()

    assert_fenced_released_and_moved_on(db, runner, to=TaskState.READY)


def test_inside_the_wait_an_unreadable_never_started_worker_is_held_and_said(db, config):
    """The control for the test above: the wait is a bound, not a shortcut."""
    never_started(db, age_seconds=1100)
    blind = FakeBackend(
        executions=[], journal=db.writes, list_raises=RuntimeError("Cloud Run unavailable")
    )
    # 620 s past the dispatch deadline: past a five-minute alert, so the hold
    # is reported, and still inside the twenty-minute wait.
    loud = ReconcilerConfig(**{**config.__dict__, "held_lease_alert_minutes": 5})
    reconciler, _, stream = build(db, loud, blind)

    report = reconciler.run_once()

    assert_held_and_unfenced(db)
    held = [entry for entry in report.held_past_ttl if entry["lease_id"] == "lease_1"]
    assert held, report.held_past_ttl
    assert any("never started" in why for why in held[0]["why"]), held[0]["why"]
    errors = [line for line in lines(stream) if line.get("message") == HELD_PAST_TTL]
    assert len(errors) == 1 and errors[0]["severity"] == "ERROR", errors


# ---------------------------------------------------------------------------
# F-3: #450 stands for a worker that started
# ---------------------------------------------------------------------------


def test_a_missing_execution_whose_worker_heartbeated_is_still_held_when_unreadable(db, config):
    """A lease that HAS heartbeated -- the worker started -- on a backend that
    cannot be read, long past every bound. Nothing proves the agent stopped."""
    seed_running_task(db, generation=1, pool_active=1, silent_seconds=3600)
    db.documents["attempts/att_1"]["execution_name"] = EXECUTION
    blind = FakeBackend(
        executions=[], journal=db.writes, list_raises=RuntimeError("Cloud Run unavailable")
    )
    reconciler, _, _ = build(db, config, blind)

    report = reconciler.run_once()

    assert report.outcomes == [], [o.as_dict() for o in report.outcomes]
    assert db.doc("leases/lease_1")["released_at"] is None
    assert set(pools(db).values()) == {1}
    assert db.doc("tasks/task_1")["current_generation"] == 1
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value


# ---------------------------------------------------------------------------
# F-4: the four tasks of #560, as they are in Firestore today
# ---------------------------------------------------------------------------


def incident(db: FakeFirestore) -> None:
    never_started(db, age_seconds=10 * 3600, task_generation=2, cancel_requested=True)


def assert_cancelled_and_freed(db: FakeFirestore) -> None:
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None
    assert set(pools(db).values()) == {0}, pools(db)
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value
    assert task["end_cause"] == EndCause.CANCEL_REQUESTED.value
    assert task["current_lease_id"] is None
    types = db.event_types("task_1")
    assert EventType.LEASE_RELEASED.value in types
    assert types[-1] == EventType.CANCELLED.value


@pytest.mark.parametrize(
    "backend_says",
    ["listed_and_will_not_cancel", "listed_and_cancels", "not_listed", "unreadable"],
)
def test_the_560_state_is_cleaned_up_by_one_pass(db, config, backend_says):
    incident(db)
    executions = (
        [pending_execution(10 * 3600)] if backend_says.startswith("listed") else []
    )
    backend = FakeBackend(
        executions=executions,
        journal=db.writes,
        terminate_raises=(
            RuntimeError("cannot be cancelled")
            if backend_says == "listed_and_will_not_cancel"
            else None
        ),
        list_raises=(
            RuntimeError("Cloud Run unavailable") if backend_says == "unreadable" else None
        ),
    )
    reconciler, _, _ = build(db, config, backend)

    reconciler.run_once()

    assert_cancelled_and_freed(db)
    # Not fenced a second time: generation 2 already stops anything at 1.
    assert db.doc("tasks/task_1")["current_generation"] == 2


def test_a_second_pass_over_the_cleaned_up_state_changes_nothing(db, config):
    incident(db)
    backend = FakeBackend(
        executions=[pending_execution(10 * 3600)],
        journal=db.writes,
        terminate_raises=RuntimeError("cannot be cancelled"),
    )
    reconciler, _, _ = build(db, config, backend)
    reconciler.run_once()
    before = dict(pools(db))

    reconciler.run_once()

    assert pools(db) == before == {path: 0 for path in before}
    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value


def test_the_tenant_is_never_crossed(db, config):
    """The repair writes only the incident's own task, lease and pools."""
    incident(db)
    reconciler, _, _ = build(db, config, FakeBackend(executions=[], journal=db.writes))
    reconciler.run_once()
    touched = {
        path.split("/")[0] + "/" + path.split("/")[1]
        for _, path, *_ in db.writes
        if not path.startswith(("reconciler_passes/", "pools/"))
    }
    # The incident's own superseded attempt gets its end in the same commit
    # (#630): att_1 is generation 1 of task_1, under a task fenced to 2.
    assert touched <= {"tasks/task_1", "leases/lease_1", "attempts/att_1"}, touched
    assert db.doc("tasks/task_1")["tenant_id"] == TENANT
    assert db.doc("attempts/att_1")["tenant_id"] == TENANT
