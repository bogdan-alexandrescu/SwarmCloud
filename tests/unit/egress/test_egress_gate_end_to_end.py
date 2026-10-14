"""The gate's forwarder against the real swarm-egress (EGRESS-GATE; part of #1030).

docs/browser-egress.md §2.2: Chromium -> the gate's loopback forwarder ->
swarm-egress -> the vendor. Here the first two hops are the real code over
loopback and the vendor is test_egress_server's recording fake, so the test
holds what neither side's own tests can: the ticket the gate presents is one
swarm-egress accepts, the vendor gets the credential and never the ticket,
and a swarm-egress refusal or kill switch reaches the browser as a refusal
-- never as a direct connection.

Every key, password and ticket is built at run time.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

from agent_worker.livegate import egress as gate_egress

from test_egress_server import TARGET, Harness, Vendor

PROXY_ENV = {gate_egress.EGRESS_ENV: "proxy"}


def _run(tmp_path: Path, scenario, *, vendor: Vendor | None = None, **config):
    harness = Harness(tmp_path, vendor=vendor or Vendor(), **config)
    records: list[tuple[str, dict]] = []

    async def main() -> None:
        await harness.start()
        settings = gate_egress.EgressSettings.from_env(PROXY_ENV)
        dialled: list[tuple[str, int]] = []

        async def dial(host: str, port: int):
            dialled.append((host, port))
            return await asyncio.open_connection("127.0.0.1", harness.port)

        forwarder = gate_egress.LocalForwarder(
            settings, record=lambda e, f: records.append((e, dict(f))),
            peer_uid=lambda peer, local: os.getuid(), dial=dial, clock=harness.clock)
        await forwarder.start()
        try:
            await asyncio.wait_for(scenario(harness, forwarder), 20)
        finally:
            await forwarder.close()
            await harness.stop()
        assert set(dialled) <= {(settings.forwarder_host, settings.forwarder_port)}

    asyncio.run(main())
    return harness, records


async def _browser(forwarder: gate_egress.LocalForwarder) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", forwarder.port)
    writer.write(f"CONNECT {TARGET} HTTP/1.1\r\nHost: {TARGET}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return int(head.split(b" ", 2)[1]), reader, writer


def test_a_page_load_goes_gate_to_swarm_egress_to_the_vendor_with_the_credential_only_there(tmp_path):
    async def scenario(h: Harness, forwarder) -> None:
        forwarder.set_ticket(h.ticket(), 300)
        status, reader, writer = await _browser(forwarder)
        assert status == 200
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"ping"
        writer.close()

    harness, _ = _run(tmp_path, scenario)
    (head,) = harness.vendor.heads
    assert head.startswith(f"CONNECT {TARGET} ")
    # The vendor's authorization is swarm-egress's credential, added there.
    assert "proxy-authorization:" in head.lower()
    for ticket in harness.tickets:
        assert ticket not in head, "the attempt ticket never goes past swarm-egress"


def test_without_a_ticket_swarm_egress_is_never_asked_and_the_vendor_never_dialled(tmp_path):
    async def scenario(h: Harness, forwarder) -> None:
        status, _, writer = await _browser(forwarder)
        assert status == 503
        writer.close()

    harness, _ = _run(tmp_path, scenario)
    assert harness.dialled == [] and harness.vendor.heads == []


def test_the_kill_switch_reaches_the_browser_as_a_refusal_not_a_direct_connection(tmp_path):
    async def scenario(h: Harness, forwarder) -> None:
        forwarder.set_ticket(h.ticket(), 300)
        status, _, writer = await _browser(forwarder)
        assert status == 502
        writer.close()

    harness, records = _run(tmp_path, scenario, enabled=False)
    assert harness.dialled == []
    assert ("egress_refused", {"cause": "egress_disabled"}) in records


def test_a_ticket_swarm_egress_did_not_mint_is_refused(tmp_path):
    async def scenario(h: Harness, forwarder) -> None:
        forwarder.set_ticket(secrets.token_urlsafe(40) + "." + secrets.token_urlsafe(32), 300)
        status, _, writer = await _browser(forwarder)
        assert status == 502
        writer.close()

    harness, records = _run(tmp_path, scenario)
    assert harness.dialled == []
    causes = [c["cause"] for e, c in records if e == "egress_refused"]
    assert len(causes) == 1 and causes[0] in ("malformed", "bad_mac")
