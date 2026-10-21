"""swarm-egress, the forwarder that holds the vendor credential
(docs/browser-egress.md §2.2, §3.1, §3.5; lane EGRESS-SVC, part of #1030).

Drives the real CONNECT server over loopback against a fake vendor proxy that
records what it was sent. The lane's acceptance, one test or more each:

  * a CONNECT without a valid attempt ticket is refused, and the vendor is
    never dialled;
  * the vendor gets the credential and the sticky US session; the browser's
    ticket never goes past swarm-egress;
  * the bypass list and internal names are refused;
  * the caps: per attempt (closes the tunnel mid-stream), per tenant and for
    the platform per month, from a byte ledger that survives a restart;
  * the kill switch fails closed: off refuses every new CONNECT and closes
    the open tunnels, and a configuration that cannot be read is off;
  * a stale generation gets no egress, and a newer one closes the older
    generation's tunnels (invariant 5);
  * no log line carries the password, the username, the ticket or the
    CONNECT host.

Nothing needs a vendor, a cloud or a credential: every password and key is
built at runtime.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

import pytest

from swarm_egress.config import EgressConfig, parse_config
from swarm_egress.ledger import Ledger
from swarm_egress.server import EgressServer, KeyRing
from swarm_egress.ticket import mint_ticket
from swarm_egress.vendor import parse_credential

TARGET = "www.example-search.test:443"
TARGET_HOST = "www.example-search.test"
LOGGER = "swarm_egress"


class Clock:
    def __init__(self) -> None:
        self.at = time.time()

    def __call__(self) -> float:
        return self.at


class Vendor:
    """The vendor's proxy: records each CONNECT head it is sent, answers
    `status`, then echoes, or first sends `blast` bytes."""

    def __init__(self, status: int = 200, blast: int = 0) -> None:
        self.status = status
        self.blast = blast
        self.heads: list[str] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head.decode("latin-1"))
            reason = "OK" if self.status == 200 else "Refused"
            writer.write(f"HTTP/1.1 {self.status} {reason}\r\n\r\n".encode())
            await writer.drain()
            if self.status != 200:
                writer.close()
                return
            if self.blast:
                writer.write(b"x" * self.blast)
                await writer.drain()
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()


class Harness:
    def __init__(self, tmp_path: Path, *, vendor: Vendor, **config: Any) -> None:
        self.key = secrets.token_hex(32).encode()
        self.password = secrets.token_hex(12)
        self.username = "brd-customer-c1-zone-res"
        self.credential = parse_credential(json.dumps({
            "vendor": "brightdata", "host": "brd.superproxy.io", "port": 44445, "tls": False,
            "zones": {
                "residential": {"username": self.username, "password": self.password,
                                "sticky": "attempt"},
                "isp": {"username": self.username + "-isp", "password": self.password,
                        "sticky": "task"},
            },
        }).encode())
        body = {
            "enabled": True, "country": "us", "tenants": ["eng", "research"],
            "default_zone": "residential", "tenant_zones": {},
            "monthly_cap_bytes": 10**9, "tenant_monthly_cap_bytes": 10**9,
            "attempt_cap_bytes": 10**8, "connects_per_minute": 600,
            "max_tunnels_per_attempt": 64,
            "bypass_hosts": ["github.com", ".github.com"],
        }
        body.update(config)
        self.config: EgressConfig = parse_config(body)
        self.ledger_path = tmp_path / "usage.json"
        self.clock = Clock()
        self.ledger = Ledger(self.ledger_path, clock=self.clock)
        self.vendor = vendor
        self.dialled: list[tuple[str, int, bool]] = []
        self.has_credential = True
        self.tickets: list[str] = []
        self.server = EgressServer(
            keys=KeyRing(self.key),
            credential=lambda: self.credential if self.has_credential else None,
            config=lambda: self.config,
            ledger=self.ledger,
            dial=self._dial,
            clock=self.clock,
        )
        self.listener: asyncio.base_events.Server | None = None
        self.port = 0

    async def _dial(self, host: str, port: int, tls: bool):
        self.dialled.append((host, port, tls))
        return await asyncio.open_connection("127.0.0.1", self.vendor.port)

    async def start(self) -> None:
        await self.vendor.start()
        self.listener = await asyncio.start_server(self.server.handle, "127.0.0.1", 0)
        self.port = self.listener.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        await self.server.close_all("shutdown")
        if self.listener is not None:
            self.listener.close()
        await self.vendor.stop()

    def ticket(self, *, tenant: str = "eng", task: str = "task_1", attempt: str = "att_1",
               generation: int = 1, seconds: int = 300) -> str:
        ticket = mint_ticket(self.key, tenant_id=tenant, task_id=task, attempt_id=attempt,
                             generation=generation, exp=int(self.clock()) + seconds)
        self.tickets.append(ticket)
        return ticket

    async def connect(self, target: str = TARGET, ticket: str | None = None, *,
                      method: str = "CONNECT"):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        head = f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n"
        if ticket is not None:
            head += f"Proxy-Authorization: Bearer {ticket}\r\n"
        writer.write((head + "\r\n").encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        status = int(response.split(b" ", 2)[1])
        return status, reader, writer


def run(tmp_path: Path, scenario, *, vendor: Vendor | None = None, **config: Any) -> Harness:
    harness = Harness(tmp_path, vendor=vendor or Vendor(), **config)

    async def main() -> None:
        await harness.start()
        try:
            await asyncio.wait_for(scenario(harness), 20)
        finally:
            await harness.stop()

    asyncio.run(main())
    return harness


async def _closed(reader: asyncio.StreamReader) -> bool:
    """True when the tunnel is closed from swarm-egress's side."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), 5)
            if not data:
                return True
    except (ConnectionError, asyncio.IncompleteReadError):
        return True


def _events(caplog) -> list[dict[str, Any]]:
    out = []
    for record in caplog.records:
        if record.name.startswith(LOGGER):
            out.append(json.loads(record.getMessage()))
    return out


# -- the ticket ------------------------------------------------------------------


def test_a_connect_without_a_ticket_is_refused_and_the_vendor_is_never_dialled(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect(ticket=None)
        assert status == 407
        writer.close()

    harness = run(tmp_path, scenario)
    assert harness.dialled == [] and harness.vendor.heads == []
    assert {"event": "egress_refused", "cause": "no_ticket", "status": 407}.items() <= _events(caplog)[-1].items()


def test_a_ticket_under_another_key_or_expired_is_refused(tmp_path):
    async def scenario(h: Harness) -> None:
        foreign = mint_ticket(secrets.token_hex(32).encode(), tenant_id="eng", task_id="t",
                              attempt_id="a", generation=1, exp=int(h.clock()) + 60)
        status, _, writer = await h.connect(ticket=foreign)
        assert status == 407
        writer.close()
        expired = h.ticket(seconds=1)
        h.clock.at += 2
        status, _, writer = await h.connect(ticket=expired)
        assert status == 407
        writer.close()

    harness = run(tmp_path, scenario)
    assert harness.dialled == []


def test_only_connect_is_served(tmp_path):
    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect("http://www.example-search.test/", h.ticket(),
                                            method="GET")
        assert status == 405
        writer.close()

    assert run(tmp_path, scenario).dialled == []


def test_a_good_ticket_gets_a_tunnel_through_the_vendor_with_the_credential_and_the_session(tmp_path):
    async def scenario(h: Harness) -> None:
        status, reader, writer = await h.connect(ticket=h.ticket())
        assert status == 200
        writer.write(b"hello through the tunnel")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(24), 5) == b"hello through the tunnel"
        writer.close()

    harness = run(tmp_path, scenario)
    assert harness.dialled == [("brd.superproxy.io", 44445, False)]
    head = harness.vendor.heads[0]
    assert head.startswith(f"CONNECT {TARGET} HTTP/1.1\r\n")
    auth = [line for line in head.split("\r\n") if line.lower().startswith("proxy-authorization:")]
    assert len(auth) == 1
    user, password = base64.b64decode(auth[0].split()[-1]).decode().split(":", 1)
    assert password == harness.password
    assert user.startswith(harness.username + "-country-us-session-")
    # The browser's ticket stops at swarm-egress.
    assert harness.tickets[0] not in head
    assert harness.ledger.attempt_bytes("att_1") >= 48


def test_a_tenant_zone_selects_its_credential(tmp_path):
    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect(ticket=h.ticket(tenant="research"))
        assert status == 200
        writer.close()

    harness = run(tmp_path, scenario, tenant_zones={"research": "isp"})
    auth = [line for line in harness.vendor.heads[0].split("\r\n")
            if line.lower().startswith("proxy-authorization:")][0]
    user = base64.b64decode(auth.split()[-1]).decode().split(":", 1)[0]
    assert user.startswith(harness.username + "-isp-country-us-session-")


# -- the bypass list and the target --------------------------------------------------


@pytest.mark.parametrize("target", ["github.com:443", "api.github.com:443", "10.1.2.3:443",
                                    "swarm-api.swarm-system.svc.cluster.local:443",
                                    "www.example-search.test:80"])
def test_bypassed_internal_and_non_https_targets_are_refused(tmp_path, target):
    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect(target, h.ticket())
        assert status == 403
        writer.close()

    assert run(tmp_path, scenario).dialled == []


# -- the kill switch -------------------------------------------------------------


def test_the_switch_off_refuses_every_new_connect(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 403
        writer.close()

    harness = run(tmp_path, scenario, enabled=False)
    assert harness.dialled == []
    assert _events(caplog)[-1]["cause"] == "egress_disabled"


def test_switching_off_closes_the_open_tunnels_fail_closed(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario(h: Harness) -> None:
        status, reader, writer = await h.connect(ticket=h.ticket())
        assert status == 200
        h.config = EgressConfig(enabled=False, cause="disabled")
        await h.server.supervise_once()
        assert await _closed(reader)
        assert h.server.open_tunnels == 0
        writer.close()

    run(tmp_path, scenario)
    closed = [e for e in _events(caplog) if e["event"] == "egress_tunnel_closed"]
    assert closed and closed[-1]["cause"] == "egress_disabled"


def test_a_configuration_that_cannot_be_read_is_off(tmp_path):
    async def scenario(h: Harness) -> None:
        h.config = EgressConfig(enabled=False, cause="config_unreadable")
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 403
        writer.close()

    assert run(tmp_path, scenario).dialled == []


def test_a_tenant_not_switched_on_is_refused(tmp_path):
    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect(ticket=h.ticket(tenant="sales"))
        assert status == 403
        writer.close()

    assert run(tmp_path, scenario).dialled == []


def test_no_credential_is_refused_and_nothing_is_dialled(tmp_path):
    async def scenario(h: Harness) -> None:
        h.has_credential = False
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 503
        writer.close()

    assert run(tmp_path, scenario).dialled == []


# -- fencing (invariant 5) ------------------------------------------------------------


def test_a_stale_generation_gets_no_egress_and_a_newer_one_closes_the_older_tunnels(tmp_path):
    async def scenario(h: Harness) -> None:
        status, old_reader, old_writer = await h.connect(ticket=h.ticket(generation=3))
        assert status == 200
        status, _, writer = await h.connect(ticket=h.ticket(generation=4, attempt="att_2"))
        assert status == 200
        assert await _closed(old_reader)
        status, _, stale = await h.connect(ticket=h.ticket(generation=3))
        assert status == 403
        for w in (old_writer, writer, stale):
            w.close()

    assert len(run(tmp_path, scenario).dialled) == 2


def test_a_tunnel_ends_when_its_ticket_expires(tmp_path):
    async def scenario(h: Harness) -> None:
        status, reader, writer = await h.connect(ticket=h.ticket(seconds=60))
        assert status == 200
        h.clock.at += 61
        await h.server.supervise_once()
        assert await _closed(reader)
        writer.close()

    run(tmp_path, scenario)


# -- caps ----------------------------------------------------------------------


def test_the_attempt_cap_closes_the_tunnel_mid_stream_and_refuses_the_next(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario(h: Harness) -> None:
        status, reader, writer = await h.connect(ticket=h.ticket())
        assert status == 200
        received = 0
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), 5)
                if not data:
                    break
                received += len(data)
        except ConnectionError:
            pass
        assert received < 400_000
        writer.close()
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 403
        writer.close()

    run(tmp_path, scenario, vendor=Vendor(blast=2_000_000), attempt_cap_bytes=100_000)
    events = _events(caplog)
    assert any(e["event"] == "egress_tunnel_closed" and e["cause"] == "attempt_cap" for e in events)
    assert events[-1]["cause"] == "attempt_cap"


def test_the_tenant_monthly_cap_refuses_that_tenant_only(tmp_path):
    async def scenario(h: Harness) -> None:
        h.ledger.add("eng", "att_old", 5_000)
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 403
        writer.close()
        status, _, writer = await h.connect(ticket=h.ticket(tenant="research", task="t2",
                                                             attempt="a2"))
        assert status == 200
        writer.close()

    run(tmp_path, scenario, tenant_monthly_cap_bytes=4_000)


def test_the_platform_monthly_cap_refuses_everyone(tmp_path):
    async def scenario(h: Harness) -> None:
        h.ledger.add("research", "att_old", 5_000)
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 403
        writer.close()

    run(tmp_path, scenario, monthly_cap_bytes=4_000)


def test_the_byte_ledger_survives_a_restart_and_starts_again_each_month(tmp_path):
    clock = Clock()
    clock.at = 1_790_000_000.0  # 2026-09-21
    ledger = Ledger(tmp_path / "usage.json", clock=clock)
    ledger.add("eng", "att_1", 1234)
    ledger.flush()
    again = Ledger(tmp_path / "usage.json", clock=clock)
    assert (again.platform_bytes(), again.tenant_bytes("eng"), again.attempt_bytes("att_1")) == (
        1234, 1234, 1234)
    clock.at += 40 * 86400  # next month
    assert again.platform_bytes() == 0 and again.tenant_bytes("eng") == 0


def test_a_ledger_that_cannot_be_read_fails_closed(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{not json")
    ledger = Ledger(path, clock=Clock())
    config = parse_config({"enabled": True, "monthly_cap_bytes": 10**9,
                           "tenant_monthly_cap_bytes": 10**9, "attempt_cap_bytes": 10**8})
    assert ledger.over_cap("eng", "att_1", config) == "ledger_unreadable"


def test_new_connects_are_rate_limited_per_attempt(tmp_path):
    async def scenario(h: Harness) -> None:
        ticket = h.ticket()
        statuses = []
        for _ in range(3):
            status, _, writer = await h.connect(ticket=ticket)
            statuses.append(status)
            writer.close()
        assert statuses == [200, 200, 429]

    run(tmp_path, scenario, connects_per_minute=2)


def test_the_vendor_refusing_is_a_502_and_recorded(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario(h: Harness) -> None:
        status, _, writer = await h.connect(ticket=h.ticket())
        assert status == 502
        writer.close()

    run(tmp_path, scenario, vendor=Vendor(status=407))
    events = _events(caplog)
    assert any({"event": "egress_vendor_refused", "status": 407}.items() <= e.items() for e in events)
    assert events[-1]["cause"] == "vendor_refused" and events[-1]["status"] == 502


# -- the head: a probe is not a request -----------------------------------------------
#
# Lane EGRESS-PROBE-QUIET (part of #1822): the manifest's tcpSocket readiness
# (every 5 s) and liveness (every 10 s) probes, and the live gate's forwarder
# probe, open a connection and close it without a byte. The 2026-10-21 capture
# logged 18 a minute as `egress_refused` cause `head`, drowning the real ones.


async def _raw(h: Harness, data: bytes, *, eof: bool = True) -> bytes:
    """Send `data` (then half-close when `eof`) and return everything
    swarm-egress answers before it closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", h.port)
    if data:
        writer.write(data)
        await writer.drain()
    if eof:
        writer.write_eof()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return answer


def _refusals(caplog) -> list[dict[str, Any]]:
    return [e for e in _events(caplog) if e["event"] == "egress_refused"]


def test_a_connection_that_closes_without_a_byte_is_not_a_refusal(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    answers: list[bytes] = []

    async def scenario(h: Harness) -> None:
        for _ in range(3):
            answers.append(await _raw(h, b""))
        # A real refusal after the probes, so an empty log cannot pass this test.
        status, _, writer = await h.connect(ticket=None)
        assert status == 407
        writer.close()

    harness = run(tmp_path, scenario)
    # MUTATION: raising Refusal(400, "head") for an empty IncompleteReadError
    # again answers each probe with a 400 and these fail.
    assert answers == [b"", b"", b""]
    # MUTATION: recording a zero-byte connection as `egress_refused` makes this
    # list four long, three of them cause `head`.
    assert [(e["cause"], e["status"]) for e in _refusals(caplog)] == [("no_ticket", 407)]
    assert harness.dialled == []


@pytest.mark.parametrize("data", [
    pytest.param(b"CONNECT " + TARGET.encode() + b" HTTP/1.1\r\nHost: x\r\n", id="partial"),
    pytest.param(b"C", id="one-byte"),
    pytest.param(b"GARBAGE\r\n\r\n", id="malformed-request-line"),
    pytest.param(b"CONNECT " + TARGET.encode() + b" HTTP/1.1\r\nX-Pad: "
                 + b"a" * 9000 + b"\r\n\r\n", id="oversized"),
])
def test_a_partial_malformed_or_oversized_head_is_still_refused(tmp_path, caplog, data):
    caplog.set_level(logging.INFO, logger=LOGGER)
    answers: list[bytes] = []

    async def scenario(h: Harness) -> None:
        answers.append(await _raw(h, data))

    harness = run(tmp_path, scenario)
    # MUTATION: treating any IncompleteReadError (not only one with no bytes)
    # as a probe answers the partial and one-byte heads with nothing.
    assert answers[0].startswith(b"HTTP/1.1 400 ")
    assert b"X-Swarm-Egress-Refused: head\r\n" in answers[0]
    # MUTATION: skipping the record for a short head drops this event.
    assert [(e["cause"], e["status"]) for e in _refusals(caplog)] == [("head", 400)]
    assert harness.dialled == []


@pytest.mark.parametrize("data", [
    pytest.param(b"CONNECT " + TARGET.encode() + b" HTTP/1.1\r\n", id="mid-head"),
    pytest.param(b"", id="silent"),
])
def test_a_head_that_times_out_is_still_refused(tmp_path, caplog, monkeypatch, data):
    import swarm_egress.server as server

    monkeypatch.setattr(server, "HEAD_TIMEOUT_SECONDS", 0.2)
    caplog.set_level(logging.INFO, logger=LOGGER)
    answers: list[bytes] = []

    async def scenario(h: Harness) -> None:
        answers.append(await _raw(h, data, eof=False))

    run(tmp_path, scenario)
    # MUTATION: treating a timeout as a probe answers nothing here: a client
    # that holds the connection open is not a probe that closed it.
    assert answers[0].startswith(b"HTTP/1.1 400 ")
    # MUTATION: skipping the record for a timed-out head drops this event.
    assert [(e["cause"], e["status"]) for e in _refusals(caplog)] == [("head", 400)]


# -- no credential in any log ------------------------------------------------------


def test_no_log_line_carries_the_password_the_username_a_ticket_or_the_host(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario(h: Harness) -> None:
        status, reader, writer = await h.connect(ticket=h.ticket())
        writer.write(b"ping")
        await writer.drain()
        await asyncio.wait_for(reader.readexactly(4), 5)
        writer.close()
        for target in ("github.com:443", TARGET):
            _, _, w = await h.connect(target, h.ticket(generation=0))
            w.close()
        _, _, w = await h.connect(ticket=None)
        w.close()
        h.config = EgressConfig(enabled=False, cause="disabled")
        _, _, w = await h.connect(ticket=h.ticket())
        w.close()

    harness = run(tmp_path, scenario)
    assert caplog.records, "the scenario logged nothing, so this test proved nothing"
    forbidden = [harness.password, harness.username, TARGET_HOST, "github.com", "brd.superproxy.io",
                 *harness.tickets, *(t.split(".")[1] for t in harness.tickets)]
    for record in caplog.records:
        text = record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__)
        for value in forbidden:
            assert value not in text, f"{record.name} logged a forbidden value"
