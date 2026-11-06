"""Jev spend through the state routes (lane AQ-JEV-ACCOUNTING, `swarm_api.jevaccount`).

Drives `state/jev-reserve`, `state/jev-usage` and `state/jev-settle` over the
in-memory Firestore, signed as a worker signs them (the helpers of
`test_attemptstate`). Each test names the rule it holds: usage through the
state routes is recorded; a park, a cancel and a crash settle; a replayed
record or settle charges nothing twice; two attempts cannot both reserve the
last of the month; no number from the worker raises a grant. Offline: no
credentials, no emulator.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_api import jevaccount

from .test_attemptstate import (  # noqa: F401 -- fixtures
    TASK,
    Worker,
    client,
    opened,
    worker_tokens,
)

TENANT = "eng"


def _ws(db, cap: int | None) -> None:
    db.docs[f"workspace_settings/{TENANT}"] = {
        "tenant_id": TENANT, "code_intelligence": {"jev": True, "jev_monthly_cap_tokens": cap}}


def _month(db) -> dict[str, Any] | None:
    for path, doc in db.docs.items():
        if path.startswith(f"jev_usage/{TENANT}_20") and "kind" not in doc:
            return doc
    return None


def _row(db, generation: int = 1, task: str = TASK) -> dict[str, Any]:
    return db.docs[f"jev_usage/{TENANT}_{task}_g{generation}"]


def _call(call_id: str | None = None, *, tokens: int = 3_000, outcome: str = "ok",
          kind: str = "J4", **extra: Any) -> dict[str, Any]:
    return {"call_id": call_id or secrets.token_hex(6), "source": "judgment", "kind": kind,
            "backend": "internal-jev", "requested_model": "jev-small",
            "served_model": "jev-small-2", "input_tokens": tokens, "output_tokens": None,
            "outcome": outcome, **extra}


def test_the_limits_are_the_workers():
    from agent_worker.judge import budget

    assert jevaccount.MAX_CALLS_PER_TASK == budget.MAX_CALLS_PER_TASK
    assert jevaccount.MAX_INPUT_TOKENS_PER_TASK == budget.MAX_INPUT_TOKENS_PER_TASK
    # MEASURED_TOKENS_PER_CALL is the shared package's own object:
    # tests/unit/jev_client/test_shared_client.py holds it.


def test_reserve_grants_the_task_allowance_bounded_by_the_month(client, db):
    worker = Worker()
    opened(client, db, worker)
    _ws(db, 50_000)
    answer = worker.post(client, "jev-reserve", {})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["granted_calls"], body["granted_tokens"]) == (jevaccount.MAX_CALLS_PER_TASK,
                                                               50_000)
    assert body["reason"] is None
    held = db.docs[f"jev_usage/{TENANT}_reservations"]["held"]
    assert held[f"{TASK}_g1"]["remaining_tokens"] == 50_000


def test_reserve_takes_no_number_from_the_worker(client, db):
    """Editing the ledger cannot raise authority: the body is the tuple alone."""
    worker = Worker()
    opened(client, db, worker)
    for extra in ({"granted_tokens": 10**9}, {"monthly_cap_tokens": 10**9},
                  {"month_used_tokens": 0}, {"max_calls": 99}):
        assert worker.post(client, "jev-reserve", extra).status_code == 422, extra


def test_two_attempts_cannot_both_reserve_the_last_of_the_month(client, db):
    first = Worker()
    opened(client, db, first)
    second = Worker(task="task_state_2", attempt="att_state_2", lease="lease_state_2")
    opened(client, db, second)
    _ws(db, 6_000)
    one = first.post(client, "jev-reserve", {}).json()
    two = second.post(client, "jev-reserve", {}).json()
    assert one["granted_tokens"] == 6_000
    assert (two["granted_calls"], two["granted_tokens"], two["reason"]) == \
        (0, 0, "budget_monthly")


def test_usage_through_the_state_routes_is_recorded_and_charged_once(client, db):
    worker = Worker()
    opened(client, db, worker)
    _ws(db, None)
    worker.post(client, "jev-reserve", {})
    record = _call("c1", tokens=3_100)
    first = worker.post(client, "jev-usage", record)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    replay = worker.post(client, "jev-usage", record)
    assert replay.json()["replayed"] is True
    row = _row(db)
    assert (row["calls"], row["input_tokens"]) == (1, 3_100)
    assert row["by_kind"]["J4"]["outcomes"] == {"ok": 1}
    assert row["by_kind"]["J4"]["output_tokens"] is None  # unknown, never 0
    assert _month(db)["input_tokens"] == 3_100
    call = db.docs[f"jev_usage/{TENANT}_{TASK}_g1_cc1"]
    assert (call["kind"], call["requested_model"], call["served_model"]) == \
        ("call", "jev-small", "jev-small-2")
    held = db.docs[f"jev_usage/{TENANT}_reservations"]["held"][f"{TASK}_g1"]
    assert held["remaining_tokens"] == jevaccount.MAX_INPUT_TOKENS_PER_TASK - 3_100


def test_a_skip_is_an_outcome_not_a_call(client, db):
    worker = Worker()
    opened(client, db, worker)
    worker.post(client, "jev-usage", _call(tokens=0, outcome="skipped:budget_calls"))
    row = _row(db)
    assert row["calls"] == 0
    assert row["by_kind"]["J4"]["outcomes"] == {"skipped:budget_calls": 1}


@pytest.mark.parametrize("outcome", ["parked", "cancelled", "crashed", "retried", "failed"])
def test_every_ending_settles_and_releases_the_reservation(client, db, outcome):
    worker = Worker()
    opened(client, db, worker)
    _ws(db, None)
    worker.post(client, "jev-reserve", {})
    worker.post(client, "jev-usage", _call(tokens=4_000))
    if outcome == "cancelled":
        db.docs[f"tasks/{TASK}"]["state"] = "CANCELLED"
        db.docs[f"tasks/{TASK}"]["current_generation"] = 1
    answer = worker.post(client, "jev-settle", {"outcome": outcome, "unrecorded_calls": 1,
                                                "unrecorded_tokens": 2_850})
    assert answer.status_code == 200, answer.text
    assert answer.json()["released_tokens"] == jevaccount.MAX_INPUT_TOKENS_PER_TASK - 4_000
    row = _row(db)
    assert row["settlement"]["outcome"] == outcome
    assert (row["calls"], row["input_tokens"]) == (2, 6_850)
    assert row["by_kind"]["unattributed"]["calls"] == 1
    assert db.docs[f"jev_usage/{TENANT}_reservations"]["held"] == {}
    replay = worker.post(client, "jev-settle", {"outcome": outcome, "unrecorded_calls": 1,
                                                "unrecorded_tokens": 2_850})
    assert replay.json()["replayed"] is True
    assert (_row(db)["calls"], _row(db)["input_tokens"]) == (2, 6_850)
    assert _month(db)["input_tokens"] == 6_850


def test_a_crashed_attempt_is_reconciled_by_the_next_reserve(client, db):
    """The dead attempt never settled: the retry's reserve releases its hold,
    marks it abandoned with its unreported spend unknown, and carries its
    recorded spend into the task's limits."""
    dead = Worker()
    opened(client, db, dead)
    _ws(db, None)
    dead.post(client, "jev-reserve", {})
    dead.post(client, "jev-usage", _call(tokens=10_000))
    retry = Worker(attempt="att_state_1b", lease="lease_state_1b", generation=2)
    opened(client, db, retry)
    answer = retry.post(client, "jev-reserve", {}).json()
    assert f"{TASK}_g1" in answer["released"]
    assert (answer["task_calls"], answer["task_input_tokens"]) == (1, 10_000)
    assert (answer["granted_calls"], answer["granted_tokens"]) == \
        (jevaccount.MAX_CALLS_PER_TASK - 1, jevaccount.MAX_INPUT_TOKENS_PER_TASK - 10_000)
    settlement = _row(db, 1)["settlement"]
    assert settlement["outcome"] == "abandoned"
    assert settlement["unreported_calls"] is None and settlement["unreported_tokens"] is None
    assert list(db.docs[f"jev_usage/{TENANT}_reservations"]["held"]) == [f"{TASK}_g2"]


def test_a_replayed_reserve_grants_nothing_more(client, db):
    worker = Worker()
    opened(client, db, worker)
    _ws(db, 9_000)
    one = worker.post(client, "jev-reserve", {}).json()
    two = worker.post(client, "jev-reserve", {}).json()
    assert two["replayed"] is True
    assert two["granted_tokens"] == one["granted_tokens"] == 9_000


def test_spend_past_the_grant_is_recorded_and_flagged(client, db):
    worker = Worker()
    opened(client, db, worker)
    _ws(db, 3_000)
    assert worker.post(client, "jev-reserve", {}).json()["granted_tokens"] == 3_000
    worker.post(client, "jev-usage", _call(tokens=2_900))
    over = worker.post(client, "jev-usage", _call(tokens=2_900)).json()
    assert over["over_grant"] is True
    row = _row(db)
    assert row["input_tokens"] == 5_800
    assert row["over_grant"]["input_tokens"] == 2_800


def test_the_cap_is_held_to_what_the_month_already_spent(client, db):
    worker = Worker()
    opened(client, db, worker)
    _ws(db, 10_000)
    from datetime import datetime, timezone

    db.docs[f"jev_usage/{TENANT}_{datetime.now(timezone.utc).strftime('%Y-%m')}"] = {
        "tenant_id": TENANT, "month": "x", "input_tokens": 8_000}
    body = worker.post(client, "jev-reserve", {}).json()
    assert (body["granted_tokens"], body["reason"]) == (0, "budget_monthly")


def test_another_tenants_worker_cannot_record(client, db):
    from .test_attemptstate import OTHER_WORKER

    worker = Worker()
    opened(client, db, worker)
    answer = worker.post(client, "jev-usage", _call(), token=OTHER_WORKER)
    assert answer.status_code == 401
