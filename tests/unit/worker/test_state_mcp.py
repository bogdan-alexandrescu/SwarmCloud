"""The `swarm-state` MCP server and its worker relay (docs/design/agent-autonomy.md
§3.4; lane AU-4 STATE-READS, part of #1933).

The shim (`agent_worker.statemcp.server`) holds nothing; the relay
(`agent_worker.statemcp.relay.StateReads`) validates each spooled request,
signs a GET under `swarm-state-req/v1` with the attempt key, and writes
swarm-api's answer. These tests hold, offline:

  * the shim's vocabulary is swarm-api's (`attemptreads.ARGUMENTS`), and no
    tool takes a tenant, a namespace or a filter;
  * the tuple the relay sends is the worker's own, never a request's: the
    tenant reaches swarm-api only as the lease's tuple says;
  * a stale generation (409) ends the attempt through the fenced exit;
  * every tool is read-only: the relay sends GETs and nothing else;
  * the proof verifies under swarm-api's own verifier and the state purpose;
  * `logs_unavailable` reaches the agent as a refusal it should not retry;
  * claude-code names `swarm-state` in its one `--mcp-config` exactly when
    the worker exported the spool.

No credentials, no network: the transport is a recorder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker.children import PROOF_HEADER, TIMESTAMP_HEADER, AttemptKey, ChildApiError
from agent_worker.gitmcp import server as git_shim
from agent_worker.runners import claude_code
from agent_worker.statemcp import relay as relay_mod
from agent_worker.statemcp import server as shim
from agent_worker.statemcp.relay import (
    StateReadRefused,
    StateReads,
    StateReadsConfig,
    StateReadsFenced,
    validate_request,
)
from swarm_api import attemptreads as api_reads
from swarm_api import childkey as api_childkey

CFG = StateReadsConfig(tenant_id="eng", task_id="task_fixer", attempt_id="att_fixer",
                       lease_id="lease_fixer", generation=3)
#: One well-formed argument set per tool.
GOOD = {
    "agents_list": {"state": "FAILED", "window_hours": 7},
    "agent_view": {"agent_id": "task_failed"},
    "agent_events": {"agent_id": "task_failed", "limit": 50},
    "agent_log_tail": {"agent_id": "task_failed", "stream": "agent_stderr"},
    "agent_artifacts": {"agent_id": "task_failed", "name": "report.md"},
    "workflow_view": {"workflow_id": "wf_1"},
    "run_view_sc": {"run_id": "run_1"},
    "heartbeats": {"agent_id": "task_failed"},
    "logs_query": {"agent_id": "task_failed", "severity": "ERROR"},
}


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    warning = info


class Api:
    """`children.ChildApi`, recorded: every call, and a scripted answer."""

    def __init__(self, error: ChildApiError | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def call(self, method: str, path: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        self.calls.append({"method": method, "path": path, "body": body,
                           "headers": dict(headers or {})})
        if self.error is not None:
            raise self.error
        return 200, {"ok": True, "tool": path.split("/reads/")[1].split("?")[0],
                     "result": {"read": True}}


def make_relay(api: Api, key: AttemptKey | None = None) -> StateReads:
    key = key or AttemptKey()
    return StateReads(CFG, sign=key.sign, api=api, logger=Log(), wall=lambda: 1_800_000_000)


def test_the_shims_vocabulary_is_swarm_apis():
    # MUTATION: adding, dropping or renaming a tool or an argument on either side turns this red.
    assert shim.ARGUMENTS == api_reads.ARGUMENTS
    # MUTATION: a required argument that differs between the shim and swarm-api turns this red.
    assert shim.REQUIRED == api_reads.REQUIRED
    # MUTATION: a relay budget that is not swarm-api's 120 an hour turns this red.
    assert relay_mod.CALLS_PER_HOUR == api_reads.CALLS_PER_HOUR == 120
    for tool, names in shim.ARGUMENTS.items():
        # MUTATION: giving any tool a tenant, namespace or filter argument turns this red.
        assert not names & {"tenant_id", "tenant", "namespace", "filter", "resource"}, tool


def test_the_shim_refuses_an_argument_that_names_a_tenant_without_spooling(tmp_path):
    spooled: list[Any] = []
    server = shim.StateServer(tmp_path, submitter=lambda *a, **k: spooled.append(a) or {})
    for extra in ({"tenant_id": "research"}, {"namespace": "swarm-tenant-research"},
                  {"filter": "resource.type=k8s_container"}):
        text, failed = server.call("agents_list", extra)
        # MUTATION: a shim that forwards undeclared arguments turns this red.
        assert failed and json.loads(text)["refused"]["code"] == "request_invalid", extra
    assert spooled == []
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == list(api_reads.TOOLS)


def test_the_relay_sends_the_workers_tuple_never_the_requests():
    api = Api()
    relay = make_relay(api)
    # A request that names a tenant is refused before anything leaves.
    refused = relay.answer("agents_list", {"tenant_id": "research"}, request_id="r1")
    # MUTATION: a relay that copies request arguments into the query unchecked turns this red.
    assert refused["refused"]["code"] == "request_invalid" and api.calls == []
    answer = relay.answer("agents_list", {"state": "FAILED"}, request_id="r2")
    assert answer == {"tool": "agents_list", "result": {"read": True}}
    (call,) = api.calls
    path, _, query = call["path"].partition("?")
    assert path == "/v1/attempts/att_fixer/reads/agents_list"
    # MUTATION: a relay that takes the tenant from anywhere but its own config turns this red.
    assert query.startswith("tenant_id=eng&task_id=task_fixer&lease_id=lease_fixer&generation=3&")
    assert query.count("tenant_id=") == 1


def test_every_tool_is_a_signed_get_under_the_state_purpose():
    key = AttemptKey()
    api = Api()
    relay = make_relay(api, key)
    for tool, args in GOOD.items():
        assert "result" in relay.answer(tool, args, request_id=f"r-{tool.replace('_', '-')}"), tool
    assert len(api.calls) == len(shim.TOOL_NAMES)
    for call in api.calls:
        # MUTATION: any tool sent as POST (or with a body) turns this red: every tool is a read.
        assert call["method"] == "GET" and call["body"] is None
        headers = call["headers"]
        message = api_childkey.state_request_message("GET", call["path"], b"",
                                                     headers[TIMESTAMP_HEADER])
        proof = headers[PROOF_HEADER]
        # MUTATION: signing under `swarm-child-req/v1`, or without the query, turns this red.
        assert api_childkey.verify_proof(key.public, proof, message)


def test_a_stale_generation_ends_the_attempt_through_the_fence(tmp_path):
    api = Api(ChildApiError(409, "attempt_fenced", "task has moved to a newer generation",
                            retryable=False))
    relay = make_relay(api)
    relay.prepare(tmp_path)
    git_shim.write_request(relay.spool(tmp_path), "state-1-abc", "agent_view",
                       {"agent_id": "task_failed"})
    # MUTATION: answering a 409 as an ordinary refusal (no `StateReadsFenced`) turns this red.
    with pytest.raises(StateReadsFenced):
        relay.tick(tmp_path)
    response = json.loads((relay.spool(tmp_path) / "responses" / "state-1-abc.json").read_text())
    assert response["refused"]["code"] == "fenced"


def test_logs_unavailable_is_a_refusal_the_agent_should_not_retry():
    api = Api(ChildApiError(503, "logs_unavailable", "no log view is configured",
                            retryable=True))
    answer = make_relay(api).answer("logs_query", {"agent_id": "task_failed"}, request_id="r1")
    # MUTATION: marking `logs_unavailable` retryable sends the agent into a loop and turns this red.
    assert answer["refused"] == {"code": "logs_unavailable",
                                 "message": "no log view is configured", "retryable": False}


def test_the_relays_budget_spares_a_looping_agent():
    api = Api()
    relay = StateReads(StateReadsConfig(**{**CFG.__dict__, "calls_per_hour": 2}),
                       sign=AttemptKey().sign, api=api, logger=Log(), clock=lambda: 10.0)
    for n in range(2):
        assert "result" in relay.answer("agent_view", {"agent_id": "t"}, request_id=f"r{n}")
    # MUTATION: removing the relay's hourly budget turns this red.
    assert relay.answer("agent_view", {"agent_id": "t"},
                        request_id="r9")["refused"]["code"] == "rate_limited"
    assert len(api.calls) == 2


def test_validate_request_refuses_a_bool_and_a_control_character():
    with pytest.raises(StateReadRefused):
        validate_request("agents_list", {"limit": True})
    with pytest.raises(StateReadRefused):
        validate_request("agent_view", {"agent_id": "task\nx"})
    assert validate_request("agents_list", {"limit": 5}) == ("agents_list", {"limit": "5"})


def test_claude_code_names_swarm_state_exactly_when_the_spool_is_exported(tmp_path):
    spool = tmp_path / "work" / ".swarm-state"
    path = claude_code.write_mcp_config(tmp_path / "cfg", state_spool=str(spool))
    servers = json.loads(Path(path).read_text())["mcpServers"]
    # MUTATION: dropping the `state_spool` branch in `write_mcp_config` turns this red.
    assert servers[shim.SERVER_NAME] == {
        "type": "stdio", "command": sys.executable,
        "args": ["-I", "-m", "agent_worker.statemcp", "--spool", str(spool)]}
    without = claude_code.write_mcp_config(tmp_path / "cfg2", git_spool=str(tmp_path / "g"))
    # MUTATION: naming `swarm-state` for an attempt the worker did not offer it turns this red.
    assert shim.SERVER_NAME not in json.loads(Path(without).read_text())["mcpServers"]
