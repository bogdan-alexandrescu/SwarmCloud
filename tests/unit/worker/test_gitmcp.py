"""`swarm-git`, the shim (docs/runner-git-tools.md §1.1, §1.3; lane G3).

The shim holds nothing and decides nothing: it turns an MCP `tools/call` (or a
CLI call, for a runner without MCP) into one spool request and hands back the
worker's answer. These tests hold its schema budget, its vocabulary (the one
`gittools` validates against), and the spool round trip, with a thread
standing in for the worker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent_worker.gitmcp import server

#: graphmcp's budget (tests/unit/worker/test_graphmcp_budgets.py): every tool
#: schema plus the instructions under 2k tokens, at a conservative 3 bytes a
#: token for JSON. The agent pays it on every turn.
SCHEMA_TOKEN_BUDGET = 2000
BYTES_PER_TOKEN = 3


def test_the_tool_schemas_and_instructions_fit_graphmcps_two_thousand_token_budget():
    listed = json.dumps({"tools": server.TOOLS}, separators=(",", ":"))
    spent = len(listed.encode()) + len(server.INSTRUCTIONS.encode())
    assert spent <= SCHEMA_TOKEN_BUDGET * BYTES_PER_TOKEN, spent


def test_there_are_exactly_the_designs_tools_and_none_takes_a_token_or_remote():
    # The five of runner-git-tools.md §1.1, then lane AU-1's eight
    # (docs/design/agent-autonomy.md §3.12).
    assert server.TOOL_NAMES == ("git_fetch", "git_rebase_onto", "git_push_branch",
                                 "read_private", "pr_checks", "pr_view", "run_list", "run_view",
                                 "issue_view", "issue_list", "release_status", "issue_create",
                                 "issue_comment")
    every = set().union(*server.ARGUMENTS.values())
    assert every == {"ref", "branch", "repo", "path", "pr", "number", "workflow", "head_sha",
                     "status", "limit", "run_id", "failed_only", "labels", "state", "query",
                     "sha", "form", "title", "sections", "dedupe_key", "body", "reopen"}
    # Only read_private names a repository (§1.2 other_repository).
    assert [name for name, args in server.ARGUMENTS.items() if "repo" in args] == ["read_private"]


def test_the_shim_imports_no_worker_machinery():
    """It holds nothing: importing it pulls in neither the worker's lifecycle,
    its git library nor the attempt key."""
    code = ("import sys, agent_worker.gitmcp.server, agent_worker.gitmcp.__main__; "
            "bad = [m for m in ('agent_worker.lifecycle', 'agent_worker.gittools', "
            "'agent_worker.children', 'agent_worker.gitops') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def _worker(spool: Path, answer: dict, seen: list[dict], stop: threading.Event) -> None:
    """Stand in for `GitTools.tick`: answer every request, record what it read."""
    requests = spool / "requests"
    while not stop.is_set():
        for path in sorted(requests.glob("*.json")) if requests.is_dir() else []:
            seen.append(json.loads(path.read_text()))
            responses = spool / "responses"
            responses.mkdir(parents=True, exist_ok=True)
            (responses / path.name).write_text(json.dumps(answer))
            path.unlink()
        time.sleep(0.01)


def _with_worker(spool: Path, answer: dict):
    seen: list[dict] = []
    stop = threading.Event()
    thread = threading.Thread(target=_worker, args=(spool, answer, seen, stop), daemon=True)
    thread.start()
    return seen, stop, thread


def test_initialize_and_tools_list_answer_without_the_spool(tmp_path: Path):
    shim = server.GitServer(tmp_path / "spool")
    init = shim.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "swarm-git"
    assert init["result"]["instructions"] == server.INSTRUCTIONS
    listed = shim.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == list(server.TOOL_NAMES)
    assert shim.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert not (tmp_path / "spool").exists()


def test_a_tool_call_goes_through_the_spool_and_returns_the_workers_answer(tmp_path: Path):
    spool = tmp_path / "spool"
    answer = {"tool": "git_fetch", "result": {"ref": "main", "resolved_sha": "a" * 40}}
    seen, stop, thread = _with_worker(spool, answer)
    try:
        response = server.GitServer(spool).handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "git_fetch", "arguments": {"ref": "main"}}})
    finally:
        stop.set()
        thread.join()
    assert seen == [{"tool": "git_fetch", "arguments": {"ref": "main"}}]
    content = response["result"]
    assert content["isError"] is False
    assert json.loads(content["content"][0]["text"]) == answer
    assert list((spool / "responses").iterdir()) == []  # read once, then removed


def test_a_refusal_is_a_tool_error_the_agent_reads(tmp_path: Path):
    spool = tmp_path / "spool"
    refused = {"refused": {"code": "protected_branch", "message": "no", "retryable": False}}
    _seen, stop, thread = _with_worker(spool, refused)
    try:
        text, failed = server.GitServer(spool).call("git_push_branch", {"branch": "main"})
    finally:
        stop.set()
        thread.join()
    assert failed is True
    assert json.loads(text)["refused"]["code"] == "protected_branch"


def test_a_malformed_call_is_refused_without_writing_the_spool(tmp_path: Path):
    spool = tmp_path / "spool"
    shim = server.GitServer(spool)
    for name, arguments in [("git_push_branch", {"refspec": "+HEAD:main"}),
                            ("git_fetch", {}),
                            ("git_clone", {"url": "x"}),
                            ("read_private", "acme/lib")]:
        text, failed = shim.call(name, arguments)
        assert failed is True, name
        assert json.loads(text)["refused"]["code"] in ("request_invalid", "unknown_tool")
    assert not spool.exists()


def test_no_answer_in_time_says_the_request_is_still_queued(tmp_path: Path):
    ticks = iter(range(100))
    answer = server.submit(tmp_path / "spool", "pr_checks", {}, wait_seconds=3,
                           clock=lambda: float(next(ticks)), sleep=lambda _s: None)
    assert answer["refused"]["code"] == "worker_did_not_answer"
    assert answer["refused"]["retryable"] is True
    assert len(list((tmp_path / "spool" / "requests").glob("*.json"))) == 1


def _module(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "agent_worker.gitmcp", *args],
                          capture_output=True, text=True, env=env, timeout=60, input="")


def test_the_cli_form_makes_one_call_and_exits_by_the_answer(tmp_path: Path):
    spool = tmp_path / "spool"
    env = {**os.environ, server.SPOOL_ENV: str(spool)}
    seen, stop, thread = _with_worker(spool, {"tool": "git_rebase_onto",
                                              "result": {"conflicts": []}})
    try:
        done = _module("git_rebase_onto", "--ref", "main", env=env)
    finally:
        stop.set()
        thread.join()
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["result"] == {"conflicts": []}
    assert seen == [{"tool": "git_rebase_onto", "arguments": {"ref": "main"}}]

    seen, stop, thread = _with_worker(spool, {"refused": {"code": "bad_ref", "message": "x"}})
    try:
        done = _module("git_fetch", "--ref", "x", env=env)
    finally:
        stop.set()
        thread.join()
    assert done.returncode == 1


def test_without_a_spool_the_server_refuses_to_start(tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if k != server.SPOOL_ENV}
    done = _module(env=env)
    assert done.returncode == 2
    assert server.SPOOL_ENV in done.stderr


def test_the_stdio_server_answers_line_by_line(tmp_path: Path):
    env = {**os.environ, server.SPOOL_ENV: str(tmp_path / "spool")}
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "no/such"},
    ]
    done = subprocess.run([sys.executable, "-m", "agent_worker.gitmcp"], env=env, timeout=60,
                          input="\n".join(json.dumps(m) for m in lines) + "\nnot json\n",
                          capture_output=True, text=True)
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a.get("id") for a in answers] == [1, 2, 3, None]
    assert answers[2]["error"]["code"] == server.METHOD_NOT_FOUND
    assert answers[3]["error"]["code"] == server.PARSE_ERROR
