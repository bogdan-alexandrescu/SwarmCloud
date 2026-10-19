"""Workspace roles: the roster and its writes (docs/design/workspace-roles.md §4.3).

    GET    /v1/workspace/roles           the selected workspace's default, overrides,
                                         the caller's own role and the recent events;
                                         every role reads it (Q7)
    PUT    /v1/workspace/roles/person    {email, role, reason?, via?}   its admin
    DELETE /v1/workspace/roles/person    {email, reason?, via?}         back to the default
    PUT    /v1/workspace/roles/default   {role, reason?, via?}          viewer | member

    GET    /v1/admin/workspaces/{tenant_id}/roles            a platform admin's, for any
    PUT    /v1/admin/workspaces/{tenant_id}/roles/person     registered group workspace,
    DELETE /v1/admin/workspaces/{tenant_id}/roles/person     without switching to it: how
    PUT    /v1/admin/workspaces/{tenant_id}/roles/default    a first admin is named (Q2)
    GET    /v1/admin/roles/person?email=…                    every override one email holds

The email is in the body, not the path, so addresses do not land in request
logs and load-balancer access logs (as `routes/people.py` avoids them).

THE WRITES ARE NEW ROUTES, so `role_admin_required` is RAISED here, never sent
through `refusals.refuse`: its switch governs the existing routes that gained
the gate, and letting a non-admin write a role while that switch is off would
be the breach itself. Every write is one signed, append-only grant and its
`admin_audit` row in one batch (`roles.RoleStore.write`); a write that would
change nothing is 409 `role_unchanged` and writes nothing. No response carries
a grant's document id: the id is what a tenant worker would need to overwrite
a grant (§4.4).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from .. import roles
from ..auth import AuthContext
from ..deps import AppContext, admin_auth, current_auth, get_context, tenant_scope
from ..errors import (
    DefaultRoleNotAllowed,
    NotFound,
    PersonalWorkspaceRolesFixed,
    RoleUnavailable,
    RoleUnchanged,
    UpstreamUnavailable,
)

router = APIRouter(tags=["roles"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    #: Where the change was made: shown beside it, never acted on. A platform
    #: admin's write is recorded `platform_admin` whatever this says.
    via: Literal["console", "cli"] = "console"


class PersonRole(_Body):
    email: str
    role: str


class PersonClear(_Body):
    email: str


class DefaultRole(_Body):
    role: str


# ---------------------------------------------------------------------------
# Shared by the workspace and the platform-admin routes
# ---------------------------------------------------------------------------


def _member(ctx: AppContext, email: str, tenant_id: str, group: str) -> bool | None:
    """Whether `email` is in the group now (§4.6: a grant to someone not yet
    in it is stored and applies when they join). None when the directory did
    not answer: the write does not depend on it."""
    tenant = SimpleNamespace(kind="group", tenant_id=tenant_id, principal=group)
    try:
        return ctx.authenticator.is_tenant_member(email, tenant, fresh=True)
    except UpstreamUnavailable:
        return None


def _set_person(ctx: AppContext, *, tenant_id: str, group: str, by: str, via: str,
                body: PersonRole) -> dict[str, Any]:
    role = roles.check_role(body.role)
    subject = roles.subject_for_write(body.email)
    reason = roles.clean_reason(body.reason)
    store = ctx.roles
    assert store is not None
    current = store.newest_valid(tenant_id, subject, fresh=True)
    if current is not None and current.role == role:
        raise RoleUnchanged(f"{subject} already holds {role} in {tenant_id}; nothing was written")
    if current is not None and current.role is not None:
        previous, from_source = current.role, roles.SOURCE_OVERRIDE
    else:
        previous, from_source = store.default_role(tenant_id, fresh=True)[0], roles.SOURCE_DEFAULT
    member = _member(ctx, subject, tenant_id, group)
    granted_at = store.write(
        tenant_id=tenant_id, subject=subject, role=role, by=by, via=via, reason=reason,
        previous=current, action=roles.AUDIT_GRANTED,
        detail={"from": previous, "to": role, "from_source": from_source, "member": member})
    return {"email": subject, "role": role, "previous_role": previous,
            "source": roles.SOURCE_OVERRIDE, "granted_at": granted_at, "member": member}


def _clear_person(ctx: AppContext, *, tenant_id: str, by: str, via: str,
                  body: PersonClear) -> dict[str, Any]:
    subject = roles.subject_for_write(body.email)
    reason = roles.clean_reason(body.reason)
    store = ctx.roles
    assert store is not None
    current = store.newest_valid(tenant_id, subject, fresh=True)
    if current is None or current.role is None:
        raise RoleUnchanged(
            f"{subject} already holds {tenant_id}'s default role; nothing was written")
    default = store.default_role(tenant_id, fresh=True)[0]
    granted_at = store.write(
        tenant_id=tenant_id, subject=subject, role=None, by=by, via=via, reason=reason,
        previous=current, action=roles.AUDIT_CLEARED,
        detail={"from": current.role, "to": default})
    return {"email": subject, "role": default, "previous_role": current.role,
            "source": roles.SOURCE_DEFAULT, "granted_at": granted_at}


def _set_default(ctx: AppContext, *, tenant_id: str, by: str, via: str,
                 body: DefaultRole) -> dict[str, Any]:
    role = roles.check_role(body.role)
    if role not in roles.DEFAULT_ROLES:
        raise DefaultRoleNotAllowed(
            "a workspace's default role is viewer or member, never admin: an admin is "
            "named person by person", detail={"allowed": list(roles.DEFAULT_ROLES)})
    reason = roles.clean_reason(body.reason)
    store = ctx.roles
    assert store is not None
    previous, previous_grant = store.default_role(tenant_id, fresh=True)
    if previous == role:
        raise RoleUnchanged(f"{tenant_id}'s default is already {role}; nothing was written")
    granted_at = store.write(
        tenant_id=tenant_id, subject=roles.DEFAULT_SUBJECT, role=role, by=by, via=via,
        reason=reason, previous=previous_grant, action=roles.AUDIT_DEFAULT_SET,
        detail={"from": previous, "to": role})
    return {"tenant_id": tenant_id, "default_role": role, "previous_default": previous,
            "default_source": "grant", "granted_at": granted_at}


def _workspace_writer(ctx: AppContext, auth: AuthContext, requested_via: str) -> str:
    """Authorise a role write in the SELECTED workspace and return the `via`
    it is recorded with. A personal workspace is 409 before anything else:
    its person is its fixed admin, so there is nothing to grant (§4.5)."""
    if roles.is_personal(auth):
        raise PersonalWorkspaceRolesFixed(
            "a personal workspace has only its person, who is its admin; its roles "
            "cannot be changed")
    store = ctx.roles
    assert store is not None
    me = store.resolve(auth, fresh=True)
    if me.role != roles.ADMIN:
        # RAISED, not refused: a new route (see the module docstring).
        raise store.refusal(auth, me, roles.ADMIN)
    if me.source != roles.SOURCE_PLATFORM_ADMIN:
        return requested_via
    if auth.tenant_via == roles.VIA_PLATFORM_ADMIN:
        return roles.VIA_PLATFORM_ADMIN
    # A member platform admin: recorded as one unless their own grant makes
    # them this workspace's admin anyway (§4.2.4, "Actions with their own
    # audit rows ... also carry via: platform_admin").
    try:
        own = store.person_role(auth.tenant_id, auth.email, fresh=True).role
    except RoleUnavailable:
        own = None
    return requested_via if own == roles.ADMIN else roles.VIA_PLATFORM_ADMIN


# ---------------------------------------------------------------------------
# The selected workspace's own
# ---------------------------------------------------------------------------


@router.get("/v1/workspace/roles")
def workspace_roles(
    auth: AuthContext = Depends(current_auth),
    _tenant: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    assert ctx.roles is not None
    return roles.workspace_payload(ctx.roles, auth)


@router.put("/v1/workspace/roles/person")
def workspace_set_person(
    body: PersonRole,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    via = _workspace_writer(ctx, auth, body.via)
    return _set_person(ctx, tenant_id=tenant_id, group=auth.tenant_principal, by=auth.email,
                       via=via, body=body)


@router.delete("/v1/workspace/roles/person")
def workspace_clear_person(
    body: PersonClear,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    via = _workspace_writer(ctx, auth, body.via)
    return _clear_person(ctx, tenant_id=tenant_id, by=auth.email, via=via, body=body)


@router.put("/v1/workspace/roles/default")
def workspace_set_default(
    body: DefaultRole,
    auth: AuthContext = Depends(current_auth),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    via = _workspace_writer(ctx, auth, body.via)
    return _set_default(ctx, tenant_id=tenant_id, by=auth.email, via=via, body=body)


# ---------------------------------------------------------------------------
# A platform admin's, for any registered group workspace
# ---------------------------------------------------------------------------


def _registered(ctx: AppContext, tenant_id: str) -> str:
    """The group of a registered GROUP workspace, through the same collision
    check a member's request makes; 404 for anything else -- a `u-` id
    included, so no personal workspace is reachable here (§4.5)."""
    group = roles.registered_group_tenants(ctx.settings).get(tenant_id)
    if group is None:
        raise NotFound("no registered group workspace by that id")
    ctx.store.assert_tenant_scope(tenant_id, group)
    return group


@router.get("/v1/admin/workspaces/{tenant_id}/roles")
def admin_workspace_roles(
    tenant_id: str,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _registered(ctx, tenant_id)
    assert ctx.roles is not None
    return roles.admin_payload(ctx.roles, tenant_id)


@router.put("/v1/admin/workspaces/{tenant_id}/roles/person")
def admin_set_person(
    tenant_id: str,
    body: PersonRole,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    group = _registered(ctx, tenant_id)
    return _set_person(ctx, tenant_id=tenant_id, group=group, by=auth.email,
                       via=roles.VIA_PLATFORM_ADMIN, body=body)


@router.delete("/v1/admin/workspaces/{tenant_id}/roles/person")
def admin_clear_person(
    tenant_id: str,
    body: PersonClear,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _registered(ctx, tenant_id)
    return _clear_person(ctx, tenant_id=tenant_id, by=auth.email,
                         via=roles.VIA_PLATFORM_ADMIN, body=body)


@router.put("/v1/admin/workspaces/{tenant_id}/roles/default")
def admin_set_default(
    tenant_id: str,
    body: DefaultRole,
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    _registered(ctx, tenant_id)
    return _set_default(ctx, tenant_id=tenant_id, by=auth.email,
                        via=roles.VIA_PLATFORM_ADMIN, body=body)


@router.get("/v1/admin/roles/person")
def admin_person_roles(
    email: str = Query(...),
    auth: AuthContext = Depends(admin_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every override `email` holds, across the registered group workspaces,
    with the key each grant was signed under (`key`), which is what a key
    rotation is checked against (§4.13)."""
    subject = roles.clean_email(email)
    store = ctx.roles
    assert store is not None
    workspaces: list[dict[str, Any]] = []
    for tenant_id, group in roles.registered_group_tenants(ctx.settings).items():
        try:
            grant = store.newest_valid(tenant_id, subject, fresh=True)
        except RoleUnavailable:
            workspaces.append({"tenant_id": tenant_id, "group": group, "unavailable": True})
            continue
        if grant is not None and grant.role is not None:
            entry = roles.grant_api(subject, grant)
            entry.update(tenant_id=tenant_id, group=group, key=grant.key)
            workspaces.append(entry)
    return {"email": subject, "workspaces": workspaces}
