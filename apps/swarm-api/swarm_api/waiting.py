"""What a READY task is waiting for, read live from its own pools (#362).

WHY THIS EXISTS BESIDE `blocked_by`
------------------------------------
`task.blocked_by` is what the scheduler wrote on its LAST pass over the task.
It is a record, not a reading: it is as old as that pass, and a task the
scheduler has not reached yet has none. A READY task behind a full pool is
invariant 1 working -- it holds no capacity and shows no progress -- and the
one thing a person looking at it needs is WHICH pool refuses it, now. So the
task routes serve `waiting_for` beside `blocked_by`, and the UI keeps the two
apart: the bar is labelled "last scheduler pass", this line is the live one.

HOW IT AVOIDS RESTATING ADMISSION
----------------------------------
  * `required` is `pool_names_for(...)` -- the frozen function admission calls
    -- with the backend `resolve_backend` picks, and `units` is
    `RESOURCE_CLASSES[task.resource_class]`, which is what the scheduler's
    drain loop weighs a task by (`scheduler/loop.py`, `_admit_one`).
  * whether a pool refuses is `headroom.analyse_profile`'s answer, which is
    `evaluate_capacity` underneath. The states below only DESCRIBE a refusal
    that function already made (a limit of 0, a limit below the task's units,
    or a full pool); none of them decides one.

WHAT IS NEVER CLAIMED
---------------------
A pool that was not read, or whose document carries no `hard_limit` key, is
`unknown` with `limit: null`. It is never a 0 and never "full": admission
would read the missing key as 0, but a document without the field is a
configuration gap to name, not a measurement to render. A required pool with
no document is uncapped by construction (the same rule `evaluate_capacity`
applies) and is not listed at all.

READ-ONLY. One `get_all` over the task's own pools -- de-duplicated across a
page for the listing -- no transaction and no write. Leasing is untouched;
this module cannot reserve anything (invariant 2). Every pool name comes from
the TASK's tenant id, so another tenant's pool is never read (invariant 9).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.models import SlotPool, Task, pool_names_for, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import BlockedReason, TaskState

from .headroom import analyse_profile

log = logging.getLogger(__name__)

POOLS = "pools"

STATE_PAUSED = "paused"
STATE_UNKNOWN = "unknown"
STATE_ZERO = "zero"
STATE_BELOW_UNITS = "below_units"
STATE_FULL = "full"
STATE_OPEN = "open"

#: Lead order. Paused first because it is the one that needs a person and
#: waiting never clears it; unknown next because it may be hiding a refusal;
#: a ceiling that can never admit this task (0, or below its units) before a
#: pool that is merely full and will free up. Within a tier, `pool_names_for`
#: order, which the stable sort keeps.
_TIER = {
    STATE_PAUSED: 0,
    STATE_UNKNOWN: 1,
    STATE_ZERO: 2,
    STATE_BELOW_UNITS: 2,
    STATE_FULL: 3,
    STATE_OPEN: 4,
}


def required_pools(task: Task) -> tuple[list[str], int] | None:
    """Every pool admission would reserve for this task, and its units.

    None when the task names a profile or resource class the catalogue no
    longer has: admission could not place it either, and a guess at its pools
    would be a confident answer about pools it may not need.
    """
    profile = RUNNER_PROFILES.get(task.runner_profile)
    resource = RESOURCE_CLASSES.get(task.resource_class)
    if profile is None or resource is None:
        return None
    required = pool_names_for(
        tenant_id=task.tenant_id,
        provider=task.provider,
        resource_class=task.resource_class,
        runner_profile=task.runner_profile,
        backend=resolve_backend(profile).value,
    )
    return required, resource.units


def read_pools(db: Any, names: Iterable[str]) -> tuple[dict[str, dict[str, Any] | None], bool]:
    """The named pool documents, in ONE `get_all`, and whether every name answered.

    A name mapped to None was read and has no document. A name missing from
    the map was not read. A failed read is reported as nothing read -- every
    pool then renders `unknown` -- rather than failing the task GET it rides on.
    """
    wanted = list(dict.fromkeys(n for n in names if n))
    if not wanted:
        return {}, True
    collection = db.collection(POOLS)
    try:
        snaps = list(db.get_all([collection.document(name) for name in wanted]))
    except Exception as exc:  # the task itself was read; only this reading failed
        log.warning("waiting_for: pool read failed: %s", type(exc).__name__)
        return {}, False
    raw: dict[str, dict[str, Any] | None] = {}
    for snap in snaps:
        raw[snap.id] = snap.to_dict() if snap.exists else None
    return raw, all(name in raw for name in wanted)


def _limit_of(data: Mapping[str, Any]) -> int | None:
    value = data.get("hard_limit")
    return None if value is None else int(value)


def waiting_for(
    task: Task,
    raw_pools: Mapping[str, Mapping[str, Any] | None],
    *,
    listing_complete: bool,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    """The live admission picture for a READY task; None for any other state.

    `raw_pools` maps a pool name to its stored document, or to None when the
    pool was read and has no document. A required name absent from the map is
    unread when `listing_complete` is False and has no document when it is True.
    """
    if task.state not in (TaskState.READY, TaskState.LEASED):
        return None
    when = as_of or utcnow()
    need = required_pools(task)
    if need is None:
        return {
            "as_of": when,
            "admissible_now": None,
            "holds_capacity": False,
            "lead": None,
            "pools": [],
            "complete": False,
        }
    required, units = need

    readable: dict[str, SlotPool] = {}
    unread: list[str] = []
    limit_unknown: set[str] = set()
    active_of: dict[str, int | None] = {}
    for name in required:
        if name not in raw_pools:
            if not listing_complete:
                unread.append(name)
                active_of[name] = None
            continue
        data = raw_pools[name]
        if data is None:
            continue  # no document: uncapped, never listed
        active = int(data.get("active", 0) or 0)
        active_of[name] = active
        enabled = bool(data.get("enabled", True))
        limit = _limit_of(data)
        if limit is None:
            limit_unknown.add(name)
            if enabled:
                unread.append(name)
                continue
            # Paused is checked first and refuses at any limit, so a paused
            # pool with no limit is still a known refusal. The 0 here never
            # reaches a caller: `limit_unknown` serves it as null.
            limit = 0
        readable[name] = SlotPool(
            name=name,
            hard_limit=limit,
            adaptive_target=data.get("adaptive_target"),
            quota_derived_limit=data.get("quota_derived_limit"),
            active=active,
            enabled=enabled,
        )

    analysis = analyse_profile(required=required, pools=readable, units=units, unread=unread)
    blockers = {b["pool"]: b for b in analysis["blockers"]}

    rows: list[dict[str, Any]] = []
    for name in required:
        if name in unread:
            rows.append(_row(name, STATE_UNKNOWN, active_of.get(name), 0, units, None))
            continue
        pool = readable.get(name)
        if pool is None:
            continue
        blocker = blockers.get(name)
        limit = None if name in limit_unknown else pool.effective_limit
        if blocker is None:
            rows.append(_row(name, STATE_OPEN, pool.active, limit, units, None))
            continue
        reason = str(blocker.get("reason"))
        if reason == BlockedReason.MANUAL_PAUSE.value:
            state = STATE_PAUSED
        elif blocker["limit"] == 0:
            state = STATE_ZERO
        elif blocker["limit"] < units:
            state = STATE_BELOW_UNITS
        else:
            state = STATE_FULL
        rows.append(_row(name, state, blocker["active"], limit, units, reason))
    rows.sort(key=lambda r: required.index(r["pool"]))

    lead = next((r for r in rows if r["state"] != STATE_OPEN), None)
    if blockers:
        admissible: bool | None = False
    else:
        admissible = True if analysis["complete"] else None
    return {
        "as_of": when,
        # True: every required pool was read and none refuses; the next
        # scheduler pass may admit it. None: an unknown pool could be refusing.
        "admissible_now": admissible,
        # Always False, and served so no reader has to know invariant 1 to
        # read a READY task correctly.
        "holds_capacity": False,
        "lead": dict(lead) if lead else None,
        "pools": rows,
        "complete": bool(analysis["complete"]),
    }


def _row(
    name: str, state: str, active: int | None, limit: int | None, units: int, reason: str | None
) -> dict[str, Any]:
    return {
        "pool": name,
        "state": state,
        "active": active,
        "limit": limit,
        "units": units,
        "reason": reason,
    }


def waiting_for_page(db: Any, tasks: Sequence[Task], *, as_of: datetime) -> dict[str, dict[str, Any] | None]:
    """`waiting_for` for every task on a page, from ONE read of their pools.

    Only READY tasks need a reading, so a page with none reads nothing. The
    pool names are the union over the page's READY tasks -- de-duplicated, so
    twenty tasks behind `tenant:eng` ask for it once.
    """
    ready = [t for t in tasks if t.state is TaskState.READY]
    ready_ids = {t.id for t in ready}
    names: list[str] = []
    for t in ready:
        need = required_pools(t)
        if need is not None:
            names.extend(need[0])
    raw, complete = read_pools(db, names) if names else ({}, True)
    return {
        t.id: waiting_for(t, raw, listing_complete=complete, as_of=as_of) if t.id in ready_ids else None
        for t in tasks
    }
