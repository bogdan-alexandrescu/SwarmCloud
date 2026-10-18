"""The RFB handshake reaches a viewer through the tunnel's writer.

`tunnel.TunnelWriter` sends nothing on `write()`: bytes leave as one DATA
frame on `drain()`. `test_live_rfb.py` drives `RfbSession` over a socket
pair, whose writer sends on `write()`, so it cannot see a handshake step
that writes and then waits to read without draining.

That is what happened on SwarmCloud on 2026-10-12 (task_57efee1ee7354f3789d9):
a viewer received "RFB 003.008" and the security types, chose None, and never
received the SecurityResult, because the gate wrote it, did not drain, and
waited for ClientInit -- which the viewer only sends after the
SecurityResult. Both sides waited, and the Browser tab showed "Connecting to
the agent's browser" for good.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any

from agent_worker.livegate import rfb, tunnel


class _NoInput:
    """An injector the handshake never reaches: no input is sent before ServerInit."""

    def __getattr__(self, name: str) -> Any:
        async def nothing(*_args: Any, **_kwargs: Any) -> None:
            return None

        return nothing


class _Tunnel:
    """Records what the gate puts on the tunnel, as the relay would receive it."""

    def __init__(self) -> None:
        self.sent = bytearray()
        self.arrived = asyncio.Event()

    async def send(self, op: int, conn_id: str, payload: bytes = b"") -> None:
        assert op == tunnel.OP_DATA
        self.sent += payload
        self.arrived.set()


async def _received(wire: _Tunnel, want: bytes, timeout: float = 2.0) -> bool:
    async def wait() -> None:
        while want not in bytes(wire.sent):
            wire.arrived.clear()
            await wire.arrived.wait()

    try:
        await asyncio.wait_for(wait(), timeout)
    except asyncio.TimeoutError:
        return False
    return True


def test_the_security_result_reaches_the_viewer_before_the_gate_waits_for_client_init() -> None:
    async def run() -> None:
        wire = _Tunnel()
        writer = tunnel.TunnelWriter(wire, "conn-1")  # type: ignore[arg-type]
        reader = asyncio.StreamReader()
        hub = rfb.FrameHub(64, 40)
        session = rfb.RfbSession("conn-1", reader, writer, hub, rfb.ControlState(_NoInput()), _NoInput())  # type: ignore[arg-type]
        serving = asyncio.create_task(session.serve())
        try:
            assert await _received(wire, rfb.PROTOCOL_VERSION), "the gate never sent its protocol version"
            reader.feed_data(rfb.PROTOCOL_VERSION)
            assert await _received(wire, bytes([1, 1])), "the gate never offered security type None"
            before = len(wire.sent)
            reader.feed_data(bytes([1]))  # the viewer chooses None
            # A real viewer sends ClientInit only after it has read the
            # SecurityResult, so the gate must have sent it by now -- not
            # merely written it into a buffer nobody drains.
            got = await _received(wire, struct.pack(">I", 0), timeout=2.0)
            assert got and len(wire.sent) > before, (
                "the SecurityResult was written but never drained to the tunnel: "
                "the viewer waits for it, and the gate waits for ClientInit")
            reader.feed_data(bytes([1]))  # ClientInit, shared
            server_init = struct.pack(">HH", 64, 40)
            assert await _received(wire, server_init), "ServerInit never reached the viewer"
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    asyncio.run(run())


def test_a_refused_security_type_is_drained_to_the_viewer_before_the_connection_ends() -> None:
    async def run() -> None:
        wire = _Tunnel()
        writer = tunnel.TunnelWriter(wire, "conn-2")  # type: ignore[arg-type]
        reader = asyncio.StreamReader()
        session = rfb.RfbSession("conn-2", reader, writer, rfb.FrameHub(64, 40),
                                 rfb.ControlState(_NoInput()), _NoInput())  # type: ignore[arg-type]
        serving = asyncio.create_task(session.serve())
        try:
            assert await _received(wire, rfb.PROTOCOL_VERSION)
            reader.feed_data(rfb.PROTOCOL_VERSION)
            assert await _received(wire, bytes([1, 1]))
            reader.feed_data(bytes([2]))  # VNC authentication: not offered
            assert await _received(wire, struct.pack(">I", 1)), (
                "the failed SecurityResult was never drained, so the viewer cannot tell why it was refused")
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    asyncio.run(run())
