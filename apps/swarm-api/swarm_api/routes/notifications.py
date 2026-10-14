"""The notification feed (#1206, lane NOTIF-API; docs/notifications.md).

    GET  /v1/notifications             the caller's feed, derived on this read
    POST /v1/notifications/read        {ids: [...]} -> {ok: true}
    POST /v1/notifications/read-all    -> {ok: true}

Every person's own: the tenant is `tenant_scope`'s (the verified identity and
`X-Swarm-Tenant`, never a body), the read marks are keyed by the caller's own
address, and admin items are derived only when `is_admin` says so. Not in
`auth.CONTINUATION_ROUTES`, so a continuation-scoped account, the rollup
sweeper and the schedule tick are refused: they are not people. Nothing here
takes or returns a credential, an image, a command or a resource spec.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import Field, StringConstraints

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..notifications import MAX_ID_CHARS, MAX_IDS, Notifications, Viewer
from ..schemas import StrictModel

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])

NotificationId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_ID_CHARS)]


class ReadRequest(StrictModel):
    ids: list[NotificationId] = Field(min_length=1, max_length=MAX_IDS)


def _service(ctx: AppContext) -> Notifications:
    return Notifications(ctx.db, ctx.store, now=ctx.now)


def _viewer(auth: AuthContext, tenant_id: str) -> Viewer:
    return Viewer(email=auth.email, tenant_id=tenant_id, is_admin=auth.is_admin,
                  is_owner=auth.is_owner, member_scope=bool(auth.member_scope))


@router.get("")
def get_notifications(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Needs-you items oldest first, then at most 50 updates of the last 7
    days newest first; `partial: true` when a source could not be read."""
    return _service(ctx).feed(_viewer(auth, tenant_id))


@router.post("/read")
def mark_read(
    body: ReadRequest,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _service(ctx).mark_read(_viewer(auth, tenant_id), body.ids)
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _service(ctx).mark_all_read(_viewer(auth, tenant_id))
    return {"ok": True}
