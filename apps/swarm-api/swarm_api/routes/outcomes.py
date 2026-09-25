"""`GET /v1/outcomes`: what the work in a span ended as, bucketed by when it ended.

All the logic is in `swarm_api.outcomes`; this file is the gate and the wiring.

DEPENDENCIES, IN ORDER, and the order is the contract:

  1. `current_auth` -- 401 unauthenticated, then the per-principal limiter (429).
  2. `tenant_scope` -- the collision check; a tenant-id collision is a 409. A
     disabled tenant can still read, as on every read route. The id this
     returns is the ONLY tenant a non-admin can ever read (invariant 9).
  3. Only when anything beyond that tenant is asked for -- scope=platform, a
     `tenant`, an `exclude_tenant`, or group=tenant_id -- `require_admin`: 403
     for a non-admin, 503 when Cloud Identity did not answer the admin lookup,
     and 403 for a pool admin, because this route is not in POOL_ADMIN_ROUTES.
     The gate runs BEFORE any parameter is validated, so a non-admin learns
     nothing about tenant ids -- not even whether the ones they named exist.
  4. Parameter validation, 422 validation_failed.

ONE ERROR SHAPE. Every query parameter is declared as `str | None` or
`list[str]` and validated by `outcomes.parse_params`, which raises
`errors.ValidationFailed`. A FastAPI Literal or enum would put a second,
differently shaped 422 body on this route; the `{code, message, detail}`
envelope is the only one a caller has to parse. A `tenant_id` in the query
string is not a parameter and is ignored.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..auth import AuthContext, require_admin
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..outcomes import platform_requested

router = APIRouter(prefix="/v1", tags=["outcomes"])

#: The (method, template) `require_admin` is handed. Not in POOL_ADMIN_ROUTES,
#: so a pool admin asking for platform scope is refused like any non-admin.
PLATFORM_ROUTE = ("GET", "/v1/outcomes")


@router.get("/outcomes")
def get_outcomes(
    tz: str | None = Query(default=None, description="IANA zone, required"),
    span: str | None = Query(default=None, description="24h | 7d | 14d | 30d | 90d"),
    since: str | None = Query(default=None, description="ISO 8601 with offset, or YYYY-MM-DD"),
    until: str | None = Query(default=None, description="exclusive; only with since"),
    bucket: str | None = Query(default=None, description="auto | hour | day | week | month"),
    scope: str | None = Query(default=None, description="tenant | platform"),
    tenant: list[str] = Query(default=[], description="platform scope: include"),
    exclude_tenant: list[str] = Query(default=[], description="platform scope: exclude"),
    profile: list[str] = Query(default=[]),
    submitted_by: list[str] = Query(default=[]),
    kind: str | None = Query(default=None, description="all | standalone | steps"),
    group: str | None = Query(default=None, description="runner_profile | submitted_by | tenant_id"),
    compare: str | None = Query(default=None, description="none | previous"),
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    raw = {
        "tz": tz,
        "span": span,
        "since": since,
        "until": until,
        "bucket": bucket,
        "scope": scope,
        "tenant": tenant,
        "exclude_tenant": exclude_tenant,
        "profile": profile,
        "submitted_by": submitted_by,
        "kind": kind,
        "group": group,
        "compare": compare,
    }
    if platform_requested(raw):
        require_admin(auth, PLATFORM_ROUTE)
    return ctx.outcomes.read(tenant_id=tenant_id, raw=raw)
