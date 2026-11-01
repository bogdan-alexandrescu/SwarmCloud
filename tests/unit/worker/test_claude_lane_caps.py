"""The claude-code runner caps what every lane turn carries (TOKEN-A lane-caps,
part of #2524 items 6 and 8).

Measured 2026-10-20..27 (wf_9a256dde8d174201947c): 97% of the pool's tokens
were cache reads, the same long context re-read on every turn. So:

* the repository's own `swarmcloud` MCP server (`.mcp.json`) is not loaded in
  a lane -- no lane in the sample called it, and its ~100 tool schemas sat in
  every turn's prefix;
* a Bash result is capped at 12,000 characters (`BASH_MAX_OUTPUT_LENGTH`);
* a second `scripts/changed-guards.sh` on a tree no edit has touched since
  the last one is warned, never blocked;
* the agent is told to search the graph before `grep -rn` across the tree
  and to send broad exploration to a cheap child.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext


def _settings(tmp_path: Path) -> dict:
    return json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())


def test_the_repository_swarmcloud_server_is_not_loaded_in_a_lane(tmp_path):
    settings = _settings(tmp_path)
    # MUTATION: drop `disabledMcpjsonServers` -> the ~100 swarmcloud tools are
    # back in every turn's prefix and this fails.
    assert "swarmcloud" in settings["disabledMcpjsonServers"]
    # And it is the name the repository's .mcp.json declares.
    repo = Path(__file__).resolve().parents[3]
    assert "swarmcloud" in json.loads((repo / ".mcp.json").read_text())["mcpServers"]


def test_a_bash_result_is_capped_near_twelve_thousand_characters(tmp_path):
    env = _settings(tmp_path)["env"]
    # MUTATION: remove the cap -> a 30k-line log lands in every later turn.
    assert env["BASH_MAX_OUTPUT_LENGTH"] == "12000"


def _guards_entry(settings: dict) -> dict:
    (entry,) = [
        e for e in settings["hooks"]["PreToolUse"]
        if any(claude_code.GUARDS_REPEAT_HOOK.name in h["command"] for h in e["hooks"])
    ]
    return entry


def test_the_settings_register_the_guards_repeat_warning(tmp_path):
    entry = _guards_entry(_settings(tmp_path))
    # MUTATION: leave the entry out of PreToolUse -> this fails.
    assert entry["matcher"] == "Bash"
    (hook,) = entry["hooks"]
    assert str(claude_code.GUARDS_REPEAT_HOOK) in hook["command"]
    # Its state lives beside the settings, outside the checkout.
    assert str(tmp_path / "settings-dir" / claude_code.GUARDS_STATE_NAME) in hook["command"]


def test_a_new_settings_file_forgets_an_earlier_attempts_guard_run(tmp_path):
    directory = tmp_path / "settings-dir"
    directory.mkdir()
    (directory / claude_code.GUARDS_STATE_NAME).write_text('{"fingerprint": "x"}')
    claude_code.write_headless_settings(directory)
    assert not (directory / claude_code.GUARDS_STATE_NAME).exists()


# --- the hook itself, run as the CLI runs it ---------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "a"], cwd=repo, env=env, check=True)
    return repo


def _hook(state: Path, cwd: Path, command: str, tool: str = "Bash") -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, "-I", str(claude_code.GUARDS_REPEAT_HOOK), str(state)],
        input=payload, capture_output=True, text=True, timeout=30,
        env={**os.environ, **_GIT_ENV},
    )


def _warning(result: subprocess.CompletedProcess) -> str:
    # Never a block: exit 0 every time, the warning only as context.
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return ""
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out
    return out["additionalContext"]


@pytest.mark.parametrize("command", [
    "scripts/changed-guards.sh",
    "bash scripts/changed-guards.sh 2>&1 | tail -40",
    "cd /x && timeout 600 ./scripts/changed-guards.sh --shard 1/2",
])
def test_a_second_guard_run_with_no_edit_between_is_warned(tmp_path, command):
    repo = _repo(tmp_path)
    state = tmp_path / "state.json"
    assert _warning(_hook(state, repo, command)) == ""
    # MUTATION: never compare with the last run -> no warning and this fails.
    warned = _warning(_hook(state, repo, command))
    assert "changed-guards.sh" in warned
    assert "no file" in warned
    # MUTATION: tell the agent to run it again after an edit -> this fails
    # (#2981: the worker's pre-publish gate runs it after the turn).
    assert "pre-publish gate runs it after your turn" in warned
    assert "run it again only after" not in warned


@pytest.mark.parametrize("edit", ["tracked", "untracked", "commit"])
def test_an_edit_since_the_last_guard_run_is_not_warned(tmp_path, edit):
    repo = _repo(tmp_path)
    state = tmp_path / "state.json"
    assert _warning(_hook(state, repo, "scripts/changed-guards.sh")) == ""
    if edit == "tracked":
        (repo / "a.py").write_text("x = 2\n")
    elif edit == "untracked":
        (repo / "b.py").write_text("y = 1\n")
    else:
        (repo / "a.py").write_text("x = 3\n")
        env = {**os.environ, **_GIT_ENV}
        subprocess.run(["git", "commit", "-q", "-am", "b"], cwd=repo, env=env, check=True)
    # MUTATION: warn on every second run whatever changed -> this fails.
    assert _warning(_hook(state, repo, "scripts/changed-guards.sh")) == ""


@pytest.mark.parametrize("command", [
    "cat scripts/changed-guards.sh",
    "grep -n reporter scripts/changed-guards.sh",
    "scripts/related-tests.sh --run",
])
def test_a_command_that_only_names_the_script_is_not_a_guard_run(tmp_path, command):
    repo = _repo(tmp_path)
    state = tmp_path / "state.json"
    assert _warning(_hook(state, repo, command)) == ""
    assert _warning(_hook(state, repo, command)) == ""
    assert not state.exists()


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"tool_input": 3}'])
def test_input_it_cannot_read_lets_the_call_through(tmp_path, payload):
    result = subprocess.run(
        [sys.executable, "-I", str(claude_code.GUARDS_REPEAT_HOOK), str(tmp_path / "s.json")],
        input=payload, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_outside_a_git_tree_it_says_nothing(tmp_path):
    state = tmp_path / "state.json"
    assert _warning(_hook(state, tmp_path, "scripts/changed-guards.sh")) == ""
    assert _warning(_hook(state, tmp_path, "scripts/changed-guards.sh")) == ""


# --- the exploration note (#2524 item 8's steering line) ----------------------

def test_the_note_names_the_graph_tools_and_a_cheap_child():
    note = claude_code.exploration_note(graph=True, children="/w/.swarm-children")
    # MUTATION: drop either clause -> this fails.
    for phrase in ("swarm-graph", "`search`", "`call_graph`", "grep -rn",
                   ".swarm-children", "model_class", "sonnet", "haiku"):
        assert phrase in note
    assert "\n" not in note


def test_the_note_names_only_what_the_attempt_has():
    assert claude_code.exploration_note(graph=False, children="") is None
    graph_only = claude_code.exploration_note(graph=True, children="")
    assert "call_graph" in graph_only and "model_class" not in graph_only
    children_only = claude_code.exploration_note(graph=False, children="/w/.swarm-children")
    assert "model_class" in children_only and "call_graph" not in children_only


def test_the_note_reaches_the_agents_prompt(tmp_path, monkeypatch):
    work = tmp_path / "work"
    checkout = work / "repo"
    checkout.mkdir(parents=True)
    (tmp_path / "artifacts").mkdir()
    ctx = RunnerContext(
        work_dir=work, artifacts_dir=tmp_path / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "Fix the thing."}, repo_dir=checkout,
    )
    seen: dict[str, str] = {}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = run_ctx.payload["prompt"]
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.CDP_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(claude_code.CHILDREN_ENV, str(work / ".swarm-children"))
    claude_code.body(ctx)
    # MUTATION: stop appending the note in `body` -> this fails.
    assert seen["prompt"].startswith("Fix the thing.")
    assert "model_class" in seen["prompt"]
