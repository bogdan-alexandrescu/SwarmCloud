"""`sc login`: a developer signs in AS THEMSELVES, and IAP lets them through.

WHY THIS EXISTS. Measured 2026-09-24 against https://swarm.saga.xyz: a user's
own gcloud access token is refused by IAP with 401 and error code 900, because
this platform's IAP uses a Google-managed OAuth client
(`terraform/modules/frontend` sets no `oauth2_client_id`, on purpose), and
Google says so outright: "Google-managed OAuth clients cannot programmatically
access IAP-protected applications. However, IAP-protected applications that use
the Google-managed OAuth client can still be accessed programmatically using a
separate OAuth client configured through the programmatic_clients setting"
(cloud.google.com/iap/docs/custom-oauth-configuration). Until this module the
only way a laptop got through was impersonating a service account -- so every
developer acted as `swarm-verify`, and the API's tenant boundary saw one caller.

THE DOCUMENTED PATH, and the one implemented here, is Google's "Authenticate
from a desktop app" (cloud.google.com/iap/docs/authentication-howto):

  1. ONE "Desktop app" OAuth client per deployment, created once by whoever
     runs it, and allowlisted on the IAP resource as a programmatic client
     (terraform/bootstrap/iap_programmatic_clients.tf, per backend service).
  2. Each developer runs the installed-app flow against it: a browser opens
     `accounts.google.com/o/oauth2/v2/auth` with `scope=openid email` and
     `access_type=offline`, Google redirects to a listener on 127.0.0.1, and
     the code is exchanged at `oauth2.googleapis.com/token` with the client id
     and secret.
  3. The response's `id_token` -- audience: the Desktop client id -- is sent
     as `Authorization: Bearer`. IAP admits it because that client is
     allowlisted, and forwards the developer's OWN identity to swarm-api in
     `X-Goog-IAP-JWT-Assertion`, which is what the API authenticates.
  4. The refresh token mints the next ID token when this one expires (about an
     hour); nobody signs in again until they sign out or it is revoked.

Two additions to Google's minimal example, both standard for an installed app
(RFC 8252): a random `state`, checked on the callback, so another page on this
machine cannot complete a sign-in the user did not start; and PKCE with S256,
so an authorisation code intercepted on its way to the loopback listener cannot
be exchanged by anyone without the verifier. `prompt=consent` is asked every
time because Google issues a refresh token only on consent -- without it, a
second `sc login` after `sc logout` would get an ID token and no way to renew
it.

WHAT IS KEPT WHERE. The refresh token goes into the credential store under the
context (`credentials.py`); the ID token lives in this process's memory and
nowhere else, the same rule `client.py` states for every bearer it handles.

JWT CLAIMS ARE READ, NOT VERIFIED, and that is deliberate. The token arrived
over TLS from Google's token endpoint in answer to this process's own request;
it is read here only for its expiry (when to refresh) and its email (what
`sc whoami` prints). The party that must VERIFY it is IAP, and it does.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import html
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from . import credentials
from .client import SwarmError
from .config import Deployment
from .invocation import terminal_command

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

#: What `sc login` asks for: who you are, and nothing else. No Cloud scope, so
#: the refresh token stored on this laptop cannot read a bucket or call any
#: Google API -- it can mint an ID token for this client, which IAP takes.
SCOPES = "openid email"

#: Re-mint this long before `exp`. A long `swarm tail` that presented a token
#: in its last minute would otherwise die at the 59-minute mark with a 401 that
#: reads like a permission problem -- the reason `client.py` re-mints early too.
_REFRESH_MARGIN_SECONDS = 5 * 60

#: How long `sc login` waits for the browser. Long enough to pick an account
#: and read a consent screen; short enough that a forgotten terminal returns.
LOGIN_TIMEOUT_SECONDS = 300.0

#: Replaced in tests with a fake browser. `webbrowser.open` returns at once,
#: which is the shape the loopback listener below relies on.
open_browser: Callable[[str], bool] = webbrowser.open


class SignInRequired(SwarmError):
    """This deployment takes a signed-in developer, and none is signed in."""


def sign_in_hint(deployment: Deployment) -> str:
    """'sign-in required for <context>: run sc login (a browser window opens)'.

    The owner's sentence, with the command spelled so it runs (`terminal_command`
    is the package's one spelling of a `sc` command) and `--context` added when
    the context is not the current one -- otherwise the command signs in to a
    different deployment than the one that asked.
    """
    command = "sc login" if deployment.current else f"sc login --context {deployment.context}"
    return (
        f"sign-in required for {deployment.context}: run `{terminal_command(command)}` "
        f"(a browser window opens) -- {deployment.url} admits you as yourself "
        "once you have signed in"
    )


# -- small pieces ------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, unverified -- see the module docstring for why."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError) as exc:
        raise SwarmError("Google's token endpoint returned an id_token that is not a JWT") from exc
    if not isinstance(claims, dict):
        raise SwarmError("the id_token's payload is not a JSON object")
    return claims


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect with a client secret or a refresh token in the body."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _post(url: str, form: dict[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    """POST a form to Google's OAuth endpoints; return the JSON, or raise readably.

    The error body's `error` and `error_description` are quoted; the request
    body never is, because it carries the secret, the code or the token.
    """
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        error = str(body.get("error") or f"HTTP {exc.code}")
        described = str(body.get("error_description") or "")
        raise _OAuthError(error, described, exc.code) from exc
    except urllib.error.URLError as exc:
        raise SwarmError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SwarmError(f"{urllib.parse.urlsplit(url).netloc} answered with something that is not JSON") from exc
    return body if isinstance(body, dict) else {}


class _OAuthError(SwarmError):
    def __init__(self, error: str, description: str, status: int) -> None:
        super().__init__(f"{error}: {description}".rstrip(": ") + f" (HTTP {status})", status=status)
        self.error = error


def _require_client(deployment: Deployment) -> None:
    if not deployment.client_id:
        raise SwarmError(
            f"context {deployment.context!r} ({deployment.url}) has no OAuth client id, so "
            "there is nothing to sign in with. The deployment's operator creates one "
            "Desktop OAuth client and shares its id and secret; then run "
            f"`{terminal_command(f'sc context add {deployment.context} --url {deployment.url} --client-id <id> --client-secret-stdin')}`, "
            "or set it in the plugin with /plugin configure sc@swarmcloud"
        )


def client_secret_for(deployment: Deployment, store: credentials.Store | None = None) -> str:
    """The Desktop client's secret: this process's, else the store's, else ''."""
    if deployment.client_secret:
        return deployment.client_secret
    chosen = store if store is not None else credentials.store()
    return chosen.get(credentials.key(deployment.context, credentials.CLIENT_SECRET)) or ""


def _secret_or_ask(deployment: Deployment, store: credentials.Store, supplied: str | None) -> str:
    """The secret for a login: supplied, known, or typed -- and then remembered.

    Google requires the client secret for a Desktop app client even with PKCE,
    and documents that it is not treated as confidential for installed apps.
    It is still kept out of argv, out of the config file and out of any output.
    """
    secret = supplied or client_secret_for(deployment, store)
    slot = credentials.key(deployment.context, credentials.CLIENT_SECRET)
    if not secret and sys.stdin is not None and sys.stdin.isatty():
        secret = getpass.getpass(
            f"OAuth client secret for {deployment.context} (the Desktop client's; input hidden): "
        ).strip()
    if not secret:
        raise SwarmError(
            f"no OAuth client secret for {deployment.context}. Give it once with "
            f"`{terminal_command(f'sc context add {deployment.context} --url {deployment.url} --client-id {deployment.client_id} --client-secret-stdin')}`, "
            "set it in the plugin (/plugin configure sc@swarmcloud), or export "
            "SWARM_OAUTH_CLIENT_SECRET"
        )
    if store.get(slot) != secret:
        store.set(slot, secret)
    return secret


# -- the loopback listener ---------------------------------------------------


class _Callback:
    """What arrived at the loopback address: the query of the first real request."""

    def __init__(self) -> None:
        self.params: dict[str, str] | None = None
        self.done = threading.Event()


def _listener(callback: _Callback) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ARG002 - nothing on stderr mid-flow
            pass

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if "code" not in params and "error" not in params:
                # A favicon or a pre-fetch. Not the redirect; not an answer.
                self.send_response(404)
                self.end_headers()
                return
            if callback.params is None:
                callback.params = params
                callback.done.set()
            ok = "code" in params
            page = (
                "<!doctype html><title>SwarmCloud</title><body style='font-family:sans-serif'>"
                + (
                    "<h1>Signed in.</h1><p>You can close this tab and return to the terminal.</p>"
                    if ok
                    else f"<h1>Sign-in did not complete.</h1><p>{html.escape(params.get('error', ''))}</p>"
                )
                + "</body>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    return HTTPServer(("127.0.0.1", 0), Handler)


# -- the three operations ----------------------------------------------------


def login(
    deployment: Deployment,
    *,
    store: credentials.Store | None = None,
    client_secret: str | None = None,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    notify: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the installed-app flow; store the refresh token; return the ID token's claims.

    `notify` receives the one line a person needs if no browser opens: the URL
    to open by hand. It goes to stderr in the CLI, so stdout stays the result.
    """
    _require_client(deployment)
    chosen = store if store is not None else credentials.store()
    secret = _secret_or_ask(deployment, chosen, client_secret)

    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))

    callback = _Callback()
    server = _listener(callback)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": deployment.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        if notify is not None:
            notify(f"Opening a browser to sign in to {deployment.context}. If none opens, visit:\n  {url}")
        try:
            open_browser(url)
        except Exception:  # noqa: BLE001 - a missing browser is not a failed login
            pass
        if not callback.done.wait(timeout):
            raise SwarmError(
                f"no answer from the browser within {int(timeout)}s; run "
                f"`{terminal_command('sc login')}` again when you are ready to sign in"
            )
    finally:
        server.shutdown()
        server.server_close()

    params = callback.params or {}
    if params.get("state") != state:
        raise SwarmError(
            "the sign-in callback carried the wrong state, so it was not the sign-in "
            "this command started -- nothing was stored. Run it again"
        )
    if "error" in params:
        raise SwarmError(
            f"Google did not sign you in: {params['error']}. Nothing was stored"
        )

    try:
        tokens = _post(TOKEN_URI, {
            "client_id": deployment.client_id,
            "client_secret": secret,
            "code": params.get("code", ""),
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    except _OAuthError as exc:
        raise SwarmError(
            f"Google refused to exchange the sign-in code: {exc}. If this says "
            "invalid_client, the client id or secret for this context is wrong"
        ) from None

    refresh = str(tokens.get("refresh_token") or "")
    id_token = str(tokens.get("id_token") or "")
    if not refresh or not id_token:
        raise SwarmError(
            "Google signed you in but returned no "
            + ("refresh token" if not refresh else "ID token")
            + " -- is this client a Desktop app client? Nothing was stored"
        )
    claims = decode_claims(id_token)
    chosen.set(credentials.key(deployment.context, credentials.REFRESH_TOKEN), refresh)
    return claims


def logout(
    deployment: Deployment,
    *,
    store: credentials.Store | None = None,
    revoke: bool = True,
) -> bool:
    """Forget this person's refresh token, and revoke it at Google.

    Revoked as well as forgotten: a copy of the token that outlived its deletion
    here (a backup, a synced keychain) stops working too. The client secret is
    KEPT -- it belongs to the deployment, not to the person, and the next
    `sc login` needs it. Returns whether a sign-in existed.
    """
    chosen = store if store is not None else credentials.store()
    slot = credentials.key(deployment.context, credentials.REFRESH_TOKEN)
    refresh = chosen.get(slot)
    if not refresh:
        return False
    if revoke:
        try:
            _post(REVOKE_URI, {"token": refresh})
        except SwarmError:
            # Forgetting still happens: a token nobody holds any more is the
            # outcome the person asked for, and Google expires unused ones.
            pass
    chosen.delete(slot)
    return True


class SignedIn:
    """ID tokens for one deployment, minted from its stored refresh token.

    Nothing is decided at construction: `sc login` in another terminal must be
    picked up by an MCP server that is already running, so "not signed in" is
    re-checked on every call that has no valid token, never remembered.
    """

    def __init__(self, deployment: Deployment, *, store: credentials.Store | None = None) -> None:
        self.deployment = deployment
        self._store = store
        self._token = ""
        self._claims: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def store(self) -> credentials.Store:
        return self._store if self._store is not None else credentials.store()

    def signed_in(self) -> bool:
        return bool(self.store.get(credentials.key(self.deployment.context, credentials.REFRESH_TOKEN)))

    def id_token(self) -> str:
        with self._lock:
            if self._token and self._claims.get("exp", 0) - _REFRESH_MARGIN_SECONDS > time.time():
                return self._token
            return self._refresh()

    def claims(self) -> dict[str, Any]:
        self.id_token()
        return dict(self._claims)

    def _refresh(self) -> str:
        _require_client(self.deployment)
        store = self.store
        refresh = store.get(credentials.key(self.deployment.context, credentials.REFRESH_TOKEN))
        if not refresh:
            raise SignInRequired(sign_in_hint(self.deployment))
        secret = client_secret_for(self.deployment, store)
        if not secret:
            raise SwarmError(
                f"signed in to {self.deployment.context}, but its OAuth client secret is "
                "not in the credential store any more; run "
                f"`{terminal_command('sc login')}` to give it again"
            )
        try:
            tokens = _post(TOKEN_URI, {
                "client_id": self.deployment.client_id,
                "client_secret": secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            })
        except _OAuthError as exc:
            if exc.error == "invalid_grant":
                # Expired, revoked, or the password changed. The stored token is
                # dead; keeping it would make every call fail the same way.
                store.delete(credentials.key(self.deployment.context, credentials.REFRESH_TOKEN))
                raise SignInRequired(
                    sign_in_hint(self.deployment)
                    + ". The previous sign-in expired or was revoked"
                ) from None
            raise
        token = str(tokens.get("id_token") or "")
        if not token:
            raise SwarmError(
                "Google refreshed the sign-in but returned no ID token; run "
                f"`{terminal_command('sc login')}` again"
            )
        self._token = token
        self._claims = decode_claims(token)
        return token
