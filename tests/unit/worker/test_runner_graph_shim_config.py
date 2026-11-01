"""Every runtime registers the shim as `swarm-graph` when the graph is served
(docs/design/graph-service.md §4.2; lane GS-WORKER, part of #1384).

claude-code and claude-code-browser (the same runner module) write it into the
one `--mcp-config`; codex names it in `-c mcp_servers.swarm-graph.*` overrides.
The argv is fixed and names no host, bucket or commit; the service URL is the
worker's setting, passed in the server entry's `env`, never an argument
(invariant 10). A staged snapshot still registers the local server, and the
two never appear together.

Offline: the CLI is never started; `run_cli_agent` is replaced.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from agent_worker import graphstage
from agent_worker.graphmcp import shim
from swarm_common.profiles import RUNNER_PROFILES

URL = "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"


def _ctx(work: Path, repo: Path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=work, artifacts_dir=work / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "Implement it."}, repo_dir=repo)


_CLEARED = ("SWARM_GIT", "SWARM_CHILDREN", "SWARM_SEARCH", "PLAYWRIGHT_BROWSERS_PATH",
            "SWARM_CDP_ENDPOINT", graphstage.SNAPSHOT_ENV, graphstage.CONTEXT_ENV,
            graphstage.UNSTAGED_ENV, graphstage.PIN_ENV, graphstage.TICKET_FILE_ENV, graphstage.URL_ENV)


def _run(module: Any, monkeypatch: pytest.MonkeyPatch, work: Path,
         env: dict[str, str]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        seen["prompt"] = ctx.payload.get("prompt")
        return {}

    monkeypatch.setattr(module, "run_cli_agent", fake_run)
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    repo = work / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    module.body(_ctx(work, repo))
    return seen


def _served(work: Path, tmp_path: Path) -> dict[str, str]:
    context = work / graphstage.CONTEXT_DIR_NAME
    return {graphstage.CONTEXT_ENV: str(context),
            graphstage.PIN_ENV: str(context / graphstage.PIN_NAME),
            graphstage.TICKET_FILE_ENV: str(tmp_path / "private" / "graph-ticket"),
            graphstage.URL_ENV: URL}


def _shim_argv(env: dict[str, str], work: Path) -> list[str]:
    return [sys.executable, "-I", "-m", "agent_worker.graphmcp.shim",
            "--pin", env[graphstage.PIN_ENV],
            "--ticket-file", env[graphstage.TICKET_FILE_ENV],
            "--workdir", str(work / "repo")]


def _mcp_servers(seen: dict[str, Any]) -> dict[str, Any] | None:
    args = seen["args"]
    if "--mcp-config" not in args:
        return None
    return json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]


# --------------------------------------------------------------------------
# the argv
# --------------------------------------------------------------------------


def test_shim_argv_is_this_interpreter_isolated_and_names_no_host():
    argv = graphstage.shim_argv("/w/.swarm-context/pin.json", "/p/graph-ticket", "/w/repo")
    assert argv == [sys.executable, "-I", "-m", graphstage.SHIM_MODULE,
                    "--pin", "/w/.swarm-context/pin.json", "--ticket-file", "/p/graph-ticket",
                    "--workdir", "/w/repo"]
    assert graphstage.SHIM_MODULE == shim.__name__
    assert not any("http" in part for part in argv)
    assert graphstage.shim_argv("/p", "/t", None)[-2:] == ["--ticket-file", "/t"]


def test_the_names_are_the_shims_own():
    from agent_worker import config

    assert graphstage.URL_ENV == shim.URL_ENV
    assert graphstage.SERVER_NAME == "swarm-graph"
    assert config.DEFAULT_GRAPH_SERVICE_URL == graphstage.DEFAULT_SERVICE_URL == shim.DEFAULT_URL


def test_the_service_url_is_the_workers_setting(monkeypatch):
    """Read from the worker's own environment, never from a task (invariant 10)."""
    from agent_worker.config import DEFAULT_GRAPH_SERVICE_URL, WorkerConfig

    for name, value in {"TASK_ID": "t", "ATTEMPT_ID": "a", "LEASE_ID": "l", "TENANT_ID": "acme",
                        "RUNNER_PROFILE": "claude-code", "PROJECT_ID": "proj-test",
                        "GENERATION": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(graphstage.URL_ENV, raising=False)
    assert WorkerConfig.from_env().graph_service_url == DEFAULT_GRAPH_SERVICE_URL
    monkeypatch.setenv(graphstage.URL_ENV, "http://graph.internal:8080/mcp")
    assert WorkerConfig.from_env().graph_service_url == "http://graph.internal:8080/mcp"


def test_the_argv_starts_the_shim_and_its_tool_list_is_local(tmp_path):
    """Not only a name in a file: the argv answers `tools/list` without the service."""
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"index_sha": "a" * 40}))
    argv = graphstage.shim_argv(str(pin), str(tmp_path / "graph-ticket"), None)
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    done = subprocess.run(argv, input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", graphstage.URL_ENV: "http://127.0.0.1:9/mcp"})
    answers = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert answers[0]["result"]["serverInfo"]["name"] == graphstage.SERVER_NAME
    assert answers[1]["result"]["tools"], done.stderr


# --------------------------------------------------------------------------
# claude-code and claude-code-browser
# --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["claude-code", "claude-code-browser"])
def test_both_claude_profiles_run_the_runner_that_writes_the_config(profile):
    assert "agent_worker.runners.claude_code" in RUNNER_PROFILES[profile].runner_argv


def test_claude_code_registers_the_shim_with_the_url_in_its_env(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    work = tmp_path / "work"
    env = _served(work, tmp_path)
    seen = _run(claude_code, monkeypatch, work, env)
    servers = _mcp_servers(seen)
    command, *args = _shim_argv(env, work)
    assert servers == {graphstage.SERVER_NAME: {
        "type": "stdio", "command": command, "args": args,
        "env": {graphstage.URL_ENV: URL}}}
    # The context directory reaches the agent for CONTEXT.md; the ticket's
    # path is the shim's argument, not the agent's variable.
    assert seen["env"][graphstage.CONTEXT_ENV] == env[graphstage.CONTEXT_ENV]
    assert graphstage.TICKET_FILE_ENV not in seen["env"]
    # A served graph gets the graph clause of the exploration line (LANE CAPS,
    # part of #2524), and nothing else.
    assert seen["prompt"] == ("Implement it.\n\n"
                              + claude_code.exploration_note(graph=True, children=""))


def test_claude_code_browser_registers_the_shim_beside_the_live_browser(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    work = tmp_path / "work"
    env = {**_served(work, tmp_path), "PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright",
           "SWARM_CDP_ENDPOINT": "http://127.0.0.1:9222"}
    seen = _run(claude_code, monkeypatch, work, env)
    assert seen["args"].count("--mcp-config") == 1
    servers = _mcp_servers(seen)
    assert claude_code.MCP_SERVER_NAME in servers
    command, *args = _shim_argv(env, work)
    assert servers[graphstage.SERVER_NAME] == {
        "type": "stdio", "command": command, "args": args, "env": {graphstage.URL_ENV: URL}}


def test_claude_code_without_a_url_uses_the_in_cluster_default(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    work = tmp_path / "work"
    env = _served(work, tmp_path)
    del env[graphstage.URL_ENV]
    servers = _mcp_servers(_run(claude_code, monkeypatch, work, env))
    assert servers[graphstage.SERVER_NAME]["env"] == {graphstage.URL_ENV: shim.DEFAULT_URL}


def test_claude_code_with_a_snapshot_still_registers_the_local_server(monkeypatch, tmp_path):
    """The control: `staged` (the default) is exactly today's entry."""
    from agent_worker.runners import claude_code

    work = tmp_path / "work"
    snapshot = str(work / ".swarm-context" / "snapshot")
    servers = _mcp_servers(_run(claude_code, monkeypatch, work, {
        graphstage.SNAPSHOT_ENV: snapshot, graphstage.CONTEXT_ENV: str(work / ".swarm-context")}))
    assert servers == {graphstage.SERVER_NAME: {
        "type": "stdio", "command": sys.executable,
        "args": ["-I", "-m", "agent_worker.graphmcp", "--snapshot", snapshot,
                 "--workdir", str(work / "repo")]}}


def test_claude_code_needs_both_the_pin_and_the_ticket(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    work = tmp_path / "work"
    env = _served(work, tmp_path)
    del env[graphstage.TICKET_FILE_ENV]
    assert _mcp_servers(_run(claude_code, monkeypatch, work, env)) is None


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------


def _overrides(seen: dict[str, Any]) -> list[str]:
    return [seen["args"][i + 1] for i, a in enumerate(seen["args"]) if a == "-c"]


def test_codex_registers_the_shim_and_its_url_as_toml_overrides(monkeypatch, tmp_path):
    from agent_worker.runners import codex

    work = tmp_path / "work"
    env = _served(work, tmp_path)
    seen = _run(codex, monkeypatch, work, env)
    key = f"mcp_servers.{graphstage.SERVER_NAME}"
    command, *args = _shim_argv(env, work)
    overrides = _overrides(seen)
    assert [o.split("=", 1)[0] for o in overrides] == [f"{key}.command", f"{key}.args",
                                                      f"{key}.env"]
    # codex parses each value as TOML: every one must parse, to what we meant.
    parsed = {o.split("=", 1)[0]: tomllib.loads("v = " + o.split("=", 1)[1])["v"]
              for o in overrides}
    assert parsed == {f"{key}.command": command, f"{key}.args": args,
                      f"{key}.env": {graphstage.URL_ENV: URL}}
    assert seen["env"][graphstage.CONTEXT_ENV] == env[graphstage.CONTEXT_ENV]
    assert seen["prompt"] == "Implement it."


def test_codex_with_a_snapshot_still_registers_the_local_server(monkeypatch, tmp_path):
    from agent_worker.runners import codex

    work = tmp_path / "work"
    snapshot = str(work / ".swarm-context" / "snapshot")
    seen = _run(codex, monkeypatch, work, {graphstage.SNAPSHOT_ENV: snapshot})
    key = f"mcp_servers.{graphstage.SERVER_NAME}"
    assert _overrides(seen) == [
        f"{key}.command={json.dumps(sys.executable)}",
        f"{key}.args={json.dumps(['-I', '-m', 'agent_worker.graphmcp', '--snapshot', snapshot, '--workdir', str(work / 'repo')])}"]


def test_codex_without_a_graph_names_no_server(monkeypatch, tmp_path):
    from agent_worker.runners import codex

    seen = _run(codex, monkeypatch, tmp_path / "work", {})
    assert seen["args"] == []
