"""A workspace's merge settings (TRAIN-WS, docs/merge-trains.md §3).

    GET /v1/workspaces/{tenant_id}/merge-settings   the settings, each registered
                                                    repository's override and the
                                                    mode it effectively merges in
    PUT /v1/workspaces/{tenant_id}/merge-settings   change any of them; audited

`{tenant_id}` is the workspace's tenant id: `u-<user>` for a personal
workspace, the group's slug for a group one. Who may read and who may change
it is `mergesettings.authorize`'s, and the change and its `admin_audit` entry
are one transaction (`MergeSettingsStore.put`). Nothing here takes or returns
a credential, an image, a command or a resource spec (invariant 10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..mergesettings import MergeSettingsPut, MergeSettingsStore, authorize, can_edit, view

router = APIRouter(prefix="/v1/workspaces", tags=["workspace"])


def _store(ctx: AppContext) -> MergeSettingsStore:
    return MergeSettingsStore(ctx.store.db, now=ctx.now)


def _answer(ctx: AppContext, auth: AuthContext, tenant, store: MergeSettingsStore) -> dict:
    tenant_id = tenant.tenant_id
    return view(tenant_id, store.get(tenant_id), store.document(tenant_id),
                store.registrations(tenant_id), editable=can_edit(auth, tenant))


@router.get("/{tenant_id}/merge-settings")
def get_merge_settings(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize(auth, ctx.store.get_tenant(tenant_id), tenant_id, write=False)
    return _answer(ctx, auth, tenant, _store(ctx))


@router.put("/{tenant_id}/merge-settings")
def put_merge_settings(
    tenant_id: str,
    body: MergeSettingsPut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize(auth, ctx.store.get_tenant(tenant_id), tenant_id, write=True)
    store = _store(ctx)
    store.put(tenant_id, body, by=(auth.email or "").strip().lower())
    return _answer(ctx, auth, tenant, store)
