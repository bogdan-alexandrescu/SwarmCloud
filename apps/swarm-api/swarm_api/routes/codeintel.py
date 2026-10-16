"""A workspace's code-intelligence setting (CTXQ-SET, docs/design/context-quality.md §4.7).

    GET /v1/code-intelligence                          the caller's selected workspace's
                                                       setting (what the console reads)
    GET /v1/workspaces/{tenant_id}/code-intelligence   the setting, its defaults,
                                                       each registered repository's
                                                       override and effective context
    PUT /v1/workspaces/{tenant_id}/code-intelligence   change it: its owner or a
                                                       platform admin; audited

`{tenant_id}` is the workspace's tenant id. Who may read and who may change
it is `codeintel.authorize`'s, and the change and its `admin_audit` entry are
one transaction (`CodeIntelligenceStore.put`). A repository's own override is
`PATCH /v1/repositories/{repo_id}` with `context` (routes/repositories.py).
Nothing here takes or returns a credential, an image, a command or a URL
(invariant 10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..codeintel import (
    CodeIntelligencePut,
    CodeIntelligenceStore,
    authorize,
    can_edit,
    settings_of,
    view,
)
from ..deps import AppContext, current_auth, get_context, tenant_scope

router = APIRouter(tags=["workspace"])


def _store(ctx: AppContext) -> CodeIntelligenceStore:
    return CodeIntelligenceStore(ctx.db, now=ctx.now)


def _answer(auth: AuthContext, tenant: Tenant, store: CodeIntelligenceStore) -> dict:
    tenant_id = tenant.tenant_id
    block = store.block(tenant_id)
    return view(tenant_id, settings_of(block), block, store.registrations(tenant_id),
                editable=can_edit(auth, tenant))


@router.get("/v1/code-intelligence")
def get_selected_code_intelligence(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's setting: the same answer as the path form
    (`tenant_scope` is the collision-guarded selection, as for the judgment backend)."""
    tenant = authorize(auth, ctx.store.get_tenant(tenant_id), tenant_id, write=False)
    return _answer(auth, tenant, _store(ctx))


@router.get("/v1/workspaces/{tenant_id}/code-intelligence")
def get_code_intelligence(
    tenant_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize(auth, ctx.store.get_tenant(tenant_id), tenant_id, write=False)
    return _answer(auth, tenant, _store(ctx))


@router.put("/v1/workspaces/{tenant_id}/code-intelligence")
def put_code_intelligence(
    tenant_id: str,
    body: CodeIntelligencePut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = authorize(auth, ctx.store.get_tenant(tenant_id), tenant_id, write=True)
    store = _store(ctx)
    store.put(tenant_id, body, by=(auth.email or "").strip().lower())
    return _answer(auth, tenant, store)
