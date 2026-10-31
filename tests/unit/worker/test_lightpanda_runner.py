"""The `claude-code-lightpanda` runner branch (docs/design/lightpanda-runtime.md
§6.2 and §6.6; contract request 127; part of #2688).

A `claude-code-lightpanda` agent never receives Chromium and never claims a
live view: no SWARM_CDP_ENDPOINT, no `playwright` MCP server, no `--cdp-endpoint`
or 9222, no `browser.json`. Its Lightpanda child sees only the allow-listed
environment, both Lightpanda commands block private networks, three tools are
denied, and the `serve` child is supervised: restarted on exit, at most 20
times, one log line per exit and never the environment.

MUTATION: drop the branch's `env.pop(CDP_ENDPOINT_ENV)`, or `lightpanda` from
`write_mcp_config`, or the restart limit in `Supervisor._run`, and a test
below fails.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import browser_tools, lightpanda
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext

TOKEN_ENVS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN", "SWARM_SECRET")


class FakeSupervisor:
    made: list["FakeSupervisor"] = []

    def __init__(self, argv, env, *, log, **_kw):
        self.argv = list(argv)
        self.env = dict(env)
        self.log = log
        self.started = False
        self.stopped = False
        FakeSupervisor.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _ctx(tmp_path: Path, payload: dict[str, Any] | None = None) -> RunnerContext:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    return RunnerContext(
        work_dir=work, artifacts_dir=tmp_path / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload=payload or {"prompt": "Read three pages."},
    )


def _run(tmp_path: Path, monkeypatch, profile: str, *, fail: bool = False) -> dict[str, Any]:
    """`claude_code.body` on `profile`, with the CLI faked; what it was handed."""
    FakeSupervisor.made = []
    token = secrets.token_hex(16)
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.GIT_SPOOL_ENV,
                 claude_code.SEARCH_SPOOL_ENV, claude_code.STATE_SPOOL_ENV,
                 claude_code.MCP_RELAY_ENV, claude_code.CHILDREN_ENV):
        monkeypatch.delenv(name, raising=False)
    # A live-browser endpoint that must never reach a lightpanda agent, and
    # credentials that must never reach the Lightpanda child.
    monkeypatch.setenv(claude_code.CDP_ENDPOINT_ENV, "http://127.0.0.1:9222")
    for name in TOKEN_ENVS:
        monkeypatch.setenv(name, token)
    monkeypatch.setenv(claude_code.CACHE_HOME_ENV, str(tmp_path / "cache"))
    monkeypatch.setenv(lightpanda.PROFILE_ENV, profile)
    monkeypatch.setattr(claude_code.lightpanda, "Supervisor", FakeSupervisor)
    states: list[Any] = []
    monkeypatch.setattr(browser_tools, "write_state", lambda *a, **k: states.append(a))
    seen: dict[str, Any] = {"token": token, "states": states}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = run_ctx.payload["prompt"]
        seen["argv"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        seen["running"] = [(s.started, s.stopped) for s in FakeSupervisor.made]
        if fail:
            raise claude_code.RunnerFailure("the CLI failed")
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    ctx = _ctx(tmp_path)
    if fail:
        with pytest.raises(claude_code.RunnerFailure):
            claude_code.body(ctx)
    else:
        claude_code.body(ctx)
    argv = seen["argv"]
    seen["mcp"] = (json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]
                   if "--mcp-config" in argv else {})
    seen["supervisors"] = list(FakeSupervisor.made)
    return seen


# --------------------------------------------------------------------------- #
# Never Chromium, never a live view                                           #
# --------------------------------------------------------------------------- #


def test_a_lightpanda_agent_never_receives_chromium_and_never_claims_a_live_view(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, lightpanda.PROFILE)
    assert claude_code.CDP_ENDPOINT_ENV not in seen["env"]
    assert seen["env"][lightpanda.CDP_ENV] == "http://127.0.0.1:9223"
    assert lightpanda.MCP_SERVER_NAME in seen["mcp"]
    assert claude_code.MCP_SERVER_NAME not in seen["mcp"]
    everything = json.dumps([seen["argv"], seen["mcp"]])
    assert "--cdp-endpoint" not in everything
    assert "9222" not in everything
    assert seen["states"] == []
    assert "no live view" in seen["prompt"]
    assert seen["prompt"].startswith("Read three pages.")
    assert lightpanda.LIGHTPANDA_NOTE in seen["prompt"]


def test_a_submitted_browser_tool_is_refused_at_validation():
    from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs

    profile = RUNNER_PROFILES[lightpanda.PROFILE]
    assert "browser_tool" not in profile.inputs
    with pytest.raises(InputRefused):
        check_inputs(profile, {"browser_tool": "auto"})


def test_the_image_and_port_are_the_catalogues_and_not_the_live_gates():
    from swarm_common.profiles import RUNNER_PROFILES

    assert RUNNER_PROFILES[lightpanda.PROFILE].image == "agent-runtime-lightpanda"
    assert lightpanda.CDP_PORT == 9223
    assert lightpanda.BINARY == "/opt/swarm/lightpanda/lightpanda"


def test_the_serve_child_starts_with_the_cli_and_is_stopped_after_it(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, lightpanda.PROFILE)
    assert seen["running"] == [(True, False)]
    (supervisor,) = seen["supervisors"]
    assert supervisor.stopped


def test_the_serve_child_is_stopped_when_the_cli_fails(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, lightpanda.PROFILE, fail=True)
    (supervisor,) = seen["supervisors"]
    assert supervisor.started and supervisor.stopped


def test_plain_claude_code_is_unchanged(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, "claude-code")
    assert seen["supervisors"] == []
    assert lightpanda.CDP_ENV not in seen["env"]
    assert lightpanda.MCP_SERVER_NAME not in seen["mcp"]
    assert "--disallowedTools" not in seen["argv"]
    assert lightpanda.LIGHTPANDA_NOTE not in seen["prompt"]


# --------------------------------------------------------------------------- #
# The child's environment, the flags and the denied tools                     #
# --------------------------------------------------------------------------- #


ALLOWED = {"PATH", "HOME", "TMPDIR", "LIGHTPANDA_DISABLE_TELEMETRY", "LIGHTPANDA_DISABLE_CORE_DUMP"}


def _env_dash_i(args: list[str]) -> dict[str, str]:
    """The NAME=VALUE pairs after `-i`, up to the binary."""
    assert args[0] == "-i"
    pairs = {}
    for arg in args[1:]:
        if "=" not in arg:
            break
        name, value = arg.split("=", 1)
        pairs[name] = value
    return pairs


def test_the_serve_child_env_is_exactly_the_allow_list(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, lightpanda.PROFILE)
    (supervisor,) = seen["supervisors"]
    token = seen["token"]
    assert set(supervisor.env) == ALLOWED
    assert supervisor.env["LIGHTPANDA_DISABLE_TELEMETRY"] == "true"
    assert supervisor.env["LIGHTPANDA_DISABLE_CORE_DUMP"] == "1"
    assert supervisor.env["HOME"] == str(tmp_path / "cache" / "lightpanda")
    assert supervisor.argv[0] == "/usr/bin/env"
    assert _env_dash_i(supervisor.argv[1:]) == supervisor.env
    assert token not in json.dumps([supervisor.argv, supervisor.env])
    for name in TOKEN_ENVS:
        assert name not in json.dumps([supervisor.argv, supervisor.env])


def test_the_mcp_server_runs_under_the_same_scrubbed_env(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, lightpanda.PROFILE)
    server = seen["mcp"][lightpanda.MCP_SERVER_NAME]
    assert server["command"] == "/usr/bin/env"
    assert "env" not in server
    assert set(_env_dash_i(server["args"])) == ALLOWED
    assert server["args"][-3:] == ["/opt/swarm/lightpanda/lightpanda", "mcp", "--block-private-networks"]
    assert seen["token"] not in json.dumps(seen["mcp"])


def test_child_env_takes_nothing_else_from_the_parent(tmp_path):
    token = secrets.token_hex(16)
    parent = {name: token for name in TOKEN_ENVS}
    parent.update(PATH="/usr/bin:/bin", TMPDIR=str(tmp_path / "t"), HOME="/home/elsewhere")
    env = lightpanda.child_env(parent, tmp_path / "cache")
    assert set(env) == ALLOWED
    assert token not in json.dumps(env)
    assert env["HOME"] == str(tmp_path / "cache" / "lightpanda")
    assert env["PATH"] == "/usr/bin:/bin"


def test_both_commands_block_private_networks():
    env = lightpanda.child_env({}, Path("/c"))
    serve = lightpanda.serve_argv(env)
    assert serve[serve.index(lightpanda.BINARY) + 1:] == [
        "serve", "--host", "127.0.0.1", "--port", "9223", "--cdp-max-connections", "64",
        "--block-private-networks", "--log-level", "warn"]
    assert "--block-private-networks" in lightpanda.mcp_server(env)["args"]


def test_disallowed_tools_names_the_three_tools_and_a_flag_follows(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, lightpanda.PROFILE)
    argv = seen["argv"]
    at = argv.index("--disallowedTools")
    assert argv[at + 1].split(",") == [
        "mcp__lightpanda__screenshot", "mcp__lightpanda__search", "mcp__lightpanda__getEnv"]
    # The flag is variadic in the CLI: the next argument must be a flag.
    assert argv[at + 2].startswith("--")


# --------------------------------------------------------------------------- #
# The supervisor                                                               #
# --------------------------------------------------------------------------- #


def test_the_limits_are_the_designs():
    assert lightpanda.MAX_RESTARTS == 20
    assert lightpanda.RESTART_FLOOR_SECONDS == 1.0


def test_the_supervisor_restarts_an_exiting_serve_and_stops_after_twenty(tmp_path):
    token = secrets.token_hex(16)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path),
           "LIGHTPANDA_DISABLE_TELEMETRY": "true", "LIGHTPANDA_DISABLE_CORE_DUMP": token}
    lines: list[tuple[str, dict[str, Any]]] = []
    calls: list[dict[str, Any]] = []

    def popen(argv, **kw):
        calls.append(kw)
        return subprocess.Popen(argv, **kw)

    supervisor = lightpanda.Supervisor(
        ["/bin/sh", "-c", "exit 3"], env, log=lambda msg, **f: lines.append((msg, f)),
        floor=0.0, popen=popen)
    supervisor.start()
    supervisor.join(30)
    assert supervisor.starts == 21  # the first start and 20 restarts
    assert len(lines) == 21  # one line per exit
    assert all(msg == "lightpanda_exited" and f["returncode"] == 3 for msg, f in lines)
    assert [f["gave_up"] for _, f in lines] == [False] * 20 + [True]
    assert token not in json.dumps(lines)
    assert not any(set(f) & ALLOWED for _, f in lines)
    # The runner's process group, which the worker kills with the attempt.
    assert not any(kw.get("start_new_session") for kw in calls)
    assert all(kw["env"] == env for kw in calls)


def test_the_supervisor_keeps_a_floor_between_starts(tmp_path):
    lines: list[Any] = []
    supervisor = lightpanda.Supervisor(
        ["/bin/sh", "-c", "exit 0"], {"HOME": str(tmp_path / "h")},
        log=lambda msg, **f: lines.append(f), max_restarts=3, floor=0.2)
    began = time.monotonic()
    supervisor.start()
    supervisor.join(30)
    assert supervisor.starts == 4
    assert time.monotonic() - began >= 0.6


def test_a_binary_that_will_not_start_is_logged_and_bounded(tmp_path):
    lines: list[Any] = []
    supervisor = lightpanda.Supervisor(
        [str(tmp_path / "no-such-lightpanda")], {}, log=lambda msg, **f: lines.append(f),
        max_restarts=2, floor=0.0)
    supervisor.start()
    supervisor.join(30)
    assert supervisor.starts == 3
    assert [f["reason"] for f in lines] == ["FileNotFoundError"] * 3


def test_stop_kills_a_running_serve_without_a_restart(tmp_path):
    lines: list[Any] = []
    procs: list[subprocess.Popen] = []
    running = threading.Event()

    def popen(argv, **kw):
        procs.append(subprocess.Popen(argv, **kw))
        running.set()
        return procs[-1]

    supervisor = lightpanda.Supervisor(
        [sys.executable, "-c", "import time; time.sleep(60)"], {}, log=lambda msg, **f: lines.append(f),
        floor=0.0, popen=popen)
    supervisor.start()
    assert running.wait(10)
    supervisor.stop()
    assert procs and procs[0].poll() is not None
    assert supervisor.starts == 1
    assert lines == []
