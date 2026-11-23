"""Join requests: a person asks to join a group workspace through its join
link, and that workspace's admins or a platform admin decide
(docs/design/join-requests.md §4.2, §4.3, §4.6, §4.8; lane JOIN-API, part of
archived issue 2325; owner's answers to Q1-Q6, 2026-10-25).

The person's own (`current_auth`; not tenant-scoped, because the person is
not in the workspace yet):

    POST   /v1/join/lookup                {code}            {display_name} | 404
    POST   /v1/join-requests              {code, message?}  202 made, 200 the open one
    GET    /v1/join-requests/mine                           {requests: [...]}
    POST   /v1/join-requests/{ref}/withdraw

The person's own, in the tenant `X-Swarm-Tenant` selects (any role there;
only the caller, never a subject):

    POST   /v1/workspace/leave            {reason?}         ends their platform membership
                                                            (a JSON body, `{}` at least)

A workspace admin's, in the tenant `X-Swarm-Tenant` selects (its `admin`, or
a platform admin):

    GET    /v1/workspace/join-link                          {on, set_by, set_at}
    PUT    /v1/workspace/join-link        {on, rotate?}     ... and `link` when just made
    GET    /v1/workspace/members                            the platform memberships
    DELETE /v1/workspace/members/person   {email, reason?}

The decisions, for Admin › People and roles, which is not tenant-switched
(`people_scope`, then `join_scope`: a platform admin, or an admin of THAT
request's workspace, read fresh):

    POST   /v1/admin/join-requests/{ref}/approve   {role?}
    POST   /v1/admin/join-requests/{ref}/deny      {reason}

WHAT A CODE SHOWS. A code that does not work -- unknown, rotated, switched
off, for a workspace whose group changed, too long, empty -- is one
`join_link_unknown` with one body (`JoinStore.lookup`), on both routes that
take a code, and it is answered before anything about the caller's
membership is read. A working one shows the workspace's display name and
nothing else: no tenant id (it is the link document's id), no group, no
count, no admin. The code travels only in a body, never a path or a query,
and both routes are in `main.UNCAPTURED_ROUTES`. Only the PUT that makes a
code returns it, once, inside `link`; its hash is never served.

WHAT APPROVAL DOES (§4.3). One Firestore transaction, `JoinStore.decide`'s:
it re-reads the request and verifies its MAC and its state, writes the
decision and its audit row, and -- with `platform_memberships` on -- the
signed membership and its row and, when the admin lowered the role, the
role override and its row (`RoleStore.write(writer=...)`), through `also`.
All land or none do. With the switch off the decision is recorded and the
answer names the `gcloud` line that adds the person to the group:
`admitted: false`, C's behaviour.

No response carries a document id: a request's, a decision's, a
membership's or a role grant's id is what a worker would need to overwrite
it. Every body is a closed model (an extra field is 422) with no image,
command, resource spec or backend parameter (invariant 10).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field

from .. import joinrequests, memberships, roles
from .. import workspaces as ws
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import (
    Forbidden,
    JoinAlreadyMember,
    JoinAlreadyReaches,
    JoinDeciderRequired,
    JoinNotOffered,
    JoinRequestInvalid,
    JoinRequestUnknown,
    JoinRoleNotAllowed,
    JoinWorkspaceChanged,
    MembershipUnavailable,
    NotFound,
    UpstreamUnavailable,
    WorkspaceLeaveLastAdmin,
    WorkspaceLeaveNotMember,
    WorkspaceLeavePersonOnly,
)
from ..joinrequests import Decision, JoinRequest, RequestView
from ..judgment import settings_keys
from ..schemas import StrictModel
from . import last_admin
from .people import (
    join_scope,
    join_store,
    membership_store,
    people_scope,
    workspace_labels,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["join-requests"])

#: The two routes that take a join code in their body (`main.UNCAPTURED_ROUTES`),
#: and the one whose answer carries a new one.
LOOKUP_ROUTE = "/v1/join/lookup"
ASK_ROUTE = "/v1/join-requests"
LINK_ROUTE = "/v1/workspace/join-link"
#: The person's own: leave the selected workspace (`roles.PERSON_ROUTES`).
LEAVE_ROUTE = "/v1/workspace/leave"

#: What a person is shown for a workspace with no display name: never its id
#: or its group, which a working code does not reveal (§4.2).
UNNAMED = "a group workspace"
#: A code is 22 characters (`secrets.token_urlsafe(16)`). The body allows far
#: more, so a long code is the one `join_link_unknown` (the store hashes it as
#: the empty string), never a 422 that tells it apart from an unknown one.
CODE_BODY_MAX = 4096
#: The platform memberships one workspace's list reads, and asks the group
#: about one by one: more than a team joins by request, and bounded so the
#: list is never a scan of the collection.
MEMBERS_SCAN = 2000
MEMBERS_SHOWN = 200
#: The reason a role lowered on approval is recorded with; every member reads it.
LOWERED_REASON = "set when their join request was approved"


def platform_memberships_on(settings: Any) -> bool:
    """`platform_memberships` (lane JOIN-RESOLVE's setting, off by default):
    whether an approval writes a membership that admits (Q1 (A)), or only
    records the decision and names the group change (C's behaviour)."""
    return getattr(settings, "platform_memberships", False) is True


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class CodeBody(StrictModel):
    code: str = Field(max_length=CODE_BODY_MAX)


class AskBody(CodeBody):
    #: Shown to the deciding admins beneath the request; redacted when written.
    message: str | None = Field(default=None, max_length=joinrequests.MESSAGE_MAX)


class LinkBody(StrictModel):
    on: bool
    #: A new code: the old one stops working at once.
    rotate: bool = False


class MemberRemoval(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    reason: str | None = Field(default=None, max_length=memberships.REASON_MAX)


class LeaveBody(StrictModel):
    """REQUIRED, `{}` at least, as application/json (epic 21): no body, or
    any body a cross-site form can send (urlencoded, multipart, text/plain),
    is a 422 before anything is read, so another site cannot make a signed-in
    person leave on their front-door cookie alone -- a page elsewhere cannot
    send application/json without a CORS preflight swarm-api never grants."""

    #: No `email`: only the caller leaves, so a subject is a 422.
    reason: str | None = Field(default=None, max_length=memberships.REASON_MAX)


class ApproveBody(StrictModel):
    #: `viewer` or the workspace's default; none given is the default (Q4 (a)).
    role: str | None = Field(default=None, max_length=32)


class DenyBody(StrictModel):
    #: Required, and shown to the person. Optional in the MODEL so the decider
    #: check runs before it: `JoinStore.decide` refuses a blank one (422).
    reason: str | None = Field(default=None, max_length=joinrequests.REASON_MAX)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _person(auth: AuthContext, refusal: Callable[[str], Exception] = Forbidden) -> str:
    """The caller, when they are a person who may ask (§4.1) or leave
    (§4.4): never a service account, the schedule tick, the rollup sweeper,
    a listed service account or a pool admin acting as one."""
    email = (auth.email or "").strip().lower()
    if (auth.is_pool_admin or auth.is_rollup_sweeper or auth.is_schedule_tick
            or auth.member_scope or auth.tenant_member or ws.is_service_identity(email)
            or not memberships.is_person(email)):
        raise refusal("joining or leaving a workspace is a person's own request; this "
                      "identity is not a person")
    return email


_registered = last_admin.registered
_in_group = last_admin.in_group


def _label(ctx: AppContext, tenant_id: str) -> str:
    return workspace_labels(ctx, (tenant_id,)).get(tenant_id, UNNAMED)


def _mine(view: RequestView, label: str) -> dict[str, Any]:
    return dict(view.to_api(), display_name=label)


def _still_in_group(request: Request, ctx: AppContext, email: str, group: str) -> bool | None:
    try:
        return _in_group(request, ctx, email, group)
    except UpstreamUnavailable:
        return None


def _group_workspace(ctx: AppContext, auth: AuthContext) -> str:
    """The group of the SELECTED workspace, when it is a registered group
    workspace whose id still selects the group the request resolved; a
    personal or service workspace takes no requests (§4.6)."""
    if roles.is_personal(auth):
        raise JoinNotOffered("a personal workspace has one person and takes no join requests")
    group = _registered(ctx).get(auth.tenant_id)
    if group is None or group != (auth.tenant_principal or "").strip().lower():
        raise JoinNotOffered("only a registered group workspace takes join requests")
    return group


def _resolve_admin(ctx: AppContext, auth: AuthContext,
                   refusal: Callable[[roles.Resolution], Exception], *,
                   probe_admin: bool = False) -> roles.Resolution:
    """The caller's role in the selected workspace, read FRESH, when it is
    `admin` -- by their own grant or as a platform admin; else `refusal`.
    `probe_admin` only from a route on `roles.PROBE_ADMIN_ROUTES`; anywhere
    else a scoped probe admin is 403 `probe_admin_not_permitted`."""
    store = ctx.roles
    assert store is not None
    me = store.resolve(auth, fresh=True, probe_admin=probe_admin)
    if me.role != roles.ADMIN:
        if me.probe_admin:
            raise roles.probe_admin_refusal(auth)
        raise refusal(me)
    return me


def _not_decider(_me: Any = None) -> JoinDeciderRequired:
    return JoinDeciderRequired(
        "only this workspace's admins, or a platform admin, decide who is in it; nothing "
        "was changed")


def _same_request(view: RequestView) -> Callable[[Any, JoinRequest, Decision], None]:
    """The transaction's own re-read must be the request `join_scope`
    authorised: the same workspace, group and person, or nothing lands."""
    expected = (view.request.tenant_id, view.request.group, view.request.requester)

    def check(_writer: Any, request: JoinRequest, _decision: Decision) -> None:
        if (request.tenant_id, request.group, request.requester) != expected:
            raise _not_decider()

    return check


# ---------------------------------------------------------------------------
# The person's own
# ---------------------------------------------------------------------------


@router.post(LOOKUP_ROUTE)
def lookup(
    body: CodeBody,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """What a working code shows: the workspace's display name, nothing
    else. Every code that does not work is the one 404 (§4.2). Throttled by
    `current_auth`'s per-caller budget, as every route is."""
    _person(auth)
    tenant_id, _group = join_store(ctx).lookup(body.code, _registered(ctx))
    return {"display_name": _label(ctx, tenant_id)}


@router.post(ASK_ROUTE)
def ask(
    body: AskBody,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> JSONResponse:
    """Ask to join the workspace a working code names: 202 with the new
    request, or 200 with the person's open one there, which writes nothing.
    The code is checked FIRST, so nothing about the caller's membership is
    read, or answered, for a code that does not work. A platform admin
    (409 `join_already_reaches`) and a member, by the group or by a counted
    platform membership (409 `join_already_member`), need nobody's decision.
    The rate limits are `JoinStore.ask`'s, in the transaction that writes."""
    email = _person(auth)
    joins = join_store(ctx)
    registered = _registered(ctx)
    tenant_id, group = joins.lookup(body.code, registered)
    label = _label(ctx, tenant_id)
    if auth.is_admin:
        raise JoinAlreadyReaches(
            f"you are a platform admin, so you reach {label} already; nobody needs to decide "
            "a request from you")
    ctx.store.assert_tenant_scope(tenant_id, group)
    member = _in_group(request, ctx, email, group)
    if not member and platform_memberships_on(ctx.settings):
        member = membership_store(ctx).is_member(email, tenant_id, registered, fresh=True)
    if member:
        raise JoinAlreadyMember(f"you are already in {label}; switch to it from the "
                                "workspace menu")
    view, created = joins.ask(tenant_id=tenant_id, group=group, requester=email,
                              message=body.message)
    return JSONResponse(status_code=202 if created else 200, content=_mine(view, label))


@router.get("/v1/join-requests/mine")
def my_requests(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The caller's own requests, newest first, every state, each with the
    workspace's display name, and a denial's reason (§4.8)."""
    email = _person(auth)
    views = join_store(ctx).mine(email)
    labels = workspace_labels(ctx, (v.request.tenant_id for v in views))
    return {"requests": [_mine(v, labels.get(v.request.tenant_id, UNNAMED)) for v in views]}


@router.post("/v1/join-requests/{ref}/withdraw")
def withdraw(
    ref: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The requester withdraws their own open request. Anyone else's ref,
    and one that fails its MAC, reads as one that does not exist (§4.6)."""
    email = _person(auth)
    try:
        view, _ = join_store(ctx).decide(ref, state=joinrequests.WITHDRAWN, by=email,
                                         via=joinrequests.VIA_REQUESTER)
    except JoinRequestInvalid:
        raise JoinRequestUnknown(joinrequests.REQUEST_UNKNOWN_MESSAGE) from None
    return {"ref": view.request.ref, "state": view.state}


# ---------------------------------------------------------------------------
# The selected workspace's admin
# ---------------------------------------------------------------------------


def _link_admin(ctx: AppContext, auth: AuthContext, *, probe_admin: bool = False) -> str:
    """The join link is the workspace `admin`'s, or a platform admin's: the
    403 is `role_admin_required`, RAISED, because these are new routes (as
    `routes/roles.py` raises it). A scoped probe admin's too (`probe_admin`,
    `roles.PROBE_ADMIN_ROUTES`): the join probe opens the link a request is
    made through."""
    if roles.is_personal(auth):
        raise JoinNotOffered("a personal workspace has one person and takes no join requests")
    store = ctx.roles
    assert store is not None
    _resolve_admin(ctx, auth, lambda me: store.refusal(auth, me, roles.ADMIN),
                   probe_admin=probe_admin)
    return _group_workspace(ctx, auth)


@router.get(LINK_ROUTE)
def get_join_link(
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Whether the link is on, and who set it when. Never the code, which
    is not stored, and never its hash."""
    _link_admin(ctx, auth, probe_admin=True)
    return join_store(ctx).link(tenant_id).to_api()


@router.put(LINK_ROUTE)
def set_join_link(
    body: LinkBody,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Turn the link on or off, or rotate its code. `link` is in the answer
    only when a code was just made -- the one time it is ever shown -- with
    the code in the URL's fragment, which a browser never sends."""
    group = _link_admin(ctx, auth, probe_admin=True)
    state, code = join_store(ctx).set_link(tenant_id=tenant_id, group=group, on=body.on,
                                           rotate=body.rotate, by=auth.email)
    out = state.to_api()
    if code is not None:
        base = (getattr(ctx.settings, "console_url", "") or "").rstrip("/")
        out["link"] = f"{base}/join#{code}"
    return out


def _members_admin(ctx: AppContext, auth: AuthContext, *,
                   probe_admin: bool = False) -> tuple[str, roles.Resolution]:
    """Who lists and removes platform memberships: the decider of §4.6. A
    scoped probe admin lists them (`probe_admin`, from `list_members`) and
    never removes one: removal is not something the probes measure."""
    if roles.is_personal(auth):
        raise JoinNotOffered("a personal workspace has one person and takes no join requests")
    me = _resolve_admin(ctx, auth, _not_decider, probe_admin=probe_admin)
    return _group_workspace(ctx, auth), me


@router.get("/v1/workspace/members")
def list_members(
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Everyone whose newest valid platform membership here is `member`,
    newest first: who let them in, when and how, whether the workspace's
    group changed since (`group_changed`: it admits nobody then), and
    whether a fresh per-group check finds them in the Google group too
    (`still_in_group`, null when the directory did not answer). Never a
    document id."""
    group, _me = _members_admin(ctx, auth, probe_admin=True)
    registered = _registered(ctx)
    store = membership_store(ctx)
    keys = settings_keys(getattr(ctx.settings, "settings_key", ""),
                         getattr(ctx.settings, "settings_key_previous", ""))
    try:
        snaps = list(ctx.db.collection(memberships.COLLECTION)
                     .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                     .limit(MEMBERS_SCAN).stream())
    except Exception as exc:  # noqa: BLE001 - any Firestore failure is a retry
        log.warning("workspace memberships unreadable (%s)", type(exc).__name__)
        raise MembershipUnavailable(
            "this workspace's memberships could not be read; retry shortly") from None
    subjects: list[str] = []
    for snap in snaps:
        doc = snap.to_dict() or {}
        subject = doc.get("subject")
        if (subject in subjects or doc.get("tenant_id") != tenant_id
                or not memberships.is_person(subject)
                or not memberships.membership_verifies(keys, doc)):
            continue
        subjects.append(subject)
        if len(subjects) >= MEMBERS_SHOWN:
            break
    rows: list[dict[str, Any]] = []
    for subject in subjects:
        # Each person's own newest valid grant decides, as resolution reads it.
        grant = store.read(subject, fresh=True).newest.get(tenant_id)
        if grant is None or grant.state != memberships.MEMBER:
            continue
        rows.append({"email": subject, "by": grant.by, "at": grant.at, "via": grant.via,
                     "group_changed": not grant.counts_for(registered),
                     "still_in_group": _still_in_group(request, ctx, subject, group)})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"platform_memberships": platform_memberships_on(ctx.settings), "members": rows}


@router.delete("/v1/workspace/members/person")
def remove_member(
    body: MemberRemoval,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Write a `removed` grant over the person's platform membership here,
    with its audit row (§4.4). It ends the platform membership only: when
    the person is in the Google group too, `still_in_group` says so, and the
    group is the other half, which the platform cannot change.

    A person who holds `admin` by override here -- the caller removing
    themselves included -- whom the group does not hold is removed only
    while another admin who can still select the workspace remains (409
    `workspace_leave_last_admin`, nothing written but the refusal's audit
    row), checked and written in one transaction over the workspace's guard
    document, as `leave_workspace` does (`last_admin`)."""
    group, me = _members_admin(ctx, auth)
    subject = memberships.clean_person(body.email, "email")
    store = membership_store(ctx)
    # Read fresh first, so a person with nothing to remove hears that before
    # any role or group lookup; read again inside the transaction, which decides.
    grant = store.read(subject, fresh=True).newest.get(tenant_id)
    if grant is None or grant.state != memberships.MEMBER:
        raise _no_membership(subject)
    via = (memberships.VIA_PLATFORM_ADMIN if me.source == roles.SOURCE_PLATFORM_ADMIN
           else memberships.VIA_WORKSPACE_ADMIN)
    by = (auth.email or "").strip().lower()
    registered = _registered(ctx)
    if subject == by:
        message = ("you are this workspace's only admin who can still select it; name another "
                   "admin who is a member before you remove yourself. Nothing was written")
    else:
        message = (f"{subject} is this workspace's only admin who can still select it; name "
                   "another admin who is a member before removing them. Nothing was written")

    def _apply(txn: Any) -> bool | None:
        last_admin.read_guard(ctx, txn, tenant_id)
        current = store.read(subject, transaction=txn).newest.get(tenant_id)
        if current is None or current.state != memberships.MEMBER:
            raise _no_membership(subject)
        # Decided here, after the guard read, never before the transaction: a
        # promotion committed in between changed the guard, so this try is
        # aborted and the retry reads the new role. Fail-closed for an admin:
        # the group holding them keeps them, and their override, here; a
        # lookup that fails is a 503, never a guess.
        admin = last_admin.admin_by_override(ctx, tenant_id, subject)
        still: bool | None = (_in_group(request, ctx, subject, group) if admin
                              else _still_in_group(request, ctx, subject, group))
        if admin and not still:
            last_admin.require_admin_remains(
                request, ctx, txn, tenant_id=tenant_id, subject=subject, by=by, group=group,
                registered=registered, message=message)
        else:
            last_admin.write_guard(ctx, txn, tenant_id, by=by)
        store.write(tenant_id=tenant_id, group=current.group, subject=subject,
                    state=memberships.REMOVED, by=auth.email, via=via, reason=body.reason,
                    detail={"still_in_group": still}, writer=txn)
        return still

    still = last_admin.run(ctx, _apply, tenant_id=tenant_id, subject=subject, by=by,
                           detail={"route": last_admin.ROUTE_REMOVE, "via": via})
    return {"email": subject, "state": memberships.REMOVED, "still_in_group": still}


def _no_membership(subject: str) -> NotFound:
    return NotFound(f"{subject} holds no platform membership of this workspace; nothing "
                    "was written")


def _leavable(read: memberships.MembershipRead, tenant_id: str,
              registered: dict[str, str]) -> memberships.Membership:
    """The caller's membership here when it still counts for the registered
    group; a bounded read cut short before it answered is a 503, never a
    404 that the unread grant could contradict."""
    grant = read.newest.get(tenant_id)
    if grant is not None and grant.counts_for(registered):
        return grant
    if grant is None and read.cut_short:
        raise MembershipUnavailable(
            "your workspace memberships could not be read: too many records failed "
            "verification; an operator has been alerted. Nothing was written")
    raise _not_member()


def _not_member() -> WorkspaceLeaveNotMember:
    return WorkspaceLeaveNotMember(
        "you hold no platform membership of this workspace, so there is nothing to leave; "
        "if its Google group lists you, the group's own admins change that. Nothing was "
        "written")


@router.post(LEAVE_ROUTE)
def leave_workspace(
    request: Request,
    body: LeaveBody,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The caller leaves the workspace their own sign-in selected: a
    `removed` grant over THEIR platform membership here, `via: self`, with
    its audit row (§4.4). Never anyone else's -- the body has no subject --
    and never the Google group, which the platform cannot change:
    `still_in_group` says when the group still lists them. A role override
    they held here is KEPT, exactly as `remove_member` keeps it, so asking
    and being approved again restores what an admin named.

    Only a membership that still counts for the workspace's registered group
    is left (`counts_for`, as resolution reads it). The workspace's last
    `admin` by override whom the group does not hold cannot leave it without
    another admin who can still select it (409 `workspace_leave_last_admin`,
    `last_admin.admin_remains`). That check and the `removed` grant are one
    Firestore transaction, which reads and writes the workspace's guard
    document, so two admins leaving at once -- or one leaving while another
    is removed or demoted -- conflict and the one retried re-checks who is
    left: both cannot go. A refusal leaves one audit row and nothing else."""
    email = _person(auth, WorkspaceLeavePersonOnly)
    if roles.is_personal(auth):
        raise JoinNotOffered("a personal workspace is yours alone; there is nothing to leave")
    registered = _registered(ctx)
    group = registered.get(tenant_id)
    if group is None or group != (auth.tenant_principal or "").strip().lower():
        raise JoinNotOffered("only a group workspace holds platform memberships to leave")
    store = membership_store(ctx)
    # Read fresh first, so a person with nothing to leave hears that before
    # any group lookup; read again inside the transaction, which decides.
    _leavable(store.read(email, fresh=True), tenant_id, registered)
    reason = body.reason
    message = ("you are this workspace's only admin who can still select it; name another "
               "admin who is a member before you leave. Nothing was written")

    def _apply(txn: Any) -> bool | None:
        last_admin.read_guard(ctx, txn, tenant_id)
        current = _leavable(store.read(email, transaction=txn), tenant_id, registered)
        # Decided after the guard read, as `remove_member` decides it.
        admin = last_admin.admin_by_override(ctx, tenant_id, email)
        if admin:
            # The group holding the caller keeps them, and their override, here.
            still: bool | None = _in_group(request, ctx, email, group)
        else:
            still = _still_in_group(request, ctx, email, group)
        if admin and not still:
            last_admin.require_admin_remains(
                request, ctx, txn, tenant_id=tenant_id, subject=email, by=email, group=group,
                registered=registered, message=message)
        else:
            last_admin.write_guard(ctx, txn, tenant_id, by=email)
        store.write(tenant_id=tenant_id, group=current.group, subject=email,
                    state=memberships.REMOVED, by=email, via=memberships.VIA_SELF,
                    reason=reason, detail={"still_in_group": still}, writer=txn)
        return still

    still = last_admin.run(ctx, _apply, tenant_id=tenant_id, subject=email, by=email,
                           detail={"route": last_admin.ROUTE_LEAVE, "via": memberships.VIA_SELF})
    return {"email": email, "state": memberships.REMOVED, "still_in_group": still}


# ---------------------------------------------------------------------------
# The decisions, for Admin › People and roles
# ---------------------------------------------------------------------------


def _role_change(ctx: AppContext, *, tenant_id: str, subject: str, chosen: str, default: str,
                 by: str, via: str, ref: str) -> dict[str, Any] | None:
    """The role write that makes `chosen` the person's role, or None when it
    already is. Read inside the approval's transaction, after its last-admin
    guard read (`approve_join_request`), and written there. The default
    chosen over a stored override CLEARS the override, so the person holds
    exactly the role the approving admin saw -- never an older grant's."""
    store = ctx.roles
    assert store is not None
    current = store.newest_valid(tenant_id, subject, fresh=True)
    held = current is not None and current.role is not None
    effective = current.role if held else default  # type: ignore[union-attr]
    if effective == chosen:
        return None
    common = {"tenant_id": tenant_id, "subject": subject, "by": by,
              "via": roles.VIA_PLATFORM_ADMIN if via == joinrequests.VIA_PLATFORM_ADMIN
              else roles.VIA_PROBE_ADMIN if via == joinrequests.VIA_PROBE_ADMIN
              else "console",
              "reason": LOWERED_REASON, "previous": current}
    if chosen == default:
        return dict(common, role=None, action=roles.AUDIT_CLEARED,
                    detail={"from": effective, "to": default, "join_request": ref})
    return dict(common, role=chosen, action=roles.AUDIT_GRANTED,
                detail={"from": effective, "to": chosen,
                        "from_source": roles.SOURCE_OVERRIDE if held else roles.SOURCE_DEFAULT,
                        "member": True, "join_request": ref})


@router.post("/v1/admin/join-requests/{ref}/approve")
def approve_join_request(
    ref: str,
    request: Request,
    body: ApproveBody | None = None,
    scope: dict[str, str] | None = Depends(people_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Approve an open request (§4.3): its workspace must still be the
    registered group it named (409 `join_workspace_changed`), and the role
    is the workspace's default or `viewer` below it (409
    `join_role_not_allowed`). With `platform_memberships` on, the decision,
    the membership, a lowered role and every audit row are ONE transaction;
    with it off, the decision is recorded and `group_change` names the
    `gcloud` line that adds the person to the group (`admitted: false`).

    An admitting approval is also a write of the last-admin guard
    (`routes/last_admin.py`): it reads the guard document and the person's
    role inside the transaction, and an approval that would lower the
    `admin` override of someone who can already select the workspace (the
    group took them in, or a platform membership counts) is 409
    `workspace_leave_last_admin` when no other such admin would remain. An
    admin named ahead of their joining, who cannot select it yet, is lowered
    without the check: no admin who could select the workspace is lost."""
    view, via = join_scope(ctx, scope, ref, auth)
    tenant_id, group, subject = view.request.tenant_id, view.request.group, view.request.requester
    if _registered(ctx).get(tenant_id) != group:
        raise JoinWorkspaceChanged(
            "this workspace is no longer the group the request was made for; the person can "
            "ask again. Nothing was written.")
    ctx.store.assert_tenant_scope(tenant_id, group)
    store = ctx.roles
    assert store is not None
    default = store.default_role(tenant_id, fresh=True)[0]
    chosen = default if body is None or body.role is None else roles.check_role(body.role)
    if chosen not in joinrequests.OFFERED_ROLES or chosen not in (default, roles.VIEWER):
        raise JoinRoleNotAllowed(
            f"an approval gives this workspace's default ({default}) or viewer below it; "
            "name an admin after they have joined")
    if via == joinrequests.VIA_PROBE_ADMIN:
        # A probe admin's approval never changes or clears a stored `admin`
        # role -- one a platform admin named ahead of the person's joining.
        # A fast answer; `also` decides it again inside the transaction.
        roles.probe_admin_admin_target(store, tenant_id, subject, chosen)
    by = (auth.email or "").strip().lower()
    admitted = platform_memberships_on(ctx.settings)
    registered = _registered(ctx)
    same = _same_request(view)
    # The authenticator's own store (`membership_store`), so the write's
    # eviction is what the person's next request reads, not a second cache's
    # 15 s (§4.4).
    grants = membership_store(ctx)

    def also(writer: Any, asked: JoinRequest, decision: Decision) -> None:
        same(writer, asked, decision)
        if not admitted:
            return
        # The guard first, then the role read fresh, so whether this lowers
        # an admin is decided on a role no guarded change committed over.
        last_admin.read_guard(ctx, writer, tenant_id)
        role_change = _role_change(ctx, tenant_id=tenant_id, subject=subject, chosen=chosen,
                                   default=default, by=by, via=via, ref=view.request.ref)
        if via == joinrequests.VIA_PROBE_ADMIN and role_change is not None:
            # On the role read after the guard: an `admin` grant a platform
            # admin stored since the early check is refused, never cleared.
            roles.refuse_probe_admin_admin_target(tenant_id, role_change["previous"], chosen)
        lowers = (role_change is not None and role_change["previous"] is not None
                  and role_change["previous"].role == roles.ADMIN)
        if lowers:
            held = grants.read(subject, transaction=writer).newest.get(tenant_id)
            # Fail-closed: a group lookup that fails is a 503, never a guess.
            lowers = ((held is not None and held.counts_for(registered))
                      or _in_group(request, ctx, subject, group))
        if lowers:
            last_admin.require_admin_remains(
                request, ctx, writer, tenant_id=tenant_id, subject=subject, by=by, group=group,
                registered=registered,
                message=f"{subject} is this workspace's only admin who can still select it; "
                        "name another admin who is a member before approving them at a lower "
                        "role. Nothing was written")
        else:
            last_admin.write_guard(ctx, writer, tenant_id, by=by)
        # The role first, the membership after: whichever fails, the
        # transaction carries both, so neither lands alone.
        if role_change is not None:
            store.write(**role_change, writer=writer)
        grants.write(
            tenant_id=tenant_id, group=group, subject=subject, state=memberships.MEMBER,
            by=by, via=memberships.VIA_JOIN_REQUEST, detail={"ref": asked.ref},
            writer=writer)

    try:
        join_store(ctx).decide(ref, state=joinrequests.APPROVED, by=by, via=via, role=chosen,
                               admitted=admitted, also=also)
    except WorkspaceLeaveLastAdmin:
        last_admin.audit_refusal(ctx, tenant_id=tenant_id, subject=subject, by=by, detail={
            "route": last_admin.ROUTE_JOIN_APPROVED, "via": via, "to": chosen})
        raise
    out: dict[str, Any] = {"ref": view.request.ref, "state": joinrequests.APPROVED,
                           "email": subject, "tenant_id": tenant_id, "role": chosen,
                           "admitted": admitted, "group_change": None}
    if not admitted:
        out["group_change"] = {
            "group": group,
            "command": f"gcloud identity groups memberships add --group-email={group} "
                       f"--member-email={subject}",
        }
    return out


@router.post("/v1/admin/join-requests/{ref}/deny")
def deny_join_request(
    ref: str,
    body: DenyBody | None = None,
    scope: dict[str, str] | None = Depends(people_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Deny an open request, with the reason the person is shown. The
    person may ask that workspace again after `DENIED_HOLD_DAYS`."""
    view, via = join_scope(ctx, scope, ref, auth)
    _view, decision = join_store(ctx).decide(
        ref, state=joinrequests.DENIED, by=(auth.email or "").strip().lower(), via=via,
        reason=body.reason if body is not None else None, also=_same_request(view))
    return {"ref": view.request.ref, "state": joinrequests.DENIED,
            "email": view.request.requester, "tenant_id": view.request.tenant_id,
            "reason": decision.reason}
