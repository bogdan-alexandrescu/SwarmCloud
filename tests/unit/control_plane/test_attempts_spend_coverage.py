"""`GET /v1/attempts` `coverage`: what a row WITHOUT a cost means (#72).

`with_spend` over `attempts` said "cost measured on 2 of 7", and the other 5
could be four different facts: an attempt still running, a profile whose
runner never reports a cost, a row whose task is gone, and an attempt that
finished on a reporting profile and recorded nothing -- the SIGTERMed CLI run,
the expensive case the figure exists to expose. `coverage` now splits the rows
without a cost into those four, so the five counters PARTITION the page:

    with_spend + in_flight_unreported + not_reported_by_profile
               + not_recorded + profile_unknown == attempts

and `scope` says the counts are this page's, not the window's.

"Still running" is `attempt_is_over`'s judgement, not `completed_at is None`:
a hard-killed worker never writes `completed_at`, and on a terminal task, a
superseded generation or a released lease that row has ended -- counting it
as in flight would hide the spent-and-recorded-nothing case forever.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from swarm_common.models import Task, TaskState
from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.routes.attempts import COST_REPORTING_RUNNERS, profile_reports_cost

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

PARTITION = (
    "with_spend",
    "in_flight_unreported",
    "not_reported_by_profile",
    "not_recorded",
    "profile_unknown",
)


def _attempt(
    db,
    attempt_id: str,
    *,
    task_id: str,
    minutes_ago: float,
    finished: bool,
    cost_usd: float | None = None,
    generation: int = 1,
) -> None:
    created = NOW - timedelta(minutes=minutes_ago)
    doc: dict[str, Any] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": "eng",
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": None,
        "created_at": created,
        "started_at": created + timedelta(seconds=5),
        "completed_at": created + timedelta(minutes=1) if finished else None,
        "exit_code": 0 if finished else None,
        "error": None,
        "peak_rss_bytes": None,
        "oom_near_miss": False,
        "checkpoints": [],
    }
    # Absent, never zero, as `control.record_spend` writes it.
    if cost_usd is not None:
        doc["cost_usd"] = cost_usd
    db.docs[f"attempts/{attempt_id}"] = doc


def _task(db, task_id: str, profile: str, **input_: Any) -> None:
    seed_task(db, task_id=task_id, tenant_id="eng", runner_profile=profile)
    db.docs[f"tasks/{task_id}"]["input"] = dict(input_)


def _running(db, task_id: str, attempt_id: str, *, generation: int = 1) -> None:
    """The task holds `attempt_id`'s lease at its generation: that attempt is live."""
    doc = db.docs[f"tasks/{task_id}"]
    doc["state"] = "RUNNING"
    doc["current_lease_id"] = f"lease_{attempt_id}"
    doc["current_generation"] = generation


def _coverage(client, **params: Any) -> dict[str, Any]:
    response = client.get("/v1/attempts", params=params, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["coverage"]


def test_the_issue_page_of_seven_says_which_one_lost_its_cost(client, db):
    """#72's page, row for row. Only the SIGTERMed claude-code run is a gap."""
    seed_tenant(db, "eng")
    _task(db, "t_paid_1", "claude-code")
    _task(db, "t_paid_2", "claude-code")
    _task(db, "t_run_1", "claude-code")
    _task(db, "t_run_2", "codex")
    _task(db, "t_generic", "generic")
    _task(db, "t_sigterm", "claude-code")
    _task(db, "t_mock", "mock")
    _attempt(db, "a1", task_id="t_paid_1", minutes_ago=70, finished=True, cost_usd=0.42)
    _attempt(db, "a2", task_id="t_paid_2", minutes_ago=60, finished=True, cost_usd=1.10)
    _attempt(db, "a3", task_id="t_run_1", minutes_ago=50, finished=False)
    _attempt(db, "a4", task_id="t_run_2", minutes_ago=40, finished=False)
    _attempt(db, "a5", task_id="t_generic", minutes_ago=30, finished=True)
    _attempt(db, "a6", task_id="t_sigterm", minutes_ago=20, finished=True)
    _attempt(db, "a7", task_id="t_mock", minutes_ago=10, finished=True)
    _running(db, "t_run_1", "a3")
    _running(db, "t_run_2", "a4")

    assert _coverage(client, limit=7) == {
        "scope": "page",
        "attempts": 7,
        "finished": 5,
        "with_spend": 2,
        "in_flight_unreported": 2,
        "not_reported_by_profile": 2,
        "not_recorded": 1,
        "profile_unknown": 0,
    }


def test_a_running_attempt_that_already_reported_counts_as_measured(client, db):
    """`record_spend` runs before `completed_at` is written, so a row can carry
    a cost while still in flight. It is measured, not pending."""
    seed_tenant(db, "eng")
    _task(db, "t1", "claude-code")
    _attempt(db, "a1", task_id="t1", minutes_ago=5, finished=False, cost_usd=0.05)
    _running(db, "t1", "a1")

    coverage = _coverage(client)
    assert coverage["with_spend"] == 1
    assert coverage["in_flight_unreported"] == 0
    assert coverage["finished"] == 0


def test_a_mock_told_to_report_a_cost_that_recorded_none_is_a_gap(client, db):
    """The mock reports a cost only when its input carries one. Asked to and
    silent is the same gap a SIGTERMed CLI run is; not asked is by design."""
    seed_tenant(db, "eng")
    _task(db, "t_asked", "mock", spend={"usage": {"input_tokens": 10}, "total_cost_usd": 0.01})
    _task(db, "t_not_asked", "mock")
    _attempt(db, "a_asked", task_id="t_asked", minutes_ago=20, finished=True)
    _attempt(db, "a_not_asked", task_id="t_not_asked", minutes_ago=10, finished=True)

    coverage = _coverage(client)
    assert coverage["not_recorded"] == 1
    assert coverage["not_reported_by_profile"] == 1


def test_a_row_whose_task_is_gone_is_not_guessed(client, db):
    """No task, no profile: the API cannot say whether a cost was due, so the
    row is counted as unknown rather than as a gap or as by-design."""
    seed_tenant(db, "eng")
    _attempt(db, "a_orphan", task_id="t_deleted", minutes_ago=10, finished=True)
    _task(db, "t_renamed", "no-such-profile")
    _attempt(db, "a_renamed", task_id="t_renamed", minutes_ago=5, finished=True)

    coverage = _coverage(client)
    assert coverage["profile_unknown"] == 2
    assert coverage["not_recorded"] == 0


def test_the_five_counters_partition_every_page(client, db):
    seed_tenant(db, "eng")
    _task(db, "t_cc", "claude-code")
    _task(db, "t_gen", "generic")
    _task(db, "t_live_cc", "claude-code")
    _task(db, "t_live_gen", "generic")
    shapes = [
        ("t_cc", True, 0.3),
        ("t_cc", True, None),
        ("t_live_cc", False, None),
        ("t_gen", True, None),
        ("t_gone", True, None),
        ("t_cc", False, 0.0),
        ("t_live_gen", False, None),
    ]
    for i, (task_id, finished, cost) in enumerate(shapes):
        _attempt(db, f"a{i}", task_id=task_id, minutes_ago=100 - i, finished=finished, cost_usd=cost)
    _running(db, "t_live_cc", "a2")
    _running(db, "t_live_gen", "a6")

    token = None
    totals = dict.fromkeys(PARTITION, 0)
    for _ in range(10):
        params: dict[str, Any] = {"limit": 3}
        if token:
            params["page_token"] = token
        response = client.get("/v1/attempts", params=params, headers=auth_header("alice"))
        body = response.json()
        coverage = body["coverage"]
        assert coverage["scope"] == "page"
        assert coverage["attempts"] == len(body["attempts"])
        assert sum(coverage[name] for name in PARTITION) == coverage["attempts"], coverage
        # Ended is judged beyond `completed_at` (a5 has none and its task let
        # go of it), so `finished` is at least the rows that carry one.
        assert coverage["finished"] >= sum(1 for a in body["attempts"] if a["completed_at"])
        assert coverage["finished"] + coverage["in_flight_unreported"] <= coverage["attempts"]
        for name in PARTITION:
            totals[name] += coverage[name]
        token = body["next_page_token"]
        if token is None:
            break

    assert totals == {
        "with_spend": 2,
        "in_flight_unreported": 2,
        "not_reported_by_profile": 1,
        "not_recorded": 1,
        "profile_unknown": 1,
    }


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_a_hard_killed_attempt_on_a_terminal_task_is_a_gap_not_in_flight(client, db, terminal):
    """A worker OOM-killed, SIGKILLed after the grace period or lost with its
    node never writes `completed_at`. On a terminal task that attempt has
    ended, and on a reporting profile it spent and recorded nothing -- the case
    the figure exists to expose, which `completed_at is None` hid forever."""
    seed_tenant(db, "eng")
    _task(db, "t1", "claude-code")
    db.docs["tasks/t1"]["state"] = terminal
    db.docs["tasks/t1"]["current_generation"] = 1
    _attempt(db, "a1", task_id="t1", minutes_ago=30, finished=False)

    coverage = _coverage(client)
    assert coverage["in_flight_unreported"] == 0
    assert coverage["not_recorded"] == 1
    assert coverage["finished"] == 1


def test_a_superseded_attempt_without_completed_at_has_ended(client, db):
    """Generation 1 never wrote its end; generation 2 is now running. Only the
    newer attempt is in flight."""
    seed_tenant(db, "eng")
    _task(db, "t1", "codex")
    _attempt(db, "a_old", task_id="t1", minutes_ago=30, finished=False, generation=1)
    _attempt(db, "a_new", task_id="t1", minutes_ago=5, finished=False, generation=2)
    _running(db, "t1", "a_new", generation=2)

    coverage = _coverage(client)
    assert coverage["in_flight_unreported"] == 1
    assert coverage["not_recorded"] == 1


def test_an_attempt_whose_task_let_go_of_its_lease_has_ended(client, db):
    """The task was reclaimed back to READY after the attempt existed: nothing
    holds that lease, so nothing is running it."""
    seed_tenant(db, "eng")
    _task(db, "t1", "generic")
    _attempt(db, "a1", task_id="t1", minutes_ago=30, finished=False)
    db.docs["tasks/t1"]["current_generation"] = 1
    db.docs["tasks/t1"]["updated_at"] = NOW

    coverage = _coverage(client)
    assert coverage["in_flight_unreported"] == 0
    assert coverage["not_reported_by_profile"] == 1


def test_a_live_attempt_stays_in_flight(client, db):
    """The control for the three above: the task holds this attempt's lease at
    its generation, so it is in flight however long it has run."""
    seed_tenant(db, "eng")
    _task(db, "t1", "claude-code")
    _attempt(db, "a1", task_id="t1", minutes_ago=300, finished=False)
    _running(db, "t1", "a1")

    coverage = _coverage(client)
    assert coverage["in_flight_unreported"] == 1
    assert coverage["not_recorded"] == 0
    assert coverage["finished"] == 0


def test_a_row_whose_task_is_gone_has_ended_even_without_completed_at(client, db):
    """No task can hold its lease, so it is not in flight; with no profile to
    judge it by, it is unknown."""
    seed_tenant(db, "eng")
    _attempt(db, "a_orphan", task_id="t_deleted", minutes_ago=10, finished=False)

    coverage = _coverage(client)
    assert coverage["in_flight_unreported"] == 0
    assert coverage["profile_unknown"] == 1
    assert coverage["finished"] == 1


def test_every_cost_reporting_runner_is_a_real_runner_module():
    """`COST_REPORTING_RUNNERS` restates the worker outside the frozen
    catalogue (contract request 44); a renamed module must fail here, not
    quietly turn every claude-code row into "by design"."""
    root = Path(__file__).resolve().parents[3] / "apps" / "agent-worker"
    for module in sorted(COST_REPORTING_RUNNERS):
        assert (root / Path(*module.split("."))).with_suffix(".py").is_file(), module
    argv_tails = {p.runner_argv[-1] for p in RUNNER_PROFILES.values() if p.runner_argv}
    assert COST_REPORTING_RUNNERS <= argv_tails


def test_the_mock_is_the_only_declared_cost_profile():
    """`profile_reports_cost` reads `cost_declared` as the mock's "reports
    only when the input asks". A second declared profile must decide its own
    rule rather than inherit that one."""
    declared = sorted(name for name, p in RUNNER_PROFILES.items() if p.cost_declared)
    assert declared == ["mock"]


def _bare_task(profile: str, input_: dict[str, Any] | None = None) -> Task:
    return Task(
        id="t",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.SUCCEEDED,
        runner_profile=profile,
        resource_class="standard",
        input=input_ or {},
        submitted_by="seed@eng",
    )


@pytest.mark.parametrize(
    ("profile", "input_", "expected"),
    [
        ("claude-code", None, True),
        ("claude-code-review", None, True),
        ("codex", None, True),
        ("browser", None, False),
        ("generic", None, False),
        ("merge", None, False),
        ("post-verdict", None, False),
        ("mock", None, False),
        ("mock", {"spend": {"usage": {"input_tokens": 10}}}, False),
        ("mock", {"spend": {"total_cost_usd": 0.0}}, True),
        ("no-such-profile", None, None),
    ],
)
def test_which_profiles_owe_a_cost(profile, input_, expected):
    """Read off the catalogue's runner argv, so a new profile on the claude-code
    runner (as `claude-code-review` is) owes a cost the moment it exists."""
    assert profile_reports_cost(_bare_task(profile, input_)) is expected
