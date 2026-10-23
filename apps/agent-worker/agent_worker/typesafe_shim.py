"""The TypeSafe forwarder's ticket and the worker's loopback shim
(docs/design/workspace-secrets.md §3.5, SEC-MIGRATE-TYPESAFE; lane
TS-MIGRATE-WORKER, part of #1347).

With the `inject` delivery mode the TypeSafe key never enters the pod: the
forwarder in swarm-system (`apps/swarm-typesafe-fwd/`) reads it with its own
identity and adds it to each request, and the pod presents a short-lived
ticket instead. This module is the worker's half.

THE SWITCH. `SWARM_TYPESAFE_INJECT=on` in the worker's environment, OFF by
default. The scheduler sets it on the pods it labels `swarm-typesafe: inject`
(lane TS-MIGRATE-POD), so the variable and the label always arrive together.
It is honoured only on GKE (`GKE_AUTOPILOT`): the forwarder is a cluster
Service, and a Cloud Run worker could never reach it. A Cloud Run worker with
the variable set keeps today's key file. Switched off, nothing here runs and
nothing is constructed beyond this object: the jev key file, the judge's key
and `JEV_API_URL` are exactly what they were.

THE TICKET. `POST /v1/attempts/{attempt_id}/typesafe-ticket`
(`swarm_api.typesafe_ticket`), proven with the attempt key in this heap under
the route's own purpose, TYPESAFE_TICKET_PURPOSE, with `/live-session`'s body.
swarm-api checks the fence (RUNNING under this attempt's live lease at this
generation), the workspace's judgment backend and which key resolves, and
answers `{ticket, expires_in, scope}`. It is renewed when a third of its life
is left, on the lifecycle's control poll, as the egress and graph tickets are,
and on demand when the forwarder says `ticket_invalid`. The forwarder allows
no grace past `exp`, so a ticket within EXPIRY_MARGIN_SECONDS of it is never
sent. It lives in this process's memory only: never a file, never a log line
(each one is registered for redaction as it arrives).

THE FENCE (invariant 5). A 409 from the mint, or a 409 `stale_generation`
from the forwarder, makes this attempt fenced: the shim refuses every later
request without forwarding it, nothing more is minted, and the lifecycle's
next poll exits fenced without touching the lease (`TypesafeTicketFenced`).
A stale worker therefore gets nothing.

THE SHIM, AND WHY NOT A FILE. jev-browser reads TYPESAFE_API_KEY once at
start (`jev.mjs` caches it in `cachedKey`) and reads `JEV_API_URL` once, into
a module constant, while a ticket lives minutes. A file jev re-reads would
need a change to jev-browser itself; a loopback shim needs only the URL. So
the worker serves `POST /v1/systemone` on 127.0.0.1 (an ephemeral port) and,
for each request, opens ONE connection to the forwarder at exactly
FORWARDER_URL with `Host` exactly FORWARDER_HOST, the CURRENT ticket as the
one `Authorization` bearer, the body with a `Content-Length` (never chunked),
`Connection: close`, and only `Content-Type`, `Accept` and `User-Agent` of the
caller's headers. The answer goes back with its status, `Content-Type` and the
forwarder's `X-Swarm-Typesafe-Refused` code, nothing else. The judge's calls
take the same path (`judge/settings.py`), so the attempt has one ticket.

WHO MAY CALL IT. Only a loopback peer: the server binds 127.0.0.1, and every
connection's peer address is checked again before anything is read. And only
with the shim's local token as the bearer: 32 random bytes made here, written
with the shim's URL to `private/jev-typesafe.shim` (mode 0600, never
checkpointed, never uploaded: `workspace.py`), which `jev_launch.py` and the
judge read. The token is worth nothing beyond this pod's loopback and this
attempt. It is what keeps a web page in the live browser, which shares the
pod's network, from spending the workspace's TypeSafe credit: a page cannot
send an `Authorization` header to another origin without a CORS preflight,
and the shim answers no OPTIONS.

NOTHING HERE LOGS a ticket, the local token, a body, a header or a signature.
Log lines carry codes, statuses and byte counts.
"""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

#: The worker's switch, set by the scheduler with the pod's
#: `swarm-typesafe: inject` label (TS-MIGRATE-POD). Exactly `on` turns it on.
SWITCH_ENV = "SWARM_TYPESAFE_INJECT"
SWITCH_ON = "yes"
#: `swarm_common.profiles.Backend.GKE_AUTOPILOT`: the only backend that can
#: reach the forwarder's cluster Service.
GKE_BACKEND = "GKE_AUTOPILOT"

#: The forwarder, exactly as SEC-INJECT-B serves it (§3.5, "What the built
#: forwarder asks of that lane"): a short Host is its 404.
FORWARDER_HOST = "swarm-typesafe-fwd.swarm-system.svc.cluster.local"
FORWARDER_PORT = 8080
FORWARDER_PATH = "/v1/systemone"
FORWARDER_URL = f"http://{FORWARDER_HOST}:{FORWARDER_PORT}{FORWARDER_PATH}"
#: The forwarder's refusal header, passed back to the caller as a code.
REFUSED_HEADER = "X-Swarm-Typesafe-Refused"
#: The shim's own refusals, so a caller can tell them from the forwarder's.
SHIM_REFUSED_HEADER = "X-Swarm-Typesafe-Shim"

#: `swarm_api.typesafe_ticket.TICKET_PURPOSE`, restated (this image does not
#: carry swarm-api); tests/unit/worker/test_typesafe_shim.py holds them equal.
TYPESAFE_TICKET_PURPOSE = "swarm-typesafe-ticket-request/v1"
#: `swarm_api.typesafe_ticket.MAX_TICKET_SECONDS`: no answer claiming more
#: life is believed, so a misconfigured setting cannot push renewal past exp.
MAX_TICKET_SECONDS = 900
MAX_TICKET_BYTES = 4096
_TICKET = re.compile(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

#: Renew once this fraction of the ticket's life is left (`egress_ticket`'s):
#: swarm-api's 300 s default is renewed at 100 s left, several control polls
#: and backoffs before the forwarder would refuse it.
RENEW_FRACTION = 1 / 3
#: A ticket this close to `exp` is not sent: the forwarder allows no grace,
#: and the request still has to reach it.
EXPIRY_MARGIN_SECONDS = 5.0
RETRY_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0

#: The file `jev_launch.py` and the judge read: the shim's URL and local
#: token, never a ticket and never a key. In `private/`.
SHIM_FILE_NAME = "jev-typesafe.shim"
SHIM_URL_FIELD = "shim_url"
TOKEN_FIELD = "token"
LOOPBACK = "127.0.0.1"
#: The longest body the shim reads; the forwarder holds its own, smaller
#: `max_request_bytes` and answers 413 itself.
MAX_BODY_BYTES = 4 * 1024 * 1024
#: The longest answer the shim relays.
MAX_ANSWER_BYTES = 16 * 1024 * 1024
#: The forwarder's own deadline is `max_response_seconds`; this only stops a
#: connection that never answers from holding a thread forever.
FORWARD_TIMEOUT_SECONDS = 120.0
#: The one refusal code shape passed back: a word, never a value.
_CODE = re.compile(r"[a-z_]{1,40}")

#: Why there is no ticket, as logged.
NO_ATTEMPT_KEY = "no_attempt_key"
STALE_GENERATION = "stale_generation"
TICKET_INVALID = "ticket_invalid"


class TypesafeTicketFenced(Exception):
    """This attempt is superseded (invariant 5). The message is a code."""


class TicketUnavailable(Exception):
    """No ticket this time: `cause` is a word, `retryable` whether asking again may help."""

    def __init__(self, cause: str, *, retryable: bool) -> None:
        super().__init__(cause)
        self.cause = cause
        self.retryable = retryable


def switched_on(environ: Mapping[str, str], backend: str | None) -> bool:
    """Whether this worker uses the forwarder: the switch, on GKE only."""
    return (environ.get(SWITCH_ENV) or "").strip() == SWITCH_ON and backend == GKE_BACKEND


def is_loopback(host: Any) -> bool:
    """Whether a peer address is this host's loopback (an IPv4-mapped one included)."""
    try:
        address = ipaddress.ip_address(str(host))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def mint(cfg: Any, key: Any, api: Any, *, now: float) -> tuple[str, int]:
    """Ask swarm-api for this attempt's TypeSafe ticket: `(ticket, expires_in)`.

    The body is `/live-session`'s; the signature is under
    TYPESAFE_TICKET_PURPOSE. Raises TypesafeTicketFenced on a 409 and
    TicketUnavailable for everything else, the answer's shape included.
    """
    from .handoff import sign_headers

    body = json.dumps({
        "tenant_id": cfg.tenant_id,
        "task_id": cfg.task_id,
        "attempt_id": cfg.attempt_id,
        "lease_id": cfg.lease_id,
        "generation": cfg.generation,
    }).encode("utf-8")
    path = f"/v1/attempts/{quote(cfg.attempt_id, safe='')}/typesafe-ticket"
    try:
        headers = sign_headers(key, TYPESAFE_TICKET_PURPOSE, "POST", path, body, now=now)
        _, payload = api.call("POST", path, body=body, headers=headers)
    except Exception as exc:
        status = getattr(exc, "status", None)
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        if status == 409:
            raise TypesafeTicketFenced(f"typesafe ticket: 409 {code}") from None
        retryable = getattr(exc, "retryable", None)
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            # 403 backend off / no key / unproven: no ticket will come.
            raise TicketUnavailable(code, retryable=False) from None
        if status == 503 and code == "typesafe_unavailable":
            # The deployment has no forwarder key: asking again changes nothing.
            raise TicketUnavailable(code, retryable=False) from None
        raise TicketUnavailable(code, retryable=retryable is not False) from None
    payload = payload if isinstance(payload, Mapping) else {}
    ticket = payload.get("ticket")
    seconds = payload.get("expires_in")
    if not isinstance(ticket, str) or len(ticket) > MAX_TICKET_BYTES \
            or not _TICKET.fullmatch(ticket):
        raise TicketUnavailable("not_a_ticket", retryable=True)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise TicketUnavailable("not_a_ticket", retryable=True)
    return ticket, min(seconds, MAX_TICKET_SECONDS)


def _write_private(path: Path, text: str) -> None:
    """`text` at `path`, mode 0600, by a new temporary renamed into place, so
    a reader sees the old file or the new one and a planted link is replaced."""
    from .graph_ticket import write_ticket

    write_ticket(path, text)


def read_shim_file(path: Path) -> tuple[str, str] | None:
    """`(shim_url, token)` from the shim file, or None when it is not one."""
    try:
        if Path(path).is_symlink():
            return None
        parsed = json.loads(Path(path).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {SHIM_URL_FIELD, TOKEN_FIELD}:
        return None
    url, token = parsed[SHIM_URL_FIELD], parsed[TOKEN_FIELD]
    if not isinstance(url, str) or not isinstance(token, str) or not url or not token:
        return None
    return url, token


class TypesafeTicket:
    """One attempt's TypeSafe ticket, in memory: the first mint, the renewals
    on the control poll and the shim's on-demand renewal. Thread-safe: the
    shim's request threads and the lifecycle's poll share it."""

    def __init__(self, cfg: Any, *, children: Any, logger: Any,
                 wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.children = children
        self.log = logger
        self._wall = wall
        self._lock = threading.Lock()
        self._ticket: str | None = None
        self._expires_at = 0.0
        self._renew_at = 0.0
        self.retry_at = 0.0
        self._backoff = RETRY_SECONDS
        self.renewals = 0
        #: Asked for at least once: `due` is False before `start`.
        self.started = False
        #: A permanent refusal: nothing more is asked this attempt.
        self.ended = False
        #: Why this attempt is fenced, once it is; then nothing is sent or minted.
        self.fenced: str | None = None

    def _credentials(self) -> tuple[Any, Any] | None:
        key = getattr(self.children, "_key", None)
        api = getattr(self.children, "_api", None)
        if key is None or api is None or not getattr(self.children, "keyed", False):
            return None
        return key, api

    def start(self) -> str | None:
        """The first ticket. None when one is held, else why not. Raises only
        TypesafeTicketFenced. A transient failure leaves it to the poll."""
        self.started = True
        if self._credentials() is None:
            self.ended = True
            return NO_ATTEMPT_KEY
        with self._lock:
            return self._mint_locked(first=True)

    def current(self) -> str | None:
        """The ticket to send now, or None: never one within the margin of exp."""
        with self._lock:
            if self.fenced or self._ticket is None:
                return None
            if self._wall() >= self._expires_at - EXPIRY_MARGIN_SECONDS:
                return None
            return self._ticket

    def renew_now(self, stale: str | None = None) -> str | None:
        """The shim's renewal: the forwarder refused `stale` (or there was
        none). Returns the ticket to send, or None. Never raises: a fence
        found here is recorded for the poll."""
        with self._lock:
            if self.fenced or self.ended:
                return None
            if self._ticket is not None and self._ticket != stale \
                    and self._wall() < self._expires_at - EXPIRY_MARGIN_SECONDS:
                return self._ticket  # another thread already renewed it
            try:
                self._mint_locked(first=False)
            except TypesafeTicketFenced as exc:
                self.fenced = str(exc)
                return None
            if self._ticket is None or self._ticket == stale \
                    or self._wall() >= self._expires_at - EXPIRY_MARGIN_SECONDS:
                return None  # the mint failed: never the old one, never an expired one
            return self._ticket

    def mark_fenced(self, why: str) -> None:
        """The forwarder said `stale_generation`: stop, and let the poll exit."""
        with self._lock:
            self.fenced = why
            self._ticket = None
        self.log.warning("typesafe ticket fenced; nothing more is sent", code=why)

    def due(self) -> bool:
        """Whether `keep` has anything to do now: a cheap check for every poll."""
        if self.fenced:
            return True
        if not self.started or self.ended:
            return False
        now = self._wall()
        return now >= self._renew_at and now >= self.retry_at

    def keep(self) -> None:
        """Renew when due. Raises TypesafeTicketFenced once the attempt is fenced."""
        if self.fenced:
            raise TypesafeTicketFenced(self.fenced)
        if not self.due():
            return
        if self._credentials() is None:
            self.ended = True
            return
        with self._lock:
            self._mint_locked(first=False)

    def forget(self) -> None:
        """The attempt is over: drop the ticket, ask for nothing more."""
        with self._lock:
            self._ticket = None
            self.ended = True

    # -- internals (the lock is held) ---------------------------------------------

    def _mint_locked(self, *, first: bool) -> str | None:
        found = self._credentials()
        if found is None:
            self.ended = True
            return NO_ATTEMPT_KEY
        key, api = found
        now = self._wall()
        try:
            ticket, seconds = mint(self.cfg, key, api, now=now)
        except TypesafeTicketFenced:
            self._ticket = None
            raise
        except TicketUnavailable as failed:
            if not failed.retryable:
                self.ended = True
                self.log.warning("typesafe ticket refused; it is not asked for again",
                                 code=failed.cause)
            else:
                self._retry_later(failed.cause)
            return failed.cause
        register = getattr(self.log, "register_secret", None)
        if callable(register):
            register(ticket)
        if not first and self._ticket is not None:
            self.renewals += 1
        self._ticket = ticket
        ticket = ""
        self._expires_at = now + seconds
        self._renew_at = now + seconds * (1 - RENEW_FRACTION)
        self.retry_at = 0.0
        self._backoff = RETRY_SECONDS
        self.log.info("typesafe ticket minted" if first else "typesafe ticket renewed",
                      expires_in=seconds, renewals=self.renewals)
        return None

    def _retry_later(self, cause: str) -> None:
        self.retry_at = self._wall() + self._backoff
        self.log.warning("typesafe ticket not minted; trying again", code=cause,
                         retry_seconds=int(self._backoff))
        self._backoff = min(self._backoff * 2, RETRY_MAX_SECONDS)


def forwarder_connection() -> http.client.HTTPConnection:
    """One connection to the forwarder's Service, for one request."""
    return http.client.HTTPConnection(FORWARDER_HOST, FORWARDER_PORT,
                                      timeout=FORWARD_TIMEOUT_SECONDS)


class _Handler(BaseHTTPRequestHandler):
    """One request per connection (HTTP/1.0 semantics, `Connection: close`)."""

    server: "_Server"
    protocol_version = "HTTP/1.0"
    server_version = "swarm-typesafe-shim"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the stdlib's name
        """Never the request line: the standard library's would log it."""

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """A parse error or an unknown method: a code, never an echo of the request."""
        if code == 501:  # an unknown method is a path that does not exist
            self._answer(404, b"", refused="not_found")
        else:
            self._answer(400, b"", refused="bad_request")

    def _answer(self, status: int, body: bytes, *, refused: str | None = None,
                content_type: str | None = None, forwarder_code: str | None = None) -> None:
        self.close_connection = True
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            if refused:
                self.send_header(SHIM_REFUSED_HEADER, refused)
            if forwarder_code:
                self.send_header(REFUSED_HEADER, forwarder_code)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass

    def _not_found(self) -> None:
        if not is_loopback(self.client_address[0]):
            return self._answer(403, b"", refused="not_loopback")
        self._answer(404, b"", refused="not_found")

    do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _not_found

    def do_POST(self) -> None:  # noqa: N802 -- the stdlib's name
        if not is_loopback(self.client_address[0]):
            # Before anything of the request is read or used.
            self.server.shim.count("not_loopback")
            return self._answer(403, b"", refused="not_loopback")
        if self.path != FORWARDER_PATH:
            return self._answer(404, b"", refused="not_found")
        given = self.headers.get_all("Authorization") or []
        expected = f"Bearer {self.server.shim.token}".encode("ascii")
        if len(given) != 1 or not hmac.compare_digest(given[0].strip().encode("utf-8", "replace"),
                                                      expected):
            self.server.shim.count("unauthorized")
            return self._answer(401, b"", refused="unauthorized")
        if self.headers.get("Transfer-Encoding") is not None:
            return self._answer(400, b"", refused="bad_request")
        length = (self.headers.get("Content-Length") or "").strip()
        if not length.isdigit():
            return self._answer(411, b"", refused="length_required")
        size = int(length)
        if size > MAX_BODY_BYTES:
            return self._answer(413, b"", refused="too_large")
        body = self.rfile.read(size)
        if len(body) != size:
            return self._answer(400, b"", refused="bad_request")
        status, content_type, code, answer, refused = self.server.shim.forward(
            body, {name: self.headers.get(name) for name in ("Content-Type", "Accept",
                                                             "User-Agent")})
        self._answer(status, answer, refused=refused, content_type=content_type,
                     forwarder_code=code)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, shim: "LoopbackShim") -> None:
        self.shim = shim
        super().__init__((LOOPBACK, 0), _Handler)


class LoopbackShim:
    """The 127.0.0.1 server jev-browser and the judge call; see the module docstring."""

    def __init__(self, ticket: TypesafeTicket, *, logger: Any,
                 connect: Callable[[], http.client.HTTPConnection] = forwarder_connection) -> None:
        self.ticket = ticket
        self.log = logger
        self._connect = connect
        self.token = secrets.token_urlsafe(32)
        register = getattr(logger, "register_secret", None)
        if callable(register):
            register(self.token)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.path: Path | None = None
        self.counts: dict[str, int] = {}
        self._counts_lock = threading.Lock()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the shim is not serving")
        return f"http://{LOOPBACK}:{self._server.server_address[1]}{FORWARDER_PATH}"

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("the shim is not serving")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self, private_dir: Path) -> Path:
        """Serve, then write the shim file into `private_dir`; its path."""
        self._server = _Server(self)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="typesafe-shim", daemon=True)
        self._thread.start()
        path = Path(private_dir) / SHIM_FILE_NAME
        _write_private(path, json.dumps({SHIM_URL_FIELD: self.url, TOKEN_FIELD: self.token},
                                        sort_keys=True))
        self.path = path
        return path

    def stop(self) -> None:
        """Stop serving and remove the shim file. Never raises."""
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def count(self, what: str) -> None:
        with self._counts_lock:
            self.counts[what] = self.counts.get(what, 0) + 1

    def forward(self, body: bytes, headers: Mapping[str, str | None]
                ) -> tuple[int, str | None, str | None, bytes, str | None]:
        """`(status, content type, forwarder code, answer, shim code)`.

        A `ticket_invalid` is renewed and sent once more; a `stale_generation`
        fences the attempt. Nothing is sent without a live ticket.
        """
        if self.ticket.fenced:
            self.count(STALE_GENERATION)
            return 409, None, None, b"", STALE_GENERATION
        ticket = self.ticket.current() or self.ticket.renew_now()
        for round_ in range(2):
            if ticket is None:
                why = STALE_GENERATION if self.ticket.fenced else "no_ticket"
                self.count(why)
                return (409 if self.ticket.fenced else 503), None, None, b"", why
            try:
                status, content_type, code, answer = self._send(ticket, body, headers)
            except ValueError:
                # A caller header that cannot be sent (a CR or LF in it).
                return 400, None, None, b"", "bad_request"
            except (socket.timeout, TimeoutError):
                self.count("forwarder_timeout")
                return 504, None, None, b"", "forwarder_timeout"
            except (OSError, http.client.HTTPException):
                self.count("forwarder_unreachable")
                return 502, None, None, b"", "forwarder_unreachable"
            if status == 401 and code == TICKET_INVALID and round_ == 0:
                ticket = self.ticket.renew_now(stale=ticket)
                continue
            if status == 409 and code == STALE_GENERATION:
                self.ticket.mark_fenced(STALE_GENERATION)
            self.count(f"status_{status}")
            self.log.info("typesafe_shim_forwarded", status=status, code=code,
                          bytes_in=len(body), bytes_out=len(answer))
            return status, content_type, code, answer, None
        return 502, None, None, b"", "ticket_invalid"  # pragma: no cover -- the loop returns

    def _send(self, ticket: str, body: bytes, headers: Mapping[str, str | None]
              ) -> tuple[int, str | None, str | None, bytes]:
        conn = self._connect()
        try:
            conn.putrequest("POST", FORWARDER_PATH, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", FORWARDER_HOST)
            conn.putheader("Authorization", f"Bearer {ticket}")
            conn.putheader("Content-Type", headers.get("Content-Type") or "application/json")
            for name in ("Accept", "User-Agent"):
                value = headers.get(name)
                if value:
                    conn.putheader(name, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Connection", "close")
            conn.endheaders(body)
            response = conn.getresponse()
            answer = response.read(MAX_ANSWER_BYTES + 1)
            if len(answer) > MAX_ANSWER_BYTES:
                raise http.client.HTTPException("answer too large")
            code = (response.getheader(REFUSED_HEADER) or "").strip()
            return (response.status, response.getheader("Content-Type"),
                    code if _CODE.fullmatch(code) else None, answer)
        finally:
            ticket = ""
            conn.close()


class TypesafeInject:
    """The attempt's whole `inject` path, as the lifecycle holds it.

    `on` is decided once, from the switch and the backend. Off, every method
    is a no-op and `shim_file` is None, so the attempt runs exactly as before.
    """

    def __init__(self, cfg: Any, *, children: Any, logger: Any, environ: Mapping[str, str],
                 backend: str | None, wall: Callable[[], float] = time.time,
                 connect: Callable[[], http.client.HTTPConnection] = forwarder_connection) -> None:
        self.on = switched_on(environ, backend)
        self.log = logger
        self.ticket = TypesafeTicket(cfg, children=children, logger=logger, wall=wall)
        self._connect = connect
        self.shim: LoopbackShim | None = None
        #: Why there is no shim, once `start` has decided; None while there is one.
        self.unavailable: str | None = None

    @property
    def shim_file(self) -> Path | None:
        return self.shim.path if self.shim is not None else None

    @property
    def fenced(self) -> bool:
        return bool(self.ticket.fenced)

    def start(self, private_dir: Path) -> Path | None:
        """Mint the first ticket and serve the shim; the shim file, or None.

        Raises only TypesafeTicketFenced. A refusal (the workspace's backend
        is not TypeSafe, no key resolves, no attempt key) is no shim: jev is
        not registered and the judge is `no_key`, and nothing reaches the key
        file's path either. A transient failure still serves the shim; the
        poll and the shim itself ask again.
        """
        if not self.on or self.shim is not None:
            return self.shim_file
        cause = self.ticket.start()
        if self.ticket.ended:
            self.unavailable = cause or "refused"
            self.log.warning("typesafe forwarder unavailable for this attempt",
                             code=self.unavailable)
            return None
        shim = LoopbackShim(self.ticket, logger=self.log, connect=self._connect)
        try:
            path = shim.start(private_dir)
        except OSError as exc:
            shim.stop()
            self.unavailable = type(exc).__name__
            self.log.warning("typesafe shim not started", error=self.unavailable)
            return None
        self.shim = shim
        self.log.info("typesafe shim serving", pending=cause)
        return path

    def due(self) -> bool:
        return self.on and self.ticket.due()

    def keep(self) -> None:
        if self.on:
            self.ticket.keep()

    def close(self) -> None:
        """Stop the shim and drop the ticket, on every exit. Never raises."""
        if self.shim is not None:
            self.shim.stop()
        self.ticket.forget()


__all__ = [
    "FORWARDER_HOST", "FORWARDER_PATH", "FORWARDER_PORT", "FORWARDER_URL", "GKE_BACKEND",
    "LOOPBACK", "MAX_TICKET_SECONDS", "REFUSED_HEADER", "SHIM_FILE_NAME", "SHIM_REFUSED_HEADER",
    "SHIM_URL_FIELD", "SWITCH_ENV", "SWITCH_ON", "TOKEN_FIELD", "TYPESAFE_TICKET_PURPOSE",
    "LoopbackShim", "TicketUnavailable", "TypesafeInject", "TypesafeTicket",
    "TypesafeTicketFenced", "forwarder_connection", "is_loopback", "mint", "read_shim_file",
    "switched_on",
]
