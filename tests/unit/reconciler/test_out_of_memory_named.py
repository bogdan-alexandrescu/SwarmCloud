"""An attempt that ran out of memory at its limit fails its task the first time, and says so (#1926).

WHAT WAS WRONG. task_021ff60c26f5440eb9c8 (AI Studio, claude-code, 8 GiB) ran
three attempts over about two hours on 2026-10-21/22. Attempts gen 1 and gen 3
were OOM-killed (exit 137) and requeued at the same size; gen 5 sat at
8.58-8.59 GB of its 8,589,934,592-byte limit on every heartbeat sample from
t=1900 s to 2220 s, then went silent and was reclaimed as "lease silent for
102s". The task ended FAILED lost_worker, and memory was named nowhere.

OWNER DECISION 2026-10-22 ("Name it, stop retrying it"): an attempt that was
OOM-killed, or reclaimed as silent while its last heartbeats sat at its memory
limit, FAILS its task after that first attempt, whatever `max_attempts` says,
with words that say it ran out of memory at its limit. A node-pressure eviction
or any other lost worker keeps today's retry.

WHAT EACH TEST PINS:

  * an OOM-killed attempt with attempts left FAILS the task and does not
    requeue it; `last_error`, the attempt's error and the `failed` event say
    "ran out of memory: the 8 GiB limit was reached (OOMKilled, exit 137)";
  * Cloud Run's memory sentence reads as the same limit;
  * an OOM kill before the runner started fails too, refund or not;
  * a silent lease whose last heartbeat samples sit at the limit fails with
    the stalled wording, whether its execution has ended or is still active;
  * a silent lease with memory well under the limit requeues as today;
  * a node-pressure eviction requeues as today;
  * the fail releases the lease and its slots and fences the generation
    exactly as today's reclaim does (invariants 1, 3 and 5);
  * the stall rule's threshold, its sample count, and the attempt and
    generation it reads.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore,
with the scripted GKE backend of the eviction suite.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_worker_evicted import (
    ATTEMPT,
    GENERATION,
    GKE,
    JOB_NAME,
    LEASE,
    NAMESPACE,
    NODE_PRESSURE,
    POOLS,
    TASK,
    failed_job,
    seed_a_running_attempt,
)

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger as build_reconciler_logger
from reconciler.memory import (
    OOM_STALL_SAMPLES,
    OOM_STALL_SHARE,
    out_of_memory_killed,
    stalled_at_memory_limit,
)
from reconciler.model import ExecutionPhase, ExecutionView, Termination
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import EndCause, utcnow
from swarm_common.states import EventType, TaskState
from worker_seeds import TENANT

#: The claude-code runner's limit, as cgroup `memory.max` reads it.
LIMIT = 8 * 1024**3

OOM_WORDS = "ran out of memory: the 8 GiB limit was reached (OOMKilled, exit 137)"
STALLED_WORDS = (
    "ran out of memory: the 8 GiB limit was reached "
    "(stalled at the limit; its lease went silent)"
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


def gke_oom() -> Termination:
    """What `GkeBackend.termination` reads off a pod the kernel OOM-killed."""
    return Termination(
        exit_code=137,
        detail="container worker: OOMKilled: the container passed its 8 GiB memory limit",
    )


def cloud_run_oom() -> Termination:
    """What `CloudRunBackend.termination` reads off a task past its memory limit."""
    return Termination(
        exit_code=137,
        detail="OOMKilled: Memory limit of 8192 MiB exceeded with 8243 MiB used.",
    )


def no_reason() -> Termination:
    return Termination(exit_code=None, detail="the backend recorded no exit code for this execution")


def running_job() -> ExecutionView:
    """The Job still listed active: a worker stalled, not exited."""
    return ExecutionView(
        name=JOB_NAME,
        backend=GKE,
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(minutes=9),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=GENERATION,
        namespace=NAMESPACE,
    )


def seed_heartbeats(
    db: FakeFirestore,
    mem: list[int | None],
    *,
    limit: int | None = LIMIT,
    peak: int | None = None,
    attempt_id: str = ATTEMPT,
    generation: int = GENERATION,
    seconds_ago: float = 150,
    key: str = "hb",
) -> None:
    """One HEARTBEAT event per 30 samples, as `lifecycle._usage_reading` writes them."""
    at = utcnow() - timedelta(seconds=seconds_ago)
    chunks = [mem[i : i + 30] for i in range(0, len(mem), 30)] or [[]]
    t = 1900.0
    for n, chunk in enumerate(chunks):
        series = []
        for value in chunk:
            series.append({"t": t, "cpu": 1.2, "mem": value, "thr": 0.0})
            t += 5.0
        db.seed(
            f"tasks/{TASK}/events/evt_{key}_{attempt_id}_{generation}_{n}",
            {
                "event_id": f"evt_hb_{n}",
                "task_id": TASK,
                "tenant_id": TENANT,
                "type": EventType.HEARTBEAT.value,
                "at": at - timedelta(seconds=150 * (len(chunks) - 1 - n)),
                "attempt_id": attempt_id,
                "lease_id": LEASE,
                "generation": generation,
                "detail": {
                    "series": series,
                    "series_clock": t,
                    "memory_limit_bytes": limit,
                    "peak_rss_bytes": peak if peak is not None else max(
                        (v for v in chunk if v is not None), default=None
                    ),
                    "cpu_seconds": 100.0 + n,
                },
            },
        )


def reconcile(
    db: FakeFirestore,
    config: ReconcilerConfig,
    termination: Any,
    *,
    execution: ExecutionView | None = None,
) -> Any:
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(
        name=GKE,
        executions=[execution or failed_job()],
        journal=db.writes,
        terminations={JOB_NAME: termination},
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()


def failed_events(db: FakeFirestore) -> list[dict[str, Any]]:
    return [e for e in db.events(TASK) if e["type"] == EventType.FAILED.value]


# ---------------------------------------------------------------------------
# OOM-killed: fails on the first attempt
# ---------------------------------------------------------------------------


def test_an_oom_killed_attempt_with_attempts_left_fails_its_task(db, reconciler_config):
    """task_021ff60c's gen 1: exit 137 OOMKilled, attempt 1 of 3, requeued."""
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)

    report = reconcile(db, reconciler_config, gke_oom())

    assert [o.kind for o in report.outcomes] == ["stale_lease"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    # MUTATION: drop the out-of-memory branch from `Reconciler._repair`'s plan
    # -> the task is requeued READY with two attempts left, as gen 1 was.
    assert task["state"] == TaskState.FAILED.value, task
    assert task["attempt_count"] < task["max_attempts"], task
    # MUTATION: map an OOM to another EndCause -> fails (the contract is frozen).
    assert task["end_cause"] == EndCause.LOST_WORKER.value, task
    # MUTATION: leave the memory words off the error -> fails.
    assert task["last_error"].startswith(f"reconciled: {OOM_WORDS}"), task["last_error"]
    # MUTATION: write a retry time on a task that will not retry -> fails.
    assert task.get("next_eligible_at") is None, task
    # MUTATION: requeue first and fail later -> a `ready` event is written.
    assert EventType.READY.value not in db.event_types(TASK), db.event_types(TASK)
    attempt_error = str(db.doc(f"attempts/{ATTEMPT}").get("error") or "")
    # MUTATION: pass the finding's old reason as `attempt_reason` -> fails.
    assert OOM_WORDS in attempt_error, attempt_error
    failed = failed_events(db)
    assert len(failed) == 1, db.event_types(TASK)
    # MUTATION: keep the finding kind as the failed event's `reason` -> fails.
    assert failed[0]["detail"]["reason"] == OOM_WORDS, failed[0]
    assert failed[0]["detail"]["detail"].startswith(OOM_WORDS), failed[0]
    assert failed[0]["detail"]["finding"] == "stale_lease", failed[0]
    assert failed[0]["detail"]["out_of_memory"] == "oom_killed", failed[0]


def test_cloud_runs_memory_sentence_names_the_same_limit(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)

    reconcile(db, reconciler_config, cloud_run_oom())

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    # MUTATION: read only GKE's "passed its ... memory limit" -> the limit is
    # unnamed ("its memory limit was reached") and this fails.
    assert OOM_WORDS in task["last_error"], task["last_error"]


def test_an_oom_kill_with_no_readable_limit_still_names_memory(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)

    reconcile(
        db,
        reconciler_config,
        Termination(exit_code=137, detail="container worker: OOMKilled: the container passed its memory limit"),
    )

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    # MUTATION: require a limit before naming the kill -> fails.
    assert "ran out of memory: its memory limit was reached (OOMKilled, exit 137)" in task[
        "last_error"
    ], task["last_error"]


def test_an_oom_kill_before_the_runner_started_fails_too(db, reconciler_config):
    """The ended-at-startup rule refunds and requeues; an OOM is not retried at the same size."""
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=30)
    db.doc(f"tasks/{TASK}")["state"] = TaskState.STARTING.value
    db.doc(f"leases/{LEASE}")["state"] = TaskState.STARTING.value

    report = reconcile(db, reconciler_config, gke_oom())

    assert [o.kind for o in report.outcomes] == ["worker_ended_at_startup"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    # MUTATION: leave WORKER_ENDED_AT_STARTUP out of the OOM-kill rule's kinds
    # -> the task is refunded and requeued READY.
    assert task["state"] == TaskState.FAILED.value, task
    assert task["last_error"].startswith(f"reconciled: {OOM_WORDS}"), task["last_error"]


def test_a_cancel_requested_before_the_oom_still_ends_cancelled(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)
    db.doc(f"tasks/{TASK}")["cancel_requested"] = True

    reconcile(db, reconciler_config, gke_oom())

    task = db.doc(f"tasks/{TASK}")
    # MUTATION: test the OOM before the cancel in the plan -> FAILED.
    assert task["state"] == TaskState.CANCELLED.value, task


# ---------------------------------------------------------------------------
# Stalled at the limit, then silent: fails on the first attempt
# ---------------------------------------------------------------------------


def at_limit(n: int) -> list[int | None]:
    """task_021ff60c gen 5's samples: 8.58-8.59 GB of 8,589,934,592."""
    return [LIMIT - 4_000_000 - (i % 3) * 1_000_000 for i in range(n)]


@pytest.mark.parametrize(
    "execution,kind",
    [
        pytest.param(None, "stale_lease", id="execution-ended"),
        pytest.param(running_job(), "dead_worker", id="execution-active"),
    ],
)
def test_a_silent_lease_whose_last_heartbeats_sat_at_the_limit_fails(
    db, reconciler_config, execution, kind
):
    """task_021ff60c's gen 5: 'reconciled: lease silent for 102s', end_cause lost_worker."""
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=102)
    seed_heartbeats(db, [LIMIT // 2] * 30 + at_limit(30), peak=LIMIT)

    report = reconcile(db, reconciler_config, no_reason(), execution=execution)

    assert [o.kind for o in report.outcomes] == [kind], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    # MUTATION: skip the heartbeat read in `Reconciler._out_of_memory` -> the
    # task is requeued READY, as gen 5's predecessors were.
    assert task["state"] == TaskState.FAILED.value, task
    assert task["end_cause"] == EndCause.LOST_WORKER.value, task
    # MUTATION: word the stall as an OOM kill -> fails.
    assert task["last_error"].startswith(f"reconciled: {STALLED_WORDS}"), task["last_error"]
    assert "lease silent for" in task["last_error"], task["last_error"]
    failed = failed_events(db)
    assert len(failed) == 1, db.event_types(TASK)
    assert failed[0]["detail"]["reason"] == STALLED_WORDS, failed[0]
    assert failed[0]["detail"]["out_of_memory"] == "stalled_at_limit", failed[0]
    assert failed[0]["detail"]["memory_limit_bytes"] == LIMIT, failed[0]


def test_a_silent_lease_well_under_its_limit_requeues_as_today(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)
    seed_heartbeats(db, [LIMIT // 2] * 30, peak=LIMIT // 2 + 1)

    reconcile(db, reconciler_config, no_reason())

    task = db.doc(f"tasks/{TASK}")
    # MUTATION: drop the share check (any heartbeat counts) -> FAILED.
    assert task["state"] == TaskState.READY.value, task
    assert "ran out of memory" not in task["last_error"], task["last_error"]
    assert task["last_error"].endswith("its execution has ended (failed): the worker is lost"), task[
        "last_error"
    ]


def test_a_silent_lease_with_no_heartbeat_events_requeues_as_today(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)

    reconcile(db, reconciler_config, no_reason())

    # MUTATION: read "no evidence" as "at the limit" -> FAILED.
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.READY.value


def test_a_heartbeat_read_that_fails_requeues_as_today(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)
    seed_heartbeats(db, at_limit(30))
    logger = build_reconciler_logger(stream=io.StringIO())

    class _Unreadable(ControlStore):
        def attempt_events(self, task_id: str, attempt_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("firestore unavailable")

    store = _Unreadable(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    backend = FakeBackend(
        name=GKE, executions=[failed_job()], journal=db.writes, terminations={JOB_NAME: no_reason()}
    )
    Reconciler(store=store, backends=[backend], config=reconciler_config, logger=logger).run_once()

    # MUTATION: let the read's exception escape the repair -> no repair at all
    # (the lease is never released), and this fails.
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.READY.value
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None


# ---------------------------------------------------------------------------
# A node's pressure is not the task's own limit
# ---------------------------------------------------------------------------

#: kubelet's words for a node short of memory, not the container past its own.
NODE_MEMORY_PRESSURE = (
    "The node was low on resource: memory. Threshold quantity: 100Mi, available: 90Mi. "
)


@pytest.mark.parametrize("message", [NODE_PRESSURE, NODE_MEMORY_PRESSURE], ids=["disk", "memory"])
def test_a_node_pressure_eviction_requeues_as_today(db, reconciler_config, message):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3)
    # Even with the attempt's own samples high: the node evicted it.
    seed_heartbeats(db, at_limit(30))

    report = reconcile(
        db,
        reconciler_config,
        Termination(
            exit_code=137,
            detail="container worker: OOMKilled: the container passed its 8 GiB memory limit",
            eviction=message.strip(),
        ),
    )

    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    # MUTATION: let an eviction's termination count as an OOM kill -> FAILED.
    assert task["state"] == TaskState.READY.value, task
    assert "ran out of memory" not in task["last_error"], task["last_error"]


def test_a_node_pressure_eviction_reclaimed_as_silent_requeues_as_today(db, reconciler_config):
    """Inside the eviction rule's grace the stale-lease rule reclaims it."""
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)
    seed_heartbeats(db, at_limit(30))
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(
        name=GKE,
        executions=[failed_job(ended_seconds_ago=5)],
        journal=db.writes,
        terminations={
            JOB_NAME: Termination(
                exit_code=137,
                detail="container worker: ContainerStatusUnknown",
                eviction=NODE_MEMORY_PRESSURE.strip(),
            )
        },
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(
        store=store, backends=[backend], config=reconciler_config, logger=logger
    ).run_once()

    assert [o.kind for o in report.outcomes] == ["stale_lease"], report.as_dict()
    # MUTATION: run the stall rule on an evicted attempt -> FAILED.
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.READY.value


# ---------------------------------------------------------------------------
# Invariants 1, 3 and 5: the fail releases and fences exactly as a reclaim does
# ---------------------------------------------------------------------------


def _slots_and_fence(db: FakeFirestore) -> dict[str, Any]:
    lease = db.doc(f"leases/{LEASE}")
    task = db.doc(f"tasks/{TASK}")
    return {
        "released": lease.get("released_at") is not None,
        "pools": {pool: db.doc(f"pools/{pool}")["active"] for pool in POOLS},
        "generation": task["current_generation"],
        "lease_id": task.get("current_lease_id"),
        "attempt_count": task["attempt_count"],
    }


@pytest.mark.parametrize("how", ["oom_killed", "stalled"])
def test_the_fail_releases_and_fences_exactly_as_a_reclaim(reconciler_config, how):
    oom, plain = FakeFirestore(), FakeFirestore()
    seed_a_running_attempt(oom, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)
    seed_a_running_attempt(plain, attempt_count=1, max_attempts=3, heartbeat_seconds_ago=200)
    if how == "stalled":
        seed_heartbeats(oom, at_limit(30))
    reconcile(oom, reconciler_config, gke_oom() if how == "oom_killed" else no_reason())
    reconcile(plain, reconciler_config, no_reason())

    assert oom.doc(f"tasks/{TASK}")["state"] == TaskState.FAILED.value
    assert plain.doc(f"tasks/{TASK}")["state"] == TaskState.READY.value
    # MUTATION: skip the release or the fence for an OOM fail -> the two differ.
    assert _slots_and_fence(oom) == _slots_and_fence(plain)
    assert _slots_and_fence(oom)["released"] is True
    assert set(_slots_and_fence(oom)["pools"].values()) == {0}
    assert _slots_and_fence(oom)["generation"] == GENERATION + 1


# ---------------------------------------------------------------------------
# The rules, pure
# ---------------------------------------------------------------------------


def _events(mem: list[int | None], **kwargs: Any) -> list[dict[str, Any]]:
    db = FakeFirestore()
    seed_heartbeats(db, mem, **kwargs)
    return db.events(TASK)


def _stalled(events: list[dict[str, Any]], **kwargs: Any) -> Any:
    return stalled_at_memory_limit(
        events, attempt_id=kwargs.get("attempt_id", ATTEMPT), generation=GENERATION
    )


def test_the_threshold_is_98_percent_of_the_limit_over_the_last_samples():
    assert OOM_STALL_SHARE == 0.98
    assert OOM_STALL_SAMPLES == 6
    edge = int(LIMIT * OOM_STALL_SHARE) + 1
    below = int(LIMIT * OOM_STALL_SHARE) - 1
    # MUTATION: compare with > the limit itself -> 98% no longer counts.
    assert _stalled(_events([edge] * OOM_STALL_SAMPLES, peak=edge)) is not None
    # MUTATION: lower the share -> a sample just under 98% counts.
    assert _stalled(_events([below] * OOM_STALL_SAMPLES, peak=below)) is None


def test_one_late_sample_below_the_limit_is_not_a_stall():
    # MUTATION: read any of the last samples, not all of them -> fails.
    assert _stalled(_events(at_limit(29) + [LIMIT // 2], peak=LIMIT - 1)) is None


def test_fewer_samples_than_the_rule_reads_is_not_a_stall():
    # MUTATION: judge on whatever samples there are -> fails.
    assert _stalled(_events(at_limit(OOM_STALL_SAMPLES - 1), peak=LIMIT - 1)) is None


def test_a_missing_sample_is_not_at_the_limit():
    # MUTATION: skip None samples -> the five readable ones read as a stall.
    assert _stalled(_events(at_limit(OOM_STALL_SAMPLES - 1) + [None], peak=LIMIT - 1)) is None


def test_a_series_older_than_the_limit_reads_its_peak_instead():
    """A worker that wrote no `mem` points: peak_rss at the limit is the owner's other sign."""
    # MUTATION: drop the peak fallback -> None.
    found = _stalled(_events([], peak=LIMIT))
    assert found is not None and found.limit_bytes == LIMIT
    assert _stalled(_events([], peak=LIMIT - 10_000_000)) is None


def test_no_limit_means_no_judgement():
    # MUTATION: default the limit -> fails.
    assert _stalled(_events(at_limit(30), limit=None)) is None


def test_only_this_attempt_at_this_generation_is_read():
    other_attempt = _events(at_limit(30), attempt_id="att_other")
    other_generation = _events(at_limit(30), generation=GENERATION + 1)
    # MUTATION: read every heartbeat of the task -> a predecessor's stall counts.
    assert _stalled(other_attempt) is None
    assert _stalled(other_generation) is None


def test_the_newest_heartbeat_decides():
    db = FakeFirestore()
    seed_heartbeats(db, at_limit(30), seconds_ago=400, key="older")
    seed_heartbeats(db, [LIMIT // 2] * 30, seconds_ago=100, key="newer")
    # MUTATION: sort the heartbeats oldest-last -> the older stall decides.
    assert _stalled(db.events(TASK)) is None


@pytest.mark.parametrize(
    "termination,words",
    [
        pytest.param(gke_oom(), OOM_WORDS, id="gke"),
        pytest.param(cloud_run_oom(), OOM_WORDS, id="cloud-run"),
        pytest.param(
            Termination(exit_code=137, detail="OOMKilled: Memory limit of 1536 MiB exceeded"),
            "ran out of memory: the 1.5 GiB limit was reached (OOMKilled, exit 137)",
            id="cloud-run-fraction",
        ),
        pytest.param(Termination(exit_code=137, detail="container worker: Error"), None, id="sigkill"),
        pytest.param(Termination(exit_code=1, detail="container worker: Error"), None, id="crash"),
        pytest.param(
            Termination(exit_code=137, detail=gke_oom().detail, eviction="The node was low on resource: memory."),
            None,
            id="evicted",
        ),
        pytest.param(None, None, id="unread"),
    ],
)
def test_what_reads_as_an_oom_kill(termination, words):
    found = out_of_memory_killed(termination)
    # MUTATION: treat exit 137 alone as an OOM -> the sigkill case reads as one.
    assert (found.words if found else None) == words
