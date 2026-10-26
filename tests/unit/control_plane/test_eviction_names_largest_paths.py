"""A disk-limit eviction names what filled the volume, from the last reading before it (#2436).

WHAT WAS WRONG. task_73850f24085447368991's pod was evicted for `/tmp` past
3 GiB, twice, and its last_error read only `evicted: EmptyDir volume "tmp"
exceeds 3Gi; evicted for disk 2 times: not retried again` -- nothing about
what had filled it, so the next run filled it the same way. kubelet evicts with
no grace period, so the worker cannot say it at the moment of eviction; it puts
its scratch volumes' largest paths on every HEARTBEAT event instead
(`agent_worker.scratchusage`), and the reconciler, repairing the eviction,
reports the newest reading.

WHAT EACH TEST PINS:

  * the evicted volume's largest paths, from the NEWEST reading of THIS
    attempt at THIS generation, reach the task's last_error and the attempt's
    error, formatted as the worker's `scratchusage.format_top` formats them;
  * a reading of another generation is never reported;
  * no reading, or an events read that fails, leaves the reason as it was and
    the repair goes ahead (the failure is logged);
  * a node-pressure eviction is not this step's size and gets no report;
  * the second disk eviction still FAILS the task, `not retried again`.

Against the real `Reconciler` and `ControlStore` over the control-plane fake
Firestore; only the backend is scripted, reporting what kubelet did.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_worker.scratchusage import format_top
from reconciler.detect import EVERY_VOLUME, evicted_volume
from reconciler.logs import build_logger
from reconciler.model import DISK_EVICTIONS_KEY, ExecutionPhase, ExecutionView, Termination
from reconciler.repair import Reconciler
from reconciler.scratchreport import scratch_report
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState

from .fakes import FakeFirestore
from .test_reconciler_gke_namespaced import ENG, GKE, config, plain_filter, seed_stranded, seed_tenant

GIB = 1024**3
TASK = "task_73850f24085447368991"
TMP_EVICTION = 'Usage of EmptyDir volume "tmp" exceeds the limit "3Gi". '
NODE_PRESSURE = (
    "The node was low on resource: ephemeral-storage. Threshold quantity: 10Gi, "
    "available: 9Gi. "
)


def _entry(top: list[tuple[str, float]], *, used: float, partial: bool = False) -> dict[str, Any]:
    """One volume's reading, exactly as `scratchusage.sample` shapes it."""
    return {
        "used_bytes": int(used * GIB),
        "limit_bytes": 3 * GIB,
        "top": [[path, int(gib * GIB)] for path, gib in top],
        "partial": partial,
    }


OLDER = _entry([("/tmp/old-build", 1.0)], used=1.1)
NEWEST = _entry(
    [
        ("/tmp/pgdata", 2.1),
        ("/tmp/ms-playwright", 0.7),
        ("/tmp/test-logs", 0.05),
    ],
    used=2.9,
)
OTHER_GENERATION = _entry([("/tmp/stale-generation", 2.5)], used=2.6)


class _Backend:
    """A GKE backend whose one Job has Failed: the pod kubelet evicted."""

    name = GKE

    def __init__(self, ids: dict[str, str], eviction: str) -> None:
        namespace, job = ids["execution"].split("/", 1)
        self._execution = ExecutionView(
            name=job,
            backend=GKE,
            phase=ExecutionPhase.FAILED,
            created_at=utcnow() - timedelta(minutes=9),
            task_id=ids["task"],
            attempt_id=ids["attempt"],
            tenant_id=ENG,
            generation=1,
            namespace=namespace,
            completed_at=utcnow() - timedelta(seconds=60),
            ended=True,
        )
        self._termination = Termination(
            exit_code=137,
            message=None,
            detail="container worker: ContainerStatusUnknown",
            eviction=eviction.strip(),
        )

    def list_executions(self) -> list[Any]:
        return [self._execution]

    def termination(self, execution: Any) -> Any:
        return self._termination

    def terminate(self, execution: Any) -> bool:
        return True

    def list_job_resources(self) -> list[Any]:
        return []

    def delete_job_resource(self, resource: Any) -> bool:
        return True


class _EventsUnreadable(ControlStore):
    def attempt_events(self, task_id: str, attempt_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("events query refused")


def seed(db: FakeFirestore, **overrides: Any) -> dict[str, str]:
    seed_tenant(db, ENG)
    return seed_stranded(
        db, TASK, state="RUNNING", heartbeat_seconds_ago=60, minutes_ago=9, **overrides
    )


def heartbeat(
    db: FakeFirestore,
    ids: dict[str, str],
    at: datetime,
    scratch: dict[str, Any] | None,
    *,
    generation: int = 1,
) -> None:
    """One HEARTBEAT event, shaped as `ControlPlane.emit` writes it."""
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    detail: dict[str, Any] = {"cpu_seconds": 12.0}
    if scratch is not None:
        detail["scratch"] = scratch
    db.docs[f"tasks/{ids['task']}/events/{event_id}"] = {
        "event_id": event_id,
        "task_id": ids["task"],
        "tenant_id": ENG,
        "type": EventType.HEARTBEAT.value,
        "at": at,
        "attempt_id": ids["attempt"],
        "lease_id": ids["lease"],
        "generation": generation,
        "detail": detail,
    }


def reconcile(
    db: FakeFirestore, ids: dict[str, str], eviction: str, *, store_class: type = ControlStore
) -> tuple[Any, io.StringIO]:
    stream = io.StringIO()
    logger = build_logger(stream=stream)
    store = store_class(db, logger=logger, filter_factory=plain_filter)
    rec = Reconciler(
        store=store,
        backends=[_Backend(ids, eviction)],
        config=config(enable_gke=False),
        logger=logger,
    )
    return rec.run_once(), stream


def _at(minutes_ago: float) -> datetime:
    return (utcnow() - timedelta(minutes=minutes_ago)).replace(microsecond=0)


def _hms(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Through the repair
# ---------------------------------------------------------------------------


def test_a_tmp_eviction_names_the_largest_paths_of_the_last_reading():
    db = FakeFirestore()
    ids = seed(db)
    newest_at = _at(2)
    heartbeat(db, ids, _at(6), {"tmp": OLDER})
    heartbeat(db, ids, newest_at, {"tmp": NEWEST, "home": _entry([("/home/swarm/.npm", 1.5)], used=1.5)})
    heartbeat(db, ids, _at(1), None)  # a beat whose walk found no volumes

    report, _ = reconcile(db, ids, TMP_EVICTION)

    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == TaskState.READY.value, task
    error = task["last_error"]
    assert error.startswith('reconciled: evicted: EmptyDir volume "tmp" exceeds 3Gi'), error
    # The worker's own formatting, read off the same reading.
    assert format_top(NEWEST) in error, error
    assert "/tmp/pgdata 2.1 GiB, /tmp/ms-playwright" in error, error
    assert f"largest at {_hms(newest_at)}" in error, error
    assert "/tmp/old-build" not in error, "an older reading was reported"
    assert "/home/swarm/.npm" not in error, "another volume's paths were reported"
    assert "retrying once" in error, error
    assert task["metadata"][DISK_EVICTIONS_KEY] == 1
    attempt = db.docs[f"attempts/{ids['attempt']}"]
    assert format_top(NEWEST) in str(attempt.get("error")), attempt


def test_readings_of_another_generation_are_ignored():
    db = FakeFirestore()
    ids = seed(db)
    heartbeat(db, ids, _at(3), {"tmp": NEWEST})
    # Newer, but written by a worker of another generation of this attempt id.
    heartbeat(db, ids, _at(1), {"tmp": OTHER_GENERATION}, generation=2)

    reconcile(db, ids, TMP_EVICTION)

    error = db.docs[f"tasks/{TASK}"]["last_error"]
    assert "/tmp/pgdata 2.1 GiB" in error, error
    assert "/tmp/stale-generation" not in error, error


def test_no_reading_leaves_the_reason_unchanged():
    without = FakeFirestore()
    reconcile(without, seed(without), TMP_EVICTION)
    baseline = without.docs[f"tasks/{TASK}"]["last_error"]

    db = FakeFirestore()
    ids = seed(db)
    heartbeat(db, ids, _at(2), None)
    reconcile(db, ids, TMP_EVICTION)

    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == TaskState.READY.value, task
    assert task["last_error"] == baseline
    assert "largest" not in baseline, baseline


def test_an_events_read_failure_never_blocks_the_repair():
    db = FakeFirestore()
    ids = seed(db)
    heartbeat(db, ids, _at(2), {"tmp": NEWEST})

    report, stream = reconcile(db, ids, TMP_EVICTION, store_class=_EventsUnreadable)

    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == TaskState.READY.value, task
    assert task["current_lease_id"] is None
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert "largest" not in task["last_error"], task["last_error"]
    assert task["last_error"].startswith('reconciled: evicted: EmptyDir volume "tmp" exceeds 3Gi')
    logged = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    warned = [line for line in logged if "scratch" in str(line.get("message") or line.get("event"))]
    assert warned, logged
    assert "events query refused" in json.dumps(warned)


def test_node_pressure_eviction_gets_no_report():
    db = FakeFirestore()
    ids = seed(db)
    heartbeat(db, ids, _at(2), {"tmp": NEWEST})

    report, _ = reconcile(db, ids, NODE_PRESSURE)

    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    error = db.docs[f"tasks/{TASK}"]["last_error"]
    assert error.startswith("reconciled: evicted: The node was low on resource"), error
    assert "largest" not in error and "/tmp/pgdata" not in error, error


def test_second_disk_eviction_still_fails_and_names_paths():
    db = FakeFirestore()
    ids = seed(db, attempt_count=2, max_attempts=3)
    db.docs[f"tasks/{TASK}"]["metadata"] = {DISK_EVICTIONS_KEY: 1}
    heartbeat(db, ids, _at(2), {"tmp": NEWEST})

    reconcile(db, ids, TMP_EVICTION)

    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == TaskState.FAILED.value, task
    assert task["attempt_count"] < task["max_attempts"]
    assert "not retried again" in task["last_error"], task["last_error"]
    assert format_top(NEWEST) in task["last_error"], task["last_error"]
    assert task["metadata"][DISK_EVICTIONS_KEY] == 2


# ---------------------------------------------------------------------------
# The pure pieces
# ---------------------------------------------------------------------------


def _event(at: datetime, scratch: Any, *, attempt: str = "att_1", generation: int = 1,
           kind: str = EventType.HEARTBEAT.value) -> dict[str, Any]:
    return {"type": kind, "at": at, "attempt_id": attempt, "generation": generation,
            "detail": {"scratch": scratch}}


def test_the_evicted_volume_is_named_from_kubelets_message():
    assert evicted_volume(TMP_EVICTION) == "tmp"
    assert evicted_volume(
        "Pod ephemeral local storage usage exceeds the total limit of containers 10Gi. "
    ) == EVERY_VOLUME
    assert evicted_volume(
        'Container worker exceeded its local ephemeral storage limit "4Gi". '
    ) == EVERY_VOLUME
    assert evicted_volume(NODE_PRESSURE) is None


def test_the_report_reads_as_path_size_pairs_with_the_volumes_use():
    at = datetime(2026, 10, 26, 10, 41, 12, tzinfo=timezone.utc)
    words = scratch_report([_event(at, {"tmp": NEWEST})], attempt_id="att_1", generation=1,
                           volume="tmp")
    assert words == (
        f"largest at 10:41:12Z: {format_top(NEWEST)} "
        '(volume "tmp" 2.9 GiB of 3 GiB)'
    ), words


def test_a_partial_reading_says_so_and_at_most_five_paths_are_named():
    at = datetime(2026, 10, 26, 10, 41, 12, tzinfo=timezone.utc)
    many = _entry([(f"/tmp/p{i}", 0.5) for i in range(8)], used=2.9, partial=True)
    words = scratch_report([_event(at, {"tmp": many})], attempt_id="att_1", generation=1,
                           volume="tmp")
    assert words is not None and "(partial)" in words, words
    assert "/tmp/p4" in words and "/tmp/p5" not in words, words


def test_a_pod_level_eviction_names_the_fullest_volume():
    at = datetime(2026, 10, 26, 10, 41, 12, tzinfo=timezone.utc)
    roomy = {**_entry([("/workspace/repo", 1.0)], used=1.0), "limit_bytes": 5 * GIB}
    words = scratch_report([_event(at, {"workspace": roomy, "tmp": NEWEST})],
                           attempt_id="att_1", generation=1, volume=EVERY_VOLUME)
    assert words is not None and "/tmp/pgdata" in words and '"tmp"' in words, words
    assert "/workspace/repo" not in words, words


def test_no_reading_of_this_attempt_is_no_report():
    at = datetime(2026, 10, 26, 10, 41, 12, tzinfo=timezone.utc)
    events = [
        _event(at, {"tmp": NEWEST}, attempt="att_other"),
        _event(at, {"tmp": NEWEST}, kind="checkpoint"),
        _event(at, {"tmp": NEWEST}, generation=2),
        _event(at, "not a reading"),
        _event(at, {"home": NEWEST}),
    ]
    assert scratch_report(events, attempt_id="att_1", generation=1, volume="tmp") is None
    assert scratch_report([], attempt_id="att_1", generation=1, volume="tmp") is None
