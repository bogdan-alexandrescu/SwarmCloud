"""The memory guard and the recorded peak read the cgroup's working set, not its page cache.

The load analysis of 2026-11-06 counted 4,907 guard kills in seven days on 512
pods, and the victim's median share of its pod's memory was 7%: the guard
judged `memory.current`, which counts the clean page cache the kernel drops
under pressure long before its OOM killer runs. A tree that read a repository
once looked full.

What is pinned here:

  * a sample whose `memory.current` is high only because of `inactive_file`
    kills nothing;
  * the same sample with that memory as anon, or as shmem (the tmpfs
    workspace, which the kernel cannot drop), kills;
  * the victim is still the largest process by its own resident memory;
  * the guard still acts at 95% of `memory.max`, before the OOM killer;
  * the peak an attempt records for sizing (`peak_rss_bytes`) excludes the
    reclaimable cache, and the raw cgroup high-water rides beside it.

No real process is signalled. Imports are inside the tests so each one fails
on its own.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

GIB = 1024**3
MIB = 1024**2
LIMIT = 8 * GIB

RUNNER, CLI, BASH, PYTEST, NODE = 1000, 1001, 1002, 1003, 1004


class NoCpu:
    source = "scripted"

    def cumulative(self) -> float | None:
        return None

    def consumed(self) -> float | None:
        return None


class Kills:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))


def _tree():
    from agent_worker.metrics import ProcessInfo

    worker = os.getpid()
    return [
        ProcessInfo(pid=worker, ppid=1, pgid=worker, rss_bytes=300 * MIB, name="python"),
        ProcessInfo(pid=RUNNER, ppid=worker, pgid=RUNNER, rss_bytes=80 * MIB, name="python"),
        ProcessInfo(pid=CLI, ppid=RUNNER, pgid=RUNNER, rss_bytes=400 * MIB, name="claude"),
        ProcessInfo(pid=BASH, ppid=CLI, pgid=RUNNER, rss_bytes=4 * MIB, name="bash"),
        # The smaller process by resident memory, though the cache it read is
        # what fills the cgroup: the victim is still chosen by its own RSS.
        ProcessInfo(pid=PYTEST, ppid=BASH, pgid=RUNNER, rss_bytes=2 * GIB, name="pytest"),
        ProcessInfo(pid=NODE, ppid=BASH, pgid=RUNNER, rss_bytes=1 * GIB, name="git"),
    ]


def _stat(*, anon: int = 0, shmem: int = 0, inactive_file: int = 0, active_file: int = 0) -> dict[str, int]:
    """A cgroup v2 `memory.stat`: shmem is swap-backed and sits on the anon LRU lists."""
    return {
        "anon": anon,
        "file": shmem + inactive_file + active_file,
        "shmem": shmem,
        "inactive_anon": anon // 2 + shmem,
        "active_anon": anon - anon // 2,
        "inactive_file": inactive_file,
        "active_file": active_file,
    }


def _sampler(*, current: int, stat: dict[str, int], kill):
    from agent_worker.metrics import ResourceSampler

    return ResourceSampler(
        pid_provider=lambda: RUNNER,
        disk_provider=lambda: 0,
        memory_limit_bytes=LIMIT,
        interval_seconds=2.0,
        cpu_meter=NoCpu(),
        clock=lambda: 100.0,
        memory_current_reader=lambda: current,
        memory_stat_reader=lambda: dict(stat),
        memory_max_reader=lambda: LIMIT,
        process_table=_tree,
        kill=kill,
    )


BASE = 3 * GIB  # anon the agent really holds
HIGH = int(LIMIT * 0.97)  # memory.current, over the 95% guard line
EXTRA = HIGH - BASE


def test_a_cgroup_full_only_of_inactive_file_cache_kills_nothing():
    kills = Kills()
    sampler = _sampler(current=HIGH, stat=_stat(anon=BASE, inactive_file=EXTRA), kill=kills)

    sampler.sample_once()

    assert kills.sent == []
    assert sampler.usage.memory_guard_kills == 0


def test_the_same_reading_held_as_anon_kills_the_largest_process_by_its_own_rss():
    kills = Kills()
    sampler = _sampler(current=HIGH, stat=_stat(anon=HIGH), kill=kills)

    sampler.sample_once()

    assert kills.sent == [(PYTEST, signal.SIGKILL)]


def test_the_same_reading_held_as_shmem_in_the_tmpfs_workspace_kills():
    kills = Kills()
    sampler = _sampler(current=HIGH, stat=_stat(anon=BASE, shmem=EXTRA), kill=kills)

    sampler.sample_once()

    assert kills.sent == [(PYTEST, signal.SIGKILL)]


def test_active_file_stays_counted_so_the_guard_still_acts_before_the_oom_killer():
    kills = Kills()
    sampler = _sampler(current=HIGH, stat=_stat(anon=BASE, active_file=EXTRA), kill=kills)

    sampler.sample_once()

    assert kills.sent == [(PYTEST, signal.SIGKILL)]


def test_the_guard_line_is_still_95_percent_of_memory_max_on_the_working_set():
    cache = 1 * GIB
    above, below = Kills(), Kills()
    _sampler(
        current=int(LIMIT * 0.96) + cache, stat=_stat(anon=int(LIMIT * 0.96), inactive_file=cache), kill=above
    ).sample_once()
    _sampler(
        current=int(LIMIT * 0.94) + cache, stat=_stat(anon=int(LIMIT * 0.94), inactive_file=cache), kill=below
    ).sample_once()

    assert above.sent == [(PYTEST, signal.SIGKILL)]
    assert below.sent == []


def test_an_unreadable_memory_stat_falls_back_to_memory_current():
    kills = Kills()
    sampler = _sampler(current=HIGH, stat={}, kill=kills)

    sampler.sample_once()

    assert kills.sent == [(PYTEST, signal.SIGKILL)]


def test_a_kill_records_the_working_set_beside_the_raw_reading():
    kills = Kills()
    cache = 256 * MIB
    sampler = _sampler(current=HIGH + cache, stat=_stat(anon=HIGH, inactive_file=cache), kill=kills)

    sampler.sample_once()

    (kill,) = sampler.drain_guard_kills()
    assert kill["memory_current_bytes"] == HIGH + cache
    assert kill["working_set_bytes"] == HIGH


def test_the_working_set_is_memory_current_less_inactive_file_and_never_negative():
    from agent_worker.metrics import working_set_bytes

    assert working_set_bytes(5 * GIB, {"inactive_file": 2 * GIB, "active_file": GIB, "shmem": GIB}) == 3 * GIB
    assert working_set_bytes(GIB, {"inactive_file": 2 * GIB}) == 0
    assert working_set_bytes(GIB, {}) == GIB
    assert working_set_bytes(None, {"inactive_file": GIB}) is None


def test_memory_stat_is_parsed_from_the_cgroup(monkeypatch, tmp_path: Path):
    from agent_worker import metrics

    (tmp_path / "memory.stat").write_text("anon 100\nfile 200\ninactive_file 150\nshmem 20\nbroken x\n")
    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path)

    assert metrics.cgroup_memory_stat() == {"anon": 100, "file": 200, "inactive_file": 150, "shmem": 20}

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "absent")
    assert metrics.cgroup_memory_stat() == {}


def _cgroup(root: Path, *, current: int, peak: int, stat: dict[str, int]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.current").write_text(f"{current}\n")
    (root / "memory.peak").write_text(f"{peak}\n")
    (root / "memory.stat").write_text("".join(f"{k} {v}\n" for k, v in stat.items()))


def test_the_recorded_peak_excludes_reclaimable_cache_and_keeps_the_raw_beside_it(
    monkeypatch, tmp_path: Path
):
    from agent_worker import metrics

    root = tmp_path / "cgroup"
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    sampler = _sampler(current=0, stat={}, kill=Kills())

    # The agent holds 2 GiB, then reads a repository: 5 GiB of clean cache.
    _cgroup(root, current=2 * GIB, peak=2 * GIB, stat=_stat(anon=2 * GIB))
    sampler.sample_once()
    _cgroup(root, current=7 * GIB, peak=7 * GIB + 100 * MIB, stat=_stat(anon=2 * GIB, inactive_file=5 * GIB))
    sampler.sample_once()

    assert sampler.usage.peak_rss_bytes == 2 * GIB
    assert sampler.usage.peak_memory_current_bytes == 7 * GIB + 100 * MIB


def test_the_recorded_peak_counts_the_tmpfs_workspace(monkeypatch, tmp_path: Path):
    from agent_worker import metrics

    root = tmp_path / "cgroup"
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    sampler = _sampler(current=0, stat={}, kill=Kills())

    _cgroup(root, current=5 * GIB, peak=5 * GIB, stat=_stat(anon=2 * GIB, shmem=2 * GIB, inactive_file=GIB))
    sampler.sample_once()

    assert sampler.usage.peak_rss_bytes == 4 * GIB


def test_a_near_miss_is_judged_on_the_working_set(monkeypatch, tmp_path: Path):
    from agent_worker import metrics

    root = tmp_path / "cgroup"
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    sampler = _sampler(current=0, stat={}, kill=Kills())

    _cgroup(root, current=int(LIMIT * 0.99), peak=int(LIMIT * 0.99),
            stat=_stat(anon=2 * GIB, inactive_file=int(LIMIT * 0.99) - 2 * GIB))
    sampler.sample_once()

    assert sampler.evaluate_near_miss({}) is False


def test_combining_runners_keeps_the_highest_raw_peak():
    from agent_worker.metrics import ResourceUsage, combine_usage

    first = ResourceUsage(peak_rss_bytes=2 * GIB, peak_memory_current_bytes=7 * GIB)
    second = ResourceUsage(peak_rss_bytes=3 * GIB, peak_memory_current_bytes=4 * GIB)
    unmeasured = ResourceUsage(peak_rss_bytes=GIB)

    combined = combine_usage([first, second, unmeasured])

    assert combined is not None
    assert combined.peak_rss_bytes == 3 * GIB
    assert combined.peak_memory_current_bytes == 7 * GIB
    assert combine_usage([unmeasured]).peak_memory_current_bytes is None


def test_the_exported_usage_line_carries_the_raw_peak_beside_the_working_set():
    from agent_worker.metrics import LoggingMetricsExporter, ResourceUsage

    lines: list[tuple[str, dict]] = []

    class Log:
        def info(self, msg: str, **kw) -> None:
            lines.append((msg, kw))

    LoggingMetricsExporter(Log()).export(
        ResourceUsage(peak_rss_bytes=2 * GIB, peak_memory_current_bytes=7 * GIB), {}
    )

    (_, fields), = lines
    assert fields["peak_rss_bytes"] == 2 * GIB
    assert fields["peak_memory_current_bytes"] == 7 * GIB
