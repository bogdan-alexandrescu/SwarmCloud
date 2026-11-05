"""The sc plugin releases the catalogue that holds claude-code-lightpanda
(lane PLUGIN-LIGHTPANDA-BRIDGE, part of #2688; docs/design/lightpanda-runtime.md).

* MEASURED 2026-10-31 18:35 UTC on the installed plugin at 0.5.75:
  `swarm_dispatch` with profile claude-code-lightpanda, and `swarm_workflow`
  with a step on it, were refused by the bridge before anything was sent
  ("there is no runner profile called 'claude-code-lightpanda'"). The bridge
  reads the frozen catalogue, `swarm_common.profiles.RUNNER_PROFILES`, from
  the `sc-v<version>` tag it installs, and sc-v0.5.75 was cut before contract
  request 127 merged (#2918).
* Main's CI tags the bridge `sc-v<version>` once the unit tests pass, and only
  for a version it has not tagged yet (docs/ci.md, "The plugin's bridge tag is
  made once the unit tests pass"), so the profile reaches a session only when
  plugin.json's version moves past 0.5.75, with the bridge pin beside it and
  a CHANGELOG entry naming it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from swarm_common.profiles import RUNNER_PROFILES

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_CHANGELOG = _PLUGIN / "CHANGELOG.md"

#: The bridge tag that predates claude-code-lightpanda in the catalogue.
_TAG_BEFORE_THE_PROFILE = (0, 5, 75)


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text())


def _release_entry() -> str:
    """The 0.5.76 entry, the version that released the profile: a later bump
    adds its own entry above it (0.5.77 for codex, contract request 130) and
    must not need the profile named again."""
    sections = re.split(r"^## ", _CHANGELOG.read_text(), flags=re.MULTILINE)
    entry = next((s for s in sections if s.startswith("0.5.76 ")), None)
    assert entry is not None, "no `## 0.5.76` entry in plugin/CHANGELOG.md"
    return entry


def test_the_catalogue_the_bridge_ships_holds_the_profile():
    assert "claude-code-lightpanda" in RUNNER_PROFILES


def test_the_version_moved_past_the_tag_that_predates_the_profile():
    version = _manifest()["version"]
    assert tuple(int(part) for part in version.split(".")) > _TAG_BEFORE_THE_PROFILE, version


def test_the_bridge_pin_names_the_plugins_own_version():
    manifest = _manifest()
    pinned = json.dumps(manifest["mcpServers"])
    assert re.findall(r"@sc-v([0-9.]+)#", pinned) == [manifest["version"]]


def test_the_releasing_changelog_entry_names_the_profile():
    entry = _release_entry()
    text = " ".join(entry.split())
    assert "claude-code-lightpanda" in text
    for tool in ("swarm_dispatch", "swarm_workflow", "swarm_profiles"):
        assert f"`{tool}`" in text, tool
