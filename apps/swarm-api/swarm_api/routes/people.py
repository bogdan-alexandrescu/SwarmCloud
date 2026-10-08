"""Admin → People: the admin-role routes (docs/workspaces.md §6.3, §6.5).

Lane W2 of #847 builds the grant and remove routes; lane W7 extends this file
with the People list, approvals, limits and loans. Every route here is
admin-only through `admin_auth`, and none is in `auth.POOL_ADMIN_ROUTES`, so a
pool admin or the rollup sweeper is refused.

    PUT    /v1/admin/admins/{email}   grant admin
    DELETE /v1/admin/admins/{email}   remove admin

The safeguards (the owner, the last admin) and the audit entry live in
`swarm_api.admins`, inside the transaction of each change, not here: a check in
the route would read one moment and write another.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..admins import AdminRoles, build_admin_roles
from ..auth import AuthContext
from ..deps import AppContext, admin_auth, get_context

router = APIRouter(prefix="/v1/admin", tags=["people"])


def _roles(ctx: AppContext) -> AdminRoles:
    """The authenticator's instance, so a change here clears the cache the
    next request's admin check reads on this instance."""
    roles = getattr(ctx.authenticator, "admin_roles", None)
    if roles is None:
        roles = build_admin_roles(ctx.db, ctx.settings, now=ctx.now)
        ctx.authenticator.admin_roles = roles
    return roles


@router.put("/admins/{email}")
def grant_admin(
    email: str,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Make an allowed-domain person an admin. Idempotent: granting an admin
    again changes nothing and writes no audit entry (`changed: false`)."""
    return _roles(ctx).grant(email, by=auth.email)


@router.delete("/admins/{email}")
def remove_admin(
    email: str,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Remove an admin role document. Refused for the owner (`OWNER_PROTECTED`,
    or `OWNER_FROM_CONFIG` for the owner themselves) and for the last admin
    (`LAST_ADMIN`). A config admin (ADMIN_USERS, ADMIN_GROUPS) without a
    document is a 404 that says so: only configuration removes that right."""
    return _roles(ctx).remove(email, by=auth.email)
