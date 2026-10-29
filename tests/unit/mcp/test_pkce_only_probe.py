"""`sc login --pkce-only` is Q1's one-request probe (plan §6.3).

The installed-app flow with PKCE and NO client secret: none is read, asked
for, stored or sent. Whether Google accepts that for the deployment's Desktop
client decides whether discovery must serve the secret. A one-value install
whose document served no secret signs in the same way, and refreshes the same
way.
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

import pytest

from swarm_mcp import config, credentials, discovery, signin

URL = "https://swarm.example.test"


class Store:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def _jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.{secrets.token_hex(8)}"


@pytest.fixture()
def google(monkeypatch):
    """Google's two endpoints, recording every form posted to the token one."""
    posted: list[dict[str, str]] = []

    def post(url, form, *, timeout=30.0):  # noqa: ARG001
        posted.append(dict(form))
        body = {"id_token": _jwt({"email": "dev@example.com", "exp": int(time.time()) + 3600})}
        if form.get("grant_type") == "authorization_code":
            body["refresh_token"] = secrets.token_hex(16)
        return body

    def browser(url: str) -> bool:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        back = urllib.parse.urlencode({"code": secrets.token_hex(6), "state": query["state"]})

        def visit() -> None:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(f"{query['redirect_uri']}?{back}", timeout=10).read()

        threading.Thread(target=visit, daemon=True).start()
        return True

    monkeypatch.setattr(signin, "_post", post)
    monkeypatch.setattr(signin, "open_browser", browser)
    return posted


def _deployment(**overrides) -> config.Deployment:
    values = {
        "context": "team",
        "url": URL,
        "client_id": f"{secrets.randbelow(10**9)}-{secrets.token_hex(6)}.apps.googleusercontent.com",
        "source": "test",
        "front_door": True,
    }
    values.update(overrides)
    return config.Deployment(**values)


def test_probe_sends_no_secret(google):
    store = Store()
    known = secrets.token_hex(12)
    deployment = _deployment()
    # A secret this machine already knows is still not sent by the probe.
    store.set(credentials.key("team", credentials.CLIENT_SECRET), known)

    claims = signin.login(deployment, store=store, pkce_only=True, timeout=10)

    assert claims["email"] == "dev@example.com"
    assert len(google) == 1
    form = google[0]
    assert "client_secret" not in form
    assert known not in json.dumps(form)
    assert form["code_verifier"] and form["grant_type"] == "authorization_code"


def test_a_one_value_install_without_a_served_secret_signs_in_and_refreshes_with_pkce(google):
    store = Store()
    deployment = _deployment(skew=config.SKEW_REQUIRED)

    signin.login(deployment, store=store, timeout=10)
    token = signin.SignedIn(deployment, store=store).id_token()

    assert token
    assert [form["grant_type"] for form in google] == ["authorization_code", "refresh_token"]
    assert all("client_secret" not in form for form in google)
    assert credentials.key("team", credentials.CLIENT_SECRET) not in store.values


def test_a_refused_probe_names_the_switch(monkeypatch, google):  # noqa: ARG001
    def refuse(url, form, *, timeout=30.0):  # noqa: ARG001
        raise signin._OAuthError("invalid_client", "client_secret is missing.", 401)

    monkeypatch.setattr(signin, "_post", refuse)

    with pytest.raises(signin.SwarmError, match="discovery_serves_client_secret = true"):
        signin.login(_deployment(), store=Store(), pkce_only=True, timeout=10)


def test_the_served_secret_is_used_when_discovery_serves_one(google):
    served = secrets.token_hex(12)
    deployment = _deployment(skew=config.SKEW_REQUIRED, client_secret=served)

    signin.login(deployment, store=Store(), timeout=10)

    assert google[0]["client_secret"] == served


def test_discovery_module_is_what_fills_the_secret():
    # The one place a served secret enters: `discovery.fill`, beside its client.
    assert "oauth_client_secret" in discovery.Document.__dataclass_fields__
