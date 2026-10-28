"""Admin → People: the admin-role routes (docs/workspaces.md §6.3, §6.5).

Lane W2 of #847 builds the grant and remove routes; lane W7 extends this file
with the People list, approvals, limits and loans. Every route here is
admin-only through `admin_auth`, and none is in `auth.POOL_ADMIN_ROUTES`, so a
pool admin or the rollup sweeper is refused. The one exception is the READ of
the People list (ADMIN-PEOPLE-ROLES; part of #2157, BGD "Both, by scope"):
`people_scope` also answers a workspace admin, with only the group workspaces
they administer and the people in them. Every write stays a platform admin's.

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
                                                         (`auth.ROLLUP_SWEEPER_ROUTES`);
                                                         it also prunes join requests
                                                         decided or expired 90 days
                                                         ago (join-requests.md §4.1)

Lane JOIN-API (docs/design/join-requests.md §4.6, §4.8; part of #2325): the
People read gains the open join requests its reader decides, `pending`
counts them, and a requester's cell reads `requested`. `join_scope`, beside
`people_scope`, is who may decide one request: a platform admin, or an
admin of THAT request's workspace, read fresh. The decision routes are
`routes/join_requests.py`'s.

Lane WK-RECORD (docs/design/workspace-kinds.md §4.2.2):

    POST /v1/admin/workspaces                    file a personal, group or service
                                                 workspace; body {kind, principal,
                                                 display_name?, limits?,
                                                 first_admin?}
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

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ..admins import AdminRoles, build_admin_roles
from ..auth import AuthContext
from ..brokerclient import AccountPool
from ..deps import AppContext, admin_auth, current_auth, get_context
from .. import roles
from .. import workspaces as ws
from ..errors import (
    ApiError,
    Forbidden,
    JoinDeciderRequired,
    JoinRequestInvalid,
    JoinRequestUnknown,
    RoleUnavailable,
    ValidationFailed,
)
from ..groups import MembershipResolver
from ..joinrequests import VIA_PLATFORM_ADMIN, VIA_WORKSPACE_ADMIN, JoinStore, RequestView
from ..memberships import MembershipStore
from ..people import (
    MAX_CEILING,
    MAX_LOAN_REASON,
    MAX_REASON,
    MIN_CEILING,
    SCOPE_PLATFORM,
    People,
    name_lookup,
)
from ..publish_workspace import WorkspacePublisher, publisher_for
from ..schemas import StrictModel
from ..specsigning import signer_from_settings
from .accounts import account_pool
from .roles import _member as group_member

router = APIRouter(prefix="/v1/admin", tags=["people"])

#: Where an app context keeps its one JoinStore, and the MembershipStore of a
#: context whose authenticator has none, so each cache is per process, not
#: per request (as `people._NAMES_ATTR`).
_JOINS_ATTR = "_join_store"
_MEMBERS_ATTR = "_membership_store"


def join_store(ctx: AppContext) -> JoinStore:
    store = getattr(ctx, _JOINS_ATTR, None)
    if store is None:
        store = JoinStore(ctx.db, ctx.settings, metrics=ctx.metrics, now=ctx.now)
        setattr(ctx, _JOINS_ATTR, store)
    return store


def membership_store(ctx: AppContext) -> MembershipStore:
    """The authenticator's own store, so every membership write (an approval,
    a removal) evicts the cache the person's next request reads: a second
    store's eviction left a removed person selecting the workspace for up to
    `role_cache_seconds` (docs/design/join-requests.md §4.4; BGD,
    2026-10-26, lane JOIN-ONE-STORE)."""
    shared = getattr(ctx.authenticator, "memberships", None)
    if shared is not None:
        return shared
    # Only a hand-built context reaches here: `deps.build_context` always
    # hands the authenticator a store. Its authenticator reads none, so no
    # resolution has a cache this one could leave stale.
    store = getattr(ctx, _MEMBERS_ATTR, None)
    if store is None:
        store = MembershipStore(ctx.db, ctx.settings, metrics=ctx.metrics, now=ctx.now)
        setattr(ctx, _MEMBERS_ATTR, store)
    return store


def workspace_labels(ctx: AppContext, tenant_ids: Any) -> dict[str, str]:
    """tenant_id -> the workspace's display name (`tenants/{id}.display_name`,
    what the shell draws), for each id that has one. A label only: nothing
    decides anything by it. One that cannot be read is left out, and the
    caller says what it shows instead."""
    out: dict[str, str] = {}
    for tenant_id in sorted(set(tenant_ids)):
        try:
            tenant = ctx.store.get_tenant(tenant_id)
        except Exception:  # noqa: BLE001 - a label, never the answer
            continue
        name = getattr(tenant, "display_name", None) if tenant is not None else None
        if isinstance(name, str) and name.strip():
            out[tenant_id] = name.strip()
    return out


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
    #: A GROUP workspace's first admin (WORKSPACE-ADMIN-FIELDS, part of #2157;
    #: docs/design/workspace-roles.md §4.6, §8): one person's address, granted
    #: `admin` in this workspace and no other. Checked by
    #: `roles.subject_for_write` before anything is created.
    first_admin: str | None = Field(default=None, max_length=320)


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


def administered_workspaces(ctx: AppContext, auth: AuthContext) -> dict[str, str]:
    """tenant_id -> group for every GROUP workspace the caller administers by
    their own stored role: the `/v1/tenants/mine` entries whose role is
    `admin` (what the console's `adminWorkspaces` reads), each asked FRESH,
    so a role taken away a moment ago is not read from a cache. Only the
    caller's verified memberships are asked (`tenant_choices`), never the
    selected tenant alone and never anything the request names; their own
    personal workspace and every `u-` id are left out. A workspace whose role
    could not be read is not administered: an unread role is never a wider
    one. When NO role was read and none said admin, that is "could not ask",
    `RoleUnavailable`'s 503, as `require_admin` answers an unread admin
    lookup: a 403 would tell a real workspace admin they are not one."""
    store = ctx.roles
    if store is None:
        return {}
    email = (auth.email or "").strip().lower()
    out: dict[str, str] = {}
    unread: RoleUnavailable | None = None
    read = 0
    for tenant_id, principal in auth.tenant_choices:
        if principal == email or tenant_id.startswith("u-"):
            continue
        try:
            role = store.person_role(tenant_id, email, fresh=True).role
        except RoleUnavailable as exc:
            unread = exc
            continue
        read += 1
        if role == roles.ADMIN:
            out[tenant_id] = principal
    if not out and read == 0 and unread is not None:
        raise unread
    return out


def people_scope(request: Request, auth: AuthContext = Depends(current_auth),
                 ctx: AppContext = Depends(get_context)) -> dict[str, str] | None:
    """WHO MAY READ ADMIN › PEOPLE AND ROLES (ADMIN-PEOPLE-ROLES; part of
    #2157). None for a platform admin, who reads everything, exactly as
    `admin_auth` decides it (its 503 while Cloud Identity does not answer
    included). Otherwise the group workspaces the caller administers, which
    is the whole of what they read; and when they administer none -- a member,
    a viewer, someone in no group -- `admin_auth`'s own 403. A pool admin, a
    listed service account and the platform's own identities are refused as
    before: none of them holds a person's role."""
    try:
        admin_auth(request, auth)
        return None
    except Forbidden:
        if (auth.is_pool_admin or auth.is_rollup_sweeper or auth.is_schedule_tick
                or auth.member_scope or auth.tenant_member
                or ws.is_service_identity(auth.email)):
            raise
        administered = administered_workspaces(ctx, auth)
        if not administered:
            raise
        return administered


def join_scope(ctx: AppContext, scope: dict[str, str] | None, ref: str) -> tuple[RequestView, str]:
    """WHO MAY DECIDE ONE JOIN REQUEST (join-requests.md §4.6, Q3 (a)): the
    request, read fresh, and the `via` its decision is recorded with. `scope`
    is `people_scope`'s answer, which every decision route declares, so a
    member, a viewer, a pool admin, a listed service account and the
    platform's own identities are refused before this runs.

    A platform admin (`scope` None) decides any request, `platform_admin`.
    A workspace admin decides a request only for a workspace they administer
    NOW -- `administered_workspaces` asked each role fresh -- and for the
    group it names; anything else is 403 `join_decider_required`. An unknown
    ref, or one whose request fails its MAC, is the SAME 403 to a workspace
    admin, so deciding is never a way to learn which refs exist in another
    workspace; a platform admin is told which it is."""
    store = join_store(ctx)
    try:
        view = store.view(ref)
    except (JoinRequestUnknown, JoinRequestInvalid):
        if scope is None:
            raise
        raise _not_decider() from None
    if scope is None:
        return view, VIA_PLATFORM_ADMIN
    group = scope.get(view.request.tenant_id)
    if group is None or group.strip().lower() != view.request.group:
        raise _not_decider()
    return view, VIA_WORKSPACE_ADMIN


def _not_decider() -> JoinDeciderRequired:
    return JoinDeciderRequired(
        "only an admin of this request's workspace, or a platform admin, decides it; "
        "nothing was changed")


def open_join_requests(ctx: AppContext, people: People,
                       scope: dict[str, str] | None) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The open join requests the reader of Admin › People and roles decides:
    every one for a platform admin, else those of the workspaces in `scope`
    whose request names the group the workspace has now. It fails alone, as
    each section of that read does: None and the error's code."""
    try:
        if scope is None:
            views = join_store(ctx).open_for(None)
        else:
            views = [v for v in join_store(ctx).open_for(scope)
                     if scope.get(v.request.tenant_id, "").strip().lower() == v.request.group]
        labels = workspace_labels(ctx, (v.request.tenant_id for v in views))
        return people.join_rows(views, labels), None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        return None, getattr(exc, "code", None) or type(exc).__name__


@router.get("/people")
def list_people(
    request: Request,
    scope: dict[str, str] | None = Depends(people_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> dict:
    """§6.4: everyone who has signed in, pending requests first, the last 50
    audit entries, and the accounts this admin may lend. The lendable list
    comes from the quota broker; when it does not answer, the rest of the
    page is still served and `lendable_accounts` is null with the reason.

    THE PERSON × WORKSPACE GRID (S5c, S5f): `workspaces` is one column per
    group workspace and each row's `roles` its cell in each. A platform admin
    (`scope: platform`) is served every registered group workspace and
    everything above. A workspace admin (`scope: workspaces`) is served only
    the workspaces they administer and the people in them, and none of the
    rest (`People.in_workspaces`). Every write on this router stays a
    platform admin's (`admin_auth`).

    A PLATFORM ADMIN'S CELL IS `admin` in every column, for both readers
    (`people.role_cell`, `RoleStore.resolve`'s rule; BGD 2026-10-26, part of
    #2391): the admin holders are read BEFORE the grid and their emails passed
    to it, and their rows carry `platform_admin: true`, which the page draws
    as "(platform admin)" under the cell (`people.mark_platform_admin`; BGD
    2026-10-26, "Mark the source"). A workspace admin is not served the list
    itself, so for them the marker is the only place it shows. ADMIN_GROUPS
    members hold admin through a group nobody can list here
    (`AdminRoles.holders`), so their cells stay the stored role. When the
    holders cannot be read the grid is drawn from the stored roles, as before
    this rule, and the Admins card says why."""
    join_requests, join_error = open_join_requests(ctx, people, scope)
    admins, admins_error = admin_holders(ctx)
    platform_admins = {str(a["email"]).lower() for a in admins or ()}
    if scope is not None:
        return people.in_workspaces(ctx.roles, scope, join_requests, join_error,
                                    platform_admins=platform_admins)
    body = people.everyone(store=ctx.store, join_requests=join_requests,
                           join_requests_error=join_error)
    body["scope"] = SCOPE_PLATFORM
    # The grid's columns fail alone, like the two sections below: a registry
    # that cannot be read leaves the list served and says why.
    try:
        columns, grants = people.role_columns(
            ctx.roles, ctx.authenticator.registered_group_workspaces())
        people.add_roles(body["people"], columns, grants, join_requests,
                         platform_admins=platform_admins)
        body["workspaces"] = columns
        body["workspaces_error"] = None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        body["workspaces"] = None
        body["workspaces_error"] = getattr(exc, "code", None) or type(exc).__name__
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
    body["admins"] = admins
    body["admins_error"] = admins_error
    return body


def admin_holders(ctx: AppContext) -> tuple[list[dict[str, Any]] | None, str | None]:
    """`AdminRoles.holders()` and None, or None and why it could not be read:
    the Admins card's section and the grid's platform admins, read once."""
    try:
        return _roles(ctx).holders(), None
    except Exception as exc:  # noqa: BLE001 - one section, never the page
        return None, getattr(exc, "code", None) or type(exc).__name__


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
    body = {**people.sweep(pool), "loans_error": pool_error}
    # Join requests decided or expired more than 90 days ago (Q5 (a)); the
    # `admin_audit` rows stay. Fails alone: the dispatch sweep has run.
    try:
        body["join_requests_pruned"], body["join_prune_error"] = join_store(ctx).prune(), None
    except Exception as exc:  # noqa: BLE001 - the next sweep prunes them
        body["join_requests_pruned"] = None
        body["join_prune_error"] = getattr(exc, "code", None) or type(exc).__name__
    return body


#: A record in one of these states is still being created, so naming its
#: first admin is part of adding it; a `ready` one already has its people.
BEING_CREATED = frozenset({ws.REQUESTED, ws.APPROVED, ws.APPLYING, ws.NEEDS_OWNER})
FIRST_ADMIN_REASON = "first admin, named when the workspace was added"


def name_first_admin(ctx: AppContext, record: dict[str, Any], email: str, *,
                     by: str) -> dict[str, Any]:
    """Grant `email` admin in the workspace `record` is, and nowhere else,
    through the signed, append-only override and its `admin_audit` row
    (`roles.RoleStore.write`, the write `PUT /v1/admin/workspaces/{id}/roles/
    person` makes). The tenant id is the stored record's, which
    `People.create` derived from the group's address. A person not yet in
    the group still gets the grant; it applies once they are in it (§4.6),
    and `member` says which. A grant already standing is not written again,
    so a repeated request is idempotent. A record no longer being created is
    answered `granted: false`: its admins are People and roles' to change. A
    write that fails leaves the workspace filed and says why; the request
    repeated while it is being created writes the grant then."""
    tenant_id = str(record.get("tenant_id") or "")
    group = str(record.get("principal") or "")
    answer: dict[str, Any] = {"email": email, "role": roles.ADMIN}
    if ws.state_of(record) not in BEING_CREATED:
        return {**answer, "granted": False, "reason": "workspace_exists"}
    store = ctx.roles
    if store is None:
        return {**answer, "granted": False, "reason": "role_unavailable"}
    try:
        current = store.newest_valid(tenant_id, email, fresh=True)
        member = group_member(ctx, email, tenant_id, group)
        if current is not None and current.role == roles.ADMIN:
            return {**answer, "granted": True, "member": member}
        if current is not None and current.role is not None:
            previous, from_source = current.role, roles.SOURCE_OVERRIDE
        else:
            previous, from_source = store.default_role(tenant_id, fresh=True)[0], \
                roles.SOURCE_DEFAULT
        store.write(
            tenant_id=tenant_id, subject=email, role=roles.ADMIN, by=by,
            via=roles.VIA_PLATFORM_ADMIN, reason=FIRST_ADMIN_REASON, previous=current,
            action=roles.AUDIT_GRANTED,
            detail={"from": previous, "to": roles.ADMIN, "from_source": from_source,
                    "member": member, "first_admin": True})
    except ApiError as exc:
        return {**answer, "granted": False, "reason": exc.code}
    return {**answer, "granted": True, "member": member}


@router.post("/workspaces")
def create_workspace(
    body: NewWorkspaceRequest,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
    people: People = Depends(people_service),
) -> JSONResponse:
    """File a workspace of any kind (docs/design/workspace-kinds.md §4.2).
    202 with the record when this call created it (already `approved` when
    the requester may approve it), 200 when it already stood.

    `first_admin` (a group workspace's only) is checked BEFORE anything is
    created -- one person's address, never a service account -- and granted
    after (`name_first_admin`), its outcome served as `first_admin`."""
    first_admin = None
    if body.first_admin is not None:
        if body.kind != ws.GROUP:
            raise ValidationFailed(
                "first_admin names a group workspace's first admin: a personal "
                "workspace's person is its admin, and a service workspace has no people",
                detail={"field": "first_admin"})
        first_admin = roles.subject_for_write(body.first_admin)
    status, record = people.create(
        ctx.submissions.workspaces, kind=body.kind, principal=body.principal,
        display_name=body.display_name,
        limits=body.limits.model_dump(exclude_none=True) if body.limits else None,
        by=auth.email, is_owner=bool(auth.is_owner))
    content = people.admin_view(record)
    if first_admin is not None:
        content["first_admin"] = name_first_admin(
            ctx, record, first_admin, by=(auth.email or "").strip().lower())
    return JSONResponse(status_code=status, content=content)


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
