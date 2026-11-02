"""Agents use the tools switched on for them (owner's choice, 2 November 2026; part of #2688).

The first experiment batch (2 November 2026, eight workflows, 56 steps) found
that `swarm-graph` was offered in all 42 steps with code intelligence on and
never called: its tools sat behind the CLI's tool search and nothing said
when to use them. The jev browser was offered on every rendered step of the
Jev groups and got no calls; Playwright did every one. So, in the Claude Code
runner:

* a switched-on graph is loaded at the session's start (the `--mcp-config`
  entry's `alwaysLoad`) and the prompt says what it answers better than a
  text search; a switched-off step has neither;
* a ready jev browser is the first choice on a rendered step, loaded up
  front, with Playwright still registered as the fallback;
* the agent's `jev_decide` tool is registered, loaded up front, only on a
  step whose workspace opted in to Jev judgments (the worker's
  `SWARM_JUDGE_CONFIG`), over the worker's own config and so its ledger;
* the runner's result reports the session's calls per tool family.

Offline: the CLI is never started; `run_cli_agent` is replaced.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import browser_tools, graphstage
from agent_worker.judge import attempt as judge_attempt
from agent_worker.judge import mcp as judge_mcp
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.cliagent import cli_stream_files

ENDPOINT = "http://127.0.0.1:9222"
PROMPT = "Implement it."

_CLEARED = ("SWARM_GIT", "SWARM_CHILDREN", "SWARM_SEARCH", "SWARM_STATE",
            claude_code.BROWSERS_PATH_ENV, claude_code.CDP_ENDPOINT_ENV, claude_code.HANDOFF_ENV,
            claude_code.JEV_BACKEND_ENV, claude_code.JEV_KEY_FILE_ENV, claude_code.JEV_URL_ENV,
            claude_code.MCP_RELAY_ENV, judge_attempt.CONFIG_ENV,
            graphstage.SNAPSHOT_ENV, graphstage.CONTEXT_ENV, graphstage.UNSTAGED_ENV,
            graphstage.PIN_ENV, graphstage.TICKET_FILE_ENV, graphstage.URL_ENV)


def _call(name: str, tool_input: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One tool call and its result, as the CLI's stream-json prints them."""
    ident = "toolu_" + secrets.token_hex(8)
    return [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": ident, "name": name, "input": tool_input or {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident, "content": [{"type": "text", "text": "ok"}]}]}},
    ]


def _stream(*calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "system", "subtype": "init", "session_id": "s"}]
    for call in calls:
        events.extend(call)
    events.append({"type": "result", "subtype": "success", "result": "done"})
    return events


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str], *,
         stream: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The runner's body with `env`; what it handed the CLI, and its result."""
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["prompt"] = ctx.payload.get("prompt")
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if stream is not None:
            path = ctx.artifacts_dir / cli_stream_files(spec).stdout
            path.write_text("".join(json.dumps(e) + "\n" for e in stream))
        return {"summary": "", "stdout_truncated": False,
                "metrics": {"duration_seconds": 1.0}}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    ctx = RunnerContext(work_dir=work, artifacts_dir=work / "artifacts",
                        input_path=work / "input.json", result_path=work / "result.json",
                        quota_path=work / "quota.json", payload={"prompt": PROMPT}, repo_dir=repo)
    seen["result"] = claude_code.body(ctx)
    return seen


def _servers(seen: dict[str, Any]) -> dict[str, Any]:
    args = seen["args"]
    if "--mcp-config" not in args:
        return {}
    return json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]


def _graph_env(tmp_path: Path) -> dict[str, str]:
    context = tmp_path / "work" / graphstage.CONTEXT_DIR_NAME
    return {graphstage.SNAPSHOT_ENV: str(context / "snapshot"), graphstage.CONTEXT_ENV: str(context)}


def _served_env(tmp_path: Path) -> dict[str, str]:
    context = tmp_path / "work" / graphstage.CONTEXT_DIR_NAME
    return {graphstage.CONTEXT_ENV: str(context),
            graphstage.PIN_ENV: str(context / graphstage.PIN_NAME),
            graphstage.TICKET_FILE_ENV: str(tmp_path / "private" / "graph-ticket"),
            graphstage.URL_ENV: "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"}


def _jev_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """A rendered step whose jev browser can run: installed, internal backend, routed."""
    binary = tmp_path / "jev-browser-mcp"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(claude_code, "JEV_MCP_BIN", str(binary))
    return {claude_code.BROWSERS_PATH_ENV: "/opt/playwright", claude_code.CDP_ENDPOINT_ENV: ENDPOINT,
            claude_code.JEV_BACKEND_ENV: sorted(claude_code.JEV_INTERNAL_BACKENDS)[0],
            claude_code.JEV_URL_ENV: "http://jev.internal.example:8080"}


def _judge_config(tmp_path: Path) -> Path:
    """The worker's `register.prepare` config, as far as the runner reads it: a file."""
    path = tmp_path / "judge" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "ledger": str(tmp_path / "judge" / "ledger.json")}))
    return path


# --------------------------------------------------------------------------
# (1) the code graph
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env_of", [_graph_env, _served_env], ids=["staged", "served"])
def test_a_switched_on_graph_loads_up_front_and_the_prompt_says_to_use_it_first(
        monkeypatch, tmp_path, env_of):
    seen = _run(monkeypatch, tmp_path, env_of(tmp_path))
    entry = _servers(seen)[graphstage.SERVER_NAME]
    # MUTATION: drop `alwaysLoad` and the graph's tools wait behind tool search.
    assert entry["alwaysLoad"] is True
    assert claude_code.GRAPH_FIRST_NOTE in seen["prompt"]
    for phrase in ("callers", "defin", "change touches", "first", "text search"):
        assert phrase in claude_code.GRAPH_FIRST_NOTE
    # One short line.
    assert "\n" not in claude_code.GRAPH_FIRST_NOTE and len(claude_code.GRAPH_FIRST_NOTE) < 400


def test_a_switched_off_step_has_neither_the_graph_nor_its_line(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {})
    assert graphstage.SERVER_NAME not in _servers(seen)
    assert claude_code.GRAPH_FIRST_NOTE not in seen["prompt"]


# --------------------------------------------------------------------------
# (2) the jev browser first on a rendered step
# --------------------------------------------------------------------------

def test_a_ready_jev_browser_is_first_and_playwright_stays_registered(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, _jev_ready(monkeypatch, tmp_path))
    servers = _servers(seen)
    # MUTATION: leave jev behind tool search and the agent never finds it.
    assert servers[claude_code.JEV_MCP_SERVER_NAME]["alwaysLoad"] is True
    # The fallback is still there.
    assert claude_code.MCP_SERVER_NAME in servers
    assert claude_code.BROWSER_TOOL_LEAD_JEV_FIRST in seen["prompt"]
    lead = claude_code.BROWSER_TOOL_LEAD_JEV_FIRST
    assert "jev" in lead and "first" in lead and "fallback" in lead and "browser_*" in lead


def test_the_auto_note_puts_jev_first_only_when_it_is_ready():
    ready = browser_tools.choose("auto", jev_installed=True, jev_key=True)
    note = claude_code.live_browser_note(ready, search=False)
    assert note.startswith(claude_code.BROWSER_TOOL_LEAD_JEV_FIRST)
    # Its strength is the first one named.
    assert note.index("The jev tools") < note.index("The browser_* tools")
    missing = browser_tools.choose("auto", jev_installed=False, jev_key=True)
    plain = claude_code.live_browser_note(missing, search=False)
    assert plain.startswith(claude_code.BROWSER_TOOL_LEAD["auto"])
    assert claude_code.BROWSER_TOOL_LEAD_JEV_FIRST not in plain
    # A task that chose Playwright is never told to prefer jev.
    chosen = browser_tools.choose("playwright", jev_installed=True, jev_key=True)
    assert claude_code.BROWSER_TOOL_LEAD_JEV_FIRST not in claude_code.live_browser_note(chosen, search=False)


def test_a_jev_browser_that_is_not_ready_is_not_registered(monkeypatch, tmp_path):
    env = _jev_ready(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_code, "JEV_MCP_BIN", str(tmp_path / "absent"))
    seen = _run(monkeypatch, tmp_path, env)
    servers = _servers(seen)
    assert claude_code.JEV_MCP_SERVER_NAME not in servers
    assert claude_code.MCP_SERVER_NAME in servers
    assert claude_code.BROWSER_TOOL_LEAD_JEV_FIRST not in seen["prompt"]


# --------------------------------------------------------------------------
# (3) the agent's own Jev decision tool
# --------------------------------------------------------------------------

def test_the_decision_tool_is_registered_up_front_when_the_workspace_opted_in(monkeypatch, tmp_path):
    config = _judge_config(tmp_path)
    seen = _run(monkeypatch, tmp_path, {judge_attempt.CONFIG_ENV: str(config)})
    entry = _servers(seen)[judge_mcp.SERVER_NAME]
    # The worker's own config, so the worker's ledger and budget; this
    # interpreter, isolated, as every platform server.
    assert entry == {"type": "stdio", "command": sys.executable,
                     "args": ["-I", "-m", "agent_worker.judge", "--config", str(config)],
                     "alwaysLoad": True}
    assert claude_code.JEV_DECIDE_NOTE in seen["prompt"]
    assert judge_mcp.TOOL_NAME in claude_code.JEV_DECIDE_NOTE


def test_the_decision_tool_is_absent_without_the_opt_in(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {})
    assert judge_mcp.SERVER_NAME not in _servers(seen)
    assert claude_code.JEV_DECIDE_NOTE not in seen["prompt"]
    # A config the worker did not write (a stale variable) registers nothing.
    gone = _run(monkeypatch, tmp_path, {judge_attempt.CONFIG_ENV: str(tmp_path / "nowhere.json")})
    assert judge_mcp.SERVER_NAME not in _servers(gone)
    assert claude_code.JEV_DECIDE_NOTE not in gone["prompt"]


def test_the_config_names_no_key(monkeypatch, tmp_path):
    """The key (TypeSafe) is read by the tool's process from the file the
    worker's config names; neither the MCP config nor the prompt carries it."""
    key = secrets.token_hex(16)
    key_file = tmp_path / "private" / "jev-key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    config = _judge_config(tmp_path)
    config.write_text(json.dumps({"version": 1, "key_file": str(key_file),
                                  "ledger": str(tmp_path / "judge" / "ledger.json")}))
    seen = _run(monkeypatch, tmp_path, {judge_attempt.CONFIG_ENV: str(config)})
    path = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
    assert key not in path.read_text() and key not in seen["prompt"]
    assert "env" not in _servers(seen)[judge_mcp.SERVER_NAME]


# --------------------------------------------------------------------------
# (4) calls per tool family
# --------------------------------------------------------------------------

def test_calls_are_counted_by_family():
    stream = _stream(
        _call("mcp__swarm-graph__search", {"query": "x"}),
        _call("mcp__swarm-graph__call_graph"),
        _call("mcp__jev__judge_page"),
        _call("mcp__playwright__browser_navigate", {"url": "http://127.0.0.1:5173/"}),
        _call("mcp__playwright__browser_snapshot"),
        _call("mcp__playwright__browser_click"),
        _call("mcp__swarm-jev__jev_decide", {"state": {}, "questions": {}}),
        _call("Bash", {"command": "grep -rn x ."}),
        _call("Read", {"file_path": "a.py"}),
        _call("mcp__swarm-git__git_fetch"),
    )
    # MUTATION: count only the browser (the old rule) and the graph is never seen.
    assert claude_code.tool_calls_by_family(stream) == {
        "code_graph": 2, "jev_browser": 1, "playwright": 3, "jev_decision": 1,
        "built_in": 2, "other_mcp": 1}
    assert claude_code.tool_calls_by_family([]) == dict.fromkeys(claude_code.TOOL_FAMILIES, 0)


def test_the_result_reports_the_counts_on_every_step(monkeypatch, tmp_path):
    stream = _stream(_call("mcp__swarm-graph__impact"), _call("Grep", {"pattern": "x"}))
    seen = _run(monkeypatch, tmp_path, _graph_env(tmp_path), stream=stream)
    counts = seen["result"]["metrics"]["tool_calls"]
    assert counts == {"read": True, "complete": True,
                      "families": {"code_graph": 1, "jev_browser": 0, "playwright": 0,
                                   "jev_decision": 0, "built_in": 1, "other_mcp": 0}}
    # The CLI's own metrics are kept.
    assert seen["result"]["metrics"]["duration_seconds"] == 1.0


def test_an_unread_stream_is_not_a_session_with_no_calls(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {}, stream=None)
    assert seen["result"]["metrics"]["tool_calls"] == {"read": False, "complete": True,
                                                      "families": None}
