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
       no progress for longer than the threshold is fenced (invariant 5), with
       an event that names the reason -- and NOT killed in that pass, because
       SIGTERM sends a live worker down a path that parks the task without
       checking the fence. The worker stops itself; the next pass releases the
       lease and requeues the task, killing the Job first if it outlived its
       fence.
  S-2  HEARTBEATING IS NOT PROGRESS, BUT PROGRESS IS NOT ONLY THE WORK TREE: an
       attempt spending CPU, or changing its work tree, is left alone, and so
       is one whose quiet cannot be PROVEN because nothing measured it.
  L-1  LEFT RUNNING: a Job still active after its task finished is terminated,
       with an event on the task naming the reason; the task is not re-fenced.
  L-2  its lease, if it still holds one, is released only after the kill is
       confirmed. The orphan-lease rule used to release it regardless.
  L-3  ... and that holds INSIDE the grace too, when no kill has been tried at
       all; when the Job's task could not be read; when the task was
       re-admitted mid-pass; with `RECONCILER_ENABLE_GKE_EVICTION=false`; and
       when a by-name probe found the Job and its kill was refused. No rule
       that only releases may return a slot while the execution holding it is
       still running.
  G-1  a Job of a NEWER generation is never touched, even when the task moves
       on to it in the middle of the pass that finds the old one stuck.

"Progress" is what `reconciler/progress.py` says it is, and why: CPU across the
heartbeat events, and the work tree across checkpoints. The event shapes below
are the ones `agent_worker.control.ControlPlane.emit` writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

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


def _silent_since(db: FakeFirestore, ids: dict[str, str], seconds: int) -> None:
    """The worker's last heartbeat was `seconds` ago: it has stopped beating."""
    last = utcnow() - timedelta(seconds=seconds)
    lease = db.docs[f"leases/{ids['lease']}"]
    lease["heartbeat_at"] = last
    lease["expires_at"] = last + timedelta(seconds=120)


def _fenced_and_nothing_else(db: FakeFirestore, ids: dict[str, str], batch: RbacBatchApi,
                             report: Any) -> None:
    """What the pass that finds a stuck attempt must leave behind."""
    assert [o.kind for o in report.outcomes] == ["stuck_no_progress"], (
        f"{[o.as_dict() for o in report.outcomes]}"
    )
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["current_generation"] == 2, "the stuck generation must be fenced"
    # NOT deleted in the same pass. Deleting the Job SIGTERMs a worker that is
    # alive, and its SIGTERM path parks the task SCHEDULED_RETRY without
    # checking the fence -- a park nothing ever promotes. The fence alone makes
    # the worker stop the agent and exit at its next poll, touching nothing.
    assert batch.deleted == [], "a live worker was SIGTERMed in the pass that fenced it"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)
    fenced = reconciler_events(db, ids["task"], "generation_fenced")
    assert fenced and fenced[-1]["detail"]["finding"] == "stuck_no_progress"
    assert "no progress" in fenced[-1]["detail"]["reason"]


def test_a_browser_job_stuck_without_progress_is_fenced_then_released_and_requeued():
    """49 minutes of heartbeats, and nothing else: CPU flat, work tree unchanged.

    The worker is alive -- its lease heartbeated ten seconds ago, which is
    exactly why no existing rule touches it -- and the agent under it has done
    nothing for three quarters of an hour. 0.002 cores is the worker's own
    polling; a page load costs whole CPU-seconds.

    Pass 1 fences. The worker sees the fence at its next poll, stops the agent
    and exits 70 (`lifecycle._apply_control_signals`), so its Job fails and its
    lease goes quiet. Pass 2 releases that lease through the frozen release
    and requeues the task: one attempt of three spent, so READY.
    """
    db = FakeFirestore()
    ids = running_browser_task(db, "task_stuck0000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002)
    job = k8s_job(task_id=ids["task"])
    batch = RbacBatchApi(jobs=[job], listable={ENG_NS})
    rec, _ = reconciler(db, gke(batch))

    first = rec.run_once()

    _fenced_and_nothing_else(db, ids, batch, first)

    # The worker's fenced exit: the Job fails on its own, the heartbeats stop.
    job.status.conditions = [SimpleNamespace(type="Failed", status="True")]
    _silent_since(db, ids, 300)

    second = rec.run_once()

    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "READY", (
        f"one attempt of three spent: retryable. {[o.as_dict() for o in second.outcomes]}"
    )
    assert task["current_lease_id"] is None
    assert task["current_generation"] == 2, "fenced once, not twice"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)
    assert batch.deleted == [], "the Job ended itself; there was nothing to kill"


def test_a_stuck_job_that_outlives_its_fence_is_terminated_before_its_lease_is_released():
    """The worker did NOT stop at its fence -- its loop is wedged too, so it has
    also stopped heartbeating -- and its Job is still active on the next pass.
    The Job is killed first, and the lease comes back only after the kill."""
    db = FakeFirestore()
    ids = running_browser_task(db, "task_wedge0000000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002)
    at_kill: dict[str, Any] = {}

    def on_delete(namespace: str, name: str) -> None:
        at_kill["generation"] = db.docs[f"tasks/{ids['task']}"]["current_generation"]
        at_kill["released_at"] = db.docs[f"leases/{ids['lease']}"]["released_at"]

    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS}, on_delete=on_delete
    )
    rec, _ = reconciler(db, gke(batch))

    first = rec.run_once()
    _fenced_and_nothing_else(db, ids, batch, first)
    _silent_since(db, ids, 300)

    second = rec.run_once()

    assert batch.deleted == [job_name(ids)], f"{[o.as_dict() for o in second.outcomes]}"
    assert at_kill == {"generation": 2, "released_at": None}, (
        f"fenced before the kill, released only after it: {at_kill}"
    )
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "READY"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)


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
    db: FakeFirestore,
    task_id: str,
    *,
    state: str,
    lease_released: bool,
    ended_seconds_ago: int = 1200,
    heartbeat_seconds_ago: int = 1200,
) -> dict[str, str]:
    """A browser task that ended 20 minutes ago, as `ControlPlane.finish` leaves it.

    `ended_seconds_ago` moves the end inside `LEFT_RUNNING_GRACE_SECONDS`, and
    `heartbeat_seconds_ago` says when the worker last heartbeated its lease.
    """
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(db, task_id, state="RUNNING", minutes_ago=60,
                        heartbeat_seconds_ago=heartbeat_seconds_ago)
    ended = utcnow() - timedelta(seconds=ended_seconds_ago)
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
# L-3: no rule that only releases returns a slot while its Job still runs
# ---------------------------------------------------------------------------


def delete_attempts(batch: RbacBatchApi) -> list[tuple[str, ...]]:
    """Every delete SENT, refused or not. `batch.deleted` records only successes."""
    return [c for c in batch.calls if c[0] == "delete_namespaced_job"]


def _outcomes(report: Any) -> list[dict[str, Any]]:
    return [o.as_dict() for o in report.outcomes]


def _capture_release_at_kill(db: FakeFirestore, ids: dict[str, str], into: dict[str, Any]):
    def on_delete(namespace: str, name: str) -> None:
        into["released_at"] = db.docs[f"leases/{ids['lease']}"]["released_at"]

    return on_delete


def task_read_fails() -> tuple[type, list[str]]:
    """A ControlStore whose by-id task read fails, as a Firestore read does
    under a deadline or a 503 -- and the ids it was asked for."""
    reads: list[str] = []

    class TaskReadFails(ControlStore):
        def task_by_id(self, task_id: str) -> Any:
            reads.append(task_id)
            raise RuntimeError("504 Deadline Exceeded")

    return TaskReadFails, reads


@pytest.mark.parametrize("evicting", [True, False], ids=["eviction-on", "eviction-off"])
@pytest.mark.parametrize(
    ("ended_seconds_ago", "heartbeat_seconds_ago"),
    [(60, 70), (200, 210)],
    ids=["lease-fresh", "lease-silent"],
)
def test_inside_the_grace_a_finished_tasks_lease_is_held_while_its_job_runs(
    evicting: bool, ended_seconds_ago: int, heartbeat_seconds_ago: int
):
    """The worker wrote SUCCEEDED, then hung inside `release_lease`.

    `ControlPlane.finish` is a transition and THEN a separate release, so a
    worker wedged between them leaves a terminal task, an unreleased lease and
    a pod still up. The reconciler runs every five minutes, so its first pass
    after that almost always lands inside the 300s left-running grace.

    There, the orphan-lease rule ("task is SUCCEEDED but the lease was never
    released") returned 2 units to each of the seven pools with the 8 vCPU pod
    still running and no kill tried. Once the lease had gone silent, a
    dead_worker kill that failed did not stop that release either.

    The kill here answers 403, as it would under a Role that lost `delete`, so
    whichever rule owns the Job, its lease must still be held when the pass
    ends. With eviction on, the left-running rule owns the Job through its
    grace, and nothing is sent to it yet. With eviction off, the orphan rule
    tries the kill at once, as it did before the eviction rules existed -- and
    its refusal must hold the lease just the same.
    """
    db = FakeFirestore()
    ids = finished_browser_task(
        db, "task_hung0000000000001", state="SUCCEEDED", lease_released=False,
        ended_seconds_ago=ended_seconds_ago, heartbeat_seconds_ago=heartbeat_seconds_ago,
    )
    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS}, deletable=set()
    )
    rec, _ = reconciler(db, gke(batch), enable_gke_eviction=evicting)

    report = rec.run_once()

    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None, (
        f"capacity came back while the pod holding it still ran: {_outcomes(report)}"
    )
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)
    assert db.docs[f"tasks/{ids['task']}"]["state"] == "SUCCEEDED"
    if evicting:
        assert delete_attempts(batch) == [], (
            f"the left-running rule owns this Job through its grace: {_outcomes(report)}"
        )
    else:
        assert delete_attempts(batch), (
            f"with eviction off, the orphan rule must still try the kill: {_outcomes(report)}"
        )


def test_a_lease_held_through_the_grace_comes_back_once_its_job_is_killed():
    """The other half, so that the hold above cannot pass as "never release".

    Pass 1 lands inside the grace: nothing killed, nothing released. Pass 2
    lands after it: the left-running rule deletes the Job, and only then
    returns the lease's units to every pool.
    """
    db = FakeFirestore()
    ids = finished_browser_task(
        db, "task_hung0000000000002", state="SUCCEEDED", lease_released=False,
        ended_seconds_ago=60, heartbeat_seconds_ago=70,
    )
    at_kill: dict[str, Any] = {}
    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS},
        on_delete=_capture_release_at_kill(db, ids, at_kill),
    )
    rec, _ = reconciler(db, gke(batch))

    first = rec.run_once()

    assert delete_attempts(batch) == [], _outcomes(first)
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None, _outcomes(first)
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)

    # Ten minutes on: the worker is still wedged, and the grace has passed.
    later = utcnow() - timedelta(minutes=10)
    db.docs[f"tasks/{ids['task']}"].update({"completed_at": later, "updated_at": later})
    _silent_since(db, ids, 610)

    second = rec.run_once()

    assert batch.deleted == [job_name(ids)], _outcomes(second)
    assert at_kill == {"released_at": None}, f"released before the kill: {at_kill}"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None, _outcomes(second)
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)
    assert db.docs[f"tasks/{ids['task']}"]["state"] == "SUCCEEDED"


def test_a_job_whose_task_could_not_be_read_keeps_its_lease():
    """The by-id read of the Job's task fails. The pass promises to conclude
    nothing about that execution -- so nothing about its lease either.

    The orphan-lease rule used to release that lease in the same pass, as
    "lease references a task that no longer exists": a read that FAILED,
    reported as an absence. And with the lease silent, a dead_worker over the
    same Job killed and released it, though the pass had said it would not
    judge that Job.
    """
    db = FakeFirestore()
    ids = finished_browser_task(db, "task_unread000000000001", state="FAILED",
                                lease_released=False)
    batch = RbacBatchApi(jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS})
    store_class, reads = task_read_fails()
    rec, _ = reconciler(db, gke(batch), store_class=store_class)

    report = rec.run_once()

    assert reads == [ids["task"]], "the path under test did not run: no task read failed"
    assert delete_attempts(batch) == [], _outcomes(report)
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None, _outcomes(report)
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)
    assert not [o for o in report.outcomes if "no longer exists" in o.reason], (
        f"a failed read was reported as an absence: {_outcomes(report)}"
    )
    assert any(ids["task"] in error for error in report.errors), report.errors


def test_with_eviction_off_no_task_is_read_by_id_and_the_kill_precedes_the_release():
    """`RECONCILER_ENABLE_GKE_EVICTION=false` is the way back to the reconciler
    as it was before the eviction rules, so it turns off their input as well:
    the by-id read of tasks outside the concurrency states, and the three
    deferrals that read made possible.

    With it off, the read that would have failed is never made, and the Job is
    judged as it was before -- an orphan -- killed first, and only then its
    lease released. With the kill refused, the lease is held: that is the
    eviction-off case of the grace test above.
    """
    db = FakeFirestore()
    ids = finished_browser_task(db, "task_unread000000000002", state="FAILED",
                                lease_released=False, heartbeat_seconds_ago=60)
    at_kill: dict[str, Any] = {}
    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS},
        on_delete=_capture_release_at_kill(db, ids, at_kill),
    )
    store_class, reads = task_read_fails()
    rec, _ = reconciler(db, gke(batch), store_class=store_class, enable_gke_eviction=False)

    report = rec.run_once()

    assert reads == [], "eviction is off, and a task was still read by id"
    assert batch.deleted == [job_name(ids)], _outcomes(report)
    assert at_kill == {"released_at": None}, f"released before the kill: {at_kill}"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None, _outcomes(report)
    assert set(pool_actives(db).values()) == {0}, pool_actives(db)
    assert db.docs[f"tasks/{ids['task']}"]["state"] == "FAILED"


def test_the_old_job_of_a_task_readmitted_mid_pass_keeps_its_lease_until_it_is_killed():
    """FAILED, then retried: between this pass's snapshot and its by-id read,
    the scheduler re-admitted the task as generation 2 on a new lease. The
    generation-1 Job is still up, and its lease is unreleased.

    The orphan rule leaves that Job for the next pass, which holds the task,
    its lease and its generation together. The orphan-lease rule used to
    release the old lease in THIS pass ("task points at a different lease")
    with the old Job still running. On the next pass the old Job is an
    obsolete generation: killed, and only then its lease released. Generation
    2's lease is never touched.
    """
    db = FakeFirestore()
    ids = finished_browser_task(db, "task_retry000000000001", state="FAILED",
                                lease_released=False, heartbeat_seconds_ago=60)
    newer = {"lease": "lease_retry000000000001_g2", "attempt": "att_retry000000000001_g2"}
    at_kill: dict[str, Any] = {}
    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"], generation=1)], listable={ENG_NS},
        on_delete=_capture_release_at_kill(db, ids, at_kill),
    )
    readmitted: list[bool] = []

    def readmit() -> None:
        if readmitted:
            return  # the race happens once; the next pass sees its result
        readmitted.append(True)
        now = utcnow()
        db.docs[f"tasks/{ids['task']}"].update({
            "state": "LEASED", "current_generation": 2, "current_lease_id": newer["lease"],
            "attempt_count": 2, "completed_at": None, "updated_at": now,
        })
        db.docs[f"leases/{newer['lease']}"] = {
            **db.docs[f"leases/{ids['lease']}"],
            "lease_id": newer["lease"], "attempt_id": newer["attempt"], "generation": 2,
            "state": "LEASED", "created_at": now, "heartbeat_at": None,
            "dispatch_deadline": now + timedelta(seconds=300),
            "expires_at": now + timedelta(seconds=120), "released_at": None,
        }
        for path, doc in db.docs.items():
            if path.startswith("pools/"):
                doc["active"] += 2

    class ReadmittedBeforeTheRead(ControlStore):
        def task_by_id(self, task_id: str) -> Any:
            readmit()
            return super().task_by_id(task_id)

    rec, _ = reconciler(db, gke(batch), store_class=ReadmittedBeforeTheRead)

    first = rec.run_once()

    assert readmitted, "the path under test did not run: the task was never read by id"
    assert delete_attempts(batch) == [], _outcomes(first)
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None, (
        f"the old lease came back with its Job still running: {_outcomes(first)}"
    )
    # Generation 1's units and generation 2's, both held.
    assert set(pool_actives(db).values()) == {4}, pool_actives(db)

    second = rec.run_once()

    assert batch.deleted == [job_name(ids)], _outcomes(second)
    assert at_kill == {"released_at": None}, f"released before the kill: {at_kill}"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None, _outcomes(second)
    assert db.docs[f"leases/{newer['lease']}"]["released_at"] is None, _outcomes(second)
    task = db.docs[f"tasks/{ids['task']}"]
    assert (task["state"], task["current_generation"], task["current_lease_id"]) == (
        "LEASED", 2, newer["lease"]
    ), _outcomes(second)
    assert set(pool_actives(db).values()) == {2}, pool_actives(db)


def test_a_job_found_by_name_whose_kill_is_refused_keeps_its_lease():
    """The namespace list fails (a 5xx), so the Job is read by name instead,
    under a lease silent for twenty minutes: a dead_worker, and its kill
    answers 403.

    The task is FAILED, so the orphan-lease rule -- which never looks for
    compute -- names the same lease, and it used to release it straight after
    the refused kill, in the same pass. A slot whose execution the pass failed
    to stop is returned by no rule in that pass.
    """
    db = FakeFirestore()
    ids = finished_browser_task(db, "task_refused00000000001", state="FAILED",
                                lease_released=False)
    batch = RbacBatchApi(
        jobs=[k8s_job(task_id=ids["task"])], listable={ENG_NS},
        list_fails_with=500, deletable=set(),
    )
    rec, _ = reconciler(db, gke(batch))

    report = rec.run_once()

    job = ids["execution"].split("/", 1)[1]
    assert ("read_namespaced_job", ENG_NS, job) in batch.calls, "the probe was not tried"
    assert delete_attempts(batch), f"the kill was never tried: {_outcomes(report)}"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None, _outcomes(report)
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

    This pass must not lay a finger on generation 2 -- not its Job, not its
    lease, not the task's generation, not the task's pointer to its lease --
    and the next pass still gets rid of generation 1's Job, which is still
    running under a generation nobody holds any more.
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

    readmitted: list[bool] = []

    def readmit() -> None:
        if readmitted:
            return  # the race happens once; a later pass sees its result
        readmitted.append(True)
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

    def generation_two_untouched(report: Any) -> None:
        outcomes = [o.as_dict() for o in report.outcomes]
        assert new_job in batch.jobs, f"generation 2's Job was deleted: {outcomes}"
        task = db.docs[f"tasks/{ids['task']}"]
        assert task["current_generation"] == 2, f"generation 2 was fenced: {outcomes}"
        assert task["state"] == "RUNNING", outcomes
        assert task["current_lease_id"] == newer["lease"], (
            f"generation 2's lease was unhooked: {outcomes}"
        )
        assert db.docs[f"leases/{newer['lease']}"]["released_at"] is None, outcomes
        # Generation 1's units came back once (by the other instance),
        # generation 2's are held: net, one browser lease's worth everywhere.
        assert set(pool_actives(db).values()) == {2}, pool_actives(db)

    first = rec.run_once()

    assert [o.kind for o in first.outcomes] == ["stuck_no_progress"], (
        "the stuck finding must have been made -- or this test proves nothing"
    )
    generation_two_untouched(first)

    second = rec.run_once()

    generation_two_untouched(second)
    assert batch.deleted == [job_name(ids)], (
        f"generation 1's Job outlived its generation and must go: "
        f"{[o.as_dict() for o in second.outcomes]}"
    )


def test_the_stuck_threshold_that_ships_is_the_one_these_tests_exercise():
    """The tests above use the default rather than overriding it, so that the
    value in the image is the value under test. If the default moves past the
    quiet span they seed, they must move with it rather than pass vacuously."""
    assert ReconcilerConfig.stuck_after_seconds < QUIET.total_seconds()
    assert ReconcilerConfig.stuck_after_seconds > 10 * HEARTBEAT_EVENT_EVERY.total_seconds()
