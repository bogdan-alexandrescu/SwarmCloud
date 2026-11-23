"""A verdict gate that reads further reviews (`also_task_ids`): any NOT_YET runs the fix.

An issue run whose plan touches a risk path compiles a security review
beside the first review, and its one fix step stages both verdicts: the
first review's `verdict.json` and the security review's
`security-verdict.json`. swarm-api writes the fix's gate as
`{"task_id": <review>, "verdict_in": ["NOT_YET"], "also_task_ids":
[<security review>]}`. Pinned here, each against the way it would most
likely go wrong:

  * the fix agent runs when ONLY the security verdict says NOT_YET -- the
    first review's MERGE must not shut a gate the other review opened;
  * both MERGE shut the gate, the agent never starts, the step still
    succeeds and passes the first review's verdict on;
  * a further review whose verdict this step does not stage fails without
    running the agent, and a malformed `also_task_ids` is refused rather
    than read as no further review;
  * the block swarm-api writes is the block the worker reads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode, InputUnavailable
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_input_from import run_upstream


def _verdict_text(verdict: str, findings: Any = None) -> str:
    return json.dumps({"verdict": verdict, "findings": findings or []})


def _upstreams(db: Any, worker_factory: Any, *, review: str, security: str) -> None:
    run_upstream(db, worker_factory, artifact_name="verdict.json", artifact_text=review)
    run_upstream(db, worker_factory, task_id="task_sec", attempt_id="att_sec",
                 lease_id="lease_sec", artifact_name="security-verdict.json",
                 artifact_text=security)


def _seed_fix(db: Any, *, stage_security: bool = True) -> None:
    seed_attempt(
        db,
        task_id="task_2",
        attempt_id="att_2",
        lease_id="lease_2",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    staged = {"task_up": "verdict.json"}
    if stage_security:
        staged["task_sec"] = "security-verdict.json"
    db.doc("tasks/task_2")["metadata"] = {
        "input_from": staged,
        "dispatch": {
            "strategy": "collect",
            "carrier": "checkpoints",
            "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"],
                             "also_task_ids": ["task_sec"]},
        },
    }


def _run_fix(worker_factory: Any, monkeypatch: Any, *, runner_allowed: bool) -> int:
    worker, _config, _exporter = worker_factory(
        task_id="task_2", attempt_id="att_2", lease_id="lease_2"
    )
    if not runner_allowed:
        def refuse(*_args, **_kwargs):
            raise AssertionError("the runner was started behind a shut verdict gate")

        monkeypatch.setattr(worker, "_run_child_supervised", refuse)
    return worker.run()


def test_the_fix_agent_runs_when_only_the_security_verdict_says_not_yet(
    db, worker_factory, monkeypatch, runner_inputs
):
    _upstreams(db, worker_factory, review=_verdict_text("MERGE", ["no code blocker"]),
               security=_verdict_text("NOT_YET", [{"summary": "the token reaches the log"}]))
    _seed_fix(db)

    # MUTATION: decide on the first review's verdict alone and the gate is
    # shut: the runner is never started and the security blocker ships.
    assert _run_fix(worker_factory, monkeypatch, runner_allowed=True) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    gate = task["result_summary"]["verdict_gate"]
    assert gate["agent_ran"] is True
    assert gate["verdict"] == "MERGE"
    assert gate["also"] == [{"task_id": "task_sec", "file": "security-verdict.json",
                             "verdict": "NOT_YET", "gate_reading": "NOT_YET"}]
    # The fix sees both sets of findings.
    assert gate["findings"] == ["no code blocker", "the token reaches the log"]
    assert task["result_summary"]["runner"]["status"] != "skipped"
    assert runner_inputs[-1]["task_id"] == "task_2"


def test_both_merge_shut_the_gate_and_the_step_still_succeeds(db, worker_factory, monkeypatch):
    _upstreams(db, worker_factory, review=_verdict_text("MERGE"),
               security=_verdict_text("MERGE"))
    _seed_fix(db)

    assert _run_fix(worker_factory, monkeypatch, runner_allowed=False) == ExitCode.OK

    task = db.doc("tasks/task_2")
    assert task["state"] == TaskState.SUCCEEDED.value
    gate = task["result_summary"]["verdict_gate"]
    assert gate["agent_ran"] is False
    assert [entry["verdict"] for entry in gate["also"]] == ["MERGE"]


def test_the_first_reviews_not_yet_still_runs_the_fix(
    db, worker_factory, monkeypatch, runner_inputs
):
    _upstreams(db, worker_factory, review=_verdict_text("NOT_YET", ["the sort is unstable"]),
               security=_verdict_text("MERGE"))
    _seed_fix(db)

    assert _run_fix(worker_factory, monkeypatch, runner_allowed=True) == ExitCode.OK
    assert db.doc("tasks/task_2")["result_summary"]["verdict_gate"]["agent_ran"] is True


def test_a_further_review_this_step_stages_nothing_from_fails(db, worker_factory, monkeypatch):
    _upstreams(db, worker_factory, review=_verdict_text("MERGE"),
               security=_verdict_text("NOT_YET"))
    _seed_fix(db, stage_security=False)

    assert _run_fix(worker_factory, monkeypatch, runner_allowed=False) != ExitCode.OK
    assert db.doc("tasks/task_2")["state"] != TaskState.SUCCEEDED.value


@pytest.mark.parametrize("also", ["task_sec", [""], [7], ["task_up"]])
def test_a_malformed_also_is_refused_rather_than_read_as_none(also):
    with pytest.raises(InputUnavailable):
        verdict_mod.gate_from_dispatch({"verdict_gate": {
            "task_id": "task_up", "verdict_in": ["NOT_YET"], "also_task_ids": also}})


def test_the_block_the_api_writes_is_the_block_the_worker_reads():
    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="integrate", carrier="branches").with_routing(
        builds_on="task_impl", gate_task_id="task_review", gate_verdicts=("NOT_YET",),
        gate_also_task_ids=("task_sec",),
    ).to_metadata()
    assert block["verdict_gate"]["also_task_ids"] == ["task_sec"]
    assert verdict_mod.gate_from_dispatch(block) == verdict_mod.VerdictGate(
        task_id="task_review", verdict_in=("NOT_YET",), also_task_ids=("task_sec",))
    # Without further reviews the block is exactly the one it always was.
    plain = DispatchOptions(strategy="integrate", carrier="branches").with_routing(
        builds_on="task_impl", gate_task_id="task_review", gate_verdicts=("NOT_YET",),
    ).to_metadata()
    assert plain["verdict_gate"] == {"task_id": "task_review", "verdict_in": ["NOT_YET"]}
