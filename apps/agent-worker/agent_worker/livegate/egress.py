"""The live browser's proxied egress, the gate's half (docs/browser-egress.md §2.2-§2.5;
lane EGRESS-GATE, part of #1030).

WHAT IS ROUTED. Only the headed Chromium this gate launches. When the
sidecar's environment says `SWARM_BROWSER_EGRESS=proxy` (the dispatcher sets
it, once per attempt, for a switched-on tenant and profile -- lane
EGRESS-DISPATCH), Chromium is started with

    --proxy-server=http://127.0.0.1:<P>      this module's local forwarder
    --proxy-bypass-list=<local>;github.com;*.github.com;...   (§2.3)
    --force-webrtc-ip-handling-policy=disable_non_proxied_udp

and with the variable unset, or `direct`, nothing about the launch changes.
The agent's own traffic, the worker's and the gate's tunnel never pass here.

THREE HOPS (§2.2). Chromium -> the local forwarder on loopback -> swarm-egress
in swarm-system (TCP 3128, `SWARM_EGRESS_FORWARDER`) -> the vendor. Chromium's
`--proxy-server` cannot carry a credential, so the forwarder adds one -- the
attempt EGRESS TICKET swarm-api minted for this tenant, task, attempt and
generation, which the worker hands over on ctl.sock (`egress_ticket`) -- to
each CONNECT in its proxy authorization header, under the bearer scheme
(`SCHEME`, as swarm-egress's server.py reads it). The ticket is the only
egress secret that is ever in this pod; the vendor credential lives only in
swarm-egress (owner decision Q4).

FAIL CLOSED (owner decision Q7). With egress on, Chromium is never started
without the proxy flag (`chromium_proxy_args` refuses to render without a
listening forwarder, and the gate's launch checks it too), and the forwarder
refuses every CONNECT while it holds no ticket, after the worker reported
that no ticket can be had (`fail`), after the ticket expired, and whenever
swarm-egress cannot be reached or refuses. It never dials a site itself:
the only address it connects to is swarm-egress. Nothing here falls back to
direct, for part of an attempt or the whole of it. What Chromium is answered
on a refusal is a 502 or 503 (never a 407, which would make it prompt for a
proxy password); the gate puts a page on screen saying why (`notice_url`),
which is what the person watching and the agent reading the page see.

ONLY THE SIDECAR'S UID MAY USE IT (§2.2 hop 2). The agent shares the pod's
network namespace, so it can reach `127.0.0.1:<P>` too, and would otherwise
send its own traffic through the paid pool under our ticket. Each accepted
connection's peer socket is looked up in /proc/net/tcp, which lists every
socket in the namespace with its owner, and served only when its uid is this
process's (Chromium runs as the gate's uid). Anything else is refused with
cause `peer_uid`, as is a peer whose owner cannot be read.

ONLY CONNECT. A plain-`http:` request through a proxy is a `GET http://...`,
which would cross a residential peer in clear (§3.2); the forwarder answers
anything but CONNECT with 405, and swarm-egress carries only port 443.

NOTHING CARRIED HERE IS LOGGED: not the ticket, not a CONNECT's host, not a
byte of a tunnel. Records carry event names, causes and counts.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import re
import socket
import struct
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: The sidecar's switch, set by the dispatcher (EGRESS-DISPATCH). `proxy`
#: routes the browser; unset, empty or `direct` changes nothing.
EGRESS_ENV = "SWARM_BROWSER_EGRESS"
#: Where swarm-egress listens, `host:port`. The default is its Service in
#: swarm-system on `swarm_egress.main.PORT` (kubernetes/swarm-system/swarm-egress.yaml),
#: the one destination `allow-egress-proxy.yaml` opens to a labelled pod.
FORWARDER_ENV = "SWARM_EGRESS_FORWARDER"
DEFAULT_FORWARDER = "swarm-egress.swarm-system.svc.cluster.local:3128"
#: Hosts beyond `DEFAULT_BYPASS` that go direct: the console's hostname and
#: terraform's `extra_bypass_hosts`, comma- or semicolon-separated, rendered
#: from configuration by the dispatcher, never hard-coded here (§2.3).
BYPASS_ENV = "SWARM_EGRESS_BYPASS"

MODE_PROXY = "proxy"
MODE_DIRECT = "direct"

#: Always direct, whatever the dispatcher passes: the list terraform/infra/egress.tf
#: (`egress_bypass_hosts`) gives swarm-egress, which REFUSES a CONNECT for one
#: of them (policy.py `bypass`). A host missing here would fail closed in the
#: browser rather than leak; the test holds the two lists together.
DEFAULT_BYPASS: tuple[str, ...] = ("github.com", ".github.com", ".githubusercontent.com")

#: The proxy authorization scheme swarm-egress accepts the ticket under.
SCHEME = "Bearer"
#: A ticket is ~300 characters; anything past this is not one swarm-api made.
MAX_TICKET_CHARS = 4096
#: swarm-api clamps a ticket's life to 60-900 s (`swarm_api.egress`).
MAX_TICKET_SECONDS = 900

#: Chromium's CONNECT head, read from loopback.
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT_SECONDS = 10.0
#: Opening TCP to swarm-egress, inside the VPC.
DIAL_TIMEOUT_SECONDS = 10.0
#: swarm-egress answers after its own vendor dial (15 s) and the vendor's
#: answer (30 s), so a little past their sum.
ANSWER_TIMEOUT_SECONDS = 50.0
#: The reachability probe the gate runs when a ticket arrives.
PROBE_TIMEOUT_SECONDS = 5.0
CHUNK = 65536

#: A cause swarm-egress names in `X-Swarm-Egress-Refused` (server.py): a word,
#: never free text, so a hostile answer cannot put anything else in a record.
_CAUSE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HOST = re.compile(r"(?:\*?\.)?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_TARGET = re.compile(r"(?:\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):[0-9]{1,5}")

#: /proc/net/tcp's TCP_ESTABLISHED.
_ESTABLISHED = "01"
PROC_TCP_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")

Record = Callable[[str, Mapping[str, Any]], None]


class EgressConfigError(ValueError):
    """The sidecar's egress environment is not one this gate can honour."""


# --------------------------------------------------------------------------
# Configuration: the sidecar's environment
# --------------------------------------------------------------------------


def _split_hosts(raw: str) -> list[str]:
    return [h.strip().lower() for h in re.split(r"[,;\s]+", raw or "") if h.strip()]


@dataclass(frozen=True)
class EgressSettings:
    """Whether this browser is proxied, where swarm-egress is, and what goes direct."""

    proxy: bool = False
    forwarder_host: str = ""
    forwarder_port: int = 0
    bypass_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "EgressSettings":
        mode = (env.get(EGRESS_ENV) or "").strip().lower()
        if mode in ("", MODE_DIRECT):
            return cls()
        if mode != MODE_PROXY:
            # Fail closed: a value nobody meant is not read as "direct".
            raise EgressConfigError(f"{EGRESS_ENV} must be {MODE_PROXY} or {MODE_DIRECT}")
        forwarder = (env.get(FORWARDER_ENV) or DEFAULT_FORWARDER).strip()
        host, sep, port = forwarder.rpartition(":")
        if not sep or not port.isdigit() or not 0 < int(port) < 65536 or not host \
                or not _HOST.fullmatch(host.lower()) or host.startswith(("*", ".")):
            raise EgressConfigError(f"{FORWARDER_ENV} must be host:port")
        extra = _split_hosts(env.get(BYPASS_ENV) or "")
        if not all(_HOST.fullmatch(h) for h in extra):
            raise EgressConfigError(f"{BYPASS_ENV} lists host names or .suffixes only")
        hosts: list[str] = []
        for entry in (*DEFAULT_BYPASS, *extra):
            entry = "." + entry[2:] if entry.startswith("*.") else entry
            if entry not in hosts:
                hosts.append(entry)
        return cls(proxy=True, forwarder_host=host.lower(), forwarder_port=int(port),
                   bypass_hosts=tuple(hosts))

    def bypass_list(self) -> str:
        """`--proxy-bypass-list`: `<local>` first, a `.suffix` as Chromium's `*.suffix`.

        The same rendering as `swarm_egress.policy.chromium_bypass_list`, which
        this image does not carry; the test holds the two equal.
        """
        out = ["<local>"]
        for entry in self.bypass_hosts:
            out.append("*" + entry if entry.startswith(".") else entry)
        return ";".join(out)


def chromium_proxy_args(settings: EgressSettings, port: int | None) -> list[str]:
    """Chromium's egress flags: none when direct; the proxy, or an error, when on.

    There is no third answer. With egress on and no listening forwarder this
    RAISES rather than return a list without `--proxy-server`, so no caller
    can start an unproxied browser by forgetting to check (owner decision Q7).
    """
    if not settings.proxy:
        return []
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise EgressConfigError("browser egress is on and its forwarder is not listening; "
                                "Chromium is not started without the proxy")
    return [
        f"--proxy-server=http://127.0.0.1:{port}",
        f"--proxy-bypass-list={settings.bypass_list()}",
        # §2.3: WebRTC's STUN would otherwise show sites swarm-nat's address
        # around the proxy. The switch Chromium's WebRtcIPHandling policy sets.
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]


# --------------------------------------------------------------------------
# What the browser shows when egress is unavailable
# --------------------------------------------------------------------------

#: What each cause means to the person watching and to the agent, in one line.
_REASONS: dict[str, str] = {
    "no_ticket": "the worker has not handed the browser its egress ticket yet",
    "ticket_expired": "this attempt's egress ticket expired and was not renewed",
    "forwarder_unreachable": "the SwarmCloud egress forwarder (swarm-egress) cannot be reached",
    "egress_off": "SwarmCloud refused this attempt an egress ticket (the switch is off)",
    "egress_fenced": "SwarmCloud refused this attempt an egress ticket (it is no longer current)",
    "egress_unavailable": "this deployment has no egress key, so no ticket can be minted",
    "mint_refused": "SwarmCloud refused this attempt an egress ticket",
    "mint_failed": "SwarmCloud could not be asked for this attempt's egress ticket",
    "no_attempt_key": "the worker has no attempt key to ask for an egress ticket with",
    "worker_unprotected": "the worker cannot protect an egress ticket in its memory",
    "egress_disabled": "browser egress was switched off for the platform",
    "tenant_off": "browser egress was switched off for this tenant",
}


def describe(cause: str) -> str:
    return _REASONS.get(cause, "the egress path refused the connection")


def notice_html(cause: str) -> str:
    """The page the gate opens when proxied egress is unavailable: fixed text and a cause word."""
    word = cause if _CAUSE.fullmatch(cause or "") else "unknown"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Browser egress unavailable</title></head>"
        "<body style='font-family:sans-serif;max-width:46em;margin:3em auto;line-height:1.5'>"
        "<h1>Browsing is stopped: the proxied egress is unavailable</h1>"
        f"<p>This browser is set to reach the internet only through SwarmCloud's egress "
        f"proxy, and that path is unavailable: {html.escape(describe(word))} "
        f"(<code>{html.escape(word)}</code>).</p>"
        "<p>SwarmCloud does <strong>not</strong> fall back to a direct connection, so web "
        "pages will not load in this browser until the path returns. Tell the person who "
        "asked for this task; do not keep retrying page loads.</p>"
        "</body></html>"
    )


def notice_url(cause: str) -> str:
    """`notice_html` as a data: URL, which loads without any network."""
    return "data:text/html;charset=utf-8," + urllib.parse.quote(notice_html(cause))


# --------------------------------------------------------------------------
# Who is on the other end of a loopback connection
# --------------------------------------------------------------------------


def _hex_v4(address: tuple[str, int]) -> str:
    host, port = address[0], address[1]
    packed = socket.inet_aton(host)
    return f"{struct.unpack('<I', packed)[0]:08X}:{port:04X}"


def _hex_v6_mapped(address: tuple[str, int]) -> str:
    """An IPv4 address as /proc/net/tcp6 lists it on a dual-stack socket."""
    packed = ipaddress.IPv6Address(f"::ffff:{address[0]}").packed
    # Four 32-bit words, each written in the host's (little-endian) order.
    text = "".join(f"{struct.unpack('<I', packed[i:i + 4])[0]:08X}" for i in range(0, 16, 4))
    return f"{text}:{address[1]:04X}"


def socket_owner(peer: tuple[str, int], local: tuple[str, int],
                 tables: tuple[str, ...] = PROC_TCP_TABLES,
                 read: Callable[[str], str] | None = None) -> int | None:
    """The uid owning the socket at `peer` that is connected to `local`, or None.

    `peer` is the connecting side's address as this server sees it, so in the
    namespace's table its socket is the row whose local address is `peer` and
    whose remote address is `local`. Loopback IPv4 only: the forwarder listens
    on 127.0.0.1.
    """
    def _read(path: str) -> str:
        with open(path, encoding="ascii") as handle:
            return handle.read()

    reader = read or _read
    try:
        wanted = {(_hex_v4(peer), _hex_v4(local)), (_hex_v6_mapped(peer), _hex_v6_mapped(local))}
    except (OSError, ValueError, struct.error):
        return None
    for path in tables:
        try:
            text = reader(path)
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 8:
                continue
            if (fields[1].upper(), fields[2].upper()) in wanted and fields[3] == _ESTABLISHED:
                try:
                    return int(fields[7])
                except ValueError:
                    return None
    return None


# --------------------------------------------------------------------------
# The local forwarder
# --------------------------------------------------------------------------


Dial = Callable[[str, int], Any]
PeerUid = Callable[[tuple[str, int], tuple[str, int]], "int | None"]


async def _default_dial(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


class LocalForwarder:
    """The CONNECT-only server Chromium's `--proxy-server` points at.

    States: `waiting` (no ticket yet: every CONNECT refused `no_ticket`),
    `ready` (a live ticket: each CONNECT goes to swarm-egress with it), and
    `failed` (the worker said no ticket can be had, with its cause: every
    CONNECT refused with it). A ticket past its expiry is refused
    `ticket_expired` until the worker renews it. No state dials anywhere but
    swarm-egress.
    """

    def __init__(self, settings: EgressSettings, *, record: Record,
                 uid: int | None = None, peer_uid: PeerUid | None = None,
                 dial: Dial | None = None, clock: Callable[[], float] = time.time,
                 on_unavailable: Callable[[str], None] | None = None) -> None:
        if not settings.proxy:
            raise EgressConfigError("the forwarder runs only when browser egress is on")
        self.settings = settings
        self._record = record
        self._uid = os.getuid() if uid is None else uid
        self._peer_uid = peer_uid or socket_owner
        self._dial = dial or _default_dial
        self._clock = clock
        self._on_unavailable = on_unavailable
        self._ticket: str | None = None
        self._expires_at: float | None = None
        self.state = "waiting"
        self.cause: str | None = "no_ticket"
        self.port = 0
        self._server: Any = None
        self._local: tuple[str, int] = ("127.0.0.1", 0)
        self._last_refusal: str | None = None
        self._told: set[str] = set()
        self._live: set[asyncio.Task[Any]] = set()
        self.connects = 0
        self.refused = 0
        self.bytes_up = 0
        self.bytes_down = 0

    # -- lifecycle ---------------------------------------------------------------

    @property
    def listening(self) -> bool:
        return self._server is not None and self.port > 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Listen on loopback (an ephemeral port by default); the port Chromium is given."""
        if host not in ("127.0.0.1",):
            raise EgressConfigError("the forwarder listens on 127.0.0.1 only")
        self._server = await asyncio.start_server(self._serve, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._local = (host, self.port)
        return self.port

    async def close(self) -> None:
        server, self._server = self._server, None
        self._ticket = None
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in list(self._live):
            task.cancel()
        if self._live:
            await asyncio.gather(*self._live, return_exceptions=True)

    # -- what the worker hands over --------------------------------------------

    def set_ticket(self, ticket: Any, expires_in: Any) -> None:
        if not isinstance(ticket, str) or not ticket or len(ticket) > MAX_TICKET_CHARS \
                or any(c.isspace() for c in ticket) or not ticket.isascii():
            raise ValueError("no ticket")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) \
                or not 0 < expires_in <= MAX_TICKET_SECONDS:
            raise ValueError("expires_in is seconds, at most the ticket's longest life")
        self._ticket = ticket
        self._expires_at = self._clock() + expires_in
        was = self.state
        self.state, self.cause = "ready", None
        self._last_refusal = None
        self._told.clear()
        if was != "ready":
            self._record("egress_ready", {"expires_in": expires_in})

    def fail(self, cause: str) -> None:
        """No ticket can be had (the mint was refused, no attempt key, ...): refuse everything."""
        word = cause if _CAUSE.fullmatch(cause or "") else "mint_failed"
        self._ticket = None
        self._expires_at = None
        self.state, self.cause = "failed", word
        self._record("egress_failed", {"cause": word})
        self._unavailable(word)

    def _unavailable(self, cause: str) -> None:
        if cause in self._told or self._on_unavailable is None:
            return
        self._told.add(cause)
        try:
            self._on_unavailable(cause)
        except Exception:  # noqa: BLE001 -- a notice never stops the forwarder
            pass

    async def probe(self) -> bool:
        """Whether swarm-egress accepts a TCP connection now. Sends no byte."""
        try:
            reader, writer = await asyncio.wait_for(
                self._dial(self.settings.forwarder_host, self.settings.forwarder_port),
                PROBE_TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        return True

    def ticket_live(self) -> bool:
        return self._ticket is not None and self._expires_at is not None \
            and self._clock() < self._expires_at

    def stats(self) -> dict[str, Any]:
        """The route, its state and counts: what `status` carries. Never the ticket."""
        state, cause = self.state, self.cause
        if state == "ready" and not self.ticket_live():
            state, cause = "expired", "ticket_expired"
        return {
            "route": MODE_PROXY,
            "state": state,
            "cause": cause,
            "expires_at": self._expires_at,
            "connects": self.connects,
            "refused": self.refused,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
        }

    # -- one connection from Chromium -------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._live.add(task)
        try:
            await self._handle(reader, writer)
        except Exception:  # noqa: BLE001 -- one connection's failure is never the gate's
            pass
        finally:
            if task is not None:
                self._live.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    def _refuse(self, writer: asyncio.StreamWriter, status: int, cause: str) -> None:
        self.refused += 1
        if cause != self._last_refusal:
            # Once per change of cause: a page's hundred refused CONNECTs are one record.
            self._last_refusal = cause
            self._record("egress_refused", {"cause": cause})
        reason = {403: "Forbidden", 405: "Method Not Allowed", 502: "Bad Gateway",
                  503: "Service Unavailable"}.get(status, "Refused")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n"
                     "Connection: close\r\n\r\n".encode("ascii"))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or len(peer) < 2:
            return self._refuse(writer, 403, "peer_uid")
        try:
            owner = self._peer_uid((str(peer[0]), int(peer[1])), self._local)
        except Exception:  # noqa: BLE001 -- an owner that cannot be read is not ours
            owner = None
        if owner != self._uid:
            return self._refuse(writer, 403, "peer_uid")
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            return
        if len(head) > MAX_HEAD_BYTES:
            return self._refuse(writer, 403, "head")
        request = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        if len(request) != 3 or request[0] != "CONNECT":
            return self._refuse(writer, 405, "method")
        target = request[1]
        if not _TARGET.fullmatch(target):
            return self._refuse(writer, 403, "target")
        if self.state == "failed":
            return self._refuse(writer, 503, self.cause or "mint_failed")
        if self._ticket is None:
            return self._refuse(writer, 503, "no_ticket")
        if not self.ticket_live():
            self._unavailable("ticket_expired")
            return self._refuse(writer, 503, "ticket_expired")
        ticket = self._ticket
        try:
            up_reader, up_writer = await asyncio.wait_for(
                self._dial(self.settings.forwarder_host, self.settings.forwarder_port),
                DIAL_TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError):
            if not self.connects:
                self._unavailable("forwarder_unreachable")
            return self._refuse(writer, 502, "forwarder_unreachable")
        try:
            authorization = " ".join((SCHEME, ticket))
            up_writer.write((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                             f"Proxy-Authorization: {authorization}\r\n\r\n").encode("ascii"))
            del ticket, authorization
            await up_writer.drain()
            answer = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), ANSWER_TIMEOUT_SECONDS)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            up_writer.close()
            if not self.connects:
                self._unavailable("forwarder_unreachable")
            return self._refuse(writer, 502, "forwarder_unreachable")
        status, cause = _answer(answer)
        if status != 200:
            up_writer.close()
            if cause in ("egress_disabled", "tenant_off"):
                self._unavailable(cause)
            # Never a 407 to Chromium: it would ask for a proxy password.
            return self._refuse(writer, 502, cause)
        self.connects += 1
        self._last_refusal = None
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await self._relay(reader, writer, up_reader, up_writer)

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     up_reader: asyncio.StreamReader, up_writer: asyncio.StreamWriter) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, upward: bool) -> None:
            try:
                while True:
                    data = await src.read(CHUNK)
                    if not data:
                        break
                    if upward:
                        self.bytes_up += len(data)
                    else:
                        self.bytes_down += len(data)
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(pump(reader, up_writer, True), pump(up_reader, writer, False))


def _answer(head: bytes) -> tuple[int, str]:
    """swarm-egress's status, and its refusal cause as a word (or `refused`)."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    cause = "refused"
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "x-swarm-egress-refused":
            value = value.strip()
            if _CAUSE.fullmatch(value):
                cause = value
    return status, cause


__all__ = [
    "BYPASS_ENV",
    "DEFAULT_BYPASS",
    "DEFAULT_FORWARDER",
    "EGRESS_ENV",
    "FORWARDER_ENV",
    "EgressConfigError",
    "EgressSettings",
    "LocalForwarder",
    "chromium_proxy_args",
    "describe",
    "notice_url",
    "socket_owner",
]
