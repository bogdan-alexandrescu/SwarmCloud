#!/usr/bin/env python3
"""Admission contention harness: the real frozen admission, under offered load.

THE QUESTION (S32, spec §8 row 6, docs/BUILD_PROMPT_V2.md:740)
-------------------------------------------------------------
"Firestore contention at 100 admissions/min? Measure before designing the
sharding." Every admission reads and writes `global` plus four to six narrower
pools in ONE transaction (CONTRACT.md invariant 2). Firestore sustains roughly
one write per second per document before contention shows, and 200/min is
already 3.3/s on `global`. Whether that saturates is the input to a frozen
change (`pool_names_for()` sharding `global`), so it is measured, not argued.

WHAT RUNS
---------
`SchedulerStore.acquire_lease` -- the production method, which calls the
FROZEN `acquire_lease_in_transaction` inside `firestore.transactional` -- and
`SchedulerStore.release_lease`, which calls the frozen release. Nothing in this
file restates admission. The per-admission numbers are read from the log record
`acquire_lease` itself writes (`admission_runs`, `admission_reruns`,
`admission_latency_ms`, `admission_outcome`), so this measures exactly what
production logs rather than a parallel count that could disagree with it.

N admitters (threads, standing in for N scheduler instances: one instance
admits serially) take arrivals from an OPEN-LOOP schedule: arrival i is due at
`start + i * 60 / rate`, whether or not earlier ones have finished. A closed
loop would slow its own offered load exactly when the database slows, and
report a healthy latency at a rate it never offered. The lag between an
arrival's due time and its start is recorded too, and a row whose achieved
rate falls short of the offered one says so: that row measured the harness.

WHERE IT RUNS -- A DEDICATED BENCH DATABASE ONLY (owner decision OD-B17-1)
---------------------------------------------------------------------------
It writes pools named `global`, `tenant:...` and so on, because those are the
names the frozen `pool_names_for()` returns. Against the live `swarm` database
that would overwrite the live pools' limits and counts. So `check_database`
refuses the live database, `(default)` (another team's, in a shared project)
and any name that is not `swarm-bench` or `swarm-bench-<suffix>`, before a
client is even built. Pool limits are set far above the offered load: this
measures contention, not denials, and a denial would be a short-circuit that
reads as a fast admission.

Every document it writes carries `bench_run_id`; cleanup deletes this run's
tasks, leases and pools and nothing else. A run lock (`bench_lock/contention`)
refuses a second concurrent run against the same database, whose writes would
be counted as this run's contention.

Usage (normally through scripts/bench-contention.sh):
    bench_contention.py --project P --database swarm-bench --live-database swarm
        [--rates 100,200,400] [--duration 60] [--admitters 8]
        [--provider anthropic] [--no-release] [--samples OUT.jsonl]
        [--report OUT.json] [--keep] [--break-lock]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from google.cloud import firestore

from swarm_common.admission import AdmissionConfig, AdmissionDenied, _snapshot
from swarm_common.models import Task, pool_names_for
from swarm_common.states import TaskState

from scheduler.store import SchedulerStore

HERE = Path(__file__).resolve().parent

#: The logger `SchedulerStore.acquire_lease` writes its admission record to.
STORE_LOGGER = "scheduler.store"

#: Never written by this harness: the platform's database (CONTRACT.md) and
#: Firestore's `(default)`, which belongs to whoever in this shared project got
#: there first. The wrapper also passes the environment's FIRESTORE_DATABASE as
#: `--live-database`, so a renamed live database is refused too.
LIVE_DATABASES = frozenset({"swarm", "(default)"})
BENCH_DATABASE = re.compile(r"^swarm-bench(-[a-z0-9]+)*$")

#: Far above any offered load, so capacity never binds. Kept well inside an int64.
BENCH_POOL_LIMIT = 1_000_000_000

#: The shape of the heaviest real admission: `claude-code` with a provider
#: reserves seven pools (global, tenant, resource, runner, backend, provider,
#: provider-per-tenant). The pool LIST comes from the frozen `pool_names_for`,
#: never from here.
DEFAULT_PROFILE = "claude-code"
DEFAULT_RESOURCE_CLASS = "standard"
DEFAULT_BACKEND = "CLOUD_RUN_JOB"
DEFAULT_PROVIDER = "anthropic"

#: A row is suspect, not saturated, when the harness could not offer the rate.
#: 95% leaves room for the scheduling jitter of a sleeping thread.
OFFERED_RATE_FLOOR = 0.95

LOCK_COLLECTION = "bench_lock"
LOCK_DOC = "contention"
#: A lock older than this is a crashed run, and --break-lock is not needed.
LOCK_STALE_SECONDS = 3600


class Refused(Exception):
    """The harness will not run against this target."""


def check_database(name: str, *, live: str = "swarm") -> None:
    """Refuse anything that is not a dedicated bench database."""
    if name in LIVE_DATABASES or (live and name == live):
        raise Refused(
            f"{name!r} is a live database; this harness overwrites pool documents "
            "and runs only against a dedicated bench database (swarm-bench[-suffix])"
        )
    if not BENCH_DATABASE.match(name):
        raise Refused(
            f"{name!r} is not a bench database name; expected swarm-bench or "
            "swarm-bench-<suffix>, so nothing live can be named by accident"
        )


def _benchstat() -> Any:
    spec = importlib.util.spec_from_file_location("benchstat", HERE / "benchstat.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchstat = _benchstat()


# ---------------------------------------------------------------------------
# Capturing the record acquire_lease writes
# ---------------------------------------------------------------------------


class _AdmissionRecords(logging.Handler):
    """Collects `acquire_lease`'s admission records, keyed by task id."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._lock_records = threading.Lock()
        self._by_task: dict[str, logging.LogRecord] = {}

    def emit(self, record: logging.LogRecord) -> None:
        task_id = getattr(record, "task_id", None)
        if task_id is None or not hasattr(record, "admission_outcome"):
            return
        with self._lock_records:
            self._by_task[str(task_id)] = record

    def pop(self, task_id: str) -> logging.LogRecord | None:
        with self._lock_records:
            return self._by_task.pop(task_id, None)


class _Capture:
    def __enter__(self) -> _AdmissionRecords:
        self._logger = logging.getLogger(STORE_LOGGER)
        self._level = self._logger.level
        self.handler = _AdmissionRecords()
        self._logger.addHandler(self.handler)
        # DEBUG, so a clean denial's record is captured as well.
        self._logger.setLevel(logging.DEBUG)
        return self.handler

    def __exit__(self, *exc: Any) -> None:
        self._logger.removeHandler(self.handler)
        self._logger.setLevel(self._level)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    runner_profile: str = DEFAULT_PROFILE
    resource_class: str = DEFAULT_RESOURCE_CLASS
    backend: str = DEFAULT_BACKEND
    provider: str | None = DEFAULT_PROVIDER

    def pools(self, tenant_id: str) -> list[str]:
        return pool_names_for(
            tenant_id=tenant_id,
            provider=self.provider,
            resource_class=self.resource_class,
            runner_profile=self.runner_profile,
            backend=self.backend,
        )


def tenant_for(run_id: str) -> str:
    return f"bench-{run_id}"


def seed_pools(db: Any, run_id: str, shape: Shape) -> list[str]:
    names = shape.pools(tenant_for(run_id))
    now = datetime.now(timezone.utc)
    batch = db.batch()
    for name in names:
        batch.set(
            db.collection("pools").document(name),
            {
                "name": name,
                "hard_limit": BENCH_POOL_LIMIT,
                "adaptive_target": None,
                "quota_derived_limit": None,
                "active": 0,
                "enabled": True,
                "updated_at": now,
                "bench_run_id": run_id,
            },
        )
    batch.commit()
    return names


def seed_tasks(db: Any, run_id: str, shape: Shape, count: int, *, label: str) -> list[Task]:
    """READY tasks, written BEFORE the timed window so seeding is not measured."""
    tenant = tenant_for(run_id)
    now = datetime.now(timezone.utc)
    tasks: list[Task] = []
    batch, pending = db.batch(), 0
    for i in range(count):
        task = Task(
            id=f"bench-{run_id}-{label}-{i:05d}",
            tenant_id=tenant,
            created_at=now,
            updated_at=now,
            state=TaskState.READY,
            runner_profile=shape.runner_profile,
            resource_class=shape.resource_class,
            input={},
            submitted_by="bench-contention",
            provider=shape.provider,
        )
        batch.set(
            db.collection("tasks").document(task.id),
            {
                "id": task.id,
                "tenant_id": tenant,
                "created_at": now,
                "updated_at": now,
                "state": TaskState.READY.value,
                "runner_profile": shape.runner_profile,
                "resource_class": shape.resource_class,
                "provider": shape.provider,
                "input": {},
                "submitted_by": "bench-contention",
                "priority": 0,
                "attempt_count": 0,
                "current_generation": 0,
                "current_lease_id": None,
                "cancel_requested": False,
                "blocked_by": [],
                "bench_run_id": run_id,
            },
        )
        pending += 1
        tasks.append(task)
        if pending == 400:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    return tasks


# ---------------------------------------------------------------------------
# One offered rate
# ---------------------------------------------------------------------------


@dataclass
class Admission:
    task_id: str
    due: float
    started: float
    outcome: str | None = None          # from acquire_lease's own record
    runs: int | None = None
    latency_ms: float | None = None
    lease_id: str | None = None
    release_ms: float | None = None
    release_error: str | None = None
    raised: str | None = None           # exception type name, never its text

    @property
    def lag_ms(self) -> float:
        return (self.started - self.due) * 1000.0


@dataclass
class RateResult:
    rate_per_min: int
    admitters: int
    pools: int
    duration_s: float
    admissions: list[Admission] = field(default_factory=list)


def _admit_one(
    store: SchedulerStore,
    records: _AdmissionRecords,
    task: Task,
    due: float,
    *,
    shape: Shape,
    config: AdmissionConfig,
    release: bool,
    clock: Callable[[], float],
) -> Admission:
    adm = Admission(task_id=task.id, due=due, started=clock())
    lease = None
    try:
        lease = store.acquire_lease(task, units=1, backend=shape.backend, config=config)
    except AdmissionDenied:
        adm.raised = "AdmissionDenied"
    except Exception as exc:  # noqa: BLE001 -- recorded, never swallowed silently
        adm.raised = type(exc).__name__
    record = records.pop(task.id)
    if record is not None:
        adm.outcome = str(record.admission_outcome)
        adm.runs = int(record.admission_runs)
        adm.latency_ms = float(record.admission_latency_ms)
    if lease is not None:
        adm.lease_id = lease.lease_id
        if release:
            t0 = clock()
            try:
                store.release_lease(lease.lease_id, reason="bench_contention")
                adm.release_ms = (clock() - t0) * 1000.0
            except Exception as exc:  # noqa: BLE001
                adm.release_error = type(exc).__name__
    return adm


def run_rate(
    db: Any,
    tasks: Sequence[Task],
    *,
    rate_per_min: int,
    admitters: int,
    shape: Shape,
    config: AdmissionConfig | None = None,
    release: bool = True,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RateResult:
    """Offer `len(tasks)` admissions at `rate_per_min`, open loop."""
    if rate_per_min <= 0:
        raise ValueError("rate_per_min must be positive")
    if admitters <= 0:
        raise ValueError("admitters must be positive")
    config = config or AdmissionConfig()
    store = SchedulerStore(db)
    interval = 60.0 / rate_per_min
    result = RateResult(
        rate_per_min=rate_per_min,
        admitters=admitters,
        pools=len(shape.pools(tasks[0].tenant_id)) if tasks else 0,
        duration_s=len(tasks) * interval,
    )
    with _Capture() as records, ThreadPoolExecutor(max_workers=admitters) as pool:
        start = clock()
        futures = []
        for i, task in enumerate(tasks):
            due = start + i * interval
            wait = due - clock()
            if wait > 0:
                sleep(wait)
            futures.append(
                pool.submit(
                    _admit_one, store, records, task, due,
                    shape=shape, config=config, release=release, clock=clock,
                )
            )
        result.admissions = [f.result() for f in futures]
    return result


# ---------------------------------------------------------------------------
# Samples, rows and the table
# ---------------------------------------------------------------------------


def labels_for(result: RateResult) -> dict[str, str]:
    return {
        "rate_per_min": str(result.rate_per_min),
        "admitters": str(result.admitters),
        "pools": str(result.pools),
    }


def samples(result: RateResult) -> list[dict[str, Any]]:
    """benchstat samples. A number that was not taken is null with a reason."""
    labels = labels_for(result)
    out: list[dict[str, Any]] = []

    def put(metric: str, unit: str, value: float | None, reason: str = "") -> None:
        row: dict[str, Any] = {"metric": metric, "unit": unit, "value": value, "labels": labels}
        if value is None:
            row["not_measured"] = reason
        out.append(row)

    for a in result.admissions:
        if a.outcome is None:
            why = (
                f"acquire_lease wrote no admission record for {a.task_id}"
                + (f" (it raised {a.raised})" if a.raised else "")
            )
            put("contention.admission_latency", "ms", None, why)
            put("contention.reruns", "count", None, why)
            put("contention.aborted", "count", None, why)
        else:
            put("contention.admission_latency", "ms", a.latency_ms)
            put("contention.reruns", "count", float(max(0, (a.runs or 1) - 1)))
            put("contention.aborted", "count", 1.0 if a.outcome == "aborted" else 0.0)
        put("contention.arrival_lag", "ms", a.lag_ms)
        if a.lease_id is not None:
            if a.release_ms is not None:
                put("contention.release_latency", "ms", a.release_ms)
            elif a.release_error is not None:
                put("contention.release_latency", "ms", None,
                    f"release of {a.lease_id} raised {a.release_error}")
    achieved = achieved_rate(result)
    put("contention.achieved_rate_per_min", "per_min", achieved,
        "" if achieved is not None else "fewer than two admissions started")
    return out


def achieved_rate(result: RateResult) -> float | None:
    starts = sorted(a.started for a in result.admissions)
    if len(starts) < 2 or starts[-1] <= starts[0]:
        return None
    return (len(starts) - 1) / (starts[-1] - starts[0]) * 60.0


def row(result: RateResult) -> dict[str, Any]:
    """One table row. Percentiles come from benchstat: nearest rank, no mean."""
    adm = result.admissions
    measured = [a for a in adm if a.outcome is not None]
    latency = benchstat.summarize_values(
        [a.latency_ms for a in measured if a.latency_ms is not None],
        missing=len(adm) - len(measured), unit="ms",
    )
    reruns = [max(0, (a.runs or 1) - 1) for a in measured]
    rerun_summary = benchstat.summarize_values(reruns, missing=len(adm) - len(measured))
    lag = benchstat.summarize_values([a.lag_ms for a in adm], unit="ms")
    achieved = achieved_rate(result)
    out = {
        "rate_per_min": result.rate_per_min,
        "admitters": result.admitters,
        "pools": result.pools,
        "offered": len(adm),
        "n": latency["n"],
        "missing": latency["missing"],
        "latency_ms": {k: latency.get(k) for k in ("p50", "p95", "p99", "max")},
        "reruns": {
            "total": sum(reruns),
            "admissions_rerun": sum(1 for r in reruns if r > 0),
            "p50": rerun_summary.get("p50"),
            "p95": rerun_summary.get("p95"),
            "p99": rerun_summary.get("p99"),
            "max": rerun_summary.get("max"),
        },
        "outcomes": {
            k: sum(1 for a in measured if a.outcome == k)
            for k in ("leased", "denied", "aborted", "error")
        },
        "unrecorded": len(adm) - len(measured),
        "release_errors": sum(1 for a in adm if a.release_error),
        "lag_ms": {k: lag.get(k) for k in ("p50", "p99", "max")},
        "achieved_rate_per_min": achieved,
    }
    out["findings"] = assess(out)
    return out


def assess(r: dict[str, Any]) -> list[str]:
    """Facts about a row that change how it may be read. Not a verdict on sharding."""
    findings: list[str] = []
    achieved = r.get("achieved_rate_per_min")
    if achieved is None or achieved < OFFERED_RATE_FLOOR * r["rate_per_min"]:
        findings.append(
            "rate_not_offered: the harness did not sustain the offered rate, so this row "
            "measures the harness; rerun with more --admitters"
        )
    if r["outcomes"].get("aborted"):
        findings.append("aborted: admissions exhausted Firestore's retries (saturation)")
    if r["outcomes"].get("denied"):
        findings.append("denied: a pool bound, which this harness is set up never to do")
    if r["outcomes"].get("error") or r["unrecorded"]:
        findings.append("errors: some admissions failed or were not recorded; see samples")
    if r["release_errors"]:
        findings.append("release_errors: some leases could not be released")
    return findings


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def render(rows: Sequence[dict[str, Any]]) -> str:
    head = (
        f"{'rate/min':>8} {'adm':>4} {'pools':>5} {'n':>5} {'miss':>4} "
        f"{'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'reruns':>6} {'rerun%':>6} "
        f"{'rr p99':>6} {'abort':>5} {'lag p99':>8} {'achieved':>8}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        n = r["n"] or 0
        pct = (100.0 * r["reruns"]["admissions_rerun"] / n) if n else None
        lines.append(
            f"{r['rate_per_min']:>8} {r['admitters']:>4} {r['pools']:>5} {n:>5} "
            f"{r['missing']:>4} {_fmt(r['latency_ms']['p50']):>8} "
            f"{_fmt(r['latency_ms']['p95']):>8} {_fmt(r['latency_ms']['p99']):>8} "
            f"{r['reruns']['total']:>6} {_fmt(pct):>6} {_fmt(r['reruns']['p99']):>6} "
            f"{r['outcomes']['aborted']:>5} {_fmt(r['lag_ms']['p99']):>8} "
            f"{_fmt(r['achieved_rate_per_min']):>8}"
        )
        for f in r["findings"]:
            lines.append(f"{'':>8} ! {f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lock and cleanup
# ---------------------------------------------------------------------------


def take_lock(db: Any, run_id: str, *, break_lock: bool = False) -> None:
    ref = db.collection(LOCK_COLLECTION).document(LOCK_DOC)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def _take(txn: Any) -> None:
        # A real Transaction.get returns a generator; the snapshot helper the
        # frozen admission uses accepts either shape.
        snap = _snapshot(txn.get(ref))
        if snap.exists and not break_lock:
            held = snap.to_dict() or {}
            since = held.get("since")
            age = (now - since).total_seconds() if isinstance(since, datetime) else None
            if age is None or age < LOCK_STALE_SECONDS:
                raise Refused(
                    f"run {held.get('run_id')!r} holds the bench lock (since {since}); "
                    "two runs at once would count each other's writes as contention. "
                    "--break-lock if that run is dead"
                )
        txn.set(ref, {"run_id": run_id, "since": now})

    _take(db.transaction())


def release_lock(db: Any, run_id: str) -> None:
    """Delete the lock only if this run still holds it, read and delete in one
    transaction, so a --break-lock taken in between is not deleted."""
    ref = db.collection(LOCK_COLLECTION).document(LOCK_DOC)

    @firestore.transactional
    def _release(txn: Any) -> None:
        snap = _snapshot(txn.get(ref))
        if snap.exists and (snap.to_dict() or {}).get("run_id") == run_id:
            txn.delete(ref)

    _release(db.transaction())


def cleanup(db: Any, run_id: str, *, task_ids: Sequence[str], lease_ids: Sequence[str],
            pools: Sequence[str]) -> int:
    """Delete this run's documents, and only documents this run wrote."""
    refs = [db.collection("tasks").document(t) for t in task_ids]
    refs += [db.collection("leases").document(lease) for lease in lease_ids]
    refs += [db.collection("pools").document(p) for p in pools]
    deleted = 0
    batch, pending = db.batch(), 0
    for ref in refs:
        snap = ref.get()
        if not snap.exists:
            continue
        data = snap.to_dict() or {}
        # A lease document has no bench_run_id (the frozen admission writes it),
        # so it is matched on the run's own task ids instead.
        mine = data.get("bench_run_id") == run_id or (
            ref.path.startswith("leases/") and str(data.get("task_id", "")).startswith(f"bench-{run_id}-")
        )
        if not mine:
            continue
        batch.delete(ref)
        pending += 1
        deleted += 1
        if pending == 400:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    return deleted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_rates(text: str) -> list[int]:
    rates = [int(x) for x in text.split(",") if x.strip()]
    if not rates or any(r <= 0 for r in rates):
        raise argparse.ArgumentTypeError("--rates must be positive integers, comma-separated")
    return rates


def run(
    db: Any,
    *,
    rates: Sequence[int],
    duration_s: float,
    admitters: int,
    shape: Shape,
    release: bool = True,
    keep: bool = False,
    break_lock: bool = False,
    run_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[RateResult], list[dict[str, Any]]]:
    run_id = run_id or uuid.uuid4().hex[:10]
    take_lock(db, run_id, break_lock=break_lock)
    results: list[RateResult] = []
    task_ids: list[str] = []
    pools: list[str] = []
    try:
        pools = seed_pools(db, run_id, shape)
        for rate in rates:
            count = max(2, int(round(rate * duration_s / 60.0)))
            tasks = seed_tasks(db, run_id, shape, count, label=f"r{rate}")
            task_ids += [t.id for t in tasks]
            results.append(
                run_rate(db, tasks, rate_per_min=rate, admitters=admitters, shape=shape,
                         release=release, clock=clock, sleep=sleep)
            )
    finally:
        if not keep:
            lease_ids = [a.lease_id for r in results for a in r.admissions if a.lease_id]
            cleanup(db, run_id, task_ids=task_ids, lease_ids=lease_ids, pools=pools)
        release_lock(db, run_id)
    return results, [row(r) for r in results]


def main(argv: Sequence[str] | None = None, *, db_factory: Callable[[str, str], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_contention.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--live-database", default="swarm")
    parser.add_argument("--rates", type=parse_rates, default=[100, 200, 400])
    parser.add_argument("--duration", type=float, default=60.0,
                        help="seconds of offered load per rate (n = rate * duration / 60)")
    parser.add_argument("--admitters", type=int, default=8)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER,
                        help="'none' drops the two provider pools (five pools instead of seven)")
    parser.add_argument("--no-release", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--break-lock", action="store_true")
    parser.add_argument("--samples", help="write benchstat samples (JSONL) here")
    parser.add_argument("--report", help="write the table rows (JSON) here")
    args = parser.parse_args(argv)

    try:
        check_database(args.database, live=args.live_database)
    except Refused as exc:
        sys.stderr.write(f"bench_contention: refused: {exc}\n")
        return 2
    if args.admitters <= 0 or args.duration <= 0:
        parser.error("--admitters and --duration must be positive")

    shape = Shape(provider=None if args.provider in ("", "none") else args.provider)
    db = (db_factory or (lambda p, d: firestore.Client(project=p, database=d)))(
        args.project, args.database
    )
    try:
        results, rows = run(
            db, rates=args.rates, duration_s=args.duration, admitters=args.admitters,
            shape=shape, release=not args.no_release, keep=args.keep, break_lock=args.break_lock,
        )
    except Refused as exc:
        sys.stderr.write(f"bench_contention: refused: {exc}\n")
        return 2

    if args.samples:
        with open(args.samples, "a", encoding="utf-8") as fh:
            for r in results:
                for s in samples(r):
                    fh.write(json.dumps(s, sort_keys=True) + "\n")
    if args.report:
        Path(args.report).write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(render(rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
