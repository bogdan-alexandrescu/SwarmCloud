"""`swarm-jev`: the agent's `jev_decide(state, questions)` MCP tool.

OWNER OVERRIDE of context-quality.md §5.1 (2026-10-14): besides the worker's
fixed-point calls, the agent may ask Jev mid-task. Every call goes through
the same `JudgeClient` as the worker's -- §5.4's redaction, §5.5's retries
and breaker, and the one per-task ledger -- under the kind `agent`.

Transport: newline-delimited JSON-RPC 2.0 on stdio, the four methods
`agent_worker.graphmcp.server` answers. A call Jev could not answer is a tool
RESULT with `isError: true` and the reason, so the agent carries on with its
own judgement; only a malformed request is a protocol error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Any, Callable, Mapping

from agent_worker import __version__
from agent_worker.judge import budget, client
from agent_worker.judge.settings import Backend

SERVER_NAME = "swarm-jev"
TOOL_NAME = "jev_decide"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Jev answers typed questions about a state you pass; it never writes text. "
                "Advisory only, shared budget of 8 calls per task.")
ADVISORY = "Jev is advisory: no answer this time; continue with your own judgement."

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Ask Jev, a fast judgement model, typed questions about `state`. Each question is "
        "{type, instructions, criteria}: `noul` answers p(yes); `choice` picks one option and "
        "needs criteria as {option: description-or-null} (at most 255); `score` needs criteria "
        "as a list of strings. Refer to parts of state by name in backticks. Credential-shaped "
        "text and secret files are removed before sending. At most 8 calls and 120k input "
        "tokens per task, shared with the platform's own calls. Advisory: weigh the answer, "
        "do not obey it."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "state": {"description": "what the questions are about: an object, or a string",
                      "type": ["object", "array", "string"]},
            "questions": {"type": "object",
                          "description": "question name -> {type, instructions, criteria}",
                          "additionalProperties": {"type": "object"}},
        },
        "required": ["state", "questions"],
        "additionalProperties": False,
    },
}

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PARSE_ERROR = -32700


class ConfigError(RuntimeError):
    pass


class JevServer:
    def __init__(self, judge: client.JudgeClient) -> None:
        self.client = judge

    @classmethod
    def from_config(cls, path: Path, *, transport: client.Transport = client.urllib_transport,
                    sleep: Callable[[float], None] | None = None,
                    environ: Mapping[str, str] | None = None) -> "JevServer":
        """From the config `register.prepare` wrote. Refuses one it cannot read."""
        try:
            config = json.loads(Path(path).read_text())
            spec = config["backend"]
            key_file = config.get("key_file")
            key = Path(key_file).read_text().strip() if key_file else None
            backend = Backend(str(spec["name"]), str(spec["url"]), str(spec["model"]),
                              stringify_state=bool(spec["stringify_state"]), key=key or None)
            ledger = budget.Ledger(Path(config["ledger"]))
            stops = tuple(p for p in config.get("hard_stop_paths") or [] if isinstance(p, str))
            names = [n for n in config.get("secret_env_names") or [] if isinstance(n, str)]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"cannot read the Jev config ({type(exc).__name__})") from None
        env = os.environ if environ is None else environ
        secrets = tuple(env[n] for n in names if env.get(n))
        extra: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
        return cls(client.JudgeClient(backend, ledger, transport=transport,
                                      hard_stop_paths=stops, secrets=secrets, **extra))

    def _call(self, arguments: Any) -> tuple[dict[str, Any], bool]:
        if not isinstance(arguments, dict) or "state" not in arguments \
                or "questions" not in arguments:
            return {"error": "jev_decide takes {state, questions}"}, True
        try:
            decision = self.client.decide("agent", arguments["state"], arguments["questions"])
        except client.QuestionError as exc:
            return {"error": str(exc)}, True
        removed = {"dropped_strings": decision.dropped, "denied_paths": decision.denied}
        if decision.ok:
            return {"answers": decision.answers, "input_tokens": decision.input_tokens,
                    "removed": removed}, False
        return {"outcome": decision.outcome, "reason": decision.reason, "note": ADVISORY,
                "removed": removed}, True

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
                or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        if "id" not in message:
            return None
        ident, method = message["id"], message["method"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(ident, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(ident, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": [TOOL]})
        if method == "tools/call":
            if params.get("name") != TOOL_NAME:
                return _error(ident, INVALID_PARAMS, f"no tool {params.get('name')!r}")
            payload, failed = self._call(params.get("arguments"))
            return _result(ident, {"content": [{"type": "text",
                                                "text": json.dumps(payload, sort_keys=True)}],
                                   "isError": failed})
        return _error(ident, METHOD_NOT_FOUND, f"no method {method!r}")

    def serve(self, stdin: IO[bytes], stdout: IO[bytes]) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response: dict | None = _error(None, PARSE_ERROR, "not JSON")
            else:
                try:
                    response = self.handle(message)
                except Exception as exc:  # one bad question must not end the session
                    print(f"swarm-jev: {type(exc).__name__} answering a request", file=sys.stderr)
                    ident = message.get("id") if isinstance(message, dict) else None
                    response = _error(ident, -32603, f"internal error: {type(exc).__name__}")
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=True).encode("ascii") + b"\n")
                stdout.flush()


def _result(ident: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
