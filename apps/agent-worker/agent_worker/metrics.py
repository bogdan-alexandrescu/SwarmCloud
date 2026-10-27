"""Per-attempt resource measurement, and the export that feeds sizing decisions.

`RESOURCE_CLASSES` in the frozen catalogue were sized from one measured Claude
Code lane (~2.5 GiB resident) doubled. Doubling a single measurement is a guess
with a safety factor, not a sizing policy, so every attempt reports what it
actually used:

    peak_rss_bytes    high-water resident memory of the whole runner tree
    peak_disk_bytes   high-water bytes on the ephemeral disk
    oom_near_miss     the attempt came within a hair of its memory limit

`oom_near_miss` is the one that matters. An OOM kill is obvious in the logs; the
attempt that finished at 97% of its limit looks like a success and is the one
that will be killed next week when the model gets chattier. Because
`requests == limits` platform-wide there is no headroom to absorb that, so the
near-miss has to be visible before it becomes a kill.

Measurement prefers cgroup v2 (`memory.peak`, `memory.events`), which is what the
kernel's OOM killer itself looks at, and falls back to summing RSS across the
child's process group when no cgroup is visible -- for example on a developer's
laptop during a local smoke run.

CPU (docs/web-ui/redesign-v2.md section 6, S5). Every resource class reserves
whole vCPUs with `requests == limits`, and until this was measured nothing said
whether an agent used a tenth of one or all four -- so "requested vs used" had a
memory row and no CPU row, and could not have one without a worker change:

    cpu_seconds       CPU time consumed while the runner ran
    peak_cpu_cores    the busiest sampling interval, in cores
    mean_cpu_cores    cpu_seconds over the wall time it was measured across
    cpu_source        where the numbers came from: cgroup, proc or rusage

PER ATTEMPT, NOT PER RUNNER. One attempt can start several runners -- a short
rate limit and a reloaded credential both restart in place -- and each gets a
sampler of its own. `combine_usage` is how the worker turns them back into the
attempt's figures: CPU summed, peaks maximised, and one mean taken as total CPU
over total runner time. Reporting the last runner alone understated every
restarted attempt, and restarted the cumulative HEARTBEAT total from zero.

The same preference order as memory, for the same reason: cgroup v2 `cpu.stat`
is the container's own account and what its CPU limit is enforced against. With
no cgroup, the runner's process TREE is walked in /proc -- a tree and not a
process group, because the CLI runners start the agent in a new session and a
group reading would miss the agent entirely. With neither (macOS), only the
kernel's total for reaped children is available, so there is a total and no
peak. None means not measured, never zero.

WHERE THEY GO. The first three, with the limit they are a fraction of, are
typed fields on the attempt document (contract request #15, accepted on #184
on 2026-09-25; `attempt_cpu_fields`), written while a runner runs and when it
is reaped -- that is what Details draws. The cumulative `cpu_seconds` and
`cpu_source` also ride on every periodic HEARTBEAT event, the one time series
the platform keeps, for the reconciler's stuck-browser judgement.

USE OVER TIME, AND THROTTLING (LIVE-UTIL-API, #1689). The owner, 2026-10-20: a
limit and a peak "will always show maximum when in fact the pods do not run at
max cpu the entire time". So every `POINT_SECONDS` the sampler also takes one
point -- the cores used over that window, the container's `memory.current`
(RSS of the runner's process group without a cgroup), and the share of the
window's CFS periods the container spent throttled, `cpu.stat`
`nr_throttled / nr_periods` -- and each HEARTBEAT carries the points taken
since the one before (`Worker._usage_reading`), which
`GET /v1/tasks/{id}/utilisation` serves. Throttling is MEASURED this way rather
than inferred from a peak at the limit:

    throttled_seconds     `throttled_usec` over the attempt, in seconds. Summed
                          across the container's CPUs by the kernel, so it can
                          exceed the wall time; it is the time work WAITED.
    peak_throttled_share  the most-throttled window, 0..1
    mean_throttled_share  throttled periods over all periods, the attempt's

They ride on the HEARTBEAT and not on the attempt document: `Attempt` is the
frozen contract, and typed fields for them are contract change request 107 in
docs/contract-change-requests.md. A window in which no CFS period ran (no
quota, or nothing ran) is a MEASURED 0.0 -- the counters were read and nothing
was throttled; a cgroup without the counters is None.
"""

from __future__ import annotations

import functools
import os
import resource
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

CGROUP_ROOT = Path("/sys/fs/cgroup")

#: One utilisation point per this many seconds (owner request 2026-10-20,
#: #1689): fine enough to show a 4-core burst between two idle stretches, which
#: the 60-150 s between HEARTBEAT events averages away, and coarse enough that
#: `HEARTBEAT_POINTS` of them cover a heartbeat period in one small event.
POINT_SECONDS = 5.0

#: Points a sampler holds before the oldest is dropped. Twice what one
#: heartbeat carries (`lifecycle.HEARTBEAT_POINTS`), so a beat that was late
#: or failed loses its oldest points and never grows the worker's memory.
POINTS_KEPT = 60

#: Fraction of the memory limit above which an attempt is a near miss.
NEAR_MISS_FRACTION = 0.90

#: Fraction of cgroup `memory.max` at which the sampler kills the agent's
#: largest process (#2504). Above `NEAR_MISS_FRACTION`, which only reports, and
#: below the limit, where the kernel's OOM killer takes the whole container --
#: worker included -- so the attempt ends `lost_worker` with nothing recorded.
#: Five claude-code attempts (f-013, f-014, f-039, f-040, f-041) ended that way
#: on an agent's pytest, tsc or dev server. At 95% of an 8 GiB container the
#: guard acts with ~400 MiB still free, two sample intervals before a burst
#: that fills the rest would.
MEMORY_GUARD_FRACTION = 0.95


@dataclass
class ResourceUsage:
    peak_rss_bytes: int = 0
    peak_disk_bytes: int = 0
    oom_near_miss: bool = False
    #: cgroup `memory.events` counters, when the kernel exposes them.
    memory_events: dict[str, int] | None = None
    samples: int = 0
    #: CPU. All None until measured, and None -- not 0 -- when nothing could be
    #: read: an attempt that used no CPU and one nobody measured are different
    #: facts, and a "requested vs used" row must not draw the second as idle.
    cpu_seconds: float | None = None
    peak_cpu_cores: float | None = None
    mean_cpu_cores: float | None = None
    cpu_source: str | None = None
    #: The wall time `cpu_seconds` was measured across, first sample to last.
    #: Not reported anywhere by itself; it is what lets `combine_usage` give an
    #: attempt of several runners ONE mean -- total CPU over total time --
    #: rather than an average of means that weighs a one-second run like a
    #: one-hour one.
    cpu_wall_seconds: float | None = None
    #: Throttling (LIVE-UTIL-API, #1689); see the module docstring. None until
    #: a cgroup's `cpu.stat` throttling counters were read twice.
    throttled_seconds: float | None = None
    peak_throttled_share: float | None = None
    mean_throttled_share: float | None = None
    #: The CFS periods behind the mean, so `combine_usage` weighs a runner by
    #: how long it ran rather than averaging two runners' means.
    cpu_periods: int | None = None
    throttled_periods: int | None = None
    #: Processes the memory guard killed (`ResourceSampler`, #2504). A count,
    #: not the processes: the worker logs each one as it drains them.
    memory_guard_kills: int = 0

    def merge_rss(self, value: int) -> None:
        self.peak_rss_bytes = max(self.peak_rss_bytes, int(value))

    def merge_disk(self, value: int) -> None:
        self.peak_disk_bytes = max(self.peak_disk_bytes, int(value))


def combine_usage(parts: Sequence[ResourceUsage]) -> ResourceUsage | None:
    """One attempt's usage from the runners it started, in the order they ran.

    None when there are no parts: an attempt that never started a runner
    measured nothing, which is not the same as measuring zero.

    * `cpu_seconds` and `cpu_wall_seconds` add up, over the parts that have
      them. A part with no reading contributes nothing rather than a zero, so
      a total is None only when NO runner was measured.
    * The peaks -- memory, disk, CPU cores -- are the highest of any part, and
      a near miss in any runner is a near miss for the attempt.
    * `mean_cpu_cores` is total CPU over total time across the parts that have
      both, and exists only if at least one part was long enough to have a
      mean of its own (see `ResourceSampler._min_interval`), so combining
      cannot turn two too-short spans into the "ten cores" that rule exists
      to refuse.
    * Throttling the same way: `throttled_seconds` and the periods add up,
      the peak share is the highest, and the mean is throttled periods over
      all periods -- None only when no runner read the counters.
    * `memory_guard_kills` adds up: each runner's guard killed its own.
    * `memory_events` is the latest part's that has any: cgroup counters are
      the container's own running totals, so the latest already includes
      every runner before it.

    The parts are read, never modified. The last one may belong to a runner
    that is still running, whose sampler is still writing to it.
    """
    if not parts:
        return None
    out = ResourceUsage()
    timed_cpu = 0.0
    timed_wall = 0.0
    any_mean = False
    for part in parts:
        out.merge_rss(part.peak_rss_bytes)
        out.merge_disk(part.peak_disk_bytes)
        out.oom_near_miss = out.oom_near_miss or bool(part.oom_near_miss)
        if part.memory_events:
            out.memory_events = dict(part.memory_events)
        out.samples += int(part.samples)
        out.memory_guard_kills += int(part.memory_guard_kills)
        if part.cpu_source is not None:
            out.cpu_source = part.cpu_source
        cpu, wall = part.cpu_seconds, part.cpu_wall_seconds
        if cpu is not None:
            out.cpu_seconds = (out.cpu_seconds or 0.0) + cpu
        if wall is not None:
            out.cpu_wall_seconds = (out.cpu_wall_seconds or 0.0) + wall
        if part.peak_cpu_cores is not None and (
            out.peak_cpu_cores is None or part.peak_cpu_cores > out.peak_cpu_cores
        ):
            out.peak_cpu_cores = part.peak_cpu_cores
        if cpu is not None and wall is not None:
            timed_cpu += cpu
            timed_wall += wall
        any_mean = any_mean or part.mean_cpu_cores is not None
        if part.throttled_seconds is not None:
            out.throttled_seconds = (out.throttled_seconds or 0.0) + part.throttled_seconds
        if part.peak_throttled_share is not None and (
            out.peak_throttled_share is None or part.peak_throttled_share > out.peak_throttled_share
        ):
            out.peak_throttled_share = part.peak_throttled_share
        if part.cpu_periods is not None and part.throttled_periods is not None:
            out.cpu_periods = (out.cpu_periods or 0) + part.cpu_periods
            out.throttled_periods = (out.throttled_periods or 0) + part.throttled_periods
    if any_mean and timed_wall > 0:
        out.mean_cpu_cores = timed_cpu / timed_wall
    out.mean_throttled_share = throttled_share(out.throttled_periods, out.cpu_periods)
    return out


def throttled_share(throttled: int | None, periods: int | None) -> float | None:
    """Throttled CFS periods over all periods; 0.0 when none ran, None when not read."""
    if throttled is None or periods is None:
        return None
    if periods <= 0:
        return 0.0
    return max(0.0, min(1.0, throttled / periods))


# ---------------------------------------------------------------------------
# Reading memory
# ---------------------------------------------------------------------------


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except (OSError, ValueError):
        return None
    if raw in ("", "max"):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def cgroup_memory_current() -> int | None:
    return _read_int(CGROUP_ROOT / "memory.current")


def cgroup_memory_max() -> int | None:
    """cgroup v2 `memory.max`, the limit the kernel enforces; None for `max` or absent."""
    return _read_int(CGROUP_ROOT / "memory.max")


def cgroup_memory_peak() -> int | None:
    """cgroup v2 `memory.peak`; present on modern kernels, absent on older ones."""
    return _read_int(CGROUP_ROOT / "memory.peak")


def cgroup_memory_events() -> dict[str, int]:
    events: dict[str, int] = {}
    try:
        for line in (CGROUP_ROOT / "memory.events").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    events[parts[0]] = int(parts[1])
                except ValueError:
                    continue
    except OSError:
        return {}
    return events


def _proc_rss(pid: int) -> int:
    """Resident bytes for one pid, from /proc. Returns 0 where /proc is absent."""
    try:
        parts = (Path("/proc") / str(pid) / "statm").read_text().split()
        return int(parts[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return 0


def process_group_rss(pgid: int) -> int:
    """Sum RSS across every process in the group (the runner and its children)."""
    proc = Path("/proc")
    if proc.is_dir():
        total = 0
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if os.getpgid(int(entry.name)) != pgid:
                    continue
            except (ProcessLookupError, PermissionError, OSError):
                continue
            total += _proc_rss(int(entry.name))
        return total
    # Darwin / no procfs: ask ps. Only used for local development runs.
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-o", "rss=", "-g", str(pgid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            total += int(line) * 1024
    return total


# ---------------------------------------------------------------------------
# Reading CPU
# ---------------------------------------------------------------------------


def cgroup_cpu_seconds() -> float | None:
    """cgroup v2 `cpu.stat` `usage_usec`, in seconds. None when absent or unreadable."""
    try:
        text = (CGROUP_ROOT / "cpu.stat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec":
            try:
                return int(parts[1]) / 1_000_000
            except ValueError:
                return None
    return None


@dataclass(frozen=True)
class CpuThrottling:
    """cgroup v2 `cpu.stat` CFS counters, cumulative since the cgroup began."""

    nr_periods: int
    nr_throttled: int
    throttled_usec: int


def cgroup_cpu_throttling() -> CpuThrottling | None:
    """The container's throttling counters; None when `cpu.stat` lacks any of the three.

    `nr_periods` and `nr_throttled` count CFS enforcement periods (100 ms by
    default) that ran and that hit the quota; `throttled_usec` is the time
    runnable work waited, summed across CPUs. None -- not zeros -- for a
    missing file or a missing counter (cgroup v1, a kernel without CFS
    bandwidth), because a throttling share of 0.0 would claim a measurement.
    """
    try:
        text = (CGROUP_ROOT / "cpu.stat").read_text()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("nr_periods", "nr_throttled", "throttled_usec"):
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                return None
    if len(values) != 3:
        return None
    return CpuThrottling(values["nr_periods"], values["nr_throttled"], values["throttled_usec"])


def cgroup_cpu_limit_cores() -> float | None:
    """The container's CPU LIMIT in cores, from cgroup v2 `cpu.max`; None if unknown.

    `cpu.max` is `"<quota> <period>"` in microseconds -- `"200000 100000"` is
    two cores -- or `"max <period>"` when nothing is enforced. The limit is what
    a "requested vs used" CPU bar is drawn against, and reading it from the
    cgroup is reading what the kernel enforces rather than what a catalogue
    says was asked for. None for `max`, for a missing or unreadable file and
    for anything that does not parse: the caller then falls back to the
    resource class the container was sized with, and says that it did.
    """
    try:
        parts = (CGROUP_ROOT / "cpu.max").read_text().split()
    except OSError:
        return None
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


#: The four CPU fields contract request #15 added to `Attempt`, in its order.
ATTEMPT_CPU_FIELDS = ("cpu_seconds", "peak_cpu_cores", "mean_cpu_cores", "cpu_limit_cores")

#: Where `cpu_limit_cores` came from (contract request #26, accepted on #184,
#: 2026-09-26): the container's cgroup `cpu.max`, or the catalogue cpu of the
#: class it was sized with. The vocabulary `Attempt.cpu_limit_source` takes;
#: the UI's `limitSource` words it (`cgroup limit`, `<class> limit`).
CPU_LIMIT_SOURCES = ("cgroup", "resource_class")


def attempt_cpu_fields(
    usage: ResourceUsage | None, *, limit_cores: float | None, limit_source: str | None = None
) -> dict[str, float | str]:
    """The attempt document's CPU fields, rounded, with what was not measured LEFT OUT.

    CONTRACT REQUEST #15, ACCEPTED on #184 (2026-09-25). `Attempt` carries
    `cpu_seconds`, `peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores`,
    and the worker writes them on its own attempt document
    (`control.record_cpu_usage`) with each periodic reading and when each
    runner is reaped. They REPLACE the interim home #188 gave them, flat keys
    on HEARTBEAT events that the API read back with an events query per
    request. The HEARTBEAT keeps only the cumulative `cpu_seconds` and
    `cpu_source` it carried before, which the reconciler's stuck-browser
    judgement (`reconciler.progress`) turns into a rate.

    LEFT OUT, NOT WRITTEN AS NULL. The write is a merge, so an omitted key
    keeps what an earlier write put there where a null would erase it -- and
    a figure is never measured and then un-measured: `combine_usage` keeps
    every runner's. None on the attempt still means not measured, never zero.

    NOTHING WHEN NOTHING WAS MEASURED, not even the limit: a limit alone would
    read as a reading with its figures missing, and the attempt has none.

    `limit_source` (contract request #26) is `cgroup` or `resource_class`, and
    is written as `cpu_limit_source` only beside a limit. The reading's time,
    `cpu_measured_at`, is the control's to stamp when it writes
    (`control.record_cpu_usage`), so that it is the time of the write.

    `usage` is the ATTEMPT's (`combine_usage` over every runner it started).
    Three decimal places throughout; 1.0 is one whole vCPU. The mean is over
    RUNNER wall time, so setup, the clone and retry waits are not idle time.
    """
    if usage is None:
        return {}
    measured = {
        "cpu_seconds": _rounded(usage.cpu_seconds),
        "peak_cpu_cores": _rounded(usage.peak_cpu_cores),
        "mean_cpu_cores": _rounded(usage.mean_cpu_cores),
    }
    fields = {name: value for name, value in measured.items() if value is not None}
    if not fields:
        return {}
    out: dict[str, float | str] = dict(fields)
    limit = _rounded(limit_cores)
    if limit is not None:
        out["cpu_limit_cores"] = limit
        # CONTRACT REQUEST #26 (accepted on #184, 2026-09-26): where the limit
        # came from, only beside a limit. A source with no limit says nothing.
        if limit_source in CPU_LIMIT_SOURCES:
            out["cpu_limit_source"] = limit_source
    return out


def _proc_stat(entry: Path) -> tuple[int, int, int]:
    """(pid, ppid, CPU clock ticks including reaped children) from /proc/<pid>/stat.

    The command name is field 2 and may itself contain spaces and parentheses,
    so the line is split after the LAST ')'. What follows starts at field 3, so
    ppid (field 4) is index 1 and utime, stime, cutime, cstime (fields 14-17)
    are indices 11-14.
    """
    raw = (entry / "stat").read_text()
    fields = raw[raw.rindex(")") + 2 :].split()
    return int(entry.name), int(fields[1]), sum(int(value) for value in fields[11:15])


@dataclass(frozen=True)
class ProcessInfo:
    """One process as the memory guard sees it. The command name, never argv:
    an argv can carry a secret, and a kill is logged."""

    pid: int
    ppid: int
    pgid: int
    rss_bytes: int
    name: str


def _proc_ids(entry: Path) -> tuple[int, int, int, str]:
    """(pid, ppid, pgid, command name) from /proc/<pid>/stat.

    Split after the LAST ')' as `_proc_stat` does: ppid is field 4 and pgid
    field 5, indices 1 and 2 of what follows. The name is between the first
    '(' and that last ')'.
    """
    raw = (entry / "stat").read_text()
    close = raw.rindex(")")
    fields = raw[close + 2 :].split()
    return int(entry.name), int(fields[1]), int(fields[2]), raw[raw.index("(") + 1 : close]


def proc_process_table() -> list[ProcessInfo]:
    """Every process in /proc with its parent, group and RSS; [] without procfs."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    out: list[ProcessInfo] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid, ppid, pgid, name = _proc_ids(entry)
        except (OSError, ValueError, IndexError):
            continue  # exited between the listing and the read
        out.append(ProcessInfo(pid=pid, ppid=ppid, pgid=pgid, rss_bytes=_proc_rss(pid), name=name))
    return out


def memory_guard_victim(table: Sequence[ProcessInfo], runner_pid: int) -> ProcessInfo | None:
    """The agent process to sacrifice: the largest-RSS one of the runner's.

    The runner's are its descendants by parent pid and the members of its
    process group (procman starts it with `start_new_session`, so the group is
    the runner's own; a dev server that double-forks to init stays in it).
    Never this worker, the runner, or the runner's direct children -- the
    agent CLI, whose death ends the attempt as surely as the OOM killer would.
    None when nothing is left to choose.
    """
    by_pid = {p.pid: p for p in table}
    children: dict[int, list[int]] = {}
    for p in table:
        children.setdefault(p.ppid, []).append(p.pid)
    runner = by_pid.get(runner_pid)
    group = runner.pgid if runner is not None else runner_pid
    agents: set[int] = {p.pid for p in table if p.pgid == group}
    stack, seen = [runner_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        agents.add(pid)
        stack.extend(children.get(pid, ()))
    protected = {os.getpid(), runner_pid, *children.get(runner_pid, ())}
    candidates = [by_pid[pid] for pid in agents - protected if pid in by_pid and by_pid[pid].rss_bytes > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.rss_bytes, p.pid))


def process_tree_cpu_seconds(root_pid: int) -> float | None:
    """CPU seconds used by `root_pid` and every live descendant. None without procfs.

    Walked by PARENT pid, not process group: the CLI runners start the agent
    with `start_new_session=True`, so it is in a different group from the
    runner, and a group reading would count the runner's Python and not the
    agent doing the work.

    Each second is counted once: a live process's own time is summed, and a
    process that has exited and been reaped is already inside its parent's
    `cutime`/`cstime`. A process reparented away from the tree (a daemon that
    double-forks) is lost; nothing an agent CLI does needs that.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    try:
        hertz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    ticks: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid, ppid, used = _proc_stat(entry)
        except (OSError, ValueError, IndexError):
            continue  # exited between the listing and the read
        ticks[pid] = used
        children.setdefault(ppid, []).append(pid)
    if root_pid not in ticks:
        return None
    total = 0
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += ticks.get(pid, 0)
        stack.extend(children.get(pid, ()))
    return total / hertz


def reaped_children_cpu_seconds() -> float:
    """CPU seconds of every child this process has waited for, and theirs.

    Exact, portable, and only complete once the runner has been reaped -- so it
    is the TOTAL at stop, never a live reading.
    """
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


class CpuMeter(Protocol):
    #: "cgroup", "proc" or "rusage" -- recorded with the numbers so a reader
    #: knows whether they cover the container or only the runner's tree.
    source: str | None

    def cumulative(self) -> float | None:
        """A live, monotonic-while-running CPU total in seconds, or None."""

    def consumed(self) -> float | None:
        """CPU seconds since the meter was built; exact once the runner is reaped."""


class SystemCpuMeter:
    """cgroup v2 if the container has one, else the process tree, else rusage.

    Built when the runner starts, so both baselines -- the cgroup counter and
    the reaped-children total -- exclude everything the worker did before it.
    """

    def __init__(self, pid_provider: Callable[[], int | None]) -> None:
        self._pid_provider = pid_provider
        self._cgroup_start = cgroup_cpu_seconds()
        self._reaped_start = reaped_children_cpu_seconds()
        if self._cgroup_start is not None:
            self.source: str | None = "cgroup"
        elif Path("/proc").is_dir():
            self.source = "proc"
        else:
            self.source = "rusage"

    def cumulative(self) -> float | None:
        if self.source == "cgroup":
            return cgroup_cpu_seconds()
        if self.source == "proc":
            pid = self._pid_provider()
            return process_tree_cpu_seconds(pid) if pid else None
        return None

    def consumed(self) -> float | None:
        if self.source == "cgroup":
            now = cgroup_cpu_seconds()
            if now is None or self._cgroup_start is None:
                return None
            return max(0.0, now - self._cgroup_start)
        # The tree reading loses the runner the moment it exits; the kernel's
        # reaped-children total is where its time went, and it is exact.
        return max(0.0, reaped_children_cpu_seconds() - self._reaped_start)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _next_deadline(deadline: float, every: float, now: float) -> float:
    """The next deadline on the cadence, or one period from now if it fell behind."""
    following = deadline + every
    return following if following > now else now + every


class ResourceSampler:
    """Background sampler. Start it when the child starts, stop it when it ends."""

    def __init__(
        self,
        *,
        pid_provider: Callable[[], int | None],
        disk_provider: Callable[[], int],
        memory_limit_bytes: int,
        interval_seconds: float = 2.0,
        near_miss_fraction: float = NEAR_MISS_FRACTION,
        cpu_meter: CpuMeter | None = None,
        clock: Callable[[], float] = time.monotonic,
        point_seconds: float = POINT_SECONDS,
        series_origin: float | None = None,
        memory_guard_fraction: float = MEMORY_GUARD_FRACTION,
        memory_guard_cooldown_seconds: float | None = None,
        memory_current_reader: Callable[[], int | None] = cgroup_memory_current,
        memory_max_reader: Callable[[], int | None] = cgroup_memory_max,
        process_table: Callable[[], Sequence[ProcessInfo]] = proc_process_table,
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        self._pid_provider = pid_provider
        self._disk_provider = disk_provider
        self._limit = memory_limit_bytes
        self._interval = interval_seconds
        self._near_miss_fraction = near_miss_fraction
        self.usage = ResourceUsage()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Injected in tests; built on the first sample otherwise, which is the
        # moment the runner has just started.
        self._cpu_meter = cpu_meter
        self._clock = clock
        #: The shortest interval turned into a rate. Half the sampling
        #: interval, and never under half a second: 0.1s of CPU over 10ms of
        #: wall clock is "ten cores" only by arithmetic.
        self._min_interval = max(0.5, interval_seconds / 2)
        self._cpu_first: float | None = None
        self._cpu_base: tuple[float, float] | None = None
        self._t_first: float | None = None
        self._t_last: float | None = None
        # The utilisation series (#1689). A point's `t` is seconds since
        # `series_origin` on `clock`: the worker passes ONE origin to every
        # runner's sampler, so an attempt that restarts its runner in place
        # keeps one clock and its points stay in order.
        self._point_seconds = point_seconds
        # Without one, the first point's time (read there, not here: the
        # clock is read only when a sample is taken).
        self._origin: float | None = series_origin
        self._point_base: tuple[float, float | None, CpuThrottling | None] | None = None
        self._points: deque[dict[str, Any]] = deque(maxlen=POINTS_KEPT)
        self._points_lock = threading.Lock()
        # The memory guard (#2504); see `_guard_memory`. Judged against the
        # kernel's own `memory.max`, never `memory_limit_bytes`: the sized
        # class is what the near miss reports against, and a guard that killed
        # on it where the cgroup has no limit would kill on a guess.
        self._guard_fraction = memory_guard_fraction
        #: Two sample intervals by default: a SIGKILLed process's memory is
        #: freed by the kernel, not at once, and the reading just after a kill
        #: would otherwise take a second process for the first one's pages.
        self._guard_cooldown = (
            2 * interval_seconds if memory_guard_cooldown_seconds is None else memory_guard_cooldown_seconds
        )
        self._guard_quiet_until: float | None = None
        self._memory_current_reader = memory_current_reader
        self._memory_max_reader = memory_max_reader
        self._process_table = process_table
        self._kill = kill
        self._guard_kills: list[dict[str, Any]] = []
        self._guard_lock = threading.Lock()

    def _meter(self) -> CpuMeter:
        if self._cpu_meter is None:
            self._cpu_meter = SystemCpuMeter(self._pid_provider)
        return self._cpu_meter

    def _sample_cpu(self) -> None:
        now = self._clock()
        if self._t_first is None:
            self._t_first = now
        self._t_last = now
        try:
            meter = self._meter()
            self.usage.cpu_source = meter.source
            reading = meter.cumulative()
        except Exception:
            reading = None
        if reading is None:
            return
        if self._cpu_first is None:
            self._cpu_first = reading
            self._cpu_base = (now, reading)
            self.usage.cpu_seconds = 0.0
            return
        # Held at its high-water mark: a tree reading dips when a process
        # exits before its parent reaps it, and a total must not go backwards.
        self.usage.cpu_seconds = max(self.usage.cpu_seconds or 0.0, reading - self._cpu_first)
        # Kept current while the runner is alive, so a combination taken
        # mid-run (a heartbeat, a crash's export) pairs CPU-so-far with
        # time-so-far rather than with nothing.
        self.usage.cpu_wall_seconds = now - self._t_first
        # AND THE MEAN WITH THEM (#184). It used to be set only in
        # `_finish_cpu`, and `combine_usage` gives an attempt a mean only when
        # some part has one -- so a RUNNING attempt never had a mean, and the
        # Details pane could draw its peak and not its mean until it ended.
        # The same floor `_finish_cpu` applies: a span shorter than the
        # shortest rate interval is not turned into a mean.
        if self.usage.cpu_wall_seconds >= self._min_interval:
            self.usage.mean_cpu_cores = self.usage.cpu_seconds / self.usage.cpu_wall_seconds
        base_time, base_reading = self._cpu_base or (now, reading)
        elapsed = now - base_time
        if elapsed < self._min_interval:
            return
        delta = reading - base_reading
        self._cpu_base = (now, reading)
        if delta < 0:
            return
        rate = delta / elapsed
        if self.usage.peak_cpu_cores is None or rate > self.usage.peak_cpu_cores:
            self.usage.peak_cpu_cores = rate

    def _finish_cpu(self) -> None:
        try:
            consumed = self._meter().consumed()
        except Exception:
            consumed = None
        if consumed is not None:
            self.usage.cpu_seconds = consumed
        if self.usage.cpu_seconds is None or self._t_first is None or self._t_last is None:
            return
        span = self._t_last - self._t_first
        self.usage.cpu_wall_seconds = span
        if span >= self._min_interval:
            self.usage.mean_cpu_cores = self.usage.cpu_seconds / span

    def series_elapsed(self) -> float:
        """Now, on the clock the points' `t` is on."""
        now = self._clock()
        if self._origin is None:
            self._origin = now
        return now - self._origin

    def _memory_now(self) -> int | None:
        """The container's memory now: cgroup `memory.current`, else the group's RSS."""
        current = cgroup_memory_current()
        if current is not None:
            return current
        pid = self._pid_provider()
        if not pid:
            return None
        try:
            return process_group_rss(os.getpgid(pid)) or None
        except (ProcessLookupError, OSError):
            return None

    def sample_point(self) -> None:
        """Close the current window into one point, and open the next.

        `{"t", "cpu", "mem", "thr"}`: seconds on the series clock at the
        window's end, cores used over the window, memory now, and the share of
        the window's CFS periods that were throttled. Short keys because the
        points ride on every HEARTBEAT (`lifecycle.HEARTBEAT_DETAIL_MAX_BYTES`).
        Each is None when it could not be read, never 0. The first call only
        opens a window. Also keeps the runner's throttling figures current.
        """
        now = self._clock()
        if self._origin is None:
            self._origin = now
        try:
            cpu = self._meter().cumulative()
        except Exception:
            cpu = None
        throttling = cgroup_cpu_throttling()
        memory = self._memory_now()
        base = self._point_base
        self._point_base = (now, cpu, throttling)
        if base is None:
            return
        base_time, base_cpu, base_throttling = base
        elapsed = now - base_time
        if elapsed <= 0:
            return
        cores: float | None = None
        if cpu is not None and base_cpu is not None and cpu >= base_cpu:
            cores = (cpu - base_cpu) / elapsed
        share: float | None = None
        if throttling is not None and base_throttling is not None:
            periods = throttling.nr_periods - base_throttling.nr_periods
            throttled = throttling.nr_throttled - base_throttling.nr_throttled
            waited = throttling.throttled_usec - base_throttling.throttled_usec
            if periods >= 0 and throttled >= 0 and waited >= 0:
                share = throttled_share(throttled, periods)
                usage = self.usage
                usage.cpu_periods = (usage.cpu_periods or 0) + periods
                usage.throttled_periods = (usage.throttled_periods or 0) + throttled
                usage.throttled_seconds = (usage.throttled_seconds or 0.0) + waited / 1_000_000
                if share is not None and (
                    usage.peak_throttled_share is None or share > usage.peak_throttled_share
                ):
                    usage.peak_throttled_share = share
                usage.mean_throttled_share = throttled_share(
                    usage.throttled_periods, usage.cpu_periods
                )
        point = {
            "t": round(now - self._origin, 1),
            "cpu": _rounded(cores),
            "mem": memory,
            "thr": _rounded(share),
        }
        with self._points_lock:
            self._points.append(point)

    def drain_points(self) -> list[dict[str, Any]]:
        """The points taken since the last drain, oldest first; each is served once."""
        with self._points_lock:
            out = list(self._points)
            self._points.clear()
        return out

    def sample_once(self) -> None:
        peak = cgroup_memory_peak()
        if peak is not None:
            self.usage.merge_rss(peak)
        else:
            current = cgroup_memory_current()
            if current is not None:
                self.usage.merge_rss(current)
            else:
                pid = self._pid_provider()
                if pid:
                    try:
                        self.usage.merge_rss(process_group_rss(os.getpgid(pid)))
                    except (ProcessLookupError, OSError):
                        pass
        try:
            self.usage.merge_disk(self._disk_provider())
        except OSError:
            pass
        self._sample_cpu()
        self._guard_memory()
        self.usage.samples += 1

    def _guard_memory(self) -> None:
        """At `memory_guard_fraction` of `memory.max`, SIGKILL the agent's largest process.

        The kernel's OOM killer, left to act, took the whole container and the
        worker with it (#2504). Killing the agent's biggest process -- its
        pytest, tsc or dev server -- costs the agent one command it can see
        fail and rerun smaller; the worker survives to checkpoint and record.
        Never the worker, the runner or the CLI (`memory_guard_victim`). One
        kill per cooldown. Never raises: the sampler thread must survive.
        """
        try:
            current = self._memory_current_reader()
            limit = self._memory_max_reader()
            if current is None or not limit or current < limit * self._guard_fraction:
                return
            now = self._clock()
            if self._guard_quiet_until is not None and now < self._guard_quiet_until:
                return
            runner = self._pid_provider()
            if not runner:
                return
            victim = memory_guard_victim(self._process_table(), runner)
            if victim is None:
                return
            self._kill(victim.pid, signal.SIGKILL)
            self._guard_quiet_until = now + self._guard_cooldown
            self.usage.memory_guard_kills += 1
            with self._guard_lock:
                self._guard_kills.append(
                    {
                        "pid": victim.pid,
                        "name": victim.name,
                        "rss_bytes": victim.rss_bytes,
                        "memory_current_bytes": current,
                        "memory_max_bytes": limit,
                    }
                )
        except Exception:
            return

    def drain_guard_kills(self) -> list[dict[str, Any]]:
        """The memory guard's kills since the last drain, oldest first; each served once."""
        with self._guard_lock:
            out = list(self._guard_kills)
            self._guard_kills.clear()
        return out

    def _run(self) -> None:
        # Two cadences on one thread: the 2 s sample behind the peaks, and the
        # 5 s point behind the series, each on its own deadline so neither
        # drifts to the other's multiple.
        next_sample = self._clock() + self._interval
        next_point = self._clock() + self._point_seconds
        while not self._stop.wait(max(0.0, min(next_sample, next_point) - self._clock())):
            now = self._clock()
            if now >= next_sample:
                self.sample_once()
                next_sample = _next_deadline(next_sample, self._interval, now)
            if now >= next_point:
                self.sample_point()
                next_point = _next_deadline(next_point, self._point_seconds, now)

    def start(self) -> None:
        self.sample_once()
        self.sample_point()
        self._thread = threading.Thread(target=self._run, daemon=True, name="swarm-sampler")
        self._thread.start()

    def stop(self) -> ResourceUsage:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.sample_once()
        events = cgroup_memory_events()
        if events:
            self.usage.memory_events = events
        self.usage.oom_near_miss = self.evaluate_near_miss(events)
        self._finish_cpu()
        return self.usage

    def evaluate_near_miss(self, events: dict[str, int] | None = None) -> bool:
        events = events if events is not None else (self.usage.memory_events or {})
        # The kernel already told us: reclaim was forced at the limit.
        if events.get("max", 0) > 0 or events.get("oom", 0) > 0:
            return True
        if self._limit <= 0:
            return False
        return self.usage.peak_rss_bytes >= self._limit * self._near_miss_fraction


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

METRIC_PREFIX = "custom.googleapis.com/swarm/attempt"


class LoggingMetricsExporter:
    """Writes the measurements as one structured log line.

    Always used in addition to Cloud Monitoring, because a metric write that
    fails must not take the measurement with it -- the log line is what the
    tuning report falls back to.
    """

    def __init__(self, logger: Any) -> None:
        self._log = logger

    def export(self, usage: ResourceUsage, labels: dict[str, str]) -> None:
        self._log.info(
            "attempt resource usage",
            peak_rss_bytes=usage.peak_rss_bytes,
            peak_disk_bytes=usage.peak_disk_bytes,
            oom_near_miss=usage.oom_near_miss,
            memory_events=usage.memory_events or {},
            samples=usage.samples,
            # Null when not measured, never 0; see ResourceUsage.
            cpu_seconds=_rounded(usage.cpu_seconds),
            peak_cpu_cores=_rounded(usage.peak_cpu_cores),
            mean_cpu_cores=_rounded(usage.mean_cpu_cores),
            cpu_source=usage.cpu_source,
            memory_guard_kills=usage.memory_guard_kills,
            **labels,
        )


def _rounded(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(float(value), places)


#: Upper bound on building the Cloud Monitoring client, and separately on each
#: `create_time_series` call. The export runs on the attempt's exit path, and
#: the library's default retry policy would otherwise let a hung or throttled
#: endpoint hold the exit for minutes; a measurement that misses its write is
#: still in the log line.
#:
#: 5 s, from a measurement: on mock execution swarm-job-smoke-mock-sk8h9
#: (2026-10-06 06:05) 53 s passed with nothing logged between the "attempt
#: resource usage" line and the quota park's broker POST, against 1.3-3.1 s
#: across 24 other quota parks. The likely cause was the client's construction
#: (credential discovery, a channel), which had no bound at all. 5 s is above
#: every healthy span measured there and an order of magnitude below the hang.
#: The worker also records its park or terminal state BEFORE it exports
#: (`Worker._metrics_after_the_record`), so this bound only limits how long a hung
#: exporter holds the exit -- never the park.
EXPORT_TIMEOUT_SECONDS = 5.0


def call_with_timeout(fn: Callable[[], Any], timeout_seconds: float, *, name: str) -> Any:
    """`fn()`, or `TimeoutError` once `timeout_seconds` have passed.

    On a daemon thread, because neither a client's construction nor a gRPC
    call can be interrupted from outside: a call that never returns is left
    behind and dies with the process, which is exiting anyway.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # handed to the caller below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name=name)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(f"{name} did not return within {timeout_seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _monitored_resource(project_id: str, region: str, labels: dict[str, str]) -> Any:
    """The `generic_task` resource every series is written against.

    Built from `google.api.monitored_resource_pb2`, the type `TimeSeries.resource`
    is declared with: google-cloud-monitoring does not re-export it as
    `monitoring_v3.MonitoredResource`, and asking for that raised on every
    attempt before a request existed (observer P12, 518 warnings on 2026-10-06).

    Why `generic_task`: it is one of the few resource types Cloud Monitoring
    accepts custom metrics against, and it fits both backends the same way.
    `cloud_run_job` is not writable for custom metrics at all, and `k8s_pod`
    would give the GKE backend a second schema whose labels (cluster, pod)
    the sizing report does not group by. Its five labels are all required:
    the tenant is the namespace, the runner profile the job, and the attempt
    the task, so one series is one attempt.
    """
    from google.api import monitored_resource_pb2

    return monitored_resource_pb2.MonitoredResource(
        type="generic_task",
        labels={
            "project_id": project_id,
            "location": region,
            "namespace": labels.get("tenant_id") or "unknown",
            "job": labels.get("runner_profile") or "unknown",
            "task_id": labels.get("attempt_id") or "unknown",
        },
    )


def _timed_out(exc: BaseException) -> bool:
    """Our bound, or the RPC's deadline (api_core's `DeadlineExceeded`, or the
    `RetryError` its retry policy raises when the deadline ends the retries)."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ in (
        "DeadlineExceeded",
        "RetryError",
    )


class CloudMonitoringExporter:
    """Writes GAUGE time series per attempt to Cloud Monitoring.

    Three for memory and disk, always; up to three for CPU, only when measured
    and in a SEPARATE write. Those are new metric types, created on their first
    write, and one call that failed on them would take the three series the
    sizing report already depends on down with it.

    Never raises and never logs more than one warning per exporter: a failed
    write costs the attempt nothing, and one line says why rather than one per
    write or per retry of the export.
    """

    def __init__(
        self,
        project_id: str,
        region: str,
        logger: Any,
        client: Any | None = None,
        *,
        timeout_seconds: float = EXPORT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_id = project_id
        self._region = region
        self._log = logger
        self._client = client
        self._warned = False
        self.timeout_seconds = timeout_seconds

    def _build_client(self) -> Any:
        # The import too: it is the library's first load, and it runs on the
        # bounded thread with the construction.
        from google.cloud import monitoring_v3  # lazy: never imported by tests

        return monitoring_v3.MetricServiceClient()

    def _get_client(self) -> Any:
        # Bounded: an unbounded construction is the likely cause of the 53 s
        # measured on 2026-10-06 (EXPORT_TIMEOUT_SECONDS).
        if self._client is None:
            self._client = call_with_timeout(
                self._build_client, self.timeout_seconds, name="swarm-metrics-client"
            )
        return self._client

    def _warn_once(self, exc: Exception, write: str) -> None:
        if self._warned:
            return
        self._warned = True
        self._log.warning(
            "metrics export failed",
            exporter=type(self).__name__,
            write=write,
            error=str(exc),
        )

    def export(self, usage: ResourceUsage, labels: dict[str, str]) -> None:
        try:
            self._export(usage, labels)
        except Exception as exc:
            self._warn_once(exc, "build")

    def _export(self, usage: ResourceUsage, labels: dict[str, str]) -> None:
        # The client first, under its bound: it is what imports the library,
        # and a construction that hung must not be followed by an unbounded
        # import of the same modules here.
        try:
            client = self._get_client()
        except Exception as exc:
            self._warn_once(exc, "client")
            return

        from google.cloud import monitoring_v3

        now = time.time()
        interval = monitoring_v3.TimeInterval(
            {"end_time": {"seconds": int(now), "nanos": int((now % 1) * 1e9)}}
        )
        resource = _monitored_resource(self._project_id, self._region, labels)
        metric_labels = {
            k: str(v)
            for k, v in labels.items()
            if k in ("tenant_id", "runner_profile", "resource_class", "backend")
        }
        values = {
            "peak_rss_bytes": int(usage.peak_rss_bytes),
            "peak_disk_bytes": int(usage.peak_disk_bytes),
            "oom_near_miss": 1 if usage.oom_near_miss else 0,
        }

        def _series(named: dict[str, int]) -> list[Any]:
            out = []
            for name, value in named.items():
                s = monitoring_v3.TimeSeries()
                s.metric.type = f"{METRIC_PREFIX}/{name}"
                for key, val in metric_labels.items():
                    s.metric.labels[key] = val
                s.resource = resource
                s.points = [
                    monitoring_v3.Point({"interval": interval, "value": {"int64_value": value}})
                ]
                out.append(s)
            return out

        # Integers in milli-units, like every other series here: the metric
        # kind is fixed on first write, and one INT64 convention is one fewer
        # way for a dashboard to be off by a factor of a thousand.
        cpu: dict[str, int] = {}
        if usage.cpu_seconds is not None:
            cpu["cpu_time_ms"] = int(round(usage.cpu_seconds * 1000))
        if usage.peak_cpu_cores is not None:
            cpu["peak_cpu_millicores"] = int(round(usage.peak_cpu_cores * 1000))
        if usage.mean_cpu_cores is not None:
            cpu["mean_cpu_millicores"] = int(round(usage.mean_cpu_cores * 1000))

        writes = [("memory", _series(values))]
        if cpu:
            writes.append(("cpu", _series(cpu)))

        for write, series in writes:
            # Each write on its own: a CPU write that fails (new metric types)
            # must not take the memory series down with it, and vice versa.
            # Bounded twice: the RPC's own deadline, and a thread join in case
            # the library does not honour it. A write that TIMED OUT, by
            # either bound, ends the export: the next one would wait on the
            # same hung endpoint.
            try:
                call_with_timeout(
                    functools.partial(
                        client.create_time_series,
                        name=f"projects/{self._project_id}",
                        time_series=series,
                        timeout=self.timeout_seconds,
                    ),
                    self.timeout_seconds,
                    name=f"swarm-metrics-{write}",
                )
            except Exception as exc:
                self._warn_once(exc, write)
                if _timed_out(exc):
                    return


class CompositeExporter:
    def __init__(self, exporters: list[Any], logger: Any) -> None:
        self._exporters = exporters
        self._log = logger

    def export(self, usage: ResourceUsage, labels: dict[str, str]) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(usage, labels)
            except Exception as exc:
                # Never let telemetry failure change the attempt's outcome.
                self._log.warning(
                    "metrics export failed",
                    exporter=type(exporter).__name__,
                    error=str(exc),
                )


def build_metrics_exporter(
    *, project_id: str, region: str, logger: Any, enable_cloud_monitoring: bool = True
) -> CompositeExporter:
    exporters: list[Any] = [LoggingMetricsExporter(logger)]
    if enable_cloud_monitoring and project_id:
        exporters.append(CloudMonitoringExporter(project_id, region, logger))
    return CompositeExporter(exporters, logger)
