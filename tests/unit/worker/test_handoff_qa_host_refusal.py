"""A hand-off on the QA identity's console host is answered 5 `qa_identity_host` (part of #1333).

MEASURED 2026-10-31 and 2026-11-01 (accept runs 41234694248 and
41256718065): the live-browser group's `qa_handoff_refused` read "exited 0
with code None, not 5 with qa_identity_host". Two causes, each held here:

1. THE WORKER. The gate refuses `pause` on the console host
   (`livegate/__main__.py`, `OpRefused("qa_identity_host")`), and ctl.sock
   carries it as `refused:qa_identity_host`, which `Gate._translate` makes a
   `GateRefused` with that code. `GateRefused` is a `GateLost`, and the
   worker's `_open` caught `GateLost` alone: it answered `gate_unavailable`
   and marked the whole live path unavailable for the rest of the attempt.
   A refusal of THIS request is answered with the gate's own word, and the
   live path stays open.
2. THE JUDGE. `swarm-handoff request` only queues the request (it prints
   `requested` and exits 0; the agent's guide says to run `wait` next): the
   answer, refused or not, is `wait`'s. The acceptance prompt said "never run
   swarm-handoff wait", so the agent reported `request`'s 0 and no code
   (tests/acceptance/test_live_browser.py, `qa_identity_prompt`).

The command is driven as the agent runs it: `request`, the worker's tick,
then `wait`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_worker import handoff as handoff_mod
from agent_worker.livegate.ctl import CtlRefused

from test_handoff import (  # noqa: F401 - `gate` and `trace` are fixtures
    FakeGateClient,
    _bare,
    _command_module,
    gate,
    trace,
)

ACCEPTANCE = Path(__file__).resolve().parents[2] / "acceptance"


class _RefusesOnTheConsole(FakeGateClient):
    """The gate on the console host: `pause` refused as ctl.sock carries it."""

    def call(self, op, args):
        if op == "pause":
            self.ops.append((op, dict(args)))
            raise CtlRefused("refused", "refused:qa_identity_host")
        return super().call(op, args)


def _ask_and_wait(path, work, db, monkeypatch, capsys) -> tuple[int, dict, int, dict]:
    cmd = _command_module()
    monkeypatch.setenv("SWARM_HANDOFF", str(work / handoff_mod.SPOOL_DIR_NAME))
    asked = cmd.main(["request", "--reason", "QA identity acceptance: a hand-off on the console"])
    request_out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    path.tick(work, lambda: db.doc("tasks/task_1"))
    waited = cmd.main(["wait", "--max-seconds", "0.3", "--poll", "0.1"])
    wait_out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return asked, request_out, waited, wait_out


def test_the_console_host_refusal_reaches_the_agent_as_5_qa_identity_host(
    tmp_path, db, trace, monkeypatch, capsys
):
    refusing = _RefusesOnTheConsole(trace)
    path, routes, work = _bare(tmp_path, db, trace, refusing)

    asked, request_out, waited, wait_out = _ask_and_wait(path, work, db, monkeypatch, capsys)

    assert (asked, request_out["outcome"]) == (0, "requested")
    assert waited == handoff_mod.EXIT_REFUSED == 5
    assert wait_out["outcome"] == "refused" and wait_out["code"] == "qa_identity_host", wait_out
    # No hold was opened: swarm-api was never asked, nothing is counted.
    assert path.active is None and path.requests_made == 0
    assert not any(p.endswith("/handoff") for p in routes.paths())
    # The gate refused this request, not the live path: it stays open.
    assert path.unavailable is None and path.offered
    assert not (work / handoff_mod.SPOOL_DIR_NAME / "unavailable").exists()


def test_a_lost_gate_on_pause_is_still_gate_unavailable(tmp_path, db, trace, gate, monkeypatch, capsys):
    """The control: a gate that broke, not one that said no, ends the live path as before."""
    path, routes, work = _bare(tmp_path, db, trace, gate)
    gate.lose_on = "pause"

    _, _, waited, wait_out = _ask_and_wait(path, work, db, monkeypatch, capsys)

    assert waited == 5 and wait_out["code"] == handoff_mod.GATE_UNAVAILABLE
    assert path.unavailable == handoff_mod.GATE_UNAVAILABLE


def test_the_acceptance_prompt_reads_the_answer_from_wait():
    """The judge reads `handoff_exit` and `handoff_code` from what the agent
    reports: they must be `wait`'s, bounded so a granted hold is never waited
    out, since `request` alone never carries an answer."""
    sys.path.insert(0, str(ACCEPTANCE))
    try:
        import test_live_browser as live
    finally:
        sys.path.remove(str(ACCEPTANCE))
    prompt = live.qa_identity_prompt("https://console.example.test")
    assert "swarm-handoff wait --max-seconds" in prompt
    assert "Never run swarm-handoff wait" not in prompt
    assert "of swarm-handoff wait" in prompt
