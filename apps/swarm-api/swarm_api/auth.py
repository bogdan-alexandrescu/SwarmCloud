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

from .errors import Forbidden, Unauthenticated
from .groups import MembershipResolver
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

    @property
    def email(self) -> str:
        return self.principal.email


class TokenVerifier(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...


class GoogleTokenVerifier:
    """Verifies a Google-issued ID token against Google's public keys."""

    def __init__(self, audience: str = "") -> None:
        self._audience = audience or None
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


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise Unauthenticated("missing Authorization header")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        # Deliberately does not echo the header value.
        raise Unauthenticated("Authorization header must be 'Bearer <google id token>'")
    return value.strip()


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
        token = bearer_token(authorization)
        try:
            claims = self._verifier.verify(token)
        except AuthError as exc:
            raise Unauthenticated(str(exc)) from None

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
        member_groups = self._groups.groups_for(email, candidates) if candidates else ()

        principal = Principal(
            email=email,
            subject=subject,
            domain=domain,
            groups=tuple(member_groups),
        )
        tenant_id = resolve_tenant(principal, self._settings.tenant_groups)
        admin_set = {g.lower() for g in self._settings.admin_groups}
        is_admin = any(g.lower() in admin_set for g in member_groups)
        return AuthContext(principal=principal, tenant_id=tenant_id, is_admin=is_admin)


def require_admin(ctx: AuthContext) -> AuthContext:
    if not ctx.is_admin:
        raise Forbidden("admin group membership is required for this operation")
    return ctx
