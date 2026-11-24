"""One planner turn as headless Claude Code on a leased subscription account
(docs/design/voice-it.md §3.6; planner.py runs it, plannerlease.py leases it).

THE CHILD. `claude -p` with the planner's pin, stream-json output, no session
persistence, the planner's system prompt in place of Claude Code's, and a
private temporary HOME (0700, its own cwd and TMPDIR) removed after the turn,
whatever the turn did. Its environment is BUILT, never inherited: PATH, HOME,
TMPDIR, the locale, the leased token in CLAUDE_CODE_OAUTH_TOKEN and the
switches below -- so no swarm-voice setting and no other credential of this
process reaches it. The subscription token is used only through Claude Code
itself, never sent to the Messages API by swarm-voice.

ON A KEY (docs/design/llm-providers.md §5.3). When the planner's lease lands
on an Anthropic developer key (plannerlease.py hands its token over as an
`ApiKey`), the child is given that key in ANTHROPIC_API_KEY in place of
CLAUDE_CODE_OAUTH_TOKEN: one credential variable, never both. A turn that ends
on a spent account says which (`ChildOutcome.spent`: the key's credit gone, or
rate-limited), read from the CLI's scrubbed result.

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

THE TWO PINS (docs/design/voice-it-own-speech.md §14.1). The planner runs the
planning class, `VOICE_PLANNER_MODEL` (Terraform's `local.runner_models.fable`),
on both engines; own speech's fast front runs the haiku class,
`VOICE_FRONT_MODEL` (`local.runner_models.haiku`), picked by first-reply
latency. Both are set on the pod by voice.tf; the defaults below are the same
pins, for a process started without them. A session may plan on another
class's pin: Voice it › Settings' `planner_model`, which swarm-api reads at the
session's start and sends in its start body (`admitted_model` checks it,
`ClaudeCode.with_model` runs it); a session started without one keeps the pin.

ONE WARM CHILD PER SESSION (`ClaudeCode.converse`, the front's): the same
rules -- built environment, `--tools ""`, `--strict-mcp-config` with only the
tools it is handed, private HOME -- with `--input-format stream-json`, so each
sentence is one user message on the running child's stdin and none pays
Claude Code's start. `Conversation.close` stops it and removes its HOME. It
also runs with `--include-partial-messages`, so `Conversation.say(on_text=)`
hands out the turn's text as it is written (`feed_text`): the front speaks its
first sentence before its turn ends. Each delta line is scrubbed as every line
is; claudefront re-scrubs the joined text, so a token cut across two deltas
is still masked.

THE PLANNER'S WARM CHILD is the same `Conversation` (plannerwarm.py), with a
turn that still ends at `speak`: `say(done=, closes=)` waits for `done`, then
for the CLI to take the closing tool's result, and sends the CLI's `interrupt`
control request, so no further model round runs, the child stays up and its
`result` carries that message's own usage. A child that does not settle within
STOP_GRACE_SECONDS is `spoiled`: the turn keeps its answer, and `alive` is
false from then on. `sign_seconds` bounds the wait for a reused child's first
event.

NOTHING SECRET IN WHAT IS CAPTURED: every stdout event is scrubbed of the
token and the grant (prompts.scrub, the worker's redaction rule: each known
value masked wherever it appears) before it is parsed into the transcript;
stderr is scrubbed, kept to its tail, and never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
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

from swarm_common.profiles import MODEL_CLASSES

from . import mcp_server, prompts

log = logging.getLogger("swarm_voice.claudeturn")

#: The planning class's pin (`local.runner_models.fable`), read from
#: VOICE_PLANNER_MODEL when set (Terraform renders the map's pin there, so a
#: deployment never runs this literal). It is a fallback for a run with no
#: environment. A test holds it, and FRONT_MODEL below, equal to the map, so
#: it cannot drift from the pin.
MODEL = "claude-fable-5-2"
#: The haiku class's pin (`local.runner_models.haiku`), read from
#: VOICE_FRONT_MODEL when set.
FRONT_MODEL = "claude-haiku-5-5"
PLANNER_MODEL_ENV = "VOICE_PLANNER_MODEL"
FRONT_MODEL_ENV = "VOICE_FRONT_MODEL"
#: The MCP server's name, so its tools are `mcp__planner__<tool>` to the CLI.
SERVER = mcp_server.SERVER_NAME
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
#: The variable an Anthropic developer key goes in (an `ApiKey` token).
KEY_ENV = "ANTHROPIC_API_KEY"
#: Why a turn's account could not serve it (`ChildOutcome.spent`, §5.2).
SPENT_CREDIT = "credit_exhausted"
SPENT_RATE = "rate_limited"
#: What the CLI's error result says for each, lower-cased; credit first, as
#: the broker's classifier matches it before the rate-limit markers.
_SPENT_MARKERS = (
    (SPENT_CREDIT, ("credit balance is too low", "credit_balance_exhausted", "billing_error")),
    (SPENT_RATE, ("rate_limit_error", "rate_limit", "api error: 429")),
)
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



def planner_model() -> str:
    """The planner's model: VOICE_PLANNER_MODEL, else the planning class's pin."""
    return os.environ.get(PLANNER_MODEL_ENV, "").strip() or MODEL


def front_model() -> str:
    """The fast front's model: VOICE_FRONT_MODEL, else the haiku class's pin."""
    return os.environ.get(FRONT_MODEL_ENV, "").strip() or FRONT_MODEL


def admitted_model(model: str) -> bool:
    """Whether `model` is the id of one of the pool's model classes
    (`MODEL_CLASSES`' own rule): the only ids a start body may name."""
    return any(c.admits(model) for c in MODEL_CLASSES.values())


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


class ApiKey(str):
    """What a planner lease on an Anthropic developer key hands over, in place
    of a plan's token; `ClaudeCode.env` gives it to the child in KEY_ENV. Its
    repr never shows it."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ApiKey(<redacted>)"


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
    #: SPENT_CREDIT or SPENT_RATE when the turn ended on a spent account, else None.
    spent: str | None = None


def note_result(outcome: ChildOutcome, event: dict[str, Any]) -> None:
    """Read the CLI's (scrubbed) `result` event into `outcome`."""
    outcome.is_error = bool(event.get("is_error"))
    text = event.get("result")
    outcome.result_text = text if isinstance(text, str) else ""
    outcome.spent = None
    if outcome.is_error:
        said = outcome.result_text.lower()
        outcome.spent = next((why for why, markers in _SPENT_MARKERS if any(m in said for m in markers)), None)


_COUNTS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _counts(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    return {k: v for k in _COUNTS if isinstance(v := usage.get(k), int) and not isinstance(v, bool)}


TextSink = Callable[[str], None]


def feed_text(event: dict[str, Any], on_text: TextSink, state: dict[str, Any]) -> None:
    """Hand the (scrubbed) text an event carries to `on_text`, in order: each
    `text_delta` of `--include-partial-messages`' stream events, or, when no
    delta came this turn (a CLI without partial messages), a whole assistant
    message's text blocks. `state` is the turn's, kept by the caller."""
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event") if isinstance(event.get("event"), dict) else {}
        if inner.get("type") == "message_start" and state.get("text"):
            # A new model message after text: the two are one spoken line.
            state["gap"] = True
        delta = inner.get("delta") if isinstance(inner.get("delta"), dict) else {}
        if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                state["deltas"] = True
                _hand(text, on_text, state)
    elif kind == "assistant" and not state.get("deltas"):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "".join(t for t in texts if isinstance(t, str))
        if joined:
            if state.get("text"):
                state["gap"] = True
            _hand(joined, on_text, state)


def _hand(text: str, on_text: TextSink, state: dict[str, Any]) -> None:
    if state.pop("gap", False):
        text = " " + text
    state["text"] = True
    try:
        on_text(text)
    except Exception as exc:  # noqa: BLE001 -- a listener never breaks the turn
        log.warning("voice front text listener failed (%s)", type(exc).__name__)


class ClaudeCode:
    """Starts one turn's `claude -p` child (module docstring)."""

    def __init__(self, *, binary: str | None = None, model: str | None = None, max_turns: int = 8,
                 python: str | None = None, tmp_root: str | None = None) -> None:
        self.binary = binary or os.environ.get("CLAUDE_CODE_BIN", "").strip() or "claude"
        self.model = model or planner_model()
        self.max_turns = max_turns
        self.python = python or sys.executable
        self.tmp_root = tmp_root

    def with_model(self, model: str) -> "ClaudeCode":
        """This launcher on `model`, for one session's turns; itself when it
        already runs it."""
        if model == self.model:
            return self
        other = copy.copy(self)
        other.model = model
        return other

    def args(self, *, system: str, mcp_config: str) -> list[str]:
        return [self.binary, "-p", "--model", self.model, "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--tools", "", "--mcp-config", mcp_config,
                "--strict-mcp-config", "--allowedTools", f"mcp__{SERVER}", "--permission-mode", "dontAsk",
                "--disable-slash-commands", "--max-turns", str(self.max_turns), "--system-prompt", system]

    def converse_args(self, *, system: str, mcp_config: str) -> list[str]:
        """The session-long child's: `args`, reading user messages as stream-json."""
        argv = self.args(system=system, mcp_config=mcp_config)
        # Text deltas as they are written (`feed_text`), so the front's first
        # sentence can be spoken before its turn ends (§12.6).
        return [*argv[:4], "--input-format", "stream-json", "--include-partial-messages", *argv[4:]]

    def env(self, *, home: str, token: str) -> dict[str, str]:
        """The child's whole environment: a key's token (`ApiKey`) as
        ANTHROPIC_API_KEY, a plan's as CLAUDE_CODE_OAUTH_TOKEN, never both."""
        variable = KEY_ENV if isinstance(token, ApiKey) else TOKEN_ENV
        return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home, "TMPDIR": home,
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1", **QUIET_ENV, variable: str(token)}

    def mcp_config(self, *, socket_path: str, nonce: str) -> dict[str, Any]:
        package_root = str(Path(__file__).resolve().parents[1])
        return {"mcpServers": {SERVER: {
            "type": "stdio", "command": self.python, "args": ["-m", "swarm_voice.mcp_server"],
            "env": {mcp_server.SOCKET_ENV: socket_path, mcp_server.NONCE_ENV: nonce,
                    "PYTHONPATH": package_root, TOKEN_ENV: "", KEY_ENV: ""}}}}

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

    async def converse(self, *, system: str, token: str, tools: list[dict[str, Any]], handler: ToolHandler,
                       known: tuple[str, ...]) -> "Conversation":
        """Start one session-long child (module docstring, "ONE WARM CHILD");
        the caller closes it. Raises OSError when the CLI cannot start, after
        removing what it made."""
        home = tempfile.mkdtemp(prefix="vf-", dir=self.tmp_root)
        os.chmod(home, 0o700)
        nonce = secrets.token_hex(16)
        bridge = ToolBridge(tools, handler, nonce)
        try:
            socket_path = os.path.join(home, "tools.sock")
            await bridge.start(socket_path)
            config = os.path.join(home, "mcp.json")
            fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self.mcp_config(socket_path=socket_path, nonce=nonce), fh)
            process = await asyncio.create_subprocess_exec(
                *self.converse_args(system=system, mcp_config=config), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.env(home=home, token=token), cwd=home, start_new_session=True, limit=MAX_LINE)
        except BaseException:
            await bridge.close()
            shutil.rmtree(home, ignore_errors=True)
            raise
        return Conversation(process, home=home, bridge=bridge, known=known, model=self.model)

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
                note_result(outcome, event)
                final = _counts(event.get("usage")) or None
        outcome.usage = _totals(by_message, final)

    @staticmethod
    async def _stderr(process: asyncio.subprocess.Process, outcome: ChildOutcome, known: tuple[str, ...]) -> None:
        assert process.stderr is not None
        tail = b""
        while chunk := await process.stderr.read(65536):
            tail = (tail + chunk)[-STDERR_TAIL * 2:]
        outcome.stderr_tail = prompts.scrub(tail.decode("utf-8", "replace"), known)[-STDERR_TAIL:]


class ChildGone(Exception):
    """The session-long child exited, was closed, or went silent before
    answering. `usage` is what its turn had used by then."""

    def __init__(self, message: str = "", usage: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.usage: dict[str, int] = dict(usage or {})


def _totals(by_message: dict[str, dict[str, int]], final: dict[str, int] | None = None) -> dict[str, int]:
    """A turn's token counts: the CLI's own `final`, else its messages' summed."""
    totals: dict[str, int] = {}
    for counts in ([final] if final else by_message.values()):
        for name, value in counts.items():
            totals[name] = totals.get(name, 0) + value
    return totals


class _Ending:
    """How a `say(done=)` turn is brought to its end at the closing tool: which
    step it is at, and when that step has waited long enough."""

    def __init__(self, closes: str | None) -> None:
        self.tool = f"mcp__{SERVER}__{closes}" if closes else None
        self._uses: set[str] = set()
        #: The closing tool's result was taken by the CLI.
        self.taken = False
        self.interrupted = False
        self.deadline: float | None = None

    def wait(self) -> None:
        """Give the step now begun STOP_GRACE_SECONDS."""
        self.deadline = asyncio.get_running_loop().time() + STOP_GRACE_SECONDS

    def left(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - asyncio.get_running_loop().time())

    def see(self, event: dict[str, Any]) -> None:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        for block in (b for b in blocks if isinstance(b, dict)):
            if block.get("type") == "tool_use" and block.get("name") == self.tool:
                self._uses.add(str(block.get("id")))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in self._uses:
                self.taken = True


class Conversation:
    """One running `claude -p --input-format stream-json` child: `say` sends
    one user message and returns that turn's outcome; `close` stops the child
    and removes its private HOME. Turns run one at a time. Its repr names no
    token and no text."""

    def __init__(self, process: asyncio.subprocess.Process, *, home: str, bridge: ToolBridge,
                 known: tuple[str, ...], model: str) -> None:
        self._process = process
        self.home = home
        self._bridge = bridge
        self._known = known
        self.model = model
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._stderr = ChildOutcome()
        self._lock = asyncio.Lock()
        self.closed = False
        #: A turn did not settle: what the child does next is nobody's (module docstring).
        self.spoiled = False
        self._reader = asyncio.create_task(self._read(), name="voice-front-child-out")
        self._errors = asyncio.create_task(ClaudeCode._stderr(process, self._stderr, known),
                                           name="voice-front-child-err")

    def __repr__(self) -> str:
        return f"Conversation(model={self.model!r}, alive={self.alive})"

    @property
    def alive(self) -> bool:
        return not self.closed and not self.spoiled and self._process.returncode is None

    @property
    def stderr_tail(self) -> str:
        return self._stderr.stderr_tail

    async def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                try:
                    raw = await self._process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    continue
                if not raw:
                    break
                # Scrubbed BEFORE it is parsed, as a planner turn's are.
                line = prompts.scrub(raw.decode("utf-8", "replace"), self._known).strip()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._events.put_nowait(event)
        finally:
            self._events.put_nowait(None)

    def _drop_stale(self) -> None:
        """Forget what the child wrote after its last turn's result: it is not
        a sign of life and belongs to no turn. Its end (None) is kept."""
        while not self._events.empty():
            if self._events.get_nowait() is None:
                self._events.put_nowait(None)
                return

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps(message).encode() + b"\n")
            await self._process.stdin.drain()
        except (ConnectionError, RuntimeError) as exc:
            raise ChildGone(f"the conversation's child stopped reading ({type(exc).__name__})") from None

    async def _next(self, done: asyncio.Event | None, ending: _Ending,
                    sign_seconds: float | None) -> dict[str, Any] | None:
        """The child's next event (None at its end). Raises TimeoutError past
        `sign_seconds`, or past the ending's step once `done` is set."""
        if done is None and sign_seconds is None:
            return await self._events.get()
        loop = asyncio.get_running_loop()
        silent_at = None if sign_seconds is None else loop.time() + sign_seconds
        event = asyncio.ensure_future(self._events.get())
        stop = asyncio.ensure_future(done.wait()) if done is not None and not done.is_set() else None
        try:
            while not event.done():
                if done is not None and done.is_set():
                    if ending.deadline is None:
                        ending.wait()
                    left = ending.left()
                else:
                    left = None if silent_at is None else max(0.0, silent_at - loop.time())
                if left is not None and left <= 0:
                    raise asyncio.TimeoutError
                watch = {event} if stop is None or stop.done() else {event, stop}
                await asyncio.wait(watch, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                if ending.tool is None and done is not None and done.is_set() and not ending.interrupted:
                    break
            return event.result() if event.done() else {}
        finally:
            for task in (event, stop):
                if task is not None and not task.done():
                    task.cancel()

    async def _interrupt(self, ending: _Ending) -> None:
        """Stop the turn's model rounds; the child then writes its `result`."""
        ending.interrupted = True
        ending.wait()
        await self._send({"type": "control_request", "request_id": "end-" + secrets.token_hex(8),
                          "request": {"subtype": "interrupt"}})

    async def say(self, text: str, *, on_text: TextSink | None = None, done: asyncio.Event | None = None,
                  closes: str | None = None, sign_seconds: float | None = None) -> ChildOutcome:
        """One user message; the turn's scrubbed outcome. Raises ChildGone.
        `on_text`, when given, is handed the turn's text as it streams
        (`feed_text`), before the outcome returns. With `done`, the turn is
        interrupted once it is set and the tool named `closes` has its result;
        with `sign_seconds`, a child that writes nothing for that long is
        ChildGone (module docstring, "THE PLANNER'S WARM CHILD")."""
        async with self._lock:
            if not self.alive:
                raise ChildGone("the conversation's child is not running")
            if done is not None:
                self._drop_stale()
            await self._send({"type": "user", "message": {"role": "user",
                                                          "content": [{"type": "text", "text": text}]}})
            outcome = ChildOutcome()
            by_message: dict[str, dict[str, int]] = {}
            streamed: dict[str, Any] = {}
            ending = _Ending(closes)
            while True:
                try:
                    if (done is not None and done.is_set() and not ending.interrupted
                            and (ending.taken or ending.tool is None)):
                        await self._interrupt(ending)
                    event = await self._next(done, ending, sign_seconds)
                except (asyncio.TimeoutError, ChildGone) as exc:
                    self.spoiled = True
                    if done is not None and done.is_set():
                        # It answered; it only did not settle. The answer stands.
                        outcome.stopped, outcome.usage = True, _totals(by_message)
                        return outcome
                    if isinstance(exc, ChildGone):
                        raise
                    raise ChildGone("the conversation's child gave no sign of life") from None
                if event is None:
                    # EOF: put it back, so the next say sees it too.
                    self._events.put_nowait(None)
                    self.spoiled = True
                    if done is not None and done.is_set():
                        outcome.stopped, outcome.usage = True, _totals(by_message)
                        return outcome
                    raise ChildGone("the conversation's child exited before it answered", _totals(by_message))
                kind = event.get("type")
                if not event or kind == "control_response":
                    continue
                sign_seconds = None
                if on_text is not None:
                    feed_text(event, on_text, streamed)
                if kind == "stream_event":
                    # Deltas repeat the assistant messages that follow them:
                    # not kept, so the outcome's events stay as they were.
                    continue
                if len(outcome.events) < MAX_EVENTS:
                    outcome.events.append(event)
                ending.see(event)
                message_ = event.get("message") if isinstance(event.get("message"), dict) else {}
                if kind == "assistant" and message_:
                    by_message[str(message_.get("id") or len(by_message))] = _counts(message_.get("usage"))
                elif kind == "result":
                    note_result(outcome, event)
                    outcome.usage = _totals(by_message, _counts(event.get("usage")) or None)
                    if ending.interrupted:
                        # The error it reports is the interrupt itself.
                        outcome.stopped, outcome.is_error, outcome.spent = True, False, None
                    return outcome

    async def close(self) -> None:
        """Stop the child and remove its HOME. Idempotent."""
        if self.closed:
            return
        self.closed = True
        try:
            if self._process.stdin is not None:
                with contextlib.suppress(Exception):
                    self._process.stdin.close()
            if self._process.returncode is None:
                await ClaudeCode._stop(self._process)
            for task in (self._reader, self._errors):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        finally:
            await self._bridge.close()
            shutil.rmtree(self.home, ignore_errors=True)
