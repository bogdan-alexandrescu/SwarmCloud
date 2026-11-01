"""swarm-api's half of the ChatGPT-plan account pool (#2688).

The broker accepts `provider: "openai"` on its sign-in and register routes, and
`sc account add --provider openai` sends it. swarm-api sat between them pinning
every sign-in to `anthropic` and refusing `openai` on register, so a ChatGPT
plan could not be added through this API at all (the owner chose on
2026-11-01 to build this half).

WHAT IS PINNED HERE

  * a sign-in start naming `openai` reaches the broker with it, and the
    broker's answer (its provider and the localhost redirect) comes back;
  * a sign-in start naming nothing is a Claude sign-in, exactly as before;
  * any other provider is refused, naming the known ones, before the broker
    is called;
  * register accepts `openai`, and still refuses anything else by name with
    the known providers in the refusal's detail;
  * no answer and no log line carries key material: the broker alone holds it.

The end-to-end tests drive the shipped swarm-api routes through the shipped
`BrokerClient`, whose transport hands the bytes to the shipped broker app.
Nothing leaves the process.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from swarm_api.brokerclient import BrokerClient
from swarm_api.main import create_app as create_api

from .conftest import auth_header
from .test_openai_pool_accounts import (
    _Secrets,
    _SignInEndpoint,
    fake_jwt,
    id_token,
)

TENANT = "eng"


class _PlatformCaller:
    """swarm-api reaches the broker as a platform caller."""

    def resolve(self, authorization):
        return (None, True)


class _ClaudeEndpoint:
    """Anthropic's token endpoint, without a network."""

    def __init__(self):
        self.answer = {
            "access_token": secrets.token_hex(16),
            "refresh_token": secrets.token_hex(16),
            "expires_in": 28800,
        }

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        return dict(self.answer)


class _Bridge:
    """A BrokerClient transport that hands the request to the broker app."""

    def __init__(self, broker_client):
        self._c = broker_client
        self.bodies = []

    def __call__(self, method, url, *, headers, body=None, timeout=30.0):
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if body:
            self.bodies.append((parts.path, json.loads(body.decode())))
        r = self._c.request(method, path, content=body, headers=dict(headers))
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, data

    def sent(self, path):
        return [b for p, b in self.bodies if p == path]


class _Token:
    def token(self, audience: str) -> str:
        return "t"


@pytest.fixture()
def broker_app(broker, monkeypatch):
    from quota_broker.accountstore import AccountStore
    from quota_broker.main import create_app

    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(
        "BROKER_SERVICE_ACCOUNT", "swarm-quota-broker@p.iam.gserviceaccount.com"
    )
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE",
        "swarm-agent-worker-{tenant}@p.iam.gserviceaccount.com",
    )
    plan_endpoint = _SignInEndpoint()
    app = create_app(
        broker,
        identity=_PlatformCaller(),
        account_store=AccountStore(broker.db),
        plan_token_endpoint=plan_endpoint,
    )
    app.state.secret_store = _Secrets()
    app.state.token_endpoint = _ClaudeEndpoint()
    app.state.test_plan_endpoint = plan_endpoint
    return app


@pytest.fixture()
def api(api_context, broker_app):
    bridge = _Bridge(TestClient(broker_app, raise_server_exceptions=False))
    app = create_api(api_context)
    app.state.account_pool = BrokerClient(
        base_url="https://broker.invalid",
        audience="https://broker.invalid",
        transport=bridge,
        tokens=_Token(),
    )
    c = TestClient(app, raise_server_exceptions=False)
    c.bridge = bridge
    return c


def _key_material(*answers: dict) -> list[str]:
    out = []
    for answer in answers:
        out += [answer[k] for k in ("access_token", "refresh_token", "id_token") if k in answer]
    return out


# -- the sign-in start ------------------------------------------------------


def test_a_sign_in_naming_openai_reaches_the_broker_with_it(api):
    r = api.post(
        "/v1/accounts/authorize",
        headers=auth_header("alice"),
        json={"label": "plan", "provider": "openai"},
    )

    assert r.status_code == 200, r.text
    sent = api.bridge.sent("/v1/accounts/authorize")
    assert sent == [
        {"owner_tenant": TENANT, "label": "plan", "provider": "openai", "lend_to": []}
    ]
    body = r.json()
    # The broker's echo survives the proxy: `sc account add --provider openai`
    # refuses an answer that does not name the provider it asked for.
    assert body["provider"] == "openai"
    assert body["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert "verifier" not in r.text


def test_a_sign_in_naming_no_provider_is_a_claude_sign_in_as_before(api):
    r = api.post(
        "/v1/accounts/authorize", headers=auth_header("alice"), json={"label": "team"}
    )

    assert r.status_code == 200, r.text
    sent = api.bridge.sent("/v1/accounts/authorize")
    assert sent == [
        {"owner_tenant": TENANT, "label": "team", "provider": "anthropic", "lend_to": []}
    ]
    assert r.json()["provider"] == "anthropic"
    assert "localhost" not in r.json()["redirect_uri"]


@pytest.mark.parametrize("provider", ["gemini", "OpenAI ", ""])
def test_a_sign_in_naming_an_unknown_provider_is_refused_before_the_broker(
    api, provider
):
    r = api.post(
        "/v1/accounts/authorize",
        headers=auth_header("alice"),
        json={"label": "plan", "provider": provider},
    )

    assert r.status_code == 422, r.text
    errors = r.json()["detail"]["errors"]
    assert [e["loc"] for e in errors] == [["body", "provider"]]
    assert "'anthropic'" in errors[0]["msg"] and "'openai'" in errors[0]["msg"]
    assert api.bridge.sent("/v1/accounts/authorize") == []


def test_a_plan_sign_in_finishes_as_an_openai_account_with_no_token_in_the_answer(
    api, broker_app, caplog
):
    caplog.set_level(logging.DEBUG)
    started = api.post(
        "/v1/accounts/authorize",
        headers=auth_header("alice"),
        json={"label": "plan", "provider": "openai"},
    )
    assert started.status_code == 200, started.text

    done = api.post(
        "/v1/accounts/exchange",
        headers=auth_header("alice"),
        json={"state": started.json()["state"], "code": secrets.token_hex(8)},
    )

    assert done.status_code == 201, done.text
    assert done.json()["account"]["provider"] == "openai"
    assert done.json()["account"]["owner_tenant"] == TENANT
    # The exchange body carries no provider: the pending sign-in decides it.
    assert set(api.bridge.sent("/v1/accounts/exchange")[0]) == {
        "state",
        "code",
        "expected_owner",
    }
    material = _key_material(broker_app.state.test_plan_endpoint.answer)
    assert material
    for value in material:
        assert value not in started.text
        assert value not in done.text
        assert value not in caplog.text


# -- register ----------------------------------------------------------------


def test_register_accepts_a_chatgpt_plan_and_returns_no_key_material(
    api, broker_app, caplog
):
    caplog.set_level(logging.DEBUG)
    account_id = secrets.token_hex(6)
    tokens = {
        "id_token": id_token(account_id),
        "access_token": fake_jwt({"exp": int(time.time()) + 3600}),
        "refresh_token": secrets.token_hex(16),
        "account_id": account_id,
    }
    credential = json.dumps(
        {"OPENAI_API_KEY": None, "tokens": tokens, "last_refresh": "2026-11-01T19:00:00Z"}
    )

    r = api.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"label": "pasted", "provider": "openai", "credential": credential},
    )

    assert r.status_code == 201, r.text
    assert r.json()["account"]["provider"] == "openai"
    assert api.bridge.sent("/v1/accounts")[0]["provider"] == "openai"
    for value in _key_material(tokens):
        assert value not in r.text
        assert value not in caplog.text


@pytest.mark.parametrize("provider", ["acme", "gemini"])
def test_register_refuses_an_unknown_provider_by_name_with_the_known_ones(
    api, provider
):
    r = api.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"label": "x", "provider": provider, "credential": secrets.token_hex(8)},
    )

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "validation_failed"
    assert repr(provider) in body["message"]
    assert body["detail"]["known_providers"] == ["anthropic", "openai"]
    assert api.bridge.sent("/v1/accounts") == []


def test_swarm_api_knows_the_same_providers_the_broker_accepts():
    """swarm-api does not import the broker, so the pair is restated; this is
    what keeps the two lists from drifting."""
    from quota_broker.main import ACCOUNT_PROVIDERS as broker_providers

    from swarm_api import schemas

    assert tuple(schemas.ACCOUNT_PROVIDERS) == tuple(broker_providers)
