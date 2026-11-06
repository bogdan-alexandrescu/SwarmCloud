"""ChatGPT-plan pool accounts: the `openai` account kind (#2688).

THE OWNER'S CHOICE (BGD, 2026-11-01): the codex runner runs on ChatGPT PLANS
from a shared pool, the way claude-code runs on the pool of Claude
subscriptions. This module is that pool's credential: what is stored, how it
is refreshed, what a worker is handed, and how a plan's usage is read.

WHAT WAS MEASURED, AND WHERE
----------------------------
* AI Studio (sagaxyz/ai-studio, first-hand on a live ChatGPT Plus login,
  2026-08-10): the sign-in is OAuth with PKCE at https://auth.openai.com,
  authorize `/oauth/authorize`, token `/oauth/token`, the Codex CLI's public
  client id, and the redirect `http://localhost:1455/auth/callback` -- the
  documented `127.0.0.1` form FAILED live, so `localhost` is pinned here.
* The codex CLI in agent-runtime-base (codex-cli 0.154.0, read from its binary
  on 2026-11-01): its authorize request carries `id_token_add_organizations`
  and `codex_cli_simplified_flow` beside the PKCE parameters, scope
  `openid profile email offline_access`; it reads a plan's limits from
  `/wham/usage` under the Codex backend with a `ChatGPT-Account-Id` header, as
  `rate_limit.primary_window` / `secondary_window`, each with `used_percent`,
  `limit_window_seconds`, `reset_after_seconds` and `reset_at`. The usage
  route's answer was NOT captured live from here: `parse_usage` reads exactly
  those names and refuses anything else, so a shape change fails in one place
  and leaves the previous reading standing.
* swarm-voice's "Sign in with ChatGPT" refresh (apps/swarm-voice/.../credential.py):
  a FORM POST with `grant_type`, `client_id` and `refresh_token`, the rotated
  refresh token stored before the access token is used, refreshes serialised.
  The approach is reused here; nothing is imported across apps.

THE RULES THIS MODULE HOLDS
---------------------------
* The broker is the ONLY writer. Every refresh REPLACES the refresh token, so
  two refreshers of one plan lock it out; `credentials.CredentialRefresher`
  serialises them per account and persists the rotated pair BEFORE the access
  token is published, exactly as for a Claude account.
* The worker is handed the ACCESS token and the plan's account id, never the
  refresh token. The worker-facing secret holds what `published_value`
  returns -- a JSON object of exactly two fields, the access token and the
  account id -- while the `-refresh` secret holds the pair and is the broker's
  alone.
* A refused refresh (`invalid_grant`, or a 400/401) is `ReauthRequired`: the
  account is marked `reauth needed` and only a new sign-in clears it.
* No token is ever logged, raised or returned: errors carry a status only,
  because an error body can quote the request.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode

from .accounts import WindowReading
from .oauth import CredentialError, Organisation, ReauthRequired

#: The account kind's provider name -- the codex profile's `provider` in the
#: frozen catalogue, so the quota routes and `accounts_serving` already know it.
PROVIDER = "openai"

ISSUER = "https://auth.openai.com"
AUTHORIZE_ENDPOINT = f"{ISSUER}/oauth/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/oauth/token"
#: The Codex CLI's public client: the `aud` of the id_token a plan sign-in
#: returns. Not a secret -- a public PKCE client, like Claude Code's.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
#: MEASURED: `localhost`, not `127.0.0.1`, which failed live. The port is the
#: one the client registered; the person's own machine listens on it
#: (`sc account add --provider openai`).
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"

#: Where plan traffic goes (the Responses wire), and the usage route codex
#: itself reads its plan's windows from.
CODEX_BACKEND = "https://chatgpt.com/backend-api/codex"
USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"

#: The stored pair says what it is, so the one refresher can tell a plan's
#: credential from a Claude one by reading it, with no provider argument
#: threaded through every caller of `refresh_secret`.
KIND = "openai_plan"

#: The id_token's claim namespace that carries the plan's account id.
_AUTH_CLAIM = "https://api.openai.com/auth"

#: A plan's access token lives about an hour, so the Claude window (three
#: hours before an eight-hour expiry) would exchange it on every five-minute
#: sweep -- twelve rotations an hour for nothing. Half its life instead: a
#: published token always has about 25 minutes left, and a refresh token is
#: exchanged twice an hour.
REFRESH_WINDOW = timedelta(minutes=30)

#: The refused-refresh answers that mean a human must sign in again.
_DEAD_GRANT = (
    "invalid_grant",
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
)

#: Opaque identity prefixes, distinct from the Claude ones, so a plan's id and
#: an organisation's can never hash to one identity.
IDENTITY_PREFIX = "openai-account"
SEAT_PREFIX = "openai-seat"


@dataclass(frozen=True)
class PlanCredential:
    access_token: str
    refresh_token: str
    account_id: str
    expires_at: datetime
    #: Which plan and which login, from the id_token, for the duplicate check.
    #: Recorded on the account document, never serialised with the pair.
    organisation: Organisation | None = None

    def needs_refresh(self, now: datetime, window: timedelta = REFRESH_WINDOW) -> bool:
        return now + min(window, REFRESH_WINDOW) >= self.expires_at

    def redacted(self) -> dict[str, Any]:
        """Safe to log: shape and timing only, never material."""
        return {
            "expires_at": self.expires_at.isoformat(),
            "access_token_len": len(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "has_account_id": bool(self.account_id),
        }


class PlanTokenEndpoint(Protocol):
    def exchange(self, refresh_token: str) -> dict[str, Any]: ...

    def redeem(
        self, *, code: str, verifier: str, redirect_uri: str, state: str = ""
    ) -> dict[str, Any]: ...


def is_plan_payload(payload: str) -> bool:
    """Whether a stored or pasted credential is a ChatGPT plan's.

    Ours carries `kind`; codex's own `~/.codex/auth.json` carries `tokens`
    with an `account_id` -- the file a person might paste.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("kind") == KIND:
        return True
    tokens = data.get("tokens")
    return isinstance(tokens, dict) and "account_id" in tokens


def _jwt_claims(token: str) -> dict[str, Any]:
    """A JWT's payload, UNVERIFIED -- only ever for a token this service just
    received from the token endpoint over TLS, or one it stored itself."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _digest(prefix: str, value: str) -> str:
    # The shape of `oauth.organisation_identity`, under this kind's own prefix.
    digest = hashlib.sha256(f"{prefix}:{value}".encode("utf-8")).hexdigest()
    return f"{prefix}-sha256:{digest}"


def organisation_from_id_token(id_token: str | None, account_id: str) -> Organisation | None:
    """Which plan a sign-in authorised, opaque, for the one-seat-one-account rule.

    The plan's account id is the identity (a Team workspace is one account id);
    the login's user id is the seat. Both are hashed: neither is stored.
    """
    claims = _jwt_claims(id_token or "")
    auth = claims.get(_AUTH_CLAIM) if isinstance(claims.get(_AUTH_CLAIM), dict) else {}
    account = str(auth.get("chatgpt_account_id") or account_id or "")
    if not account:
        return None
    user = auth.get("chatgpt_user_id") or auth.get("user_id")
    plan = auth.get("chatgpt_plan_type")
    return Organisation(
        identity=_digest(IDENTITY_PREFIX, account),
        name="ChatGPT",
        plan=str(plan).capitalize() if isinstance(plan, str) and plan else None,
        seat=_digest(SEAT_PREFIX, str(user)) if user else None,
    )


def _account_id_from(result: dict[str, Any]) -> str:
    for token in (result.get("id_token"), result.get("access_token")):
        auth = _jwt_claims(str(token or "")).get(_AUTH_CLAIM)
        if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
            return str(auth["chatgpt_account_id"])
    return str(result.get("account_id") or "")


def _expiry(access_token: str, raw: Any, now: datetime) -> datetime:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    exp = _jwt_claims(access_token).get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    # Unknown expiry reads as expired: the next sweep refreshes it, which is
    # the cheap direction to be wrong in.
    return now


def parse_plan_credential(payload: str, *, now: datetime | None = None) -> PlanCredential:
    """Read a stored pair, or codex's own auth.json pasted at registration."""
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        raise CredentialError("the ChatGPT plan credential is not JSON") from None
    if not isinstance(data, dict):
        raise CredentialError("the ChatGPT plan credential is not a JSON object")
    id_token = None
    if isinstance(data.get("tokens"), dict):
        tokens = data["tokens"]
        id_token = tokens.get("id_token")
        data = {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "account_id": tokens.get("account_id"),
        }
    access = str(data.get("access_token") or "")
    refresh = str(data.get("refresh_token") or "")
    account_id = str(data.get("account_id") or "")
    if not refresh:
        raise CredentialError("the ChatGPT plan credential has no refresh token")
    if not account_id:
        account_id = _account_id_from({"id_token": id_token, "access_token": access})
    if not account_id:
        raise CredentialError("the ChatGPT plan credential names no account id")
    return PlanCredential(
        access_token=access,
        refresh_token=refresh,
        account_id=account_id,
        expires_at=_expiry(access, data.get("expires_at"), now),
        organisation=organisation_from_id_token(id_token, account_id) if id_token else None,
    )


def serialise_plan(credential: PlanCredential) -> str:
    """The stored pair: `{base}-refresh`, readable by the broker alone."""
    return json.dumps(
        {
            "kind": KIND,
            "access_token": credential.access_token,
            "refresh_token": credential.refresh_token,
            "account_id": credential.account_id,
            "expires_at": int(credential.expires_at.timestamp() * 1000),
        },
        sort_keys=True,
    )


def published_value(credential: PlanCredential) -> str:
    """What `{base}` holds and a worker reads: the access token and the plan's
    account id, NEVER the refresh token. Key-sorted, so an unchanged token is
    byte-identical and the refresher's "already published" check matches it.
    The worker passes them to codex as CODEX_PLAN_ACCESS_TOKEN and
    CODEX_PLAN_ACCOUNT_ID."""
    return json.dumps(
        {"access_token": credential.access_token, "account_id": credential.account_id},
        sort_keys=True,
    )


def refresh_plan(
    credential: PlanCredential, endpoint: PlanTokenEndpoint, *, now: datetime | None = None
) -> PlanCredential:
    """Exchange the refresh token. The result carries the ROTATED refresh token
    (or the presented one, when the endpoint did not rotate it)."""
    now = now or datetime.now(timezone.utc)
    result = endpoint.exchange(credential.refresh_token)
    if not isinstance(result, dict):
        raise CredentialError("the ChatGPT token endpoint returned an unexpected shape")
    access = str(result.get("access_token") or "")
    if not access:
        raise CredentialError("the ChatGPT token endpoint returned no access_token")
    expires_in = result.get("expires_in")
    expires_at = (
        now + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, (int, float)) and expires_in
        else _expiry(access, None, now)
    )
    account_id = _account_id_from(result) or credential.account_id
    id_token = result.get("id_token")
    return PlanCredential(
        access_token=access,
        refresh_token=str(result.get("refresh_token") or credential.refresh_token),
        account_id=account_id,
        expires_at=expires_at,
        organisation=organisation_from_id_token(id_token, account_id) if id_token else None,
    )


def credential_from_sign_in(payload: dict[str, Any], *, now: datetime | None = None) -> PlanCredential:
    """The pair a redeemed authorization code returned, or a refusal that says
    nothing was saved."""
    now = now or datetime.now(timezone.utc)
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access or not refresh:
        raise CredentialError(
            "the ChatGPT sign-in returned no refresh token, so this plan could "
            "not be kept alive. Nothing was saved."
        )
    account_id = _account_id_from(payload)
    if not account_id:
        raise CredentialError(
            "the ChatGPT sign-in did not name the plan's account, so codex could "
            "not use it. Nothing was saved."
        )
    expires_in = payload.get("expires_in")
    return PlanCredential(
        access_token=access,
        refresh_token=refresh,
        account_id=account_id,
        expires_at=(
            now + timedelta(seconds=int(expires_in))
            if isinstance(expires_in, (int, float)) and expires_in
            else _expiry(access, None, now)
        ),
        organisation=organisation_from_id_token(payload.get("id_token"), account_id),
    )


def build_authorize_url(*, state: str, code_challenge: str) -> str:
    """The plan sign-in's authorize URL, with the parameters codex sends."""
    return f"{AUTHORIZE_ENDPOINT}?" + urlencode(
        {
            "response_type": "code",
            "client_id": CODEX_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
        }
    )


class HttpPlanTokenEndpoint:
    """The real endpoint. Injected so tests never make a network call."""

    def __init__(self, url: str = TOKEN_ENDPOINT, client_id: str = CODEX_CLIENT_ID) -> None:
        self._url = url
        self._client_id = client_id

    def _post(self, form: dict[str, str], *, refresh: bool) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self._url,
            data=urlencode(form).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The body is read for its error CODE only, never quoted: it can
            # echo what was sent.
            detail = exc.read().decode("utf-8", "replace")[:400]
            if refresh and (exc.code in (400, 401) or any(c in detail for c in _DEAD_GRANT)):
                raise ReauthRequired(
                    f"the ChatGPT token endpoint refused the refresh token ({exc.code}); "
                    "a person must sign in to the plan again"
                ) from None
            raise CredentialError(f"the ChatGPT token endpoint returned {exc.code}") from None
        except urllib.error.URLError as exc:
            raise CredentialError(
                f"could not reach the ChatGPT token endpoint: {type(exc.reason).__name__}"
            ) from None
        except ValueError:
            raise CredentialError("the ChatGPT token endpoint answered with no JSON") from None

    def exchange(self, refresh_token: str) -> dict[str, Any]:
        return self._post(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": refresh_token,
            },
            refresh=True,
        )

    def redeem(
        self, *, code: str, verifier: str, redirect_uri: str, state: str = ""
    ) -> dict[str, Any]:
        return self._post(
            {
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            refresh=False,
        )


# -- usage ---------------------------------------------------------------------

#: A window named by its length, in the names the Claude pool already uses, so
#: the hold logic's floors and the health page's columns apply unchanged: codex
#: reports a five-hour window and a weekly one.
_WINDOW_NAMES = {5 * 3600: "five_hour", 7 * 24 * 3600: "seven_day"}


def _window_name(seconds: int, fallback: str) -> str:
    if seconds in _WINDOW_NAMES:
        return _WINDOW_NAMES[seconds]
    return f"window_{seconds}s" if seconds > 0 else fallback


def _parse_window(raw: Any, now: datetime) -> tuple[int, WindowReading] | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    reset_at = raw.get("reset_at")
    after = raw.get("reset_after_seconds")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool) and reset_at > 0:
        resets = datetime.fromtimestamp(reset_at, tz=timezone.utc)
    elif isinstance(after, (int, float)) and not isinstance(after, bool):
        resets = now + timedelta(seconds=float(after))
    else:
        return None
    seconds = raw.get("limit_window_seconds")
    seconds = int(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else 0
    return seconds, WindowReading(
        utilization=max(0.0, min(1.0, float(pct) / 100.0)), resets_at=resets
    )


def parse_usage(payload: Any, *, now: datetime | None = None) -> dict[str, WindowReading]:
    """A plan's windows from codex's usage answer. Raises rather than returning
    an empty dict: an empty reading would claim the plan has nothing used."""
    from .usage import UsageUnavailable

    now = now or datetime.now(timezone.utc)
    limits = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        raise UsageUnavailable("the plan's usage answer carried no rate_limit")
    windows: dict[str, WindowReading] = {}
    for key, fallback in (("primary_window", "primary"), ("secondary_window", "secondary")):
        parsed = _parse_window(limits.get(key), now)
        if parsed is not None:
            seconds, reading = parsed
            windows[_window_name(seconds, fallback)] = reading
    if not windows:
        raise UsageUnavailable("the plan's usage answer carried no readable window")
    return windows


def fetch_usage(published: str, *, timeout_seconds: float = 10.0) -> dict[str, WindowReading]:
    """Read one plan's windows, given what `{base}` holds (`published_value`)."""
    import urllib.error
    import urllib.request

    from .usage import RateLimited, UsageUnavailable

    try:
        data = json.loads(published)
        token, account = str(data["access_token"]), str(data["account_id"])
    except (TypeError, ValueError, KeyError):
        raise UsageUnavailable("the plan's published credential is not readable") from None
    if not token or not account:
        raise UsageUnavailable("no access token for this plan")
    request = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={
            "authorization": f"Bearer {token}",
            "chatgpt-account-id": account,
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raw = exc.headers.get("retry-after", "") if exc.headers else ""
            try:
                retry_after = int(raw)
            except (TypeError, ValueError):
                retry_after = 300
            raise RateLimited(retry_after) from None
        raise UsageUnavailable(f"the plan's usage route returned {exc.code}") from None
    except urllib.error.URLError as exc:
        raise UsageUnavailable(
            f"could not reach the plan's usage route: {type(exc.reason).__name__}"
        ) from None
    try:
        payload = json.loads(body)
    except ValueError:
        raise UsageUnavailable("the plan's usage route answered with no JSON") from None
    return parse_usage(payload)
