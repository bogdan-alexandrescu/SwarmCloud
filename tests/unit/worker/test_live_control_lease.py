"""The gate keeps its own control lease, and a refused agent is told why (CONTROL-LAPSE, part of #1030).

MEASURED 2026-10-13 (task e8cb...): the console's control lapsed at 15:11:12Z
and swarm-api recorded control returned, but the gate held control until a
relay restart at 16:03:47Z -- 72 `cdp_connection_refused` in 54 minutes, each
a bare "a person is in control of the browser" (3458 g2: 44 in 8 min; 8fcb
g4: 12). These tests hold the gate's half:

* control the relay stops renewing ends at the gate by itself after the
  lease, with `control_ended` cause `lease_expired`, and the agent resumes;
* a renewal -- the relay naming the controller again -- keeps it, and is not
  a second grant;
* swarm-api's revoke ends control with its cause, and the agent's CDP
  connection is accepted after it;
* a refused agent connection says why and since when, and asks it to retry
  later, over the CDP endpoint's own HTTP.

Offline, over test_livegate_main's fakes. Every ticket is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from test_live_cdp_proxy import World
from test_livegate_main import Rig, _started, _until, short_tmp  # noqa: F401  (fixture)

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import cdpproxy, tunnel

#: The production lease (read by name, so this file imports before it exists).
LEASE = getattr(gate_main, "CONTROL_LEASE_SECONDS", 50.0)


class Agent:
    """An agent's CDP connection, as the proxy writes to it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


def _rig(tmp, lease: float) -> tuple[Rig, list[tuple[str, dict[str, Any]]]]:
    rig = Rig(tmp)
    records: list[tuple[str, dict[str, Any]]] = []
    rig.gate = gate_main.LiveGate(rig.cfg, rig.deps, control_lease_seconds=lease,
                                  record=lambda event, fields: records.append((event, dict(fields))))
    return rig, records


def _control_actions(rig: Rig) -> list[tuple[str, Any]]:
    actions = [json.loads(f[2]) for f in rig.relay.frames if f[0] == tunnel.OP_ACTION]
    return [(a["event"], a.get("cause")) for a in actions if a.get("kind") == "control"]


async def _granted(rig: Rig) -> None:
    gate = rig.gate
    await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
    rig.relay.push(tunnel.OP_OPEN, "v1")
    await _until(lambda: gate.viewers == 1)
    rig.relay.push(tunnel.OP_CONTROL, "v1")
    await _until(lambda: gate.control.controller == "v1")
    assert gate.proxy.paused and gate.proxy.in_control


def test_control_with_no_renewal_ends_at_the_gate_when_its_lease_runs_out(short_tmp):
    async def body() -> None:
        rig, records = _rig(short_tmp, lease=0.3)
        stop, task = await _started(rig)
        gate = rig.gate
        await _granted(rig)
        refused = await gate.proxy.connect_agent(Agent())
        assert refused is None, "the agent waits while a person is in control"

        await _until(lambda: gate.control.controller is None and not gate.proxy.paused, timeout=3.0)
        assert ("control_ended", {"cause": "lease_expired"}) in records
        assert ("ended", "lease_expired") in _control_actions(rig)
        # The agent has its browser back.
        assert await gate.proxy.connect_agent(Agent()) is not None
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_renewal_keeps_control_and_is_not_a_second_grant(short_tmp):
    async def body() -> None:
        rig, records = _rig(short_tmp, lease=0.4)
        stop, task = await _started(rig)
        gate = rig.gate
        await _granted(rig)
        for _ in range(8):  # 0.8 s: twice the lease, renewed every 0.1 s
            await asyncio.sleep(0.1)
            rig.relay.push(tunnel.OP_CONTROL, "v1")
        await asyncio.sleep(0.05)
        assert gate.control.controller == "v1" and gate.proxy.in_control
        assert _control_actions(rig) == [("granted", None)]
        assert not [r for r in records if r[0] == "control_lease_expired"]
        # And when the renewals stop, the lease runs out.
        await _until(lambda: gate.control.controller is None, timeout=3.0)
        assert ("ended", "lease_expired") in _control_actions(rig)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_swarm_apis_revoke_ends_control_with_its_cause_and_the_agent_is_let_back_in(short_tmp):
    async def body() -> None:
        rig, records = _rig(short_tmp, lease=LEASE)
        stop, task = await _started(rig)
        gate = rig.gate
        await _granted(rig)
        assert await gate.proxy.connect_agent(Agent()) is None
        rig.relay.push(tunnel.OP_CONTROL, "", b"renew_lapsed")
        await _until(lambda: gate.control.controller is None and not gate.proxy.paused)
        assert ("control_ended", {"cause": "renew_lapsed"}) in records
        assert _control_actions(rig)[-1] == ("ended", "renew_lapsed")
        assert await gate.proxy.connect_agent(Agent()) is not None
        # Idempotent: a second revoke of nobody changes nothing.
        rig.relay.push(tunnel.OP_CONTROL, "", b"renew_lapsed")
        await asyncio.sleep(0.05)
        assert [a for a in _control_actions(rig) if a[0] == "ended"] == [("ended", "renew_lapsed")]
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_revoke_cause_that_is_not_a_code_is_a_plain_release():
    assert gate_main._revoke_cause(b"") == "released"
    assert gate_main._revoke_cause(b"renew_lapsed") == "renew_lapsed"
    assert gate_main._revoke_cause(b"Not A Code") == "released"
    assert gate_main._revoke_cause(b"\xff") == "released"
    assert gate_main._revoke_cause(b"x" * 64) == "released"


def test_the_refusal_says_a_person_is_in_control_and_since_when(short_tmp):
    async def body() -> None:
        rig, records = _rig(short_tmp, lease=LEASE)
        stop, task = await _started(rig)
        gate = rig.gate
        await _granted(rig)
        assert await gate.proxy.connect_agent(Agent()) is None
        cause, reason = gate.proxy.refusal()
        assert cause == "person_in_control"
        assert "a person is in control of this browser since " in reason
        assert "Z (" in reason and " s)" in reason
        assert f"{int(LEASE)} s after their console stops renewing" in reason
        assert f"Retry in {cdpproxy.RETRY_AFTER_SECONDS} s" in reason
        refused = [f for e, f in records if e == "cdp_connection_refused"]
        assert refused and refused[-1]["cause"] == "person_in_control"
        assert isinstance(refused[-1]["held_seconds"], int)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_refused_connection_is_told_why_over_http(tmp_path):
    """What Playwright's connectOverCDP reads: the status line and the body."""

    async def body() -> None:
        w = World(tmp_path)
        await w.proxy.pause()
        server = await cdpproxy.serve_agents(w.proxy, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            version = json.loads((await reader.read()).split(b"\r\n\r\n", 1)[1])
            writer.close()
            path = version["webSocketDebuggerUrl"].split(f"{port}", 1)[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Key: " + secrets.token_hex(8)
                          + "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            answer = (await reader.read()).decode()
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
        head, _, text = answer.partition("\r\n\r\n")
        assert head.startswith("HTTP/1.1 503 ")
        assert f"Retry-After: {cdpproxy.RETRY_AFTER_SECONDS}" in head
        assert "the agent is held while the browser is handed to a person since " in text
        await w.chromium.stop()

    asyncio.run(body())


def test_a_gate_stopped_while_a_person_holds_control_stops_and_ends_the_episode(short_tmp):
    """The task ends with a person in control (task c6bf0368...: taken
    11:34:19, SUCCEEDED 11:40:50 with the episode open). The gate's stop used
    to wait forever on resuming the agent through a pipe whose reader it had
    already cancelled, so it never stopped and the episode never ended."""

    async def body() -> None:
        rig, records = _rig(short_tmp, lease=LEASE)
        stop, task = await _started(rig)
        gate = rig.gate
        await _granted(rig)
        stop.set()
        assert await asyncio.wait_for(task, 10) == 0
        assert gate.control.controller is None
        assert ("control_ended", {"cause": "tunnel_closed"}) in records

    asyncio.run(body())
