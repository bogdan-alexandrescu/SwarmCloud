"""The account pool, tenant-scoped, proxied to the quota broker.

WHY EVERY ROUTE HERE IS A PROXY
-------------------------------
An account is a Claude subscription the platform runs agents on, and its
credential is a rotating OAuth pair: refreshing it REVOKES the token it
replaces. Two components that both write one do not race, they brick it. The
quota broker is the platform's single writer, so nothing in this file touches
Secret Manager; `brokerclient.py` speaks HTTP to that service and this file adds
the one thing the broker cannot know -- which human is calling.

WHERE THE TENANT COMES FROM
---------------------------
`ctx.submissions.tenant_for(auth)`, always -- the same call every other
tenant-scoped route in this service makes, and never the raw `auth.tenant_id`.
The difference is two guards that only exist on that path:

  * THE COLLISION CHECK. The frozen `tenant_id_for_group` slugs the LOCAL PART
    only, so `eng@saga.xyz` and `eng@partner.com` are two different Google
    groups that both derive tenant `eng`. `Store.ensure_tenant` compares the
    calling group against the one the tenant document was created for and
    answers 409 when they differ, because serving it would hand one group
    another group's credentials. On this surface that is not an abstract
    isolation breach: the loser's member could list, re-lend, delete and
    REFRESH the winner's account, and refreshing revokes the token every pod
    holding it is using.
  * THE DISABLED-TENANT REFUSAL. An admin who disables a tenant
    (`PUT /v1/admin/tenants/{id}` sets `enabled=false`) stops every other route
    in the API; an account route reading `auth.tenant_id` directly would keep
    serving registrations and refreshes for it.

Never the body and never a query parameter. What a caller sends as
`owner_tenant` is read for exactly one purpose -- to be compared with the
resolved tenant and refused when it differs -- and never used as the value
anything is filed under, so no ordering of checks exists in which a body field
could put a live credential into somebody else's pool.

ONE KIND OF CREDENTIAL
----------------------
The pool holds CLAUDE SUBSCRIPTIONS and nothing else. There is no API-key
branch here and no credential-kind selector: `provider` is accepted only so a
caller that names something else is told why, and the single accepted value is
`anthropic`. The unrelated `POST /v1/tenants/me/credentials`, which writes a
tenant's own provider API key into that tenant's secret, is a different surface
and is untouched by this file.

WHY OWNERSHIP IS RE-CHECKED HERE RATHER THAN LEFT TO THE BROKER
---------------------------------------------------------------
swarm-api authenticates to the broker as a PLATFORM caller, because it speaks
for every tenant rather than for one worker's service account. The broker's own
`_authorize` therefore says yes to whatever tenant this service names, which
makes the human-facing boundary this file's job and nobody else's.

The subtle half is LENDING. `GET /v1/accounts?tenant_id=X` returns the accounts
X may RUN on -- the ones it owns and the ones lent to it -- which is exactly
right for the list. It is exactly wrong as an authorisation set for a write: a
borrower must not be able to pause, re-lend, refresh or delete the lender's
account. So every mutating route resolves the account and requires
`owner_tenant == caller's tenant`.

An account the caller does not own answers 404, the same answer another
tenant's task id gets on `/v1/tasks/{id}`. Distinguishing "not yours" from "does
not exist" would confirm that somebody else's label exists.

NO KEY MATERIAL LEAVES THIS FILE
--------------------------------
`credential` is write-only: it appears in one request body, is forwarded once,
and appears in no response, no log line and no error message. The broker's
`account_to_api` shape carries no key material either -- not even a length --
and the refresh outcome carries only tenant, provider, a boolean, a reason and
an expiry.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from ..auth import AuthContext
from ..brokerclient import AccountPool, BrokerClient
from ..deps import AppContext, current_auth, get_context
from ..errors import Forbidden, NotFound, ValidationFailed
from ..schemas import (
    SUBSCRIPTION_PROVIDER,
    AccountCreate,
    AccountLending,
    AccountSignInFinish,
    AccountSignInStart,
    AccountStateChange,
)

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


def account_pool(
    request: Request,
    ctx: AppContext = Depends(get_context),
) -> AccountPool:
    """The broker client, built once per process and injectable in tests.

    It hangs off `app.state` rather than off `AppContext` so that a test (or a
    future admin surface) can substitute a pool without rebuilding the whole
    application context -- and, more importantly, so that the unit tests need no
    metadata server, no credentials and no network. Constructing the real client
    opens no connection and reads no credential, so doing it lazily here costs
    nothing and keeps `create_app()` free of outbound dependencies.
    """
    pool = getattr(request.app.state, "account_pool", None)
    if pool is None:
        pool = BrokerClient(
            base_url=ctx.settings.quota_broker_url,
            audience=ctx.settings.quota_broker_audience,
            # "Not configured" means refuse, outside local development. An
            # unset audience would otherwise fall back to the service URL,
            # and terraform pins a CUSTOM audience on the broker -- so the
            # token would be minted for a value the broker is guaranteed to
            # reject, and the operator would see an identity 403 instead of
            # the missing variable. Same gate as
            # `GoogleTokenVerifier(require_audience=settings.hardened)`.
            require_audience=ctx.settings.hardened,
        )
        request.app.state.account_pool = pool
    return pool


def _tenant_id(ctx: AppContext, auth: AuthContext) -> str:
    """The caller's tenant, through the guards, never the raw token field.

    `tenant_for` is what refuses a tenant-id collision (409) and a disabled
    tenant (403). Reading `auth.tenant_id` here instead would make the account
    pool the one surface in this service where both are unenforced -- on the
    one surface that administers live subscription credentials.

    It is called BEFORE any broker call on every route, including the register
    route, so a refusal happens before a pasted credential is forwarded
    anywhere.
    """
    return ctx.submissions.tenant_for(auth).tenant_id


def _owned(pool: AccountPool, tenant_id: str, account_id: str) -> dict[str, Any]:
    """The caller's OWN account with this id, or 404.

    Ownership, not visibility: an account lent to this tenant is in the list and
    is usable, and is still someone else's to pause, re-lend or delete.
    """
    payload = pool.list_accounts(tenant_id)
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        if account.get("account_id") == account_id and account.get("owner_tenant") == tenant_id:
            return account
    raise NotFound(f"no account {account_id!r} in this tenant's pool")


@router.get("")
def list_accounts(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Every account this tenant may run on: the ones it owns and the ones lent.

    `tenant_id` is echoed from the RESOLVED tenant rather than from the broker's
    answer, so the page can never be told it is looking at a tenant it is not.
    """
    tenant_id = _tenant_id(ctx, auth)
    payload = pool.list_accounts(tenant_id)
    return {
        "accounts": list(payload.get("accounts") or []),
        "tenant_id": tenant_id,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def register_account(
    body: AccountCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Register a subscription into the caller's own pool.

    The credential is taken as pasted and forwarded once. The broker parses it
    BEFORE writing anything, so a credential with no refresh token is refused at
    paste time -- with a 422 that says so -- rather than becoming a pool entry
    that looks healthy and dies silently at its first expiry.
    """
    tenant_id = _tenant_id(ctx, auth)
    if body.owner_tenant is not None:
        # Compared, never used. A caller that names its own tenant is simply
        # agreeing with the token and is let through; one that names another is
        # told the rule, because a silent override would leave an operator
        # believing they had registered an account somewhere they had not.
        if body.owner_tenant.strip().lower() != tenant_id.lower():
            raise Forbidden(
                "an account is registered into the caller's own tenant "
                f"({tenant_id!r}); owner_tenant names a different one. The "
                "owner comes from the verified token, so the field can be omitted"
            )
    provider = body.provider.strip().lower()
    if provider != SUBSCRIPTION_PROVIDER:
        # THE POOL HOLDS SUBSCRIPTIONS, so this is not a choice -- it is the
        # one accepted value, checked so a caller naming anything else is told
        # why rather than having a live credential filed under a provider the
        # broker cannot refresh. Every account here is an OAuth pair the broker
        # rotates; there is no API-key path in the account pool at all.
        raise ValidationFailed(
            "the account pool holds Claude subscriptions only, so provider "
            f"must be {SUBSCRIPTION_PROVIDER!r}, not {provider!r}",
            detail={"known_providers": [SUBSCRIPTION_PROVIDER]},
        )
    result = pool.register(
        owner_tenant=tenant_id,
        label=body.label,
        provider=provider,
        lend_to=list(body.lend_to),
        credential=body.credential,
    )
    return {
        "account": result.get("account"),
        "expires_at": result.get("expires_at"),
        "note": (
            "stored write-only; no read path in this API can return key material"
        ),
    }


@router.post("/authorize")
def begin_sign_in(
    body: AccountSignInStart,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Start a browser sign-in and hand back the URL to open.

    THE SEAM THAT WAS MISSING. The broker grew these routes and the UI grew the
    calls, and for one deploy there was nothing in between: the browser asked
    swarm-api for a route swarm-api did not have. That is the third time in one
    day that both ends of a path were built and the middle was not, which is
    why this docstring says so rather than describing what a proxy is.

    The tenant comes from the verified token, never from the body -- the same
    rule every other route here follows, and the reason the body has no
    owner_tenant field at all.
    """
    return pool.begin_sign_in(
        owner_tenant=_tenant_id(ctx, auth),
        label=body.label,
        provider=SUBSCRIPTION_PROVIDER,
        lend_to=list(body.lend_to),
    )


@router.post("/exchange", status_code=status.HTTP_201_CREATED)
def finish_sign_in(
    body: AccountSignInFinish,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Redeem the code and register the account.

    The caller's tenant is resolved here and checked by the broker against the
    pending sign-in it issued, so a code cannot be completed into a tenant other
    than the one that started it.
    """
    _tenant_id(ctx, auth)
    result = pool.finish_sign_in(state=body.state, code=body.code)
    return {
        "account": result.get("account"),
        "expires_at": result.get("expires_at"),
        "note": "stored write-only; no read path in this API can return key material",
    }


@router.post("/{account_id}/refresh")
def refresh_account(
    account_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Exchange this account's credential now, and report what happened.

    The broker's sweep already does this on a timer. This exists so that an
    operator who has just pasted a credential can answer "did that work?"
    without waiting an unknown number of minutes -- and it goes through the
    broker for the same reason everything else here does.
    """
    _owned(pool, _tenant_id(ctx, auth), account_id)
    return pool.refresh(account_id)


@router.put("/{account_id}/lending")
def set_lending(
    account_id: str,
    body: AccountLending,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Replace the list of tenants this account lends to. Owner only."""
    _owned(pool, _tenant_id(ctx, auth), account_id)
    return pool.set_lending(account_id, lend_to=list(body.lend_to))


@router.put("/{account_id}/state")
def set_account_state(
    account_id: str,
    body: AccountStateChange,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """AVAILABLE, PAUSED, DRAINING or REAUTH_REQUIRED. Owner only.

    The name is not validated here: the broker owns the state machine and
    answers an unknown value with a 422 listing what it accepts. A second copy
    of that list in this service would be one more thing to drift.
    """
    _owned(pool, _tenant_id(ctx, auth), account_id)
    return pool.set_state(account_id, state=body.state, reason=body.reason)


@router.delete("/{account_id}")
def remove_account(
    account_id: str,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Remove the account from the pool. Owner only.

    The DOCUMENT goes and the secret stays, which is the broker's decision and
    is reported back verbatim: deleting a Secret Manager secret is irreversible
    and takes its version history with it, so an account removed by mistake
    stays re-registerable.
    """
    _owned(pool, _tenant_id(ctx, auth), account_id)
    return pool.remove(account_id)
