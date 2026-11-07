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

THE JOIN REQUESTS RING THROUGH HERE (docs/design/join-requests.md §4.6; lane
JOIN-BELL-WIRE, part of #2325). The feed and read-all, which both derive the
items, hand `Notifications` the app's one join store (`people.join_store`)
and the person's administered group workspaces (`people.administered_workspaces`,
each role read fresh, as Admin › People reads them). A platform admin is rung
for every open request and needs no such read. When it fails the viewer's
`administers` is None: the join source is reported unread, `partial: true`,
never "administers nothing" and never a 503 for the whole bell. Marking ids
read derives nothing, so it reads neither.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import Field, StringConstraints

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..notifications import MAX_ID_CHARS, MAX_IDS, Notifications, Viewer
from ..preferences import DEFAULT_REMINDER
from ..schemas import StrictModel
from .admin import secret_metadata
from .people import administered_workspaces, join_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])

NotificationId = Annotated[str, StringConstraints(min_length=1, max_length=MAX_ID_CHARS)]


class ReadRequest(StrictModel):
    ids: list[NotificationId] = Field(min_length=1, max_length=MAX_IDS)


def _service(ctx: AppContext, meta: Any | None = None, *, joins: bool = False) -> Notifications:
    """`meta` is the app's one Secret Manager metadata reader, the one Admin ›
    Platform secrets reads (`routes.admin.secret_metadata`), so the feed's
    `platform_secrets_unset` and that page count through the same reader. Only
    the feed reads it; marking read counts nothing. `joins` hands over the
    app's one join store, for the routes that derive the items."""
    return Notifications(ctx.db, ctx.store, now=ctx.now, secret_metadata=meta,
                         joins=join_store(ctx) if joins else None)


def _administers(ctx: AppContext, auth: AuthContext) -> tuple[tuple[str, str], ...] | None:
    """The group workspaces whose join requests ring this person, sorted; ()
    for a platform admin, whom the join source rings for every request without
    it. None when the read failed: an unread role is never "administers
    nothing", and never a 503 for the whole bell."""
    if auth.is_admin:
        return ()
    try:
        return tuple(sorted(administered_workspaces(ctx, auth).items()))
    except Exception as exc:  # noqa: BLE001 - reported as partial by the join source
        log.warning("notifications: administered workspaces unread (%s)", type(exc).__name__)
        return None


def _viewer(auth: AuthContext, tenant_id: str,
            administers: tuple[tuple[str, str], ...] | None = ()) -> Viewer:
    prefs = auth.preferences
    return Viewer(email=auth.email, tenant_id=tenant_id, is_admin=auth.is_admin,
                  is_owner=auth.is_owner, member_scope=bool(auth.member_scope),
                  muted=frozenset(prefs.muted) if prefs else frozenset(),
                  reminder=prefs.reminder if prefs else DEFAULT_REMINDER,
                  # None also for a caller whose preferences are never read (no
                  # key configured, a service identity): only "unavailable" is
                  # a read that failed.
                  preferences_unread=prefs is None and auth.default_source == "unavailable",
                  administers=administers)


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
    return _service(ctx, meta, joins=True).feed(
        _viewer(auth, tenant_id, _administers(ctx, auth)))


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
    # Read-all marks the items the feed shows, so it derives them as the feed does.
    _service(ctx, joins=True).mark_all_read(_viewer(auth, tenant_id, _administers(ctx, auth)))
    return {"ok": True}
