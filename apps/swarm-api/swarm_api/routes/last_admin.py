"""The last-admin guard: no route leaves a group workspace without an admin
of its own who can still select it (docs/design/join-requests.md §4.4).

These routes can lower who administers a workspace, and every one goes
through this guard:

    POST   /v1/workspace/leave                  the caller, when they hold `admin`
    DELETE /v1/workspace/members/person         a person who holds `admin`
    PUT    /v1/workspace/roles/person           an `admin` override set lower
    DELETE /v1/workspace/roles/person           an `admin` override cleared
    (and the platform admin's /v1/admin/workspaces/{tenant}/roles/person pair)
    POST   /v1/admin/join-requests/{ref}/approve   an approval lowering the
                                                override of an admin who
                                                can already select it

HOW IT HOLDS UNDER CONCURRENCY. EVERY write those routes make -- a lowering
or not, naming an admin included -- is one Firestore transaction that FIRST
reads the workspace's guard document (`memberships.LEAVE_GUARD_COLLECTION/
{tenant}`, `read_guard`), THEN reads the subject's role fresh and decides
there whether the write lowers an admin, and WRITES the guard document with
its change: through `require_admin_remains` when it lowers one, through
`write_guard` when it does not. Whether to guard is never decided before the
transaction. So any commit in between that changed who is an admin -- a
promotion, a guarded leave that counted the subject as the admin who remains
-- changed the guard too: Firestore aborts the writer that read it before,
and its retry reads the subject's new role and, if it now lowers an admin,
re-checks who is left. Two lowerings, or a lowering and a stale "not an
admin" write, cannot both pass. Naming an admin is never REFUSED, so a
platform admin can always name a new one; it only takes part in the guard.

WHO COUNTS. Another person counts only while they hold `admin` by override,
read fresh, AND can still select the workspace: their platform membership,
read inside the transaction, still counts for the registered group, or a
per-group check finds them in it -- asked before the transaction when the
person changed is an admin (`lookups_ahead`), so no network call holds the
guard, and inside it only for an admin not asked before. A platform admin is never counted:
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
AUDIT_REFUSED = roles.AUDIT_LAST_ADMIN_REFUSED
#: Which route was refused, in the row's `detail.route`.
ROUTE_LEAVE = "leave"
ROUTE_REMOVE = "member_removed"
ROUTE_ROLE_SET = "role_set"
ROUTE_ROLE_CLEARED = "role_cleared"
ROUTE_JOIN_APPROVED = "join_approved"


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


def lookups_ahead(request: Request, ctx: AppContext, *, tenant_id: str, subject: str,
                  group: str, registered: Mapping[str, str]) -> dict[str, bool]:
    """The directory answers `admin_remains` would need, asked BEFORE the
    transaction (epic 21): a per-group check is a network call, and made
    inside the transaction it holds the guard across it, so contention 503s
    grow under load. Empty unless `subject` holds `admin` by override now,
    so a write that lowers no admin asks the directory nothing more than it
    did. Stops at the first other admin the group holds, after any whose
    platform membership already counts (that needs no lookup at all).

    Nothing is decided on these answers alone: inside the transaction the
    roles are read again (`admin_remains`), an answer is used only for a
    person still `admin` there, and anyone not asked here is asked there.
    A lookup that fails is a 503 here, as it would be inside."""
    from .people import membership_store

    if not admin_by_override(ctx, tenant_id, subject):
        return {}
    store = ctx.roles
    assert store is not None
    members = membership_store(ctx)
    answers: dict[str, bool] = {}
    for other in store.admin_subjects(tenant_id):
        if other == subject:
            continue
        grant = members.read(other, fresh=True).newest.get(tenant_id)
        if grant is not None and grant.counts_for(registered):
            return answers
        answers[other] = in_group(request, ctx, other, group)
        if answers[other]:
            return answers
    return answers


def admin_remains(request: Request, ctx: AppContext, txn: Any, *, tenant_id: str,
                  email: str, group: str, registered: Mapping[str, str],
                  known: Mapping[str, bool] | None = None) -> bool:
    """Whether someone other than `email` is this workspace's `admin` by
    override AND can still select it: their platform membership, read inside
    `txn`, still counts for the registered group, or a per-group check finds
    them in it -- the answer `lookups_ahead` got before the transaction
    (`known`) when there is one, else a fresh check here. An override alone
    is not an admin: it outlives a leave and a removal by design, and an
    admin can be named before they join. The admins are read fresh, every
    one of them (`RoleStore.admin_subjects`, never the capped roster); a
    group check that fails is a 503, never a guess that lets the last admin
    go."""
    from .people import membership_store

    store = ctx.roles
    assert store is not None
    others = [other for other in store.admin_subjects(tenant_id) if other != email]
    members = membership_store(ctx)
    for other in others:
        grant = members.read(other, transaction=txn).newest.get(tenant_id)
        if grant is not None and grant.counts_for(registered):
            return True
    known = known or {}
    if any(known.get(other) for other in others):
        return True
    return any(in_group(request, ctx, other, group) for other in others if other not in known)


def guard_ref(ctx: AppContext, tenant_id: str) -> Any:
    return ctx.db.collection(memberships.LEAVE_GUARD_COLLECTION).document(tenant_id)


def read_guard(ctx: AppContext, txn: Any, tenant_id: str) -> None:
    """The transaction's FIRST read: what makes two guarded changes conflict."""
    _snapshot(txn.get(guard_ref(ctx, tenant_id)))


def write_guard(ctx: AppContext, txn: Any, tenant_id: str) -> None:
    """Write the guard document inside `txn`: every transaction that read it
    (`read_guard`) and has not committed yet is aborted and retried, and
    decides again on what this one changed.

    It records no one (epic 21): the document holds no authority and lives
    as long as its workspace, one per workspace, overwritten in place, so
    it carries the workspace and the time alone. Who changed what is the
    audit rows' (`admin_audit`), which have their own retention."""
    txn.set(guard_ref(ctx, tenant_id), {"tenant_id": tenant_id,
                                        "at": datetime.now(timezone.utc)})


def require_admin_remains(request: Request, ctx: AppContext, txn: Any, *, tenant_id: str,
                          subject: str, group: str, registered: Mapping[str, str],
                          message: str, known: Mapping[str, bool] | None = None) -> None:
    """Refuse (409 `workspace_leave_last_admin`) unless another admin who can
    still select the workspace remains once `subject` is no longer one; when
    one does, write the guard document inside `txn`. Call it after
    `read_guard` and the route's own reads, before the route's writes;
    `known` is `lookups_ahead`'s, made before the transaction."""
    if not admin_remains(request, ctx, txn, tenant_id=tenant_id, email=subject, group=group,
                         registered=registered, known=known):
        raise WorkspaceLeaveLastAdmin(message)
    write_guard(ctx, txn, tenant_id)


def contended(exc: BaseException) -> bool:
    """A transaction that cannot have committed: Firestore aborted it, on a
    read or after the decorator's own retries ran out."""
    if isinstance(exc, gexc.Aborted):
        return True
    return isinstance(exc, ValueError) and str(exc).startswith(_COMMIT_RETRIES_EXHAUSTED)


def audit_refusal(ctx: AppContext, *, tenant_id: str, subject: str, by: str,
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
        audit_refusal(ctx, tenant_id=tenant_id, subject=subject, by=by, detail=detail)
        raise
    except Exception as exc:  # only contention is re-raised as a retry
        if not contended(exc):
            raise
        log.warning("last-admin guard contended tenant=%s", tenant_id)
        raise contended_error(
            "others in this workspace were changing its members or admins at the same "
            "moment; retry shortly. Nothing was written") from None
