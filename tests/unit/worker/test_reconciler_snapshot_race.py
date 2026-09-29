"""A task missing from the snapshot is not a task that no longer exists (#332).

`ControlStore.snapshot()` reads `tasks` and `leases` in two queries. On
2026-09-29 the scheduler's admission of task_7ead19eefca1439ea9df committed
between them: the tasks query ran while the task was still READY, the leases
query ran after the commit. `detect_orphan_leases` saw a lease whose task was
absent and called it "a task that no longer exists". The repair fenced the task
(generation 1 -> 2), released the 2-second-old lease and skipped the task-state
repair because the task was not in `snapshot.tasks`. The task stayed LEASED,
pointing at a released lease, and no rule looked at it again.

Four things, each tested here with no emulator and no credentials:

  (a) the pass reads an absent task by id before calling its lease an orphan;
  (b) the fence and the release refuse, inside their transactions, while the
      task they re-read still holds the lease at the lease's generation;
  (c) a task in a concurrency state whose lease is released or missing is
      requeued (or failed once its attempts are spent) by a backstop rule,
      without returning any capacity a second time;
  (d) a genuine orphan -- task gone, or terminal -- is still released.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import Finding, FindingKind
from reconciler.logs import build_logger
from reconciler.model import ControlSnapshot
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

TENANT = "eng"
TASK = "task_7ead19eefca1439ea9df"
LEASE = "lease_ba0d3832c4a84c528565"
ATTEMPT = "att_114e3bae232a40c0a49c"


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


def _pools() -> list[str]:
    return pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )


def seed_task(
    db: FakeFirestore,
    *,
    task_id: str = TASK,
    state: TaskState = TaskState.LEASED,
    generation: int = 1,
    lease_id: str | None = LEASE,
    attempt_count: int = 1,
    max_attempts: int = 3,
) -> None:
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id, "tenant_id": TENANT, "state": state.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": generation, "current_lease_id": lease_id,
            "attempt_count": attempt_count, "max_attempts": max_attempts,
            "updated_at": utcnow(), "cancel_requested": False,
        },
    )


def seed_lease(
    db: FakeFirestore,
    *,
    lease_id: str = LEASE,
    task_id: str = TASK,
    generation: int = 1,
    age_seconds: int = 2,
    released: bool = False,
    pool_active: int = 1,
) -> None:
    """A lease exactly as admission writes it, `age_seconds` ago.

    `heartbeat_at` None and a dispatch deadline 300s out: a lease the stale-lease
    rule leaves alone, so the only rule that can touch it is the orphan rule.
    """
    now = utcnow()
    created = now - timedelta(seconds=age_seconds)
    db.seed(
        f"leases/{lease_id}",
        {
            "lease_id": lease_id, "task_id": task_id, "attempt_id": ATTEMPT,
            "tenant_id": TENANT, "generation": generation, "pools": _pools(), "units": 1,
            "state": TaskState.LEASED.value,
            "created_at": created,
            "dispatch_deadline": created + timedelta(seconds=300),
            "expires_at": created + timedelta(seconds=120),
            "heartbeat_at": None,
            "released_at": now - timedelta(seconds=1) if released else None,
            "release_reason": "reconciler:orphan_lease" if released else None,
        },
    )
    for pool in _pools():
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": pool_active,
                                  "enabled": True, "updated_at": now})


class _TasksQueryRanBeforeAdmission(ControlStore):
    """The snapshot of the 02:57:04 pass: the lease is in it, its task is not.

    Everything else -- `task_by_id`, every transaction -- reads the real
    documents, which is what Firestore did after the admission committed.
    """

    def __init__(self, *args, missing: str, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self._missing = missing

    def snapshot(self, **kwargs) -> ControlSnapshot:  # noqa: ANN003
        snap = super().snapshot(**kwargs)
        snap.tasks.pop(self._missing, None)
        return snap


def _store(db: FakeFirestore, *, missing: str | None = None) -> ControlStore:
    logger = build_logger(stream=__import__("io").StringIO())
    if missing is None:
        return ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return _TasksQueryRanBeforeAdmission(
        db, logger=logger, txn_runner=FakeTransactionRunner(db), missing=missing
    )


def _reconciler(store: ControlStore, config: ReconcilerConfig, db: FakeFirestore) -> Reconciler:
    backend = FakeBackend(executions=[], journal=db.writes)
    logger = build_logger(stream=__import__("io").StringIO())
    return Reconciler(store=store, backends=[backend], config=config, logger=logger)


def _active(db: FakeFirestore) -> dict[str, int]:
    return {pool: db.doc(f"pools/{pool}")["active"] for pool in _pools()}


# ---------------------------------------------------------------------------
# (a) absence from the snapshot is not absence
# ---------------------------------------------------------------------------


def test_a_just_admitted_lease_whose_task_the_snapshot_missed_is_not_an_orphan(db, config):
    seed_task(db)
    seed_lease(db)
    store = _store(db, missing=TASK)
    assert TASK not in store.snapshot().tasks, "the race this test exists for was not staged"

    report = _reconciler(store, config, db).run_once()

    orphan = [o for o in report.outcomes if o.kind == FindingKind.ORPHAN_LEASE.value]
    assert orphan == [], (
        "a lease whose task is merely absent from the snapshot was treated as naming "
        f"a task that no longer exists: {[o.as_dict() for o in orphan]}"
    )
    assert db.doc(f"leases/{LEASE}")["released_at"] is None, "the live lease was released"
    assert db.doc(f"tasks/{TASK}")["current_generation"] == 1, "the healthy task was fenced"
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.LEASED.value
    assert set(_active(db).values()) == {1}, "capacity for a live lease was returned"


def test_an_absent_task_that_cannot_be_read_is_not_called_gone(db, config):
    """A read that FAILS is not an absence either: nothing is concluded."""
    seed_task(db)
    seed_lease(db)

    class _Unreadable(_TasksQueryRanBeforeAdmission):
        def task_by_id(self, task_id: str):  # noqa: ANN201
            raise RuntimeError("deadline exceeded")

    logger = build_logger(stream=__import__("io").StringIO())
    store = _Unreadable(db, logger=logger, txn_runner=FakeTransactionRunner(db), missing=TASK)

    report = _reconciler(store, config, db).run_once()

    assert [o for o in report.outcomes if o.kind == FindingKind.ORPHAN_LEASE.value] == []
    assert db.doc(f"leases/{LEASE}")["released_at"] is None
    assert db.doc(f"tasks/{TASK}")["current_generation"] == 1


# ---------------------------------------------------------------------------
# (b) the repair re-reads the task and refuses
# ---------------------------------------------------------------------------


def test_the_orphan_lease_repair_refuses_while_the_task_holds_the_lease(db, config):
    """Even a finding that reaches `_repair` must not fence or release.

    The snapshot is minutes old by the time slow repairs ahead of this one are
    done; the transactions are the last place the truth is read.
    """
    seed_task(db)
    seed_lease(db)
    store = _store(db, missing=TASK)
    reconciler = _reconciler(store, config, db)
    snapshot = store.snapshot()
    finding = Finding(
        kind=FindingKind.ORPHAN_LEASE,
        reason="lease references a task that no longer exists",
        task_id=TASK,
        lease_id=LEASE,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=1,
    )

    outcome = reconciler._repair(finding, snapshot, {})

    assert outcome.invalidated_to is None, "the task holding this lease was fenced"
    assert outcome.released is False, "the lease its task still holds was released"
    assert db.doc(f"tasks/{TASK}")["current_generation"] == 1
    assert db.doc(f"leases/{LEASE}")["released_at"] is None
    assert set(_active(db).values()) == {1}


def test_the_store_guards_refuse_inside_their_transactions(db):
    seed_task(db)
    seed_lease(db)
    store = _store(db)

    assert store.invalidate_generation(TASK, 1, unless_holding_lease=LEASE) is None
    assert store.release_lease(
        LEASE, "reconciler:orphan_lease", refuse_while_task_holds_it=True
    ) is False
    assert db.doc(f"tasks/{TASK}")["current_generation"] == 1
    assert db.doc(f"leases/{LEASE}")["released_at"] is None
    assert set(_active(db).values()) == {1}


def test_the_release_guard_still_releases_a_lease_its_task_has_superseded(db):
    """The partial-repair lease: task fenced to 2, still naming the gen-1 lease.

    Holding it would leak its slots for ever -- the case
    test_orphan_lease_after_partial_repair.py exists for.
    """
    seed_task(db, generation=2)
    seed_lease(db, generation=1)
    store = _store(db)

    assert store.release_lease(
        LEASE, "reconciler:orphan_lease", refuse_while_task_holds_it=True
    ) is True
    assert set(_active(db).values()) == {0}


# ---------------------------------------------------------------------------
# (c) the backstop: a concurrency-state task whose lease is gone
# ---------------------------------------------------------------------------


def test_a_leased_task_whose_lease_is_released_is_requeued(db, config):
    """task_7ead19eefca1439ea9df as it has been since 02:57:06."""
    seed_task(db, generation=2)
    # Capacity already came back when the lease was released: every pool at 0.
    seed_lease(db, generation=1, age_seconds=9000, released=True, pool_active=0)

    report = _reconciler(_store(db), config, db).run_once()

    outcomes = [o for o in report.outcomes if o.task_id == TASK]
    assert outcomes, f"nothing reclaimed a LEASED task with a released lease: {report.as_dict()}"
    assert outcomes[0].kind == FindingKind.LEASELESS_TASK.value
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value
    assert task["current_lease_id"] is None
    assert task["current_generation"] == 3, "any worker of the old attempt must be fenced"
    # Invariants 1-3: the requeue returns nothing a second time.
    assert set(_active(db).values()) == {0}, "a released lease's capacity was returned twice"


def test_a_running_task_whose_lease_document_is_missing_is_requeued(db, config):
    seed_task(db, state=TaskState.RUNNING, generation=4, lease_id="lease_never_written")
    for pool in _pools():
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 0,
                                  "enabled": True, "updated_at": utcnow()})

    _reconciler(_store(db), config, db).run_once()

    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.READY.value
    assert set(_active(db).values()) == {0}


def test_the_backstop_fails_a_task_whose_attempts_are_spent(db, config):
    seed_task(db, generation=2, attempt_count=3, max_attempts=3)
    seed_lease(db, generation=1, age_seconds=9000, released=True, pool_active=0)

    _reconciler(_store(db), config, db).run_once()

    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.FAILED.value


def test_the_backstop_leaves_a_task_with_a_live_lease_alone(db, config):
    """The ordinary just-admitted task: its lease is unreleased."""
    seed_task(db)
    seed_lease(db)

    report = _reconciler(_store(db), config, db).run_once()

    assert report.outcomes == []
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.LEASED.value
    assert db.doc(f"tasks/{TASK}")["current_generation"] == 1


# ---------------------------------------------------------------------------
# (d) a genuine orphan is still released
# ---------------------------------------------------------------------------


def test_a_lease_whose_task_really_does_not_exist_is_released(db, config):
    seed_lease(db, task_id="task_gone", age_seconds=30)

    report = _reconciler(_store(db), config, db).run_once()

    orphan = [o for o in report.outcomes if o.kind == FindingKind.ORPHAN_LEASE.value]
    assert orphan and orphan[0].released is True
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None
    assert set(_active(db).values()) == {0}


@pytest.mark.parametrize("terminal", [TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED])
def test_a_lease_whose_task_is_terminal_is_released(db, config, terminal):
    # A terminal task that still names its lease: the worker ended without
    # releasing it. The task leaves the snapshot's states, so it is read by id.
    seed_task(db, state=terminal)
    seed_lease(db, age_seconds=30)

    report = _reconciler(_store(db), config, db).run_once()

    orphan = [o for o in report.outcomes if o.kind == FindingKind.ORPHAN_LEASE.value]
    assert orphan and orphan[0].released is True
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None
    assert set(_active(db).values()) == {0}
    assert db.doc(f"tasks/{TASK}")["state"] == terminal.value
