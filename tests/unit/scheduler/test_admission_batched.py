"""Admission reserves a batch of READY tasks in one transaction (BGD, 2026-11-06).

The load analysis of 2026-11-06 found `pools/global` taking 52 writes in its
busiest minute -- one per admitted task, each its own transaction -- against
Firestore's sustained ceiling of about one write per second per document, and
ready-to-leased p99 of 476 s (1,680 s browser, 2,843 s large). Pinned here,
against the production `SchedulerStore` over the control-plane suite's
in-memory Firestore:

  B-1  three tasks that fit are admitted in ONE transaction that writes each
       pool document once, with the summed reservation;
  B-2  a task that does not fit stays READY, takes nothing, and the rest of
       the batch still admits -- including a task the batch's own earlier
       admissions crowd out (each task all-or-nothing, invariant 2);
  B-3  two schedulers racing for one pool never over-reserve it;
  B-4  the batch keeps the drain's priority and fairness order;
  B-5  a contended batch transaction skips the batch, wrote nothing, and the
       drain goes on (#1822);
  B-6  the setting is bounded.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from google.api_core import exceptions as gexc

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    RecordingDispatcher,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeDocumentRef, FakeFirestore, FakeTransaction  # noqa: E402

from swarm_common.admission import AdmissionConfig, AdmissionDenied  # noqa: E402
from swarm_common.models import Lease  # noqa: E402

from scheduler import loop as loop_mod  # noqa: E402
from scheduler.dispatch import BackendRouter  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import AdmissionRequest, SchedulerStore  # noqa: E402

BACKEND = "cloud_run_jobs"


class _CountingTransaction(FakeTransaction):
    """`FakeTransaction` that records, per committed transaction, the paths it
    wrote; and, when racing, aborts a commit whose read set changed, as
    Firestore's optimistic concurrency does."""

    def __init__(self, db: "_Db") -> None:
        super().__init__(db)
        self._read_set: dict[str, Any] = {}

    def _clean_up(self) -> None:
        super()._clean_up()
        self._read_set = {}

    def get(self, ref: Any, **kwargs: Any) -> Any:
        result = super().get(ref, **kwargs)
        if isinstance(ref, FakeDocumentRef):
            self._read_set.setdefault(ref.path, copy.deepcopy(result.to_dict()))
            self._db.after_read(ref.path)
        return result

    def _commit(self) -> list[Any]:
        for path, seen in self._read_set.items():
            if self._db.docs.get(path) != seen:
                self._db.aborts.append(path)
                self._buffer = []
                raise gexc.Aborted(f"{path} changed after this transaction read it")
        self._db.committed_writes.append([ref.path for _op, ref, _data in self._buffer])
        return super()._commit()


class _Db(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.committed_writes: list[list[str]] = []
        self.aborts: list[str] = []
        self._after_read: dict[str, Callable[[], None]] = {}

    def transaction(self, **kwargs: Any) -> _CountingTransaction:
        return _CountingTransaction(self)

    def interleave(self, path: str, writer: Callable[[], None]) -> None:
        """Run `writer` once, right after the next transactional read of `path`."""
        self._after_read[path] = writer

    def after_read(self, path: str) -> None:
        writer = self._after_read.pop(path, None)
        if writer is not None:
            writer()

    def admissions(self) -> list[list[str]]:
        """The committed transactions that reserved capacity: the only ones,
        in these tests, that write a pool (nothing here releases a lease)."""
        return [w for w in self.committed_writes if any(p.startswith("pools/") for p in w)]


@pytest.fixture
def db() -> _Db:
    return _Db()


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch) -> None:
    monkeypatch.setattr(loop_mod, "_sleep", lambda _s: None, raising=False)


def _scheduler(db: FakeFirestore, **overrides: Any) -> tuple[Scheduler, RecordingDispatcher]:
    settings = scheduler_settings(**overrides)
    dispatcher = RecordingDispatcher()
    scheduler = Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )
    return scheduler, dispatcher


def _seed(db: FakeFirestore, tasks: list[tuple[str, str]], *, priorities: dict[str, int] | None = None) -> None:
    """READY tasks, oldest first in the order given."""
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for i, (task_id, tenant_id) in enumerate(tasks):
        seed_task(db=db, task_id=task_id, tenant_id=tenant_id,
                  created_at=base + timedelta(seconds=i),
                  priority=(priorities or {}).get(task_id, 0))


def _state(db: FakeFirestore, task_id: str) -> str:
    return db.docs[f"tasks/{task_id}"]["state"]


def _leases(db: FakeFirestore) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("leases/")]


def _leased(db: FakeFirestore) -> set[str]:
    return {d["task_id"] for d in _leases(db)}


# --------------------------------------------------------------------------
# B-1
# --------------------------------------------------------------------------

def test_three_tasks_that_fit_are_admitted_in_one_transaction_with_one_write_per_pool(db):
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)
    _seed(db, [("t1", "eng"), ("t2", "eng"), ("t3", "eng")])
    scheduler, dispatcher = _scheduler(db)

    report = scheduler.drain()

    assert report.leased == 3 and report.dispatched == 3, report.to_dict()
    admissions = db.admissions()
    # MUTATION: admit each task in its own transaction (the frozen
    # `acquire_lease` per task, as before 2026-11-06) and this is 3.
    assert len(admissions) == 1, admissions
    (writes,) = admissions
    pool_writes = [p for p in writes if p.startswith("pools/")]
    # MUTATION: write a pool once per task inside the batch and global
    # appears three times here.
    assert sorted(pool_writes) == sorted(set(pool_writes)), pool_writes
    assert "pools/global" in pool_writes and "pools/tenant:eng" in pool_writes
    # The summed reservation, and one lease per task with its own generation.
    assert db.docs["pools/global"]["active"] == 3
    assert db.docs["pools/tenant:eng"]["active"] == 3
    assert _leased(db) == {"t1", "t2", "t3"}
    for task_id in ("t1", "t2", "t3"):
        task = db.docs[f"tasks/{task_id}"]
        lease = db.docs[f"leases/{task['current_lease_id']}"]
        assert lease["generation"] == task["current_generation"] == 1
        assert task["attempt_count"] == 1


# --------------------------------------------------------------------------
# B-2
# --------------------------------------------------------------------------

def test_a_task_that_does_not_fit_stays_ready_while_the_others_admit(db):
    seed_tenant(db, "eng", max_active=10)
    seed_tenant(db, "full", max_active=1)
    seed_pool(db, "tenant:full", hard_limit=1, active=1)
    seed_pool(db, "global", hard_limit=10)
    _seed(db, [("t1", "eng"), ("t2", "full"), ("t3", "eng"), ("t4", "eng")])
    scheduler, _dispatcher = _scheduler(db)

    report = scheduler.drain()

    assert _leased(db) == {"t1", "t3", "t4"}
    assert _state(db, "t2") == "READY"
    assert db.docs["tasks/t2"]["blocked_by"], "the denial is recorded on the task"
    assert report.denied >= 1
    # The full tenant's task took NOTHING in any pool (invariant 2).
    assert db.docs["pools/global"]["active"] == 3
    assert db.docs["pools/tenant:full"]["active"] == 1
    # MUTATION: one transaction per task and this is 3.
    assert len(db.admissions()) == 1, db.admissions()


def test_a_task_the_batch_itself_crowds_out_takes_nothing(db):
    """Judged against the pools as the batch's earlier tasks left them."""
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=2)
    _seed(db, [("t1", "eng"), ("t2", "eng"), ("t3", "eng")])
    store = SchedulerStore(db)
    requests = [AdmissionRequest(task=store.get_task(t), units=1, backend=BACKEND)
                for t in ("t1", "t2", "t3")]

    outcomes = store.acquire_leases(requests, config=AdmissionConfig())

    assert [type(o) for o in outcomes] == [Lease, Lease, AdmissionDenied]
    assert outcomes[2].reasons[0]["pool"] == "global"
    assert db.docs["pools/global"]["active"] == 2
    # MUTATION: reserve a denied task's other pools (all-or-nothing broken)
    # and the tenant pool reads 3.
    assert db.docs["pools/tenant:eng"]["active"] == 2
    assert _state(db, "t3") == "READY"
    assert len(db.committed_writes) == 1
    assert db.committed_writes[0].count("pools/global") == 1


def test_a_task_no_longer_ready_or_repeated_is_denied_and_the_rest_admit(db):
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)
    _seed(db, [("t1", "eng"), ("t2", "eng")])
    store = SchedulerStore(db)
    t1, t2 = store.get_task("t1"), store.get_task("t2")
    db.docs["tasks/t2"]["state"] = "CANCELLED"

    outcomes = store.acquire_leases(
        [AdmissionRequest(task=t, units=1, backend=BACKEND) for t in (t1, t2, t1)],
        config=AdmissionConfig(),
    )

    assert isinstance(outcomes[0], Lease)
    assert outcomes[1].reasons[0]["reason"] == "not_ready"
    assert outcomes[2].reasons[0]["reason"] == "not_ready"
    assert db.docs["pools/global"]["active"] == 1
    assert len(_leases(db)) == 1


# --------------------------------------------------------------------------
# B-3
# --------------------------------------------------------------------------

def test_two_schedulers_racing_never_over_reserve_a_pool(db):
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=3)
    _seed(db, [("a1", "eng"), ("a2", "eng"), ("a3", "eng"), ("b1", "eng"), ("b2", "eng")])
    first, second = SchedulerStore(db), SchedulerStore(db)
    mine = [AdmissionRequest(task=first.get_task(t), units=1, backend=BACKEND)
            for t in ("a1", "a2", "a3")]
    theirs = [AdmissionRequest(task=second.get_task(t), units=1, backend=BACKEND)
              for t in ("b1", "b2")]
    raced: list[Any] = []
    # The other scheduler commits its batch between this one's read of the
    # global pool and its commit.
    db.interleave("pools/global", lambda: raced.extend(
        second.acquire_leases(theirs, config=AdmissionConfig())))

    outcomes = first.acquire_leases(mine, config=AdmissionConfig())

    assert db.aborts, "nothing raced: the test proved nothing"
    assert [type(o) for o in raced] == [Lease, Lease]
    # Re-run against fresh reads: one slot was left.
    assert [type(o) for o in outcomes] == [Lease, AdmissionDenied, AdmissionDenied]
    # MUTATION: write `active` from the batch's first read instead of
    # re-running, and this is 3 with 4 leases -- or 5 if the limit is ignored.
    assert db.docs["pools/global"]["active"] == 3
    assert len(_leases(db)) == 3
    assert sum(1 for t in ("a1", "a2", "a3", "b1", "b2") if _state(db, t) == "LEASED") == 3


def test_two_drains_racing_never_over_reserve_a_pool(db):
    seed_tenant(db, "eng", max_active=10)
    seed_tenant(db, "ops", max_active=10)
    seed_pool(db, "global", hard_limit=4)
    _seed(db, [("e1", "eng"), ("e2", "eng"), ("e3", "eng"), ("o1", "ops"), ("o2", "ops"), ("o3", "ops")])
    scheduler_a, _ = _scheduler(db)
    scheduler_b, _ = _scheduler(db)
    db.interleave("pools/global", scheduler_b.drain)

    scheduler_a.drain()

    assert db.aborts, "nothing raced: the test proved nothing"
    assert db.docs["pools/global"]["active"] == 4
    assert len(_leases(db)) == 4
    held = sum(1 for d in _leases(db))
    assert held == db.docs["pools/global"]["active"]


# --------------------------------------------------------------------------
# B-4
# --------------------------------------------------------------------------

def test_priority_order_is_kept_within_a_batch(db):
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=2)
    _seed(db, [("low1", "eng"), ("low2", "eng"), ("high1", "eng"), ("high2", "eng")],
          priorities={"high1": 50, "high2": 40})
    scheduler, dispatcher = _scheduler(db)

    scheduler.drain()

    # MUTATION: judge the batch in query order (oldest first) and the two
    # low-priority tasks take the two slots.
    assert _leased(db) == {"high1", "high2"}
    assert _state(db, "low1") == "READY" and _state(db, "low2") == "READY"
    assert len(db.admissions()) == 1


def test_fairness_order_is_kept_within_a_batch(db):
    seed_tenant(db, "big", max_active=10)
    seed_tenant(db, "small", max_active=10)
    seed_pool(db, "global", hard_limit=2)
    _seed(db, [("big1", "big"), ("big2", "big"), ("big3", "big"), ("small1", "small")])
    scheduler, _dispatcher = _scheduler(db)

    scheduler.drain()

    # Round-robin: one each, not the big tenant's two oldest.
    assert _leased(db) == {"big1", "small1"}
    assert len(db.admissions()) == 1


# --------------------------------------------------------------------------
# B-5
# --------------------------------------------------------------------------

class _AbortingStore(SchedulerStore):
    def __init__(self, db: FakeFirestore, aborts: int) -> None:
        super().__init__(db)
        self.aborts = aborts
        self.batches: list[list[str]] = []

    def acquire_leases(self, requests, *, config):  # type: ignore[override]
        self.batches.append([r.task.id for r in requests])
        if self.aborts:
            self.aborts -= 1
            raise gexc.Aborted("409 contention")
        return super().acquire_leases(requests, config=config)


def test_a_contended_batch_wrote_nothing_and_the_drain_goes_on(db):
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)
    _seed(db, [(f"t{i}", "eng") for i in range(4)])
    settings = scheduler_settings(admission_batch_size=2)
    store = _AbortingStore(db, aborts=1)
    dispatcher = RecordingDispatcher()
    scheduler = Scheduler(
        settings=settings, store=store,
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )

    report = scheduler.drain()

    assert store.batches[:2] == [["t0", "t1"], ["t2", "t3"]]
    assert report.contended == 2
    # The aborted pair is retried on the next pass and admitted.
    assert _leased(db) == {"t0", "t1", "t2", "t3"}
    assert db.docs["pools/global"]["active"] == 4


# --------------------------------------------------------------------------
# B-6
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 51])
def test_the_batch_size_is_bounded(size):
    with pytest.raises(ValueError, match="admission_batch_size"):
        scheduler_settings(admission_batch_size=size)


def test_the_batch_size_reads_its_environment_variable(monkeypatch):
    from scheduler.settings import SchedulerSettings

    monkeypatch.setenv("PROJECT_ID", "proj")
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.delenv("ADMISSION_BATCH_SIZE", raising=False)
    assert SchedulerSettings.from_env().admission_batch_size == 10
    monkeypatch.setenv("ADMISSION_BATCH_SIZE", "7")
    assert SchedulerSettings.from_env().admission_batch_size == 7
