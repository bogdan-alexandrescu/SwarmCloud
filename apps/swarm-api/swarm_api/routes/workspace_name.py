"""Rename a workspace (WORKSPACE-ADMIN-FIELDS, part of #2157; config-e.html S6a).

    PUT /v1/workspaces/{tenant_id}/name   {display_name}   its admins and platform
                                                           admins; audited

THE DISPLAY NAME ONLY. `tenants/{tenant_id}.display_name` is what the shell
draws for the workspace in force (`/v1/tenants/me`, Spine.tsx
`workspaceName`), and a job-made workspace's record carries the same label
for Admin › Tenants. The tenant id, the principal, the group, the namespace,
the service account and every grant stay exactly as they were: nothing on
this platform decides anything by a display name (`workspaces.MAX_DISPLAY_NAME`).

WHO. A platform admin, or the workspace's own `admin` as `roles.RoleStore`
resolves it for a workspace the caller is verifiably in (`tenant_choices`):
a personal workspace's person, or a group workspace's admin by grant. The
caller's membership and the workspace's principal come from the verified
request, never from the tenant document, which the tenant's own worker can
rewrite. Everyone else is 403 `role_admin_required` BEFORE anything is read,
so an id that names nothing and one that names another workspace answer
alike (invariant 9). A NEW route, so the refusal is raised, never sent
through `refusals.refuse`'s switch: letting the write through is the breach.

WHAT. 1-60 characters after trimming, none of them a control, format or
separator character (no newline, bell, zero-width or bidi override that would
make one name look like another in the switcher), and never "Your workspace",
which the switcher draws for the caller's own personal workspace. The name,
the record's copy and one `admin_audit` row (who, old name, new name) are one
transaction; the same name again writes nothing.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from fastapi import APIRouter, Depends
from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot

from .. import roles
from .. import workspaces as ws
from ..admins import AUDIT_COLLECTION
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context
from ..errors import NotFound, RoleAdminRequired, ValidationFailed
from ..redaction import redact
from ..schemas import StrictModel
from ..store import TENANTS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workspaces", tags=["workspace"])

AUDIT_RENAMED = "workspace_renamed"
#: The switcher's own label for the caller's personal workspace
#: (Spine.tsx `OWN_WORKSPACE_LABEL`): a group named so would pass for it.
RESERVED_NAMES = frozenset({"your workspace"})
#: Control, format, surrogate, private-use and line/paragraph separators.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
#: The longest previous name copied into the audit row: the stored one may
#: have been written by the tenant's own worker.
_AUDIT_FROM_MAX = 200


class RenameRequest(StrictModel):
    #: Bounded loosely here so the precise refusal is `clean_name`'s, which
    #: counts characters after trimming.
    display_name: str = Field(max_length=4 * ws.MAX_DISPLAY_NAME)


def clean_name(value: str) -> str:
    """The name as stored, or `ValidationFailed` (422) saying why."""
    name = value.strip()
    if not name:
        raise ValidationFailed("a workspace's name may not be empty",
                               detail={"field": "display_name"})
    if len(name) > ws.MAX_DISPLAY_NAME:
        raise ValidationFailed(
            f"a workspace's name is at most {ws.MAX_DISPLAY_NAME} characters",
            detail={"field": "display_name", "max": ws.MAX_DISPLAY_NAME})
    if any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in name):
        raise ValidationFailed(
            "a workspace's name may not contain control or invisible formatting characters",
            detail={"field": "display_name"})
    if " ".join(name.split()).casefold() in RESERVED_NAMES:
        raise ValidationFailed(
            "that name is what the switcher calls each person's own workspace",
            detail={"field": "display_name"})
    return name


def _refused(tenant_id: str) -> RoleAdminRequired:
    return RoleAdminRequired(
        "Only the workspace's admins, or a platform admin, may rename it. "
        "Nothing was changed.", detail={"tenant_id": tenant_id, "required": roles.ADMIN})


def authorize(ctx: AppContext, auth: AuthContext, tenant_id: str) -> str:
    """The `via` the rename is recorded with, or the 403. Asks nothing of the
    tenant document: membership is the verified request's, and the role is
    `RoleStore.resolve`'s for that membership, read fresh."""
    if auth.is_admin:
        return roles.VIA_PLATFORM_ADMIN
    principal = dict(auth.tenant_choices).get(tenant_id)
    if principal is None and auth.tenant_id == tenant_id \
            and auth.tenant_via != roles.VIA_PLATFORM_ADMIN:
        principal = auth.tenant_principal or None
    if principal is None or ctx.roles is None:
        raise _refused(tenant_id)
    me = ctx.roles.resolve(roles.with_tenant(auth, tenant_id, principal), fresh=True)
    if me.role != roles.ADMIN:
        raise _refused(tenant_id)
    return "workspace_admin"


def _audit_text(value: Any) -> str | None:
    if value is None:
        return None
    return redact(str(value)[:_AUDIT_FROM_MAX]).text


def rename(db: Any, *, tenant_id: str, name: str, by: str, via: str,
           now: Any) -> dict[str, Any]:
    """Write the name, the record's copy and the audit row in one
    transaction; 404 when the workspace has no tenant document."""
    tenant_ref = db.collection(TENANTS).document(tenant_id)
    record_ref = db.collection(ws.WORKSPACES).document(tenant_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> dict[str, Any]:
        tenant_snap = _snapshot(txn.get(tenant_ref))
        record_snap = _snapshot(txn.get(record_ref))
        if not tenant_snap.exists:
            raise NotFound(f"workspace {tenant_id!r} not found")
        previous = (tenant_snap.to_dict() or {}).get("display_name")
        record = record_snap.to_dict() if record_snap.exists else None
        record_stale = record is not None and record.get("display_name") != name
        if previous == name and not record_stale:
            return {"tenant_id": tenant_id, "display_name": name,
                    "previous_display_name": previous, "changed": False}
        txn.update(tenant_ref, {"display_name": name})
        if record_stale:
            txn.update(record_ref, {"display_name": name})
        txn.set(db.collection(AUDIT_COLLECTION).document(), {
            "action": AUDIT_RENAMED, "target_tenant_id": tenant_id, "by": by, "at": now(),
            "detail": {"from": _audit_text(previous), "to": name, "via": via},
        })
        return {"tenant_id": tenant_id, "display_name": name,
                "previous_display_name": previous, "changed": True}

    return _apply(transaction)


@router.put("/{tenant_id}/name")
def rename_workspace(
    tenant_id: str,
    body: RenameRequest,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    via = authorize(ctx, auth, tenant_id)
    name = clean_name(body.display_name)
    by = (auth.email or "").strip().lower()
    result = rename(ctx.db, tenant_id=tenant_id, name=name, by=by, via=via, now=ctx.now)
    log.info("workspace rename tenant=%s changed=%s via=%s", tenant_id, result["changed"], via)
    return result
