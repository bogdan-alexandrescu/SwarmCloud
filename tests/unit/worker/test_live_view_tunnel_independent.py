"""The agent's browser does not depend on the viewer's tunnel (NOLIVE-GUARD, part of #1366).

MEASURED 2026-10-15 (benchmark devb-1015): 8 of 60 claude-code-browser
attempts ran with no live browser. Their pods logged, in order, "chromium up;
CDP proxy on 127.0.0.1:9222", "live gate: ready", "worker registered", then
~4 s later `tunnel_refused {"cause": "relay unreachable: gaierror"}`, and the
worker logged "no live path for this attempt" and started the agent with no
SWARM_CDP_ENDPOINT and no Playwright MCP entry. The gate's CDP proxy was up
the whole time: what failed was the HUMAN's view, the tunnel to the relay.

These tests hold the split:

  * a ready gate gives the agent its browser (SWARM_CDP_ENDPOINT, and so the
    runner's Playwright MCP entry) whether or not the tunnel opened, and the
    tunnel failure is said as "live view unavailable";
  * the tunnel is retried with a jittered backoff for the attempt's life, and
    the view opens late once the relay answers;
  * a gate that is NOT ready never starts the agent headless: the attempt
    fails retryably before the agent, its lease and capacity released;
  * "no live path for this attempt" is no longer said for a ready gate.

Every ticket here is built at runtime.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.errors import ExitCode
from agent_worker.hardening import FAILED, PROTECTED, MemoryProtection
from agent_worker.runners import claude_code
from swarm_common.states import TaskState

from test_handoff import (  # noqa: F401  (agent and trace are pytest fixtures, used by name)
    CDP,
    CTL,
    REQUEST,
    CtlRefused,
    FakeGateClient,
    FakeLiveRoutes,
    _live_worker,
    _Log,
    agent,
    trace,
)
from worker_seeds import seed_attempt

NO_LIVE_PATH = "no live path for this attempt"


class RelayDownGate(FakeGateClient):
    """A ready gate whose relay does not resolve for the first `failures` tickets.

    The gate's own `open_tunnel` records `tunnel_refused` and its ctl.sock
    answers `failed`, which L3's client raises as `CtlRefused`.
    """

    def __init__(self, trace: list[str], failures: int = 0) -> None:
        super().__init__(trace)
        self.failures = failures
        self.tunnel = "none"

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "tunnel_ticket":
            assert self.registered
            self.ops.append((op, dict(args)))
            self.trace.append("gate:tunnel_ticket")
            if self.failures > 0:
                self.failures -= 1
                raise CtlRefused("the gate refused tunnel_ticket: failed")
            self.tunnel = "open"
            return {"tunnel": "open"}
        result = super().call(op, args)
        if op == "status":
            return {"tunnel": self.tunnel, "browser": "running", "last_episode": None}
        return result


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _path(gate: FakeGateClient, routes: Any, *, memory: Any = None, key: Any = "make",
          clock: Clock | None = None, notes: list | None = None):
    if key == "make":
        key = children_mod.AttemptKey()
        routes.public_key = key.public
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=memory or MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes if key is not None else None, unavailable=None),
        live=True, gate_client=lambda p: gate,
        environ={"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP},
        wall=clock or Clock(), rand=lambda: 0.5,
    )
    path.setup(notes.append if notes is not None else None)
    return path


def _messages(path) -> list[str]:
    return [msg for msg, _ in path.log.lines]


# --------------------------------------------------------------------------
# A ready gate whose tunnel failed still gives the agent its browser
# --------------------------------------------------------------------------


class _SessionDown(FakeLiveRoutes):
    """swarm-api's live-session route failing the way a network blip does."""

    def call(self, method, path, *, body=None, headers=None):
        if path.endswith("/live-session"):
            self.calls.append({"path": path, "body": {}})
            raise children_mod.ChildApiError(503, "live_unavailable", "", retryable=True)
        return super().call(method, path, body=body, headers=headers)


@pytest.mark.parametrize("where", ["relay_gaierror_at_the_gate", "swarm_api_blip"])
def test_a_ready_gate_whose_tunnel_failed_still_gives_the_agent_its_browser(
    tmp_path, db, trace, where  # noqa: F811
):
    if where == "relay_gaierror_at_the_gate":
        gate, routes = RelayDownGate(trace, failures=1), FakeLiveRoutes(db, trace)
    else:
        gate, routes = RelayDownGate(trace), _SessionDown(db, trace)
    notes: list[dict[str, Any]] = []
    path = _path(gate, routes, notes=notes)

    assert path.browser_ready and not path.gate_missing
    work = tmp_path / "work"
    work.mkdir()
    env = path.prepare(work)
    assert env.get("SWARM_CDP_ENDPOINT") == CDP, "the agent lost its browser to the viewer's tunnel"
    # ...and with it, the runner's Playwright MCP entry.
    config = json.loads(claude_code.write_mcp_config(tmp_path / "mcp", env["SWARM_CDP_ENDPOINT"]).read_text())
    assert claude_code.MCP_SERVER_NAME in config["mcpServers"]

    assert NO_LIVE_PATH not in _messages(path)
    assert path.view is not None, "the view did not open, and the path must say so"
    (note,) = notes
    assert note["phase"] == "live_view"
    assert note["message"].startswith("live view unavailable")
    assert routes.ticket not in json.dumps(path.log.lines, default=str)


def test_the_runner_of_a_live_attempt_whose_tunnel_failed_gets_the_cdp_endpoint(
    db, worker_factory, agent, trace, monkeypatch  # noqa: F811
):
    """End to end through the lifecycle: the agent runs, with its browser."""
    write_plan, records = agent
    seed_attempt(db)
    routes = FakeLiveRoutes(db, trace)
    gate = RelayDownGate(trace, failures=10_000)  # the relay never resolves this attempt
    worker = _live_worker(worker_factory, routes, gate, trace, monkeypatch)
    assert worker.run() == ExitCode.OK
    (seen,) = records()
    assert seen["env"]["SWARM_CDP_ENDPOINT"] == CDP
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    marks = [e["detail"] for e in db.events("task_1")
             if (e.get("detail") or {}).get("cause") == "live_browser_unavailable"]
    assert marks and all(m["message"].startswith("live view unavailable") for m in marks), marks


# --------------------------------------------------------------------------
# The tunnel is retried, with jitter, and the view opens late
# --------------------------------------------------------------------------


def test_the_tunnel_is_retried_with_backoff_and_the_view_opens_after_n_failures(tmp_path, db, trace):  # noqa: F811
    failures = 4
    gate, routes, clock = RelayDownGate(trace, failures=failures), FakeLiveRoutes(db, trace), Clock()
    path = _path(gate, routes, clock=clock)
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731

    def tickets() -> int:
        return sum(1 for op, _ in gate.ops if op == "tunnel_ticket")

    assert tickets() == 1 and path.view is not None
    # Not on every poll: a tick inside the backoff asks for nothing.
    path.tick(work, fetch)
    assert tickets() == 1, "a failed tunnel was retried before its backoff"
    for _ in range(failures):
        clock.now += handoff_mod.TUNNEL_RETRY_MAX_SECONDS * 2
        path.tick(work, fetch)
    assert tickets() == failures + 1
    assert gate.tunnel == "open" and path.view is None, "the view never opened once the relay resolved"
    assert "live view opened" in _messages(path)
    retries = [kw for msg, kw in path.log.lines if msg == "live view not open; retrying"]
    assert len(retries) == failures, path.log.lines
    assert all(kw["code"] == handoff_mod.TUNNEL_REFUSED for kw in retries)
    assert [kw["tries"] for kw in retries] == list(range(1, failures + 1))
    # Never a ticket in a log line.
    assert routes.ticket not in json.dumps(path.log.lines, default=str)
    # Once open, nothing more is asked for.
    clock.now += handoff_mod.TUNNEL_RETRY_MAX_SECONDS * 2
    path.tick(work, fetch)
    assert tickets() == failures + 1


def test_the_retry_backoff_is_jittered_and_capped_for_the_attempts_life(db, trace):  # noqa: F811
    gate = RelayDownGate(trace, failures=10_000)
    delays = []
    for rand in (0.0, 1.0):
        clock = Clock()
        cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                              generation=1, runner_profile="mock")
        key = children_mod.AttemptKey()
        routes = FakeLiveRoutes(db, trace)
        routes.public_key = key.public
        path = handoff_mod.HandoffPath(
            cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
            children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
            live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL},
            wall=clock, rand=lambda r=rand: r,
        )
        path.setup()
        delays.append(path._tunnel_retry_at - clock.now)
        # Thirty more failures: still retried, never more than the cap apart.
        for _ in range(30):
            clock.now = path._tunnel_retry_at
            path._keep_tunnel({"tunnel": "none", "browser": "running"})
        assert path._tunnel_retry_at - clock.now <= handoff_mod.TUNNEL_RETRY_MAX_SECONDS * 1.25
        assert not path._tunnel_ended
    low, high = delays
    assert low < high, "the first retry is not jittered"


# --------------------------------------------------------------------------
# "no live path" is not said for a ready gate, whatever else is missing
# --------------------------------------------------------------------------


class _SessionClosed(FakeLiveRoutes):
    def call(self, method, path, *, body=None, headers=None):
        if path.endswith("/live-session"):
            self.calls.append({"path": path, "body": {}})
            raise children_mod.ChildApiError(409, "live_session_closed", "", retryable=False)
        return super().call(method, path, body=body, headers=headers)


@pytest.mark.parametrize("case", ["unprotected", "no_key", "session_closed", "channel_lost"])
def test_no_live_path_is_never_the_answer_for_a_ready_gate(tmp_path, db, trace, case):  # noqa: F811
    gate = RelayDownGate(trace)
    routes: Any = FakeLiveRoutes(db, trace)
    kwargs: dict[str, Any] = {}
    if case == "unprotected":
        kwargs["memory"] = MemoryProtection(FAILED, "prctl refused in this test")
    elif case == "no_key":
        kwargs["key"] = None
    elif case == "session_closed":
        routes = _SessionClosed(db, trace)
    else:
        gate.lose_on = "tunnel_ticket"
    path = _path(gate, routes, **kwargs)
    assert path.browser_ready
    assert NO_LIVE_PATH not in _messages(path), path.log.lines
    work = tmp_path / "work"
    work.mkdir()
    assert path.prepare(work).get("SWARM_CDP_ENDPOINT") == CDP


# --------------------------------------------------------------------------
# A gate that is not ready never starts the agent
# --------------------------------------------------------------------------


def test_a_gate_that_is_not_ready_is_gate_missing(db, trace):  # noqa: F811
    gate = RelayDownGate(trace)
    gate.refuse_registration = True
    path = _path(gate, FakeLiveRoutes(db, trace))
    assert path.gate_missing and not path.browser_ready
    assert path.unavailable == handoff_mod.GATE_UNAVAILABLE


def _capacity_released(db) -> bool:
    lease = db.doc("leases/lease_1")
    pools = [db.doc(name if name.startswith("pools/") else f"pools/{name}") for name in lease["pools"]]
    return lease.get("released_at") is not None and all(int(p["active"]) < 1 for p in pools)


def test_a_gate_that_is_not_ready_never_starts_the_agent_and_the_attempt_is_retried(
    db, worker_factory, agent, trace, monkeypatch  # noqa: F811
):
    write_plan, records = agent
    write_plan(REQUEST)
    seed_attempt(db)
    gate = RelayDownGate(trace)
    gate.refuse_registration = True  # a restarted gate refuses every registration
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch)
    assert worker.run() == ExitCode.FAILED
    assert records() == [], "the agent started headless on a live-browser profile"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task.get("last_error")
    assert task["last_error"].startswith(f"{handoff_mod.LIVE_GATE_UNAVAILABLE}: "), task["last_error"]
    assert task.get("next_eligible_at") is not None
    assert _capacity_released(db), db.doc("leases/lease_1")


def test_a_gate_that_is_not_ready_on_the_last_attempt_fails_the_task(
    db, worker_factory, agent, trace, monkeypatch  # noqa: F811
):
    seed_attempt(db, attempt_count=3)
    gate = RelayDownGate(trace)
    gate.refuse_registration = True
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch)
    assert worker.run() == ExitCode.FAILED
    _, records = agent
    assert records() == []
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert _capacity_released(db)
