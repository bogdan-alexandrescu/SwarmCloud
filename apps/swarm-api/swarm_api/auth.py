"""Caller authentication: Google ID token -> Principal -> tenant.

There is no shared platform bearer token anywhere in this service. A shared
secret carries no identity, and without identity there is no tenant to attribute
a task to, no way to scope a list response and no boundary to enforce.

Nothing in this module -- not a log line, not an exception message, not an error
response -- may contain the Authorization header or any part of the token. The
`_redact` helper exists so that the only thing available to log is a short,
non-reversible fingerprint.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

from swarm_common.identity import (
    AuthError,
    Principal,
    assert_allowed_domain,
    resolve_tenant,
    tenant_id_for_group,
    tenant_id_for_user,
    tenant_member_for,
    worker_service_account_id,
)

from .errors import (
    Forbidden,
    MembershipUnavailable,
    PreferencesUnavailable,
    Unauthenticated,
    UpstreamUnavailable,
    WorkspaceNotReady,
)
from .gitidentity import clean_name, is_service_account
from .groups import GroupLookupError, MembershipResolver
from .memberships import is_person
from .registry import EMPTY as EMPTY_REGISTRY
from .registry import RegistrySnapshot, WorkspaceRegistry
from .settings import ApiSettings
from .store import TENANTS
from .workspaces import READY, WORKSPACES, is_service_identity, state_of

if TYPE_CHECKING:  # the context only carries what `preferences` read
    from .memberships import MembershipStore
    from .preferences import Preferences, StoredPreferences

log = logging.getLogger(__name__)


#: The routes that act in no workspace, as (HTTP method, route template), and
#: so are served when the caller's preferences cannot be read and the request
#: names no workspace (docs/design/user-profile.md §4.5, Q9). They are what a
#: person needs to see their choices and pick one; on them `default_source` is
#: "unavailable" and `tenant_choices` keeps today's order. Every other route
#: answers that request 503 `preferences_unavailable` (`require_known_default`).
#:
#: A CLOSED LIST, held equal to these three by
#: tests/unit/control_plane/test_default_workspace.py: a route that acts in a
#: workspace -- the bell's `GET /v1/notifications` among them, whose items are
#: a workspace's -- must not join it by accident, because joining it is
#: running in a guessed workspace.
PREFERENCE_FREE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/v1/tenants/me"),
    ("GET", "/v1/tenants/mine"),
    ("GET", "/v1/profile"),
})


#: The request header a caller uses to pick ONE of its verified tenant
#: memberships (the tenant switcher, owner decision 2026-10-01). It SELECTS and
#: never grants: `Authenticator._select_tenant` honours it only when the named
#: tenant is among the registered tenant groups Cloud Identity confirmed for
#: this caller on this request, or is the caller's OWN ready personal workspace
#: (#1200), or -- for a service account, and only when named -- its OWN service
#: tenant (`Authenticator._with_own_service_tenant`, HW-ACCEPT-ID). Absent (or
#: empty), the tenant is that personal workspace when it is ready, else today's
#: first match in the admin-ordered `tenant_groups`.
TENANT_HEADER = "X-Swarm-Tenant"

#: The same selection as a QUERY parameter, accepted on `TENANT_QUERY_ROUTES`
#: only. Those are the routes a browser fetches by itself -- an `<img src>`, a
#: download `href`, an "open full" tab -- and a browser-initiated request
#: carries no custom header, so without this a console switched to a
#: non-default tenant would 404 every artifact of the tasks it lists. It is
#: validated by exactly the same `Authenticator._select_tenant` as the header:
#: it selects among the caller's verified memberships and never grants one.
#:
#: The checkpoint archive is here for the same reason: the console offers it
#: as a plain download link (`checkpointDownloadHref`), so the browser can
#: stream a large archive to disk rather than into memory, and that link can
#: carry no header either.
TENANT_QUERY = "tenant"
TENANT_QUERY_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content"),
    # A live-browser recording is a `<video src>` (docs/design/live-browser.md
    # §7; Recordings.tsx and Theater.tsx), which sends no header either.
    ("GET", "/v1/tasks/{task_id}/live/recordings/raw"),
    ("GET", "/v1/tasks/{task_id}/attempts/{attempt_id}/live/recordings/{seq}"),
})


#: `GET /v1/tenants/mine`'s `via` for a group workspace the person reaches by
#: a platform-side membership, not its Google group (docs/design/
#: join-requests.md §4.5 step 3), beside `roles.VIA_MEMBER`, `VIA_PERSONAL`
#: and `VIA_PLATFORM_ADMIN`.
VIA_PLATFORM_MEMBER = "platform_member"


class TenantNotMember(Forbidden):
    """`X-Swarm-Tenant` named a tenant the caller is not a verified member of.

    Its own code so a client can drop a stale stored choice on exactly this
    refusal and on no other 403. The message never echoes the header value,
    and is the same whether the named tenant exists or not: "not yours" and
    "no such tenant" must not be distinguishable, or the header is an oracle
    for enumerating tenant ids.
    """

    code = "tenant_not_member"


def _redact(token: str) -> str:
    """A stable, non-reversible tag for correlating logs without leaking a token."""
    return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class AuthContext:
    principal: Principal
    tenant_id: str
    is_admin: bool
    #: WHO the tenant is, as opposed to who is calling. For a group tenant this
    #: is the group email (`eng@example.com`); for the personal fallback it is the
    #: user's own address. `tenant_id` alone cannot identify a tenant, because
    #: the frozen `tenant_id_for_group` slugs the local part only: `eng@example.com`
    #: and `eng@partner.com` both produce `eng`. The store compares this against
    #: the tenant document so the second group cannot inherit the first's
    #: service account, secrets and GCS prefix.
    tenant_principal: str = ""
    #: Cloud Identity could not say whether this caller is in an admin group.
    #:
    #: `is_admin` is a boolean and a failed lookup has no boolean. Folding one
    #: into the other makes "the directory did not answer" indistinguishable
    #: from "you are not an admin" -- and the second is what `require_admin`
    #: then tells an operator, in the middle of the incident they opened the
    #: admin surface to deal with, about a privilege they hold.
    #:
    #: Only ever true while `is_admin` is False; a confirmed membership, or an
    #: `admin_users` entry, is an answer and needs no lookup.
    admin_unresolved: bool = False
    #: On `ApiSettings.admin_pool_users`: may call the admin routes in
    #: `POOL_ADMIN_ROUTES`, and no other. NOT a kind of admin -- `is_admin`
    #: stays False for such a caller, so nothing that reads that flag widens.
    #: The only place this is consulted is `require_admin`.
    is_pool_admin: bool = False
    #: The listed email when the tenant came from a service-account listing
    #: (contract request 30); "" otherwise (today's only case).
    tenant_member: str = ""
    #: "" for an ordinary member (a human, or -- unlisted -- a service
    #: account): every route behaves exactly as today. "continuation" for a
    #: listed service account: it may submit a `continues_task` workflow and
    #: read what IT submitted, and nothing else. NOT a kind of admin and not
    #: read by `require_admin` -- `is_admin`/`is_pool_admin` stay False for
    #: every listed account regardless of this field.
    member_scope: str = ""
    #: Every tenant this caller may act in, as (tenant_id, principal) -- what
    #: `GET /v1/tenants/mine` lists and the only values `X-Swarm-Tenant` may
    #: select. FIRST, the caller's OWN personal workspace, (u-<caller>, the
    #: caller's email), when `PersonalTenants.ready` says it is ready (#1200,
    #: owner decision 2026-10-12: personal by default, groups are what you
    #: switch to). Then every registered tenant group Cloud Identity CONFIRMED
    #: this caller is in, (tenant_id, group email), in the admin order of
    #: `tenant_groups`. Derived from the same answers the default tenant is
    #: resolved from, so the list and the default can never disagree: when it
    #: is non-empty its first entry IS the default tenant. Empty for a caller
    #: in no group without a ready workspace, and for a listed service account.
    tenant_choices: tuple[tuple[str, str], ...] = ()
    #: On `ApiSettings.rollup_sweeper_users`: the identity the per-tenant
    #: Cloud Scheduler rollup jobs present (D17). May call the routes in
    #: `ROLLUP_SWEEPER_ROUTES` and nothing else -- refused on every other
    #: authenticated route by `require_continuation_route`, the per-route
    #: default-deny `current_auth` applies to every request, and let through
    #: `require_admin` on those routes alone. NOT an admin (`is_admin` and
    #: `is_pool_admin` stay False) and NOT a tenant member: `tenant_id` is the
    #: personal fallback the frozen `resolve_tenant` derives, which no route
    #: it can reach reads -- the rollup route takes its tenant as a parameter.
    is_rollup_sweeper: bool = False
    #: On `SCHEDULE_TICK_USERS` (`schedule_tick_users`): the identity the
    #: one-minute schedule tick presents, `swarm-schedule-tick` (docs/
    #: schedules.md §2.1, owner decision SD10). May call the routes in
    #: `SCHEDULE_TICK_ROUTES` and nothing else, by the same two gates as the
    #: rollup sweeper. NOT an admin, NOT a tenant member and NOT the rollup
    #: sweeper: its reach (due schedules across tenants) differs from the
    #: sweeper's (one named tenant), so it is a different account on a
    #: different list, and the sweeper's route set is not widened.
    is_schedule_tick: bool = False
    #: What a person's commits are named (`gitidentity`), and the name the
    #: platform calls them by (docs/design/user-profile.md §4.4, Q11 (a)): the
    #: name stored in their preferences when they set one, else the verified
    #: token's `name` claim -- which an IAP assertion never carries -- else
    #: the email's local part. Cleaned for a commit identity
    #: (`gitidentity.clean_name`). Never asked of a directory. A task keeps
    #: the name recorded at admission, so a later rename rewrites no commit.
    display_name: str = ""
    #: The caller is `ApiSettings.platform_owner` (docs/workspaces.md §6.5).
    #: Always also `is_admin`. Read by the admin-role routes, which let only
    #: the owner change the owner's role.
    is_owner: bool = False
    #: The caller's own `user_preferences` document as authentication read it
    #: (`preferences.StoredPreferences`; the defaults when they have none), so
    #: no route reads it a second time -- the bell's mutes and reminder come
    #: from here (§4.6). None when it could not be read, and for every caller
    #: that is not a person, which never reads it.
    preferences: "StoredPreferences | None" = None
    #: Why the first entry of `tenant_choices` is first (§4.5), for
    #: `GET /v1/tenants/mine` and `GET /v1/profile`: "profile" (the stored
    #: default), "personal" (the caller's own ready workspace), "first_group"
    #: (the first confirmed group in admin order), "unavailable" (the
    #: preferences could not be read, so today's order stands and is not the
    #: person's known default), or "" when there is no choice at all.
    default_source: str = ""
    #: Why a stored default was NOT applied, or "": "not_a_member" (a group
    #: the caller is no longer confirmed in) or "not_ready" (their own
    #: personal workspace, while it is not ready). The stored value is kept;
    #: it applies again as soon as it is one of `tenant_choices`.
    default_ignored: str = ""
    #: The request names no workspace AND the preferences could not be read,
    #: so its default workspace is not known (Q9). Every route except
    #: `PREFERENCE_FREE_ROUTES` then answers 503 `preferences_unavailable`
    #: (`require_known_default`); it never runs in the computed default.
    default_unknown: bool = False
    #: The registered GROUP workspaces a platform admin may select although
    #: Cloud Identity did not confirm them as a member, as (tenant_id, group
    #: email) in the admin order of `tenant_groups` (docs/design/
    #: workspace-roles.md §4.2.2, owner answer Q5). A SEPARATE list from
    #: `tenant_choices`, so nothing that reads the memberships -- the default
    #: tenant, `principal.groups`, the stored default -- changes. Empty unless
    #: `is_admin` and `ApiSettings.platform_admin_reach` are both true and the
    #: caller is a person (`Authenticator._special` is false). Never holds an
    #: id already in `tenant_choices`, and never a `u-` id: a group slug can
    #: land in the personal namespace, and a personal workspace is reachable
    #: by its own person only. Built from configuration; asks Cloud Identity
    #: nothing.
    platform_reach: tuple[tuple[str, str], ...] = ()
    #: How the caller reached `tenant_id`: "platform_admin" when
    #: `X-Swarm-Tenant` selected an entry of `platform_reach` (a platform
    #: admin acting in a group workspace they are not a member of, which
    #: §4.2.4 audits), "" otherwise -- a membership, their own personal
    #: workspace, or the default.
    tenant_via: str = ""
    #: The registered GROUP workspaces this person joined by an approved
    #: request rather than through the Google group: a signed
    #: `workspace_memberships` grant (docs/design/join-requests.md §4.5 step
    #: 1, lane JOIN-RESOLVE), as (tenant_id, group email) in the grants' `at`
    #: order. Every entry is ALSO in `tenant_choices`, appended after the
    #: confirmed groups -- this list says which choices came from a
    #: membership, for `GET /v1/tenants/mine`'s `via: "platform_member"`.
    #: Empty unless `ApiSettings.platform_memberships` is on and the caller
    #: is a person (`Authenticator._special` is false). Never an id already
    #: among the confirmed groups (the Google answer wins), never a `u-` id,
    #: and never a group that is not, on this request, the registered group
    #: its id selects (`MembershipRead.counted` over `_reachable_groups`).
    platform_memberships: tuple[tuple[str, str], ...] = ()
    #: The switch is on and this person's memberships could not be read
    #: (Firestore failed, or the bounded read ended first; §4.4). Their
    #: groups and personal workspace are offered as usual; a header naming
    #: any other non-`u-` id answers 503 `membership_unavailable` rather than
    #: `tenant_not_member`, because the unread membership could have changed
    #: the answer. With no header, a default the memberships could have
    #: decided is unknown (`Authenticator._with_unread_memberships`), never
    #: guessed.
    memberships_unread: bool = False

    @property
    def email(self) -> str:
        return self.principal.email


class TokenVerifier(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...


class GoogleTokenVerifier:
    """Verifies a Google-issued ID token against Google's public keys.

    `require_audience` exists because `verify_oauth2_token` silently SKIPS the
    `aud` check when the audience is None. With no audience pinned, any Google
    ID token belonging to an allowed-domain account authenticates -- including
    one a third-party SaaS obtained when an employee signed in with Google, which
    that third party could then replay here. Outside local development the
    process refuses to start rather than run with the check off, because the
    failure is otherwise invisible: the service works normally.
    """

    def __init__(self, audience: str = "", *, require_audience: bool = False) -> None:
        self._audience = audience or None
        if require_audience and not self._audience:
            raise ValueError(
                "API_AUDIENCE is required outside local development. Without it the "
                "'aud' claim is not checked and any Google ID token from an allowed "
                "domain is accepted. Set it to this service's Cloud Run URL (the "
                "audience the caller mints its ID token for)."
            )
        self._request = None

    def _transport(self) -> Any:
        if self._request is None:
            # Lazy so importing the app needs no network and no credentials.
            from google.auth.transport import requests as google_requests

            self._request = google_requests.Request()
        return self._request

    def verify(self, token: str) -> dict[str, Any]:
        from google.oauth2 import id_token as google_id_token

        try:
            claims = google_id_token.verify_oauth2_token(
                token, self._transport(), audience=self._audience
            )
        except ValueError as exc:
            # ValueError is what google-auth raises for every bad-token case.
            # The message can echo token content, so it is never propagated.
            log.info("id token rejected (%s): %s", _redact(token), type(exc).__name__)
            raise AuthError("id token verification failed") from None
        if not claims.get("email"):
            raise AuthError("id token carries no email claim")
        if claims.get("email_verified") is False:
            raise AuthError("id token email is not verified")
        return claims


class IapAssertionVerifier:
    """Verifies the JWT Identity-Aware Proxy puts on every request it forwards.

    WHY THIS EXISTS. Behind IAP a browser sends NO Authorization header. IAP has
    already authenticated the person and forwards the result in
    `x-goog-iap-jwt-assertion`; there is no Google ID token for a single-page app
    to obtain and present. Without this, every request from the web UI arrived
    with nothing to verify and was rejected 401 -- a signed-in user, a valid
    certificate, a healthy load balancer, and an API that could not see any of
    it.

    THE AUDIENCE IS NOT OPTIONAL and is a different shape from an ID token's.
    IAP mints per BACKEND SERVICE:

        /projects/<PROJECT NUMBER>/global/backendServices/<BACKEND SERVICE ID>

    Unpinned, google-auth skips the `aud` check entirely, and an assertion
    minted by IAP for ANY other backend in any project would authenticate here.
    That is the same failure the ID-token verifier documents above, and it is
    worse in this direction: an IAP assertion is issued to anyone who can reach
    any IAP-protected resource.

    Two audiences are accepted because two backend services front this platform
    -- the API and the UI -- and a request may arrive through either.

    The keys live at a DIFFERENT endpoint from Google's ordinary OAuth certs
    (`https://www.gstatic.com/iap/verify/public_key`) and are ES256, not RS256,
    so `verify_oauth2_token` cannot be reused.
    """

    #: Where IAP publishes its signing keys. Not the OAuth cert endpoint.
    CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"

    def __init__(self, audiences: tuple[str, ...]) -> None:
        self._audiences = tuple(a for a in audiences if a)
        self._request = None

    @property
    def configured(self) -> bool:
        """False when no audience is pinned, in which case this verifier is OFF.

        Deliberately not "verify without an audience": see the class docstring.
        A misconfigured IAP verifier that accepts everything is worse than one
        that accepts nothing, because nothing is visible.
        """
        return bool(self._audiences)

    def _transport(self) -> Any:
        if self._request is None:
            from google.auth.transport import requests as google_requests

            self._request = google_requests.Request()
        return self._request

    def verify(self, assertion: str) -> dict[str, Any]:
        from google.oauth2 import id_token as google_id_token

        if not self.configured:
            raise AuthError("IAP audience is not configured")

        last: Exception | None = None
        for audience in self._audiences:
            try:
                claims = google_id_token.verify_token(
                    assertion,
                    self._transport(),
                    audience=audience,
                    certs_url=self.CERTS_URL,
                )
            except ValueError as exc:
                # Wrong audience for THIS backend is expected when two are
                # configured; only the last failure is reported.
                last = exc
                continue

            # IAP puts the verified identity in `email`, and `sub` carries a
            # stable `accounts.google.com:<id>` rather than a bare subject.
            email = str(claims.get("email", ""))
            if not email:
                raise AuthError("IAP assertion carries no email claim")
            # IAP only ever forwards identities it has already authenticated, so
            # there is no unverified-email case to reject -- but the downstream
            # code reads `email_verified`, and absent would read as False.
            claims.setdefault("email_verified", True)
            return claims

        log.info("IAP assertion rejected (%s): %s", _redact(assertion),
                 type(last).__name__ if last else "no audience matched")
        raise AuthError("IAP assertion verification failed")


class StaticTokenVerifier:
    """Maps opaque strings to claims. Local development and tests only."""

    def __init__(self, tokens: dict[str, dict[str, Any]]) -> None:
        self._tokens = dict(tokens)

    def verify(self, token: str) -> dict[str, Any]:
        claims = self._tokens.get(token)
        if claims is None:
            raise AuthError("unknown token")
        return dict(claims)


def bearer_tokens(authorization: str | None) -> list[str]:
    """Every bearer credential in an Authorization header, in order.

    A header can legitimately carry more than one credential, and Cloud Run
    exercises that: with IAM authentication enabled it adds its OWN
    Authorization header to the request, and Starlette joins repeated headers
    with ", ". A naive `partition(" ")` then returns
    `<caller-token>, Bearer <platform-token>` as a single value, which is not a
    JWT and fails verification with MalformedError -- while the caller's token
    verifies perfectly everywhere else. That was diagnosed live on 2026-09-16 by
    comparing token fingerprints: the fingerprint the service logged never
    matched the one the client sent.

    So the header is split and every bearer credential returned, letting the
    caller try each rather than guessing which position the real one occupies.
    """
    if not authorization:
        raise Unauthenticated("missing Authorization header")

    found: list[str] = []
    for part in authorization.split(","):
        scheme, _, value = part.strip().partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            found.append(value.strip())
    if not found:
        # Deliberately does not echo the header value.
        raise Unauthenticated("Authorization header must be 'Bearer <google id token>'")
    return found


def bearer_token(authorization: str | None) -> str:
    """The first bearer credential. Kept for callers that want exactly one."""
    return bearer_tokens(authorization)[0]


class PersonalTenants:
    """Whether a verified caller's OWN personal workspace is ready to act in (#1200).

    Ready means BOTH documents the workspace job leaves agree on this person:

      * `tenants/u-<caller>` exists, is `kind: user`, and its principal is the
        caller's email -- the frozen slug keeps only the local part, so the id
        alone does not say whose tenant it is;
      * `workspaces/u-<caller>` is in state `ready` (docs/workspaces.md §1.1),
        and its principal is that same email. Not `approved`: the build has
        not made the service account, namespace and secrets yet, and the
        workspace gate refuses a submit there anyway, so offering it as the
        default would move a group member's work into a tenant that refuses it.

    Asked of `email` only, which the caller passes from the verified token.

    Only a READY answer is cached, per email for `cache_seconds`, as
    `AdminRoles` caches a role: two document reads on every request otherwise.
    A 'not ready' or absent answer is read again on every request (one read
    when there is no `tenants/u-<caller>`, two otherwise), because the moment
    the workspace job finishes is exactly when the person acts: Admin › Setup
    polls while the workspace is built, and a cached 'not ready' refused the
    one-person installer's pool-account sign-in 403 for up to the window after
    it turned ready (E2E-3, DAYONE-PERSONAL-READY). No cap of a few seconds
    either: the person clicks within one. Nothing refused becomes allowed
    except by the documents really saying ready, and nothing here can offer
    one person's tenant to another, so a cached ready only delays a
    withdrawal by the window. Raises whatever Firestore raises, and caches
    nothing then; `Authenticator` turns that into a 503.
    """

    def __init__(
        self,
        db: Any,
        *,
        cache_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, bool]] = {}
        self._service_cache: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def ready(self, email: str) -> bool:
        return self._cached(self._cache, email, self._read, keep_false=False)

    def owns_service_tenant(self, email: str) -> bool:
        """Whether `tenants/<tenant_id_for_user(email)>` is `kind: user` with
        exactly `email` as its principal: a SERVICE identity's own service
        tenant (HW-ACCEPT-ID, owner decision 2026-10-19). The first half of
        `ready` and nothing more: a Terraform-written service tenant has no
        `workspaces/` record, and the workspace gate never judges a service
        account (WD7). Asked only for a caller `own_service_tenant` admits,
        and cached separately from `ready`, so a person's answer is never
        this one. Raises whatever Firestore raises.
        """
        return self._cached(self._service_cache, email, self._names)

    def _cached(self, cache: dict[str, tuple[float, bool]], email: str,
                read: Callable[[str], bool], *, keep_false: bool = True) -> bool:
        email = (email or "").strip().lower()
        if not email:
            return False
        stamp = self._clock()
        with self._lock:
            cached = cache.get(email)
        if cached is not None and cached[0] > stamp:
            return cached[1]
        answer = read(email)
        with self._lock:
            if answer or keep_false:
                cache[email] = (stamp + self._cache_seconds, answer)
            else:
                cache.pop(email, None)
        return answer

    def _names(self, email: str) -> bool:
        tenant_snap = self._db.collection(TENANTS).document(tenant_id_for_user(email)).get()
        tenant = (tenant_snap.to_dict() or {}) if tenant_snap.exists else {}
        return tenant.get("kind") == "user" and _principal(tenant) == email

    def _read(self, email: str) -> bool:
        if not self._names(email):
            return False
        record_snap = self._db.collection(WORKSPACES).document(tenant_id_for_user(email)).get()
        record = (record_snap.to_dict() or {}) if record_snap.exists else None
        return state_of(record) == READY and _principal(record or {}) == email


def _principal(doc: Mapping[str, Any]) -> str:
    return str(doc.get("principal") or "").strip().lower()


def is_worker_identity(email: str, tenant_id: str, configured_prefix: str = "") -> bool:
    """Whether `email` is a worker identity: what a tenant's agents run as, and
    what a workflow's agent speaks as through the MCP bridge.

    Any address under the frozen worker prefix (`worker_service_account_id("")`)
    or the configured one (`ApiSettings.tenant_service_account_prefix`), and
    `tenant_id`'s own derived worker. Both prefixes, so a deployment that names
    its workers otherwise does not make the frozen names a principal, nor the
    reverse. Decided from the verified address alone: a worker can rewrite its
    own `tenants/<id>` document (contract request 87), so no stored field may
    make one look like anything else.
    """
    local = (email or "").strip().lower().split("@", 1)[0]
    configured = configured_prefix or "swarm-agent-worker"
    return local.startswith((worker_service_account_id(""), f"{configured}-")) or \
        local == worker_service_account_id(tenant_id)


def own_service_tenant(auth: AuthContext, configured_prefix: str = "") -> str:
    """The id of the service tenant this caller may act in AS ITS PRINCIPAL, or "".

    The conditions that need no read, shared by tenant selection
    (`Authenticator._with_own_service_tenant`) and the await-input answer
    (`awaitinput.service_principal`), so the two can never admit different
    callers:

      * not the schedule tick, not the rollup sweeper, not a listed
        (continuation-scoped) account -- those act for nobody, even were the
        listed address a tenant's principal;
      * a service account (`is_service_account` / `is_service_identity`): a
        person's own tenant is `PersonalTenants.ready`'s, and needs its
        workspace record;
      * not a worker identity (`is_worker_identity`), by either prefix or the
        derived tenant's own worker.

    The id is `tenant_id_for_user` of the VERIFIED email, never anything the
    caller sent. It is not yet the caller's: the frozen slug drops the domain,
    so whoever asks must still read `tenants/<id>` and find `kind: user` with
    exactly this email as principal (`PersonalTenants.owns_service_tenant`).
    """
    if auth.is_schedule_tick or auth.is_rollup_sweeper or auth.member_scope or auth.tenant_member:
        return ""
    email = (auth.email or "").strip().lower()
    if not email or not (is_service_account(email) or is_service_identity(email)):
        return ""
    own = tenant_id_for_user(email)
    return "" if is_worker_identity(email, own, configured_prefix) else own


def _computed_source(ctx: AuthContext) -> str:
    """Why `tenant_choices`' first entry is first when no stored default
    applies (§4.5): the caller's own ready workspace, else the first confirmed
    group in admin order. "" when there is no choice. The personal entry is
    the one whose principal is the caller (`_with_own_workspace` makes it)."""
    if not ctx.tenant_choices:
        return ""
    return "personal" if ctx.tenant_choices[0][1] == ctx.email else "first_group"


class Authenticator:
    """Turns an Authorization header into an AuthContext."""

    def __init__(
        self,
        settings: ApiSettings,
        verifier: TokenVerifier,
        groups: MembershipResolver,
        iap: Any = None,
        admin_roles: Any = None,
        personal: "PersonalTenants | None" = None,
        preferences: "Preferences | None" = None,
        registry: WorkspaceRegistry | None = None,
        memberships: "MembershipStore | None" = None,
    ) -> None:
        self._settings = settings
        self._verifier = verifier
        self._groups = groups
        self._iap = iap
        # `swarm_api.admins.AdminRoles`, the `admin_roles/` documents. None
        # (a hand-built authenticator in a test) means configuration alone
        # decides admin, which is exactly the behaviour before §6.5.
        self.admin_roles = admin_roles
        # Whether a caller's own personal workspace is ready (#1200). None (a
        # hand-built authenticator in a test) offers no personal tenant, which
        # is exactly the behaviour before #1200.
        self._personal = personal
        # A person's `user_preferences` document (docs/design/user-profile.md
        # §4.5): their stored default workspace and name. None (a hand-built
        # authenticator in a test) reads nothing and applies no stored
        # default, which is exactly the behaviour before the profile.
        self._preferences = preferences
        # The `ready` job-made group and service workspaces
        # (docs/design/workspace-kinds.md §4.4). None (a hand-built
        # authenticator in a test) registers no group and admits no account,
        # which is exactly the behaviour before the registry.
        self._registry = registry
        # Platform-side workspace memberships (docs/design/join-requests.md
        # §4.5). Read only while `platform_memberships` is on; None (a
        # hand-built authenticator in a test, or a deployment that has not
        # wired the store) reads nothing and admits nobody by membership,
        # which is exactly the behaviour before join requests.
        self._memberships = memberships
        # Read once, here, at process start: an address on two lists is
        # refused before the service takes a request (`schedule_tick_users`).
        self._schedule_tick_users = frozenset(schedule_tick_users(settings))

    @property
    def iap(self) -> Any:
        """The IAP assertion verifier, or None. `main.FrontDoor` asks it, before
        any route runs, whether a write comes from the QA account."""
        return self._iap

    @property
    def schedule_tick_configured(self) -> bool:
        """Whether any address is admitted as the schedule tick. False until
        lane S13 renders SCHEDULE_TICK_USERS: the tick then admits nobody."""
        return bool(self._schedule_tick_users)

    def authenticate(
        self,
        authorization: str | None,
        iap_assertion: str | None = None,
        *,
        tenant: str | None = None,
    ) -> AuthContext:
        """`tenant` is the `X-Swarm-Tenant` value, None or "" when absent.

        It is applied AFTER the identity is verified and its memberships
        resolved, and can only narrow the result to one of them -- see
        `_select_tenant`. A refusal raises before any route body runs.

        Between the two, `_with_default` applies the person's stored default
        workspace (docs/design/user-profile.md §4.5). `_select_tenant` is
        unchanged by it: a request that names a tenant acts in that tenant,
        one that names none acts in the first choice, as before.
        """
        ctx = self._authenticate(authorization, iap_assertion)
        ctx = self._with_own_service_tenant(self._with_own_workspace(ctx, tenant), tenant)
        ctx = self._with_default(ctx, tenant)
        ctx = self._with_unread_memberships(ctx, tenant)
        return self._select_tenant(ctx, tenant)

    @staticmethod
    def _with_unread_memberships(ctx: AuthContext, requested: str | None) -> AuthContext:
        """A headerless request whose default the unread memberships could
        have decided acts in no guessed workspace (docs/design/
        join-requests.md §4.4, with user-profile.md's Q9 rule).

        Two cases, both only while `memberships_unread`: the stored default
        was not among the choices (`default_ignored == "not_a_member"`), and
        it may be a workspace joined by request; or there was no choice at
        all, so the first membership would have been the default. Either way
        `default_unknown` is set, and `require_known_default` answers every
        route outside `PREFERENCE_FREE_ROUTES` 503 `membership_unavailable`
        rather than running the work in another workspace. A request that
        names a workspace, and every other caller, is untouched."""
        if not ctx.memberships_unread or requested or ctx.default_unknown:
            return ctx
        if ctx.default_ignored == "not_a_member":
            return replace(ctx, default_unknown=True, default_source="unavailable",
                           default_ignored="")
        if not ctx.tenant_choices:
            return replace(ctx, default_unknown=True, default_source="unavailable")
        return ctx

    def _with_default(self, ctx: AuthContext, requested: str | None) -> AuthContext:
        """Put the person's stored default workspace first (§4.5 steps 1-2).

        Only a person (`_special` is false): a service identity, a listed
        account and the platform's jobs never read preferences, and keep
        today's default. The read goes through `Preferences.cached` (15
        seconds per instance, keyed by email; a failure is never cached).

        The stored default applies only when it is one of `tenant_choices`,
        which this request's verified identity produced: a preference SELECTS
        exactly as `X-Swarm-Tenant` does and never grants (invariant 9). A
        group the person has left is ignored (`not_a_member`), and so is their
        own workspace while it is not ready (`not_ready`); the stored value is
        left as it is, and nothing here writes.

        AN UNREADABLE DOCUMENT IS NEVER A GUESS (Q9). The order is left as it
        is and `default_source` says "unavailable"; when the request names no
        workspace, `default_unknown` makes every route outside
        `PREFERENCE_FREE_ROUTES` a 503 (`require_known_default`), so nothing
        runs in the computed default the person may have moved away from. A
        request that names a workspace does not need the default and is
        served, with `preferences` None.

        A header naming a tenant outside `tenant_choices` is refused by
        `_select_tenant` without this reading anything, as before. One naming
        an entry of `platform_reach` is read for like a membership, so a
        platform admin in a group workspace they are not in keeps their own
        name and preferences (docs/design/workspace-roles.md §4.2.2).
        """
        if self._preferences is None or self._special(ctx):
            return replace(ctx, default_source=_computed_source(ctx))
        if requested and not self._names_a_choice(ctx, requested):
            return ctx
        try:
            stored = self._preferences.cached(ctx.email)
        except Exception as exc:  # noqa: BLE001 - PreferencesUnavailable or any Firestore failure
            log.warning("preferences unreadable (%s)", type(exc).__name__)
            return replace(
                ctx, preferences=None, default_source="unavailable",
                default_unknown=not requested,
            )
        ctx = replace(
            ctx, preferences=stored,
            display_name=clean_name(stored.display_name) or ctx.display_name,
        )
        chosen = stored.default_workspace
        if not chosen:
            return replace(ctx, default_source=_computed_source(ctx))
        entry = next((c for c in ctx.tenant_choices if c[0] == chosen), None)
        if entry is None:
            ignored = "not_ready" if chosen == tenant_id_for_user(ctx.email) else "not_a_member"
            return replace(ctx, default_source=_computed_source(ctx), default_ignored=ignored)
        return replace(
            ctx,
            tenant_id=entry[0],
            tenant_principal=entry[1],
            tenant_choices=(entry, *(c for c in ctx.tenant_choices if c != entry)),
            default_source="profile",
        )

    def _with_own_workspace(self, ctx: AuthContext, requested: str | None) -> AuthContext:
        """Put the caller's OWN ready personal workspace first, as the default (#1200).

        THE ID IS DERIVED FROM THE VERIFIED EMAIL, NEVER FROM THE HEADER: the
        only personal tenant this can ever add is `tenant_id_for_user(ctx.email)`,
        and only when `PersonalTenants.ready` confirms that tenant's documents
        name THIS email -- the frozen slug drops the domain, so `bob@example.com`
        and `bob@partner.com` derive one id, and the principal is what tells
        them apart. Another person's `u-` id is never in the list, so
        `_select_tenant` refuses it exactly as it refuses a group the caller is
        not in.

        Only an ordinary human caller: the schedule tick, the rollup sweeper, a
        listed service account and any service identity keep today's tenant,
        so a CI or acceptance account's headerless default is unchanged. (A
        service account may still NAME its own service tenant:
        `_with_own_service_tenant`.)

        A header naming neither a group this caller is in nor their own id is
        refused below without reading anything, so a refusal still costs no
        Firestore read. A failed read is a 503, never a guess, when the request
        would act in the default or in the personal tenant: falling back to the
        group would run work in a different tenant from the one this caller
        acts in by default -- another secret, another GCS prefix -- exactly the
        misfiling `_from_claims` refuses for a failed group lookup. A request
        that NAMES one of its groups is unaffected by the answer, so it goes on
        in that group without the personal entry. An entry of `platform_reach`
        is treated exactly like one of its groups here (docs/design/
        workspace-roles.md §4.2.2 step 2): the own entry is still offered
        beside it, and a failed read still never refuses it.
        """
        if self._personal is None or self._special(ctx):
            return ctx
        own = tenant_id_for_user(ctx.email)
        if requested and requested != own and not self._names_a_choice(ctx, requested):
            return ctx
        try:
            ready = self._personal.ready(ctx.email)
        except Exception as exc:  # noqa: BLE001 - any Firestore failure
            log.warning("personal workspace unresolved (%s)", type(exc).__name__)
            if requested and requested != own:
                return ctx
            raise UpstreamUnavailable(
                "your personal workspace could not be read; retry shortly"
            ) from None
        if not ready:
            return ctx
        return replace(
            ctx,
            tenant_id=own,
            tenant_principal=ctx.email,
            tenant_choices=((own, ctx.email), *(
                c for c in ctx.tenant_choices if c[0] != own
            )),
        )

    def _with_own_service_tenant(self, ctx: AuthContext, requested: str | None) -> AuthContext:
        """Offer a service identity its OWN service tenant, when it names it
        (HW-ACCEPT-ID, owner decision 2026-10-19).

        A service tenant is a `kind: user` tenant whose principal is a service
        account -- `u-sw-c90291` for swarm-verify, `u-or-9a0792` for
        oracle-api. `_with_own_workspace` skips every service identity, and
        `PersonalTenants.ready` wants a `workspaces/` record such a tenant never
        has, so an account in a group could never act in its own tenant: the
        release's await-input checks, which answer as swarm-verify, were 403
        `person_required` in the group tenant `smoke` on every run.

        Only when the header names EXACTLY `own_service_tenant(ctx)` -- derived
        from the verified email, for a service account that is not a worker,
        a job or a listed account -- and `tenants/<that id>`, read through
        `PersonalTenants`' cache, is `kind: user` with exactly the caller's
        email as principal. The entry is APPENDED: the headerless default is
        unchanged (acceptance relies on an account in a group keeping it), and
        a request that names no tenant, or another one, reads nothing here.
        A failed read is a 503, never a fall-through to another tenant, as
        `_with_own_workspace` answers it.

        LISTED AS THE DEFAULT WHEN IT IS THE DEFAULT (docs/design/qa-identity.md
        §4.9 M4, lane QI-API). A service account in no registered group already
        acts, with no header, in `tenant_id_for_user` of its own address -- the
        frozen personal fallback -- yet `tenant_choices` stayed empty, so
        `GET /v1/tenants/mine` listed nothing and the console's switcher showed
        the QA account no workspace (measured 2026-10-20). When the request
        names no tenant and the resolved default IS that own tenant, with the
        caller as its principal, the same read confirms it and it becomes the
        one entry, first, as the default. This changes no tenant a request
        acts in -- it was already the default -- and lists only the caller's
        OWN tenant, derived from its verified address and confirmed against
        `tenants/<id>`'s principal (invariant 9). A failed read lists nothing
        and refuses nothing, because nothing here selects.
        """
        if self._personal is None:
            return ctx
        own = own_service_tenant(
            ctx, getattr(self._settings, "tenant_service_account_prefix", "")
        )
        if not own:
            return ctx
        if not requested:
            return self._list_own_service_default(ctx, own)
        if requested != own:
            return ctx
        try:
            owned = self._personal.owns_service_tenant(ctx.email)
        except Exception as exc:  # noqa: BLE001 - any Firestore failure
            log.warning("service tenant unresolved (%s)", type(exc).__name__)
            raise UpstreamUnavailable(
                "your service tenant could not be read; retry shortly"
            ) from None
        if not owned:
            return ctx
        return replace(ctx, tenant_choices=(
            *(c for c in ctx.tenant_choices if c[0] != own), (own, ctx.email),
        ))

    def _list_own_service_default(self, ctx: AuthContext, own: str) -> AuthContext:
        """`_with_own_service_tenant`'s headerless half: list the own tenant
        first when it is already the default. Selects nothing."""
        email = (ctx.email or "").strip().lower()
        if ctx.tenant_id != own or (ctx.tenant_principal or "").strip().lower() != email:
            return ctx
        if any(tenant_id == own for tenant_id, _ in ctx.tenant_choices):
            return ctx
        try:
            owned = self._personal.owns_service_tenant(ctx.email)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - any Firestore failure
            log.warning("service tenant unresolved for the list (%s)", type(exc).__name__)
            return ctx
        if not owned:
            return ctx
        return replace(ctx, tenant_choices=((own, ctx.email), *ctx.tenant_choices))

    @staticmethod
    def _names_a_choice(ctx: AuthContext, requested: str) -> bool:
        """Whether `requested` is one of the caller's memberships or, for a
        platform admin, one of `platform_reach`'s entries."""
        named = {tenant_id for tenant_id, _ in ctx.tenant_choices}
        if ctx.is_admin:
            named.update(tenant_id for tenant_id, _ in ctx.platform_reach)
        return requested in named

    @property
    def preferences(self) -> Any:
        """The preferences store `_with_default` reads, or None. The profile
        routes save through this same instance (`routes.profile.
        preferences_store`), so a save's `forget()` empties the cached copy
        authentication reads and the next request acts in the new default
        (#1653): with two instances the old default stayed for 15 seconds."""
        return self._preferences

    @staticmethod
    def _special(ctx: AuthContext) -> bool:
        """Callers whose tenant is not a person's: never offered a personal one."""
        return bool(
            ctx.is_schedule_tick or ctx.is_rollup_sweeper or ctx.member_scope
            or ctx.tenant_member or not ctx.email or is_service_identity(ctx.email)
        )

    def _authenticate(
        self, authorization: str | None, iap_assertion: str | None
    ) -> AuthContext:
        # IAP FIRST, when it is present and configured. Behind the load balancer
        # there is no Authorization header to fall back to -- a browser has no
        # Google ID token to mint -- so this is the only credential a web caller
        # ever has. A direct caller from inside the VPC still presents a bearer
        # and takes the path below.
        #
        # AN ASSERTION MEANS THE BEARER IS NEVER READ (docs/design/qa-identity.md
        # §4.9, M3 measured NO). The QA browser's gate sends the QA account's
        # IAP JWT in `Authorization`, and IAP may pass it through. Behind IAP
        # that header is a credential meant for IAP, never this service's, so a
        # request carrying an assertion is judged on the assertion alone --
        # and when no IAP audience is pinned, it is refused rather than
        # judged on whatever else it carries. `main.FrontDoor` also removes the
        # header before any route reads it.
        if iap_assertion:
            if self._iap is None or not self._iap.configured:
                log.info("IAP assertion presented and no IAP audience is pinned; refused")
                raise Unauthenticated(
                    "this request carries an IAP assertion and this deployment verifies none")
            try:
                return self._from_claims(self._iap.verify(iap_assertion))
            except Forbidden:
                raise
            except AuthError as exc:
                # Not a fall-through to the bearer path: an assertion that fails
                # to verify is a caller claiming an identity it cannot prove,
                # and trying a second mechanism would let a bad assertion be
                # masked by a good bearer.
                raise Unauthenticated(str(exc)) from None
        return self._authenticate_bearer(authorization)

    def _authenticate_bearer(self, authorization: str | None) -> AuthContext:
        # Try every bearer credential the header carries. Cloud Run adds its own
        # Authorization header when IAM authentication is on, so the caller's
        # token is not always the only one -- see bearer_tokens(). Verification
        # is what decides which is real; position is not reliable.
        candidates_tokens = bearer_tokens(authorization)
        claims = None
        last: AuthError | None = None
        for token in candidates_tokens:
            try:
                claims = self._verifier.verify(token)
                break
            except AuthError as exc:
                last = exc
        if claims is None:
            log.info(
                "no bearer credential verified (%d presented)", len(candidates_tokens)
            )
            raise Unauthenticated(str(last) if last else "id token verification failed") from None

        # Symmetric with the IAP branch of `authenticate`: an AuthError raised
        # while turning VERIFIED claims into a context (a service account
        # listed under two tenants, a listed account without an explicit
        # email_verified) is the caller's identity failing to resolve -- a 401,
        # not an unhandled 500.
        try:
            return self._from_claims(claims)
        except Forbidden:
            raise
        except AuthError as exc:
            raise Unauthenticated(str(exc)) from None

    def _from_claims(self, claims: dict[str, Any]) -> AuthContext:
        """Verified claims -> AuthContext, whatever verified them.

        Shared by the bearer and IAP paths deliberately. Tenant resolution,
        domain enforcement, group membership and the admin decision are the part
        that must NOT differ by how the caller arrived -- two copies of this
        would be two tenant boundaries, and CONTRACT.md invariant 9 depends on
        there being one.
        """
        raw_email = str(claims.get("email", ""))
        # Refused BEFORE anything reads it -- including the listing lookup
        # just below, which is why this is here rather than folded into
        # `tenant_member_for` (`identity.py` is frozen and already normalises
        # its own input with `.strip().lower()`; restoring this property
        # there would mean editing the frozen module). Owner decision
        # 2026-09-29, contract request 30 entry: the entry's prose says a
        # trailing newline does not match, and this is where that is now
        # true, for every caller, not only a listed one -- a verified token
        # is never expected to carry incidental whitespace on its email claim,
        # so refusing it here costs no real caller anything.
        if raw_email != raw_email.strip():
            raise AuthError("id token email carries leading or trailing whitespace")
        email = raw_email.lower()
        subject = str(claims.get("sub", ""))
        # IAP prefixes a stable "accounts.google.com:" (see
        # IapAssertionVerifier's docstring above); a bearer ID token's `sub`
        # carries no such prefix. Stripped here, once, for BOTH paths, so
        # `principal.subject` is the bare id either way.
        subject = subject.removeprefix("accounts.google.com:")

        # THE SCHEDULE TICK (docs/schedules.md §2.1, SD10), first, for the
        # rollup sweeper's reasons below: a service-account address is in no
        # Workspace domain and no group, and a failed directory lookup must
        # not 503 a job that runs every minute. `schedule_tick_users` refused
        # an address that is also on any other list, at start.
        if email and email in self._schedule_tick_users:
            if claims.get("email_verified") is not True:
                raise AuthError("the schedule tick requires a verified email claim")
            principal = Principal(
                email=email,
                subject=subject,
                domain=email.rsplit("@", 1)[1],
                groups=(),
            )
            return AuthContext(
                principal=principal,
                tenant_id=resolve_tenant(principal, ()),
                is_admin=False,
                tenant_principal=email,
                is_schedule_tick=True,
            )

        # THE ROLLUP SWEEPER (D17), before anything else is asked, for the
        # reasons the listed path below gives: a service-account address is in
        # no Workspace domain and no group, and a failed Cloud Identity lookup
        # must not 503 a scheduled job. settings.py refuses an address that is
        # both this and anything else, so nothing below is skipped for a
        # caller that would otherwise have reached it.
        sweepers = {
            u.lower() for u in getattr(self._settings, "rollup_sweeper_users", ())
        }
        if email and email in sweepers:
            # An explicit True, as on the listed path: with the domain check
            # and both directory passes skipped, this claim is the only
            # outside confirmation of the identity left.
            if claims.get("email_verified") is not True:
                raise AuthError("the rollup sweeper requires a verified email claim")
            principal = Principal(
                email=email,
                subject=subject,
                domain=email.rsplit("@", 1)[1],
                groups=(),
            )
            return AuthContext(
                principal=principal,
                tenant_id=resolve_tenant(principal, ()),
                is_admin=False,
                tenant_principal=email,
                is_rollup_sweeper=True,
            )

        # A SERVICE ACCOUNT THE TENANT LISTS (contract request 30), resolved by
        # an exact email AND unique-id match before anything else is asked --
        # before ALLOWED_USERS and the domain check (a service-account address
        # is in no Workspace domain), and before either Cloud Identity pass (it
        # is in no group, and a failed lookup must not 503 it). getattr because
        # hand-built settings in tests predate the field, as `allowed_users`.
        listing = getattr(self._settings, "tenant_service_accounts", ())
        member = tenant_member_for(email, subject, listing)
        if member is not None:
            # The listed path requires an EXPLICIT True, not merely "not
            # False" -- the weaker rule GoogleTokenVerifier applies on the
            # bearer path elsewhere. A listed account skips both Cloud
            # Identity passes below, so this claim is the only outside
            # confirmation of the identity left. (On the IAP path this can
            # never fire: IapAssertionVerifier.verify already does
            # claims.setdefault("email_verified", True) before this code
            # runs, so this check is bearer-path-only in practice.)
            if claims.get("email_verified") is not True:
                raise AuthError("listed service account requires a verified email claim")
            principal = Principal(
                email=email,
                subject=subject,
                domain=email.rsplit("@", 1)[1],
                groups=(),
            )
            log.info("tenant member %s resolved by listing", email)
            return AuthContext(
                principal=principal,
                tenant_id=resolve_tenant(
                    principal,
                    self._settings.tenant_groups,
                    service_accounts=listing,
                ),
                is_admin=False,
                tenant_principal=member.principal,
                admin_unresolved=False,
                is_pool_admin=False,
                tenant_member=email,
                member_scope="continuation",
            )

        # ALLOWED_USERS is checked first and is purely additive. Authorisation
        # is otherwise by hosted domain, which is right for an organisation and
        # collapses for anyone without one: a single developer on a personal
        # project would have to set allowed_domains=["gmail.com"] to let
        # themselves in, which authorises every Google account on earth to run
        # agents on their billing account. The safe configuration has to be
        # expressible or the unsafe one gets used.
        #
        # The address comes from the verified token either way, so this decides
        # WHICH verified identities are admitted, never whether the identity was
        # verified.
        # THE REGISTRY of job-made workspaces (docs/design/workspace-kinds.md
        # §4.4), read here -- after the tick, the sweeper and a listed account,
        # which act for no tenant group, and before anything a group or an
        # admitted account decides. Raises RegistryUnavailable (503) when it
        # has not been read within its stale window: without it, which tenant
        # this caller belongs to is not known.
        registry = self._registry.snapshot() if self._registry is not None else EMPTY_REGISTRY

        allowed_users = {u.lower() for u in getattr(self._settings, "allowed_users", ())}
        if email and email in allowed_users:
            domain = email.rsplit("@", 1)[1] if "@" in email else ""
        elif email and self._admits_service(email, subject, registry):
            # A `ready` service record admits its account beside
            # ALLOWED_USERS, its domain taken from the address the same way,
            # and only the account whose unique id the record pins: a
            # recreated account under the same address is not it.
            # An EXPLICIT True, as on the listed path: the domain check is
            # skipped, so this claim is the only outside confirmation left.
            if claims.get("email_verified") is not True:
                raise AuthError("an admitted service account requires a verified email claim")
            domain = email.rsplit("@", 1)[1]
        else:
            try:
                domain = assert_allowed_domain(email, self._settings.core.allowed_domains)
            except AuthError as exc:
                pending = registry.pending.get(email) if email else None
                if pending is not None:
                    # Its service workspace exists and is not ready: say so,
                    # instead of the domain message. The caller's own record
                    # only, by its verified address; no email in the body.
                    log.info("service workspace not ready: workspace=%s state=%s",
                             pending.get("workspace_id") or "-", pending.get("state"))
                    raise WorkspaceNotReady(
                        f"This service account's SwarmCloud workspace is "
                        f"{pending.get('state')}, not ready, so it is not admitted yet. "
                        "An admin follows its setup in Admin › Tenants.",
                        detail={"workspace_id": pending.get("workspace_id"),
                                "state": pending.get("state")},
                    ) from None
                # Correct identity, wrong organisation: that is a 403, not a 401.
                raise Forbidden(str(exc)) from None

        # TENANT_GROUPS first, in their admin order, then the registry's
        # groups in `ready_at` order (§4.4; owner answer Q4 (a)). Every walk
        # below -- the lookup, `resolve_tenant`, the principal and the choices
        # -- uses this one tuple, so they can never disagree.
        tenant_groups = self._tenant_groups(registry)

        # TWO QUESTIONS, ASKED SEPARATELY, because they have different shapes.
        #
        # The tenant is the FIRST match in admin priority order, so a group that
        # could only ever lose to a confirmed one is irrelevant however it
        # answers -- which is exactly the rule `groups_for` applies, and why one
        # flaky group does not take down the API.
        #
        # Admin is ANY match over the whole list, so every admin group matters
        # wherever it sits. Asked together -- `tenant_groups + admin_groups`,
        # admin groups necessarily last -- the priority rule swallowed precisely
        # the failures that decide admin: a caller confirmed in `eng` whose
        # `swarm-admins` lookup failed came back as `("eng@example.com",)`, and
        # `any(g in admin_set ...)` read that silence as "not an admin". The
        # 403 that followed said "admin group membership is required for this
        # operation" to someone who has it.
        tenant_candidates = tuple(dict.fromkeys(tenant_groups))
        try:
            member_groups = (
                self._groups.groups_for(email, tenant_candidates) if tenant_candidates else ()
            )
        except GroupLookupError as exc:
            # Only raised when the failed lookup could actually have changed the
            # answer. Falling back to the personal tenant there would run the
            # caller's work under a DIFFERENT tenant: another secret, another GCS
            # prefix, another namespace, and their group cannot see the task
            # afterwards. A 503 is recoverable; a misfiled task is not.
            log.warning("tenant resolution unavailable for %s: %s", email, exc)
            raise UpstreamUnavailable(
                "group membership could not be resolved; retry shortly"
            ) from None

        admin_users = {u.lower() for u in self._settings.admin_users}
        owner = (getattr(self._settings, "platform_owner", "") or "").lower()
        is_owner = bool(owner) and email == owner
        # The `admin_roles/` documents (docs/workspaces.md §6.5). The
        # configuration is the fallback and is never skipped for them: the
        # owner and ADMIN_USERS are admins whatever Firestore says or fails to
        # say, which is what keeps admin reachable before the migration has
        # run and after admin documents are lost. A failed read is the same
        # third state as a failed group lookup -- unresolved, a 503 on an
        # admin route, never a grant and never a "no".
        role_admin = False
        roles_unresolved = False
        if self.admin_roles is not None and not is_owner and email not in admin_users:
            try:
                role_admin = self.admin_roles.holds_admin(email)
            except Exception as exc:  # noqa: BLE001 - any Firestore failure
                log.warning("admin role unresolved (%s)", type(exc).__name__)
                roles_unresolved = True
        admin_candidates = tuple(dict.fromkeys(self._settings.admin_groups))
        admin_groups_held: tuple[str, ...] = ()
        admin_unresolved = False
        # Not deduplicated against the tenant list: a group that is BOTH is the
        # case that hid best, since the tenant pass answers it at a priority
        # where a failure is discarded. The per-(caller, group) cache in
        # `CloudIdentityGroups` means asking again inside the TTL costs nothing.
        #
        # Skipped entirely for an `admin_users` caller. That escape hatch exists
        # for deployments where the Groups API cannot be read AT ALL (see
        # ApiSettings.admin_users), so making it depend on a group lookup would
        # break it in the one situation it was added for.
        #
        # NOT skipped for the owner or a Firestore admin: their
        # `principal.groups` would lose the admin groups `GET /v1/tenants/mine`
        # reports, and a failed lookup cannot hurt them -- `admin_unresolved`
        # is dropped below for anyone already an admin.
        if admin_candidates and email not in admin_users:
            try:
                admin_groups_held = self._groups.groups_for(email, admin_candidates)
            except GroupLookupError as exc:
                log.warning("admin membership unresolved for %s: %s", email, exc)
                admin_unresolved = True

        member_groups = tuple(dict.fromkeys(tuple(member_groups) + admin_groups_held))

        principal = Principal(
            email=email,
            subject=subject,
            domain=domain,
            groups=tuple(member_groups),
        )
        tenant_id = resolve_tenant(principal, tenant_groups)
        tenant_principal = self._tenant_principal(member_groups, email, tenant_groups)
        admin_set = {g.lower() for g in self._settings.admin_groups}
        # Group membership OR an explicitly named email. The second is the
        # escape hatch described on ApiSettings.admin_users: with no readable
        # groups, `admin_set` is empty and the first test can never be true,
        # so every operator screen 403s for everyone. The email is the one
        # from the verified assertion, not something the caller supplied.
        is_admin = (
            any(g.lower() in admin_set for g in member_groups)
            or email.lower() in admin_users
            or is_owner
            or role_admin
        )
        # The NARROW capability, deliberately kept out of `is_admin`: that flag
        # is read by the operator screens and by service.py's cross-tenant
        # fields, and a pool admin must switch on none of them. What it does
        # grant is decided per route, by `require_admin` against
        # POOL_ADMIN_ROUTES. getattr because hand-built settings in tests
        # predate the field, exactly as `allowed_users` above.
        admin_pool_users = {
            u.lower() for u in getattr(self._settings, "admin_pool_users", ())
        }
        ctx = AuthContext(
            principal=principal,
            tenant_id=tenant_id,
            is_admin=is_admin,
            tenant_principal=tenant_principal,
            # A confirmed membership settles the question; the doubt only
            # survives while the answer is still False.
            admin_unresolved=(admin_unresolved or roles_unresolved) and not is_admin,
            is_pool_admin=email.lower() in admin_pool_users,
            tenant_choices=self._tenant_choices(member_groups, tenant_groups),
            # Q11 (a): the token's name claim, else the email's local part.
            # `_with_default` puts a stored name in front of both.
            display_name=clean_name(claims.get("name")) or clean_name(email.split("@", 1)[0]),
            is_owner=is_owner,
        )
        # Then the platform memberships (docs/design/join-requests.md §4.5),
        # APPENDED after the confirmed groups, so they can only add: the
        # groups' order, `principal.groups` and -- unless there was no choice
        # at all -- the default tenant are exactly as before. Before the
        # reach, so a platform admin who joined a workspace by request
        # selects it as a member, as one in its group does.
        ctx = self._with_platform_memberships(ctx, tenant_groups)
        # Last, from the finished context, so it can only ADD a separate list:
        # the tenant, the choices and `principal.groups` above are exactly as
        # they were before the reach existed.
        return replace(ctx, platform_reach=self._platform_reach(ctx, tenant_groups))

    @property
    def memberships(self) -> "MembershipStore | None":
        """The membership store this authenticator reads, or None. A route
        that writes a membership writes through this same instance, so the
        write's cache eviction is the one the next request's read sees."""
        return self._memberships

    def _memberships_enabled(self) -> bool:
        # getattr because hand-built settings in tests predate the field,
        # exactly as `platform_admin_reach` above.
        return bool(getattr(self._settings, "platform_memberships", False)) \
            and self._memberships is not None

    def _with_platform_memberships(
        self, ctx: AuthContext, tenant_groups: tuple[str, ...]
    ) -> AuthContext:
        """Append the person's counted platform memberships to
        `tenant_choices` (§4.5 steps 1-2).

        NOTHING IS READ unless `platform_memberships` is on, a store is
        wired, and the caller is a person: not the tick, the sweeper, a
        listed account or any service identity (`_special`), and an address
        `memberships.is_person` accepts. Off, the context is returned as it
        came, so every request is answered exactly as before join requests.

        A membership COUNTS only for the group it names, and only while that
        group is the registered group its id selects on THIS request:
        `registered` is `_reachable_groups` over the same `tenant_groups`
        every walk in `_from_claims` used (admin order, first group per id,
        no `u-` id). A group replaced in TENANT_GROUPS, or a job-made record
        no longer `ready`, admits nobody through an old membership. The
        grant's signature, shape and newest-per-tenant rule are the store's
        (`MembershipStore.read`): a forged, replayed or overwritten grant
        never reaches this list.

        An id already among the confirmed groups is skipped: the Google
        answer wins, and the choice is not listed twice. Asks Cloud Identity
        nothing -- the read is one Firestore query per person, cached for
        the role cache's 15 s.

        When there was no choice at all (no confirmed group; the personal
        workspace is applied after this, and goes first when ready) the
        first membership becomes the default: `tenant_id` and
        `tenant_principal` are replaced TOGETHER from that entry, as
        `_with_own_workspace` replaces the frozen default, so "when it is
        non-empty its first entry IS the default tenant" still holds.

        A FAILED READ NEVER STOPS A GROUP MEMBER (§4.4): the choices are left
        as they are and `memberships_unread` is set, which `_select_tenant`
        turns into a 503 only for a header the memberships could have
        answered. So does a read that reached its bound (`cut_short`): what
        it found is offered, and the rest is unknown.
        """
        if not self._memberships_enabled() or self._special(ctx) or not is_person(ctx.email):
            return ctx
        registered = dict(self._reachable_groups(tenant_groups))
        try:
            read = self._memberships.read(ctx.email)  # type: ignore[union-attr]
        except MembershipUnavailable:
            return replace(ctx, memberships_unread=True)
        # A read that reached its bound answers the tenants it saw; any other
        # is unknown, so it is offered what it found AND marked unread.
        ctx = replace(ctx, memberships_unread=read.cut_short)
        counted = read.counted(registered)
        chosen = {tenant_id for tenant_id, _ in ctx.tenant_choices}
        joined = tuple(
            (tenant_id, group) for tenant_id, group in counted
            if tenant_id not in chosen and not tenant_id.startswith("u-")
        )
        if not joined:
            return ctx
        if not ctx.tenant_choices:
            ctx = replace(ctx, tenant_id=joined[0][0], tenant_principal=joined[0][1])
        return replace(
            ctx, tenant_choices=(*ctx.tenant_choices, *joined), platform_memberships=joined,
        )

    def _tenant_groups(self, registry: RegistrySnapshot) -> tuple[str, ...]:
        """`TENANT_GROUPS`, then every registered group not already in it."""
        configured = tuple(self._settings.tenant_groups)
        known = {g.lower() for g in configured}
        return configured + tuple(g for g in registry.groups if g.lower() not in known)

    def _admits_service(self, email: str, subject: str, registry: RegistrySnapshot) -> bool:
        """Whether a `ready` service record admits `email` with the verified
        `subject`, which must be the `unique_id` the record pins. Never a
        worker identity, by either prefix: the registry already refuses every
        `swarm-` account, and a deployment's own worker prefix is refused
        here too."""
        pinned = registry.services.get(email)
        if not pinned or subject != pinned:
            return False
        own = tenant_id_for_user(email)
        prefix = getattr(self._settings, "tenant_service_account_prefix", "")
        return not is_worker_identity(email, own, prefix)

    def _reach_enabled(self) -> bool:
        # getattr because hand-built settings in tests predate the field,
        # exactly as `allowed_users` above.
        return bool(getattr(self._settings, "platform_admin_reach", False))

    def _reachable_groups(self, tenant_groups: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        """(tenant_id, group) for every registered tenant group a platform
        admin could reach: the walk `_tenant_choices` makes -- admin order,
        the first group per id -- over ALL of `tenant_groups`
        (`_tenant_groups`: `TENANT_GROUPS`, then the registry's `ready`
        job-made GROUP records, owner decision 2026-10-19), with every
        `u-` id dropped. The registry holds no personal or service record
        among its groups, and nothing but `ready` (registry.py), so neither
        kind is ever reached through it. A group slug can land in the personal namespace
        (the frozen `tenant_id_for_group` adds no prefix), and a personal
        workspace is reachable by its own person only (§4.2.1). Dropping the
        id, rather than the group, keeps the first-per-id rule: a later group
        never stands in for a dropped one under the same id."""
        reach: dict[str, str] = {}
        for group in tenant_groups:
            reach.setdefault(tenant_id_for_group(group), group.lower())
        return tuple((t, g) for t, g in reach.items() if not t.startswith("u-"))

    def _platform_reach(
        self, ctx: AuthContext, tenant_groups: tuple[str, ...]
    ) -> tuple[tuple[str, str], ...]:
        """`AuthContext.platform_reach` for this context (§4.2.2 step 1).

        Empty unless the caller is a platform admin (`is_admin`; never
        `is_pool_admin`, the sweeper, the tick or a listed account), the
        reach is switched on, and the caller is a person. Every id already
        among the caller's memberships is left out: a member reaches it as a
        member. `tenant_groups` is the one tuple this request's memberships
        were walked over (`_tenant_groups`), so a ready job-made group is
        reached exactly as a Terraform group is, and after every one of them.
        Asks Cloud Identity nothing -- `tenant_groups` is configuration and
        the registry read this request already made -- so a request naming a
        reach workspace costs no directory call beyond the ones every request
        already makes.
        """
        if not ctx.is_admin or not self._reach_enabled() or self._special(ctx):
            return ()
        member_ids = {tenant_id for tenant_id, _ in ctx.tenant_choices}
        return tuple(
            (tenant_id, group) for tenant_id, group in self._reachable_groups(tenant_groups)
            if tenant_id not in member_ids
        )

    def holds_platform_admin(self, email: str, *, fresh: bool = False) -> bool:
        """Whether `email` is a platform admin, asked without a request.

        The same four sources `_from_claims` decides `is_admin` from:
        `PLATFORM_OWNER`, `ADMIN_USERS`, an `admin_roles/` document and an
        `ADMIN_GROUPS` membership. `fresh` reads the role document past
        `admins.AdminRoles`' cache and asks Cloud Identity past the group
        cache, for a re-check that a demotion a moment ago must already
        change (§4.2.3). A lookup that fails is a 503, never a guess either
        way, exactly as `is_tenant_member` -- unless another source has
        already said yes, which no failure can turn into a no.
        """
        email = (email or "").strip().lower()
        if not email:
            return False
        owner = (getattr(self._settings, "platform_owner", "") or "").lower()
        if owner and email == owner:
            return True
        if email in {u.lower() for u in self._settings.admin_users}:
            return True
        unresolved = False
        if self.admin_roles is not None:
            try:
                # Only a fresh question passes the keyword, so a resolver
                # written before it is called as it always was.
                held = (
                    self.admin_roles.holds_admin(email, fresh=True)
                    if fresh else self.admin_roles.holds_admin(email)
                )
            except Exception as exc:  # noqa: BLE001 - any Firestore failure
                log.warning("admin role unresolved (%s)", type(exc).__name__)
                unresolved = True
            else:
                if held:
                    return True
        admin_candidates = tuple(dict.fromkeys(self._settings.admin_groups))
        if admin_candidates:
            try:
                groups = (
                    self._groups.groups_for(email, admin_candidates, fresh=True)
                    if fresh else self._groups.groups_for(email, admin_candidates)
                )
            except GroupLookupError as exc:
                log.warning("admin membership unresolved for %s: %s", email, exc)
                unresolved = True
            else:
                admin_set = {g.lower() for g in admin_candidates}
                if any(g.lower() in admin_set for g in groups):
                    return True
        if unresolved:
            raise UpstreamUnavailable(
                "admin membership could not be resolved; retry shortly"
            )
        return False

    def _reach_excluded(self, email: str) -> bool:
        """The stored identities `_special` keeps out of the reach on a
        request: a service identity, the schedule tick, the rollup sweeper
        and a tenant's listed service account."""
        sweepers = {u.lower() for u in getattr(self._settings, "rollup_sweeper_users", ())}
        listed = {m.email.lower() for m in getattr(self._settings, "tenant_service_accounts", ())}
        return bool(
            is_service_identity(email) or email in self._schedule_tick_users
            or email in sweepers or email in listed
        )

    def may_act_in(self, email: str, tenant: Any, *, fresh: bool = False) -> str:
        """How the stored person `email` may act in `tenant` NOW, with nobody
        on the call: "member", "platform_admin" or "" (§4.2.3).

        For the paths that ask "is this person a member now?" of a stored
        owner -- a schedule's firing, an issue run's auto step, a human-step
        answer -- so a platform admin's own schedule in a group workspace they
        are not in keeps firing, as Q5 says it must.

        * "member" when `is_tenant_member` says so (`fresh` is passed on),
          at no extra cost;
        * else "platform_admin" when the tenant is a GROUP tenant, its id is
          not a `u-` id, it is the registered group that id selects
          (`_registered_reach`: a Terraform group, or a `ready` job-made
          group from the registry; a group removed from `tenant_groups`, or
          whose record is no longer `ready`, is in nobody's reach), the reach
          is on, `email` is a person, and
          `holds_platform_admin(email, fresh=True)` -- one lookup per admin
          source, asked only once membership has said no;
        * else "".

        A failed lookup is a 503 (`UpstreamUnavailable`), never a guess --
        the registry's `RegistryUnavailable` included.
        """
        if self.is_tenant_member(email, tenant, fresh=fresh):
            return "member"
        email = (email or "").strip().lower()
        if not email or not self._reach_enabled() or getattr(tenant, "kind", "") != "group":
            return ""
        tenant_id = (getattr(tenant, "tenant_id", "") or "").strip()
        principal = (getattr(tenant, "principal", "") or "").strip().lower()
        if tenant_id.startswith("u-") or self._reach_excluded(email):
            return ""
        if not self._registered_reach(tenant_id, principal):
            return ""
        return "platform_admin" if self.holds_platform_admin(email, fresh=True) else ""

    def registered_group_workspaces(self) -> dict[str, str]:
        """tenant_id -> group for every registered group workspace a platform
        admin may administer: `_reachable_groups` over `_tenant_groups`, the
        walk every request's reach makes -- `TENANT_GROUPS` in admin order,
        then the registry's `ready` job-made groups in `ready_at` order, the
        first group per id, every `u-` id dropped. Never a personal or service
        workspace, never a group whose record is not `ready` (registry.py).

        Read from the registry this authenticator already holds, so it is
        cached exactly as a request's own read is (`RegistryUnavailable`, a
        503, when it has not been read within its stale window). The admin
        role routes (`routes/roles.py`, `routes/admin.py`) read it here
        instead of each building a `WorkspaceRegistry`, whose first read per
        request would be uncached."""
        registry = self._registry.snapshot() if self._registry is not None else EMPTY_REGISTRY
        return dict(self._reachable_groups(self._tenant_groups(registry)))

    def _registered_reach(self, tenant_id: str, principal: str) -> bool:
        """Whether (tenant_id, principal) is an entry of the reach a request
        would build now: `_reachable_groups` over `_tenant_groups`, the same
        tuple `_from_claims` walks. An id a Terraform group selects is
        answered from `TENANT_GROUPS` alone -- the registry never registers
        a group under a Terraform tenant's id, and the first group per id
        wins -- so a Terraform group's answer never waits on a registry
        read. Any other id reads the registry (cached; `RegistryUnavailable`,
        a 503, when it has not been read within its stale window)."""
        entry = (tenant_id, principal)
        configured = self._reachable_groups(tuple(self._settings.tenant_groups))
        if any(t == tenant_id for t, _ in configured):
            return entry in configured
        registry = self._registry.snapshot() if self._registry is not None else EMPTY_REGISTRY
        return entry in self._reachable_groups(self._tenant_groups(registry))

    def is_tenant_member(self, email: str, tenant: Any, *, fresh: bool = False) -> bool:
        """Whether `email` belongs to `tenant` NOW, asked of the directory again.

        `fresh` skips the resolver's per-(caller, group) cache
        (`groups.CloudIdentityGroups.is_member(fresh=True)`), for a decision
        that a removal from the group a minute ago must already change: an
        await-input answer (docs/design/human-steps.md §4.2). Without it the
        answer may be up to `group_cache_ttl_seconds` old, which is right for
        the ordinary request path and nothing else.

        For work submitted on a member's behalf with nobody on the call -- an
        issue run's auto approval and its CI fix rounds (`routes.runs.
        run_owner_auth`) -- which may come long after that member was removed
        from the tenant's group (invariant 9). A group tenant asks Cloud
        Identity about its ONE stored group, the same per-group check every
        request's tenant resolution makes; a personal tenant is its own
        address. A lookup that fails is a 503, never a guess either way.

        With `platform_memberships` on, a person the group does not list is
        still a member while they hold a membership that admits them there
        (`_platform_member`, docs/design/join-requests.md §4.5 step 5), read
        fresh when `fresh` is: a schedule a platform member owns keeps firing
        until they are removed, and the removal is what stops it.
        """
        email = (email or "").strip().lower()
        if not email:
            return False
        principal = (getattr(tenant, "principal", "") or "").strip().lower()
        if getattr(tenant, "kind", "") != "group":
            return principal == email
        try:
            # Only a fresh question passes the keyword, so every resolver
            # written before it -- the tests' fakes included -- is called as
            # it always was.
            held = (
                self._groups.groups_for(email, (principal,), fresh=True)
                if fresh else self._groups.groups_for(email, (principal,))
            )
        except GroupLookupError as exc:
            log.warning("tenant membership unresolved for %s: %s", email, exc)
            raise UpstreamUnavailable(
                "group membership could not be resolved; retry shortly"
            ) from None
        if principal in {g.lower() for g in held}:
            return True
        return self._platform_member(email, tenant, principal, fresh=fresh)

    def _platform_member(self, email: str, tenant: Any, principal: str, *, fresh: bool) -> bool:
        """`is_tenant_member`'s second half (§4.5 step 5): whether a person the
        group does not list holds a membership that admits them to this
        group tenant now. Read only with the switch on, for a person
        (`is_person`, and none of the stored identities `_reach_excluded`
        keeps out), for a non-`u-` id that is the registered group its id
        selects (`_registered_reach`). `fresh` reads past the store's cache.
        A read that cannot answer raises `MembershipUnavailable`, a 503, as
        a failed group lookup does -- never a guess either way."""
        if not self._memberships_enabled() or not is_person(email) or self._reach_excluded(email):
            return False
        tenant_id = (getattr(tenant, "tenant_id", "") or "").strip()
        if not tenant_id or tenant_id.startswith("u-") or not principal:
            return False
        if not self._registered_reach(tenant_id, principal):
            return False
        return self._memberships.is_member(  # type: ignore[union-attr]
            email, tenant_id, {tenant_id: principal}, fresh=fresh)

    def _tenant_choices(
        self, member_groups: tuple[str, ...], tenant_groups: tuple[str, ...]
    ) -> tuple[tuple[str, str], ...]:
        """(tenant_id, group) for every registered tenant group in `member_groups`.

        `tenant_groups` is `TENANT_GROUPS` followed by the registry's `ready`
        job-made groups (`_tenant_groups`), so a job-made group is offered
        after every Terraform group and never before it is ready.

        The SAME walk as `_tenant_principal` and `resolve_tenant` -- admin
        order over `tenant_groups`, membership from what Cloud Identity
        confirmed -- just not stopping at the first match. Nothing is asked of
        the directory here: `member_groups` holds only confirmed memberships
        (`groups_for` drops a group whose lookup failed below a confirmed
        match), so an unanswered group is never offered.

        One entry per tenant id. The frozen `tenant_id_for_group` slugs the
        local part only, so two registered groups can share an id; the first
        in admin order is the one the id selects, which is the one the default
        rule would pick too, and listing the second would offer a choice that
        selects something else.
        """
        member_of = {g.lower() for g in member_groups}
        choices: dict[str, str] = {}
        for group in tenant_groups:
            if group.lower() in member_of:
                choices.setdefault(tenant_id_for_group(group), group.lower())
        return tuple(choices.items())

    def _select_tenant(self, ctx: AuthContext, requested: str | None) -> AuthContext:
        """Apply `X-Swarm-Tenant`: pick one of `ctx.tenant_choices`, or refuse.

        THE HEADER SELECTS; IT NEVER GRANTS (invariant 9). The only tenants it
        can name are the ones in `tenant_choices`, which this request's own
        verified identity produced: the caller's confirmed groups and, when it
        is ready, the caller's OWN personal workspace (`_with_own_workspace`),
        or a service account's own service tenant when the header named it
        (`_with_own_service_tenant`), or -- with `platform_memberships` on --
        a group workspace the person joined by an approved request
        (`_with_platform_memberships`), selected as a member, with the
        registered group as `tenant_principal` and `tenant_via` left "".
        When those memberships could not be read, any non-`u-` id outside
        the choices is 503 `membership_unavailable`, not 403.
        Everything else -- another group's tenant, another person's `u-`
        tenant, the caller's own `u-` tenant before its workspace is ready, an
        admin group, a tenant that does not exist -- is one 403, raised here,
        before any route body runs, so nothing is written.

        `tenant_id` and `tenant_principal` are replaced TOGETHER from one
        entry, so they keep describing the same tenant (`_tenant_principal`'s
        invariant) and `Store.assert_tenant_scope`/`ensure_tenant` keep their
        collision check. Nothing else changes: `is_admin`, `admin_unresolved`,
        `is_pool_admin` and `principal.groups` are about the caller, not the
        tenant, and stay exactly as resolved.

        Absent or empty: the context is returned untouched -- the default,
        which is the first entry of `tenant_choices`. Matched exactly: tenant ids are lowercase
        slugs, and a near miss is a 403 rather than a guess.

        A PLATFORM ADMIN'S REACH (docs/design/workspace-roles.md §4.2.2 step
        3) is tried only after the memberships, and only for `is_admin`: a
        match replaces `tenant_id` and `tenant_principal` together from the
        one entry, as a membership does, and sets `tenant_via` to
        "platform_admin". `platform_reach` never holds a `u-` id, so another
        person's personal workspace is the same 403 for a platform admin as
        for anyone. When the admin lookup did not answer (`admin_unresolved`)
        the answer for any other id but a `u-` one is 503, not 403: the
        unanswered lookup could have changed it, and a 403 would tell an
        operator mid-incident they lack a privilege they hold. EVERY such id,
        not only the registered ones, so that during the outage the answer
        still does not say which tenant ids exist; a `u-` id is a 403 either
        way, since no admin answer could ever select one.
        """
        if not requested:
            return ctx
        for tenant_id, group in ctx.tenant_choices:
            if tenant_id == requested:
                if tenant_id == ctx.tenant_id and group == ctx.tenant_principal:
                    return ctx
                return replace(ctx, tenant_id=tenant_id, tenant_principal=group)
        if ctx.is_admin:
            for tenant_id, group in ctx.platform_reach:
                if tenant_id == requested:
                    return replace(
                        ctx, tenant_id=tenant_id, tenant_principal=group,
                        tenant_via="platform_admin",
                    )
        if ctx.admin_unresolved and self._reach_enabled() and not self._special(ctx) and \
                not requested.startswith("u-"):
            log.info("%s unresolved for %s: admin lookup did not answer", TENANT_HEADER, ctx.email)
            raise UpstreamUnavailable(
                "admin membership could not be resolved, and it decides whether "
                f"you may select this tenant; retry shortly, or send no {TENANT_HEADER}"
            )
        if ctx.memberships_unread and not requested.startswith("u-"):
            # Any non-`u-` id, registered or not, so the outage does not say
            # which ids exist; a `u-` id is never a platform membership.
            log.info("%s unresolved for %s: memberships unread", TENANT_HEADER, ctx.email)
            raise MembershipUnavailable(
                "your workspace memberships could not be read, and they decide whether "
                f"you may select this tenant; retry shortly, or send no {TENANT_HEADER}"
            )
        # The value is caller-supplied and arbitrary; it is not logged.
        log.info("%s refused for %s: not a verified membership", TENANT_HEADER, ctx.email)
        raise TenantNotMember(
            f"{TENANT_HEADER} names a tenant you are not a verified member of. "
            "It can only select one of the tenants GET /v1/tenants/mine lists; "
            f"send no {TENANT_HEADER} to use your default tenant."
        )

    def _tenant_principal(
        self, member_groups: tuple[str, ...], email: str, tenant_groups: tuple[str, ...]
    ) -> str:
        """The group `resolve_tenant` picked, or the caller for a personal tenant.

        Mirrors `resolve_tenant`'s rule exactly -- first match in the
        ADMIN-ORDERED list -- so the principal and the tenant id can never
        describe different tenants.
        """
        member_of = {g.lower() for g in member_groups}
        for group in tenant_groups:
            if group.lower() in member_of:
                return group.lower()
        return email


#: Methods the QA account may send from any origin: they change nothing.
QA_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _claimed_email(assertion: str) -> str | None:
    """The `email` claim of a JWT-shaped assertion, UNVERIFIED, or None when it
    is not one. A pre-filter only: nothing is admitted on it."""
    import base64
    import binascii
    import json

    parts = assertion.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError, RecursionError):
        return None
    email = payload.get("email") if isinstance(payload, dict) else None
    return str(email).strip().lower() if isinstance(email, str) else ""


def qa_origin_refused(settings: Any, iap: Any, *, method: str, origin: str | None,
                      assertion: str | None) -> bool:
    """THE FRONT-DOOR GUARD (docs/design/qa-identity.md §4.3, §3.2): whether a
    request must be refused 403 `qa_identity_origin` before any route runs.

    True only for a request IAP authenticated as the configured QA account,
    with a method other than GET, HEAD or OPTIONS, whose `Origin` is absent or
    is not exactly `https://<console host>`. The second guard against cross-site
    request forgery: the gate adds the QA credential to every console-host
    request a page makes, so a page elsewhere that posts to the console would
    otherwise post as the QA account. The console's own fetches carry its
    origin, so this costs it nothing.

    The identity is the VERIFIED assertion's. A JWT-shaped assertion whose
    unverified `email` is not the QA account is passed over without a second
    verification (it cannot be the QA account: verification would only
    confirm the claim or refuse it later); any other is verified here. Every
    failure to verify is "not the QA account": authentication refuses it
    after this, with its own 401.
    """
    if (method or "").upper() in QA_SAFE_METHODS or not assertion:
        return False
    from .qa_identity import account, console_origin

    qa = account(settings)
    if not qa:
        return False
    expected = console_origin(settings)
    if expected and origin == expected:
        return False
    claimed = _claimed_email(assertion)
    if claimed is not None and claimed != qa:
        return False
    if iap is None or not getattr(iap, "configured", False):
        return False
    try:
        claims = iap.verify(assertion)
    except Exception:  # noqa: BLE001 - AuthError or any verifier failure
        return False
    return str(claims.get("email", "")).strip().lower() == qa


def person_refusal(auth: AuthContext) -> str | None:
    """Why this caller is not a PERSON, or None when it is one.

    For the routes only a person may call: an await-input answer
    (docs/design/human-steps.md §4.2 step 1, `routes.await_input`). A
    verified human ID token is the only thing that passes. Refused: any
    `*.gserviceaccount.com` principal (`gitidentity.is_service_account`) or
    other service identity (`workspaces.is_service_identity`) -- a tenant's
    worker GSA, the scheduler, a CI identity, one admitted through
    ALLOWED_USERS and confirmed in a tenant's group -- and the platform's own
    jobs and listed accounts, whatever their address: the schedule tick, the
    rollup sweeper, a listed service account (contract request 30). Admin
    status is not read: it is about the caller, and confers no exemption.

    The answer route makes ONE exception after this refuses, and decides it
    itself: a service tenant's own principal answering in that tenant
    (`awaitinput.service_principal`, owner decision 2026-10-18). This
    function's answer is unchanged by it.
    """
    email = auth.email
    if auth.is_schedule_tick or auth.is_rollup_sweeper:
        return "the platform's scheduled jobs answer for no person"
    if auth.member_scope or auth.tenant_member:
        return "a listed service account answers for no person"
    if not email or is_service_account(email) or is_service_identity(email):
        return "a service account answers for no person; only a person signed in as themselves may"
    return None


#: Every admin route a POOL ADMIN (`ApiSettings.admin_pool_users`) may call, as
#: (HTTP method, route template). Nothing else on the admin surface is
#: reachable by that caller.
#:
#: The template is the route's path IN THE ROUTER THAT DECLARES IT: an
#: `APIRouter(prefix=...)` is part of it, an `include_router(prefix=...)` or a
#: parent router's prefix is not. On the pinned FastAPI (0.141.1)
#: `include_router` keeps the original route and puts that route in
#: `scope["route"]`, which is what `deps.admin_auth` reads. main.py includes
#: every router without a prefix, so today these strings are also the public
#: URLs; test_pool_admin_is_narrow.py holds every entry, and every admin-gated
#: route, equal to a path the app publishes in its OpenAPI document, so a
#: prefix added later fails there rather than closing the gate's route.
#:
#: AN ALLOW-LIST, AND THAT IS THE DESIGN. The owner decided on 2026-09-24 that
#: the verification gate gets the runner ceiling and is NOT an admin: full
#: admin can disable any tenant (PUT /v1/admin/tenants/{id}/limits) and rewrite
#: any tenant's workflow state. A deny-list of the dangerous routes would be
#: right on the day it was written and wrong the day an admin route was added,
#: silently -- the new route would be open to the gate until somebody thought
#: to deny it. Here a new admin route is admin-only until somebody decides
#: otherwise and adds it below, where the decision is visible in review.
#: tests/unit/control_plane/test_pool_admin_is_narrow.py holds this set equal to
#: the decided one and sweeps every admin route in the routers against it.
#:
#: Only the PUT: race-test reads the pool back from Firestore, not the API, so
#: no admin GET is needed. The route takes the profile as a parameter, so the
#: capability is any runner profile's ceiling, not only mock's.
POOL_ADMIN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("PUT", "/v1/admin/limits/runner/{runner_profile}"),
    }
)


#: Every route the ROLLUP SWEEPER (`ApiSettings.rollup_sweeper_users`) may
#: call, as (HTTP method, route template) under the same rule as
#: POOL_ADMIN_ROUTES -- the declaring router's path -- and for the same reason
#: an ALLOW-LIST: a route added later is closed to the sweeper until named here.
#:
#: The one route terraform/modules/scheduler/jobs.tf (`workflow_rollup`) calls,
#: once per registered tenant: it converges the STORED Workflow.state of
#: workflows nobody reads (docs/workflows.md, "Workflow state"). Admin would
#: open every /v1/admin route to a scheduled job's identity, including
#: `PUT /v1/admin/tenants/{tenant_id}/limits`, which disables a tenant.
#: tests/unit/control_plane/test_rollup_sweeper_is_narrow.py holds this set
#: equal to the decided one and sweeps every authenticated route against it.
#:
#: And the issue-run tick (#454, terraform jobs.tf `issue_run_advance`): it
#: moves one tenant's live issue runs as far as their tasks say, which a run
#: nobody reads -- and an `auto` run's approval -- otherwise waits on for
#: ever. What it can submit is bounded by the run documents, not the caller:
#: only an `auto` run's own stored plan, in the run's own tenant, as the
#: member who created it (`routes.runs.run_owner_auth`).
#:
#: And the repository index poll (docs/repo-index.md §3.3, lane RI4, terraform
#: jobs.tf `repo_index_poll`): it reads one tenant's registered repositories'
#: heads with that tenant's token and queues an index run -- a fixed prompt
#: by profile name -- in that tenant, as the registration's creator
#: (`routes.admin.registration_owner_auth`). It is the one route here the
#: sweeper alone may call: §6.1 admits only the scheduler's identity.
ROLLUP_SWEEPER_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/admin/workflows/rollup"),
        ("POST", "/v1/admin/runs/advance"),
        ("POST", "/v1/admin/repositories/poll"),
        # The merge step's wake tick (docs/merge-step.md "Revised 2026-10-06"
        # §1, lane MS2): reads the named tenant's CI_PENDING parks with that
        # tenant's token and writes only the wake marker on them.
        ("POST", "/v1/admin/merges/wake"),
        # The GitHub user-token refresh sweep (docs/onboarding.md §3.4 item 6,
        # owner decision D2; lane OB3): the swarm-forge-refresh job. It spends
        # each due connection's refresh token and writes the new pair to that
        # user's own slots; it submits nothing and moves no task.
        ("POST", "/v1/admin/forge/refresh"),
        # The personal-workspace dispatch sweep (docs/workspaces.md §2.2, lane
        # W7 of #847): publishes again the workspace id of an `approved`
        # record whose build never claimed it. It decides nothing: only a
        # record an admin already approved is published, by its opaque id.
        ("POST", "/v1/admin/workspaces/sweep"),
        # The `task_finished` push (#748): the scheduler's wake topic's second
        # subscription. It opens a MERGE verdict's pull request with the
        # finished task's own tenant's token, for that tenant's gated
        # integrator, and moves only a step it claimed (`verdictpublish`).
        ("POST", "/v1/admin/tasks/finished"),
        # NOT the issue sweeper's route: it, its Cloud Scheduler job and
        # SWEEP_ENABLED were removed on 2026-10-12 (Schedules S9, second
        # change; docs/schedules.md §8.1 step 4). The tenant's `issue-sweep`
        # schedule replaced them, and the schedule tick, not this identity,
        # fires it.
        # The stranded-PR sweep (part of #295, jobs.tf `stranded_pr_sweep`):
        # reads one tenant's registered repositories' open pull requests with
        # that tenant's token, stores the rows and logs `pr_stranded`. It
        # submits nothing for this identity: `redrive: true` is refused unless
        # the caller is an admin (routes/strandedprs.py).
        ("POST", "/v1/admin/stranded-prs/sweep"),
        # The workspace secrets rotation sweep (#1347, owner decision Q5;
        # jobs.tf `secrets_rotation_sweep`, hourly): disables previous
        # versions whose 24-hour grace has passed, each with its audit row,
        # and never destroys one. It takes no argument, reads no value and
        # grants nothing: what it may disable is fixed by the records' own
        # `disable_after`.
        ("POST", "/v1/admin/workspace-secrets/rotation-sweep"),
        # The GitHub push hint (docs/design/graph-service.md §6.2, lane
        # GS-HOOKS of #1384). OWNER DECISION 2026-10-16, recorded on #1384:
        # the public receiver swarm-github-hooks RUNS AS this account and
        # forwards a verified push's `{forge, full_name}` here; the design's
        # own identity for it was declined. The route takes no tenant and no
        # sha, and only polls now, through the poll's own `_poll_one` and with
        # each workspace's own token, what the five-minute poll would poll
        # anyway. THE TRADE-OFF THE OWNER ACCEPTED: a public-facing service
        # now holds every route in this set, for every tenant
        # (docs/runbooks/github-app.md, "Step 9").
        ("POST", "/v1/admin/repositories/push"),
        # The per-run MCP credential sweep (docs/design/registered-mcp-
        # servers.md §4.4, owner decision Q2 of #1433; jobs.tf, lane MCP-IAM,
        # every 10 minutes). It deletes the Secret Manager secret of a
        # credential whose task has ended or whose `expires_in` has passed,
        # and marks its row. It takes no argument, reads no value, submits
        # nothing and grants nothing: what it may destroy is fixed by each
        # row's own owner and expiry, and only a `-mcpc-` secret of the row's
        # own tenant.
        ("POST", "/v1/admin/mcp-credentials/sweep"),
    }
)


#: Every route the SCHEDULE TICK (`SCHEDULE_TICK_USERS`, the
#: `swarm-schedule-tick` account, docs/schedules.md §2.1 and owner decision
#: SD10) may call, under ROLLUP_SWEEPER_ROUTES' rule and for its reason: an
#: ALLOW-LIST, so a route added later is closed to the tick until named here.
#:
#: One route. It reads due schedules ACROSS tenants -- a reach the rollup
#: sweeper's per-tenant routes do not have -- and submits nothing as itself:
#: every firing is submitted as the schedule's stored owner, in the
#: schedule's stored tenant, while the directory still says they are a member
#: (`schedulefire.schedule_owner_auth`). The route also refuses an admin
#: (`routes.schedule_tick`), so this list is the whole of who may tick.
#: tests/unit/control_plane/test_schedule_tick.py holds this set to the
#: decided one and sweeps every authenticated route against it.
SCHEDULE_TICK_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/admin/schedules/tick"),
    }
)

#: The environment variable lane S13 renders into swarm-api's environment
#: (terraform: `swarm-schedule-tick`'s email). Read by `schedule_tick_users`.
SCHEDULE_TICK_USERS_ENV = "SCHEDULE_TICK_USERS"


def schedule_tick_users(
    settings: Any, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The addresses admitted as the schedule tick, lower-cased.

    `ApiSettings.schedule_tick_users` when settings carries it, else
    `SCHEDULE_TICK_USERS` from the environment, comma-separated. settings.py
    is not this lane's file (docs/schedules.md §9, S2), so the variable is
    read here until a settings field exists; the field, once added, wins.

    UNSET IS NOBODY. With no value the tick route admits no caller at all --
    not an admin, not the rollup sweeper -- so before S13 renders the
    variable, the tick is closed rather than open to whoever else can reach
    /v1/admin (`routes.schedule_tick` says so in its 403).

    An address that is also on ROLLUP_SWEEPER_USERS, ADMIN_USERS,
    ADMIN_POOL_USERS, ALLOWED_USERS or PLATFORM_OWNER is refused at start,
    for settings.py's reason: two roles on one address make which applies
    depend on the order this module asks. A service-account address only:
    a person's address here would let that person tick every tenant.
    """
    configured = getattr(settings, "schedule_tick_users", None)
    if configured is None:
        environ = os.environ if environ is None else environ
        configured = environ.get(SCHEDULE_TICK_USERS_ENV, "").split(",")
    users = tuple(dict.fromkeys(u.strip().lower() for u in configured if u and u.strip()))
    for user in users:
        if "@" not in user or not user.endswith(".gserviceaccount.com"):
            raise ValueError(
                f"{SCHEDULE_TICK_USERS_ENV} names {user!r}; it takes the schedule tick's "
                "service-account address only"
            )
    owner = (getattr(settings, "platform_owner", "") or "").strip().lower()
    for name, values in (
        ("ROLLUP_SWEEPER_USERS", getattr(settings, "rollup_sweeper_users", ())),
        ("ADMIN_USERS", getattr(settings, "admin_users", ())),
        ("ADMIN_POOL_USERS", getattr(settings, "admin_pool_users", ())),
        ("ALLOWED_USERS", getattr(settings, "allowed_users", ())),
        ("PLATFORM_OWNER", (owner,) if owner else ()),
    ):
        overlap = sorted(set(users) & {v.strip().lower() for v in values})
        if overlap:
            raise ValueError(
                f"{SCHEDULE_TICK_USERS_ENV} and {name} both name {', '.join(overlap)}; "
                "the schedule tick may call one route and is no other kind of caller"
            )
    return users


#: Every route a CONTINUATION-SCOPED account (member_scope="continuation") may
#: call, as (HTTP method, route template) -- an ALLOW-LIST, same reasoning as
#: POOL_ADMIN_ROUTES: a route defaults to CLOSED for this scope until named
#: here, where opening it is a decision visible in review. The templates follow
#: the same rule as POOL_ADMIN_ROUTES (the declaring router's path, its own
#: prefix included). tests/unit/control_plane/test_continuation_scope_is_narrow.py
#: holds this set equal to the decided one and sweeps every route in every
#: router against it, the same shape as test_pool_admin_is_narrow.py.
#:
#: Being here opens the ROUTE, not every request to it: `POST /v1/workflows`
#: still refuses this scope without `continues_task` (service.py), and every
#: task and workflow read is filtered to what the caller submitted
#: (`deps.submission_scope` -> `Store`'s required `submitted_by`).
CONTINUATION_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/workflows"),  # only WITH continues_task -- enforced in service.py
    ("GET", "/v1/tasks"),
    ("GET", "/v1/tasks/{task_id}"),
    ("GET", "/v1/tasks/{task_id}/events"),
    ("GET", "/v1/tasks/{task_id}/attempts"),
    ("GET", "/v1/tasks/{task_id}/artifacts"),
    ("GET", "/v1/tasks/{task_id}/artifacts/content"),
    ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
    ("GET", "/v1/tasks/{task_id}/checkpoints"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path:path}"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content"),
    ("GET", "/v1/tasks/{task_id}/logs"),
    ("GET", "/v1/tasks/{task_id}/transcript"),
    ("GET", "/v1/tasks/{task_id}/answer"),
    ("GET", "/v1/tasks/{task_id}/input"),
    ("GET", "/v1/workflows"),
    ("GET", "/v1/workflows/{workflow_id}"),
    ("GET", "/v1/resource-classes"),  # static catalogue, no tenant data
    ("GET", "/v1/runtimes"),          # static catalogue, no tenant data
})


def require_continuation_route(
    ctx: AuthContext, route: tuple[str, str] | None
) -> AuthContext:
    """A continuation-scoped caller may reach only CONTINUATION_ROUTES, the
    rollup sweeper only ROLLUP_SWEEPER_ROUTES, and the schedule tick only
    SCHEDULE_TICK_ROUTES.

    The per-route default-deny `deps.current_auth` applies to EVERY
    authenticated request, which is why the sweeper's is here too: a route
    that is not admin-gated never reaches `require_admin`, so that gate alone
    would leave the sweeper an ordinary member everywhere else.

    An ordinary member (`member_scope == ""`, not the sweeper) passes, unless
    `require_known_default` refuses the request: this is the per-route gate
    every authenticated request reaches (`deps.current_auth`, the live
    socket), which is why that check is made here too. `route=None` (no route
    matched) fails closed, same as `require_admin`.
    """
    if ctx.is_schedule_tick:
        if route is not None and route in SCHEDULE_TICK_ROUTES:
            return ctx
        method, path = route if route else ("?", "unmatched")
        raise Forbidden(f"the schedule tick may not call {method} {path}")
    if ctx.is_rollup_sweeper:
        if route is not None and route in ROLLUP_SWEEPER_ROUTES:
            return ctx
        method, path = route if route else ("?", "unmatched")
        raise Forbidden(f"the rollup sweeper may not call {method} {path}")
    if not ctx.member_scope:
        return require_known_default(ctx, route)
    if route is not None and route in CONTINUATION_ROUTES:
        return ctx
    method, path = route if route else ("?", "unmatched")
    raise Forbidden(f"a continuation-scoped account may not call {method} {path}")


def require_known_default(
    ctx: AuthContext, route: tuple[str, str] | None
) -> AuthContext:
    """A request that names no workspace, whose default workspace could not
    be read, is 503 `preferences_unavailable` (docs/design/user-profile.md
    §4.5, Q9) on every route outside `PREFERENCE_FREE_ROUTES`.

    Raised before the route body runs, so nothing is submitted, read in a
    guessed workspace or queued. The message says how to name a workspace:
    the console's switcher, SWARM_TENANT or the plugin's tenant option, or
    `--tenant`. It never falls back to the computed default, for the reason
    `_with_own_workspace` gives a failed personal-workspace read: that is
    the misfiling the default exists to prevent. `route=None` (no route
    matched) is not on the list, so it fails closed.
    """
    if ctx.default_unknown and (route is None or route not in PREFERENCE_FREE_ROUTES):
        if ctx.memberships_unread:
            # The default is unknown because the platform memberships were
            # not read (`_with_unread_memberships`), whatever the preferences
            # said: name the read that failed.
            raise MembershipUnavailable(
                "your workspace memberships could not be read, and one may be your "
                f"default workspace; retry shortly, or name a workspace with {TENANT_HEADER}")
        raise PreferencesUnavailable()
    return ctx


def require_admin(
    ctx: AuthContext, route: tuple[str, str] | None = None
) -> AuthContext:
    """The admin gate, and the difference between "no" and "I could not ask".

    `route` is the (method, route template) being called, which is what lets a
    pool admin through on the routes in POOL_ADMIN_ROUTES and nowhere else. A
    caller that passes no route gets the full-admin rule and nothing more, so
    forgetting it fails closed.

    503, not 403, when Cloud Identity did not answer. A 403 here is a statement
    about the CALLER -- "you are not an admin" -- and an operator who is one
    reads it as a permissions problem: they go and check the group, the IAM
    bindings, ADMIN_GROUPS in the deployment. None of that is wrong, so the
    search ends nowhere while the directory quietly recovers. A 503 names the
    dependency, is retryable, and is the same answer tenant resolution already
    gives for the same outage one question earlier.

    This only ever widens what a caller can do after a RETRY; it never grants
    anything, because the 503 path returns no context at all.
    """
    if ctx.is_admin:
        return ctx
    # BEFORE the unresolved 503, deliberately. The pass depends only on the
    # verified email and the route, not on the question Cloud Identity failed
    # to answer -- and for the gate that lookup is the likely one to fail: it
    # is not on admin_users, so every request asks the directory about a
    # service-account address. Swapped, race-test 503s at step 1 whenever the
    # directory does not answer. Pinned in test_pool_admin_is_narrow.py by
    # test_a_pool_admin_whose_admin_lookup_failed_can_still_narrow_the_pool.
    if ctx.is_pool_admin and route is not None and route in POOL_ADMIN_ROUTES:
        return ctx
    if ctx.is_schedule_tick:
        if route is not None and route in SCHEDULE_TICK_ROUTES:
            return ctx
        method, path = route if route else ("?", "unmatched")
        raise Forbidden(f"the schedule tick may not call {method} {path}")
    if ctx.is_rollup_sweeper:
        if route is not None and route in ROLLUP_SWEEPER_ROUTES:
            return ctx
        method, path = route if route else ("?", "unmatched")
        raise Forbidden(f"the rollup sweeper may not call {method} {path}")
    if ctx.admin_unresolved:
        raise UpstreamUnavailable(
            "admin group membership could not be resolved; retry shortly. This is "
            "NOT a refusal -- Cloud Identity did not answer, so whether you are an "
            "admin is unknown"
        )
    if ctx.is_pool_admin:
        # Said plainly, so the gate's operator does not go looking for a
        # broken grant: the grant works, and this route is not in it. Built
        # from the allow-list so it cannot go stale when the list changes.
        granted = ", ".join(f"{method} {path}" for method, path in sorted(POOL_ADMIN_ROUTES))
        raise Forbidden(
            "this route is not in the ADMIN_POOL_USERS allow-list "
            f"({granted}); it needs an admin (an admin role, ADMIN_GROUPS membership "
            "or ADMIN_USERS)"
        )
    raise Forbidden("admin group membership is required for this operation")
