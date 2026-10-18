"""The bridge answers quick calls while any number of rows hold (#1437, BRIDGE-POOL).

Measured 2026-10-16: with ~17 workflows attached as live rows, two submits
through the bridge ended NOT_SUBMITTED and `swarm_check` timed out after
1800 s, because `serve()` ran every `tools/call` on ONE pool of 32 workers and
each row holds one `swarm_follow` for its window. These hold `serve()` to two
lanes: a hold runs on a pool of `HOLD_CALLS`, every other call on a quick pool
no hold can occupy.

Offline: `server._call` is replaced by a stand-in whose holds block on an
Event, and the client is a placeholder; nothing leaves the process, and no
wait is longer than the test needs.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp import server

#: The arguments that make each blocking tool hold, and the ones that do not.
HOLDING = [
    ("swarm_follow", {"task_ids": ["t1"], "format": "progress", "wait_seconds": 600}),
    ("swarm_follow", {"task_ids": ["t1"], "format": "lines", "wait_seconds": 30}),
    ("swarm_follow", {"task_ids": ["t1"], "format": "progress", "parents": ["t0"]}),
    ("swarm_follow", {"task_ids": ["t1"], "format": "progress", "wait_seconds": "600"}),
    ("swarm_wait", {"task_ids": ["t1"]}),
    ("swarm_wait", {"task_ids": ["t1"], "timeout_seconds": 60}),
    ("swarm_collect", {"task_ids": ["t1"]}),
    ("swarm_collect", {"task_ids": ["t1"], "wait_seconds": 60}),
    ("swarm_setup_status", {"wait_for_github_seconds": 120}),
    ("swarm_account_remove", {"account_id": "acct-1"}),
]

QUICK = [
    ("swarm_follow", {"task_ids": ["t1"]}),
    ("swarm_follow", {"task_ids": ["t1"], "format": "progress", "wait_seconds": 0}),
    ("swarm_follow", {"task_ids": ["t1"], "format": "progress", "parents": []}),
    ("swarm_wait", {"task_ids": ["t1"], "timeout_seconds": 0}),
    ("swarm_collect", {"task_ids": ["t1"], "wait_seconds": 0}),
    ("swarm_setup_status", {}),
    ("swarm_workflow_status", {"workflow_id": "wf_1"}),
    ("swarm_check", {}),
    ("swarm_keys", {}),
    ("swarm_workflow", {"repo": "o/r", "title": "x", "steps": []}),
    ("swarm_workflows", {}),
    ("swarm_status", {"task_ids": ["t1"]}),
]


@pytest.mark.parametrize(("name", "args"), HOLDING)
def test_every_call_that_blocks_is_a_hold(name, args):
    assert server._holds(name, args) is True


@pytest.mark.parametrize(("name", "args"), QUICK)
def test_a_call_that_answers_from_a_few_reads_is_quick(name, args):
    assert server._holds(name, args) is False


def test_the_hold_pool_size_is_read_from_the_environment_and_bounded():
    assert server.HOLD_CALLS_DEFAULT == 256
    assert server._hold_calls({}) == 256
    assert server._hold_calls({"SWARM_MCP_MAX_HOLDS": "300"}) == 300
    assert server._hold_calls({"SWARM_MCP_MAX_HOLDS": "5"}) == 32
    assert server._hold_calls({"SWARM_MCP_MAX_HOLDS": "5000"}) == 1024
    assert server._hold_calls({"SWARM_MCP_MAX_HOLDS": "lots"}) == 256
    assert server.MAX_CONCURRENT_CALLS == 32


class _Wire:
    """stdout for `serve`: every answer line, and an event per answered id."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self.at: dict[object, float] = {}
        self._lock = threading.Lock()
        self.answered = threading.Condition(self._lock)

    def write(self, text: str) -> int:
        for line in text.splitlines():
            if line.strip():
                message = json.loads(line)
                with self.answered:
                    self.lines.append(message)
                    if "id" in message:
                        self.at[message["id"]] = time.monotonic()
                    self.answered.notify_all()
        return len(text)

    def flush(self) -> None:
        return None

    def wait_for(self, ids: set, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.answered:
            while not ids <= set(self.at):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.answered.wait(left)
            return True


def _request(message_id: int, name: str, args: dict) -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": message_id, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }) + "\n"


def test_quick_calls_answer_while_every_hold_worker_is_held(monkeypatch):
    """With HOLD_CALLS holds blocked on an Event, a status read, a check and a
    submit each answer while the holds are still held. On the single pool of
    32 the holds took every worker and the three queued behind them."""
    monkeypatch.delenv("SWARM_MCP_MAX_HOLDS", raising=False)
    holds = server._hold_calls()
    release = threading.Event()
    entered = threading.Semaphore(0)
    lanes: dict[str, list[bool]] = {"hold": [], "quick": []}
    lane_lock = threading.Lock()

    def _fake_call(client, name, args, *, keepalive=False):  # noqa: ARG001
        is_hold = server._holds(name, args)
        with lane_lock:
            lanes["hold" if is_hold else "quick"].append(mcp_client.in_hold_lane())
        if is_hold:
            entered.release()
            release.wait(30)
            return "held"
        return f"answered {name}"

    monkeypatch.setattr(server, "_call", _fake_call)
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: object())

    quick_ids = {10_001, 10_002, 10_003}
    wire = _Wire()
    report: dict[str, object] = {}

    def _stdin():
        for i in range(holds):
            yield _request(i, "swarm_follow", {
                "task_ids": [f"t{i % 5}"], "format": "progress",
                "wait_seconds": 1800, "parents": [f"p{i % 5}"],
            })
        # Every hold is running and blocked before any quick call is sent.
        got = 0
        deadline = time.monotonic() + 15
        while got < holds and entered.acquire(timeout=max(0.0, deadline - time.monotonic())):
            got += 1
        report["holds_running"] = got
        sent = time.monotonic()
        yield _request(10_001, "swarm_workflow_status", {"workflow_id": "wf_1"})
        yield _request(10_002, "swarm_check", {})
        yield _request(10_003, "swarm_workflow", {"repo": "o/r", "title": "x", "steps": []})
        report["quick_answered_while_held"] = wire.wait_for(quick_ids, timeout=10)
        report["quick_wait"] = max(
            (wire.at.get(i, time.monotonic()) - sent) for i in quick_ids
        )
        report["holds_still_held"] = not release.is_set()
        release.set()

    with contextlib.redirect_stdout(wire):
        server.serve(stdin=_stdin())

    assert report["holds_running"] == holds, f"only {report['holds_running']} of {holds} holds ran"
    assert report["quick_answered_while_held"] is True
    assert report["holds_still_held"] is True
    assert report["quick_wait"] < 5.0
    answers = {m["id"]: m["result"]["content"][0]["text"] for m in wire.lines if "id" in m}
    assert answers[10_001] == "answered swarm_workflow_status"
    assert answers[10_002] == "answered swarm_check"
    assert answers[10_003] == "answered swarm_workflow"
    assert sum(1 for i in range(holds) if answers.get(i) == "held") == holds
    # The hold pool's threads are marked, so the rate gate can serve a quick
    # call's request first; a quick thread is not.
    assert lanes["hold"] and all(lanes["hold"])
    assert lanes["quick"] and not any(lanes["quick"])


def test_serve_paces_the_client_it_builds(monkeypatch):
    """The shared reads and the rate gate are switched on by `serve` for its
    one client, and by nothing else (the CLI's client is not paced)."""
    paced: list[object] = []

    class _Recorded(mcp_client.SwarmClient):
        def __init__(self, *a, **k) -> None:  # noqa: ARG002 - no network
            self.tenant = k.get("tenant")

        def pace_for_bridge(self, **kw) -> None:  # noqa: ARG002
            paced.append(self)

    monkeypatch.setattr(server, "SwarmClient", _Recorded)
    monkeypatch.setattr(server, "_call", lambda client, name, args, keepalive=False: "ok")
    wire = _Wire()
    with contextlib.redirect_stdout(wire):
        server.serve(stdin=iter([
            _request(1, "swarm_check", {}),
            _request(2, "swarm_follow", {"task_ids": ["t1"], "wait_seconds": 5, "format": "progress"}),
        ]))
    assert len(paced) == 1
    assert {m["id"] for m in wire.lines} == {1, 2}
