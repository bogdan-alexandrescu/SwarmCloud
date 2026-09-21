"""swarm-api's client for the quota broker's account routes.

WHY THIS IS A PROXY AND NOT A SECOND IMPLEMENTATION
---------------------------------------------------
Refreshing an OAuth credential REVOKES the token it replaces. Two components
that both rotate the same account do not merely race -- they brick it, because
the loser writes a token the provider has already invalidated and every pod
holding it starts failing at once. The quota broker is therefore the platform's
single writer for subscription credentials, and "single" has to mean one
implementation as well as one process.

So this module speaks HTTP to that service, and nothing in swarm-api opens
Secret Manager for an account. The routes in `routes/accounts.py` add exactly
one thing on top: the caller's tenant, taken from the verified token.

THE IDENTITY THIS CLIENT PRESENTS
---------------------------------
The broker requires a Google ID token, so a service-to-service call needs one
minted for this revision's own service account. On Cloud Run that comes from
the metadata server, which is why `MetadataIdToken` below asks
`instance/service-accounts/default/identity` rather than signing anything
locally: the runtime holds no key material to sign with, by design.

THE AUDIENCE IS NOT THE URL, and assuming it was would fail in staging.
terraform gives the broker a CUSTOM AUDIENCE --
`https://swarm-quota-broker.<env>.swarm.internal` (infra/locals.tf,
`push_audiences`) -- and the broker's own `WorkerIdentity` then verifies `aud`
against exactly that string. The service URL is a different value.

So OUTSIDE LOCAL DEVELOPMENT AN UNSET AUDIENCE REFUSES, `require_audience=True`,
the same rule `GoogleTokenVerifier(require_audience=settings.hardened)` applies
to the human-facing token and the same rule `WorkerIdentity` applies to
`BROKER_AUDIENCE`. Falling back to the base URL there would mint a token this
platform's broker is GUARANTEED to reject, and the rejection arrives as an
opaque 403 in staging rather than as the misconfiguration it is. The fallback
survives only for a local run, where the two values genuinely are the same.

REDIRECTS ARE REFUSED, NEVER FOLLOWED.
`urllib.request.urlopen` follows a 301/302/303/307/308 by default and re-sends
every header it was given -- including `Authorization` -- to whatever host the
`Location` names, with no same-origin restriction (`requests` strips the header
on a cross-host redirect; urllib does not). The header on these requests is a
full impersonation of this service to the single writer of every subscription
credential in the platform, so a misconfigured `QUOTA_BROKER_URL` (a bare
domain, an apex-to-www rule, a load balancer in front of the wrong backend)
would hand that identity to an arbitrary host. `_RefuseRedirects` below refuses
every redirect rather than trying to decide which ones are safe: the broker is
a single Cloud Run service at a known URL and has no legitimate reason to
redirect, so a redirect is a configuration error and is reported as one.

WHY THE 503 HERE HAS ITS OWN CODE
---------------------------------
swarm-api already answers 503 `upstream_unavailable` when Cloud Identity cannot
resolve group membership. The remedies have nothing in common: that one is
"retry, or fix the Workspace delegation", this one is "the quota broker is down
or this service cannot reach it". An operator who cannot tell the two apart
from the response spends the incident in the wrong system, so
`AccountPoolUnavailable` carries `account_pool_unavailable` and says the words
"account pool".

WHAT THIS MODULE REFUSES TO DO
------------------------------
`POST /v1/accounts` carries a pasted OAuth credential in its body. Nothing here
logs a request body, puts one in an exception message, or echoes one back: the
only things that reach a log line are the method, the path and the status.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode, urlsplit

from .errors import UpstreamUnavailable, ValidationFailed

log = logging.getLogger(__name__)

_UA = "swarmcloud-swarm-api"
_TIMEOUT = 15.0

#: Cloud Run's metadata server. `?audience=` is what makes the returned JWT
#: acceptable to the receiving service; `format=full` includes the email claim
#: the broker matches against its platform list.
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

#: Refresh a cached ID token this long before it actually expires, so a call
#: that starts just before the boundary does not arrive just after it.
_TOKEN_SKEW_SECONDS = 120.0


class AccountPoolUnavailable(UpstreamUnavailable):
    """The account pool could not be reached, or answered something unusable.

    A subclass with its OWN code, not a reuse of `upstream_unavailable`: see the
    module docstring. Still a 503, because it is still "try again, this is not
    your fault", and `create_app`'s ApiError handler renders it by class.
    """

    code = "account_pool_unavailable"


class BrokerUnreachable(RuntimeError):
    """A transport could not reach the endpoint at all.

    Distinct from "the endpoint answered badly", which arrives as a status code.
    Raised by the transport and translated by the caller, because the sentence
    an operator needs differs by which endpoint went missing -- the metadata
    server and the broker fail for entirely different reasons.
    """


class BrokerRedirected(BrokerUnreachable):
    """The endpoint answered with a redirect, which this client will not follow.

    A subclass of `BrokerUnreachable` so that every existing handler still
    treats it as "the endpoint did not serve this request", and a distinct class
    so `_call` can say the one thing an operator needs to hear: the URL is
    wrong, and the request was stopped before this service's identity token
    reached the host the redirect named.
    """


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. No exceptions, not even same-host ones.

    Deciding which redirects are safe means reimplementing the same-origin rule
    for a header that is a full service identity, and getting it slightly wrong
    is indistinguishable from getting it right until the day it matters. The
    broker is one Cloud Run service at one known URL; if it answers with a
    redirect, the URL is wrong.

    Raising here (rather than returning None, which would surface as an
    ordinary HTTPError for the 3xx) stops urllib inside the handler chain --
    before it builds the follow-up request and re-attaches the Authorization
    header.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        try:
            fp.close()
        except Exception:  # pragma: no cover - best effort, the socket is done
            pass
        # Hosts only. Never a header, and never the redirect target's query
        # string, which is somebody else's URL and may itself carry a secret.
        raise BrokerRedirected(
            f"HTTP {code} redirect from {_host(req.full_url)} to {_host(newurl)}"
        )


#: Built once. `build_opener` skips the default `HTTPRedirectHandler` because
#: `_RefuseRedirects` subclasses it, so there is no path through this opener
#: that follows a redirect. It is deliberately NOT installed globally with
#: `install_opener`: this module's policy is this module's, not the process's.
_OPENER = urllib.request.build_opener(_RefuseRedirects)


class Transport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = _TIMEOUT,
    ) -> tuple[int, Any]: ...


class IdTokenSource(Protocol):
    def token(self, audience: str) -> str: ...


class AccountPool(Protocol):
    """What `routes/accounts.py` needs. A test supplies its own implementation.

    Deliberately narrow, and deliberately not "a generic HTTP client": every
    method here is one of the broker's account routes, so a route handler cannot
    reach a broker endpoint this API was never meant to call.
    """

    def list_accounts(self, tenant_id: str) -> dict[str, Any]: ...

    def register(
        self,
        *,
        owner_tenant: str,
        label: str,
        provider: str,
        lend_to: list[str],
        credential: str,
    ) -> dict[str, Any]: ...

    def set_lending(self, account_id: str, *, lend_to: list[str]) -> dict[str, Any]: ...

    def set_state(self, account_id: str, *, state: str, reason: str) -> dict[str, Any]: ...

    def begin_sign_in(
        self, *, owner_tenant: str, label: str, provider: str, lend_to: list[str]
    ) -> dict[str, Any]:
        """Start a browser sign-in. Returns the URL and the state that keys it.

        The PKCE verifier stays in the broker, keyed by that state. It is never
        returned here and must never be: a verifier the client holds is a PKCE
        flow that proves nothing.
        """
        return self._call(
            "POST",
            "/v1/accounts/authorize",
            body={
                "owner_tenant": owner_tenant,
                "label": label,
                "provider": provider,
                "lend_to": lend_to,
            },
        )

    def finish_sign_in(self, *, state: str, code: str) -> dict[str, Any]:
        """Redeem the code the callback page displayed, and register the account."""
        return self._call(
            "POST",
            "/v1/accounts/exchange",
            body={"state": state, "code": code},
        )
    def refresh(self, account_id: str) -> dict[str, Any]: ...

    def remove(self, account_id: str) -> dict[str, Any]: ...


def urllib_transport(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: float = _TIMEOUT,
) -> tuple[int, Any]:
    """One request, with the stdlib. Returns (status, parsed body).

    The body comes back parsed as JSON when it parses and as text when it does
    not -- the metadata server answers `text/plain` with a bare JWT, and Cloud
    Run's IAM layer answers HTML, so a client that insisted on JSON would read
    both as "broken" rather than as the two very different things they are.

    REDIRECTS ARE REFUSED. `_OPENER` is used instead of `urlopen` for exactly
    one reason: `urlopen` follows a redirect and re-sends `Authorization` to the
    new host. See `_RefuseRedirects`.
    """
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _parse(raw)
    except urllib.error.URLError as exc:
        raise BrokerUnreachable(str(exc.reason)) from None
    except (TimeoutError, OSError) as exc:  # pragma: no cover - socket-level
        raise BrokerUnreachable(f"{type(exc).__name__}: {exc}") from None


def _parse(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _host(url: str) -> str:
    """The host, for an error message. Never the path or the query."""
    return urlsplit(url).hostname or url


def _token_expiry(token: str) -> float | None:
    """`exp` out of an unverified JWT, or None if it cannot be read.

    Unverified is correct here: this token was just handed over by the metadata
    server on a link-local address and is about to be sent straight back out.
    The claim is read to decide how long to CACHE it, never to trust anything,
    and an unreadable one simply means "do not cache".
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return None


class MetadataIdToken:
    """ID tokens for service-to-service calls, from the Cloud Run metadata server.

    Cached per audience until shortly before expiry. The metadata server is
    fast and local, but it is still a network hop on the request path and these
    tokens last an hour; minting one per API call would be a round trip per
    keystroke on the accounts screen.

    NOTHING HERE LOGS THE TOKEN. It is a full impersonation of this service to
    the broker, which is the single writer of every subscription credential in
    the platform.
    """

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        now: Callable[[], float] = time.time,
        skew_seconds: float = _TOKEN_SKEW_SECONDS,
    ) -> None:
        self._transport = transport or urllib_transport
        self._now = now
        self._skew = skew_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def token(self, audience: str) -> str:
        if not audience:
            # "Not configured" means refuse. An audience-less ID token is not
            # merely useless to the broker -- it is the exact shape of token
            # that verifies against a service which forgot to pin `aud`.
            raise AccountPoolUnavailable(
                "the account pool is unavailable: no audience is configured for "
                "the quota broker, and an ID token minted without one cannot be "
                "verified by it"
            )
        cached = self._cache.get(audience)
        if cached is not None and cached[1] > self._now():
            return cached[0]

        url = f"{METADATA_IDENTITY_URL}?{urlencode({'audience': audience, 'format': 'full'})}"
        try:
            status, payload = self._transport(
                "GET", url, headers={"Metadata-Flavor": "Google"}
            )
        except BrokerUnreachable as exc:
            raise AccountPoolUnavailable(
                "the account pool is unavailable: this service could not reach "
                f"the metadata server to mint its own identity token ({exc}). "
                "Outside Cloud Run there is no metadata server, which is why "
                "the client is injectable in tests"
            ) from None
        if status != 200 or not isinstance(payload, str) or not payload.strip():
            # The status, never the payload: a metadata error body is not
            # sensitive, but a success body IS the token and one shared code
            # path would eventually log it.
            raise AccountPoolUnavailable(
                "the account pool is unavailable: the metadata server would not "
                f"mint an identity token for the quota broker (HTTP {status})"
            )
        token = payload.strip()
        expiry = _token_expiry(token)
        if expiry is not None:
            self._cache[audience] = (token, expiry - self._skew)
        return token


class StaticIdToken:
    """A fixed token. Local development and tests only, like StaticTokenVerifier."""

    def __init__(self, token: str) -> None:
        self._token = token

    def token(self, audience: str) -> str:
        return self._token


class BrokerClient:
    """The quota broker's account routes, over HTTP.

    Every collaborator is injectable -- the token source and the transport --
    so the unit tests exercise this exact code with no credentials, no metadata
    server and no network.
    """

    def __init__(
        self,
        *,
        base_url: str,
        audience: str = "",
        require_audience: bool = False,
        tokens: IdTokenSource | None = None,
        transport: Transport | None = None,
        timeout: float = _TIMEOUT,
    ) -> None:
        self._base = (base_url or "").strip().rstrip("/")
        configured = (audience or "").strip()
        if self._base and require_audience and not configured:
            # NOT CONFIGURED MEANS REFUSE, here as everywhere else in this
            # service. The fallback below is right for a local broker and
            # catastrophic in a deployed environment: terraform pins a custom
            # audience on the broker, so a token minted for the service URL is
            # one the broker is guaranteed to reject -- and it rejects it as a
            # 403 about this service's identity, which reads like "you are not
            # permitted" to the operator whose screen it lands on. Refusing at
            # construction turns a silent staging-only failure into a 503 that
            # names the variable.
            #
            # Gated on `self._base` so that a deployment with NO broker at all
            # still gets the QUOTA_BROKER_URL message from `_call`, which is
            # the accurate one for that case.
            raise AccountPoolUnavailable(
                "the account pool is unavailable: QUOTA_BROKER_URL is set but "
                "QUOTA_BROKER_AUDIENCE is not. This platform's terraform gives "
                "the quota broker a custom audience "
                "(https://swarm-quota-broker.<env>.swarm.internal), which is "
                "not its service URL, and the broker verifies `aud` against "
                "exactly that string; minting a token for the URL instead "
                "would be rejected by the broker as an identity failure. Set "
                "QUOTA_BROKER_AUDIENCE to the broker's BROKER_AUDIENCE"
            )
        # Falls back to the base URL because that IS the audience for a plain
        # Cloud Run service and for a local broker -- and ONLY there, because
        # `require_audience` is on everywhere else. It is NOT the audience in
        # this platform's terraform, which pins a custom one -- see the module
        # docstring.
        self._audience = configured or self._base
        self._tokens = tokens if tokens is not None else MetadataIdToken()
        self._transport = transport or urllib_transport
        self._timeout = timeout

    # ------------------------------------------------------------------ routes

    def list_accounts(self, tenant_id: str) -> dict[str, Any]:
        return self._call("GET", "/v1/accounts", params={"tenant_id": tenant_id})

    def register(
        self,
        *,
        owner_tenant: str,
        label: str,
        provider: str,
        lend_to: list[str],
        credential: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/v1/accounts",
            payload={
                "owner_tenant": owner_tenant,
                "label": label,
                "provider": provider,
                "lend_to": list(lend_to),
                # Taken as pasted. `quota_broker.oauth.parse_credential` is the
                # one reader of this shape; a second parse here would be a
                # second definition of it.
                "credential": credential,
            },
        )

    def set_lending(self, account_id: str, *, lend_to: list[str]) -> dict[str, Any]:
        return self._call(
            "PUT",
            f"/v1/accounts/{quote(account_id, safe='')}/lending",
            payload={"lend_to": list(lend_to)},
        )

    def set_state(self, account_id: str, *, state: str, reason: str) -> dict[str, Any]:
        return self._call(
            "PUT",
            f"/v1/accounts/{quote(account_id, safe='')}/state",
            payload={"state": state, "reason": reason},
        )

    def begin_sign_in(
        self, *, owner_tenant: str, label: str, provider: str, lend_to: list[str]
    ) -> dict[str, Any]:
        """Start a browser sign-in. Returns the URL and the state that keys it.

        The PKCE verifier stays in the broker, keyed by that state. It is never
        returned here and must never be: a verifier the client holds is a PKCE
        flow that proves nothing.
        """
        return self._call(
            "POST",
            "/v1/accounts/authorize",
            body={
                "owner_tenant": owner_tenant,
                "label": label,
                "provider": provider,
                "lend_to": lend_to,
            },
        )

    def finish_sign_in(self, *, state: str, code: str) -> dict[str, Any]:
        """Redeem the code the callback page displayed, and register the account."""
        return self._call(
            "POST",
            "/v1/accounts/exchange",
            body={"state": state, "code": code},
        )
    def refresh(self, account_id: str) -> dict[str, Any]:
        return self._call(
            "POST", f"/v1/accounts/{quote(account_id, safe='')}/refresh", payload={}
        )

    def remove(self, account_id: str) -> dict[str, Any]:
        return self._call("DELETE", f"/v1/accounts/{quote(account_id, safe='')}")

    # ----------------------------------------------------------------- plumbing

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._base:
            # Refuse, rather than degrade into some local write path. There is
            # no local write path: this service is not allowed to be a second
            # writer of a subscription credential.
            raise AccountPoolUnavailable(
                "the account pool is unavailable: this deployment has no quota "
                "broker configured (QUOTA_BROKER_URL is unset). The broker is the "
                "only writer of subscription credentials, so there is nothing "
                "this API can do locally instead"
            )

        url = self._base + path
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v})}"
        headers = {
            "Authorization": f"Bearer {self._tokens.token(self._audience)}",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            status, data = self._transport(
                method, url, headers=headers, body=body, timeout=self._timeout
            )
        except BrokerRedirected as exc:
            # Before `BrokerUnreachable`, which this subclasses: "could not
            # reach the broker" would send an operator to look at the broker's
            # health, and the broker is fine. The URL is wrong.
            log.warning("account pool redirected: %s %s (%s)", method, path, exc)
            raise AccountPoolUnavailable(
                "the account pool is unavailable: the quota broker URL answered "
                f"with a redirect ({exc}), and this client refuses to follow "
                "one. Every request here carries an ID token that impersonates "
                "this service to the single writer of every subscription "
                "credential in the platform, and following the redirect would "
                "deliver that token to the host it names. Point "
                "QUOTA_BROKER_URL at the broker's own https:// service URL"
            ) from None
        except BrokerUnreachable as exc:
            # The host and the reason. Never the body -- a register body is a
            # pasted OAuth credential.
            log.warning("account pool unreachable: %s %s (%s)", method, path, exc)
            raise AccountPoolUnavailable(
                "the account pool is unavailable: could not reach the quota "
                f"broker at {_host(self._base)} ({exc})"
            ) from None

        log.info("account pool %s %s -> %s", method, path, status)
        return self._result(status, data, path)

    def _result(self, status: int, data: Any, path: str) -> dict[str, Any]:
        if 200 <= status < 300:
            return data if isinstance(data, dict) else {}

        # The broker's own errors are JSON with a `code`. Anything else came
        # from in front of the application -- Cloud Run's IAM check answers
        # 401/403 with HTML -- and at 422 and above the two mean completely
        # different things, so they are not collapsed.
        #
        # At 401/403 they mean the SAME thing (see below), which is why that
        # case is handled first and ignores this flag entirely.
        broker_said = isinstance(data, dict) and "code" in data
        message = str(data.get("message") or "") if isinstance(data, dict) else ""

        if status in (401, 403):
            # NEVER the human caller's 403, whatever shape the body has.
            #
            # swarm-api authenticates to the broker as a PLATFORM caller, and
            # the broker's `_authorize` returns immediately for a platform
            # caller -- so there is no tenant-level refusal it can send this
            # service. Every 401/403 reachable from here is about THIS
            # SERVICE's own identity: not listed in PLATFORM_SERVICE_ACCOUNTS,
            # token verification failed, missing bearer token, or Cloud Run's
            # IAM check refusing the invocation before the app is reached.
            #
            # Re-raising any of those as the caller's `Forbidden` tells an
            # operator they lack a permission no human permission could grant,
            # and sends them into the wrong system for a platform IAM fix. The
            # human-facing tenant boundary is `routes/accounts.py`'s job and is
            # enforced before the call is made at all.
            raise AccountPoolUnavailable(
                "the account pool rejected this service's own identity "
                f"(HTTP {status}"
                + (f": {message}" if message else "")
                + "). swarm-api's service account needs "
                "roles/run.invoker on the quota broker, must be listed in its "
                "PLATFORM_SERVICE_ACCOUNTS, and the ID token audience must match "
                "the broker's BROKER_AUDIENCE"
            )
        if broker_said and status == 422:
            # BrokerValidationError -- "the thing you named does not exist", or
            # a credential with no refresh token, refused at paste time.
            raise ValidationFailed(message or "the account pool refused this request")
        if broker_said:
            raise AccountPoolUnavailable(
                f"the account pool answered HTTP {status}"
                + (f": {message}" if message else "")
            )

        if status == 404:
            raise AccountPoolUnavailable(
                f"the account pool has no route {path!r} (HTTP 404): the quota "
                "broker deployed here is older than this API"
            )
        raise AccountPoolUnavailable(f"the account pool answered HTTP {status}")


__all__ = [
    "AccountPool",
    "AccountPoolUnavailable",
    "BrokerClient",
    "BrokerRedirected",
    "BrokerUnreachable",
    "IdTokenSource",
    "METADATA_IDENTITY_URL",
    "MetadataIdToken",
    "StaticIdToken",
    "Transport",
    "urllib_transport",
]
