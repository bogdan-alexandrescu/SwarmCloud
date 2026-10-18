"""The gate's outbound tunnel to the relay (live browser L3m, part of #1030).

docs/design/live-browser.md §3.3. The pod listens on nothing: the gate DIALS
OUT to `swarm-live` over plain ws on TCP 8080 (owner decision 2026-10-11 (b)),
presents the one-use tunnel ticket the worker handed it over ctl.sock as its
first message, waits for the relay's `{"type": "ready"}`, and from then on
every message is a binary tunnel frame (apps/swarm-live/swarm_live/rendezvous.py,
"THE TUNNEL'S FRAMES"):

    op (1 byte) | len(conn_id) (1 byte) | conn_id (ascii) | payload

`OPEN`, `DATA`, `CLOSE`, `CONTROL` and `DEVICE` (the controller's device
choice, LB-DEV-B) arrive; `DATA`, `CLOSE`, `ACTION` and
`REPORT` (the page's origin and the device, report.py) leave. Each viewer (or the recorder) is one RFB stream with its own conn_id,
served by `livegate.rfb.RfbSession` over a reader and writer this module
makes for it (`StreamMux`).

WHY A WEBSOCKET CLIENT OF ITS OWN. The worker's dependencies carry none, and
cdpproxy.py already speaks the server side of RFC 6455 without one. A client
differs in two ways only: it masks what it sends, and it reads unmasked
frames. Plain ws, no TLS: the relay is in-cluster and TLS ends at swarm-api's
load balancer (§3.3, decision (b)).

THE ADDRESS. `SWARM_LIVE_URL` when the sidecar's environment carries it, else
the relay's in-cluster Service, `ws://swarm-live.swarm-system.svc.cluster.local:8080`
(kubernetes/swarm-system/swarm-live.yaml names it "the gate's address"). The
pod's resolver has no search path (scheduler.dispatch GKE_POD_DNS_CONFIG), so
the name is fully qualified. The relay trusts nothing but swarm-api's MAC'd
ticket; the address only decides where the gate knocks.

A SILENT RELAY IS A CLOSED TUNNEL (LB-R6). A relay pod that vanished sends
no FIN, and a tunnel that waits on it for ever is one the worker never
reopens -- the session stays open with its last report, and every viewer the
new relay admits has no gate to meet. The relay's server pings every 20 s
(uvicorn's default `ws_ping_interval`, swarm_live.main) and the recorder asks
for frames at least once a second, so `Tunnel.run` closes a link it has heard
nothing on -- not a frame, not a ping -- for `SILENT_SECONDS`. The gate's
`status` then says `closed` and the worker hands it a fresh ticket
(handoff.HandoffPath._keep_tunnel).

A RESTARTING RELAY IS REJOINED AT ONCE (RELAY-ROLL). The relay closes every
tunnel with `CLOSE_RESTARTING` (1012, "restarting, reconnect now") when it is
stopped for a roll. `Tunnel.close_code` keeps the code, the gate's `status`
reports it, and the worker -- the only holder of the attempt key that proves a
tunnel ticket -- reopens on `reconnect_delay`'s schedule: jittered, from about
250 ms, never past `RESTART_RETRY_MAX_SECONDS`, instead of the 5 s doubling to
a minute every other close gets. MEASURED 2026-10-12/14: 40 relay restarts in
~48 h, one per release, each leaving the gate up to a minute behind the relay.

NOTHING CARRIED HERE IS LOGGED. The ticket admits whoever holds it, and DATA
is a person's keystrokes (§5.3.3). Log lines carry close codes, conn ids and
counts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import socket
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: The relay's in-cluster Service (kubernetes/swarm-system/swarm-live.yaml).
DEFAULT_RELAY_URL = "ws://swarm-live.swarm-system.svc.cluster.local:8080"
TUNNEL_PATH = "/v1/tunnel"

#: The relay's frame ops (rendezvous.py `OP_*`); test_livegate_main.py holds
#: these equal to the relay's own.
OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION, OP_REPORT, OP_DEVICE = 1, 2, 3, 4, 5, 6, 7
_OPS = frozenset({OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION, OP_REPORT, OP_DEVICE})
MAX_CONN_ID = 64

#: The relay refuses a message over 16 MiB (swarm_live.main MAX_MESSAGE_BYTES).
#: RFB is a byte stream, so a large update is cut into DATA frames this size.
MAX_DATA_CHUNK = 1024 * 1024
MAX_MESSAGE = 16 * 1024 * 1024

#: How long the relay has to say `ready` after the ticket.
READY_SECONDS = 10.0
#: How long the relay may say nothing at all -- not a frame, not a ping --
#: before the gate takes the link for dead and closes it. Over three of the
#: relay's 20-second pings, so one lost ping is not a dead relay.
SILENT_SECONDS = 75.0

#: The relay's "restarting, reconnect now" (swarm_live.rendezvous
#: CLOSE_RESTARTING; RFC 6455's 1012 "Service Restart", which uvicorn sends
#: on SIGTERM too). test_livegate_tunnel_restart.py holds the two equal.
CLOSE_RESTARTING = 1012
#: The first reopen after a restart, before jitter, and the longest wait
#: between reopens while the new relay comes up. The cap is under the worker's
#: 10 s poll (CONTROL_POLL_SECONDS), so every poll tries again.
RESTART_RETRY_SECONDS = 0.25
RESTART_RETRY_MAX_SECONDS = 4.0
#: Every other close: the wait after a refused reopen, doubling to the cap,
#: so a relay that stays down costs a route call a minute (LB-R6).
RETRY_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _BINARY, _CLOSE, _PING, _PONG, _CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0


class TunnelError(Exception):
    """The tunnel could not be opened, or was refused; the message is a code, never content."""


def encode_frame(op: int, conn_id: str, payload: bytes = b"") -> bytes:
    raw = conn_id.encode("ascii")
    if op not in _OPS or len(raw) > MAX_CONN_ID:
        raise ValueError("bad tunnel frame")
    return bytes([op, len(raw)]) + raw + payload


def decode_frame(data: bytes) -> tuple[int, str, bytes]:
    if len(data) < 2 or data[0] not in _OPS or data[1] > MAX_CONN_ID or len(data) < 2 + data[1]:
        raise ValueError("bad tunnel frame")
    try:
        conn_id = data[2:2 + data[1]].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("bad tunnel frame") from None
    return data[0], conn_id, bytes(data[2 + data[1]:])


def reconnect_delay(close_code: int | None, attempt: int, *,
                    rand: Callable[[], float] = random.random) -> float:
    """Seconds to wait before reopen number `attempt` (0 first) of a tunnel
    that closed with `close_code`.

    A restart (`CLOSE_RESTARTING`): `RESTART_RETRY_SECONDS` doubling, jittered
    to 75-125 % so a relay's every gate does not knock in the same instant,
    capped at `RESTART_RETRY_MAX_SECONDS` -- the first try is ~250 ms away.
    Anything else: `RETRY_SECONDS` doubling to `RETRY_MAX_SECONDS`, unjittered,
    as before."""
    step = max(0, min(int(attempt), 16))
    if close_code == CLOSE_RESTARTING:
        base = RESTART_RETRY_SECONDS * (2 ** step)
        return min(RESTART_RETRY_MAX_SECONDS, base * (0.75 + 0.5 * rand()))
    return min(RETRY_MAX_SECONDS, RETRY_SECONDS * (2 ** step))


#: getaddrinfo's failures by errno, for the gate's `tunnel_refused` record
#: (NOLIVE-GUARD, #1366): EAI_AGAIN is a resolver that did not answer in time
#: (an overloaded cache), EAI_NONAME a name it said does not exist. The two
#: point at different fixes, and "gaierror" alone told them apart from nothing.
_GAI_NAMES = {
    getattr(socket, name): name
    for name in ("EAI_AGAIN", "EAI_NONAME", "EAI_FAIL", "EAI_NODATA", "EAI_SYSTEM")
    if hasattr(socket, name)
}

#: The class of a relay lookup failure (LB-DNS, part of #1366), said in the
#: gate's `tunnel_refused` cause and in the worker's retry log. Each calls for
#: a different manifest fix, which is why the class and not "gaierror" is said:
#:   resolver_busy  EAI_AGAIN: the resolver did not answer in time -- NodeLocal
#:                  DNSCache or kube-dns behind it swamped by a burst of pod
#:                  starts (a fixed-ClusterIP Service pinned through hostAliases
#:                  takes the lookup away);
#:   no_address     EAI_NONAME, or EAI_NODATA: the resolver answered, and the
#:                  name had no address -- swarm-live is a headless Service, so
#:                  its name has none while its one pod is not ready
#:                  (publishNotReadyAddresses);
#:   other          any other getaddrinfo failure, by its EAI name or errno.
RESOLVER_BUSY = "resolver_busy"
NO_ADDRESS = "no_address"
RESOLVER_OTHER = "other"


def resolver_class(exc: BaseException) -> str | None:
    """`RESOLVER_BUSY`, `NO_ADDRESS` or `RESOLVER_OTHER` for a resolver failure; None for anything else."""
    if not isinstance(exc, socket.gaierror):
        return None
    code = _GAI_NAMES.get(exc.errno) if isinstance(exc.errno, int) else None
    if code == "EAI_AGAIN":
        return RESOLVER_BUSY
    if code in ("EAI_NONAME", "EAI_NODATA"):
        return NO_ADDRESS
    return RESOLVER_OTHER


def unreachable_cause(exc: BaseException) -> str:
    """Why the relay could not be reached: the exception's class, and for a
    resolver failure its EAI name (or errno) and its `resolver_class`, e.g.
    `gaierror(EAI_AGAIN, resolver_busy)` -- never a bare "gaierror". Never the
    message, which can quote the host."""
    name = type(exc).__name__
    klass = resolver_class(exc)
    if klass is None:
        return name
    errno = getattr(exc, "errno", None)
    code = _GAI_NAMES.get(errno) if isinstance(errno, int) else None
    if code is None:
        code = f"errno {errno}" if isinstance(errno, int) else "no errno"
    return f"{name}({code}, {klass})"


def relay_url(environ: Mapping[str, str]) -> str:
    """The tunnel's URL: `SWARM_LIVE_URL` (or the in-cluster Service) + `/v1/tunnel`."""
    base = (environ.get("SWARM_LIVE_URL") or "").strip() or DEFAULT_RELAY_URL
    parts = urlsplit(base)
    if parts.scheme != "ws" or not parts.hostname:
        raise ValueError("SWARM_LIVE_URL must be a ws:// URL")
    return base.rstrip("/") + TUNNEL_PATH


# --------------------------------------------------------------------------
# A websocket client: just enough RFC 6455 for one long-lived connection
# --------------------------------------------------------------------------


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    pad = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(n, "big")


class WebSocketClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self.close_code: int | None = None
        #: `time.monotonic()` when the relay last sent anything, a ping included.
        self.last_heard = time.monotonic()

    @classmethod
    async def connect(cls, url: str, *, timeout: float = 10.0) -> "WebSocketClient":
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 80
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=MAX_MESSAGE), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise TunnelError(f"relay unreachable: {unreachable_cause(exc)}") from None
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        try:
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            writer.close()
            raise TunnelError(f"relay handshake failed: {type(exc).__name__}") from None
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if len(status) < 2 or status[1] != "101" or headers.get("sec-websocket-accept") != expected:
            writer.close()
            raise TunnelError(f"relay refused the upgrade: {status[1] if len(status) > 1 else 'malformed'}")
        return cls(reader, writer)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        key = os.urandom(4)
        async with self._lock:
            self._writer.write(head + key + _mask(payload, key))
            await self._writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(_TEXT, text.encode())

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(_BINARY, data)

    async def receive(self) -> tuple[bool, bytes] | None:
        """The next message as (is_text, payload), or None once the connection has closed."""
        opcode = None
        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                b0, b1 = await self._reader.readexactly(2)
                n = b1 & 0x7F
                if n == 126:
                    (n,) = struct.unpack(">H", await self._reader.readexactly(2))
                elif n == 127:
                    (n,) = struct.unpack(">Q", await self._reader.readexactly(8))
                key = await self._reader.readexactly(4) if b1 & 0x80 else b""
                if size + n > MAX_MESSAGE:
                    await self.close(1009)
                    return None
                payload = await self._reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            self.last_heard = time.monotonic()
            if key:
                payload = _mask(payload, key)
            op = b0 & 0x0F
            if op == _CLOSE:
                self.close_code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                await self.close(1000)
                return None
            if op == _PING:
                await self._send_frame(_PONG, payload)
                continue
            if op == _PONG:
                continue
            if op != _CONTINUATION:
                opcode = op
            chunks.append(payload)
            size += n
            if b0 & 0x80:
                return opcode == _TEXT, b"".join(chunks)

    async def close(self, code: int = 1000) -> None:
        if self._writer.is_closing():
            return
        try:
            await self._send_frame(_CLOSE, struct.pack(">H", code))
        except (ConnectionError, RuntimeError):
            pass
        self._writer.close()


# --------------------------------------------------------------------------
# The tunnel
# --------------------------------------------------------------------------


Connector = Callable[[str], Awaitable[Any]]


class Tunnel:
    """One session's tunnel: the ticket, the relay's `ready`, then frames to `on_frame`."""

    def __init__(self, url: str, *, connect: Connector | None = None,
                 silent_seconds: float = SILENT_SECONDS) -> None:
        self._url = url
        self._connect = connect or WebSocketClient.connect
        self._silent = silent_seconds
        self._ws: Any = None
        self.closed = False
        #: The relay's close code once `run` has ended (None before, or when
        #: the link ended without one): `CLOSE_RESTARTING` asks for a reopen now.
        self.close_code: int | None = None

    async def open(self, ticket: str) -> None:
        """Dial, present `ticket`, and wait for `ready`. `TunnelError` if refused."""
        ws = await self._connect(self._url)
        try:
            await ws.send_text(ticket)
            message = await asyncio.wait_for(ws.receive(), READY_SECONDS)
        except asyncio.TimeoutError:
            await ws.close()
            raise TunnelError("relay_not_ready") from None
        except (OSError, ConnectionError) as exc:
            raise TunnelError(f"relay_failed: {type(exc).__name__}") from None
        ready = False
        if message is not None and message[0]:
            try:
                ready = json.loads(message[1]).get("type") == "ready"
            except (ValueError, AttributeError):
                ready = False
        if not ready:
            code = getattr(ws, "close_code", None)
            await ws.close()
            raise TunnelError(f"relay_refused: {code}")
        self._ws = ws

    async def send(self, op: int, conn_id: str, payload: bytes = b"") -> None:
        if self._ws is None or self.closed:
            raise ConnectionError("tunnel closed")
        if op == OP_DATA and len(payload) > MAX_DATA_CHUNK:
            for at in range(0, len(payload), MAX_DATA_CHUNK):
                await self._ws.send_bytes(encode_frame(op, conn_id, payload[at:at + MAX_DATA_CHUNK]))
            return
        await self._ws.send_bytes(encode_frame(op, conn_id, payload))

    async def action(self, entry: Mapping[str, Any]) -> None:
        """One action-log line for the recording (§7). Names and causes, never content."""
        try:
            await self.send(OP_ACTION, "", json.dumps(dict(entry), separators=(",", ":")).encode())
        except (ConnectionError, OSError):
            pass

    async def report(self, payload: bytes) -> None:
        """The page's origin and the device (report.py `encode_report`), for the relay to keep."""
        try:
            await self.send(OP_REPORT, "", payload)
        except (ConnectionError, OSError):
            pass

    async def run(self, on_frame: Callable[[int, str, bytes], Awaitable[None]]) -> int | None:
        """Deliver every frame until the relay closes; returns its close code."""
        assert self._ws is not None, "open() first"
        watchdog = asyncio.create_task(self._watch_silence())
        try:
            while True:
                message = await self._ws.receive()
                if message is None:
                    self.close_code = getattr(self._ws, "close_code", None)
                    return self.close_code
                is_text, data = message
                if is_text:
                    continue
                try:
                    op, conn_id, payload = decode_frame(data)
                except ValueError:
                    log.info("live tunnel: dropped a malformed frame")
                    continue
                await on_frame(op, conn_id, payload)
        finally:
            self.closed = True
            watchdog.cancel()

    async def _watch_silence(self) -> None:
        """Close the link once the relay has been silent for `silent_seconds`.

        Closing, not cancelling the read: a read cancelled half-way through a
        frame would lose bytes. The close ends `receive()`, and `run` with it.
        A connection that does not say when it last heard (a test double) is
        never judged silent.
        """
        check = max(0.01, min(self._silent / 4, 5.0))
        while not self.closed:
            await asyncio.sleep(check)
            heard = getattr(self._ws, "last_heard", None)
            if isinstance(heard, (int, float)) and time.monotonic() - heard > self._silent:
                log.warning("live tunnel: the relay has been silent for %ds; closing", int(self._silent))
                await self._ws.close(1001)
                return

    async def close(self) -> None:
        self.closed = True
        if self._ws is not None:
            await self._ws.close()


# --------------------------------------------------------------------------
# One RFB stream per conn_id
# --------------------------------------------------------------------------


class TunnelWriter:
    """The writer half `RfbSession` writes to: DATA frames on the tunnel."""

    def __init__(self, tunnel: Tunnel, conn_id: str) -> None:
        self._tunnel = tunnel
        self._conn_id = conn_id
        self._buffer = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._buffer += data

    async def drain(self) -> None:
        if self._closed:
            raise ConnectionError("stream closed")
        if self._buffer:
            data, self._buffer = bytes(self._buffer), bytearray()
            await self._tunnel.send(OP_DATA, self._conn_id, data)

    def is_closing(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        self._buffer.clear()


class StreamMux:
    """Routes the relay's frames to one `serve(conn_id, reader, writer)` per stream."""

    def __init__(self, tunnel: Tunnel, serve: Callable[[str, asyncio.StreamReader, TunnelWriter], Awaitable[None]],
                 on_control: Callable[..., Awaitable[None]],
                 on_device: Callable[[bytes], Awaitable[None]] | None = None) -> None:
        self._tunnel = tunnel
        self._serve = serve
        self._on_control = on_control
        self._on_device = on_device
        self._streams: dict[str, tuple[asyncio.StreamReader, asyncio.Task[None]]] = {}

    @property
    def open_streams(self) -> int:
        return len(self._streams)

    async def on_frame(self, op: int, conn_id: str, payload: bytes) -> None:
        if op == OP_OPEN:
            if conn_id in self._streams or not conn_id:
                return
            reader = asyncio.StreamReader()
            writer = TunnelWriter(self._tunnel, conn_id)
            task = asyncio.create_task(self._run(conn_id, reader, writer))
            self._streams[conn_id] = (reader, task)
        elif op == OP_DATA:
            stream = self._streams.get(conn_id)
            if stream is not None:
                stream[0].feed_data(payload)
        elif op == OP_CLOSE:
            stream = self._streams.get(conn_id)
            if stream is not None:
                stream[0].feed_eof()
        elif op == OP_CONTROL:
            # A revoke's cause rides in the payload (rendezvous.py); a plain
            # grant or release has none, and is passed as before.
            if payload:
                await self._on_control(conn_id, payload)
            else:
                await self._on_control(conn_id)
        elif op == OP_DEVICE and self._on_device is not None:
            await self._on_device(payload)

    async def _run(self, conn_id: str, reader: asyncio.StreamReader, writer: TunnelWriter) -> None:
        try:
            await self._serve(conn_id, reader, writer)
        finally:
            self._streams.pop(conn_id, None)
            if not self._tunnel.closed:
                try:
                    await self._tunnel.send(OP_CLOSE, conn_id)
                except (ConnectionError, OSError):
                    pass

    async def close(self) -> None:
        tasks = [task for _, task in self._streams.values()]
        for reader, _ in self._streams.values():
            reader.feed_eof()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.clear()
