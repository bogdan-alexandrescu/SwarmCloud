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
    POST /v1/admin/people/{workspace_id}/loan/decline    decline the open loan
                                                         request; body {reason?}
                                                         (LOAN-DECLINE)
    POST /v1/admin/workspaces/sweep                      the dispatch sweep: an
                                                         admin, or the swarm-tick
                                                         scheduler identity
                                                         (`auth.ROLLUP_SWEEPER_ROUTES`)

Lane ADMIN-PEOPLE-ROLES (part of #2157; config-e.html frames S5c, S5f):

    GET    /v1/admin/people                              also a WORKSPACE admin's,
                                                         scoped (below)
    PUT    /v1/admin/people/workspaces/{tenant_id}/role  a person's role there;
                                                         body {email, role}
    DELETE /v1/admin/people/workspaces/{tenant_id}/role  back to the default;
                                                         body {email}

WHO READS WHAT. A platform admin (`admin_auth`'s rule) reads everyone and
every registered group workspace, as before, plus each row's role cells. A
person who is not one reads the page only if they ADMINISTER a group
workspace -- a group their verified membership lists (`tenant_choices`, the
same pass `/v1/tenants/mine` serves) whose signed grants make them `admin`,
read fresh -- and then only those workspaces, the people in them, and of each
person only the cells of those workspaces. Everyone else is refused exactly
as before (`admin_auth`'s 403, or its 503 when the directory did not answer).
The approve, deny, retry, ceiling, loan, admin and workspace routes are
unchanged and stay a platform admin's: a personal workspace and its
Claude account are the platform's, never a group admin's. The scope is
decided on the server from the caller's verified identity alone: no query
parameter, path or header widens it.

Lane WK-RECORD (docs/design/workspace-kinds.md §4.2.2):

    POST /v1/admin/workspaces                    file a personal, group or service
                                                 workspace; body {kind, principal,
                                                 display_name?, limits?}
    GET  /v1/admin/workspaces?kind=&state=       records, newest first, at most 200
    GET  /v1/admin/workspaces/{workspace_id}     one record, for the progress view

Approve and retry of a SERVICE record are the platform owner's (Q2 (a)):
anyone else is 403 WORKSPACE_APPROVAL_OWNER_ONLY. The request body names a
kind, an address, a label and the four limit numbers -- no image, command,
resource spec or backend parameter (invariant 10) -- and an extra field is
422.

A person is addressed by WORKSPACE ID, never an email or a tenant id, so a URL
names nobody. No route takes or returns a credential, an image, a command or a
resource spec (invariant 10): the ceiling is one number, and a loan names an
account by id. The state machine, the audit and the publish are
`swarm_api.people`'s; the publisher is `publish_workspace.publisher_for`, held
on `app.state` so a test injects a fake, exactly as `account_pool` is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .. import roles
from ..admins import AdminRoles, build_admin_roles
from ..auth import AuthContext, require_admin
from ..brokerclient import AccountPool
from ..deps import AppContext, admin_auth, current_auth, get_context
from ..errors import Forbidden, RoleUnavailable
from .. import workspaces as ws
from ..groups import MembershipResolver
from ..people import (
    MAX_CEILING,
    MAX_LOAN_REASON,
    MAX_REASON,
    MIN_CEILING,
    People,
    add_role_cells,
    grid_columns,
    name_lookup,
    workspace_rosters,
)
from ..publish_workspace import WorkspacePublisher, publisher_for
from ..schemas import StrictModel
from ..specsigning import signer_from_settings
from .accounts import account_pool
from .roles import PersonClear, PersonRole, _clear_person, _registered, _set_person

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


class LoanDeclineRequest(StrictModel):
    #: Optional, and shown to the person in their notification feed.
    reason: str | None = Field(default=None, max_length=MAX_LOAN_REASON)


class RequestLimits(StrictModel):
    #: Partial: what is not given is the kind's default (`ws.DEFAULT_LIMITS_BY_KIND`).
    #: Bounded again in `people.request_limits`, beside the ceiling's bounds.
    max_active: int | None = Field(default=None, ge=MIN_CEILING, le=MAX_CEILING)
    capacity_units: int | None = Field(default=None, ge=MIN_CEILING, le=MAX_CEILING)
    quota_pods: int | None = Field(default=None, ge=MIN_CEILING)
    quota_cpu: int | None = Field(default=None, ge=MIN_CEILING)


class NewWorkspaceRequest(StrictModel):
    kind: Literal["personal", "group", "service"]
    #: The person's, the group's or the service account's email. Its shape
    #: for the kind is checked in `workspaces.validate_kind_request`.
    principal: str = Field(min_length=3, max_length=254)
    display_name: str | None = Field(default=None, max_length=ws.MAX_DISPLAY_NAME)
    limits: RequestLimits | None = None


def workspace_directory(request: Request,
                        ctx: AppContext = Depends(get_context)) -> MembershipResolver | None:
    """The directory a group request is checked against: the authenticator's
    own resolver, the one every request's tenant lookup uses, so the check
    asks Cloud Identity exactly as resolution will. A test sets
    `app.state.workspace_directory` to a resolver over a recording session.
    `_groups` is read because `auth.py` is lane WK-RESOLVE's to change."""
    directory = getattr(request.app.state, "workspace_directory", None)
    if directory is None:
        directory = getattr(ctx.authenticator, "_groups", None)
    return directory


def workspace_publisher(request: Request,
                        ctx: AppContext = Depends(get_context)) -> WorkspacePublisher:
    """Built once per process from the settings; a test sets
    `app.state.workspace_publisher` to a fake before the first call."""
    publisher = getattr(request.app.state, "workspace_publisher", None)
    if publisher is None:
        publisher = publisher_for(ctx.settings)
        request.app.state.workspace_publisher = publisher
    return publisher


def approval_signer(request: Request, ctx: AppContext = Depends(get_context)) -> Any:
    """The signer of every workspace approval and retry: the step-spec KMS
    signer, built once per process from the settings (lane
    WK-SIGNED-APPROVAL); a test sets `app.state.workspace_approval_signer`
    to a fake before the first call. None where no key is configured, which
    `signer_from_settings` allows only outside a deployment."""
    if not hasattr(request.app.state, "workspace_approval_signer"):
        request.app.state.workspace_approval_signer = signer_from_settings(ctx.settings)
    return request.app.state.workspace_approval_signer


def people_service(
    ctx: AppContext = Depends(get_context),
    publisher: WorkspacePublisher = Depends(workspace_publisher),
    directory: MembershipResolver | None = Depends(workspace_directory),
    signer: Any = Depends(approval_signer),
) -> People:
    return People(ctx.db, publisher=publisher, now=ctx.now,
                  console_url=ctx.settings.console_url,
                  project_id=ctx.settings.project_id,
                  names=name_lookup(ctx),
                  groups=directory,
                  rules=ws.KindRules.from_settings(ctx.settings),
                  signer=signer,
                  verify_settings=ctx.settings,
                  firestore_off_tenants=tuple(
                      getattr(ctx.settings, "worker_firestore_off_tenants", ()) or ()))


# -- Admin › People and roles: who reads it (ADMIN-PEOPLE-ROLES) ---------------

#: Why a section a workspace admin is not served is null: it is the platform's.
PLATFORM_ADMIN_ONLY = "platform_admin_only"


@dataclass(frozen=True)
class PeopleReader:
    """Who is reading Admin › People and roles. `workspaces` is None for a
    platform admin (every registered group workspace) and otherwise the
    group workspaces this person administers, tenant id -> group, never
    empty."""

    auth: AuthContext
    workspaces: dict[str, str] | None

    @property
    def platform(self) -> bool:
        return self.workspaces is None


def administered(ctx: AppContext, auth: AuthContext) -> dict[str, str]:
    """The group workspaces `auth` administers: each group its VERIFIED
    membership lists (`tenant_choices`; never a platform admin's reach, never
    the person's own workspace) whose signed grants say `admin`, read fresh
    so a demotion counts on the next request. A workspace whose grants could
    not be read is left out: unknown is not admin."""
    store = ctx.roles
    if store is None:
        return {}
    out: dict[str, str] = {}
    for tenant_id, principal in auth.tenant_choices:
        if principal == auth.email or tenant_id.startswith("u-") or tenant_id in out:
            continue
        try:
            role = store.person_role(tenant_id, auth.email, fresh=True).role
        except RoleUnavailable:
            continue
        if role == roles.ADMIN:
            out[tenant_id] = principal.lower()
    return out


def people_reader(
    request: Request,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> PeopleReader:
    """`admin_auth`'s rule first; a person it refuses with a 403 is let in
    only as the admin of the workspaces `administered` finds, and refused
    with that same 403 when it finds none. A 503 (the directory did not
    answer) is never turned into a narrower read."""
    path = getattr(request.scope.get("route"), "path", None)
    route = (request.method.upper(), path) if path else None
    try:
        return PeopleReader(auth=require_admin(auth, route), workspaces=None)
    except Forbidden:
        mine = administered(ctx, auth)
        if not mine:
            raise
        return PeopleReader(auth=auth, workspaces=mine)


@router.get("/people")
def list_people(
    request: Request,
    reader: PeopleReader = Depends(people_reader),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> dict:
    """§6.4: everyone who has signed in, pending requests first, the last 50
    audit entries, and the accounts this admin may lend. The lendable list
    comes from the quota broker; when it does not answer, the rest of the
    page is still served and `lendable_accounts` is null with the reason.

    ADMIN-PEOPLE-ROLES: `scope`, the grid's `workspaces` (its columns) and
    each row's `roles` (one cell per column). A workspace admin is served
    `scope: workspace_admin`, their workspaces, their people
    (`People.in_workspaces`), and null or empty for every platform section."""
    assert ctx.roles is not None
    if not reader.platform:
        workspaces = reader.workspaces or {}
        rosters = workspace_rosters(ctx.roles, workspaces)
        rows = people.in_workspaces(rosters)
        return {
            "scope": "workspace_admin",
            "workspaces": grid_columns(workspaces, rosters),
            "people": rows, "count": len(rows), "pending": 0,
            "provisioning": None, "audit": [],
            "lendable_accounts": None, "lendable_error": PLATFORM_ADMIN_ONLY,
            "admins": None, "admins_error": PLATFORM_ADMIN_ONLY,
        }
    auth = reader.auth
    body = people.everyone(store=ctx.store)
    body["scope"] = "platform"
    # The grid's columns: every registered group workspace, the Terraform
    # groups then the `ready` job-made ones (`routes/roles.py` lists the
    # same). When the registry cannot be read the rest of the page is served
    # with no columns and the reason.
    try:
        workspaces = ctx.authenticator.registered_group_workspaces()
        body["workspaces_error"] = None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        workspaces = {}
        body["workspaces_error"] = getattr(exc, "code", None) or type(exc).__name__
    rosters = workspace_rosters(ctx.roles, workspaces)
    body["workspaces"] = grid_columns(workspaces, rosters)
    add_role_cells(body["people"], rosters)
    try:
        pool = account_pool(request, ctx)
        body["lendable_accounts"] = people.lendable(pool, admin_email=auth.email,
                                                    store=ctx.store)
        body["lendable_error"] = None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        body["lendable_accounts"] = None
        body["lendable_error"] = getattr(exc, "code", None) or type(exc).__name__
    # Who the admins are, for the Admins card: one more section of this read,
    # not a route of its own, and like the lendable list it fails alone.
    try:
        body["admins"] = _roles(ctx).holders()
        body["admins_error"] = None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        body["admins"] = None
        body["admins_error"] = getattr(exc, "code", None) or type(exc).__name__
    return body


def _acting_in(ctx: AppContext, reader: PeopleReader, tenant_id: str) -> str:
    """The group of `tenant_id` for a role write there, or the refusal. A platform
    admin may act in any registered group workspace (`routes/roles._registered`,
    404 otherwise) and is recorded `via: platform_admin`. A workspace admin
    only in one `administered` found for THIS request: anything else is 403,
    before anything is read about it, and the message names no one in it."""
    if reader.platform:
        return _registered(ctx, tenant_id)
    group = (reader.workspaces or {}).get(tenant_id)
    if group is None:
        raise Forbidden("you administer other workspaces, not this one; nothing was changed")
    return group


def _via(reader: PeopleReader, requested: str) -> str:
    return roles.VIA_PLATFORM_ADMIN if reader.platform else requested


@router.put("/people/workspaces/{tenant_id}/role")
def set_person_role(
    tenant_id: str,
    body: PersonRole,
    reader: PeopleReader = Depends(people_reader),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A cell of the grid: a person's own role in one group workspace. The
    write is `routes/roles.py`'s, one signed grant and its audit row."""
    group = _acting_in(ctx, reader, tenant_id)
    return _set_person(ctx, tenant_id=tenant_id, group=group, by=reader.auth.email,
                       via=_via(reader, body.via), body=body)


@router.delete("/people/workspaces/{tenant_id}/role")
def clear_person_role(
    tenant_id: str,
    body: PersonClear,
    reader: PeopleReader = Depends(people_reader),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Back to the workspace's default role."""
    _acting_in(ctx, reader, tenant_id)
    return _clear_person(ctx, tenant_id=tenant_id, by=reader.auth.email,
                         via=_via(reader, body.via), body=body)


@router.post("/workspaces/sweep")
def sweep_workspaces(
    request: Request,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> dict:
    """§2.2's dispatch sweep, and the pending loans of every workspace now
    ready (LOAN-NEW). Declared before the `{workspace_id}` routes so the
    literal path is never read as a workspace id. The broker client is built
    here rather than as a dependency so that a deployment whose broker is
    misconfigured still gets its dispatch and its stuck reports; the loans
    then wait, and `loans_error` says why."""
    try:
        pool = account_pool(request, ctx)
        pool_error = None
    except Exception as exc:  # noqa: BLE001 - the loans wait, the sweep runs
        pool, pool_error = None, getattr(exc, "code", None) or type(exc).__name__
    return {**people.sweep(pool), "loans_error": pool_error}


@router.post("/workspaces")
def create_workspace(
    body: NewWorkspaceRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> JSONResponse:
    """File a workspace of any kind (docs/design/workspace-kinds.md §4.2).
    202 with the record when this call created it (already `approved` when
    the requester may approve it), 200 when it already stood."""
    status, record = people.create(
        ctx.submissions.workspaces, kind=body.kind, principal=body.principal,
        display_name=body.display_name,
        limits=body.limits.model_dump(exclude_none=True) if body.limits else None,
        by=auth.email, is_owner=bool(auth.is_owner))
    return JSONResponse(status_code=status, content=people.admin_view(record))


@router.get("/workspaces")
def list_workspaces(
    kind: Literal["personal", "group", "service"] | None = Query(default=None),
    state: Literal["requested", "approved", "applying", "needs_owner", "ready", "denied",
                   "failed"] | None = Query(default=None),
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    """Records, newest first, at most 200: Admin › Tenants' "Being created".
    No email is read from the URL."""
    rows = people.records(kind=kind, state=state)
    return {"workspaces": rows, "count": len(rows)}


@router.get("/workspaces/{workspace_id}")
def get_workspace_record(
    workspace_id: str,
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    return {"workspace": people.admin_view(people.record(workspace_id))}


@router.post("/workspaces/{workspace_id}/approve")
def approve_workspace(
    workspace_id: str,
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    """From `requested`, or `denied` at any time. An admin's OWN request is
    approved automatically when they make it (`POST /v1/workspace`, owner
    decision 2026-10-09), through the same `People._approve_in`; one that is
    still `requested` (made before they were an admin) may be approved here,
    and the audit entry says so (`detail.self_approval`). A workspace whose
    tenant predates self-service setup is refused `WORKSPACE_MIGRATING` (§3.3)
    once that refusal's switch is on; until then it is logged (refusals.py)."""
    return people.approve(workspace_id, by=auth.email, is_owner=bool(auth.is_owner))


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
    return people.retry(workspace_id, by=auth.email, is_owner=bool(auth.is_owner))


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


@router.post("/people/{workspace_id}/loan/decline")
def decline_workspace_loan(
    workspace_id: str,
    body: LoanDeclineRequest | None = None,
    auth: AuthContext = Depends(admin_auth),
    people: People = Depends(people_service),
) -> dict:
    """Close the person's open loan request without lending (LOAN-DECLINE).
    The body may be omitted; 409 `LOAN_REQUEST_NOT_OPEN` when no request is
    open. No broker call: nothing was lent, so there is nothing to undo."""
    return people.decline_loan(workspace_id, by=auth.email,
                               reason=body.reason if body is not None else None)
