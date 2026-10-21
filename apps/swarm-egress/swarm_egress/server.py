"""The CONNECT forwarder (docs/browser-egress.md §2.2 hop 3, §3.1, §3.5).

One request per connection, from the gate's local forwarder in a labelled
browser pod:

    CONNECT <host>:443 HTTP/1.1

with the attempt egress ticket in the Proxy-Authorization header, under the
bearer scheme,

checked in this order, each refusal answered with a status, an
`X-Swarm-Egress-Refused: <cause>` header the gate records, and one log line:

    method        405  anything but CONNECT
    egress_disabled 403 the platform switch is off, or the configuration could
                       not be read (fail closed, owner decision Q7)
    no_ticket / malformed / bad_mac / expired / too_long   407
    stale_generation 403  a newer generation of the task has been seen
                          (invariant 5); seeing a newer one also closes the
                          older generation's open tunnels
    tenant_off    403  the tenant is not switched on (§6.1)
    bypass / internal / port / target   403  policy.check_target
    platform_cap / tenant_cap / attempt_cap / ledger_unreadable   403
    rate / tunnels 429  per-attempt limits on new CONNECTs and open tunnels
    no_credential / no_zone 503  the vendor credential is not loaded
    vendor_unreachable / vendor_refused 502

then a CONNECT to the vendor carrying the credential and the sticky session
(vendor.py), and from there it only relays bytes, counting them into the
ledger. A tunnel is closed when either end closes, when the switch goes off,
when the tenant is switched off, when its ticket expires, when a newer
generation of its task appears, or when a cap is reached.

THE LOG carries event names, causes, statuses, ids and byte counts. Never the
ticket, the vendor username (it carries the session id) or password, the
CONNECT host, or anything the vendor sent back; every line goes through
swarm_redaction as well. tests/unit/egress/test_egress_server.py fails if a
line carries any of them.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from swarm_redaction import redact

from .config import EgressConfig
from .ledger import Ledger
from .policy import TargetRefused, check_target
from .ticket import MAX_TICKET_SECONDS, SKEW_SECONDS, Ticket, TicketRefused, verify_ticket
from .vendor import VendorCredential, session_id

log = logging.getLogger("swarm_egress")

#: A request head larger than this is not a CONNECT from our forwarder.
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT_SECONDS = 10.0
DIAL_TIMEOUT_SECONDS = 15.0
VENDOR_ANSWER_TIMEOUT_SECONDS = 30.0
CHUNK = 65536

Dialer = Callable[[str, int, bool], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

_REASONS = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed",
            407: "Proxy Authentication Required", 429: "Too Many Requests",
            502: "Bad Gateway", 503: "Service Unavailable"}


def event(name: str, **fields: object) -> None:
    """One log line: JSON, redacted, and only the fields a caller passes,
    which are never a host, a ticket or a credential."""
    log.info(redact(json.dumps({"event": name, **fields}, sort_keys=True)).text)


class KeyRing:
    """swarm-egress-key's current value and, for a ticket's life after a
    rotation, the previous one: a ticket minted just before the rotation
    still verifies until it would have expired anyway."""

    def __init__(self, current: bytes, *, clock: Callable[[], float] = time.time) -> None:
        if not current:
            raise ValueError("swarm-egress-key is empty")
        self._current = current
        self._previous: bytes | None = None
        self._previous_until = 0.0
        self._clock = clock

    def rotate(self, new: bytes) -> bool:
        if not new or new == self._current:
            return False
        self._previous, self._current = self._current, new
        self._previous_until = self._clock() + MAX_TICKET_SECONDS + SKEW_SECONDS
        return True

    def keys(self) -> list[bytes]:
        if self._previous is not None and self._clock() < self._previous_until:
            return [self._current, self._previous]
        return [self._current]


@dataclass(eq=False)
class Tunnel:
    ticket: Ticket
    client: asyncio.StreamWriter
    upstream: asyncio.StreamWriter
    up: int = 0
    down: int = 0
    cause: str | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self, cause: str) -> None:
        if self.cause is not None:
            return
        self.cause = cause
        self.closed.set()
        for writer in (self.client, self.upstream):
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


class Refusal(Exception):
    def __init__(self, status: int, cause: str) -> None:
        super().__init__(cause)
        self.status = status
        self.cause = cause


class _NoRequest(Exception):
    """The connection closed before sending a byte: a TCP health check, not a
    request. Kubernetes' tcpSocket readiness (every 5 s) and liveness (every
    10 s) probes in kubernetes/swarm-system/swarm-egress.yaml, and the live
    gate's forwarder probe, connect and close without writing; recorded as
    `egress_refused` cause `head` they were 18 lines a minute (1,417 in 80
    minutes of the 2026-10-21 capture) that drowned every real refusal and
    inflated any count built on the log (#1822). Nothing was asked, so nothing
    is refused: no answer, no record. One byte, a timeout or an oversized head
    is still a refused request."""


async def _default_dial(host: str, port: int, tls: bool):
    if tls:
        import ssl

        return await asyncio.open_connection(host, port, ssl=ssl.create_default_context(),
                                             server_hostname=host)
    return await asyncio.open_connection(host, port)


class EgressServer:
    def __init__(
        self,
        *,
        keys: KeyRing,
        credential: Callable[[], VendorCredential | None],
        config: Callable[[], EgressConfig],
        ledger: Ledger,
        dial: Dialer = _default_dial,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keys = keys
        self._credential = credential
        self._config = config
        self._ledger = ledger
        self._dial = dial
        self._clock = clock
        self._tunnels: set[Tunnel] = set()
        #: The highest generation seen per (tenant, task): the fence. Held in
        #: memory; after a restart the first ticket re-establishes it, and a
        #: stale worker's ticket is still bounded by its expiry and by
        #: swarm-api's refusal to renew it.
        self._generations: dict[tuple[str, str], int] = {}
        self._connects: dict[str, collections.deque[float]] = {}

    @property
    def open_tunnels(self) -> int:
        return len(self._tunnels)

    # -- the request -----------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except Refusal as refusal:
            await self._refuse(writer, refusal)
        except _NoRequest:
            log.debug(json.dumps({"event": "egress_no_request"}))
        except Exception as exc:  # noqa: BLE001 - one connection's failure is logged, never fatal
            event("egress_error", cause=type(exc).__name__)
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _refuse(self, writer: asyncio.StreamWriter, refusal: Refusal) -> None:
        event("egress_refused", cause=refusal.cause, status=refusal.status)
        lines = [f"HTTP/1.1 {refusal.status} {_REASONS.get(refusal.status, 'Refused')}",
                 f"X-Swarm-Egress-Refused: {refusal.cause}", "Content-Length: 0",
                 "Connection: close"]
        if refusal.status == 407:
            lines.append('Proxy-Authenticate: Bearer realm="swarm-egress"')
        try:
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            await writer.drain()
        except Exception:  # noqa: BLE001 - the browser may already be gone
            pass

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]]:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
        except asyncio.IncompleteReadError as short:
            if not short.partial:
                raise _NoRequest from None
            raise Refusal(400, "head") from None
        except (asyncio.LimitOverrunError, asyncio.TimeoutError):
            raise Refusal(400, "head") from None
        if len(raw) > MAX_HEAD_BYTES:
            raise Refusal(400, "head")
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise Refusal(400, "head")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return parts[0], parts[1], headers

    def _admit(self, method: str, target: str, headers: dict[str, str]
               ) -> tuple[Ticket, EgressConfig, tuple[str, int]]:
        if method != "CONNECT":
            raise Refusal(405, "method")
        config = self._config()
        if not config.enabled:
            if config.cause != "disabled":
                event("egress_switch_off", reason=config.cause)
            raise Refusal(403, "egress_disabled")
        scheme, _, raw = headers.get("proxy-authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not raw.strip():
            raise Refusal(407, "no_ticket")
        now = self._clock()
        try:
            ticket = verify_ticket(self._keys.keys(), raw.strip(), now=now)
        except TicketRefused as refused:
            raise Refusal(407, refused.cause) from None
        self._fence(ticket)
        if ticket.tenant_id not in config.tenants:
            raise Refusal(403, "tenant_off")
        try:
            host_port = check_target(target, bypass_hosts=config.bypass_hosts,
                                     allowed_ports=config.allowed_ports)
        except TargetRefused as refused:
            raise Refusal(403, refused.cause) from None
        cap = self._ledger.over_cap(ticket.tenant_id, ticket.attempt_id, config)
        if cap:
            raise Refusal(403, cap)
        recent = self._connects.setdefault(ticket.attempt_id, collections.deque())
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= config.connects_per_minute:
            raise Refusal(429, "rate")
        recent.append(now)
        open_here = sum(1 for t in self._tunnels if t.ticket.attempt_id == ticket.attempt_id)
        if open_here >= config.max_tunnels_per_attempt:
            raise Refusal(429, "tunnels")
        return ticket, config, host_port

    def _fence(self, ticket: Ticket) -> None:
        task = (ticket.tenant_id, ticket.task_id)
        seen = self._generations.get(task)
        if seen is not None and ticket.generation < seen:
            raise Refusal(403, "stale_generation")
        if seen is None or ticket.generation > seen:
            self._generations[task] = ticket.generation
            for tunnel in list(self._tunnels):
                if (tunnel.ticket.tenant_id, tunnel.ticket.task_id) == task and \
                        tunnel.ticket.generation < ticket.generation:
                    self._end(tunnel, "stale_generation")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        method, target, headers = await self._read_head(reader)
        ticket, config, (host, port) = self._admit(method, target, headers)
        credential = self._credential()
        if credential is None:
            raise Refusal(503, "no_credential")
        zone = config.zone_for(ticket.tenant_id)
        if zone not in credential.zones:
            raise Refusal(503, "no_zone")
        session = session_id(ticket.tenant_id, ticket.task_id, ticket.attempt_id,
                             sticky=credential.zones[zone].sticky)
        authorization = credential.proxy_authorization(zone, country=config.country,
                                                       session=session)
        try:
            up_reader, up_writer = await asyncio.wait_for(
                self._dial(credential.host, credential.port, credential.tls), DIAL_TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError):
            raise Refusal(502, "vendor_unreachable") from None
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        try:
            up_writer.write((f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
                             f"Proxy-Authorization: {authorization}\r\n\r\n").encode("ascii"))
            await up_writer.drain()
            answer = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"),
                                            VENDOR_ANSWER_TIMEOUT_SECONDS)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError):
            up_writer.close()
            raise Refusal(502, "vendor_unreachable") from None
        del authorization
        status_line = answer.split(b"\r\n", 1)[0].split(b" ")
        status = int(status_line[1]) if len(status_line) > 1 and status_line[1].isdigit() else 0
        if status != 200:
            up_writer.close()
            # The status only: the vendor's headers and body can name the host.
            event("egress_vendor_refused", status=status, tenant_id=ticket.tenant_id,
                  attempt_id=ticket.attempt_id)
            raise Refusal(502, "vendor_refused")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await self._relay(ticket, zone, reader, writer, up_reader, up_writer)

    # -- the tunnel ------------------------------------------------------------

    async def _relay(self, ticket: Ticket, zone: str, client_reader: asyncio.StreamReader,
                     client_writer: asyncio.StreamWriter, up_reader: asyncio.StreamReader,
                     up_writer: asyncio.StreamWriter) -> None:
        tunnel = Tunnel(ticket=ticket, client=client_writer, upstream=up_writer)
        self._tunnels.add(tunnel)
        event("egress_tunnel_open", tenant_id=ticket.tenant_id, task_id=ticket.task_id,
              attempt_id=ticket.attempt_id, generation=ticket.generation, zone=zone)

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, upward: bool) -> None:
            try:
                while tunnel.cause is None:
                    data = await src.read(CHUNK)
                    if not data:
                        break
                    self._ledger.add(ticket.tenant_id, ticket.attempt_id, len(data))
                    if upward:
                        tunnel.up += len(data)
                    else:
                        tunnel.down += len(data)
                    cap = self._ledger.over_cap(ticket.tenant_id, ticket.attempt_id, self._config())
                    if cap:
                        self._end(tunnel, cap)
                        break
                    dst.write(data)
                    await dst.drain()
                if tunnel.cause is None and dst.can_write_eof():
                    dst.write_eof()
            except (OSError, asyncio.IncompleteReadError, RuntimeError):
                pass

        pumps = asyncio.gather(pump(client_reader, up_writer, True),
                               pump(up_reader, client_writer, False))
        closed = asyncio.ensure_future(tunnel.closed.wait())
        try:
            await asyncio.wait({pumps, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._end(tunnel, tunnel.cause or "eof")
            pumps.cancel()
            closed.cancel()
            await asyncio.gather(pumps, closed, return_exceptions=True)
            event("egress_tunnel_closed", cause=tunnel.cause, tenant_id=ticket.tenant_id,
                  attempt_id=ticket.attempt_id, bytes_up=tunnel.up, bytes_down=tunnel.down)

    def _end(self, tunnel: Tunnel, cause: str) -> None:
        self._tunnels.discard(tunnel)
        tunnel.close(cause)

    # -- the supervisor ------------------------------------------------------------

    async def supervise_once(self) -> None:
        """The kill switch, the tenant switch, ticket expiry and the caps, for
        every open tunnel; then the ledger is written."""
        config = self._config()
        now = self._clock()
        for tunnel in list(self._tunnels):
            t = tunnel.ticket
            if not config.enabled:
                self._end(tunnel, "egress_disabled")
            elif t.tenant_id not in config.tenants:
                self._end(tunnel, "tenant_off")
            elif t.exp <= now:
                self._end(tunnel, "ticket_expired")
            else:
                cap = self._ledger.over_cap(t.tenant_id, t.attempt_id, config)
                if cap:
                    self._end(tunnel, cap)
        cutoff = now - 60
        for attempt, recent in list(self._connects.items()):
            if not recent or recent[-1] <= cutoff:
                del self._connects[attempt]
        self._ledger.flush()
        await asyncio.sleep(0)

    async def supervise(self, interval: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.supervise_once()
            except Exception as exc:  # noqa: BLE001 - the supervisor must keep running
                event("egress_supervisor_error", cause=type(exc).__name__)

    async def close_all(self, cause: str) -> None:
        for tunnel in list(self._tunnels):
            self._end(tunnel, cause)
        self._ledger.flush()
        await asyncio.sleep(0)


__all__ = ["EgressServer", "KeyRing", "Refusal", "Tunnel", "event"]
