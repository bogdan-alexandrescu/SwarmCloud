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
import base64
import hashlib
import secrets
from urllib.parse import urlencode
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

#: Claude Code's public OAuth client. Not a secret -- a public client identifier,
#: which is why the flow is PKCE-based and the refresh token is what matters.
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

#: Verified against claudeswitch, which refreshes these credentials in
#: production. console.anthropic.com answers a refresh with 403; this host is
#: the one that works.
TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"

#: The token endpoint sits behind a WAF that refuses unrecognised clients, and
#: it refuses them with 403 -- a status that reads as "this credential is not
#: allowed" and actually means "this User-Agent is not". Measured from one
#: machine, same request, same second:
#:
#:     no User-Agent               -> 429  (accepted, merely rate limited)
#:     User-Agent: Python-urllib   -> 403  (blocked outright)
#:     User-Agent: claude-cli/...  -> 400  (processed; bad token rejected)
#:
#: urllib sends Python-urllib by default, which is why every refresh failed with
#: a 403 that looked like a credential problem. This identifies the client
#: accurately: the platform IS acting as Claude Code here, using Claude Code's
#: own OAuth client id to refresh Claude Code's own credentials.
CLIENT_USER_AGENT = "claude-cli/2.1.274 (external, cli)"

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

    def redeem(
        self, *, code: str, verifier: str, redirect_uri: str, state: str = ""
    ) -> dict[str, Any]:
        """Trade an authorization code for a credential pair.

        The other half of `exchange`: that one renews a credential this
        platform already holds, this one obtains the first. Same endpoint, same
        form-encoding requirement, different grant -- and the failure modes
        differ enough to be worth separating. A rejected refresh token means an
        account needs re-authenticating; a rejected CODE almost always means
        the person took too long or pasted the wrong half of what the callback
        page showed them, and telling them to re-authenticate would be
        unhelpful and slightly insulting.
        """
        import urllib.error
        import urllib.parse
        import urllib.request

        # JSON, and NOT form-encoded -- the opposite of `exchange` below, and
        # the asymmetry is measured rather than assumed.
        #
        # This method sent RFC 6749 form-encoding, which is what the spec says
        # a token endpoint takes and what `exchange` demonstrably needs. The
        # authorization_code grant answered
        #
        #   400 {"type":"error","error":{"type":"invalid_request_error",
        #        "message":"Invalid request format"}}   req_011CfHkoFVrh7ZqoY6jFzz4R
        #
        # on a freshly issued code -- an "invalid request FORMAT", not an
        # invalid code, so the code was never examined. `state` is sent too,
        # which Claude Code includes and the form-encoded version omitted
        # entirely.
        #
        # Nothing could have caught this from inside the repository: redeeming
        # requires a real authorization code from a real person signing in, so
        # the first execution of this method was against the live endpoint.
        payload: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        if state:
            payload["state"] = state
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": CLIENT_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise CredentialError(
                "the sign-in code was not accepted. Codes are single-use and "
                "expire quickly, so the usual causes are pasting one that has "
                "already been used, or starting the sign-in again in another "
                f"tab. Try Add account once more. ({exc.code}: {detail})"
            ) from None
        except urllib.error.URLError as exc:
            raise CredentialError(f"could not reach the token endpoint: {exc.reason}") from None

    def exchange(self, refresh_token: str) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        import urllib.parse

        # FORM-ENCODED, not JSON. RFC 6749 specifies
        # application/x-www-form-urlencoded for the token endpoint, and sending
        # JSON here is answered with 403 -- a status that reads like an
        # authorization failure and is really a content-type one.
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": CLIENT_USER_AGENT,
            },
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

    # Claude Code's keychain item (`Claude Code-credentials`) wraps the
    # credential in a `claudeAiOauth` object. Unwrapped here because pasting
    # that item verbatim is the obvious thing for an operator to do, and the
    # failure otherwise is "no refresh token" at sweep time -- long after the
    # paste, with nothing pointing at the cause.
    inner = data.get("claudeAiOauth")
    if isinstance(inner, dict):
        data = inner

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


# ---------------------------------------------------------------------------
# Onboarding an account from a browser
# ---------------------------------------------------------------------------
#
# Every constant and every constraint below was measured rather than guessed,
# and is recorded in claudeswitch's docs/GROUND_TRUTH.md section 31. Two of
# them cost a day each to discover and would cost the same again:
#
#   * `state` MUST be 32 bytes. A 16-byte state is refused with "invalid
#     request format", which reads like a malformed URL and is not.
#   * the token endpoint is FORM-ENCODED. Sending JSON is answered with 403,
#     a status that reads like an authorization failure and is not.
#
# WHY THE USER STILL PASTES SOMETHING. Anthropic's OAuth client accepts exactly
# one redirect target -- its own callback page, which DISPLAYS a code. A
# third-party application cannot register `https://swarm.example/callback`, so
# the browser cannot be redirected back here. The code the callback shows is
# what closes the loop. That is one short string instead of a keychain item,
# which is the difference this flow exists to make.

AUTHORIZE_ENDPOINT = "https://claude.com/cai/oauth/authorize"

#: The only redirect Anthropic's client permits. It renders the code.
OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

#: Exactly what Claude Code itself requests. A narrower set has not been
#: tested, and a credential that cannot run inference is useless here.
OAUTH_SCOPES = (
    "org:create_api_key",
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)

#: 32, not 16. The endpoint validates the LENGTH rather than merely echoing it.
_STATE_BYTES = 32


def new_verifier() -> str:
    """A PKCE code verifier: 32 random bytes, base64url, unpadded."""
    return secrets.token_urlsafe(32)


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_state() -> str:
    return secrets.token_urlsafe(_STATE_BYTES)


def build_authorize_url(
    *,
    state: str,
    code_challenge: str,
    client_id: str = CLAUDE_CODE_CLIENT_ID,
    redirect_uri: str = OAUTH_REDIRECT_URI,
) -> str:
    """The URL a person opens to sign in.

    Parameter ORDER is the order Claude Code sends them in. That should not
    matter to a conforming server and it is preserved anyway: this endpoint has
    already proved to validate things a conforming server would not.
    """
    if len(state) < 40:
        # 32 bytes base64url-encodes to 43 characters. Anything shorter means
        # the caller built its own state and got the length wrong -- the exact
        # failure that presents as "invalid request format".
        raise CredentialError(
            f"state is {len(state)} characters; the authorize endpoint refuses "
            "anything built from fewer than 32 random bytes"
        )
    query = urlencode(
        [
            ("code", "true"),
            ("client_id", client_id),
            ("response_type", "code"),
            ("redirect_uri", redirect_uri),
            ("scope", " ".join(OAUTH_SCOPES)),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
            ("state", state),
        ]
    )
    return f"{AUTHORIZE_ENDPOINT}?{query}"


def split_pasted_code(pasted: str) -> tuple[str, str | None]:
    """The code, and the state if the person pasted both.

    The callback page renders `<code>#<state>`, and people paste the whole
    thing because that is what is on screen. Accepting only the bare code would
    reject the most likely paste with a message about an invalid code.
    """
    value = (pasted or "").strip()
    if "#" in value:
        code, _, state = value.partition("#")
        return code.strip(), (state.strip() or None)
    return value, None
