"""A workspace's default review gate (#2524, BGD 2026-10-28: "every review step").

    GET /v1/workspaces/{tenant_id}/review-gate   the policy in force, the
                                                 platform's default, and whether
                                                 the caller may change it
    PUT /v1/workspaces/{tenant_id}/review-gate   `{enabled, min_lines,
                                                 risk_paths}`, each optional; its
                                                 owner or admin, or a platform
                                                 admin; audited

Every review step that names no `metadata.review_gate` of its own is
decided by this policy (`reviewgate`, which says where it is stored and why
it is read range-checked). A key left out takes the platform's value; the
platform's risk paths always apply, whatever list the workspace adds.
Nothing here takes or returns a credential, an image, a command or a URL
(invariant 10).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..errors import RoleAdminRequired
from ..qasettings import authorize_read, can_edit
from ..reviewgate import ReviewGateSettingsStore, checked_setting, setting_view
from ..roles import ADMIN

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> ReviewGateSettingsStore:
    return ReviewGateSettingsStore(ctx.db, now=ctx.now)


def _answer(ctx: AppContext, auth: AuthContext, tenant: Tenant,
            store: ReviewGateSettingsStore) -> dict:
    return setting_view(tenant.tenant_id, store.document(tenant.tenant_id),
                        editable=can_edit(auth, tenant, ctx.roles))


@router.get("/v1/workspaces/{tenant_id}/review-gate")
def get_review_gate(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/review-gate")
def put_review_gate(
    tenant_id: str,
    body: dict[str, Any] = Body(...),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    if not can_edit(auth, tenant, ctx.roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its "
            "review gate. Nothing was changed.",
            detail={"tenant_id": tenant_id, "required": ADMIN})
    store = _store(ctx)
    store.put(tenant_id, checked_setting(body), by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)
