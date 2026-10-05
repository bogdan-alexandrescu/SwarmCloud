"""One language server as a child process speaking LSP over stdio.

docs/repo-index.md §3.5: no editor, no network listener -- the indexer
starts the server, writes JSON-RPC 2.0 messages framed by `Content-Length`
headers to its stdin and reads its answers from its stdout.

Three limits stop a server, and each says which one did:

  per request   `request(..., timeout=)`; a request the server does not
                answer in time raises RequestTimeout and the server keeps
                running (one slow site is not a broken server)
  the budget    a deadline for the whole server; a request that would run
                past it raises BudgetExceeded
  memory        a watchdog sums the resident memory of every process in the
                server's session (pyright and tsserver run worker processes
                of their own) and kills the session over the limit. The
                indexer's requests equal its limits (invariant 7), so a
                server allowed to grow until the kernel's OOM killer acts
                would take the whole index step with it, not just itself

A stopped server is killed as a process group: it was started in its own
session, so its workers go with it.

Requests the server sends to the client are answered here, from the
reader thread: `workspace/configuration` from the spec's settings, the
registration and progress requests with null, anything else with
MethodNotFound. Notifications (diagnostics, progress, log messages) are
read and dropped; the index records none of a server's own text, which is
untrusted output derived from the repository.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

# How often the memory watchdog looks. A server grows by tens of MiB per
# poll at most, so a quarter-second keeps an overshoot small.
WATCH_INTERVAL_SECONDS = 0.25
# How long a polite shutdown gets before the process group is killed.
SHUTDOWN_GRACE_SECONDS = 2.0
# One message's ceiling. A definition answer is a few hundred bytes; a
# diagnostics notification for a huge file can be megabytes. Past this the
# stream is treated as broken rather than buffered without bound.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


class LspError(Exception):
    """Base of the ways a request can fail."""


class RequestTimeout(LspError):
    """The server did not answer this request in its time."""


class BudgetExceeded(LspError):
    """The server's overall budget ran out."""


class ServerExited(LspError):
    """The server is gone: it exited, crashed or was stopped."""


class ServerError(LspError):
    """The server answered with a JSON-RPC error."""

    def __init__(self, code: int) -> None:
        super().__init__(f"error {code}")
        self.code = code


def session_rss_bytes(session: int, proc: Path = Path("/proc")) -> int:
    """Resident bytes of every process whose session id is `session`."""
    total = 0
    try:
        entries = os.listdir(proc)
    except OSError:
        return 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = (proc / entry / "stat").read_text()
            # Fields after the parenthesised command name, which may itself
            # contain spaces or parentheses: state ppid pgrp session ...
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[3]) != session:
                continue
            resident = int((proc / entry / "statm").read_text().split()[1])
        except (OSError, ValueError, IndexError):
            continue
        total += resident * _PAGE_SIZE
    return total


class LspClient:
    """A started server, its reader thread and its memory watchdog."""

    def __init__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], *,
                 deadline: float, memory_limit_bytes: int,
                 settings: Mapping[str, Any] | None = None,
                 workspace_folders: list[dict] | None = None) -> None:
        self.deadline = deadline
        self.memory_limit_bytes = memory_limit_bytes
        self.settings = dict(settings or {})
        self.workspace_folders = workspace_folders or []
        self.memory_exceeded = False
        self.peak_rss_bytes = 0
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._closed = False
        self.process = subprocess.Popen(
            list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._read_loop, name="lsp-reader", daemon=True)
        self._reader.start()
        self._watchdog = threading.Thread(target=self._watch_loop, name="lsp-watchdog", daemon=True)
        self._watchdog.start()

    # --- the wire -----------------------------------------------------------

    def _send(self, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
        with self._write_lock:
            stdin = self.process.stdin
            if stdin is None or stdin.closed:
                raise ServerExited("stdin closed")
            try:
                stdin.write(frame)
                stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise ServerExited("stdin closed") from exc

    def _read_message(self) -> dict | None:
        stdout = self.process.stdout
        assert stdout is not None
        length = None
        while True:
            line = stdout.readline(4096)
            if not line:
                return None
            line = line.strip()
            if not line:
                if length is None:
                    continue
                break
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        if length < 0 or length > MAX_MESSAGE_BYTES:
            return None
        body = stdout.read(length)
        if len(body) != length:
            return None
        return json.loads(body)

    def _read_loop(self) -> None:
        try:
            while True:
                message = self._read_message()
                if message is None:
                    break
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    if "id" in message:
                        self._answer(message)
                    continue
                if isinstance(message.get("id"), int):
                    with self._cond:
                        self._responses[message["id"]] = message
                        self._cond.notify_all()
        except (ValueError, OSError):
            pass
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _answer(self, request: dict) -> None:
        method = request.get("method")
        params = request.get("params") or {}
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "workspace/configuration":
            reply["result"] = [self._setting(item.get("section")) for item in params.get("items", [])]
        elif method == "workspace/workspaceFolders":
            reply["result"] = self.workspace_folders
        elif method in ("client/registerCapability", "client/unregisterCapability",
                        "window/workDoneProgress/create", "window/showMessageRequest",
                        "workspace/codeLens/refresh", "workspace/semanticTokens/refresh",
                        "workspace/inlayHint/refresh", "workspace/diagnostic/refresh"):
            reply["result"] = None
        else:
            reply["error"] = {"code": -32601, "message": "method not supported by the indexer"}
        try:
            self._send(reply)
        except ServerExited:
            pass

    def _setting(self, section: str | None) -> Any:
        if not section:
            return self.settings
        if section in self.settings:
            return self.settings[section]
        # "python.analysis" may be asked of {"python": {"analysis": ...}}.
        node: Any = self.settings
        for part in section.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    # --- the limits ---------------------------------------------------------

    def _watch_loop(self) -> None:
        while self.process.poll() is None:
            rss = session_rss_bytes(self.process.pid)
            self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
            if rss > self.memory_limit_bytes:
                self.memory_exceeded = True
                self._kill()
                break
            time.sleep(WATCH_INTERVAL_SECONDS)

    def _kill(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # --- requests -----------------------------------------------------------

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, timeout: float) -> Any:
        """The result of `method`, or one of the LspError subclasses."""
        now = time.monotonic()
        if now >= self.deadline:
            raise BudgetExceeded()
        limit = min(now + timeout, self.deadline)
        with self._cond:
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        with self._cond:
            while request_id not in self._responses:
                if self._closed:
                    raise ServerExited()
                remaining = limit - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            response = self._responses.pop(request_id, None)
        if response is None:
            # A late answer to this id is dropped by nobody waiting for it;
            # tell the server it may stop working on it.
            try:
                self.notify("$/cancelRequest", {"id": request_id})
            except ServerExited:
                pass
            if time.monotonic() >= self.deadline:
                raise BudgetExceeded()
            raise RequestTimeout()
        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {}
            code = error.get("code")
            raise ServerError(code if isinstance(code, int) else 0)
        return response.get("result")

    def exit_status(self) -> int | None:
        return self.process.poll()

    def close(self) -> None:
        """Ask the server to shut down, then kill its process group anyway."""
        if self.process.poll() is None and not self._closed:
            # A budget that ran out ends the queries, not the shutdown: the
            # server gets its grace to exit cleanly, and is killed after it.
            self.deadline = max(self.deadline, time.monotonic() + SHUTDOWN_GRACE_SECONDS)
            try:
                self.request("shutdown", None, timeout=SHUTDOWN_GRACE_SECONDS)
                self.notify("exit", None)
            except LspError:
                pass
            try:
                self.process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        # Its workers too, even when the server itself already exited.
        self._kill()
        try:
            self.process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._reader.join(timeout=SHUTDOWN_GRACE_SECONDS)
        self._watchdog.join(timeout=SHUTDOWN_GRACE_SECONDS)
