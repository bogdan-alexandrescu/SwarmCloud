"""BGD's four answers to COUNTS-TIME-VIEW's questions, as `GET /v1/outcomes` serves them (COUNTS-API-ADDS, part of #2149).

BGD, 2026-10-24 17:26Z, each a change to the `usage` block:

1. PER-BUCKET GROUP ROWS. Each grouping carries `series`: agents and retries
   per bucket for the five busiest keys of the WHOLE span (the same five in
   every bucket), one "all others" series for the rest, and every key that is
   not a measurement ("not recorded", "not ended", "unread") as a series of its
   own marked `measured: false`, so the view can keep it out of the stack
   (charts README). Every agent is in exactly one series, so the series of a
   bucket sum to its `agents`.
2. A COUNT OF ESTIMATED AGENTS. `cost.estimated` beside `cost.estimated_usd`,
   wherever that is served: "55 estimated from a stopped run ($56)".
3. EVERY GROUPING IN ONE READ. `usage.breakdowns` carries agent type, model,
   workspace and pool account from the one fold, and `group` is not in the
   cache key, so picking a breakdown never costs a read.
4. "HOLDS NO ACCOUNT". Under `pool_account`, an agent whose profile never
   takes a pool account -- or that was never admitted -- is its own key, apart
   from "not recorded", which stays for an agent that could have held one and
   whose events name none.

Offline: pure folds, FakeFirestore, a fixed clock.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_api import outcomes
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.outcomes import (
    HOLDS_NO_ACCOUNT,
    NOT_ENDED,
    NOT_RECORDED,
    OTHER_KEYS,
    USAGE_BREAKDOWNS,
    USAGE_SERIES_NAMED,
    arrival_from_doc,
    parse_params,
    tuple_from_docs,
)

from . import test_outcomes_route as route_tests
from .test_outcomes_counts_rollup import (
    NOW,
    SEVEN_DAYS,
    T_END,
    T_SUBMIT,
    _attempt,
    _fold,
    _task,
)

DOC = Path(__file__).resolve().parents[3] / "docs" / "outcomes.md"


def _agents(spec):
    """(id, profile, days before T_SUBMIT, attempts) -> ended tuples and arrivals."""
    ended, arrived = [], []
    for task_id, profile, back, attempts in spec:
        created = T_SUBMIT - timedelta(days=back)
        doc = _task(task_id, runner_profile=profile, created_at=created,
                    completed_at=T_END, attempt_count=attempts, current_generation=attempts)
        docs = [_attempt(g, cost=0.1) for g in range(1, attempts + 1)]
        ended.append(tuple_from_docs(doc, docs, {}, pool_account=None))
        arrived.append(arrival_from_doc(doc))
    return ended, arrived


def _series(folded, by):
    return {s["key"]: s for s in folded["usage"]["breakdowns"][by]["series"]}


# --------------------------------------------------------------------------
# 1. Per-bucket group rows
# --------------------------------------------------------------------------

def test_per_bucket_groups_sum_to_each_buckets_agents_and_keep_the_spans_five():
    # p1..p7 over three submission days. p1 is the busiest over the span but
    # absent from one day; p6 and p7 are the two quietest.
    spec = [
        ("a1", "p1", 0, 1), ("a2", "p1", 0, 2), ("a3", "p1", 1, 1), ("a4", "p1", 1, 1),
        ("b1", "p2", 0, 1), ("b2", "p2", 2, 3), ("b3", "p2", 2, 1),
        ("c1", "p3", 1, 1), ("c2", "p3", 2, 1),
        ("d1", "p4", 0, 1), ("d2", "p4", 1, 2),
        ("e1", "p5", 2, 1), ("e2", "p5", 0, 1),
        ("f1", "p6", 1, 1),
        ("g1", "p7", 2, 2),
    ]
    ended, arrived = _agents(spec)
    folded = _fold({"usage": "submitted"}, ended=ended, arrived=arrived)
    series = folded["usage"]["breakdowns"]["runner_profile"]["series"]
    named = [s["key"] for s in series if s["measured"] and not s["other"]]
    # MUTATION: pick each bucket's own top five and p1, absent on day 2, is not
    # named there; the series are one list for the span, so this is the span's.
    assert named == ["p1", "p2", "p3", "p4", "p5"]
    assert len(named) == USAGE_SERIES_NAMED
    other = [s for s in series if s["other"]]
    # MUTATION: drop the keys past the five and the buckets no longer sum.
    assert len(other) == 1 and other[0]["key"] == OTHER_KEYS and other[0]["keys"] == 2
    buckets = folded["usage"]["buckets"]
    for s in series:
        # MUTATION: give a series only the buckets it appears in and its length varies.
        assert len(s["agents"]) == len(buckets) == len(s["retries"])
    for i, bucket in enumerate(buckets):
        # MUTATION: count an agent in two series and a bucket's sum exceeds its agents.
        assert sum(s["agents"][i] for s in series) == bucket["agents"]
        assert sum(s["retries"][i] for s in series) == bucket["retries"]
    day0 = SEVEN_DAYS.index(T_SUBMIT.date())
    by_key = {s["key"]: s for s in series}
    # MUTATION: count attempts rather than retries and p1's day is 3.
    assert by_key["p1"]["agents"][day0] == 2 and by_key["p1"]["retries"][day0] == 1
    assert by_key["p1"]["agents"][day0 - 2] == 0
    assert other[0]["agents"][day0 - 1] == 1 and other[0]["retries"][day0 - 2] == 1
    # The requested group carries the same series.
    assert folded["usage"]["groups"]["series"] == series


def test_a_key_that_is_not_a_measurement_is_its_own_series_never_named_or_other():
    ended, arrived = _agents([(f"k{i}", "claude-code", 0, 1) for i in range(3)])
    open_doc = _task("open", runner_profile="claude-code", completed_at=None)
    folded = _fold(
        {"usage": "submitted"},
        ended=ended,
        arrived=[*arrived, arrival_from_doc(open_doc)],
    )
    series = _series(folded, "pool_account")
    # MUTATION: fold the unknowns into the named series and "not recorded" is measured.
    assert series[NOT_RECORDED]["measured"] is False and series[NOT_RECORDED]["other"] is False
    assert series[NOT_ENDED]["measured"] is False
    assert set(series) == {NOT_RECORDED, NOT_ENDED}
    day0 = SEVEN_DAYS.index(T_SUBMIT.date())
    assert series[NOT_RECORDED]["agents"][day0] == 3 and series[NOT_ENDED]["agents"][day0] == 1


# --------------------------------------------------------------------------
# 2. A count of estimated agents
# --------------------------------------------------------------------------

def test_cost_estimated_counts_the_agents_whose_cost_was_a_stopped_runs_estimate():
    docs = [
        _task("s1", state="CANCELLED", cancel_requested=True, result_summary={"cost_estimated": True}),
        _task("s2", state="CANCELLED", cancel_requested=True, result_summary={"cost_estimated": True}),
        _task("r"),
        _task("q"),
    ]
    costs = {"s1": 1.0, "s2": 0.5, "r": 0.25, "q": None}
    ended = [tuple_from_docs(d, [_attempt(1, cost=costs[d["id"]])], {}) for d in docs]
    folded = _fold({"usage": "submitted"}, ended=ended, arrived=[arrival_from_doc(d) for d in docs])
    usage = folded["usage"]
    bucket = usage["buckets"][SEVEN_DAYS.index(T_SUBMIT.date())]
    # MUTATION: count every reporting agent and this is 3; never serve it and it is a KeyError.
    assert bucket["cost"]["estimated"] == 2 and bucket["cost"]["estimated_usd"] == 1.5
    assert usage["totals"]["cost"]["estimated"] == 2
    rows = {r["key"]: r for r in usage["groups"]["rows"]}
    assert rows["claude-code"]["cost"]["estimated"] == 2
    empty = usage["buckets"][0]
    # MUTATION: serve null for a read bucket with no estimate and the view prints "null estimated".
    assert empty["cost"]["estimated"] == 0 and empty["cost"]["estimated_usd"] is None


# --------------------------------------------------------------------------
# 3. Every grouping in one read
# --------------------------------------------------------------------------

@pytest.fixture
def week(tokens, group_map):
    db = route_tests.FakeFirestore()
    route_tests.seed_week(db)
    ctx = route_tests._context(db, tokens, StaticGroups(group_map), route_tests.Clock())
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def test_group_is_not_part_of_the_cache_key():
    by_profile = parse_params({"tz": "UTC", "span": "7d", "group": "runner_profile"}, now=NOW)
    by_account = parse_params({"tz": "UTC", "span": "7d", "group": "pool_account"}, now=NOW)
    # MUTATION: keep `group` in `canonical()` and these differ.
    assert by_profile.canonical() == by_account.canonical()


def test_two_breakdowns_of_one_span_cost_one_read_and_agree_on_totals(week):
    first = route_tests.ok(week, "alice", **route_tests.WEEK, usage="submitted", group="runner_profile")
    assert first["cached"] is False and first["reads"] > 0
    second = route_tests.ok(week, "alice", **route_tests.WEEK, usage="submitted", group="pool_account")
    # MUTATION: key the cache by `group` and the second breakdown is a second scan.
    assert second["cached"] is True and second["reads"] == 0
    assert second["totals"] == first["totals"]
    assert second["usage"]["totals"] == first["usage"]["totals"]
    assert second["usage"]["breakdowns"] == first["usage"]["breakdowns"]
    assert set(first["usage"]["breakdowns"]) == set(USAGE_BREAKDOWNS)
    # The request's own group, not the fold's, is what each response names.
    assert first["filters"]["group"] == "runner_profile" and second["filters"]["group"] == "pool_account"
    assert first["groups"]["by"] == "runner_profile" and second["groups"]["by"] == "pool_account"
    assert second["usage"]["groups"]["by"] == "pool_account"
    assert second["usage"]["groups"]["rows"] == second["usage"]["breakdowns"]["pool_account"]["rows"]
    for name in USAGE_BREAKDOWNS:
        grouping = first["usage"]["breakdowns"][name]
        # MUTATION: lose an agent from a breakdown and it no longer agrees with the totals.
        assert sum(r["agents"] for r in grouping["rows"]) == first["usage"]["totals"]["agents"], name


# --------------------------------------------------------------------------
# 4. "holds no account"
# --------------------------------------------------------------------------

def test_a_mock_agent_holds_no_account_and_a_claude_agent_with_no_event_is_not_recorded():
    mock = _task("m", runner_profile="mock")
    claude = _task("c")
    browser = _task("b", runner_profile="browser")
    never_admitted = _task("n", state="CANCELLED", cancel_requested=True,
                           attempt_count=0, current_generation=0)
    on_pool = _task("p")
    ended = [
        tuple_from_docs(mock, [_attempt(1, cost=0.0)], {}),
        tuple_from_docs(claude, [_attempt(1, cost=0.1)], {}),
        tuple_from_docs(browser, [_attempt(1, cost=0.1)], {}),
        tuple_from_docs(never_admitted, [], {}),
        tuple_from_docs(on_pool, [_attempt(1, cost=0.1)], {}, pool_account="acct-claude-01"),
    ]
    open_mock = _task("om", runner_profile="mock", completed_at=None)
    docs = [mock, claude, browser, never_admitted, on_pool, open_mock]
    folded = _fold({"usage": "submitted", "group": "pool_account"},
                   ended=ended, arrived=[arrival_from_doc(d) for d in docs])
    rows = {r["key"]: r["agents"] for r in folded["usage"]["groups"]["rows"]}
    # MUTATION: key every agent with no account "not recorded" and mock, browser,
    # the never-admitted agent and the open mock agent are all in it.
    assert rows == {HOLDS_NO_ACCOUNT: 4, NOT_RECORDED: 1, "acct-claude-01": 1}
    series = _series(folded, "pool_account")
    # "holds no account" is a fact, so it stacks; "not recorded" is not.
    assert series[HOLDS_NO_ACCOUNT]["measured"] is True
    assert series[NOT_RECORDED]["measured"] is False
    # The outcome groups take the same key.
    timeline = {r["key"] for r in folded["groups"]["rows"]}
    assert timeline == {HOLDS_NO_ACCOUNT, NOT_RECORDED, "acct-claude-01"}


# --------------------------------------------------------------------------
# The doc says what the route serves
# --------------------------------------------------------------------------

def test_the_doc_names_every_field_this_lane_added():
    text = DOC.read_text(encoding="utf-8")
    for needle in (
        "`usage.breakdowns`", "`series`", "`measured`", "`cost.estimated`",
        f'"{HOLDS_NO_ACCOUNT}"', f'"{OTHER_KEYS}"', f"{USAGE_SERIES_NAMED} busiest",
        *(f"`{name}`" for name in USAGE_BREAKDOWNS),
    ):
        # MUTATION: ship the fields without documenting them and this fails.
        assert needle in text, needle
    # MUTATION: leave `group` in the doc's list of what keys the cache.
    assert "tz, filters, kind, group, compare" not in text
    assert outcomes.Params.canonical.__doc__ and "group" in outcomes.Params.canonical.__doc__
