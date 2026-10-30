"""scripts/release/marketplace.py: the plugin marketplace and the plugin bundle
of a release (DAYONE-RELEASE-1; plan §6.4, §6.5; D28).

A teammate adds the marketplace at `https://storage.googleapis.com/swarmcloud/
marketplace.json` and installs `sc@swarmcloud`; the plugin's bridge is the
release's own wheel, and the plugin carries the range of deployment versions it
supports, which the bridge reads at start (swarm_mcp.discovery).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "release" / "marketplace.py"
VERSION = "2026.10.1"
HOME = "https://storage.googleapis.com/swarmcloud"


def _load():
    assert SCRIPT.is_file(), f"{SCRIPT.relative_to(REPO)} does not exist"
    spec = importlib.util.spec_from_file_location("swarm_release_marketplace", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """A copy of plugin/, prepared for the release as assemble.sh prepares it."""
    target = tmp_path / "plugin"
    shutil.copytree(REPO / "plugin", target, ignore=shutil.ignore_patterns("node_modules", "*.test.ts"))
    _load().prepare_plugin(target, VERSION, REPO / "install" / "compat.json")
    return target


def test_marketplace_names_plugin_sc_at_version() -> None:
    """One plugin, `sc`, at the release's version, from the release's plugin
    directory; the marketplace is named `swarmcloud`, so the install line is
    `sc@swarmcloud` (D28)."""
    module = _load()
    root = module.marketplace(VERSION, f"./releases/{VERSION}/plugin")
    assert root["name"] == "swarmcloud"
    assert [p["name"] for p in root["plugins"]] == ["sc"]
    (sc,) = root["plugins"]
    assert sc["version"] == VERSION
    assert sc["source"] == f"./releases/{VERSION}/plugin"
    assert root["owner"]["name"]
    pinned = module.marketplace(VERSION, "./plugin")
    assert pinned["plugins"][0]["source"] == "./plugin"
    # A version that is not a release name is refused.
    with pytest.raises(ValueError):
        module.marketplace("main", "./plugin")


def test_plugin_bundle_carries_supported_deployment_range(plugin: Path) -> None:
    """The plugin directory carries `compat.json` = {plugin: V, deployments:
    {min, max_exclusive}}, read by the bridge exactly as swarm_mcp.discovery
    reads it, and the range admits the release's own deployment."""
    from swarm_mcp import discovery

    compat = json.loads((plugin / "compat.json").read_text())
    assert compat["plugin"] == VERSION
    assert set(compat["deployments"]) == {"min", "max_exclusive"}
    env = {discovery.PLUGIN_ROOT_ENV: str(plugin)}
    span = discovery.supported(env)
    assert span is not None, "the bridge cannot read the bundle's compat.json"
    assert discovery.plugin_version(env) == VERSION
    # The release's own deployment is always in range.
    assert not (span.min and discovery.older(VERSION, span.min))
    assert not (span.max_exclusive and not discovery.older(VERSION, span.max_exclusive))


def test_range_that_excludes_its_own_release_is_refused(tmp_path: Path) -> None:
    module = _load()
    source = tmp_path / "compat.json"
    source.write_text(json.dumps({"deployments": {"min": "2027.01.1", "max_exclusive": ""}}))
    with pytest.raises(ValueError):
        module.compat(VERSION, source)
    source.write_text(json.dumps({"deployments": {"min": "", "max_exclusive": VERSION}}))
    with pytest.raises(ValueError):
        module.compat(VERSION, source)


def test_plugin_bridge_is_the_release_wheel(plugin: Path) -> None:
    """The plugin's MCP server runs the release's bridge wheel from the release
    home, with the frozen contract's wheel from the same release beside it
    (`swarm-common` is not on PyPI, so a bare name would be an index lookup).
    Never a git ref of this repository: a company's teammate has no access to
    it. `SWARM_MCP_FROM` still overrides it for a developer's working copy."""
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == VERSION
    args = manifest["mcpServers"]["swarmcloud"]["args"]
    text = json.dumps(args)
    assert "git+" not in text
    bridge = f"swarm-mcp @ {HOME}/releases/{VERSION}/swarm_mcp-{VERSION}-py3-none-any.whl"
    common = f"swarm-common @ {HOME}/releases/{VERSION}/swarm_common-{VERSION}-py3-none-any.whl"
    assert f"${{SWARM_MCP_FROM:-{bridge}}}" in args
    assert args[args.index("--with") + 1] == common
    assert args[-1] == "swarm-mcp"
    # Everything else in the manifest is the checkout's, unchanged.
    original = json.loads((REPO / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    for key in set(original) - {"version", "mcpServers"}:
        assert manifest[key] == original[key], key
    assert manifest["mcpServers"]["swarmcloud"]["env"] == original["mcpServers"]["swarmcloud"]["env"]


def test_wheel_version_is_the_normalised_release_name() -> None:
    """A wheel's file name carries the PEP 440 form of the version: the
    plugin and install.sh must name the file the build actually wrote."""
    module = _load()
    assert module.wheel_version("2026.10.1") == "2026.10.1"
    assert module.wheel_version("2026.01.2") == "2026.1.2"
    assert module.wheel_version("2026.11.3-rc.2") == "2026.11.3rc2"
