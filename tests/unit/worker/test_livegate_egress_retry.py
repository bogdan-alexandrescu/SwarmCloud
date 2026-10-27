"""One bounded retry of the gate's upstream CONNECT (part of #1346).

Visual QA 2026-10-15: a Playwright session's egress chip read "Proxy failing"
and then "Residential · US", and one host failed twice with
ERR_TUNNEL_CONNECTION_FAILED before it loaded. Chromium reports every non-200
answer to its CONNECT that way, and before this change the gate's forwarder
answered 502 on the FIRST failure of the swarm-egress hop -- a dial refused
or timed out, or swarm-egress's own `vendor_unreachable` / `vendor_refused` --
and marked the whole path failing (`healthy`) until the next CONNECT was
served, which is what turned the chip red.

A CONNECT that has not been answered 200 has carried no byte to the site, so
dialling swarm-egress once more is safe. These tests hold:

* a dial that fails once and then succeeds: the page's CONNECT is answered
  200, its bytes are relayed, and the path stays healthy (the chip green);
* swarm-egress answering `vendor_refused` once and then 200: the same;
* a failure on both tries is still a 502 and still marks the path failing,
  after exactly two dials -- bounded, and still fail closed;
* a policy refusal (`tenant_off`, a 403) is not retried: it is not transient;
* the reachability probe tries twice before it calls swarm-egress unreachable.

Offline, every ticket built at run time; the harness is test_egress_gate's.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_worker.livegate import egress

from test_egress_gate import Upstream, World, _ticket, run

SWARM_EGRESS = ("swarm-egress.swarm-system.svc.cluster.local", 3128)


class FlakyDial:
    """`dial` for a World: refuses the first `fails` dials, then connects."""

    def __init__(self, world: World, fails: int) -> None:
        self.world = world
        self.fails = fails

    async def __call__(self, host: str, port: int):
        self.world.dialled.append((host, port))
        if self.fails > 0:
            self.fails -= 1
            raise ConnectionRefusedError("swarm-egress dropped the SYN")
        return await asyncio.open_connection("127.0.0.1", self.world.upstream.port)


class RefusesFirst(Upstream):
    """swarm-egress refusing its first `refusals` CONNECTs with `cause`, then serving."""

    def __init__(self, refusals: int, cause: str, status: int = 502) -> None:
        super().__init__()
        self.refusals = refusals
        self.refused_cause = cause
        self.refused_status = status

    async def _serve(self, reader, writer) -> None:
        if self.refusals > 0:
            self.refusals -= 1
            self.status, self.cause = self.refused_status, self.refused_cause
        else:
            self.status, self.cause = 200, ""
        await super()._serve(reader, writer)


def _page_load(health: dict[str, Any]):
    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        status, reader, writer = await world.connect()
        health["status"] = status
        if status == 200:
            writer.write(b"get /")
            await writer.drain()
            health["page"] = await asyncio.wait_for(reader.readexactly(5), 5)
        writer.close()
        health["healthy"] = world.forwarder.healthy()
        health["stats"] = world.forwarder.stats()
    return scenario


def _flaky_world(fails: int):
    def make(*args: Any, **kw: Any) -> World:
        world = World(*args, **kw)
        world.forwarder._dial = FlakyDial(world, fails)
        return world
    return make


def test_a_dial_that_fails_once_then_succeeds_loads_the_page_and_stays_healthy(monkeypatch):
    import test_egress_gate
    monkeypatch.setattr(test_egress_gate, "World", _flaky_world(1))
    health: dict[str, Any] = {}
    world = run(_page_load(health), Upstream())
    assert health["status"] == 200, "one refused dial reached Chromium as ERR_TUNNEL_CONNECTION_FAILED"
    assert health["page"] == b"GET /"
    assert health["healthy"] == (True, None), "one transient failure turned the chip to 'Proxy failing'"
    assert world.dialled == [SWARM_EGRESS] * 2
    assert ("egress_retried", {"cause": "forwarder_unreachable"}) in world.records
    assert health["stats"]["retried"] == 1 and health["stats"]["connects"] == 1
    assert world.unavailable == [], "a retried failure put the notice page on screen"


def test_a_vendor_refusal_once_then_a_tunnel_loads_the_page_and_stays_healthy():
    health: dict[str, Any] = {}
    world = run(_page_load(health), RefusesFirst(1, "vendor_refused"))
    assert health["status"] == 200, "one vendor refusal reached Chromium as ERR_TUNNEL_CONNECTION_FAILED"
    assert health["page"] == b"GET /"
    assert health["healthy"] == (True, None)
    assert world.upstream.accepted == 2
    assert ("egress_retried", {"cause": "vendor_refused"}) in world.records


def test_a_failure_on_both_tries_is_refused_and_the_path_is_failing():
    health: dict[str, Any] = {}
    world = run(_page_load(health), RefusesFirst(5, "vendor_unreachable"))
    assert health["status"] == 502
    assert health["healthy"] == (False, "vendor_unreachable")
    assert world.upstream.accepted == 2, "a transient refusal is tried once more, and only once"
    assert egress.CONNECT_ATTEMPTS == 2
    assert ("egress_refused", {"cause": "vendor_unreachable"}) in world.records


def test_swarm_egress_down_is_dialled_twice_and_never_the_site():
    health: dict[str, Any] = {}
    world = run(_page_load(health), None)
    assert health["status"] == 502
    assert health["healthy"] == (False, "forwarder_unreachable")
    assert world.dialled == [SWARM_EGRESS] * 2


def test_a_policy_refusal_is_not_retried():
    health: dict[str, Any] = {}
    world = run(_page_load(health), RefusesFirst(5, "tenant_off", status=403))
    assert health["status"] == 502
    assert world.upstream.accepted == 1
    assert health["healthy"] == (False, "tenant_off")
    assert "egress_retried" not in world.records.events()


def test_the_probe_tries_twice_before_it_calls_swarm_egress_unreachable(monkeypatch):
    import test_egress_gate
    monkeypatch.setattr(test_egress_gate, "World", _flaky_world(1))
    got: dict[str, Any] = {}

    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        got["probe"] = await world.forwarder.probe()

    world = run(scenario, Upstream())
    assert got["probe"] is True, "one dropped SYN at ticket time marked the path failing"
    assert world.dialled == [SWARM_EGRESS] * 2
