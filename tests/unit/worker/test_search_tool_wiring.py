"""The search tool, wired (docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

The relay lives inside `ChildPath`, beside the git tools', and claude-code
names the `swarm-search` shim. What this file holds:

  * the tool is offered ONLY when swarm-api's flag is on (its key-registration
    answer says `search_tool: true`), the attempt is keyed, and the profile is
    a live-browser claude-code one; with the flag off there is no
    `SWARM_SEARCH`, no server in `--mcp-config` and no word about a search
    tool in the note;
  * claude-code's live-browser note tells the agent to search with the tool
    and open the results in the live browser, and the shim shares the one
    `--mcp-config` with Playwright MCP;
  * the configured command really starts the shim and lists one tool;
  * a search is relayed signed with the attempt key, carrying the attempt's
    fencing fields, and a fenced search raises the error the lifecycle exits
    on (invariant 5);
  * a malformed request is refused before anything leaves the worker.

Only swarm-api is faked, at the `ChildApi.call` boundary, with
`swarm_api.childkey`'s own verifier.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import searchtools
from agent_worker.gitmcp import server as git_shim
from agent_worker.searchmcp import server as shim
from swarm_api import childkey as api_childkey
from swarm_api import search as api_search

from worker_seeds import TENANT

NONCE = "a1b2c3d4-test-nonce-not-a-secret"
BROWSER = "claude-code-browser"
RESULT = {"provider": "brave", "query": "q", "count": 1,
          "results": [{"title": "t", "url": "https://example.com/", "snippet": "s"}]}


# --------------------------------------------------------------------------
# The worker's restatements, held to swarm-api's
# --------------------------------------------------------------------------


def test_the_shims_limits_are_swarm_apis():
    assert shim.MAX_QUERY_CHARS == api_search.MAX_QUERY_CHARS
    assert shim.MAX_COUNT == api_search.MAX_COUNT
    assert shim.DEFAULT_COUNT == api_search.DEFAULT_COUNT


def test_the_offer_rule_is_swarm_apis():
    """The worker offers the tool on exactly the profiles swarm-api answers."""
    from swarm_common.profiles import RUNNER_PROFILES

    offered = {name for name in RUNNER_PROFILES if children_mod.offers_search_tool(name)}
    answered = {name for name in RUNNER_PROFILES
                if api_search.offers_search({"runner_profile": name})}
    assert offered == {BROWSER}
    assert answered >= offered


# --------------------------------------------------------------------------
# The relay inside ChildPath
# --------------------------------------------------------------------------


class FakeRoutes:
    """swarm-api's worker-only routes: the key registration and the search."""

    def __init__(self, *, flag: bool = True) -> None:
        self.flag = flag
        self.calls: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.fence = False
        self.missing = False

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            self.public_key = json.loads(body).get("public_key")
            return 201, {"registered": "key" if self.public_key else "tombstone",
                         "search_tool": self.flag}
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        message = api_childkey.request_message(method, path, body or b"", stamp)
        if not (self.public_key and proof
                and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(403, "search_unproven", "", retryable=False)
        if path.endswith("/search"):
            if self.missing:
                raise children_mod.ChildApiError(404, "not_found", "no such route",
                                                 retryable=False)
            if self.fence:
                raise children_mod.ChildApiError(409, "search_fenced", "superseded",
                                                 retryable=False)
            return 200, dict(RESULT)
        raise AssertionError(f"unexpected route {method} {path}")

    def searches(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].endswith("/search")]


class _Log:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, msg: str, **k: Any) -> None:
        self.lines.append((msg, k))

    def warning(self, msg: str, **k: Any) -> None:
        self.lines.append((msg, k))

    def error(self, msg: str, **k: Any) -> None:
        self.lines.append((msg, k))


def _path(routes: FakeRoutes, *, profile: str = BROWSER, nonce: str = NONCE):
    from agent_worker.hardening import PROTECTED, MemoryProtection

    cfg = SimpleNamespace(
        child_submit_retry_seconds=5, max_child_requests_per_tick=4,
        max_child_request_bytes=262144, tenant_id=TENANT, attempt_id="att_1",
        lease_id="lease_1", generation=1, task_id="task_1", swarm_api_url=None,
        swarm_api_audience=None, child_nonce=nonce, runner_profile=profile,
        git_branch_prefix="swarm/", git_author_name="swarmcloud agent",
        git_author_email="swarmcloud-agent@users.noreply.github.com",
        git_clone_timeout_seconds=60,
    )
    path = children_mod.ChildPath(cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
                                  api=routes)
    path.register(lambda _note: None)
    return path


def _prepare(path, tmp_path: Path) -> tuple[dict[str, str], Path]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return path.prepare(work, task={"id": "task_1"}), work


def test_a_live_browser_attempt_with_the_flag_on_gets_the_spool(tmp_path):
    path = _path(FakeRoutes(flag=True))
    env, work = _prepare(path, tmp_path)
    assert env[shim.SPOOL_ENV] == str(work / shim.SPOOL_DIR_NAME)
    assert path.search_offered and path.search_unavailable is None


@pytest.mark.parametrize("why,routes,profile,nonce", [
    ("search_tool_disabled", FakeRoutes(flag=False), BROWSER, NONCE),
    ("profile_has_no_search_tool", FakeRoutes(flag=True), "claude-code", NONCE),
    ("profile_has_no_search_tool", FakeRoutes(flag=True), "codex", NONCE),
    ("no_attempt_key", FakeRoutes(flag=True), BROWSER, ""),
])
def test_no_search_spool_unless_flag_profile_and_key(tmp_path, why, routes, profile, nonce):
    path = _path(routes, profile=profile, nonce=nonce)
    env, work = _prepare(path, tmp_path)
    assert shim.SPOOL_ENV not in env
    assert not (work / shim.SPOOL_DIR_NAME).exists()
    assert not path.search_offered and path.search_unavailable == why


def test_an_old_swarm_api_that_says_nothing_offers_no_search(tmp_path):
    """A registration answer without `search_tool` (a swarm-api before this
    lane) is the flag off, never on."""
    routes = FakeRoutes()
    original = routes.call

    def call(method, path, **kw):
        status, payload = original(method, path, **kw)
        payload.pop("search_tool", None)
        return status, payload

    routes.call = call  # type: ignore[method-assign]
    path = _path(routes)
    env, _ = _prepare(path, tmp_path)
    assert shim.SPOOL_ENV not in env and path.search_unavailable == "search_tool_disabled"


def test_a_search_is_relayed_signed_on_the_tick(tmp_path):
    routes = FakeRoutes()
    path = _path(routes)
    env, work = _prepare(path, tmp_path)
    spool = Path(env[shim.SPOOL_ENV])
    git_shim.write_request(spool, "r1", "search", {"query": "  python\n  docs ", "count": 3})
    assert path.has_requests(work)
    # A newline inside the query is a control character: refused locally.
    assert path.tick(work) == 1
    refused = json.loads((spool / "responses" / "r1.json").read_text())
    assert refused["refused"]["code"] == "request_invalid"
    assert routes.searches() == []
    git_shim.write_request(spool, "r2", "search", {"query": "python   docs", "count": 3})
    assert path.tick(work) == 1
    answer = json.loads((spool / "responses" / "r2.json").read_text())
    assert answer == {"tool": "search", "result": RESULT}
    (call,) = routes.searches()
    assert call["path"] == "/v1/attempts/att_1/search"
    sent = json.loads(call["body"])
    assert sent == {"tenant_id": TENANT, "task_id": "task_1", "attempt_id": "att_1",
                    "lease_id": "lease_1", "generation": 1, "request_id": "r2",
                    "query": "python docs", "count": 3}
    assert not path.has_requests(work)


@pytest.mark.parametrize("tool,arguments", [
    ("search", {}),
    ("search", {"query": ""}),
    ("search", {"query": "x" * 401}),
    ("search", {"query": "q", "count": 0}),
    ("search", {"query": "q", "count": 11}),
    ("search", {"query": "q", "count": True}),
    ("search", {"query": "q", "provider": "google"}),
    ("search", {"query": "q", "url": "https://example.com"}),
    ("git_fetch", {"ref": "main"}),
])
def test_a_malformed_request_is_refused_before_anything_leaves(tmp_path, tool, arguments):
    routes = FakeRoutes()
    path = _path(routes)
    env, work = _prepare(path, tmp_path)
    spool = Path(env[shim.SPOOL_ENV])
    git_shim.write_request(spool, "r1", tool, arguments)
    path.tick(work)
    answer = json.loads((spool / "responses" / "r1.json").read_text())
    assert answer["refused"]["code"] in ("request_invalid", "unknown_tool")
    assert routes.searches() == []


def test_a_fenced_search_raises_the_error_the_lifecycle_exits_on(tmp_path):
    routes = FakeRoutes()
    routes.fence = True
    path = _path(routes)
    env, work = _prepare(path, tmp_path)
    spool = Path(env[shim.SPOOL_ENV])
    git_shim.write_request(spool, "r1", "search", {"query": "q"})
    with pytest.raises(children_mod.ChildSubmitFenced):
        path.tick(work)
    answer = json.loads((spool / "responses" / "r1.json").read_text())
    assert answer["refused"]["code"] == "fenced"


def test_a_switched_off_route_tells_the_agent_to_use_the_browser(tmp_path):
    """The flag turned off mid-attempt: the route is a 404, never a crash."""
    routes = FakeRoutes()
    routes.missing = True
    path = _path(routes)
    env, work = _prepare(path, tmp_path)
    spool = Path(env[shim.SPOOL_ENV])
    git_shim.write_request(spool, "r1", "search", {"query": "q"})
    path.tick(work)
    answer = json.loads((spool / "responses" / "r1.json").read_text())
    assert answer["refused"]["code"] == "search_unavailable"


def test_the_relays_log_holds_no_query(tmp_path):
    routes = FakeRoutes()
    path = _path(routes)
    env, work = _prepare(path, tmp_path)
    git_shim.write_request(Path(env[shim.SPOOL_ENV]), "r1", "search",
                           {"query": "my private question"})
    path.tick(work)
    assert "my private question" not in json.dumps(path.log.lines, default=str)


def test_the_hourly_budget_is_refused_retryable(tmp_path):
    relay = searchtools.SearchTools(
        searchtools.SearchToolsConfig(TENANT, "task_1", "att_1", "lease_1", 1, calls_per_hour=1),
        key=children_mod.AttemptKey(), api=FakeRoutes(), logger=_Log(),
    )
    relay._api.public_key = relay._key.public  # registered
    assert "result" in relay.answer("search", {"query": "q"}, request_id="r1")
    refused = relay.answer("search", {"query": "q"}, request_id="r2")
    assert refused["refused"]["code"] == "rate_limited" and refused["refused"]["retryable"]


def test_preparing_again_keeps_the_relay(tmp_path):
    path = _path(FakeRoutes())
    _, work = _prepare(path, tmp_path)
    relay = path._search
    again = path.prepare(work, task={"id": "task_1"})
    assert path._search is relay and shim.SPOOL_ENV in again


# --------------------------------------------------------------------------
# claude-code names the shim, and its note says so
# --------------------------------------------------------------------------


def _ctx(tmp_path: Path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Find the release notes."},
    )


def _run(monkeypatch, tmp_path, *, search: str | None,
         endpoint: str | None = "http://127.0.0.1:9222") -> tuple[dict[str, Any], Any]:
    from agent_worker.runners import claude_code

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        seen["prompt"] = ctx.payload.get("prompt")
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name, value in ((shim.SPOOL_ENV, search), (git_shim.SPOOL_ENV, None),
                        ("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright"),
                        ("SWARM_CDP_ENDPOINT", endpoint)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    claude_code.body(_ctx(tmp_path))
    args = seen["args"]
    config = (json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
              if "--mcp-config" in args else None)
    return seen, config


def _shim_server(spool: str) -> dict[str, Any]:
    return {"type": "stdio", "command": sys.executable,
            "args": ["-I", "-m", "agent_worker.searchmcp", "--spool", spool]}


def test_claude_code_names_the_search_shim_beside_the_live_browser(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    spool = str(tmp_path / shim.SPOOL_DIR_NAME)
    seen, config = _run(monkeypatch, tmp_path, search=spool)
    assert seen["args"].count("--mcp-config") == 1
    assert config["mcpServers"][shim.SERVER_NAME] == _shim_server(spool)
    assert claude_code.MCP_SERVER_NAME in config["mcpServers"]
    assert seen["env"][shim.SPOOL_ENV] == spool
    assert claude_code.LIVE_BROWSER_NOTE in seen["prompt"]
    assert claude_code.LIVE_BROWSER_SEARCH_NOTE in seen["prompt"]
    note = claude_code.LIVE_BROWSER_SEARCH_NOTE.lower()
    assert "search tool" in note and "open the results in the live browser" in note


def test_without_the_flag_there_is_no_search_server_and_no_word_of_it(monkeypatch, tmp_path):
    """The control: no spool (the flag off), the live browser's config and
    note exactly as before this lane."""
    from agent_worker.runners import claude_code

    seen, config = _run(monkeypatch, tmp_path, search=None)
    assert set(config["mcpServers"]) == {claude_code.MCP_SERVER_NAME}
    assert shim.SPOOL_ENV not in seen["env"]
    assert claude_code.LIVE_BROWSER_NOTE in seen["prompt"]
    assert "search tool" not in seen["prompt"]


def test_the_configured_command_starts_the_shim_and_lists_one_tool(tmp_path):
    from agent_worker.runners import claude_code

    spool = tmp_path / shim.SPOOL_DIR_NAME
    path = claude_code.write_mcp_config(tmp_path / "cfg", search_spool=str(spool))
    server = json.loads(path.read_text())["mcpServers"][shim.SERVER_NAME]
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search", "arguments": {"query": "q", "provider": "x"}}},
    ]
    done = subprocess.run(
        [server["command"], *server["args"]],
        input="".join(json.dumps(line) + "\n" for line in lines),
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert done.returncode == 0, done.stderr
    answers = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert answers[0]["result"]["serverInfo"]["name"] == shim.SERVER_NAME
    assert [t["name"] for t in answers[1]["result"]["tools"]] == ["search"]
    # An argument outside the schema is refused by the shim, before the spool.
    assert answers[2]["result"]["isError"] is True
    assert not (spool / "requests").exists() or not any((spool / "requests").iterdir())


def test_the_git_shim_still_lists_its_own_tools():
    """The loop was made reusable, not changed: swarm-git's vocabulary is its own."""
    server = git_shim.GitServer(Path("/nonexistent"))
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == list(git_shim.TOOL_NAMES)
    assert "search" not in git_shim.TOOL_NAMES
