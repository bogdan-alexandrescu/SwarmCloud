"""A workspace's Code intelligence switch (CTXQ-UI's seam; docs/design/context-quality.md §4.7).

    GET  /v1/code-intelligence                       the selected workspace's setting
    PUT  /v1/workspaces/{tenant_id}/code-intelligence   change it: its owner or an admin

WHERE IT IS STORED. The `code_intelligence` field of the workspace's one
settings document, `workspace_settings/{tenant_id}`, beside JUDGE-SELECT's
`judgment_backend` and TRAIN-WS's `merge` (owner decision 2026-10-14: one
document per workspace, one field per feature). Written with `merge=True`, so
it never touches another setting's field. No `tenant_settings` collection.

WHAT IT HOLDS. `jev` -- whether Jev is ever sent excerpts of this workspace's
code -- OFF unless set (§4.7: off for every workspace by default). `context`
-- the local pack and graph tools, from which nothing leaves the platform --
ON unless set; a repository's own `context` override (on its registration)
beats it. Each is a boolean: nothing here takes a URL, a key, an image or a
command (invariant 10), and the body forbids every field it does not name.

READ: the caller's selected workspace (`tenant_scope`, through the collision
guard). WRITE: the owner of a personal workspace or a platform admin
(`mergesettings.authorize`'s rule, as for the merge settings); a caller who is
not a member gets the established 404. Every change is one `admin_audit`
entry in the same transaction; a PUT that changes nothing audits nothing.

This route module is self-contained so that it shares no file with lane
CTXQ-SET, which owns `swarm_api/codeintel.py` and the repository override;
CTXQ-SET may move the store there and keep these paths.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from google.cloud import firestore
from pydantic import StrictBool, model_validator

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from ..admins import AUDIT_COLLECTION
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import Forbidden, NotFound
from ..mergesettings import COLLECTION, can_edit, is_member
from ..schemas import StrictModel

router = APIRouter(tags=["workspace"])

#: The field of `workspace_settings/{tenant_id}` this route owns.
FIELD = "code_intelligence"
#: Off by default for every workspace: Jev never sees code until someone chooses it (§4.7).
JEV_DEFAULT = False
#: On by default: local context sends nothing off the platform (§4.7).
CONTEXT_DEFAULT = True
AUDIT_ACTION = "workspace.code_intelligence.set"


class CodeIntelligenceFields(StrictModel):
    jev: StrictBool | None = None
    context: StrictBool | None = None

    @model_validator(mode="after")
    def _names_something(self) -> "CodeIntelligenceFields":
        if self.jev is None and self.context is None:
            raise ValueError("name jev or context")
        return self


class CodeIntelligencePut(StrictModel):
    code_intelligence: CodeIntelligenceFields


def settings_of(stored: Any) -> dict[str, Any]:
    """The effective setting from the stored map; anything not a bool is the default."""
    data = stored if isinstance(stored, dict) else {}
    jev = data.get("jev")
    context = data.get("context")
    return {
        "jev": jev if isinstance(jev, bool) else JEV_DEFAULT,
        "context": context if isinstance(context, bool) else CONTEXT_DEFAULT,
        "set_at": data.get("updated_at"),
        "set_by": data.get("updated_by"),
    }


def _ref(ctx: AppContext, tenant_id: str) -> Any:
    return ctx.db.collection(COLLECTION).document(tenant_id)


def read(ctx: AppContext, tenant_id: str) -> dict[str, Any]:
    snap = _ref(ctx, tenant_id).get()
    return settings_of((snap.to_dict() or {}).get(FIELD) if snap.exists else None)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _body(ctx: AppContext, auth: AuthContext, tenant: Tenant) -> dict[str, Any]:
    s = read(ctx, tenant.tenant_id)
    return {
        "tenant_id": tenant.tenant_id,
        "can_edit": can_edit(auth, tenant),
        FIELD: {"jev": s["jev"], "context": s["context"],
                "set_at": _iso(s["set_at"]), "set_by": s["set_by"]},
    }


@router.get("/v1/code-intelligence")
def get_code_intelligence(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The selected workspace's Code intelligence setting."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"workspace {tenant_id!r} not found")
    return _body(ctx, auth, tenant)


@router.put("/v1/workspaces/{tenant_id}/code-intelligence")
def put_code_intelligence(
    tenant_id: str,
    body: CodeIntelligencePut,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None or not (auth.is_admin or can_edit(auth, tenant) or is_member(auth, tenant_id)):
        raise NotFound(f"workspace {tenant_id!r} not found")
    if not can_edit(auth, tenant):
        raise Forbidden("only a workspace's owner or a platform admin can change its "
                        "Code intelligence setting; nothing was changed")
    by = (auth.email or "").strip().lower()
    wanted = body.code_intelligence
    ref = _ref(ctx, tenant_id)
    now = ctx.now()
    transaction = ctx.db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> None:
        snap = _snapshot(txn.get(ref))
        whole = (snap.to_dict() or {}) if snap.exists else {}
        before = settings_of(whole.get(FIELD))
        after = {
            "jev": wanted.jev if wanted.jev is not None else before["jev"],
            "context": wanted.context if wanted.context is not None else before["context"],
        }
        changed_from = {k: before[k] for k in after if before[k] != after[k]}
        changed_to = {k: after[k] for k in after if before[k] != after[k]}
        if not changed_to:
            return
        txn.set(ref, {FIELD: {"tenant_id": tenant_id, **after,
                              "updated_at": now, "updated_by": by}}, merge=True)
        txn.set(ctx.db.collection(AUDIT_COLLECTION).document(), {
            "action": AUDIT_ACTION,
            "target_tenant_id": tenant_id,
            "by": by,
            "at": now,
            "detail": {"tenant_id": tenant_id, "from": changed_from, "to": changed_to},
        })

    _apply(transaction)
    return _body(ctx, auth, tenant)
