"""Runner sizes as a versioned platform setting (SIZES-API, part of #1689).

docs/design/runner-sizes.md §4.3-§4.5. The owner, 2026-10-20: a platform
admin changes a class's CPU request, CPU limit or memory in the console --
until now a frozen-contract change, a pull request and a release -- sees the
units and the cost before saving, and can see who changed what, when and why,
and restore an earlier version (§7 Q4).

TWO SHAPES, in database `swarm`, written only here:

    control/runner_sizes             the pointer: current_version, updated_at, updated_by
    runner_size_versions/{000003}    one IMMUTABLE document per version: the whole
                                     table, who, when, why, what changed

Every version is the whole table, never a patch, so a reader needs one
document and a restore is a copy. A version document is never updated or
deleted: the audit trail is the collection. No pointer is version 0, the code
defaults (`swarm_common.profiles.default_sizes`).

WHAT STAYS FROZEN. Class names, the defaults, the bounds and the unit formula
are `swarm_common.profiles` (RS-1 to RS-3): this module never restates one.
Every table written is judged by the frozen `check_size` first, and every
table READ is judged again, so a document written straight to Firestore with
a typo reads as the code defaults (and logs at ERROR) rather than sizing a pod.

WHAT IS NOT EDITED. `units` is derived (`units_for`) and refused in a body;
`disk_gib` is shown, never edited (owner Q5). A memory cut below the class's
7-day peak needs `confirm_below_recent_peak` (owner Q6): the console asks for
the class name typed before it sends it.

WHO FOLLOWS. Only GKE profiles: a Cloud Run profile runs at the code default
whatever the version says, because Cloud Run fixes resources on the Job
(`effective_size`, owner Q3). The recent use a cut is judged against is
therefore read from GKE attempts only.

Invariants: this writes no lease, pool or task (1-3). The scheduler pins the
version an agent is admitted at (SIZES-SCHED-2), so a resize never touches a
running agent. Nothing here is accepted from a non-admin caller, and no caller
of the task API can name a size at all (10).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import Attempt
from swarm_common.profiles import (
    CLASS_ORDER,
    CPU_BURST_MAX_RATIO,
    CPU_REQUEST_MAX,
    CPU_REQUEST_MIN,
    CPU_REQUEST_STEP,
    LIVE_DISPLAY_MIN,
    LIVE_WORKER_MIN,
    MEMORY_GIB_MAX,
    MEMORY_GIB_MIN,
    MEMORY_PER_CPU_MAX,
    MEMORY_PER_CPU_MIN,
    RUNNER_PROFILES,
    Backend,
    LiveShare,
    ResourceSize,
    SizeRefusal,
    check_size,
    default_sizes,
    resolve_backend,
)
from swarm_common.states import EventType

from .codec import attempt_from_dict
from .errors import Conflict, NotFound, ValidationFailed
from .store import ATTEMPTS, CONTROL, EVENTS, POOLS, TASKS
from .utilisation import points_of

log = logging.getLogger(__name__)

POINTER_DOC = "runner_sizes"
VERSIONS = "runner_size_versions"

#: The fields an admin may set on a class and on each live-split container.
#: `disk_gib` is shown, not edited (owner Q5); `units` is derived.
EDITABLE = ("cpu_request", "cpu_limit", "memory_gib")
LIVE_ROLES = ("worker", "display")

#: Autopilot general-purpose list prices, us-central1, as
#: docs/browser-startup-speed.md records them (confirmed 2026-10-13 only
#: through secondary sources; that doc says so). Autopilot bills REQUESTS, so
#: a CPU limit above the request prices at nothing (design §2.2.4).
PRICES = {"vcpu_hour": 0.0445, "gib_hour": 0.0049225, "source": "docs list price, 2026-10-13"}

#: The window a cut is judged against, and the preview's readings (owner Q6).
RECENT_DAYS = 7
#: Attempts read for that window, newest first, across every class. At the
#: 2026-10-20 rate (41 implement attempts a day) about three weeks' worth.
RECENT_ATTEMPTS_LIMIT = 1000
#: Attempts per class whose LIVE-UTIL points are read (one HEARTBEAT query per
#: task). 30 is the design's "enough data" threshold (§4.9): enough for the
#: shares, and it bounds a preview to 30 event reads per class.
SERIES_ATTEMPTS = 30
SERIES_EVENTS_LIMIT = 2000
#: §4.9's readings: a sample under 0.5 core is idle; within 5% of the
#: enforced limit is "at the limit"; a run of 40 minutes or more is long.
IDLE_CORES = 0.5
AT_LIMIT_FRACTION = 0.95
LONG_RUN_MINUTES = 40.0
#: The platform-wide ceilings a resize moves, named on the preview: `global`,
#: GKE's backend pool and each `resource:<class>`. A `tenant:` or `provider:`
#: pool is one per tenant and is read on Pool limits; Cloud Run's backend pool
#: admits only agents at the code defaults, which a resize does not move.
PREVIEW_POOLS = ("global", f"backend:{Backend.GKE_AUTOPILOT.value}")
VERSIONS_PAGE_MAX = 200

GIB = 1024 ** 3


class StaleBaseVersion(Conflict):
    """409: the body's `base_version` is not the current one (two admins at once)."""

    code = "stale_base_version"


class SizeOutOfBounds(ValidationFailed):
    """422: the table breaks a frozen bound; `detail.refusals` names every one."""

    code = "size_out_of_bounds"


class BelowRecentPeak(ValidationFailed):
    """422: a memory cut under the class's 7-day max peak, unconfirmed (owner Q6)."""

    code = "below_recent_peak"


# --------------------------------------------------------------------------
# The shapes
# --------------------------------------------------------------------------


def version_id(version: int) -> str:
    return f"{version:06d}"


def _clean(value: Any) -> Any:
    """A whole float stored as an int, so 4.0 and 4 are one value in a diff."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def _share_doc(share: LiveShare) -> dict[str, Any]:
    return {name: _clean(getattr(share, name)) for name in EDITABLE}


def size_doc(size: ResourceSize, *, units: bool = True) -> dict[str, Any]:
    """A size as stored and served. `units` is derived, stored so a reader of an
    old version sees what it charged without re-deriving."""
    out: dict[str, Any] = {
        "cpu_request": _clean(size.cpu_request),
        "cpu_limit": _clean(size.cpu_limit),
        "memory_gib": _clean(size.memory_gib),
        "disk_gib": _clean(size.disk_gib),
    }
    if units:
        out["units"] = size.units
    if size.live_split is not None:
        out["live_split"] = dict(zip(LIVE_ROLES, (_share_doc(s) for s in size.live_split)))
    return out


def table_doc(table: Mapping[str, ResourceSize]) -> dict[str, dict[str, Any]]:
    return {name: size_doc(table[name]) for name in CLASS_ORDER}


def _share_of(raw: Any) -> LiveShare | None:
    if not isinstance(raw, Mapping) or set(raw) - set(EDITABLE):
        return None
    if any(name not in raw for name in EDITABLE):
        return None
    return LiveShare(**{name: raw[name] for name in EDITABLE})


def size_of(raw: Any) -> ResourceSize | None:
    """A stored size, or None when its SHAPE is wrong. Its values are judged by
    `check_size` afterwards, which reports every bound instead of the first."""
    if not isinstance(raw, Mapping):
        return None
    fields = ("cpu_request", "cpu_limit", "memory_gib", "disk_gib")
    if any(name not in raw for name in fields):
        return None
    split = raw.get("live_split")
    live: tuple[LiveShare, LiveShare] | None = None
    if split is not None:
        if not isinstance(split, Mapping) or set(split) != set(LIVE_ROLES):
            return None
        shares = tuple(_share_of(split[role]) for role in LIVE_ROLES)
        if any(share is None for share in shares):
            return None
        live = (shares[0], shares[1])  # type: ignore[assignment]
    return ResourceSize(**{name: raw[name] for name in fields}, live_split=live)


def table_of(raw: Any) -> dict[str, ResourceSize] | None:
    """A stored version's whole table, a class it omits at its default; None
    when the shape is wrong or `check_size` refuses it."""
    if not isinstance(raw, Mapping) or set(raw) - set(CLASS_ORDER):
        return None
    defaults = default_sizes()
    table: dict[str, ResourceSize] = {}
    for name in CLASS_ORDER:
        if name not in raw:
            table[name] = defaults[name]
            continue
        size = size_of(raw[name])
        if size is None:
            return None
        table[name] = size
    if check_size(table):
        return None
    return table


@dataclass(frozen=True)
class SizeVersion:
    """The current setting as read.

    `version` is the pointer's (0: none written), what a write's `base_version`
    is compared with. `sizes` is the table in effect: the version's, or the code
    defaults when there is none or it is malformed, and then `in_effect` is 0.
    """

    version: int
    sizes: Mapping[str, ResourceSize]
    in_effect: int
    malformed: bool = False
    updated_at: Any = None
    updated_by: str | None = None


def _pointer_ref(db: Any) -> Any:
    return db.collection(CONTROL).document(POINTER_DOC)


def _version_ref(db: Any, version: int) -> Any:
    return db.collection(VERSIONS).document(version_id(version))


def _pointer_version(data: Mapping[str, Any] | None) -> int | None:
    """The pointer's version: 0 for no pointer, None for a malformed one."""
    if data is None:
        return 0
    value = data.get("current_version")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def current(db: Any) -> SizeVersion:
    """The current version, validated (two document reads at most)."""
    pointer = _pointer_ref(db).get()
    data = pointer.to_dict() if pointer.exists else None
    number = _pointer_version(data)
    defaults = default_sizes()
    stamp = {"updated_at": (data or {}).get("updated_at"), "updated_by": (data or {}).get("updated_by")}
    if number == 0:
        return SizeVersion(0, defaults, 0, **stamp)
    if number is None:
        log.error("runner sizes: control/%s has no usable current_version; serving the code defaults",
                  POINTER_DOC)
        return SizeVersion(0, defaults, 0, malformed=True, **stamp)
    snap = _version_ref(db, number).get()
    table = table_of((snap.to_dict() or {}).get("classes")) if snap.exists else None
    if table is None:
        log.error("runner sizes: version %d is missing or malformed; serving the code defaults", number)
        return SizeVersion(number, defaults, 0, malformed=True, **stamp)
    return SizeVersion(number, table, number, **stamp)


def current_or_defaults(db: Any | None) -> SizeVersion:
    """`current(db)`, or the code defaults (version 0) for a caller with no
    store: the offline smoke matrix builds `/v1/runtimes` by calling the route
    with a context that carries settings and no database
    (tests/integration/test_smoke_backend_matrix.py), and the catalogue it
    reads there is the code's."""
    if db is None:
        return SizeVersion(0, default_sizes(), 0)
    return current(db)


def current_sizes(db: Any) -> Mapping[str, ResourceSize]:
    """The table in effect, for a reader that needs only the sizes."""
    return current(db).sizes


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


def bounds() -> dict[str, Any]:
    """The frozen bounds, served so the console checks a keystroke against the
    same numbers `check_size` refuses with."""
    return {
        "cpu_request": {"min": CPU_REQUEST_MIN, "max": CPU_REQUEST_MAX, "step": CPU_REQUEST_STEP},
        "cpu_burst_max_ratio": CPU_BURST_MAX_RATIO,
        "memory_gib": {"min": MEMORY_GIB_MIN, "max": MEMORY_GIB_MAX},
        "memory_per_cpu": {"min": MEMORY_PER_CPU_MIN, "max": MEMORY_PER_CPU_MAX},
        "disk_gib": {"min": 1, "max": "memory_gib - 2", "editable": False},
        "live_split": {
            "worker_min_cpu": LIVE_WORKER_MIN[0], "worker_min_memory_gib": LIVE_WORKER_MIN[1],
            "display_min_cpu": LIVE_DISPLAY_MIN[0], "display_min_memory_gib": LIVE_DISPLAY_MIN[1],
        },
    }


def followers() -> dict[str, dict[str, list[str]]]:
    """Per class, the profiles that follow a resize (GKE) and the ones fixed at
    the code default (Cloud Run). Computed from the catalogue, so the page never
    restates which profile runs where. A control-plane profile is never run and
    is listed nowhere."""
    out: dict[str, dict[str, list[str]]] = {name: {"follows": [], "fixed": []} for name in CLASS_ORDER}
    for name, profile in sorted(RUNNER_PROFILES.items()):
        if profile.control_plane or profile.resource_class not in out:
            continue
        group = "follows" if resolve_backend(profile) is Backend.GKE_AUTOPILOT else "fixed"
        out[profile.resource_class][group].append(name)
    return out


def served(setting: SizeVersion) -> dict[str, Any]:
    """`GET /v1/admin/runner-sizes`, and every write's answer."""
    return {
        "current_version": setting.version,
        "in_effect_version": setting.in_effect,
        "malformed": setting.malformed,
        "classes": table_doc(setting.sizes),
        "defaults": table_doc(default_sizes()),
        "bounds": bounds(),
        "followers": followers(),
        "prices": dict(PRICES),
        "updated_at": setting.updated_at,
        "updated_by": setting.updated_by,
    }


def class_entry(name: str, size: ResourceSize, size_version: int) -> dict[str, Any]:
    """One class as `/v1/resource-classes` and `/v1/runtimes` serve it."""
    return {
        "name": name,
        # The REQUEST, kept under its old name for one release so a client
        # written before the limit existed reads the guaranteed number.
        "cpu": _clean(size.cpu_request),
        "cpu_request": _clean(size.cpu_request),
        # What it may burst to on GKE Autopilot when its node has idle
        # requested CPU; Cloud Run renders the request alone (invariant 7).
        "cpu_limit": _clean(size.cpu_limit),
        # Request AND limit: memory never bursts.
        "memory_gib": _clean(size.memory_gib),
        # A SLICE OF memory_gib, not capacity on top of it: the workspace is a
        # memory-backed tmpfs, because the Terraform google provider cannot
        # express Cloud Run's disk-backed empty_dir (`medium` accepts only "MEMORY").
        "disk_gib": _clean(size.disk_gib),
        # Weighted capacity units, which is what admission counts -- never a
        # number of agents.
        "units": size.units,
        # 0: the code defaults.
        "size_version": size_version,
    }


# --------------------------------------------------------------------------
# Applying a body
# --------------------------------------------------------------------------


def _refusal(name: str, field: str, value: Any, bound: str) -> SizeRefusal:
    shown = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return SizeRefusal(name, field, shown, bound)


def refusal_doc(refusal: SizeRefusal) -> dict[str, Any]:
    return {"class": refusal.resource_class, "field": refusal.field,
            "value": refusal.value, "bound": refusal.bound}


def _apply_split(name: str, size: ResourceSize, raw: Any, out: list[SizeRefusal]) -> ResourceSize:
    if size.live_split is None:
        out.append(_refusal(name, "live_split", None, "live_split is the browser class's alone"))
        return size
    if not isinstance(raw, Mapping):
        out.append(_refusal(name, "live_split", None, "live_split is {worker, display}"))
        return size
    shares = dict(zip(LIVE_ROLES, size.live_split))
    for role, fields in raw.items():
        if role not in shares or not isinstance(fields, Mapping):
            out.append(_refusal(name, f"live_split.{role}", None, "live_split names worker or display"))
            continue
        changes: dict[str, Any] = {}
        for field, value in fields.items():
            if field in EDITABLE:
                changes[field] = value
            else:
                out.append(_refusal(name, f"live_split.{role}.{field}", value,
                                    f"a live share sets only {', '.join(EDITABLE)}"))
        shares[role] = replace(shares[role], **changes)
    return replace(size, live_split=(shares["worker"], shares["display"]))


def apply_changes(
    base: Mapping[str, ResourceSize], classes: Mapping[str, Any]
) -> tuple[dict[str, ResourceSize], list[SizeRefusal]]:
    """`base` with the body's classes applied, and every refusal: the body's own
    (a field that is derived, not edited or not a size; a class that does not
    exist) followed by the frozen `check_size` over the whole new table."""
    table = {name: base[name] for name in CLASS_ORDER}
    out: list[SizeRefusal] = []
    for name, fields in classes.items():
        if name not in table:
            out.append(_refusal(name, "resource_class", None, f"resource_class is one of {CLASS_ORDER}"))
            continue
        if not isinstance(fields, Mapping):
            out.append(_refusal(name, "resource_class", None, "a class's change is an object of fields"))
            continue
        size = table[name]
        changes: dict[str, Any] = {}
        for field, value in fields.items():
            if field in EDITABLE:
                changes[field] = value
            elif field == "live_split":
                size = _apply_split(name, size, value, out)
            elif field == "units":
                out.append(_refusal(name, "units", value,
                                    "units is derived (units_for(cpu_request, memory_gib)); never sent"))
            elif field == "disk_gib":
                out.append(_refusal(name, "disk_gib", value, "disk_gib is shown, not edited"))
            else:
                out.append(_refusal(name, field, value,
                                    f"a class sets only {', '.join(EDITABLE)} and live_split"))
        table[name] = replace(size, **changes)
    return table, out + check_size(table)


def _diff(before: Mapping[str, ResourceSize], after: Mapping[str, ResourceSize]) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for name in CLASS_ORDER:
        old, new = size_doc(before[name], units=False), size_doc(after[name], units=False)
        for field in ("cpu_request", "cpu_limit", "memory_gib", "disk_gib"):
            if old[field] != new[field]:
                changed.append({"class": name, "field": field, "from": old[field], "to": new[field]})
        old_split, new_split = old.get("live_split") or {}, new.get("live_split") or {}
        for role in LIVE_ROLES:
            for field in EDITABLE:
                a = (old_split.get(role) or {}).get(field)
                b = (new_split.get(role) or {}).get(field)
                if a != b:
                    changed.append({"class": name, "field": f"live_split.{role}.{field}", "from": a, "to": b})
    return changed


def _priced(size: ResourceSize) -> float:
    return round(size.cpu_request * PRICES["vcpu_hour"] + size.memory_gib * PRICES["gib_hour"], 4)


def _class_valid(name: str, refusals: Iterable[SizeRefusal]) -> bool:
    return not any(r.resource_class == name for r in refusals)


# --------------------------------------------------------------------------
# Recent use (§4.5 `recent_use`, §4.9)
# --------------------------------------------------------------------------


def _wall_minutes(attempt: Attempt) -> float | None:
    if attempt.started_at is None or attempt.completed_at is None:
        return None
    return (attempt.completed_at - attempt.started_at).total_seconds() / 60.0


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _p95(values: Sequence[float]) -> float | None:
    """Nearest rank: a value some attempt really reached, never interpolated."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def recent_attempts(db: Any, *, now: datetime) -> dict[str, list[Attempt]]:
    """The window's GKE attempts by class, newest first.

    A Cloud Run attempt is left out: it ran at the code default whatever the
    version said, so its peak says nothing about the size being changed. The
    class is the TASK's (the attempt does not record it), read in one
    `get_all` over the window's distinct tasks.
    """
    since = now - timedelta(days=RECENT_DAYS)
    query = (
        db.collection(ATTEMPTS)
        .where(filter=FieldFilter("created_at", ">=", since))
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(RECENT_ATTEMPTS_LIMIT)
    )
    attempts: list[Attempt] = []
    for snap in query.stream():
        try:
            attempt = attempt_from_dict(snap.to_dict() or {})
        except (KeyError, TypeError, ValueError):
            continue
        if attempt.backend == Backend.GKE_AUTOPILOT.value:
            attempts.append(attempt)
    task_ids = list(dict.fromkeys(a.task_id for a in attempts))
    classes: dict[str, str] = {}
    collection = db.collection(TASKS)
    for start in range(0, len(task_ids), 100):
        refs = [collection.document(task_id) for task_id in task_ids[start:start + 100]]
        for snap in db.get_all(refs):
            if snap.exists:
                classes[snap.id] = (snap.to_dict() or {}).get("resource_class")
    out: dict[str, list[Attempt]] = {name: [] for name in CLASS_ORDER}
    for attempt in attempts:
        name = classes.get(attempt.task_id)
        if name in out:
            out[name].append(attempt)
    return out


def _points(db: Any, attempts: Sequence[Attempt]) -> dict[str, list[dict[str, Any]]]:
    """LIVE-UTIL points per attempt id, for the newest `SERIES_ATTEMPTS`. An
    attempt whose beats carry no series has no entry: not measured, not zero."""
    out: dict[str, list[dict[str, Any]]] = {}
    chosen = attempts[:SERIES_ATTEMPTS]
    by_task: dict[str, list[Attempt]] = {}
    for attempt in chosen:
        by_task.setdefault(attempt.task_id, []).append(attempt)
    for task_id, owned in by_task.items():
        query = (
            db.collection(TASKS).document(task_id).collection(EVENTS)
            .where(filter=FieldFilter("type", "==", EventType.HEARTBEAT.value))
            .limit(SERIES_EVENTS_LIMIT)
        )
        events = [snap.to_dict() or {} for snap in query.stream()]
        for attempt in owned:
            points = [
                point
                for event in events
                if event.get("attempt_id") == attempt.attempt_id
                and event.get("generation") in (None, attempt.generation)
                for point in points_of(event)
            ]
            if points:
                out[attempt.attempt_id] = points
    return out


def _shares(
    attempts: Sequence[Attempt], points: Mapping[str, list[dict[str, Any]]], default_request: float
) -> dict[str, float | None]:
    """§4.9's shares, over SAMPLES pooled across the attempts that have them.
    None where no attempt carried a sample the share needs."""
    idle = above = at_limit = cpu_samples = limit_samples = 0
    long_at = long_samples = 0
    throttled: list[float] = []
    for attempt in attempts:
        series = points.get(attempt.attempt_id)
        if not series:
            continue
        request = attempt.sized_cpu_request or default_request
        limit = attempt.cpu_limit_cores or attempt.sized_cpu_limit
        wall = _wall_minutes(attempt)
        long_run = wall is not None and wall >= LONG_RUN_MINUTES
        for point in series:
            cores = point.get("cpu_cores")
            if point.get("throttled_share") is not None:
                throttled.append(point["throttled_share"])
            if cores is None:
                continue
            cpu_samples += 1
            idle += cores < IDLE_CORES
            above += cores > request
            if limit:
                pinned = cores >= AT_LIMIT_FRACTION * limit
                limit_samples += 1
                at_limit += pinned
                if long_run:
                    long_samples += 1
                    long_at += pinned

    def share(count: int, total: int) -> float | None:
        return count / total if total else None

    return {
        "share_idle": share(idle, cpu_samples),
        "share_above_request": share(above, cpu_samples),
        "share_at_limit": share(at_limit, limit_samples),
        "share_at_limit_long": share(long_at, long_samples),
        "throttled_share": sum(throttled) / len(throttled) if throttled else None,
    }


def _memory_peaks(attempts: Sequence[Attempt]) -> list[float]:
    return [a.peak_rss_bytes / GIB for a in attempts if isinstance(a.peak_rss_bytes, (int, float))]


def use_of(
    name: str, attempts: Sequence[Attempt], points: Mapping[str, list[dict[str, Any]]], *, since: datetime
) -> dict[str, Any]:
    """One class's `recent_use`."""
    default_request = float(default_sizes()[name].cpu_request)
    peaks = _memory_peaks(attempts)
    means = [a.mean_cpu_cores for a in attempts if isinstance(a.mean_cpu_cores, (int, float))]
    groups: dict[int, list[Attempt]] = {}
    for attempt in attempts:
        # An attempt admitted before sizes were recorded ran at the code
        # defaults of its day, which is version 0.
        groups.setdefault(attempt.size_version or 0, []).append(attempt)
    by_version = []
    for version in sorted(groups):
        members = groups[version]
        walls = [w for w in (_wall_minutes(a) for a in members) if w is not None]
        group_peaks = _memory_peaks(members)
        shares = _shares(members, points, default_request)
        by_version.append({
            "size_version": version,
            "attempts": len(members),
            "wall_minutes_median": _median(walls),
            "wall_minutes_median_long": _median([w for w in walls if w >= LONG_RUN_MINUTES]),
            "memory_peak_gib_max": max(group_peaks) if group_peaks else None,
            "share_at_limit": shares["share_at_limit"],
            "throttled_share": shares["throttled_share"],
        })
    return {
        "attempts": len(attempts),
        "since": since,
        "memory_peak_gib_max": max(peaks) if peaks else None,
        "memory_peak_gib_p95": _p95(peaks),
        "cpu_mean_cores_median": _median(means),
        **_shares(attempts, points, default_request),
        "oom_near_miss": sum(1 for a in attempts if a.oom_near_miss),
        "by_size_version": by_version,
    }


def _below_peak(
    base: Mapping[str, ResourceSize],
    table: Mapping[str, ResourceSize],
    recent: Mapping[str, Sequence[Attempt]],
) -> dict[str, dict[str, Any]]:
    """The classes whose memory the table CUTS to under their 7-day max peak."""
    out: dict[str, dict[str, Any]] = {}
    for name in CLASS_ORDER:
        new, old = table[name].memory_gib, base[name].memory_gib
        if not new < old:
            continue
        peaks = _memory_peaks(recent.get(name, ()))
        if peaks and new < max(peaks):
            out[name] = {"memory_gib": new, "memory_peak_gib_max": max(peaks),
                         "memory_peak_gib_p95": _p95(peaks)}
    return out


# --------------------------------------------------------------------------
# The routes' logic
# --------------------------------------------------------------------------


def _pools(db: Any, before: Mapping[str, ResourceSize], after: Mapping[str, ResourceSize] | None,
           valid: Mapping[str, bool]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    query = db.collection(POOLS).limit(500)
    for snap in query.stream():
        name = snap.id
        if name not in PREVIEW_POOLS and not name.startswith("resource:"):
            continue
        limit = (snap.to_dict() or {}).get("hard_limit")
        if isinstance(limit, bool) or not isinstance(limit, int):
            continue
        if name.startswith("resource:"):
            classes = [name.split(":", 1)[1]] if name.split(":", 1)[1] in before else []
        else:
            classes = list(CLASS_ORDER)
        if not classes:
            continue
        rows.append({
            "pool": name,
            "hard_limit": limit,
            "admits_before": {c: limit // before[c].units for c in classes},
            "admits_after": {
                c: (limit // after[c].units if after is not None and valid[c] else None) for c in classes
            },
        })
    rows.sort(key=lambda row: row["pool"])
    return rows


def preview(db: Any, classes: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """`POST /v1/admin/runner-sizes:preview`: what the body would do; nothing written."""
    setting = current(db)
    base = setting.sizes
    table, refusals = apply_changes(base, classes)
    valid = {name: _class_valid(name, refusals) for name in CLASS_ORDER}
    per_class: dict[str, Any] = {}
    for name in CLASS_ORDER:
        ok = valid[name]
        per_class[name] = {
            "units_before": base[name].units,
            "units_after": table[name].units if ok else None,
            "cost_hour_before": _priced(base[name]),
            "cost_hour_after": _priced(table[name]) if ok else None,
        }
    named = [name for name in CLASS_ORDER if name in classes] or list(CLASS_ORDER)
    recent = recent_attempts(db, now=now)
    since = now - timedelta(days=RECENT_DAYS)
    use = {name: use_of(name, recent[name], _points(db, recent[name]), since=since) for name in named}
    return {
        "base_version": setting.version,
        "valid": not refusals,
        "refusals": [refusal_doc(r) for r in refusals],
        "classes": per_class,
        "pools": _pools(db, base, table, valid),
        "recent_use": use,
        # The classes a save would need `confirm_below_recent_peak` for.
        "below_recent_peak": [] if refusals else sorted(_below_peak(base, table, recent)),
    }


def _write(
    db: Any,
    *,
    base_version: int,
    base: Mapping[str, ResourceSize],
    table: Mapping[str, ResourceSize],
    by: str,
    reason: str,
    now: datetime,
    restored_from: int | None = None,
) -> int:
    """Create version base+1 and move the pointer, in one transaction that
    refuses 409 if the pointer moved since `base_version` was read."""
    pointer_ref = _pointer_ref(db)
    number = base_version + 1
    version_ref = _version_ref(db, number)
    document = {
        "version": number,
        "previous_version": base_version,
        "created_at": now,
        "created_by": by,
        "reason": reason,
        "restored_from": restored_from,
        "classes": table_doc(table),
        "changed": _diff(base, table),
    }

    @firestore.transactional
    def _apply(txn: Any) -> None:
        pointer = _snapshot(txn.get(pointer_ref))
        found = _pointer_version(pointer.to_dict() if pointer.exists else None)
        if found != base_version:
            raise StaleBaseVersion(
                f"runner sizes are at version {found}, not {base_version}; read them again",
                detail={"current_version": found, "base_version": base_version},
            )
        # Never over an existing version, even one a hand-edited pointer skipped back past.
        if _snapshot(txn.get(version_ref)).exists:
            raise StaleBaseVersion(
                f"runner size version {number} already exists; read them again",
                detail={"current_version": found, "base_version": base_version},
            )
        txn.set(version_ref, document)
        txn.set(pointer_ref, {"current_version": number, "updated_at": now, "updated_by": by})

    _apply(db.transaction())
    log.info(
        "runner_sizes version=%d previous=%d restored_from=%s changed=%s",
        number, base_version, restored_from,
        ",".join(f"{c['class']}.{c['field']}" for c in document["changed"]) or "-",
    )
    return number


def _checked(
    db: Any,
    setting: SizeVersion,
    base_version: int,
    table: Mapping[str, ResourceSize],
    refusals: list[SizeRefusal],
    *,
    confirm: bool,
    now: datetime,
) -> None:
    if setting.version != base_version:
        raise StaleBaseVersion(
            f"runner sizes are at version {setting.version}, not {base_version}; read them again",
            detail={"current_version": setting.version, "base_version": base_version},
        )
    if refusals:
        raise SizeOutOfBounds(
            "the sizes break a frozen bound",
            detail={"refusals": [refusal_doc(r) for r in refusals]},
        )
    if not confirm:
        cut = _below_peak(setting.sizes, table, recent_attempts(db, now=now))
        if cut:
            raise BelowRecentPeak(
                "a memory cut goes below what the class used in the last "
                f"{RECENT_DAYS} days; confirm it to save",
                detail={"classes": cut},
            )


def put(db: Any, *, base_version: int, classes: Mapping[str, Any], reason: str, by: str,
        confirm_below_recent_peak: bool, now: datetime) -> dict[str, Any]:
    """`PUT /v1/admin/runner-sizes`: a new version from the current one and the body."""
    setting = current(db)
    table, refusals = apply_changes(setting.sizes, classes)
    _checked(db, setting, base_version, table, refusals, confirm=confirm_below_recent_peak, now=now)
    _write(db, base_version=base_version, base=setting.sizes, table=table, by=by, reason=reason, now=now)
    return served(current(db))


def restore(db: Any, *, base_version: int, version: int, reason: str, by: str,
            confirm_below_recent_peak: bool, now: datetime) -> dict[str, Any]:
    """`POST /v1/admin/runner-sizes/restore`: a NEW version whose classes are
    `version`'s (0: the code defaults). Never rewinds the pointer."""
    setting = current(db)
    if version == 0:
        table: Mapping[str, ResourceSize] = default_sizes()
    else:
        snap = _version_ref(db, version).get()
        if not snap.exists:
            raise NotFound(f"runner size version {version} does not exist")
        parsed = table_of((snap.to_dict() or {}).get("classes"))
        if parsed is None:
            raise SizeOutOfBounds(
                f"runner size version {version} is malformed and cannot be restored",
                detail={"refusals": []},
            )
        table = parsed
    refusals = check_size(table)
    _checked(db, setting, base_version, table, refusals, confirm=confirm_below_recent_peak, now=now)
    _write(db, base_version=base_version, base=setting.sizes, table=table, by=by, reason=reason,
           now=now, restored_from=version)
    return served(current(db))


def versions(db: Any, *, limit: int, before: int | None) -> dict[str, Any]:
    """`GET /v1/admin/runner-sizes/versions`: the audit, newest first."""
    query: Any = db.collection(VERSIONS)
    if before is not None:
        query = query.where(filter=FieldFilter("version", "<", before))
    query = query.order_by("version", direction=firestore.Query.DESCENDING).limit(
        max(1, min(limit, VERSIONS_PAGE_MAX)))
    rows = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        rows.append({key: data.get(key) for key in
                     ("version", "previous_version", "created_at", "created_by", "reason",
                      "changed", "restored_from")})
    return {"versions": rows}
