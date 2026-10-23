"""A gate-raised hand-off goes through the worker's own path (CAPTCHA-HANDOFF; part of #2058).

docs/design/live-browser.md §4.1. Owner decision, BGD 2026-10-23: when the
gate sees a CAPTCHA on the agent's page it raises the hand-off itself. The
gate puts the request in its `status` (test_captcha_handoff_gate.py); the
worker raises it through exactly the path `swarm-handoff request` takes --
validation, the redaction filter, `max_handoffs_per_attempt`, pausing the
agent's CDP, `POST /v1/tasks/{id}/handoff` -- with `origin: "gate"`, and the
agent's `swarm-handoff wait` waits on it.

The gate and swarm-api are test_handoff.py's doubles; the agent's command is
the real `images/agent-runtime-browser/swarm-handoff`.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from agent_worker import handoff as handoff_mod

from test_handoff import FakeGateClient, _answer, _bare, _command_module, _spool_request
from test_handoff import trace  # noqa: F401  (a pytest fixture, used by name)


class ChallengeGate(FakeGateClient):
    """The gate, with a CAPTCHA request in its `status` until a `pause` takes it off."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.challenge: dict[str, Any] | None = None

    def raise_challenge(self, host: str = "shop.example.com", *, request_id: str | None = None,
                        reason: str | None = None) -> str:
        request_id = request_id or "gate-" + secrets.token_hex(6)
        self.challenge = {"request_id": request_id, "reason": reason or f"CAPTCHA on {host}",
                          "expect": host}
        return request_id

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = super().call(op, args)
        if op == "status":
            return {**result, "challenge": self.challenge}
        if op == "pause":
            self.challenge = None
        return result


def _world(tmp_path, db, trace):
    gate = ChallengeGate(trace)
    path, routes, work = _bare(tmp_path, db, trace, gate)
    return gate, path, routes, work


def _sent(routes) -> list[dict[str, Any]]:
    return [c["body"] for c in routes.calls if c["path"].endswith("/handoff")]


def _fetch(db):
    return lambda: db.doc("tasks/task_1")


def _spool(work: Path) -> Path:
    return work / handoff_mod.SPOOL_DIR_NAME


def test_a_gate_raised_request_pauses_the_agent_and_is_recorded_with_origin_gate(tmp_path, db, trace):
    gate, path, routes, work = _world(tmp_path, db, trace)
    request_id = gate.raise_challenge()
    assert path.tick(work, _fetch(db)) is None
    # MUTATION: skip the gate's `status` request in HandoffPath.tick and nothing is raised.
    assert path.active is not None and path.active["request_id"] == request_id
    # §4.1 step 2 before step 3: the agent's CDP is held before the wait is recorded.
    assert trace.index("gate:pause") < trace.index("route:handoff")
    pause = [a for op, a in gate.ops if op == "pause"]
    assert pause == [{"request_id": request_id, "expect": "shop.example.com"}]
    (sent,) = _sent(routes)
    # MUTATION: send the gate's request without `origin` and the console cannot tell it apart.
    assert sent["origin"] == "gate"
    assert sent["reason"] == "CAPTCHA on shop.example.com" and sent["expect"] == "shop.example.com"
    answer = _answer(work, request_id)
    assert answer["outcome"] == "waiting" and answer["origin"] == "gate"
    # MUTATION: drop "do not retry" from GATE_WAITING and the agent is not told to wait.
    assert "solve a CAPTCHA" in answer["message"] and "do not retry" in answer["message"]
    # The ledger keeps it apart from the agent's own requests.
    (entry,) = json.loads((_spool(work) / handoff_mod.HISTORY_NAME).read_text())
    assert entry["origin"] == "gate"


def test_the_agents_wait_waits_on_the_gates_request_and_returns_when_the_person_does(
    tmp_path, db, trace, monkeypatch, capsys
):
    gate, path, routes, work = _world(tmp_path, db, trace)
    request_id = gate.raise_challenge()
    path.tick(work, _fetch(db))
    cmd = _command_module()
    monkeypatch.setenv("SWARM_HANDOFF", str(_spool(work)))
    # MUTATION: leave `.last` alone in HandoffPath._raise_for_gate and `wait` says "nothing to wait for".
    assert cmd.main(["wait", "--max-seconds", "0.2", "--poll", "0.1"]) == handoff_mod.EXIT_STILL_WAITING
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["request_id"] == request_id
    db.doc("tasks/task_1")["human_wait"] = None  # a person solved it and returned control
    path.tick(work, _fetch(db))
    assert path.active is None and "gate:resume" in trace
    assert cmd.main(["wait", "--max-seconds", "0.2", "--poll", "0.1"]) == handoff_mod.EXIT_RETURNED
    # The same request in the gate's next `status` is not raised again.
    gate.challenge = {"request_id": request_id, "reason": "CAPTCHA on shop.example.com",
                      "expect": "shop.example.com"}
    path.tick(work, _fetch(db))
    # MUTATION: forget the ids already raised and the solved CAPTCHA is asked about twice.
    assert gate.names().count("pause") == 1 and len(_sent(routes)) == 1


def test_a_gate_raised_request_is_validated_and_redacted_like_an_agents(tmp_path, db, trace):
    gate, path, routes, work = _world(tmp_path, db, trace)
    refused = gate.raise_challenge("localhost")
    path.tick(work, _fetch(db))
    answer = _answer(work, refused)
    # MUTATION: skip `expect_refusal` for a gate request in HandoffPath._open and this fails.
    assert answer["outcome"] == "refused" and answer["code"] == "expect_invalid"
    assert "pause" not in gate.names() and _sent(routes) == []
    leaked = "ghp_" + secrets.token_hex(18)
    gate.raise_challenge(reason=f"CAPTCHA on shop.example.com {leaked}")
    path.tick(work, _fetch(db))
    (sent,) = _sent(routes)
    # MUTATION: send the gate's reason without the redaction filter and this fails.
    assert leaked not in json.dumps(sent)


def test_past_the_per_attempt_maximum_the_agent_gets_the_existing_refusal(
    tmp_path, db, trace, monkeypatch, capsys
):
    gate, path, routes, work = _world(tmp_path, db, trace)
    fetch = _fetch(db)
    for n in range(handoff_mod.MAX_HANDOFFS_PER_ATTEMPT):
        _spool_request(work, f"r{n}", {"reason": "sign in"})
        path.tick(work, fetch)
        db.doc("tasks/task_1")["human_wait"] = None
        path.tick(work, fetch)
    request_id = gate.raise_challenge()
    path.tick(work, fetch)
    answer = _answer(work, request_id)
    # MUTATION: count only the agent's own requests against the cap and a fourth hand-off opens.
    assert answer["outcome"] == "refused" and answer["code"] == "handoff_limit"
    assert answer["exit"] == handoff_mod.EXIT_REFUSED
    assert gate.names().count("pause") == handoff_mod.MAX_HANDOFFS_PER_ATTEMPT
    assert len(_sent(routes)) == handoff_mod.MAX_HANDOFFS_PER_ATTEMPT
    cmd = _command_module()
    monkeypatch.setenv("SWARM_HANDOFF", str(_spool(work)))
    assert cmd.main(["wait", "--max-seconds", "0.2", "--poll", "0.1"]) == handoff_mod.EXIT_REFUSED


def test_an_agents_own_request_during_the_gates_hand_off_joins_it(tmp_path, db, trace):
    """The runner's note still says to ask for help on a CAPTCHA; an agent
    that does so while the gate's hand-off is open is answered by that one,
    not by a second hand-off for a CAPTCHA a person already solved."""
    gate, path, routes, work = _world(tmp_path, db, trace)
    request_id = gate.raise_challenge()
    path.tick(work, _fetch(db))
    _spool_request(work, "mine", {"reason": "please solve the CAPTCHA", "expect": "shop.example.com"})
    path.tick(work, _fetch(db))
    joined = _answer(work, "mine")
    # MUTATION: leave the agent's request queued behind the gate's and a second hand-off opens after it.
    assert joined["outcome"] == "waiting" and joined["joined"] == request_id
    db.doc("tasks/task_1")["human_wait"] = None
    path.tick(work, _fetch(db))
    path.tick(work, _fetch(db))
    # MUTATION: answer only the gate's id on return and the agent's `wait` never ends.
    assert _answer(work, "mine")["outcome"] == "returned"
    assert _answer(work, request_id)["outcome"] == "returned"
    assert gate.names().count("pause") == 1 and len(_sent(routes)) == 1


def test_an_agents_own_request_carries_no_origin(tmp_path, db, trace):
    """swarm-api's default is "agent"; a worker that names it would be refused
    by a swarm-api from before `origin` existed, mid-rollout."""
    gate, path, routes, work = _world(tmp_path, db, trace)
    _spool_request(work, "r1", {"reason": "sign in"})
    path.tick(work, _fetch(db))
    (sent,) = _sent(routes)
    # MUTATION: send `origin` on every request and this fails.
    assert "origin" not in sent


def test_the_browser_runners_note_says_never_solve_a_captcha_the_platform_asks_a_person():
    from agent_worker.runners import claude_code

    note = claude_code.LIVE_BROWSER_NOTE
    # MUTATION: drop the sentence from LIVE_BROWSER_NOTE and this fails.
    assert ("Never try to solve or get around a CAPTCHA yourself: the platform asks a person "
            "to solve it") in note
