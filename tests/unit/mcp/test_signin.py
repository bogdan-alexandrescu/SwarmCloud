"""Signing in AS YOURSELF, through IAP, with one OAuth client per deployment.

WHY THIS EXISTS. Measured 2026-09-24 against https://swarm.saga.xyz: a user's
own gcloud access token is refused by IAP with 401, error code 900, because the
deployment uses a Google-managed IAP OAuth client and "Google-managed OAuth
clients cannot programmatically access IAP-protected applications"
(cloud.google.com/iap/docs/custom-oauth-configuration). The only way a laptop
got through was impersonating a service account -- so every developer acted as
`swarm-verify`, not as themselves, and the tenant boundary saw one caller.

Google's documented answer for a desktop client (cloud.google.com/iap/docs/
authentication-howto, "Authenticate from a desktop app") is: a Desktop OAuth
client, allowlisted on the IAP resource through `programmatic_clients`; the
installed-app loopback flow; the `id_token` from the token endpoint sent as
`Authorization: Bearer`; and the refresh token to mint the next one. That is
what `sc login` does, and what these tests hold it to.

NO REAL GOOGLE CALL ANYWHERE. `FakeGoogle` is an OAuth authorisation server on
127.0.0.1 that checks what Google checks -- the state round-trip, the PKCE
verifier against its challenge, the client secret, the redirect URI -- and
mints unsigned JWTs. The "browser" is a thread that follows its redirect back
to the loopback listener, the way a real one does.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib
import io
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swarm_mcp import auth, client, sc, server
from swarm_mcp.client import SwarmClient, SwarmError

CLIENT_ID = "209012342332-abcdef.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-desktop-secret"
EMAIL = "dev@example.test"


def _signin():
    return importlib.import_module("swarm_mcp.signin")


def _config():
    return importlib.import_module("swarm_mcp.config")


def _credentials():
    return importlib.import_module("swarm_mcp.credentials")


class FakeStore:
    name = "fake"

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def describe(self) -> str:
        return "a fake store"

    def get(self, key: str) -> str | None:
        return self.items.get(key)

    def set(self, key: str, value: str) -> None:
        self.items[key] = value

    def delete(self, key: str) -> bool:
        return self.items.pop(key, None) is not None


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'RS256', 'kid': 'fake'})}.{_b64(claims)}.c2lnbmF0dXJl"


class FakeGoogle:
    """accounts.google.com and oauth2.googleapis.com, on loopback."""

    def __init__(self, *, deny: bool = False, id_token_ttl: int = 3600) -> None:
        self.deny = deny
        self.id_token_ttl = id_token_ttl
        self.code = "4/fake-authorisation-code"
        self.refresh_token = "1//fake-refresh-token"
        self.challenge: str | None = None
        self.auth_params: dict[str, str] = {}
        self.token_requests: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.minted = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ARG002
                pass

            def _json(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                params = dict(urllib.parse.parse_qsl(parsed.query))
                fake.auth_params = params
                fake.challenge = params.get("code_challenge")
                back = {"state": params.get("state", "")}
                if fake.deny:
                    back["error"] = "access_denied"
                else:
                    back["code"] = fake.code
                self.send_response(302)
                self.send_header(
                    "Location", f"{params['redirect_uri']}?{urllib.parse.urlencode(back)}"
                )
                self.end_headers()

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                if self.path.startswith("/revoke"):
                    fake.revoked.append(form.get("token", ""))
                    return self._json(200, {})
                fake.token_requests.append(form)
                if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                    return self._json(401, {"error": "invalid_client"})
                grant = form.get("grant_type")
                if grant == "authorization_code":
                    verifier = form.get("code_verifier", "")
                    expected = (
                        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                        .rstrip(b"=")
                        .decode()
                    )
                    if form.get("code") != fake.code or expected != fake.challenge:
                        return self._json(400, {"error": "invalid_grant"})
                    if form.get("redirect_uri") != fake.auth_params.get("redirect_uri"):
                        return self._json(400, {"error": "redirect_uri_mismatch"})
                    return self._json(200, fake._tokens(refresh=True))
                if grant == "refresh_token":
                    if form.get("refresh_token") != fake.refresh_token or fake.refresh_token in fake.revoked:
                        return self._json(
                            400,
                            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
                        )
                    return self._json(200, fake._tokens(refresh=False))
                return self._json(400, {"error": "unsupported_grant_type"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.auth_uri = f"{base}/o/oauth2/v2/auth"
        self.token_uri = f"{base}/token"
        self.revoke_uri = f"{base}/revoke"

    def _tokens(self, *, refresh: bool) -> dict:
        self.minted += 1
        body = {
            "access_token": f"ya29.fake-{self.minted}",
            "expires_in": 3599,
            "scope": "openid https://www.googleapis.com/auth/userinfo.email",
            "token_type": "Bearer",
            "id_token": _jwt(
                {
                    "iss": "https://accounts.google.com",
                    "aud": CLIENT_ID,
                    "email": EMAIL,
                    "email_verified": True,
                    "exp": int(time.time()) + self.id_token_ttl,
                    "n": self.minted,
                }
            ),
        }
        if refresh:
            body["refresh_token"] = self.refresh_token
        return body

    def browser(self, url: str) -> bool:
        """A browser: follow Google's redirect back to the loopback listener,
        in the background, the way `webbrowser.open` returns at once."""

        def _visit() -> None:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(url, timeout=10).read()

        threading.Thread(target=_visit, daemon=True).start()
        return True

    def close(self) -> None:
        self.httpd.shutdown()


@pytest.fixture()
def google(monkeypatch):
    fake = FakeGoogle()
    signin = _signin()
    monkeypatch.setattr(signin, "AUTH_URI", fake.auth_uri)
    monkeypatch.setattr(signin, "TOKEN_URI", fake.token_uri)
    monkeypatch.setattr(signin, "REVOKE_URI", fake.revoke_uri)
    monkeypatch.setattr(signin, "open_browser", fake.browser)
    yield fake
    fake.close()


@pytest.fixture()
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(_credentials(), "store", lambda environ=None: fake)
    return fake


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in (
        "SWARM_API_URL", "API_URL", "SWARM_API_HOST", "API_HOST", "SWARM_REPO_ROOT",
        "PROJECT_ID", "SWARM_ID_TOKEN", "SWARM_IAP_CLIENT_ID", "SWARM_IMPERSONATE_SA",
        "SWARM_ACCESS_TOKEN", "K_SERVICE", "CLOUD_RUN_JOB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: False)
    monkeypatch.setattr(
        client, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"gcloud: {a}"))
    )


@pytest.fixture()
def team(store):
    """A team deployment's context, with its client secret already stored."""
    _config().add_context("team", "https://swarm.example.test", client_id=CLIENT_ID)
    store.set("team/oauth-client-secret", CLIENT_SECRET)
    return _config().resolve()


# ==========================================================================
# sc login
# ==========================================================================


def test_login_runs_the_loopback_flow_and_stores_the_refresh_token(google, store, team):
    claims = _signin().login(team)

    assert claims["email"] == EMAIL
    assert store.items["team/refresh-token"] == google.refresh_token
    # Google's desktop flow, as documented: openid + email, offline, a
    # loopback redirect, and PKCE with S256 -- the verifier checked by the fake.
    params = google.auth_params
    assert params["client_id"] == CLIENT_ID
    assert set(params["scope"].split()) == {"openid", "email"}
    assert params["access_type"] == "offline"
    assert params["redirect_uri"].startswith("http://127.0.0.1:")
    assert params["code_challenge_method"] == "S256"
    assert params["state"]
    # Consent is asked every time, because Google issues a refresh token only
    # on consent -- a second `sc login` after `sc logout` would otherwise get
    # an id token and no way to mint the next one.
    assert params.get("prompt") == "consent"


def test_a_refused_consent_stores_nothing_and_says_so(monkeypatch, store, team):
    fake = FakeGoogle(deny=True)
    signin = _signin()
    monkeypatch.setattr(signin, "AUTH_URI", fake.auth_uri)
    monkeypatch.setattr(signin, "TOKEN_URI", fake.token_uri)
    monkeypatch.setattr(signin, "open_browser", fake.browser)
    try:
        with pytest.raises(SwarmError) as caught:
            signin.login(team)
    finally:
        fake.close()
    assert "access_denied" in str(caught.value)
    assert "team/refresh-token" not in store.items


def test_a_callback_with_the_wrong_state_is_refused(monkeypatch, google, store, team):
    """The state parameter is what stops another page on this machine from
    completing a sign-in the user did not start."""
    signin = _signin()

    def _forged(url: str) -> bool:
        redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["redirect_uri"]

        def _visit() -> None:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(f"{redirect}?code=stolen&state=not-the-state", timeout=10).read()

        threading.Thread(target=_visit, daemon=True).start()
        return True

    monkeypatch.setattr(signin, "open_browser", _forged)
    with pytest.raises(SwarmError) as caught:
        signin.login(team, timeout=10)
    assert "state" in str(caught.value).lower()
    assert "team/refresh-token" not in store.items


def test_login_without_a_client_id_names_the_fix(store):
    _config().add_context("bare", "https://swarm.example.test")
    with pytest.raises(SwarmError) as caught:
        _signin().login(_config().resolve())
    assert "--client-id" in str(caught.value)


def test_the_cli_login_signs_in_and_reports_the_principal(monkeypatch, google, store, team):
    """`sc login` end to end: flow, store, then ONE call to the API with the
    new token, so the user learns now -- not at their first dispatch -- if the
    deployment has not allowlisted this client."""
    seen: list[str] = []

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _opener(request, timeout=None):  # noqa: ARG001
        seen.append(request.get_header("Authorization"))
        return _Response(json.dumps({"email": EMAIL, "tenant_id": "u-dev"}).encode())

    monkeypatch.setattr(client, "_open", _opener)
    out = io.StringIO()
    code = sc.main(["login"], out=out)
    assert code == 0, out.getvalue()
    assert EMAIL in out.getvalue() and "u-dev" in out.getvalue()
    assert seen and seen[0].startswith("Bearer ") and seen[0].count(".") == 2, seen
    # Nothing on this machine outranks the sign-in, so nothing to warn about.
    assert "not as you" not in out.getvalue()


@pytest.mark.parametrize(
    "higher,named",
    [
        ("SWARM_IMPERSONATE_SA", "ci-runner@example.iam.gserviceaccount.com"),
        ("SWARM_ID_TOKEN", "SWARM_ID_TOKEN"),
        ("metadata", "metadata server"),
    ],
)
def test_login_checks_the_sign_in_it_made_not_a_tier_that_outranks_it(
    monkeypatch, google, store, team, higher, named
):
    """REVIEW OF PR #61, 2026-09-25: `sc login` verified the WRONG credential.

    It ended by calling the API through `SwarmClient(deployment=...)`, whose
    tier comes from `auth.detect()` -- and detect ranks SWARM_ID_TOKEN, the
    metadata server and SWARM_IMPERSONATE_SA above a sign-in. On any of those
    the check ran as the service account and never presented the token just
    minted, so it printed "signed in ... as dev@..." and the SERVICE ACCOUNT's
    tenant, and exited 0 even when IAP had not allowlisted the Desktop client.
    The runbook's step 5 and the PR's owner steps both read that output as
    proof the allowlist works. SWARM_IMPERSONATE_SA was the README's
    documented laptop setup until this PR, and the metadata tier is what
    Cloud Shell and Workstations are on.

    So: exactly one request, carrying the ID token the sign-in minted
    (audience: the Desktop client), whatever else is set -- and the developer
    is TOLD that everything else on this machine still acts as that other
    identity, not as them.
    """
    if higher == "metadata":
        monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: True)
    elif higher == "SWARM_ID_TOKEN":
        monkeypatch.setenv("SWARM_ID_TOKEN", "an.explicit.token")
    else:
        monkeypatch.setenv(higher, named)
    seen: list[str] = []

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _opener(request, timeout=None):  # noqa: ARG001
        seen.append(request.get_header("Authorization"))
        return _Response(json.dumps({"email": EMAIL, "tenant_id": "u-dev"}).encode())

    monkeypatch.setattr(client, "_open", _opener)
    out = io.StringIO()
    code = sc.main(["login"], out=out)
    text = out.getvalue()

    assert code == 0, text
    assert len(seen) == 1, f"one verification request, not {len(seen)}"
    claims = _signin().decode_claims(seen[0].removeprefix("Bearer "))
    assert claims["email"] == EMAIL and claims["aud"] == CLIENT_ID, (
        "the check must present the ID token `sc login` just minted, not the "
        f"credential of a tier that outranks it: {claims}"
    )
    assert "not as you" in text and named in text, text


# ==========================================================================
# Silent refresh
# ==========================================================================


def test_a_new_process_mints_its_id_token_from_the_stored_refresh_token(google, store, team):
    store.set("team/refresh-token", google.refresh_token)
    signin = _signin()
    source = signin.SignedIn(team)

    first = source.id_token()
    second = source.id_token()
    assert first == second, "an unexpired token is reused, not re-minted per request"
    refreshes = [r for r in google.token_requests if r["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1
    assert signin.decode_claims(first)["aud"] == CLIENT_ID, (
        "IAP's programmatic allowlist admits a token whose audience is the "
        "allowlisted client -- the desktop client id, not the service url"
    )


def test_a_token_near_expiry_is_refreshed_before_it_is_sent(monkeypatch, store, team):
    """Minting a few minutes early avoids a long `sc` or `swarm tail` dying at
    the 59-minute mark with a 401 that reads like a permission problem."""
    fake = FakeGoogle(id_token_ttl=60)
    signin = _signin()
    monkeypatch.setattr(signin, "TOKEN_URI", fake.token_uri)
    store.set("team/refresh-token", fake.refresh_token)
    try:
        source = signin.SignedIn(team)
        source.id_token()
        source.id_token()
    finally:
        fake.close()
    assert len([r for r in fake.token_requests if r["grant_type"] == "refresh_token"]) == 2


def test_the_front_door_is_sent_the_signed_in_id_token(monkeypatch, google, store, team):
    store.set("team/refresh-token", google.refresh_token)
    headers: list[str] = []

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _opener(request, timeout=None):  # noqa: ARG001
        headers.append(request.get_header("Authorization"))
        return _Response(b'{"runtimes": []}')

    monkeypatch.setattr(client, "_open", _opener)
    built = SwarmClient()
    assert built.tier is auth.Tier.SIGNED_IN
    assert built.front_door is True
    built.request("GET", "/v1/runtimes")
    token = headers[0].removeprefix("Bearer ")
    assert _signin().decode_claims(token)["email"] == EMAIL


# ==========================================================================
# Not signed in, logout
# ==========================================================================


def test_not_signed_in_names_the_context_and_the_command(store, team):
    built = SwarmClient()
    with pytest.raises(SwarmError) as caught:
        built.request("GET", "/v1/stats")
    message = str(caught.value)
    assert message.startswith("sign-in required for team:"), message
    assert "sc login" in message
    assert "browser" in message


def test_a_tool_call_answers_sign_in_required_for_the_plugins_context(monkeypatch, store):
    """What the owner asked for, verbatim in shape: an MCP tool answers
    'sign-in required for <context>: run sc login (a browser window opens)'."""
    monkeypatch.setenv("SWARM_PLUGIN_DEPLOYMENT_URL", "https://plugin.example.test")
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_SECRET", CLIENT_SECRET)

    requests = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "swarm_status", "arguments": {"task_ids": ["t_1"]}},
        }),
    ]
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    server.serve(stdin=io.StringIO("\n".join(requests) + "\n"))
    replies = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    call = next(r for r in replies if r.get("id") == 2)["result"]
    assert call.get("isError") is True
    text = call["content"][0]["text"]
    assert text.startswith("sign-in required for plugin.example.test:"), text
    assert "sc login" in text and "browser" in text


def test_logout_forgets_and_revokes(google, store, team):
    store.set("team/refresh-token", google.refresh_token)
    signin = _signin()
    assert signin.logout(team) is True
    assert "team/refresh-token" not in store.items
    assert google.revoked == [google.refresh_token], "logout must revoke at Google, not only forget"
    assert store.items.get("team/oauth-client-secret") == CLIENT_SECRET, (
        "the client secret belongs to the deployment, not to the person; logout keeps it"
    )
    with pytest.raises(SwarmError) as caught:
        signin.SignedIn(team).id_token()
    assert "sign-in required for team" in str(caught.value)


def test_a_revoked_refresh_token_asks_for_sign_in_again(google, store, team):
    store.set("team/refresh-token", google.refresh_token)
    google.revoked.append(google.refresh_token)
    with pytest.raises(SwarmError) as caught:
        _signin().SignedIn(team).id_token()
    assert "sign-in required for team" in str(caught.value)
    assert google.refresh_token not in str(caught.value), "never echo a credential"


def test_whoami_shows_context_url_principal_and_tenant(monkeypatch, google, store, team):
    store.set("team/refresh-token", google.refresh_token)

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        client,
        "_open",
        lambda request, timeout=None: _Response(json.dumps({"email": EMAIL, "tenant_id": "u-dev"}).encode()),
    )
    out = io.StringIO()
    assert sc.main(["whoami", "--json"], out=out) == 0
    shown = json.loads(out.getvalue())
    assert shown["context"] == "team"
    assert shown["url"] == "https://swarm.example.test"
    assert shown["principal"] == EMAIL
    assert shown["tenant"] == "u-dev"
    assert google.refresh_token not in out.getvalue()


def test_whoami_when_signed_out_says_so_and_fails(store, team):
    out = io.StringIO()
    code = sc.main(["whoami"], out=out)
    assert code == 1
    assert "sc login" in out.getvalue()


# ==========================================================================
# The macOS Keychain backend, without the Keychain
# ==========================================================================


def test_the_keychain_is_given_the_secret_on_stdin_never_in_argv(monkeypatch):
    """argv is readable by `ps`. The secret goes to `security -i` on stdin."""
    credentials = _credentials()
    calls: list[tuple[list[str], str | None]] = []

    def _run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(credentials.subprocess, "run", _run)
    credentials.KeychainStore().set("team/refresh-token", "1//secret-value")
    assert calls
    for argv, stdin in calls:
        assert not any("1//secret-value" in arg for arg in argv), argv
    assert any(stdin and "1//secret-value" in stdin for _, stdin in calls)


@pytest.mark.parametrize("value", ['has"quote', "has\nnewline", "has\\backslash", "has space"])
def test_the_keychain_refuses_a_value_that_could_inject_a_second_command(monkeypatch, value):
    """`security -i` reads one command per LINE. A newline in a value would be
    a second command, run with the user's keychain authority."""
    credentials = _credentials()
    monkeypatch.setattr(
        credentials.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("security was run")),
    )
    with pytest.raises(SwarmError):
        credentials.KeychainStore().set("team/refresh-token", value)


def test_the_file_store_is_private(tmp_path, monkeypatch):
    """Linux without a Secret Service falls back to a 0600 file, and says so."""
    credentials = _credentials()
    monkeypatch.setenv("SWARM_CREDENTIAL_STORE", "file")
    chosen = credentials.store()
    chosen.set("team/refresh-token", "1//r")
    assert chosen.get("team/refresh-token") == "1//r"
    path = chosen.path
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert "0600" in chosen.describe()
