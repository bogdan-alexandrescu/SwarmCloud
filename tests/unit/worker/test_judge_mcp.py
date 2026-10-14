"""`jev_decide`, the agent's own Jev tool (owner override of context-quality.md §5.1).

Same budget, same redaction, same breaker as the worker's calls, counted
against the one per-task ledger; registered only when the workspace opted in
to Jev for code, and with no credential at all on the Internal backends.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_worker.judge import budget, mcp, register, settings

INTERNAL = {settings.INTERNAL_URL_ENV: "http://decide.internal:8080/v1/decide"}


def _register(tmp_path: Path, given: settings.JudgeSettings, *, key: str | None = None,
              servers: dict | None = None, **extra: Any) -> tuple[register.Registered, dict]:
    servers = {} if servers is None else servers
    done = register.register_jev_tool(servers, attempt_dir=tmp_path, settings=given, key=key,
                                      environ=INTERNAL, **extra)
    return done, servers


def test_not_registered_unless_the_workspace_opted_in(tmp_path: Path) -> None:
    done, servers = _register(tmp_path, settings.JudgeSettings())
    assert not done.registered and done.reason == "workspace_not_opted_in"
    assert servers == {}
    assert not (tmp_path / register.JUDGE_DIR).exists()


def test_not_registered_when_the_repository_turns_it_off(tmp_path: Path) -> None:
    done, servers = _register(tmp_path, settings.JudgeSettings(code_opt_in=True,
                                                               repository_override="off"))
    assert not done.registered and servers == {}


def test_internal_jev_registers_with_no_credential_anywhere(tmp_path: Path) -> None:
    done, servers = _register(tmp_path, settings.JudgeSettings(code_opt_in=True),
                              secret_env_names=("PROVIDER_KEY",), hard_stop_paths=("infra/prod/",))
    assert done.registered
    entry = servers[mcp.SERVER_NAME]
    assert entry["type"] == "stdio" and entry["args"][-2:] == ["--config", str(done.config_path)]
    assert "env" not in entry
    config = json.loads(done.config_path.read_text())
    assert config["backend"]["name"] == "internal-jev"
    assert config["key_file"] is None
    assert config["secret_env_names"] == ["PROVIDER_KEY"]
    assert config["hard_stop_paths"] == ["infra/prod/"]
    assert stat.S_IMODE(os.stat(done.config_path).st_mode) == 0o600
    assert not any(p.name == register.KEY_FILE for p in tmp_path.rglob("*"))


def test_typesafe_writes_its_key_0600_beside_the_config_never_in_it(tmp_path: Path) -> None:
    key = "k-" + "q" * 30
    done, servers = _register(tmp_path, settings.JudgeSettings(code_opt_in=True,
                                                               backend="typesafe"), key=key)
    assert done.registered
    assert key not in done.config_path.read_text()
    assert key not in json.dumps(servers)
    key_file = Path(json.loads(done.config_path.read_text())["key_file"])
    assert key_file.read_text() == key
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600


def test_typesafe_without_a_key_is_not_registered(tmp_path: Path) -> None:
    done, servers = _register(tmp_path, settings.JudgeSettings(code_opt_in=True,
                                                               backend="typesafe"))
    assert not done.registered and done.reason == "no_key" and servers == {}


def test_the_worker_and_the_tool_share_one_ledger(tmp_path: Path) -> None:
    done, _ = _register(tmp_path, settings.JudgeSettings(code_opt_in=True,
                                                         monthly_cap_tokens=50_000),
                        month_used_tokens=1_000)
    worker_side = register.worker_client(done, transport=lambda *a: (500, b""))
    assert worker_side.ledger.path == done.ledger_path
    state = budget.Ledger(done.ledger_path).read()
    assert state["max_calls"] == 8 and state["max_input_tokens"] == 120_000
    assert state["monthly_cap_tokens"] == 50_000 and state["month_used_tokens"] == 1_000


# -- the server ---------------------------------------------------------------


class Jev:
    def __init__(self, body: Any) -> None:
        self.body = body
        self.sent: list[dict] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float):
        self.sent.append(json.loads(body))
        if isinstance(self.body, int):
            return self.body, b"{}"
        return 200, json.dumps(self.body).encode()


def _server(tmp_path: Path, jev: Jev, environ: dict | None = None) -> mcp.JevServer:
    done, _ = _register(tmp_path, settings.JudgeSettings(code_opt_in=True),
                        secret_env_names=("PROVIDER_KEY",))
    return mcp.JevServer.from_config(done.config_path, transport=jev, sleep=lambda s: None,
                                     environ=environ or {})


def _call(server: mcp.JevServer, arguments: Any) -> tuple[dict, bool]:
    reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "jev_decide", "arguments": arguments}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_tools_list_names_jev_decide_only(tmp_path: Path) -> None:
    server = _server(tmp_path, Jev({}))
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    (tool,) = reply["result"]["tools"]
    assert tool["name"] == "jev_decide"
    assert set(tool["inputSchema"]["required"]) == {"state", "questions"}
    init = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == mcp.SERVER_NAME


def test_jev_decide_answers_and_counts_against_the_task(tmp_path: Path) -> None:
    jev = Jev({"answers": {"q": {"noul": 0.7}}, "usage": {"input_tokens": 900}})
    server = _server(tmp_path, jev)
    answer, failed = _call(server, {"state": {"diff": "x"},
                                    "questions": {"q": {"type": "noul", "instructions": "ok?"}}})
    assert not failed and answer["answers"] == {"q": {"noul": 0.7}}
    ledger = server.client.ledger.read()
    assert ledger["calls"] == 1 and ledger["audit"][0]["kind"] == "agent"
    assert isinstance(jev.sent[0]["state"], str)  # Internal Jev: state is a string


def test_jev_decide_scrubs_the_tool_process_own_secrets(tmp_path: Path) -> None:
    secret = "pv-" + "m4" * 12
    jev = Jev({"answers": {"q": {"noul": 0.7}}, "usage": {"input_tokens": 1}})
    server = _server(tmp_path, jev, environ={"PROVIDER_KEY": secret})
    _call(server, {"state": {"note": f"value {secret}", "keep": "this"},
                   "questions": {"q": {"type": "noul", "instructions": "ok?"}}})
    assert secret not in json.dumps(jev.sent)
    assert json.loads(jev.sent[0]["state"]) == {"keep": "this"}


def test_a_question_holding_a_credential_is_refused_not_masked(tmp_path: Path) -> None:
    jev = Jev({"answers": {}})
    server = _server(tmp_path, jev)
    token = "ghp_" + "Q1w2E3r4T5" * 4
    answer, failed = _call(server, {"state": "s", "questions": {
        "q": {"type": "noul", "instructions": f"is {token} valid?"}}})
    assert failed and jev.sent == []
    assert token not in json.dumps(answer)


def test_jev_decide_falls_back_without_failing_the_session(tmp_path: Path) -> None:
    server = _server(tmp_path, Jev(503))
    answer, failed = _call(server, {"state": "s", "questions": {
        "q": {"type": "noul", "instructions": "ok?"}}})
    assert failed and answer["outcome"] == "failed"
    assert "advisory" in answer["note"]


def test_a_bad_argument_is_a_tool_error_not_a_crash(tmp_path: Path) -> None:
    server = _server(tmp_path, Jev({}))
    answer, failed = _call(server, {"state": "s",
                                    "questions": {"c": {"type": "choice", "instructions": "w"}}})
    assert failed and "criteria" in answer["error"]
    answer, failed = _call(server, "not an object")
    assert failed


def test_serve_answers_line_by_line(tmp_path: Path) -> None:
    server = _server(tmp_path, Jev({}))
    stdin = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
                       b'{"jsonrpc":"2.0","method":"notifications/initialized"}\nnot json\n')
    stdout = io.BytesIO()
    server.serve(stdin, stdout)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert lines[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert lines[1]["error"]["code"] == -32700 and len(lines) == 2


def test_main_refuses_a_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from agent_worker.judge.__main__ import main
    assert main(["--config", str(tmp_path / "nope.json")]) == 2
