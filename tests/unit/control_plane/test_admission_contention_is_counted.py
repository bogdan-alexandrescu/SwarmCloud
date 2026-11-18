"""Admission says how many times its transaction body ran, and how long it took.

S32 / spec §8 row 6: "Firestore contention at 100 admissions/min? Measure
before designing the sharding." Contention on the `global` pool shows up in
exactly one place -- Firestore aborting the admission commit and
`firestore.transactional` re-running the body against fresh reads -- and until
this change nothing counted those re-runs. A scheduler fighting over `global`
looked identical to an idle one, except slower, and nothing said how much
slower either.

`SchedulerStore.acquire_lease` now logs one record per admission carrying:

    admission_outcome     leased | denied | aborted | error
    admission_runs        how many times the transaction body ran
    admission_reruns      runs - 1: the contention count
    admission_latency_ms  wall time for the whole transactional call

The contended cases are driven with `ContendedFirestore`, whose commit aborts
when a document it read has changed, so the REAL `firestore.transactional`
re-runs the REAL frozen `acquire_lease_in_transaction`. Nothing here counts
calls to a stub.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from swarm_common.admission import AdmissionConfig, AdmissionDenied

from scheduler.store import SchedulerStore

from .conftest import seed_pool, seed_task
from .fakes import FakeFirestore, FakeTransaction
from .test_request_cancel_is_transactional import ContendedFirestore

TENANT = "eng"
BACKEND = "CLOUD_RUN_JOB"
ADMISSION = AdmissionConfig()
STORE_LOGGER = "scheduler.store"


def _admission_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if hasattr(r, "admission_outcome")]


def _world(db: FakeFirestore, *, global_limit: int = 10) -> None:
    seed_pool(db, "global", hard_limit=global_limit)
    seed_pool(db, f"tenant:{TENANT}", hard_limit=10)


def _admit(db: Any, task_id: str):
    store = SchedulerStore(db)
    return store.acquire_lease(store.get_task(task_id), units=1, backend=BACKEND, config=ADMISSION)


def test_an_uncontended_admission_logs_one_run_and_no_reruns(caplog) -> None:
    db = ContendedFirestore()
    _world(db)
    seed_task(db, task_id="t1", tenant_id=TENANT)

    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER):
        lease = _admit(db, "t1")

    records = _admission_records(caplog)
    assert len(records) == 1, f"expected one admission record, got {len(records)}"
    rec = records[0]
    assert rec.admission_outcome == "leased"
    assert rec.admission_runs == 1
    assert rec.admission_reruns == 0
    assert rec.task_id == "t1"
    assert rec.tenant_id == TENANT
    assert rec.lease_id == lease.lease_id
    assert isinstance(rec.admission_latency_ms, float)
    assert rec.admission_latency_ms >= 0
    assert rec.levelno == logging.INFO, "a granted lease is always worth one INFO line"


def test_a_pool_written_mid_transaction_is_counted_as_a_rerun(caplog) -> None:
    """Another admitter commits to `global` between this one's read and commit.

    The commit aborts, the body runs again against the new `active`, and the
    second run commits. That is one re-run, and it is the contention signal.
    """
    db = ContendedFirestore()
    _world(db)
    seed_task(db, task_id="t1", tenant_id=TENANT)
    seed_task(db, task_id="t2", tenant_id=TENANT)

    db.interleave("pools/global", lambda: _admit(db, "t2"))
    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER):
        _admit(db, "t1")

    assert db.aborts == ["pools/global"], (
        "the interleaved admission did not abort this one; nothing was contended"
    )
    mine = [r for r in _admission_records(caplog) if r.task_id == "t1"]
    assert len(mine) == 1
    assert mine[0].admission_outcome == "leased"
    assert mine[0].admission_runs == 2
    assert mine[0].admission_reruns == 1
    # Both admissions landed: all-or-nothing held and nothing was double-counted.
    assert db.docs["pools/global"]["active"] == 2


def test_losing_the_last_slot_after_a_rerun_is_a_denial_with_one_rerun(caplog) -> None:
    """The last-slot race: re-run, re-read, find the pool full, deny.

    Logged at INFO, not DEBUG, because a denial that needed a re-run is a
    contended denial -- exactly what the measurement is for.
    """
    db = ContendedFirestore()
    _world(db, global_limit=1)
    seed_task(db, task_id="t1", tenant_id=TENANT)
    seed_task(db, task_id="t2", tenant_id=TENANT)

    db.interleave("pools/global", lambda: _admit(db, "t2"))
    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER), pytest.raises(AdmissionDenied):
        _admit(db, "t1")

    mine = [r for r in _admission_records(caplog) if r.task_id == "t1"]
    assert len(mine) == 1
    assert mine[0].admission_outcome == "denied"
    assert mine[0].admission_runs == 2
    assert mine[0].admission_reruns == 1
    assert mine[0].levelno == logging.INFO
    assert db.docs["pools/global"]["active"] == 1, "the limit was exceeded"


def test_a_clean_denial_is_debug_so_a_full_platform_does_not_flood_the_log(caplog) -> None:
    """A full pool denies every READY task on every pass. One INFO line each
    would be thousands per drain, about nothing contended."""
    db = FakeFirestore()
    _world(db, global_limit=0)
    seed_task(db, task_id="t1", tenant_id=TENANT)

    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER), pytest.raises(AdmissionDenied):
        _admit(db, "t1")

    (rec,) = _admission_records(caplog)
    assert rec.admission_outcome == "denied"
    assert rec.admission_runs == 1
    assert rec.admission_reruns == 0
    assert rec.levelno == logging.DEBUG


class _AlwaysAborts(FakeTransaction):
    def _commit(self) -> list[Any]:
        self._buffer = []
        raise gexc.Aborted("contended")


class _NeverCommits(FakeFirestore):
    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _AlwaysAborts(self)


def test_exhausting_the_retries_is_logged_as_aborted_and_still_raises(caplog) -> None:
    """Five aborts in a row is the saturation case. It must be counted as
    `aborted` -- not `error`, and not lost -- and the caller still sees the
    exception, unchanged, so the drain's handling does not move."""
    db = _NeverCommits()
    _world(db)
    seed_task(db, task_id="t1", tenant_id=TENANT)

    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER), pytest.raises(ValueError) as raised:
        _admit(db, "t1")

    assert isinstance(raised.value.__cause__, gexc.Aborted)
    (rec,) = _admission_records(caplog)
    assert rec.admission_outcome == "aborted"
    assert rec.admission_runs == 5
    assert rec.admission_reruns == 4
    assert rec.levelno == logging.WARNING
    # Nothing was reserved: an abort is the "nothing" half of all-or-nothing.
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["tasks/t1"]["state"] == "READY"


class _ReadAborts(FakeTransaction):
    """ABORTED from a transactional READ of a contended pool document.

    `firestore.transactional` retries an Aborted only from commit, so this one
    reaches `acquire_lease` raw, after a single run of the body."""

    def get(self, ref: Any, **kwargs: Any) -> Any:
        if getattr(ref, "path", "").startswith("pools/"):
            raise gexc.Aborted("contended read")
        return super().get(ref, **kwargs)


class _ReadsAbort(FakeFirestore):
    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _ReadAborts(self)


def test_an_aborted_read_is_counted_as_aborted_not_error(caplog) -> None:
    """Read-time ABORTED is lock contention at its worst. Classed as `error`
    it would vanish from the saturation count docs/scaling.md §5 keys on."""
    db = _ReadsAbort()
    _world(db)
    seed_task(db, task_id="t1", tenant_id=TENANT)

    with caplog.at_level(logging.DEBUG, logger=STORE_LOGGER), pytest.raises(gexc.Aborted):
        _admit(db, "t1")

    (rec,) = _admission_records(caplog)
    assert rec.admission_outcome == "aborted"
    assert rec.admission_runs == 1
    assert rec.admission_reruns == 0
    assert rec.levelno == logging.WARNING
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["tasks/t1"]["state"] == "READY"


def test_the_scheduler_exports_admission_latency_and_reruns_as_metrics(make_scheduler, db) -> None:
    """Item 1's metric: the same numbers the log carries, on /metrics."""
    scheduler = make_scheduler()
    store = scheduler.store
    assert store.admission_observer is not None, "the scheduler did not wire its metrics into the store"
    _world(db)
    seed_task(db, task_id="t1", tenant_id=TENANT)

    store.acquire_lease(store.get_task("t1"), units=1, backend=BACKEND, config=ADMISSION)

    body = scheduler._metrics.render()[0].decode()
    assert 'swarm_scheduler_admission_seconds_count{outcome="leased"} 1.0' in body
    # No re-run, so no re-run series has been incremented.
    assert 'swarm_scheduler_admission_reruns_total{outcome="leased"}' not in body


def test_observe_admission_counts_reruns_and_aborts() -> None:
    from scheduler.metrics import SchedulerMetrics

    metrics = SchedulerMetrics()
    metrics.observe_admission("leased", 3, 120.0)
    metrics.observe_admission("aborted", 5, 900.0)

    body = metrics.render()[0].decode()
    assert 'swarm_scheduler_admission_reruns_total{outcome="leased"} 2.0' in body
    assert 'swarm_scheduler_admission_reruns_total{outcome="aborted"} 4.0' in body
    assert 'swarm_scheduler_admission_seconds_count{outcome="aborted"} 1.0' in body


def test_a_failing_observer_never_changes_the_admission(caplog) -> None:
    db = FakeFirestore()
    _world(db)
    seed_task(db, task_id="t1", tenant_id=TENANT)
    store = SchedulerStore(db)

    def boom(outcome: str, runs: int, latency_ms: float) -> None:
        raise RuntimeError("metrics backend down")

    store.admission_observer = boom
    lease = store.acquire_lease(store.get_task("t1"), units=1, backend=BACKEND, config=ADMISSION)

    assert lease.lease_id
    assert db.docs["pools/global"]["active"] == 1
