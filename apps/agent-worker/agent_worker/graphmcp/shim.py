"""The pod's `swarm-graph`: a stdio shim in front of the shared graph service
(docs/design/graph-service.md §4.3-§4.4, §10; lane GS-SHIM, part of #1384).

    python -I -m agent_worker.graphmcp.shim --pin <pin.json> \\
        --ticket-file <private/graph-ticket> --workdir <checkout>

The worker registers this argv under the MCP name `swarm-graph` (§4.2), so the
agent sees the same server and the same tools it saw over a staged snapshot.
It holds no shard and decodes nothing: the `swarm-graph` Deployment computes
every answer. The shim does the one thing only the pod can -- mark rows
`stale` from the agent's own working tree.

LOCAL, NEVER FORWARDED: `initialize`, `ping` and `tools/list`, from this
package's own `server.py` and `tools.py`. The agent's tool list is therefore
the shim's version's, and the service down never empties it.

FORWARDED: `tools/call`, in four steps (§4.4):

  1. read the ticket file -- one stat, and a read only when it changed (the
     worker renews it by write-then-rename, §4.1 step 2);
  2. POST the call (its name and arguments only) to `$SWARM_GRAPH_URL` with
     the ticket as the bearer credential, within 10 s;
  3. take `freshness.observe()`, the one git call (§4.3);
  4. answer `tools.remark(service_text, obs)`: the service computed under the
     empty observation, and remark gives the page the local server would have
     given under this one -- byte for byte, `next_cursor` included.

TOLD PLAINLY, NEVER SILENTLY WRONG (§10). Every failure is an answer from
`tools.error_answer` under the pod's own observation -- freshness first,
`isError: true`, no `rows` -- in §10's words. The shim retries once, after a
second, when the service is unreachable or answers 5xx (but not 503
`disabled`, which retrying cannot change), and re-reads the ticket once and
retries on a 401, which covers a renewal racing the call. Nothing is cached
in the pod, so there is no stale answer to fall back to wrongly.

THE TICKET (Q1: the ticket file). It goes to one place: the Authorization
header of a request to the service URL. The transport uses no proxy (the
pod's egress proxy must never see it) and follows no redirect (a redirect
would carry the header elsewhere). It never reaches stderr or an answer; the
log says statuses and causes from a fixed vocabulary only. The URL is a
worker setting passed in the server entry's `env`, never an argument a caller
can set (invariant 10).

ADDITIONAL REPOSITORIES (docs/design/multi-repository-steps.md §4.9, lane
MR-GRAPH, part of #1434). One shim, not one per repository. At start it finds
each additional repository the worker pinned beside the step's own --
`<pin dir>/repos/<name>/pin.json`, its ticket `<ticket dir>/graph-ticket-<name>`,
its checkout `work/repos/<name>` -- and then lists every tool with an
optional `repo` naming one of them (`tools.tools_with_repo`). A call with
`repo` is sent with THAT repository's ticket, its answer marked by THAT
checkout's observation; `repo` itself is taken off before it is forwarded.
A `repo` that names nothing pinned is told so, with the names that are. A
step with no additional graph lists and forwards exactly what it did before.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from swarm_common.models import ADDITIONAL_REPOSITORY_NAME

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Freshness
from agent_worker.graphmcp.server import INVALID_PARAMS, GraphServer

#: The worker setting naming the service; the worker passes it in the server
#: entry's `env` (§4.2). Not an argument: a caller cannot point it elsewhere.
URL_ENV = "SWARM_GRAPH_URL"
DEFAULT_URL = "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"
#: §4.4: one answer waits at most this long for the service.
CALL_TIMEOUT_SECONDS = 10.0
#: §10: an unreachable service is asked once more, after this pause.
RETRY_SECONDS = 1.0
#: A ticket is a few hundred bytes (`<b64url(json)>.<b64url(mac)>`); more is
#: not a ticket and is never sent.
MAX_TICKET_BYTES = 4096
#: An answer is at most 4 KiB (`tools.ANSWER_CAP`) wrapped in JSON-RPC; a
#: response past this is not an answer.
MAX_RESPONSE_BYTES = 64 * 1024

#: §10's words, exactly.
UNREACHABLE = ("the graph service is unreachable ({cause}); no answer was computed. "
               "Retry later, or read the code directly.")
DISABLED = "the graph service is switched off by the platform; no answer was computed."
TICKET_REFUSED = "the graph service refused this attempt's ticket ({why}); no answer was computed."
INDEX_MISSING = ("the index of {sha12} is no longer stored (pruned or missing); "
                 "no answer was computed.")
MANIFEST_MISMATCH = "the stored index of {sha12} does not match its promoted digest; refused."
#: Not in §10's table: a status the service's contract (§2.2) does not name.
UNEXPECTED_STATUS = "the graph service answered HTTP {status}; no answer was computed."
#: An unreachable cause: a 200 that is not a JSON-RPC tools/call answer.
UNREADABLE = "its answer could not be read"
#: A `repo` that names no pinned additional repository (§4.9).
NO_REPOSITORY = ("no graph is pinned for that repository in this step; repo is one of: "
                 "{names}. Read its checkout under work/repos/ directly.")
NO_REPOSITORIES = "this step has no additional repository with a graph; leave repo out."
UNREADABLE_PIN = "the pin of repository {name} is not readable ({why}); no answer was computed."

#: `graphstage.REPOS_CONTEXT_DIR_NAME`, `graphstage.PIN_NAME` and
#: `graphstage.REPOS_DIR_NAME`, restated so the shim does not import the
#: stager; tests/unit/worker/test_graphstage_additional.py holds them equal.
REPOS_CONTEXT_DIR_NAME = "repos"
PIN_NAME = "pin.json"
REPOS_DIR_NAME = "repos"
#: `graph_ticket.ticket_file_name(name)`'s prefix, held equal the same way.
TICKET_FILE_PREFIX = "graph-ticket-"

_SHA = re.compile(r"[0-9a-f]{40}")
#: base64url and the one `.` between payload and MAC: nothing that could end
#: or split a header.
_TICKET = re.compile(r"[A-Za-z0-9_\-.]+")


class ServiceUnreachable(Exception):
    """The service could not be asked: no connection, a timeout, a redirect."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class PinError(ValueError):
    """pin.json is missing or not the worker's record."""


Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


# --- the service URL and the HTTP transport -------------------------------------

def service_url(environ: Any = None) -> str:
    """`$SWARM_GRAPH_URL`, or the in-cluster default; http(s) with a host only."""
    url = ((environ if environ is not None else os.environ).get(URL_ENV) or "").strip() \
        or DEFAULT_URL
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{URL_ENV} must be an http(s) URL with a host")
    return url


def request_headers(ticket: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ticket}", "Content-Type": "application/json",
            "Accept": "application/json"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the ticket to another URL: refuse every one."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _cause(exc: BaseException) -> str:
    """A cause from a fixed vocabulary: an exception's text is never echoed."""
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                        http.client.RemoteDisconnected)):
        return "connection reset"
    if isinstance(exc, socket.gaierror):
        return "the service name did not resolve"
    return "network error"


def http_transport(url: str, body: bytes, headers: dict[str, str],
                   timeout: float) -> tuple[int, bytes]:
    """(status, body) of one POST; every HTTP status is returned, only a request
    that got no status (or a redirect) raises `ServiceUnreachable`."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # No proxy, whatever the pod's environment says; built per call, so no
    # handler holds state between answers.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ServiceUnreachable("redirected, not followed") from None
        try:
            data = exc.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException):
            data = b""
        return exc.code, data
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise ServiceUnreachable(_cause(exc)) from None


# --- the ticket file -------------------------------------------------------------

class TicketFile:
    """The worker's `private/graph-ticket`, re-read only when it changes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.reads = 0
        self._seen: tuple[int, int, int] | None = None
        self._value: tuple[str | None, str] = (None, "missing")

    def read(self, *, force: bool = False) -> tuple[str | None, str]:
        """(the ticket, "") or (None, why it cannot be sent)."""
        try:
            stat = self.path.stat()
        except OSError:
            self._seen, self._value = None, (None, "missing")
            return self._value
        seen = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if force or seen != self._seen:
            self._seen, self._value = seen, self._load()
        return self._value

    def _load(self) -> tuple[str | None, str]:
        self.reads += 1
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_TICKET_BYTES + 1)
        except OSError:
            return None, "missing"
        text = raw.decode("ascii", "replace").strip()
        if not text or len(raw) > MAX_TICKET_BYTES or not _TICKET.fullmatch(text):
            return None, "invalid"
        return text, ""


def _expired(ticket: str, now: float) -> bool:
    """Whether the ticket's own `exp` has passed. The payload is not secret
    (only the MAC is), so the shim may read it to say why it was refused."""
    payload = ticket.split(".", 1)[0]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        return False
    exp = data.get("exp") if isinstance(data, dict) else None
    return isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp <= now


# --- additional repositories (§4.9) --------------------------------------------

class Repository:
    """One additional repository's pinned graph: its ticket file, its pin and
    its checkout. The pin is read, and the freshness established, at the
    first call that names it."""

    def __init__(self, name: str, *, pin: Path | str, ticket: TicketFile,
                 workdir: Path | str | None) -> None:
        self.name = name
        self.pin = Path(pin)
        self.ticket = ticket
        self.workdir = Path(workdir) if workdir else None
        self._loaded: tuple[str, Any] | None = None

    def load(self) -> tuple[str, Any]:
        """`(index_sha, Freshness)`. Raises PinError."""
        if self._loaded is None:
            record = load_pin(self.pin)
            self._loaded = (record["index_sha"],
                            Freshness.establish(record["index_sha"], workdir=self.workdir,
                                                record=record))
        return self._loaded


def discover(pin: Path | str, ticket_file: Path | str) -> dict[str, Repository]:
    """Every additional repository the worker pinned beside `pin`, by name.

    Only a real directory whose name the contract's ADDITIONAL_REPOSITORY_NAME
    accepts and that holds a `pin.json` counts; a link is never followed, so
    nothing outside the context directory is read as a pin.
    """
    context = Path(pin).parent
    root = context / REPOS_CONTEXT_DIR_NAME
    if root.is_symlink() or not root.is_dir():
        return {}
    work = context.parent
    found: dict[str, Repository] = {}
    for child in sorted(root.iterdir()):
        name = child.name
        if not ADDITIONAL_REPOSITORY_NAME.fullmatch(name) or child.is_symlink() \
                or not child.is_dir():
            continue
        pinned = child / PIN_NAME
        if pinned.is_symlink() or not pinned.is_file():
            continue
        checkout = work / REPOS_DIR_NAME / name
        found[name] = Repository(
            name, pin=pinned,
            ticket=TicketFile(Path(ticket_file).parent / f"{TICKET_FILE_PREFIX}{name}"),
            workdir=checkout if checkout.is_dir() else None)
    return found


# --- the server ------------------------------------------------------------------

def _json(raw: bytes) -> Any:
    if len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _code(data: Any) -> str | None:
    """The service's refusal code, from `{"error"|"code"|"reason": "<code>"}`."""
    if not isinstance(data, dict):
        return None
    for key in ("error", "code", "reason"):
        if isinstance(data.get(key), str):
            return data[key]
    return None


def _is_answer(data: Any) -> bool:
    """A JSON-RPC response carrying a tools/call result or an error."""
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        return False
    error = data.get("error")
    if isinstance(error, dict):
        return isinstance(error.get("code"), int) and isinstance(error.get("message"), str)
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list) \
            or not result["content"]:
        return False
    first = result["content"][0]
    return isinstance(first, dict) and isinstance(first.get("text"), str)


class ShimServer(GraphServer):
    """`GraphServer` with a remote `tools/call`; everything else is local."""

    def __init__(self, freshness: Freshness, *, ticket: TicketFile, index_sha: str,
                 url: str, transport: Transport = http_transport,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 repositories: Mapping[str, Repository] | None = None) -> None:
        # No snapshot: the service holds it. `GraphServer.handle` reads
        # `self.snapshot` only for `tools/call`, which this class answers.
        super().__init__(None, freshness)  # type: ignore[arg-type]
        self.ticket = ticket
        self.index_sha = index_sha
        self.url = url
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        #: The additional repositories' graphs (§4.9), by name; empty for a
        #: step that reads none, which then lists and forwards as before.
        self.repositories: dict[str, Repository] = dict(repositories or {})

    def handle(self, message: Any) -> dict | None:
        if not (isinstance(message, dict) and message.get("jsonrpc") == "2.0"
                and "id" in message and message.get("method") == "tools/call"):
            response = super().handle(message)
            if self.repositories and isinstance(message, dict) \
                    and message.get("method") == "tools/list" \
                    and isinstance(response, dict) and "result" in response:
                response["result"] = {"tools": tools.tools_with_repo(self.repositories)}
            return response
        ident = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(ident, INVALID_PARAMS, "params must be an object")
        name = params.get("name")
        if not isinstance(name, str):
            return _error(ident, INVALID_PARAMS, "tools/call needs a tool name")
        try:
            repo, arguments = tools.split_repo(params.get("arguments"))
        except tools.ToolInputError as exc:
            return _answer(ident, tools.error_answer(self.freshness.observe(), str(exc)), True)
        ticket, index_sha, freshness = self.ticket, self.index_sha, self.freshness
        if repo is not None:
            chosen = self.repositories.get(repo)
            if chosen is None:
                told = (NO_REPOSITORY.format(names=", ".join(sorted(self.repositories)))
                        if self.repositories else NO_REPOSITORIES)
                return _answer(ident, tools.error_answer(self.freshness.observe(), told), True)
            try:
                index_sha, freshness = chosen.load()
            except PinError as exc:
                told = UNREADABLE_PIN.format(name=chosen.name, why=exc)
                return _answer(ident, tools.error_answer(self.freshness.observe(), told), True)
            ticket = chosen.ticket
        forward = {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        if forward["params"]["arguments"] is None:
            del forward["params"]["arguments"]
        reply, failure = self._ask(forward, ticket=ticket, index_sha=index_sha)
        obs = freshness.observe()
        if reply is not None and isinstance(reply.get("error"), dict):
            error = reply["error"]
            return _error(ident, error["code"], error["message"][:1024])
        if reply is not None:
            result = reply["result"]
            try:
                text = tools.remark(result["content"][0]["text"], obs)
            except (ValueError, TypeError, KeyError, AttributeError):
                failure = UNREACHABLE.format(cause=UNREADABLE)
            else:
                failed = result.get("isError") is True or "error" in json.loads(text)
                return _answer(ident, text, failed)
        self._log(failure)
        return _answer(ident, tools.error_answer(obs, failure), True)

    # --- asking the service ---

    def _ask(self, forward: dict, *, ticket: TicketFile | None = None,
             index_sha: str | None = None) -> tuple[dict | None, str]:
        """(the service's JSON-RPC response, "") or (None, what the agent is told).

        `ticket` and `index_sha` are an additional repository's (§4.9); the
        step's own when None."""
        source = ticket if ticket is not None else self.ticket
        index_sha = index_sha if index_sha is not None else self.index_sha
        ticket_text, why = source.read()
        if ticket_text is None:
            return None, TICKET_REFUSED.format(why=why)
        payload = tools.dumps(forward).encode("ascii")
        status, data = self._send(ticket_text, payload)
        if status == 401:
            # A renewal may have raced this call: read the file again, whatever
            # its stat says, and ask once more.
            ticket_text, why = source.read(force=True)
            if ticket_text is None:
                return None, TICKET_REFUSED.format(why=why)
            status, data = self._send(ticket_text, payload)
            if status == 401:
                return None, TICKET_REFUSED.format(why=self._refused_why(data, ticket_text))
        sha12 = index_sha[:12]
        if status is None:
            return None, UNREACHABLE.format(cause=data)
        if status == 200:
            return data, ""
        if status == 503:
            return None, DISABLED
        if status == 404:
            return None, INDEX_MISSING.format(sha12=sha12)
        if status == 409:
            return None, MANIFEST_MISMATCH.format(sha12=sha12)
        return None, UNEXPECTED_STATUS.format(status=status)

    def _send(self, ticket: str, payload: bytes) -> tuple[int | None, Any]:
        """(status, decoded body), or (None, the cause) when the service could
        not be asked, after one retry a second later. A 503 `disabled` is
        returned at once: asking again cannot turn the switch back on."""
        outcome: tuple[int | None, Any] = (None, "network error")
        for attempt in range(2):
            if attempt:
                self._sleep(RETRY_SECONDS)
            try:
                status, raw = self._transport(self.url, payload, request_headers(ticket),
                                              CALL_TIMEOUT_SECONDS)
            except ServiceUnreachable as exc:
                outcome = (None, exc.cause)
                continue
            data = _json(raw)
            if status == 200 and not _is_answer(data):
                outcome = (None, UNREADABLE)
                continue
            if status >= 500 and not (status == 503 and _code(data) == "disabled"):
                outcome = (None, f"HTTP {status}")
                continue
            return status, data
        return outcome

    def _refused_why(self, data: Any, ticket: str) -> str:
        """`expired` or `invalid`: the service's word when it gives one, else
        the ticket's own `exp` against the clock."""
        reason = data.get("reason") if isinstance(data, dict) else None
        if reason in ("expired", "invalid"):
            return reason
        return "expired" if _expired(ticket, self._clock()) else "invalid"

    @staticmethod
    def _log(failure: str) -> None:
        # §10's words carry a status or a fixed cause, never the ticket.
        print(f"swarm-graph shim: {failure}", file=sys.stderr)


def _answer(ident: Any, text: str, failed: bool) -> dict:
    return {"jsonrpc": "2.0", "id": ident,
            "result": {"content": [{"type": "text", "text": text}], "isError": failed}}


def _error(ident: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


# --- the pin and main ---------------------------------------------------------------

def load_pin(path: Path | str) -> dict[str, Any]:
    """The worker's `pin.json` (§4.1 step 1): `freshness.json`'s record plus the
    digest. The shim needs `index_sha`; the rest is `Freshness.establish`'s."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PinError("the pin is not readable JSON") from None
    if not isinstance(record, dict):
        raise PinError("the pin is not a JSON object")
    if not isinstance(record.get("index_sha"), str) or not _SHA.fullmatch(record["index_sha"]):
        raise PinError("the pin names no 40-hex index_sha")
    base = record.get("base_sha")
    if base is not None and (not isinstance(base, str) or not _SHA.fullmatch(base)):
        raise PinError("the pin's base_sha is not 40 hex")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarm-graph", description=__doc__.split("\n")[0])
    parser.add_argument("--pin", required=True, help="the worker's pin.json")
    parser.add_argument("--ticket-file", required=True, help="the worker's graph ticket file")
    parser.add_argument("--workdir", default=None,
                        help="the step's checkout, for the paths dirtied in this step")
    args = parser.parse_args(argv)
    try:
        pin = load_pin(args.pin)
        url = service_url()
    except ValueError as exc:
        print(f"swarm-graph: {exc}", file=sys.stderr)
        return 2
    freshness = Freshness.establish(pin["index_sha"], workdir=args.workdir, record=pin)
    server = ShimServer(freshness, ticket=TicketFile(args.ticket_file),
                        index_sha=pin["index_sha"], url=url, transport=http_transport,
                        repositories=discover(args.pin, args.ticket_file))
    server.serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
