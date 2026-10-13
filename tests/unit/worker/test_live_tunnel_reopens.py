"""A live session whose tunnel dropped is joined again by its gate (LB-R6, part of #1030).

MEASURED 2026-10-12 on SwarmCloud, re-QA pass 8 (task_4e0cf1c649634b949703,
claude-code-browser on GKE): attempt 2 (att_75e17009c2a7418b85e7, generation 2)
resumed at 21:23:16Z, the owner took and returned control (21:24:41Z /
21:25:34Z), and the agent kept driving. At 21:28Z a fresh console tab showed
LIVE, the origin strip `https://github.com` and the iPhone 15 chip -- every
one of them read from `live_sessions/{id}`, which keeps the last report the
gate sent -- while the phone frame stayed solid black for over 30 s.

The gate's tunnel to the relay is opened ONCE per attempt
(`HandoffPath.setup`), and nothing opened it again. The design says the
opposite (docs/design/live-browser.md §3.3): "A relay restart drops every
live session for the seconds it takes to come back; both ends reconnect".
The console end did (LiveBrowser.tsx retries with a new ticket); the gate's
did not. So once the tunnel dropped -- a relay restart, a rollout, a
connection the relay closed -- the session stayed `open`, its stored origin
and device kept the strip and the chips drawn, and every viewer the relay
was asked to admit had no tunnel to meet: no RFB byte, and the frame showed
the viewer's own dark background.

These tests hold the worker's half: the gate's `status` already says
`tunnel: closed`; the worker now asks swarm-api for a fresh tunnel ticket
(the live-session route answers an open session with one, counting
`tunnel_tickets`) and hands it to the gate, at most once per backoff, and
stops when swarm-api says the session is over. The gate's half -- a reopened
tunnel serves a viewer frames on the resumed mobile preset, and a tunnel
the relay went silent on is closed so it can be reopened -- is in
test_live_resume_view.py.

Every ticket here is built at runtime.
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace
from typing import Any

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.hardening import PROTECTED, MemoryProtection

from test_handoff import CTL, FakeGateClient, FakeLiveRoutes, _Log, _spool_request
from test_handoff import trace  # noqa: F401  (a pytest fixture, used by name)


class TunnelGate(FakeGateClient):
    """The gate's ctl.sock, whose `status` reports the tunnel as the gate sees it."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.tunnel = "open"

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = super().call(op, args)
        if op == "status":
            return {"tunnel": self.tunnel, "browser": "running", "last_episode": None}
        if op == "tunnel_ticket":
            self.tunnel = "open"
            return {"tunnel": "open"}
        return result


class FreshTickets(FakeLiveRoutes):
    """swarm-api's live-session route: a new one-use ticket on every call, or a refusal."""

    def __init__(self, db, trace: list[str]) -> None:
        super().__init__(db, trace)
        self.refuse: children_mod.ChildApiError | None = None
        self.issued: list[str] = []

    def call(self, method, path, *, body=None, headers=None):
        if path.endswith("/live-session"):
            if self.refuse is not None:
                self.calls.append({"path": path, "body": {}})
                raise self.refuse
            self.ticket = "tkt-" + secrets.token_hex(16)
            self.issued.append(self.ticket)
        return super().call(method, path, body=body, headers=headers)

    def session_calls(self) -> int:
        return sum(1 for p in self.paths() if p.endswith("/live-session"))


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _path(tmp_path, db, trace):
    gate = TunnelGate(trace)
    routes = FreshTickets(db, trace)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    clock = Clock()
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_2", lease_id="lease_2",
                          generation=2, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL}, wall=clock,
    )
    path.setup()
    assert path.offered and routes.session_calls() == 1
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    return path, gate, routes, clock, work


def _tickets_handed(gate: TunnelGate) -> list[str]:
    return [args["ticket"] for op, args in gate.ops if op == "tunnel_ticket"]


def test_a_tunnel_the_relay_dropped_is_reopened_with_a_fresh_ticket(tmp_path, db, trace):  # noqa: F811
    path, gate, routes, clock, work = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    first = _tickets_handed(gate)
    assert first == routes.issued[:1]

    gate.tunnel = "closed"  # the relay restarted: the gate's tunnel ended
    assert path.tick(work, fetch) is None
    assert routes.session_calls() == 2, "the worker never asked swarm-api for a new tunnel ticket"
    handed = _tickets_handed(gate)
    assert handed == routes.issued, "the fresh ticket did not reach the gate"
    assert handed[-1] != handed[0], "a ticket is one-use: the relay refuses it twice"
    assert gate.tunnel == "open"
    assert path.offered, "a reopened tunnel is still a live path"
    assert ("live tunnel reopened", {"reopened": 1}) in path.log.lines


def test_an_open_tunnel_asks_swarm_api_for_nothing(tmp_path, db, trace):  # noqa: F811
    path, gate, routes, clock, work = _path(tmp_path, db, trace)
    for _ in range(5):
        clock.now += 30
        path.tick(work, lambda: db.doc("tasks/task_1"))
    assert routes.session_calls() == 1
    assert len(_tickets_handed(gate)) == 1


def test_a_failed_reopen_is_retried_after_a_backoff_not_every_tick(tmp_path, db, trace):  # noqa: F811
    path, gate, routes, clock, work = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    gate.tunnel = "closed"
    routes.refuse = children_mod.ChildApiError(503, "live_unavailable", "", retryable=True)
    path.tick(work, fetch)
    assert routes.session_calls() == 2
    for _ in range(3):
        clock.now += 1
        path.tick(work, fetch)
    assert routes.session_calls() == 2, "a refused reopen is not retried on every poll"

    routes.refuse = None
    clock.now += handoff_mod.TUNNEL_RETRY_MAX_SECONDS
    path.tick(work, fetch)
    assert routes.session_calls() == 3 and gate.tunnel == "open"


def test_a_session_swarm_api_closed_is_not_asked_for_again(tmp_path, db, trace):  # noqa: F811
    """409 live_session_closed (or live_fenced): the attempt's live path has
    ended -- a stale generation, a cancel -- and no ticket will come."""
    path, gate, routes, clock, work = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    gate.tunnel = "closed"
    routes.refuse = children_mod.ChildApiError(409, "live_session_closed", "", retryable=False)
    path.tick(work, fetch)
    for _ in range(5):
        clock.now += handoff_mod.TUNNEL_RETRY_MAX_SECONDS
        path.tick(work, fetch)
    assert routes.session_calls() == 2
    assert len(_tickets_handed(gate)) == 1


def test_the_tunnel_is_reopened_while_a_person_is_being_waited_for(tmp_path, db, trace):  # noqa: F811
    """The hand-off is exactly when a person needs the view."""
    path, gate, routes, clock, work = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    _spool_request(work, "r1", {"reason": "sign in", "expect": "github.com"})
    path.tick(work, fetch)
    assert path.active is not None
    gate.tunnel = "closed"
    clock.now += 1
    assert path.tick(work, fetch) is None
    assert routes.session_calls() == 2 and gate.tunnel == "open"
    assert path.active is not None, "reopening the tunnel does not end the hand-off"
