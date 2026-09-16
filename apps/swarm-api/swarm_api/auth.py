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
    ) -> None:
        self._settings = settings
        self._verifier = verifier
        self._groups = groups

    def authenticate(self, authorization: str | None) -> AuthContext:
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

        email = str(claims.get("email", "")).lower()
        subject = str(claims.get("sub", ""))
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
        is_admin = any(g.lower() in admin_set for g in member_groups)
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
