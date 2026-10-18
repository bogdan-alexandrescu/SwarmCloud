"""The `swarm-git` shim: the tool schemas, the spool protocol, and the stdio loop.

THE SPOOL (docs/runner-git-tools.md §1.3), the one channel between the agent
and the worker for git tools:

    $SWARM_GIT = work/.swarm-git/
      requests/<request_id>.json   {"tool": <name>, "arguments": {...}}  (write .tmp, rename)
      responses/<request_id>.json  the worker's: {"tool", "result": {...}} or {"refused": {...}}

`agent_worker.gittools` imports every constant below, so the worker and the
shim cannot disagree about a tool or an argument.

THE TRANSPORT is graphmcp's: newline-delimited JSON-RPC 2.0, `initialize`,
`tools/list`, `tools/call` and `ping`. A refused call is a tool RESULT with
`isError: true` carrying the refusal's code, so the agent reads it and acts;
nothing but answers goes to stdout.

WAITING HERE IS THE AGENT'S, NOT THE WORKER'S. The shim polls for its answer
in the agent's own process; the worker answers on its control poll and never
waits on the agent (invariant 4 is about the worker).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import IO, Any, Callable

from agent_worker import __version__

SERVER_NAME = "swarm-git"
#: The spool's directory under `work/`, and the variable naming it to the agent.
SPOOL_DIR_NAME = ".swarm-git"
SPOOL_ENV = "SWARM_GIT"
#: A request id is a file name: the child spool's shape (`children.REQUEST_ID`).
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
#: The largest request file the worker reads. The arguments are a few names.
MAX_REQUEST_BYTES = 16 * 1024
#: How long the shim waits for the worker's answer. A push of a large bundle
#: is the slow case; the worker answers on its next control poll.
WAIT_SECONDS = 900.0
POLL_SECONDS = 0.5

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Git on the task's own repository through the platform; this container "
                "holds no token. Push only commits, never forced. A refusal's code says why.")

_REF = {"type": "string", "description": "branch, tag, or 40-hex sha"}

TOOLS: list[dict[str, Any]] = [
    {"name": "git_fetch",
     "description": "Fetch ref from the task's repository into origin/<ref> "
                    "(a sha into refs/swarm/fetched/<sha>). Returns the sha.",
     "inputSchema": {"type": "object", "required": ["ref"], "properties": {"ref": _REF}}},
    {"name": "git_rebase_onto",
     "description": "git_fetch ref, then rebase HEAD onto it. A conflict leaves the "
                    "rebase in progress and lists the paths: fix, then git rebase --continue.",
     "inputSchema": {"type": "object", "required": ["ref"], "properties": {"ref": _REF}}},
    {"name": "git_push_branch",
     "description": "Push HEAD's commits to this task's branch (default: its own). "
                    "Scanned for credentials first; never forced.",
     "inputSchema": {"type": "object", "properties": {
         "branch": {"type": "string", "description": "a branch this task may push"}}}},
    {"name": "read_private",
     "description": "Read a file (max 1 MiB) or list a directory in an allowlisted "
                    "repository at a ref.",
     "inputSchema": {"type": "object", "required": ["repo", "path", "ref"], "properties": {
         "repo": {"type": "string", "description": "owner/name"},
         "path": {"type": "string", "description": "repo-relative; empty for the root"},
         "ref": _REF}}},
    {"name": "pr_checks",
     "description": "CI state of a pull request in the task's repository (default: "
                    "this task's), with red rows' log tails.",
     "inputSchema": {"type": "object", "properties": {
         "pr": {"type": "integer", "minimum": 1}}}},
]
TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
#: Every argument each tool accepts, and the ones it requires: the spool's whole
#: vocabulary. Anything else in a request is refused by the worker.
ARGUMENTS: dict[str, frozenset[str]] = {
    tool["name"]: frozenset(tool["inputSchema"]["properties"]) for tool in TOOLS
}
REQUIRED: dict[str, frozenset[str]] = {
    tool["name"]: frozenset(tool["inputSchema"].get("required", ())) for tool in TOOLS
}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def new_request_id() -> str:
    return f"git-{int(time.time())}-{secrets.token_hex(6)}"


def write_request(spool: Path, request_id: str, tool: str, arguments: dict[str, Any]) -> Path:
    """Spool one request atomically: `.tmp`, then a rename the worker sees whole."""
    requests = spool / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    final = requests / f"{request_id}.json"
    tmp = requests / f".{request_id}.json.tmp"
    tmp.write_text(json.dumps({"tool": tool, "arguments": arguments}), encoding="utf-8")
    os.replace(tmp, final)
    return final


def submit(
    spool: Path,
    tool: str,
    arguments: dict[str, Any],
    *,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Write a request and wait for the worker's answer; the answer, as written."""
    request_id = new_request_id()
    write_request(spool, request_id, tool, arguments)
    response = spool / "responses" / f"{request_id}.json"
    deadline = clock() + wait_seconds
    while True:
        if response.is_file():
            try:
                answer = json.loads(response.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                answer = None
            if isinstance(answer, dict):
                response.unlink(missing_ok=True)
                return answer
        if clock() >= deadline:
            return {"refused": {
                "code": "worker_did_not_answer",
                "message": f"no answer within {int(wait_seconds)}s; request {request_id} is "
                           "still queued and its answer will be in the spool's responses/",
                "retryable": True}}
        sleep(poll_seconds)


class GitServer:
    """The stdio loop over one spool. The class attributes are the server's
    whole vocabulary, so another spool-backed shim (`agent_worker.searchmcp`)
    is this loop with its own name and tools, never a second copy of it."""

    name: str = SERVER_NAME
    instructions: str = INSTRUCTIONS
    tools: list[dict[str, Any]] = TOOLS
    arguments: dict[str, frozenset[str]] = ARGUMENTS
    required: dict[str, frozenset[str]] = REQUIRED

    def __init__(self, spool: Path, *, submitter: Callable[..., dict[str, Any]] = submit) -> None:
        self.spool = Path(spool)
        self._submit = submitter

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
                "serverInfo": {"name": self.name, "version": __version__},
                "instructions": self.instructions,
            })
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": self.tools})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return _error(ident, INVALID_PARAMS, "tools/call needs a tool name")
            text, failed = self.call(name, params.get("arguments"))
            return _result(ident, {"content": [{"type": "text", "text": text}],
                                   "isError": failed})
        return _error(ident, METHOD_NOT_FOUND, f"no method {method!r}")

    def call(self, name: str, arguments: Any) -> tuple[str, bool]:
        """One tool call through the spool: the answer's text, and whether it failed.

        Only the shape is checked here, for a fast answer; the worker checks
        everything again, because the spool is writable without this shim."""
        if name not in self.arguments:
            return _dumps({"refused": {"code": "unknown_tool",
                                       "message": f"tools are {', '.join(self.arguments)}"}}), True
        args = arguments if arguments is not None else {}
        if not isinstance(args, dict):
            return _dumps({"refused": {"code": "request_invalid",
                                       "message": "arguments must be an object"}}), True
        unknown = sorted(set(args) - self.arguments[name])
        missing = sorted(self.required[name] - set(args))
        if unknown or missing:
            return _dumps({"refused": {
                "code": "request_invalid",
                "message": f"{name} takes {sorted(self.arguments[name])}; unknown {unknown}, "
                           f"missing {missing}"}}), True
        answer = self._submit(self.spool, name, args)
        return _dumps(answer), "refused" in answer

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
                except Exception as exc:  # one bad call must not end the session
                    print(f"{self.name}: {type(exc).__name__} answering a request", file=sys.stderr)
                    ident = message.get("id") if isinstance(message, dict) else None
                    response = _error(ident, -32603, f"internal error: {type(exc).__name__}")
            if response is not None:
                stdout.write(_dumps(response).encode("ascii") + b"\n")
                stdout.flush()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _result(ident: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
