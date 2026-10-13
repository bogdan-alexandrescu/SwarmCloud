"""The gate says whether the agent is driving the live browser (LIVE-UNUSED; part of #1030).

MEASURED 2026-10-13 (task_7cda0d5335f049b4b442, "the agent is not doing the
job"): the agent worked in a browser of its own while the live view stayed
blank, and the console said nothing, because nothing told it. The agent can
reach this browser only through the gate's CDP proxy, so the proxy is where
"the agent connected" is known:

  * the proxy dates the FIRST accepted agent connection (`agent_connected_at`)
    and counts the open ones (`agent_connections`), telling `on_agents` on
    every accept and close;
  * the gate's `status` carries both, and so does its report to the relay
    (`agent`, report.py `agent_report`), sent again on each change and on the
    first frame, which the console times its note from;
  * `agent_connected` goes on the timeline ONCE, through the control-action
    mechanism `reconnecting_to_page` uses -- no new frozen EventType.

Against the real `CdpProxy` and the real `LiveGate` (the device-recover rig's
scripted Chromium); no Chromium, no network.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from test_live_cdp_proxy import FakeAgent, World, run, settle
from test_live_device_recover import Rig, _run, _until
from test_livegate_main import Rig as MainRig
from test_livegate_main import _reports as _main_reports
from test_livegate_main import _started, short_tmp  # noqa: F401  (fixture)

from agent_worker.livegate import tunnel

from agent_worker.livegate.report import agent_report, encode_report

T_CONNECT = 1_791_900_042.0


# --------------------------------------------------------------------------- #
# The proxy                                                                    #
# --------------------------------------------------------------------------- #


def test_the_proxy_dates_the_first_agent_connection_and_counts_the_open_ones(tmp_path):
    async def body():
        told: list[int] = []
        times = iter([T_CONNECT, T_CONNECT + 30])
        w = World(tmp_path, on_agents=told.append, wall=lambda: next(times))
        assert w.proxy.agent_connected_at is None and w.proxy.agent_connections == 0

        await w.connect()
        assert w.proxy.agent_connected_at == T_CONNECT
        assert w.proxy.agent_connections == 1
        second = await w.proxy.connect_agent(FakeAgent())
        assert second is not None
        # The FIRST connection dates it; a second does not move it.
        assert w.proxy.agent_connected_at == T_CONNECT
        assert w.proxy.agent_connections == 2

        await w.proxy.disconnect_agent(second)
        assert w.proxy.agent_connections == 1
        await w.proxy.disconnect_agent(second)  # a second close is not a second change
        await w.proxy.chromium_closed()
        assert w.proxy.agent_connections == 0
        # An agent that connected and left did use this browser.
        assert w.proxy.agent_connected_at == T_CONNECT
        assert told == [1, 2, 1, 0]
        await w.chromium.stop()

    run(body)


def test_a_connection_refused_while_a_person_is_in_control_is_not_the_agent_using_it(tmp_path):
    async def body():
        told: list[int] = []
        w = World(tmp_path, on_agents=told.append)
        await w.proxy.pause()
        assert await w.proxy.connect_agent(FakeAgent()) is None
        assert w.proxy.agent_connected_at is None and w.proxy.agent_connections == 0
        assert told == []
        await w.chromium.stop()

    run(body)


# --------------------------------------------------------------------------- #
# The report                                                                   #
# --------------------------------------------------------------------------- #


def test_the_report_carries_the_agent_only_when_given_one():
    plain = json.loads(encode_report(None, None))
    assert plain == {"origin": None, "device": None}
    agent = agent_report(connected_at=None, connections=0, first_frame_at=T_CONNECT - 10)
    assert json.loads(encode_report(None, None, agent)) == {
        "origin": None, "device": None,
        "agent": {"connected_at": None, "connections": 0, "first_frame_at": T_CONNECT - 10},
    }


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #


class _Reports:
    """The rig's tunnel, keeping each report's `agent`."""

    def __init__(self, rig: Rig) -> None:
        self.agents: list[dict[str, Any] | None] = []

        async def report(payload: bytes) -> None:
            self.agents.append(json.loads(payload).get("agent"))

        rig.tunnel.report = report  # type: ignore[method-assign]


def _answering_browser_targets(rig: Rig) -> None:
    """The rig's Chromium also answers `Target.attachToBrowserTarget`, as the real one does."""
    chromium = rig.chromium
    answer = chromium._answer
    count = iter(range(1, 100))

    def _answer(method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        if method == "Target.attachToBrowserTarget":
            return {"result": {"sessionId": f"B{next(count)}"}}
        return answer(method, params, session)

    chromium._answer = _answer  # type: ignore[method-assign]


def test_the_gate_status_and_report_say_when_the_agent_connects_and_the_timeline_says_it_once(tmp_path):
    async def body(rig: Rig) -> None:
        _answering_browser_targets(rig)
        reports = _Reports(rig)
        gate = rig.gate
        reporter = asyncio.create_task(gate._reporter(rig.tunnel))  # type: ignore[arg-type]
        sender = asyncio.create_task(gate._send_actions(rig.tunnel))  # type: ignore[arg-type]
        try:
            status = gate.status()
            assert status["agent_connected_at"] is None and status["agent_connections"] == 0
            await _until(lambda: bool(reports.agents))
            assert reports.agents[-1] is not None and reports.agents[-1]["connected_at"] is None

            first = await gate.proxy.connect_agent(FakeAgent())
            assert first is not None
            await settle()
            status = gate.status()
            assert isinstance(status["agent_connected_at"], float)
            assert status["agent_connections"] == 1
            await _until(lambda: reports.agents[-1] is not None and reports.agents[-1]["connections"] == 1)
            assert reports.agents[-1]["connected_at"] == status["agent_connected_at"]

            second = await gate.proxy.connect_agent(FakeAgent())
            await gate.proxy.disconnect_agent(first)
            await gate.proxy.disconnect_agent(second)
            await settle()
            await _until(lambda: reports.agents[-1] is not None and reports.agents[-1]["connections"] == 0)
            # Still dated: the agent did use this browser.
            assert gate.status()["agent_connected_at"] == status["agent_connected_at"]
            assert gate.status()["agent_connections"] == 0

            await _until(lambda: "agent_connected" in rig.events())
            await settle()
            assert rig.events().count("agent_connected") == 1
        finally:
            reporter.cancel()
            sender.cancel()
            await asyncio.gather(reporter, sender, return_exceptions=True)

    _run(body, tmp_path)


def test_an_agent_that_connected_before_the_tunnel_is_put_on_the_timeline_when_it_opens(tmp_path):
    async def body(rig: Rig) -> None:
        _answering_browser_targets(rig)
        gate = rig.gate
        tunnel, gate.tunnel = gate.tunnel, None
        assert await gate.proxy.connect_agent(FakeAgent()) is not None
        await settle()
        assert "agent_connected" not in rig.events()
        gate.tunnel = tunnel
        sender = asyncio.create_task(gate._send_actions(tunnel))  # type: ignore[arg-type]
        try:
            gate._tell_agent_connected()
            gate._tell_agent_connected()
            await _until(lambda: "agent_connected" in rig.events())
            await settle()
            assert rig.events().count("agent_connected") == 1
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    _run(body, tmp_path)


def test_the_first_frame_sends_a_report_so_the_console_can_time_its_note(short_tmp: Path):
    """The console says "not using this browser yet" a minute after the first
    frame, so the first frame is reported the moment it is published -- not
    whenever the origin or the device next changes."""
    async def body() -> None:
        rig = MainRig(short_tmp)
        stop, task = await _started(rig)
        await rig.gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        await _until(lambda: len(_main_reports(rig)) >= 1)
        assert _main_reports(rig)[-1]["agent"]["first_frame_at"] is None
        rig.relay.push(tunnel.OP_OPEN, "v1")
        await _until(lambda: "first_frame" in rig.gate.status()["startup"])
        first = rig.gate.status()["startup"]["first_frame"]
        await _until(lambda: _main_reports(rig)[-1]["agent"]["first_frame_at"] == first)
        stop.set()
        assert await task == 0

    asyncio.run(body())
