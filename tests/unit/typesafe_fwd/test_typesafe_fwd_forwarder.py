"""swarm-typesafe-fwd, the forwarder that holds the TypeSafe key so it never
enters the agent pod (docs/design/workspace-secrets.md §3.5, the `inject`
delivery mode; owner decision §10 Q8; lane SEC-INJECT-B, part of #1347).

Drives the REAL HTTP server over loopback, with a fake upstream that records
what it was sent and a fake Secret Manager reader that records what it was
asked for. The lane's acceptance, one test or more each:

  * a ticket minted by `swarm_api.typesafe_ticket` (the reference) verifies
    and is forwarded with the named secret's value as the bearer key;
  * a forged, expired, stale-key, malformed or over-long ticket is 401
    `ticket_invalid`; a tenant not served is 403 `tenant_off`; a lower
    generation is 409 `stale_generation`; and in every one nothing is
    forwarded and no secret is read;
  * another path, another method, a query string, an absolute-form target or
    a Host naming another host is 404 `not_found`, nothing forwarded;
  * the caller's Authorization (and every other header but Content-Type,
    Accept and User-Agent) is replaced or dropped, never passed through;
  * a ticket naming another tenant's key is refused, and the secret is read
    from the forwarder's own project, never one a ticket names;
  * the kill switch, and a setting that cannot be read, refuse (503 `disabled`);
  * the per-attempt rate limit refuses past its limit (429 `rate_limited`);
  * the previous ring key still verifies after a rotation;
  * no log line, response header or error carries the key, the MAC key or
    the ticket.

Nothing needs a cloud, a network or a credential: every key and value is
built at runtime.
"""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest

from swarm_api.typesafe_ticket import mint_typesafe_ticket
from swarm_typesafe_fwd.config import FwdConfig
from swarm_typesafe_fwd.forwarder import Forwarder
from swarm_typesafe_fwd.keyring import KeyRing, SecretCache
from swarm_typesafe_fwd.server import make_server
from swarm_typesafe_fwd.upstream import UpstreamResponse, UpstreamTimeout

PROJECT = "swarm-test-project"
SERVICE_HOST = "swarm-typesafe-fwd.swarm-system.svc.cluster.local"
PATH = "/v1/systemone"
LOGGER = "swarm_typesafe_fwd"
TENANT = "acme"
OTHER = "globex"


def _fake(prefix: str) -> str:
    # Built at runtime, never one literal (CLAUDE.md, "Nothing you add may
    # look like a credential").
    return prefix + secrets.token_hex(24)


class Clock:
    def __init__(self) -> None:
        self.at = float(int(time.time()))

    def __call__(self) -> float:
        return self.at


class Reader:
    """Secret Manager: a value per full version name, and every name asked."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.asked: list[str] = []

    def put(self, secret_id: str, version: str, value: str) -> None:
        self.values[f"projects/{PROJECT}/secrets/{secret_id}/versions/{version}"] = value.encode()

    def __call__(self, name: str) -> bytes:
        self.asked.append(name)
        if name not in self.values:
            raise RuntimeError("Secret Manager answered HTTP 404")
        return self.values[name]


@dataclass
class Upstream:
    """TypeSafe: records each request it is sent and streams an answer."""

    status: int = 200
    chunks: tuple[bytes, ...] = (b'{"verdict":', b'"pass"}')
    fail: Exception | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, body: bytes, headers: dict[str, str], timeout: float) -> UpstreamResponse:
        self.sent.append({"body": body, "headers": dict(headers), "timeout": timeout})
        if self.fail is not None:
            raise self.fail
        chunks = self.chunks

        def stream() -> Iterator[bytes]:
            yield from chunks

        return UpstreamResponse(status=self.status, content_type="application/json",
                                chunks=stream(), close=lambda: None)


class World:
    def __init__(self, *, config: FwdConfig | None = None) -> None:
        self.clock = Clock()
        self.mac_key = _fake("k")
        self.ring = KeyRing(self.mac_key.encode())
        self.reader = Reader()
        self.tenant_value = _fake("ts")
        self.other_value = _fake("ts")
        self.reader.put(f"swarm-tenant-{TENANT}-typesafe", "3", self.tenant_value)
        self.reader.put(f"swarm-tenant-{TENANT}-typesafe", "latest", self.tenant_value)
        self.reader.put(f"swarm-tenant-{OTHER}-typesafe", "latest", self.other_value)
        self.upstream = Upstream()
        self.config = config or FwdConfig(enabled=True, cause="enabled",
                                          tenants=frozenset({TENANT, OTHER}),
                                          requests_per_minute=60, max_request_bytes=4096,
                                          max_response_seconds=30)
        self.cache = SecretCache(self.reader, project=PROJECT, clock=self.clock)
        self.forwarder = Forwarder(keys=self.ring, secrets=self.cache,
                                   config=lambda: self.config, upstream=self.upstream,
                                   clock=self.clock)
        self.server = make_server(self.forwarder, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def ticket(self, *, key: str | None = None, tenant_id: str = TENANT, task_id: str = "t-1",
               attempt_id: str = "a-1", generation: int = 1, scope: str = "tenant",
               secret_id: str | None = None, secret_version: str = "3",
               life: int = 300) -> str:
        return mint_typesafe_ticket(
            key or self.mac_key, tenant_id=tenant_id, task_id=task_id, attempt_id=attempt_id,
            generation=generation, scope=scope,
            secret_id=secret_id or f"swarm-tenant-{tenant_id}-typesafe",
            secret_version=secret_version, exp=int(self.clock()) + life)

    def call(self, *, method: str = "POST", target: str = PATH, host: str | None = SERVICE_HOST,
             ticket: str | None = None, body: bytes = b'{"q":1}',
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                conn.putheader("Host", host)
            if ticket is not None:
                conn.putheader("Authorization", f"Bearer {ticket}")
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


def _refused(answer: tuple[int, dict[str, str], bytes], status: int, code: str) -> None:
    got, headers, body = answer
    assert (got, headers.get("x-swarm-typesafe-refused")) == (status, code), (got, headers, body)


def _secrets_in(world: World) -> list[str]:
    return [world.mac_key, world.tenant_value, world.other_value]


# ---------------------------------------------------------------------------
# the reference ticket verifies, and the key is the one the ticket names
# ---------------------------------------------------------------------------
def test_a_ticket_swarm_api_minted_is_forwarded_with_the_named_key(world):
    status, headers, body = world.call(ticket=world.ticket(), body=b'{"page":"x"}')
    assert status == 200, (status, headers, body)
    assert body == b'{"verdict":"pass"}'
    assert headers["content-type"] == "application/json"
    assert "x-swarm-typesafe-refused" not in headers
    (sent,) = world.upstream.sent
    assert sent["body"] == b'{"page":"x"}'
    assert sent["headers"]["Authorization"] == f"Bearer {world.tenant_value}"
    assert sent["timeout"] == 30
    # Read from the forwarder's own project, at the version the ticket pins.
    assert world.reader.asked == [
        f"projects/{PROJECT}/secrets/swarm-tenant-{TENANT}-typesafe/versions/3"]


def test_latest_is_read_when_the_ticket_pins_latest(world):
    status, _, _ = world.call(ticket=world.ticket(secret_version="latest"))
    assert status == 200
    assert world.reader.asked[-1].endswith("/versions/latest")


def test_a_value_is_cached_for_at_most_sixty_seconds(world):
    for _ in range(2):
        assert world.call(ticket=world.ticket())[0] == 200
    assert len(world.reader.asked) == 1
    world.clock.at += 61
    assert world.call(ticket=world.ticket())[0] == 200
    assert len(world.reader.asked) == 2


# ---------------------------------------------------------------------------
# tickets that must not verify: nothing forwarded, nothing read
# ---------------------------------------------------------------------------
def _forged(world: World) -> str:
    good = world.ticket()
    payload, mac = good.split(".")
    flipped = ("A" if mac[0] != "A" else "B") + mac[1:]
    return f"{payload}.{flipped}"


def _bad_tickets(world: World) -> dict[str, str | None]:
    return {
        "missing": None,
        "forged_mac": _forged(world),
        "other_key": world.ticket(key=_fake("k")),
        "expired": world.ticket(life=-1),
        "too_long_lived": world.ticket(life=3600),
        "no_dot": "abc",
        "two_dots": "a.b.c",
        "not_base64": "!!!.???",
        "empty_parts": ".",
        "bad_version": world.ticket(secret_version="../../other"),
        "tenant_scope_names_another_tenants_key": world.ticket(
            secret_id=f"swarm-tenant-{OTHER}-typesafe"),
        "user_scope_names_a_non_typesafe_secret": world.ticket(
            scope="user", secret_id="swarm-tenant-u-pat-git"),
        "unknown_scope": world.ticket(scope="platform"),
        "not_a_tenant_secret": world.ticket(secret_id="swarm-github-app-key"),
    }


BAD_TICKETS = ("missing", "forged_mac", "other_key", "expired", "too_long_lived", "no_dot",
               "two_dots", "not_base64", "empty_parts", "bad_version",
               "tenant_scope_names_another_tenants_key", "user_scope_names_a_non_typesafe_secret",
               "unknown_scope", "not_a_tenant_secret")


def test_the_bad_ticket_list_is_the_one_the_cases_build(world):
    assert tuple(_bad_tickets(world)) == BAD_TICKETS


@pytest.mark.parametrize("case", BAD_TICKETS)
def test_every_bad_ticket_is_ticket_invalid_and_nothing_is_forwarded(world, case):
    answer = world.call(ticket=_bad_tickets(world)[case])
    _refused(answer, 401, "ticket_invalid")
    assert world.upstream.sent == []
    assert world.reader.asked == []


def test_a_ticket_with_an_extra_claim_is_refused_even_under_the_right_key(world):
    import base64
    import hashlib
    import hmac

    from swarm_api.typesafe_ticket import TICKET_DOMAIN

    claims = {"tenant_id": TENANT, "task_id": "t", "attempt_id": "a", "generation": 1,
              "scope": "tenant", "secret_id": f"swarm-tenant-{TENANT}-typesafe",
              "secret_version": "3", "exp": int(world.clock()) + 60, "nonce": "0" * 16,
              "project": "elsewhere"}
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(world.mac_key.encode(), TICKET_DOMAIN + payload.encode(), hashlib.sha256)
    ticket = payload + "." + base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")
    _refused(world.call(ticket=ticket), 401, "ticket_invalid")
    assert world.upstream.sent == []


def test_a_tenant_not_served_is_tenant_off(world):
    world.config = FwdConfig(enabled=True, cause="enabled", tenants=frozenset({OTHER}),
                             requests_per_minute=60, max_request_bytes=4096,
                             max_response_seconds=30)
    _refused(world.call(ticket=world.ticket()), 403, "tenant_off")
    assert world.upstream.sent == [] and world.reader.asked == []


def test_tenant_a_never_spends_tenant_b_key(world):
    """B is served and its key exists; A's ticket naming it is refused, and
    B's own ticket spends only B's."""
    _refused(world.call(ticket=world.ticket(secret_id=f"swarm-tenant-{OTHER}-typesafe",
                                            secret_version="latest")), 401, "ticket_invalid")
    assert world.reader.asked == []
    assert world.call(ticket=world.ticket(tenant_id=OTHER, secret_version="latest"))[0] == 200
    assert world.upstream.sent[-1]["headers"]["Authorization"] == f"Bearer {world.other_value}"


def test_a_lower_generation_is_stale(world):
    assert world.call(ticket=world.ticket(generation=2))[0] == 200
    _refused(world.call(ticket=world.ticket(generation=1)), 409, "stale_generation")
    assert len(world.upstream.sent) == 1
    # The same generation and a higher one still serve.
    assert world.call(ticket=world.ticket(generation=2))[0] == 200
    assert world.call(ticket=world.ticket(generation=3))[0] == 200


# ---------------------------------------------------------------------------
# the key ring
# ---------------------------------------------------------------------------
def test_the_previous_ring_key_still_verifies_after_a_rotation(world):
    old = world.ticket()
    new_key = _fake("k")
    assert world.ring.rotate(new_key.encode())
    assert world.call(ticket=old)[0] == 200
    assert world.call(ticket=world.ticket(key=new_key))[0] == 200
    # A second rotation drops the first key: a stale key outside the ring.
    assert world.ring.rotate(_fake("k").encode())
    _refused(world.call(ticket=old), 401, "ticket_invalid")
    assert len(world.upstream.sent) == 2


def test_rotating_to_the_same_value_keeps_the_ring():
    ring = KeyRing(b"x" * 40)
    assert not ring.rotate(b"x" * 40)
    assert ring.keys() == [b"x" * 40]


# ---------------------------------------------------------------------------
# only POST /v1/systemone at the Service's name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,target,host", [
    ("GET", PATH, SERVICE_HOST),
    ("PUT", PATH, SERVICE_HOST),
    ("DELETE", PATH, SERVICE_HOST),
    ("OPTIONS", PATH, SERVICE_HOST),
    ("PATCH", PATH, SERVICE_HOST),
    ("BREW", PATH, SERVICE_HOST),
    ("POST", "/v1/systemone/", SERVICE_HOST),
    ("POST", "/v1/other", SERVICE_HOST),
    ("POST", "/", SERVICE_HOST),
    ("POST", "/v1/systemone?u=http://metadata.google.internal", SERVICE_HOST),
    ("POST", "//api.typesafe.ai/v1/systemone", SERVICE_HOST),
    ("POST", "/v1/../v1/systemone", SERVICE_HOST),
    ("POST", "http://169.254.169.254/v1/systemone", SERVICE_HOST),
    ("POST", "https://api.typesafe.ai/v1/systemone", SERVICE_HOST),
    ("POST", PATH, "metadata.google.internal"),
    ("POST", PATH, "api.typesafe.ai"),
    ("POST", PATH, f"{SERVICE_HOST}:9090"),
    ("POST", PATH, f"{SERVICE_HOST}.evil.test"),
    ("POST", PATH, None),
])
def test_anything_but_post_systemone_at_the_service_is_404(world, method, target, host):
    answer = world.call(method=method, target=target, host=host, ticket=world.ticket())
    _refused(answer, 404, "not_found")
    assert world.upstream.sent == [] and world.reader.asked == []


@pytest.mark.parametrize("host", [SERVICE_HOST, f"{SERVICE_HOST}:8080", SERVICE_HOST.upper()])
def test_the_service_name_with_or_without_the_port_is_served(world, host):
    assert world.call(host=host, ticket=world.ticket())[0] == 200


# ---------------------------------------------------------------------------
# headers: the caller's authorization never passes
# ---------------------------------------------------------------------------
def test_the_callers_authorization_is_replaced_and_other_headers_dropped(world):
    status, _, _ = world.call(ticket=world.ticket(), headers={
        "Accept": "application/json", "User-Agent": "jev-browser/0.2.0",
        "Cookie": "session=abc", "X-Forwarded-For": "10.0.0.1", "X-Api-Key": "caller",
        "Proxy-Authorization": "Basic caller", "X-Goog-User-Project": "elsewhere"})
    assert status == 200
    (sent,) = world.upstream.sent
    assert sent["headers"] == {"Authorization": f"Bearer {world.tenant_value}",
                               "Content-Type": "application/json",
                               "Accept": "application/json",
                               "User-Agent": "jev-browser/0.2.0"}


def test_two_authorization_headers_are_refused(world):
    answer = world.call(ticket=world.ticket(),
                        headers={"Authorization": f"Bearer {world.ticket()}"})
    _refused(answer, 401, "ticket_invalid")
    assert world.upstream.sent == []


def test_a_body_over_the_limit_is_413_and_not_forwarded(world):
    _refused(world.call(ticket=world.ticket(), body=b"x" * 4097), 413, "too_large")
    assert world.upstream.sent == [] and world.reader.asked == []


# ---------------------------------------------------------------------------
# the setting: kill switch, fail closed, rate limit
# ---------------------------------------------------------------------------
def test_the_kill_switch_refuses(world):
    world.config = FwdConfig(enabled=False, cause="disabled", tenants=frozenset({TENANT}),
                             requests_per_minute=60, max_request_bytes=4096,
                             max_response_seconds=30)
    _refused(world.call(ticket=world.ticket()), 503, "disabled")
    assert world.upstream.sent == [] and world.reader.asked == []


def test_the_rate_limit_refuses_past_its_limit(world):
    world.config = FwdConfig(enabled=True, cause="enabled", tenants=frozenset({TENANT}),
                             requests_per_minute=3, max_request_bytes=4096,
                             max_response_seconds=30)
    for _ in range(3):
        assert world.call(ticket=world.ticket())[0] == 200
    _refused(world.call(ticket=world.ticket()), 429, "rate_limited")
    assert len(world.upstream.sent) == 3
    # Per attempt: another attempt of the same task is not limited by this one.
    assert world.call(ticket=world.ticket(attempt_id="a-2"))[0] == 200
    # And the window moves on.
    world.clock.at += 61
    assert world.call(ticket=world.ticket())[0] == 200


# ---------------------------------------------------------------------------
# upstream failures
# ---------------------------------------------------------------------------
def test_an_unreadable_key_is_502_and_nothing_is_forwarded(world):
    _refused(world.call(ticket=world.ticket(secret_version="9")), 502, "key_unreadable")
    assert world.upstream.sent == []


def test_an_upstream_timeout_is_504(world):
    world.upstream.fail = UpstreamTimeout("timed out")
    _refused(world.call(ticket=world.ticket()), 504, "upstream_timeout")


def test_an_upstream_failure_is_502(world):
    world.upstream.fail = OSError("connection refused")
    _refused(world.call(ticket=world.ticket()), 502, "upstream_failed")


def test_the_upstream_status_is_streamed_back(world):
    world.upstream.status = 429
    world.upstream.chunks = (b'{"error":"slow down"}',)
    status, headers, body = world.call(ticket=world.ticket())
    assert (status, body) == (429, b'{"error":"slow down"}')
    assert "x-swarm-typesafe-refused" not in headers


# ---------------------------------------------------------------------------
# nothing secret leaves: logs, headers, errors
# ---------------------------------------------------------------------------
def test_no_key_value_or_ticket_reaches_a_log_line_or_a_response(world, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    tickets = [world.ticket(), world.ticket(generation=0), world.ticket(life=-1),
               world.ticket(secret_version="9")]
    answers = [world.call(ticket=t) for t in tickets]
    world.upstream.fail = OSError(f"refused {world.tenant_value}")
    answers.append(world.call(ticket=world.ticket(generation=5)))
    world.upstream.fail = None
    lines = [r.getMessage() for r in caplog.records if r.name.startswith(LOGGER)]
    assert lines, "the forwarder logged nothing"
    # Every line is a JSON event with the fields §3.5 names and no others.
    allowed = {"event", "code", "status", "attempt_id", "tenant_id", "task_id", "scope",
               "secret_id", "bytes_in", "bytes_out", "duration_ms", "cause", "port"}
    for line in lines:
        assert set(json.loads(line)) <= allowed, line
    forbidden = _secrets_in(world) + tickets
    # A ticket's payload half alone is not logged either.
    forbidden += [t.split(".")[0] for t in tickets]
    for text in lines + [repr(a) for a in answers]:
        for value in forbidden:
            assert value not in text


def test_the_log_line_carries_the_code_and_the_ids(world, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    world.call(ticket=world.ticket(attempt_id="a-77", task_id="t-77"))
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name.startswith(LOGGER)]
    served = [e for e in events if e["event"] == "typesafe_forwarded"]
    assert served and served[-1]["attempt_id"] == "a-77"
    assert served[-1]["tenant_id"] == TENANT
    assert served[-1]["secret_id"] == f"swarm-tenant-{TENANT}-typesafe"
    assert served[-1]["code"] == "forwarded" and served[-1]["status"] == 200
