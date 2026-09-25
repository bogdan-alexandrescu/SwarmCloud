"""CPU is measured, not assumed (docs/web-ui/redesign-v2.md §6 S5).

`metrics.py` measured memory and disk and nothing else, so "requested vs used"
had no CPU row and could not have one: every resource class reserves whole
vCPUs with `requests == limits`, and nothing recorded whether an agent used a
tenth of one or all four.

What is pinned here:

  * the arithmetic -- cumulative CPU seconds in, total / mean / peak cores
    out -- against a scripted meter and clock, so it is exact;
  * the two readers against real inputs: a cgroup v2 `cpu.stat` and a real
    CPU-burning process tree;
  * that the numbers reach the places a reader can get them: the exported
    usage, the resource log line, the HEARTBEAT event series (the cumulative
    total, which is the only time series the platform records), and -- since
    contract request #15 was accepted -- the four typed fields on the attempt
    document, which Details draws;
  * that "not measured" stays None and is never written as zero;
  * that an attempt which restarts its runner in place reports ALL of its
    runners -- summed, maximised, one mean -- and not just the last one.

Imports are inside the tests so each one fails on its own.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import EventType

from conftest import seed_attempt


class ScriptedMeter:
    """Cumulative CPU seconds, handed out in order."""

    source = "scripted"

    def __init__(self, readings: list[float | None], consumed: float | None = None) -> None:
        self._readings = list(readings)
        self._consumed = consumed

    def cumulative(self) -> float | None:
        return self._readings.pop(0) if self._readings else None

    def consumed(self) -> float | None:
        return self._consumed


class ScriptedClock:
    def __init__(self, times: list[float]) -> None:
        self._times = list(times)
        self._last = self._times[0]

    def __call__(self) -> float:
        if self._times:
            self._last = self._times.pop(0)
        return self._last


def _sampler(meter, clock, interval: float = 2.0):
    from agent_worker.metrics import ResourceSampler

    return ResourceSampler(
        pid_provider=lambda: None,
        disk_provider=lambda: 0,
        memory_limit_bytes=0,
        interval_seconds=interval,
        cpu_meter=meter,
        clock=clock,
    )


def test_total_mean_and_peak_cores_from_cumulative_readings():
    # t=0: 10s, t=2: 13s (1.5 cores), t=4: 14s (0.5 cores), stop at t=5: 14s.
    sampler = _sampler(ScriptedMeter([10.0, 13.0, 14.0, 14.0]), ScriptedClock([0, 2, 4, 5]))
    sampler.sample_once()
    sampler.sample_once()
    sampler.sample_once()
    usage = sampler.stop()

    assert usage.cpu_seconds == pytest.approx(4.0)
    assert usage.peak_cpu_cores == pytest.approx(1.5)
    assert usage.mean_cpu_cores == pytest.approx(4.0 / 5)
    assert usage.cpu_source == "scripted"


def test_the_meters_exact_total_wins_over_the_live_readings_at_stop():
    """A live process-tree reading loses a child's time the moment it exits;
    the kernel's reaped-children total does not."""
    sampler = _sampler(ScriptedMeter([0.0, 1.0, 1.0], consumed=2.5), ScriptedClock([0, 2, 4]))
    sampler.sample_once()
    sampler.sample_once()
    usage = sampler.stop()

    assert usage.cpu_seconds == pytest.approx(2.5)
    assert usage.peak_cpu_cores == pytest.approx(0.5)


def test_a_too_short_interval_is_not_turned_into_a_peak():
    """0.1s of CPU over 0.01s of wall clock is "ten cores" only by arithmetic."""
    sampler = _sampler(ScriptedMeter([0.0, 0.1, 0.1]), ScriptedClock([0, 0.01, 0.02]))
    sampler.sample_once()
    sampler.sample_once()
    usage = sampler.stop()

    assert usage.peak_cpu_cores is None
    assert usage.cpu_seconds == pytest.approx(0.1)


def test_not_measured_is_none_not_zero():
    sampler = _sampler(ScriptedMeter([None, None, None]), ScriptedClock([0, 2, 4]))
    sampler.sample_once()
    sampler.sample_once()
    usage = sampler.stop()

    assert usage.cpu_seconds is None
    assert usage.peak_cpu_cores is None
    assert usage.mean_cpu_cores is None


def test_a_cgroup_v2_cpu_stat_is_read_in_seconds(tmp_path: Path, monkeypatch):
    from agent_worker import metrics

    (tmp_path / "cpu.stat").write_text(
        "usage_usec 12500000\nuser_usec 10000000\nsystem_usec 2500000\n"
        "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
    )
    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path)
    assert metrics.cgroup_cpu_seconds() == pytest.approx(12.5)

    (tmp_path / "cpu.stat").unlink()
    assert metrics.cgroup_cpu_seconds() is None


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs procfs")
def test_a_real_process_tree_is_measured_including_its_children(tmp_path: Path):
    """The runner starts the CLI in a NEW SESSION, so a process-GROUP reading
    would miss the agent entirely. The tree is walked by parent pid instead.

    Only the CHILD burns CPU, and it is still alive when the reading is taken,
    so the number can only come from walking into it."""
    from agent_worker.metrics import process_tree_cpu_seconds

    child = tmp_path / "child.py"
    child.write_text(
        "import hashlib, time\n"
        "digest = hashlib.sha256(b'swarm')\n"
        "end = time.monotonic() + 1.5\n"
        "while time.monotonic() < end:\n"
        "    digest.update(digest.digest())\n"
        "time.sleep(3)\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"kid = subprocess.Popen([sys.executable, {str(child)!r}], start_new_session=True)\n"
        "kid.wait()\n"
    )
    proc = subprocess.Popen([sys.executable, str(parent)])
    try:
        time.sleep(2.2)
        measured = process_tree_cpu_seconds(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    assert measured is not None
    assert measured >= 0.3, measured


def test_the_resource_log_line_carries_cpu():
    from agent_worker.metrics import LoggingMetricsExporter, ResourceUsage
    from agent_worker.logs import StructuredLogger

    stream = io.StringIO()
    usage = ResourceUsage(peak_rss_bytes=1, cpu_seconds=3.25, peak_cpu_cores=1.5,
                          mean_cpu_cores=0.8, cpu_source="cgroup")
    LoggingMetricsExporter(StructuredLogger(stream=stream)).export(usage, {"tenant_id": "eng"})

    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["message"] == "attempt resource usage"
    assert line["cpu_seconds"] == 3.25
    assert line["peak_cpu_cores"] == 1.5
    assert line["mean_cpu_cores"] == 0.8
    assert line["cpu_source"] == "cgroup"


def test_a_cpu_bound_attempt_reports_the_cpu_it_used(db, worker_factory):
    """End to end on the production worker, with the mock runner burning CPU
    for real. A lower bound only: on a shared CI host the cgroup reading may
    include other work, and the burn is wall-clock bounded, so it can only be
    trusted not to be SMALLER than a fraction of the burn."""
    seed_attempt(db, task_input={"prompt": "burn", "steps": 2, "sleep_seconds": 0.2,
                                 "cpu_burn_seconds": 1.5})
    worker, _, exporter = worker_factory()
    assert worker.run() == ExitCode.OK

    usage, _labels = exporter.exports[-1]
    assert usage.cpu_seconds is not None, "CPU was not measured at all"
    assert usage.cpu_seconds >= 0.2, usage
    assert usage.cpu_source in ("cgroup", "proc", "rusage")


def test_the_heartbeat_series_carries_cumulative_cpu(db, worker_factory):
    """HEARTBEAT is the one time series the platform keeps. A reader turns two
    consecutive cumulative readings into utilisation over that span, which a
    monotonic peak could never give it."""
    seed_attempt(db, task_input={"prompt": "burn", "steps": 4, "sleep_seconds": 4.5,
                                 "cpu_burn_seconds": 2.0})
    worker, _, _ = worker_factory(heartbeat_interval_seconds=1)
    assert worker.run() == ExitCode.OK

    beats = [e for e in db.events("task_1") if e["type"] == EventType.HEARTBEAT.value]
    assert beats, "no heartbeat events at all"
    assert all("cpu_seconds" in b["detail"] for b in beats), beats
    measured = [b["detail"]["cpu_seconds"] for b in beats if b["detail"]["cpu_seconds"] is not None]
    assert measured, f"no heartbeat carried a CPU reading: {beats}"
    assert max(measured) > 0


# ---------------------------------------------------------------------------
# one attempt, several runners
#
# An attempt restarts its runner in place after a short rate limit or a
# reloaded credential, and every runner got a sampler of its own. So the
# exported figures were the LAST runner's, the attempt document's memory peak
# was overwritten by whichever runner ended last, and the HEARTBEAT total --
# cumulative by design -- fell back to zero at every restart.
# ---------------------------------------------------------------------------


def test_runners_combine_as_totals_maxima_and_one_mean():
    from agent_worker.metrics import ResourceUsage, combine_usage

    first = ResourceUsage(
        peak_rss_bytes=900, peak_disk_bytes=10, oom_near_miss=True, samples=3,
        memory_events={"max": 1}, cpu_seconds=3.0, peak_cpu_cores=2.5,
        mean_cpu_cores=3.0, cpu_wall_seconds=1.0, cpu_source="cgroup",
    )
    second = ResourceUsage(
        peak_rss_bytes=100, peak_disk_bytes=50, samples=4,
        memory_events={"max": 2}, cpu_seconds=1.0, peak_cpu_cores=0.5,
        mean_cpu_cores=1.0 / 3.0, cpu_wall_seconds=3.0, cpu_source="cgroup",
    )

    usage = combine_usage([first, second])

    assert usage.cpu_seconds == pytest.approx(4.0)
    assert usage.peak_cpu_cores == pytest.approx(2.5)
    # Total over total: 4s of CPU across 4s of runner time. NOT the mean of the
    # two means (1.67), which would weigh a one-second run like a three-second one.
    assert usage.mean_cpu_cores == pytest.approx(1.0)
    assert usage.cpu_wall_seconds == pytest.approx(4.0)
    assert usage.peak_rss_bytes == 900
    assert usage.peak_disk_bytes == 50
    assert usage.oom_near_miss is True
    assert usage.samples == 7
    # cgroup counters are the container's own running totals: the latest is all of it.
    assert usage.memory_events == {"max": 2}
    assert usage.cpu_source == "cgroup"


def test_a_runner_too_short_for_a_mean_still_counts_in_the_total():
    from agent_worker.metrics import ResourceUsage, combine_usage

    short = ResourceUsage(cpu_seconds=0.4, cpu_wall_seconds=0.2, cpu_source="proc")
    long_ = ResourceUsage(
        cpu_seconds=1.0, mean_cpu_cores=1.0 / 3.0, cpu_wall_seconds=3.0, cpu_source="proc"
    )

    alone = combine_usage([short])
    assert alone.cpu_seconds == pytest.approx(0.4)
    assert alone.mean_cpu_cores is None, "0.4s over 0.2s is two cores only by arithmetic"

    both = combine_usage([short, long_])
    assert both.cpu_seconds == pytest.approx(1.4)
    assert both.mean_cpu_cores == pytest.approx(1.4 / 3.2)


def test_combining_keeps_not_measured_as_none():
    from agent_worker.metrics import ResourceUsage, combine_usage

    assert combine_usage([]) is None
    usage = combine_usage([ResourceUsage(peak_rss_bytes=5), ResourceUsage(peak_rss_bytes=7)])
    assert usage.peak_rss_bytes == 7
    assert usage.cpu_seconds is None
    assert usage.peak_cpu_cores is None
    assert usage.mean_cpu_cores is None
    assert usage.cpu_wall_seconds is None


def test_the_span_cpu_was_measured_across_is_kept_for_combining():
    sampler = _sampler(ScriptedMeter([10.0, 13.0, 14.0, 14.0]), ScriptedClock([0, 2, 4, 5]))
    sampler.sample_once()
    sampler.sample_once()
    sampler.sample_once()
    usage = sampler.stop()

    assert usage.cpu_wall_seconds == pytest.approx(5.0)


def test_an_attempt_that_restarts_its_runner_reports_every_runners_usage(
    db, worker_factory, monkeypatch
):
    """One attempt, two runners: a refused credential, reloaded in place.

    CPU comes from a scripted meter -- exactly 2.0s per runner -- and memory
    from a reading that is high for the first runner's process group and low
    for the second's, so every figure asserted below is exact. The old worker
    exported 2.0s, wrote the second runner's lower memory peak over the
    first's, and restarted its HEARTBEAT total from zero with the second runner.
    """
    from agent_worker import lifecycle, metrics

    class TwoSecondsPerRunner:
        source = "scripted"

        def __init__(self, _pid_provider) -> None:
            self._reads = 0

        def cumulative(self) -> float:
            self._reads += 1
            return 100.0 + 0.1 * self._reads

        def consumed(self) -> float:
            return 2.0

    high, low = 900 * 2**20, 100 * 2**20
    groups: list[int] = []

    def rss_of_group(pgid: int) -> int:
        if pgid not in groups:
            groups.append(pgid)
        return high if pgid == groups[0] else low

    monkeypatch.setattr(metrics, "SystemCpuMeter", TwoSecondsPerRunner)
    monkeypatch.setattr(metrics, "cgroup_memory_peak", lambda: None)
    monkeypatch.setattr(metrics, "cgroup_memory_current", lambda: None)
    monkeypatch.setattr(metrics, "process_group_rss", rss_of_group)
    # Every other heartbeat is an event, not every fifth, so the second runner
    # is certain to appear in the series. (`% 1 == 1` is never true, so 2 is
    # the densest setting there is.)
    monkeypatch.setattr(lifecycle, "HEARTBEAT_EVENT_EVERY", 2)

    seed_attempt(db, task_input={"prompt": "rotated once", "steps": 3, "sleep_seconds": 5.0,
                                 "credential_revoked_times": 1})
    worker, _, exporter = worker_factory(heartbeat_interval_seconds=1)

    assert worker.run() == ExitCode.OK
    assert len(groups) == 2, f"expected two runners to be sampled, saw {len(groups)}"

    usage, _labels = exporter.exports[-1]
    assert usage.cpu_seconds == pytest.approx(4.0), usage
    assert usage.peak_rss_bytes == high, usage
    assert db.doc("attempts/att_1")["peak_rss_bytes"] == high

    events = db.events("task_1")
    restart = max(i for i, e in enumerate(events) if e["type"] == EventType.RETRYING.value)
    after = [
        e["detail"]["cpu_seconds"]
        for e in events[restart:]
        if e["type"] == EventType.HEARTBEAT.value and e["detail"].get("cpu_seconds") is not None
    ]
    assert after, "no heartbeat carried CPU while the second runner ran"
    assert min(after) >= 2.0, f"the cumulative total went back below the first runner's: {after}"


# ---------------------------------------------------------------------------
# #184: peak AND mean of the limit, drawn in Details -- as typed fields
#
# The owner asked for CPU beside memory and workspace, as the peak and the mean
# cores of the runtime's CPU limit. #188 put the figures on the HEARTBEAT event,
# because the frozen `Attempt` had no CPU fields, and emitted a `final`
# reading when each runner was reaped.
#
# Contract request #15 was ACCEPTED on #184 (2026-09-25): `Attempt` gains
# `cpu_seconds`, `peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores`, and
# they REPLACE the interim path. So the worker writes them on its own attempt
# document -- with each periodic reading while a runner runs, and when each
# runner is reaped -- and the HEARTBEAT event goes back to what it carried
# before #188: the cumulative `cpu_seconds` and `cpu_source` the reconciler's
# stuck-browser judgement turns into a rate, and nothing else about CPU.
# ---------------------------------------------------------------------------

#: The four typed fields, as `swarm_common.models.Attempt` declares them.
CPU_FIELDS = ("cpu_seconds", "peak_cpu_cores", "mean_cpu_cores", "cpu_limit_cores")

#: What a HEARTBEAT event's detail carried before #188, and carries again.
HEARTBEAT_KEYS = {"elapsed_seconds", "peak_rss_bytes", "checkpoints", "cpu_seconds", "cpu_source"}


def _heartbeats(db):
    return [e for e in db.events("task_1") if e["type"] == EventType.HEARTBEAT.value]


def test_the_attempt_declares_the_four_fields_as_not_measured_by_default():
    """None, never 0: an attempt fenced before its runner started measured
    nothing, which an idle agent's 0.0 cores must not be confused with."""
    from dataclasses import fields

    from swarm_common.models import Attempt

    declared = {f.name: f.default for f in fields(Attempt)}
    for name in CPU_FIELDS:
        assert name in declared, f"Attempt does not declare {name} (contract request #15)"
        assert declared[name] is None, f"Attempt.{name} defaults to {declared[name]!r}, not None"


def test_the_mean_is_kept_current_while_the_runner_runs():
    """Set only at stop, a RUNNING attempt never had a mean -- and `combine_usage`
    gives an attempt one only when some runner has one."""
    from agent_worker.metrics import combine_usage

    # t=0: 10s, t=2: 13s, t=4: 14s -- and the runner is still alive.
    sampler = _sampler(ScriptedMeter([10.0, 13.0, 14.0]), ScriptedClock([0, 2, 4]))
    sampler.sample_once()
    sampler.sample_once()
    assert sampler.usage.mean_cpu_cores == pytest.approx(3.0 / 2)
    sampler.sample_once()

    assert sampler.usage.mean_cpu_cores == pytest.approx(4.0 / 4)
    assert combine_usage([sampler.usage]).mean_cpu_cores == pytest.approx(1.0)


def test_a_span_too_short_for_a_rate_gets_no_mean_while_running_either():
    sampler = _sampler(ScriptedMeter([0.0, 0.1]), ScriptedClock([0, 0.01]))
    sampler.sample_once()
    sampler.sample_once()

    assert sampler.usage.mean_cpu_cores is None


def test_the_cpu_limit_is_read_from_cgroup_cpu_max(tmp_path: Path, monkeypatch):
    from agent_worker import metrics

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path)
    assert metrics.cgroup_cpu_limit_cores() is None, "no cpu.max: not known"

    cases = {
        "200000 100000\n": 2.0,
        "150000 100000\n": 1.5,
        "max 100000\n": None,
        "0 100000\n": None,
        "garbage\n": None,
        "one two\n": None,
    }
    for text, expected in cases.items():
        (tmp_path / "cpu.max").write_text(text)
        assert metrics.cgroup_cpu_limit_cores() == expected, text


def test_the_attempt_cpu_fields_are_rounded_and_what_was_not_measured_is_left_out():
    """Left OUT, not written as null: the write is a merge, and an omitted key
    keeps what an earlier write put there, where a null would erase it."""
    from agent_worker.metrics import ResourceUsage, attempt_cpu_fields

    usage = ResourceUsage(
        cpu_seconds=1.23456, peak_cpu_cores=1.99999, mean_cpu_cores=0.5004,
        cpu_wall_seconds=2.46912, cpu_source="cgroup",
    )
    assert attempt_cpu_fields(usage, limit_cores=2.0) == {
        "cpu_seconds": 1.235,
        "peak_cpu_cores": 2.0,
        "mean_cpu_cores": 0.5,
        "cpu_limit_cores": 2.0,
    }
    # Too short for a rate: a total and a limit, and no peak or mean at all.
    short = ResourceUsage(cpu_seconds=0.1, cpu_source="proc")
    assert attempt_cpu_fields(short, limit_cores=None) == {"cpu_seconds": 0.1}
    # Nothing measured: nothing to write -- not even the limit, which alone
    # would read as a reading.
    assert attempt_cpu_fields(None, limit_cores=4.0) == {}
    assert attempt_cpu_fields(ResourceUsage(), limit_cores=4.0) == {}


def test_a_reaped_runner_writes_its_attempt_and_emits_no_event(
    db, worker_factory, monkeypatch, tmp_path: Path
):
    """#188 emitted a `final` HEARTBEAT the moment a runner was reaped; the
    typed fields replace it. The attempt's figures -- every runner combined --
    go on the attempt document, and nothing goes into the task's events."""
    from agent_worker import metrics
    from agent_worker.metrics import ResourceUsage

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "no-cgroup-here")
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._task = {"resource_class": "large"}  # sized by the task's class: 8 vCPU
    emitted: list = []
    monkeypatch.setattr(
        worker.control, "emit", lambda kind, detail=None, **_kw: emitted.append(kind)
    )

    class Reaped:
        usage = ResourceUsage(
            peak_rss_bytes=100, cpu_seconds=12.3456, peak_cpu_cores=1.23456,
            mean_cpu_cores=0.5, cpu_wall_seconds=24.0, cpu_source="cgroup",
        )

        def stop(self):
            return self.usage

    worker._sampler = Reaped()
    worker._stop_sampler()

    assert emitted == [], f"a reaped runner still emitted {emitted}: the interim path is back"
    doc = db.doc("attempts/att_1")
    assert doc["cpu_seconds"] == 12.346
    assert doc["peak_cpu_cores"] == 1.235
    assert doc["mean_cpu_cores"] == 0.5
    assert doc["cpu_limit_cores"] == 8.0
    assert doc["peak_rss_bytes"] == 100, "the memory peak is written beside it, as before"


def test_a_run_writes_the_attempts_own_figures_while_it_runs_and_at_the_end(
    db, worker_factory, monkeypatch
):
    """The live reading Details draws for a RUNNING attempt is the attempt
    document itself, rewritten with each periodic reading. The last write is
    the attempt's combined figures -- the same ones the export reports."""
    from agent_worker import lifecycle

    # Every other heartbeat is a reading, so a short run is sure to take one
    # while its runner is alive.
    monkeypatch.setattr(lifecycle, "HEARTBEAT_EVENT_EVERY", 2)
    seed_attempt(db, task_input={"prompt": "burn", "steps": 3, "sleep_seconds": 1.5,
                                 "cpu_burn_seconds": 0.5})
    worker, _, exporter = worker_factory(heartbeat_interval_seconds=1)

    calls: list[tuple[dict, bool]] = []
    real = worker.control.record_cpu_usage

    def spy(fields):
        calls.append((dict(fields), worker._sampler is not None))
        return real(fields)

    monkeypatch.setattr(worker.control, "record_cpu_usage", spy)
    assert worker.run() == ExitCode.OK

    assert any(live for _, live in calls), f"nothing was written while the runner ran: {calls}"
    assert not calls[-1][1], "the last write was not the one taken when the runner was reaped"
    usage, _labels = exporter.exports[-1]
    doc = db.doc("attempts/att_1")
    assert doc["cpu_seconds"] == round(usage.cpu_seconds, 3)
    for name in ("peak_cpu_cores", "mean_cpu_cores"):
        figure = getattr(usage, name)
        if figure is None:
            assert name not in doc, f"{name} was written though it was never measured"
        else:
            assert doc[name] == round(figure, 3), name


def test_the_heartbeat_event_carries_only_what_it_carried_before_the_interim_path(
    db, worker_factory
):
    """The reconciler's stuck-browser judgement (`reconciler.progress`) reads
    `cpu_seconds` off consecutive HEARTBEAT events, so that key stays -- and
    its meaning, the attempt's cumulative total. Every key #188 added for the
    Details rows is gone from the event, and so is the `final` reading."""
    seed_attempt(db, task_input={"prompt": "burn", "steps": 2, "sleep_seconds": 1.5,
                                 "cpu_burn_seconds": 0.5})
    worker, _, _ = worker_factory(heartbeat_interval_seconds=1)
    assert worker.run() == ExitCode.OK

    beats = _heartbeats(db)
    assert beats, "no heartbeat events at all"
    for beat in beats:
        assert set(beat["detail"]) == HEARTBEAT_KEYS, beat["detail"]


def test_the_cpu_limit_falls_back_to_the_class_the_container_was_sized_with(
    db, worker_factory, monkeypatch, tmp_path: Path
):
    """With no cgroup `cpu.max`, the limit is the catalogue cpu of the class the
    scheduler SIZED the container with -- the task's own class -- not the
    profile's, which is all `WorkerConfig.resource_class` knows."""
    from agent_worker import metrics

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "no-cgroup-here")
    seed_attempt(db, task_input={"prompt": "sized", "steps": 1, "sleep_seconds": 0.3})
    db.doc("tasks/task_1")["resource_class"] = "large"  # mock's own class is standard
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK

    assert db.doc("attempts/att_1")["cpu_limit_cores"] == 8.0


def test_the_cpu_limit_comes_from_cgroup_when_there_is_one(db, worker_factory, monkeypatch, tmp_path):
    from agent_worker import metrics

    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cpu.max").write_text("300000 100000\n")
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    seed_attempt(db, task_input={"prompt": "cgroup", "steps": 1, "sleep_seconds": 0.3})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK

    assert db.doc("attempts/att_1")["cpu_limit_cores"] == 3.0


def test_the_sized_class_is_the_one_the_scheduler_sizes_the_container_with(worker_factory):
    """Restated from `scheduler.dispatch.resource_class_for` (the worker image
    does not install the scheduler), so the two are compared here."""
    from types import SimpleNamespace

    from scheduler.dispatch import resource_class_for
    from swarm_common.profiles import RUNNER_PROFILES

    worker, _, _ = worker_factory()
    profile = RUNNER_PROFILES["mock"]
    for named in ("standard", "large", "browser", "a-class-no-longer-in-the-catalogue"):
        worker._task = {"resource_class": named}
        task = SimpleNamespace(id="task_1", resource_class=named)
        assert worker._sized_resource_class() == resource_class_for(task, profile), named


def test_an_attempt_that_must_write_nothing_writes_no_cpu(db, worker_factory):
    """The tenant-mismatch exit is the one path that writes NOTHING, not even
    onto what may be another tenant's attempt document."""
    from agent_worker.metrics import ResourceUsage

    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._runner_usage.append(ResourceUsage(cpu_seconds=5.0, cpu_source="cgroup"))
    worker._writes_forbidden = True
    worker._record_cpu()
    assert all(name not in db.doc("attempts/att_1") for name in CPU_FIELDS)

    worker._writes_forbidden = False
    worker._record_cpu()
    assert db.doc("attempts/att_1")["cpu_seconds"] == 5.0
