"""The issue fetch bounds its connect and its read separately (observer proposal P3).

Measured 2026-10-06: C1E's issue fetch timed out at exactly 20 s and succeeded
on a retry 2 s later; C1A's took 19.99 s against the same single 20 s timeout.
One number bounded the TCP+TLS connect and every read alike, so a connect that
stalled spent the whole 20 s before anything tried again. Now:

  * a connect is bounded at `issue._CONNECT_TIMEOUT` (5 s) and tried up to
    `issue._CONNECT_TRIES` (3) times with backoff, inside the one request;
  * once connected, the socket's timeout is `issue._READ_TIMEOUT` (20 s): a
    forge that accepted and then says nothing still fails at the read timeout,
    and that stall is NOT retried at the connect level;
  * a 404 is one connection and one request, never retried;
  * a redirect is still refused, and the token is in no message;
  * the other forge calls (`forge._request`) keep their single 30 s timeout and
    their single connect, because they pass no connect bound.

The stalled connects are a stand-in `socket.create_connection` that raises the
TimeoutError a real connect raises when its timeout passes, after recording the
timeout it was given; the reads are against a real local server.

MUTATIONS: drop the `connect=` argument from `issue._open` -- the stalled
connect is not retried and the timeout recorded is 20, not 5. Drop the
`settimeout(read)` in `forge._ConnectBounded.connect` -- the slow-answer test
fails at the 5 s connect timeout instead of reading. Retry `ssl.SSLError` --
the certificate test sees three connects.
"""

from __future__ import annotations

import dataclasses
import http.server
import socket
import ssl
import threading
import time
import urllib.request
from typing import Any, Iterator

import pytest

from agent_worker import forge
from agent_worker import issue as issue_mod

from fake_github import fresh_token

_real_create_connection = socket.create_connection


class _Server:
    """A local HTTP server; `delay` seconds before answering, or never."""

    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}', *,
                 delay: float = 0.0, headers: dict[str, str] | None = None) -> None:
        self.hits: list[str] = []
        self.release = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits.append(self.path)
                if delay == float("inf"):
                    outer.release.wait(10)
                    return
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "_Server":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


class _Connects:
    """Stands in for `socket.create_connection`: records each timeout, and
    raises `fail[i]` for the i-th call (None: connect for real)."""

    def __init__(self, *fail: BaseException | None) -> None:
        self.fail = list(fail)
        self.timeouts: list[float] = []
        self.sockets: list[socket.socket] = []

    def __call__(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):  # noqa: ANN001
        self.timeouts.append(timeout)
        index = len(self.timeouts) - 1
        if index < len(self.fail) and self.fail[index] is not None:
            raise self.fail[index]
        sock = _real_create_connection(address, timeout, *args, **kwargs)
        self.sockets.append(sock)
        return sock


@pytest.fixture
def sleeps(monkeypatch) -> Iterator[list[float]]:
    """The connect backoff's sleeps, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(
        issue_mod, "_CONNECT", dataclasses.replace(issue_mod._CONNECT, sleep=slept.append)
    )
    yield slept


def _request(url: str, token: str) -> urllib.request.Request:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    return req


def test_the_values_are_the_ones_the_measurement_asked_for():
    assert issue_mod._CONNECT_TIMEOUT == 5
    assert issue_mod._CONNECT_TRIES == 3
    assert issue_mod._READ_TIMEOUT == 20
    assert issue_mod._CONNECT.timeout == issue_mod._CONNECT_TIMEOUT
    assert issue_mod._CONNECT.tries == issue_mod._CONNECT_TRIES
    # Three stalled connects and their backoff stay inside the old single
    # 20 s bound: the split never makes the worst connect slower than before.
    worst = issue_mod._CONNECT_TIMEOUT * issue_mod._CONNECT_TRIES + sum(
        issue_mod._CONNECT.backoff_seconds * 2**i for i in range(issue_mod._CONNECT_TRIES - 1)
    )
    assert worst <= 20


def test_a_stalled_connect_is_retried_and_the_fetch_succeeds(monkeypatch, sleeps):
    connects = _Connects(TimeoutError("timed out"), None)
    monkeypatch.setattr(socket, "create_connection", connects)
    token = fresh_token()
    with _Server() as server:
        status, data, _headers = issue_mod._open(_request(f"{server.base}/repos/o/r/issues/1", token))

    assert (status, data) == (200, {"ok": True})
    assert server.hits == ["/repos/o/r/issues/1"]
    # Each connect bounded at 5 s, not at the 20 s read timeout.
    assert connects.timeouts == [5, 5]
    assert sleeps == [issue_mod._CONNECT.backoff_seconds]
    # The stalled try cost its connect timeout, then one backoff: ~5 s + backoff,
    # where the single timeout cost 20 s.
    assert connects.timeouts[0] + sum(sleeps) <= 5 + issue_mod._CONNECT.backoff_seconds
    # Once connected, the socket waits for the answer for the read timeout.
    (sock,) = connects.sockets
    assert sock.gettimeout() == 20


def test_a_refused_connect_is_retried_too(monkeypatch, sleeps):
    connects = _Connects(ConnectionRefusedError(111, "Connection refused"), None)
    monkeypatch.setattr(socket, "create_connection", connects)
    with _Server() as server:
        status, _data, _headers = issue_mod._open(_request(f"{server.base}/x", fresh_token()))
    assert status == 200
    assert connects.timeouts == [5, 5]
    assert len(sleeps) == 1


def test_a_connect_that_stalls_every_try_is_unreachable_after_three(monkeypatch, sleeps):
    stall = TimeoutError("timed out")
    connects = _Connects(stall, stall, stall)
    monkeypatch.setattr(socket, "create_connection", connects)
    token = fresh_token()
    with _Server() as server:
        with pytest.raises(issue_mod.IssueUnreachable) as raised:
            issue_mod._open(_request(f"{server.base}/x", token))
        assert server.hits == []
    assert connects.timeouts == [5, 5, 5]
    base = issue_mod._CONNECT.backoff_seconds
    assert sleeps == [base, base * 2]
    assert token not in str(raised.value)
    assert "timed out" in str(raised.value)


def test_a_certificate_refusal_is_not_retried(monkeypatch, sleeps):
    """A host that could not prove itself is not a blip: one connect, no backoff."""
    connects = _Connects(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    monkeypatch.setattr(socket, "create_connection", connects)
    with pytest.raises(issue_mod.IssueUnavailable):
        issue_mod._open(_request("http://127.0.0.1:9/x", fresh_token()))
    assert connects.timeouts == [5]
    assert sleeps == []


def test_an_answer_slower_than_the_connect_timeout_is_still_read(monkeypatch, sleeps):
    """The read is bounded by the read timeout, not the connect timeout."""
    monkeypatch.setattr(
        issue_mod, "_CONNECT", dataclasses.replace(issue_mod._CONNECT, timeout=0.1)
    )
    monkeypatch.setattr(issue_mod, "_READ_TIMEOUT", 3)
    with _Server(delay=0.6) as server:
        status, data, _headers = issue_mod._open(_request(f"{server.base}/x", fresh_token()))
    assert (status, data) == (200, {"ok": True})
    assert sleeps == []


def test_a_read_that_stalls_fails_at_the_read_timeout_and_is_not_retried(monkeypatch, sleeps):
    monkeypatch.setattr(issue_mod, "_READ_TIMEOUT", 0.5)
    connects = _Connects()
    monkeypatch.setattr(socket, "create_connection", connects)
    token = fresh_token()
    with _Server(delay=float("inf")) as server:
        started = time.monotonic()
        with pytest.raises(issue_mod.IssueUnreachable) as raised:
            issue_mod._open(_request(f"{server.base}/x", token))
        elapsed = time.monotonic() - started
        assert server.hits == ["/x"]
    assert 0.4 <= elapsed < 3, elapsed
    # One connect: a stalled READ is not a stalled connect, and is not retried
    # inside the request (the whole fetch is retried by `stage_issue`).
    assert connects.timeouts == [5]
    assert sleeps == []
    assert token not in str(raised.value)


def test_a_404_is_one_connect_and_one_request(monkeypatch, sleeps):
    connects = _Connects()
    monkeypatch.setattr(socket, "create_connection", connects)
    with _Server(status=404, body=b'{"message": "Not Found"}') as server:
        status, data, _headers = issue_mod._open(_request(f"{server.base}/x", fresh_token()))
    assert (status, data) == (404, {"message": "Not Found"})
    assert server.hits == ["/x"]
    assert connects.timeouts == [5]
    assert sleeps == []


def test_a_404_ends_stage_issue_without_a_retry(monkeypatch, sleeps, tmp_path):
    """Through the whole fetch: an issue the forge says is missing is asked for once."""
    asked: list[str] = []

    def answer(req: urllib.request.Request) -> tuple[int, Any, dict[str, str]]:
        asked.append(req.full_url)
        return 404, {"message": "Not Found"}, {}

    monkeypatch.setattr(issue_mod, "_open", answer)
    policy = forge.RetryPolicy(attempts=3, sleep=lambda _s: None)
    with pytest.raises(issue_mod.IssueUnavailable):
        issue_mod.stage_issue(
            number=7, repository_url="https://github.com/octo/widgets", token=None,
            refusal=None, work_dir=tmp_path, scrub=lambda text: text, logger=None, retry=policy,
        )
    assert len(asked) == 1


def test_a_redirect_is_still_refused_through_the_bounded_connect(monkeypatch, sleeps):
    connects = _Connects()
    monkeypatch.setattr(socket, "create_connection", connects)
    token = fresh_token()
    with _Server(status=302, body=b"", headers={"Location": "https://elsewhere.invalid/x"}) as server:
        with pytest.raises(issue_mod.IssueUnavailable) as raised:
            issue_mod._open(_request(f"{server.base}/start", token))
    assert server.hits == ["/start"]
    assert connects.timeouts == [5]
    assert "302" in str(raised.value)
    assert token not in str(raised.value)


def test_the_other_forge_calls_keep_one_timeout_and_one_connect(monkeypatch):
    """`forge._request` passes no connect bound: its connect is bounded by its
    30 s request timeout and is tried once, as before."""
    stall = TimeoutError("timed out")
    connects = _Connects(stall)
    monkeypatch.setattr(socket, "create_connection", connects)
    with pytest.raises(forge.ForgeUnavailable):
        forge._request("http://127.0.0.1:9/x", token=fresh_token())
    assert connects.timeouts == [forge._TIMEOUT]
