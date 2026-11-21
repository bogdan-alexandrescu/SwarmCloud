"""The last-admin guard: no route leaves a group workspace without an admin
of its own who can still select it (docs/design/join-requests.md §4.4).

Three routes lower who administers a workspace, and every one goes through
this guard:

    POST   /v1/workspace/leave                  the caller, when they hold `admin`
    DELETE /v1/workspace/members/person         a person who holds `admin`
    PUT    /v1/workspace/roles/person           an `admin` override set lower
    DELETE /v1/workspace/roles/person           an `admin` override cleared
    (and the platform admin's /v1/admin/workspaces/{tenant}/roles/person pair)

HOW IT HOLDS UNDER CONCURRENCY. Each of those writes is one Firestore
transaction that FIRST reads the workspace's guard document
(`memberships.LEAVE_GUARD_COLLECTION/{tenant}`, `read_guard`), then checks
who else is an admin (`require_admin_remains`) and writes the guard document
with its own change. Two changes that each lower a different admin therefore
conflict on the guard: Firestore aborts one, and its retry re-checks who is
left, so both cannot pass. Naming an admin never lowers anyone and takes no
guard, so a platform admin can always name a new one.

WHO COUNTS. Another person counts only while they hold `admin` by override,
read fresh, AND can still select the workspace: their platform membership,
read inside the transaction, still counts for the registered group, or a
fresh per-group check finds them in it. A platform admin is never counted:
their reach is the platform's, not the workspace's. Every read that fails is
a 503, never an answer that lets the last admin go.

A REFUSAL writes nothing the refused route would have written -- no grant,
no membership, no guard -- and leaves one `workspace_last_admin_refused`
`admin_audit` row, written after the transaction rolled back (`run`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from fastapi import Request
from google.api_core import exceptions as gexc
from google.cloud import firestore

from swarm_common.admission import _snapshot

from .. import memberships, roles
from ..admins import AUDIT_COLLECTION
from ..deps import AppContext
from ..errors import (
    MembershipUnavailable,
    UpstreamUnavailable,
    WorkspaceLeaveLastAdmin,
)
from ..groups import GroupLookupError
from ..store import _COMMIT_RETRIES_EXHAUSTED

log = logging.getLogger(__name__)

#: The `admin_audit` row a refused leave, removal or demotion leaves.
AUDIT_REFUSED = "workspace_last_admin_refused"
#: Which route was refused, in the row's `detail.route`.
ROUTE_LEAVE = "leave"
ROUTE_REMOVE = "member_removed"
ROUTE_ROLE_SET = "role_set"
ROUTE_ROLE_CLEARED = "role_cleared"


def registered(ctx: AppContext) -> dict[str, str]:
    """tenant_id -> group of every registered group workspace now."""
    return {t: g.strip().lower()
            for t, g in ctx.authenticator.registered_group_workspaces().items()}


def in_group(request: Request, ctx: AppContext, email: str, group: str) -> bool:
    """The fresh per-group check of the DIRECTORY alone -- never a platform
    membership. A lookup that fails is a 503, never a guess either way."""
    # Imported here: `routes/people.py` imports `routes/roles.py`, which imports this.
    from .people import workspace_directory

    directory = workspace_directory(request, ctx)
    if directory is None:
        raise UpstreamUnavailable("group membership could not be resolved; retry shortly")
    try:
        held = directory.groups_for(email, (group,), fresh=True)
    except GroupLookupError:
        raise UpstreamUnavailable("group membership could not be resolved; retry shortly") \
            from None
    return group in {g.strip().lower() for g in held}


def admin_by_override(ctx: AppContext, tenant_id: str, email: str) -> bool:
    """Whether `email`'s own newest valid grant here, read fresh, is `admin`
    (`RoleUnavailable`, a 503, when it cannot be read)."""
    store = ctx.roles
    assert store is not None
    mine = store.newest_valid(tenant_id, email, fresh=True)
    return mine is not None and mine.role == roles.ADMIN


def admin_remains(request: Request, ctx: AppContext, txn: Any, *, tenant_id: str,
                  email: str, group: str, registered: Mapping[str, str]) -> bool:
    """Whether someone other than `email` is this workspace's `admin` by
    override AND can still select it: their platform membership, read inside
    `txn`, still counts for the registered group, or a fresh per-group check
    finds them in it. An override alone is not an admin: it outlives a leave
    and a removal by design, and an admin can be named before they join.
    The roster is read fresh; a group check that fails is a 503, never a
    guess that lets the last admin go."""
    from .people import membership_store

    store = ctx.roles
    assert store is not None
    others = [other for other, grant in store.overrides(tenant_id, fresh=True)
              if other != email and grant.role == roles.ADMIN]
    members = membership_store(ctx)
    for other in others:
        grant = members.read(other, transaction=txn).newest.get(tenant_id)
        if grant is not None and grant.counts_for(registered):
            return True
    return any(in_group(request, ctx, other, group) for other in others)


def guard_ref(ctx: AppContext, tenant_id: str) -> Any:
    return ctx.db.collection(memberships.LEAVE_GUARD_COLLECTION).document(tenant_id)


def read_guard(ctx: AppContext, txn: Any, tenant_id: str) -> None:
    """The transaction's FIRST read: what makes two guarded changes conflict."""
    _snapshot(txn.get(guard_ref(ctx, tenant_id)))


def require_admin_remains(request: Request, ctx: AppContext, txn: Any, *, tenant_id: str,
                          subject: str, by: str, group: str, registered: Mapping[str, str],
                          message: str) -> None:
    """Refuse (409 `workspace_leave_last_admin`) unless another admin who can
    still select the workspace remains once `subject` is no longer one; when
    one does, write the guard document inside `txn`. Call it after
    `read_guard` and the route's own reads, before the route's writes."""
    if not admin_remains(request, ctx, txn, tenant_id=tenant_id, email=subject, group=group,
                         registered=registered):
        raise WorkspaceLeaveLastAdmin(message)
    txn.set(guard_ref(ctx, tenant_id), {"tenant_id": tenant_id, "by": by,
                                        "at": datetime.now(timezone.utc)})


def contended(exc: BaseException) -> bool:
    """A transaction that cannot have committed: Firestore aborted it, on a
    read or after the decorator's own retries ran out."""
    if isinstance(exc, gexc.Aborted):
        return True
    return isinstance(exc, ValueError) and str(exc).startswith(_COMMIT_RETRIES_EXHAUSTED)


def _audit_refusal(ctx: AppContext, *, tenant_id: str, subject: str, by: str,
                   detail: Mapping[str, Any]) -> None:
    """One row for a refused change. Written after the transaction rolled
    back; a row that cannot be written is logged, and the refusal stands."""
    try:
        ctx.db.collection(AUDIT_COLLECTION).document().set({
            "action": AUDIT_REFUSED, "target_tenant_id": tenant_id,
            "target_email": subject, "by": (by or "").strip().lower(),
            "at": datetime.now(timezone.utc),
            "detail": dict(detail, refused=WorkspaceLeaveLastAdmin.code),
        })
    except Exception as exc:  # noqa: BLE001 - the 409 stands; the row is a report
        log.warning("last-admin refusal row unwritten tenant=%s (%s)", tenant_id,
                    type(exc).__name__)


def run(ctx: AppContext, apply: Callable[[Any], Any], *, tenant_id: str, subject: str,
        by: str, detail: Mapping[str, Any], contended_error: type[UpstreamUnavailable] =
        MembershipUnavailable) -> Any:
    """Run `apply(txn)` as one Firestore transaction. A last-admin refusal
    leaves its audit row and is raised; contention is a 503 (`contended_error`)
    saying nothing was written."""
    try:
        return firestore.transactional(apply)(ctx.db.transaction())
    except WorkspaceLeaveLastAdmin:
        _audit_refusal(ctx, tenant_id=tenant_id, subject=subject, by=by, detail=detail)
        raise
    except Exception as exc:  # noqa: BLE001 - only contention is re-raised as a retry
        if not contended(exc):
            raise
        log.warning("last-admin guard contended tenant=%s", tenant_id)
        raise contended_error(
            "others in this workspace were changing its members or admins at the same "
            "moment; retry shortly. Nothing was written") from None
