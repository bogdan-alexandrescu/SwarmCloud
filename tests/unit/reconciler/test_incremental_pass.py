"""Between full sweeps a pass reads what changed and what falls due (BGD, 2026-11-06).

WHAT WAS WRONG. Every pass read every live task, every unreleased lease and
everything hanging off them. On 2026-11-06 the load analysis measured a pass
at a 26.4 s median, 30.4 s p90 and 115.6 s worst on a 60 s tick over six
hours, growing with the documents: 348 passes in 360 ticks, 5 of them longer
than the tick.

WHAT EACH TEST PINS (`Reconciler._snapshot`,
`ControlStore.incremental_snapshot`):

  * a task that CHANGED since the last pass is read and repaired on the next
    tick, which is an incremental one;
  * a task nobody touched whose lease EXPIRES is read through the lease's
    deadline and repaired on the next tick;
  * a change the cursor cannot see (a write that did not stamp
    `updated_at`) is left alone by the incremental passes and repaired by
    the next full sweep, `full_sweep_every` passes after the last;
  * an incremental pass over N unchanged running tasks reads the due lease
    and its own task, attempt and execution, and none of the N.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore;
only the backend is scripted.
"""

from __future__ import annotations

import io
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from typing import Any

import fakes
import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_worker_evicted import (
    ATTEMPT,
    GENERATION,
    GKE,
    JOB_NAME,
    LEASE,
    NAMESPACE,
    POOLS,
    TASK,
    seed_a_running_attempt,
)
from worker_seeds import TENANT

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import TaskState


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
        enable_checkpoint_gc=False,
        full_sweep_every=10,
    )


def job(task_id: str = TASK, attempt_id: str = ATTEMPT, name: str = JOB_NAME) -> ExecutionView:
    return ExecutionView(
        name=name,
        backend=GKE,
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(minutes=9),
        task_id=task_id,
        attempt_id=attempt_id,
        tenant_id=TENANT,
        generation=GENERATION,
        namespace=NAMESPACE,
    )


def healthy(db: FakeFirestore) -> None:
    """The seeded attempt with a worker that beat ten seconds ago."""
    seed_a_running_attempt(db, heartbeat_seconds_ago=10)
    now = utcnow()
    db.doc(f"leases/{LEASE}").update(
        {"heartbeat_at": now - timedelta(seconds=10), "expires_at": now + timedelta(seconds=110)}
    )


def goes_silent(db: FakeFirestore) -> None:
    """The worker's last beat is 110 s old, and its lease expires in 10 s.

    The lease's own clock moving, as time does; neither document is written
    by anyone, so the task's `updated_at` stays where admission left it."""
    now = utcnow()
    db.doc(f"leases/{LEASE}").update(
        {"heartbeat_at": now - timedelta(seconds=110), "expires_at": now + timedelta(seconds=10)}
    )


def fenced_behind_its_job(db: FakeFirestore, *, stamped: bool) -> None:
    """A fence bumped the task's generation; its old Job is still running.

    `stamped`: the writer stamped `updated_at`, as every writer here should.
    Unstamped is a change the incremental read cannot see."""
    change: dict[str, Any] = {"current_generation": GENERATION + 1}
    if stamped:
        change["updated_at"] = utcnow()
    db.doc(f"tasks/{TASK}").update(change)


class World:
    def __init__(
        self, db: FakeFirestore, config: ReconcilerConfig, executions: list[ExecutionView]
    ) -> None:
        self.db = db
        logger = build_logger(stream=io.StringIO())
        self.backend = FakeBackend(name=GKE, executions=executions, journal=db.writes)
        self.reconciler = Reconciler(
            store=ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db)),
            backends=[self.backend],
            config=config,
            logger=logger,
            hold_releaser=None,
            finish_announcer=None,
        )

    def run(self) -> Any:
        return self.reconciler.run_once()

    def reclaimed(self) -> bool:
        return self.db.doc(f"leases/{LEASE}").get("released_at") is not None


def sweep(report: Any) -> str | None:
    return report.as_dict().get("sweep")


def test_a_changed_task_is_repaired_on_the_next_tick(db, config):
    healthy(db)
    world = World(db, config, [job()])
    first = world.run()
    assert sweep(first) == "full"
    assert not world.reclaimed() and world.backend.terminated == []

    fenced_behind_its_job(db, stamped=True)
    second = world.run()

    # MUTATION: a full read on every pass makes this "full".
    assert sweep(second) == "incremental"
    # MUTATION: dropping the changed-tasks read leaves the old Job running
    # and the stale generation's lease held until the next full sweep.
    assert list(world.backend.terminated) == [JOB_NAME]
    assert world.reclaimed()
    assert not second.errors, second.errors


def test_an_untouched_task_whose_lease_expires_is_repaired_by_the_deadline_read(db, config):
    healthy(db)
    world = World(db, config, [job()])
    world.run()
    assert not world.reclaimed()

    goes_silent(db)
    second = world.run()

    assert sweep(second) == "incremental"
    # MUTATION: dropping the `expires_at` read never sees this lease between
    # full sweeps: the dead worker keeps its slot for up to ten minutes.
    assert world.reclaimed()
    task = db.doc(f"tasks/{TASK}")
    assert task["current_generation"] == GENERATION + 1, task
    assert task["state"] == TaskState.READY.value, task
    assert not second.errors, second.errors


def test_the_full_sweep_finds_a_change_the_cursor_missed(db, config):
    config = replace(config, full_sweep_every=3)
    healthy(db)
    world = World(db, config, [job()])
    assert sweep(world.run()) == "full"

    fenced_behind_its_job(db, stamped=False)
    second, third = world.run(), world.run()
    assert [sweep(second), sweep(third)] == ["incremental", "incremental"]
    # MUTATION: an incremental pass that judged executions it did not read
    # the task of would act here, on no evidence it read.
    assert world.backend.terminated == [] and not world.reclaimed()

    fourth = world.run()
    # MUTATION: never running the full sweep again leaves it for ever.
    assert sweep(fourth) == "full"
    assert list(world.backend.terminated) == [JOB_NAME]
    assert world.reclaimed()


N = 40


def seed_unchanged(db: FakeFirestore) -> list[ExecutionView]:
    """N running tasks, admitted nine minutes ago, whose workers beat just now."""
    now = utcnow()
    admitted = now - timedelta(minutes=9)
    executions = []
    for i in range(N):
        task, lease, attempt = f"task_quiet{i:03d}", f"lease_quiet{i:03d}", f"att_quiet{i:03d}"
        name = f"swarm-quiet{i:03d}-1"
        db.seed(
            f"tasks/{task}",
            {
                "id": task, "tenant_id": TENANT, "state": TaskState.RUNNING.value,
                "runner_profile": "claude-code", "resource_class": "standard",
                "current_generation": GENERATION, "current_lease_id": lease,
                "attempt_count": 1, "max_attempts": 3,
                "updated_at": admitted, "cancel_requested": False,
                "last_error": None, "metadata": {},
            },
        )
        db.seed(
            f"leases/{lease}",
            {
                "lease_id": lease, "task_id": task, "attempt_id": attempt,
                "tenant_id": TENANT, "generation": GENERATION,
                "pools": POOLS, "units": 1, "state": TaskState.RUNNING.value,
                "created_at": admitted,
                "dispatch_deadline": admitted + timedelta(seconds=480),
                "expires_at": now + timedelta(seconds=110),
                "heartbeat_at": now - timedelta(seconds=10),
                "released_at": None,
            },
        )
        db.seed(
            f"attempts/{attempt}",
            {
                "attempt_id": attempt, "task_id": task, "tenant_id": TENANT,
                "generation": GENERATION, "lease_id": lease, "backend": GKE,
                "created_at": admitted, "execution_name": f"{NAMESPACE}/{name}",
            },
        )
        executions.append(job(task, attempt, name))
    return executions


@pytest.fixture
def reads(monkeypatch) -> Counter:
    """Collection -> documents read, by query or by id, since the last clear."""
    counted: Counter = Counter()
    stream, get = fakes.FakeQuery.stream, fakes.FakeDocumentRef.get

    def counting_stream(self: Any, **kwargs: Any) -> Any:
        for snap in stream(self, **kwargs):
            counted[self._prefix] += 1
            yield snap

    def counting_get(self: Any, *args: Any, **kwargs: Any) -> Any:
        snap = get(self, *args, **kwargs)
        if snap.exists:
            counted[self.path.rsplit("/", 1)[0]] += 1
        return snap

    monkeypatch.setattr(fakes.FakeQuery, "stream", counting_stream)
    monkeypatch.setattr(fakes.FakeDocumentRef, "get", counting_get)
    return counted


def test_an_incremental_pass_over_unchanged_tasks_reads_only_the_due_set(db, config, reads):
    healthy(db)
    executions = [job(), *seed_unchanged(db)]
    world = World(db, config, executions)
    first = world.run()
    assert sweep(first) == "full"
    assert first.tasks_examined == N + 1 and not world.reclaimed()
    assert reads["tasks"] >= N + 1 and reads["leases"] >= N + 1

    goes_silent(db)
    reads.clear()
    second = world.run()

    assert sweep(second) == "incremental"
    assert world.reclaimed()
    # MUTATION: reading every live task again makes these N + 1.
    assert second.tasks_examined == 1 and second.leases_examined == 1
    assert second.executions_examined == 1
    # The due lease, its task, its attempt -- and nothing of the N. Reads made
    # by the repair's own transactions re-read the same documents only.
    assert reads["tasks"] <= 4, reads
    assert reads["leases"] <= 4, reads
    assert reads["attempts"] <= 4, reads
    assert not second.errors, second.errors
