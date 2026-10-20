"""A running attempt's CPU, memory and throttling, as a series (LIVE-UTIL-API, #1689).

The owner, 2026-10-20: a limit and a peak "will always show maximum when in
fact the pods do not run at max cpu the entire time" -- and throttling is to be
MEASURED, so 'starved' stops being an inference from a peak at the limit.

What is pinned:

  * every 5 s the sampler takes one point from the container's cgroup: cores
    used over the window, `memory.current`, and the share of the window's CFS
    periods that were throttled (`cpu.stat` `nr_throttled` / `nr_periods`);
  * the attempt's cumulative throttled seconds (`throttled_usec`), and a peak
    and a period-weighted mean throttled share, combined across runners;
  * None means not measured, never zero -- a cgroup with no throttling
    counters gives `thr` None, not 0.0;
  * each HEARTBEAT carries the points taken since the previous one, newest
    `HEARTBEAT_POINTS` of them, and the event stays under a size cap.

Imports are inside the tests so each one fails on its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

GIB = 1024**3


class ScriptedClock:
    def __init__(self, times: list[float]) -> None:
        self._times = list(times)
        self._last = self._times[0]

    def __call__(self) -> float:
        if self._times:
            self._last = self._times.pop(0)
        return self._last


def _cgroup(root: Path, *, usage_usec: int, periods: int | None = None, throttled: int | None = None,
            throttled_usec: int | None = None, memory: int | None = None) -> None:
    lines = [f"usage_usec {usage_usec}", "user_usec 0", "system_usec 0"]
    if periods is not None:
        lines += [f"nr_periods {periods}", f"nr_throttled {throttled}", f"throttled_usec {throttled_usec}"]
    (root / "cpu.stat").write_text("\n".join(lines) + "\n")
    if memory is None:
        (root / "memory.current").unlink(missing_ok=True)
    else:
        (root / "memory.current").write_text(f"{memory}\n")


def _sampler(clock, origin: float):
    from agent_worker.metrics import ResourceSampler, SystemCpuMeter

    return ResourceSampler(
        pid_provider=lambda: None,
        disk_provider=lambda: 0,
        memory_limit_bytes=8 * GIB,
        cpu_meter=SystemCpuMeter(lambda: None),
        clock=clock,
        series_origin=origin,
    )


@pytest.fixture
def cgroup(tmp_path: Path, monkeypatch) -> Path:
    from agent_worker import metrics

    root = tmp_path / "cgroup"
    root.mkdir()
    _cgroup(root, usage_usec=0, periods=0, throttled=0, throttled_usec=0, memory=GIB)
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    return root


def test_each_point_is_cores_memory_and_throttled_share_over_its_window(cgroup: Path):
    # MUTATION: divide nr_throttled by elapsed seconds instead of nr_periods in
    # `ResourceSampler.sample_point` -- the shares become 2.0 and 5.0 and this fails.
    sampler = _sampler(ScriptedClock([100.0, 105.0, 110.0]), origin=100.0)
    sampler.sample_point()  # the window's base: no point yet

    _cgroup(cgroup, usage_usec=5_000_000, periods=50, throttled=10, throttled_usec=400_000, memory=2 * GIB)
    sampler.sample_point()
    _cgroup(cgroup, usage_usec=15_000_000, periods=100, throttled=35, throttled_usec=1_400_000, memory=None)
    sampler.sample_point()

    points = sampler.drain_points()
    assert points == [
        {"t": 5.0, "cpu": 1.0, "mem": 2 * GIB, "thr": 0.2},
        # No memory.current and no pid to sum RSS over: not measured, never 0.
        {"t": 10.0, "cpu": 2.0, "mem": None, "thr": 0.5},
    ]
    assert sampler.drain_points() == [], "a drained point was served twice"


def test_the_attempt_keeps_throttled_seconds_and_a_peak_and_mean_share(cgroup: Path):
    # MUTATION: average the per-window shares in `combine_usage` instead of
    # weighting by periods -- the mean becomes 0.35 -> (0.2+0.5+0.1)/3 and fails.
    from agent_worker.metrics import combine_usage

    first = _sampler(ScriptedClock([0.0, 5.0, 10.0]), origin=0.0)
    first.sample_point()
    _cgroup(cgroup, usage_usec=5_000_000, periods=50, throttled=10, throttled_usec=400_000, memory=GIB)
    first.sample_point()
    _cgroup(cgroup, usage_usec=15_000_000, periods=100, throttled=35, throttled_usec=1_400_000, memory=GIB)
    first.sample_point()
    assert first.usage.throttled_seconds == pytest.approx(1.4)
    assert first.usage.peak_throttled_share == pytest.approx(0.5)
    assert first.usage.mean_throttled_share == pytest.approx(0.35)

    # A second runner, restarted in place: its sampler starts from the
    # counters as they stand, and the attempt's figures continue.
    second = _sampler(ScriptedClock([20.0, 25.0]), origin=0.0)
    second.sample_point()
    _cgroup(cgroup, usage_usec=16_000_000, periods=200, throttled=45, throttled_usec=1_900_000, memory=GIB)
    second.sample_point()
    assert second.drain_points()[0]["t"] == 25.0, "points of a later runner left the attempt's clock"

    attempt = combine_usage([first.usage, second.usage])
    assert attempt is not None
    assert attempt.throttled_seconds == pytest.approx(1.9)
    assert attempt.peak_throttled_share == pytest.approx(0.5)
    assert attempt.mean_throttled_share == pytest.approx(45 / 200)


def test_no_throttling_counters_is_not_measured_rather_than_zero(cgroup: Path):
    # MUTATION: default a missing `nr_periods` to 0 in `cgroup_cpu_throttling`
    # -- `thr` reads 0.0 and the attempt's figures 0.0, and this fails.
    from agent_worker.metrics import combine_usage

    _cgroup(cgroup, usage_usec=0, memory=GIB)
    sampler = _sampler(ScriptedClock([0.0, 5.0]), origin=0.0)
    sampler.sample_point()
    _cgroup(cgroup, usage_usec=2_500_000, memory=GIB)
    sampler.sample_point()

    assert sampler.drain_points() == [{"t": 5.0, "cpu": 0.5, "mem": GIB, "thr": None}]
    attempt = combine_usage([sampler.usage])
    assert attempt is not None
    assert attempt.throttled_seconds is None
    assert attempt.peak_throttled_share is None
    assert attempt.mean_throttled_share is None


def test_an_unthrottled_window_is_a_measured_zero(cgroup: Path):
    # No CFS period ran (no quota, or an idle window) yet the counters are
    # there: the container was not throttled, and that is a reading.
    sampler = _sampler(ScriptedClock([0.0, 5.0]), origin=0.0)
    sampler.sample_point()
    _cgroup(cgroup, usage_usec=1_000_000, periods=0, throttled=0, throttled_usec=0, memory=GIB)
    sampler.sample_point()

    assert sampler.drain_points() == [{"t": 5.0, "cpu": 0.2, "mem": GIB, "thr": 0.0}]
    assert sampler.usage.throttled_seconds == 0.0
    assert sampler.usage.mean_throttled_share == 0.0


def _fill(sampler, cgroup: Path, count: int) -> None:
    sampler.sample_point()
    for i in range(1, count + 1):
        _cgroup(cgroup, usage_usec=i * 3_000_000, periods=i * 50, throttled=i * 5,
                throttled_usec=i * 100_000, memory=3 * GIB + i)
        sampler.sample_point()


def test_a_heartbeat_carries_the_newest_points_and_stays_under_its_size_cap(
    db, worker_factory, cgroup: Path
):
    # MUTATION: drop the `[-HEARTBEAT_POINTS:]` cap in `Worker._usage_reading`
    # -- 200 points ride on one event, the size assertion fails.
    from agent_worker.lifecycle import HEARTBEAT_DETAIL_MAX_BYTES, HEARTBEAT_POINTS
    from worker_seeds import seed_attempt

    seed_attempt(db)
    worker, _, _ = worker_factory()
    clock = ScriptedClock([float(5 * i) for i in range(0, 201)])
    worker._sampler = _sampler(clock, origin=0.0)
    _fill(worker._sampler, cgroup, 200)

    detail = worker._usage_reading()

    series = detail["series"]
    assert len(series) == HEARTBEAT_POINTS
    assert [p["t"] for p in series] == [5.0 * i for i in range(201 - HEARTBEAT_POINTS, 201)]
    assert detail["series_clock"] >= series[-1]["t"]
    assert detail["throttled_seconds"] == pytest.approx(20.0)
    assert detail["peak_throttled_share"] == pytest.approx(0.1)
    assert detail["mean_throttled_share"] == pytest.approx(0.1)
    size = len(json.dumps(detail, default=str).encode())
    assert size < HEARTBEAT_DETAIL_MAX_BYTES, size
    assert HEARTBEAT_DETAIL_MAX_BYTES <= 4096

    # The next heartbeat carries only what was taken since.
    assert worker._usage_reading()["series"] == []


def test_a_reaped_runners_last_points_ride_on_the_next_heartbeat(db, worker_factory, cgroup: Path):
    # MUTATION: stop carrying `_pending_points` from `_stop_sampler` -- the
    # points a runner took between the last heartbeat and its reap vanish.
    from worker_seeds import seed_attempt

    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._sampler = _sampler(ScriptedClock([0.0, 5.0, 10.0, 12.0, 12.0]), origin=0.0)
    _fill(worker._sampler, cgroup, 2)
    worker._stop_sampler()

    assert [p["t"] for p in worker._usage_reading()["series"]] == [5.0, 10.0]
