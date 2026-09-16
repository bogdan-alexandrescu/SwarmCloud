"""Subscription credential refresh, with exactly one writer.

A Claude subscription authenticates with a short-lived ACCESS token and a
long-lived REFRESH token, the same pair Claude Code keeps in the macOS keychain
under `Claude Code-credentials`. The access token expires; the refresh token is
exchanged at the OAuth token endpoint for a new one.

Why this lives in the quota broker and not in the worker
-------------------------------------------------------
Refresh tokens ROTATE: an exchange can return a new refresh token and invalidate
the one that was presented. That makes refreshing from the worker actively
unsafe in a swarm:

  * N workers running concurrently would each present the same refresh token and
    each invalidate the others, so most attempts fail and the credential ends up
    in an indeterminate state;
  * a worker that refreshed successfully but died before persisting the rotated
    token would strand the tenant with a refresh token nobody holds.

So there is ONE writer. The broker refreshes on its scheduled sweep, persists
the rotated refresh token BEFORE publishing the access token, and workers only
ever receive an access token they cannot refresh. A worker cannot leak a refresh
token it never had.

    swarm-tenant-<id>-<provider>-refresh   long-lived, written back on rotation
    swarm-tenant-<id>-<provider>           short-lived access token, what the
                                           worker mounts (unchanged from before)

Ordering is the safety property. If the access token were published first and
the process died, the rotated refresh token would be lost and the tenant locked
out until a human re-authenticated. Persist the means of getting more credentials
before the credentials themselves.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

#: Claude Code's public OAuth client. Not a secret -- a public client identifier,
#: which is why the flow is PKCE-based and the refresh token is what matters.
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"

#: Refresh this far before expiry. An access token that expires mid-attempt
#: fails the attempt, and attempts run for up to two hours.
DEFAULT_REFRESH_WINDOW = timedelta(hours=3)


class CredentialError(RuntimeError):
    """Refresh failed. The message NEVER contains token material."""


class ReauthRequired(CredentialError):
    """The refresh token is dead. Only a human can fix this.

    Raised for `invalid_grant`, which means the token was revoked, expired, or
    already rotated by somebody else. Retrying cannot help, so callers must stop
    rather than spin: tasks park, and an operator re-runs `claude setup-token`.
    """


@dataclass(frozen=True)
class Credential:
    access_token: str
    refresh_token: str
    expires_at: datetime

    def needs_refresh(self, now: datetime, window: timedelta = DEFAULT_REFRESH_WINDOW) -> bool:
        return now + window >= self.expires_at

    def redacted(self) -> dict[str, Any]:
        """Safe to log: shape and timing only, never material."""
        return {
            "expires_at": self.expires_at.isoformat(),
            "access_token_len": len(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
        }


class TokenEndpoint(Protocol):
    def exchange(self, refresh_token: str) -> dict[str, Any]: ...


class HttpTokenEndpoint:
    """The real endpoint. Injected so tests never make a network call."""

    def __init__(self, url: str = TOKEN_ENDPOINT, client_id: str = CLAUDE_CODE_CLIENT_ID) -> None:
        self._url = url
        self._client_id = client_id

    def exchange(self, refresh_token: str) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            # `invalid_grant` is terminal: the token is gone and retrying will
            # never succeed. Anything else may be transient.
            if "invalid_grant" in detail or exc.code in (400, 401):
                raise ReauthRequired(
                    f"the token endpoint rejected the refresh token ({exc.code}); "
                    "a human must re-authenticate"
                ) from None
            raise CredentialError(f"token endpoint returned {exc.code}") from None
        except urllib.error.URLError as exc:
            raise CredentialError(f"could not reach the token endpoint: {exc.reason}") from None


def parse_credential(payload: str, *, now: datetime | None = None) -> Credential:
    """Read a stored credential.

    Accepts the shape Claude Code itself keeps in the keychain -- camelCase
    `accessToken`/`refreshToken`/`expiresAt` -- because that is what an operator
    will paste, and the snake_case shape the token endpoint returns. Anything
    else is a configuration error, not something to guess at.
    """
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise CredentialError(
            "the stored credential is not JSON; a subscription credential needs "
            "accessToken, refreshToken and expiresAt"
        ) from None
    if not isinstance(data, dict):
        raise CredentialError("the stored credential is not a JSON object")

    access = data.get("accessToken") or data.get("access_token") or ""
    refresh = data.get("refreshToken") or data.get("refresh_token") or ""
    if not refresh:
        raise CredentialError("the stored credential has no refresh token")

    expires_at = _expiry(data, now)
    return Credential(access_token=access, refresh_token=refresh, expires_at=expires_at)


def _expiry(data: dict[str, Any], now: datetime) -> datetime:
    raw = data.get("expiresAt", data.get("expires_at"))
    if isinstance(raw, (int, float)):
        # Claude Code stores epoch MILLISECONDS. A value that looks like seconds
        # would otherwise land in 1970 and make every credential look expired.
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise CredentialError("expiresAt is not a timestamp this can read") from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if "expires_in" in data:
        return now + timedelta(seconds=int(data["expires_in"]))
    # No expiry at all: treat as already due, so the first sweep refreshes it
    # rather than handing a worker a token of unknown age.
    return now


def refresh(
    credential: Credential,
    endpoint: TokenEndpoint,
    *,
    now: datetime | None = None,
) -> Credential:
    """Exchange the refresh token. Returns the NEW credential.

    The endpoint may or may not rotate the refresh token. When it does not, the
    presented one is carried forward -- dropping it would strand the tenant on
    the next refresh.
    """
    now = now or datetime.now(timezone.utc)
    result = endpoint.exchange(credential.refresh_token)
    if not isinstance(result, dict):
        raise CredentialError("the token endpoint returned an unexpected shape")

    access = result.get("access_token") or ""
    if not access:
        raise CredentialError("the token endpoint returned no access_token")

    expires_in = result.get("expires_in")
    expires_at = now + timedelta(seconds=int(expires_in)) if expires_in else now + timedelta(hours=8)

    return Credential(
        access_token=access,
        # Carry the old one forward when the endpoint does not rotate.
        refresh_token=result.get("refresh_token") or credential.refresh_token,
        expires_at=expires_at,
    )


def serialise(credential: Credential) -> str:
    """The canonical stored form, in the shape Claude Code uses."""
    return json.dumps(
        {
            "accessToken": credential.access_token,
            "refreshToken": credential.refresh_token,
            "expiresAt": int(credential.expires_at.timestamp() * 1000),
        }
    )
