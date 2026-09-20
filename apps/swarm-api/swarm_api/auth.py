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
from dataclasses import dataclass
from typing import Any, Protocol

from swarm_common.identity import (
    AuthError,
    Principal,
    assert_allowed_domain,
    resolve_tenant,
)

from .errors import Forbidden, Unauthenticated, UpstreamUnavailable
from .groups import GroupLookupError, MembershipResolver
from .settings import ApiSettings

log = logging.getLogger(__name__)


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
        self, authorization: str | None, iap_assertion: str | None = None
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

        return self._from_claims(claims)

    def _from_claims(self, claims: dict[str, Any]) -> AuthContext:
        """Verified claims -> AuthContext, whatever verified them.

        Shared by the bearer and IAP paths deliberately. Tenant resolution,
        domain enforcement, group membership and the admin decision are the part
        that must NOT differ by how the caller arrived -- two copies of this
        would be two tenant boundaries, and CONTRACT.md invariant 9 depends on
        there being one.
        """
        email = str(claims.get("email", "")).lower()
        subject = str(claims.get("sub", ""))
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

        candidates = tuple(
            dict.fromkeys(self._settings.tenant_groups + self._settings.admin_groups)
        )
        try:
            member_groups = self._groups.groups_for(email, candidates) if candidates else ()
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
        admin_users = {u.lower() for u in self._settings.admin_users}
        is_admin = (
            any(g.lower() in admin_set for g in member_groups)
            or email.lower() in admin_users
        )
        return AuthContext(
            principal=principal,
            tenant_id=tenant_id,
            is_admin=is_admin,
            tenant_principal=tenant_principal,
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


def require_admin(ctx: AuthContext) -> AuthContext:
    if not ctx.is_admin:
        raise Forbidden("admin group membership is required for this operation")
    return ctx
