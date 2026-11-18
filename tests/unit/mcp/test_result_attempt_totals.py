"""`swarm_result` and the follow outcome say what the WHOLE task cost.

MEASURED, 2026-10-05 (lane review P1): both reported only the final attempt.
UR1's implement step served $0.51 while its two attempts cost $9.64; across 17
lanes $65.77 was served against $77.53 spent. Owner decision: the API serves
`cost_usd_total`, `attempts`, `duration_s_total` and `first_started_at` on the
task, and the bridge shows the total with the last attempt as secondary text.
A missing attempt cost is a floor (`cost_incomplete: true`), never a silent 0.

The real client against the real application over FakeFirestore, as the
follow tests run it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from swarm_mcp import compact, progress, server

from test_follow_cursor import NOW, TENANT, World, swarm, world  # noqa: F401 - fixtures


def _attempt(world: World, attempt_id: str, *, generation: int, started: Any, seconds: float,
             cost_usd: float | None) -> None:
    doc: dict[str, Any] = {
        "attempt_id": attempt_id,
        "task_id": "task_a",
        "tenant_id": TENANT,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": None,
        "created_at": started - timedelta(seconds=5),
        "started_at": started,
        "completed_at": started + timedelta(seconds=seconds),
        "exit_code": 0,
        "error": None,
        "peak_rss_bytes": None,
        "oom_near_miss": False,
        "checkpoints": [],
    }
    if cost_usd is not None:
        doc["cost_usd"] = cost_usd
    world.db.docs[f"attempts/{attempt_id}"] = doc


def _finished(world: World, *, first_cost: float | None, attempts: int = 2) -> None:
    """UR1's shape: an expensive first attempt, then a cheap one that finished."""
    world.task("task_a", state="SUCCEEDED")
    doc = world.db.docs["tasks/task_a"]
    doc["step_id"] = "implement"
    doc["attempt_count"] = attempts
    last_start = NOW - timedelta(minutes=10)
    # The task document holds the LAST attempt's start and end.
    doc["started_at"] = last_start
    doc["completed_at"] = last_start + timedelta(seconds=120)
    doc["result_summary"] = {"runner": {"summary": "done"}}
    if attempts == 2:
        _attempt(world, "att_1", generation=1, started=NOW - timedelta(minutes=60), seconds=1800,
                 cost_usd=first_cost)
    _attempt(world, f"att_{attempts}", generation=attempts, started=last_start, seconds=120, cost_usd=0.51)


def _result(client) -> dict[str, Any]:
    return json.loads(server._call(client, "swarm_result", {"task_id": "task_a"}))


# --------------------------------------------------------------------------
# swarm_result
# --------------------------------------------------------------------------


def test_swarm_result_totals_both_attempts_with_the_last_as_secondary(swarm, world):
    _finished(world, first_cost=9.13)

    got = _result(swarm)

    assert got["cost_usd_total"] == 9.64, "the task's cost is every attempt's, not the last one's $0.51"
    assert got["cost_incomplete"] is False
    assert got["attempts"] == 2
    assert got["duration_s_total"] == 1920.0
    assert got["first_started_at"].startswith("2026-09-22T11:00:00")
    assert (got["last_attempt_cost_usd"], got["last_attempt_duration_s"]) == (0.51, 120.0)
    assert got["cost"] == "$9.64 over 2 attempts (last attempt $0.51)"


def test_swarm_result_says_at_least_when_an_attempt_recorded_no_cost(swarm, world):
    _finished(world, first_cost=None)

    got = _result(swarm)

    assert got["cost_usd_total"] == 0.51
    assert got["cost_incomplete"] is True
    assert got["cost"].startswith("at least $0.51"), got["cost"]
    assert "1 of 2 attempts recorded no cost" in got["cost"]


def test_swarm_result_for_a_single_attempt_reads_as_before(swarm, world):
    _finished(world, first_cost=None, attempts=1)

    got = _result(swarm)

    assert (got["cost_usd_total"], got["attempts"], got["cost_incomplete"]) == (0.51, 1, False)
    assert got["cost"] == "$0.51", "one attempt has no 'last attempt' to set beside its total"


# --------------------------------------------------------------------------
# The follow outcome and the step result
# --------------------------------------------------------------------------


def test_the_follow_outcome_totals_both_attempts(swarm, world):
    _finished(world, first_cost=9.13)

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert got["cost_usd"] == 9.64, "`cost_usd` has always been documented as the sum"
    assert got["cost_usd_total"] == 9.64 and got["attempts"] == 2
    assert got["cost_incomplete"] is False
    assert got["last_attempt_cost_usd"] == 0.51
    # `duration_s` stays the last attempt's, now said so; the total is beside it.
    assert got["duration_s"] == 120.0
    assert got["duration_basis"] == "the last attempt's started_at to completed_at"
    assert got["duration_s_total"] == 1920.0
    assert got["first_started_at"].startswith("2026-09-22T11:00:00")


def test_the_follow_outcome_marks_a_missing_attempt_cost_as_a_floor(swarm, world):
    _finished(world, first_cost=None)

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert got["cost_usd"] == 0.51 and got["cost_incomplete"] is True
    assert "at least" in got["cost_note"], got["cost_note"]


def test_the_progress_outcome_carries_the_total_and_the_attempt_count(swarm, world):
    """`format: progress`'s slim outcome, which a workflow row reads."""
    _finished(world, first_cost=None)

    got = compact.watch_progress(swarm, ["task_a"])
    outcome = got["tasks"][0]["outcome"]

    assert outcome["cost_usd"] == 0.51
    assert outcome["cost_incomplete"] is True, "a floor must reach the row, not just the bridge"
    assert outcome["attempts"] == 2
    assert outcome["last_attempt_cost_usd"] == 0.51
    # The ready-made step result keeps its no-null rule and its total.
    result = compact.step_result(got["tasks"][0])
    assert result["cost_usd"] == 0.51


# --------------------------------------------------------------------------
# An API that serves no totals
# --------------------------------------------------------------------------


class _OldApi:
    """A deployment from before the totals: the task carries none of them."""

    def __init__(self, attempts: list[dict[str, Any]]) -> None:
        self._attempts = attempts

    def attempts(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:  # noqa: ARG002
        return self._attempts


def test_an_older_api_is_totalled_from_the_attempts_route():
    old = _OldApi([
        {"attempt_id": "a2", "generation": 2, "cost_usd": 0.51,
         "started_at": "2026-09-22T11:50:00Z", "completed_at": "2026-09-22T11:52:00Z"},
        {"attempt_id": "a1", "generation": 1, "cost_usd": None,
         "started_at": "2026-09-22T11:00:00Z", "completed_at": "2026-09-22T11:30:00Z"},
    ])

    got = progress.spend_totals(old, {"id": "task_a", "state": "SUCCEEDED"})

    assert got["attempts"] == 2
    assert got["cost_usd_total"] == 0.51 and got["cost_incomplete"] is True
    assert got["duration_s_total"] == 1920.0
    assert got["first_started_at"] == "2026-09-22T11:00:00Z"
    assert got["last_attempt_cost_usd"] == 0.51


def test_the_swarm_result_command_prints_the_total_and_the_last_attempt(swarm, world, capsys):
    import argparse

    from swarm_mcp import cli

    _finished(world, first_cost=9.13)

    cli.cmd_result(swarm, argparse.Namespace(task_id="task_a", json=False))

    lines = [ln.strip() for ln in capsys.readouterr().out.splitlines()]
    assert "cost    $9.64 over 2 attempts (last attempt $0.51)" in lines, lines
