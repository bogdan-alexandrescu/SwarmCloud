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
    usage, the resource log line, and the HEARTBEAT event series, which is the
    only time series the platform records;
  * that "not measured" stays None and is never written as zero.

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
