#!/usr/bin/env python3
"""The acceptance group's echo MCP server (lane MCP-ACCEPT, part of #1433).

docs/design/registered-mcp-servers.md §6, row MCP-ACCEPT, and owner decision
Q10 (2026-10-17): a dev-only Cloud Run service that scales to zero, reached
over public HTTPS through a task's relay exactly as a caller's own server
would be (terraform/infra/mcp_test_server.tf). An operator registers it in the
smoke tenant as a `dispatch` server; scripts/acceptance/groups/mcp.sh then
dispatches a claude-code task that names it with a per-run credential.

WHAT IT ANSWERS. MCP streamable HTTP (Q5), stateless, JSON answers only:
`initialize`, `ping`, `tools/list` and `tools/call` of its one tool, `whoami`.
`whoami` reports what the server received on that call:

  credential_sha256   sha256 of the bearer value the relay attached. The agent
                      never holds the credential, so it cannot produce this:
                      a match with the value the suite minted is the proof
                      that the server saw it (the issue's first check);
  jwt_claims_sha256   when the bearer is a compact JWT (the acceptance group
                      sends a Google ID token, which is what Cloud Run's IAM
                      check admits), the sha256 of its claims segment. That
                      segment is the same whether or not Cloud Run hands the
                      container the token with its signature, so the judge
                      can match it either way; null for any other bearer;
  credential_echo     the bearer value itself, ON PURPOSE: a server that
                      echoes a credential is the case the relay scrubs
                      (§4.8, "Answers"), so the agent must receive a mask
                      here, and the leak search over every log, artifact,
                      checkpoint and event must not find the value;
  task, attempt       the relay's X-Swarm-Task / X-Swarm-Attempt, so the
                      judge knows which attempt's relay made the call;
  nonce               the argument the agent passed, bounded.

WHAT IT HOLDS: nothing. No store, no identity with a grant, no secret. It is
not public (owner decision 2026-10-18): Cloud Run admits only the acceptance
identity's ID token and refuses anything else 403 before this code runs. A
request that does reach it without a bearer is 401 -- never 403, so the
judge can tell the two refusals apart. It never logs a header, a body
or an argument: one line per request, the JSON-RPC method, the tool and the
status (Cloud Logging is a log route too).

STANDARD LIBRARY ONLY: it runs from the swarm-verify image (alpine, python3,
no pip), which carries it for this reason.

    server.py                 serve on 0.0.0.0:$PORT (8080 when unset)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SERVER_NAME = "swarm-acceptance-echo"
SERVER_VERSION = "1"
#: The only path it serves MCP on. A registration's URL ends with it.
MCP_PATH = "/mcp"
TOOL = "whoami"
#: Newest first. An `initialize` asking for one of these gets it back; any
#: other gets the newest, and the client decides (the MCP spec's negotiation).
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
#: A whoami call is a few hundred bytes; this is not an upload path.
MAX_BODY_BYTES = 64 * 1024
#: §4.3's bounds on a per-run credential, so a header the relay could not
#: have sent is refused rather than hashed.
MIN_CREDENTIAL, MAX_CREDENTIAL = 8, 8192
NONCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
#: The relay's own headers (apps/agent-worker/agent_worker/mcp_relay.py
#: TASK_HEADER, ATTEMPT_HEADER). Ids, not secrets.
TASK_HEADER = "X-Swarm-Task"
ATTEMPT_HEADER = "X-Swarm-Attempt"
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

WHOAMI = {
    "name": TOOL,
    "description": ("Reports what this echo server received on this call: the sha256 of the "
                    "bearer credential, the credential itself, the relay's task and attempt "
                    "ids, and the nonce you pass."),
    "inputSchema": {
        "type": "object",
        "properties": {"nonce": {"type": "string", "description": "Any short marker; echoed."}},
        "additionalProperties": False,
    },
}


def credential_sha256(value: str) -> str:
    """What `whoami` reports, and what the judge recomputes from the minted value."""
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def jwt_claims_sha256(value: str) -> str | None:
    """sha256 of a compact JWT's claims segment, or None for any other value."""
    parts = value.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    return hashlib.sha256(parts[1].encode("ascii")).hexdigest()


def bearer(header: str | None) -> str | None:
    """The bearer value of one Authorization header, or None when it is not one."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not (MIN_CREDENTIAL <= len(value) <= MAX_CREDENTIAL):
        return None
    if not all(33 <= ord(c) <= 126 for c in value):
        return None
    return value


def _header_id(value: str | None) -> str | None:
    return value if value is not None and _ID.match(value) else None


def whoami(credential: str, arguments: Any, headers: dict[str, str | None]) -> dict[str, Any]:
    nonce = arguments.get("nonce") if isinstance(arguments, dict) else None
    return {
        "server": SERVER_NAME,
        "credential_sha256": credential_sha256(credential),
        "jwt_claims_sha256": jwt_claims_sha256(credential),
        "credential_echo": credential,
        "task": _header_id(headers.get(TASK_HEADER)),
        "attempt": _header_id(headers.get(ATTEMPT_HEADER)),
        "nonce": nonce if isinstance(nonce, str) and NONCE.match(nonce) else None,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def answer(message: Any, credential: str, headers: dict[str, str | None]) -> dict[str, Any] | None:
    """One JSON-RPC message's answer; None for a notification or a response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "not a JSON-RPC 2.0 message")
    if "id" not in message or "method" not in message:
        return None
    msg_id, method = message["id"], message["method"]
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Call whoami to see what this server received."}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [WHOAMI]}}
    if method == "tools/call":
        if params.get("name") != TOOL:
            return _error(msg_id, -32602, f"unknown tool; the one tool is {TOOL}")
        report = whoami(credential, params.get("arguments"), headers)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": json.dumps(report, sort_keys=True)}],
            "isError": False}}
    return _error(msg_id, -32601, "method not found")


def _tool_of(message: Any) -> str | None:
    if isinstance(message, dict) and message.get("method") == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        return name if isinstance(name, str) and len(name) <= 64 else "?"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = SERVER_NAME
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the base's name
        # The base class logs the request line; this server logs its own one
        # line per request (`_log`) and nothing that could carry a header.
        return

    def _log(self, status: int, methods: list[str], tools: list[str]) -> None:
        print(json.dumps({"severity": "INFO", "message": "mcp_echo", "path": self.path[:64],
                          "http_method": self.command, "status": status,
                          "rpc_methods": methods[:8], "tools": tools[:8]}), flush=True)

    def _send(self, status: int, payload: Any = None, *, allow: str | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        if allow is not None:
            self.send_header("Allow", allow)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- http.server's spelling
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path.split("?", 1)[0] == MCP_PATH:
            # No server-to-client stream: the spec lets a server answer 405.
            self._send(405, {"error": "this server opens no SSE stream"}, allow="POST")
        else:
            self._send(404, {"error": "not found"})
        self._log(self._status, [], [])

    def do_DELETE(self) -> None:  # noqa: N802
        self._send(405 if self.path.split("?", 1)[0] == MCP_PATH else 404,
                   {"error": "stateless: there is no session to end"}, allow="POST")
        self._log(self._status, [], [])

    def send_response(self, code: int, message: str | None = None) -> None:
        self._status = code
        super().send_response(code, message)

    def do_POST(self) -> None:  # noqa: N802
        methods: list[str] = []
        tools: list[str] = []
        try:
            self._post(methods, tools)
        finally:
            self._log(getattr(self, "_status", 0), methods, tools)

    def _post(self, methods: list[str], tools: list[str]) -> None:
        if self.path.split("?", 1)[0] != MCP_PATH:
            self._send(404, {"error": "not found"})
            return
        credential = bearer(self.headers.get("Authorization"))
        if credential is None:
            self._send(401, {"error": "a bearer credential is required"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0:
            self._send(411, {"error": "Content-Length is required"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": f"at most {MAX_BODY_BYTES} bytes"})
            return
        try:
            parsed = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, UnicodeDecodeError):
            self._send(400, _error(None, -32700, "parse error"))
            return
        batch = isinstance(parsed, list)
        messages = parsed if batch else [parsed]
        if not messages:
            self._send(400, _error(None, -32600, "an empty batch"))
            return
        headers = {TASK_HEADER: self.headers.get(TASK_HEADER),
                   ATTEMPT_HEADER: self.headers.get(ATTEMPT_HEADER)}
        answers = []
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("method"), str):
                methods.append(message["method"][:64])
            tool = _tool_of(message)
            if tool is not None:
                tools.append(tool)
            reply = answer(message, credential, headers)
            if reply is not None:
                answers.append(reply)
        if not answers:
            # Notifications and responses only: accepted, nothing to say.
            self._send(202)
            return
        self._send(200, answers if batch else answers[0])


def serve(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    """The server, bound and not yet serving (the judge's selftest binds port 0)."""
    chosen = int(os.environ.get("PORT", "8080")) if port is None else port
    httpd = ThreadingHTTPServer((host, chosen), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    httpd = serve()
    print(json.dumps({"severity": "INFO", "message": "mcp_echo listening",
                      "port": httpd.server_address[1]}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
