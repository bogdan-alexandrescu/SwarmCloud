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

Lane W7 (docs/workspaces.md §6.3-§6.4):

    GET  /v1/admin/people                                everyone who has signed in
    POST /v1/admin/workspaces/{workspace_id}/approve     approve, then publish
    POST /v1/admin/workspaces/{workspace_id}/deny        deny; body {reason}
    POST /v1/admin/workspaces/{workspace_id}/retry       from failed/needs_owner
    PUT  /v1/admin/workspaces/{workspace_id}/limits      the ceiling; body {max_active}
    PUT  /v1/admin/people/{workspace_id}/loan            lend or reclaim; body
                                                         {account_id, lend}
    POST /v1/admin/workspaces/sweep                      the dispatch sweep: an
                                                         admin, or the swarm-tick
                                                         scheduler identity
                                                         (`auth.ROLLUP_SWEEPER_ROUTES`)

A person is addressed by WORKSPACE ID, never an email or a tenant id, so a URL
names nobody. No route takes or returns a credential, an image, a command or a
resource spec (invariant 10): the ceiling is one number, and a loan names an
account by id. The state machine, the audit and the publish are
`swarm_api.people`'s; the publisher is `publish_workspace.publisher_for`, held
on `app.state` so a test injects a fake, exactly as `account_pool` is.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ..admins import AdminRoles, build_admin_roles
from ..auth import AuthContext
from ..brokerclient import AccountPool
from ..deps import AppContext, admin_auth, get_context
from ..people import MAX_CEILING, MAX_REASON, MIN_CEILING, People
from ..publish_workspace import WorkspacePublisher, publisher_for
from ..schemas import StrictModel
from .accounts import account_pool

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


# -- People and the approval flow (lane W7) ------------------------------------


class DenyRequest(StrictModel):
    #: Shown to the person (§1.3). Blank after trimming is refused in
    #: `People.deny`, so a reason of spaces is not a reason.
    reason: str = Field(min_length=1, max_length=MAX_REASON)


class CeilingRequest(StrictModel):
    #: The one number (§6.4). `capacity_units` and the namespace quota follow
    #: it; a caller cannot set them apart.
    max_active: int = Field(ge=MIN_CEILING, le=MAX_CEILING)


class LoanRequest(StrictModel):
    account_id: str = Field(min_length=1, max_length=200)
    #: True lends the account to the person; False reclaims it.
    lend: bool


def workspace_publisher(request: Request,
                        ctx: AppContext = Depends(get_context)) -> WorkspacePublisher:
    """Built once per process from the settings; a test sets
    `app.state.workspace_publisher` to a fake before the first call."""
    publisher = getattr(request.app.state, "workspace_publisher", None)
    if publisher is None:
        publisher = publisher_for(ctx.settings)
        request.app.state.workspace_publisher = publisher
    return publisher


def people_service(
    ctx: AppContext = Depends(get_context),
    publisher: WorkspacePublisher = Depends(workspace_publisher),
) -> People:
    return People(ctx.db, publisher=publisher, now=ctx.now,
                  console_url=ctx.settings.console_url)


@router.get("/people")
def list_people(
    request: Request,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> dict:
    """§6.4: everyone who has signed in, pending requests first, the last 50
    audit entries, and the accounts this admin may lend. The lendable list
    comes from the quota broker; when it does not answer, the rest of the
    page is still served and `lendable_accounts` is null with the reason."""
    body = people.everyone(store=ctx.store)
    try:
        pool = account_pool(request, ctx)
        body["lendable_accounts"] = people.lendable(pool, admin_email=auth.email,
                                                    store=ctx.store)
        body["lendable_error"] = None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        body["lendable_accounts"] = None
        body["lendable_error"] = getattr(exc, "code", None) or type(exc).__name__
    return body


@router.post("/workspaces/sweep")
def sweep_workspaces(
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    """§2.2's dispatch sweep. Declared before the `{workspace_id}` routes so
    the literal path is never read as a workspace id."""
    return people.sweep()


@router.post("/workspaces/{workspace_id}/approve")
def approve_workspace(
    workspace_id: str,
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    """From `requested`, or `denied` at any time. An admin may approve their
    own request; the audit entry says so (`detail.self_approval`)."""
    return people.approve(workspace_id, by=auth.email)


@router.post("/workspaces/{workspace_id}/deny")
def deny_workspace(
    workspace_id: str,
    body: DenyRequest,
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    return people.deny(workspace_id, by=auth.email, reason=body.reason)


@router.post("/workspaces/{workspace_id}/retry")
def retry_workspace(
    workspace_id: str,
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    return people.retry(workspace_id, by=auth.email)


@router.put("/workspaces/{workspace_id}/limits")
def set_workspace_limits(
    workspace_id: str,
    body: CeilingRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> dict:
    return people.set_ceiling(workspace_id, by=auth.email, max_active=body.max_active,
                              store=ctx.store)


@router.put("/people/{workspace_id}/loan")
def set_workspace_loan(
    workspace_id: str,
    body: LoanRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    return people.set_loan(workspace_id, by=auth.email, account_id=body.account_id,
                           lend=body.lend, pool=pool, store=ctx.store)
