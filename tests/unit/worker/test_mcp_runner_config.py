"""The CLI runners name the caller's MCP servers through the worker's relay
(MCP-RUNNERS, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.7 step 4 and 5, and the
§1.2 side note about a repository's own `.mcp.json`. The relay and its file
are MCP-WORKER's (`agent_worker.mcp_relay`, `mcp_attach.McpAttachment.start`);
every case here writes the file through that real code, so the format the
runners read is the one the worker writes.

What each case pins (the lane's red-first list):

  * claude-code's `--mcp-config` holds each server as an `http` entry at the
    relay's loopback URL with the LOCAL token, and never the credential;
  * codex's `-c` overrides likewise, the token in the CLI's environment only;
  * the prompt gains one line per server;
  * a relay file that is not the worker's fails the runner before the CLI;
  * a checkpointed `work/` holds no credential, and the next attempt's
    local token is not this one's;
  * the PINNED binaries (images/agent-runtime-base/Dockerfile) list the fake
    server's tools through the relay, and the fake sees the credential --
    run only where those binaries are installed, which is the image;
  * the §1.2 question, answered: the pinned claude-code loads the
    repository's `.mcp.json` under `--print`, and the worker's config wins a
    server name both declare.

With no relay file (every task while SWARM_MCP_SERVERS is off) nothing
changes, and a control case holds that.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import functools
import gzip
import http.client
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import mcp_attach as attach_mod
from agent_worker import mcp_relay as relay_mod
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.logs import build_logger
from agent_worker.runners import claude_code, codex
from agent_worker.runners.base import RunnerContext, RunnerFailure

from worker_seeds import TENANT

REPO = Path(__file__).resolve().parents[3]
PUBLIC = "8.8.8.8"
TOOL = "kb_search"


def fake_credential() -> str:
    return "cred-" + secrets.token_hex(20)


def _logger(stream: io.StringIO | None = None, attempt_id: str = "att_1"):
    return build_logger(task_id="task_1", attempt_id=attempt_id, tenant_id=TENANT,
                        generation=1, runner_profile="claude-code",
                        stream=stream or io.StringIO())


class FakeMcp:
    """A loopback MCP server speaking streamable HTTP with JSON answers: it
    initializes, lists one tool and records every request's headers."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw or b"null")
                except ValueError:
                    body = None
                outer.requests.append((dict(self.headers.items()), body))
                if not isinstance(body, dict) or "id" not in body:
                    return self._empty(202)
                method = body.get("method")
                if method == "initialize":
                    result: Any = {
                        "protocolVersion": (body.get("params") or {}).get(
                            "protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-kb", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": TOOL, "description": "Search the knowledge base.",
                                         "inputSchema": {"type": "object", "properties": {}}}]}
                else:
                    result = {}
                out = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self) -> None:
                self._empty(405)

            do_DELETE = do_GET

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def connector(self, host: str, address: str, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def methods(self) -> list[str]:
        return [b.get("method") for _, b in self.requests if isinstance(b, dict)]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    fake = FakeMcp()
    yield fake
    fake.stop()


def attach(private: Path, credential: str, *, upstream: FakeMcp | None = None,
           names: tuple[str, ...] = ("kb",), stream: io.StringIO | None = None,
           attempt_id: str = "att_1") -> attach_mod.McpAttachment:
    """The worker's own attachment, started on `servers` the way the
    lifecycle starts it (STEP 6a''): the relay serves and writes its file."""
    factory = functools.partial(relay_mod.Relay, resolver=lambda host: [PUBLIC],
                                connector=(upstream.connector if upstream else None)
                                or relay_mod.connect)
    cfg = SimpleNamespace(task_id="task_1", attempt_id=attempt_id, generation=1)
    att = attach_mod.McpAttachment(cfg, children=None, logger=_logger(stream, attempt_id),
                                   relay_factory=factory)
    att.result = attach_mod.Attached(servers=[
        relay_mod.Server(name=name, url=f"https://{name}.example.com/mcp",
                         host=f"{name}.example.com", path="/mcp", header="Authorization",
                         scheme="Bearer", description=f"The {name} graph of decisions.",
                         credential=credential)
        for name in names])
    private.mkdir(parents=True, exist_ok=True)
    assert att.start(private) is not None
    return att


def _ctx(work: Path, prompt: str = "Answer from the knowledge base.") -> RunnerContext:
    work.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work, artifacts_dir=work / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": prompt})


def _run_body(module, monkeypatch, work: Path, relay_file: Path | None, *,
              git_spool: str | None = None) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        seen["prompt"] = ctx.payload.get("prompt")
        return {}

    monkeypatch.setattr(module, "run_cli_agent", fake_run)
    # SWARM_CHILDREN too: a SwarmCloud agent container sets it, and the runner
    # then appends the child-task note to the prompt a test compares.
    for name in ("SWARM_GIT", "SWARM_CHILDREN", "SWARM_SEARCH", "PLAYWRIGHT_BROWSERS_PATH",
                 "SWARM_CDP_ENDPOINT", "SWARM_GRAPH_SNAPSHOT", "SWARM_GRAPH_PIN",
                 "SWARM_GRAPH_UNSTAGED"):
        monkeypatch.delenv(name, raising=False)
    if git_spool is not None:
        monkeypatch.setenv("SWARM_GIT", git_spool)
    if relay_file is None:
        monkeypatch.delenv(attach_mod.RELAY_ENV, raising=False)
    else:
        monkeypatch.setenv(attach_mod.RELAY_ENV, str(relay_file))
    module.body(_ctx(work))
    return seen


def _mcp_config(seen: dict[str, Any]) -> dict[str, Any] | None:
    args = seen["args"]
    if "--mcp-config" not in args:
        return None
    assert args.count("--mcp-config") == 1, "one config file, every server in it"
    return json.loads(Path(args[args.index("--mcp-config") + 1]).read_text(encoding="utf-8"))


def _files_holding(root: Path, value: str) -> list[str]:
    needle = value.encode()
    return [str(p) for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


# --------------------------------------------------------------------------
# claude-code
# --------------------------------------------------------------------------


def test_claude_code_config_holds_the_relay_url_and_local_token_never_the_credential(
        monkeypatch, tmp_path):
    credential = fake_credential()
    att = attach(tmp_path / "private", credential)
    try:
        relay = att.relay
        seen = _run_body(claude_code, monkeypatch, tmp_path / "work", att.path)
        config = _mcp_config(seen)
        assert config == {"mcpServers": {"kb": {
            "type": "http", "url": relay.url_for("kb"),
            "headers": {"Authorization": f"Bearer {relay.token}"}}}}, \
            "claude-code's --mcp-config does not name the relay for an attached server"
        assert relay.url_for("kb").startswith("http://127.0.0.1:")
        # The credential is in no file the runner wrote, no flag, no variable
        # and not the prompt.
        assert _files_holding(tmp_path / "work", credential) == []
        assert credential not in json.dumps(seen)
        # The line per server, with its description and where its tools are.
        assert ("The MCP server `kb` (The kb graph of decisions) is connected for this "
                "task: its tools are listed under the MCP server `kb`.") in seen["prompt"]
        assert seen["prompt"].startswith("Answer from the knowledge base.")
    finally:
        att.close()


def test_claude_code_puts_every_server_beside_the_platforms_in_one_config(monkeypatch, tmp_path):
    att = attach(tmp_path / "private", fake_credential(), names=("kb", "docs"))
    try:
        seen = _run_body(claude_code, monkeypatch, tmp_path / "work", att.path,
                         git_spool=str(tmp_path / ".swarm-git"))
        servers = _mcp_config(seen)["mcpServers"]
        assert set(servers) == {"swarm-git", "kb", "docs"}
        assert servers["docs"]["url"] == att.relay.url_for("docs")
        assert servers["swarm-git"]["type"] == "stdio"
    finally:
        att.close()


def test_without_a_relay_claude_code_is_unchanged(monkeypatch, tmp_path):
    """The control, and the dark launch: no relay file, no config, no line."""
    seen = _run_body(claude_code, monkeypatch, tmp_path / "work", None)
    assert _mcp_config(seen) is None
    assert seen["prompt"] == "Answer from the knowledge base."


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------


def _overrides(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "-c"]


def test_codex_overrides_hold_the_relay_url_and_local_token_never_the_credential(
        monkeypatch, tmp_path):
    credential = fake_credential()
    att = attach(tmp_path / "private", credential)
    try:
        relay = att.relay
        seen = _run_body(codex, monkeypatch, tmp_path / "work", att.path)
        overrides = _overrides(seen["args"])
        assert f"mcp_servers.kb.url={json.dumps(relay.url_for('kb'))}" in overrides, \
            "codex's overrides do not name the relay for an attached server"
        assert ('mcp_servers.kb.bearer_token_env_var="SWARM_MCP_RELAY_TOKEN"'
                in overrides)
        # The local token in the CLI's environment, never in its argv.
        assert seen["env"]["SWARM_MCP_RELAY_TOKEN"] == relay.token
        assert relay.token not in json.dumps(seen["args"])
        assert credential not in json.dumps(seen)
        assert _files_holding(tmp_path / "work", credential) == []
        assert "its tools are listed under the MCP server `kb`." in seen["prompt"]
    finally:
        att.close()


def test_without_a_relay_codex_is_unchanged(monkeypatch, tmp_path):
    seen = _run_body(codex, monkeypatch, tmp_path / "work", None)
    assert "-c" not in seen["args"]
    assert "SWARM_MCP_RELAY_TOKEN" not in seen["env"]
    assert seen["prompt"] == "Answer from the knowledge base."


# --------------------------------------------------------------------------
# A relay file that is not the worker's
# --------------------------------------------------------------------------


def _bad_files(private: Path, good: dict[str, Any]) -> dict[str, Path]:
    def variant(name: str, data: Any) -> Path:
        path = private / f"{name}.json"
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return path

    server = good["servers"][0]
    files = {
        "remote url": variant("remote", {**good, "servers": [
            {**server, "url": "https://kb.example.com/mcp"}]}),
        "path of another name": variant("other", {**good, "servers": [
            {**server, "url": server["url"].replace("/mcp/kb", "/mcp/docs")}]}),
        "platform name": variant("platform", {**good, "servers": [
            {**server, "name": "swarm-git",
             "url": server["url"].replace("/mcp/kb", "/mcp/swarm-git")}]}),
        "reserved name": variant("reserved", {**good, "servers": [
            {**server, "name": "playwright",
             "url": server["url"].replace("/mcp/kb", "/mcp/playwright")}]}),
        "no token": variant("notoken", {**good, "token": ""}),
        "not json": variant("notjson", "{"),
        "missing": private / "absent.json",
    }
    link = private / "link.json"
    link.symlink_to(private / "mcp-relay.json")
    files["a link"] = link
    return files


@pytest.mark.parametrize("module", [claude_code, codex], ids=["claude-code", "codex"])
def test_a_relay_file_that_is_not_the_workers_fails_before_the_cli(monkeypatch, tmp_path, module):
    att = attach(tmp_path / "private", fake_credential())
    try:
        good = json.loads(att.path.read_text())
        started = []
        for why, path in _bad_files(tmp_path / "private", good).items():
            try:
                _run_body(module, monkeypatch, tmp_path / "work", path)
            except RunnerFailure as exc:
                assert str(exc).startswith("mcp_relay_unreadable"), why
            else:
                started.append(why)
        assert started == [], f"the CLI was started on a relay file that is not the worker's: {started}"
    finally:
        att.close()


# --------------------------------------------------------------------------
# The checkpoint
# --------------------------------------------------------------------------


def _archive_bytes(store, record) -> bytes:
    return gzip.decompress(store.download_bytes(record.archive_key))


def test_a_checkpointed_work_holds_no_credential_and_the_next_token_differs(
        monkeypatch, store, tmp_path):
    credential = fake_credential()
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    att = attach(ws.private, credential)
    try:
        token = att.relay.token
        seen = _run_body(claude_code, monkeypatch, ws.work, att.path)
        config_path = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
        assert config_path.is_relative_to(ws.work)
        record = CheckpointManager(store=store, tenant_id=TENANT, task_id="task_1",
                                   attempt_id="att_1", generation=1,
                                   logger=_logger()).create(ws)
        archive = _archive_bytes(store, record)
        # The control: the search can find what is there. The local token IS
        # in `work/`, deliberately (§4.7 step 5) ...
        assert token.encode() in archive
        # ... and the caller's credential is not.
        assert credential.encode() not in archive
        # Nor anywhere on disk once the attempt has closed its relay.
    finally:
        att.close()
    assert not (ws.private / attach_mod.RELAY_FILE).exists()
    assert _files_holding(tmp_path / "ws", credential) == []

    # The next attempt: restored, attached again, its own token.
    ws2 = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager2 = CheckpointManager(store=store, tenant_id=TENANT, task_id="task_1",
                                 attempt_id="att_2", generation=2, logger=_logger())
    manager2.restore(manager2.find_latest(), ws2)
    restored = json.loads((ws2.work / claude_code.SETTINGS_DIR_NAME
                           / claude_code.MCP_CONFIG_NAME).read_text())
    assert restored["mcpServers"]["kb"]["headers"]["Authorization"] == f"Bearer {token}"
    att2 = attach(ws2.private, credential, attempt_id="att_2")
    try:
        seen = _run_body(claude_code, monkeypatch, ws2.work, att2.path)
        header = _mcp_config(seen)["mcpServers"]["kb"]["headers"]["Authorization"]
        assert header == f"Bearer {att2.relay.token}"
        assert att2.relay.token != token
    finally:
        att2.close()


# --------------------------------------------------------------------------
# The pinned binaries (the image test: only where they are installed)
# --------------------------------------------------------------------------


def _pinned(arg: str) -> str:
    text = (REPO / "images/agent-runtime-base/Dockerfile").read_text(encoding="utf-8")
    match = re.search(rf"^ARG {arg}=(\S+)$", text, re.MULTILINE)
    assert match, f"{arg} is not pinned in the image"
    return match.group(1)


def _installed(binary: str, arg: str) -> bool:
    if shutil.which(binary) is None:
        return False
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return _pinned(arg) in out


needs_claude = pytest.mark.skipif(not _installed("claude", "CLAUDE_CODE_VERSION"),
                                  reason="the pinned claude-code is not installed (image test)")
needs_codex = pytest.mark.skipif(not _installed("codex", "CODEX_VERSION"),
                                 reason="the pinned codex is not installed (image test)")


class FakeModelApi:
    """A loopback model API that records each request and refuses it: the
    CLI's first request carries the tools it offers the model."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                outer.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                out = b'{"type":"error","error":{"type":"invalid_request_error","message":"fake"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tool_names(self) -> list[str]:
        names: list[str] = []
        for raw in self.bodies:
            try:
                body = json.loads(raw)
            except ValueError:
                continue
            for tool in body.get("tools") or [] if isinstance(body, dict) else []:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    names.append(tool["name"])
        return names

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def model_api():
    fake = FakeModelApi()
    yield fake
    fake.stop()


def _cli_env(home: Path, **extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LC_ALL": "C.UTF-8",
            "CI": "1", "NO_COLOR": "1", **extra}


def _claude(work: Path, config: Path, model_api: FakeModelApi) -> subprocess.CompletedProcess:
    """The runner's flags (`claude_code.SPEC`), HOME and the prompt on stdin,
    against the fake model API."""
    env = _cli_env(work, ANTHROPIC_API_KEY="sk-" + secrets.token_hex(16),
                   ANTHROPIC_BASE_URL=model_api.url,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
    return subprocess.run(["claude", *claude_code.SPEC.args_default, "--mcp-config", str(config)],
                          cwd=work / "repo", env=env, input="List your tools.",
                          capture_output=True, text=True, timeout=180)


@needs_claude
def test_the_pinned_claude_code_lists_the_servers_tools_through_the_relay(
        monkeypatch, tmp_path, upstream, model_api):
    credential = fake_credential()
    att = attach(tmp_path / "private", credential, upstream=upstream)
    try:
        work = tmp_path / "work"
        (work / "repo").mkdir(parents=True)
        seen = _run_body(claude_code, monkeypatch, work, att.path)
        config = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
        _claude(work, config, model_api)
        assert f"mcp__kb__{TOOL}" in model_api.tool_names(), \
            "the pinned claude-code did not offer the relayed server's tools"
        assert "tools/list" in upstream.methods()
        # The server saw the caller's credential, and only ever that.
        assert {h.get("Authorization") for h, _ in upstream.requests} == {f"Bearer {credential}"}
        assert all(credential not in raw.decode("utf-8", "replace")
                   for raw in model_api.bodies)
    finally:
        att.close()


@needs_claude
def test_the_pinned_claude_code_loads_the_repositorys_mcp_json_and_ours_wins_a_name(
        monkeypatch, tmp_path, upstream, model_api):
    """§1.2, answered: the pinned CLI under `--print` without
    `--strict-mcp-config` DOES load the checkout's `.mcp.json`. A server
    named only there is offered to the model; a server the repository names
    the same as an attached one is not contacted, the attached one is."""
    att = attach(tmp_path / "private", fake_credential(), upstream=upstream)
    try:
        work = tmp_path / "work"
        repo = work / "repo"
        repo.mkdir(parents=True)
        # The repository's own servers: one under a name of its own, at the
        # relay with the right token (so it can list tools), and one under
        # the attached server's name, at a path of the relay no server has.
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "repo-own": {"type": "http", "url": att.relay.url_for("kb"),
                         "headers": {"Authorization": f"Bearer {att.relay.token}"}},
            "kb": {"type": "http", "url": att.relay.url_for("kb").replace("/mcp/kb", "/mcp/zz"),
                   "headers": {"Authorization": "Bearer " + secrets.token_hex(8)}},
        }}))
        stream = io.StringIO()
        att.relay.log = _logger(stream)
        seen = _run_body(claude_code, monkeypatch, work, att.path)
        config = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
        _claude(work, config, model_api)
        tools = model_api.tool_names()
        assert f"mcp__repo-own__{TOOL}" in tools, "the repository's .mcp.json was not loaded"
        assert f"mcp__kb__{TOOL}" in tools
        assert '"status": 401' not in stream.getvalue() and "/mcp/zz" not in stream.getvalue(), \
            "the repository's `kb` was contacted: the attached one did not win the name"
    finally:
        att.close()


@needs_codex
def test_the_pinned_codex_lists_the_servers_tools_through_the_relay(
        monkeypatch, tmp_path, upstream, model_api):
    """0.154.0 accepts `url` and `bearer_token_env_var`, and initializes and
    lists the server's tools through the relay (which admits only the local
    token). It offers MCP tools to the model as deferred nested tools of its
    `exec` tool, so the model API's first request is not where they show."""
    credential = fake_credential()
    att = attach(tmp_path / "private", credential, upstream=upstream)
    try:
        work = tmp_path / "work"
        (work / "repo").mkdir(parents=True)
        seen = _run_body(codex, monkeypatch, work, att.path)
        provider = ('model_providers.fake={name="fake", base_url="' + model_api.url
                    + '/v1", env_key="OPENAI_API_KEY", wire_api="responses", '
                    'request_max_retries=0, stream_max_retries=0}')
        env = _cli_env(work, OPENAI_API_KEY="sk-" + secrets.token_hex(16), **seen["env"])
        subprocess.run(["codex", *codex.SPEC.args_default, "-c", provider,
                        "-c", 'model_provider="fake"', *seen["args"], *codex.SPEC.stdin_arg],
                       cwd=work / "repo", env=env, input="List your tools.",
                       capture_output=True, text=True, timeout=180)
        assert "tools/list" in upstream.methods(), \
            "the pinned codex did not list the relayed server's tools"
        assert {h.get("Authorization") for h, _ in upstream.requests} == {f"Bearer {credential}"}
        assert model_api.bodies, "codex never reached the fake model API"
        assert all(credential not in raw.decode("utf-8", "replace")
                   for raw in model_api.bodies)
    finally:
        att.close()
