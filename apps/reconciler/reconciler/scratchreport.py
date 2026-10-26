"""What filled the volume a disk-limit eviction names, from the attempt's last reading (#2436).

WHY. kubelet evicts a pod past an emptyDir's `sizeLimit` with NO GRACE
PERIOD, so the worker cannot say what filled it at the moment it happens.
task_73850f24085447368991's last_error read only `evicted: EmptyDir volume
"tmp" exceeds 3Gi; evicted for disk 2 times: not retried again`, and the next
run filled `/tmp` the same way. The worker therefore reads its scratch
volumes on every heartbeat and puts the largest paths on the HEARTBEAT event;
`scratch_report` turns the newest such reading into the words the repair
appends to the eviction's reason, so the task's last_error and the attempt's
error -- which the console and swarm-mcp already show -- say what to move.

THE READING'S SHAPE is the worker's (`agent_worker/scratchusage.py`), restated
here as data rather than imported: the reconciler does not depend on the
worker package. One entry per volume, keyed by the kubelet volume name:

    detail["scratch"] = {"tmp": {"used_bytes": int, "limit_bytes": int,
                                 "top": [[absolute path, bytes], ...],
                                 "partial": bool}}

The sizes are written as `scratchusage.size_words` / `limit_words` write
them, so a report reads like the worker's own warning in the step log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from swarm_common.states import EventType

from .detect import EVERY_VOLUME, _one_line
from .model import as_datetime

_HEARTBEAT = EventType.HEARTBEAT.value

#: Largest paths named per report: the worker's `scratchusage.TOP`.
TOP = 5

GIB = 1024**3
MIB = 1024**2


def _bytes(value: Any) -> int | None:
    # bool is an int in Python; a `True` here is a corrupt field, not a size.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def size_words(n: int | float) -> str:
    """`2.1 GiB` or `30.0 MiB`, as `scratchusage.size_words` writes a size."""
    if n >= GIB:
        return f"{n / GIB:.1f} GiB"
    return f"{n / MIB:.1f} MiB"


def limit_words(n: int) -> str:
    """`3 GiB`, as `scratchusage.limit_words` writes a limit."""
    if n % GIB == 0:
        return f"{n // GIB} GiB"
    return size_words(n)


def format_top(entry: dict[str, Any], top: int = TOP) -> str:
    """One volume's largest paths as `path 2.1 GiB, path 0.7 GiB`: `scratchusage.format_top`."""
    pairs = []
    for item in entry.get("top") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        size = _bytes(item[1])
        if size is None:
            continue
        pairs.append(f"{item[0]} {size_words(size)}")
        if len(pairs) >= top:
            break
    return ", ".join(pairs)


def _fullness(entry: dict[str, Any]) -> float:
    used = _bytes(entry.get("used_bytes")) or 0
    limit = _bytes(entry.get("limit_bytes")) or 0
    return used / limit if limit else 0.0


def scratch_report(
    events: Iterable[dict[str, Any]],
    *,
    attempt_id: str,
    generation: int | None,
    volume: str,
) -> str | None:
    """`largest at 10:41:12Z: /tmp/pgdata 2.1 GiB, ... (volume "tmp" 2.9 GiB of 3 GiB)`, or None.

    Reads only HEARTBEAT events of this attempt at this generation, filtered
    as `progress.assess` filters them: a new attempt never inherits the
    readings of the one it replaced. The NEWEST one that carries a scratch
    reading is reported; there is no merging, because an older reading can
    only be smaller or name paths since deleted. `volume` is the kubelet
    volume name the eviction named, or `EVERY_VOLUME` for a pod-level or
    container-level limit, which reports the fullest volume of the reading.
    `(partial)` marks a reading whose bounded walk stopped early: its sizes
    are a floor. None with no reading, or no paths for that volume.
    """
    newest: tuple[datetime, dict[str, Any]] | None = None
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
        reading = detail.get("scratch")
        if not isinstance(reading, dict) or not reading:
            continue
        if newest is None or at > newest[0]:
            newest = (at, reading)
    if newest is None:
        return None
    at, reading = newest
    entries = {name: entry for name, entry in reading.items() if isinstance(entry, dict)}
    if volume == EVERY_VOLUME:
        if not entries:
            return None
        name = max(entries, key=lambda key: _fullness(entries[key]))
    else:
        name = volume
    entry = entries.get(name)
    if entry is None:
        return None
    paths = format_top(entry)
    if not paths:
        return None
    used, limit = _bytes(entry.get("used_bytes")), _bytes(entry.get("limit_bytes"))
    use = f'volume "{name}"'
    if used is not None and limit:
        use += f" {size_words(used)} of {limit_words(limit)}"
    partial = " (partial)" if entry.get("partial") is True else ""
    when = at.astimezone(timezone.utc).strftime("%H:%M:%SZ")
    return _one_line(f"largest at {when}: {paths} ({use}){partial}")
