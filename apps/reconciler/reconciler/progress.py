"""What "progress" means for a running attempt, from what the worker already writes.

The owner's requirement (2026-09-24, attached to keeping
`cluster-autoscaler.kubernetes.io/safe-to-evict=false` on browser pods): evict a
browser pod that is stuck for some time WITHOUT PROGRESS. A pod that may not be
evicted by the autoscaler holds its node for as long as it runs, so a wedged
one has to be found by something else, and this is that something.

HEARTBEATING IS NOT PROGRESS. The worker heartbeats from its own supervision
loop (`agent_worker.lifecycle._run_child_supervised`), which keeps running
whatever the runner child is doing -- a Chromium wedged on a page that never
answers heartbeats exactly as well as one that is working. So the lease's
`heartbeat_at` proves the WORKER is alive and nothing more.

WHAT THE WORKER WRITES WHILE AN ATTEMPT RUNS, surveyed on 2026-09-24:

* per heartbeat (every 30s): the lease's `heartbeat_at` -- liveness only;
* every fifth heartbeat (every 150s): a `heartbeat` EVENT carrying
  `cpu_seconds`, the attempt's cumulative CPU, measured from the container's
  cgroup or the runner's process tree (`agent_worker.metrics`), and None when
  it could not be measured;
* every checkpoint (every 120s, on a timer): a `checkpoint_completed` event
  carrying `size_bytes`, the gzipped archive of `work/`, plus
  `task.latest_checkpoint` and `task.updated_at`;
* per runner STEP: nothing. The browser runner reports its actions only in
  `result.json`, when it exits;
* log activity: only in GCS (`live/stdout.tail.log`), and the browser runner
  prints nothing to stdout until it exits.

So of everything written, two things move when an agent does work and stand
still when it is wedged:

1. **CPU.** Loading or rendering a page costs whole CPU-seconds; a browser
   blocked on something that never arrives costs almost none, and the worker's
   own polling, heartbeating and checkpointing sit far below the floor
   (`ReconcilerConfig.stuck_cpu_floor_cores`). An interval between two
   heartbeat events whose CPU rate reaches the floor is progress.
2. **The work tree.** The checkpoint is taken on a timer whether or not
   anything changed, so a checkpoint HAPPENING is not progress. But gzip of an
   unchanged tar is byte-for-byte the same length, so a checkpoint whose
   `size_bytes` differs from the one before it means `work/` changed. That is
   the signal for agents that edit files. The browser runner writes to
   `artifacts/`, which is not checkpointed, so for browser work this signal
   rarely moves -- which is exactly why CPU has to be the other half. Judging
   browser work by the work tree alone would evict every healthy browser run
   that lasted longer than the threshold.

Neither `checkpoint_completed` happening, nor `task.updated_at` moving (every
checkpoint touches it), nor the `heartbeat` event's `elapsed_seconds` or
`checkpoints` counters (both timers) is progress.

THE VERDICT ACTS ONLY ON POSITIVE EVIDENCE. Quiet is not assumed from silence.
A span counts as quiet only when both signals were MEASURED across all of it
-- CPU samples and checkpoints each no further apart than
`stuck_evidence_max_gap_seconds` from its start to now -- and every measurement
showed nothing. An attempt whose CPU could not be measured, or whose events
stopped arriving, is "not judged", never "stuck": the reconciler's standing rule
is that a component which cannot see must not act, and that is the rule here.

WHAT THIS DOES NOT CATCH, stated so nobody relies on it: a browser spinning at
full CPU on a page that never finishes reads as progressing. That attempt is
still bounded by the worker's own `timeout_seconds` (5400s for `browser`), as
every attempt was before this rule existed.

Pure: no I/O. `repair.Reconciler._read_progress` reads the events and hands
them here; `detect.detect_stuck_executions` turns the verdict into a finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from swarm_common.states import EventType

from .model import as_datetime

#: Event types, IMPORTED as the frozen contract spells them, never restated.
_HEARTBEAT = EventType.HEARTBEAT.value
_CHECKPOINT = EventType.CHECKPOINT_COMPLETED.value
#: Moments the attempt can be said to have begun working. The clock starts at
#: the latest of these, so a restore or a slow start never counts as quiet.
_STARTS = frozenset(
    {
        EventType.STARTING.value,
        EventType.RUNNING.value,
        EventType.CHECKPOINT_RESTORED.value,
    }
)


@dataclass(frozen=True)
class _Sample:
    at: datetime
    value: float
    source: str | None = None


@dataclass(frozen=True)
class Assessment:
    """The progress verdict on one attempt."""

    attempt_id: str
    #: True when the evidence was complete enough to decide either way.
    judged: bool
    #: True only when judged AND quiet for at least the threshold.
    stuck: bool
    #: Why it could not be judged. None when it was.
    unjudged_because: str | None = None
    last_progress_at: datetime | None = None
    #: What the last progress was: "attempt_started", "cpu", "workspace_changed",
    #: or "cpu_unmeasurable" (two samples that cannot be compared, which is
    #: counted as progress because it cannot be ruled out).
    last_progress_signal: str | None = None
    quiet_seconds: float | None = None
    #: Over the quiet span: CPU samples, their mean rate, and checkpoints.
    cpu_samples: int = 0
    mean_cpu_cores: float | None = None
    checkpoints: int = 0
    stuck_after_seconds: int = 0
    cpu_floor_cores: float = 0.0

    def reason(self) -> str:
        """One line for the finding, the pass report and the task's events."""
        mean = f"{self.mean_cpu_cores:.3f}" if self.mean_cpu_cores is not None else "unknown"
        since = self.last_progress_at.isoformat() if self.last_progress_at else "unknown"
        return (
            f"no progress for {self.quiet_seconds or 0:.0f}s "
            f"(threshold {self.stuck_after_seconds}s) while the lease kept heartbeating: "
            f"mean CPU {mean} cores across {self.cpu_samples} measurements, under the "
            f"{self.cpu_floor_cores} floor; work tree unchanged across "
            f"{self.checkpoints} checkpoints; last progress was "
            f"{self.last_progress_signal} at {since}"
        )

    def as_detail(self) -> dict[str, Any]:
        return {
            "quiet_seconds": round(self.quiet_seconds, 1) if self.quiet_seconds is not None else None,
            "stuck_after_seconds": self.stuck_after_seconds,
            "last_progress_at": self.last_progress_at.isoformat() if self.last_progress_at else None,
            "last_progress_signal": self.last_progress_signal,
            "cpu_samples": self.cpu_samples,
            "mean_cpu_cores": round(self.mean_cpu_cores, 4) if self.mean_cpu_cores is not None else None,
            "cpu_floor_cores": self.cpu_floor_cores,
            "checkpoints": self.checkpoints,
        }


def _number(value: Any) -> float | None:
    # bool is an int in Python; a `True` here is a corrupt field, not a reading.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gap(times: list[datetime], start: datetime, end: datetime, max_gap: float) -> str | None:
    """Why `times` does NOT cover [start, end] in steps of at most `max_gap`.

    None when it does. The first measurement must come within `max_gap` of the
    span's start, the last within `max_gap` of its end, and no two consecutive
    ones may be further apart than that.
    """
    if not times:
        return "no measurement at all"
    points = sorted(times)
    lead = (points[0] - start).total_seconds()
    if lead > max_gap:
        return f"first measurement {lead:.0f}s after the span began"
    for earlier, later in zip(points, points[1:]):
        hole = (later - earlier).total_seconds()
        if hole > max_gap:
            return f"a {hole:.0f}s hole between measurements at {earlier.isoformat()}"
    tail = (end - points[-1]).total_seconds()
    if tail > max_gap:
        return f"last measurement {tail:.0f}s ago"
    return None


def assess(
    events: Iterable[dict[str, Any]],
    *,
    attempt_id: str,
    generation: int,
    started_at: datetime | None,
    now: datetime,
    stuck_after_seconds: int,
    cpu_floor_cores: float,
    max_gap_seconds: int,
) -> Assessment:
    """Decide whether one attempt has gone without progress for too long.

    `events` may hold the whole task's stream: only events of THIS attempt at
    THIS generation are read. That filter is the difference between judging an
    attempt and judging its task -- a new attempt must never inherit the quiet
    history of the one it replaced, or the first pass after a re-admission
    would evict the replacement for its predecessor's silence.
    """
    base = dict(
        attempt_id=attempt_id,
        stuck_after_seconds=stuck_after_seconds,
        cpu_floor_cores=cpu_floor_cores,
    )
    mine: list[tuple[datetime, str, dict[str, Any]]] = []
    for event in events:
        if event.get("attempt_id") != attempt_id:
            continue
        stamped = event.get("generation")
        if stamped is not None and _number(stamped) != float(generation):
            continue
        at = as_datetime(event.get("at"))
        if at is None:
            continue
        detail = event.get("detail")
        mine.append((at, str(event.get("type") or ""), detail if isinstance(detail, dict) else {}))
    mine.sort(key=lambda item: item[0])

    starts = [at for at, kind, _ in mine if kind in _STARTS]
    if started_at is not None:
        starts.append(started_at)
    if not starts:
        return Assessment(
            judged=False, stuck=False,
            unjudged_because="no record of when this attempt started", **base,
        )
    last_progress, signal = max(starts), "attempt_started"

    def advance(at: datetime, why: str) -> None:
        nonlocal last_progress, signal
        if at >= last_progress:
            last_progress, signal = at, why

    # -- CPU ------------------------------------------------------------
    cpu = [
        _Sample(at, value, detail.get("cpu_source"))
        for at, kind, detail in mine
        if kind == _HEARTBEAT and (value := _number(detail.get("cpu_seconds"))) is not None
    ]
    for earlier, later in zip(cpu, cpu[1:]):
        span = (later.at - earlier.at).total_seconds()
        if span <= 0:
            continue
        if later.source != earlier.source or later.value < earlier.value:
            # A total that went backwards, or two totals from different
            # meters, cannot be turned into a rate. Unknown is not quiet.
            advance(later.at, "cpu_unmeasurable")
            continue
        if False and (later.value - earlier.value) / span >= cpu_floor_cores:  # MUTATION M1
            advance(later.at, "cpu")

    # -- work tree ------------------------------------------------------
    sizes = [
        _Sample(at, value)
        for at, kind, detail in mine
        if kind == _CHECKPOINT and (value := _number(detail.get("size_bytes"))) is not None
    ]
    for earlier, later in zip(sizes, sizes[1:]):
        if False and later.value != earlier.value:  # MUTATION M4
            advance(later.at, "workspace_changed")

    quiet = (now - last_progress).total_seconds()
    measured = dict(
        last_progress_at=last_progress,
        last_progress_signal=signal,
        quiet_seconds=quiet,
        **base,
    )
    if quiet < stuck_after_seconds:
        return Assessment(judged=True, stuck=False, **measured)

    # -- is the quiet PROVEN? --------------------------------------------
    # The sample that closed the last progress interval is the quiet span's
    # left edge, so it is included; everything after it is inside the span.
    quiet_cpu = [s for s in cpu if s.at >= last_progress]
    holes = None  # MUTATION M3
    if holes is not None:
        return Assessment(
            judged=False, stuck=False,
            unjudged_because=f"CPU was not measured across the quiet span: {holes}",
            **measured,
        )
    quiet_sizes = [s for s in sizes if s.at >= last_progress]
    holes = None  # MUTATION M3
    if holes is not None:
        return Assessment(
            judged=False, stuck=False,
            unjudged_because=f"the work tree was not checkpointed across the quiet span: {holes}",
            **measured,
        )
    mean: float | None = None
    if len(quiet_cpu) >= 2:
        width = (quiet_cpu[-1].at - quiet_cpu[0].at).total_seconds()
        if width > 0:
            mean = (quiet_cpu[-1].value - quiet_cpu[0].value) / width
    return Assessment(
        judged=True,
        stuck=True,
        cpu_samples=len(quiet_cpu),
        mean_cpu_cores=mean,
        checkpoints=len(quiet_sizes),
        **measured,
    )
