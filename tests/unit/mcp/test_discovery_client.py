"""The plugin needs one value: the bridge reads the rest from discovery.

DAYONE-PLUGIN-1 (plan §6, D28). A teammate configures `deployment_url` and
nothing else; the bridge reads the Desktop client id and the IAP audience from
`https://<H>/.well-known/swarmcloud`. A value the user did set still wins, so
the live deployment's three-value installs keep working. The document is
cached for an hour and read again when the API answers 401.
"""

from __future__ import annotations

import json
import secrets

import pytest

from swarm_mcp import auth, config, discovery
from swarm_mcp.client import SwarmClient, SwarmError

HOST = "swarm.example.test"
URL = f"https://{HOST}"
DOC_URL = f"{URL}/.well-known/swarmcloud"


def _client_id() -> str:
    return f"{secrets.randbelow(10**12)}-{secrets.token_hex(8)}.apps.googleusercontent.com"


def _document(client_id: str, **extra) -> dict:
    return {
        "schema": 1,
        "name": "example",
        "version": "1.4.0",
        "console_url": f"{URL}/",
        "api_url": f"{URL}/v1",
        "oauth_client_id": client_id,
        "iap_audience": "/projects/123/global/backendServices/456",
        "plugin": {"min_version": "0.1.0"},
        **extra,
    }


class FakeDiscovery:
    """The discovery service, counted: what was asked, and how often."""

    def __init__(self, document: dict) -> None:
        self.document = document
        self.asked: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:  # noqa: ARG002
        self.asked.append(url)
        return 200, json.dumps(self.document).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def served(monkeypatch):
    fake = FakeDiscovery(_document(_client_id()))
    clock = Clock()
    cache = discovery.Cache(get=fake, clock=clock)
    monkeypatch.setattr(discovery, "CACHE", cache)
    fake.clock = clock
    return fake


def _plugin_env(tmp_path, **values) -> dict[str, str]:
    environ = {"SWARM_CONFIG_DIR": str(tmp_path), config.plugin_env(config.PLUGIN_URL): URL}
    for key, value in values.items():
        environ[config.plugin_env(key)] = value
    return environ


def test_one_value_fills_client_id_and_audience(tmp_path, served):
    deployment = config.resolve(_plugin_env(tmp_path))

    assert deployment is not None
    assert deployment.url == URL
    assert deployment.client_id == served.document["oauth_client_id"]
    assert deployment.iap_audience == served.document["iap_audience"]
    assert deployment.front_door is True
    assert deployment.skew == config.SKEW_REQUIRED
    assert served.asked == [DOC_URL]


def test_one_value_takes_the_served_secret_only_with_the_served_client(tmp_path, served):
    served.document["oauth_client_secret"] = secrets.token_hex(12)

    deployment = config.resolve(_plugin_env(tmp_path))

    assert deployment.client_secret == served.document["oauth_client_secret"]


def test_user_config_wins(tmp_path, served):
    mine = _client_id()
    secret = secrets.token_hex(12)
    served.document["oauth_client_secret"] = secrets.token_hex(12)

    deployment = config.resolve(_plugin_env(tmp_path, oauth_client_id=mine, oauth_client_secret=secret))

    assert deployment.client_id == mine
    assert deployment.client_secret == secret
    # Every value was configured, so resolving asks discovery for nothing; the
    # client still checks skew whenever the document can be read.
    assert served.asked == []
    assert deployment.skew == config.SKEW_CHECKED


def test_a_solo_run_app_deployment_is_never_asked_for_a_document(tmp_path, served):
    environ = {
        "SWARM_CONFIG_DIR": str(tmp_path),
        config.plugin_env(config.PLUGIN_URL): "https://swarm-api-abc123-ew.a.run.app",
    }

    deployment = config.resolve(environ)

    assert deployment.client_id == ""
    assert deployment.skew == ""
    assert served.asked == []


def test_one_value_with_no_document_names_the_address_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=lambda url, timeout: (404, b"")))

    with pytest.raises(discovery.DiscoveryUnreachable) as caught:
        config.resolve(_plugin_env(tmp_path))

    assert str(caught.value) == f"cannot read {DOC_URL} (404)"
    assert caught.value.exit_code == 3


def _client(deployment) -> SwarmClient:
    client = SwarmClient(deployment=deployment, connect=False, tier=auth.Tier.EXPLICIT)
    client.base_url = URL
    client._sends_cached_credential = lambda: False
    return client


def test_discovery_cached_and_refetched_on_401(tmp_path, served):
    deployment = config.resolve(_plugin_env(tmp_path))
    client = _client(deployment)
    answers: list = [{"ok": 1}, {"ok": 2}, SwarmError("refused", status=401)]

    def send(method, path, **kwargs):  # noqa: ARG001
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client._send = send

    assert client.request("GET", "/v1/tasks") == {"ok": 1}
    assert client.request("GET", "/v1/tasks") == {"ok": 2}
    # Resolving read it once; two calls within the hour read nothing more.
    assert served.asked == [DOC_URL]

    with pytest.raises(SwarmError) as caught:
        client.request("GET", "/v1/tasks")
    assert caught.value.status == 401
    # The 401 read the document again: a rotated client is picked up.
    assert served.asked == [DOC_URL, DOC_URL]


def test_discovery_read_again_after_an_hour(tmp_path, served):
    deployment = config.resolve(_plugin_env(tmp_path))
    client = _client(deployment)
    client._send = lambda method, path, **kwargs: {}

    client.request("GET", "/v1/tasks")
    served.clock.now += discovery.TTL_SECONDS + 1
    client.request("GET", "/v1/tasks")

    assert served.asked == [DOC_URL, DOC_URL]


def test_a_configured_install_reaches_a_deployment_that_predates_discovery(tmp_path, monkeypatch):
    asked: list[str] = []

    def missing(url, timeout):  # noqa: ARG001
        asked.append(url)
        return 404, b""

    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=missing))
    deployment = config.resolve(
        _plugin_env(tmp_path, oauth_client_id=_client_id(), oauth_client_secret=secrets.token_hex(12))
    )
    client = _client(deployment)
    client._send = lambda method, path, **kwargs: {"ok": True}

    assert client.request("GET", "/v1/tasks") == {"ok": True}
    assert client.request("GET", "/v1/tasks") == {"ok": True}
    assert client.skew.unchecked == f"cannot read {DOC_URL} (404)"
    # Asked once, not once per call: a missing document is remembered for the hour.
    assert asked == [DOC_URL]
