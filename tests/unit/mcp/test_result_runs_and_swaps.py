"""`swarm_result`'s outputs carry every agent run and each account swap (SWAP-RUNS, #2457).

The worker writes `result_summary.runs`, `.swaps`, `.swap_cost` and
`.last_run_duration_seconds` only for an attempt that ran the agent more than
once, and then `duration_seconds` is the sum of the runs. `outputs_of` passes
them on and `render.produced_lines` says them in one line; a one-run task's
outputs are what they were.
"""

from __future__ import annotations

from swarm_mcp import render
from swarm_mcp.client import outputs_of

#: The shape the worker writes (tests/unit/worker/test_swap_runs.py), with the
#: 2026-10-26 lane's figures: a swap at minute 42 of a 7,200 s timeout.
TWO_RUNS = {
    "artifacts": [],
    "exit_code": -9,
    "duration_seconds": 7189.6,
    "last_run_duration_seconds": 4657.8,
    "runs": [
        {"run": 1, "started_at": "2026-10-26T09:07:00Z", "ended_at": "2026-10-26T09:49:03Z",
         "duration_s": 2531.8, "exit_code": -15, "account": "eng:ai-eng-2",
         "resumed": False, "after_swap": False,
         "tokens": {"input_tokens": 900, "output_tokens": 40000}},
        {"run": 2, "started_at": "2026-10-26T09:49:05Z", "ended_at": "2026-10-26T11:06:52Z",
         "duration_s": 4657.8, "exit_code": -9, "timed_out": True,
         "account": "eng:ai-eng-1-max", "resumed": True, "after_swap": True,
         "reread": {"reached": True, "seconds": 212.4, "tool": "Edit",
                    "tokens": {"input_tokens": 300, "cache_read_input_tokens": 1500000}}},
    ],
    "swaps": [
        {"at": "2026-10-26T09:50:13Z", "from": "eng:ai-eng-2", "to": "eng:ai-eng-1-max",
         "reason": "exhausted", "after_run": 1},
    ],
    "swap_cost": {"reread_seconds": 212.4,
                  "reread_tokens": {"input_tokens": 300, "cache_read_input_tokens": 1500000},
                  "measured_runs": 1, "reached_action": 1},
}


def _task(summary: dict) -> dict:
    return {"state": "FAILED", "result_summary": summary}


def test_a_two_run_task_reports_its_runs_its_swap_and_the_summed_duration() -> None:
    produced = outputs_of(_task(TWO_RUNS))

    assert produced["duration_s"] == 7189.6
    assert produced["last_run_duration_s"] == 4657.8
    assert [r["account"] for r in produced["runs"]] == ["eng:ai-eng-2", "eng:ai-eng-1-max"]
    assert produced["runs"][1]["reread"]["seconds"] == 212.4
    assert produced["swaps"] == TWO_RUNS["swaps"]
    assert produced["swap_cost"] == TWO_RUNS["swap_cost"]

    line = render.runs_line(produced)
    assert line == (
        "2 runs · 1 account swap (ai-eng-2 exhausted → ai-eng-1-max at 09:50 UTC)"
        " · re-read 212s, 1,500,300 tokens · last run 4657.8s"
    )
    assert line in "\n".join(render.produced_lines(produced, render.PLAIN))


def test_a_one_run_task_reports_exactly_what_it_did_before() -> None:
    produced = outputs_of(_task({"artifacts": [], "exit_code": 0, "duration_seconds": 60.6}))

    assert produced["duration_s"] == 60.6
    for key in ("runs", "swaps", "swap_cost", "last_run_duration_s"):
        assert key not in produced, key
    assert render.runs_line(produced) is None
    assert "runs" not in "\n".join(render.produced_lines(produced, render.PLAIN))


def test_the_progress_duration_says_it_sums_the_runs() -> None:
    from swarm_mcp import progress

    # No started/completed pair: the duration is the runner summary's.
    assert progress._duration({"result_summary": TWO_RUNS}) == {
        "duration_s": 7189.6,
        "duration_basis": "the last attempt's 2 agent runs, summed",
    }
    assert progress._duration({"result_summary": {"duration_seconds": 60.6}}) == {
        "duration_s": 60.6,
        "duration_basis": "the last attempt's runner",
    }
