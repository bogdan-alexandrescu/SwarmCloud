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

THE RUNNER-KEY LINK (docs/design/secrets-scopes.md §4, §6.2, §6.3 and §11
Q8: a value arrives only through the console form a one-time link opens, or
a terminal's --stdin; lane SSC-LINKS). `POST /me/credentials/links`
`{provider}` mints a link to the console's runner-key form: 32 random bytes,
`runner_key_links/{sha256(token)}` (never the token), bound to the caller's
email, the workspace and the provider, ten minutes, one use. It takes no
value and answers `{url, path, provider, expires_at}`, `url` null when no
console origin is configured. The form's write, `POST /me/credentials`,
sends the token back as `link_token`; the link is consumed before the write
and grants nothing: every check a write without a link meets runs first. A
token that is not this caller's live link for this workspace and provider is
`KEY_LINK_INVALID` through that switch, as on the provider-key routes; off,
it is logged and the write goes on, on the caller's own session. A write
without a link (a terminal's --stdin) is unchanged.

FEATURES (FEATURES-READ, part of #1748). `/me` carries `features:
{caller_mcp, await_input, service_answer}`, each `true`, `false` or
"unknown" (`schemas.TenantFeatures`), read through the same calls the
features themselves make. A state that could not be read is "unknown", never
`false`: a caller reading `false` withdraws the feature from its users.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends, status
from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot

from swarm_common.profiles import RUNNER_PROFILES

from .. import awaitinput, mcp_servers, refusals, roles
from ..auth import AuthContext
from ..codec import console_origin, tenant_to_api
from ..deps import AppContext, current_auth, get_context
from ..errors import KeyLinkInvalid, ValidationFailed
from ..schemas import FEATURE_UNKNOWN, CredentialCreate, FeatureState, StrictModel, TenantFeatures
from ..validation import AWAIT_INPUT_PROFILE, known_providers

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])

#: One document per minted runner-key link, keyed by sha256(token). Never the token.
RUNNER_KEY_LINKS = "runner_key_links"
#: Ten minutes, one use (secrets-scopes.md §6.3): long enough to switch from
#: the terminal to the browser and paste, short enough that a link found in
#: a scrollback or a chat log later is dead. The provider-key link's TTL.
RUNNER_KEY_LINK_TTL = timedelta(minutes=10)
#: The console's runner-key form, under Config › Secrets (§6.1): the
#: workspace scope, the provider, and the link's token.
RUNNER_KEY_FORM = "/config/secrets?scope=workspace&runner_key={provider}&t={token}"
RUNNER_KEY_LINK_INVALID_MESSAGE = (
    "this runner-key link is unknown, already used, expired, or was minted for someone "
    "else, another workspace or another provider; mint a new one"
)


class RunnerKeyLinkCreate(StrictModel):
    #: A `known_providers()` name, as the write takes. No value: a link only.
    provider: str = Field(min_length=1, max_length=64)


class RunnerKeyWrite(CredentialCreate):
    #: The `t` of the one-time link (`POST /me/credentials/links`), when the
    #: console's form was opened from one. Unset is a terminal's --stdin write.
    link_token: str | None = Field(default=None, min_length=1, max_length=256)


def _link_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _provider(raw: str) -> str:
    provider = raw.strip().lower()
    allowed = known_providers()
    if provider not in allowed:
        raise ValidationFailed(
            f"unknown provider {provider!r}",
            detail={"known_providers": list(allowed)},
        )
    return provider


def consume_runner_key_link(db: Any, token: str, *, email: str, tenant_id: str, provider: str,
                            now: datetime) -> bool:
    """Mark the link used if it is this caller's, for this workspace and
    provider, unused and unexpired; False otherwise, and then nothing is
    written: someone else presenting a leaked link does not burn it."""
    ref = db.collection(RUNNER_KEY_LINKS).document(_link_digest(token))
    transaction = db.transaction()

    @firestore.transactional
    def _consume(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        link = (snap.to_dict() or {}) if snap.exists else None
        if not link or link.get("used_at") is not None:
            return False
        expires_at = link.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= now:
            return False
        if (link.get("email"), link.get("tenant_id"), link.get("provider")) != \
                (email, tenant_id, provider):
            return False
        txn.update(ref, {"used_at": now})
        return True

    return _consume(transaction)


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


@router.post("/me/credentials/links", status_code=status.HTTP_201_CREATED)
def mint_credential_link(
    body: RunnerKeyLinkCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """A one-time link to the console's runner-key form (SSC-LINKS), for
    whoever the write itself admits: the same provider check and the same
    `role_admin_required` gate run first. The link's URL, path, provider and
    expiry, and nothing else: never the link document."""
    provider = _provider(body.provider)
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.ADMIN)
    tenant = ctx.submissions.tenant_for(auth)
    token = secrets.token_urlsafe(32)
    now = ctx.now()
    expires_at = now + RUNNER_KEY_LINK_TTL
    ctx.db.collection(RUNNER_KEY_LINKS).document(_link_digest(token)).set({
        "email": auth.email, "tenant_id": tenant.tenant_id, "provider": provider,
        "created_at": now, "expires_at": expires_at, "used_at": None,
    })
    path = RUNNER_KEY_FORM.format(provider=provider, token=token)
    base = console_origin(ctx.settings.console_url)
    log.info("minted a runner-key link provider=%s tenant=%s", provider, tenant.tenant_id)
    return {"url": f"{base}{path}" if base else None, "path": path,
            "provider": provider, "expires_at": expires_at.isoformat()}


@router.post("/me/credentials", status_code=status.HTTP_201_CREATED)
def put_credential(
    body: RunnerKeyWrite,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    provider = _provider(body.provider)
    # The workspace's provider key is its admin's (Q6). A NEW refusal on an
    # existing route, so through its switch: off, a member's write goes
    # through and `refusal report-only code=role_admin_required` names them.
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.ADMIN)
    tenant = ctx.submissions.tenant_for(auth)
    # The link is consumed before the write and grants nothing: every check
    # above ran whatever it says.
    via_link = False
    if body.link_token is not None:
        via_link = consume_runner_key_link(ctx.db, body.link_token, email=auth.email,
                                           tenant_id=tenant.tenant_id, provider=provider,
                                           now=ctx.now())
        if not via_link:
            refusals.refuse(KeyLinkInvalid(RUNNER_KEY_LINK_INVALID_MESSAGE))
    result = ctx.credentials.put_credential(tenant, provider, body.api_key)
    ctx.store.register_credential(tenant.tenant_id, provider)
    ctx.metrics.credentials_written.labels(
        tenant=tenant.tenant_id, provider=provider
    ).inc()
    # `result.to_api()` carries no payload and no payload length by construction.
    return {
        "credential": result.to_api(),
        "via_link": via_link,
        "note": (
            "stored write-only; no read path in this API can return key material"
        ),
    }
