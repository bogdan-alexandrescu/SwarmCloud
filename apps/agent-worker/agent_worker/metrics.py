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
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CGROUP_ROOT = Path("/sys/fs/cgroup")

#: Fraction of the memory limit above which an attempt is a near miss.
NEAR_MISS_FRACTION = 0.90


@dataclass
class ResourceUsage:
    peak_rss_bytes: int = 0
    peak_disk_bytes: int = 0
    oom_near_miss: bool = False
    #: cgroup `memory.events` counters, when the kernel exposes them.
    memory_events: dict[str, int] | None = None
    samples: int = 0

    def merge_rss(self, value: int) -> None:
        self.peak_rss_bytes = max(self.peak_rss_bytes, int(value))

    def merge_disk(self, value: int) -> None:
        self.peak_disk_bytes = max(self.peak_disk_bytes, int(value))


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
# Sampling
# ---------------------------------------------------------------------------


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
    ) -> None:
        self._pid_provider = pid_provider
        self._disk_provider = disk_provider
        self._limit = memory_limit_bytes
        self._interval = interval_seconds
        self._near_miss_fraction = near_miss_fraction
        self.usage = ResourceUsage()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        self.usage.samples += 1

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.sample_once()

    def start(self) -> None:
        self.sample_once()
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
            **labels,
        )


class CloudMonitoringExporter:
    """Writes three GAUGE time series per attempt to Cloud Monitoring."""

    def __init__(self, project_id: str, region: str, logger: Any, client: Any | None = None) -> None:
        self._project_id = project_id
        self._region = region
        self._log = logger
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import monitoring_v3  # lazy: never imported by tests

            self._client = monitoring_v3.MetricServiceClient()
        return self._client

    def export(self, usage: ResourceUsage, labels: dict[str, str]) -> None:
        from google.cloud import monitoring_v3

        now = time.time()
        interval = monitoring_v3.TimeInterval(
            {"end_time": {"seconds": int(now), "nanos": int((now % 1) * 1e9)}}
        )
        resource = monitoring_v3.MonitoredResource(
            {
                "type": "generic_task",
                "labels": {
                    "project_id": self._project_id,
                    "location": self._region,
                    "namespace": labels.get("tenant_id", "unknown"),
                    "job": labels.get("runner_profile", "unknown"),
                    "task_id": labels.get("attempt_id", "unknown"),
                },
            }
        )
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
        series = []
        for name, value in values.items():
            s = monitoring_v3.TimeSeries()
            s.metric.type = f"{METRIC_PREFIX}/{name}"
            for key, val in metric_labels.items():
                s.metric.labels[key] = val
            s.resource = resource
            s.points = [
                monitoring_v3.Point({"interval": interval, "value": {"int64_value": value}})
            ]
            series.append(s)
        self._get_client().create_time_series(
            name=f"projects/{self._project_id}", time_series=series
        )


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
