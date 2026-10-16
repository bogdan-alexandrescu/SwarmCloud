"""The bridge stays under swarm-api's rate limit (#1437, BRIDGE-POOL).

swarm-api allows each principal 20 requests a second with a burst of 40, and
every hold of one laptop is one principal. With `pace_for_bridge`, the client
takes a token per request from a bucket of 15/s and 30 -- below the API's, so
the console in the same browser keeps room -- quick calls before holds, and a
GET answered 429 is tried again; a write never is.

Offline: the real `SwarmClient` over a replaced `client._open`; the retry's
sleep is recorded, not slept.
"""

from __future__ import annotations

import email.message
import io
import json
import statistics
import threading
import time

import pytest
import urllib.error

from swarm_mcp import client as mcp_client
from swarm_mcp.client import RateGate, SwarmClient, SwarmError


class _Response:
    status = 200

    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _too_many(url: str, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    body = json.dumps({"detail": "rate limit exceeded"}).encode()
    return urllib.error.HTTPError(url, 429, "Too Many Requests", headers, io.BytesIO(body))


@pytest.fixture()
def answers(monkeypatch):
    """A queue of answers for the transport: a payload, or an HTTPError to raise."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Answers", (), {})()
    state.queue = []
    state.sent = []

    def _opener(req, timeout=None):  # noqa: ARG001
        state.sent.append((req.get_method(), req.full_url))
        answer = state.queue.pop(0) if state.queue else {"task": {"task_id": "t1", "state": "RUNNING"}}
        if callable(answer):
            raise answer(req.full_url)
        return _Response(answer)

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return state


def _paced(sleeps: list[float], environ: dict | None = None) -> SwarmClient:
    client = SwarmClient(base_url="http://api.invalid")
    client.pace_for_bridge(environ=environ or {}, sleep=sleeps.append)
    return client


# -- the bucket -------------------------------------------------------------

def test_the_defaults_sit_below_the_apis_limit():
    client = SwarmClient(base_url="http://api.invalid", connect=False)
    client.pace_for_bridge(environ={})
    assert (client._gate.rate, client._gate.burst) == (15.0, 30)
    assert mcp_client.RATE_PER_SECOND_DEFAULT < 20 and mcp_client.RATE_BURST_DEFAULT < 40
    # A third of the burst is kept for quick calls.
    assert client._gate.reserve == 10


def test_the_environment_moves_the_rate_but_never_past_the_apis():
    client = SwarmClient(base_url="http://api.invalid", connect=False)
    client.pace_for_bridge(environ={"SWARM_MCP_RATE_PER_SECOND": "18", "SWARM_MCP_RATE_BURST": "35"})
    assert (client._gate.rate, client._gate.burst) == (18.0, 35)
    client.pace_for_bridge(environ={"SWARM_MCP_RATE_PER_SECOND": "100", "SWARM_MCP_RATE_BURST": "500"})
    assert (client._gate.rate, client._gate.burst) == (20.0, 40)
    client.pace_for_bridge(environ={"SWARM_MCP_RATE_PER_SECOND": "fast", "SWARM_MCP_RATE_BURST": ""})
    assert (client._gate.rate, client._gate.burst) == (15.0, 30)


def test_the_bucket_holds_requests_to_its_rate():
    """Past the burst, takes come no faster than the rate: 30 takes at 50/s
    with a burst of 5 need at least (30 - 5) / 50 = 0.5 s."""
    gate = RateGate(50, 5)
    began = time.monotonic()
    for _ in range(30):
        gate.take()
    assert time.monotonic() - began >= 0.45


def test_the_paced_client_takes_a_token_per_request(answers):
    client = SwarmClient(base_url="http://api.invalid")
    client.pace_for_bridge(environ={"SWARM_MCP_RATE_PER_SECOND": "20", "SWARM_MCP_RATE_BURST": "3"})
    answers.queue = [{"attempts": []}] * 13
    began = time.monotonic()
    for i in range(13):
        client.attempts(f"t{i}")  # not a shared route: every call is a request
    # 3 from the burst, 10 more at 20/s.
    assert time.monotonic() - began >= 0.45
    assert len(answers.sent) == 13


class _FakeClock:
    """Time moves only when a test moves it, so no assertion depends on a CI
    machine's scheduling."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.002)


def test_a_quick_call_is_never_made_to_wait_behind_holds():
    """Sixteen holds asking for tokens as fast as they can: they stop at the
    reserve, and when the quick calls then outrun the bucket, the next token
    refilled goes to the waiting quick call, never to a hold."""
    clock = _FakeClock()
    gate = RateGate(20, 6, clock=clock)  # reserve 2
    stop = threading.Event()
    hold_takes = [0]
    lock = threading.Lock()

    def _hold() -> None:
        while not stop.is_set():
            gate.take(hold=True)
            with lock:
                hold_takes[0] += 1

    for _ in range(16):
        threading.Thread(target=_hold, daemon=True).start()
    try:
        # The holds spend 6 - 2 = 4 tokens and no more: the clock never moves.
        _until(lambda: hold_takes[0] == 4, "the holds never spent what they may")
        quick_done = []

        def _quick() -> None:
            for _ in range(3):  # the reserve is 2: the third must wait for a refill
                gate.take()
                quick_done.append(1)

        quick = threading.Thread(target=_quick, daemon=True)
        quick.start()
        _until(lambda: len(quick_done) == 2 and gate._quick_waiting == 1, "the quick call did not queue")
        assert hold_takes[0] == 4
        clock.now += 0.1  # two tokens refill; holds need three, quick needs one
        quick.join(10)
        assert quick_done == [1, 1, 1], "a quick call was held behind the holds"
        assert hold_takes[0] == 4, "a hold took a token a quick call was waiting for"
    finally:
        stop.set()  # the hold threads are daemons; one blocked in `take` is left


def test_a_hold_does_not_spend_the_reserve():
    clock = _FakeClock()
    gate = RateGate(1, 9, clock=clock)  # reserve 3; the clock never refills it
    taken = [0]

    def _holds() -> None:
        for _ in range(9):
            gate.take(hold=True)
            taken[0] += 1

    threading.Thread(target=_holds, daemon=True).start()
    _until(lambda: taken[0] == 6, "the hold did not take what it may")
    # It is now blocked at the reserve; give it every chance to take more.
    for _ in range(50):
        time.sleep(0.002)
        assert taken[0] == 6, "a hold took from the quick calls' reserve"
    for _ in range(3):  # all three reserve tokens are there for quick calls
        gate.take()
    assert gate._tokens == 0.0


# -- the 429 retry ------------------------------------------------------------

def test_a_get_answered_429_is_tried_again_with_backoff(answers):
    sleeps: list[float] = []
    client = _paced(sleeps)
    answers.queue = [_too_many, _too_many, {"attempts": []}]
    assert client.attempts("t1") == []
    assert len(answers.sent) == 3
    assert len(sleeps) == 2
    assert 2.0 <= sleeps[0] <= 2.5 and 4.0 <= sleeps[1] <= 5.0


def test_retry_after_is_honoured(answers):
    sleeps: list[float] = []
    client = _paced(sleeps)
    answers.queue = [lambda url: _too_many(url, "7"), {"attempts": []}]
    client.attempts("t1")
    assert sleeps == [7.0]


def test_after_three_retries_the_429_is_reported_as_it_is_today(answers):
    sleeps: list[float] = []
    client = _paced(sleeps)
    answers.queue = [_too_many] * 6
    with pytest.raises(SwarmError) as caught:
        client.attempts("t1")
    assert caught.value.status == 429
    assert "GET /v1/tasks/t1/attempts?limit=20 -> 429" in str(caught.value)
    assert len(answers.sent) == 4
    assert len(sleeps) == 3
    assert 8.0 <= sleeps[2] <= 10.0


def test_a_write_is_never_retried(answers):
    sleeps: list[float] = []
    client = _paced(sleeps)
    answers.queue = [_too_many, {"ok": True}]
    with pytest.raises(SwarmError) as caught:
        client.request("POST", "/v1/workflows", payload={"steps": []})
    assert caught.value.status == 429
    assert answers.sent == [("POST", "http://api.invalid/v1/workflows")]
    assert sleeps == []


def test_a_shared_read_answered_429_is_retried_once_for_every_caller(answers):
    sleeps: list[float] = []
    client = _paced(sleeps)
    answers.queue = [_too_many, {"task": {"task_id": "t1", "state": "RUNNING"}}]
    assert client.task("t1")["state"] == "RUNNING"
    assert client.task("t1")["state"] == "RUNNING"
    assert len(answers.sent) == 2


def test_a_client_that_is_not_the_bridge_does_not_retry(answers):
    client = SwarmClient(base_url="http://api.invalid")
    answers.queue = [_too_many, {"attempts": []}]
    with pytest.raises(SwarmError):
        client.attempts("t1")
    assert len(answers.sent) == 1
