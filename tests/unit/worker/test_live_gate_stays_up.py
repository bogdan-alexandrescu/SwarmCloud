"""The live gate stays usable after a refused operation, and says why when it is not (GATE-STAYS-UP; #2205).

MEASURED 2026-10-24 (task_23c48f150cbf4220a301): one `set_viewport_size` the
gate refused, then exit 5 `gate_unavailable` on every `swarm-handoff device`
and `theme` for the rest of the step, and a fresh CDP attach refused too. The
cause: every reply and event for an agent is sent from inside Chromium's one
pipe reader, and a send to an agent that had gone raised out of it -- the
reader ended, Chromium was never read again, every gate call (the device
switch, a new agent's `Target.attachToBrowserTarget`) waited for ever, and the
worker's ctl client timed out and closed its only channel for good.

Every test drives the real `CdpProxy`, `DeviceController`, `PipeTransport`,
`LiveGate` or ctl.sock pair against scripted peers (test_live_cdp_proxy's
`FakeChromium`, which pumps replies through one task exactly as the pipe
reader does). No Chromium, no network, no cloud credential.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from test_live_browser_relaunch import Rig
from test_live_cdp_proxy import FakeAgent, World, _Display, _Scale, settle
from test_live_device_recover import _until

from agent_worker.livegate import ctl
from agent_worker.livegate.cdpproxy import PipeTransport
from agent_worker.livegate.device import DeviceController

#: Long enough for anything in these tests that works; short next to a hang.
BOUND = 3.0


class GoneAgent(FakeAgent):
    """An agent whose process went after its last command: every send now fails
    as asyncio's `drain` does on a reset connection."""

    def __init__(self) -> None:
        super().__init__()
        self.gone = False

    async def send(self, message: dict[str, Any]) -> None:
        if self.gone:
            raise ConnectionResetError("Connection lost")
        await super().send(message)


class StalledAgent(FakeAgent):
    """An agent that stopped reading: a send never finishes once it has."""

    def __init__(self) -> None:
        super().__init__()
        self.stalled = False

    async def send(self, message: dict[str, Any]) -> None:
        if self.stalled:
            await asyncio.Event().wait()
        await super().send(message)


def run(body: Any) -> None:
    asyncio.run(asyncio.wait_for(body(), 20))


async def _refused_viewport_then_gone(w: World, agent: GoneAgent | StalledAgent) -> DeviceController:
    """The agent's page, the gate's device on it, then Playwright's
    `set_viewport_size` -- refused -- and the agent going away while Chromium
    still has events for its page."""
    w.agent = agent
    await w.connect()
    devices = DeviceController(w.proxy, _Display(), _Scale(), record=lambda k, f: None)
    page = await w.attach_page("T-page")
    await devices.attach_page("T-page")
    request = await w.send("Emulation.setDeviceMetricsOverride",
                           {"width": 800, "height": 600, "deviceScaleFactor": 1, "mobile": False}, page)
    reply = agent.response(request)
    assert reply is not None and "refused by the live gate" in reply["error"]["message"]
    assert "Emulation.setDeviceMetricsOverride" in w.refused()
    if isinstance(agent, GoneAgent):
        agent.gone = True
    else:
        agent.stalled = True
    # Chromium goes on talking to the page the agent held.
    w.chromium.emit("Page.lifecycleEvent", {"frameId": "F1", "name": "load"}, page)
    w.chromium.emit("Runtime.consoleAPICalled", {"type": "log", "args": []}, page)
    await settle()
    return devices


def test_a_refused_set_device_metrics_followed_by_a_device_preset_succeeds(tmp_path):
    async def body() -> None:
        w = World(tmp_path)
        devices = await _refused_viewport_then_gone(w, GoneAgent())
        device = await asyncio.wait_for(devices.select("phone-390"), BOUND)
        assert device.name == "phone-390"
        assert await asyncio.wait_for(devices.handle_theme({"theme": "dark"}), BOUND) == {"theme": "dark"}
        # Only the agent that went was dropped, and the record says so.
        assert ("cdp_agent_dropped", {"cause": "send_failed"}) in w.records
        assert w.conn.closed and w.proxy.agent_connections == 0
        await w.chromium.stop()

    run(body)


def test_a_refused_op_followed_by_a_fresh_cdp_attach_succeeds(tmp_path):
    async def body() -> None:
        w = World(tmp_path)
        await _refused_viewport_then_gone(w, GoneAgent())
        fresh = FakeAgent()
        connection = await asyncio.wait_for(w.proxy.connect_agent(fresh), BOUND)
        assert connection is not None
        # And it is served: its command reaches Chromium and is answered.
        w.agent, w.conn = fresh, connection
        request = await w.send("Browser.getVersion", {})
        assert fresh.response(request) is not None and "error" not in fresh.response(request)
        await w.chromium.stop()

    run(body)


def test_an_agent_that_stops_reading_is_dropped_and_the_device_still_switches(tmp_path):
    async def body() -> None:
        w = World(tmp_path, agent_send_seconds=0.05)
        devices = await _refused_viewport_then_gone(w, StalledAgent())
        assert (await asyncio.wait_for(devices.select("phone-390"), BOUND)).name == "phone-390"
        assert ("cdp_agent_dropped", {"cause": "send_stalled"}) in w.records
        assert await asyncio.wait_for(w.proxy.connect_agent(FakeAgent()), BOUND) is not None
        await w.chromium.stop()

    run(body)


def test_the_pipe_reader_reads_on_after_a_handler_raises(tmp_path):
    """`PipeTransport.run` over a real pipe: a sink that raises on one message
    is still handed the next."""

    async def body() -> None:
        to_gate, from_chromium = os.pipe()
        to_chromium, unused = os.pipe()
        transport = PipeTransport(read_fd=to_gate, write_fd=unused)
        await transport.open()
        seen: list[int] = []

        class Sink:
            async def on_chromium_message(self, message: dict[str, Any]) -> None:
                seen.append(message["id"])
                if message["id"] == 1:
                    raise ConnectionResetError("Connection lost")

        reader = asyncio.create_task(transport.run(Sink()))
        os.write(from_chromium, b'{"id":1,"result":{}}\0{"id":2,"result":{}}\0')
        await _until(lambda: seen == [1, 2], BOUND)
        assert not reader.done()
        os.close(from_chromium)
        await asyncio.wait_for(reader, BOUND)
        await transport.close()
        os.close(to_chromium)

    run(body)


# --------------------------------------------------------------------------
# A browser that is not there says why (`GateDown`), through ctl.sock
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state,closed,reason", [
    ("restarting", True, "restarting"),
    ("running", True, "restarting"),  # exited; the relaunch is being decided
    ("closed", True, "crashed"),
])
def test_a_device_or_theme_while_the_browser_is_down_is_refused_with_why(tmp_path, state, closed, reason):
    async def main() -> None:
        rig = Rig(tmp_path)
        try:
            await rig.start()
            gate = rig.gate
            gate.browser_state, gate.browser_closed = state, closed
            for op, args in (("device", {"preset": "phone-390"}), ("theme", {"theme": "dark"})):
                with pytest.raises(ctl.OpRefused) as refused:
                    await gate.handle_ctl(op, args)
                assert refused.value.code == reason
            gate.wiped = True
            with pytest.raises(ctl.OpRefused) as refused:
                await gate.handle_ctl("device", {"preset": "phone-390"})
            assert refused.value.code == "wiped"
        finally:
            await rig.stop()

    asyncio.run(asyncio.wait_for(main(), 20))


def test_a_device_before_the_first_browser_is_up_is_refused_as_starting(tmp_path):
    async def main() -> None:
        rig = Rig(tmp_path)
        with pytest.raises(ctl.OpRefused) as refused:
            await rig.gate.handle_ctl("device", {"preset": "phone-390"})
        assert refused.value.code == "starting"

    asyncio.run(main())


@pytest.fixture()
def short_dir():
    """A directory whose socket paths fit in sun_path's 108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="lg", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class SlowGate:
    """ctl.sock's server over a handler whose device switch takes `device_seconds`."""

    def __init__(self, root: Path, *, device_seconds: float, op_seconds: float | None = None,
                 down: str | None = None) -> None:
        self.sock = root / "ctl.sock"
        self.marker = root / "gate-started"
        self.device_seconds = device_seconds
        self.down = down
        self.records: list[tuple[str, dict[str, str]]] = []
        self.finished: list[str] = []
        bound = {} if op_seconds is None else {"op_seconds": op_seconds}
        self.server = ctl.CtlServer(str(self.sock), str(self.marker), self.handler,
                                    lambda e, f: self.records.append((e, dict(f))), **bound)

    async def handler(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "device":
            if self.down is not None:
                raise ctl.GateDown(self.down)
            await asyncio.sleep(self.device_seconds)
            self.finished.append(op)
        return {"op": op}


def test_a_device_switch_the_browser_does_not_finish_is_answered_unresponsive_and_the_channel_lives(short_dir):
    async def main() -> None:
        gate = SlowGate(short_dir, device_seconds=0.6, op_seconds=0.1)
        await gate.server.start()
        worker = ctl.CtlClient(str(gate.sock), timeout=5)
        await asyncio.to_thread(worker.register)
        with pytest.raises(ctl.CtlRefused) as refused:
            await asyncio.to_thread(worker.call, "device", {"preset": "phone-390"})
        assert refused.value.error == "refused:unresponsive"
        assert ("gate_ctl_slow", {"op": "device"}) in gate.records
        # The next command is read and answered at once: nothing waits on the switch.
        assert await asyncio.wait_for(asyncio.to_thread(worker.call, "status", {}), BOUND) == {"op": "status"}
        # And the switch was let finish, not cancelled half-way.
        await _until(lambda: gate.finished == ["device"], BOUND)
        await asyncio.to_thread(worker.close)
        await gate.server.close()

    asyncio.run(main())


def test_a_call_the_client_gave_up_on_keeps_the_channel_and_its_late_answer_is_skipped(short_dir):
    async def main() -> None:
        gate = SlowGate(short_dir, device_seconds=0.6, op_seconds=30)
        await gate.server.start()
        worker = ctl.CtlClient(str(gate.sock), timeout=0.2)
        await asyncio.to_thread(worker.register)
        with pytest.raises(ctl.CtlTimeout):
            await asyncio.to_thread(worker.call, "device", {"preset": "phone-390"})
        await _until(lambda: gate.finished == ["device"], BOUND)
        # The device's late reply is on the wire first; the status reply is the one returned.
        assert await asyncio.to_thread(worker.call, "status", {}) == {"op": "status"}
        assert await asyncio.to_thread(worker.call, "theme", {"theme": "dark"}) == {"op": "theme"}
        await asyncio.to_thread(worker.close)
        await gate.server.close()

    asyncio.run(main())


def test_a_browser_that_is_down_is_refused_with_its_reason_on_the_wire(short_dir):
    async def main() -> None:
        gate = SlowGate(short_dir, device_seconds=0, down="restarting")
        await gate.server.start()
        worker = ctl.CtlClient(str(gate.sock), timeout=5)
        await asyncio.to_thread(worker.register)
        with pytest.raises(ctl.CtlRefused) as refused:
            await asyncio.to_thread(worker.call, "device", {"preset": "phone-390"})
        assert refused.value.error == "refused:restarting"
        assert await asyncio.to_thread(worker.call, "status", {}) == {"op": "status"}
        await asyncio.to_thread(worker.close)
        await gate.server.close()

    asyncio.run(main())
