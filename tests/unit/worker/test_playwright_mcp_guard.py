"""The Playwright MCP guard restarts a server whose backend is poisoned.

On a claude-code-browser QA leg, after the page had loaded, every
browser_snapshot, browser_wait_for, browser_find and browser_click answered
"Cannot read properties of undefined (reading 'length')", so the flow never ran
a step. The throw is in the third-party server (@playwright/mcp 0.0.83 on
playwright-core 1.64.0-alpha-1790635538000): its BrowserBackend.callTool
catches it and answers `### Error\\nTypeError: ...` with `isError`, and its MCP
server keeps that backend until the browser disconnects, so the same state
throws on every later call.

FAKE_SERVER reproduces that: a newline-delimited JSON-RPC MCP server whose
tab list becomes `undefined` after a navigation, so every later tool reads
`.length` of undefined, caught and answered the way upstream answers it. The
guard (images/agent-runtime-browser/playwright-mcp-guard.mjs) runs in front of
it as the runner registers it: `node guard <server> <args...>`.

MUTATION: register the server without the guard (claude_code.write_mcp_config),
or make the guard pass the poisoned answer through, and the snapshot test below
gets the TypeError back instead of the page.

Like test_jev_internal_compat.py this needs `node`, and FAILS under CI without
it rather than skipping.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
IMAGE_DIR = ROOT / "images" / "agent-runtime-browser"
GUARD = IMAGE_DIR / "playwright-mcp-guard.mjs"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
TIMEOUT = 20

# Upstream's shape: Tab state read on every call, and BrowserBackend.callTool's
# catch, `### Error\n${String(error)}` with isError. A navigation leaves the tab
# list undefined; nothing resets it until the process is replaced.
FAKE_SERVER = r"""
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

const [log, mode] = process.argv.slice(2);
const pid = String(process.pid);
const note = event => appendFileSync(log, `${pid} ${event}\n`);
note("start");
let tabs = mode === "always" ? undefined : [{ url: "about:blank" }];
const send = message => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...message }) + "\n");

function handle(name, args) {
  if (name === "browser_navigate") {
    tabs = undefined;   // the frames/targets list a navigation left behind
    return `navigated to ${args.url}`;
  }
  if (name === "browser_refuse") throw new Error("Ref e5 not found in the current page snapshot");
  return `${name} saw ${tabs.length} tab(s) on server ${pid}`;
}

createInterface({ input: process.stdin }).on("line", line => {
  const message = JSON.parse(line);
  if (message.method === "initialize") {
    note(`initialize ${JSON.stringify(message.id)}`);
    send({ id: message.id, result: { protocolVersion: message.params.protocolVersion,
                                      capabilities: { tools: {} }, serverInfo: { name: "fake", version: "0" } } });
  } else if (message.method === "notifications/initialized") {
    note("initialized");
  } else if (message.method === "tools/call") {
    note(`call ${message.params.name}`);
    let result;
    try {
      result = { content: [{ type: "text", text: handle(message.params.name, message.params.arguments ?? {}) }] };
    } catch (error) {
      result = { content: [{ type: "text", text: `### Error\n${String(error)}` }], isError: true };
    }
    send({ id: message.id, result });
  }
});
"""


@pytest.fixture(scope="module")
def node() -> str:
    found = shutil.which("node")
    if not found:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the guard cannot run; it must not pass by skipping")
        pytest.skip("node is not installed here; CI runs these")
    return found


class Session:
    """The CLI's side of the guard: requests in, lines out, by id."""

    def __init__(self, node: str, tmp_path: Path, mode: str = "after-navigate") -> None:
        server = tmp_path / "fake-server.mjs"
        server.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = tmp_path / "server.log"
        self.log.write_text("", encoding="utf-8")
        self.proc = subprocess.Popen(
            [node, str(GUARD), node, str(server), str(self.log), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        answer = json.loads(self.lines.get(timeout=TIMEOUT))
        assert answer["id"] == self.next_id, answer
        return answer

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def start(self) -> None:
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "cli", "version": "1"}})
        self.send({"method": "notifications/initialized"})

    def events(self) -> list[tuple[str, str]]:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [tuple(line.split(" ", 1)) for line in lines]  # type: ignore[misc]

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout=TIMEOUT)
        assert self.lines.empty(), "the guard answered something nobody asked for"
        return code


@pytest.fixture()
def session(node, tmp_path):
    started = Session(node, tmp_path)
    started.start()
    yield started
    if started.proc.poll() is None:
        started.proc.kill()


def _text(result: dict[str, Any]) -> str:
    return "\n".join(item["text"] for item in result["content"])


def _servers(events: list[tuple[str, str]]) -> list[str]:
    return [pid for pid, event in events if event == "start"]


def test_the_fake_server_alone_fails_every_call_after_a_navigation(node, tmp_path):
    # The defect as reported, without the guard: once poisoned, always poisoned.
    server = tmp_path / "fake-server.mjs"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    log = tmp_path / "server.log"
    lines = [{"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": {}}}
             for n, name in enumerate(["browser_navigate", "browser_snapshot", "browser_wait_for",
                                       "browser_find", "browser_click"], start=1)]
    done = subprocess.run([node, str(server), str(log)], input="".join(json.dumps(m) + "\n" for m in lines),
                          capture_output=True, text=True, timeout=TIMEOUT)
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a["result"].get("isError", False) for a in answers] == [False, True, True, True, True]
    assert "TypeError: Cannot read properties of undefined (reading 'length')" in _text(answers[1]["result"])


def test_a_snapshot_after_the_navigation_is_answered_by_a_fresh_server(session):
    first = _servers(session.events())
    assert session.call("browser_navigate", url="https://app.example/")["content"][0]["text"].startswith("navigated")
    result = session.call("browser_snapshot")
    assert not result.get("isError"), result
    assert "TypeError" not in _text(result)
    servers = _servers(session.events())
    assert len(servers) == 2 and servers[0] == first[0]
    assert _text(result) == f"browser_snapshot saw 1 tab(s) on server {servers[1]}"
    # The fresh server was initialized as the CLI initialized the first one.
    fresh = [event for pid, event in session.events() if pid == servers[1]]
    assert fresh == ["start", 'initialize "swarm-playwright-mcp-guard-reinit"', "initialized",
                     "call browser_snapshot"]
    # Every later call works, on the fresh server, with no further restart.
    for name in ("browser_wait_for", "browser_find", "browser_click"):
        assert not session.call(name).get("isError")
    assert len(_servers(session.events())) == 2
    assert session.close() == 0


def test_an_action_is_not_repeated_and_the_error_says_what_to_do(session):
    session.call("browser_navigate", url="https://app.example/")
    result = session.call("browser_click", ref="e3")
    assert result["isError"] is True
    text = _text(result)
    assert "restarted and reconnected to the same live browser" in text
    assert '"browser_click" was not repeated' in text and "browser_snapshot" in text
    # The next call is answered by the fresh server (held until it is up).
    snapshot = session.call("browser_snapshot")
    events = session.events()
    servers = _servers(events)
    assert len(servers) == 2
    assert _text(snapshot) == f"browser_snapshot saw 1 tab(s) on server {servers[1]}"
    # The click ran once, on the poisoned server, and never on the fresh one.
    assert [pid for pid, event in events if event == "call browser_click"] == [servers[0]]
    assert session.close() == 0


def test_a_page_that_poisons_a_fresh_server_too_gets_a_clear_error_and_no_loop(node, tmp_path):
    session = Session(node, tmp_path, mode="always")
    try:
        session.start()
        result = session.call("browser_snapshot")
        assert result["isError"] is True
        assert "failed again with the same internal error" in _text(result)
        assert "Cannot read properties of undefined (reading 'length')" in _text(result)
        assert len(_servers(session.events())) == 2
        assert session.close() == 0
    finally:
        if session.proc.poll() is None:
            session.proc.kill()


def test_any_other_tool_error_passes_through_untouched(session):
    result = session.call("browser_refuse")
    assert result == {"content": [{"type": "text",
                                   "text": "### Error\nError: Ref e5 not found in the current page snapshot"}],
                      "isError": True}
    assert len(_servers(session.events())) == 1
    assert session.close() == 0


def test_the_image_installs_the_guard_where_the_runner_registers_it():
    from agent_worker.runners import claude_code

    text = DOCKERFILE.read_text(encoding="utf-8")
    assert ("install -o root -g root -m 0444 /tmp/swarm-browser-build/playwright-mcp-guard.mjs "
            f"{claude_code.PLAYWRIGHT_MCP_GUARD}") in text
    assert claude_code.PLAYWRIGHT_MCP_NODE == "/usr/local/bin/node"
