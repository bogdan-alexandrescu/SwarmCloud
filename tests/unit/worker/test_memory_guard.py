"""The sampler sacrifices the agent's biggest process before the kernel takes the worker (#2504).

Five claude-code attempts (f-013, f-014, f-039, f-040, f-041) filled their
container with an agent's pytest, tsc and dev servers, and the kernel's OOM
killer took the whole container -- worker included -- so each ended
`lost_worker`. `ResourceSampler` read `memory.current` and `memory.max` and
did nothing with them.

What is pinned here:

  * at 95% or more of `memory.max` the guard SIGKILLs exactly the largest
    descendant of the runner, and below it nothing;
  * the worker, the runner (`child.pid`) and the agent CLI (the runner's
    direct children) are never chosen, even when one of them is the largest,
    and nor is a process outside the runner's tree or group;
  * no candidate means no kill and no exception, and a reader or kill that
    raises does not take the sampler down;
  * one kill, then a cooldown, so the kernel can reclaim before the next
    reading is acted on;
  * each kill is recorded for the worker to drain, and counted on the usage;
  * against the real process table: a real `sleep` grandchild under a
    `start_new_session` child is killed, and only it.

No real process is signalled except in the last test, which kills only the
`sleep` it started. Imports are inside the tests so each one fails on its own.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

GIB = 1024**3
LIMIT = 8 * GIB

RUNNER, CLI, BASH, PYTEST, NODE, OUTSIDER = 1000, 1001, 1002, 1003, 1004, 2000


class NoCpu:
    source = "scripted"

    def cumulative(self) -> float | None:
        return None

    def consumed(self) -> float | None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Kills:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))


def _tree(*, worker_rss: int = 300 << 20, runner_rss: int = 80 << 20, cli_rss: int = 400 << 20):
    from agent_worker.metrics import ProcessInfo

    worker = os.getpid()
    return [
        ProcessInfo(pid=worker, ppid=1, pgid=worker, rss_bytes=worker_rss, name="python"),
        # procman starts the runner with start_new_session: its own group.
        ProcessInfo(pid=RUNNER, ppid=worker, pgid=RUNNER, rss_bytes=runner_rss, name="python"),
        ProcessInfo(pid=CLI, ppid=RUNNER, pgid=RUNNER, rss_bytes=cli_rss, name="claude"),
        ProcessInfo(pid=BASH, ppid=CLI, pgid=RUNNER, rss_bytes=4 << 20, name="bash"),
        ProcessInfo(pid=PYTEST, ppid=BASH, pgid=RUNNER, rss_bytes=4 * GIB, name="pytest"),
        ProcessInfo(pid=NODE, ppid=BASH, pgid=RUNNER, rss_bytes=1 * GIB, name="node"),
        # Not the runner's: another session entirely, and the biggest of all.
        ProcessInfo(pid=OUTSIDER, ppid=1, pgid=OUTSIDER, rss_bytes=10 * GIB, name="other"),
    ]


def _sampler(*, fraction_of_max: float, table, kill, clock=None, current=None, interval=2.0):
    from agent_worker.metrics import ResourceSampler

    reading = {"current": int(LIMIT * fraction_of_max)} if current is None else current
    return ResourceSampler(
        pid_provider=lambda: RUNNER,
        disk_provider=lambda: 0,
        memory_limit_bytes=LIMIT,
        interval_seconds=interval,
        cpu_meter=NoCpu(),
        clock=clock or Clock(),
        memory_current_reader=lambda: reading["current"],
        memory_max_reader=lambda: LIMIT,
        process_table=table,
        kill=kill,
    )


def test_the_guard_fraction_is_above_the_near_miss_and_below_the_limit():
    from agent_worker.metrics import MEMORY_GUARD_FRACTION, NEAR_MISS_FRACTION

    assert MEMORY_GUARD_FRACTION == 0.95
    assert NEAR_MISS_FRACTION < MEMORY_GUARD_FRACTION < 1.0


def test_at_96_percent_exactly_the_largest_descendant_is_killed():
    kills = Kills()
    sampler = _sampler(fraction_of_max=0.96, table=_tree, kill=kills)

    sampler.sample_once()

    assert kills.sent == [(PYTEST, signal.SIGKILL)]


def test_at_94_percent_nothing_is_killed():
    kills = Kills()
    sampler = _sampler(fraction_of_max=0.94, table=_tree, kill=kills)

    sampler.sample_once()
    sampler.sample_once()

    assert kills.sent == []
    assert sampler.drain_guard_kills() == []
    assert sampler.usage.memory_guard_kills == 0


@pytest.mark.parametrize(
    "biggest",
    [
        {"worker_rss": 20 * GIB},
        {"runner_rss": 20 * GIB},
        {"cli_rss": 20 * GIB},
    ],
    ids=["worker", "runner", "cli"],
)
def test_the_worker_the_runner_and_the_cli_are_never_chosen(biggest):
    kills = Kills()
    sampler = _sampler(fraction_of_max=0.99, table=lambda: _tree(**biggest), kill=kills)

    sampler.sample_once()

    assert kills.sent == [(PYTEST, signal.SIGKILL)]
    chosen = [pid for pid, _ in kills.sent]
    assert os.getpid() not in chosen and RUNNER not in chosen and CLI not in chosen


def test_a_process_outside_the_runners_tree_and_group_is_never_chosen():
    from agent_worker.metrics import ProcessInfo

    worker = os.getpid()
    table = [
        ProcessInfo(pid=worker, ppid=1, pgid=worker, rss_bytes=1 * GIB, name="python"),
        ProcessInfo(pid=RUNNER, ppid=worker, pgid=RUNNER, rss_bytes=1 * GIB, name="python"),
        ProcessInfo(pid=CLI, ppid=RUNNER, pgid=RUNNER, rss_bytes=1 * GIB, name="claude"),
        ProcessInfo(pid=OUTSIDER, ppid=1, pgid=OUTSIDER, rss_bytes=10 * GIB, name="other"),
        # A sibling of the runner, in the worker's group: not the agent's.
        ProcessInfo(pid=OUTSIDER + 1, ppid=worker, pgid=worker, rss_bytes=9 * GIB, name="git"),
    ]
    kills = Kills()
    sampler = _sampler(fraction_of_max=0.99, table=lambda: table, kill=kills)

    sampler.sample_once()

    assert kills.sent == []
    assert sampler.drain_guard_kills() == []


def test_a_process_in_the_runners_group_but_reparented_away_is_still_a_candidate():
    from agent_worker.metrics import ProcessInfo

    table = _tree() + [
        # A daemonised dev server: double-forked to init, still in the group.
        ProcessInfo(pid=3000, ppid=1, pgid=RUNNER, rss_bytes=6 * GIB, name="next-server"),
    ]
    kills = Kills()
    sampler = _sampler(fraction_of_max=0.97, table=lambda: table, kill=kills)

    sampler.sample_once()

    assert kills.sent == [(3000, signal.SIGKILL)]


def test_with_no_candidate_nothing_is_killed_and_nothing_raises():
    from agent_worker.metrics import ProcessInfo

    worker = os.getpid()
    table = [
        ProcessInfo(pid=worker, ppid=1, pgid=worker, rss_bytes=1 * GIB, name="python"),
        ProcessInfo(pid=RUNNER, ppid=worker, pgid=RUNNER, rss_bytes=1 * GIB, name="python"),
        ProcessInfo(pid=CLI, ppid=RUNNER, pgid=RUNNER, rss_bytes=6 * GIB, name="claude"),
    ]
    kills = Kills()
    sampler = _sampler(fraction_of_max=0.99, table=lambda: table, kill=kills)

    sampler.sample_once()
    sampler.sample_once()

    assert kills.sent == []
    assert sampler.drain_guard_kills() == []
    assert sampler.usage.samples == 2


def test_a_reader_or_a_kill_that_raises_does_not_take_the_sampler_down():
    def broken_table():
        raise OSError("no /proc")

    def broken_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    for table, kill in ((broken_table, Kills()), (_tree, broken_kill)):
        sampler = _sampler(fraction_of_max=0.99, table=table, kill=kill)
        sampler.sample_once()
        assert sampler.usage.samples == 1

    from agent_worker.metrics import ResourceSampler

    def broken_reader() -> int | None:
        raise RuntimeError("cgroup gone")

    kills = Kills()
    sampler = ResourceSampler(
        pid_provider=lambda: RUNNER,
        disk_provider=lambda: 0,
        memory_limit_bytes=LIMIT,
        cpu_meter=NoCpu(),
        clock=Clock(),
        memory_current_reader=broken_reader,
        memory_max_reader=lambda: LIMIT,
        process_table=_tree,
        kill=kills,
    )
    sampler.sample_once()
    assert kills.sent == []
    assert sampler.usage.samples == 1


def test_no_runner_or_no_limit_means_no_kill():
    from agent_worker.metrics import ResourceSampler

    for pid, limit in ((None, LIMIT), (RUNNER, None)):
        kills = Kills()
        sampler = ResourceSampler(
            pid_provider=lambda pid=pid: pid,
            disk_provider=lambda: 0,
            memory_limit_bytes=LIMIT,
            cpu_meter=NoCpu(),
            clock=Clock(),
            memory_current_reader=lambda: LIMIT,
            memory_max_reader=lambda limit=limit: limit,
            process_table=_tree,
            kill=kills,
        )
        sampler.sample_once()
        assert kills.sent == []


def test_one_kill_then_a_cooldown_of_two_sample_intervals():
    alive = {PYTEST, NODE}

    def table():
        return [p for p in _tree() if p.pid not in (PYTEST, NODE) or p.pid in alive]

    kills = Kills()

    def kill(pid: int, sig: int) -> None:
        kills(pid, sig)
        alive.discard(pid)

    clock = Clock()
    sampler = _sampler(fraction_of_max=0.98, table=table, kill=kill, clock=clock, interval=2.0)

    sampler.sample_once()
    assert kills.sent == [(PYTEST, signal.SIGKILL)]

    # Still over the line one interval later: the kernel has not reclaimed yet.
    clock.now += 2.0
    sampler.sample_once()
    clock.now += 1.9
    sampler.sample_once()
    assert kills.sent == [(PYTEST, signal.SIGKILL)]

    # Past two intervals and still over: the next largest goes.
    clock.now += 0.2
    sampler.sample_once()
    assert kills.sent == [(PYTEST, signal.SIGKILL), (NODE, signal.SIGKILL)]
    assert sampler.usage.memory_guard_kills == 2


def test_each_kill_is_recorded_once_for_the_worker_to_drain():
    kills = Kills()
    current = {"current": int(LIMIT * 0.96)}
    sampler = _sampler(fraction_of_max=0.96, table=_tree, kill=kills, current=current)

    sampler.sample_once()

    recorded = sampler.drain_guard_kills()
    assert recorded == [
        {
            "pid": PYTEST,
            "name": "pytest",
            "rss_bytes": 4 * GIB,
            "memory_current_bytes": int(LIMIT * 0.96),
            "memory_max_bytes": LIMIT,
        }
    ]
    # Served once; and no argv, which could carry a secret.
    assert sampler.drain_guard_kills() == []
    assert all("argv" not in k and "cmdline" not in k for k in recorded)
    assert sampler.usage.memory_guard_kills == 1


def test_the_kill_count_survives_combining_runners():
    from agent_worker.metrics import ResourceUsage, combine_usage

    combined = combine_usage(
        [ResourceUsage(memory_guard_kills=1), ResourceUsage(), ResourceUsage(memory_guard_kills=2)]
    )

    assert combined is not None
    assert combined.memory_guard_kills == 3


def test_the_proc_table_reads_pid_ppid_pgid_rss_and_name_of_this_process():
    if not Path("/proc/self/stat").exists():
        pytest.skip("no procfs")
    from agent_worker.metrics import proc_process_table

    mine = {p.pid: p for p in proc_process_table()}[os.getpid()]

    assert mine.ppid == os.getppid()
    assert mine.pgid == os.getpgid(0)
    assert mine.rss_bytes > 0
    assert mine.name


def _children_of(pid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue
        if int(raw[raw.rindex(")") + 2 :].split()[1]) == pid:
            out.append(int(entry.name))
    return out


def _alive(pid: int) -> bool:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError:
        return False
    return raw[raw.rindex(")") + 2 :].split()[0] != "Z"


def test_a_real_sleep_grandchild_is_killed_and_only_it():
    if not Path("/proc/self/stat").exists():
        pytest.skip("no procfs")
    from agent_worker.metrics import ResourceSampler

    # runner (own session) -> sh, the "CLI" -> sleep, the agent's process.
    # `; sleep 60` after the first keeps sh from exec'ing into it.
    runner = subprocess.Popen(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-c",
            "import subprocess, time; subprocess.Popen(['sh', '-c', 'sleep 60; sleep 60']).wait(); time.sleep(60)",
        ],
        start_new_session=True,
    )
    try:
        cli: list[int] = []
        sleeper: list[int] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not sleeper:
            cli = _children_of(runner.pid)
            sleeper = _children_of(cli[0]) if cli else []
            if not sleeper:
                time.sleep(0.05)
        assert cli and sleeper, "the tree did not come up"

        sampler = ResourceSampler(
            pid_provider=lambda: runner.pid,
            disk_provider=lambda: 0,
            memory_limit_bytes=LIMIT,
            cpu_meter=NoCpu(),
            clock=Clock(),
            memory_current_reader=lambda: LIMIT,
            memory_max_reader=lambda: LIMIT,
        )
        sampler.sample_once()

        kills = sampler.drain_guard_kills()
        assert [k["pid"] for k in kills] == [sleeper[0]]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(sleeper[0]):
            time.sleep(0.05)
        assert not _alive(sleeper[0])
        assert runner.poll() is None and _alive(cli[0]) and _alive(os.getpid())
    finally:
        os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=10)


def test_the_worker_logs_each_kill_when_it_reaps_the_runner(
    db, worker_factory, monkeypatch, tmp_path: Path
):
    from agent_worker import metrics
    from agent_worker.metrics import ResourceUsage

    from worker_seeds import seed_attempt

    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "no-cgroup-here")
    seed_attempt(db)
    worker, _, _ = worker_factory()
    warnings: list[tuple[str, dict]] = []
    monkeypatch.setattr(worker.log, "warning", lambda msg, **kw: warnings.append((msg, kw)))
    kill = {
        "pid": PYTEST,
        "name": "pytest",
        "rss_bytes": 4 * GIB,
        "memory_current_bytes": int(LIMIT * 0.96),
        "memory_max_bytes": LIMIT,
    }

    class Guarded:
        usage = ResourceUsage(peak_rss_bytes=100, memory_guard_kills=1)

        def __init__(self) -> None:
            self.kills = [dict(kill)]

        def stop(self):
            return self.usage

        def drain_points(self):
            return []

        def drain_guard_kills(self):
            out, self.kills = self.kills, []
            return out

    worker._sampler = Guarded()
    worker._stop_sampler()

    logged = [kw for msg, kw in warnings if msg == "memory guard killed the agent's largest process"]
    assert logged == [kill]
    assert worker._attempt_usage().memory_guard_kills == 1


def test_the_exported_usage_line_carries_the_kill_count():
    import io
    import json

    from agent_worker.logs import StructuredLogger
    from agent_worker.metrics import LoggingMetricsExporter, ResourceUsage

    stream = io.StringIO()
    LoggingMetricsExporter(StructuredLogger(stream=stream)).export(
        ResourceUsage(memory_guard_kills=2), {"tenant_id": "eng"}
    )

    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["memory_guard_kills"] == 2
