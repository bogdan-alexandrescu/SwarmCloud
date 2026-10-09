"""`swarm_workflow_launch` on the repo-local bridge (#888 box 101).

This checkout's `.mcp.json` starts the bridge with `SWARM_MCP_CONFIG_FROM=repo`
and nothing else: no `SWARM_SC_PLUGIN_ROOT`, which only the sc plugin's
manifest sets. Observed 2026-10-08: every launch there failed with
"SWARM_SC_PLUGIN_ROOT is not set", and once the variable was supplied by hand
the copy landed in the system temp directory (`/var/folders/...` on macOS),
which the Workflow tool refused until the file was copied into the repository.

So in repo mode the plugin root defaults to this checkout's `plugin/`, and the
per-run copies go under the checkout, in a folder that ignores itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import launch, server

from test_plugin_agents_and_workflows import _PLUGIN, _RUN_JS
from test_plugin_rows_match_console import _FIXTURE, _Recorder


@pytest.fixture
def _checkout(monkeypatch, tmp_path):
    """A stand-in checkout holding the plugin's real run.js, so the copies a
    test writes never land in the working tree running the suite."""
    root = tmp_path / "checkout"
    (root / "plugin" / "workflows").mkdir(parents=True)
    shutil.copy(_RUN_JS, root / "plugin" / "workflows" / "run.js")
    monkeypatch.setenv("SWARM_REPO_ROOT", str(root))
    monkeypatch.setenv("SWARM_MCP_CONFIG_FROM", "repo")
    monkeypatch.delenv(launch.PLUGIN_ROOT_ENV, raising=False)
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    return root


def test_the_repo_local_bridge_finds_run_js_in_this_checkouts_plugin(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_CONFIG_FROM", "repo")
    monkeypatch.delenv(launch.PLUGIN_ROOT_ENV, raising=False)
    monkeypatch.delenv("SWARM_REPO_ROOT", raising=False)
    assert launch.plugin_root().resolve() == _PLUGIN.resolve()
    assert launch.template() == _RUN_JS.read_text(encoding="utf-8")


def test_the_repo_local_mcp_json_is_the_bridge_this_covers():
    """The fallback is keyed on repo mode; if .mcp.json stopped setting it, the
    repo-local bridge would be back to the error."""
    config = json.loads((_PLUGIN.parent / ".mcp.json").read_text())
    env = config["mcpServers"]["swarmcloud"].get("env") or {}
    assert env.get("SWARM_MCP_CONFIG_FROM") == "repo"


def test_a_repo_local_launch_writes_its_copy_under_the_checkout(_checkout, tmp_path):
    spec = {**_FIXTURE["chain"], "title": "nightly"}
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec}))
    assert "error" not in reply, reply
    script = Path(reply["script_path"])
    assert script.is_file()
    assert script.parent == _checkout / launch.REPO_RUN_DIR
    assert not (tmp_path / "system-tmp" / launch.RUN_DIR).exists(), "nothing goes to the system temp dir"
    # The copy is the checkout's run.js with the title in it.
    assert script.read_text().splitlines()[1] == '  name: "SC · nightly · 3 steps",'


def test_the_repo_local_run_folder_ignores_itself(_checkout):
    script = launch.write_script("SC · t · 1 steps", "d")
    ignore = script.parent / ".gitignore"
    assert ignore.read_text().splitlines()[0] == "*"
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(_checkout)], check=True)
        status = subprocess.run(
            ["git", "-C", str(_checkout), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert launch.REPO_RUN_DIR.as_posix() not in status, status


def test_an_explicit_plugin_root_still_wins_in_repo_mode(_checkout, monkeypatch, tmp_path):
    other = tmp_path / "other-plugin"
    (other / "workflows").mkdir(parents=True)
    (other / "workflows" / "run.js").write_text(_RUN_JS.read_text())
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(other))
    assert launch.plugin_root() == other


def test_a_repo_mode_bridge_with_no_plugin_folder_says_so(_checkout):
    shutil.rmtree(_checkout / "plugin")
    with pytest.raises(launch.SwarmError) as caught:
        launch.template()
    assert launch.PLUGIN_ROOT_ENV in str(caught.value)
    assert str(_checkout / "plugin") in str(caught.value)


def test_outside_repo_mode_the_copies_stay_in_the_system_temp_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("SWARM_MCP_CONFIG_FROM", raising=False)
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    script = launch.write_script("SC · t · 1 steps", "d")
    assert script.parent == tmp_path / launch.RUN_DIR
