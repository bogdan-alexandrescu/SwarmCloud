"""The admission contention harness, offline.

`scripts/bench_contention.py` is what an operator runs against a dedicated
bench Firestore database to answer spec §8 row 6 ("Firestore contention at 100
admissions/min?") before anyone designs sharding. A SwarmCloud agent has no
credentials to run it, so this file proves the parts that decide whether its
numbers can be trusted:

  * it refuses every database that is not a bench database, before it builds
    a client -- it overwrites pool documents named `global`;
  * it drives the REAL `SchedulerStore.acquire_lease`, which calls the frozen
    `acquire_lease_in_transaction`, and reads the re-run count from the record
    that method logs rather than counting on its own;
  * a re-run forced by a concurrent commit shows up as a re-run, and a
    retry-exhausted admission as an abort;
  * an admission whose record never arrived is NOT MEASURED, not fast;
  * cleanup deletes this run's documents and nothing else.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from control_plane.fakes import FakeFirestore, FakeTransaction
from control_plane.test_request_cancel_is_transactional import ContendedFirestore

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "scripts" / "bench_contention.py"
WRAPPER = ROOT / "scripts" / "bench-contention.sh"


def _load():
    spec = importlib.util.spec_from_file_location("bench_contention", HARNESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses resolve annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bc = _load()


class FakeClock:
    """Sleeping advances time; nothing else does. Admissions take zero time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run_one(db: Any, *, rate: int = 120, count: int = 6, release: bool = True, run_id: str = "r1"):
    shape = bc.Shape()
    bc.seed_pools(db, run_id, shape)
    tasks = bc.seed_tasks(db, run_id, shape, count, label="x")
    clock = FakeClock()
    result = bc.run_rate(db, tasks, rate_per_min=rate, admitters=1, shape=shape,
                         release=release, clock=clock, sleep=clock.sleep)
    return shape, tasks, result


def _by_metric(samples: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    return [s for s in samples if s["metric"] == metric]


# --------------------------------------------------------------------------
# 1. It refuses anything live
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["swarm", "(default)", "prod-swarm", "bench", "swarm-bench_x", "SWARM-BENCH"])
def test_a_database_that_is_not_a_bench_database_is_refused(name: str) -> None:
    with pytest.raises(bc.Refused):
        bc.check_database(name, live="swarm")


def test_the_environments_live_database_is_refused_even_with_a_bench_name() -> None:
    with pytest.raises(bc.Refused):
        bc.check_database("swarm-bench", live="swarm-bench")


@pytest.mark.parametrize("name", ["swarm-bench", "swarm-bench-dev", "swarm-bench-b17-2"])
def test_a_bench_database_is_accepted(name: str) -> None:
    bc.check_database(name, live="swarm")


def test_main_refuses_before_it_builds_a_client() -> None:
    built: list[tuple[str, str]] = []
    rc = bc.main(["--project", "p", "--database", "swarm"],
                 db_factory=lambda p, d: built.append((p, d)))
    assert rc == 2
    assert built == [], "a client was built for the live database"


def test_the_wrapper_refuses_the_live_database_without_credentials(tmp_path) -> None:
    """The shell guard runs before python, gcloud or any client."""
    proc = subprocess.run(
        ["bash", str(WRAPPER), "--database", "swarm"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SWARM_ENV_FILE": str(tmp_path / "none")},
    )
    assert proc.returncode != 0
    assert "bench database" in (proc.stdout + proc.stderr)


# --------------------------------------------------------------------------
# 2. It runs the real admission, and reads production's own record
# --------------------------------------------------------------------------

def test_every_offered_admission_runs_the_frozen_transaction_and_is_measured() -> None:
    db = FakeFirestore()
    shape, tasks, result = _run_one(db)

    assert len(result.admissions) == len(tasks) == 6
    assert result.pools == 7, "claude-code with a provider reserves seven pools"
    # The frozen function ran: every task moved READY -> LEASED at generation 1
    # and a lease document exists for it.
    for task, adm in zip(tasks, result.admissions):
        assert adm.outcome == "leased"
        assert adm.runs == 1
        doc = db.docs[f"tasks/{task.id}"]
        assert doc["state"] == "LEASED"
        assert doc["current_generation"] == 1
        assert db.docs[f"leases/{adm.lease_id}"]["released_at"] is not None
    # Released through the frozen release: every pool is back to zero.
    for name in shape.pools(tasks[0].tenant_id):
        assert db.docs[f"pools/{name}"]["active"] == 0, name

    samples = bc.samples(result)
    latency = _by_metric(samples, "contention.admission_latency")
    assert len(latency) == 6 and all(isinstance(s["value"], float) for s in latency)
    assert [s["value"] for s in _by_metric(samples, "contention.reruns")] == [0.0] * 6
    assert [s["value"] for s in _by_metric(samples, "contention.aborted")] == [0.0] * 6
    assert latency[0]["labels"] == {"rate_per_min": "120", "admitters": "1", "pools": "7"}


def test_the_open_loop_schedule_offers_the_stated_rate() -> None:
    db = FakeFirestore()
    _, _, result = _run_one(db, rate=120, count=6)
    # Arrivals 0.5 s apart; with zero-time admissions nothing lags.
    assert [a.due - result.admissions[0].due for a in result.admissions] == pytest.approx(
        [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    )
    assert bc.achieved_rate(result) == pytest.approx(120.0)
    r = bc.row(result)
    assert r["n"] == 6 and r["offered"] == 6
    assert not any(f.startswith("rate_not_offered") for f in r["findings"])


def test_a_concurrent_commit_to_global_is_a_rerun_in_the_samples() -> None:
    db = ContendedFirestore()
    shape = bc.Shape()
    bc.seed_pools(db, "r1", shape)
    tasks = bc.seed_tasks(db, "r1", shape, 3, label="x")
    # Another admitter takes a slot on `global` between the first admission's
    # read and its commit: the commit aborts and the body re-runs.
    other = bc.seed_tasks(db, "r1", shape, 1, label="other")[0]
    from scheduler.store import SchedulerStore
    from swarm_common.admission import AdmissionConfig

    db.interleave("pools/global", lambda: SchedulerStore(db).acquire_lease(
        other, units=1, backend=shape.backend, config=AdmissionConfig()))
    clock = FakeClock()
    result = bc.run_rate(db, tasks, rate_per_min=60, admitters=1, shape=shape,
                         clock=clock, sleep=clock.sleep)

    assert db.aborts == ["pools/global"], "nothing was contended; the test raced nothing"
    reruns = [s["value"] for s in _by_metric(bc.samples(result), "contention.reruns")]
    assert reruns == [1.0, 0.0, 0.0]
    r = bc.row(result)
    assert r["reruns"]["total"] == 1
    assert r["reruns"]["admissions_rerun"] == 1


class _AlwaysAborts(FakeTransaction):
    def _commit(self) -> list[Any]:
        self._buffer = []
        raise gexc.Aborted("contended")


class _Saturated(FakeFirestore):
    """Every admission commit aborts; batches and the lock still work."""

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _AlwaysAborts(self)


def test_an_admission_that_exhausts_its_retries_is_an_abort_not_a_fast_one() -> None:
    db = _Saturated()
    _, _, result = _run_one(db, count=3)

    assert [a.outcome for a in result.admissions] == ["aborted"] * 3
    assert [a.runs for a in result.admissions] == [5] * 3
    samples = bc.samples(result)
    assert [s["value"] for s in _by_metric(samples, "contention.aborted")] == [1.0] * 3
    assert [s["value"] for s in _by_metric(samples, "contention.reruns")] == [4.0] * 3
    r = bc.row(result)
    assert r["outcomes"]["aborted"] == 3
    assert any(f.startswith("aborted") for f in r["findings"])


class _ReadAborts(FakeTransaction):
    def get(self, ref: Any, **kwargs: Any) -> Any:
        if getattr(ref, "path", "").startswith("pools/"):
            raise gexc.Aborted("contended read")
        return super().get(ref, **kwargs)


class _ReadsAbort(FakeFirestore):
    """Every transactional read of a pool document returns ABORTED, which
    `firestore.transactional` does not retry: it reaches the caller raw."""

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return _ReadAborts(self)


def test_an_aborted_read_is_an_abort_in_the_samples_not_an_error() -> None:
    db = _ReadsAbort()
    _, _, result = _run_one(db, count=1)

    (adm,) = result.admissions
    assert adm.outcome == "aborted"
    assert adm.raised == "Aborted"
    samples = bc.samples(result)
    assert [s["value"] for s in _by_metric(samples, "contention.aborted")] == [1.0]
    r = bc.row(result)
    assert r["outcomes"]["aborted"] == 1
    assert r["outcomes"]["error"] == 0
    assert any(f.startswith("aborted") for f in r["findings"])


def test_release_lock_leaves_another_runs_lock_alone() -> None:
    db = FakeFirestore()
    bc.take_lock(db, "mine")
    bc.release_lock(db, "theirs")
    assert db.docs[f"{bc.LOCK_COLLECTION}/{bc.LOCK_DOC}"]["run_id"] == "mine"
    bc.release_lock(db, "mine")
    assert f"{bc.LOCK_COLLECTION}/{bc.LOCK_DOC}" not in db.docs


def test_an_admission_with_no_record_is_not_measured(monkeypatch) -> None:
    """If acquire_lease stopped logging, the harness must go red, not fast."""
    monkeypatch.setattr(logging.getLogger(bc.STORE_LOGGER), "disabled", True)
    db = FakeFirestore()
    _, _, result = _run_one(db, count=3)

    latency = _by_metric(bc.samples(result), "contention.admission_latency")
    assert [s["value"] for s in latency] == [None] * 3
    assert all("no admission record" in s["not_measured"] for s in latency)
    r = bc.row(result)
    assert r["n"] == 0 and r["missing"] == 3
    assert any(f.startswith("errors") for f in r["findings"])


def test_a_row_the_harness_could_not_offer_says_so() -> None:
    r = {
        "rate_per_min": 400, "achieved_rate_per_min": 250.0,
        "outcomes": {"leased": 10, "denied": 0, "aborted": 0, "error": 0},
        "unrecorded": 0, "release_errors": 0,
    }
    assert any(f.startswith("rate_not_offered") for f in bc.assess(r))
    r["achieved_rate_per_min"] = 399.0
    assert bc.assess(r) == []


def test_the_table_states_n() -> None:
    db = FakeFirestore()
    _, _, result = _run_one(db, count=4)
    text = bc.render([bc.row(result)])
    header, _, line = text.splitlines()[:3]
    assert " n " in f" {header} "
    assert line.split()[3] == "4"


# --------------------------------------------------------------------------
# 3. It cleans up after itself, and only after itself
# --------------------------------------------------------------------------

def test_run_deletes_its_own_documents_and_nothing_else() -> None:
    db = FakeFirestore()
    db.docs["tasks/someone-elses"] = {"id": "someone-elses", "state": "READY"}
    db.docs["pools/runner:mock"] = {"name": "runner:mock", "hard_limit": 3, "active": 1}
    clock = FakeClock()

    results, rows = bc.run(db, rates=[60, 120], duration_s=3, admitters=1, shape=bc.Shape(),
                           run_id="r9", clock=clock, sleep=clock.sleep)

    assert [r["rate_per_min"] for r in rows] == [60, 120]
    assert [r["offered"] for r in rows] == [3, 6]
    leftover = [p for p in db.docs if p.startswith(("tasks/bench-r9", "leases/", "pools/"))
                and p != "pools/runner:mock"]
    assert leftover == [], f"the run left documents behind: {leftover}"
    assert "tasks/someone-elses" in db.docs
    assert db.docs["pools/runner:mock"]["active"] == 1
    assert not any(p.startswith("bench_lock/") for p in db.docs), "the lock was not released"


def test_a_second_run_is_refused_while_the_first_holds_the_lock() -> None:
    db = FakeFirestore()
    bc.take_lock(db, "first")
    with pytest.raises(bc.Refused):
        bc.take_lock(db, "second")
    bc.take_lock(db, "second", break_lock=True)
    bc.release_lock(db, "first")  # not first's any more: must not delete second's
    assert db.docs["bench_lock/contention"]["run_id"] == "second"


def test_the_harness_builds_no_client_at_import() -> None:
    """Importing it must not need credentials (CLAUDE.md, Writing Python)."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import sys, importlib.util as u; s=u.spec_from_file_location('m', {str(HARNESS)!r}); "
         "m=u.module_from_spec(s); sys.modules['m']=m; s.loader.exec_module(m); print('ok')"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent"},
    )
    assert proc.stdout.strip() == "ok", proc.stderr
