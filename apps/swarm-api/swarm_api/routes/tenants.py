"""Tenant self-service, including the write-only credential endpoint.

`POST /v1/tenants/me/credentials` is the only route in the service that accepts
key material. It writes the key into THAT tenant's own Secret Manager secret,
sets that secret's IAM policy to exactly that tenant's service account, and
returns metadata. There is no corresponding GET: `GET /v1/tenants/me` reports
which providers have a key registered, never the key.

WORKSPACE ROLES (docs/design/workspace-roles.md §4.3, lane ROLES-API-CORE).
`/me` and every `/mine` entry carry `role: {name, source}` -- the seam Settings
› Profile reads (§4.11) -- and each `/mine` entry its `via`: "personal",
"member", or "platform_admin" for a registered group workspace a platform
admin reaches without being in it (§4.2.2 step 5). A role that could not be
read is left out rather than guessed; the console then shows the derived one.
`POST /me/credentials` writes the workspace's provider key, which is its
admin's (Q6): behind `role_admin_required`'s switch, off at ship.

FEATURES (FEATURES-READ, part of #1748). `/me` carries `features:
{caller_mcp, await_input, service_answer}`, each `true`, `false` or
"unknown" (`schemas.TenantFeatures`), read through the same calls the
features themselves make. A state that could not be read is "unknown", never
`false`: a caller reading `false` withdraws the feature from its users.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, status

from swarm_common.profiles import RUNNER_PROFILES

from .. import awaitinput, mcp_servers, roles
from ..auth import AuthContext
from ..codec import tenant_to_api
from ..deps import AppContext, current_auth, get_context
from ..errors import ValidationFailed
from ..schemas import FEATURE_UNKNOWN, CredentialCreate, FeatureState, TenantFeatures
from ..validation import AWAIT_INPUT_PROFILE, known_providers

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


@router.get("/me")
def get_me(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = ctx.submissions.tenant_for(auth)
    return {
        "tenant": tenant_to_api(tenant),
        "principal": {
            "email": auth.email,
            "domain": auth.principal.domain,
            "groups": list(auth.principal.groups),
            "is_admin": auth.is_admin,
            # `is_admin: false` is an assertion about the caller. When the
            # directory did not answer it is not one, and an operator reading
            # this screen during an incident has to be able to tell.
            "is_admin_unresolved": auth.admin_unresolved,
        },
        # WHICH ENVIRONMENT THIS API RUNS AS, for the badge every screen draws
        # (apps/swarm-ui/src/Brand.tsx; ui-audit §B9.S5 specifies the plain
        # string). It is the value this process ACTS on -- `hardened` is derived
        # from it -- not a label kept for display.
        "environment": ctx.settings.core.environment,
        # False when nobody declared it and the frozen default "dev" filled
        # in. A client must treat that as unknown, not as dev: see
        # `ApiSettings.environment_declared`.
        "environment_declared": ctx.settings.environment_declared,
        "features": _features(ctx, auth),
        **_role_field(ctx, auth),
    }


def _features(ctx: AppContext, auth: AuthContext) -> dict[str, FeatureState]:
    """Whether three features are on for this workspace (FEATURES-READ, part
    of #1748), so a caller such as Product Oracle need not learn it by
    submitting. Each is read where the feature itself reads it -- CALLED, not
    restated -- and each separately, so one failed read leaves the other two.
    """
    return TenantFeatures(
        # The switch `require_enabled` and the attach route obey.
        caller_mcp=_read("caller_mcp", mcp_servers.mcp_servers_enabled),
        await_input=_read("await_input", _await_input_available),
        # The answer route's own predicate (`member_refusal`, `may_answer`),
        # asked of the workspace this request acts in.
        service_answer=_read(
            "service_answer",
            lambda: awaitinput.service_principal(ctx, auth, auth.tenant_id)),
    ).model_dump()


def _await_input_available() -> FeatureState:
    """The catalogue's `available` for `await-input`. A catalogue without the
    profile is not "off" -- this build does not say -- so it is unknown, where
    `awaitinput.dark()` folds it into dark for the expiry sweep's safety."""
    profile = RUNNER_PROFILES.get(AWAIT_INPUT_PROFILE)
    return FEATURE_UNKNOWN if profile is None else profile.available


def _read(name: str, read: Callable[[], Any]) -> FeatureState:
    """`read()` when it answers a boolean, else "unknown". A failure is
    never served as `false`, and never fails the identity route."""
    try:
        value = read()
    except Exception:  # noqa: BLE001 - an unread state is reported as unread
        log.warning("tenants/me could not read feature %s", name, exc_info=True)
        return FEATURE_UNKNOWN
    return value if isinstance(value, bool) else FEATURE_UNKNOWN


def _role_field(ctx: AppContext, auth: AuthContext) -> dict:
    """`{"role": {name, source}}` for the selected tenant, or nothing when the
    role could not be read: an absent field is the console's cue to show the
    derived role, where a guessed one would be a wrong answer."""
    resolved = ctx.roles.try_resolve(auth) if ctx.roles is not None else None
    return {} if resolved is None else {"role": resolved.to_api()}


@router.get("/mine")
def get_mine(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> list[dict]:
    """Every tenant the caller may select with `X-Swarm-Tenant`, default first.

    Exactly `AuthContext.tenant_choices`, computed by the same pass that
    resolved the default tenant -- not a second check that could disagree with
    it -- so the first entry IS the tenant a request without the header acts in.

    FIRST, when it is ready, the caller's OWN personal workspace (#1200, owner
    decision 2026-10-12: personal by default, groups are what you switch to):
    `kind: user`, named "Your workspace (u-<you>)". Its id is derived from the
    verified email and confirmed against its tenant document and workspace
    record (`auth.PersonalTenants`); another person's is never listed, and
    one that is not `ready` yet is not listed either.

    THEN the registered tenant groups Cloud Identity confirmed for this
    request, in admin order, `kind: group`. Nothing is read for them, so a
    group listed here need not have a tenant document yet; selecting it on a
    submit creates one exactly as a first submit by its default member does.
    Their `display_name` is the group email: it is what an admin registered,
    it is unique where the tenant id is not, and it needs no read.

    Empty for a caller in no registered group without a ready workspace.

    EXCEPT THAT A STORED DEFAULT GOES FIRST (docs/design/user-profile.md
    §4.5): the workspace the person chose on Settings › Profile, when it is
    one of these, is moved to the front by `Authenticator._with_default`, so
    the first entry is still the tenant a headerless request acts in. That
    entry carries `is_default: true` and `default_source` -- "profile",
    "personal", "first_group", or "unavailable" when the preferences could
    not be read (this route is in `auth.PREFERENCE_FREE_ROUTES`, so it is
    still served, in today's order) -- and, when a stored default was not
    applied, `default_ignored` ("not_a_member" or "not_ready"). Every other
    entry carries `is_default: false`. The entries and their order are
    otherwise exactly `tenant_choices`.

    AFTER THEM, for a platform admin with the reach on, every registered group
    workspace they are NOT a member of (`AuthContext.platform_reach`), each
    `via: "platform_admin"`, `is_default: false` and `role: admin` by rule --
    no read. Every other entry's role costs one cached grant read; a
    personal entry's costs none.
    """
    entries = [
        _choice(ctx, auth, tenant_id, principal, first=index == 0)
        for index, (tenant_id, principal) in enumerate(auth.tenant_choices)
    ]
    for tenant_id, group in auth.platform_reach:
        entries.append({
            "tenant_id": tenant_id, "display_name": group, "kind": "group",
            "is_default": False, "via": roles.VIA_PLATFORM_ADMIN,
            "role": {"name": roles.ADMIN, "source": roles.SOURCE_PLATFORM_ADMIN},
        })
    return entries


def _choice(ctx: AppContext, auth: AuthContext, tenant_id: str, principal: str, *,
            first: bool) -> dict:
    # The personal entry is the one whose principal is the caller themselves;
    # `_with_own_workspace` is the only place such an entry is made.
    if principal == auth.email:
        entry = {"tenant_id": tenant_id, "display_name": f"Your workspace ({tenant_id})",
                 "kind": "user", "via": roles.VIA_PERSONAL}
    else:
        entry = {"tenant_id": tenant_id, "display_name": principal, "kind": "group",
                 "via": roles.VIA_MEMBER}
    entry.update(_role_field(ctx, roles.with_tenant(auth, tenant_id, principal)))
    entry["is_default"] = first
    if first:
        entry["default_source"] = auth.default_source
        if auth.default_ignored:
            entry["default_ignored"] = auth.default_ignored
    return entry


@router.post("/me/credentials", status_code=status.HTTP_201_CREATED)
def put_credential(
    body: CredentialCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = body.provider.strip().lower()
    allowed = known_providers()
    if provider not in allowed:
        raise ValidationFailed(
            f"unknown provider {provider!r}",
            detail={"known_providers": list(allowed)},
        )
    # The workspace's provider key is its admin's (Q6). A NEW refusal on an
    # existing route, so through its switch: off, a member's write goes
    # through and `refusal report-only code=role_admin_required` names them.
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.ADMIN)
    tenant = ctx.submissions.tenant_for(auth)
    result = ctx.credentials.put_credential(tenant, provider, body.api_key)
    ctx.store.register_credential(tenant.tenant_id, provider)
    ctx.metrics.credentials_written.labels(
        tenant=tenant.tenant_id, provider=provider
    ).inc()
    # `result.to_api()` carries no payload and no payload length by construction.
    return {
        "credential": result.to_api(),
        "note": (
            "stored write-only; no read path in this API can return key material"
        ),
    }
