"""The ledger's agent counts over time: tokens, run time, retries, estimated cost and pool account (COUNTS-ROLLUP, part of #2149).

BGD's decisions, 2026-10-23 (~19:30Z, #2149), as this file holds them:

* THE UNIT IS AGENTS, RETRIES APART. An agent is counted once, in the bucket
  of the day it was SUBMITTED (`created_at`), and its attempts beyond the
  first are a second figure in the same bucket.
* NULL IS NOT 0. A token sum no attempt reported is null, with how many
  attempts did report beside it; an attempt with no `completed_at` is "not
  measured", never 0 s.
* COST IS AN ESTIMATE AND SAYS SO. A stopped run's list-price estimate is in
  the sum AND in `estimated_usd`; "k of n agents reported" is `reporting` of
  `agents`.
* THE POOL ACCOUNT COMES FROM THE AGENT'S OWN EVENTS, read once when its day
  is derived; "not recorded" when the events hold none.
* THE TIMELINE IS UNTOUCHED: its parameters give its response, byte for byte
  apart from what every read varies (`reads`, `cached`); the new figures are
  only sent to a caller who asks for them with `usage`.

Offline: pure folds, FakeFirestore, a fixed clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from swarm_api import outcomes
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.outcomes import (
    NOT_RECORDED,
    DayResult,
    Outcomes,
    arrival_from_doc,
    fold,
    parse_params,
    tuple_from_docs,
)

from . import test_outcomes_route as route_tests
from .conftest import seed_task, seed_tenant
from .test_task_account import GroupFirestore, _event

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
TODAY = date(2026, 10, 23)
SEVEN_DAYS = [TODAY - timedelta(days=6 - i) for i in range(7)]
SUBMITTED_DAY = TODAY - timedelta(days=2)
ENDED_DAY = TODAY - timedelta(days=1)
T_SUBMIT = datetime.combine(SUBMITTED_DAY, datetime.min.time(), UTC) + timedelta(hours=9)
T_END = datetime.combine(ENDED_DAY, datetime.min.time(), UTC) + timedelta(hours=9)
TOKENS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _task(task_id: str, *, state: str = "SUCCEEDED", **overrides):
    base = {
        "id": task_id,
        "tenant_id": "eng",
        "state": state,
        "created_at": T_SUBMIT,
        "completed_at": T_END,
        "runner_profile": "claude-code",
        "submitted_by": "alice@example.com",
        "workflow_id": None,
        "step_id": None,
        "attempt_count": 1,
        "current_generation": 1,
        "timeout_seconds": 600,
        "depends_on": [],
        "last_error": None if state == "SUCCEEDED" else "runner exited 1",
        "cancel_requested": False,
    }
    base.update(overrides)
    return base


def _attempt(generation: int, *, cost=None, run_s: float | None = 60.0, **spend):
    started = T_SUBMIT + timedelta(hours=generation)
    attempt = {
        "generation": generation,
        "created_at": started - timedelta(seconds=5),
        "started_at": started,
        "completed_at": started + timedelta(seconds=run_s) if run_s is not None else None,
        "exit_code": 0,
        "cost_usd": cost,
    }
    attempt.update(spend)
    return attempt


def _fold(raw: dict, *, ended=(), arrived=()):
    params = parse_params({"tz": "UTC", "span": "7d", **raw}, now=NOW)
    days = {("eng", d): DayResult("sealed") for d in SEVEN_DAYS}
    by_day: dict[date, DayResult] = {}
    for t in ended:
        day = datetime.fromtimestamp(t["completed"] / 1000, UTC).date()
        by_day.setdefault(day, DayResult("sealed")).ended.append(t)
    for a in arrived:
        day = datetime.fromtimestamp(a["created"] / 1000, UTC).date()
        by_day.setdefault(day, DayResult("sealed")).arrived.append(a)
    days.update({("eng", d): r for d, r in by_day.items()})
    return fold(params=params, tenants=["eng"], days=days, main_days=SEVEN_DAYS, generated_at=NOW)


def _bucket(folded, day: date):
    return folded["usage"]["buckets"][SEVEN_DAYS.index(day)]


# --------------------------------------------------------------------------
# Agents, counted on their submission day, with retries apart
# --------------------------------------------------------------------------

def test_an_agent_with_two_attempts_is_one_agent_and_one_retry_on_its_submission_day():
    doc = _task("a", attempt_count=2, current_generation=2)
    ended = tuple_from_docs(doc, [_attempt(1, cost=1.0), _attempt(2, cost=0.5)], {})
    folded = _fold({"usage": "submitted"}, ended=[ended], arrived=[arrival_from_doc(doc)])

    usage = folded["usage"]
    # MUTATION: serve the usage block under the completion basis and this is "completed_at".
    assert usage["basis"] == "created_at"
    submitted = _bucket(folded, SUBMITTED_DAY)
    # MUTATION: count attempts instead of agents and this is 2.
    assert submitted["agents"] == 1
    # MUTATION: count retries as attempts (not attempts beyond the first) and this is 2.
    assert submitted["retries"] == 1
    # MUTATION: place the agent by its end and the submission day reads 0 and the end day 1.
    assert _bucket(folded, ENDED_DAY)["agents"] == 0
    # MUTATION: drop the join to the ended tuple and the agent reads as not ended.
    assert submitted["agents_ended"] == 1
    # MUTATION: sum the bucket's cost from the first attempt only and this is 1.0.
    assert submitted["cost"]["sum_usd"] == 1.5


def test_the_same_agent_under_the_end_basis_sits_on_its_end_day():
    doc = _task("a", attempt_count=2, current_generation=2)
    ended = tuple_from_docs(doc, [_attempt(1, cost=1.0), _attempt(2, cost=0.5)], {})
    folded = _fold({"usage": "ended"}, ended=[ended], arrived=[arrival_from_doc(doc)])
    # MUTATION: ignore the `usage` basis and the end basis is also placed by submission.
    assert folded["usage"]["basis"] == "completed_at"
    assert _bucket(folded, ENDED_DAY)["agents"] == 1
    assert _bucket(folded, SUBMITTED_DAY)["agents"] == 0


def test_an_agent_still_open_is_counted_submitted_and_reports_nothing():
    doc = _task("open", state="RUNNING", completed_at=None)
    folded = _fold({"usage": "submitted"}, arrived=[arrival_from_doc(doc)])
    row = _bucket(folded, SUBMITTED_DAY)
    # MUTATION: count only ended agents and the open one disappears.
    assert row["agents"] == 1 and row["agents_ended"] == 0
    # MUTATION: serve an empty sum as 0 and this is 0.0.
    assert row["cost"] == {
        "sum_usd": None, "estimated_usd": None, "estimated": 0, "agents": 1, "reporting": 0,
    }
    assert row["retries"] == 0


# --------------------------------------------------------------------------
# Tokens and run time: null-aware sums with their coverage
# --------------------------------------------------------------------------

def test_a_token_sum_with_one_attempt_not_reporting_is_null_aware_with_coverage_one_of_two():
    doc = _task("a", attempt_count=2, current_generation=2)
    reporting = _attempt(
        1, cost=0.4, input_tokens=100, output_tokens=20, cache_read_input_tokens=300,
        cache_creation_input_tokens=5,
    )
    silent = _attempt(2, cost=None)
    t = tuple_from_docs(doc, [reporting, silent], {})
    # MUTATION: treat a missing count as 0 and still sum -- same value; the coverage below catches it.
    assert t["input_tokens"] == 100 and t["cache_read_input_tokens"] == 300
    # MUTATION: count every attempt as reporting and this is [2, 2, 2, 2].
    assert t["token_reporting"] == [1, 1, 1, 1]

    folded = _fold({"usage": "submitted"}, ended=[t], arrived=[arrival_from_doc(doc)])
    tokens = _bucket(folded, SUBMITTED_DAY)["tokens"]
    # MUTATION: drop the coverage and "1 of 2" cannot be drawn.
    assert tokens["input_tokens"] == {"sum": 100, "reporting": 1, "attempts": 2}
    assert tokens["cache_creation_input_tokens"] == {"sum": 5, "reporting": 1, "attempts": 2}


def test_a_token_no_attempt_reported_is_null_never_zero():
    doc = _task("a")
    t = tuple_from_docs(doc, [_attempt(1, cost=None)], {})
    for name in TOKENS:
        # MUTATION: default a missing sum to 0 and this is 0.
        assert t[name] is None
    folded = _fold({"usage": "submitted"}, ended=[t], arrived=[arrival_from_doc(doc)])
    # MUTATION: start the bucket's sum at 0 and serve it and this is {"sum": 0, ...}.
    assert _bucket(folded, SUBMITTED_DAY)["tokens"]["output_tokens"] == {
        "sum": None, "reporting": 0, "attempts": 1,
    }


def test_a_token_count_that_is_not_a_count_is_not_reported():
    t = tuple_from_docs(_task("a"), [_attempt(1, input_tokens=True, output_tokens=-3)], {})
    # MUTATION: accept a bool as an int and input_tokens is 1.
    assert t["input_tokens"] is None and t["output_tokens"] is None


def test_an_attempt_with_no_completed_at_is_not_measured_rather_than_zero_seconds():
    doc = _task("a", attempt_count=2, current_generation=2)
    t = tuple_from_docs(doc, [_attempt(1, run_s=90.0), _attempt(2, run_s=None)], {})
    # MUTATION: count the open attempt as 0 s and run_measured is 2.
    assert t["run_s"] == 90.0 and t["run_measured"] == 1 and t["attempts"] == 2

    only_open = tuple_from_docs(_task("b"), [_attempt(1, run_s=None)], {})
    # MUTATION: serve an unmeasured run as 0.0 and this is 0.0.
    assert only_open["run_s"] is None and only_open["run_measured"] == 0

    folded = _fold(
        {"usage": "submitted"}, ended=[t, only_open],
        arrived=[arrival_from_doc(doc), arrival_from_doc(_task("b"))],
    )
    run = _bucket(folded, SUBMITTED_DAY)["run_s"]
    # MUTATION: count unmeasured attempts as measured and this reads 3 of 3.
    assert run == {"sum": 90.0, "measured": 1, "attempts": 3}


# --------------------------------------------------------------------------
# Cost: estimated apart, coverage by agents
# --------------------------------------------------------------------------

def test_a_stopped_run_estimate_lands_in_estimated_usd_and_in_the_sum():
    stopped = _task("s", state="CANCELLED", cancel_requested=True,
                    result_summary={"cost_estimated": True})
    reported = _task("r")
    silent = _task("q")
    ended = [
        tuple_from_docs(stopped, [_attempt(1, cost=1.25)], {}),
        tuple_from_docs(reported, [_attempt(1, cost=0.5)], {}),
        tuple_from_docs(silent, [_attempt(1, cost=None)], {}),
    ]
    # MUTATION: read `cost_estimated` off the attempt instead of the task's summary and this is False.
    assert ended[0]["cost_estimated"] is True and ended[1]["cost_estimated"] is False
    folded = _fold(
        {"usage": "submitted"}, ended=ended,
        arrived=[arrival_from_doc(d) for d in (stopped, reported, silent)],
    )
    cost = _bucket(folded, SUBMITTED_DAY)["cost"]
    # MUTATION: leave the estimate out of the sum and sum_usd is 0.5.
    assert cost["sum_usd"] == 1.75
    # MUTATION: never split the estimate out and estimated_usd is None.
    assert cost["estimated_usd"] == 1.25
    # MUTATION: count the silent agent as reporting and this is 3 of 3.
    assert (cost["reporting"], cost["agents"]) == (2, 3)
    assert folded["usage"]["cost_label"] == "Estimated cost (list price, not a bill)"


# --------------------------------------------------------------------------
# Pool account and provider
# --------------------------------------------------------------------------

def test_group_pool_account_groups_by_the_events_account_with_not_recorded_when_none():
    db = GroupFirestore()
    seed_tenant(db, "eng")
    for task_id in ("on_pool", "no_event"):
        doc = seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED",
                        runner_profile="claude-code", created_at=T_SUBMIT)
        doc.update(current_generation=1, attempt_count=1, completed_at=T_END)
        db.docs[f"attempts/att_{task_id}"] = {
            "attempt_id": f"att_{task_id}", "task_id": task_id, "tenant_id": "eng",
            "generation": 1, "created_at": T_SUBMIT, "started_at": T_SUBMIT,
            "completed_at": T_END, "exit_code": 0, "cost_usd": 0.25,
        }
    _event(db, "on_pool", "account_assigned", "acct-blue")
    # Another tenant's event for the same id is never read as this task's.
    _event(db, "no_event", "account_assigned", "acct-other", tenant="research")

    service = Outcomes(store=SimpleNamespace(db=db), rollups=None, now=lambda: NOW)
    derived = service.derive_day("eng", ENDED_DAY)
    accounts = {t["id"]: t["pool_account"] for t in derived.ended}
    # MUTATION: skip the events read and on_pool is "not recorded".
    assert accounts == {"on_pool": "acct-blue", "no_event": NOT_RECORDED}
    # MUTATION: count one events read per task instead of per 30 tasks and this is 2.
    assert len(db.group_queries) == 1

    arrived = [arrival_from_doc(db.docs[f"tasks/{i}"]) for i in ("on_pool", "no_event")]
    folded = _fold({"usage": "submitted", "group": "pool_account"},
                   ended=derived.ended, arrived=arrived)
    rows = {r["key"]: r for r in folded["usage"]["groups"]["rows"]}
    # MUTATION: key the usage rows by profile and this is {"claude-code"}.
    assert set(rows) == {"acct-blue", NOT_RECORDED}
    assert rows["acct-blue"]["agents"] == 1 and rows["acct-blue"]["cost"]["sum_usd"] == 0.25
    timeline = {r["key"] for r in folded["groups"]["rows"]}
    # MUTATION: leave `group=pool_account` out of the outcome groups and this is the profile.
    assert timeline == {"acct-blue", NOT_RECORDED}


def test_an_events_read_that_fails_fails_the_derive_rather_than_sealing_a_guess():
    db = GroupFirestore()
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="t", tenant_id="eng", state="SUCCEEDED",
                    runner_profile="claude-code", created_at=T_SUBMIT)
    doc.update(current_generation=1, completed_at=T_END)
    db.fail_next_group_query = 1
    service = Outcomes(store=SimpleNamespace(db=db), rollups=None, now=lambda: NOW)
    # MUTATION: swallow the failure (the list pages' rule) and the day seals "not recorded".
    with pytest.raises(Exception):
        service.derive_day("eng", ENDED_DAY)


def test_a_task_that_cannot_hold_an_account_costs_no_events_read():
    db = GroupFirestore()
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="m", tenant_id="eng", state="SUCCEEDED",
                    runner_profile="mock", created_at=T_SUBMIT)
    doc.update(current_generation=1, completed_at=T_END)
    service = Outcomes(store=SimpleNamespace(db=db), rollups=None, now=lambda: NOW)
    derived = service.derive_day("eng", ENDED_DAY)
    # MUTATION: read events for every task and this is 1.
    assert db.group_queries == []
    assert derived.ended[0]["pool_account"] == NOT_RECORDED


def test_group_provider_is_the_profiles_provider():
    ended = [
        tuple_from_docs(_task("c"), [_attempt(1, cost=0.1)], {}),
        tuple_from_docs(_task("m", runner_profile="mock"), [_attempt(1, cost=0.0)], {}),
    ]
    # MUTATION: store the profile name as the provider and this is "claude-code".
    assert ended[0]["provider"] == "anthropic"
    assert ended[1]["provider"] == outcomes.NO_PROVIDER
    folded = _fold({"usage": "ended", "group": "provider"}, ended=ended)
    assert {r["key"] for r in folded["usage"]["groups"]["rows"]} == {"anthropic", outcomes.NO_PROVIDER}


# --------------------------------------------------------------------------
# Re-derive, and the Timeline unchanged
# --------------------------------------------------------------------------

def test_a_day_stored_under_v8_is_derived_again_under_v9():
    base = {
        "derive_version": 8,
        "classifier_version": outcomes.CLASSIFIER_VERSION,
        "built_at": T_SUBMIT,
        "sealed": True,
        "ended": outcomes._encode_rows([], outcomes.ENDED_COLS),
        "arrived": outcomes._encode_rows([], outcomes.ARRIVED_COLS),
    }
    # MUTATION: leave DERIVE_VERSION at 8 and this is 8. (10 since #2322.)
    assert outcomes.DERIVE_VERSION >= 9
    # MUTATION: accept another derive version and the v8 day is served as is.
    assert outcomes._decode_stored(base, []) is None
    assert outcomes._decode_stored({**base, "derive_version": outcomes.DERIVE_VERSION}, []) is not None


def test_a_day_stored_under_v9_is_derived_again_under_v10():
    """#2322 BROWSER-USE-FLAG: the browser columns are new in version 10."""
    base = {
        "derive_version": 9,
        "classifier_version": outcomes.CLASSIFIER_VERSION,
        "built_at": T_SUBMIT,
        "sealed": True,
        "ended": outcomes._encode_rows([], outcomes.ENDED_COLS),
        "arrived": outcomes._encode_rows([], outcomes.ARRIVED_COLS),
    }
    # MUTATION: add the columns without the bump and a v9 day is served without them.
    # (11 since contract request 118's class.)
    assert outcomes.DERIVE_VERSION >= 10
    assert outcomes._decode_stored(base, []) is None
    assert outcomes._decode_stored({**base, "derive_version": outcomes.DERIVE_VERSION}, []) is not None
    for column in ("browser_s", "browser_used"):
        assert column in outcomes.ENDED_COLS
    for column in ("pool_account", "provider", "run_s", "attempts", "cost_estimated", *TOKENS):
        # MUTATION: drop a column from the tuple and a v9 day cannot hold it.
        assert column in outcomes.ENDED_COLS


def test_a_day_stored_under_v10_is_derived_again_under_v11():
    """Contract request 118 (BGD 2026-10-27): `budget_exceeded` is a class of
    its own. A day counted under 10 classed every budget stop as a runner
    error, so it is refused and re-derived."""
    base = {
        "derive_version": 10,
        "classifier_version": outcomes.CLASSIFIER_VERSION,
        "built_at": T_SUBMIT,
        "sealed": True,
        "ended": outcomes._encode_rows([], outcomes.ENDED_COLS),
        "arrived": outcomes._encode_rows([], outcomes.ARRIVED_COLS),
    }
    # MUTATION: add the class without the bump and a v10 day is served with
    # its budget stops still counted as runner errors. (12 since contract
    # request 126's class.)
    assert outcomes.DERIVE_VERSION >= 11
    assert "budget_exceeded" in outcomes.FAILURE_KEYS
    assert outcomes._decode_stored(base, []) is None
    assert outcomes._decode_stored({**base, "derive_version": outcomes.DERIVE_VERSION}, []) is not None


def test_a_day_stored_under_v11_is_derived_again_under_v12():
    """Contract request 126 (BGD 2026-10-27): `fix_loop_exhausted` is a class
    of its own. A day counted under 11 has no such key, so it is refused and
    re-derived."""
    base = {
        "derive_version": 11,
        "classifier_version": outcomes.CLASSIFIER_VERSION,
        "built_at": T_SUBMIT,
        "sealed": True,
        "ended": outcomes._encode_rows([], outcomes.ENDED_COLS),
        "arrived": outcomes._encode_rows([], outcomes.ARRIVED_COLS),
    }
    # MUTATION: add the class without the bump and a v11 day is served
    # without the key, its exhausted loops counted nowhere.
    assert outcomes.DERIVE_VERSION == 12
    assert "fix_loop_exhausted" in outcomes.FAILURE_KEYS
    assert outcomes._decode_stored(base, []) is None
    assert outcomes._decode_stored({**base, "derive_version": 12}, []) is not None


@pytest.fixture
def timeline(tokens, group_map):
    db = route_tests.FakeFirestore()
    route_tests.seed_week(db)
    ctx = route_tests._context(db, tokens, StaticGroups(group_map), route_tests.Clock())
    return TestClient(create_app(ctx), raise_server_exceptions=False)


#: The keys the Timeline's response carried under DERIVE_VERSION 8.
_TIMELINE_KEYS = {
    "scope", "tz", "requested", "since", "until", "bucket", "bucket_chosen_by", "basis",
    "filters", "vocab", "buckets", "totals", "retries", "latency", "groups",
    "workflows_failed", "coverage", "previous", "reads", "cached", "generated_at",
}
_BUCKET_KEYS = {
    "start", "end", "state", "in_progress", "unread_reason", "submitted", "ended", "succeeded",
    "failed", "dead_lettered", "cancelled", "rate", "failure_classes", "cost",
}
_GROUP_ROW_KEYS = {
    "key", "submitted", "ended", "succeeded", "failed", "dead_lettered", "cancelled", "rate",
    "cost", "declared_cost", "series",
}


def _stable(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in ("reads", "cached")}


def test_the_timelines_existing_response_is_unchanged_for_its_existing_parameters(timeline):
    query = {**route_tests.WEEK, "compare": "previous", "group": "submitted_by"}
    before = route_tests.ok(timeline, "alice", **query)
    # MUTATION: send `usage` with every response and the Timeline's keys grow.
    assert set(before) == _TIMELINE_KEYS
    assert set(before["buckets"][0]) == _BUCKET_KEYS
    assert set(before["groups"]["rows"][0]) == _GROUP_ROW_KEYS
    assert before["basis"] == {"outcomes": "completed_at", "submitted": "created_at"}
    assert before["filters"]["group"] == "submitted_by"

    with_usage = route_tests.ok(timeline, "alice", **query, usage="submitted")
    # MUTATION: let `usage` change any other figure and the two differ beyond it.
    assert _stable({k: v for k, v in with_usage.items() if k != "usage"}) == _stable(before)
    # MUTATION: not declare `usage` on the route and it never reaches the service.
    assert with_usage["usage"]["basis"] == "created_at"
    assert len(with_usage["usage"]["buckets"]) == len(before["buckets"])


def test_an_unknown_usage_basis_is_the_services_422(timeline):
    response = route_tests.get(timeline, "alice", **route_tests.WEEK, usage="weekly")
    # MUTATION: accept any `usage` value and this is 200.
    assert response.status_code == 422
    assert response.json()["detail"]["parameter"] == "usage"
