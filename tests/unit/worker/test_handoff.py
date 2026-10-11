"""The live-browser hand-off, the worker's half (docs/design/live-browser.md §4; lane LB-L4).

The acceptance test of lane L4's row:

  * request -> gate paused -> signed route called;
  * hold expiry -> checkpoint, wipe, park(HUMAN_REQUIRED), refund within the bound;
  * cancel during hold -> exit code 4, wipe, cancel path;
  * unprotected heap -> exit 5 and no tunnel.

The gate's end of ctl.sock (lane L3, `agent_worker.livegate.ctl`) is a test
double here, `FakeGateClient`, with L3's client surface (`register`, `call`,
`close`) and its error names (`CtlError`, `CtlRefused`): L3 is built in
parallel and may not be on main. swarm-api's worker routes are a double over
the worker test's Firestore that verifies the attempt key's signature with
swarm-api's OWN message builder (`swarm_api.handoff.handoff_message`), so a
drift between the two signers fails here. The agent runs the real
`images/agent-runtime-browser/swarm-handoff`.

Every ticket is built at runtime.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker import lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode
from agent_worker.hardening import FAILED, PROTECTED, MemoryProtection
from swarm_api import childkey as api_childkey
from swarm_api import handoff as api_handoff
from swarm_api import validation as api_validation
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from worker_seeds import seed_attempt

REPO = Path(__file__).resolve().parents[3]
COMMAND = REPO / "images" / "agent-runtime-browser" / "swarm-handoff"
NONCE = "handoff-test-nonce-not-a-secret"
CTL = "/run/swarm-gate/ctl.sock"
CDP = "http://127.0.0.1:9222"

AGENT = r'''
import json, os, subprocess, sys, pathlib
plan = json.load(open(sys.argv[1]))
seen = {"env": dict(os.environ), "steps": []}
command = [sys.executable, plan["command"]]
for step in plan.get("steps", []):
    proc = subprocess.run(command + step, capture_output=True, text=True)
    out = proc.stdout.strip().splitlines()
    seen["steps"].append({"argv": step, "exit": proc.returncode,
                          "out": json.loads(out[-1]) if out else None})
with open(plan["record"], "a") as handle:
    handle.write(json.dumps(seen) + "\n")
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None,
     "metrics": {}, "artifacts": []}))
'''


class CtlError(Exception):
    """L3's name for a failed channel; the worker matches it by name."""


class CtlRefused(CtlError):
    """L3's name for a refused operation."""


class FakeGateClient:
    """L3's `CtlClient` surface, recording every operation in `trace`."""

    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.ops: list[tuple[str, dict[str, Any]]] = []
        self.registered = False
        self.refuse_registration = False
        self.lose_on: str | None = None
        self.on_status: Any = None
        self.resume_result: dict[str, Any] = {}

    def register(self) -> None:
        self.trace.append("gate:register")
        if self.refuse_registration:
            raise CtlRefused("refused")
        self.registered = True

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.registered
        self.ops.append((op, dict(args)))
        self.trace.append(f"gate:{op}" + (":wipe" if args.get("wipe") else ""))
        if self.lose_on == op:
            raise CtlError("channel closed by the gate")
        if op == "status" and self.on_status is not None:
            self.on_status()
        if op == "resume":
            return dict(self.resume_result)
        return {}

    def close(self) -> None:
        self.trace.append("gate:close")

    def names(self) -> list[str]:
        return [op for op, _ in self.ops]


class FakeLiveRoutes:
    """swarm-api's worker routes the worker calls: child-key, live-session, handoff, resolve."""

    def __init__(self, db, trace: list[str], *, hold_seconds: float = 600) -> None:
        self.db = db
        self.trace = trace
        self.hold_seconds = hold_seconds
        self.public_key: str | None = None
        self.calls: list[dict[str, Any]] = []
        # Built at runtime: nothing in this file has a credential's shape.
        self.ticket = "tkt-" + secrets.token_hex(16)

    def _verify(self, purpose: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        proof = headers.get(children_mod.PROOF_HEADER)
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        if purpose == api_handoff.HANDOFF_PURPOSE:
            message = api_handoff.handoff_message("POST", path, body, stamp)
        else:
            message = handoff_mod.signed_message(purpose, "POST", path, body, stamp)
        if not (self.public_key and proof and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(403, "handoff_unproven", "", retryable=False)

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        payload = json.loads(body) if body else {}
        self.calls.append({"path": path, "body": payload})
        self.trace.append(f"route:{path.rsplit('/', 1)[-1]}")
        if path.endswith("/child-key"):
            self.public_key = payload.get("public_key")
            return 201, {"registered": "key" if self.public_key else "tombstone"}
        if path.endswith("/live-session"):
            self._verify(handoff_mod.LIVE_SESSION_PURPOSE, path, body, headers)
            return 201, {"ticket": self.ticket, "session_id": "ls_1"}
        task = self.db.doc("tasks/task_1")
        if path.endswith("/handoff"):
            self._verify(api_handoff.HANDOFF_PURPOSE, path, body, headers)
            assert task["state"] == TaskState.RUNNING.value
            now = utcnow()
            wait = {
                "reason": payload["reason"], "expect": payload.get("expect"),
                "requested_at": now, "deadline": now + timedelta(seconds=self.hold_seconds),
                "controller": None, "request_id": payload["request_id"],
                "attempt_id": payload["attempt_id"], "generation": payload["generation"],
            }
            task["human_wait"] = wait
            return 201, {"human_wait": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                                        for k, v in wait.items()}, "created": True}
        if path.endswith("/handoff/resolve"):
            self._verify(api_handoff.HANDOFF_PURPOSE, path, body, headers)
            self.trace.append(f"resolve:{payload['outcome']}")
            task["human_wait"] = None
            return 200, {"resolved": True}
        raise AssertionError(f"unexpected route {path}")

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    plan_path = tmp_path / "plan.json"
    record = tmp_path / "record.jsonl"
    monkeypatch.setattr(lifecycle, "_runner_argv", lambda cfg: [sys.executable, str(script), str(plan_path)])

    def write_plan(*steps: list[str]) -> None:
        plan_path.write_text(json.dumps({"record": str(record), "command": str(COMMAND), "steps": list(steps)}))

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []

    write_plan()
    return write_plan, records


@pytest.fixture
def trace() -> list[str]:
    return []


@pytest.fixture
def gate(trace) -> FakeGateClient:
    return FakeGateClient(trace)


def _live_worker(worker_factory, routes, gate, trace, monkeypatch, **kwargs):
    worker, cfg, _ = worker_factory(
        child_api=routes, child_nonce=NONCE, swarm_api_url="https://swarm-api.example", **kwargs
    )
    worker.handoff = handoff_mod.HandoffPath(
        cfg,
        logger=worker.log,
        memory=kwargs.get("memory") or MemoryProtection(PROTECTED, "test"),
        children=worker.children,
        live=True,
        gate_client=lambda path: gate if path == CTL else None,
        environ={"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP},
    )
    original_checkpoint = worker._checkpoint

    def checkpoint(label, *args, **kw):
        trace.append(f"checkpoint:{label}")
        return original_checkpoint(label, *args, **kw)

    monkeypatch.setattr(worker, "_checkpoint", checkpoint)
    original_park = handoff_mod.park_human_required

    def park(*args, **kw):
        trace.append("park")
        return original_park(*args, **kw)

    monkeypatch.setattr(lifecycle.handoff_mod, "park_human_required", park)
    return worker


REQUEST = ["request", "--reason", "Sign in to GitHub as the bot account", "--expect", "github.com",
           "--id", "signin-1"]
WAIT = ["wait", "--max-seconds", "25", "--poll", "0.1"]


# --------------------------------------------------------------------------
# request -> gate paused -> signed route called; a person returns control
# --------------------------------------------------------------------------


def test_a_request_pauses_the_gate_then_calls_the_signed_route_and_returns(
    db, worker_factory, agent, gate, trace, monkeypatch, log_stream
):
    write_plan, records = agent
    write_plan(REQUEST, WAIT)
    seed_attempt(db)
    routes = FakeLiveRoutes(db, trace)
    gate.resume_result = {"url": "https://github.com/login?return_to=%2Fsecret", "title": "GitHub"}

    def person_returns() -> None:
        # Lane L9's Return control: swarm-api clears `human_wait`.
        task = db.doc("tasks/task_1")
        if task.get("human_wait"):
            task["human_wait"] = None

    gate.on_status = person_returns
    worker = _live_worker(worker_factory, routes, gate, trace, monkeypatch)
    assert worker.run() == ExitCode.OK

    # The slot first, then the tunnel, before the agent existed.
    assert trace.index("gate:register") < trace.index("route:live-session") < trace.index("gate:tunnel_ticket")
    assert gate.ops[0] == ("tunnel_ticket", {"ticket": routes.ticket, "session_id": "ls_1"})
    # §4.1: paused BEFORE the wait is recorded.
    assert trace.index("gate:pause") < trace.index("route:handoff")
    (sent,) = [c["body"] for c in routes.calls if c["path"].endswith("/handoff")]
    assert sent == {
        "tenant_id": "eng", "attempt_id": "att_1", "lease_id": "lease_1", "generation": 1,
        "request_id": "signin-1", "reason": "Sign in to GitHub as the bot account",
        "expect": "github.com",
    }
    assert ("pause", {"request_id": "signin-1", "expect": "github.com"}) in gate.ops
    assert "resume" in gate.names()

    (seen,) = records()
    request, wait = seen["steps"]
    assert request["exit"] == 0 and request["out"]["request_id"] == "signin-1"
    # §5.9: origin and path, no query; never who or what was typed.
    assert wait["exit"] == 0, wait
    assert wait["out"]["outcome"] == "returned"
    assert wait["out"]["url"] == "https://github.com/login"
    assert wait["out"]["title"] == "GitHub"
    assert isinstance(wait["out"]["held_seconds"], int)

    # The agent got the spool and the CDP address, never the ticket or a display.
    env = seen["env"]
    assert env[handoff_mod.HANDOFF_ENV].endswith(handoff_mod.SPOOL_DIR_NAME)
    assert env["SWARM_CDP_ENDPOINT"] == CDP
    assert "DISPLAY" not in env and "SWARM_GATE_CTL" not in env
    assert routes.ticket not in "\n".join(env.values())
    assert routes.ticket not in log_stream.getvalue()
    # §5.5: wiped at the attempt's end, the ordinary success included.
    assert trace[-2:] == ["gate:end_control:wipe", "gate:close"]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


# --------------------------------------------------------------------------
# hold expiry -> checkpoint, wipe, park(HUMAN_REQUIRED), refund within the bound
# --------------------------------------------------------------------------


def _expire(db, worker_factory, agent, gate, trace, monkeypatch, *, resumes_used: int | None = None):
    write_plan, records = agent
    write_plan(REQUEST, WAIT)
    seed_attempt(db)
    if resumes_used is not None:
        db.doc("tasks/task_1").setdefault("metadata", {})[
            handoff_mod.HUMAN_WAIT_RESUMES_METADATA_KEY
        ] = resumes_used
    routes = FakeLiveRoutes(db, trace, hold_seconds=1)
    worker = _live_worker(worker_factory, routes, gate, trace, monkeypatch)
    assert worker.run() == ExitCode.PARKED
    return records, routes


def test_hold_expiry_wipes_checkpoints_parks_human_required_and_refunds(
    db, worker_factory, agent, gate, trace, monkeypatch
):
    records, routes = _expire(db, worker_factory, agent, gate, trace, monkeypatch)
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert task["current_lease_id"] is None and task["human_wait"] is None
    # Invariants 1 and 4: the slot is given back; nothing waits.
    assert db.doc("leases/lease_1")["released_at"] is not None
    # Invariant 8: a checkpoint, written before the park.
    assert task["latest_checkpoint"]
    # §4.3: the park does not spend an attempt, up to the bound.
    assert task["attempt_count"] == 0
    assert task["metadata"][handoff_mod.HUMAN_WAIT_RESUMES_METADATA_KEY] == 1
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked[-1]["detail"]["attempt_refunded"] is True
    assert parked[-1]["detail"]["cause"] == handoff_mod.HOLD_EXPIRED

    # The order: resolved at swarm-api while RUNNING, wiped, checkpointed, parked.
    assert "resolve:expired" in trace
    order = [trace.index(x) for x in
             ("resolve:expired", "gate:end_control:wipe", "checkpoint:human-wait", "park")]
    assert order == sorted(order), trace
    assert trace.count("gate:end_control:wipe") == 1, "wiped once per attempt"

    (seen,) = records()
    wait = seen["steps"][1]
    assert wait["exit"] == handoff_mod.EXIT_EXPIRED == 3, wait
    assert wait["out"]["outcome"] == "expired"


def test_past_the_bound_a_human_park_counts_as_an_attempt(
    db, worker_factory, agent, gate, trace, monkeypatch
):
    _expire(db, worker_factory, agent, gate, trace, monkeypatch,
            resumes_used=handoff_mod.MAX_HUMAN_RESUMES)
    task = db.doc("tasks/task_1")
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert task["attempt_count"] == 1
    assert task["metadata"][handoff_mod.HUMAN_WAIT_RESUMES_METADATA_KEY] == handoff_mod.MAX_HUMAN_RESUMES


# --------------------------------------------------------------------------
# cancel during hold -> exit code 4, wipe, cancel path
# --------------------------------------------------------------------------


def test_a_cancel_during_the_hold_answers_4_wipes_and_cancels(
    db, worker_factory, agent, gate, trace, monkeypatch
):
    write_plan, records = agent
    write_plan(REQUEST, WAIT)
    seed_attempt(db)
    routes = FakeLiveRoutes(db, trace)

    def cancel_arrives() -> None:
        task = db.doc("tasks/task_1")
        if task.get("human_wait"):
            task["cancel_requested"] = True

    gate.on_status = cancel_arrives
    worker = _live_worker(worker_factory, routes, gate, trace, monkeypatch)
    assert worker.run() == ExitCode.CANCELLED
    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value
    assert "resolve:cancelled" in trace
    assert ("end_control", {"cause": "cancelled", "wipe": True}) in gate.ops
    assert trace.index("gate:end_control:wipe") < trace.index("checkpoint:cancellation")
    assert "park" not in trace
    (seen,) = records()
    wait = seen["steps"][1]
    assert wait["exit"] == handoff_mod.EXIT_CANCELLED == 4, wait


# --------------------------------------------------------------------------
# unprotected heap -> exit 5 and no tunnel
# --------------------------------------------------------------------------


def test_an_unprotected_heap_gets_no_tunnel_and_the_agent_is_refused_with_5(
    db, worker_factory, agent, gate, trace, monkeypatch
):
    write_plan, records = agent
    write_plan(REQUEST)
    seed_attempt(db)
    routes = FakeLiveRoutes(db, trace)
    worker = _live_worker(
        worker_factory, routes, gate, trace, monkeypatch,
        memory=MemoryProtection(FAILED, "prctl refused in this test"),
    )
    assert worker.run() == ExitCode.OK
    # The slot is still taken, so the agent cannot take it; nothing else is sent.
    assert "gate:register" in trace
    assert "tunnel_ticket" not in gate.names() and "pause" not in gate.names()
    assert not any(p.endswith("/live-session") or p.endswith("/handoff") for p in routes.paths())
    assert worker.handoff.unavailable == handoff_mod.WORKER_UNPROTECTED
    (seen,) = records()
    (request,) = seen["steps"]
    assert request["exit"] == handoff_mod.EXIT_REFUSED == 5
    assert request["out"]["code"] == handoff_mod.WORKER_UNPROTECTED
    # No CDP address for an attempt without a live path.
    assert "SWARM_CDP_ENDPOINT" not in seen["env"]


# --------------------------------------------------------------------------
# The worker's checks, on HandoffPath directly
# --------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, msg, **kw):
        self.lines.append((msg, kw))

    warning = error = info


def _bare(tmp_path, db, trace, gate, *, hold: float = 600):
    routes = FakeLiveRoutes(db, trace, hold_seconds=hold)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    children = SimpleNamespace(_key=key, _api=routes, unavailable=None)
    cfg = SimpleNamespace(
        tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
        generation=1, runner_profile="mock",
    )
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"), children=children,
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL},
    )
    path.setup()
    assert path.offered
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    return path, routes, work


def _spool_request(work: Path, request_id: str, document: dict[str, Any]) -> None:
    (work / handoff_mod.SPOOL_DIR_NAME / "requests" / f"{request_id}.json").write_text(json.dumps(document))


def _answer(work: Path, request_id: str) -> dict[str, Any]:
    return json.loads((work / handoff_mod.SPOOL_DIR_NAME / "responses" / f"{request_id}.json").read_text())


@pytest.mark.parametrize(
    "document,code",
    [
        ({"reason": "x" * (handoff_mod.MAX_REASON_CHARS + 1)}, "reason_invalid"),
        ({"reason": "   "}, "reason_invalid"),
        ({"reason": "sign in", "expect": "https://github.com/login"}, "expect_invalid"),
        ({"reason": "sign in", "expect": "localhost"}, "expect_invalid"),
        (["not", "an", "object"], "request_invalid"),
    ],
)
def test_an_invalid_request_is_refused_before_the_gate_is_paused(tmp_path, db, trace, gate, document, code):
    path, routes, work = _bare(tmp_path, db, trace, gate)
    _spool_request(work, "bad", document)
    assert path.tick(work, lambda: db.doc("tasks/task_1")) is None
    answer = _answer(work, "bad")
    assert answer["outcome"] == "refused" and answer["exit"] == 5 and answer["code"] == code
    assert "pause" not in gate.names()
    assert not any(p.endswith("/handoff") for p in routes.paths())


def test_the_reason_is_redacted_before_it_leaves_the_worker(tmp_path, db, trace, gate):
    path, routes, work = _bare(tmp_path, db, trace, gate)
    leaked = "ghp_" + secrets.token_hex(18)
    _spool_request(work, "r1", {"reason": f"use token {leaked} to sign in"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    (sent,) = [c["body"] for c in routes.calls if c["path"].endswith("/handoff")]
    assert leaked not in json.dumps(sent)


def test_a_fourth_request_in_one_attempt_is_refused(tmp_path, db, trace, gate):
    path, routes, work = _bare(tmp_path, db, trace, gate)
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    for n in range(handoff_mod.MAX_HANDOFFS_PER_ATTEMPT):
        _spool_request(work, f"r{n}", {"reason": "sign in"})
        path.tick(work, fetch)
        assert path.active is not None
        db.doc("tasks/task_1")["human_wait"] = None  # a person returned it
        path.tick(work, fetch)
        assert _answer(work, f"r{n}")["outcome"] == "returned"
    _spool_request(work, "r9", {"reason": "sign in"})
    path.tick(work, fetch)
    assert _answer(work, "r9")["code"] == "handoff_limit"
    assert gate.names().count("pause") == handoff_mod.MAX_HANDOFFS_PER_ATTEMPT


def test_a_lost_gate_during_the_hold_ends_it_as_an_expiry(tmp_path, db, trace, gate):
    """§5.4.2: a restarted gate ends the live path; the hand-off parks."""
    path, routes, work = _bare(tmp_path, db, trace, gate)
    _spool_request(work, "r1", {"reason": "sign in"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    gate.lose_on = "status"
    assert path.tick(work, lambda: db.doc("tasks/task_1")) == handoff_mod.GATE_LOST
    assert path.unavailable == handoff_mod.GATE_UNAVAILABLE


def test_a_person_in_control_is_not_parked_under_their_fingers(tmp_path, db, trace, gate):
    """§4.3: past the deadline with a controller, the worker leaves swarm-api
    the renewal window to end idle control before it parks."""
    path, routes, work = _bare(tmp_path, db, trace, gate, hold=-1)
    _spool_request(work, "r1", {"reason": "sign in"})
    fetch = lambda: db.doc("tasks/task_1")  # noqa: E731
    path.tick(work, fetch)
    db.doc("tasks/task_1")["human_wait"]["controller"] = {"email": "alice@saga.xyz"}
    assert path.tick(work, fetch) is None
    db.doc("tasks/task_1")["human_wait"]["controller"] = None
    assert path.tick(work, fetch) == handoff_mod.HOLD_EXPIRED


def test_a_device_is_switched_by_name_only(tmp_path, db, trace, gate):
    path, routes, work = _bare(tmp_path, db, trace, gate)
    devices = work / handoff_mod.SPOOL_DIR_NAME / "devices"
    (devices / "d1.json").write_text(json.dumps({"preset": "iphone-15"}))
    (devices / "d2.json").write_text(json.dumps({"preset": "393x852"}))
    (devices / "d3.json").write_text(json.dumps({"width": 393}))
    path.tick(work, lambda: db.doc("tasks/task_1"))
    assert _answer(work, "d1")["outcome"] == "applied"
    assert _answer(work, "d3")["code"] == "preset_invalid"
    assert ("device", {"preset": "iphone-15"}) in gate.ops
    assert all(args.get("preset") in ("iphone-15", "393x852") for op, args in gate.ops if op == "device")


def test_a_refused_registration_leaves_no_live_path(tmp_path, db, trace, gate):
    gate.refuse_registration = True
    children = SimpleNamespace(_key=children_mod.AttemptKey(), _api=FakeLiveRoutes(db, trace), unavailable=None)
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    path = handoff_mod.HandoffPath(cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "t"),
                                   children=children, live=True, gate_client=lambda p: gate,
                                   environ={"SWARM_GATE_CTL": CTL})
    path.setup()
    assert path.unavailable == handoff_mod.GATE_UNAVAILABLE and not path.offered
    assert children._api.calls == []


def test_a_profile_that_is_not_live_gets_no_spool(tmp_path, db, trace, gate):
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    path = handoff_mod.HandoffPath(cfg, logger=_Log(), memory=None, children=None,
                                   gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL})
    path.setup()
    assert path.live is False and path.prepare(tmp_path) == {}
    assert trace == []


# --------------------------------------------------------------------------
# Restatements held to swarm-api's, the gate's and the command's
# --------------------------------------------------------------------------


def _command_module():
    loader = importlib.machinery.SourceFileLoader("swarm_handoff_cmd", str(COMMAND))
    spec = importlib.util.spec_from_loader("swarm_handoff_cmd", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_workers_restatements_match_swarm_api_and_the_command():
    assert handoff_mod.HANDOFF_PURPOSE == api_handoff.HANDOFF_PURPOSE
    assert handoff_mod.REQUEST_ID.pattern == api_handoff.REQUEST_ID.pattern
    assert handoff_mod._HOST.pattern == api_handoff._HOST.pattern
    assert handoff_mod.MAX_REASON_CHARS == api_handoff.MAX_REASON_CHARS
    assert handoff_mod.MAX_HANDOFFS_PER_ATTEMPT == api_handoff.MAX_HANDOFFS_PER_ATTEMPT
    assert handoff_mod.HUMAN_WAIT_RESUMES_METADATA_KEY == api_validation.HUMAN_WAIT_RESUMES_METADATA_KEY
    for args in [("POST", "/v1/tasks/t/handoff", b'{"a":1}', "17")]:
        assert handoff_mod.signed_message(handoff_mod.HANDOFF_PURPOSE, *args) == api_handoff.handoff_message(*args)
    assert handoff_mod.LIVE_SESSION_PURPOSE != handoff_mod.HANDOFF_PURPOSE
    assert {"expired", "cancelled", "unavailable"} <= set(api_handoff.OUTCOMES)
    cmd = _command_module()
    for name in ("EXIT_RETURNED", "EXIT_EXPIRED", "EXIT_CANCELLED", "EXIT_REFUSED", "EXIT_STILL_WAITING",
                 "MAX_REASON_CHARS"):
        assert getattr(cmd, name) == getattr(handoff_mod, name), name
    assert cmd.WAIT_SECONDS < 10 * 60, "claude-code's Bash tool kills a command at 10 minutes (#736)"
    ws = workspace_mod.Workspace(*(Path("/w") / n for n in ("r", "work", "a", "l", "t", "re", "p")))
    assert ws.handoff_dir.name == handoff_mod.SPOOL_DIR_NAME
    assert handoff_mod.SPOOL_DIR_NAME in ws.control_file_names()


def test_every_gate_operation_the_worker_sends_is_one_the_gate_accepts():
    ctl = pytest.importorskip("agent_worker.livegate.ctl")
    assert handoff_mod.GATE_OPS <= ctl.OPS


def test_the_agent_never_gets_a_display(tmp_path):
    ws = workspace_mod.Workspace(*(tmp_path / n for n in ("r", "work", "a", "l", "t", "re", "p")))
    assert "DISPLAY" not in ws.child_env({"DISPLAY": ":0", "X": "1"})


def test_wait_says_still_waiting_with_75(tmp_path, monkeypatch):
    cmd = _command_module()
    root = tmp_path / "spool"
    (root / "responses").mkdir(parents=True)
    (root / "responses" / "r1.json").write_text(json.dumps({"outcome": "waiting"}))
    monkeypatch.setenv("SWARM_HANDOFF", str(root))
    assert cmd.main(["wait", "r1", "--max-seconds", "0.2", "--poll", "0.05"]) == 75
    monkeypatch.delenv("SWARM_HANDOFF")
    assert cmd.main(["wait", "r1"]) == 5
