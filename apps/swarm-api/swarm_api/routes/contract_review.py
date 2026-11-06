"""A workspace's opt-in to Jev's review of its work contracts (part of #2688).

    GET /v1/contract-review                          the caller's selected
                                                     workspace's setting
    GET /v1/workspaces/{tenant_id}/contract-review   the setting, its default,
                                                     the platform's mode and
                                                     whether the caller may
                                                     change it
    PUT /v1/workspaces/{tenant_id}/contract-review   `{enabled}`: true or
                                                     false; its owner or admin,
                                                     or a platform admin;
                                                     audited

`enabled` on lets swarm-api send the workspace's work contracts' own text to
its Jev backend at submission, while the platform's SWARM_CONTRACT_REVIEW
mode is `report` or `enforce` (`contractreview`, which says where it is
stored, why it is its own setting and how the block is signed: every PUT
stores its MAC under the `swarm-settings-key`). Nothing here takes or returns
a credential, an image, a command or a URL (invariant 10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..contractreview import (
    ContractReviewPut,
    ContractReviewStore,
    authorize_write,
    view,
)
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..judgment import settings_mac
from ..qasettings import authorize_read, can_edit

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> ContractReviewStore:
    return ContractReviewStore(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _answer(ctx: AppContext, auth: AuthContext, tenant: Tenant,
            store: ContractReviewStore) -> dict:
    return view(tenant.tenant_id, store.document(tenant.tenant_id),
                editable=can_edit(auth, tenant, ctx.roles), mac=store.mac)


@router.get("/v1/contract-review")
def get_selected_contract_review(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's setting: the same answer as the path form."""
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.get("/v1/workspaces/{tenant_id}/contract-review")
def get_contract_review(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/contract-review")
def put_contract_review(
    tenant_id: str,
    body: ContractReviewPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize_write(auth, ctx.store.get_tenant(tenant_id), tenant_id, ctx.roles)
    store = _store(ctx)
    store.put(tenant_id, body.enabled, by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)
