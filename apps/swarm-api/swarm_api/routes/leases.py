"""The heartbeat of each of the caller's slot-holding tasks (#179).

A worker's heartbeat is written to its LEASE (`agent_worker/control.py`
`heartbeat()` moves `heartbeat_at` and `expires_at`), never to its task, so a
list built from `GET /v1/tasks` cannot say how long a running agent has been
quiet -- and `updated_at` is no stand-in, because a heartbeat does not move it.
`GET /v1/admin/leases` serves heartbeats but is admin-gated, so a tenant
member's Agents list could draw no "silent worker" line (AG-14, #82). This is
that read, narrowed to what the line needs.

TENANT SCOPE (invariant 9). The tenant comes from `tenant_scope`, never from
the request; a `tenant_id` in the query string is not a parameter here. The
store read refuses to run without one (`Store.live_leases_of`), and the task
join is tenant-checked again (`Store.tasks_by_id`).

ONLY WORK THAT HOLDS A SLOT (invariant 1). A row exists for a task in
LEASED/DISPATCHED/STARTING/RUNNING, through the lease the task names as its
current one. An unreleased lease of a superseded generation is the
reconciler's ORPHAN_LEASE finding, not the running worker's heartbeat, and a
terminal task whose lease is not yet released has no worker left to watch.

NEVER BEAT IS NOT BEAT LONG AGO. `heartbeat_at` and `silent_seconds` are null
until the worker's first beat. The admin route falls back to `created_at`, for
an operator who wants one number per row; a list line that read that fallback
would call a booting agent "silent for four minutes". Before the first beat the
reconciler judges a lease by its dispatch deadline alone
(`reconciler/detect.py` `detect_stale_leases`), which is what
`dispatch_overdue` carries.

THE GRACE ARRIVES WITH THE DATA and is resolved by the admin route's own
`_heartbeat_grace_seconds`, the one the reconciler parity test holds, so there
is no third copy of the number. `silent` is the reconciler's rule: a worker
that has beaten at least once and has been quiet for longer than the grace.

NO FREE TEXT. No `last_error`, no `release_reason`, no pool names: ids,
states, timestamps and numbers only, so there is nothing on this route for a
masker to miss.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.states import CONCURRENCY_STATES

from ..deps import AppContext, get_context, submission_scope, tenant_scope
from .admin import _heartbeat_grace_seconds

router = APIRouter(prefix="/v1/leases", tags=["leases"])


@router.get("")
def list_lease_heartbeats(
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Each slot-holding task's current lease heartbeat, in one request.

    One read of the tenant's live leases and one batched read of their tasks,
    so a list of N running agents costs two round trips rather than N event
    reads. The live set is bounded by the tenant's pool limit, not by history,
    so it is served whole rather than paged.
    """
    now = ctx.now()
    grace = _heartbeat_grace_seconds(ctx.settings.core)
    leases = ctx.store.live_leases_of(tenant_id)
    tasks = ctx.store.tasks_by_id(tenant_id, (lease.task_id for lease in leases))

    rows = []
    for lease in leases:
        task = tasks.get(lease.task_id)
        if task is None or lease.tenant_id != tenant_id:
            continue
        if submitted_by is not None and task.submitted_by != submitted_by:
            # Unreachable today -- a continuation-scoped caller is refused this
            # route before it runs (auth.CONTINUATION_ROUTES) -- and filtered
            # anyway, as every other task read is, should that list grow.
            continue
        if task.state not in CONCURRENCY_STATES:
            continue
        current = (
            task.current_lease_id == lease.lease_id
            if task.current_lease_id is not None
            else task.current_generation == lease.generation
        )
        if not current:
            continue
        silent_seconds = (
            None
            if lease.heartbeat_at is None
            else max(0, int((now - lease.heartbeat_at).total_seconds()))
        )
        rows.append(
            {
                "task_id": task.id,
                "lease_id": lease.lease_id,
                "attempt_id": lease.attempt_id,
                "generation": lease.generation,
                "task_state": task.state.value,
                # Only ever LEASED or DISPATCHED: the worker advances the TASK
                # through STARTING and RUNNING and touches the lease only to
                # heartbeat, so this is not called `state`.
                "dispatch_state": lease.state.value,
                "created_at": lease.created_at,
                "dispatch_deadline": lease.dispatch_deadline,
                "expires_at": lease.expires_at,
                "heartbeat_at": lease.heartbeat_at,
                "silent_seconds": silent_seconds,
                "silent": silent_seconds is not None and silent_seconds > grace,
                "expired": lease.is_expired(now),
                "dispatch_overdue": lease.dispatch_overdue(now),
            }
        )

    return {
        "tenant_id": tenant_id,
        # The clock every row's `silent_seconds` is taken against.
        "read_at": now,
        "thresholds": {
            "heartbeat_grace_seconds": grace,
            "lease_timeout_seconds": ctx.settings.core.lease_timeout_seconds,
        },
        "heartbeats": rows,
    }
