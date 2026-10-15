"""The gate rejoins a restarting relay at once (RELAY-ROLL, part of #1030).

MEASURED 2026-10-12/14: the relay restarted 40 times in ~48 h, once per
release, closing every tunnel with 1012. The gate's end came back only through
the worker's reopen (handoff.HandoffPath._keep_tunnel, LB-R6), which waited
TUNNEL_RETRY_SECONDS (5 s) after a refused reopen and doubled to a minute: a
roll that took the relay away for half a minute left the gate up to a minute
behind it, and every viewer that reconnected had no gate to meet.

These hold:

  * `livegate.tunnel.reconnect_delay` puts the first reopen after a 1012 under
    a second (~250 ms, jittered) and never past RESTART_RETRY_MAX_SECONDS,
    while every other close keeps the 5 s doubling to a minute;
  * the gate's `Tunnel` keeps the relay's close code and its `status` says it;
  * the worker reopens a 1012 tunnel in the same poll, retrying on that short
    schedule while the new relay comes up, bounded by RESTART_BURST_SECONDS;
  * the code is the relay's own (swarm_live.rendezvous.CLOSE_RESTARTING).

MUTATIONS: return RETRY_SECONDS for 1012 in `reconnect_delay`; drop the
`restart` branch in `_keep_tunnel` (a refused reopen then waits 5 s); do not
set `Tunnel.close_code` in `run`.

Every ticket here is built at runtime.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.hardening import PROTECTED, MemoryProtection
from agent_worker.livegate import tunnel

from test_handoff import CTL, _Log
from test_handoff import trace  # noqa: F401  (a pytest fixture, used by name)
from test_live_tunnel_reopens import Clock, FreshTickets, TunnelGate, _tickets_handed
from test_livegate_main import Rig, _relay_rendezvous, short_tmp  # noqa: F401  (short_tmp: a fixture)


# --- the delay ----------------------------------------------------------------


@pytest.mark.parametrize("rand", [0.0, 0.5, 0.999999])
def test_the_first_reopen_after_a_relay_restart_is_under_a_second(rand):
    delay = tunnel.reconnect_delay(tunnel.CLOSE_RESTARTING, 0, rand=lambda: rand)
    assert 0.1 <= delay < 1.0, delay
    # ~250 ms: jittered around RESTART_RETRY_SECONDS, not a fixed beat.
    assert abs(delay - tunnel.RESTART_RETRY_SECONDS) <= tunnel.RESTART_RETRY_SECONDS / 2


def test_restart_reopens_back_off_jittered_and_capped_under_the_workers_poll():
    delays = [tunnel.reconnect_delay(tunnel.CLOSE_RESTARTING, n, rand=lambda: 0.5) for n in range(12)]
    assert delays == sorted(delays) and delays[1] > delays[0]
    assert max(delays) == tunnel.RESTART_RETRY_MAX_SECONDS < 10, delays
    low = tunnel.reconnect_delay(tunnel.CLOSE_RESTARTING, 2, rand=lambda: 0.0)
    high = tunnel.reconnect_delay(tunnel.CLOSE_RESTARTING, 2, rand=lambda: 0.999)
    assert low < high, "the delay is not jittered"


@pytest.mark.parametrize("code", [None, 1000, 1001, 1006, 4410, 4404])
def test_every_other_close_keeps_the_slow_backoff(code):
    assert tunnel.reconnect_delay(code, 0) == tunnel.RETRY_SECONDS == 5
    assert tunnel.reconnect_delay(code, 10) == tunnel.RETRY_MAX_SECONDS == 60


def test_the_restart_code_is_the_relays():
    rendezvous = _relay_rendezvous()
    assert tunnel.CLOSE_RESTARTING == rendezvous.CLOSE_RESTARTING == 1012


# --- the gate keeps and reports the code --------------------------------------


class _ClosingRelay:
    def __init__(self, code: int) -> None:
        self.close_code: int | None = None
        self._code = code

    async def receive(self):
        self.close_code = self._code
        return None

    async def close(self, code: int = 1000) -> None:
        pass


def test_the_tunnel_keeps_the_relays_close_code():
    t = tunnel.Tunnel("ws://relay.test:8080/v1/tunnel")
    t._ws = _ClosingRelay(tunnel.CLOSE_RESTARTING)

    async def frames(op, conn_id, payload):
        raise AssertionError("no frame was sent")

    assert asyncio.run(t.run(frames)) == tunnel.CLOSE_RESTARTING
    assert t.closed and t.close_code == tunnel.CLOSE_RESTARTING


def test_the_gates_status_says_the_tunnels_close_code(short_tmp):  # noqa: F811
    rig = Rig(short_tmp)
    assert rig.gate.status()["tunnel_close_code"] is None
    closed = tunnel.Tunnel(rig.cfg.relay_url)
    closed.closed = True
    closed.close_code = tunnel.CLOSE_RESTARTING
    rig.gate.tunnel = closed
    status = rig.gate.status()
    assert status["tunnel"] == "closed" and status["tunnel_close_code"] == 1012


# --- the worker reopens at once -----------------------------------------------


class RestartGate(TunnelGate):
    """The gate's ctl.sock after the relay closed its tunnel with 1012; a
    `tunnel_ticket` is refused while the new relay is not up yet."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.close_code: int | None = None
        self.relay_down_for = 0

    def call(self, op, args):
        if op == "tunnel_ticket" and self.relay_down_for > 0:
            self.relay_down_for -= 1
            self.ops.append((op, args))
            raise handoff_mod.GateRefused("relay_failed: OSError")
        result = super().call(op, args)
        if op == "status":
            return {**result, "tunnel_close_code": self.close_code}
        return result


def _path(tmp_path, db, trace):
    gate = RestartGate(trace)
    routes = FreshTickets(db, trace)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    clock = Clock()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_2", lease_id="lease_2",
                          generation=2, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL}, wall=clock, sleep=sleep,
        # The slow backoff's jitter at its midpoint (x1.0), so a wait of
        # TUNNEL_RETRY_SECONDS is exactly the backoff, never short of it.
        rand=lambda: 0.5,
    )
    path.setup()
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    return path, gate, routes, clock, work, slept


def test_a_tunnel_the_relay_restarted_is_reopened_within_a_second_of_the_new_relay(tmp_path, db, trace):  # noqa: F811
    path, gate, routes, clock, work, slept = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    gate.tunnel, gate.close_code = "closed", tunnel.CLOSE_RESTARTING
    gate.relay_down_for = 2  # the new relay pod is not serving for the first two knocks
    started = clock.now
    assert path.tick(work, fetch) is None
    assert gate.tunnel == "open", "the restarted tunnel was not reopened in the same poll"
    assert len(_tickets_handed(gate)) == 4  # setup, two refused, the one that opened
    assert slept and slept[0] < 1.0, slept
    assert clock.now - started < 2.0, "the reopen waited the slow backoff"
    assert ("live tunnel reopened", {"reopened": 1, "after": "relay_restart"}) in path.log.lines


def test_a_relay_that_stays_down_holds_the_poll_a_bounded_time_and_is_tried_every_poll(tmp_path, db, trace):  # noqa: F811
    path, gate, routes, clock, work, slept = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    gate.tunnel, gate.close_code = "closed", tunnel.CLOSE_RESTARTING
    gate.relay_down_for = 10_000
    started = clock.now
    path.tick(work, fetch)
    assert clock.now - started <= handoff_mod.RESTART_BURST_SECONDS
    tried = len(_tickets_handed(gate))
    assert tried > 2
    # The next poll (the worker's is 10 s) tries again, not a minute later.
    clock.now += 10
    gate.relay_down_for = 0
    path.tick(work, fetch)
    assert len(_tickets_handed(gate)) == tried + 1 and gate.tunnel == "open"


def test_a_tunnel_closed_for_another_reason_keeps_the_slow_backoff(tmp_path, db, trace):  # noqa: F811
    path, gate, routes, clock, work, slept = _path(tmp_path, db, trace)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    gate.tunnel, gate.close_code = "closed", 1006
    gate.relay_down_for = 1
    path.tick(work, fetch)
    assert slept == [] and gate.tunnel == "closed"
    clock.now += 1
    path.tick(work, fetch)
    assert gate.tunnel == "closed", "a refused reopen is not retried on every poll"
    clock.now += handoff_mod.TUNNEL_RETRY_SECONDS
    path.tick(work, fetch)
    assert gate.tunnel == "open"
