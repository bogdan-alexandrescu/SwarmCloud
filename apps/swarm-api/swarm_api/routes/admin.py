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

import base64
import binascii
import json
import logging
from typing import Any, Mapping

from fastapi import APIRouter, Body, Depends, Query, Request
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.identity import Principal
from swarm_common.models import Lease, ProviderState, SlotPool, Task, Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend

from .. import roles, runnersizes
from ..attempt_totals import totals_for, with_totals
from ..browser_logins import CarryReaper
from ..auth import AuthContext
from ..cifix import postback_tenant as postback_ci_fixes
from ..codec import (
    lease_dispatch_overdue,
    lease_to_api,
    pool_attribution,
    pool_to_api,
    quota_to_api,
    task_to_api,
    tenant_to_api,
)
from ..deps import AppContext, admin_auth, get_context, paged_limit
from ..errors import Forbidden, NotFound, RoleUnavailable, ValidationFailed
from ..heartbeats import heartbeat_grace_seconds
from ..judgment import settings_keys
from ..people import add_names, name_lookup
from ..platform_secrets import SecretManagerMetadata, platform_secret_status
from ..mergewake import wake_tenant
from ..prgate import wake_tenant as wake_after_prs
from ..mergetrain import tick_tenant as tick_merge_trains
from ..purge import HistoryPurge, parse_query
from ..qatargets import QaTargets
from ..verdictpublish import on_task_finished
from ..waker import TASK_FINISHED
from ..repoindex import RepoIndex
from ..repositories import RepositoryPush
from ..brokerclient import AccountPool
from ..children import release_held_runs
from ..schemas import (
    AdaptiveTargetRequest,
    DrainRequest,
    HardMaxRequest,
    HistoryPurgeRequest,
    LimitRequest,
    PauseRequest,
    PlatformSettingsRequest,
    ProviderEnableRequest,
    RunnerSizesPreviewRequest,
    RunnerSizesRequest,
    RunnerSizesRestoreRequest,
    TenantFindingsEpicRequest,
    TenantLimitsRequest,
)
from ..admins import AUDIT_COLLECTION
from ..store import TENANTS
from ..task_accounts import accounts_for
from ..task_input import masking_for
from ..validation import known_providers
from ..workspaces import WORKSPACES, kind_of
from .accounts import account_pool
from .browser_logins import browser_vault
from .runs import advance_tenant_runs

router = APIRouter(prefix="/v1/admin", tags=["admin"])

log = logging.getLogger(__name__)

#: The grace now lives in `swarm_api.heartbeats`, which the task routes read
#: too (#179). The old name stays bound to the SAME function, not a copy, for
#: the importers that predate the move (tests/browser/fixtures.py,
#: tests/unit/control_plane/test_lease_heartbeat_route.py).
_heartbeat_grace_seconds = heartbeat_grace_seconds


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
    """The control document, with `dispatch_state` and `control_document` (U26).

    `dispatch_paused` is False for a missing document, as the scheduler reads
    it; `dispatch_state: unknown` / `control_document: missing` say that the
    False is a default, not a switch someone set. See `Store.get_control`.
    """
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


# -- platform settings ------------------------------------------------------

@router.get("/settings")
def get_platform_settings(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The platform settings (`Store.get_platform_settings`), each at its
    default when unset, and whether the document behind them exists."""
    return ctx.store.get_platform_settings()


@router.put("/settings")
def put_platform_settings(
    body: PlatformSettingsRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Change the settings the body names, and only those.

    `merge_by_default` (contract request 47): whether a workflow that opens
    one pull request and does not say `metadata.merge` gets a `merge` step
    appended at submission. It applies to workflows submitted AFTER the
    change; a workflow already submitted keeps the steps it was signed with.

    `max_additional_repositories` (1-10, default 5) and
    `additional_repositories_budget_gib` (1-20, default 6): the limits on a
    step's additional repositories (#1434). Read at every submission, so a
    change refuses or admits the next one with no deploy; the worker reads
    the budget at every clone. A task already submitted keeps what it was
    pinned with. Ranges: `store.PLATFORM_SETTINGS_RANGES`.

    `model_prices` (#1481): list prices by model, each named entry replacing
    that model's; every other model keeps its figure. Each figure is a
    non-negative number (a string, a bool or a null is refused, never
    coerced), and each changed entry is stamped with the admin who made it.
    Read at every stopped-attempt estimate (`spendestimate`), so a change
    prices the next one with no deploy.

    `agent_budget_ceiling` (#2459): `{usd, tokens}`, either or both, replaced
    whole (`{}` clears it). No workspace default may be saved above it in its
    unit, no step may ask for more, and every budgeted attempt's limit is at
    most it (`budget`). Read at every submission and every save.
    """
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise ValidationFailed(
            "name at least one setting to change",
            detail={"settings": sorted(PlatformSettingsRequest.model_fields)},
        )
    settings = ctx.store.set_platform_settings(changes, by=auth.email)
    ctx.metrics.admin_actions.labels(action="platform_settings").inc()
    return settings


# -- platform secrets (status only) -----------------------------------------
#
# docs/design/secrets-scopes.md §5 and §11 Q7 (SSC-PLATFORM-STATUS, part of
# #2158). Secret Manager METADATA under swarmSecretStatusViewer
# (terraform/bootstrap/platform_secret_status.tf): no value, no length, and no
# route that writes one. `admin_auth` like every route here, and not on
# `auth.POOL_ADMIN_ROUTES`, so a pool admin is refused as a member is.


def secret_metadata(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """The metadata reader, built on first use and kept on the app: building
    the app builds no client, and a test sets `app.state.secret_metadata`."""
    meta = getattr(request.app.state, "secret_metadata", None)
    if meta is None:
        meta = SecretManagerMetadata(ctx.settings.project_id)
        request.app.state.secret_metadata = meta
    return meta


@router.get("/platform-secrets")
def get_platform_secrets(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    meta: Any = Depends(secret_metadata),
) -> dict:
    """Each platform secret's status: set, enabled versions, the newest one's
    age, who declares it and what writes it; then the quota broker's account
    secrets, two rows each. A secret that cannot be read is a row saying so."""
    return platform_secret_status(meta, ctx.db, now=ctx.now)


# -- runner sizes -----------------------------------------------------------
#
# docs/design/runner-sizes.md §4.5. The logic is `swarm_api.runnersizes`; the
# bounds, the defaults and the class names are the frozen contract's. Any
# platform admin may change sizes, with a reason, and every version is audited
# and restorable (owner Q4). The scheduler reads the current version once per
# drain pass and pins it on the attempt, so a change applies to the NEXT
# admission and never to a running agent.

@router.get("/runner-sizes")
def get_runner_sizes(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The current version's sizes, the code defaults, the frozen bounds, which
    profiles follow a resize and which are fixed, and the list prices."""
    return runnersizes.served(runnersizes.current(ctx.db))


@router.post("/runner-sizes:preview")
def preview_runner_sizes(
    body: RunnerSizesPreviewRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """What the body would do, nothing written: every refusal, units and
    dollars an hour before and after, what the platform-wide pools admit, and
    the class's 7-day use (`recent_use`, design §4.9). A refused body is
    `valid: false` here, not a 422: the console previews as an admin types."""
    return runnersizes.preview(ctx.db, body.classes, now=ctx.now())


@router.put("/runner-sizes")
def put_runner_sizes(
    body: RunnerSizesRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Write version N+1 from version `base_version` and the body.

    409 `stale_base_version` when another admin saved in between; 422
    `size_out_of_bounds` naming every refusal; 422 `below_recent_peak` for an
    unconfirmed memory cut under the class's 7-day max peak (owner Q6).
    """
    payload = runnersizes.put(
        ctx.db, base_version=body.base_version, classes=body.classes, reason=body.reason,
        by=auth.email, confirm_below_recent_peak=body.confirm_below_recent_peak, now=ctx.now(),
    )
    ctx.metrics.admin_actions.labels(action="runner_sizes").inc()
    return payload


@router.post("/runner-sizes/restore")
def restore_runner_sizes(
    body: RunnerSizesRestoreRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Write a NEW version whose classes are `version`'s (0: the code
    defaults), `restored_from` naming it. The pointer is never rewound, so the
    audit reads forward."""
    payload = runnersizes.restore(
        ctx.db, base_version=body.base_version, version=body.version, reason=body.reason,
        by=auth.email, confirm_below_recent_peak=body.confirm_below_recent_peak, now=ctx.now(),
    )
    ctx.metrics.admin_actions.labels(action="runner_sizes").inc()
    return payload


@router.get("/runner-sizes/versions")
def list_runner_size_versions(
    limit: int = Query(default=50, ge=1, le=runnersizes.VERSIONS_PAGE_MAX),
    before: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The audit, newest first: who, when, why, what changed, and what a
    restore restored from. `before` pages back from a version."""
    return runnersizes.versions(ctx.db, limit=limit, before=before)


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
    """The tenant's ceiling: ONE number, the one the admin typed.

    Both `max_active` and `capacity_units` are written to `body.limit`, so the
    pool -- the smaller of the two, `Store.set_tenant_limits` -- becomes exactly
    the limit. Writing `max_active` alone is what held tenant `smoke` at 8 on
    2026-10-07: the owner raised it 8 -> 20 in the console, `capacity_units`
    stayed 8, the pool stayed 8, and this route answered 200 with a pool of 8
    that the console reported as saved (owner decision, same day: the ceiling
    is one number). PUT /v1/admin/tenants/{id}/limits still sets the two
    separately and keeps the min() rule.

    `capped_by` names what held the pool below the limit, for the console to
    say instead of a success: null when the pool is the limit. With both
    fields written it is null unless something wrote the tenant between the
    write and the read-back.
    """
    tenant = ctx.store.set_tenant_limits(
        tenant_id, max_active=body.limit, capacity_units=body.limit, by=auth.email
    )
    ctx.metrics.admin_actions.labels(action="limit_tenant").inc()
    pool = ctx.store.get_pool(f"tenant:{tenant_id}")
    return {
        "tenant": tenant_to_api(tenant),
        "pool": pool_to_api(pool) if pool is not None else None,
        "capped_by": _tenant_capped_by(tenant, pool, body.limit),
    }


def _tenant_capped_by(tenant: Tenant, pool: SlotPool | None, limit: int) -> str | None:
    """Why `tenant:<id>` is not at `limit` after a ceiling write, or None if it is."""
    if pool is not None and pool.hard_limit == limit:
        return None
    if pool is None:
        return "pool_missing"
    if tenant.capacity_units < limit:
        return "capacity_units"
    if tenant.max_active < limit:
        return "max_active"
    return "unknown"


@router.put("/tenants/{tenant_id}/limits")
def set_tenant_limits(
    tenant_id: str,
    body: TenantLimitsRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    if body.monthly_budget_usd is not None:
        # Refused rather than stored. There are no per-tenant budgets, built or
        # planned (owner decision, 2026-10-01; docs/cost-control.md section 2).
        # Per-attempt spend IS recorded (agent_worker/control.py `record_spend`),
        # but nothing enforces a budget from it, so accepting this would write a
        # number into Firestore, echo it back with a 200, enforce nothing, and
        # never reach ParkReason.BUDGET_EXHAUSTED. An admin would believe they
        # had a spend control. store.py states the standard this would violate:
        # "the document and the pool must move together or the limit is a lie".
        raise ValidationFailed(
            "monthly_budget_usd is not enforced by this control plane: per-tenant "
            "budgets are not built and not planned (owner decision, 2026-10-01), so "
            "the value could be stored but never acted on. Per-attempt cost is "
            "recorded on each attempt for reporting. Bound spend with max_active / "
            "capacity_units, which the scheduler really does enforce on every "
            "admission.",
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


@router.get("/tenants/{tenant_id}/findings-epic")
def get_findings_epic(
    tenant_id: str,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The tenant's wave epic (#638), or null when none is set."""
    if ctx.store.get_tenant(tenant_id) is None:
        raise NotFound(f"tenant {tenant_id!r} does not exist")
    return {"tenant_id": tenant_id, "findings_epic": ctx.store.get_findings_epic(tenant_id)}


@router.put("/tenants/{tenant_id}/findings-epic")
def set_findings_epic(
    tenant_id: str,
    body: TenantFindingsEpicRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Set the issue a review's MINOR findings are filed on, per tenant (#638).

    Read at workflow submission and copied into the gated step's signed
    dispatch block (`DispatchOptions.findings_epic`), so a change reaches the
    workflows submitted after it, never one already running. The number names
    an issue in the repository each run works on; the worker posts with the
    tenant's own git token. Null stops the filing.
    """
    epic = ctx.store.set_findings_epic(tenant_id, body.findings_epic)
    ctx.metrics.admin_actions.labels(action="findings_epic").inc()
    return {"tenant_id": tenant_id, "findings_epic": epic}


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
    """Every pool, with who last changed it through an admin route (#133).

    `admin_changed_by` / `admin_changed_at` are served HERE, on the admin read
    Admin > Pool limits uses, and deliberately not on `/v1/capacity`: that
    route serves pools to every tenant member, and an admin's email is not
    tenant data. Both are null for a pool no admin route has changed.
    """
    pools = sorted(ctx.store.list_pools(), key=lambda p: p.name)
    return {"pools": [{**pool_to_api(p), **pool_attribution(p)} for p in pools]}


def units_held_by_pool(leases: list[Lease]) -> dict[str, int]:
    """Per pool, the units these leases hold, by `Lease.held_units`: the one
    rule admission reserved and release returns by (#2950)."""
    held: dict[str, int] = {}
    for lease in leases:
        for name, units in lease.held_units().items():
            held[name] = held.get(name, 0) + units
    return held


@router.get("/leases")
def list_leases(
    tenant_id: str | None = Query(default=None),
    active_only: bool = Query(default=True),
    state: str | None = Query(default=None, description="LEASED or DISPATCHED"),
    overdue_only: bool = Query(default=False, description="dispatch_deadline passed with no heartbeat and no STARTING/RUNNING task"),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
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

    PAGED (F8). `truncated: true` used to be a dead end at the window; the
    page now carries `next_page_token`, set exactly when `truncated` is, and
    the rows behind it are the next page. `active_beyond_window` is per page:
    live leases not in THESE rows.
    """
    scan = ctx.store.scan_leases(
        tenant_id, active_only=active_only, limit=paged_limit(ctx, limit),
        page_token=page_token,
    )
    leases = scan.leases
    now = ctx.now()
    if state is not None:
        wanted = state.strip().upper()
        leases = [x for x in leases if x.state.value == wanted]
    if overdue_only:
        # The deadline first: it needs no read, and no lease inside it can be
        # overdue. The rest are judged below, once their tasks are read.
        leases = [x for x in leases if x.dispatch_overdue(now)]

    # One task read per distinct task, for the error text and for
    # `dispatch_overdue`, which a STARTING or RUNNING task clears (the lease's
    # own state stops at DISPATCHED -- `codec.lease_dispatch_overdue`).
    tasks: dict[str, Task | None] = {}
    errors: dict[str, str | None] = {}
    for lease in leases:
        if lease.task_id in errors:
            continue
        try:
            task = ctx.store.get_task(lease.tenant_id, lease.task_id, submitted_by=None)
            tasks[lease.task_id] = task
            # Masked by the task's own masker, as every task route serves it
            # (the PR #229 review): this row crosses tenants, and `last_error`
            # can be the agent's stderr tail.
            errors[lease.task_id] = masking_for(task).text(task.last_error)[0]
        except Exception:
            # A lease whose task is gone is itself a finding -- the reconciler
            # calls it task_missing. Do not let it fail the whole listing.
            tasks[lease.task_id] = None
            errors[lease.task_id] = None

    if overdue_only:
        leases = [
            x for x in leases if lease_dispatch_overdue(x, tasks.get(x.task_id), now)
        ]

    rows = []
    for lease in leases:
        row = lease_to_api(lease, task=tasks.get(lease.task_id), now=now)
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
            "heartbeat_grace_seconds": heartbeat_grace_seconds(core),
            "lease_timeout_seconds": core.lease_timeout_seconds,
        },
        "evaluated_at": now,
        "active_only": active_only,
        "tenant_id": tenant_id,
        # Weighted UNITS, not agents -- admission increments by the resource
        # class's units, so this and len(leases) are different numbers.
        "units_held": sum(lease.units for lease in leases),
        # Per pool, what these rows hold: the drift check's input against each
        # pool's `active`. Not `units_held` in every pool -- a provider pool
        # holds 1 per lease (contract request 129, #2950), and a lease from
        # before it holds its `units` there (`Lease.held_units`).
        "units_held_by_pool": units_held_by_pool(leases),
        # Live leases, under the tenant filter, that are not in these rows.
        # 0 is what makes a units_held / pool.active delta evidence.
        "active_beyond_window": scan.active_beyond_window,
        # The window was full and more lay behind it -- see the docstring for
        # why this is not the drift check's signal.
        "truncated": scan.truncated,
        # Rows state / overdue_only ran over.
        "examined": scan.examined,
        # The rows behind this page; null on the last one.
        "next_page_token": scan.next_page_token,
    }


@router.get("/failures")
def list_failures(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """FAILED tasks across every tenant, newest first (U19, P6).

    `GET /v1/tasks?state=FAILED` is tenant-scoped, and every task index leads
    with tenant_id, so "what is failing on the platform" had no answer short of
    one listing per tenant. This is that read, for a FULL admin only: it is not
    in `auth.POOL_ADMIN_ROUTES`, so the narrow pool-admin capability is refused
    by `admin_auth` like on every other admin route.

    Each row is a task row as `GET /v1/tasks` serves it -- masked by the task's
    own masker, `tenant_id` included, which says whose it is -- with its
    account read per tenant on the page (`accounts_for` never reads across
    tenants). A FAILED task holds no lease, so `waiting_for` and the heartbeat
    fields are null. Paged by the same (created_at, id) keyset as `GET /v1/tasks`.
    """
    page = ctx.store.list_failures(limit=paged_limit(ctx, limit), page_token=page_token)
    by_tenant: dict[str, list] = {}
    for task in page.items:
        by_tenant.setdefault(task.tenant_id, []).append(task)
    accounts: dict[str, dict] = {}
    totals: dict[str, dict] = {}
    for tenant_id, tasks in by_tenant.items():
        accounts.update(accounts_for(ctx.db, tenant_id, tasks))
        # Every attempt's spend and time, per tenant like the accounts, so the
        # row carries the same fields a member's task row does
        # (`swarm_api.attempt_totals`).
        totals.update(totals_for(ctx.db, tenant_id, [t.id for t in tasks]))
    payload = {
        "tasks": [
            with_totals(
                task_to_api(
                    task,
                    account=accounts.get(task.id),
                    console_url=ctx.settings.console_url,
                ),
                totals,
            )
            for task in page.items
        ],
        "next_page_token": page.next_page_token,
    }
    # `submitted_by_name` beside `submitted_by`, as `GET /v1/tasks` serves it.
    add_names(name_lookup(ctx), payload["tasks"], ("submitted_by",))
    return payload


@router.post("/history:purge")
def purge_history(
    body: HistoryPurgeRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Delete FAILED, CANCELLED or DEAD_LETTERED workflows and tasks, audited.

    Owner request 2026-10-11. Full admin only: the route is not in
    `POOL_ADMIN_ROUTES`, so the pool-admin capability and a tenant member are
    both refused by `admin_auth`. A DRY RUN unless the body says
    `dry_run: false` and `confirm: "purge"`; the dry run lists exactly what
    the delete would take and changes nothing. What is deleted, what is never
    deleted, the audit written before each delete and the tenant-derived
    bucket prefix are all in `swarm_api.purge`.
    """
    query = parse_query(
        states=body.states, before=body.before, tenant_id=body.tenant_id,
        exclude_ids=body.exclude_ids, dry_run=body.dry_run, confirm=body.confirm,
        limit=body.limit, page_token=body.page_token,
    )
    result = HistoryPurge(ctx.db, ctx.artifact_purger, now=ctx.now).run(query, by=auth.email)
    ctx.metrics.admin_actions.labels(
        action="history_purge_dry_run" if query.dry_run else "history_purge").inc()
    return result


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

    `truncated` says the store's window (500 documents, in document-id
    order) left matching documents out. Without it a cut set and the whole
    set were the same response (#76).

    `feeds_pool` (G5-02, QA 2026-10-07) is the pool each row's cap feeds,
    `provider:<provider>:tenant:<tenant>`, as `/v1/capacity` serves it, or
    null when no such pool document exists. The row's own `effective_limit`
    is the QUOTA DOCUMENT's (`QuotaState.effective_limit`) and is one input to
    that pool; the pool's `effective_limit` is what admission enforces
    (`SlotPool.has_capacity`). The live console drew `Quota cap 50` for a pool
    Pools showed at 40, and nothing on this route could say which binds.
    Served here rather than joined in the client so the two figures come
    from one request and cannot be of different ages. Additive: no existing
    key changed.
    """
    scan = ctx.store.scan_quota(tenant_id)
    pools = _provider_pools(ctx, {(q.provider, q.tenant_id) for q in scan.states})
    return {
        "quota": [
            {**quota_to_api(q), "feeds_pool": pools.get(_provider_pool_name(q.provider, q.tenant_id))}
            for q in sorted(scan.states, key=lambda q: (q.provider, q.tenant_id))
        ],
        "truncated": scan.truncated,
        "tenant_id": tenant_id,
        "generated_at": ctx.now(),
    }


@router.put("/quota/{provider}/{tenant_id}/adaptive-target")
def reset_adaptive_target(
    provider: str,
    tenant_id: str,
    body: AdaptiveTargetRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    broker: AccountPool = Depends(account_pool),
) -> dict:
    """Reset a throttled tenant's AIMD target, through the quota broker.

    QUOTA-RESET (owner, 2026-10-14). One 429 halves `adaptive_target` and the
    climb back is +1 per 20 consecutive successes, so a tenant can sit far
    below the ceiling an admin chose long after the cause is gone. The broker
    is the only writer of quota documents, so this route PROXIES: it checks
    the caller is a platform admin (`admin_auth`; not a pool admin -- this
    route is not in `POOL_ADMIN_ROUTES`), that the provider and tenant exist,
    and forwards the admin's verified email as `requested_by`. The broker
    clamps to `configured_hard_max`, refuses an EXHAUSTED tenant unless
    `clear_cooldown`, and audits it in `quota_audit`.
    docs/runbooks/quota-throttled-tenant.md.
    """
    name = _check_provider(provider)
    if ctx.store.get_tenant(tenant_id) is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    result = broker.set_adaptive_target(
        name,
        tenant_id,
        target=body.target,
        reason=body.reason,
        clear_cooldown=body.clear_cooldown,
        requested_by=auth.email,
    )
    ctx.metrics.admin_actions.labels(action="quota_adaptive_target_reset").inc()
    return result


#: The `admin_audit` action a configured-max change is recorded under.
QUOTA_HARD_MAX_AUDIT_ACTION = "quota_hard_max_set"


@router.put("/quota/{provider}/{tenant_id}/hard-max")
def set_quota_hard_max(
    provider: str,
    tenant_id: str,
    body: HardMaxRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    broker: AccountPool = Depends(account_pool),
) -> dict:
    """Set a tenant's `configured_hard_max`, through the quota broker.

    POOL-LIMITS-QUOTA (BGD, 2026-10-25, part of #2302: "Console edits
    configured max"). The effective limit of a `provider:<p>:tenant:<t>` pool
    is the least of its hard limit, this max and the AIMD target, and until
    this route the max could only be changed by calling the broker directly.
    It PROXIES as `reset_adaptive_target` does: a platform admin only
    (`admin_auth`; not in `POOL_ADMIN_ROUTES`, so a pool admin is refused),
    the provider and the tenant must exist (404 before the broker is
    called), and the broker stays the only writer of quota documents. The
    broker clamps the target down to a lowered max in the same write
    (`QuotaBroker.set_hard_max`); the answer says whether it did.

    AUDITED TWICE. The broker records the change in `quota_audit`, in the
    quota document's own transaction, with the `requested_by` and `reason`
    this route forwards (ACCOUNT-429-FOLLOWUP; BGD, 2026-10-25), and this
    route writes one `admin_audit` entry once the broker has answered: who,
    why, and the max and target before and after. Who is the admin's
    verified email from the token in both -- the body cannot name one.
    """
    name = provider.strip().lower()
    if name not in known_providers():
        raise NotFound(f"provider {provider!r} not found",
                       detail={"known_providers": list(known_providers())})
    if ctx.store.get_tenant(tenant_id) is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    before = next((q for q in ctx.store.list_quota(tenant_id)
                   if q.provider == name and q.tenant_id == tenant_id), None)
    result = broker.set_hard_max(name, tenant_id, hard_max=body.hard_max,
                                 requested_by=auth.email, reason=body.reason)
    after = result.get("quota") if isinstance(result, dict) else None
    after = after if isinstance(after, dict) else {}
    from_target = None if before is None else before.adaptive_target
    to_target = after.get("adaptive_target")
    change = {
        "provider": name,
        "tenant_id": tenant_id,
        "from_hard_max": None if before is None else before.configured_hard_max,
        "to_hard_max": after.get("configured_hard_max", body.hard_max),
        "from_target": from_target,
        "to_target": to_target,
        # The broker lowered the target to fit the new max.
        "target_clamped": (isinstance(from_target, int) and isinstance(to_target, int)
                           and to_target < from_target),
        "reason": body.reason,
    }
    ctx.db.collection(AUDIT_COLLECTION).document().set({
        "action": QUOTA_HARD_MAX_AUDIT_ACTION,
        "target_tenant_id": tenant_id,
        "by": auth.email,
        "at": ctx.now(),
        "detail": change,
    })
    ctx.metrics.admin_actions.labels(action="quota_hard_max_set").inc()
    return {**result, "change": {**change, "requested_by": auth.email}}


def _provider_pool_name(provider: str, tenant_id: str) -> str:
    # The name `swarm_common.models.pool_names_for` gives a profile's
    # per-tenant provider pool.
    return f"provider:{provider}:tenant:{tenant_id}"


def _provider_pools(ctx: AppContext, pairs: set[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """The provider pools the quota rows feed, by name, as `pool_to_api` serves them.

    One listing read, as `/v1/capacity` does. A listing that filled its window
    is not evidence a name outside it is absent, so only then is each missing
    name read by itself; a name absent after that has no pool document.
    """
    if not pairs:
        return {}
    page = 500
    listed = ctx.store.list_pools(limit=page)
    by_name = {pool.name: pool for pool in listed}
    wanted = {_provider_pool_name(provider, tenant) for provider, tenant in pairs}
    if len(listed) >= page:
        for name in sorted(wanted - set(by_name)):
            pool = ctx.store.get_pool(name)
            if pool is not None:
                by_name[name] = pool
    return {name: pool_to_api(by_name[name]) for name in wanted if name in by_name}


def _workspace_records(ctx: AppContext, tenant_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Each tenant's `workspaces/{tenant_id}` record, by tenant id, from ONE
    `get_all` over the page: never one read per tenant. A tenant Terraform
    made has no record and is absent from the answer. Raises when the batch
    fails; the caller then serves the page without the fields."""
    if not tenant_ids:
        return {}
    collection = ctx.db.collection(WORKSPACES)
    return {
        snap.id: snap.to_dict() or {}
        for snap in ctx.db.get_all([collection.document(t) for t in tenant_ids])
        if snap.exists
    }


@router.get("/tenants")
def list_tenants(
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every tenant, and the workspace the in-cloud job made for it (WK-ADMIN-LIST).

    `workspace_kind` (personal, group or service) and `workspace_id` (the
    record's `w-` id) tell the job's tenants from Terraform's, whose two fields
    are null (docs/design/workspace-kinds.md §4.2.2). The design names the
    first `kind`, but `kind` is already the frozen `Tenant.kind` (user or
    group) that the console reads, and a service workspace is a `user` tenant,
    so the record's kind is served beside it rather than over it. Nothing
    else of the record is served: not its principal, not its state.

    A failed read of the records serves the tenants without the two fields
    and `workspaces_unread: true`, never a 500: the tenant list is what this
    page is for.
    """
    tenants = ctx.store.list_tenants()
    rows = [tenant_to_api(t) for t in tenants]
    try:
        records = _workspace_records(ctx, [t.tenant_id for t in tenants])
    except Exception as exc:  # noqa: BLE001 - the list is served without the fields
        log.warning("admin tenants: workspace records unread (%s)", type(exc).__name__)
        return {"tenants": rows, "workspaces_unread": True}
    for row in rows:
        record = records.get(row["tenant_id"])
        row["workspace_kind"] = kind_of(record) if record is not None else None
        row["workspace_id"] = (record or {}).get("workspace_id")
    return {"tenants": rows, "workspaces_unread": False}


@router.post("/workflows/rollup")
def rollup_workflows(
    tenant_id: str = Query(..., min_length=1),
    limit: int | None = Query(default=None, ge=1),
    seal_outcomes: bool = Query(default=False),
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

    `seal_outcomes=true` ALSO SEALS the tenant's ended `outcome_days`
    (COUNTS-SEAL, #2149; `Outcomes.seal_recent`): BGD's decision is that the
    existing rollup job seals each day overnight, and this is the route that
    job already calls, as the identity it already presents, so the seal needs
    no new job, no new route on `auth.ROLLUP_SWEEPER_ROUTES` and no IAM row.
    It never touches today's live day, is idempotent and is bounded by the
    derive budget. It runs FIRST and never raises: a failed seal is reported
    under `outcomes` and the workflow sweep still runs, and a failing workflow
    sweep cannot starve the seal. An unregistered tenant is not sealed, for
    `rollup_outcomes`' reason: a misspelt id would build a rollup for a tenant
    nobody has. Without the flag the response is what it always was.
    """
    sealed: dict | None = None
    if seal_outcomes:
        if ctx.store.get_tenant(tenant_id) is None:
            sealed = {"tenant_id": tenant_id, "skipped": "unknown_tenant"}
        else:
            try:
                sealed = ctx.outcomes.seal_recent(tenant_id=tenant_id)
            except Exception:  # noqa: BLE001 -- the next tick seals what this one did not
                log.exception("outcome seal failed tenant=%s", tenant_id)
                sealed = {"tenant_id": tenant_id, "error": "seal_failed"}
        ctx.metrics.admin_actions.labels(action="outcomes_seal").inc()
    results, report = ctx.rollups.sweep(tenant_id, limit=paged_limit(ctx, limit))
    ctx.metrics.admin_actions.labels(action="workflow_rollup").inc()
    body: dict = {
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
    if sealed is not None:
        body["outcomes"] = sealed
    return body


@router.post("/runs/advance")
def advance_runs(
    tenant_id: str = Query(..., min_length=1),
    limit: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Advance one tenant's live issue runs (#454) -- the Cloud Scheduler tick.

    Modelled on POST /v1/admin/workflows/rollup, for the same reasons, and
    called the same way: one job per registered tenant, as the rollup
    sweeper (terraform/modules/scheduler/jobs.tf, `issue_run_advance`).

    WHY THIS EXISTS beside the advance every run read already does (owner
    decision on #454, "Advancing runs: swarm-api, on a Cloud Scheduler
    tick"): a run moves on a read, so a run nobody is watching never moved,
    and a `plan_approval: auto` run -- whose point is that nobody watches --
    never got its plan approved or its issue written back.

    TENANT IS EXPLICIT, not the caller's own, which for the sweeper is a
    personal tenant nobody has. Each run is read under it and an auto
    approval submits in the run's own tenant as the run's creator
    (`routes.runs.run_owner_auth`), never as the caller. Bounded by the page
    size per tick, oldest first; truncation is reported, as the rollup does.

    HELD CHILD RUNS ARE RELEASED ON THE SAME TICK (agent-autonomy.md §3.6,
    decided 2026-10-22 under BGD's overnight rule): after the runs move, each
    `after_merged` child run of THIS tenant whose predecessor is now DONE with
    its pull request merged is started, and one whose predecessor ended
    unmerged is cancelled (`children.release_held_runs`). After, not before:
    the tick that moves a predecessor to DONE starts its successor in the same
    response rather than one tick later. A held row holds no run, planner or
    workflow (invariant 1), and the release reads only `tenant_id`'s ledger
    (invariant 9). `held` is its counts.
    """
    report = advance_tenant_runs(ctx, tenant_id, limit=paged_limit(ctx, limit))
    held = release_held_runs(ctx, tenant_id)
    ctx.metrics.admin_actions.labels(action="run_advance").inc()
    return {
        "tenant_id": tenant_id,
        "report": report.to_api(),
        # Only the runs that could not be advanced, by id and error code. A
        # healthy tick returns an empty list, which is an answer.
        "failures": report.failures,
        "held": held,
    }


@router.post("/merges/wake")
def wake_merges(
    tenant_id: str = Query(..., min_length=1),
    limit: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Mark one tenant's CI-waiting merge steps whose checks have settled (lane MS2).

    Called every minute by the per-tenant Cloud Scheduler job `merge_wake`
    (terraform/modules/scheduler/jobs.tf), as the rollup sweeper, admitted
    to this route by `auth.ROLLUP_SWEEPER_ROUTES`; an admin may call it too,
    as the other ticks, to wake a merge now. docs/merge-step.md "Revised
    2026-10-06" §1: a merge step whose pull request's checks are still
    running parks CI_PENDING, holding nothing. For each such park of the
    named tenant this reads the pull request and its checks at the recorded
    head with that tenant's `-git` token, at most once per
    `issueci.CI_READ_SECONDS`, and writes the wake marker on the ones with
    nothing left pending (`mergewake.wake_tenant`). It never moves a task:
    the scheduler's `_promote_ci_waits` returns a marked park to READY, and
    only admission takes capacity (invariant 1).

    TENANT IS EXPLICIT, as on the other ticks. `limit` is the page of parks
    read per tick; truncation is reported.
    """
    report = wake_tenant(ctx, tenant_id, limit=paged_limit(ctx, limit))
    ctx.metrics.admin_actions.labels(action="merge_wake").inc()
    # The CI fixer's post-back (#263) rides this tick: the same tenant, the
    # same `-git` token and writer, the same minute, and no second Scheduler
    # job to keep in step with this one. It comments on a red pull request
    # how its fix step ended (`cifix.postback_tenant`); it never moves a task.
    postback = postback_ci_fixes(ctx, tenant_id, limit=paged_limit(ctx, limit))
    ctx.metrics.admin_actions.labels(action="ci_fix_postback").inc()
    # Lane SPEED-D3 rides it too, for the same reasons: workflows submitted
    # with `after` wait, PARKED, for pull requests of their own repository to
    # merge; this reads each with the same tenant's token and marks the gate
    # merged or closed (`prgate.wake_tenant`). It never moves a task: the
    # scheduler's dependency sweep promotes or ends the root.
    gates = wake_after_prs(ctx, tenant_id, limit=paged_limit(ctx, limit))
    ctx.metrics.admin_actions.labels(action="after_prs_wake").inc()
    # The merge train coordinator (TRAIN-WS, docs/merge-trains.md §4) rides
    # this tick too, for the reason the post-back does: the same tenant, the
    # same `-git` token and writer, the same minute. It composes, tests,
    # lands and ejects the tenant's trains and marks each concluded step's
    # park; it never moves a task. A tenant with no train entries reads two
    # empty queries and nothing else.
    trains = tick_merge_trains(ctx, tenant_id, limit=paged_limit(ctx, limit))
    ctx.metrics.admin_actions.labels(action="merge_train").inc()
    return {
        "tenant_id": tenant_id,
        "report": report.to_api(),
        # Only the parks that could not be read, by id and code. A healthy
        # tick returns an empty list, which is an answer.
        "failures": report.failures,
        "ci_fix": postback.to_api(),
        "ci_fix_failures": postback.failures,
        "after_prs": gates.to_api(),
        "after_prs_failures": gates.failures,
        "merge_train": trains.to_api(),
        "merge_train_failures": trains.failures,
    }


class RegistrationOwnerNotMember(Forbidden):
    """The registration's creator is no longer a member of its tenant: the
    poll submits nothing more as them (the issue-run tick's rule)."""

    code = "owner_not_member"


def registration_owner_auth(ctx: AppContext, tenant_id: str):
    """The submitter of a polled index run: the registration's creator, in its tenant.

    The issue-run tick's shape (`routes.runs.run_owner_auth`), for the same
    reason: the poll's caller is the scheduler's identity, which is no tenant
    member and must submit nothing as itself. Built from the stored tenant
    and the stored `created_by`, never from the caller, and only while that
    person is still a member (asked of the directory on every submission,
    invariant 9). Nothing wider than an ordinary member: not an admin, no
    member scope.
    """

    def _owner(record: Mapping[str, Any]) -> AuthContext:
        tenant = ctx.store.get_tenant(tenant_id)
        if tenant is None:
            raise NotFound(f"tenant {tenant_id!r} not found")
        email = str(record.get("created_by") or "")
        if not ctx.authenticator.is_tenant_member(email, tenant):
            raise RegistrationOwnerNotMember(
                f"the registration's creator {email or '(not recorded)'} is no longer a "
                f"member of tenant {tenant_id!r}, so nothing is indexed on their behalf; "
                "register the repository again as a current member"
            )
        return AuthContext(
            principal=Principal(
                email=email,
                # Not a token subject: this context was never authenticated.
                subject=f"repo-index-poll:{record.get('repo_id')}",
                domain=email.rsplit("@", 1)[-1],
                groups=(),
            ),
            tenant_id=tenant_id,
            is_admin=False,
            tenant_principal=tenant.principal,
        )

    return _owner


@router.post("/repositories/poll")
def poll_repositories(
    tenant_id: str = Query(..., min_length=1),
    limit: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One tenant's repository-index triggers (docs/repo-index.md §3.3, lane RI4).

    Called every five minutes by the per-tenant Cloud Scheduler job
    `repo_index_poll` (terraform/modules/scheduler/jobs.tf). For each of the
    tenant's registrations: settle finished index runs, read the default
    branch's head with the last ETag (a 304 costs no rate limit), store it,
    and queue a run where the head moved and the minimum change interval has
    passed, or the interval has; a run in flight is never duplicated -- the
    newer head becomes pending (`repoindex.RepoIndex.poll`).

    ONLY THE SCHEDULER'S IDENTITY, as §6.1 says: the rollup sweeper, admitted
    to this route by `auth.ROLLUP_SWEEPER_ROUTES`. Not an admin either: an
    operator who wants a poll now runs the job (`gcloud scheduler jobs run`),
    and a person who wants an index now uses "Index now" in their own tenant.

    TENANT IS EXPLICIT, as on the other ticks. Runs are submitted in that
    tenant as each registration's creator (`registration_owner_auth`), never
    as the caller, and they wait for admission like any task (invariants
    1-3). `limit` is the page size registrations are read in.
    """
    if not auth.is_rollup_sweeper:
        raise Forbidden(
            "POST /v1/admin/repositories/poll is the repo_index_poll scheduler job's; "
            "run the job, or use Index now on the repository"
        )
    service = RepoIndex.from_context(ctx)
    report = service.poll(
        tenant_id, tenant=service.tenant(tenant_id),
        owner_auth=registration_owner_auth(ctx, tenant_id),
        page_size=paged_limit(ctx, limit),
    )
    ctx.metrics.admin_actions.labels(action="repo_index_poll").inc()
    return {
        "tenant_id": tenant_id,
        "report": report.to_api(),
        # Only the registrations that could not be polled, by id and code. A
        # healthy tick returns an empty list, which is an answer.
        "failures": report.failures,
    }


@router.post("/repositories/push")
def push_repository(
    body: RepositoryPush,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A verified push to a registered repository's default branch (graph-service.md §6.2).

    Called by the public receiver `swarm-github-hooks` (lane GS-HOOKS) after it
    has checked `X-Hub-Signature-256`, deduped `X-GitHub-Delivery` and kept
    only a non-deleting push to `refs/heads/<default_branch>`. It forwards
    `{forge, full_name}` and nothing else; `RepositoryPush` refuses any other
    field, so a payload's `after` sha is never read or stored.

    Every registration of that repository, in every workspace that registered
    it, is polled now through the poll's own `RepoIndex._poll_one`
    (`RepoIndex.push`): its head re-read with that workspace's credential, the
    throttle and the in-flight rule applied, a run queued in that tenant as
    the registration's creator (`registration_owner_auth`). Only registrations
    whose `on_change` is `webhook` are read; the five-minute poll stays the
    fallback for a missed delivery (owner decision 2).

    The answer carries counts and, for a registration that could not be
    polled, its opaque `repo_id` and a code: never a tenant id, a head or a
    name, since the receiver needs none of them.

    ONE IDENTITY, as `poll_repositories` admits only the scheduler's: the
    rollup sweeper (§6.2 step 4). Not an admin and not a member: a push is
    forwarded by the receiver, never typed by a person.
    """
    if not auth.is_rollup_sweeper:
        raise Forbidden(
            "POST /v1/admin/repositories/push is the swarm-github-hooks receiver's; "
            "a person who wants an index now uses Index now on the repository"
        )
    service = RepoIndex.from_context(ctx)
    owner, repo = body.owner_repo
    report = service.push(
        body.forge, owner, repo,
        owner_auth_for=lambda tenant_id: registration_owner_auth(ctx, tenant_id),
        page_size=paged_limit(ctx, None),
    )
    ctx.metrics.admin_actions.labels(action="repo_index_push").inc()
    return {"report": report.to_api(), "failures": report.failures}


def _push_fields(envelope: Mapping[str, Any]) -> dict[str, str]:
    """`reason`, `task_id` and `tenant_id` from a Pub/Sub push envelope.

    The attributes first, then the JSON `data`: both the worker
    (`agent_worker.finishwake`) and `waker.PubSubWaker` put the same fields in
    each. Anything unreadable is simply absent.
    """
    message = envelope.get("message") if isinstance(envelope, Mapping) else None
    message = message if isinstance(message, Mapping) else {}
    fields: dict[str, Any] = {}
    data = message.get("data")
    if isinstance(data, str) and data:
        try:
            decoded = json.loads(base64.b64decode(data, validate=True).decode("utf-8"))
        except (ValueError, binascii.Error):
            decoded = None
        if isinstance(decoded, Mapping):
            fields.update(decoded)
    attributes = message.get("attributes")
    if isinstance(attributes, Mapping):
        fields.update(attributes)
    return {k: str(fields[k]) for k in ("reason", "task_id", "tenant_id")
            if isinstance(fields.get(k), str) and fields[k]}


@router.post("/tasks/finished")
def task_finished_push(
    envelope: dict[str, Any] = Body(default_factory=dict),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    vault: Any = Depends(browser_vault),
) -> dict:
    """A `task_finished` wake, pushed by the scheduler wake topic's second subscription (#748).

    The subscription (`api_task_finished`, terraform/modules/scheduler/main.tf)
    carries only `reason = "task_finished"` and presents the rollup sweeper's
    identity, admitted to this route by `auth.ROLLUP_SWEEPER_ROUTES`. For the
    finished task's gated integrators that need no worker, swarm-api opens
    the pull request itself and ends the step (`verdictpublish`); every other
    dependant is left to the scheduler, which receives the same message. A QA
    target's discovery step stores what it found on the target
    (`QaTargets.settle_discovery`, lane QA-DISCOVER), and a QA run's capture
    steps' found routes are merged into their leg (`QaTargets.settle_run`,
    lane QAENV-SETTLE).

    ALWAYS 200 to a well-formed push. A wake is a doorbell: this route reads
    everything again, and anything it cannot do is the worker's, after the
    scheduler's hold ends. Redelivering a push would only ring again.
    """
    if not auth.is_rollup_sweeper and not auth.is_admin:
        raise Forbidden("POST /v1/admin/tasks/finished is the wake topic's push subscription's")
    fields = _push_fields(envelope)
    task_id = fields.get("task_id", "")
    if fields.get("reason") != TASK_FINISHED or not task_id:
        return {"handled": False, "reason": fields.get("reason")}
    report = on_task_finished(ctx, task_id, tenant_id=fields.get("tenant_id"))
    # A QA target's discovery step ended: store the screens it found, or why
    # none (lane QA-DISCOVER). Never raises: every read of the target settles
    # it too, so a wake this could not use costs one read's delay.
    try:
        QaTargets(ctx.db, now=ctx.now).settle_discovery(
            task_id, ctx.inspection, tenant_id=fields.get("tenant_id"))
    except Exception:  # noqa: BLE001 -- a doorbell: the target's next read settles it
        log.exception("qa target discovery settle failed task=%s", task_id)
    # A step of a leg's QA run ended: once its capture steps all have, merge
    # what their crawls found into that leg's `discovered` (qa-environments.md
    # §5, lane QAENV-SETTLE). Never raises: the run's next step to end rings again.
    try:
        QaTargets(ctx.db, now=ctx.now).settle_run(task_id, ctx.inspection, tenant_id=fields.get("tenant_id"))
    except Exception:  # noqa: BLE001 -- a doorbell: the run's next ended step settles it
        log.exception("qa run settle failed task=%s", task_id)
    # A personal workflow that this step ended loses its browser session
    # carry now (docs/design/live-browser.md §5.5.1, lane LB-L9b). Never
    # raises; the carry sweep and the 24-hour TTL are behind it.
    CarryReaper(db=ctx.db, vault=vault, now=ctx.now).on_task_finished(
        task_id, store=ctx.store, rollups=ctx.rollups
    )
    ctx.metrics.admin_actions.labels(action="task_finished_push").inc()
    return {"handled": True, "report": report.to_api()}


@router.post("/outcomes/rollup")
def rollup_outcomes(
    tenant_id: str = Query(..., min_length=1),
    since: str = Query(..., min_length=1, description="YYYY-MM-DD, a UTC day, inclusive"),
    until: str | None = Query(default=None, description="YYYY-MM-DD, exclusive; default tomorrow"),
    repair: bool = Query(default=False),
    build_missing: bool = Query(default=True),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Backfill and drift-check one tenant's `outcome_days` rollup, <= 31 UTC days.

    Modelled on POST /v1/admin/workflows/rollup, for the same two reasons.

    WHY IT EXISTS beside the write-on-read GET /v1/outcomes already does: a
    cold 90-day view would spend its whole derive budget and come back with
    most days `unread`. This builds those days ahead of time, and it is the
    DRIFT CHECK -- it re-derives every stored sealed day and compares.

    A disagreement is REPORTED and repaired only with repair=true; a repaired
    day still reads its differences with `repaired: true`, because a repair
    that leaves no trace is the silent resolution rollup.drift_of forbids. A
    day whose derive could not complete counts as `unknown`, never
    "disagree".

    TENANT IS EXPLICIT, not the admin's own tenant_scope, which would sweep
    the wrong tenant. It must exist: a misspelt id would otherwise build a
    rollup for a tenant nobody has.
    """
    if ctx.store.get_tenant(tenant_id) is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    result = ctx.outcomes.maintain(
        tenant_id=tenant_id,
        since=since,
        until=until,
        repair=repair,
        build_missing=build_missing,
    )
    ctx.metrics.admin_actions.labels(action="outcomes_rollup").inc()
    return result


# ---------------------------------------------------------------------------
# Every group workspace's roles, for Admin › People (lane ROLES-ADMIN-LIST)
# ---------------------------------------------------------------------------

#: Firestore refuses an `in` over more than 30 values, and one page's grants
#: are read with one `in` over its workspace ids, so a page is at most 30.
ROLES_PAGE_MAX = 30
ROLES_PAGE_DEFAULT = 20
#: How many grant documents one page's read looks at. A page that fills it is
#: not evidence the grants past it are absent -- the newest grant of someone on
#: the page may be among them -- so every workspace on such a page is served
#: `unavailable` (`too_many_grants`), never a roster that could be missing the
#: grant that narrowed someone. `GET /v1/admin/workspaces/{id}/roles` still
#: reads such a workspace alone.
ROLES_PAGE_SCAN = 5000


def _role_workspaces(ctx: AppContext) -> list[tuple[str, str, str]]:
    """(tenant_id, group, registered_by) for every registered group workspace:
    `TENANT_GROUPS`, then the `ready` job-made group records, in the order and
    under the rule a platform admin's reach walks them (`Authenticator.
    _tenant_groups` then `_reachable_groups`: the first group per id, every
    `u-` id dropped), read through `Authenticator.registered_group_workspaces`:
    the authenticator's own cached registry, never one built per request. A
    registry that cannot be read is its 503, never a list without the
    job-made groups."""
    terraform = roles.registered_group_tenants(ctx.settings)
    every = ctx.authenticator.registered_group_workspaces()
    return [(tenant_id, group, "terraform" if tenant_id in terraform else "workspace_job")
            for tenant_id, group in every.items()]


def _collided(ctx: AppContext, page: list[tuple[str, str, str]]) -> set[str]:
    """The ids on the page whose `tenants/` document belongs to another
    principal: the check `_registered` makes per workspace
    (`Store.assert_tenant_scope`), as ONE `get_all` over the page."""
    collection = ctx.db.collection(TENANTS)
    groups = {tenant_id: group for tenant_id, group, _ in page}
    out: set[str] = set()
    for snap in ctx.db.get_all([collection.document(t) for t in groups]):
        if not snap.exists:
            continue
        stored = str((snap.to_dict() or {}).get("principal") or "").strip().lower()
        if stored and stored != groups.get(snap.id, ""):
            out.add(snap.id)
    return out


def _page_grants(ctx: AppContext, tenant_ids: list[str]) -> tuple[dict[tuple[str, str], list[dict]], bool]:
    """Every grant of the page's workspaces, by (tenant_id, subject), newest
    first, from ONE query -- never one per workspace -- and whether the read
    stopped at `ROLES_PAGE_SCAN`."""
    try:
        snaps = list(ctx.db.collection(roles.GRANTS_COLLECTION)
                     .where(filter=FieldFilter("tenant_id", "in", tenant_ids))
                     .limit(ROLES_PAGE_SCAN).stream())
    except Exception as exc:  # noqa: BLE001 - any Firestore failure is "unknown"
        log.warning("admin workspace roles: grants unreadable (%s)", type(exc).__name__)
        raise RoleUnavailable("the workspaces' roles could not be read; retry shortly") from None
    by_subject: dict[tuple[str, str], list[dict]] = {}
    for snap in snaps:
        doc = snap.to_dict() or {}
        tenant_id, subject = doc.get("tenant_id"), doc.get("subject")
        if isinstance(tenant_id, str) and isinstance(subject, str):
            by_subject.setdefault((tenant_id, subject), []).append(doc)
    for docs in by_subject.values():
        docs.sort(key=lambda d: str(d.get("granted_at") or ""), reverse=True)
    return by_subject, len(snaps) >= ROLES_PAGE_SCAN


class _Unverified(Exception):
    """A subject's newest `roles.SCAN_LIMIT` grants all failed verification:
    what `RoleStore.newest_valid` answers `role_unavailable`."""


def _newest_valid(ctx: AppContext, keys: tuple[str, ...], tenant_id: str, subject: str,
                  docs: list[dict]) -> roles.Grant | None:
    """`RoleStore.newest_valid` over grants already read: the newest one that
    has the right shape and verifies, within the same bound."""
    for doc in docs[:roles.SCAN_LIMIT]:
        problem = roles.shape_problem(doc, tenant_id, subject)
        if problem is None and not roles.grant_verifies(keys, doc):
            problem = "mac"
        if problem is None:
            return roles.Grant.from_doc(doc)
        ctx.metrics.role_grant_invalid.labels(reason=problem).inc()
    if len(docs) >= roles.SCAN_LIMIT:
        raise _Unverified()
    return None


def _roster_entry(ctx: AppContext, keys: tuple[str, ...], tenant_id: str,
                  grants: dict[tuple[str, str], list[dict]]) -> dict[str, Any]:
    """One workspace's default and overrides, as `roles.admin_payload` decides
    them: the default `*` grant else the migration default, and each person
    whose newest valid grant names a role, newest first."""
    default = _newest_valid(ctx, keys, tenant_id, roles.DEFAULT_SUBJECT,
                            grants.get((tenant_id, roles.DEFAULT_SUBJECT), []))
    people: list[tuple[str, roles.Grant]] = []
    for (tid, subject), docs in grants.items():
        if tid != tenant_id or subject == roles.DEFAULT_SUBJECT:
            continue
        grant = _newest_valid(ctx, keys, tenant_id, subject, docs)
        if grant is not None and grant.role is not None:
            people.append((subject, grant))
    people.sort(key=lambda item: item[1].granted_at, reverse=True)
    granted = default is not None and default.role in roles.DEFAULT_ROLES
    return {
        "default_role": default.role if granted else roles.MIGRATION_DEFAULT,  # type: ignore[union-attr]
        "default_source": "grant" if granted else "migration",
        "people": [roles.grant_api(email, g) for email, g in people[:roles.ROSTER_MAX]],
    }


@router.get("/workspaces/roles")
def list_workspace_roles(
    page_size: int = Query(default=ROLES_PAGE_DEFAULT, ge=1, le=ROLES_PAGE_MAX),
    page_token: str | None = Query(default=None),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every registered group workspace's roles, a page at a time: Admin ›
    People's Workspace roles card (docs/design/workspace-roles.md §4.3, §4.8;
    lane ROLES-ADMIN-LIST, part of #1518).

    Each entry is a group workspace -- a Terraform group or a `ready` group
    the workspace job made (`registered_by`) -- with its default role and its
    source, and each person's own role with who granted it, when and through
    what (`roles.grant_api`: never a grant's document id, never its MAC).
    Never a personal or service workspace: neither is among the registered
    groups, and a `u-` id is dropped (§4.2.1, §4.5).

    BATCHED: a page costs ONE `get_all` of its `tenants/` documents (the
    collision check) and ONE query of its grants (`tenant_id in` the page),
    however many workspaces it holds. A workspace whose grants did not verify
    within the bound, whose id collides with another principal's, or whose
    page filled `ROLES_PAGE_SCAN`, is served `unavailable` with a code and
    the rest of the page is still served.

    `page_token` is the last tenant id of the previous page, which
    `next_page_token` carries; null on the last page.
    """
    workspaces = _role_workspaces(ctx)
    start = 0
    if page_token is not None:
        ids = [tenant_id for tenant_id, _, _ in workspaces]
        if page_token not in ids:
            raise ValidationFailed(
                "page_token names no registered group workspace; read from the first page",
                detail={"field": "page_token"})
        start = ids.index(page_token) + 1
    page = workspaces[start:start + page_size]
    entries: list[dict[str, Any]] = []
    if page:
        collided = _collided(ctx, page)
        grants, truncated = _page_grants(ctx, [tenant_id for tenant_id, _, _ in page])
        keys = settings_keys(getattr(ctx.settings, "settings_key", ""),
                             getattr(ctx.settings, "settings_key_previous", ""))
        for tenant_id, group, registered_by in page:
            entry: dict[str, Any] = {"tenant_id": tenant_id, "kind": "group", "group": group,
                                     "registered_by": registered_by}
            if tenant_id in collided:
                entry.update(unavailable=True, code="tenant_collision", message=(
                    "this id's tenant belongs to a different principal; its roles are "
                    "not served"))
            elif truncated:
                entry.update(unavailable=True, code="too_many_grants", message=(
                    "this page holds more grants than one read looks at; read this "
                    "workspace alone"))
            else:
                try:
                    entry.update(_roster_entry(ctx, keys, tenant_id, grants))
                except _Unverified:
                    log.warning("admin workspace roles: grants unverified tenant=%s", tenant_id)
                    entry.update(unavailable=True, code=RoleUnavailable.code, message=(
                        "this workspace's roles could not be read: too many grants failed "
                        "verification"))
            entries.append(entry)
    more = start + page_size < len(workspaces)
    return {
        "workspaces": entries,
        "roles": roles.roles_table(),
        "next_page_token": page[-1][0] if more and page else None,
    }
