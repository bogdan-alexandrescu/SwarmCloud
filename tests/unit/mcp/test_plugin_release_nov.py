"""The sc plugin releases tonight's merged fixes, and its install faces relay
the waits' new events (lane PLUGIN-RELEASE-NOV, part of #2688).

* THE VERSION MOVED. Two plugin-side changes merged on 2026-10-31 while
  plugin.json still said 0.5.74: `swarm_check` lists a parked task's blocking
  owner questions first (#2860), and a workflow spec of any size launches
  through `swarm_workflow_launch` with only the spec handle in its args
  (#2875). The release tags the bridge `sc-v<version>` only for a version it
  has not tagged yet (docs/ci.md, "The plugin's bridge tag is made by the
  release"), so neither reached a session until the version moved past 0.5.74,
  with the bridge pin beside it and a CHANGELOG entry naming both.
* THE WAITS' EVENTS ARE RELAYED. swarm_install.waits sends a `wait_countdown`
  `detail` whenever the check's answer changes, and ends a wait at once with
  `wait_failed` and its `detail`. `/sc:install` and `/sc:team` are told to
  show both to the person.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from swarm_install import events, waits

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_CHANGELOG = _PLUGIN / "CHANGELOG.md"

#: The version on main when #2860 and #2875 merged without a bump.
_PREVIOUS_RELEASED_VERSION = (0, 5, 74)


def _version() -> str:
    return json.loads(_MANIFEST.read_text())["version"]


def test_the_version_moved_past_the_unreleased_fixes():
    version = _version()
    assert tuple(int(part) for part in version.split(".")) > _PREVIOUS_RELEASED_VERSION, version


def test_the_bridge_pin_moved_with_the_version():
    manifest = json.loads(_MANIFEST.read_text())
    pinned = json.dumps(manifest["mcpServers"])
    assert re.findall(r"@sc-v([0-9.]+)#", pinned) == [manifest["version"]]


def test_the_changelog_names_both_merged_changes_under_the_version():
    assert _CHANGELOG.is_file(), "plugin/CHANGELOG.md is missing"
    text = _CHANGELOG.read_text()
    sections = re.split(r"^## ", text, flags=re.MULTILINE)
    # The version that released them, 0.5.75: a later bump adds its own entry
    # above this one and must not need these two fixes named again.
    entry = next((s for s in sections if s.startswith("0.5.75 ")), None)
    assert entry is not None, "no `## 0.5.75` entry in plugin/CHANGELOG.md"
    assert "#2860" in entry and "swarm_check" in entry
    assert "#2875" in entry and "swarm_workflow_launch" in entry


def test_the_engine_sends_the_events_the_faces_relay():
    assert {"wait_countdown", "wait_failed"} <= events.KINDS
    assert waits.FAILED == "wait_failed"


def _paragraph(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n\n", start)
    return " ".join(text[start: end if end != -1 else None].split())


@pytest.mark.parametrize("face", ["install", "team"])
def test_a_face_relays_the_countdown_detail_and_wait_failed(face):
    events_paragraph = _paragraph((_PLUGIN / "commands" / f"{face}.md").read_text(), "**Reading the events.**")
    assert re.search(r"`wait_countdown`[^.]*`detail`[^.]*whenever it changes", events_paragraph), events_paragraph
    assert re.search(r"`wait_failed`[^.]*`detail`", events_paragraph), events_paragraph
