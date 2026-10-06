"""A resumed CLI session's cost is counted once, not twice (#667, observer P14).

Measured on 2026-10-06: C1C's implement step recorded $3.684 where the true
cost was about $1.88. The first pass reported $1.8037 and 260,533 ms of API
time; the session, resumed to finish, reported $1.8804 and 272,258 ms with only
951 output tokens. The CLI's `result` event reports `total_cost_usd`,
`duration_api_ms` and `modelUsage` for the whole SESSION, restored on resume,
and `usage`, `num_turns` and `duration_ms` for that invocation alone. Summing
the first kind counted the first pass twice.

Pinned here, with two result events:

* the runner's combination (`cliagent._combined_spend`) of a pass and its
  resume takes the session figures from the later event and adds the
  per-invocation ones;
* the same through the real claude-code runner against a fake CLI;
* the worker's sum across runner starts (`lifecycle._add_spend`) does the
  same for a runner that resumed the session of an earlier one, and still
  adds every figure across runners that did not;
* a "cumulative" figure that is lower than the one it continues was not
  carried over, and is added -- never a figure below what was measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_worker import lifecycle
from agent_worker.runners import claude_code, cliagent

from test_claude_resume_to_finish import DONE, PENDING, SESSION, _ctx, _install, _runs

FIRST = {
    "total_cost_usd": 1.8037,
    "duration_api_ms": 260_533,
    "duration_ms": 300_000,
    "num_turns": 41,
    "usage": {"input_tokens": 120, "output_tokens": 15_000, "cache_read_input_tokens": 900_000},
    "modelUsage": {"claude-opus-5-5": {"outputTokens": 15_000, "costUSD": 1.8037}},
}
RESUMED = {
    "total_cost_usd": 1.8804,
    "duration_api_ms": 272_258,
    "duration_ms": 20_000,
    "num_turns": 2,
    "usage": {"input_tokens": 8, "output_tokens": 951, "cache_read_input_tokens": 70_000},
    "modelUsage": {"claude-opus-5-5": {"outputTokens": 15_951, "costUSD": 1.8804}},
}


def test_the_runner_takes_the_sessions_figures_from_the_resumed_result():
    spend = cliagent._combined_spend(FIRST, RESUMED)

    assert spend["total_cost_usd"] == pytest.approx(1.8804)
    assert spend["duration_api_ms"] == 272_258
    assert spend["modelUsage"] == RESUMED["modelUsage"]
    # Per invocation: added.
    assert spend["usage"]["output_tokens"] == 15_951
    assert spend["usage"]["input_tokens"] == 128
    assert spend["usage"]["cache_read_input_tokens"] == 970_000
    assert spend["num_turns"] == 43
    assert spend["duration_ms"] == 320_000


def test_a_session_figure_lower_than_the_one_it_continues_is_added():
    carried_nothing = {**RESUMED, "total_cost_usd": 0.0767, "duration_api_ms": 11_725}
    spend = cliagent._combined_spend(FIRST, carried_nothing)
    assert spend["total_cost_usd"] == pytest.approx(1.8037 + 0.0767)
    assert spend["duration_api_ms"] == 260_533 + 11_725


def test_a_resume_that_reported_nothing_keeps_the_first_pass():
    assert cliagent._combined_spend(FIRST, {}) == FIRST


#: The fake CLI of test_claude_resume_to_finish, reporting what the real one
#: does: session totals for cost and API time, per-invocation usage and turns.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
home = pathlib.Path(os.environ["HOME"])
plan = json.loads((home / "plan.json").read_text())
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "prompt": argv[-1]}) + "\n")
runs = sum(1 for _ in (home / "runs.jsonl").open())
step = plan["runs"][min(runs - 1, len(plan["runs"]) - 1)]
print(json.dumps({"type": "system", "subtype": "init", "session_id": plan["session"]}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": plan["session"], "result": step["answer"], **step["spend"]}),
      flush=True)
'''


def test_a_session_resumed_to_finish_costs_what_the_cli_last_reported(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": PENDING, "spend": FIRST},
        {"answer": DONE, "spend": RESUMED},
    ])
    (tmp_path / "fake-claude").write_text(FAKE_CLI)

    out = claude_code.body(ctx)

    assert [run["resume"] for run in _runs(ctx)] == [None, SESSION]
    spend = out["structured_output"]
    assert spend["total_cost_usd"] == pytest.approx(1.8804), "the first pass was counted twice"
    assert spend["duration_api_ms"] == 272_258
    assert spend["usage"]["output_tokens"] == 15_951
    assert spend["num_turns"] == 43


# ---------------------------------------------------------------------------
# the worker, across runner starts
# ---------------------------------------------------------------------------


def _summary(cli: dict) -> dict:
    return lifecycle._usage_summary(cli)


def test_add_spend_takes_a_resumed_runners_session_figures():
    first = _summary(FIRST)
    total = lifecycle._add_spend({}, first)
    total = lifecycle._add_spend(total, _summary(RESUMED), session_base={})

    assert total["total_cost_usd"] == pytest.approx(1.8804)
    assert total["duration_api_ms"] == 272_258
    assert total["output_tokens"] == 15_951
    assert total["num_turns"] == 43
    assert total["duration_ms"] == 320_000


def test_add_spend_keeps_what_was_spent_before_the_session_began():
    """A runner restarted from the prompt began a new session: its cost stays."""
    earlier = {"total_cost_usd": 0.5, "duration_api_ms": 10_000, "num_turns": 3}
    total = lifecycle._add_spend({}, earlier)
    base = dict(total)
    total = lifecycle._add_spend(total, _summary(FIRST))
    total = lifecycle._add_spend(total, _summary(RESUMED), session_base=base)

    assert total["total_cost_usd"] == pytest.approx(0.5 + 1.8804)
    assert total["duration_api_ms"] == 10_000 + 272_258
    assert total["num_turns"] == 3 + 41 + 2


def test_add_spend_still_adds_runners_that_did_not_resume():
    total = lifecycle._add_spend(_summary(FIRST), _summary(RESUMED))
    assert total["total_cost_usd"] == pytest.approx(1.8037 + 1.8804)


def test_the_worker_counts_a_resumed_runner_once(worker_factory, tmp_path: Path):
    from agent_worker import workspace as workspace_mod

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)

    def ended(cli: dict, *, resumed: bool) -> None:
        worker._note_runner_spend_start(resumed=resumed)
        worker.ws.result_path.write_text(json.dumps({"output": cli}))
        worker._spend_pending = True
        worker._collect_spend()

    ended(FIRST, resumed=False)
    ended(RESUMED, resumed=True)

    assert worker._spend["total_cost_usd"] == pytest.approx(1.8804)
    assert worker._spend["duration_api_ms"] == 272_258
    assert worker._spend["output_tokens"] == 15_951
