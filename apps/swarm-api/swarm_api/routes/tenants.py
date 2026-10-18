"""Tenant self-service, including the write-only credential endpoint.

`POST /v1/tenants/me/credentials` is the only route in the service that accepts
key material. It writes the key into THAT tenant's own Secret Manager secret,
sets that secret's IAM policy to exactly that tenant's service account, and
returns metadata. There is no corresponding GET: `GET /v1/tenants/me` reports
which providers have a key registered, never the key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ..auth import AuthContext
from ..codec import tenant_to_api
from ..deps import AppContext, current_auth, get_context
from ..errors import ValidationFailed
from ..schemas import CredentialCreate
from ..validation import known_providers

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
    }


@router.get("/mine")
def get_mine(auth: AuthContext = Depends(current_auth)) -> list[dict]:
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
    """
    return [
        _choice(auth, tenant_id, principal, first=index == 0)
        for index, (tenant_id, principal) in enumerate(auth.tenant_choices)
    ]


def _choice(auth: AuthContext, tenant_id: str, principal: str, *, first: bool) -> dict:
    # The personal entry is the one whose principal is the caller themselves;
    # `_with_own_workspace` is the only place such an entry is made.
    if principal == auth.email:
        entry = {"tenant_id": tenant_id, "display_name": f"Your workspace ({tenant_id})",
                 "kind": "user"}
    else:
        entry = {"tenant_id": tenant_id, "display_name": principal, "kind": "group"}
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
