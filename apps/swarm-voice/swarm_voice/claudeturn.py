"""One planner turn as headless Claude Code on a leased subscription account
(docs/design/voice-it.md §3.6; planner.py runs it, plannerlease.py leases it).

THE CHILD. `claude -p` with Claude Opus 5.5, stream-json output, no session
persistence, the planner's system prompt in place of Claude Code's, and a
private temporary HOME (0700, its own cwd and TMPDIR) removed after the turn,
whatever the turn did. Its environment is BUILT, never inherited: PATH, HOME,
TMPDIR, the locale, the leased token in CLAUDE_CODE_OAUTH_TOKEN and the
switches below -- so no ANTHROPIC_API_KEY, no swarm-voice setting and no other
credential of this process reaches it. The subscription token is used only
through Claude Code itself, never sent to the Messages API by swarm-voice.

  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1  no autoupdater, bug reports,
                                              error reporting or telemetry:
                                              the CLI then reaches
                                              api.anthropic.com only, which
                                              swarm-voice's egress admits
  DISABLE_TELEMETRY=1, DISABLE_AUTOUPDATER=1, DISABLE_ERROR_REPORTING=1
                                              the same, said one by one

ITS TOOLS ARE THE PLANNER'S AND NOTHING ELSE. `--tools ""` removes every
built-in tool (no file, shell, web or agent tool exists in the child), the
only MCP server is the one this turn writes (`--strict-mcp-config`), and the
permission rule allows that server's tools alone (`--allowedTools
mcp__planner`, `--permission-mode dontAsk`). The server (mcp_server.py) relays
each call over a unix socket in the private HOME to `ToolBridge` here, which
offers the tools planner.py hands it and runs each one in THIS process, with
the session grant the child never sees.

THE TURN ENDS AT `speak`. Once the planner has said its two sentences the
child is stopped: the voice has its answer, and a further model round would
only cost time and the account's window.

NOTHING SECRET IN WHAT IS CAPTURED: every stdout event is scrubbed of the
token and the grant (prompts.scrub, the worker's redaction rule: each known
value masked wherever it appears) before it is parsed into the transcript;
stderr is scrubbed, kept to its tail, and never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import mcp_server, prompts

log = logging.getLogger("swarm_voice.claudeturn")

MODEL = "claude-opus-5-5"
#: The MCP server's name, so its tools are `mcp__planner__<tool>` to the CLI.
SERVER = mcp_server.SERVER_NAME
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
#: Set in the child so the CLI calls nothing but the model (module docstring).
QUIET_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_ERROR_REPORTING": "1",
}
#: One stream-json event; a tool result is bounded far below it (prompts.RESULT_MAX).
MAX_LINE = 4 * 1024 * 1024
#: The child's stderr kept, scrubbed, for the turn's error.
STDERR_TAIL = 4096
#: Events kept in a turn's transcript.
MAX_EVENTS = 400
#: After SIGTERM, how long the child has before SIGKILL.
STOP_GRACE_SECONDS = 2.0

#: (tool name, arguments) -> (the text the model gets, whether it is an error).
ToolHandler = Callable[[str, dict], Awaitable[tuple[str, bool]]]


def mcp_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """A Messages-API tool definition as MCP lists it."""
    return {"name": str(tool.get("name") or ""), "description": str(tool.get("description") or ""),
            "inputSchema": tool.get("input_schema") or {"type": "object", "properties": {}}}


class ToolBridge:
    """swarm-voice's end of the MCP server's socket: offers `tools` and runs
    each call with `handler`, for one turn."""

    def __init__(self, tools: list[dict[str, Any]], handler: ToolHandler, nonce: str) -> None:
        self._tools = [mcp_tool(t) for t in tools]
        self._names = {t["name"] for t in self._tools}
        self._handler = handler
        self._nonce = nonce
        self._server: asyncio.AbstractServer | None = None
        self.calls: list[str] = []

    async def start(self, path: str) -> None:
        self._server = await asyncio.start_unix_server(self._client, path=path, limit=MAX_LINE)
        os.chmod(path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def answer(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or not hmac.compare_digest(
                str(request.get("nonce") or ""), self._nonce):
            return {"text": "refused: not this turn's bridge", "is_error": True}
        if request.get("op") == "list":
            return {"tools": self._tools}
        if request.get("op") == "call":
            name = str(request.get("name") or "")
            self.calls.append(name)
            if name not in self._names:
                return {"text": f"refused: {name[:64]!r} is not a tool offered this turn", "is_error": True}
            args = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
            text, is_error = await self._handler(name, args)
            return {"text": text, "is_error": is_error}
        return {"text": "refused: unknown operation", "is_error": True}

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                except ValueError:
                    request = None
                writer.write(json.dumps(await self.answer(request)).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


@dataclass
class ChildOutcome:
    """What the child did. Every string in it is scrubbed."""

    exit_code: int | None = None
    #: Token counts over the turn's model calls (the CLI's `usage` names).
    usage: dict[str, int] = field(default_factory=dict)
    result_text: str = ""
    is_error: bool = False
    #: Stopped by the turn once `speak` was answered.
    stopped: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: str = ""


_COUNTS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _counts(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    return {k: v for k in _COUNTS if isinstance(v := usage.get(k), int) and not isinstance(v, bool)}


class ClaudeCode:
    """Starts one turn's `claude -p` child (module docstring)."""

    def __init__(self, *, binary: str | None = None, model: str = MODEL, max_turns: int = 8,
                 python: str | None = None, tmp_root: str | None = None) -> None:
        self.binary = binary or os.environ.get("CLAUDE_CODE_BIN", "").strip() or "claude"
        self.model = model
        self.max_turns = max_turns
        self.python = python or sys.executable
        self.tmp_root = tmp_root

    def args(self, *, system: str, mcp_config: str) -> list[str]:
        return [self.binary, "-p", "--model", self.model, "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--tools", "", "--mcp-config", mcp_config,
                "--strict-mcp-config", "--allowedTools", f"mcp__{SERVER}", "--permission-mode", "dontAsk",
                "--disable-slash-commands", "--max-turns", str(self.max_turns), "--system-prompt", system]

    def env(self, *, home: str, token: str) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home, "TMPDIR": home,
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1", **QUIET_ENV, TOKEN_ENV: token}

    def mcp_config(self, *, socket_path: str, nonce: str) -> dict[str, Any]:
        package_root = str(Path(__file__).resolve().parents[1])
        return {"mcpServers": {SERVER: {
            "type": "stdio", "command": self.python, "args": ["-m", "swarm_voice.mcp_server"],
            "env": {mcp_server.SOCKET_ENV: socket_path, mcp_server.NONCE_ENV: nonce,
                    "PYTHONPATH": package_root, TOKEN_ENV: ""}}}}

    async def run(self, *, prompt: str, system: str, token: str, tools: list[dict[str, Any]],
                  handler: ToolHandler, known: tuple[str, ...], done: asyncio.Event) -> ChildOutcome:
        """Run the child to its end, or until `done` is set; always removes the
        private HOME and never leaves the child running."""
        home = tempfile.mkdtemp(prefix="vp-", dir=self.tmp_root)
        os.chmod(home, 0o700)
        nonce = secrets.token_hex(16)
        bridge = ToolBridge(tools, handler, nonce)
        outcome = ChildOutcome()
        process: asyncio.subprocess.Process | None = None
        try:
            socket_path = os.path.join(home, "tools.sock")
            await bridge.start(socket_path)
            config = os.path.join(home, "mcp.json")
            fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self.mcp_config(socket_path=socket_path, nonce=nonce), fh)
            process = await asyncio.create_subprocess_exec(
                *self.args(system=system, mcp_config=config), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.env(home=home, token=token), cwd=home, start_new_session=True, limit=MAX_LINE)
            assert process.stdin is not None
            with contextlib.suppress(ConnectionError):
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
            process.stdin.close()
            reader = asyncio.create_task(self._read(process, outcome, known))
            errors = asyncio.create_task(self._stderr(process, outcome, known))
            stop = asyncio.create_task(done.wait())
            try:
                await asyncio.wait({reader, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop.done() and not reader.done():
                    outcome.stopped = True
                    await self._stop(process)
                await reader
                await errors
            finally:
                for task in (reader, errors, stop):
                    task.cancel()
            outcome.exit_code = await process.wait()
            return outcome
        finally:
            if process is not None and process.returncode is None:
                await self._stop(process)
            await bridge.close()
            shutil.rmtree(home, ignore_errors=True)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, sig)
            try:
                await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
                return
            except asyncio.TimeoutError:
                continue

    @staticmethod
    async def _read(process: asyncio.subprocess.Process, outcome: ChildOutcome, known: tuple[str, ...]) -> None:
        assert process.stdout is not None
        by_message: dict[str, dict[str, int]] = {}
        final: dict[str, int] | None = None
        while True:
            try:
                raw = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                break
            # Scrubbed BEFORE it is parsed: nothing captured ever holds a token.
            line = prompts.scrub(raw.decode("utf-8", "replace"), known).strip()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if len(outcome.events) < MAX_EVENTS:
                outcome.events.append(event)
            kind = event.get("type")
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if kind == "assistant" and message:
                by_message[str(message.get("id") or len(by_message))] = _counts(message.get("usage"))
            elif kind == "result":
                outcome.is_error = bool(event.get("is_error"))
                text = event.get("result")
                outcome.result_text = text if isinstance(text, str) else ""
                final = _counts(event.get("usage")) or None
        totals: dict[str, int] = {}
        for counts in ([final] if final else by_message.values()):
            for name, value in counts.items():
                totals[name] = totals.get(name, 0) + value
        outcome.usage = totals

    @staticmethod
    async def _stderr(process: asyncio.subprocess.Process, outcome: ChildOutcome, known: tuple[str, ...]) -> None:
        assert process.stderr is not None
        tail = b""
        while chunk := await process.stderr.read(65536):
            tail = (tail + chunk)[-STDERR_TAIL * 2:]
        outcome.stderr_tail = prompts.scrub(tail.decode("utf-8", "replace"), known)[-STDERR_TAIL:]
