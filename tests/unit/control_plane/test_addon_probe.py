"""The brave-search add-on's probe: `POST /v1/admin/setup/addons/brave-search/probe`
(lane DAYONE-SEARCH-PROBE, part of #2688).

One fixed search through the per-attempt route's provider and key source, as
swarm-api, for a platform admin; the answer is `{ok, reason, status, results}`
and never the key, a header or the provider's body. When swarm-api may not
read the key, it is `ok: false` naming the missing access, not a 500.
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from google.api_core import exceptions as gexc

from swarm_api.routes import addon_probe
from swarm_api.search import BraveSearch, SecretManagerKey

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    setup_app,
    setup_client,
    tokens,
)

PROBE = "/v1/admin/setup/addons/brave-search/probe"
SECRET = "swarm-search-api-key"
FIELDS = {"ok", "reason", "status", "results"}


def make_key() -> str:
    # Runtime-built: no line of this file holds a credential-shaped literal.
    return "BSA" + secrets.token_hex(16)


class Brave:
    """Brave's web search endpoint, faked: records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.status = 200

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        body = {"web": {"results": [{"title": "Private title", "url": "https://example.com/hit",
                                     "description": "a snippet"}]}}
        return self.status, json.dumps(body).encode()


class Key:
    def __init__(self) -> None:
        self.value = make_key()

    def read(self) -> str:
        return self.value


class Versions:
    """Secret Manager's access call, failing as Google does."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def access_secret_version(self, request):
        raise self.error


@pytest.fixture
def brave() -> Brave:
    return Brave()


@pytest.fixture
def key() -> Key:
    return Key()


def _probe(app, brave: Brave, key, *, enabled: bool = True) -> None:
    app.state.search_probe = addon_probe.SearchProbe(
        enabled=enabled, secret=SECRET, provider=BraveSearch(send=brave), key=key)


def test_one_fixed_search_answers_ok_and_a_count(setup_app, setup_client, brave, key) -> None:
    _probe(setup_app, brave, key)
    response = setup_client.post(PROBE, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == FIELDS
    assert body["ok"] is True and body["status"] == 200 and body["results"] == 1
    assert len(brave.calls) == 1
    method, url, headers = brave.calls[0]
    assert method == "GET"
    assert parse_qs(urlparse(url).query) == {"q": [addon_probe.PROBE_QUERY], "count": ["1"]}
    assert headers["X-Subscription-Token"] == key.value


def test_the_answer_carries_no_key_header_or_result_text(setup_app, setup_client, brave, key) -> None:
    _probe(setup_app, brave, key)
    ok = setup_client.post(PROBE, headers=auth_header("root")).text
    brave.status = 401
    refused = setup_client.post(PROBE, headers=auth_header("root"))
    assert refused.status_code == 200
    assert refused.json()["ok"] is False and refused.json()["status"] == 401
    assert set(refused.json()) == FIELDS
    for text in (ok, refused.text):
        for leaked in (key.value, "X-Subscription-Token", "Private title", "example.com/hit", "a snippet"):
            assert leaked not in text, (leaked, text)


def test_no_read_access_is_a_reason_naming_it_not_a_500(setup_app, setup_client, brave) -> None:
    denied = SecretManagerKey("acme-swarm", SECRET, client=Versions(gexc.PermissionDenied("denied")))
    _probe(setup_app, brave, denied)
    response = setup_client.post(PROBE, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["results"] == 0 and body["status"] is None
    assert SECRET in body["reason"] and "roles/secretmanager.secretAccessor" in body["reason"]
    assert brave.calls == [], "no search without a key"


def test_a_key_with_no_version_says_so(setup_app, setup_client, brave) -> None:
    missing = SecretManagerKey("acme-swarm", SECRET, client=Versions(gexc.NotFound("none")))
    _probe(setup_app, brave, missing)
    body = setup_client.post(PROBE, headers=auth_header("root")).json()
    assert body["ok"] is False and "no enabled version" in body["reason"]


def test_the_search_tool_off_is_not_ok_and_reads_no_key(setup_app, setup_client, brave) -> None:
    class Unread:
        def read(self) -> str:
            raise AssertionError("the key was read while the tool is off")

    _probe(setup_app, brave, Unread(), enabled=False)
    body = setup_client.post(PROBE, headers=auth_header("root")).json()
    assert body["ok"] is False and "SEARCH_TOOL_ENABLED" in body["reason"]
    assert brave.calls == []


@pytest.mark.parametrize("user", ["wadmin", "alice", "carol", "deployer", "doctor"])
def test_only_a_platform_admin_may_probe(setup_app, setup_client, brave, key, user) -> None:
    _probe(setup_app, brave, key)
    response = setup_client.post(PROBE, headers=auth_header(user))
    assert response.status_code == 403, (user, response.text)
    assert brave.calls == []


def test_built_from_settings_with_the_per_attempt_routes_key_and_provider() -> None:
    from .conftest import api_settings

    probe = addon_probe.SearchProbe.from_settings(api_settings(search_tool_enabled=True))
    assert probe.enabled is True and probe.secret == SECRET
    assert isinstance(probe.provider, BraveSearch)
    assert isinstance(probe.key, SecretManagerKey) and probe.key.secret == SECRET
    assert probe.key.client is None, "no Google client is built before the first probe"
