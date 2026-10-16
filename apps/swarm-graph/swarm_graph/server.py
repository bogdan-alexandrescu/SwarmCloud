"""The HTTP side: MCP streamable HTTP, stateless, one endpoint
(docs/design/graph-service.md §2.2).

    POST /mcp      the graph ticket as the bearer credential (Authorization)
                   Content-Type: application/json
                   body: one JSON-RPC 2.0 request (no batches)
    -> 200 application/json, one JSON-RPC response (202, no body, for a notification)
    GET  /healthz  -> 200 when the key ring is loaded and the switch is on

No `Mcp-Session-Id` and no server-sent-event stream: the ticket carries the
pin, so nothing needs state between calls or affinity to a replica.

THE HTTP STATUS SPEAKS ONLY FOR THE TICKET AND THE SERVICE (§2.2, §10). A
JSON-RPC error, and a tool that cannot answer, are a 200 carrying it. Every
other status has a JSON body `{"error": <code>}` and the same code in
`X-Swarm-Graph-Refused`, which the pod's shim turns into the §10 sentence:

    401 ticket_refused     no ticket, or one that does not verify; `reason` is
                           `expired` or `invalid` and nothing more
    404 index_missing      the pinned manifest or one of its blobs is gone
    409 manifest_mismatch  the stored manifest is not the ticket's digest
                           (logged as a security event, ids only)
    409 manifest_invalid   it is the ticket's digest, and still not a manifest
    413 too_large          a body over MAX_BODY_BYTES, never read
    503 disabled           the kill switch is off
    503 store_unavailable  GCS could not be read

THE ANSWER IS THE LOCAL SERVER'S. `initialize`, `ping` and `tools/list` are
`graphmcp.server.GraphServer.handle` itself, built per request around the
pinned snapshot and the EMPTY observation (§4.4). `tools/call` is the same
`tools.compute` and `tools.page` that `tools.call` runs, with the result cache
between them (§5.2): a hit skips `compute`, never `page`. So an answer here is
byte for byte the one `tools.call(snap, Observation.empty(index_sha), ...)`
gives, which tests/unit/graphsvc/test_graphsvc_server.py holds for every tool.

The same hardening as swarm-typesafe-fwd's server: every method reaches the one
handler (never a stdlib 501), an unparsable request is 400 with no echo, the
stdlib's request-line log is silenced, no `Server` version, one request per
connection. Nothing logged here carries a ticket, a header or a body; a
`graph_answer` line carries the tenant and repository ids, the tool, the
status, `cache: hit|miss` and milliseconds (§10 "cache cold").
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    PARSE_ERROR,
    GraphServer,
    _error,
    _result,
)
from agent_worker.graphmcp.snapshot import Snapshot, SnapshotError
from swarm_redaction import redact

from .cache import (
    ManifestCache,
    ResultCache,
    ShardCache,
    decode_result,
    encode_computed,
    encode_unresolved,
    result_key,
)
from .store import (
    IndexMissing,
    ManifestMismatch,
    ObjectStore,
    StoreUnavailable,
    TenantSource,
    open_snapshot,
)
from .ticket import GraphTicket, KeyRing, TicketRefused, bearer, verify_graph_ticket
from .warm import Warmed, Warmer

log = logging.getLogger("swarm_graph")

PORT = 8080
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
#: A JSON-RPC request to a graph tool is a few hundred bytes (`tests_for`
#: takes at most 50 paths); a body over this is refused unread.
MAX_BODY_BYTES = 64 * 1024
#: How long a connection may take to send its request head and body.
REQUEST_TIMEOUT_SECONDS = 30

TICKET_REFUSED = "ticket_refused"
INDEX_MISSING = "index_missing"
MANIFEST_MISMATCH = "manifest_mismatch"
MANIFEST_INVALID = "manifest_invalid"
TOO_LARGE = "too_large"
DISABLED = "disabled"
STORE_UNAVAILABLE = "store_unavailable"
BAD_REQUEST = "bad_request"
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"

#: Every refusal code, by its HTTP status (the shim's table, §10).
STATUSES = {
    TICKET_REFUSED: 401,
    INDEX_MISSING: 404,
    MANIFEST_MISMATCH: 409,
    MANIFEST_INVALID: 409,
    TOO_LARGE: 413,
    DISABLED: 503,
    STORE_UNAVAILABLE: 503,
    BAD_REQUEST: 400,
    NOT_FOUND: 404,
    METHOD_NOT_ALLOWED: 405,
}


def event(name: str, **fields: object) -> None:
    """One log line: JSON, redacted, and only the fields a caller passes,
    which are never a header, a body or a ticket."""
    log.info(redact(json.dumps({"event": name, **fields}, sort_keys=True)).text)


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)


def _json_reply(status: int, body: dict, *extra: tuple[str, str]) -> Reply:
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")
    return Reply(status, data, [("Content-Type", "application/json"),
                                ("Cache-Control", "no-store"), *extra])


def refusal(code: str, **detail: str) -> Reply:
    return _json_reply(STATUSES[code], {"error": code, **detail},
                       ("X-Swarm-Graph-Refused", code))


class _Refused(Exception):
    def __init__(self, code: str, **detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _EmptyFreshness:
    """What `GraphServer` reads an observation from: always the empty one."""

    def __init__(self, index_sha: str) -> None:
        self._obs = Observation.empty(index_sha)

    def observe(self) -> Observation:
        return self._obs


class GraphService:
    """One request in, one `Reply` out; no I/O but the store's."""

    def __init__(self, *, keys: KeyRing, store: ObjectStore, enabled: Callable[[], bool],
                 shards: ShardCache | None = None, results: ResultCache | None = None,
                 manifests: ManifestCache | None = None, warmer: Warmer | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.keys = keys
        self.store = store
        self.enabled = enabled
        self.shards = shards if shards is not None else ShardCache()
        self.results = results if results is not None else ResultCache()
        self.manifests = manifests if manifests is not None else ManifestCache()
        self.warmer = warmer if warmer is not None else Warmer(on_done=_warmed)
        self._clock = clock

    # -- the routes -------------------------------------------------------------

    def handle(self, method: str, path: str, headers: Iterable[tuple[str, str]],
               read: Callable[[int], bytes]) -> Reply:
        route = path.split("?", 1)[0]
        if route == HEALTH_PATH:
            if method not in ("GET", "HEAD"):
                return refusal(METHOD_NOT_ALLOWED)
            if not self.enabled():
                return refusal(DISABLED)
            return _json_reply(200, {"ok": True})
        if route != MCP_PATH:
            return refusal(NOT_FOUND)
        if method != "POST":
            return refusal(METHOD_NOT_ALLOWED)
        if not self.enabled():
            return refusal(DISABLED)
        named = {k.lower(): v for k, v in headers}
        try:
            length = int(named.get("content-length", ""))
        except ValueError:
            return refusal(BAD_REQUEST)
        if length < 0:
            return refusal(BAD_REQUEST)
        if length > MAX_BODY_BYTES:
            return refusal(TOO_LARGE)
        started = time.monotonic()
        try:
            ticket = verify_graph_ticket(self.keys.keys(), bearer(named.get("authorization")),
                                         now=self._clock())
        except TicketRefused as exc:
            event("graph_refused", code=TICKET_REFUSED, cause=exc.cause)
            return refusal(TICKET_REFUSED, reason=exc.reason)
        body = read(length)
        if len(body) != length:
            return refusal(BAD_REQUEST)
        try:
            return self._mcp(ticket, body, started)
        except _Refused as refused:
            self._log(ticket, None, refused.code, None, started)
            return refusal(refused.code, **refused.detail)

    def _mcp(self, ticket: GraphTicket, body: bytes, started: float) -> Reply:
        try:
            message = json.loads(body)
        except ValueError:
            return self._rpc(_error(None, PARSE_ERROR, "not JSON"))
        if isinstance(message, list):
            return self._rpc(_error(None, INVALID_REQUEST, "batches are not accepted"))
        snap = self._open(ticket)
        if isinstance(message, dict) and message.get("method") == "tools/call" \
                and "id" in message and message.get("jsonrpc") == "2.0":
            return self._tools_call(ticket, snap, message, started)
        response = GraphServer(snap, _EmptyFreshness(ticket.index_sha)).handle(message)  # type: ignore[arg-type]
        if response is None:
            return Reply(202, b"", [("Cache-Control", "no-store")])
        return self._rpc(response)

    def _rpc(self, response: dict) -> Reply:
        return Reply(200, tools.dumps(response).encode("ascii"),
                     [("Content-Type", "application/json"), ("Cache-Control", "no-store")])

    # -- the pinned snapshot ------------------------------------------------------

    def _open(self, ticket: GraphTicket) -> Snapshot:
        key = (ticket.tenant_id, ticket.repo_id, ticket.manifest_digest)
        snap = self.manifests.get(key)
        if snap is None:
            source = TenantSource(ticket, self.store, self.shards)
            try:
                snap = open_snapshot(self.store, ticket, source)
            except IndexMissing:
                raise _Refused(INDEX_MISSING) from None
            except StoreUnavailable:
                raise _Refused(STORE_UNAVAILABLE) from None
            except ManifestMismatch:
                event("graph_security", kind=MANIFEST_MISMATCH, tenant_id=ticket.tenant_id,
                      repo_id=ticket.repo_id)
                raise _Refused(MANIFEST_MISMATCH) from None
            except SnapshotError:
                event("graph_security", kind=MANIFEST_INVALID, tenant_id=ticket.tenant_id,
                      repo_id=ticket.repo_id)
                raise _Refused(MANIFEST_INVALID) from None
            raw_bytes = len(tools.dumps(snap.manifest))
            self.manifests.put(key, snap, raw_bytes)
        self.warmer.saw(ticket.tenant_id, ticket.repo_id, ticket.manifest_digest, snap)
        return snap

    # -- tools/call -----------------------------------------------------------------

    def _tools_call(self, ticket: GraphTicket, snap: Snapshot, message: dict,
                    started: float) -> Reply:
        ident = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._rpc(_error(ident, INVALID_PARAMS, "params must be an object"))
        name = params.get("name")
        if not isinstance(name, str):
            return self._rpc(_error(ident, INVALID_PARAMS, "tools/call needs a tool name"))
        arguments = params.get("arguments")
        obs = Observation.empty(ticket.index_sha)
        try:
            text, failed, cache = self.answer(ticket, snap, obs, name, arguments)
        except IndexMissing:
            raise _Refused(INDEX_MISSING) from None
        except StoreUnavailable:
            raise _Refused(STORE_UNAVAILABLE) from None
        self._log(ticket, name, "error" if failed else "ok", cache, started)
        return self._rpc(_result(ident, {"content": [{"type": "text", "text": text}],
                                         "isError": failed}))

    def answer(self, ticket: GraphTicket, snap: Snapshot, obs: Observation, name: str,
               arguments: Any) -> tuple[str, bool, str]:
        """(answer text, is_error, `hit`|`miss`|`none`): `tools.call`, with the
        result cache between compute and page. `IndexMissing` and
        `StoreUnavailable` are raised, never answered: they are statuses."""
        key = result_key(ticket.tenant_id, ticket.manifest_digest, name, arguments)
        cached = self.results.get(key) if key is not None else None
        cache = "hit" if cached is not None else ("miss" if key is not None else "none")
        try:
            if cached is not None:
                held = decode_result(cached)
            else:
                try:
                    computed = tools.compute(name, snap, arguments)
                except tools._Unresolved as exc:
                    held = (str(exc), list(exc.candidates))
                    if key is not None:
                        self.results.put_result(key, encode_unresolved(*held))
                else:
                    held = computed
                    if key is not None:
                        data = encode_computed(computed)
                        self.results.put_result(key, data)
                        held = decode_result(data)
            if isinstance(held, tuple):
                return tools.error_answer(obs, held[0], held[1]), True, cache
            return tools.page(obs, held.head, held.rows, tools._cursor(arguments or {}),
                              held.expand), False, cache
        except (IndexMissing, StoreUnavailable):
            raise
        except (tools.ToolInputError, SnapshotError) as exc:
            # Never cached: a refusal is cheap, and a blob that does not
            # match its name is a fault, not an answer (§5.2).
            return tools.error_answer(obs, str(exc)), True, cache

    def _log(self, ticket: GraphTicket, tool: str | None, status: str, cache: str | None,
             started: float) -> None:
        event("graph_answer", tenant_id=ticket.tenant_id, repo_id=ticket.repo_id,
              tool=tool, status=status, cache=cache,
              ms=int((time.monotonic() - started) * 1000))


def _warmed(tenant_id: str, repo_id: str, result: Warmed, ms: int) -> None:
    event("graph_warmed", tenant_id=tenant_id, repo_id=repo_id, shards=result.shards,
          stored_bytes=result.stored_bytes, cause=result.cause, ms=ms)


# --- the HTTP server ----------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "swarm-graph"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS
    service: GraphService

    def __getattr__(self, name: str) -> Any:
        # Every `do_<METHOD>` the standard library looks for is the one
        # handler, so no method is ever 501 and none skips the checks.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._write(refusal(BAD_REQUEST))
        self.close_connection = True

    def _read(self, length: int) -> bytes:
        try:
            return self.rfile.read(length)
        except OSError:
            return b""

    def _handle(self) -> None:
        self.close_connection = True
        try:
            reply = self.service.handle(self.command, self.path, list(self.headers.items()),
                                        self._read)
        except Exception as exc:  # noqa: BLE001 - never a traceback quoting a request
            event("graph_internal_error", cause=type(exc).__name__)
            reply = _json_reply(500, {"error": "internal"})
        self._write(reply)

    def _write(self, reply: Reply) -> None:
        try:
            self.send_response_only(reply.status)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if reply.body and self.command != "HEAD":
                self.wfile.write(reply.body)
            self.wfile.flush()
        except OSError:
            pass


class GraphHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback, which may quote request data.
        return


def make_server(service: GraphService, host: str, port: int) -> GraphHttpServer:
    handler = type("Handler", (_Handler,), {"service": service})
    return GraphHttpServer((host, port), handler)


__all__ = [
    "HEALTH_PATH",
    "MAX_BODY_BYTES",
    "MCP_PATH",
    "PORT",
    "STATUSES",
    "GraphHttpServer",
    "GraphService",
    "Reply",
    "event",
    "make_server",
    "refusal",
]
