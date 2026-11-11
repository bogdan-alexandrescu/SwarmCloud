"""The sc plugin protects its own version folder (PLUGIN-VERSION-GUARD, owner
9 November 2026).

Claude Code keeps each plugin version in
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, with
`.in_use/<pid>` markers naming the Claude Code processes using it, and deletes
a version folder no live marker names. A long-lived session's pid was in no
marker, its sc folder was deleted while it ran, and every launch then failed
with `run.js is not a file` for hours before anyone noticed. Held here:

* the SessionStart hook writes `.in_use/<pid of the Claude Code process>`
  into its own cache version folder -- only when missing, as an empty file,
  never touching another marker, never in a folder outside the cache, never
  re-creating a folder that is gone;
* the bridge reports a configured plugin root, or its workflows/run.js, that
  is gone as a top-level `plugin_files_missing: {root, action}` on every tool
  reply, errors included, and says nothing while they are there;
* `swarm_check` flags it STUCK, rule `plugin_files_missing`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_mcp import check, launch, server

from test_plugin_agents_and_workflows import _RUN_JS
from test_sc_watch import APP, NOW, Api

_REPO = Path(__file__).resolve().parents[3]
_HOOK = _REPO / "plugin" / "hooks" / "session-start.sh"


def _version_folder(base: Path, version: str = "9.9.9") -> Path:
    root = base / ".claude" / "plugins" / "cache" / "swarmcloud" / "sc" / version
    (root / "workflows").mkdir(parents=True)
    shutil.copy(_RUN_JS, root / "workflows" / "run.js")
    return root


# --------------------------------------------------------------------------
# The hook's marker
# --------------------------------------------------------------------------


def _hook(tmp_path: Path, root: Path) -> subprocess.CompletedProcess:
    """The hook as Claude Code runs it: through `sh -c`, whose parent -- this
    test's process -- stands in for Claude Code. No deployment is configured,
    so the hook asks no bridge."""
    sh = shutil.which("sh")
    if sh is None or shutil.which("bash") is None or shutil.which("ps") is None:  # pragma: no cover
        pytest.skip("sh, bash or ps is not installed")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(root),
    }
    return subprocess.run(
        [sh, "-c", f'"{_HOOK}"'],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
        capture_output=True, text=True, timeout=60, env=env, check=False,
    )


def test_the_hook_marks_its_version_folder_in_use_by_the_claude_code_process(tmp_path):
    root = _version_folder(tmp_path)
    done = _hook(tmp_path, root)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    marker = root / ".in_use" / str(os.getpid())
    assert marker.is_file(), sorted(p.name for p in (root / ".in_use").iterdir())
    assert marker.read_bytes() == b""
    assert [p.name for p in (root / ".in_use").iterdir()] == [str(os.getpid())]


def test_the_marker_is_written_only_when_missing_and_no_other_is_touched(tmp_path):
    root = _version_folder(tmp_path)
    in_use = root / ".in_use"
    in_use.mkdir()
    others = {"4194301": b"", "4194302": b"another process's marker"}
    for name, body in others.items():
        (in_use / name).write_bytes(body)
    first = _hook(tmp_path, root)
    assert first.returncode == 0, first.stderr
    mine = in_use / str(os.getpid())
    assert mine.is_file() and mine.read_bytes() == b""
    mine.write_bytes(b"left as it is")
    stamp = mine.stat().st_mtime_ns

    second = _hook(tmp_path, root)

    assert second.returncode == 0, second.stderr
    assert mine.read_bytes() == b"left as it is" and mine.stat().st_mtime_ns == stamp
    assert {p.name: p.read_bytes() for p in in_use.iterdir() if p != mine} == others


def test_a_folder_outside_the_plugin_cache_gets_no_marker(tmp_path):
    root = tmp_path / "checkout" / "plugin"
    (root / "workflows").mkdir(parents=True)
    done = _hook(tmp_path, root)
    assert done.returncode == 0, done.stderr
    assert not (root / ".in_use").exists()


def test_a_version_folder_already_gone_is_not_re_created(tmp_path):
    root = tmp_path / ".claude" / "plugins" / "cache" / "swarmcloud" / "sc" / "9.9.8"
    done = _hook(tmp_path, root)
    assert (done.returncode, done.stderr) == (0, "")
    assert not root.exists()


# --------------------------------------------------------------------------
# The bridge's check
# --------------------------------------------------------------------------


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.delenv("SWARM_MCP_CONFIG_FROM", raising=False)
    monkeypatch.delenv(launch.PLUGIN_ROOT_ENV, raising=False)
    return tmp_path


def test_a_present_root_reports_nothing(bridge, monkeypatch):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_version_folder(bridge)))
    assert launch.plugin_files_missing() is None
    assert server.report_plugin_files() is None
    assert server._with_plugin_files_missing('{"a": 1}') == '{"a": 1}'


def test_a_bridge_with_no_configured_root_reports_nothing(bridge):
    assert launch.plugin_files_missing() is None


def test_a_missing_root_is_reported_with_its_reload(bridge, monkeypatch, capsys):
    root = _version_folder(bridge)
    shutil.rmtree(root)
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    expected = {"root": str(root), "action": "type /reload-plugins in this session"}
    assert launch.plugin_files_missing() == expected
    assert server.report_plugin_files() == expected
    assert str(root) in capsys.readouterr().err


def test_a_root_without_run_js_is_reported(bridge, monkeypatch):
    root = _version_folder(bridge)
    (root / "workflows" / "run.js").unlink()
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    assert launch.plugin_files_missing() == {"root": str(root), "action": launch.PLUGIN_FILES_ACTION}


def test_a_titled_copy_checks_first_even_when_a_sibling_titles_it(bridge, monkeypatch, capsys):
    _version_folder(bridge, "9.9.10")
    gone = bridge / ".claude" / "plugins" / "cache" / "swarmcloud" / "sc" / "9.9.9"
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(gone))
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(bridge / "project"))
    (bridge / "project").mkdir()
    launch.write_script("SC ∴ nightly", "a run")
    err = capsys.readouterr().err
    assert "sc plugin's files are missing" in err and str(gone) in err


def _speak(*calls: dict) -> list[dict]:
    wire = io.StringIO()
    with contextlib.redirect_stdout(wire):
        server.serve(stdin=iter([json.dumps(c) + "\n" for c in calls]))
    return [json.loads(line) for line in wire.getvalue().splitlines() if line.strip()]


def _tool(call_id: int, name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


@pytest.fixture
def served(bridge, monkeypatch):
    """`serve` with a stand-in client, a config and project of its own."""
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(bridge / "config"))
    project = bridge / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(server, "SwarmClient", lambda tenant=None: object())
    return bridge


def test_every_tool_reply_carries_the_missing_files_at_its_top_level(served, monkeypatch):
    root = served / ".claude" / "plugins" / "cache" / "swarmcloud" / "sc" / "9.9.9"
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    replies = {r["id"]: r["result"] for r in _speak(
        _tool(1, "swarm_check_schedule", {}),
        _tool(2, "swarm_check_schedule", {"no_such_argument": True}),
    )}
    expected = {"root": str(root), "action": "type /reload-plugins in this session"}

    answered = json.loads(replies[1]["content"][0]["text"])
    assert answered["plugin_files_missing"] == expected
    assert "watch" in answered, answered

    refused = replies[2]
    assert refused["isError"] is True
    text = refused["content"][0]["text"]
    last = json.loads(text.rstrip().splitlines()[-1])
    assert last == {"plugin_files_missing": expected}, text


def test_a_present_root_leaves_every_reply_as_it_was(served, monkeypatch):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_version_folder(served)))
    replies = {r["id"]: r["result"] for r in _speak(
        _tool(1, "swarm_check_schedule", {}),
        _tool(2, "swarm_check_schedule", {"no_such_argument": True}),
    )}
    assert "plugin_files_missing" not in json.loads(replies[1]["content"][0]["text"])
    assert "plugin_files_missing" not in replies[2]["content"][0]["text"]


def test_a_plain_text_reply_gains_one_json_line(bridge, monkeypatch):
    root = bridge / "gone"
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    expected = {"root": str(root), "action": launch.PLUGIN_FILES_ACTION}
    assert server._with_plugin_files_missing("not json") == (
        "not json\n\n" + json.dumps({"plugin_files_missing": expected}))
    assert json.loads(server._with_plugin_files_missing('{"a": 1}')) == {"plugin_files_missing": expected, "a": 1}


# --------------------------------------------------------------------------
# swarm_check's STUCK flag
# --------------------------------------------------------------------------


def test_swarm_check_flags_a_missing_root_stuck(bridge, monkeypatch):
    root = bridge / ".claude" / "plugins" / "cache" / "swarmcloud" / "sc" / "9.9.9"
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(root))
    result = check.run(Api(), repository=APP, now=NOW, names=lambda _c: {})
    flag = result["flagged"][0]
    assert (flag["kind"], flag["rule"]) == ("STUCK", "plugin_files_missing")
    assert flag["evidence"]["root"] == str(root)
    assert flag["next"] == "type /reload-plugins in this session"
    assert result["count"]["stuck"] >= 1


def test_swarm_check_does_not_flag_a_present_root(bridge, monkeypatch):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_version_folder(bridge)))
    result = check.run(Api(), repository=APP, now=NOW, names=lambda _c: {})
    assert all(item["rule"] != "plugin_files_missing" for item in result["flagged"])
