"""A plugin/deployment version skew is always named, never silent (plan §6.5).

The plugin is installed from the release bucket, so it can run ahead of, or
behind, the deployment it talks to. Each message names both versions and the
fix, and every tool call that reaches the API answers it.
"""

from __future__ import annotations

import json
import secrets

import pytest

from swarm_mcp import auth, config, discovery
from swarm_mcp.client import SwarmClient, SwarmError

URL = "https://swarm.example.test"


def _bundle(tmp_path, *, plugin: str, low: str = "", high: str = "") -> str:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "sc", "version": plugin}))
    if low or high:
        (root / "compat.json").write_text(
            json.dumps({"plugin": plugin, "deployments": {"min": low, "max_exclusive": high}})
        )
    return str(root)


def _served(monkeypatch, *, version: str, min_plugin: str) -> tuple[list[str], dict]:
    asked: list[str] = []
    document = {
        "schema": 1,
        "name": "example",
        "version": version,
        "console_url": f"{URL}/",
        "api_url": f"{URL}/v1",
        "oauth_client_id": f"{secrets.randbelow(10**9)}-{secrets.token_hex(6)}.apps.googleusercontent.com",
        "iap_audience": "/projects/1/global/backendServices/2",
        "plugin": {"min_version": min_plugin},
    }

    def get(url, timeout):  # noqa: ARG001
        asked.append(url)
        return 200, json.dumps(document).encode()

    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=get))
    return asked, document


def _environ(tmp_path, root: str) -> dict[str, str]:
    return {
        "SWARM_CONFIG_DIR": str(tmp_path),
        config.plugin_env(config.PLUGIN_URL): URL,
        discovery.PLUGIN_ROOT_ENV: root,
    }


def _client(tmp_path, monkeypatch, root: str) -> SwarmClient:
    environ = _environ(tmp_path, root)
    monkeypatch.setenv(discovery.PLUGIN_ROOT_ENV, root)
    deployment = config.resolve(environ)
    client = SwarmClient(deployment=deployment, connect=False, tier=auth.Tier.EXPLICIT)
    client.base_url = URL
    client._sends_cached_credential = lambda: False
    return client


def test_plugin_older_than_min_names_both_versions_and_fix(tmp_path, monkeypatch):
    _served(monkeypatch, version="1.6.0", min_plugin="1.5.0")
    client = _client(tmp_path, monkeypatch, _bundle(tmp_path, plugin="1.4.2"))
    client._send = lambda method, path, **kwargs: {}

    with pytest.raises(discovery.VersionSkew) as caught:
        client.request("GET", "/v1/tasks")

    assert str(caught.value) == (
        "sc plugin 1.4.2 is older than this deployment (1.6.0) accepts (needs ≥ 1.5.0). "
        "Update it: claude plugin update sc@swarmcloud"
    )


def test_deployment_older_than_supported_names_both_versions_and_fix(tmp_path, monkeypatch):
    _served(monkeypatch, version="1.2.0", min_plugin="1.0.0")
    client = _client(tmp_path, monkeypatch, _bundle(tmp_path, plugin="1.5.0", low="1.3.0", high="2.0.0"))
    client._send = lambda method, path, **kwargs: {}

    with pytest.raises(discovery.VersionSkew) as caught:
        client.request("GET", "/v1/tasks")

    assert str(caught.value) == (
        "this deployment runs SwarmCloud 1.2.0; plugin 1.5.0 supports 1.3.0 to before 2.0.0. "
        "Ask your admin to run: swarm upgrade --to 1.5.0. Or pin the plugin to your deployment: "
        "claude plugin marketplace add "
        "https://storage.googleapis.com/swarmcloud/releases/1.2.0/marketplace.json"
    )


def test_deployment_newer_than_supported_is_named_too(tmp_path, monkeypatch):
    _served(monkeypatch, version="2.0.0", min_plugin="1.0.0")
    client = _client(tmp_path, monkeypatch, _bundle(tmp_path, plugin="1.5.0", low="1.3.0", high="2.0.0"))
    client._send = lambda method, path, **kwargs: {}

    with pytest.raises(discovery.VersionSkew, match=r"runs SwarmCloud 2\.0\.0; plugin 1\.5\.0"):
        client.request("GET", "/v1/tasks")


def test_skew_error_on_every_tool_call_never_silent(tmp_path, monkeypatch):
    _served(monkeypatch, version="1.6.0", min_plugin="1.5.0")
    client = _client(tmp_path, monkeypatch, _bundle(tmp_path, plugin="1.4.2"))
    sent: list[str] = []
    client._send = lambda method, path, **kwargs: sent.append(path) or {}

    for method, path in (("GET", "/v1/tasks"), ("POST", "/v1/tasks"), ("GET", "/v1/runs"), ("GET", "/v1/tasks")):
        with pytest.raises(discovery.VersionSkew, match="sc plugin 1.4.2 is older"):
            client.request(method, path)
    # Nothing reached the API: a skewed call never half-runs.
    assert sent == []


def test_a_404_reads_discovery_again_and_answers_the_skew(tmp_path, monkeypatch):
    asked, document = _served(monkeypatch, version="1.4.0", min_plugin="1.0.0")
    client = _client(tmp_path, monkeypatch, _bundle(tmp_path, plugin="1.4.0", low="1.0.0", high="1.5.0"))

    def send(method, path, **kwargs):  # noqa: ARG001
        raise SwarmError("no such route", status=404)

    client._send = send
    # The deployment upgrades past the plugin while the cached document says 1.4.0.
    document["version"] = "1.5.0"

    with pytest.raises(discovery.VersionSkew, match="runs SwarmCloud 1.5.0; plugin 1.4.0"):
        client.request("GET", "/v1/new-route")
    assert asked == [f"{URL}/.well-known/swarmcloud"] * 2


def test_matching_versions_pass_and_doctor_prints_both(tmp_path, monkeypatch):
    _served(monkeypatch, version="1.4.0", min_plugin="1.0.0")
    client = _client(tmp_path, monkeypatch, _bundle(tmp_path, plugin="1.4.0", low="1.3.0", high="1.5.0"))
    client._send = lambda method, path, **kwargs: {"ok": True}

    assert client.request("GET", "/v1/tasks") == {"ok": True}
    assert client.skew.status() == (
        "plugin 1.4.0 (supports deployments 1.3.0 to before 1.5.0); deployment 1.4.0 (accepts plugins ≥ 1.0.0)"
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [("1.2.0-rc1", "1.2.0"), ("1.2.0", "1.2.1"), ("0.9", "0.10"), ("garbled", "0.0.1")],
)
def test_versions_compare_as_releases(a, b):
    assert discovery.older(a, b)
    assert not discovery.older(b, a)
    assert not discovery.older("1.2", "1.2.0")
