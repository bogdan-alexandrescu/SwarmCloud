"""The account pool, tenant-scoped, proxied to the quota broker.

WHY EVERY ROUTE HERE IS A PROXY
-------------------------------
An account is a Claude subscription or a ChatGPT plan the platform runs agents
on, and its credential is a rotating OAuth pair: refreshing it REVOKES the
token it replaces. Two components that both write one do not race, they brick it. The
quota broker is the platform's single writer, so nothing in this file touches
Secret Manager; `brokerclient.py` speaks HTTP to that service and this file adds
the one thing the broker cannot know -- which human is calling.

WHERE THE TENANT COMES FROM
---------------------------
`ctx.submissions.tenant_for(auth)`, always -- the same call every other
tenant-scoped route in this service makes, and never the raw `auth.tenant_id`.
The difference is two guards that only exist on that path:

  * THE COLLISION CHECK. The frozen `tenant_id_for_group` slugs the LOCAL PART
    only, so `eng@example.com` and `eng@partner.com` are two different Google
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

ONE KIND OF CREDENTIAL, TWO PROVIDERS
-------------------------------------
The pool holds ROTATING OAUTH PAIRS and nothing else: Claude subscriptions
(`anthropic`) and, since the owner's choice of 2026-11-01 (#2688), ChatGPT
plans (`openai`, which codex runs on). There is no API-key branch here and no
credential-kind selector: `provider` names which of those two pairs an
account is, a sign-in that names neither is refused by its request model, and
a registration that names neither is refused by name with the known providers
in the refusal's detail. A sign-in that sends no provider is a Claude sign-in,
exactly as before. This file forwards the provider unchanged and never reads
or holds the pair itself. The unrelated `POST /v1/tenants/me/credentials`,
which writes a tenant's own provider API key into that tenant's secret, is a
different surface and is untouched by this file.

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

import base64
import binascii
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import Field, StrictInt
from swarm_redaction import redact as redact_text

from .. import roles
from ..accountholds import (
    EVERY_ACCOUNT,
    OWN_CURSOR_MARK,
    TaskCheck,
    Viewer,
    history_view,
    holders_view,
    own_cursor_position,
    own_page,
    viewer_of,
)
from ..auth import AuthContext, require_admin
from ..brokerclient import AccountPool, BrokerClient
from ..deps import AppContext, current_auth, get_context
from ..errors import Forbidden, NotFound, ValidationFailed
from ..schemas import (
    ACCOUNT_HEALTH_KINDS,
    ACCOUNT_HEALTH_SPANS,
    ACCOUNT_HEALTH_STATUSES,
    ACCOUNT_HEALTH_WINDOWS,
    ACCOUNT_HOLD_DEADLINE,
    ACCOUNT_PROVIDERS,
    AccountCreate,
    AccountLending,
    AccountSignInFinish,
    AccountSignInStart,
    AccountStateChange,
    StrictModel,
)

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])

#: The (method, template) `require_admin` is handed for `scope=platform` on the
#: two holder reads. Not in POOL_ADMIN_ROUTES, so a pool admin asking for the
#: platform view is refused like any non-admin -- the `outcomes.py` pattern.
HOLDERS_PLATFORM_ROUTE = ("GET", "/v1/accounts/{account_id}/holders")
HISTORY_PLATFORM_ROUTE = ("GET", "/v1/accounts/{account_id}/history")
HEALTH_PLATFORM_ROUTE = ("GET", "/v1/accounts/{account_id}/health")
HEALTH_POOL_PLATFORM_ROUTE = ("GET", "/v1/accounts/health")


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


def _writer_tenant_id(ctx: AppContext, auth: AuthContext) -> str:
    """`_tenant_id`, and then the role gate every write here passes
    (docs/design/workspace-roles.md §4.1, §4.8; lane ROLES-GATES-AUTOMATE):
    the role `member` in the selected workspace. A `viewer` is
    `role_member_required`, through its switch -- BEFORE any broker call, so a
    refused viewer's pasted credential is forwarded nowhere and no account is
    registered, re-lent, paused, refreshed or removed. The tenant guards run
    first, so a collision or a disabled tenant keeps its own answer."""
    tenant_id = _tenant_id(ctx, auth)
    if ctx.roles is not None:
        ctx.roles.require(auth, roles.MEMBER)
    return tenant_id


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


#: What a caller is told of the other side of a duplicate pair that another
#: tenant owns (#2222; BGD, 2026-10-24 17:26Z, "Name it only within the same
#: tenant"). The broker's own wording, so the two never read differently.
ANOTHER_WORKSPACE = "this subscription is already registered in another workspace"
_ANOTHER_WORKSPACE_READINGS = (
    "identical readings on two polls with an account in another workspace"
)


def _foreign(other: Any, tenant_id: str) -> bool:
    """Whether an account id names another tenant's account: an id is
    `<owner_tenant>:<label>`, so anything not under `tenant_id:` is."""
    return isinstance(other, str) and not other.startswith(f"{tenant_id}:")


def _own_view(account: Any, tenant_id: str) -> Any:
    """One broker account, with every name of ANOTHER tenant's account taken
    out of its duplicate marks (#2222).

    The broker already answers in the caller's view -- this service names the
    tenant on every call -- so on a current broker this changes nothing. It
    is the second line for the one field family that names an account other
    than the one shown: a broker older or wrong about the rule must still not
    hand one tenant another tenant's labels. Only the duplicate fields are
    touched; whatever else the broker sends passes as served.
    """
    if not isinstance(account, dict):
        return account
    out = dict(account)
    if _foreign(out.get("duplicate_of"), tenant_id):
        out["duplicate_of"] = None
        out["duplicate_reason"] = ANOTHER_WORKSPACE
    possible = out.get("possible_duplicate")
    if isinstance(possible, dict) and _foreign(possible.get("of"), tenant_id):
        out["possible_duplicate"] = {"of": None, "reason": _ANOTHER_WORKSPACE_READINGS}
    blocked = out.get("blocked_by")
    if isinstance(blocked, list):
        entries: list[Any] = []
        for entry in blocked:
            if isinstance(entry, dict) and _foreign(entry.get("duplicate_of"), tenant_id):
                entry = {"duplicate_of": None, "reason": ANOTHER_WORKSPACE}
            elif isinstance(entry, dict) and _foreign(entry.get("possible_duplicate"), tenant_id):
                entry = {"possible_duplicate": None, "advisory": True,
                         "reason": _ANOTHER_WORKSPACE_READINGS}
            entries.append(entry)
        out["blocked_by"] = entries
    return out


def _own_answer(result: Any, tenant_id: str) -> Any:
    """A broker answer carrying one `account`, through `_own_view`."""
    if isinstance(result, dict) and "account" in result:
        return {**result, "account": _own_view(result.get("account"), tenant_id)}
    return result


@router.get("")
def list_accounts(
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Every account this tenant may run on: the ones it owns and the ones lent.

    `tenant_id` is echoed from the RESOLVED tenant rather than from the broker's
    answer, so the page can never be told it is looking at a tenant it is not.

    THIS FUNCTION REBUILDS THE PAYLOAD, so every field the browser needs has to
    be named here. The broker reports which account documents it could not read
    and how many there were in total -- the count that stops a short list being
    read as the whole pool -- and a rebuild that lists only `accounts` drops
    both on the floor, which puts the page back to showing four accounts where
    there are five with nothing to say so.
    """
    tenant_id = _tenant_id(ctx, auth)
    payload = pool.list_accounts(tenant_id)
    return {
        # Each through `_own_view` (#2222): another tenant's account is never
        # named by a duplicate mark, whatever the broker sent.
        "accounts": [
            _with_hold_deadline(_with_health_row(_own_view(a, tenant_id)))
            for a in payload.get("accounts") or []
        ],
        "tenant_id": tenant_id,
        # Forwarded as served. The broker has already narrowed the ids to what
        # this tenant may see -- it is the only component that knows both the
        # document ids and who asked -- so re-deriving the rule here would be a
        # second copy of an isolation boundary.
        "unreadable_documents": list(payload.get("unreadable_documents") or []),
        # Defaulted to the number of ids rather than to 0: an older broker that
        # does not serve the count still serves the ids, and a 0 beside a
        # non-empty list would be the one shape that reads as "nothing wrong".
        "unreadable_document_count": int(
            payload.get("unreadable_document_count")
            if isinstance(payload.get("unreadable_document_count"), int)
            else len(payload.get("unreadable_documents") or [])
        ),
        #: Whether the broker's refresh sweep is still running (U27): when it
        #: last completed and how. By allow-list, like everything here. An
        #: older broker that does not serve it reads as `readable: false` --
        #: unknown -- never as a sweep that is fine.
        "refresh_sweep": _refresh_sweep(payload.get("refresh_sweep")),
    }


def _health_counts(raw: Any) -> dict[str, int] | None:
    """Counts by kind, by the allow-list; None when the broker sent none."""
    if not isinstance(raw, dict):
        return None
    return {
        kind: int(raw[kind])
        if isinstance(raw.get(kind), int) and not isinstance(raw.get(kind), bool)
        else 0
        for kind in ACCOUNT_HEALTH_KINDS
    }


def _health_word(raw: Any) -> str | None:
    return raw if raw in ACCOUNT_HEALTH_STATUSES else None


def _with_health_row(account: Any) -> Any:
    """A listing row's two health fields (#2302) by the allow-list: the last
    24 h's counts and the status. None for each when the broker sent nothing
    usable -- an older broker -- never a made-up `healthy`."""
    if not isinstance(account, dict):
        return account
    return {
        **account,
        "health_24h": _health_counts(account.get("health_24h")),
        "health_status": _health_word(account.get("health_status")),
    }


def _with_hold_deadline(account: Any) -> Any:
    """A listing row's latest hold deadline (#2222, the Timeline's B3/B4) by
    the allow-list: an ISO instant WITH ITS ZONE passes as served, anything
    else is None, so the page never has to guess what a bare clock or a
    number meant. A row with no key -- a broker older than the field -- keeps
    none, which is how the page tells "not served" from "no live hold"."""
    if not isinstance(account, dict) or ACCOUNT_HOLD_DEADLINE not in account:
        return account
    raw = account.get(ACCOUNT_HOLD_DEADLINE)
    at = None
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            at = raw
    return {**account, ACCOUNT_HOLD_DEADLINE: at}


def _refresh_sweep(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"last_completed_at": None, "last_outcome": None, "readable": False}
    completed = raw.get("last_completed_at")
    outcome = raw.get("last_outcome")
    return {
        "last_completed_at": completed if isinstance(completed, str) and completed else None,
        "last_outcome": outcome if outcome in ("ok", "error") else None,
        "readable": raw.get("readable") is True,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def register_account(
    body: AccountCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Register a subscription or a ChatGPT plan into the caller's own pool.

    The credential is taken as pasted and forwarded once. The broker parses it
    BEFORE writing anything, so a credential with no refresh token is refused at
    paste time -- with a 422 that says so -- rather than becoming a pool entry
    that looks healthy and dies silently at its first expiry.
    """
    tenant_id = _writer_tenant_id(ctx, auth)
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
    if provider not in ACCOUNT_PROVIDERS:
        # THE POOL HOLDS ROTATING PAIRS, so this is not a free choice: a
        # caller naming anything else is told why rather than having a live
        # credential filed under a provider the broker cannot refresh. There is
        # no API-key path in the account pool at all.
        raise ValidationFailed(
            "the account pool holds Claude subscriptions and ChatGPT plans, so "
            f"provider must be one of {', '.join(map(repr, ACCOUNT_PROVIDERS))}, "
            f"not {provider!r}",
            detail={"known_providers": list(ACCOUNT_PROVIDERS)},
        )
    result = pool.register(
        owner_tenant=tenant_id,
        label=body.label,
        provider=provider,
        lend_to=list(body.lend_to),
        credential=body.credential,
    )
    return {
        "account": _own_view(result.get("account"), tenant_id),
        **_registered(result),
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

    The provider is the body's, forwarded unchanged (`anthropic` when absent,
    so a Claude sign-in is exactly what it was). The broker's answer names the
    provider and the redirect it chose, which is how `sc account add
    --provider openai` knows to listen on its own machine for the callback.
    """
    return pool.begin_sign_in(
        owner_tenant=_writer_tenant_id(ctx, auth),
        label=body.label,
        provider=body.provider,
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

    WHERE THE ACCOUNT LANDS is decided by the PENDING SIGN-IN, never by this
    request: the broker reads `owner_tenant` off the record it keyed by `state`
    when `/authorize` issued it. So a redeemed code cannot be filed into a
    tenant other than the one that started the sign-in, whatever the redeemer
    sends.

    WHO MAY FINISH IT is checked too: only the tenant that started it. This
    route sends the CALLER's tenant -- resolved by `_tenant_id`, never read from
    the body, which has no such field -- as `expected_owner`, and the broker
    compares it with the pending record's owner BEFORE it redeems the code. A
    mismatch is a 403 that redeems nothing, files nothing and leaves the
    pending sign-in usable by its real owner, so holding another tenant's
    `state` neither completes their sign-in nor burns it. The broker refuses a
    platform call with no `expected_owner` at all, so this check cannot be
    skipped by a caller that does not send it.

    `_tenant_id` is called FIRST, before any broker call: it is also what
    refuses a tenant id collision (409) and a disabled tenant (403) before any
    credential is redeemed.
    """
    tenant_id = _writer_tenant_id(ctx, auth)
    result = pool.finish_sign_in(
        state=body.state, code=body.code, expected_owner=tenant_id
    )
    return {
        "account": _own_view(result.get("account"), tenant_id),
        **_registered(result),
        "expires_at": result.get("expires_at"),
        "note": "stored write-only; no read path in this API can return key material",
    }


def _registered(result: dict) -> dict[str, Any]:
    """WHICH ORGANISATION THE ADD REGISTERED (#2222), by allow-list.

    A sign-in authorises whichever organisation is selected on claude.ai, so
    an answer that says only "registered eng:saga-team" hid that it was the
    Personal subscription a second time. The broker's sentence and the
    organisation's name and plan -- never its identity -- pass through; an
    older broker that sends neither reads as not reported.
    """
    line = result.get("registered")
    org = result.get("organisation")
    return {
        "registered": line if isinstance(line, str) and line else None,
        "organisation": (
            {
                "name": org.get("name") if isinstance(org.get("name"), str) else None,
                "plan": org.get("plan") if isinstance(org.get("plan"), str) else None,
            }
            if isinstance(org, dict)
            else None
        ),
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
    tenant_id = _writer_tenant_id(ctx, auth)
    _owned(pool, tenant_id, account_id)
    return _own_answer(pool.refresh(account_id), tenant_id)


@router.put("/{account_id}/lending")
def set_lending(
    account_id: str,
    body: AccountLending,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Replace the list of tenants this account lends to. Owner only."""
    tenant_id = _writer_tenant_id(ctx, auth)
    _owned(pool, tenant_id, account_id)
    return _own_answer(pool.set_lending(account_id, lend_to=list(body.lend_to)), tenant_id)


class AccountModelClasses(StrictModel):
    """The model classes an account serves (owner decision R5). Empty: every class.

    Declared here rather than in `schemas.py`, beside the one route that takes
    it. The names are not checked here: the broker owns the account record and
    answers an unknown class with a 422 naming the vocabulary, as it does an
    unknown state.
    """

    model_classes: list[str] = Field(default_factory=list, max_length=8)


@router.put("/{account_id}/model-classes")
def set_model_classes(
    account_id: str,
    body: AccountModelClasses,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Which model classes this account's plan serves. Owner only.

    The broker then assigns a step only to an account that serves its class,
    and a step whose class no account serves waits rather than running on a
    plan that cannot run it.
    """
    tenant_id = _writer_tenant_id(ctx, auth)
    _owned(pool, tenant_id, account_id)
    return _own_answer(pool.set_model_classes(account_id, model_classes=list(body.model_classes)), tenant_id)


#: The most agents one account may be set to hold at once (#2222). The
#: broker's `quota_broker.accounts.MAX_AGENTS_LIMIT`, restated because this
#: service does not import the broker; `test_account_max_agents_api.py` holds
#: the two equal. Checked HERE as well as there so a bad number is refused
#: before any broker call, in the broker's own words.
MAX_AGENTS_LIMIT = 50


class AccountMaxAgents(StrictModel):
    """How many agents an account may hold at once (#2222), 1 to MAX_AGENTS_LIMIT.

    Declared here, beside the one route that takes it, like
    `AccountModelClasses`. Required, so an empty body is not read as the
    default. `StrictInt`, so `true`, `"6"` and `6.5` are refused as not a
    number rather than coerced into one; the range is `_checked_max_agents`.
    """

    max_agents: StrictInt


def _checked_max_agents(value: int) -> int:
    """`value`, or a 422 in the words `quota_broker.accounts.validate_max_agents`
    uses. Zero is refused rather than read as "take no agents": pausing the
    account says that, and one switch for it is enough."""
    if not 1 <= value <= MAX_AGENTS_LIMIT:
        raise ValidationFailed(
            f"an account's max_agents is 1 to {MAX_AGENTS_LIMIT}, and this is {value}; "
            "pause the account to stop it taking new agents"
        )
    return value


@router.put("/{account_id}/max-agents")
def set_max_agents(
    account_id: str,
    body: AccountMaxAgents,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """How many agents this account may hold at once. Owner only, exactly as
    lending, the served classes and the Name are (#2222).

    The caller rule is the broker's: the account's owner tenant, a `member`
    there. A platform admin acting in that workspace passes the same way and
    is recorded by `deps.current_auth`'s platform-admin audit before this
    body runs, as on every other account write. The number is checked before
    the broker is asked anything, so a bad one reads no account and writes
    none. Lowering it moves no agent already on the account; the broker
    applies it at the next assignment.
    """
    tenant_id = _writer_tenant_id(ctx, auth)
    max_agents = _checked_max_agents(body.max_agents)
    _owned(pool, tenant_id, account_id)
    return _own_answer(pool.set_max_agents(account_id, max_agents=max_agents), tenant_id)


class AccountName(StrictModel):
    """An account's Name (#1913), or null to clear it so the label shows.

    Declared here, beside the one route that takes it, like
    `AccountModelClasses`. Required, so an empty body is not read as "clear".
    The bound only stops an unbounded body: what a Name may be is the broker's
    `validate_display_name`, and its 422 reaches the caller in its own words.
    """

    display_name: str | None = Field(max_length=1024)


@router.put("/{account_id}/name")
def set_account_name(
    account_id: str,
    body: AccountName,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Set or clear this account's Name. Owner only, a `member` who may manage
    that owner's accounts, exactly as lending and the served classes are.

    The Name is display text: the id, the label, the credential secret, the
    holds and the lending do not change, and work on the account goes on.
    """
    tenant_id = _writer_tenant_id(ctx, auth)
    _owned(pool, tenant_id, account_id)
    return _own_answer(pool.set_display_name(account_id, display_name=body.display_name), tenant_id)


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
    tenant_id = _writer_tenant_id(ctx, auth)
    _owned(pool, tenant_id, account_id)
    return _own_answer(pool.set_state(account_id, state=body.state, reason=body.reason), tenant_id)


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
    _owned(pool, _writer_tenant_id(ctx, auth), account_id)
    return pool.remove(account_id)


# --------------------------------------------------------------------------
# Who holds an account, and who held it (#379 parts 2 and 3)
# --------------------------------------------------------------------------
#
# The broker serves these to a PLATFORM caller with every tenant's tasks in
# them. Every bit of per-viewer filtering is `accountholds.py`'s, given the
# caller's RESOLVED tenant and whether it owns the account; see that module for
# what each viewer is shown and why. Nothing here logs a task id: a log line is
# readable by people the response is not.


def _viewer(
    scope: str | None,
    account_id: str,
    auth: AuthContext,
    ctx: AppContext,
    pool: AccountPool,
    platform_route: tuple[str, str],
) -> tuple[Viewer, str | None, Any]:
    """(viewer, tenant, visible accounts) for a holder read, or a refusal.

    `scope=platform` is the admin view and goes through `require_admin` FIRST,
    before any tenant is resolved or any broker call is made. Every other
    caller sees the account only when the broker lists it for their tenant --
    owned or lent -- and gets the same 404 an unknown id gets otherwise.

    The third element is what a swap may name (`accountholds._swap_side`):
    the accounts in this tenant's own listing, id to label, and nothing else
    -- the same set this tenant may already read on `GET /v1/accounts`.
    """
    if scope not in (None, "", "tenant", "platform"):
        raise ValidationFailed("scope must be 'tenant' or 'platform'")
    if scope == "platform":
        require_admin(auth, platform_route)
        return "platform", None, EVERY_ACCOUNT
    tenant_id = _tenant_id(ctx, auth)
    listed = [
        a for a in pool.list_accounts(tenant_id).get("accounts") or [] if isinstance(a, dict)
    ]
    visible = {
        str(a["account_id"]): str(a.get("label") or "")
        for a in listed
        if isinstance(a.get("account_id"), str) and a.get("account_id")
    }
    for account in listed:
        if account.get("account_id") == account_id:
            return viewer_of(account, tenant_id), tenant_id, visible
    raise NotFound(f"no account {account_id!r} in this tenant's pool")


def _check(ctx: AppContext) -> TaskCheck:
    store = ctx.store
    return TaskCheck(
        tasks_by_id=store.tasks_by_id,
        list_attempts=lambda tenant, task: store.list_attempts(tenant, task),
    )


@router.get("/{account_id}/holders")
def account_holders(
    account_id: str,
    scope: str | None = Query(default=None, description="tenant | platform"),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Who is on this account now, as this caller may see it.

    Own holds with a task link, the attempt and since when; the owner of a
    lent account sees how many of each borrowing tenant's agents are on it;
    a borrower sees a count of everyone else's. No assignment id, no secret.
    """
    viewer, tenant_id, visible = _viewer(
        scope, account_id, auth, ctx, pool, HOLDERS_PLATFORM_ROUTE
    )
    return holders_view(
        pool.holds(account_id),
        viewer=viewer,
        tenant_id=tenant_id,
        check=_check(ctx),
        now=datetime.now(timezone.utc),
        visible=visible,
    )


#: What the broker's history cursor looks like -- `<instant>|<skip>`, the skip
#: at most three digits (the broker's page is at most 500). The broker enforces
#: the value; this refuses a forged one before a broker call is made.
_CURSOR_SHAPE = r"^[0-9TZ:+.\-]{1,40}\|[0-9]{1,3}$"
_CURSOR_RE = re.compile(_CURSOR_SHAPE)
#: A BORROWER's cursor -- `<instant>|own<k>`, `k` its own spans at that instant
#: (#361, `accountholds.OWN_CURSOR_MARK`). Served only to a borrower and
#: accepted only from one; every other viewer pages on the broker's own.
_OWN_CURSOR_RE = re.compile(rf"^[0-9TZ:+.\-]{{1,40}}\|{OWN_CURSOR_MARK}[0-9]{{1,3}}$")


def _opaque_cursor(raw: str | None) -> str | None:
    """The broker's cursor as this route serves it: base64url, unpadded (F12).

    The raw form carries the instant's `+00:00`, and a client that put it in
    a URL unencoded sent a `+`, which a query string decodes as a space -- so
    the route refused the very cursor it had served. base64url has no `+`,
    `/`, `=`, `|` or space, and nothing in it is new: it is the instant and
    skip of a row the caller was already shown.
    """
    if not raw:
        return None
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _broker_cursor(served: str | None) -> str | None:
    """The raw cursor inside one this route served, or a 422.

    Either the broker's own `<instant>|<skip>` or a borrower's
    `<instant>|own<k>`; which viewer may send which is decided once the viewer
    is known (`account_history`).

    THE RAW FORM IS STILL ACCEPTED FOR ONE RELEASE (owner decision 2026-10-05),
    so a client holding a cursor from before the change keeps paging. It
    contains `|`, which base64url never does, so the two cannot be confused.
    Either way the result must have the broker cursor's shape, so a forged
    value is refused before any broker call, exactly as before.
    """
    if not served:
        return None
    if "|" in served:
        raw = served
    else:
        try:
            raw = base64.urlsafe_b64decode(
                served.encode("ascii") + b"=" * (-len(served) % 4)
            ).decode("ascii")
        except (binascii.Error, UnicodeError, ValueError):
            raw = ""
    if not (_CURSOR_RE.match(raw) or _OWN_CURSOR_RE.match(raw)):
        raise ValidationFailed("the cursor is not one this route issued")
    return raw


#: The broker's HOLD_LOG_RETENTION (90 days), restated because this service does
#: not import the broker. Every instant a history read names must lie within
#: [now - retention - 1 day, now + 1 day]: the broker refuses the rest with a
#: 422, and refuses it here first so a value it cannot parse never reaches it
#: and comes back as AccountPoolUnavailable.
_HOLD_LOG_RETENTION = timedelta(days=90)


def _utc(raw: Any) -> datetime | None:
    """A parsed instant as UTC, or None for anything that is not one."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(
            timezone.utc
        )
    except (ValueError, OverflowError):
        return None


def _check_range(raw: str | None, name: str, now: datetime) -> datetime | None:
    """422 when `raw` is not an ISO 8601 instant, or is one outside the retained range.

    An unparsable value is refused HERE, for every caller, before any broker
    call: leaving it to the broker made the refusal depend on a second
    service's parser and spent a broker round trip on a value this service can
    already see is wrong. An instant whose UTC conversion overflows
    (`0001-01-01T00:00:00+01:00`) is out of range. None only when `raw` is
    absent.
    """
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(
            timezone.utc
        )
    except OverflowError:
        raise ValidationFailed(f"`{name}` is outside the range the hold log retains") from None
    except ValueError:
        raise ValidationFailed(f"`{name}` is not an ISO 8601 instant") from None
    if not now - _HOLD_LOG_RETENTION - timedelta(days=1) <= value <= now + timedelta(days=1):
        raise ValidationFailed(f"`{name}` is outside the range the hold log retains")
    return value


#: The broker's default window when `from` is absent (HISTORY_DEFAULT_SPAN).
_HISTORY_DEFAULT_SPAN = timedelta(days=7)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(value: datetime) -> datetime:
    floor = _floor_hour(value)
    return floor if floor == value else floor + timedelta(hours=1)


def _require_own_cursor(
    pool: AccountPool, account_id: str, tenant_id: str | None, cursor: str
) -> str:
    """The broker's cursor for a borrower's `<instant>|own<k>`, or a 422.

    THE INSTANT MUST BE ONE OF THE BORROWER'S OWN SPANS. Otherwise `T|0` for
    any T is a probe: the page it opens counts the other tenants' rows at or
    below T, so a borrower could bisect T to read when another tenant held the
    account. This service mints a borrower cursor only from the borrower's own
    row (`own_page`), so that is the only kind it accepts back.

    AND `k` MUST NAME ONE OF THOSE OWN SPANS: 1 <= k <= the borrower's spans at
    exactly that instant (#361). The broker's skip counts every row at the
    instant, other tenants' included; a borrower cursor used to carry that
    count, and sending `T|n` back for each n read exactly how many other
    tenants' spans lay between two of its own. The skip is now computed HERE,
    from the lookup: every row at T, in the broker's order, up to and
    including the borrower's k-th own row -- the same order the paging read
    returns them in, since both are one query on the same index. It is sent to
    the broker and never served.

    One lookup, of exactly the instant, no page walk. Its first page holds
    `HISTORY_PAGE` (100) rows; more than that sharing one microsecond is not
    a shape the hold log has, and a cursor past them is refused, not guessed.
    """
    position = own_cursor_position(cursor)
    if position is None:
        raise ValidationFailed("the cursor is not one this route issued to you")
    stamp, at, k = position
    page = pool.hold_history(
        account_id,
        start=at.isoformat(),
        end=(at + timedelta(microseconds=1)).isoformat(),
        cursor=None,
    )
    seen = own = 0
    for r in page.get("spans") or []:
        if not isinstance(r, dict) or _utc(r.get("assigned_at")) != at:
            continue
        seen += 1
        if r.get("tenant_id") == tenant_id:
            own += 1
            if own == k:
                return f"{stamp}|{seen}"
    raise ValidationFailed("the cursor is not one this route issued to you")


@router.get("/{account_id}/history")
def account_history(
    account_id: str,
    start: str | None = Query(default=None, alias="from", max_length=64),
    end: str | None = Query(default=None, alias="to", max_length=64),
    cursor: str | None = Query(default=None, max_length=80),
    scope: str | None = Query(default=None, description="tenant | platform"),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Recorded spans on this account in [from, to), newest first, paged.

    The caller's own spans name their task. The owner of a lent account sees
    every borrower's spans with times, outcome and tenant; a borrower sees only
    its own, and a count of everyone else's. The broker validates
    the window and the cursor and answers a bad one with a 422 that names it.

    `next_cursor` is OPAQUE (base64url, F12): send it back as served. The raw
    `<instant>|<skip>` form is still accepted for one release -- except from
    a borrower, whose cursor is `<instant>|own<k>` since #361 and whose cursor
    from before that is refused with a 422.
    """
    # First, as the Query pattern used to: a forged cursor reaches no broker call.
    cursor = _broker_cursor(cursor)
    viewer, tenant_id, visible = _viewer(
        scope, account_id, auth, ctx, pool, HISTORY_PLATFORM_ROUTE
    )
    now = datetime.now(timezone.utc)
    _check_range(start, "from", now)
    _check_range(end, "to", now)
    at_cursor = _check_range(cursor.rpartition("|")[0], "cursor", now) if cursor else None
    # Each viewer pages on its own kind of cursor, and the other kind is a 422
    # before any broker call: a borrower's from before #361 counted other
    # tenants' rows, and an own-count cursor means nothing to the broker.
    own_cursor = cursor if viewer == "borrower" else None
    if cursor and bool(_OWN_CURSOR_RE.match(cursor)) != (viewer == "borrower"):
        raise ValidationFailed("the cursor is not one this route issued to you")
    if viewer == "borrower" and cursor:
        if at_cursor is None:
            raise ValidationFailed("the cursor is not one this route issued to you")
        cursor = _require_own_cursor(pool, account_id, tenant_id, cursor)

    if viewer == "borrower":
        # THE UTC HOUR GRID. A borrower is told how many other agents held the
        # account in its window, and an exact count per window let it halve the
        # window until one other tenant's start time was pinned to the
        # microsecond. So `from` snaps DOWN and `to` UP to whole UTC hours, here,
        # before the broker is asked, and the response echoes the snapped window.
        # Absent bounds are made explicit so the broker's own defaults (an
        # un-snapped `now`) never apply.
        hi_raw, lo_raw = _check_range(end, "to", now), _check_range(start, "from", now)
        if (end and hi_raw is None) or (start and lo_raw is None):
            raise ValidationFailed("`from` and `to` must be ISO 8601 instants with an offset")
        hi = _ceil_hour(hi_raw or now)
        lo = _floor_hour(lo_raw or hi - _HISTORY_DEFAULT_SPAN)
        start, end = lo.isoformat(), hi.isoformat()

    def fetch(at: str | None) -> dict:
        return pool.hold_history(account_id, start=start, end=end, cursor=at)

    payload = fetch(cursor)
    if viewer == "borrower":
        payload = own_page(payload, tenant_id=tenant_id, cursor=own_cursor, fetch=fetch)
    served = history_view(
        payload,
        viewer=viewer,
        tenant_id=tenant_id,
        check=_check(ctx),
        now=datetime.now(timezone.utc),
        continued=bool(cursor),
        visible=visible,
    )
    served["next_cursor"] = _opaque_cursor(served.get("next_cursor"))
    return served


# ---------------------------------------------------------------------------
# An account's health (#2302)
# ---------------------------------------------------------------------------
#
# THE SAME READ RULE AS THE HOLDER READS ABOVE (`_viewer`): the account's
# owner, a workspace it is lent to, or a platform admin with `scope=platform`;
# anyone else gets the 404 an unknown id gets. The broker's record names every
# tenant and task that met the account, so what each viewer sees of an event
# is decided here:
#
#   * platform -- everything;
#   * owner    -- every event with its tenant; a TASK only on its own
#                 tenant's events, and only a task that tenant owns
#                 (`TaskCheck.owns`, as the holder reads do);
#   * borrower -- its own events in full (task by the same check); another
#                 tenant's with no tenant, task or message, and its time
#                 floored to the UTC hour, the grid the history read puts a
#                 borrower on (#361) so it cannot time another tenant's work.
#
# Every message goes through `swarm_redaction` here, whoever reads it: the
# worker redacts before it sends, and this is the second line, on the way out.


def _health_event(
    raw: Any, *, viewer: str, tenant_id: str | None, check: TaskCheck
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("kind") not in ACCOUNT_HEALTH_KINDS:
        return None
    at = _utc(raw.get("at"))
    if at is None:
        return None
    reset = _utc(raw.get("reset_at"))
    owner = raw.get("tenant_id") if isinstance(raw.get("tenant_id"), str) else None
    task = raw.get("task_id") if isinstance(raw.get("task_id"), str) else None
    message = redact_text(str(raw.get("message") or "")).text[:200]
    mine = viewer == "platform" or (owner is not None and owner == tenant_id)
    if viewer == "platform":
        shown_task = task
    elif mine and task and owner and check.owns(owner, task):
        shown_task = task
    else:
        shown_task = None
    if viewer == "borrower" and not mine:
        return {
            "at": _floor_hour(at).isoformat(),
            "kind": raw["kind"],
            "tenant_id": None,
            "task_id": None,
            "window": raw.get("window") if raw.get("window") in ACCOUNT_HEALTH_WINDOWS else None,
            "reset_at": reset.isoformat() if reset else None,
            "message": None,
            "own": False,
        }
    return {
        "at": at.isoformat(),
        "kind": raw["kind"],
        "tenant_id": owner,
        "task_id": shown_task,
        "window": raw.get("window") if raw.get("window") in ACCOUNT_HEALTH_WINDOWS else None,
        "reset_at": reset.isoformat() if reset else None,
        "message": message,
        "own": owner is not None and owner == tenant_id,
    }


def health_view(
    payload: Any, *, account_id: str, viewer: str, tenant_id: str | None, check: TaskCheck
) -> dict[str, Any]:
    """The broker's health record, by allow-list, as `viewer` may see it."""
    data = payload if isinstance(payload, dict) else {}
    counts_raw = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    series: list[dict[str, Any]] = []
    for entry in data.get("hourly") or []:
        hour = _utc(entry.get("hour")) if isinstance(entry, dict) else None
        if hour is None:
            continue
        counts = {k: v for k, v in (_health_counts(entry.get("counts")) or {}).items() if v}
        series.append({"hour": hour.isoformat(), "counts": counts, "total": sum(counts.values())})
    events = [
        e
        for e in (
            _health_event(raw, viewer=viewer, tenant_id=tenant_id, check=check)
            for raw in data.get("events") or []
        )
        if e is not None
    ]
    limited = _utc(data.get("limited_until"))
    return {
        "account_id": account_id,
        "status": _health_word(data.get("status")),
        "limited_until": limited.isoformat() if limited else None,
        "counts": {span: _health_counts(counts_raw.get(span)) for span in ACCOUNT_HEALTH_SPANS},
        "hourly": series,
        "events": events,
        "observed_at": data.get("observed_at") if isinstance(data.get("observed_at"), str) else None,
    }


def _pool_record(raw: Any) -> tuple[str, str, list[str]] | None:
    """(account id, owner, loans) of one pool-wide record, or None."""
    if not isinstance(raw, dict):
        return None
    account_id, owner = raw.get("account_id"), raw.get("owner_tenant")
    if not (isinstance(account_id, str) and account_id and isinstance(owner, str)):
        return None
    loans = [t for t in raw.get("lend_to") or [] if isinstance(t, str)]
    return account_id, owner, loans


@router.get("/health")
def accounts_health(
    scope: str | None = Query(default=None, description="tenant | platform"),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """Every account's health in ONE read (ACCOUNT-HEALTH-PAGE, #2302): the
    record `GET /v1/accounts/{id}/health` serves, for each account this tenant
    may run on (owned or lent to it), or for every account with
    `scope=platform` (a platform admin only, refused before the broker is
    asked).

    ONE BROKER CALL, never a listing plus one read per account: Capacity ›
    Account health polls it, and a pool of 30 would otherwise be 31 calls a
    poll. Each account is then shown by the per-account read's own viewer rule
    (`health_view`, the section above): owner, borrower or platform, every
    message redacted again. A record for an account this tenant neither owns
    nor borrows is DROPPED here even if the broker sent it -- the broker
    filters by the same rule, and this is the second line, as the redaction
    is.
    """
    if scope not in (None, "", "tenant", "platform"):
        raise ValidationFailed("scope must be 'tenant' or 'platform'")
    tenant_id: str | None
    if scope == "platform":
        require_admin(auth, HEALTH_POOL_PLATFORM_ROUTE)
        tenant_id = None
    else:
        tenant_id = _tenant_id(ctx, auth)
    payload = pool.accounts_health(tenant_id)
    data = payload if isinstance(payload, dict) else {}
    check = _check(ctx)
    kept: list[tuple[dict[str, Any], str, str, str]] = []
    for raw in data.get("accounts") or []:
        record = _pool_record(raw)
        if record is None:
            continue
        account_id, owner, loans = record
        if tenant_id is None:
            viewer = "platform"
        elif owner == tenant_id or tenant_id in loans:
            viewer = viewer_of({"owner_tenant": owner}, tenant_id)
        else:
            continue
        kept.append((raw, account_id, owner, viewer))
    # One batched ownership read per tenant for every event's task, rather
    # than one per event (`TaskCheck.prime`).
    check.prime(
        (str(e.get("tenant_id")), str(e.get("task_id")))
        for raw, _a, _o, _v in kept
        for e in raw.get("events") or []
        if isinstance(e, dict) and isinstance(e.get("tenant_id"), str) and isinstance(e.get("task_id"), str)
    )
    accounts = []
    for raw, account_id, owner, viewer in kept:
        accounts.append({
            **health_view(raw, account_id=account_id, viewer=viewer, tenant_id=tenant_id, check=check),
            "label": raw.get("label") if isinstance(raw.get("label"), str) else "",
            "owner_tenant": owner,
        })
    events_max = data.get("events_max")
    return {
        "tenant_id": tenant_id,
        "accounts": accounts,
        "events_max": events_max if isinstance(events_max, int) and not isinstance(events_max, bool) else None,
        "truncated": data.get("truncated") is True,
        "observed_at": data.get("observed_at") if isinstance(data.get("observed_at"), str) else None,
    }


@router.get("/{account_id}/health")
def account_health(
    account_id: str,
    scope: str | None = Query(default=None, description="tenant | platform"),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
    pool: AccountPool = Depends(account_pool),
) -> dict:
    """One account's health (#2302): counts for the last 1 h / 24 h / 7 d by
    kind, an hourly series for 7 days, the last 50 events newest first, and a
    one-word status. THE STATUS RULE is the broker's
    (`quota_broker.accountstore.health_status`), first match wins:

      1. `reauth_needed` -- the account is REAUTH_REQUIRED;
      2. `limited` -- a recorded 429 has not reached its reset, or its window
         readings keep it from a new agent;
      3. `erroring` -- 3 or more errors (auth, overloaded, 5xx, network,
         refused at assignment, refresh failed) in the last hour;
      4. `healthy` -- otherwise.

    Read by the account's owner, a workspace it is lent to, or a platform
    admin (`scope=platform`); anyone else is answered 404, as for an id that
    does not exist. What each sees of an event: see the section above.
    """
    viewer, tenant_id, _visible = _viewer(
        scope, account_id, auth, ctx, pool, HEALTH_PLATFORM_ROUTE
    )
    return health_view(
        pool.account_health(account_id),
        account_id=account_id,
        viewer=viewer,
        tenant_id=tenant_id,
        check=_check(ctx),
    )
