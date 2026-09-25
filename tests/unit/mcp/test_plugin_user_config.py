"""The plugin's `userConfig`, held to what the bridge actually reads.

Claude Code prompts for `userConfig` when the plugin is installed or enabled,
saves non-sensitive values under `pluginConfigs` in the user's settings.json
and SENSITIVE ones in the platform's secure credential store, and substitutes
`${user_config.KEY}` into an MCP server's `command`, `args` and `env`
(code.claude.com/docs/en/plugins-reference, "User configuration" and
"Environment variables"). The option object is STRICT: an unknown key fails
validation and the plugin does not load at all.

So three things can drift, and each drift is silent:

  * a key the manifest prompts for that no env entry carries -- the user is
    asked, answers, and the bridge never sees it;
  * an env entry whose name the bridge does not read -- the same, one hop on;
  * a key the bridge reads that the manifest never prompts for -- the bridge
    waits for a value nobody can give it.

Nothing is restated here. The manifest is parsed, the bridge's own list is
imported, and the two are compared.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO / "plugin" / ".claude-plugin" / "plugin.json"

#: The option fields Claude Code accepts (plugins-reference, "User
#: configuration"). Anything else makes the whole plugin fail to load.
_OPTION_FIELDS = {
    "type", "title", "description", "required", "default", "options",
    "multiple", "sensitive", "min", "max",
}


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text())


def _config():
    return importlib.import_module("swarm_mcp.config")


def _user_config() -> dict:
    options = _manifest().get("userConfig")
    assert isinstance(options, dict) and options, (
        "plugin.json declares no userConfig, so installing `sc` asks nothing and "
        "the bridge falls back to whatever deployment this repository names"
    )
    return options


def _server_env() -> dict:
    servers = _manifest().get("mcpServers") or {}
    env = (servers.get("swarmcloud") or {}).get("env")
    assert isinstance(env, dict) and env, "the swarmcloud server passes the bridge no env"
    return env


def test_every_option_is_a_well_formed_strict_object():
    for key, option in _user_config().items():
        assert key.isidentifier() and not key[0].isdigit(), key
        unknown = set(option) - _OPTION_FIELDS
        assert not unknown, f"{key}: {sorted(unknown)} would stop the plugin loading"
        for field in ("type", "title", "description"):
            assert option.get(field), f"{key} has no {field}, which Claude Code requires"


def test_the_options_the_manifest_prompts_for_are_the_ones_the_bridge_reads():
    assert set(_user_config()) == set(_config().PLUGIN_KEYS)


def test_every_option_reaches_the_bridge_under_the_name_it_reads():
    """Each key K arrives as `${user_config.K}` in exactly the env variable
    `config.plugin_env(K)` -- derived from the key, so a rename on either side
    is a failure here rather than a value that silently never arrives."""
    env = _server_env()
    config = _config()
    for key in _user_config():
        name = config.plugin_env(key)
        assert env.get(name) == f"${{user_config.{key}}}", (
            f"{key!r} must reach the bridge as {name}={'${user_config.' + key + '}'}; "
            f"env carries {env.get(name)!r}"
        )
    passed = {v for v in env.values() if isinstance(v, str) and "${user_config." in v}
    assert len(passed) == len(_user_config()), f"env passes an option nobody declared: {passed}"


def test_the_deployment_url_is_required_and_the_secret_is_sensitive():
    options = _user_config()
    assert options["deployment_url"].get("required") is True
    assert options["oauth_client_secret"].get("sensitive") is True, (
        "a client secret saved without `sensitive` lands in plain settings.json"
    )
    # Not required: a solo deployment has no IAP and no OAuth client at all.
    assert not options["oauth_client_id"].get("required")
    assert not options["oauth_client_secret"].get("required")


def test_nothing_in_the_manifest_names_this_repositorys_deployment():
    """The defect in one line: the plugin must not ship pointing at Saga."""
    text = _MANIFEST.read_text()
    for owned in ("saga.xyz", "saga-agents-staging", "209012342332"):
        assert owned not in text, f"plugin.json names {owned}"


def test_the_marketplace_no_longer_says_it_needs_this_repositorys_mcp_json():
    marketplace = json.loads((_REPO / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in marketplace["plugins"] if p["name"] == "sc")
    assert ".mcp.json" not in entry["description"], (
        "the plugin ships its own MCP server and asks for its deployment at "
        "install; telling people it needs this repository's .mcp.json is stale"
    )


@pytest.mark.parametrize("key", ["deployment_url", "oauth_client_id", "oauth_client_secret"])
def test_the_prompts_say_what_to_paste_and_where_it_comes_from(key):
    """A prompt someone cannot answer is a prompt they fill with a guess."""
    description = _user_config()[key]["description"].lower()
    assert len(description) > 40, key
    if key != "deployment_url":
        assert "desktop" in description, f"{key} must say it is the Desktop app client"
