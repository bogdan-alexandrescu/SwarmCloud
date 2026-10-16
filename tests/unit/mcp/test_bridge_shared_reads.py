"""The bridge's holds share their reads (#1437, BRIDGE-POOL).

Every row's hold re-read its routes every `progress.POLL_SECONDS`, and nothing
was shared between holds on the same task, so 60-85 holds of one laptop -- one
principal at swarm-api's 20/s -- drew 429s. With `pace_for_bridge`, the task
document, the workflow document, the events page and the live log window are
read once per key per poll for every caller, single-flight; nothing else is.

Offline: the real `SwarmClient` over a replaced `client._open` that counts.
"""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlsplit

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp import progress
from swarm_mcp.client import SwarmClient


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


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def wire(monkeypatch):
    """Every request sent: (method, path with query, tenant header)."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    sent: list[tuple[str, str, str | None]] = []
    lock = threading.Lock()
    gates: dict[str, threading.Event] = {}

    def _opener(req, timeout=None):  # noqa: ARG001
        parts = urlsplit(req.full_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k.lower(): v for k, v in req.header_items()}
        tenant = headers.get("x-swarm-tenant")
        with lock:
            sent.append((req.get_method(), path, tenant))
        gate = gates.get(parts.path)
        if gate is not None:
            gate.wait(10)
        # Long enough that concurrent callers of one key overlap in flight.
        time.sleep(0.01)
        if "/events" in path:
            return _Response({"events": [], "next_page_token": None})
        if "/logs" in path:
            return _Response({"streams": [{"stream": "stdout", "status": "ok", "content": ""}]})
        if "/attempts" in path:
            return _Response({"attempts": []})
        if "/artifacts" in path:
            return _Response({"artifacts": [], "complete": False})
        if path.startswith("/v1/workflows/"):
            return _Response({"workflow_id": parts.path.rsplit("/", 1)[-1], "state": "RUNNING"})
        task_id = parts.path.split("/")[3] if parts.path.startswith("/v1/tasks/") else "?"
        return _Response({"task": {"task_id": task_id, "state": "RUNNING", "tenant": tenant}})

    monkeypatch.setattr(mcp_client, "_open", _opener)
    wire_state = type("Wire", (), {})()
    wire_state.sent = sent
    wire_state.gates = gates
    return wire_state


def _paced(clock=None, **kw) -> SwarmClient:
    client = SwarmClient(base_url="http://api.invalid", **kw)
    client.pace_for_bridge(environ={}, clock=clock or time.monotonic)
    return client


def test_the_shared_read_lasts_one_poll():
    assert mcp_client.SHARED_READ_TTL_SECONDS == progress.POLL_SECONDS


def test_forty_holds_on_five_tasks_make_one_read_per_route_per_task_per_poll(wire):
    """40 holds following the same 5 tasks, each reading the task document, its
    events page and its log window: at most 5 x 3 requests per poll. Unshared,
    it was 40 x 5 x 3 = 600."""
    clock = _Clock()
    client = _paced(clock)
    tasks = [f"task_{i}" for i in range(5)]
    holds, polls = 40, 3
    start = threading.Barrier(holds + 1)
    done = threading.Barrier(holds + 1)
    errors: list[BaseException] = []

    def _hold() -> None:
        mcp_client.enter_hold_lane()
        for _ in range(polls):
            start.wait()
            try:
                for task_id in tasks:
                    client.task(task_id)
                    client.events_page(task_id, limit=50, newest_first=True)
                    client.logs(task_id, offset=0, limit_bytes=4096)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            done.wait()

    threads = [threading.Thread(target=_hold) for _ in range(holds)]
    for t in threads:
        t.start()
    per_poll: list[int] = []
    for _ in range(polls):
        before = len(wire.sent)
        start.wait()
        done.wait()
        per_poll.append(len(wire.sent) - before)
        clock.now += mcp_client.SHARED_READ_TTL_SECONDS
    for t in threads:
        t.join()
    assert not errors
    assert per_poll == [5 * 3] * polls


def test_the_workflow_document_is_shared_too(wire):
    client = _paced(_Clock())
    for _ in range(10):
        client.request("GET", "/v1/workflows/wf_1")
    assert wire.sent == [("GET", "/v1/workflows/wf_1", None)]


def test_a_read_is_never_handed_to_another_tenant(wire):
    client = _paced(_Clock(), tenant="team-a")
    first = client.task("t1")
    client.tenant = "team-b"
    second = client.task("t1")
    assert [s[2] for s in wire.sent] == ["team-a", "team-b"]
    assert first["tenant"] == "team-a" and second["tenant"] == "team-b"


def test_other_gets_and_every_write_go_to_the_api_each_time(wire):
    client = _paced(_Clock())
    client.attempts("t1")
    client.attempts("t1")
    client.artifacts("t1")
    client.artifacts("t1")
    client.request("GET", "/v1/workflows?active=true")
    client.request("GET", "/v1/workflows?active=true")
    client.request("POST", "/v1/tasks/t1/cancel", payload={})
    client.request("POST", "/v1/tasks/t1/cancel", payload={})
    methods_paths = [(m, p) for m, p, _ in wire.sent]
    assert methods_paths == [
        ("GET", "/v1/tasks/t1/attempts?limit=20"),
        ("GET", "/v1/tasks/t1/attempts?limit=20"),
        ("GET", "/v1/tasks/t1/artifacts"),
        ("GET", "/v1/tasks/t1/artifacts"),
        ("GET", "/v1/workflows?active=true"),
        ("GET", "/v1/workflows?active=true"),
        ("POST", "/v1/tasks/t1/cancel"),
        ("POST", "/v1/tasks/t1/cancel"),
    ]


def test_a_write_clears_the_shared_reads(wire):
    """After a cancel, the next status read sees what the cancel did."""
    client = _paced(_Clock())
    client.task("t1")
    client.task("t1")
    client.request("POST", "/v1/tasks/t1/cancel", payload={})
    client.task("t1")
    assert [(m, p) for m, p, _ in wire.sent] == [
        ("GET", "/v1/tasks/t1"), ("POST", "/v1/tasks/t1/cancel"), ("GET", "/v1/tasks/t1"),
    ]


def test_each_caller_gets_its_own_copy(wire):
    client = _paced(_Clock())
    first = client.task("t1")
    first["state"] = "MANGLED"
    assert client.task("t1")["state"] == "RUNNING"
    assert len(wire.sent) == 1


def test_a_different_query_is_a_different_read(wire):
    client = _paced(_Clock())
    client.logs("t1", offset=0)
    client.logs("t1", offset=4096)
    client.logs("t1", offset=0)
    assert [p for _, p, _ in wire.sent] == [
        "/v1/tasks/t1/logs?source=auto&offset=0",
        "/v1/tasks/t1/logs?source=auto&offset=4096",
    ]


def test_a_quick_call_never_waits_on_a_holds_read(wire):
    """A hold's read may sit at the rate gate behind the reserve; a quick call
    of the same key reads for itself rather than joining it."""
    client = _paced(_Clock())
    hold_gate = threading.Event()
    wire.gates["/v1/tasks/t1"] = hold_gate
    held = threading.Thread(target=lambda: (mcp_client.enter_hold_lane(), client.task("t1")))
    held.start()
    deadline = time.monotonic() + 5
    while not wire.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(wire.sent) == 1
    del wire.gates["/v1/tasks/t1"]  # the quick call's own request is not held
    answered = threading.Event()
    quick = threading.Thread(target=lambda: (client.task("t1"), answered.set()))
    quick.start()
    try:
        assert answered.wait(5), "the quick call waited on the hold's read"
        assert len(wire.sent) == 2
    finally:
        hold_gate.set()
        held.join(5)
        quick.join(5)


def test_a_client_that_is_not_the_bridge_shares_nothing(wire):
    """The CLI and every caller but `serve` read the API each time, as before."""
    client = SwarmClient(base_url="http://api.invalid")
    client.task("t1")
    client.task("t1")
    assert len(wire.sent) == 2
