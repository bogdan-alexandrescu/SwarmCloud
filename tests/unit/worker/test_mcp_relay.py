"""The worker's loopback MCP relay (MCP-WORKER, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.7 and §4.8. Every case runs
the real relay against a loopback FAKE MCP server: the relay's resolver is
handed a public address (so its address check passes) and its connector opens
plain HTTP to the fake instead of TLS to that address, which is the one seam
a unit test needs. The address check itself is exercised with the resolver
answering private, loopback and metadata addresses.

What each case pins (the lane's red-first list):

  * the credential arrives as the registered header and nowhere else;
  * a wrong local token is 401; a non-loopback peer is 403;
  * a tool off the allowed list is refused, unforwarded;
  * a body carrying a registered secret or a credential-shaped string is
    refused, unforwarded, and so is a forwarded header carrying one;
  * a 3xx is not followed;
  * a private, loopback or metadata address is refused;
  * the size, rate and timeout caps hold;
  * an echoed credential is scrubbed from the answer, JSON and SSE alike;
  * after the fence flag nothing is forwarded;
  * after close the relay holds no reference to the credential.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import gc
import http.client
import io
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import fields, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_worker import mcp_relay as relay_mod
from agent_worker.logs import StructuredLogger

#: A public address for the resolver to answer, so the relay's check passes;
#: the connector never dials it.
PUBLIC = "8.8.8.8"
HOST = "kb.example.com"


def fake_credential() -> str:
    return "cred-" + secrets.token_hex(20)


def shaped_token() -> str:
    """A GitHub-token-shaped string, assembled so no literal is one."""
    return "gh" + "p_" + secrets.token_hex(18)


class FakeMcp:
    """A loopback MCP server that records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.content_type = "application/json"
        self.extra_headers: dict[str, str] = {}
        self.body: bytes | None = None
        self.echo_auth = False
        self.delay = 0.0
        self.sse: list[bytes] | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers.items()),
                                       "header_list": list(self.headers.items()),
                                       "body": body})
                if outer.delay:
                    time.sleep(outer.delay)
                auth = " ".join(v for k, v in self.headers.items()
                                if k.lower() in ("authorization", "x-kb-key"))
                if outer.sse is not None:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for event in outer.sse:
                        self.wfile.write(event.replace(b"{auth}", auth.encode()))
                        self.wfile.flush()
                    return
                payload = outer.body if outer.body is not None else json.dumps(
                    {"jsonrpc": "2.0", "id": 1,
                     "result": {"echo": auth if outer.echo_auth else "ok"}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                for name, value in outer.extra_headers.items():
                    self.send_header(name, value.replace("{auth}", auth))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _serve

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                """A relay that refused mid-answer closes its end: not a failure."""

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def connector(self, host: str, address: str, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    fake = FakeMcp()
    yield fake
    fake.stop()


@pytest.fixture
def logger():
    stream = io.StringIO()
    log = StructuredLogger(stream=stream)
    log.stream = stream  # type: ignore[attr-defined]
    return log


def make_server(credential: str | None, **kwargs: Any) -> relay_mod.Server:
    values: dict[str, Any] = dict(name="kb", url=f"https://{HOST}/mcp", host=HOST, path="/mcp",
                                  header="Authorization", scheme="Bearer",
                                  description="the knowledge base", credential=credential)
    values.update(kwargs)
    return relay_mod.Server(**values)


def make_relay(upstream: FakeMcp, logger: Any, *servers: relay_mod.Server,
               resolved: list[str] | None = None, **kwargs: Any) -> relay_mod.Relay:
    relay = relay_mod.Relay(list(servers), logger=logger, task_id="task_1",
                            attempt_id="att_1",
                            resolver=lambda host: list([PUBLIC] if resolved is None else resolved),
                            connector=upstream.connector, **kwargs)
    return relay.start()


def call(relay: relay_mod.Relay, body: Any = None, *, name: str = "kb",
         token: str | None = None, method: str = "POST",
         headers: dict[str, str] | None = None, raw: bytes | None = None,
         timeout: float = 10) -> tuple[int, dict[str, str], bytes]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(relay.url_for(name), data=data, method=method)
    request.add_header("Authorization", f"Bearer {token if token is not None else relay.token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def tool_call(tool: str, arguments: dict[str, Any] | None = None, *, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}


def refusal(headers: dict[str, str]) -> str | None:
    return {k.lower(): v for k, v in headers.items()}.get(relay_mod.REFUSED_HEADER.lower())


# --------------------------------------------------------------------------
# the credential goes as the registered header, and nowhere else
# --------------------------------------------------------------------------


def test_the_credential_arrives_as_the_registered_header_and_nowhere_else(upstream, logger):
    credential = fake_credential()
    relay = make_relay(upstream, logger, make_server(credential, header="X-Kb-Key", scheme=""))
    try:
        status, headers, body = call(relay, tool_call("search", {"q": "decisions"}),
                                     headers={"Cookie": "session=abc", "Mcp-Session-Id": "s-1",
                                              "X-Other": "dropped"})
    finally:
        relay.close()
    assert status == 200, body
    [seen] = upstream.requests
    # Exactly once, under the registered name, as the whole value (scheme "").
    carrying = [(k, v) for k, v in seen["header_list"] if credential in v]
    assert carrying == [("X-Kb-Key", credential)]
    assert credential not in seen["body"].decode()
    lowered = {k.lower(): v for k, v in seen["header_list"]}
    # The agent's local token, its cookie and any other header are not forwarded.
    assert "authorization" not in lowered and "cookie" not in lowered
    assert "x-other" not in lowered
    assert relay.token not in json.dumps(seen["header_list"])
    # MCP's own headers are; and the fixed identity of the caller.
    assert lowered["mcp-session-id"] == "s-1"
    assert lowered["user-agent"] == relay_mod.USER_AGENT
    assert lowered["x-swarm-task"] == "task_1" and lowered["x-swarm-attempt"] == "att_1"
    assert seen["path"] == "/mcp"
    assert credential not in body.decode()
    # The log line names the server and the tool, never the value or the args.
    log = logger.stream.getvalue()
    assert '"mcp_call"' in log and '"search"' in log
    assert credential not in log and "decisions" not in log


def test_the_default_header_is_a_bearer_authorization(upstream, logger):
    credential = fake_credential()
    relay = make_relay(upstream, logger, make_server(credential))
    try:
        call(relay, tool_call("search"))
    finally:
        relay.close()
    [seen] = upstream.requests
    assert [v for k, v in seen["header_list"] if k.lower() == "authorization"] == [
        f"Bearer {credential}"]


def test_a_server_with_no_credential_gets_no_auth_header(upstream, logger):
    relay = make_relay(upstream, logger, make_server(None))
    try:
        call(relay, tool_call("search"))
    finally:
        relay.close()
    [seen] = upstream.requests
    assert "authorization" not in {k.lower() for k, _ in seen["header_list"]}


# --------------------------------------------------------------------------
# who may call it
# --------------------------------------------------------------------------


def test_a_wrong_local_token_is_401_and_nothing_is_forwarded(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        assert call(relay, tool_call("search"), token=secrets.token_urlsafe(32))[0] == 401
        assert call(relay, tool_call("search"), token="")[0] == 401
        status, headers, _ = call(relay, tool_call("search"), token="x" + relay.token)
    finally:
        relay.close()
    assert status == 401 and refusal(headers) == relay_mod.UNAUTHORIZED
    assert upstream.requests == []


def test_a_peer_that_is_not_loopback_is_403(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()),
                       is_loopback=lambda host: False)
    try:
        status, headers, _ = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 403 and refusal(headers) == relay_mod.NOT_LOOPBACK
    assert upstream.requests == []


def test_an_unknown_path_or_method_is_404(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        assert call(relay, tool_call("search"), name="other")[0] == 404
        assert call(relay, tool_call("search"), name="kb/extra")[0] == 404
        assert call(relay, None, method="PUT", raw=b"{}")[0] == 404
    finally:
        relay.close()
    assert upstream.requests == []


# --------------------------------------------------------------------------
# what the agent may send
# --------------------------------------------------------------------------


def test_a_tool_off_the_allowed_list_is_refused_unforwarded(upstream, logger):
    relay = make_relay(upstream, logger, make_server(
        fake_credential(), allowed_tools=frozenset({"search"})))
    try:
        status, headers, body = call(relay, tool_call("delete_everything", rid=7))
        batch = call(relay, [tool_call("search", rid=1), tool_call("drop", rid=2)])
        nameless = call(relay, {"jsonrpc": "2.0", "id": 3, "method": "tools/call"})
        assert upstream.requests == []
        allowed = call(relay, tool_call("search"))
        listing = call(relay, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    finally:
        relay.close()
    assert status == 200 and refusal(headers) == relay_mod.TOOL_NOT_ALLOWED
    answer = json.loads(body)
    assert answer["id"] == 7 and answer["error"]["data"]["refused"] == "tool_not_allowed"
    assert [a["id"] for a in json.loads(batch[2])] == [1, 2]
    assert refusal(nameless[1]) == relay_mod.TOOL_NOT_ALLOWED
    assert allowed[0] == 200 and listing[0] == 200
    assert len(upstream.requests) == 2


def test_an_empty_allowed_list_allows_every_tool(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        assert call(relay, tool_call("anything_at_all"))[0] == 200
    finally:
        relay.close()
    assert len(upstream.requests) == 1


def test_a_body_that_is_not_json_rpc_is_400_unforwarded(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        assert call(relay, None, raw=b"not json")[0] == 400
        assert call(relay, {"id": 1, "method": "tools/list"})[0] == 400
        assert call(relay, [])[0] == 400
    finally:
        relay.close()
    assert upstream.requests == []


def test_a_body_carrying_a_registered_secret_is_refused_unforwarded(upstream, logger):
    credential = fake_credential()
    provider_key = "pk-" + secrets.token_hex(24)
    logger.register_secret(provider_key)
    other = fake_credential()
    relay = make_relay(upstream, logger, make_server(credential),
                       make_server(other, name="crm", url="https://crm.example.com/mcp",
                                   host="crm.example.com"))
    try:
        answers = [
            call(relay, tool_call("record", {"note": f"the key is {provider_key}"})),
            call(relay, tool_call("record", {"note": credential})),
            # Another server's credential is refused on this one too.
            call(relay, tool_call("record", {"note": other})),
            # And the relay's own local token.
            call(relay, tool_call("record", {"note": relay.token})),
        ]
    finally:
        relay.close()
    for status, headers, body in answers:
        assert status == 200 and refusal(headers) == relay_mod.CREDENTIAL_IN_REQUEST, body
        assert json.loads(body)["error"]["data"]["refused"] == "credential_in_request"
    assert upstream.requests == []
    log = logger.stream.getvalue()
    assert '"rule": "registered_secret"' in log
    assert provider_key not in log and credential not in log and other not in log


def test_a_credential_shaped_string_is_refused_unforwarded(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    shaped = shaped_token()
    escaped = json.dumps(tool_call("record", {"note": "x"})).replace(
        '"x"', '"' + "".join(f"\\u{ord(c):04x}" for c in shaped) + '"').encode()
    try:
        plain = call(relay, tool_call("record", {"note": f"found {shaped} in the repo"}))
        # JSON escapes do not hide it: decoded strings are checked as well.
        hidden = call(relay, None, raw=escaped)
        # Nor does a forwarded header.
        header = call(relay, tool_call("search"), headers={"Mcp-Session-Id": shaped})
        prose = call(relay, tool_call("record", {
            "note": "a basic configuration with a bearer authentication step"}))
    finally:
        relay.close()
    for status, headers, _ in (plain, hidden, header):
        assert refusal(headers) == relay_mod.CREDENTIAL_IN_REQUEST
    assert '"rule": "github_token"' in logger.stream.getvalue()
    assert shaped not in logger.stream.getvalue()
    # Ordinary prose is not a credential: the one call that went out.
    assert prose[0] == 200 and len(upstream.requests) == 1


# --------------------------------------------------------------------------
# where it may go
# --------------------------------------------------------------------------


def test_a_redirect_is_not_followed(upstream, logger):
    target = FakeMcp()
    upstream.status = 302
    upstream.extra_headers = {"Location": f"http://127.0.0.1:{target.port}/steal"}
    upstream.body = b""
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        status, headers, body = call(relay, tool_call("search"))
    finally:
        relay.close()
        target.stop()
    assert status == 502 and refusal(headers) == relay_mod.REDIRECT_REFUSED
    assert "location" not in {k.lower() for k in headers}
    assert len(upstream.requests) == 1 and target.requests == []


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.1.2.3"], ["192.168.0.4"], ["172.16.5.5"], ["169.254.169.254"],
    ["100.64.0.1"], ["::1"], ["fd00::1"], ["fe80::1"], ["::ffff:10.0.0.1"], ["224.0.0.1"],
    ["0.0.0.0"], [PUBLIC, "10.0.0.1"], [],
])
def test_a_private_loopback_or_metadata_address_is_refused(upstream, logger, addresses):
    relay = make_relay(upstream, logger, make_server(fake_credential()), resolved=addresses)
    try:
        status, headers, _ = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 502 and refusal(headers) == relay_mod.ADDRESS_REFUSED
    assert upstream.requests == []


def test_the_address_rule():
    assert relay_mod.address_is_public(PUBLIC)
    assert relay_mod.address_is_public("2606:4700::1111")
    for address in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "100.100.0.1", "::1",
                    "fc00::1", "not-an-address", "::ffff:127.0.0.1"):
        assert not relay_mod.address_is_public(address), address


def test_a_name_that_does_not_resolve_is_refused(upstream, logger):
    def fail(host: str) -> list[str]:
        raise OSError("no such host")

    relay = relay_mod.Relay([make_server(fake_credential())], logger=logger, task_id="t",
                            attempt_id="a", resolver=fail, connector=upstream.connector).start()
    try:
        status, headers, _ = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 502 and refusal(headers) == "unresolved"
    assert upstream.requests == []


# --------------------------------------------------------------------------
# the caps
# --------------------------------------------------------------------------


def test_a_request_over_the_size_cap_is_413_unforwarded(upstream, logger):
    limits = relay_mod.Limits(max_request_bytes=200)
    relay = make_relay(upstream, logger, make_server(fake_credential(), limits=limits))
    try:
        status, headers, _ = call(relay, tool_call("record", {"note": "y" * 400}))
        small = call(relay, tool_call("record", {"note": "y"}))
    finally:
        relay.close()
    assert status == 413 and refusal(headers) == relay_mod.TOO_LARGE
    assert small[0] == 200 and len(upstream.requests) == 1


def test_an_answer_over_the_size_cap_is_not_passed_on(upstream, logger):
    upstream.body = json.dumps({"jsonrpc": "2.0", "id": 1,
                                "result": {"blob": "z" * 5000}}).encode()
    limits = relay_mod.Limits(max_response_bytes=1000)
    relay = make_relay(upstream, logger, make_server(fake_credential(), limits=limits))
    try:
        status, headers, body = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 502 and refusal(headers) == relay_mod.RESPONSE_TOO_LARGE
    assert b"zzzz" not in body


def test_the_rate_cap_holds_per_sliding_minute(upstream, logger):
    now = [1000.0]
    limits = relay_mod.Limits(calls_per_minute=2)
    relay = make_relay(upstream, logger, make_server(fake_credential(), limits=limits),
                       clock=lambda: now[0])
    try:
        assert call(relay, tool_call("search"))[0] == 200
        assert call(relay, tool_call("search"))[0] == 200
        status, headers, _ = call(relay, tool_call("search"))
        assert refusal(headers) == relay_mod.RATE_LIMITED
        assert len(upstream.requests) == 2
        now[0] += 61
        assert call(relay, tool_call("search"))[0] == 200
    finally:
        relay.close()
    assert len(upstream.requests) == 3


def test_the_timeout_cap_holds(upstream, logger):
    upstream.delay = 3.0
    limits = relay_mod.Limits(call_timeout_seconds=1)
    relay = make_relay(upstream, logger, make_server(fake_credential(), limits=limits))
    started = time.monotonic()
    try:
        status, headers, _ = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 504 and refusal(headers) == relay_mod.UPSTREAM_TIMEOUT
    assert time.monotonic() - started < 2.5


def test_limits_are_held_under_their_ceilings():
    limits = relay_mod.Limits.from_answer({
        "max_request_bytes": 10**12, "max_response_bytes": -1, "calls_per_minute": True,
        "call_timeout_seconds": 10**6})
    assert limits.max_request_bytes == relay_mod.CEILINGS["max_request_bytes"]
    assert limits.max_response_bytes == relay_mod.DEFAULT_LIMITS["max_response_bytes"]
    assert limits.calls_per_minute == relay_mod.DEFAULT_LIMITS["calls_per_minute"]
    assert limits.call_timeout_seconds == relay_mod.CEILINGS["call_timeout_seconds"]
    assert relay_mod.Limits.from_answer(None) == relay_mod.Limits()


# --------------------------------------------------------------------------
# what comes back
# --------------------------------------------------------------------------


def test_an_echoed_credential_is_scrubbed_from_the_answer(upstream, logger):
    credential = fake_credential()
    upstream.echo_auth = True
    upstream.extra_headers = {"Mcp-Session-Id": "{auth}", "Set-Cookie": "k={auth}"}
    relay = make_relay(upstream, logger, make_server(credential))
    try:
        status, headers, body = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 200
    assert credential not in body.decode() and "REDACTED" in body.decode()
    assert credential not in json.dumps(headers)
    # Only Content-Type and Mcp-Session-Id come back.
    assert "set-cookie" not in {k.lower() for k in headers}


def test_an_echoed_credential_is_scrubbed_from_a_stream(upstream, logger):
    credential = fake_credential()
    upstream.sse = [b"event: message\ndata: {\"seen\": \"{auth}\"}\n\n",
                    b"data: {\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": {}}\n\n"]
    relay = make_relay(upstream, logger, make_server(credential))
    try:
        status, headers, body = call(relay, tool_call("search"))
    finally:
        relay.close()
    assert status == 200 and headers.get("Content-Type") == "text/event-stream"
    text = body.decode()
    assert credential not in text and text.count("data:") == 2 and "REDACTED" in text


def test_a_get_stream_is_relayed(upstream, logger):
    upstream.sse = [b"data: {\"jsonrpc\": \"2.0\", \"method\": \"ping\"}\n\n"]
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        status, _, body = call(relay, None, method="GET", headers={"Mcp-Session-Id": "s-2"})
        deleted = call(relay, None, method="DELETE", headers={"Mcp-Session-Id": "s-2"})
    finally:
        relay.close()
    assert status == 200 and b"ping" in body and deleted[0] == 200
    assert [r["method"] for r in upstream.requests] == ["GET", "DELETE"]


# --------------------------------------------------------------------------
# the fence and the close
# --------------------------------------------------------------------------


def test_after_the_fence_flag_nothing_is_forwarded(upstream, logger):
    relay = make_relay(upstream, logger, make_server(fake_credential()))
    try:
        assert call(relay, tool_call("search"))[0] == 200
        relay.mark_fenced()
        status, headers, _ = call(relay, tool_call("search"))
        get = call(relay, None, method="GET")
    finally:
        relay.close()
    assert status == 409 and refusal(headers) == relay_mod.FENCED and get[0] == 409
    assert len(upstream.requests) == 1


def _holds(root: Any, value: str) -> bool:
    """Whether `value` is reachable from `root` through attributes and containers.

    The worker's logger is not walked: its registered set is where every
    credential is meant to stay until the process exits, so redaction holds
    for every line written after the relay is gone."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        item = stack.pop()
        if id(item) in seen or isinstance(item, StructuredLogger):
            continue
        seen.add(id(item))
        if isinstance(item, str):
            if value in item:
                return True
            continue
        if isinstance(item, (bytes, bytearray)):
            if value.encode() in item:
                return True
            continue
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
        elif is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, f.name) for f in fields(item))
        elif hasattr(item, "__dict__") and type(item).__module__.startswith("agent_worker"):
            stack.extend(vars(item).values())
    return False


def test_after_close_the_relay_holds_no_reference_to_the_credential(upstream, logger):
    credential = fake_credential()
    relay = make_relay(upstream, logger, make_server(credential))
    assert call(relay, tool_call("search"))[0] == 200
    port = relay.port
    assert _holds(relay, credential)  # the control: the walk does find it
    relay.close()
    gc.collect()
    assert not _holds(relay, credential)
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("POST", "/mcp/kb")
    # A closed relay is also a fenced one.
    assert relay.fenced
    relay.close()  # twice is harmless


def test_the_credential_is_in_no_repr():
    credential = fake_credential()
    assert credential not in repr(make_server(credential))
