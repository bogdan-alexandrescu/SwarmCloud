"""The planner's tools as a local MCP server over stdio, for the Claude Code
child of one turn (claudeturn.py; docs/design/voice-it.md §3.6).

Claude Code starts `python -m swarm_voice.mcp_server` from the turn's MCP
config. This process holds NOTHING: no grant, no ID token, no account token.
It relays MCP's `tools/list` and `tools/call` over a unix socket in the turn's
private temporary HOME to swarm-voice's own process (claudeturn.ToolBridge),
which offers exactly the planner's tools -- the catalogue's `draw_<kind>`
tools as served this turn, the read tools (tools_read.py) and `speak` -- and
runs each one through swarm-api with the session grant, as the planner always
has. A tool the bridge does not offer is answered with an error.

THE SOCKET is admitted by the per-turn nonce in this process's environment
(`SWARM_VOICE_PLANNER_NONCE`): the socket file sits in a 0700 directory of
swarm-voice's own uid, and the nonce is checked on every request besides.

THE PROTOCOL is MCP's JSON-RPC 2.0, one message per line on stdin and stdout
(the stdio transport): `initialize`, `notifications/initialized`, `ping`,
`tools/list`, `tools/call`. Nothing else is offered (no resources, no prompts).
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, TextIO

SOCKET_ENV = "SWARM_VOICE_PLANNER_SOCKET"
NONCE_ENV = "SWARM_VOICE_PLANNER_NONCE"
SERVER_NAME = "planner"
PROTOCOL_VERSION = "2025-06-18"
#: One bridge answer: a catalogue, a fenced read result. Far above both.
MAX_LINE = 4 * 1024 * 1024


class Bridge:
    """One connection to swarm-voice's ToolBridge, used one request at a time."""

    def __init__(self, path: str, nonce: str) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(path)
        self._file = self._sock.makefile("rwb")
        self._nonce = nonce

    def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        self._file.write(json.dumps({**request, "nonce": self._nonce}).encode() + b"\n")
        self._file.flush()
        line = self._file.readline(MAX_LINE)
        if not line:
            raise ConnectionError("the planner's tool bridge closed")
        answer = json.loads(line)
        if not isinstance(answer, dict):
            raise ValueError("the planner's tool bridge answered no object")
        return answer


def _reply(out: TextIO, msg_id: Any, *, result: Any = None, error: dict | None = None) -> None:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    out.write(json.dumps(body) + "\n")
    out.flush()


def handle(message: Any, bridge: Bridge) -> tuple[bool, Any, dict | None]:
    """(answers, result, error) for one JSON-RPC message."""
    if not isinstance(message, dict):
        return True, None, {"code": -32600, "message": "invalid request"}
    method = message.get("method")
    if "id" not in message:
        return False, None, None  # a notification: nothing to answer
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return True, {"protocolVersion": asked if isinstance(asked, str) and asked else PROTOCOL_VERSION,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER_NAME, "version": "1"}}, None
    if method == "ping":
        return True, {}, None
    if method == "tools/list":
        answer = bridge.ask({"op": "list"})
        return True, {"tools": answer.get("tools") or []}, None
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        answer = bridge.ask({"op": "call", "name": name if isinstance(name, str) else "", "arguments": args})
        return True, {"content": [{"type": "text", "text": str(answer.get("text") or "")}],
                      "isError": bool(answer.get("is_error"))}, None
    return True, None, {"code": -32601, "message": f"method not offered: {str(method)[:64]}"}


def serve(stdin: TextIO, stdout: TextIO, bridge: Bridge) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _reply(stdout, None, error={"code": -32700, "message": "parse error"})
            continue
        try:
            answers, result, error = handle(message, bridge)
        except (OSError, ValueError) as exc:
            answers, result, error = True, None, {"code": -32603, "message": type(exc).__name__}
        if answers:
            _reply(stdout, message.get("id") if isinstance(message, dict) else None, result=result, error=error)


def main() -> int:
    path = os.environ.get(SOCKET_ENV, "")
    nonce = os.environ.get(NONCE_ENV, "")
    if not path or not nonce:
        print("the planner's MCP server is started by swarm-voice's turn only", file=sys.stderr)
        return 2
    serve(sys.stdin, sys.stdout, Bridge(path, nonce))
    return 0


if __name__ == "__main__":
    sys.exit(main())
