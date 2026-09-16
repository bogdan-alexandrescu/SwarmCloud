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

from fastapi import APIRouter, Depends

from swarm_common.models import ProviderState
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES

from ..auth import AuthContext
from ..codec import pool_to_api, tenant_to_api
from ..deps import AppContext, admin_auth, get_context
from ..errors import ValidationFailed
from ..schemas import (
    DrainRequest,
    LimitRequest,
    PauseRequest,
    ProviderEnableRequest,
    TenantLimitsRequest,
)
from ..validation import known_providers

router = APIRouter(prefix="/v1/admin", tags=["admin"])


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
    pool = ctx.store.upsert_pool("global", hard_limit=body.limit)
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
    pool = ctx.store.upsert_pool(f"provider:{name}", hard_limit=body.limit)
    ctx.metrics.admin_actions.labels(action="limit_provider").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/resource/{resource_class}")
def set_resource_limit(
    resource_class: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_resource_class(resource_class)
    pool = ctx.store.upsert_pool(f"resource:{name}", hard_limit=body.limit)
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
    pool = ctx.store.upsert_pool(f"runner:{name}", hard_limit=body.limit)
    ctx.metrics.admin_actions.labels(action="limit_runner").inc()
    return {"pool": pool_to_api(pool)}


@router.put("/limits/tenant/{tenant_id}")
def set_tenant_concurrency(
    tenant_id: str,
    body: LimitRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = ctx.store.set_tenant_limits(tenant_id, max_active=body.limit)
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
    if all(
        value is None
        for value in (body.max_active, body.capacity_units, body.monthly_budget_usd, body.enabled)
    ):
        raise ValidationFailed("at least one limit must be supplied")
    tenant = ctx.store.set_tenant_limits(
        tenant_id,
        max_active=body.max_active,
        capacity_units=body.capacity_units,
        monthly_budget_usd=body.monthly_budget_usd,
        enabled=body.enabled,
    )
    ctx.metrics.admin_actions.labels(action="tenant_limits").inc()
    return {"tenant": tenant_to_api(tenant)}


# -- drains and provider switches ----------------------------------------

@router.post("/providers/{provider}/drain")
def drain_provider(
    provider: str,
    body: DrainRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    name = _check_provider(provider)
    drained = [ctx.store.upsert_pool(f"provider:{name}", enabled=not body.drain)]
    prefix = f"provider:{name}:tenant:"
    for pool in ctx.store.list_pools():
        if pool.name.startswith(prefix):
            drained.append(ctx.store.upsert_pool(pool.name, enabled=not body.drain))
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
    pool = ctx.store.upsert_pool(f"resource:{name}", enabled=not body.drain)
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
    ctx.store.set_provider_enabled(name, body.enabled)
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


@router.get("/tenants")
def list_tenants(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"tenants": [tenant_to_api(t) for t in ctx.store.list_tenants()]}
