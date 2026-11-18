"""The forwarding decision (docs/design/workspace-secrets.md §3.5).

One request, from a labelled agent pod:

    POST /v1/systemone HTTP/1.1
    Host: swarm-typesafe-fwd.swarm-system.svc.cluster.local[:8080]

with the attempt TypeSafe ticket as the bearer credential of the
Authorization header,

checked in this order, each refusal answered with a status, an
`X-Swarm-Typesafe-Refused: <code>` header and one log line, and NOTHING
forwarded and no secret read before the last check passes:

    not_found         404  any other method, path, query, target form or Host
    disabled          503  the kill switch, or a setting that is missing, not
                           JSON or has an unknown key (fail closed)
    ticket_invalid    401  missing, malformed, forged, expired, or naming a
                           secret its tenant may not spend (ticket.py)
    tenant_off        403  the ticket's tenant is not in the setting's tenants
    stale_generation  409  a higher generation of the task has been seen
                           (invariant 5)
    rate_limited      429  past requests_per_minute for this attempt
    too_large         413  a body over max_request_bytes
    bad_request       400  a chunked or unparseable body length
    key_unreadable    502  the named version could not be read
    upstream_timeout  504  TypeSafe did not answer in max_response_seconds
    upstream_failed   502  TypeSafe could not be reached

then a POST to the constant upstream (upstream.py) carrying ONLY the caller's
Content-Type, Accept and User-Agent and an Authorization header this module
sets, bearer scheme, to the key: the caller's Authorization, cookies,
forwarding headers and everything else are dropped. The answer is streamed
back with the upstream's status and Content-Type alone.

Generation fencing and the rate limit are in memory, which is why the
Deployment has one replica (kubernetes/swarm-system/swarm-typesafe-fwd.yaml).

THE LOG carries the code, the status, attempt_id, tenant_id, task_id, scope,
secret_id, byte counts and the duration. Never a header, a body, a ticket, a
key or a value; every line goes through swarm_redaction with the MAC keys and
every held value registered as literals, so even a bug that put one in a
field would print the mask.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

from swarm_redaction import redact

from .config import FwdConfig
from .keyring import KeyRing, KeyUnreadable, SecretCache
from .ticket import MAX_TICKET_SECONDS, Ticket, TicketRefused, verify_ticket
from .upstream import Upstream, UpstreamFailed, UpstreamTimeout

log = logging.getLogger("swarm_typesafe_fwd")

SERVICE_HOST = "swarm-typesafe-fwd.swarm-system.svc.cluster.local"
PORT = 8080
PATH = "/v1/systemone"
#: The caller's headers that are forwarded, by their canonical spelling; any
#: other is dropped. Authorization is this module's, never the caller's.
FORWARDED_HEADERS = ("Content-Type", "Accept", "User-Agent")
MAX_FORWARDED_HEADER_CHARS = 512
RATE_WINDOW_SECONDS = 60.0
#: Fencing entries outlive every ticket that could still be presented for
#: them: the newest exp seen for the task, plus a ticket's longest life.
FENCE_KEEP_SECONDS = float(MAX_TICKET_SECONDS)

STATUS = {
    "not_found": 404, "disabled": 503, "ticket_invalid": 401, "tenant_off": 403,
    "stale_generation": 409, "rate_limited": 429, "too_large": 413, "bad_request": 400,
    "key_unreadable": 502, "upstream_timeout": 504, "upstream_failed": 502,
}

BodyReader = Callable[[int], bytes]


def event(name: str, *, secrets: Iterable[bytes | str] = (), **fields: object) -> None:
    """One log line: JSON, redacted with `secrets` registered, and only the
    fields a caller passes, which are never a header, a body or a ticket."""
    literals = [s.decode("utf-8", "replace") if isinstance(s, bytes) else s for s in secrets]
    log.info(redact(json.dumps({"event": name, **fields}, sort_keys=True), extra=literals).text)


@dataclass
class Reply:
    """What the server writes: a status, headers, and the body's chunks.
    `finish` is called once the body has been written (or broke off), with
    the bytes written and the cause of a break, and writes the log line."""

    status: int
    headers: list[tuple[str, str]]
    chunks: Iterable[bytes]
    finish: Callable[[int, str | None], None] = field(default=lambda n, cause: None)
    #: The refusal code, or None when forwarded.
    code: str | None = None


class Forwarder:
    def __init__(self, *, keys: KeyRing, secrets: SecretCache, config: Callable[[], FwdConfig],
                 upstream: Upstream, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] | None = None) -> None:
        self._keys = keys
        self._secrets = secrets
        self._config = config
        self._upstream = upstream
        self._clock = clock
        self._monotonic = monotonic or clock
        self._lock = threading.Lock()
        #: task_id -> (highest generation seen, keep until).
        self._fence: dict[str, tuple[int, float]] = {}
        #: attempt_id -> times of the requests counted in the last minute.
        self._rate: dict[str, collections.deque[float]] = {}

    # -- logging -----------------------------------------------------------
    def _secret_literals(self) -> list[bytes | str]:
        return [*self._keys.keys(), *self._secrets.held()]

    def _log(self, name: str, **fields: object) -> None:
        event(name, secrets=self._secret_literals(), **fields)

    # -- refusals ----------------------------------------------------------
    def _refuse(self, code: str, started: float, ticket: Ticket | None = None,
                cause: str | None = None, bytes_in: int = 0) -> Reply:
        status = STATUS[code]
        body = json.dumps({"error": code}).encode()
        fields: dict[str, object] = {"code": code, "status": status, "bytes_in": bytes_in,
                                     "duration_ms": self._ms(started)}
        if ticket is not None:
            fields.update(_ids(ticket))
        if cause is not None:
            fields["cause"] = cause
        self._log("typesafe_refused", **fields)
        return Reply(status=status, code=code, chunks=[body], headers=[
            ("Content-Type", "application/json"),
            ("X-Swarm-Typesafe-Refused", code),
            ("Content-Length", str(len(body))),
        ])

    def _ms(self, started: float) -> int:
        return int((self._monotonic() - started) * 1000)

    # -- the checks --------------------------------------------------------
    @staticmethod
    def _routed(method: str, target: str, headers: Sequence[tuple[str, str]]) -> bool:
        if method != "POST" or target != PATH:
            return False
        hosts = [v for k, v in headers if k.lower() == "host"]
        if len(hosts) != 1:
            return False
        host = hosts[0].strip().lower()
        return host in (SERVICE_HOST, f"{SERVICE_HOST}:{PORT}")

    def _fenced(self, ticket: Ticket, now: float) -> bool:
        """True when a higher generation of the task has been seen."""
        with self._lock:
            for task in [t for t, (_, until) in self._fence.items() if until < now]:
                del self._fence[task]
            seen, until = self._fence.get(ticket.task_id, (-1, 0.0))
            if ticket.generation < seen:
                return True
            keep = max(until, ticket.exp + FENCE_KEEP_SECONDS)
            self._fence[ticket.task_id] = (ticket.generation, keep)
            return False

    def _limited(self, ticket: Ticket, now: float, per_minute: int) -> bool:
        with self._lock:
            for attempt in [a for a, q in self._rate.items()
                            if not q or q[-1] <= now - RATE_WINDOW_SECONDS]:
                del self._rate[attempt]
            window = self._rate.setdefault(ticket.attempt_id, collections.deque())
            while window and window[0] <= now - RATE_WINDOW_SECONDS:
                window.popleft()
            if len(window) >= per_minute:
                return True
            window.append(now)
            return False

    @staticmethod
    def _length(headers: Sequence[tuple[str, str]]) -> int | None:
        """The body's length, or None when it cannot be framed safely."""
        if any(k.lower() == "transfer-encoding" for k, _ in headers):
            return None
        lengths = [v.strip() for k, v in headers if k.lower() == "content-length"]
        if not lengths:
            return 0
        if len(set(lengths)) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 12:
            return None
        return int(lengths[0])

    @staticmethod
    def _forwarded(headers: Sequence[tuple[str, str]], key: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in FORWARDED_HEADERS:
            values = [v.strip() for k, v in headers if k.lower() == name.lower()]
            if len(values) == 1 and _header_safe(values[0]):
                out[name] = values[0]
        out["Authorization"] = f"Bearer {key}"
        return out

    # -- the request -------------------------------------------------------
    def handle(self, method: str, target: str, headers: Sequence[tuple[str, str]],
               read_body: BodyReader) -> Reply:
        started = self._monotonic()
        if not self._routed(method, target, headers):
            return self._refuse("not_found", started)
        config = self._config()
        if not config.enabled:
            return self._refuse("disabled", started, cause=config.cause)

        auths = [v for k, v in headers if k.lower() == "authorization"]
        raw = auths[0].strip() if len(auths) == 1 else ""
        scheme, _, presented = raw.partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            return self._refuse("ticket_invalid", started, cause="no_ticket")
        now = self._clock()
        try:
            ticket = verify_ticket(self._keys.keys(), presented.strip(), now=now)
        except TicketRefused as refused:
            return self._refuse("ticket_invalid", started, cause=refused.cause)

        if ticket.tenant_id not in config.tenants:
            return self._refuse("tenant_off", started, ticket)
        if self._fenced(ticket, now):
            return self._refuse("stale_generation", started, ticket)
        if self._limited(ticket, now, config.requests_per_minute):
            return self._refuse("rate_limited", started, ticket)

        length = self._length(headers)
        if length is None:
            return self._refuse("bad_request", started, ticket, cause="length")
        if length > config.max_request_bytes:
            return self._refuse("too_large", started, ticket)
        body = read_body(length) if length else b""
        if len(body) != length:
            return self._refuse("bad_request", started, ticket, cause="short_body")

        try:
            key = self._secrets.value(ticket.secret_id, ticket.secret_version)
        except KeyUnreadable as exc:
            return self._refuse("key_unreadable", started, ticket, cause=str(exc),
                                bytes_in=length)
        try:
            answer = self._upstream.send(body, self._forwarded(headers, key),
                                         float(config.max_response_seconds))
        except UpstreamTimeout:
            return self._refuse("upstream_timeout", started, ticket, bytes_in=length)
        except (UpstreamFailed, OSError):
            return self._refuse("upstream_failed", started, ticket, bytes_in=length)
        except Exception:  # noqa: BLE001 - never a traceback that could quote the request
            return self._refuse("upstream_failed", started, ticket, cause="error",
                                bytes_in=length)

        content_type = answer.content_type if _header_safe(answer.content_type) \
            else "application/octet-stream"

        def finish(bytes_out: int, cause: str | None) -> None:
            fields: dict[str, object] = {
                "code": "forwarded" if cause is None else cause, "status": answer.status,
                "bytes_in": length, "bytes_out": bytes_out, "duration_ms": self._ms(started),
                **_ids(ticket)}
            self._log("typesafe_forwarded", **fields)

        return Reply(status=answer.status, headers=[("Content-Type", content_type)],
                     chunks=_closing(answer.chunks, answer.close), finish=finish)


def _closing(chunks: Iterator[bytes], close: Callable[[], None]) -> Iterator[bytes]:
    try:
        yield from chunks
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


def _header_safe(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_FORWARDED_HEADER_CHARS
            and value.isascii() and value.isprintable())


def _ids(ticket: Ticket) -> dict[str, object]:
    return {"attempt_id": ticket.attempt_id, "tenant_id": ticket.tenant_id,
            "task_id": ticket.task_id, "scope": ticket.scope, "secret_id": ticket.secret_id}


__all__ = ["FORWARDED_HEADERS", "Forwarder", "PATH", "PORT", "Reply", "SERVICE_HOST", "STATUS",
           "event"]
