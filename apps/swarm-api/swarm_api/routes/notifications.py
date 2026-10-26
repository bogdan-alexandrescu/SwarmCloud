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

The person's mutes and reminder (docs/design/user-profile.md §4.6, PROFILE-BELL)
are the ones authentication already read, `AuthContext.preferences`: no route
here reads the preferences document a second time. When they could not be
read, a request that named no workspace never reaches these routes (it is 503
`preferences_unavailable` from `auth.require_known_default`, because the bell's
items are a workspace's and that workspace is not known), and one that named
its workspace is served unfiltered, at `as_set`, with `partial: true`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import Field, StringConstraints

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..notifications import MAX_ID_CHARS, MAX_IDS, Notifications, Viewer
from ..preferences import DEFAULT_REMINDER
from ..schemas import StrictModel
from .admin import secret_metadata

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])

NotificationId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_ID_CHARS)]


class ReadRequest(StrictModel):
    ids: list[NotificationId] = Field(min_length=1, max_length=MAX_IDS)


def _service(ctx: AppContext, meta: Any | None = None) -> Notifications:
    """`meta` is the app's one Secret Manager metadata reader, the one Admin ›
    Platform secrets reads (`routes.admin.secret_metadata`), so the feed's
    `platform_secrets_unset` and that page count through the same reader. Only
    the feed reads it; marking read counts nothing."""
    return Notifications(ctx.db, ctx.store, now=ctx.now, secret_metadata=meta)


def _viewer(auth: AuthContext, tenant_id: str) -> Viewer:
    prefs = auth.preferences
    return Viewer(email=auth.email, tenant_id=tenant_id, is_admin=auth.is_admin,
                  is_owner=auth.is_owner, member_scope=bool(auth.member_scope),
                  muted=frozenset(prefs.muted) if prefs else frozenset(),
                  reminder=prefs.reminder if prefs else DEFAULT_REMINDER,
                  # None also for a caller whose preferences are never read (no
                  # key configured, a service identity): only "unavailable" is
                  # a read that failed.
                  preferences_unread=prefs is None and auth.default_source == "unavailable")


@router.get("")
def get_notifications(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    meta: Any = Depends(secret_metadata),
) -> dict:
    """Needs-you items oldest first, then at most 50 updates of the last 7
    days newest first, less the caller's muted topics (counted in `muted`);
    `partial: true` when a source or the preferences could not be read."""
    return _service(ctx, meta).feed(_viewer(auth, tenant_id))


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
