"""A burst of browser tasks does not lose its live path to a relay name lookup (LB-DNS, part of #1366).

MEASURED 2026-10-15 (benchmark devb-1015): 8 of 60 claude-code-browser
attempts logged `tunnel_refused {"cause": "relay unreachable: gaierror"}`
about 4 s after `worker registered`, then gave the first tunnel up. swarm-live
is a headless Service (clusterIP: None, one replica), so its name has no
address while its pod is not ready; a burst of pod starts can also swamp the
resolver. Those two call for different manifest fixes, so:

  * the gate's cause names the resolver failure's class -- `resolver_busy`
    (EAI_AGAIN), `no_address` (EAI_NONAME) or `other` -- never just "gaierror";
  * the worker's first tunnel open, refused while the relay's name does not
    resolve, is tried again with a jittered backoff for up to
    `RELAY_LOOKUP_RETRY_SECONDS`, each retry logged with its class;
  * a refusal while the name resolves, and a swarm-api failure, behave as
    before: one try, then the per-poll retry.

Every ticket here is built at runtime; no test reaches a real resolver.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.hardening import PROTECTED, MemoryProtection
from agent_worker.livegate import tunnel

from test_handoff import CDP, CTL, FakeLiveRoutes, _Log, trace  # noqa: F401  (trace is a fixture)
from test_live_view_tunnel_independent import RelayDownGate, _SessionDown

AGAIN = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution swarm-live.example")
NONAME = socket.gaierror(socket.EAI_NONAME, "Name or service not known swarm-live.example")


# --------------------------------------------------------------------------
# The gate's cause names the class
# --------------------------------------------------------------------------


def test_every_resolver_failure_names_its_class_and_never_just_gaierror():
    assert tunnel.unreachable_cause(AGAIN) == "gaierror(EAI_AGAIN, resolver_busy)"
    assert tunnel.unreachable_cause(NONAME) == "gaierror(EAI_NONAME, no_address)"
    assert tunnel.unreachable_cause(socket.gaierror(socket.EAI_FAIL, "x")) == "gaierror(EAI_FAIL, other)"
    # An errno this module has no name for still says its class.
    assert tunnel.unreachable_cause(socket.gaierror(-4242, "x")) == "gaierror(errno -4242, other)"
    assert tunnel.unreachable_cause(socket.gaierror("no errno at all")) == "gaierror(no errno, other)"
    for exc in (AGAIN, NONAME, socket.gaierror(-4242, "x")):
        cause = tunnel.unreachable_cause(exc)
        assert cause != "gaierror" and "swarm-live" not in cause
    assert tunnel.resolver_class(AGAIN) == tunnel.RESOLVER_BUSY
    assert tunnel.resolver_class(NONAME) == tunnel.NO_ADDRESS
    assert tunnel.resolver_class(ConnectionRefusedError(111, "refused")) is None
    assert tunnel.unreachable_cause(ConnectionRefusedError(111, "refused")) == "ConnectionRefusedError"


@pytest.mark.parametrize("exc, said", [(AGAIN, "EAI_AGAIN, resolver_busy"), (NONAME, "EAI_NONAME, no_address")])
def test_the_gates_tunnel_refused_cause_carries_the_class(monkeypatch, exc, said):
    """The gate records `str(TunnelError)` as its `tunnel_refused` cause."""

    async def open_connection(*args: Any, **kwargs: Any):
        raise exc

    monkeypatch.setattr(tunnel.asyncio, "open_connection", open_connection)
    with pytest.raises(tunnel.TunnelError) as raised:
        asyncio.run(tunnel.WebSocketClient.connect("ws://swarm-live.example:8080/v1/tunnel"))
    assert str(raised.value) == f"relay unreachable: gaierror({said})"
    assert "swarm-live" not in str(raised.value)


# --------------------------------------------------------------------------
# The worker's first open is retried while the relay's name does not resolve
# --------------------------------------------------------------------------


class Clock:
    """The wall clock `sleep` advances, so a test's backoff costs no real time."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Resolver:
    """The pod's resolver: raises each of `failures` in turn, then resolves."""

    def __init__(self, *failures: BaseException) -> None:
        self.failures = list(failures)
        self.asked: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list:
        self.asked.append((host, port))
        if self.failures:
            raise self.failures.pop(0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", port))]


def _path(gate, routes, resolver, clock: Clock, notes: list | None = None, rand=lambda: 0.5):
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
        live=True, gate_client=lambda p: gate,
        environ={"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP},
        wall=clock, sleep=clock.sleep, rand=rand, resolve=resolver,
    )
    path.setup(notes.append if notes is not None else None)
    return path


def _tickets(gate) -> int:
    return sum(1 for op, _ in gate.ops if op == "tunnel_ticket")


def _lines(path, msg: str) -> list[dict[str, Any]]:
    return [fields for line, fields in path.log.lines if line == msg]


RETRYING = "relay name did not resolve; retrying the live tunnel"
GIVING_UP = "relay name still does not resolve; the live view is retried on later polls"


@pytest.mark.parametrize("failure, cls", [(AGAIN, "resolver_busy"), (NONAME, "no_address")])
def test_a_refusal_while_the_relay_name_does_not_resolve_is_retried_until_it_opens(
    db, trace, failure, cls  # noqa: F811
):
    gate = RelayDownGate(trace, failures=2)
    resolver, clock, routes = Resolver(failure, failure), Clock(), FakeLiveRoutes(db, trace)
    path = _path(gate, routes, resolver, clock)

    assert _tickets(gate) == 3
    assert path.view is None and path.offered and path.browser_ready
    assert ("live path open for this attempt", {}) in path.log.lines
    retries = _lines(path, RETRYING)
    assert [r["resolver"] for r in retries] == [cls, cls]
    assert [r["tries"] for r in retries] == [1, 2]
    # Jittered (rand 0.5 = the middle of 75-125 %) and doubling from the first step.
    first = handoff_mod.RELAY_LOOKUP_RETRY_FIRST_SECONDS
    assert clock.slept == [pytest.approx(first), pytest.approx(first * 2)]
    # The relay's own address was looked up, the in-cluster default.
    assert resolver.asked[0] == ("swarm-live.swarm-system.svc.cluster.local", 8080)
    # A fresh ticket for every try; none of them ever logged.
    for line, fields in path.log.lines:
        assert routes.ticket not in line + repr(fields)


def test_the_backoff_is_jittered_so_a_bursts_pods_do_not_retry_in_step(db, trace):  # noqa: F811
    low, high = [], []
    for rand, out in ((lambda: 0.0, low), (lambda: 1.0, high)):
        clock = Clock()
        _path(RelayDownGate(trace, failures=1), FakeLiveRoutes(db, trace), Resolver(AGAIN), clock, rand=rand)
        out.extend(clock.slept)
    first = handoff_mod.RELAY_LOOKUP_RETRY_FIRST_SECONDS
    assert low == [pytest.approx(first * 0.75)] and high == [pytest.approx(first * 1.25)]


def test_a_relay_name_that_never_resolves_gives_the_first_open_up_within_the_bound(db, trace):  # noqa: F811
    gate = RelayDownGate(trace, failures=10_000)
    resolver = Resolver(*([NONAME] * 10_000))
    clock, notes = Clock(), []
    path = _path(gate, FakeLiveRoutes(db, trace), resolver, clock, notes=notes)

    assert sum(clock.slept) <= handoff_mod.RELAY_LOOKUP_RETRY_SECONDS
    assert len(clock.slept) >= 3, clock.slept
    assert max(clock.slept) <= handoff_mod.RELAY_LOOKUP_RETRY_MAX_SECONDS * 1.25
    assert _tickets(gate) == len(clock.slept) + 1
    # Given up as before: the view is unavailable, the agent's browser runs,
    # and the per-poll retry takes over.
    assert path.view == handoff_mod.TUNNEL_REFUSED and path.browser_ready and path.offered
    assert _lines(path, GIVING_UP)[-1]["resolver"] == "no_address"
    assert _lines(path, "live view not open; retrying")
    assert notes and notes[-1]["reason"] == handoff_mod.TUNNEL_REFUSED
    assert "no_address" in notes[-1]["message"]


def test_a_refusal_while_the_relay_name_resolves_is_not_retried(db, trace):  # noqa: F811
    gate = RelayDownGate(trace, failures=1)
    clock, notes = Clock(), []
    path = _path(gate, FakeLiveRoutes(db, trace), Resolver(), clock, notes=notes)

    assert _tickets(gate) == 1 and clock.slept == []
    assert path.view == handoff_mod.TUNNEL_REFUSED
    assert not _lines(path, RETRYING) and not _lines(path, GIVING_UP)
    assert notes[-1]["message"] == ("live view unavailable: tunnel_refused; the agent's browser runs, "
                                    "and the view is retried")


def test_a_swarm_api_failure_is_not_a_lookup_and_is_not_retried(db, trace):  # noqa: F811
    gate = RelayDownGate(trace)
    resolver, clock = Resolver(AGAIN), Clock()
    path = _path(gate, _SessionDown(db, trace), resolver, clock)

    assert resolver.asked == [] and clock.slept == []
    assert path.view == "live_unavailable"
    assert _tickets(gate) == 0


def test_a_lookup_that_does_not_answer_in_time_is_a_busy_resolver(db, trace, monkeypatch):  # noqa: F811
    monkeypatch.setattr(handoff_mod, "RELAY_PROBE_SECONDS", 0.2)
    release = threading.Event()

    def hanging(host: str, port: int) -> list:
        release.wait(30)
        return []

    gate = RelayDownGate(trace, failures=1)
    clock = Clock()
    try:
        path = _path(gate, FakeLiveRoutes(db, trace), hanging, clock)
    finally:
        release.set()
    assert [r["resolver"] for r in _lines(path, RETRYING)] == ["resolver_busy"]
    assert path.view is None and _tickets(gate) == 2


def test_no_unit_test_reaches_the_real_resolver():
    """The suite's conftest stands in for the pod's resolver: a test that
    builds a HandoffPath without `resolve=` never does a real DNS lookup."""
    assert handoff_mod._resolve_relay("swarm-live.swarm-system.svc.cluster.local", 8080)
