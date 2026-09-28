"""The attempt's CPU reading is dated and its limit sourced (contract request #26).

The owner ACCEPTED request #26 on #184 (2026-09-26): typed
`Attempt.cpu_measured_at` and `Attempt.cpu_limit_source`, written by the
worker with the CPU figures. Request #15's four fields carried neither, so
Details said `live reading · age not recorded` -- a worker that stopped
writing an hour ago looked exactly like one that wrote a second ago -- and
`reported limit` for any limit that was not its class's.

What is pinned:

  * the source goes beside a limit, and only as `cgroup` or `resource_class`;
  * every write that carries a figure stamps `cpu_measured_at`, and nothing
    is written, not even a time, when nothing was measured;
  * the limit's source is the kernel's when `cpu.max` said one, and the
    class's otherwise;
  * the PERIODIC reading is written even when the figures did not move, so an
    idle agent's reading does not age as a stall; the reap and exit writes
    still skip an unchanged reading.

MUTATIONS: drop the source from `attempt_cpu_fields`; accept any string as a
source; stamp the time in `attempt_cpu_fields` (then an unchanged periodic
reading never compares equal and every exit rewrites); write a time with no
figure; skip an unchanged periodic reading.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from conftest import seed_attempt


def test_the_source_goes_beside_a_limit_and_only_as_a_known_word():
    from agent_worker.metrics import ResourceUsage, attempt_cpu_fields

    usage = ResourceUsage(cpu_seconds=12.0, peak_cpu_cores=1.5, mean_cpu_cores=0.5, cpu_source="cgroup")
    assert attempt_cpu_fields(usage, limit_cores=2.0, limit_source="cgroup")["cpu_limit_source"] == "cgroup"
    assert (
        attempt_cpu_fields(usage, limit_cores=8.0, limit_source="resource_class")["cpu_limit_source"]
        == "resource_class"
    )
    # A source with no limit says nothing, and a word outside the vocabulary
    # is not written.
    assert "cpu_limit_source" not in attempt_cpu_fields(usage, limit_cores=None, limit_source="cgroup")
    assert "cpu_limit_source" not in attempt_cpu_fields(usage, limit_cores=2.0, limit_source="guess")
    # Nothing measured: nothing at all.
    assert attempt_cpu_fields(None, limit_cores=2.0, limit_source="cgroup") == {}


def test_every_write_with_a_figure_is_dated_and_a_write_without_one_writes_nothing(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()

    worker.control.record_cpu_usage({"cpu_limit_cores": 2.0, "cpu_limit_source": "cgroup"})
    assert "cpu_measured_at" not in db.documents.get("attempts/att_1", {}) or (
        db.doc("attempts/att_1").get("cpu_measured_at") is None
    ), "a limit with no figure was dated as a reading"

    worker.control.record_cpu_usage(
        {"cpu_seconds": 4.0, "peak_cpu_cores": 1.0, "cpu_limit_cores": 2.0, "cpu_limit_source": "cgroup"}
    )
    doc = db.doc("attempts/att_1")
    assert isinstance(doc["cpu_measured_at"], datetime), doc
    assert doc["cpu_limit_source"] == "cgroup"
    assert doc["cpu_seconds"] == 4.0


def test_a_source_is_never_written_without_its_limit(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker.control.record_cpu_usage({"cpu_seconds": 4.0, "cpu_limit_source": "cgroup"})
    assert "cpu_limit_source" not in db.doc("attempts/att_1")


def test_a_reaped_runners_limit_is_the_classs_when_the_kernel_says_none(
    db, worker_factory, monkeypatch, tmp_path: Path
):
    from agent_worker import metrics
    from agent_worker.metrics import ResourceUsage

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "no-cgroup-here")
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._task = {"resource_class": "large"}

    class Reaped:
        usage = ResourceUsage(cpu_seconds=6.0, peak_cpu_cores=1.0, mean_cpu_cores=0.5,
                              cpu_wall_seconds=12.0, cpu_source="proc")

        def stop(self):
            return self.usage

    worker._sampler = Reaped()
    worker._stop_sampler()

    doc = db.doc("attempts/att_1")
    assert doc["cpu_limit_cores"] == 8.0
    assert doc["cpu_limit_source"] == "resource_class"
    assert isinstance(doc["cpu_measured_at"], datetime)


def test_a_limit_from_cpu_max_is_the_kernels(db, worker_factory, monkeypatch, tmp_path: Path):
    from agent_worker import metrics
    from agent_worker.metrics import ResourceUsage

    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cpu.max").write_text("300000 100000\n")
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._runner_usage.append(ResourceUsage(cpu_seconds=5.0, cpu_source="cgroup"))

    worker._record_cpu()

    doc = db.doc("attempts/att_1")
    assert doc["cpu_limit_cores"] == 3.0
    assert doc["cpu_limit_source"] == "cgroup"


def test_the_periodic_reading_is_written_even_when_the_figures_did_not_move(
    db, worker_factory, monkeypatch, tmp_path: Path
):
    """Each write dates the reading; an idle agent's unchanged figures, skipped,
    would read as a worker that stopped writing. The reap and exit writes keep
    skipping an unchanged reading: they add no time a reader needs."""
    from agent_worker import metrics
    from agent_worker.metrics import ResourceUsage

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "no-cgroup-here")
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._task = {"resource_class": "standard"}
    worker._runner_usage.append(ResourceUsage(cpu_seconds=5.0, cpu_source="proc"))

    writes: list[dict] = []
    real = worker.control.record_cpu_usage

    def spy(fields):
        writes.append(dict(fields))
        return real(fields)

    monkeypatch.setattr(worker.control, "record_cpu_usage", spy)

    worker._record_cpu(periodic=True)
    worker._record_cpu(periodic=True)
    assert len(writes) == 2, "an unchanged periodic reading was not written, so its age would grow"

    worker._record_cpu()
    worker._record_cpu()
    assert len(writes) == 2, "an unchanged reap or exit reading was written again"
