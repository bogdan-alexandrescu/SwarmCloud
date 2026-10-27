"""The browser tool a live-browser task names, what the runner registers for it,
and how the gate sees and reports it (BSTATUS-API; part of #1030).

Owner decision 2026-10-14, "Picker on Submit, Auto by default": a
`claude-code-browser` task names `browser_tool` -- auto, playwright,
dev-browser or jev (contract request 77) -- and the console's Browser tab and
theater strip show the active tool, the egress and jev's state. Measured here:

  * the runner registers what each choice means, names the chosen tool first
    in its note, and writes `browser.json` to the hand-off spool;
  * Playwright MCP, dev-browser and jev each connect with a client tag the CDP
    proxy reads, attributes and STRIPS -- nothing of it reaches Chromium;
  * the gate's report carries `browser`: the registration the worker
    forwarded, the last tool used, and the egress state with a fixed sentence
    for its cause, never a vendor's text.

MUTATION: drop `_tool_used(connection.client)` from `on_agent_message`, or the
tag's stripping in `split_client`, or `jev` from `choose`'s auto branch, and
the named test below fails.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest
from test_live_agent_unused import _answering_browser_targets
from test_live_cdp_proxy import FakeAgent, World, _frame, _http, _read_frame, run, settle
from test_live_device_recover import Rig, _run, _until

from agent_worker import browser_tools
from agent_worker.livegate import cdpproxy, egress
from agent_worker.livegate.cdpproxy import serve_agents, split_client
from agent_worker.livegate.report import browser_report, egress_report, encode_report

ENDPOINT = "http://127.0.0.1:9222"
T0 = 1_791_900_000.0


# --------------------------------------------------------------------------- #
# What each choice registers                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "selected, installed, key, tools, enabled, reason",
    [
        ("auto", True, True, ("playwright", "dev-browser", "jev"), True, None),
        (None, True, True, ("playwright", "dev-browser", "jev"), True, None),
        ("auto", True, False, ("playwright", "dev-browser"), False, "no_key"),
        ("auto", False, True, ("playwright", "dev-browser"), False, "not_installed"),
        ("playwright", True, True, ("playwright",), False, "not_selected"),
        ("dev-browser", True, True, ("dev-browser", "playwright"), False, "not_selected"),
        ("jev", True, True, ("jev", "playwright"), True, None),
        ("jev", True, False, ("playwright",), False, "no_key"),
        ("jev", False, False, ("playwright",), False, "not_installed"),
        ("chromium --headless", True, True, ("playwright", "dev-browser", "jev"), True, None),
    ],
)
def test_each_choice_registers_what_it_means(selected, installed, key, tools, enabled, reason):
    choice = browser_tools.choose(selected, jev_installed=installed, jev_key=key)
    assert choice.tools == tools
    assert (choice.jev_enabled, choice.jev_reason) == (enabled, reason)
    assert choice.selected in browser_tools.SELECTIONS
    # Every choice keeps Playwright: alone, or as the fallback.
    assert "playwright" in choice.tools
    assert browser_tools.clean_state(choice.state()) == choice.state()


@pytest.mark.parametrize(
    "selected, installed, tools, reason",
    [
        ("jev", True, ("playwright",), "no_route"),
        ("auto", True, ("playwright", "dev-browser"), "no_route"),
        ("jev", False, ("playwright",), "not_installed"),
        ("playwright", True, ("playwright",), "not_selected"),
        ("dev-browser", True, ("dev-browser", "playwright"), "not_selected"),
    ],
)
def test_no_route_to_the_decision_api_registers_no_jev(selected, installed, tools, reason):
    """JEV-OFF (#1345): an internal backend whose pod has no JEV_API_URL."""
    choice = browser_tools.choose(selected, jev_installed=installed, jev_key=True, jev_route=False)
    assert choice.tools == tools
    assert (choice.jev_enabled, choice.jev_reason) == (False, reason)
    assert browser_tools.clean_state(choice.state()) == choice.state()


def test_the_worker_names_are_the_catalogues_choices():
    from swarm_common.profiles import RUNNER_PROFILES

    declared = RUNNER_PROFILES["claude-code-browser"].inputs["browser_tool"]
    assert declared.choices == browser_tools.SELECTIONS


@pytest.mark.parametrize(
    "state",
    [
        {"tool_selected": "auto", "tools_available": ["playwright"], "jev": {"enabled": False, "reason": "no_key"},
         "extra": 1},
        {"tool_selected": "auto", "tools_available": ["playwright", "sh"], "jev": {"enabled": False, "reason": None}},
        {"tool_selected": "auto", "tools_available": ["dev-browser"], "jev": {"enabled": False, "reason": "no_key"}},
        {"tool_selected": "playwright", "tools_available": ["playwright", "jev"], "jev": {"enabled": True, "reason": None}},
        {"tool_selected": "jev", "tools_available": ["playwright"], "jev": {"enabled": True, "reason": None}},
        {"tool_selected": "auto", "tools_available": ["playwright", "playwright"],
         "jev": {"enabled": False, "reason": "no_key"}},
        {"tool_selected": "auto", "tools_available": ["playwright"], "jev": {"enabled": False, "reason": "because"}},
        {"tool_selected": "firefox", "tools_available": ["playwright"], "jev": {"enabled": False, "reason": "no_key"}},
        "auto",
    ],
)
def test_a_state_that_does_not_hold_together_is_refused(state):
    assert browser_tools.clean_state(state) is None


def test_the_tag_goes_on_a_bare_origin_and_nothing_else():
    assert browser_tools.tag_endpoint(ENDPOINT, "playwright") == ENDPOINT + "/client/playwright"
    assert browser_tools.tag_endpoint(ENDPOINT + "/", "jev") == ENDPOINT + "/client/jev"
    assert browser_tools.tag_endpoint("ws://127.0.0.1:9222/devtools/browser/x", "jev") == \
        "ws://127.0.0.1:9222/devtools/browser/x"
    with pytest.raises(ValueError):
        browser_tools.tag_endpoint(ENDPOINT, "auto")


# --------------------------------------------------------------------------- #
# The runner                                                                   #
# --------------------------------------------------------------------------- #


def _run_body(monkeypatch, tmp_path, *, payload: dict[str, Any], jev_key: bool = False,
              jev_installed: bool = True, backend: str | None = None,
              route: bool = True) -> dict[str, Any]:
    from agent_worker.runners import claude_code
    from agent_worker.runners.base import RunnerContext

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        return {}

    spool = tmp_path / "work" / ".swarm-handoff"
    spool.mkdir(parents=True)
    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    jev_bin = tmp_path / "jev-browser-mcp"
    if jev_installed:
        jev_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(claude_code, "JEV_MCP_BIN", str(jev_bin))
    monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, "/opt/playwright")
    monkeypatch.setenv("SWARM_CDP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("SWARM_HANDOFF", str(spool))
    # The key now reaches the runner as the worker's key FILE (JEV-RUNNER), not
    # as TYPESAFE_API_KEY in its environment; an internal backend needs neither.
    monkeypatch.delenv(claude_code.JEV_KEY_ENV, raising=False)
    if jev_key:
        # Built at runtime: nothing here has a credential's shape.
        key_file = tmp_path / "jev-typesafe.key"
        key_file.write_text("ts-" + secrets.token_hex(8))
        monkeypatch.setenv("SWARM_JEV_KEY_FILE", str(key_file))
    else:
        monkeypatch.delenv("SWARM_JEV_KEY_FILE", raising=False)
    if backend is None:
        monkeypatch.delenv("SWARM_JEV_BACKEND", raising=False)
    else:
        monkeypatch.setenv("SWARM_JEV_BACKEND", backend)
    # The dispatcher sends JEV_API_URL to an internal backend only while the
    # tailnet egress is on (scheduler.judgment.jev_dispatch); `route` is that.
    if backend in ("internal-jev", "internal-laya") and route:
        monkeypatch.setenv("JEV_API_URL", f"https://decision-api-staging.taile0bda9.ts.net/"
                                          f"{backend.split('-', 1)[1]}/v1/decide")
    else:
        monkeypatch.delenv("JEV_API_URL", raising=False)
    for name in ("SWARM_GIT", "SWARM_SEARCH"):
        monkeypatch.delenv(name, raising=False)
    ctx = RunnerContext(
        work_dir=tmp_path / "work", artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json", result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json", payload={"prompt": "Check the pricing page.", **payload},
    )
    claude_code.body(ctx)
    args = seen["args"]
    seen["mcp"] = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]
    seen["state"] = json.loads((spool / browser_tools.STATE_NAME).read_text())
    return seen


@pytest.mark.parametrize(
    "selected, key, servers, lead",
    [
        ("auto", True, {"playwright", "jev"}, "auto"),
        ("auto", False, {"playwright"}, "auto"),
        ("playwright", True, {"playwright"}, "playwright"),
        ("dev-browser", True, {"playwright"}, "dev-browser"),
        ("jev", True, {"playwright", "jev"}, "jev"),
        ("jev", False, {"playwright"}, "jev"),
    ],
)
def test_the_runner_registers_the_choice_and_names_it_first(monkeypatch, tmp_path, selected, key, servers, lead):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, payload={"browser_tool": selected}, jev_key=key)
    assert set(seen["mcp"]) == servers
    # Playwright MCP connects with its client tag; jev's wrapper adds its own.
    assert seen["mcp"]["playwright"]["args"] == ["--cdp-endpoint", ENDPOINT + "/client/playwright",
                                                 "--image-responses", "omit",
                                                 "--output-dir", str(tmp_path / "artifacts" / "screenshots")]
    if "jev" in servers:
        # With the key file, the launcher starts the wrapper; the config holds the
        # file's PATH and never the key (the wrapper-direct entry is the internal case).
        jev = seen["mcp"]["jev"]
        assert jev["command"] == sys.executable
        assert jev["args"][:2] == ["-I", str(claude_code.JEV_LAUNCH)]
        assert jev["args"][-1] == claude_code.JEV_MCP_BIN
        assert "ts-" not in json.dumps(jev)
    note = seen["prompt"].split("Check the pricing page.\n\n", 1)[1]
    assert note.startswith(claude_code.BROWSER_TOOL_LEAD[lead])
    assert claude_code.LIVE_BROWSER_NOTE in note
    choice = browser_tools.choose(selected, jev_installed=True, jev_key=key)
    assert seen["state"] == choice.state()
    # dev-browser is named only where it is offered.
    assert (claude_code.LIVE_BROWSER_DEV_BROWSER_NOTE in note) == ("dev-browser" in choice.tools)
    assert (claude_code.BROWSER_TOOL_STRENGTH["jev"] in note) == choice.jev_enabled


@pytest.mark.parametrize("backend", ["internal-jev", "internal-laya"])
@pytest.mark.parametrize("selected, servers", [
    ("auto", {"playwright", "jev"}), ("jev", {"playwright", "jev"}),
    ("playwright", {"playwright"}), ("dev-browser", {"playwright"}),
])
def test_an_internal_backend_needs_no_key_for_the_selected_tool(monkeypatch, tmp_path, backend, selected, servers):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, payload={"browser_tool": selected}, jev_key=False,
                     backend=backend)
    assert set(seen["mcp"]) == servers
    assert seen["state"]["jev"]["enabled"] == ("jev" in servers)
    if "jev" in servers:
        assert seen["mcp"]["jev"] == {"type": "stdio", "command": claude_code.JEV_MCP_BIN, "args": []}


def test_jev_chosen_without_a_key_runs_on_playwright_and_says_why(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, payload={"browser_tool": "jev"}, jev_key=False)
    assert claude_code.JEV_UNAVAILABLE_NOTE[browser_tools.NO_KEY] in seen["prompt"]
    assert seen["state"]["jev"] == {"enabled": False, "reason": "no_key"}
    seen = _run_body(monkeypatch, tmp_path / "again", payload={"browser_tool": "jev"}, jev_key=True,
                     jev_installed=False)
    assert claude_code.JEV_UNAVAILABLE_NOTE[browser_tools.NOT_INSTALLED] in seen["prompt"]
    assert set(seen["mcp"]) == {"playwright"}


@pytest.mark.parametrize("backend", ["internal-jev", "internal-laya"])
def test_an_internal_backend_without_the_tailnet_registers_no_jev_and_says_why(monkeypatch, tmp_path, backend):
    """JEV-OFF (#1345): measured on u-bogdan, the jev server was registered,
    its wrapper refused for want of JEV_API_URL, and the agent fell back with
    nothing said. The server the wrapper would refuse is not registered, and
    the note says why."""
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, payload={"browser_tool": "jev"}, jev_key=False,
                     backend=backend, route=False)
    assert set(seen["mcp"]) == {"playwright"}
    assert seen["state"]["jev"] == {"enabled": False, "reason": "no_route"}
    note = claude_code.JEV_UNAVAILABLE_NOTE[browser_tools.NO_ROUTE]
    assert note in seen["prompt"]
    assert "tailnet" in note and "decision API" in note
    # The company is not named (#1647, SAGA-COPY).
    assert "with your internal decision API, and this attempt has no route to it" in note
    assert "saga" not in note.lower()
    # Auto registers no jev either, and names no jev strength.
    seen = _run_body(monkeypatch, tmp_path / "auto", payload={"browser_tool": "auto"}, jev_key=False,
                     backend=backend, route=False)
    assert set(seen["mcp"]) == {"playwright"}
    assert seen["state"]["jev"] == {"enabled": False, "reason": "no_route"}
    assert claude_code.BROWSER_TOOL_STRENGTH_JEV_INTERNAL not in seen["prompt"]


def test_the_runner_reads_the_input_by_its_declared_name():
    source = (Path(__file__).resolve().parents[3] / "apps" / "agent-worker" / "agent_worker" / "runners"
              / "claude_code.py").read_text()
    assert 'ctx.payload.get("browser_tool")' in source


# --------------------------------------------------------------------------- #
# The client tag, at the CDP proxy                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "target, path, tool",
    [
        ("/client/playwright/json/version/", "/json/version/", "playwright"),
        ("/client/dev-browser/json/version", "/json/version", "dev-browser"),
        ("/json/version?client=jev", "/json/version", "jev"),
        ("/devtools/browser/abc?client=playwright", "/devtools/browser/abc", "playwright"),
        ("/client/jev", "/", "jev"),
        ("/client/curl/json/version", "/json/version", None),
        ("/json/version?client=sh", "/json/version", None),
        ("/json/version", "/json/version", None),
    ],
)
def test_split_client_strips_the_tag_and_names_only_a_tool(target, path, tool):
    assert split_client(target) == (path, tool)


def test_a_tagged_tool_is_attributed_and_chromium_never_sees_the_tag(tmp_path):
    async def body():
        told: list[tuple[str, float]] = []
        times = iter([T0, T0 + 1, T0 + 2, T0 + 20, T0 + 21])
        w = World(tmp_path, on_tool=lambda tool, at: told.append((tool, at)), wall=lambda: next(times))
        server = await serve_agents(w.proxy, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer, head = await _http(
                port, f"GET /client/dev-browser/json/version/ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            assert head.startswith(b"HTTP/1.1 200")
            length = int(next(line for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length")).split(b":")[1])
            ws_url = json.loads(await reader.readexactly(length))["webSocketDebuggerUrl"]
            writer.close()
            assert ws_url.endswith("?client=dev-browser")
            path = ws_url[len(f"ws://127.0.0.1:{port}"):]
            upgrade = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                       "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                       "Sec-WebSocket-Version: 13\r\n\r\n").encode()
            reader, writer, head = await _http(port, upgrade)
            assert head.startswith(b"HTTP/1.1 101")
            writer.write(_frame(json.dumps({"id": 1, "method": "Browser.getVersion"}).encode()))
            await writer.drain()
            assert json.loads(await asyncio.wait_for(_read_frame(reader), 2))["id"] == 1
            await settle()
            assert w.proxy.tool_last_used is not None and w.proxy.tool_last_used[0] == "dev-browser"
            # Told on the accept; the command at +2 s is inside the report interval.
            assert [t for t, _ in told] == ["dev-browser"]
            assert "client" not in json.dumps(w.chromium.received)
            writer.close()
        finally:
            server.close()
            await w.chromium.stop()

    run(body)


def test_a_change_of_tool_is_told_at_once_and_an_untagged_one_never(tmp_path):
    async def body():
        told: list[str] = []
        clock = [T0]
        w = World(tmp_path, on_tool=lambda tool, at: told.append(tool), wall=lambda: clock[0])
        plain = await w.proxy.connect_agent(FakeAgent())
        jev = await w.proxy.connect_agent(FakeAgent(), client="jev")
        mcp = await w.proxy.connect_agent(FakeAgent(), client="playwright")
        assert plain is not None and jev is not None and mcp is not None
        await w.proxy.on_agent_message(plain, {"id": 1, "method": "Browser.getVersion"})
        await settle()
        assert w.proxy.tool_last_used == ("playwright", T0)
        await w.proxy.on_agent_message(jev, {"id": 2, "method": "Browser.getVersion"})
        await settle()
        assert told == ["jev", "playwright", "jev"]
        clock[0] = T0 + cdpproxy.TOOL_REPORT_SECONDS + 1
        await w.proxy.on_agent_message(jev, {"id": 3, "method": "Browser.getVersion"})
        await settle()
        assert told == ["jev", "playwright", "jev", "jev"]
        assert w.proxy.tool_last_used == ("jev", T0 + cdpproxy.TOOL_REPORT_SECONDS + 1)
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------- #
# The report                                                                   #
# --------------------------------------------------------------------------- #


def test_the_egress_report_is_off_ok_or_failing_with_a_fixed_sentence():
    assert egress_report(proxied=False, ok=True, error=None) == \
        {"mode": "direct", "state": "off", "last_error": None}
    assert egress_report(proxied=True, ok=True, error="ignored") == \
        {"mode": "residential", "state": "ok", "last_error": None}
    failing = egress_report(proxied=True, ok=False, error=egress.describe("vendor_unreachable"))
    assert failing == {"mode": "residential", "state": "failing",
                       "last_error": "the residential egress provider cannot be reached"}
    # A cause nobody named is the generic sentence, never the cause's text.
    assert egress.describe("brightdata_said_xyz") == "the egress path refused the connection"


def test_the_report_carries_browser_only_when_given_one():
    assert "browser" not in json.loads(encode_report(None, None))
    browser = browser_report(tools=None, last_used=("jev", T0),
                             egress=egress_report(proxied=False, ok=True, error=None))
    assert json.loads(encode_report(None, None, None, browser))["browser"] == {
        "tools": None, "tool_last_used": {"name": "jev", "at": T0},
        "egress": {"mode": "direct", "state": "off", "last_error": None},
    }


class _Forwarder:
    """The parts of egress.LocalForwarder the gate's report reads."""

    def __init__(self) -> None:
        self.answer: tuple[bool, str | None] = (True, None)

    def healthy(self) -> tuple[bool, str | None]:
        return self.answer


def test_the_gate_reports_the_tools_the_last_tool_and_the_egress(tmp_path):
    async def body(rig: Rig) -> None:
        _answering_browser_targets(rig)
        gate = rig.gate
        sent: list[dict[str, Any] | None] = []

        async def report(payload: bytes) -> None:
            sent.append(json.loads(payload).get("browser"))

        rig.tunnel.report = report  # type: ignore[method-assign]
        reporter = asyncio.create_task(gate._reporter(rig.tunnel))  # type: ignore[arg-type]
        try:
            await _until(lambda: bool(sent))
            assert sent[-1] == {"tools": None, "tool_last_used": None,
                                "egress": {"mode": "direct", "state": "off", "last_error": None}}

            state = browser_tools.choose("dev-browser", jev_installed=True, jev_key=False).state()
            assert await gate.handle_ctl(browser_tools.CTL_OP, state) == {"browser_tools": "recorded"}
            await _until(lambda: sent[-1] is not None and sent[-1]["tools"] == state)
            with pytest.raises(ValueError):
                await gate.handle_ctl(browser_tools.CTL_OP, {**state, "tools_available": ["sh"]})

            connection = await gate.proxy.connect_agent(FakeAgent(), client="dev-browser")
            assert connection is not None
            await _until(lambda: sent[-1] is not None and sent[-1]["tool_last_used"] is not None)
            assert sent[-1]["tool_last_used"]["name"] == "dev-browser"

            # Proxied egress: the forwarder's state, as a fixed sentence.
            gate.cfg = dataclasses.replace(gate.cfg, egress=egress.EgressSettings(proxy=True))
            forwarder = gate.egress = _Forwarder()
            assert gate.egress_report() == {"mode": "residential", "state": "ok", "last_error": None}
            forwarder.answer = (False, "vendor_refused")
            assert gate.egress_report() == {
                "mode": "residential", "state": "failing",
                "last_error": "the residential egress provider refused the connection"}
        finally:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)

    _run(body, tmp_path)


def test_the_forwarder_is_failing_on_a_path_refusal_and_not_on_one_requests():
    records: list[str] = []
    forwarder = egress.LocalForwarder(egress.EgressSettings(proxy=True, forwarder_host="h", forwarder_port=1),
                                      record=lambda event, fields: records.append(event))
    assert forwarder.healthy() == (False, "no_ticket")
    forwarder.set_ticket("t" + secrets.token_hex(8), 600)
    assert forwarder.healthy() == (True, None)

    class _Writer:
        def write(self, data: bytes) -> None:
            pass

    forwarder._refuse(_Writer(), 403, "target")  # type: ignore[arg-type]
    assert forwarder.healthy() == (True, None)
    forwarder._refuse(_Writer(), 502, "vendor_unreachable")  # type: ignore[arg-type]
    assert forwarder.healthy() == (False, "vendor_unreachable")
    forwarder._refuse(_Writer(), 403, "target")  # type: ignore[arg-type]
    assert forwarder.healthy() == (False, "vendor_unreachable")
    forwarder.unreachable()
    assert forwarder.healthy() == (False, "forwarder_unreachable")
