"""What fills the pod's local scratch volumes, read on every heartbeat (#2436).

WHY. A GKE step's local disk is emptyDirs with a `sizeLimit` (`/workspace`,
literal `/tmp`, the worker's HOME; `scheduler.dispatch.GKE_DISK`). kubelet
measures each one du-style and, the moment one passes its limit, evicts the
pod with NO GRACE PERIOD: no SIGTERM the worker could answer, so nothing it
says at that moment is ever written. task_73850f24085447368991 was evicted
for `/tmp` past 3 GiB and its last_error said only that, with nothing about
what had filled it -- so the next run filled it the same way.

So the worker reads the volumes BEFORE the eviction, on every heartbeat, and
puts the reading on the HEARTBEAT event (`detail["scratch"]`): each volume's
used bytes, its limit and its five largest paths. The reconciler, repairing
the eviction, reports the newest reading in the task's last_error (the
reconciler restates this shape as data rather than import it). Past two
thirds of a limit the worker also warns once and writes an out-of-cadence
reading (`lifecycle.Worker._heartbeat`), so the reading the reconciler finds
is at most one beat older than the eviction.

THE SHAPE, one entry per volume, keyed by the kubelet volume name -- the name
kubelet's `Usage of EmptyDir volume "<name>"` message uses:

    {"tmp": {"used_bytes": int, "limit_bytes": int,
             "top": [[absolute path, bytes], ...], "partial": bool}}

BYTES ARE ALLOCATED BLOCKS (`st_blocks * 512`), as kubelet's du-style
accounting counts them: a sparse file costs what it allocated, not its
length. Entries are `lstat`ed and no symlink is followed (a link costs its own
blocks, and its target is on whichever volume holds it); a hard link counts
once. An entry that cannot be read is skipped, never fatal.

THE WALK IS BOUNDED, by an entry count and a wall time per volume, because it
runs on the loop that heartbeats the lease. A walk that stopped early says so
(`partial`), and its bytes are a floor.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable, Iterable

#: The variable naming the pod's local scratch volumes. MIRRORED from
#: `scheduler.dispatch.SCRATCH_VOLUMES_ENV`, which sets it on GKE only, as
#: `workspace=/workspace:5,tmp=/tmp:3,home=/home/swarm:2` -- the kubelet
#: volume name, its mountPath, its sizeLimit in GiB. Cloud Run sets nothing:
#: its disk is the memory-backed workspace, which no kubelet evicts.
SCRATCH_VOLUMES_ENV = "SWARM_SCRATCH_VOLUMES"

GIB = 1024**3
MIB = 1024**2

#: Largest paths reported per volume.
TOP = 5
#: Entries one volume's walk reads at most. 20,000 `lstat`s is tens of
#: milliseconds; a `node_modules` tree is more than that, and its walk says
#: `partial` rather than holding the heartbeat.
MAX_ENTRIES = 20_000
#: Wall time one volume's walk takes at most, for the same reason.
MAX_SECONDS = 0.25
#: How deep the walk looks for one directory that holds nearly everything
#: (`DESCEND_SHARE`) -- the `/workspace/<attempt>` wrapper -- before it lists
#: the largest paths, so the report names what is inside the wrapper rather
#: than the wrapper alone.
MAX_DESCENT = 4
DESCEND_SHARE = 0.9
#: A reported path is cut to this many characters, AFTER it is redacted, so a
#: cut can never split a secret out of the redactor's reach.
PATH_CHARS = 120
#: What the `scratch` reading may add to one HEARTBEAT event, serialised. The
#: event's series is about 2 KiB and the whole detail is held under
#: `lifecycle.HEARTBEAT_DETAIL_MAX_BYTES` (4 KiB); past this the smallest
#: paths of the emptiest volumes go first (`bounded`).
SCRATCH_DETAIL_MAX_BYTES = 1536

_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def parse_volumes(value: str | None) -> list[tuple[str, Path, int]]:
    """`SWARM_SCRATCH_VOLUMES` as `(name, mount, limit_bytes)`, or [] when absent or malformed.

    One malformed entry voids the whole value: a half-read list would report
    some volumes and silently not others.
    """
    if not value or not value.strip():
        return []
    out: list[tuple[str, Path, int]] = []
    for item in value.split(","):
        name, eq, rest = item.strip().partition("=")
        mount, colon, size = rest.rpartition(":")
        if not eq or not colon or not _NAME.match(name) or not mount.startswith("/"):
            return []
        try:
            gib = float(size)
        except ValueError:
            return []
        if not 0 < gib < float("inf"):
            return []
        out.append((name, Path(mount), int(gib * GIB)))
    return out


def warn_threshold(limit_bytes: int) -> int:
    """Two thirds of a limit: 2 GiB of `/tmp`'s 3 GiB, the issue's "about 2 GiB"."""
    return limit_bytes * 2 // 3


def size_words(n: int | float) -> str:
    """`2.1 GiB` or `30.0 MiB`: one decimal, GiB from 1 GiB up."""
    if n >= GIB:
        return f"{n / GIB:.1f} GiB"
    return f"{n / MIB:.1f} MiB"


def limit_words(n: int) -> str:
    """A limit as the manifest writes it: `3 GiB`, or one decimal when not whole."""
    if n % GIB == 0:
        return f"{n // GIB} GiB"
    return size_words(n)


def format_top(entry: dict[str, Any]) -> str:
    """One volume's largest paths as `path 2.1 GiB, path 0.7 GiB`; '' with none."""
    pairs = []
    for item in entry.get("top") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append(f"{item[0]} {size_words(item[1])}")
    return ", ".join(pairs)


def sample(
    volumes: Iterable[tuple[str, Path, int]],
    *,
    top: int = TOP,
    max_entries: int = MAX_ENTRIES,
    max_seconds: float = MAX_SECONDS,
    scrub: Callable[[str], str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """One reading of every volume that exists here: see the module docstring.

    `scrub` is the worker's redactor (`StructuredLogger.scrub_text`, over
    `agent_worker.redact`); a path is redacted whole, then cut.
    """
    out: dict[str, Any] = {}
    for name, mount, limit in volumes:
        if not mount.is_dir():
            continue
        used, sizes, children, partial = _walk(mount, max_entries, max_seconds, clock)
        node = str(mount)
        for _ in range(MAX_DESCENT):
            kids = children.get(node)
            if not kids:
                break
            biggest = max(kids, key=lambda p: sizes.get(p, 0))
            if biggest in children and sizes.get(node, 0) > 0 and (
                sizes.get(biggest, 0) >= DESCEND_SHARE * sizes[node]
            ):
                node = biggest
            else:
                break
        ranked = sorted(children.get(node, []), key=lambda p: (-sizes.get(p, 0), p))[:top]
        out[name] = {
            "used_bytes": used,
            "limit_bytes": limit,
            "top": [[_shown(path, scrub), sizes.get(path, 0)] for path in ranked],
            "partial": partial,
        }
    return bounded(out)


def bounded(reading: dict[str, Any], max_bytes: int = SCRATCH_DETAIL_MAX_BYTES) -> dict[str, Any]:
    """`reading` within `max_bytes` serialised: the emptiest volume's smallest path goes first.

    The fullest volume is the one an eviction names, so its paths are kept longest.
    """
    while len(json.dumps(reading).encode()) > max_bytes:
        trimmable = [entry for entry in reading.values() if entry["top"]]
        if not trimmable:
            break
        emptiest = min(trimmable, key=lambda e: e["used_bytes"] / max(e["limit_bytes"], 1))
        emptiest["top"].pop()
    return reading


def _shown(path: str, scrub: Callable[[str], str] | None) -> str:
    text = scrub(path) if scrub is not None else path
    return text[:PATH_CHARS]


def _walk(
    mount: Path, max_entries: int, max_seconds: float, clock: Callable[[], float]
) -> tuple[int, dict[str, int], dict[str, list[str]], bool]:
    """Allocated bytes under `mount`: the total, per path down to `MAX_DESCENT + 1`
    levels, each listed directory's children, and whether the walk stopped early."""
    deadline = clock() + max_seconds
    root = str(mount)
    sizes: dict[str, int] = {root: 0}
    children: dict[str, list[str]] = {root: []}
    seen: set[tuple[int, int]] = set()
    used = 0
    try:
        used = sizes[root] = os.lstat(root).st_blocks * 512
    except OSError:
        pass
    # (directory, its depth below the mount, the tracked paths whose totals it adds to)
    stack: list[tuple[str, int, tuple[str, ...]]] = [(root, 0, (root,))]
    entries = 0
    while stack:
        directory, depth, tracked = stack.pop()
        try:
            with os.scandir(directory) as listing:
                items = list(listing)
        except OSError:
            continue
        for item in items:
            entries += 1
            if entries > max_entries or (entries % 256 == 0 and clock() > deadline):
                return used, sizes, children, True
            try:
                st = item.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if not is_dir and st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            size = st.st_blocks * 512
            used += size
            for path in tracked:
                sizes[path] += size
            mine = tracked
            if depth + 1 <= MAX_DESCENT + 1:
                sizes[item.path] = size
                if depth <= MAX_DESCENT:
                    children.setdefault(directory, []).append(item.path)
                mine = tracked + (item.path,)
            if is_dir:
                if depth + 1 <= MAX_DESCENT:
                    children.setdefault(item.path, [])
                stack.append((item.path, depth + 1, mine))
    return used, sizes, children, False
