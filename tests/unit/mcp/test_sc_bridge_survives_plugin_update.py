"""The bridge survives a plugin update that removes its root's folder
(SC-BRIDGE-SURVIVES-UPDATE; part of #2688).

Observed 4 November 2026: the sc plugin updated from 0.5.74 to 0.5.76 under a
long-running Claude Code session and the 0.5.74 folder
(`~/.claude/plugins/cache/swarmcloud/sc/0.5.74`) was removed, but the
session's bridge had been started with SWARM_SC_PLUGIN_ROOT on it, so every
`swarm_workflow_launch` failed with "run.js is not a file". Held here:

* a removed root with a newer version beside it launches from that version,
  and the reply says so in one line naming both and the reload;
* versions compare numerically: 0.5.10 is newer than 0.5.9;
* with no usable sibling the error stays, and says a plugin update may have
  removed the folder and that /reload-plugins is the fix;
* a root that exists is used unchanged, and only a sibling of the configured
  root is ever taken.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import launch, server

from test_plugin_agents_and_workflows import _RUN_JS
from test_plugin_rows_match_console import _FIXTURE, _Recorder


def _version(cache: Path, version: str) -> Path:
    root = cache / "swarmcloud" / "sc" / version
    (root / "workflows").mkdir(parents=True)
    shutil.copy(_RUN_JS, root / "workflows" / "run.js")
    return root


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.delenv("SWARM_MCP_CONFIG_FROM", raising=False)
    monkeypatch.delenv(launch.PROJECT_DIR_ENV, raising=False)
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    folder = tmp_path / "plugins" / "cache"
    folder.mkdir(parents=True)
    return folder


def _removed_root(monkeypatch, cache: Path, version: str) -> Path:
    root = cache / "swarmcloud" / "sc" / version
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    return root


def test_a_removed_root_launches_from_its_newer_sibling_and_the_reply_says_so(cache, monkeypatch):
    newer = _version(cache, "0.5.76")
    _removed_root(monkeypatch, cache, "0.5.74")
    assert launch.plugin_root() == newer
    spec = {**_FIXTURE["chain"], "title": "nightly"}
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec}))
    assert "error" not in reply, reply
    assert Path(reply["script_path"]).read_text().splitlines()[1] == '  name: "SC · nightly · 3 steps",'
    note = reply["plugin_root_note"]
    assert "\n" not in note
    assert "0.5.74" in note and "0.5.76" in note
    assert "/reload-plugins" in note


def test_versions_compare_numerically_not_as_strings(cache, monkeypatch):
    _version(cache, "0.5.9")
    ten = _version(cache, "0.5.10")
    _removed_root(monkeypatch, cache, "0.5.8")
    assert launch.plugin_root() == ten
    assert "0.5.10" in launch.plugin_root_note()


def test_with_no_usable_sibling_the_error_names_the_reload(cache, monkeypatch):
    # A sibling without run.js, and a folder that is no version, are not used.
    (cache / "swarmcloud" / "sc" / "0.5.80").mkdir(parents=True)
    _version(cache, "latest")
    root = _removed_root(monkeypatch, cache, "0.5.74")
    assert launch.plugin_root() == root
    with pytest.raises(launch.SwarmError) as caught:
        launch.template()
    message = str(caught.value)
    assert "does not point at the sc plugin" in message
    assert "plugin update may have removed" in message
    assert "/reload-plugins" in message


def test_a_symlinked_sibling_is_never_followed(cache, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "workflows").mkdir(parents=True)
    shutil.copy(_RUN_JS, elsewhere / "workflows" / "run.js")
    (cache / "swarmcloud" / "sc").mkdir(parents=True)
    (cache / "swarmcloud" / "sc" / "0.5.99").symlink_to(elsewhere)
    root = _removed_root(monkeypatch, cache, "0.5.74")
    assert launch.plugin_root() == root
    with pytest.raises(launch.SwarmError):
        launch.template()


def test_a_root_outside_the_plugin_cache_shape_searches_nothing(monkeypatch, tmp_path):
    # A newer version folder beside it, but not under `swarmcloud/sc/`.
    folder = tmp_path / "other" / "plugins" / "sc-copy"
    (folder / "0.5.76" / "workflows").mkdir(parents=True)
    shutil.copy(_RUN_JS, folder / "0.5.76" / "workflows" / "run.js")
    root = folder / "0.5.74"
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    assert launch.plugin_root() == root
    with pytest.raises(launch.SwarmError):
        launch.template()


def test_a_root_that_exists_is_used_unchanged(cache, monkeypatch):
    current = _version(cache, "0.5.74")
    _version(cache, "0.5.76")
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(current))
    assert launch.plugin_root() == current
    assert launch.plugin_root_note() is None
    script = launch.write_script("SC · t · 1 steps", "d")
    assert script.is_file()
