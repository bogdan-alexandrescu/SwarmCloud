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
from dataclasses import dataclass, replace
from typing import Any, Protocol

from swarm_common.identity import (
    AuthError,
    Principal,
    assert_allowed_domain,
    resolve_tenant,
    tenant_id_for_group,
    tenant_member_for,
)

from .errors import Forbidden, Unauthenticated, UpstreamUnavailable
from .groups import GroupLookupError, MembershipResolver
from .settings import ApiSettings

log = logging.getLogger(__name__)


#: The request header a caller uses to pick ONE of its verified tenant
#: memberships (the tenant switcher, owner decision 2026-10-01). It SELECTS and
#: never grants: `Authenticator._select_tenant` honours it only when the named
#: tenant is among the registered tenant groups Cloud Identity confirmed for
#: this caller on this request. Absent (or empty), the tenant is today's first
#: match in the admin-ordered `tenant_groups`.
TENANT_HEADER = "X-Swarm-Tenant"

#: The same selection as a QUERY parameter, accepted on `TENANT_QUERY_ROUTES`
#: only. Those are the routes a browser fetches by itself -- an `<img src>`, a
#: download `href`, an "open full" tab -- and a browser-initiated request
#: carries no custom header, so without this a console switched to a
#: non-default tenant would 404 every artifact of the tasks it lists. It is
#: validated by exactly the same `Authenticator._select_tenant` as the header:
#: it selects among the caller's verified memberships and never grants one.
TENANT_QUERY = "tenant"
TENANT_QUERY_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
})


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
    #: is the group email (`eng@saga.xyz`); for the personal fallback it is the
    #: user's own address. `tenant_id` alone cannot identify a tenant, because
    #: the frozen `tenant_id_for_group` slugs the local part only: `eng@saga.xyz`
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
    #: Every registered tenant group Cloud Identity CONFIRMED this caller is in,
    #: as (tenant_id, group email), in the admin order of `tenant_groups` --
    #: what `GET /v1/tenants/mine` lists and the only values `X-Swarm-Tenant`
    #: may select. Derived from the same `member_groups` the default tenant is
    #: resolved from, so the list and the default can never disagree: when it
    #: is non-empty its first entry IS the default tenant. Empty for a
    #: personal tenant and for a listed service account.
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


class Authenticator:
    """Turns an Authorization header into an AuthContext."""

    def __init__(
        self,
        settings: ApiSettings,
        verifier: TokenVerifier,
        groups: MembershipResolver,
        iap: Any = None,
    ) -> None:
        self._settings = settings
        self._verifier = verifier
        self._groups = groups
        self._iap = iap

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
        """
        return self._select_tenant(
            self._authenticate(authorization, iap_assertion), tenant
        )

    def _authenticate(
        self, authorization: str | None, iap_assertion: str | None
    ) -> AuthContext:
        # IAP FIRST, when it is present and configured. Behind the load balancer
        # there is no Authorization header to fall back to -- a browser has no
        # Google ID token to mint -- so this is the only credential a web caller
        # ever has. A direct caller from inside the VPC still presents a bearer
        # and takes the path below.
        if iap_assertion and self._iap is not None and self._iap.configured:
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
        allowed_users = {u.lower() for u in getattr(self._settings, "allowed_users", ())}
        if email and email in allowed_users:
            domain = email.rsplit("@", 1)[1] if "@" in email else ""
        else:
            try:
                domain = assert_allowed_domain(email, self._settings.core.allowed_domains)
            except AuthError as exc:
                # Correct identity, wrong organisation: that is a 403, not a 401.
                raise Forbidden(str(exc)) from None

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
        # `swarm-admins` lookup failed came back as `("eng@saga.xyz",)`, and
        # `any(g in admin_set ...)` read that silence as "not an admin". The
        # 403 that followed said "admin group membership is required for this
        # operation" to someone who has it.
        tenant_candidates = tuple(dict.fromkeys(self._settings.tenant_groups))
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
        tenant_id = resolve_tenant(principal, self._settings.tenant_groups)
        tenant_principal = self._tenant_principal(member_groups, email)
        admin_set = {g.lower() for g in self._settings.admin_groups}
        # Group membership OR an explicitly named email. The second is the
        # escape hatch described on ApiSettings.admin_users: with no readable
        # groups, `admin_set` is empty and the first test can never be true,
        # so every operator screen 403s for everyone. The email is the one
        # from the verified assertion, not something the caller supplied.
        is_admin = (
            any(g.lower() in admin_set for g in member_groups)
            or email.lower() in admin_users
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
        return AuthContext(
            principal=principal,
            tenant_id=tenant_id,
            is_admin=is_admin,
            tenant_principal=tenant_principal,
            # A confirmed membership settles the question; the doubt only
            # survives while the answer is still False.
            admin_unresolved=admin_unresolved and not is_admin,
            is_pool_admin=email.lower() in admin_pool_users,
            tenant_choices=self._tenant_choices(member_groups),
        )

    def is_tenant_member(self, email: str, tenant: Any) -> bool:
        """Whether `email` belongs to `tenant` NOW, asked of the directory again.

        For work submitted on a member's behalf with nobody on the call -- an
        issue run's auto approval and its CI fix rounds (`routes.runs.
        run_owner_auth`) -- which may come long after that member was removed
        from the tenant's group (invariant 9). A group tenant asks Cloud
        Identity about its ONE stored group, the same per-group check every
        request's tenant resolution makes; a personal tenant is its own
        address. A lookup that fails is a 503, never a guess either way.
        """
        email = (email or "").strip().lower()
        if not email:
            return False
        principal = (getattr(tenant, "principal", "") or "").strip().lower()
        if getattr(tenant, "kind", "") != "group":
            return principal == email
        try:
            held = self._groups.groups_for(email, (principal,))
        except GroupLookupError as exc:
            log.warning("tenant membership unresolved for %s: %s", email, exc)
            raise UpstreamUnavailable(
                "group membership could not be resolved; retry shortly"
            ) from None
        return principal in {g.lower() for g in held}

    def _tenant_choices(self, member_groups: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        """(tenant_id, group) for every registered tenant group in `member_groups`.

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
        for group in self._settings.tenant_groups:
            if group.lower() in member_of:
                choices.setdefault(tenant_id_for_group(group), group.lower())
        return tuple(choices.items())

    def _select_tenant(self, ctx: AuthContext, requested: str | None) -> AuthContext:
        """Apply `X-Swarm-Tenant`: pick one of `ctx.tenant_choices`, or refuse.

        THE HEADER SELECTS; IT NEVER GRANTS (invariant 9). The only tenants it
        can name are the ones in `tenant_choices`, which this request's own
        verified identity produced. Everything else -- another group's tenant,
        a personal `u-` tenant (the caller's own included), an admin group, a
        tenant that does not exist -- is one 403, raised here, before any route
        body runs, so nothing is read and nothing is written.

        `tenant_id` and `tenant_principal` are replaced TOGETHER from one
        entry, so they keep describing the same tenant (`_tenant_principal`'s
        invariant) and `Store.assert_tenant_scope`/`ensure_tenant` keep their
        collision check. Nothing else changes: `is_admin`, `admin_unresolved`,
        `is_pool_admin` and `principal.groups` are about the caller, not the
        tenant, and stay exactly as resolved.

        Absent or empty: the context is returned untouched -- today's
        first-match, byte for byte. Matched exactly: tenant ids are lowercase
        slugs, and a near miss is a 403 rather than a guess.
        """
        if not requested:
            return ctx
        for tenant_id, group in ctx.tenant_choices:
            if tenant_id == requested:
                if tenant_id == ctx.tenant_id and group == ctx.tenant_principal:
                    return ctx
                return replace(ctx, tenant_id=tenant_id, tenant_principal=group)
        # The value is caller-supplied and arbitrary; it is not logged.
        log.info("%s refused for %s: not a verified membership", TENANT_HEADER, ctx.email)
        raise TenantNotMember(
            f"{TENANT_HEADER} names a tenant you are not a verified member of. "
            "It can only select one of the tenants GET /v1/tenants/mine lists; "
            f"send no {TENANT_HEADER} to use your default tenant."
        )

    def _tenant_principal(self, member_groups: tuple[str, ...], email: str) -> str:
        """The group `resolve_tenant` picked, or the caller for a personal tenant.

        Mirrors `resolve_tenant`'s rule exactly -- first match in the
        ADMIN-ORDERED list -- so the principal and the tenant id can never
        describe different tenants.
        """
        member_of = {g.lower() for g in member_groups}
        for group in self._settings.tenant_groups:
            if group.lower() in member_of:
                return group.lower()
        return email


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
ROLLUP_SWEEPER_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/admin/workflows/rollup"),
        ("POST", "/v1/admin/runs/advance"),
    }
)


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
    """A continuation-scoped caller may reach only CONTINUATION_ROUTES, and
    the rollup sweeper only ROLLUP_SWEEPER_ROUTES.

    The per-route default-deny `deps.current_auth` applies to EVERY
    authenticated request, which is why the sweeper's is here too: a route
    that is not admin-gated never reaches `require_admin`, so that gate alone
    would leave the sweeper an ordinary member everywhere else.

    An ordinary member (`member_scope == ""`, not the sweeper) returns
    immediately. `route=None` (no route matched) fails closed, same as
    `require_admin`.
    """
    if ctx.is_rollup_sweeper:
        if route is not None and route in ROLLUP_SWEEPER_ROUTES:
            return ctx
        method, path = route if route else ("?", "unmatched")
        raise Forbidden(f"the rollup sweeper may not call {method} {path}")
    if not ctx.member_scope:
        return ctx
    if route is not None and route in CONTINUATION_ROUTES:
        return ctx
    method, path = route if route else ("?", "unmatched")
    raise Forbidden(f"a continuation-scoped account may not call {method} {path}")


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
            f"({granted}); it needs an admin (ADMIN_GROUPS membership or ADMIN_USERS)"
        )
    raise Forbidden("admin group membership is required for this operation")
