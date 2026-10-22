"""Whether an attempt ran out of memory at its OWN limit, and the words that say so (#1926).

WHAT HAPPENED. task_021ff60c26f5440eb9c8, a claude-code step at 8 GiB, ran
three attempts over about two hours on 2026-10-21/22. Gen 1 and gen 3 were
OOM-killed (exit 137) and requeued at the same size; gen 5 read 8.58-8.59 GB
of its 8,589,934,592-byte limit on every heartbeat sample from t=1900 s to
2220 s, then went silent and was reclaimed as "lease silent for 102s". The
task ended FAILED lost_worker, and nothing it carried said memory.

THE RULE (owner, 2026-10-22, "Name it, stop retrying it"). An attempt whose
execution ended OOM-killed, or that is reclaimed as a dead or silent worker
whose last heartbeats sat at its memory limit, FAILS its task after that
first attempt, whatever `max_attempts` says. The retry is the same step at
the same size; it meets the same limit. This is the disk-eviction precedent
(`model.count_disk_eviction`) with no retry at all, and like it only ever
ends a task sooner. The end cause stays the frozen LOST_WORKER: the worker
was lost, to its own limit, and the words below say which.

ONLY THE TASK'S OWN LIMIT. A kubelet eviction -- a node short of memory or
disk -- is the node's, not this step's size, and keeps today's retry: a
termination that names an eviction is never an OOM here, whatever else it
says. Exit 137 alone is a SIGKILL, which the reconciler's own kill also is:
only the backend's `OOMKilled` (GKE's reason, or the prefix
`CloudRunBackend.termination` puts on Cloud Run's memory sentence) counts.

Pure: no I/O. `repair.Reconciler._out_of_memory` reads the termination this
pass already read and, for a silent worker only, the attempt's events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from swarm_common.states import EventType

from .model import as_datetime

#: A sample at or above this share of `memory_limit_bytes` is AT the limit.
#: 98%, because a container pressed against cgroup `memory.max` does not read
#: exactly the limit: the kernel reclaims page cache to stay under it, so the
#: reading hovers just below. task_021ff60c's gen 5 read 8.58-8.59 GB of
#: 8.59 GB, 99.9%. 2% of 8 GiB is ~164 MiB of headroom a healthy step would
#: have to stay inside for 30 s straight, AND then lose its lease, to be
#: misread -- the rule only ever applies to a worker already reclaimed as
#: silent or dead.
OOM_STALL_SHARE = 0.98

#: How many of the newest samples must ALL sit at the limit. Six 5-second
#: points (`agent_worker.metrics.POINT_SECONDS`) are 30 s: a spike the kernel
#: reclaimed from is not a stall, and gen 5 sat there for 320 s. Fewer
#: readable samples than this is no evidence, and no evidence keeps the retry.
OOM_STALL_SAMPLES = 6

#: The two ways an attempt ran out of memory, as the `failed` event's
#: `out_of_memory` names them.
OOM_KILLED = "oom_killed"
STALLED_AT_LIMIT = "stalled_at_limit"

#: How every out-of-memory reason begins: what the console reads to say "it
#: ran out of memory" over the frozen `lost_worker` (`AgentDetail.tsx`).
OUT_OF_MEMORY = "ran out of memory"

_HEARTBEAT = EventType.HEARTBEAT.value

#: GKE's words (`GkeBackend.termination`): "OOMKilled: the container passed
#: its 8 GiB memory limit".
_GKE_LIMIT = re.compile(r"passed its (.+?) memory limit")
#: Cloud Run's: "Memory limit of 8192 MiB exceeded with 8243 MiB used."
_CLOUD_RUN_LIMIT = re.compile(
    r"memory limit of (\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|TiB|Ki|Mi|Gi|Ti|K|M|G|T)?B?\b",
    re.IGNORECASE,
)
_BINARY = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


@dataclass(frozen=True)
class OutOfMemory:
    """An attempt that ran out of memory: how, its words, and its limit when known."""

    how: str
    words: str
    limit_bytes: int | None = None


def size_words(size: float) -> str:
    """Bytes as a person reads a memory limit: 8589934592 -> "8 GiB", 1.5 GiB -> "1.5 GiB"."""
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2)):
        if size >= scale:
            value = round(size / scale, 1)
            return f"{value:g} {unit}"
    return f"{int(size)} bytes"


def _limit_from_detail(detail: str) -> str | None:
    matched = _GKE_LIMIT.search(detail)
    if matched:
        return matched.group(1).strip()
    matched = _CLOUD_RUN_LIMIT.search(detail)
    if matched:
        number, unit = matched.groups()
        scale = _BINARY.get((unit or "")[:1].lower(), 1)
        return size_words(float(number) * scale)
    return None


def _limit_words(limit: str | None) -> str:
    return f"the {limit} limit was reached" if limit else "its memory limit was reached"


def out_of_memory_killed(termination: Any) -> OutOfMemory | None:
    """The OOM kill this pass's termination read names, or None.

    None for no read, an eviction (the node's pressure, not this step's
    limit), and anything the backend did not call `OOMKilled` -- exit 137
    alone included.
    """
    if termination is None or getattr(termination, "eviction", None):
        return None
    detail = " ".join(str(getattr(termination, "detail", "") or "").split())
    if "OOMKilled" not in detail:
        return None
    code = getattr(termination, "exit_code", None)
    exited = f", exit {code}" if isinstance(code, int) and not isinstance(code, bool) else ""
    limit = _limit_from_detail(detail)
    return OutOfMemory(
        how=OOM_KILLED,
        words=f"{OUT_OF_MEMORY}: {_limit_words(limit)} (OOMKilled{exited})",
    )


def _positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def stalled_at_memory_limit(
    events: Iterable[dict[str, Any]], *, attempt_id: str, generation: int | None
) -> OutOfMemory | None:
    """Whether this attempt's LAST heartbeats sat at its memory limit, or None.

    Reads only HEARTBEAT events of this attempt at this generation: a new
    attempt never inherits its predecessor's memory. The newest heartbeat's
    `memory_limit_bytes` is the limit; the newest `OOM_STALL_SAMPLES` points
    of the `series` (newest heartbeat first) must ALL read at least
    `OOM_STALL_SHARE` of it, and a missing reading is not at the limit. A
    worker whose newest heartbeat carries no `mem` point at all (written
    before #1689) is judged by that heartbeat's `peak_rss_bytes` reaching the
    limit instead. No limit, or too few readings, is no evidence: None.
    """
    mine: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if event.get("type") != _HEARTBEAT or event.get("attempt_id") != attempt_id:
            continue
        stamped = event.get("generation")
        if generation is not None and stamped is not None and stamped != generation:
            continue
        at = as_datetime(event.get("at"))
        detail = event.get("detail")
        if at is None or not isinstance(detail, dict):
            continue
        mine.append((at, detail))
    if not mine:
        return None
    mine.sort(key=lambda item: item[0], reverse=True)
    newest = mine[0][1]
    limit = _positive(newest.get("memory_limit_bytes"))
    if limit is None:
        return None

    samples: list[Any] = []
    for _, detail in mine:
        series = detail.get("series")
        points = [p for p in series if isinstance(p, dict)] if isinstance(series, list) else []
        points.sort(key=lambda p: p.get("t") if isinstance(p.get("t"), (int, float)) else -1.0)
        samples.extend(p.get("mem") for p in reversed(points))
        if len(samples) >= OOM_STALL_SAMPLES:
            break

    newest_points = newest.get("series")
    if not isinstance(newest_points, list) or not any(
        isinstance(p, dict) and p.get("mem") is not None for p in newest_points
    ):
        peak = _positive(newest.get("peak_rss_bytes"))
        at_limit = peak is not None and peak >= limit
    else:
        last = samples[:OOM_STALL_SAMPLES]
        at_limit = len(last) == OOM_STALL_SAMPLES and all(
            _positive(value) is not None and value >= OOM_STALL_SHARE * limit for value in last
        )
    if not at_limit:
        return None
    return OutOfMemory(
        how=STALLED_AT_LIMIT,
        words=(
            f"{OUT_OF_MEMORY}: {_limit_words(size_words(limit))} "
            "(stalled at the limit; its lease went silent)"
        ),
        limit_bytes=limit,
    )
