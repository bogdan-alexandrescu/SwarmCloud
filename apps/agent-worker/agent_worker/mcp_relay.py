"""The loopback relay between a task's agent and its caller's registered MCP
servers (docs/design/registered-mcp-servers.md §4.7 and §4.8; lane MCP-WORKER,
part of #1433).

WHY A RELAY. The credential for a registered server (a per-run value the
caller sent, or a workspace secret granted to `mcp:<name>`) must never be in
the agent's reach: not in its environment, its argv, the CLI's MCP config, a
file under the workspace, a checkpoint or a log line. So the agent's CLI is
pointed at this relay (`http://127.0.0.1:<port>/mcp/<name>`) with a per-attempt
LOCAL token, and the relay adds the real auth header on the way out. The
credential lives in this process's non-dumpable heap only, beside the attempt
key, and is dropped by `close`.

THE POLICY POINT (§4.8). Checks, in order, before anything leaves the pod:

  * the peer is loopback, else 403;
  * exactly one authorization header, bearer scheme, whose value is the
    local token (compared in constant time), else 401;
  * the attempt is not fenced (the lifecycle's control poll sets the flag;
    invariant 5), else 409 and nothing is forwarded;
  * a POST carries a `Content-Length` (no chunked request) of at most the
    registration's `max_request_bytes`, else 411 / 413;
  * the body parses as JSON-RPC 2.0, one message or a batch, else 400;
  * every `tools/call` names a tool in `allowed_tools` when that list is set
    (empty means every tool, owner decision Q4), else `tool_not_allowed`;
  * no value registered with the worker's logger (provider keys, released
    secrets, tickets, this or another server's credential, the local token)
    and no credential-shaped string (`swarm_redaction`'s rules, see
    SHAPE_RULES for the two left out) is in the body or a forwarded header,
    else `credential_in_request`, logged with the rule's name only;
  * the per-server sliding minute is under `calls_per_minute`, else
    `rate_limited`.

THEN IT FORWARDS to the registration's URL exactly -- no path beyond
`/mcp/<name>` is accepted, so the agent cannot choose one -- after resolving
the host itself and refusing unless EVERY address is public (`is_global`:
not loopback, RFC1918, link-local and the metadata server, CGNAT, ULA,
multicast, or the cluster's own ranges, which are private). The connection
goes to the checked address with SNI and certificate verification against
the registered host, so a DNS answer that changes between check and connect
cannot move it. Port 443 only (the attach refuses any other URL). Headers
out: `Content-Type`, `Accept`, `Mcp-Session-Id`, `MCP-Protocol-Version`,
`Last-Event-ID` from the agent, the registration's auth header, a fixed
`User-Agent` and the task and attempt ids. Nothing else the agent sent, an
authorization or a cookie included, and never a platform credential.

NO REDIRECT IS FOLLOWED: a 3xx reaches the agent as 502 `redirect_refused`,
without its `Location`. Answers come back with their status, `Content-Type`
and `Mcp-Session-Id` only; every chunk (a JSON body whole, an SSE stream event
by event) is scrubbed of every registered value first, so a server that
echoes the credential never shows it to the agent; capped at
`max_response_bytes` and bounded by `call_timeout_seconds` overall, checked
per read and between chunks (the TypeSafe forwarder's rule).

LOGS: one `mcp_call` line per request -- server, JSON-RPC method, tool name,
status, byte counts, duration, refusal code. Never arguments, results,
headers, a URL or a value.

"NO COPY" AFTER CLOSE, STATED HONESTLY: a Python `str` cannot be zeroed. After
`close` the relay holds no reference to any credential, and the bytes go when
the worker process exits, which it does at the end of every attempt.
"""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from swarm_redaction import KEY_VALUE, RULES

LOOPBACK = "127.0.0.1"
PATH_PREFIX = "/mcp/"
UPSTREAM_PORT = 443

#: The relay's own refusal code, on every answer it makes itself.
REFUSED_HEADER = "X-Swarm-Mcp-Refused"
USER_AGENT = "swarmcloud-mcp-relay"
TASK_HEADER = "X-Swarm-Task"
ATTEMPT_HEADER = "X-Swarm-Attempt"

#: MCP's own request headers, the only ones of the agent's that go out.
FORWARDED_HEADERS = ("Content-Type", "Accept", "Mcp-Session-Id", "MCP-Protocol-Version",
                     "Last-Event-ID")
#: The only upstream headers the agent sees (with the status).
RETURNED_HEADERS = ("Content-Type", "Mcp-Session-Id")

#: §4.2's defaults and ceilings for a registration's `limits`.
DEFAULT_LIMITS = {"max_request_bytes": 1024 * 1024, "max_response_bytes": 4 * 1024 * 1024,
                  "calls_per_minute": 60, "call_timeout_seconds": 60}
CEILINGS = {"max_request_bytes": 8 * 1024 * 1024, "max_response_bytes": 32 * 1024 * 1024,
            "calls_per_minute": 600, "call_timeout_seconds": 300}

#: One SSE line may not be longer than this; a longer one ends the stream.
MAX_LINE_BYTES = 64 * 1024

#: Refusal codes (§4.8).
NOT_LOOPBACK = "not_loopback"
UNAUTHORIZED = "unauthorized"
NOT_FOUND = "not_found"
FENCED = "fenced"
LENGTH_REQUIRED = "length_required"
TOO_LARGE = "request_too_large"
BAD_REQUEST = "bad_request"
TOOL_NOT_ALLOWED = "tool_not_allowed"
CREDENTIAL_IN_REQUEST = "credential_in_request"
RATE_LIMITED = "rate_limited"
ADDRESS_REFUSED = "address_refused"
UNRESOLVED = "unresolved"
REDIRECT_REFUSED = "redirect_refused"
RESPONSE_TOO_LARGE = "response_too_large"
UPSTREAM_TIMEOUT = "upstream_timeout"
UPSTREAM_UNREACHABLE = "upstream_unreachable"

#: The JSON-RPC error codes the relay answers with: -32600 is the spec's
#: "invalid request"; -32001 is in the server-defined range, one code for
#: every policy refusal, told apart by `error.data.refused`.
_RPC_INVALID = -32600
_RPC_REFUSED = -32001

#: `swarm_redaction`'s credential rules, as a REFUSAL rather than a mask, bar
#: two that match ordinary prose an agent legitimately sends a knowledge
#: server: the key/value rule (`"token_budget": "..."`, `password: field`)
#: and the bare `Bearer|Basic <12+ chars>` rule ("basic configuration").
#: Masking a false positive in a log costs a word; refusing one costs the
#: agent its write. Both stay in force for the registered VALUES, which are
#: refused by value whatever their shape, and in every log and artifact.
SHAPE_EXEMPT = ("key_value_assignment", "http_authorization")
SHAPE_RULES = tuple(r for r in RULES if r is not KEY_VALUE and r.name not in SHAPE_EXEMPT)

_TOKEN_SAFE = re.compile(r"[\x20-\x7e]{1,8192}")


@dataclass(frozen=True)
class Limits:
    max_request_bytes: int = DEFAULT_LIMITS["max_request_bytes"]
    max_response_bytes: int = DEFAULT_LIMITS["max_response_bytes"]
    calls_per_minute: int = DEFAULT_LIMITS["calls_per_minute"]
    call_timeout_seconds: int = DEFAULT_LIMITS["call_timeout_seconds"]

    @classmethod
    def from_answer(cls, raw: Any) -> "Limits":
        """The attach's `limits`, each a positive int held under its ceiling;
        anything else is the default, so a malformed answer never loosens one."""
        raw = raw if isinstance(raw, Mapping) else {}
        values: dict[str, int] = {}
        for name, default in DEFAULT_LIMITS.items():
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                value = default
            values[name] = min(value, CEILINGS[name])
        return cls(**values)


@dataclass(frozen=True)
class Server:
    """One attached server, as the relay holds it. `credential` is never in a
    repr, and is None for a server whose mode is `none`."""

    name: str
    url: str
    host: str
    path: str
    header: str
    scheme: str
    description: str = ""
    revision: int | None = None
    allowed_tools: frozenset[str] = frozenset()
    limits: Limits = field(default_factory=Limits)
    credential: str | None = field(default=None, repr=False)

    def auth_value(self) -> str | None:
        if self.credential is None:
            return None
        return f"{self.scheme} {self.credential}" if self.scheme else self.credential


def address_is_public(address: str) -> bool:
    """Whether a resolved address is one the relay may connect to."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    return bool(ip.is_global) and not ip.is_multicast


def resolve(host: str) -> list[str]:
    """Every address `host` resolves to for TCP on 443."""
    infos = socket.getaddrinfo(host, UPSTREAM_PORT, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a CHECKED address, with SNI and verification for the host."""

    def __init__(self, host: str, address: str, timeout: float) -> None:
        super().__init__(host, UPSTREAM_PORT, timeout=timeout,
                         context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def connect(host: str, address: str, timeout: float) -> http.client.HTTPConnection:
    return _PinnedHTTPS(host, address, timeout)


def credential_rule(text: str) -> str | None:
    """The name of the first credential rule `text` matches, or None."""
    for rule in SHAPE_RULES:
        if rule.apply is not None:
            _, found = rule.apply(text, True, False)
            if found:
                return rule.name
        elif rule.pattern.search(text):
            return rule.name
    return None


def _strings(value: Any) -> Iterable[str]:
    """Every string in a decoded JSON value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _messages(parsed: Any) -> list[Mapping[str, Any]] | None:
    """The JSON-RPC 2.0 messages of a body, or None when it is not one."""
    items = parsed if isinstance(parsed, list) else [parsed]
    if not items:
        return None
    for item in items:
        if not isinstance(item, Mapping) or item.get("jsonrpc") != "2.0":
            return None
    return list(items)


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or code


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"
    server_version = "swarm-mcp-relay"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the stdlib's name
        """Never the request line: the standard library's would log it."""

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """A parse error or an unknown method: a code, never an echo."""
        if code == 501:
            self._plain(404, NOT_FOUND)
        else:
            self._plain(400, BAD_REQUEST)

    def _plain(self, status: int, code: str, body: bytes = b"",
               content_type: str | None = None) -> None:
        self.close_connection = True
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header(REFUSED_HEADER, code)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError:
            pass

    def _other(self) -> None:
        if not self.server.relay.loopback_peer(self.client_address[0]):
            return self._plain(403, NOT_LOOPBACK)
        self._plain(404, NOT_FOUND)

    do_PUT = do_PATCH = do_OPTIONS = do_HEAD = _other

    def do_GET(self) -> None:  # noqa: N802 -- the stdlib's name
        self.server.relay.handle(self, "GET")

    def do_POST(self) -> None:  # noqa: N802 -- the stdlib's name
        self.server.relay.handle(self, "POST")

    def do_DELETE(self) -> None:  # noqa: N802 -- the stdlib's name
        self.server.relay.handle(self, "DELETE")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, relay: "Relay") -> None:
        self.relay = relay
        super().__init__((LOOPBACK, 0), _Handler)


class Relay:
    """The attempt's relay: one listener, one path per server, one local token."""

    def __init__(self, servers: Iterable[Server], *, logger: Any, task_id: str,
                 attempt_id: str,
                 resolver: Callable[[str], list[str]] = resolve,
                 connector: Callable[[str, str, float], http.client.HTTPConnection] = connect,
                 clock: Callable[[], float] = time.monotonic,
                 is_loopback: Callable[[Any], bool] | None = None) -> None:
        self.log = logger
        self._servers: dict[str, Server] = {s.name: s for s in servers}
        self._task_id = task_id
        self._attempt_id = attempt_id
        self._resolve = resolver
        self._connect = connector
        self._clock = clock
        self._is_loopback = is_loopback or _loopback
        self.token = secrets.token_urlsafe(32)
        self._register(self.token)
        for server in self._servers.values():
            self._register(server.credential)
        self._fenced = threading.Event()
        self._lock = threading.Lock()
        self._calls: dict[str, deque[float]] = {name: deque() for name in self._servers}
        self._open: set[http.client.HTTPConnection] = set()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- life --------------------------------------------------------------

    def start(self) -> "Relay":
        self._server = _Server(self)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="mcp-relay", daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the relay is not serving")
        return int(self._server.server_address[1])

    def url_for(self, name: str) -> str:
        return f"http://{LOOPBACK}:{self.port}{PATH_PREFIX}{name}"

    def endpoints(self) -> list[dict[str, str]]:
        """What a runner may be handed: name, relay URL, description. Never a
        credential; the local token is `self.token`, handed beside these."""
        return [{"name": s.name, "url": self.url_for(s.name), "description": s.description}
                for s in self._servers.values()]

    @property
    def names(self) -> list[str]:
        return list(self._servers)

    def mark_fenced(self) -> None:
        """Invariant 5: from now on nothing is forwarded, an open stream included."""
        self._fenced.set()

    @property
    def fenced(self) -> bool:
        return self._fenced.is_set()

    def close(self) -> None:
        """Stop serving, close every upstream connection, drop every
        credential reference. Never raises."""
        self._fenced.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass
        with self._lock:
            conns, self._open = list(self._open), set()
            self._servers = {}
            self._calls = {}
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass

    # -- helpers -----------------------------------------------------------

    def _register(self, value: str | None) -> None:
        register = getattr(self.log, "register_secret", None)
        if value and callable(register):
            register(value)

    def loopback_peer(self, host: Any) -> bool:
        return self._is_loopback(host)

    def _own_values(self) -> list[str]:
        values = [self.token]
        for server in self._servers.values():
            if server.credential:
                values.append(server.credential)
                # As it reads inside a JSON string, should it hold `"` or `\`.
                values.append(json.dumps(server.credential)[1:-1])
        return sorted({v for v in values if v}, key=len, reverse=True)

    def _scrub(self, text: str) -> str:
        scrub = getattr(self.log, "scrub_text", None)
        if callable(scrub):
            text = scrub(text)
        for value in self._own_values():
            if value in text:
                text = text.replace(value, "***REDACTED***")
        return text

    def scrub_bytes(self, data: bytes) -> bytes:
        text = data.decode("utf-8", "surrogateescape")
        return self._scrub(text).encode("utf-8", "surrogateescape")

    def _credential_in(self, texts: Iterable[str]) -> str | None:
        """`registered_secret` or a rule's name for the first text holding one."""
        for text in texts:
            if not text:
                continue
            if self._scrub(text) != text:
                return "registered_secret"
            rule = credential_rule(text)
            if rule is not None:
                return rule
        return None

    def _take_rate(self, name: str, limit: int) -> bool:
        now = self._clock()
        with self._lock:
            window = self._calls.setdefault(name, deque())
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    # -- one request -------------------------------------------------------

    def handle(self, h: _Handler, method: str) -> None:
        started = self._clock()
        record: dict[str, Any] = {"server": None, "http_method": method, "rpc_method": None,
                                  "tool": None, "bytes_in": 0, "bytes_out": 0}
        status, refused = 0, None
        try:
            status = self._handle(h, method, record, started)
        except _Refused as exc:
            status, refused = exc.status, exc.code
            self._refuse(h, exc, record)
        finally:
            self.log.info(
                "mcp_call", server=record["server"], http_method=method,
                rpc_method=record["rpc_method"], tool=record["tool"], status=status,
                refused=refused or record.get("cut"), rule=record.get("rule"),
                bytes_in=record["bytes_in"],
                bytes_out=record["bytes_out"],
                duration_ms=int((self._clock() - started) * 1000))

    def _refuse(self, h: _Handler, exc: _Refused, record: Mapping[str, Any]) -> None:
        ids = record.get("ids")
        if exc.status == 200 and ids is not None:
            # A policy refusal of a parsed JSON-RPC body: an error per request.
            error = {"code": _RPC_REFUSED, "message": exc.message,
                     "data": {"refused": exc.code}}
            answers = [{"jsonrpc": "2.0", "id": i, "error": error} for i in ids] or \
                [{"jsonrpc": "2.0", "id": None, "error": error}]
            payload = answers if record.get("batch") else answers[0]
            body = json.dumps(payload).encode("utf-8")
            return h._plain(200, exc.code, body, "application/json")
        if exc.status == 400 and record.get("parse_failed"):
            body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                "code": _RPC_INVALID, "message": exc.message,
                "data": {"refused": exc.code}}}).encode("utf-8")
            return h._plain(400, exc.code, body, "application/json")
        h._plain(exc.status, exc.code)

    def _handle(self, h: _Handler, method: str, record: dict[str, Any],
                started: float) -> int:
        if not self.loopback_peer(h.client_address[0]):
            raise _Refused(403, NOT_LOOPBACK)
        given = h.headers.get_all("Authorization") or []
        expected = f"Bearer {self.token}".encode("ascii")
        if len(given) != 1 or not hmac.compare_digest(
                given[0].strip().encode("utf-8", "replace"), expected):
            raise _Refused(401, UNAUTHORIZED)
        name = h.path[len(PATH_PREFIX):] if h.path.startswith(PATH_PREFIX) else ""
        server = self._servers.get(name)
        if server is None:
            raise _Refused(404, NOT_FOUND)
        record["server"] = name
        if self.fenced:
            raise _Refused(409, FENCED, "this attempt is fenced; nothing is forwarded")
        if h.headers.get("Transfer-Encoding") is not None:
            raise _Refused(411, LENGTH_REQUIRED)
        length = (h.headers.get("Content-Length") or "").strip()
        body = b""
        if method == "POST":
            if not length.isdigit():
                raise _Refused(411, LENGTH_REQUIRED)
            if int(length) > server.limits.max_request_bytes:
                raise _Refused(413, TOO_LARGE)
            body = h.rfile.read(int(length))
            if len(body) != int(length):
                raise _Refused(400, BAD_REQUEST)
        elif length not in ("", "0"):
            raise _Refused(400, BAD_REQUEST)
        record["bytes_in"] = len(body)
        headers = {n: h.headers.get(n) for n in FORWARDED_HEADERS if h.headers.get(n)}
        decoded: list[str] = []
        if method == "POST":
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            messages = _messages(parsed)
            if messages is None:
                record["parse_failed"] = True
                raise _Refused(400, BAD_REQUEST, "the body is not a JSON-RPC 2.0 message")
            record["batch"] = isinstance(parsed, list)
            record["ids"] = [m.get("id") for m in messages if "id" in m]
            record["rpc_method"] = ",".join(str(m.get("method")) for m in messages
                                            if isinstance(m.get("method"), str))[:200] or None
            # A `tools/call` with no name reads as None, which no list allows.
            tools = [(m["params"].get("name") if isinstance(m.get("params"), Mapping) else None)
                     for m in messages if m.get("method") == "tools/call"]
            record["tool"] = ",".join(str(t) for t in tools)[:200] or None
            if server.allowed_tools and any(t not in server.allowed_tools for t in tools):
                raise _Refused(200, TOOL_NOT_ALLOWED,
                               "this tool is not on the registration's allowed list")
            decoded = list(_strings(parsed))
        found = self._credential_in([body.decode("utf-8", "replace"), *decoded,
                                     *headers.values()])
        if found is not None:
            record["rule"] = found
            raise _Refused(200 if method == "POST" else 403, CREDENTIAL_IN_REQUEST,
                           f"the request carries a credential ({found}); nothing was sent")
        if not self._take_rate(name, server.limits.calls_per_minute):
            raise _Refused(200 if method == "POST" else 429, RATE_LIMITED,
                           "this server's calls-per-minute limit is reached")
        return self._forward(h, method, server, body, headers, record, started)

    def _forward(self, h: _Handler, method: str, server: Server, body: bytes,
                 headers: Mapping[str, str], record: dict[str, Any], started: float) -> int:
        deadline = started + server.limits.call_timeout_seconds
        try:
            addresses = self._resolve(server.host)
        except (OSError, UnicodeError):
            raise _Refused(502, UNRESOLVED) from None
        if not addresses or not all(address_is_public(a) for a in addresses):
            raise _Refused(502, ADDRESS_REFUSED,
                           "the server's address is not a public one; nothing was sent")
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _Refused(504, UPSTREAM_TIMEOUT)
        conn = self._connect(server.host, addresses[0], remaining)
        with self._lock:
            self._open.add(conn)
        try:
            return self._exchange(h, conn, method, server, body, headers, record, deadline)
        finally:
            with self._lock:
                self._open.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _exchange(self, h: _Handler, conn: http.client.HTTPConnection, method: str,
                  server: Server, body: bytes, headers: Mapping[str, str],
                  record: dict[str, Any], deadline: float) -> int:
        try:
            conn.putrequest(method, server.path or "/", skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            auth = server.auth_value()
            if auth is not None:
                conn.putheader(server.header, auth)
            conn.putheader("User-Agent", USER_AGENT)
            conn.putheader(TASK_HEADER, self._task_id)
            conn.putheader(ATTEMPT_HEADER, self._attempt_id)
            if method == "POST":
                conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Connection", "close")
            conn.endheaders(body if method == "POST" else None)
            response = conn.getresponse()
        except ValueError:
            raise _Refused(400, BAD_REQUEST) from None
        except (socket.timeout, TimeoutError):
            raise _Refused(504, UPSTREAM_TIMEOUT) from None
        except (OSError, http.client.HTTPException):
            raise _Refused(502, UPSTREAM_UNREACHABLE) from None
        if 300 <= response.status < 400:
            raise _Refused(502, REDIRECT_REFUSED,
                           "the server answered with a redirect, which is not followed")
        content_type = response.getheader("Content-Type") or ""
        if content_type.split(";", 1)[0].strip().lower() == "text/event-stream":
            return self._stream(h, conn, response, server, record, deadline)
        cap = server.limits.max_response_bytes
        answer = bytearray()
        try:
            while True:
                self._settle(conn, deadline)
                chunk = response.read1(65536)
                if not chunk:
                    break
                answer += chunk
                if len(answer) > cap:
                    raise _Refused(502, RESPONSE_TOO_LARGE)
                if self._clock() >= deadline:
                    raise _Refused(504, UPSTREAM_TIMEOUT)
        except (socket.timeout, TimeoutError):
            raise _Refused(504, UPSTREAM_TIMEOUT) from None
        except (OSError, http.client.HTTPException):
            raise _Refused(502, UPSTREAM_UNREACHABLE) from None
        if self.fenced:
            raise _Refused(409, FENCED)
        out = self.scrub_bytes(bytes(answer))
        h.close_connection = True
        try:
            h.send_response(response.status)
            for name in RETURNED_HEADERS:
                value = response.getheader(name)
                if value:
                    h.send_header(name, self._scrub(value))
            h.send_header("Content-Length", str(len(out)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(out)
        except OSError:
            pass
        record["bytes_out"] = len(out)
        return response.status

    def _settle(self, conn: http.client.HTTPConnection, deadline: float) -> None:
        """The next read may take only what is left of the call's time."""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise socket.timeout()
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.settimeout(remaining)

    def _stream(self, h: _Handler, conn: http.client.HTTPConnection,
                response: http.client.HTTPResponse, server: Server,
                record: dict[str, Any], deadline: float) -> int:
        """An SSE answer, event by event, each scrubbed before it is sent;
        the stream ends at the cap, the deadline, the fence or the close."""
        cap = server.limits.max_response_bytes
        h.close_connection = True
        try:
            h.send_response(response.status)
            for name in RETURNED_HEADERS:
                value = response.getheader(name)
                if value:
                    h.send_header(name, self._scrub(value))
            h.send_header("Cache-Control", "no-store")
            h.send_header("Connection", "close")
            h.end_headers()
        except OSError:
            return response.status
        total = 0
        event = bytearray()
        try:
            while not self.fenced:
                self._settle(conn, deadline)
                line = response.readline(MAX_LINE_BYTES + 1)
                if not line or len(line) > MAX_LINE_BYTES:
                    break
                event += line
                total += len(line)
                if total > cap:
                    record["cut"] = RESPONSE_TOO_LARGE
                    break
                if line in (b"\n", b"\r\n"):
                    out = self.scrub_bytes(bytes(event))
                    event.clear()
                    if self.fenced:
                        break
                    h.wfile.write(out)
                    h.wfile.flush()
                    record["bytes_out"] += len(out)
                if self._clock() >= deadline:
                    record["cut"] = UPSTREAM_TIMEOUT
                    break
        except (socket.timeout, TimeoutError):
            record["cut"] = UPSTREAM_TIMEOUT
        except (OSError, http.client.HTTPException, ValueError):
            pass
        return response.status


def _loopback(host: Any) -> bool:
    try:
        address = ipaddress.ip_address(str(host))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def header_safe(value: Any) -> bool:
    """A value that can be sent as one header line."""
    return isinstance(value, str) and bool(_TOKEN_SAFE.fullmatch(value))


__all__ = [
    "ATTEMPT_HEADER", "CEILINGS", "DEFAULT_LIMITS", "FORWARDED_HEADERS", "LOOPBACK",
    "PATH_PREFIX", "REFUSED_HEADER", "RETURNED_HEADERS", "SHAPE_RULES", "TASK_HEADER",
    "USER_AGENT", "Limits", "Relay", "Server", "address_is_public", "connect",
    "credential_rule", "header_safe", "resolve",
]
