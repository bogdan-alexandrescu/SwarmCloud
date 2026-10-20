"""The attempt egress ticket, the worker's half (EGRESS-GATE; part of #1030).

docs/browser-egress.md §2.5 and the owner's decisions of 2026-10-13 (FAIL
CLOSED; the credential only in swarm-egress). The worker holds the attempt
key, so it asks swarm-api for the egress ticket, hands it to the gate on
ctl.sock and renews it. These tests hold:

* a proxied browser's ticket is asked for with a signature swarm-api's own
  verifier accepts, under the egress route's purpose, and handed to the gate;
* a direct browser is never asked about egress;
* when no ticket can be had (swarm-api refuses, no attempt key, an unprotected
  heap) the gate is told `failed` -- never sent direct -- and the agent's
  prompt and the attempt's events say why;
* the ticket is renewed before it expires; a transient blip changes nothing
  while the ticket in hand still works;
* no ticket appears in a log line, the spool or the environment.

Offline: swarm-api is test_handoff's FakeLiveRoutes with the egress route
added, the gate a recording fake. Every ticket is built at run time.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import egress_ticket
from agent_worker import handoff as handoff_mod
from agent_worker.hardening import FAILED, PROTECTED, MemoryProtection
from agent_worker.runners import claude_code
from swarm_api import childkey as api_childkey
from swarm_api import egress as api_egress

from test_handoff import CDP, CTL, FakeGateClient, FakeLiveRoutes, _Log


class Clock:
    def __init__(self) -> None:
        self.at = 1_800_000_000.0

    def __call__(self) -> float:
        return self.at


class EgressRoutes(FakeLiveRoutes):
    """swarm-api with the egress-ticket route: verifies the attempt proof as
    swarm_api.egress does, then mints, or refuses with `refuse`."""

    def __init__(self, db, trace, *, clock: Clock) -> None:
        super().__init__(db, trace)
        self.clock = clock
        self.refuse: children_mod.ChildApiError | None = None
        self.minted: list[str] = []
        self.key = secrets.token_hex(32)

    def call(self, method, path, *, body=None, headers=None):
        if not path.endswith("/egress-ticket"):
            return super().call(method, path, body=body, headers=headers)
        headers = dict(headers or {})
        self.calls.append({"path": path, "body": json.loads(body)})
        self.trace.append("route:egress-ticket")
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        proof = headers.get(children_mod.PROOF_HEADER)
        message = api_egress.egress_ticket_message("POST", path, body, stamp)
        if not (proof and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(403, "forbidden", "unproven", retryable=False)
        if self.refuse is not None:
            raise self.refuse
        payload = json.loads(body)
        ticket = api_egress.mint_egress_ticket(
            self.key, tenant_id=payload["tenant_id"], task_id=payload["task_id"],
            attempt_id=payload["attempt_id"], generation=payload["generation"],
            exp=int(self.clock()) + 600)
        self.minted.append(ticket)
        return 201, {"route": "proxy", "ticket": ticket, "expires_in": 600, "country": "us"}


class EgressGate(FakeGateClient):
    """The gate, whose `status` says its browser's route."""

    def __init__(self, trace, *, route: str = "proxy", answer: dict[str, Any] | None = None) -> None:
        super().__init__(trace)
        self.route = route
        self.answer = answer or {"egress": "ready"}

    def call(self, op, args):
        super().call(op, args)
        if op == "status":
            return {"egress": {"route": self.route}}
        if op == "egress_ticket":
            return {"egress": "failed", "cause": args["failed"]} if "failed" in args else dict(self.answer)
        return {}

    def egress_ops(self) -> list[dict[str, Any]]:
        return [a for op, a in self.ops if op == "egress_ticket"]


def _path(tmp_path: Path, db, trace, gate, *, clock: Clock, memory=None, key: bool = True,
          refuse: children_mod.ChildApiError | None = None):
    routes = EgressRoutes(db, trace, clock=clock)
    routes.refuse = refuse
    attempt_key = children_mod.AttemptKey()
    routes.public_key = attempt_key.public
    children = SimpleNamespace(_key=attempt_key if key else None, _api=routes if key else None,
                               unavailable=None if key else "child_key_window_closed")
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=3, runner_profile="mock")
    log = _Log()
    notes: list[dict[str, Any]] = []
    path = handoff_mod.HandoffPath(
        cfg, logger=log, memory=memory or MemoryProtection(PROTECTED, "test"), children=children,
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP},
        wall=clock,
    )
    path.setup(notes.append)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    return SimpleNamespace(path=path, routes=routes, work=work, env=env, notes=notes, log=log)


def _spool(world) -> Path:
    return world.work / handoff_mod.SPOOL_DIR_NAME


def test_the_workers_purpose_is_swarm_apis():
    assert egress_ticket.EGRESS_TICKET_PURPOSE == api_egress.EGRESS_TICKET_PURPOSE
    assert egress_ticket.EGRESS_TICKET_PURPOSE != handoff_mod.LIVE_SESSION_PURPOSE
    assert "egress_ticket" in handoff_mod.GATE_OPS


def test_a_proxied_browser_gets_a_ticket_swarm_api_signed_before_the_agent_exists(tmp_path, db):
    trace: list[str] = []
    clock = Clock()
    gate = EgressGate(trace)
    world = _path(tmp_path, db, trace, gate, clock=clock)
    (handed,) = gate.egress_ops()
    (minted,) = world.routes.minted
    assert handed == {"ticket": minted, "expires_in": 600}
    claims = api_egress.verify_egress_ticket([world.routes.key], minted, now=clock())
    assert (claims["tenant_id"], claims["task_id"], claims["attempt_id"], claims["generation"]) == \
        ("eng", "task_1", "att_1", 3)
    (call,) = [c for c in world.routes.calls if c["path"].endswith("/egress-ticket")]
    assert call["path"] == "/v1/attempts/att_1/egress-ticket"
    assert world.path.egress.state == "ready"
    # The order the design needs: the gate's slot first, then the ticket.
    assert trace.index("gate:register") < trace.index("route:egress-ticket") < trace.index("gate:egress_ticket")
    # Nothing about egress for the agent while it works, and no ticket anywhere it can read.
    assert not (_spool(world) / handoff_mod.EGRESS_NOTICE_NAME).exists()
    assert minted not in json.dumps(world.env)
    for file in _spool(world).rglob("*"):
        if file.is_file():
            assert minted not in file.read_text()
    assert minted not in json.dumps(world.log.lines, default=str)
    assert world.notes == []


def test_a_direct_browser_is_never_asked_about_egress(tmp_path, db):
    trace: list[str] = []
    gate = EgressGate(trace, route="direct")
    world = _path(tmp_path, db, trace, gate, clock=Clock())
    assert gate.egress_ops() == []
    assert not any(c["path"].endswith("/egress-ticket") for c in world.routes.calls)
    assert world.path.egress.notice() is None


@pytest.mark.parametrize("code,cause", [
    ("conflict", "mint_refused"), ("egress_off", "egress_off"), ("egress_fenced", "egress_fenced"),
    ("egress_unavailable", "egress_unavailable"),
])
def test_a_refused_mint_fails_closed_and_the_agent_and_events_are_told(tmp_path, db, code, cause):
    trace: list[str] = []
    gate = EgressGate(trace)
    clock = Clock()
    refusal = children_mod.ChildApiError(409, code, "browser egress is off", retryable=False)
    world = _path(tmp_path, db, trace, gate, clock=clock, refuse=refusal)
    assert gate.egress_ops() == [{"failed": cause}], "the gate is told; it is never sent direct"
    notice = (_spool(world) / handoff_mod.EGRESS_NOTICE_NAME).read_text()
    assert cause in notice and "not fall back to a direct connection" in notice
    assert claude_code.egress_notice(world.env[handoff_mod.HANDOFF_ENV]) == notice.strip()
    (note,) = world.notes
    assert note["phase"] == "browser_egress" and cause in note["reason"]
    # A permanent refusal: nothing more is asked this attempt.
    clock.at += 10_000
    world.path.tick(world.work, lambda: {})
    assert len([c for c in world.routes.calls if c["path"].endswith("/egress-ticket")]) == 1


def test_no_attempt_key_or_an_unprotected_heap_fails_closed(tmp_path, db):
    for memory, key, cause in ((None, False, "no_attempt_key"),
                               (MemoryProtection(FAILED, "test"), True, "worker_unprotected")):
        trace: list[str] = []
        gate = EgressGate(trace)
        (tmp_path / cause).mkdir()
        world = _path(tmp_path / cause, db, trace, gate, clock=Clock(), memory=memory, key=key)
        assert gate.egress_ops() == [{"failed": cause}]
        assert not any(c["path"].endswith("/egress-ticket") for c in world.routes.calls)
        assert world.path.egress.notice() is not None


def test_the_ticket_is_renewed_before_it_expires(tmp_path, db):
    trace: list[str] = []
    clock = Clock()
    gate = EgressGate(trace)
    world = _path(tmp_path, db, trace, gate, clock=clock)
    world.path.tick(world.work, lambda: {})
    assert len(gate.egress_ops()) == 1, "not due yet"
    clock.at += 600 * (1 - egress_ticket.RENEW_FRACTION) + 1
    world.path.tick(world.work, lambda: {})
    assert len(gate.egress_ops()) == 2
    assert gate.egress_ops()[-1]["ticket"] == world.routes.minted[-1] != world.routes.minted[0]
    assert world.path.egress.renewals == 1


def test_a_transient_blip_while_the_ticket_works_changes_nothing(tmp_path, db):
    trace: list[str] = []
    clock = Clock()
    gate = EgressGate(trace)
    world = _path(tmp_path, db, trace, gate, clock=clock)
    world.routes.refuse = children_mod.ChildApiError(None, "api_unavailable", "URLError", retryable=True)
    clock.at += 450
    world.path.tick(world.work, lambda: {})
    assert len(gate.egress_ops()) == 1, "the gate keeps the ticket it has"
    assert world.path.egress.state == "ready"
    world.routes.refuse = None
    clock.at += egress_ticket.RETRY_SECONDS + 1
    world.path.tick(world.work, lambda: {})
    assert gate.egress_ops()[-1].get("ticket") == world.routes.minted[-1]


def test_a_ticket_that_expires_unrenewed_fails_closed_and_recovers_on_the_next_mint(tmp_path, db):
    trace: list[str] = []
    clock = Clock()
    gate = EgressGate(trace)
    world = _path(tmp_path, db, trace, gate, clock=clock)
    world.routes.refuse = children_mod.ChildApiError(503, "http_503", "", retryable=True)
    clock.at += 601
    world.path.tick(world.work, lambda: {})
    assert gate.egress_ops()[-1] == {"failed": "mint_failed"}
    assert (_spool(world) / handoff_mod.EGRESS_NOTICE_NAME).exists()
    world.routes.refuse = None
    clock.at += egress_ticket.RETRY_MAX_SECONDS + 1
    world.path.tick(world.work, lambda: {})
    assert "ticket" in gate.egress_ops()[-1]
    assert world.path.egress.state == "ready"
    assert not (_spool(world) / handoff_mod.EGRESS_NOTICE_NAME).exists()


def test_a_gate_that_cannot_reach_swarm_egress_is_reported_as_failed(tmp_path, db):
    trace: list[str] = []
    gate = EgressGate(trace, answer={"egress": "failed", "cause": "forwarder_unreachable"})
    world = _path(tmp_path, db, trace, gate, clock=Clock())
    assert world.path.egress.state == "failed"
    assert "forwarder_unreachable" in (_spool(world) / handoff_mod.EGRESS_NOTICE_NAME).read_text()
