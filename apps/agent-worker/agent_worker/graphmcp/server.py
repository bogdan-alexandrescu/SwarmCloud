"""The stdio transport: newline-delimited JSON-RPC 2.0, the four MCP methods it needs.

`initialize`, `tools/list`, `tools/call` and `ping`; notifications (no `id`)
are read and not answered, an unknown method is JSON-RPC's -32601. Nothing
but answers goes to stdout -- one JSON object per line, as the transport
requires -- and the server's own log lines go to stderr, never with an
argument the agent sent.

A tool that cannot answer (an unknown symbol, a bad argument, a blob that
does not match its name) is a tool RESULT with `isError: true` and the
freshness block, not a protocol error: the agent reads it and asks again.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any

from agent_worker import __version__
from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Freshness
from agent_worker.graphmcp.snapshot import Snapshot

SERVER_NAME = "swarm-graph"
#: The MCP revisions this server speaks; a client asking for another is
#: answered with the newest, as the protocol's version negotiation says.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
#: Shown to the model once; counted in the schema budget with the tools.
INSTRUCTIONS = ("Read-only code graph of this repository at the indexed commit. Every "
                "answer starts with freshness; rows marked stale are on paths changed "
                "since the index. Answers page at 4 KiB: pass next_cursor as cursor.")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class GraphServer:
    def __init__(self, snapshot: Snapshot, freshness: Freshness) -> None:
        self.snapshot = snapshot
        self.freshness = freshness

    def handle(self, message: Any) -> dict | None:
        """The response to one message, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
                or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        if "id" not in message:
            return None
        ident = message["id"]
        method = message["method"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(ident, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(ident, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": tools.TOOLS})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return _error(ident, INVALID_PARAMS, "tools/call needs a tool name")
            text, failed = tools.call(self.snapshot, self.freshness.observe(), name,
                                      params.get("arguments"))
            return _result(ident, {"content": [{"type": "text", "text": text}],
                                   "isError": failed})
        return _error(ident, METHOD_NOT_FOUND, f"no method {method!r}")

    def serve(self, stdin: IO[bytes], stdout: IO[bytes]) -> None:
        """Answer line by line until stdin closes."""
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
                    print(f"swarm-graph: {type(exc).__name__} answering a request",
                          file=sys.stderr)
                    ident = message.get("id") if isinstance(message, dict) else None
                    response = _error(ident, -32603, f"internal error: {type(exc).__name__}")
            if response is not None:
                stdout.write(tools.dumps(response).encode("ascii") + b"\n")
                stdout.flush()


def _result(ident: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
