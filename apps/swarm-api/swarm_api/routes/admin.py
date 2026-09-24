"""Admin surface: pause/resume, concurrency limits, drains, provider switches.

Every limit here lands in Firestore, not in an environment variable, because the
spec requires changing concurrency without a redeploy -- and because the
scheduler reads pool documents inside the admission transaction, a limit changed
here takes effect on the very next admission rather than on the next deploy.

Draining and disabling are different operations and both exist on purpose:

  drain   -> pool.enabled = False. No new admissions; in-flight work runs to
             completion and releases its slots normally.
  disable -> drain, plus the provider's quota documents move to DISABLED so the
             quota broker's AIMD loop cannot raise the limit back up underneath
             the drain.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Query

from swarm_common.models import ProviderState
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend

from ..auth import AuthContext
from ..codec import lease_to_api, pool_to_api, quota_to_api, tenant_to_api
from ..deps import AppContext, admin_auth, get_context, paged_limit
from ..errors import NotFound, ValidationFailed
from ..schemas import (
    DrainRequest,
    LimitRequest,
    PauseRequest,
    ProviderEnableRequest,
    TenantLimitsRequest,
)
from ..validation import known_providers

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _heartbeat_grace_seconds(core: Any) -> int:
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


def _check_provider(provider: str) -> str:
    name = provider.strip().lower()
    if name not in known_providers():
        raise ValidationFailed(
            f"unknown provider {provider!r}",
            detail={"known_providers": list(known_providers())},
        )
    return name


def _check_resource_class(resource_class: str) -> str:
    if resource_class not in RESOURCE_CLASSES:
        raise ValidationFailed(
            f"unknown resource class {resource_class!r}",
            detail={"known_resource_classes": sorted(RESOURCE_CLASSES)},
        )
    return resource_class


def _check_runner(runner_profile: str) -> str:
    if runner_profile not in RUNNER_PROFILES:
        raise ValidationFailed(
            f"unknown runner_profile {runner_profile!r}",
            detail={"known_runner_profiles": sorted(RUNNER_PROFILES)},
        )
    return runner_profile


# -- dispatch pause -------------------------------------------------------

@router.get("/dispatch")
def get_dispatch(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return ctx.store.get_control()


@router.post("/dispatch/pause")
def pause_dispatch(
    body: PauseRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    payload = ctx.store.set_dispatch_paused(True, by=auth.email, reason=body.reason)
    ctx.metrics.admin_actions.labels(action="dispatch_pause").inc()
    ctx.metrics.dispatch_paused.set(1)
    return payload


@router.post("/dispatch/resume")
def resume_dispatch(
    body: PauseRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    payload = ctx.store.set_dispatch_paused(False, by=auth.email, reason=body.reason)
    ctx.metrics.admin_actions.labels(action="dispatch_resume").inc()
    ctx.metrics.dispatch_paused.set(0)
    # The scheduler exits when it finds nothing admissible, so resuming has to
    # wake it explicitly or the queue waits for the next safety tick.
    ctx.waker.wake("dispatch_resumed", by=auth.email)
    return payload


# -- concurrency limits ---------------------------------------------------

@router.put("/limits/global")
def set_global_limit(
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # `by=auth.email` on every pool write in this module: the verified caller,
    # stamped on the pool as admin_changed_by. See Store.upsert_pool for why
    # it is not `updated_by`.
    pool = ctx.store.upsert_pool("global", hard_limit=body.limit, by=auth.email)
    ctx.metrics.admin_actions.labels(action="limit_global").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/provider/{provider}")
def set_provider_limit(
    provider: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_provider(provider)
    pool = ctx.store.upsert_pool(f"provider:{name}", hard_limit=body.limit, by=auth.email)
    ctx.metrics.admin_actions.labels(action="limit_provider").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/provider/{provider}/tenant/{tenant_id}")
def set_provider_tenant_limit(
    provider: str,
    tenant_id: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The per-tenant slice of a provider's concurrency.

    Separate from /limits/provider/{provider}, and the distinction is the one
    that matters when you are trying to raise a ceiling: a lease takes BOTH
    pools, so capacity is the lower of them. Raising the provider-wide pool to
    40 while this stays at 5 gives you 5, which is exactly what happened on
    2026-09-20 and is why this route exists.

    Verified against the tenant document rather than accepted blind: an
    upsert on a misspelt tenant id would silently create a pool that nothing
    ever reads, and it would look like the limit had been set.
    """
    name = _check_provider(provider)
    # get_tenant returns None rather than raising, so this must be checked
    # explicitly -- calling it and discarding the result would be the exact
    # silent pass the docstring above claims to prevent.
    if ctx.store.get_tenant(tenant_id) is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    pool = ctx.store.upsert_pool(
        f"provider:{name}:tenant:{tenant_id}", hard_limit=body.limit, by=auth.email
    )
    ctx.metrics.admin_actions.labels(action="limit_provider_tenant").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/backend/{backend}")
def set_backend_limit(
    backend: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The ceiling on a whole execution backend.

    Missing until 2026-09-20, which made backend:CLOUD_RUN_JOB unraisable
    through the API -- every other pool in a lease's list had a route and this
    one did not, so it silently became the binding constraint the moment the
    others were raised.

    AUTO is rejected: it is a routing instruction on a runner profile, not a
    backend anything executes on, so a pool named backend:AUTO would never be
    taken by any lease and setting it would be a no-op that looked like a
    change.
    """
    name = backend.strip().upper()
    real = {b.value for b in Backend} - {Backend.AUTO.value}
    if name not in real:
        raise ValidationFailed(
            f"unknown backend {backend!r}",
            detail={"known_backends": sorted(real)},
        )
    pool = ctx.store.upsert_pool(f"backend:{name}", hard_limit=body.limit, by=auth.email)
    ctx.metrics.admin_actions.labels(action="limit_backend").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/resource/{resource_class}")
def set_resource_limit(
    resource_class: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_resource_class(resource_class)
    pool = ctx.store.upsert_pool(f"resource:{name}", hard_limit=body.limit, by=auth.email)
    ctx.metrics.admin_actions.labels(action="limit_resource").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/runner/{runner_profile}")
def set_runner_limit(
    runner_profile: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_runner(runner_profile)
    pool = ctx.store.upsert_pool(f"runner:{name}", hard_limit=body.limit, by=auth.email)
    ctx.metrics.admin_actions.labels(action="limit_runner").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/tenant/{tenant_id}")
def set_tenant_concurrency(
    tenant_id: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = ctx.store.set_tenant_limits(tenant_id, max_active=body.limit, by=auth.email)
    ctx.metrics.admin_actions.labels(action="limit_tenant").inc()
    return {"tenant": tenant_to_api(tenant), "pool": pool_to_api(
        ctx.store.get_pool(f"tenant:{tenant_id}")
    )}


@router.put("/tenants/{tenant_id}/limits")
def set_tenant_limits(
    tenant_id: str,
    body: TenantLimitsRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if body.monthly_budget_usd is not None:
        # Refused rather than stored. There is no cost attribution anywhere in
        # this control plane -- no billing export, no per-attempt spend, no price
        # per resource class -- so accepting it would write a number into
        # Firestore, echo it back with a 200, enforce nothing, and never reach
        # ParkReason.BUDGET_EXHAUSTED. An admin would believe they had a spend
        # control. store.py states the standard this would violate: "the document
        # and the pool must move together or the limit is a lie".
        raise ValidationFailed(
            "monthly_budget_usd is not enforceable by this control plane: it has no "
            "cost attribution source, so the value could be stored but never acted "
            "on. Bound spend with max_active / capacity_units, which the scheduler "
            "really does enforce on every admission.",
            detail={
                "enforceable_limits": ["max_active", "capacity_units", "enabled"],
                "requires": "per-attempt cost attribution (billing export)",
            },
        )
    if all(
        value is None for value in (body.max_active, body.capacity_units, body.enabled)
    ):
        raise ValidationFailed("at least one limit must be supplied")
    tenant = ctx.store.set_tenant_limits(
        tenant_id,
        max_active=body.max_active,
        capacity_units=body.capacity_units,
        enabled=body.enabled,
        by=auth.email,
    )
    ctx.metrics.admin_actions.labels(action="tenant_limits").inc()
    # The pool is what the scheduler enforces, and its hard limit is the smaller
    # of max_active and capacity_units, so an operator can see what their change
    # actually did rather than only what was recorded.
    pool = ctx.store.get_pool(f"tenant:{tenant_id}")
    return {
        "tenant": tenant_to_api(tenant),
        "pool": pool_to_api(pool) if pool is not None else None,
    }


# -- drains and provider switches ----------------------------------------

@router.post("/providers/{provider}/drain")
def drain_provider(
    provider: str,
    body: DrainRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_provider(provider)
    drained = [
        ctx.store.upsert_pool(f"provider:{name}", enabled=not body.drain, by=auth.email)
    ]
    prefix = f"provider:{name}:tenant:"
    for pool in ctx.store.list_pools():
        if pool.name.startswith(prefix):
            drained.append(
                ctx.store.upsert_pool(pool.name, enabled=not body.drain, by=auth.email)
            )
    ctx.metrics.admin_actions.labels(
        action="drain_provider" if body.drain else "undrain_provider"
    ).inc()
    if not body.drain:
        ctx.waker.wake("provider_undrained", provider=name)
    return {
        "provider": name,
        "drained": body.drain,
        "reason": body.reason,
        "pools": [pool_to_api(p) for p in drained],
    }


@router.post("/resources/{resource_class}/drain")
def drain_resource_class(
    resource_class: str,
    body: DrainRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_resource_class(resource_class)
    pool = ctx.store.upsert_pool(f"resource:{name}", enabled=not body.drain, by=auth.email)
    ctx.metrics.admin_actions.labels(
        action="drain_resource" if body.drain else "undrain_resource"
    ).inc()
    if not body.drain:
        ctx.waker.wake("resource_undrained", resource_class=name)
    return {
        "resource_class": name,
        "drained": body.drain,
        "reason": body.reason,
        "pool": pool_to_api(pool),
    }


@router.post("/providers/{provider}/enabled")
def set_provider_enabled(
    provider: str,
    body: ProviderEnableRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_provider(provider)
    ctx.store.set_provider_enabled(name, body.enabled, by=auth.email)
    # Without this the broker's AIMD loop would raise the adaptive target back
    # up and quietly undo the disable.
    touched = ctx.store.set_provider_quota_state(
        name, ProviderState.AVAILABLE if body.enabled else ProviderState.DISABLED
    )
    ctx.metrics.admin_actions.labels(
        action="provider_enable" if body.enabled else "provider_disable"
    ).inc()
    if body.enabled:
        ctx.waker.wake("provider_enabled", provider=name)
    return {
        "provider": name,
        "enabled": body.enabled,
        "reason": body.reason,
        "quota_documents_updated": touched,
    }


# -- read models ----------------------------------------------------------

@router.get("/pools")
def list_pools(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    pools = sorted(ctx.store.list_pools(), key=lambda p: p.name)
    return {"pools": [pool_to_api(p) for p in pools]}


@router.get("/leases")
def list_leases(
    tenant_id: str | None = Query(default=None),
    active_only: bool = Query(default=True),
    state: str | None = Query(default=None, description="LEASED or DISPATCHED"),
    overdue_only: bool = Query(default=False, description="dispatch_deadline already passed"),
    limit: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Who is holding capacity right now, and who never started.

    P1. The documents, the decoder and the `leases-tenant-created` index all
    existed; only this route was missing, and its absence is why nothing could
    answer "which agents are actually holding a slot" -- the question an
    operator asks first during a capacity incident.

    ONE ROUTE, NOT TWO. "Silent workers" and "admitted but never dispatched"
    are the same query with different filters, so `state` and `overdue_only`
    are parameters rather than a second endpoint.

    THE THRESHOLDS COME BACK WITH THE DATA, and that is the point of this
    shape. 90 and 120 are the reconciler's `heartbeat_grace_seconds` and
    `lease_timeout_seconds`; a `const GRACE = 90` in a front end is exactly
    the restatement drift `scripts/lib/check-contract-parity.sh` exists to
    catch in shell and jq. And the grace is NOT a constant -- the reconciler
    reads HEARTBEAT_GRACE_SECONDS and otherwise derives
    `max(90, heartbeat_interval_seconds * 3)` -- so this resolves it the same
    way from the same Settings rather than becoming a third copy of the
    number.

    `last_error` is denormalised onto each row. The alternative is a caller
    issuing one task read per lease, which is an N+1 waiting to be written as
    a loop. On a dispatch failure it is a stable code plus the attempt id,
    deliberately not the upstream message, because a Cloud Run or Kubernetes
    error echoes the tenant service account, the job name and the secret
    names.

    THE ROWS SAY WHETHER A LIVE LEASE WAS LEFT OUT. The Holders screen's
    drift check compares `units_held` against `pool.active`, and a delta is
    evidence of a leak only if no live lease is missing from the rows.
    `active_beyond_window` is that count: unreleased leases, under the same
    tenant filter, outside the window. Read it, not `truncated`.

    With `active_only` (the default, and what the Holders screen asks for)
    the store reads the live set itself and keeps the newest `limit`, so a
    live lease older than any number of released ones is still a row. It
    used to read the newest `limit` documents of any state and drop the
    released ones afterwards. Lease documents are never deleted, so past
    `limit` admissions a live lease could fall out of that window, and the
    flag first added to say so, `truncated`, was then true on every call and
    told nobody anything.

    `truncated` remains: the window was full and more lay behind it (more
    live leases than `limit` when `active_only`; older documents of any
    state otherwise). `examined` is how many rows `state` and `overdue_only`
    ran over. Those two filters narrow the window; they do not change
    `active_beyond_window`, so with either set `units_held` is a filtered sum
    and not the drift check's input.
    """
    scan = ctx.store.scan_leases(
        tenant_id, active_only=active_only, limit=paged_limit(ctx, limit)
    )
    leases = scan.leases
    now = ctx.now()
    if state is not None:
        wanted = state.strip().upper()
        leases = [x for x in leases if x.state.value == wanted]
    if overdue_only:
        leases = [x for x in leases if x.dispatch_overdue(now)]

    # One batched read for the error text, not one per row.
    errors: dict[str, str | None] = {}
    for lease in leases:
        if lease.task_id in errors:
            continue
        try:
            errors[lease.task_id] = ctx.store.get_task(lease.tenant_id, lease.task_id).last_error
        except Exception:
            # A lease whose task is gone is itself a finding -- the reconciler
            # calls it task_missing. Do not let it fail the whole listing.
            errors[lease.task_id] = None

    rows = []
    for lease in leases:
        row = lease_to_api(lease)
        row["last_error"] = errors.get(lease.task_id)
        # Written out rather than `heartbeat_at ?? created_at`: the fallback is
        # a real timestamp for a lease that has never beaten, never 0 and
        # never now.
        beat = lease.heartbeat_at if lease.heartbeat_at is not None else lease.created_at
        row["silent_seconds"] = max(0, int((now - beat).total_seconds()))
        row["heartbeat_ever"] = lease.heartbeat_at is not None
        rows.append(row)

    core = ctx.settings.core
    return {
        "leases": rows,
        "thresholds": {
            "heartbeat_grace_seconds": _heartbeat_grace_seconds(core),
            "lease_timeout_seconds": core.lease_timeout_seconds,
        },
        "evaluated_at": now,
        "active_only": active_only,
        "tenant_id": tenant_id,
        # Weighted UNITS, not agents -- admission increments by the resource
        # class's units, so this and len(leases) are different numbers.
        "units_held": sum(lease.units for lease in leases),
        # Live leases, under the tenant filter, that are not in these rows.
        # 0 is what makes a units_held / pool.active delta evidence.
        "active_beyond_window": scan.active_beyond_window,
        # The window was full and more lay behind it -- see the docstring for
        # why this is not the drift check's signal.
        "truncated": scan.truncated,
        # Rows state / overdue_only ran over.
        "examined": scan.examined,
    }


@router.get("/quota")
def list_quota(
    tenant_id: str | None = Query(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Provider quota state, per provider per tenant.

    P2. `Store.list_quota` already existed and was already used by
    `/v1/providers` for the caller's own tenant; this exposes it across
    tenants for an operator.

    `effective_limit: 0` is a FACT, not a missing value -- QuotaState returns
    0 deliberately when the state is EXHAUSTED, DISABLED or COOLDOWN.

    `generated_at` is the server's clock when it read these documents, as on
    `/v1/capacity`, `/v1/stats` and `/v1/providers`. Without it a panel could
    only show when the bytes ARRIVED, which says nothing about when the
    platform computed them (docs/audits/2026-09-20/data-gaps-found-by-fanout.md
    section 2). Each row's own `updated_at` is a different fact -- when the
    broker last wrote that document -- and is not a substitute.
    """
    states = ctx.store.list_quota(tenant_id)
    return {
        "quota": [quota_to_api(q) for q in sorted(states, key=lambda q: (q.provider, q.tenant_id))],
        "tenant_id": tenant_id,
        "generated_at": ctx.now(),
    }


@router.get("/tenants")
def list_tenants(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"tenants": [tenant_to_api(t) for t in ctx.store.list_tenants()]}


@router.post("/workflows/rollup")
def rollup_workflows(
    tenant_id: str = Query(..., min_length=1),
    limit: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Refresh the stored workflow state for one tenant's live workflows.

    WHY THIS EXISTS SEPARATELY from the write-back the read routes already do.
    The read routes converge the cache for any workflow somebody looks at, which
    is most of them -- the web UI polls `GET /v1/workflows?limit=100`. This route
    converges the rest, so a query like "list my failed workflows" is answerable
    without having first listed them. That is the half of the owner's decision
    the write exists for, and without a sweep it would only hold for workflows a
    human happened to open.

    TENANT IS EXPLICIT, not derived from the admin's own identity. An admin's
    `tenant_scope` is the admin's own tenant, and sweeping that would silently do
    nothing for the tenant the operator meant.

    Terminal workflows are skipped and truncation is reported rather than hidden;
    see `WorkflowRollups.sweep`. A caller that reads `truncated: true` has not
    been given a complete census, and the response says so.
    """
    results, report = ctx.rollups.sweep(tenant_id, limit=paged_limit(ctx, limit))
    ctx.metrics.admin_actions.labels(action="workflow_rollup").inc()
    return {
        "tenant_id": tenant_id,
        "report": report.to_api(),
        # Only the ones that did not agree. A sweep over a healthy tenant returns
        # an empty list, which is an answer rather than the absence of one.
        "drifted": [
            {
                "workflow_id": r.workflow.workflow_id,
                "stored": r.drift["stored"],
                "derived": r.drift["derived"],
                "agrees": r.drift["agrees"],
                "repaired": r.drift["repaired"],
                "reason": r.drift["reason"],
            }
            for r in results
            if r.drift["agrees"] is not True
        ],
    }
