"""Browser pods that are stuck, or left running, are evicted by the reconciler.

OWNER REQUIREMENT, 2026-09-24, attached to keeping
`cluster-autoscaler.kubernetes.io/safe-to-evict=false` on browser pods (PR #31):
"we should have a way to evict browser pods that are stuck for some time without
progress or that were left open and running after the agent work was finished."
A pod the autoscaler may not evict holds its node for as long as it runs, so a
wedged one has to be found by something else.

WHAT THIS FILE PINS, against the real `Reconciler`, the real `ControlStore`, the
real `GkeBackend` over a Kubernetes API that grants only the namespaced
`swarm-reaper` Role, and the real `firestore.transactional` -- so a released
lease is the frozen `release_lease_in_transaction` returning units to every
pool (invariant 2):

  S-1  STUCK: a RUNNING browser attempt whose lease heartbeats but which shows
       no progress for longer than the threshold is fenced (invariant 5), its
       Job deleted, its lease released -- in that order -- and the task goes
       back to READY with events that name the reason.
  S-2  HEARTBEATING IS NOT PROGRESS, BUT PROGRESS IS NOT ONLY THE WORK TREE: an
       attempt spending CPU, or changing its work tree, is left alone, and so
       is one whose quiet cannot be PROVEN because nothing measured it.
  L-1  LEFT RUNNING: a Job still active after its task finished is terminated,
       with an event on the task naming the reason; the task is not re-fenced.
  L-2  its lease, if it still holds one, is released only after the kill is
       confirmed. The orphan-lease rule used to release it regardless.
  G-1  a Job of a NEWER generation is never touched, even when the task moves
       on to it in the middle of the pass that evicts the old one.

"Progress" is what `reconciler/progress.py` says it is, and why: CPU across the
heartbeat events, and the work tree across checkpoints. The event shapes below
are the ones `agent_worker.control.ControlPlane.emit` writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from swarm_common.models import utcnow

from reconciler.config import ReconcilerConfig
from reconciler.store import ControlStore

from .fakes import FakeFirestore
from .test_reconciler_gke_namespaced import (
    ENG,
    ENG_NS,
    RbacBatchApi,
    gke,
    k8s_job,
    pool_actives,
    reconciler,
    seed_stranded,
    seed_tenant,
)

#: How long the stuck attempts below have been quiet. Past the 30-minute
#: default `stuck_after_seconds`, which these tests deliberately do not override
#: -- so they exercise the value that ships.
QUIET = timedelta(minutes=49)

#: The worker's cadences (`agent_worker.lifecycle`): a heartbeat event on every
#: fifth 30s heartbeat, a periodic checkpoint every 120s.
HEARTBEAT_EVENT_EVERY = timedelta(seconds=150)
CHECKPOINT_EVERY = timedelta(seconds=120)


# ---------------------------------------------------------------------------
# What a running worker writes
# ---------------------------------------------------------------------------


def emit(
    db: FakeFirestore,
    ids: dict[str, str],
    event_type: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
    *,
    generation: int = 1,
) -> None:
    """One event, shaped exactly as `ControlPlane.emit` writes it."""
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    db.docs[f"tasks/{ids['task']}/events/{event_id}"] = {
        "event_id": event_id,
        "task_id": ids["task"],
        "tenant_id": ENG,
        "type": event_type,
        "at": at,
        "attempt_id": ids["attempt"],
        "lease_id": ids["lease"],
        "generation": generation,
        "detail": detail or {},
    }


def seed_run(
    db: FakeFirestore,
    ids: dict[str, str],
    *,
    began: datetime,
    until: datetime,
    cpu_cores: float,
    cpu_measured: bool = True,
    size_bytes: int = 4096,
    work_tree_changes_at: datetime | None = None,
    generation: int = 1,
) -> None:
    """The event stream of one attempt from `began` to `until`.

    `cpu_cores` is the mean CPU the container spends; the heartbeat events carry
    it as a cumulative total, as `Worker._heartbeat` does. The checkpoint
    archive keeps one size until `work_tree_changes_at`, when the agent writes
    to `work/`.
    """
    emit(db, ids, "starting", began - timedelta(seconds=2), generation=generation)
    emit(db, ids, "running", began, generation=generation)
    at, cpu, beats = began + timedelta(seconds=5), 0.0, 0
    while at <= until:
        beats += 1
        emit(db, ids, "heartbeat", at, {
            "elapsed_seconds": round((at - began).total_seconds(), 1),
            "peak_rss_bytes": 700_000_000,
            "checkpoints": int((at - began) / CHECKPOINT_EVERY),
            "cpu_seconds": round(cpu, 3) if cpu_measured else None,
            "cpu_source": "cgroup" if cpu_measured else None,
        }, generation=generation)
        at += HEARTBEAT_EVENT_EVERY
        cpu += cpu_cores * HEARTBEAT_EVENT_EVERY.total_seconds()
    at, seq = began + CHECKPOINT_EVERY, 0
    while at <= until:
        seq += 1
        size = size_bytes
        if work_tree_changes_at is not None and at >= work_tree_changes_at:
            size = size_bytes * 3
        emit(db, ids, "checkpoint_started", at - timedelta(seconds=1),
             {"label": "periodic"}, generation=generation)
        emit(db, ids, "checkpoint_completed", at, {
            "checkpoint_id": f"ckpt-{seq:05d}",
            "uri": f"gs://bucket/tenants/{ENG}/tasks/{ids['task']}/attempts/"
                   f"{ids['attempt']}/checkpoints/ckpt-{seq:05d}/",
            "size_bytes": size,
            "seq": seq,
        }, generation=generation)
        at += CHECKPOINT_EVERY


def running_browser_task(db: FakeFirestore, task_id: str) -> dict[str, str]:
    """A RUNNING browser task, 50 minutes in, whose worker heartbeated 10s ago."""
    seed_tenant(db, ENG, record_namespace=True)
    return seed_stranded(
        db, task_id, state="RUNNING", minutes_ago=50, heartbeat_seconds_ago=10
    )


def events(db: FakeFirestore, task_id: str, event_type: str) -> list[dict[str, Any]]:
    return [
        e for e in db.collection_docs(f"tasks/{task_id}/events") if e["type"] == event_type
    ]


def reconciler_events(db: FakeFirestore, task_id: str, event_type: str) -> list[dict[str, Any]]:
    return [e for e in events(db, task_id, event_type) if e["detail"].get("source") == "reconciler"]


def job_name(ids: dict[str, str]) -> str:
    """`<namespace>/<job>`, as `RbacBatchApi.deleted` records a deletion."""
    return ids["execution"]


# ---------------------------------------------------------------------------
# S-1: stuck without progress
# ---------------------------------------------------------------------------


def test_a_browser_job_stuck_without_progress_is_fenced_terminated_and_released():
    """49 minutes of heartbeats, and nothing else: CPU flat, work tree unchanged.

    The worker is alive -- its lease heartbeated ten seconds ago, which is
    exactly why no existing rule touches it -- and the agent under it has done
    nothing for three quarters of an hour. 0.002 cores is the worker's own
    polling; a page load costs whole CPU-seconds.
    """
    db = FakeFirestore()
    ids = running_browser_task(db, "task_stuck0000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002)
    at_kill: dict[str, Any] = {}

    def on_delete(namespace: str, name: str) -> None:
        # The order is the invariant: fenced BEFORE the kill, released AFTER it.
        at_kill["generation"] = db.docs[f"tasks/{ids['task']}"]["current_generation"]
        at_kill["released_at"] = db.docs[f"leases/{ids['lease']}"]["released_at"]

    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS}, on_delete=on_delete
    )
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert batch.deleted == [job_name(ids)], (
        f"a heartbeating browser Job with no progress for {QUIET} must be deleted; "
        f"outcomes={[o.as_dict() for o in report.outcomes]}"
    )
    assert at_kill == {"generation": 2, "released_at": None}, (
        "the generation must be fenced before the Job is deleted, and the lease "
        f"released only after: {at_kill}"
    )
    assert [o.kind for o in report.outcomes] == ["stuck_no_progress"]
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["current_generation"] == 2
    assert task["state"] == "READY", "one attempt of three spent: retryable"
    assert task["current_lease_id"] is None
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)

    fenced = reconciler_events(db, ids["task"], "generation_fenced")
    assert fenced and fenced[-1]["detail"]["finding"] == "stuck_no_progress"
    assert "no progress" in fenced[-1]["detail"]["reason"]
    ready = reconciler_events(db, ids["task"], "ready")
    assert ready and ready[-1]["detail"]["reason"] == "stuck_no_progress"


# ---------------------------------------------------------------------------
# S-2: heartbeating-and-progressing, and unproven quiet, are left alone
# ---------------------------------------------------------------------------


def _left_alone(db: FakeFirestore, ids: dict[str, str], batch: RbacBatchApi, report: Any) -> None:
    assert batch.deleted == [], (
        f"a browser Job that is not proven stuck was deleted: "
        f"{[o.as_dict() for o in report.outcomes]}"
    )
    assert "stuck_no_progress" not in [o.kind for o in report.outcomes]
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["current_generation"] == 1, "the live attempt was fenced"
    assert task["state"] == "RUNNING"
    assert task["current_lease_id"] == ids["lease"]
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)


def test_a_heartbeating_browser_job_spending_cpu_is_progressing_and_left_alone():
    """The browser runner writes nothing to `work/` -- screenshots and extracts go
    to `artifacts/`, which is not checkpointed -- so its work tree never changes
    however hard it works. Judge browser work by the work tree alone and every
    healthy browser run longer than the threshold is evicted. CPU is the signal
    that moves: 0.4 cores is a browser loading and rendering pages."""
    db = FakeFirestore()
    ids = running_browser_task(db, "task_busy00000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.4)
    batch = RbacBatchApi(jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    _left_alone(db, ids, batch, report)


def test_a_heartbeating_job_whose_work_tree_changed_recently_is_left_alone():
    """CPU near idle, but the work tree changed ten minutes ago: progress."""
    db = FakeFirestore()
    ids = running_browser_task(db, "task_edit00000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002,
             work_tree_changes_at=now - timedelta(minutes=10))
    batch = RbacBatchApi(jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    _left_alone(db, ids, batch, report)


def test_quiet_that_nothing_measured_is_not_proof_of_being_stuck():
    """CPU unmeasured (`cpu_seconds: None`, as on a runner with no cgroup and no
    /proc): nothing shows progress, and nothing shows its absence either. The
    reconciler does not act on what it cannot see."""
    db = FakeFirestore()
    ids = running_browser_task(db, "task_blind0000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.0, cpu_measured=False)
    batch = RbacBatchApi(jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    _left_alone(db, ids, batch, report)


def test_evidence_that_stopped_arriving_is_not_proof_of_being_stuck():
    """The lease still heartbeats, but the attempt's events stopped 20 minutes
    ago. Silence in the event stream is a gap in the evidence, not a
    measurement of nothing happening."""
    db = FakeFirestore()
    ids = running_browser_task(db, "task_gap00000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now - timedelta(minutes=20), cpu_cores=0.002)
    batch = RbacBatchApi(jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    _left_alone(db, ids, batch, report)


# ---------------------------------------------------------------------------
# L-1, L-2: left running after the task finished
# ---------------------------------------------------------------------------


def finished_browser_task(
    db: FakeFirestore, task_id: str, *, state: str, lease_released: bool
) -> dict[str, str]:
    """A browser task that ended 20 minutes ago, as `ControlPlane.finish` leaves it."""
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, task_id, state="RUNNING", minutes_ago=60, heartbeat_seconds_ago=1200)
    ended = utcnow() - timedelta(minutes=20)
    db.docs[f"tasks/{ids['task']}"].update(
        {"state": state, "completed_at": ended, "updated_at": ended, "current_lease_id": None}
    )
    if lease_released:
        db.docs[f"leases/{ids['lease']}"]["released_at"] = ended
        for path, doc in db.docs.items():
            if path.startswith("pools/"):
                doc["active"] -= 2
    return ids


def test_a_browser_job_left_running_after_its_task_finished_is_terminated():
    """SUCCEEDED twenty minutes ago; the lease came back then; the pod never left.

    Terminated, and the task's own timeline says why. The task is NOT fenced
    and NOT re-stated: SUCCEEDED is the worker's record of how it ended.
    """
    db = FakeFirestore()
    ids = finished_browser_task(db, "task_done0000000000001", state="SUCCEEDED",
                                lease_released=True)
    batch = RbacBatchApi(jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))
    pools_before = pool_actives(db)

    report = rec.run_once()

    assert batch.deleted == [job_name(ids)], "a Job left running after SUCCEEDED must go"
    assert [o.kind for o in report.outcomes] == ["left_running"], (
        f"{[o.as_dict() for o in report.outcomes]}"
    )
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "SUCCEEDED"
    assert task["current_generation"] == 1, "a finished task has nothing to fence"
    assert pool_actives(db) == pools_before, "the lease was already released"

    named = [
        e for e in reconciler_events(db, ids["task"], "generation_fenced")
        if e["detail"].get("finding") == "left_running"
    ]
    assert named, "the task's timeline must say why its Job was terminated"
    assert "SUCCEEDED" in named[-1]["detail"]["reason"]
    assert named[-1]["detail"]["execution"] == k8s_job(task_id=ids["task"]).metadata.name


def test_a_left_running_job_that_cannot_be_killed_keeps_its_lease():
    """FAILED, but the worker died between writing it and releasing its lease --
    and the Job cannot be deleted (the delete answers 403).

    The lease is this Job's own and is released only once the Job is gone. It
    used to be released by the orphan-lease rule whether or not the kill
    worked, handing the pool capacity the still-running pod was using.
    """
    db = FakeFirestore()
    ids = finished_browser_task(db, "task_wedged000000000001", state="FAILED",
                                lease_released=False)
    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS}, deletable=set()
    )
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    assert batch.deleted == []
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None, (
        "capacity was released while the pod holding it was still running: "
        f"{[o.as_dict() for o in report.outcomes]}"
    )
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)


# ---------------------------------------------------------------------------
# G-1: a newer generation is never touched
# ---------------------------------------------------------------------------


def _as_attempt(job: Any, attempt_id: str) -> Any:
    """The same Job, carrying a different ATTEMPT_ID, as a re-admission would."""
    container = job.spec.template.spec.containers[0]
    container.env = [
        SimpleNamespace(name=e.name, value=attempt_id if e.name == "ATTEMPT_ID" else e.value)
        for e in container.env
    ]
    return job


def test_a_job_of_a_newer_generation_is_never_touched():
    """Generation 1 is stuck. While this pass reads its evidence, another
    reconciler instance fences and releases it, and the scheduler admits
    generation 2: a new lease, a new attempt, a new Job.

    This pass still evicts what it found -- generation 1's Job -- and must not
    lay a finger on generation 2: not its Job, not its lease, not the task's
    generation, not the task's pointer to its lease.
    """
    db = FakeFirestore()
    ids = running_browser_task(db, "task_race0000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002)
    old_job = k8s_job(task_id=ids["task"], generation=1)
    batch = RbacBatchApi(jobs=[old_job], listable={ENG_NS})
    newer = {
        "lease": "lease_race0000000000001_g2",
        "attempt": "att_race0000000000001_g2",
    }
    new_job = _as_attempt(k8s_job(task_id=ids["task"], generation=2), newer["attempt"])

    def readmit() -> None:
        task = db.docs[f"tasks/{ids['task']}"]
        old_lease = db.docs[f"leases/{ids['lease']}"]
        # Another instance fenced and released generation 1 ...
        old_lease["released_at"] = utcnow()
        # ... and admission took generation 2, in one transaction.
        task.update({
            "current_generation": 2, "current_lease_id": newer["lease"],
            "state": "RUNNING", "attempt_count": 2,
        })
        db.docs[f"leases/{newer['lease']}"] = {
            **old_lease,
            "lease_id": newer["lease"], "attempt_id": newer["attempt"], "generation": 2,
            "created_at": utcnow(), "heartbeat_at": utcnow(),
            "expires_at": utcnow() + timedelta(seconds=120), "released_at": None,
        }
        db.docs[f"attempts/{newer['attempt']}"] = {
            **db.docs[f"attempts/{ids['attempt']}"],
            "attempt_id": newer["attempt"], "generation": 2, "lease_id": newer["lease"],
            "created_at": utcnow(), "started_at": utcnow(),
            "execution_name": f"{ENG_NS}/{new_job.metadata.name}",
        }
        batch.jobs.append(new_job)

    class ReadmittedMidPass(ControlStore):
        def attempt_events(self, task_id: str, attempt_id: str) -> list[dict[str, Any]]:
            found = super().attempt_events(task_id, attempt_id)
            readmit()
            return found

    rec, _ = reconciler(db, gke(batch), store_class=ReadmittedMidPass)

    report = rec.run_once()

    assert batch.deleted == [job_name(ids)], (
        f"generation 1's Job is still the one found stuck: "
        f"{[o.as_dict() for o in report.outcomes]}"
    )
    assert new_job in batch.jobs, "generation 2's Job was deleted"
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["current_generation"] == 2, "generation 2 was fenced"
    assert task["state"] == "RUNNING"
    assert task["current_lease_id"] == newer["lease"], "generation 2's lease was unhooked"
    assert db.docs[f"leases/{newer['lease']}"]["released_at"] is None
    # Generation 1's units came back once (by the other instance), generation
    # 2's are held: net, one browser lease's worth on every pool.
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)


def test_the_stuck_threshold_that_ships_is_the_one_these_tests_exercise():
    """The tests above use the default rather than overriding it, so that the
    value in the image is the value under test. If the default moves past the
    quiet span they seed, they must move with it rather than pass vacuously."""
    assert ReconcilerConfig.stuck_after_seconds < QUIET.total_seconds()
    assert ReconcilerConfig.stuck_after_seconds > 10 * HEARTBEAT_EVENT_EVERY.total_seconds()
