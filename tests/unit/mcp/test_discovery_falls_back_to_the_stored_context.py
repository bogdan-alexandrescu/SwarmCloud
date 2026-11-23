"""The bridge works when the deployment's discovery document cannot be read,
if this machine already knows the deployment's client id (defect reproduced
2026-11-23, improvement plan item 1).

With only the deployment address configured, every tool call failed with
`cannot read <address>/.well-known/swarmcloud (401)` on a deployment that does
not serve the document, although the config file held that deployment's
context with its client id (written by a terminal sign-in). The order is the
plugin's client id option, then the document, then the stored context's id;
an unreadable document is fatal only when all three are empty, and then the
error says what to do in one sentence.
"""

from __future__ import annotations

import json
import secrets

import pytest

from swarm_mcp import auth, config, discovery
from swarm_mcp.client import SwarmClient

HOST = "swarm.example.test"
URL = f"https://{HOST}"
DOC_URL = f"{URL}/.well-known/swarmcloud"


def _client_id() -> str:
    return f"{secrets.randbelow(10**12)}-{secrets.token_hex(8)}.apps.googleusercontent.com"


def _http_401(url, timeout):  # noqa: ARG001
    return 401, b""


def _http_404(url, timeout):  # noqa: ARG001
    return 404, b""


def _unreachable(url, timeout):  # noqa: ARG001
    raise discovery.DiscoveryUnreachable(f"cannot read {url} (connection refused)")


FAILURES = pytest.mark.parametrize(
    "getter", [_http_401, _http_404, _unreachable], ids=["401", "404", "unreachable"]
)


def _env(tmp_path) -> dict[str, str]:
    return {"SWARM_CONFIG_DIR": str(tmp_path), config.plugin_env(config.PLUGIN_URL): URL}


def _store_context(environ, client_id: str, name: str = "swarm-example") -> None:
    """What a terminal sign-in left: a context for the address with its id."""
    stored = config.load(environ)
    stored.contexts[name] = config.Context(name=name, url=URL, oauth_client_id=client_id)
    stored.current = name
    config.save(stored)


def _client(deployment) -> SwarmClient:
    client = SwarmClient(deployment=deployment, connect=False, tier=auth.Tier.EXPLICIT)
    client.base_url = URL
    client._sends_cached_credential = lambda: False
    client._send = lambda method, path, **kwargs: {"ok": True}
    return client


@FAILURES
def test_an_unreadable_document_with_a_stored_context_still_resolves_and_calls_work(
    tmp_path, monkeypatch, getter
):
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=getter))
    environ = _env(tmp_path)
    mine = _client_id()
    _store_context(environ, mine)

    deployment = config.resolve(environ)

    assert deployment.client_id == mine
    assert deployment.front_door is True
    client = _client(deployment)
    assert client.request("GET", "/v1/tasks") == {"ok": True}
    assert client.request("GET", "/v1/tasks") == {"ok": True}
    # The skew check is not checked, with the reason, and never blocks a call.
    assert client.skew.required is False
    assert client.skew.unchecked.startswith(f"cannot read {DOC_URL}")


@FAILURES
def test_seeding_and_resolving_work_with_a_stored_context(tmp_path, monkeypatch, getter):
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=getter))
    environ = _env(tmp_path)
    mine = _client_id()
    _store_context(environ, mine)

    seeded = config.seed_from_plugin(environ, store=object())

    assert seeded.client_id == mine
    assert config.load(environ).contexts["swarm-example"].oauth_client_id == mine


@FAILURES
def test_an_unreadable_document_with_nothing_stored_says_what_to_do_in_one_sentence(
    tmp_path, monkeypatch, getter
):
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=getter))

    with pytest.raises(discovery.DiscoveryUnreachable) as caught:
        config.resolve(_env(tmp_path))

    text = str(caught.value)
    assert text.startswith(f"cannot read {DOC_URL} (")
    assert "set the sc plugin's client id option" in text
    assert "sign in from a terminal" in text
    # One sentence: one full stop, at the end.
    assert text.rstrip(".").count(". ") == 0 and text.endswith(".")
    assert caught.value.exit_code == 3


def test_the_configured_option_wins_over_the_document_and_the_stored_context(tmp_path, monkeypatch):
    served = _client_id()
    document = {"schema": 1, "version": "1.4.0", "oauth_client_id": served}
    monkeypatch.setattr(
        discovery, "CACHE", discovery.Cache(get=lambda url, timeout: (200, json.dumps(document).encode()))
    )
    environ = _env(tmp_path)
    _store_context(environ, _client_id())

    mine = _client_id()
    environ[config.plugin_env(config.PLUGIN_CLIENT_ID)] = mine
    assert config.resolve(environ).client_id == mine


def test_the_document_wins_over_the_stored_context_and_a_later_unreadable_document_does_not_block(
    tmp_path, monkeypatch
):
    served = _client_id()
    document = {"schema": 1, "version": "1.4.0", "oauth_client_id": served}
    answers = [(200, json.dumps(document).encode())]
    monkeypatch.setattr(
        discovery, "CACHE",
        discovery.Cache(get=lambda url, timeout: answers[0] if answers else (401, b"")),
    )
    environ = _env(tmp_path)
    _store_context(environ, _client_id())

    deployment = config.resolve(environ)
    assert deployment.client_id == served

    answers.clear()
    client = _client(deployment)
    discovery.CACHE.invalidate(URL)
    # The stored context holds an id, so the document is not required.
    assert client.request("GET", "/v1/tasks") == {"ok": True}
    assert client.skew.unchecked == f"cannot read {DOC_URL} (401)"




OTHER_URL = "https://other.example.test"


def _store_other(environ, client_id: str) -> None:
    """Another deployment's context, as a sign-in to it would leave it."""
    stored = config.load(environ)
    stored.contexts["other"] = config.Context(name="other", url=OTHER_URL, oauth_client_id=client_id)
    stored.current = "other"
    config.save(stored)


@FAILURES
def test_another_deployments_stored_client_id_is_never_lent(tmp_path, monkeypatch, getter):
    # A client id is an audience: lending another deployment's would send the
    # sign-in to the wrong one. Only a context at the same address counts.
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=getter))
    environ = _env(tmp_path)
    theirs = _client_id()
    _store_other(environ, theirs)

    with pytest.raises(discovery.DiscoveryUnreachable) as caught:
        config.resolve(environ)
    assert str(caught.value).endswith(discovery.NO_CLIENT_ID)

    with pytest.raises(discovery.DiscoveryUnreachable) as seeded:
        config.seed_from_plugin(environ, store=object())
    assert str(seeded.value).endswith(discovery.NO_CLIENT_ID)
    stored = config.load(environ)
    assert stored.contexts["other"].oauth_client_id == theirs
    for context in stored.contexts.values():
        if context.url == URL:
            assert context.oauth_client_id != theirs


@FAILURES
def test_with_contexts_at_both_addresses_the_same_address_id_is_chosen(
    tmp_path, monkeypatch, getter
):
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=getter))
    environ = _env(tmp_path)
    _store_other(environ, _client_id())
    mine = _client_id()
    _store_context(environ, mine)
    _store_other(environ, _client_id())

    assert config.resolve(environ).client_id == mine
    seeded = config.seed_from_plugin(environ, store=object())
    assert seeded.client_id == mine
    assert config.load(environ).contexts["swarm-example"].oauth_client_id == mine
