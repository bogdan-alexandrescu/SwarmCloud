"""The QA identity, the worker's half (QI-WORKER; docs/design/qa-identity.md §4.5, §6; part of #1649).

The row's red-first tests:

* no `qa_identity` input means no call is made;
* a first refusal `qa_identity_not_granted` fails the attempt before the
  runner starts (as does `qa_identity_unconfigured`);
* renewal happens at two-thirds of the life;
* a refusal at renewal sends `failed` to the gate and writes `qa-identity.md`;
* no log record contains the credential (captured-log scan).

And what holds them together: the request is signed under swarm-api's own
purpose and message builder and parses as swarm-api's own request model, and
what the worker hands the gate is accepted by the gate's own verb handler
(`livegate.qaidentity.QaIdentity.handle`), so a drift on either side fails
here.

Offline: swarm-api is test_handoff's FakeLiveRoutes with the QA route added,
the gate a recording fake around the real verb handler. Every credential is
built at run time from parts; nothing in this file has a credential's shape.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker import qa_identity
from agent_worker.hardening import FAILED, PROTECTED, MemoryProtection
from agent_worker.livegate import ctl as gate_ctl
from agent_worker.livegate import qaidentity as gate_qa
from swarm_api import childkey as api_childkey
from swarm_api import qa_identity as api_qa

from test_handoff import CDP, CTL, CtlRefused, FakeGateClient, FakeLiveRoutes, _Log

HOST = "console.example.com"
LIFE = 900


def _credential() -> str:
    # Three dot-joined hex segments: what the gate's pattern accepts, built here.
    return ".".join(secrets.token_hex(n) for n in (12, 40, 32))


class Clock:
    def __init__(self) -> None:
        self.at = 1_800_000_000.0

    def __call__(self) -> float:
        return self.at


class QaRoutes(FakeLiveRoutes):
    """swarm-api with the QA identity's route: verifies the attempt proof with
    swarm_api.qa_identity's own message, parses the body with its own model,
    then answers a fresh credential or raises `refuse`."""

    def __init__(self, db, trace, *, clock: Clock) -> None:
        super().__init__(db, trace)
        self.clock = clock
        self.refuse: children_mod.ChildApiError | None = None
        self.issued: list[str] = []

    def call(self, method, path, *, body=None, headers=None):
        if not path.endswith("/qa-identity"):
            return super().call(method, path, body=body, headers=headers)
        headers = dict(headers or {})
        self.calls.append({"path": path, "body": json.loads(body)})
        self.trace.append("route:qa-identity")
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        proof = headers.get(children_mod.PROOF_HEADER)
        message = api_qa.qa_identity_message("POST", path, body, stamp)
        if not (proof and api_childkey.verify_proof(self.public_key, proof, message)):
            raise children_mod.ChildApiError(401, "bad_signature", "unproven", retryable=False)
        parsed = api_qa.QaIdentityRequest.model_validate_json(body)
        assert path == f"/v1/attempts/{parsed.attempt_id}/qa-identity"
        if self.refuse is not None:
            raise self.refuse
        credential = _credential()
        self.issued.append(credential)
        return 200, {"identity": parsed.identity, "host": HOST, "header": api_qa.CREDENTIAL_HEADER,
                     "scheme": api_qa.CREDENTIAL_SCHEME, "expires_in": LIFE,
                     "jti": secrets.token_hex(16), "credential": credential}

    def qa_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].endswith("/qa-identity")]


class QaGate(FakeGateClient):
    """The gate, whose `qa_identity` verb is the real handler's."""

    def __init__(self, trace, clock: Clock) -> None:
        super().__init__(trace)
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.qa = gate_qa.QaIdentity(lambda kind, fields: self.records.append((kind, dict(fields))),
                                     clock=clock)

    def call(self, op, args):
        super().call(op, args)
        if op == gate_qa.OP:
            try:
                return self.qa.handle(args)
            except gate_qa.QaIdentityRefused as exc:
                raise CtlRefused(exc.code) from None
        return {}

    def qa_ops(self) -> list[dict[str, Any]]:
        return [a for op, a in self.ops if op == gate_qa.OP]


class SecretLog(_Log):
    """The worker's logger surface, with the redaction registry it offers."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets: list[str] = []

    def register_secret(self, value):
        self.secrets.append(value)


def _task(**input_: Any) -> dict[str, Any]:
    return {"id": "task_1", "tenant_id": "eng", "state": "RUNNING", "input": dict(input_)}


def _world(tmp_path: Path, db, *, task: dict[str, Any], memory=None, key: bool = True,
           refuse: children_mod.ChildApiError | None = None, clock: Clock | None = None):
    trace: list[str] = []
    clock = clock or Clock()
    gate = QaGate(trace, clock)
    routes = QaRoutes(db, trace, clock=clock)
    routes.refuse = refuse
    attempt_key = children_mod.AttemptKey()
    routes.public_key = attempt_key.public
    children = SimpleNamespace(_key=attempt_key if key else None, _api=routes if key else None,
                               unavailable=None if key else "child_key_window_closed")
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=3, runner_profile="claude-code-browser")
    log = SecretLog()
    notes: list[dict[str, Any]] = []
    path = handoff_mod.HandoffPath(
        cfg, logger=log, memory=memory or MemoryProtection(PROTECTED, "test"), children=children,
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL, "SWARM_CDP_ENDPOINT": CDP},
        wall=clock,
    )
    path.setup(notes.append)
    db.seed("tasks/task_1", task)
    fails = path.start_qa_identity(task)
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    env = path.prepare(work)
    return SimpleNamespace(path=path, routes=routes, gate=gate, work=work, env=env, notes=notes,
                           log=log, clock=clock, trace=trace, fails=fails)


def _notice(world) -> Path:
    return world.work / handoff_mod.SPOOL_DIR_NAME / handoff_mod.QA_IDENTITY_NOTICE_NAME


def _everything_readable(world) -> str:
    """The agent's environment, every spool file and every log line, as one text."""
    parts = [json.dumps(world.env), json.dumps(world.log.lines, default=str),
             json.dumps(world.notes, default=str)]
    for file in (world.work / handoff_mod.SPOOL_DIR_NAME).rglob("*"):
        if file.is_file():
            parts.append(file.read_text())
    return "\n".join(parts)


# --------------------------------------------------------------------------
# The two halves agree
# --------------------------------------------------------------------------


def test_the_worker_speaks_swarm_apis_purpose_and_the_gates_verb():
    assert qa_identity.QA_IDENTITY_PURPOSE == api_qa.QA_IDENTITY_PURPOSE
    assert qa_identity.QA_IDENTITY_PURPOSE != handoff_mod.LIVE_SESSION_PURPOSE
    assert qa_identity.INPUT_KEY == api_qa.QA_IDENTITY_INPUT_KEY
    assert qa_identity.IDENTITIES == api_qa.IDENTITIES
    assert qa_identity.CTL_OP == gate_qa.OP
    assert qa_identity.CTL_OP in handoff_mod.GATE_OPS
    assert qa_identity.CTL_OP in gate_ctl.OPS


def test_a_task_that_names_the_identity_is_signed_in_before_the_agent_exists(tmp_path, db):
    world = _world(tmp_path, db, task=_task(qa_identity="console"))
    assert world.fails is None
    (call,) = world.routes.qa_calls()
    assert call["path"] == "/v1/attempts/att_1/qa-identity"
    assert call["body"] == {"tenant_id": "eng", "task_id": "task_1", "attempt_id": "att_1",
                            "lease_id": "lease_1", "generation": 3, "identity": "console"}
    (handed,) = world.gate.qa_ops()
    (issued,) = world.routes.issued
    assert handed == {"host": HOST, "header": "Authorization", "scheme": "Bearer",
                      "credential": issued, "expires_in": LIFE}
    assert world.gate.qa.held and world.gate.qa.host == HOST
    assert world.path.qa.state == "ready"
    # The gate's slot first, then swarm-api, then the gate gets the credential.
    assert world.trace.index("gate:register") < world.trace.index("route:qa-identity") \
        < world.trace.index("gate:qa_identity")
    assert not _notice(world).exists()
    assert issued not in _everything_readable(world)
    assert world.log.secrets == [issued], "the credential is registered with the log redaction"


# --------------------------------------------------------------------------
# The row's red-first tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize("task", [
    _task(), _task(browser_tool="auto"), _task(qa_identity="other"), {"id": "task_1"}, None,
])
def test_no_qa_identity_input_means_no_call_is_made(tmp_path, db, task):
    world = _world(tmp_path, db, task=task if task is not None else _task())
    if task is None:
        assert world.path.start_qa_identity(None) is None
    assert world.fails is None
    assert world.routes.qa_calls() == []
    assert world.gate.qa_ops() == []
    assert not _notice(world).exists()
    world.clock.at += 10_000
    world.path.tick(world.work, lambda: {})
    assert world.routes.qa_calls() == [] and world.gate.qa_ops() == []


@pytest.mark.parametrize("status,code", [
    (403, "qa_identity_not_granted"), (503, "qa_identity_unconfigured"),
])
def test_a_first_refusal_not_granted_or_unconfigured_fails_the_attempt_before_the_runner_starts(
        tmp_path, db, status, code):
    refusal = children_mod.ChildApiError(status, code, "", retryable=status >= 500)
    world = _world(tmp_path, db, task=_task(qa_identity="console"), refuse=refusal)
    assert world.fails == code, "the lifecycle fails the attempt on this cause before the runner"
    assert world.gate.qa_ops() == [{"failed": code}]
    assert not world.gate.qa.held
    assert world.path.start_qa_identity(_task(qa_identity="console")) == code, "asked once"
    assert len(world.routes.qa_calls()) == 1


@pytest.mark.parametrize("status,code", [
    (422, "qa_identity_not_requested"), (409, "stale_generation"), (409, "lease_not_live"),
])
def test_any_other_first_refusal_runs_the_agent_signed_out_and_told_why(tmp_path, db, status, code):
    refusal = children_mod.ChildApiError(status, code, "", retryable=False)
    world = _world(tmp_path, db, task=_task(qa_identity="console"), refuse=refusal)
    assert world.fails is None, "the task is not failed: the agent can still report what it saw"
    assert world.gate.qa_ops() == [{"failed": code}]
    text = _notice(world).read_text()
    assert code in text and "hand-off" in text
    (note,) = [n for n in world.notes if n.get("phase") == "qa_identity"]
    assert code in note["reason"]


def test_renewal_happens_at_two_thirds_of_the_life(tmp_path, db):
    world = _world(tmp_path, db, task=_task(qa_identity="console"))
    start = world.clock.at
    world.clock.at = start + LIFE * 2 / 3 - 1
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.qa_calls()) == 1, "not before two-thirds"
    world.clock.at = start + LIFE * 2 / 3
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.qa_calls()) == 2
    first, second = world.routes.issued
    assert world.gate.qa_ops()[-1]["credential"] == second != first
    assert world.path.qa.renewals == 1 and world.gate.qa.renewals == 1
    # And the next one two-thirds of a life after that.
    world.clock.at = start + LIFE * 4 / 3 - 1
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.qa_calls()) == 2
    world.clock.at = start + LIFE * 4 / 3
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.qa_calls()) == 3


def test_a_refusal_at_renewal_sends_failed_to_the_gate_and_writes_the_notice(tmp_path, db):
    world = _world(tmp_path, db, task=_task(qa_identity="console"))
    assert not _notice(world).exists()
    # A platform admin revokes the grant between issue and renewal.
    world.routes.refuse = children_mod.ChildApiError(403, "qa_identity_not_granted", "", retryable=False)
    world.clock.at += LIFE * 2 / 3
    world.path.tick(world.work, lambda: {})
    assert world.gate.qa_ops()[-1] == {"failed": "qa_identity_not_granted"}
    assert not world.gate.qa.held, "the gate dropped the credential at once"
    text = _notice(world).read_text()
    assert "qa_identity_not_granted" in text
    (note,) = [n for n in world.notes if n.get("phase") == "qa_identity"]
    assert "qa_identity_not_granted" in note["reason"]
    # Permanent: nothing more is asked this attempt.
    world.clock.at += 10_000
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.qa_calls()) == 2


def test_no_log_record_contains_the_credential(tmp_path, db, caplog):
    caplog.set_level(logging.DEBUG)
    world = _world(tmp_path, db, task=_task(qa_identity="console"))
    for _ in range(3):
        world.clock.at += LIFE * 2 / 3
        world.path.tick(world.work, lambda: {})
    # A transient failure, then a gate that refuses, then a revoke: every log path.
    world.routes.refuse = children_mod.ChildApiError(503, "qa_identity_sign_failed", "", retryable=True)
    world.clock.at += LIFE * 2 / 3
    world.path.tick(world.work, lambda: {})
    world.routes.refuse = None
    world.clock.at += LIFE
    world.path.tick(world.work, lambda: {})
    world.routes.refuse = children_mod.ChildApiError(403, "qa_identity_not_granted", "", retryable=False)
    world.clock.at += LIFE
    world.path.tick(world.work, lambda: {})
    assert len(world.routes.issued) == 5
    captured = "\n".join(
        [r.getMessage() + json.dumps(r.__dict__, default=str) for r in caplog.records]
        + [_everything_readable(world)]
    )
    assert world.log.lines, "the scan read something"
    for credential in world.routes.issued:
        assert credential not in captured
        for segment in credential.split("."):
            assert segment not in captured
    # The log lines carry the jti and codes instead.
    assert any(msg == "qa identity handed to the gate" and kw.get("jti") for msg, kw in world.log.lines)


# --------------------------------------------------------------------------
# Fail closed, and recover
# --------------------------------------------------------------------------


def test_a_transient_failure_while_the_credential_works_changes_nothing(tmp_path, db):
    world = _world(tmp_path, db, task=_task(qa_identity="console"))
    world.routes.refuse = children_mod.ChildApiError(None, "api_unavailable", "", retryable=True)
    world.clock.at += LIFE * 2 / 3
    world.path.tick(world.work, lambda: {})
    assert world.gate.qa_ops()[-1].get("failed") is None
    assert world.path.qa.state == "ready" and not _notice(world).exists()
    # The credential in hand expires unrenewed: the gate and the agent are told.
    world.clock.at += LIFE / 3
    world.path.tick(world.work, lambda: {})
    assert world.gate.qa_ops()[-1] == {"failed": qa_identity.UNREACHABLE}
    assert qa_identity.UNREACHABLE in _notice(world).read_text()
    # swarm-api answers again: signed back in, the notice gone.
    world.routes.refuse = None
    world.clock.at += 120
    world.path.tick(world.work, lambda: {})
    assert world.gate.qa.held and world.path.qa.state == "ready"
    assert not _notice(world).exists()


def test_no_attempt_key_or_an_unprotected_heap_runs_signed_out(tmp_path, db):
    for memory, key, cause in ((None, False, "no_attempt_key"),
                               (MemoryProtection(FAILED, "test"), True, "worker_unprotected")):
        world = _world(tmp_path / cause, db, task=_task(qa_identity="console"), memory=memory, key=key)
        assert world.fails is None
        assert world.routes.qa_calls() == []
        assert world.gate.qa_ops() == [{"failed": cause}]
        assert cause in _notice(world).read_text()


def test_a_gate_that_refuses_the_credential_is_failed_closed(tmp_path, db, monkeypatch):
    original = QaRoutes.call

    def wrong_host(self, method, path, *, body=None, headers=None):
        status, payload = original(self, method, path, body=body, headers=headers)
        if path.endswith("/qa-identity"):
            payload = {**payload, "host": "not a host"}
        return status, payload

    monkeypatch.setattr(QaRoutes, "call", wrong_host)
    world = _world(tmp_path, db, task=_task(qa_identity="console"))
    assert world.fails is None
    assert world.gate.qa_ops()[-1] == {"failed": qa_identity.GATE_REFUSED}
    assert qa_identity.GATE_REFUSED in _notice(world).read_text()
    (issued,) = world.routes.issued
    assert issued not in _everything_readable(world)
