"""A task's worker heartbeat, read from its lease, for the task routes (#179).

The worker writes its heartbeat to the LEASE (`agent_worker/control.py`
`heartbeat()`), never to the task, so a task row on its own cannot say a
worker has gone silent. `GET /v1/admin/leases` (admin) and `GET /v1/leases`
(the caller's live leases) serve the beat as rows of their own; this module
puts it ON each lease-holding row of `GET /v1/tasks` and `GET /v1/tasks/{id}`,
so the Agents list draws a silent worker from the page it already reads.

TWO THINGS LIVE HERE, AND EVERY ROUTE THAT SERVES THEM READS THEM FROM HERE:

  * `heartbeat_grace_seconds` -- the threshold the reconciler acts on. Moved
    out of `routes/admin.py`, not copied, so the admin leases route,
    `GET /v1/leases` and the task routes cannot draw a row amber at two
    different numbers.
  * `heartbeats_for_page` -- the page's current leases, in batched `get_all`
    reads of at most 100 documents, as `task_accounts.accounts_for` and
    `waiting.waiting_for_page` batch theirs. Never one read per row.

READ-ONLY. No transaction, no write; a lease is never extended, released or
re-fenced from here (invariants 2, 3 and 5 are untouched). Only a lease whose
`tenant_id` and `task_id` match the row's task is served, so a task document
naming another tenant's lease id reads as `no lease`, never as that lease's
beat (invariant 9).

`heartbeat` says what the reading is, beside the two numbers:

  read            the lease was read; `heartbeat_at` is its last beat, or null
                  when it has NEVER beaten -- never defaulted to the lease's
                  `created_at`, which is when it was admitted, not a beat.
  not read        the lease read failed. `heartbeat_at` is null because nothing
                  is known, and this word is what keeps that null from being
                  drawn as a silent worker.
  no lease        the task names no current lease, or the lease it names has no
                  document (or is not this task's).
  null            the task is in a state that holds no lease, so it has no
                  heartbeat to read (QUEUED, READY, PARKED, terminal ...).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from swarm_common.models import Task
from swarm_common.states import CONCURRENCY_STATES

from .codec import as_datetime

log = logging.getLogger(__name__)

LEASES = "leases"

#: The states that hold a lease -- the frozen contract's own set (invariant 1:
#: the ones that create infrastructure demand). Only these have a beat to read.
HEARTBEAT_STATES = CONCURRENCY_STATES

#: `get_all` chunk, as `Store.tasks_by_id` and the outcome ledger read: one
#: round trip per hundred rows, so a 200-row page costs two.
_BATCH = 100

READ = "read"
NOT_READ = "not read"
NO_LEASE = "no lease"


def heartbeat_grace_seconds(core: Any) -> int:
    """Resolve the grace EXACTLY as ReconcilerConfig.from_env does.

    `reconciler/config.py:82-85` reads HEARTBEAT_GRACE_SECONDS and otherwise
    derives max(90, heartbeat_interval_seconds * 3). If this diverges, the UI
    colours a row amber at a threshold the reconciler does not act on, which
    is worse than showing no threshold at all.
    """
    raw = os.environ.get("HEARTBEAT_GRACE_SECONDS")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return max(90, core.heartbeat_interval_seconds * 3)


def not_applicable() -> dict[str, Any]:
    """The reading for a task that holds no lease, and for routes that read none."""
    return {"heartbeat_at": None, "heartbeat_grace_seconds": None, "heartbeat": None}


def heartbeats_for_page(
    db: Any, tenant_id: str, tasks: Sequence[Task], *, core: Any
) -> dict[str, dict[str, Any]]:
    """Each lease-holding task's heartbeat reading, by task id.

    A task outside `HEARTBEAT_STATES` is not in the answer (the caller serves
    `not_applicable()` for it). A failed chunk marks ITS rows `not read` and
    does not fail the listing it rides on: the tasks themselves were read.
    """
    grace = heartbeat_grace_seconds(core)
    holding = [task for task in tasks if task.state in HEARTBEAT_STATES]
    out: dict[str, dict[str, Any]] = {}
    wanted: dict[str, list[Task]] = {}
    for task in holding:
        if task.current_lease_id:
            wanted.setdefault(task.current_lease_id, []).append(task)
        else:
            out[task.id] = _reading(None, grace, NO_LEASE)

    lease_ids = list(wanted)
    collection = db.collection(LEASES)
    for start in range(0, len(lease_ids), _BATCH):
        chunk = lease_ids[start : start + _BATCH]
        try:
            snaps = list(db.get_all([collection.document(lease_id) for lease_id in chunk]))
        except Exception as exc:  # the tasks were read; only this reading failed
            log.warning("heartbeat: lease read failed: %s", type(exc).__name__)
            for lease_id in chunk:
                for task in wanted[lease_id]:
                    out[task.id] = _reading(None, grace, NOT_READ)
            continue
        found = {snap.id: snap.to_dict() for snap in snaps if snap.exists}
        for lease_id in chunk:
            data = found.get(lease_id)
            for task in wanted[lease_id]:
                if (
                    data is None
                    or data.get("tenant_id") != tenant_id
                    or data.get("task_id") != task.id
                    or task.tenant_id != tenant_id
                ):
                    out[task.id] = _reading(None, grace, NO_LEASE)
                else:
                    out[task.id] = _reading(data.get("heartbeat_at"), grace, READ)
    return out


def _reading(beat: Any, grace: int, status: str) -> dict[str, Any]:
    return {
        "heartbeat_at": as_datetime(beat),
        "heartbeat_grace_seconds": grace,
        "heartbeat": status,
    }
