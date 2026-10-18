"""The TypeSafe forwarder, the worker's half (docs/design/workspace-secrets.md
§3.5, SEC-MIGRATE-TYPESAFE; lane TS-MIGRATE-WORKER, part of #1347).

Behind `SWARM_TYPESAFE_INJECT=on`, on GKE only: the worker mints the
attempt's TypeSafe ticket with the attempt key (a fake of the route VERIFIES
every proof with swarm-api's own code), renews it as the egress and graph
tickets are renewed, and serves a loopback shim that forwards jev's and the
judge's requests to the forwarder with the CURRENT ticket. A fake forwarder
on loopback records every request byte the shim sends.

Switched off, the jev key file, `JEV_API_URL` and the judge's key are today's.

Every ticket, token and key is built at runtime. Offline: no credentials, no
emulator, no network beyond 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import jevkey, lifecycle, typesafe_shim
from agent_worker.errors import ExitCode
from agent_worker.judge import attempt as judge_attempt
from agent_worker.judge import client as judge_client
from agent_worker.judge import settings as judge_settings
from agent_worker.runners import claude_code, jev_launch
from swarm_api import childkey as api_childkey
from swarm_api import typesafe_ticket as api_typesafe_ticket
from swarm_api.live import LiveSessionOpen

from worker_seeds import TENANT, seed_attempt

ENDPOINT = "http://127.0.0.1:9222"
BODY = json.dumps({"state": {"page": "x"}, "model": "jev-latest", "questions": {}}).encode()


def _fake_ticket() -> str:
    """Ticket-shaped, built at runtime: never a literal credential."""
    return f"{secrets.token_urlsafe(24)}.{secrets.token_urlsafe(32)}"


def _fake_key() -> str:
    return "ts" + "-" + secrets.token_hex(24)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeTicketRoute:
    """`POST /v1/attempts/{id}/typesafe-ticket`, proofs checked with swarm-api's code."""

    def __init__(self) -> None:
        self.public_key: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.fail: children_mod.ChildApiError | None = None
        self.expires_in = 300
        self.issued: list[str] = []

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        message = api_typesafe_ticket.typesafe_ticket_message(method, path, body or b"", stamp)
        if not (self.public_key and proof and api_childkey.verify_proof(self.public_key, proof,
                                                                         message)):
            raise children_mod.ChildApiError(403, "forbidden", "unproven", retryable=False)
        if self.fail is not None:
            raise self.fail
        ticket = _fake_ticket()
        self.issued.append(ticket)
        return 201, {"ticket": ticket, "expires_in": self.expires_in, "scope": "tenant"}


class FakeForwarder:
    """A loopback stand-in for swarm-typesafe-fwd that records what reached it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        #: Answers, in order; the last one repeats.
        self.answers: list[tuple[int, dict[str, str], bytes]] = [
            (200, {"Content-Type": "application/json"}, b'{"answers": {}}')]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({
                    "path": self.path,
                    "headers": [(k.lower(), v) for k, v in self.headers.items()],
                    "body": self.rfile.read(length),
                })
                status, headers, body = outer.answers[min(len(outer.requests) - 1,
                                                          len(outer.answers) - 1)]
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def header(self, index: int, name: str) -> list[str]:
        return [v for k, v in self.requests[index]["headers"] if k == name.lower()]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Wall:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []
        self.secrets: list[str] = []

    def info(self, msg: str, **kw: Any) -> None:
        self.lines.append(("info", msg, kw))

    def warning(self, msg: str, **kw: Any) -> None:
        self.lines.append(("warning", msg, kw))

    def register_secret(self, value: str) -> None:
        self.secrets.append(value)

    def text(self) -> str:
        return json.dumps(self.lines, default=str)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
                           lease_id="lease_1", generation=3)


ON = {typesafe_shim.SWITCH_ENV: typesafe_shim.SWITCH_ON}


@pytest.fixture
def route() -> FakeTicketRoute:
    return FakeTicketRoute()


@pytest.fixture
def fwd():
    forwarder = FakeForwarder()
    yield forwarder
    forwarder.close()


def _inject(route: FakeTicketRoute, fwd: FakeForwarder | None = None, *,
            wall: Wall | None = None, log: Log | None = None, environ=ON,
            backend: str | None = typesafe_shim.GKE_BACKEND) -> typesafe_shim.TypesafeInject:
    key = children_mod.AttemptKey()
    route.public_key = key.public
    children = SimpleNamespace(_key=key, _api=route, keyed=True)
    return typesafe_shim.TypesafeInject(
        _cfg(), children=children, logger=log or Log(), environ=environ, backend=backend,
        wall=wall or Wall(), connect=(fwd.connect if fwd is not None
                                      else typesafe_shim.forwarder_connection))


@pytest.fixture
def private(tmp_path: Path) -> Path:
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    return path


def _post(shim: typesafe_shim.LoopbackShim, *, token: str | None = None, body: bytes = BODY,
          path: str = typesafe_shim.FORWARDER_PATH, extra: dict[str, str] | None = None,
          chunked: bool = False, auth_twice: bool = False) -> tuple[int, dict[str, str], bytes]:
    host, port = shim.address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.putrequest("POST", path)
        bearer = f"Bearer {shim.token if token is None else token}"
        conn.putheader("Authorization", bearer)
        if auth_twice:
            conn.putheader("Authorization", bearer)
        conn.putheader("Content-Type", "application/json")
        for name, value in (extra or {}).items():
            conn.putheader(name, value)
        if chunked:
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
        else:
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# restatements held to their sources
# --------------------------------------------------------------------------


def test_the_purpose_and_the_limits_are_swarm_apis():
    assert typesafe_shim.TYPESAFE_TICKET_PURPOSE == api_typesafe_ticket.TICKET_PURPOSE
    assert typesafe_shim.MAX_TICKET_SECONDS == api_typesafe_ticket.MAX_TICKET_SECONDS


def test_the_forwarder_address_is_the_one_the_design_specifies():
    assert typesafe_shim.FORWARDER_URL == (
        "http://swarm-typesafe-fwd.swarm-system.svc.cluster.local:8080/v1/systemone")
    assert typesafe_shim.FORWARDER_HOST == "swarm-typesafe-fwd.swarm-system.svc.cluster.local"


def test_the_shim_files_readers_agree_with_its_writer():
    assert jev_launch.SHIM_URL_FIELD == typesafe_shim.SHIM_URL_FIELD
    assert jev_launch.TOKEN_FIELD == typesafe_shim.TOKEN_FIELD
    assert judge_attempt.TYPESAFE_SHIM_FILE == typesafe_shim.SHIM_FILE_NAME
    assert jev_launch.SHIM_URL.pattern == judge_settings.SHIM_URL_PATTERN.pattern


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("environ, backend, on", [
    (ON, "GKE_AUTOPILOT", True),
    ({}, "GKE_AUTOPILOT", False),
    ({typesafe_shim.SWITCH_ENV: "true"}, "GKE_AUTOPILOT", False),
    ({typesafe_shim.SWITCH_ENV: "off"}, "GKE_AUTOPILOT", False),
    # A Cloud Run worker cannot reach a cluster Service: today's key file.
    (ON, "CLOUD_RUN_JOB", False),
    (ON, None, False),
])
def test_the_switch_is_on_only_when_set_and_on_gke(environ, backend, on):
    assert typesafe_shim.switched_on(environ, backend) is on


def test_switched_off_nothing_is_minted_or_served(route, private):
    inject = _inject(route, environ={})
    assert inject.on is False
    assert inject.start(private) is None
    assert route.calls == [] and list(private.iterdir()) == []
    assert inject.due() is False
    inject.keep()
    inject.close()
    assert route.calls == []


# --------------------------------------------------------------------------
# the ticket
# --------------------------------------------------------------------------


def test_mint_signs_the_body_swarm_api_reads_under_the_routes_purpose(route):
    key = children_mod.AttemptKey()
    route.public_key = key.public
    ticket, seconds = typesafe_shim.mint(_cfg(), key, route, now=1_000.0)
    assert (ticket, seconds) == (route.issued[0], 300)
    (call,) = route.calls
    assert call["path"] == "/v1/attempts/att_1/typesafe-ticket"
    body = json.loads(call["body"])
    assert set(body) == set(LiveSessionOpen.model_fields)
    assert body["generation"] == 3
    LiveSessionOpen.model_validate(body)


def test_a_409_at_the_first_mint_is_fenced_and_serves_nothing(route, private):
    route.fail = children_mod.ChildApiError(409, "conflict", "fenced", retryable=False)
    inject = _inject(route)
    with pytest.raises(typesafe_shim.TypesafeTicketFenced):
        inject.start(private)
    assert inject.shim is None and list(private.iterdir()) == []


@pytest.mark.parametrize("error", [
    children_mod.ChildApiError(403, "forbidden", "backend off", retryable=False),
    children_mod.ChildApiError(403, "typesafe_no_key", "no key", retryable=False),
    children_mod.ChildApiError(503, "typesafe_unavailable", "no fwd key", retryable=True),
])
def test_a_refused_first_mint_is_no_shim_and_no_key_file(route, private, error):
    route.fail = error
    inject = _inject(route)
    assert inject.start(private) is None
    assert inject.shim is None and inject.unavailable
    assert list(private.iterdir()) == []
    assert inject.due() is False  # nothing more is asked this attempt


def test_without_an_attempt_key_there_is_no_ticket_and_no_shim(route, private):
    inject = typesafe_shim.TypesafeInject(
        _cfg(), children=SimpleNamespace(_key=None, _api=None, keyed=False), logger=Log(),
        environ=ON, backend=typesafe_shim.GKE_BACKEND)
    assert inject.start(private) is None
    assert inject.unavailable == typesafe_shim.NO_ATTEMPT_KEY
    assert route.calls == []


# --------------------------------------------------------------------------
# the shim forwards one request with the current ticket
# --------------------------------------------------------------------------


def test_the_shim_forwards_one_request_with_the_current_ticket(route, fwd, private):
    inject = _inject(route, fwd)
    path = inject.start(private)
    try:
        assert path == private / typesafe_shim.SHIM_FILE_NAME
        status, headers, answer = _post(inject.shim, extra={
            "Cookie": "a=b", "X-Forwarded-For": "10.0.0.9", "Accept": "application/json",
            "User-Agent": "jev-browser/0.2.0"})
        assert (status, answer) == (200, b'{"answers": {}}')
        assert headers["content-type"] == "application/json"
        (sent,) = fwd.requests
        assert sent["path"] == "/v1/systemone" and sent["body"] == BODY
        assert fwd.header(0, "Host") == [typesafe_shim.FORWARDER_HOST]
        # The ticket is the ONE Authorization bearer; the caller's token is gone.
        assert fwd.header(0, "Authorization") == [f"Bearer {route.issued[0]}"]
        assert fwd.header(0, "Content-Length") == [str(len(BODY))]
        assert fwd.header(0, "Transfer-Encoding") == []
        assert fwd.header(0, "Connection") == ["close"]
        assert fwd.header(0, "Accept") == ["application/json"]
        assert fwd.header(0, "User-Agent") == ["jev-browser/0.2.0"]
        names = {k for k, _ in sent["headers"]}
        assert names == {"host", "authorization", "content-type", "accept", "user-agent",
                         "content-length", "connection"}
    finally:
        inject.close()


def test_the_shim_listens_on_loopback_only(route, fwd, private):
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        assert inject.shim.address[0] == "127.0.0.1"
        url, token = typesafe_shim.read_shim_file(inject.shim_file)
        assert url == f"http://127.0.0.1:{inject.shim.address[1]}/v1/systemone"
        assert judge_settings.SHIM_URL_PATTERN.fullmatch(url)
        assert token == inject.shim.token
    finally:
        inject.close()


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("10.0.0.5", False), ("0.0.0.0", False), ("::ffff:10.0.0.5", False),
    ("swarm-typesafe-fwd", False), ("", False),
])
def test_only_a_loopback_peer_is_loopback(host, loopback):
    assert typesafe_shim.is_loopback(host) is loopback


def test_a_non_loopback_caller_is_refused_before_anything_is_forwarded(
        route, fwd, private, monkeypatch):
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        # Every peer is a remote one, as a pod-network caller would be.
        monkeypatch.setattr(typesafe_shim, "is_loopback", lambda _host: False)
        status, headers, _ = _post(inject.shim)
        assert status == 403
        assert headers[typesafe_shim.SHIM_REFUSED_HEADER.lower()] == "not_loopback"
        assert fwd.requests == []
    finally:
        inject.close()


@pytest.mark.parametrize("how, status, code", [
    ({"token": "not-the-token"}, 401, "unauthorized"),
    ({"token": ""}, 401, "unauthorized"),
    ({"auth_twice": True}, 401, "unauthorized"),
    ({"chunked": True}, 400, "bad_request"),
    ({"path": "/v1/systemone?x=1"}, 404, "not_found"),
    ({"path": "/v1/other"}, 404, "not_found"),
])
def test_a_request_the_shim_does_not_serve_is_never_forwarded(route, fwd, private, how, status,
                                                              code):
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        got, headers, _ = _post(inject.shim, **how)
        assert got == status
        assert headers[typesafe_shim.SHIM_REFUSED_HEADER.lower()] == code
        assert fwd.requests == []
    finally:
        inject.close()


# --------------------------------------------------------------------------
# renewal
# --------------------------------------------------------------------------


def test_a_renewed_ticket_is_used_on_the_next_request(route, fwd, private):
    wall = Wall()
    inject = _inject(route, fwd, wall=wall)
    inject.start(private)
    try:
        _post(inject.shim)
        assert inject.due() is False
        wall.now += 200  # 300 s ticket: due with 100 s left
        assert inject.due() is True
        inject.keep()
        assert len(route.issued) == 2
        _post(inject.shim)
        assert fwd.header(0, "Authorization") == [f"Bearer {route.issued[0]}"]
        assert fwd.header(1, "Authorization") == [f"Bearer {route.issued[1]}"]
    finally:
        inject.close()


def test_a_ticket_invalid_is_renewed_and_sent_once_more(route, fwd, private):
    fwd.answers = [
        (401, {typesafe_shim.REFUSED_HEADER: "ticket_invalid"}, b""),
        (200, {"Content-Type": "application/json"}, b'{"ok": 1}'),
    ]
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        status, _, answer = _post(inject.shim)
        assert (status, answer) == (200, b'{"ok": 1}')
        assert len(fwd.requests) == 2 and len(route.issued) == 2
        assert fwd.header(1, "Authorization") == [f"Bearer {route.issued[1]}"]
    finally:
        inject.close()


def test_an_expired_ticket_is_never_sent(route, fwd, private):
    wall = Wall()
    inject = _inject(route, fwd, wall=wall)
    inject.start(private)
    try:
        route.fail = children_mod.ChildApiError(None, "api_unavailable", "down", retryable=True)
        wall.now += 300 - typesafe_shim.EXPIRY_MARGIN_SECONDS
        status, headers, _ = _post(inject.shim)
        assert status == 503
        assert headers[typesafe_shim.SHIM_REFUSED_HEADER.lower()] == "no_ticket"
        assert fwd.requests == []
    finally:
        inject.close()


def test_a_transient_failure_backs_off_and_keeps_the_ticket_in_hand(route, fwd, private):
    wall = Wall()
    inject = _inject(route, fwd, wall=wall)
    inject.start(private)
    try:
        wall.now += 200
        route.fail = children_mod.ChildApiError(503, "unavailable", "", retryable=True)
        inject.keep()
        assert inject.ticket.retry_at == wall.now + typesafe_shim.RETRY_SECONDS
        assert inject.due() is False
        _post(inject.shim)
        assert fwd.header(0, "Authorization") == [f"Bearer {route.issued[0]}"]
    finally:
        inject.close()


# --------------------------------------------------------------------------
# the fence (invariant 5)
# --------------------------------------------------------------------------


def test_a_409_at_a_renewal_is_fenced(route, fwd, private):
    wall = Wall()
    inject = _inject(route, fwd, wall=wall)
    inject.start(private)
    try:
        wall.now += 200
        route.fail = children_mod.ChildApiError(409, "conflict", "fenced", retryable=False)
        with pytest.raises(typesafe_shim.TypesafeTicketFenced):
            inject.keep()
    finally:
        inject.close()


def test_the_forwarders_stale_generation_fences_the_attempt(route, fwd, private):
    fwd.answers = [(409, {typesafe_shim.REFUSED_HEADER: "stale_generation"}, b"")]
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        status, _, _ = _post(inject.shim)
        assert status == 409
        assert inject.fenced and inject.due() is True
        with pytest.raises(typesafe_shim.TypesafeTicketFenced):
            inject.keep()
        # And nothing more is sent: a stale worker gets nothing.
        status, headers, _ = _post(inject.shim)
        assert status == 409
        assert headers[typesafe_shim.SHIM_REFUSED_HEADER.lower()] == "stale_generation"
        assert len(fwd.requests) == 1 and len(route.issued) == 1
    finally:
        inject.close()


def test_a_409_at_the_shims_own_renewal_fences_and_sends_nothing(route, fwd, private):
    fwd.answers = [(401, {typesafe_shim.REFUSED_HEADER: "ticket_invalid"}, b"")]
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        route.fail = children_mod.ChildApiError(409, "conflict", "fenced", retryable=False)
        status, _, _ = _post(inject.shim)
        assert status == 409
        assert len(fwd.requests) == 1
        with pytest.raises(typesafe_shim.TypesafeTicketFenced):
            inject.keep()
    finally:
        inject.close()


# --------------------------------------------------------------------------
# custody: nothing logged, nothing under work/, gone at the end
# --------------------------------------------------------------------------


def test_no_ticket_or_token_is_logged_and_each_is_registered(route, fwd, private):
    log, wall = Log(), Wall()
    inject = _inject(route, fwd, wall=wall, log=log)
    inject.start(private)
    try:
        _post(inject.shim)
        wall.now += 200
        inject.keep()
        text = log.text()
        for value in [*route.issued, inject.shim.token]:
            assert value not in text
            assert value in log.secrets
        assert BODY.decode() not in text
    finally:
        inject.close()


def test_the_shim_file_is_0600_in_private_and_carries_no_ticket(route, fwd, private, tmp_path):
    inject = _inject(route, fwd)
    path = inject.start(private)
    address = inject.shim.address
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.parent == private and (tmp_path / "work") not in path.parents
        assert route.issued[0] not in path.read_text()
    finally:
        inject.close()
    # Closed: the file is gone, the port is shut and the ticket is dropped.
    assert not path.exists()
    with pytest.raises(OSError):
        http.client.HTTPConnection(*address, timeout=2).connect()
    assert inject.ticket.current() is None


# --------------------------------------------------------------------------
# jevkey.prepare: no key with the switch on, today's file with it off
# --------------------------------------------------------------------------


class _Snap:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Db:
    def __init__(self, docs):
        self.docs = docs

    def collection(self, name):
        db = self

        class _C:
            def document(self, doc_id):
                return SimpleNamespace(get=lambda **_kw: _Snap(db.docs.get(f"{name}/{doc_id}")))

        return _C()


class _Secrets:
    def __init__(self, values):
        self.values, self.reads = values, []

    def access(self, name):
        self.reads.append(name)
        return self.values[name]


def _tenant_key_world():
    key = _fake_key()
    db = _Db({"provider_keys/swarm-tenant-eng-typesafe": {"set": True}})
    return db, _Secrets({"swarm-tenant-eng-typesafe": key}), key


def _jev_prepare(private, db, client, **extra):
    from agent_worker.logs import StructuredLogger

    stream = io.StringIO()
    env = jevkey.prepare(endpoint=ENDPOINT, db=db, client=client, tenant_id="eng",
                         submitted_by=None, private_dir=private,
                         logger=StructuredLogger(stream=stream, component="test"), **extra)
    return env, stream.getvalue()


def test_switched_on_jevkey_reads_and_writes_no_key(route, fwd, private):
    db, client, key = _tenant_key_world()
    inject = _inject(route, fwd)
    shim_file = inject.start(private)
    try:
        env, logs = _jev_prepare(private, db, client, inject=True, shim_file=shim_file)
        assert env == {jevkey.KEY_FILE_ENV: str(shim_file)}
        assert client.reads == []  # the GSA is never asked
        assert not (private / jevkey.KEY_FILE_NAME).exists()
        assert key not in logs
    finally:
        inject.close()


def test_switched_on_without_a_shim_is_no_jev_and_no_key(private):
    db, client, _ = _tenant_key_world()
    env, _ = _jev_prepare(private, db, client, inject=True, shim_file=None)
    assert env == {} and client.reads == []
    assert list(private.iterdir()) == []


def test_switched_on_an_internal_backend_gets_nothing(private):
    db, client, _ = _tenant_key_world()
    shim_file = private / typesafe_shim.SHIM_FILE_NAME
    shim_file.write_text("{}")
    env, _ = _jev_prepare(private, db, client, inject=True, shim_file=shim_file,
                          backend="internal-jev")
    assert env == {}


def test_switched_off_jevkey_writes_todays_key_file(private):
    db, client, key = _tenant_key_world()
    env, _ = _jev_prepare(private, db, client)
    path = private / jevkey.KEY_FILE_NAME
    assert env == {jevkey.KEY_FILE_ENV: str(path)}
    assert path.read_text() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # And `inject=False` spelled out is the same call.
    path.unlink()
    env2, _ = _jev_prepare(private, db, client, inject=False, shim_file=None)
    assert env2 == env and path.read_text() == key


# --------------------------------------------------------------------------
# jev_launch: the shim's URL and token in shim mode, today's key otherwise
# --------------------------------------------------------------------------


def _fake_server(tmp_path: Path, *, marker: bool) -> Path:
    server = tmp_path / ("marked" if marker else "plain") / "jev-browser-mcp"
    server.parent.mkdir()
    server.write_text(
        f"#!{sys.executable}\n"
        + (f"# {jev_launch.SHIM_PASSTHROUGH_MARKER}\n" if marker else "")
        + "import hashlib, json, os\n"
        "key = os.environ.get('TYPESAFE_API_KEY', '')\n"
        "print(json.dumps({'key_sha': hashlib.sha256(key.encode()).hexdigest(),"
        " 'url': os.environ.get('JEV_API_URL')}))\n"
    )
    server.chmod(0o755)
    return server


def _launch(path: Path, server: Path, env: dict[str, str] | None = None):
    base = {"PATH": os.environ.get("PATH", ""), "SWARM_CDP_ENDPOINT": ENDPOINT}
    return subprocess.run([sys.executable, "-I", str(claude_code.JEV_LAUNCH), str(path),
                           str(server)], env={**base, **(env or {})}, capture_output=True,
                          text=True, timeout=30)


def test_the_launcher_points_jev_at_the_shim_with_its_token(route, fwd, private, tmp_path):
    inject = _inject(route, fwd)
    shim_file = inject.start(private)
    try:
        done = _launch(shim_file, _fake_server(tmp_path, marker=True))
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout)
        assert out["url"] == inject.shim.url
        assert out["key_sha"] == hashlib.sha256(inject.shim.token.encode()).hexdigest()
        assert route.issued[0] not in done.stdout + done.stderr
    finally:
        inject.close()


def test_the_launcher_refuses_a_wrapper_that_drops_the_shims_url(route, fwd, private, tmp_path):
    inject = _inject(route, fwd)
    shim_file = inject.start(private)
    try:
        done = _launch(shim_file, _fake_server(tmp_path, marker=False))
        assert done.returncode == 1 and done.stdout == ""
        assert "drops the worker shim's URL" in done.stderr
        assert inject.shim.token not in done.stderr
    finally:
        inject.close()


def test_the_launcher_refuses_a_shim_file_naming_anything_but_loopback(private, tmp_path):
    path = private / typesafe_shim.SHIM_FILE_NAME
    path.write_text(json.dumps({"shim_url": "http://10.0.0.5:8080/v1/systemone",
                                "token": secrets.token_urlsafe(16)}))
    done = _launch(path, _fake_server(tmp_path, marker=True))
    assert done.returncode == 1 and "names no loopback shim" in done.stderr


def test_switched_off_the_launcher_passes_the_key_and_no_url_as_today(private, tmp_path):
    key = _fake_key()
    path = private / jevkey.KEY_FILE_NAME
    path.write_text(key)
    done = _launch(path, _fake_server(tmp_path, marker=True))
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["key_sha"] == hashlib.sha256(key.encode()).hexdigest()
    assert out["url"] is None  # JEV_API_URL is not set for the TypeSafe backend
    assert key not in done.stdout + done.stderr


# --------------------------------------------------------------------------
# the judge follows on GKE, keeps today's path without a shim
# --------------------------------------------------------------------------


OPTED_IN = judge_settings.JudgeSettings(code_opt_in=True, backend=judge_settings.TYPESAFE)


def test_the_judge_without_a_shim_calls_typesafe_with_the_key():
    key = _fake_key()
    chosen = judge_settings.resolve_backend(OPTED_IN, key=key, environ={})
    assert chosen.url == judge_settings.TYPESAFE_URL and chosen.key == key


def test_the_judge_with_the_shim_calls_the_shim_with_its_token():
    token = secrets.token_urlsafe(16)
    url = "http://127.0.0.1:41234/v1/systemone"
    chosen = judge_settings.resolve_backend(OPTED_IN, key=token, environ={}, shim_url=url)
    assert chosen.url == url and chosen.key == token


@pytest.mark.parametrize("url", ["http://10.0.0.5:8080/v1/systemone",
                                 "https://api.typesafe.ai/v1/systemone",
                                 "http://127.0.0.1:1/v1/other"])
def test_the_judge_sends_nothing_to_a_shim_address_that_is_not_the_shims(url):
    chosen = judge_settings.resolve_backend(OPTED_IN, key=secrets.token_urlsafe(8), environ={},
                                            shim_url=url)
    assert isinstance(chosen, judge_settings.Off) and chosen.reason == "no_key"


def _judge_db() -> _Db:
    return _Db({"workspace_settings/eng": {"code_intelligence": {"jev": True},
                                           "judgment_backend": "typesafe"}})


def test_judge_setup_reads_the_shim_file_and_never_the_key_file(route, fwd, tmp_path):
    work, private = tmp_path / "work", tmp_path / "private"
    work.mkdir()
    private.mkdir(mode=0o700)
    key = _fake_key()
    (private / jevkey.KEY_FILE_NAME).write_text(key)  # a leftover must not be used
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        env = judge_attempt.setup(work=work, db=_judge_db(), tenant_id="eng",
                                  repository_url=None, environ={})
        config = json.loads(Path(env[judge_attempt.CONFIG_ENV]).read_text())
        assert config["backend"]["url"] == inject.shim.url
        assert Path(config["key_file"]).read_text() == inject.shim.token
        assert key not in json.dumps(config)
        # The judge's own client reaches the forwarder through the shim.
        client, why = judge_attempt.load({judge_attempt.CONFIG_ENV: env[judge_attempt.CONFIG_ENV]})
        assert why == "on"
        status, _ = judge_client.urllib_transport(
            client.backend.url, BODY, {"Content-Type": "application/json",
                                       "Authorization": f"Bearer {client.backend.key}"}, 10)
        assert status == 200
        assert fwd.header(0, "Authorization") == [f"Bearer {route.issued[0]}"]
    finally:
        inject.close()


def test_judge_setup_without_a_shim_keeps_todays_key_file(tmp_path):
    work, private = tmp_path / "work", tmp_path / "private"
    work.mkdir()
    private.mkdir(mode=0o700)
    key = _fake_key()
    (private / jevkey.KEY_FILE_NAME).write_text(key)
    env = judge_attempt.setup(work=work, db=_judge_db(), tenant_id="eng", repository_url=None,
                              environ={})
    config = json.loads(Path(env[judge_attempt.CONFIG_ENV]).read_text())
    assert config["backend"]["url"] == judge_settings.TYPESAFE_URL
    assert Path(config["key_file"]).read_text() == key


def test_the_judges_loopback_call_ignores_a_proxy_in_the_environment(route, fwd, private,
                                                                     monkeypatch):
    # A dead proxy: a call routed through it would fail.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    inject = _inject(route, fwd)
    inject.start(private)
    try:
        status, raw = judge_client.urllib_transport(
            inject.shim.url, BODY, {"Content-Type": "application/json",
                                    "Authorization": f"Bearer {inject.shim.token}"}, 10)
        assert (status, raw) == (200, b'{"answers": {}}')
    finally:
        inject.close()


# --------------------------------------------------------------------------
# the lifecycle
# --------------------------------------------------------------------------


AGENT = r'''
import json, os, pathlib, sys, time
time.sleep(float(sys.argv[1]))
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None, "metrics": {},
     "artifacts": []}))
'''


@pytest.fixture
def sleepy_agent(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    monkeypatch.setattr(lifecycle, "_runner_argv",
                        lambda cfg: [sys.executable, str(script), "4"])


def test_the_lifecycle_owns_one_inject_path_off_by_default(db, worker_factory, monkeypatch):
    monkeypatch.delenv(typesafe_shim.SWITCH_ENV, raising=False)
    worker, _, _ = worker_factory()
    assert isinstance(worker.typesafe, typesafe_shim.TypesafeInject)
    assert worker.typesafe.ticket.children is worker.children
    assert worker.typesafe.on is False


def test_a_cloud_run_worker_ignores_the_switch(db, worker_factory, monkeypatch):
    monkeypatch.setenv(typesafe_shim.SWITCH_ENV, typesafe_shim.SWITCH_ON)
    worker, _, _ = worker_factory()  # the mock profile runs on Cloud Run
    assert worker.cfg.backend != typesafe_shim.GKE_BACKEND
    assert worker.typesafe.on is False


def test_a_fenced_first_mint_stands_down_without_touching_the_lease(
        db, worker_factory, monkeypatch, sleepy_agent):
    def fenced(self, private_dir):
        raise typesafe_shim.TypesafeTicketFenced("typesafe ticket: 409 conflict")

    monkeypatch.setattr(typesafe_shim.TypesafeInject, "start", fenced)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None


def test_a_fenced_renewal_mid_run_stops_the_agent_and_leaves_the_lease(
        db, worker_factory, monkeypatch, sleepy_agent):
    def fenced(self):
        raise typesafe_shim.TypesafeTicketFenced("typesafe ticket: 409 conflict")

    monkeypatch.setattr(typesafe_shim.TypesafeInject, "due", lambda self: True)
    monkeypatch.setattr(typesafe_shim.TypesafeInject, "keep", fenced)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.GENERATION_FENCED
    assert db.doc("leases/lease_1")["released_at"] is None


def test_switched_off_the_run_never_asks_and_the_shim_is_closed(
        db, worker_factory, monkeypatch, sleepy_agent):
    kept: list[int] = []
    closed: list[int] = []
    original_close = typesafe_shim.TypesafeInject.close
    monkeypatch.setattr(typesafe_shim.TypesafeInject, "keep", lambda self: kept.append(1))
    monkeypatch.setattr(typesafe_shim.TypesafeInject, "close",
                        lambda self: (closed.append(1), original_close(self)))
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    assert kept == [] and closed == [1]
