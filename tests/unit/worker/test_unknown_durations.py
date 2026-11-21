"""An unknown duration is None, never 0.0 (issue 56).

A 0.0 reads as "instant": a child that never started, a run closed with no
result and no child, and a swap re-read with no seconds were all stored as
0.0 and summed into the attempt's totals as if measured. These pin:

  * `ChildProcess.elapsed_seconds` is None for a child that never started;
  * a run closed with no result and no child stores `duration_s: None`;
  * an attempt's summed duration counts only measured runs and says
    `duration_partial: true` when any run's duration is unknown;
  * a swap re-read with unknown seconds is left out of `reread_seconds` and
    `swap_cost` says `partial: true`.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_worker import lifecycle
from agent_worker.procman import ChildProcess


def test_a_child_that_never_started_has_no_elapsed_seconds(tmp_path):
    child = ChildProcess(
        ["true"], cwd=tmp_path, env={}, stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
        max_stdout_bytes=1024, max_stderr_bytes=1024, logger=SimpleNamespace(),
    )
    assert child.elapsed_seconds is None


def test_a_run_closed_with_no_result_and_no_child_stores_no_duration():
    worker = SimpleNamespace(
        _run_open={"run": 1, "started_at": "2026-11-21T05:00:00Z", "after_swap": False},
        _child=None, _spend={}, ws=None, _runs=[],
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    lifecycle.Worker._close_run(worker, None, spent_before={})

    [run] = worker._runs
    assert run["duration_s"] is None
    assert run["exit_code"] is None


def test_a_two_run_attempt_with_one_unknown_run_sums_the_known_and_is_partial():
    runs = [{"run": 1, "duration_s": 12.5}, {"run": 2, "duration_s": None}]
    assert lifecycle._runs_duration(runs) == {"duration_seconds": 12.5, "duration_partial": True}


def test_a_two_run_attempt_with_every_run_measured_is_not_partial():
    runs = [{"run": 1, "duration_s": 12.5}, {"run": 2, "duration_s": 3.25}]
    assert lifecycle._runs_duration(runs) == {"duration_seconds": 15.75}


def test_an_attempt_with_no_measured_run_has_no_duration():
    runs = [{"run": 1, "duration_s": None}, {"run": 2}]
    assert lifecycle._runs_duration(runs) == {"duration_seconds": None, "duration_partial": True}


def test_a_swap_reread_with_unknown_seconds_is_skipped_and_marked():
    runs = [
        {"run": 1, "duration_s": 10.0},
        {"run": 2, "duration_s": 5.0, "after_swap": True,
         "reread": {"reached": True, "seconds": 4.5, "tokens": {"input_tokens": 7}}},
        {"run": 3, "duration_s": 6.0, "after_swap": True,
         "reread": {"reached": False, "seconds": None, "tokens": {"input_tokens": 3}}},
    ]
    cost = lifecycle._runs_summary(runs, [{"after_run": 1}, {"after_run": 2}])["swap_cost"]

    assert cost["reread_seconds"] == 4.5
    assert cost["partial"] is True
    assert cost["reread_tokens"] == {"input_tokens": 10}
    assert cost["measured_runs"] == 2


def test_a_swap_reread_with_every_seconds_known_is_not_marked():
    runs = [
        {"run": 1, "duration_s": 10.0},
        {"run": 2, "duration_s": 5.0, "after_swap": True,
         "reread": {"reached": True, "seconds": 4.5, "tokens": {}}},
    ]
    cost = lifecycle._runs_summary(runs, [{"after_run": 1}])["swap_cost"]

    assert cost["reread_seconds"] == 4.5
    assert "partial" not in cost
