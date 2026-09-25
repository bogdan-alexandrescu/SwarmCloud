"""GET /v1/outcomes, the pure core: parameters, boundaries, the classifier, the fold.

Issue #185 (owner decisions, 2026-09-25) turns the Timeline into an outcome
ledger over a real span. The numbers it draws are only as right as four pure
pieces, each tested here with no store at all:

  * the RATE excludes every cancel, and a bucket with nothing decided has no
    rate -- never 0 %. Its interval is Wilson 95 %, checked against the
    contract's value (272 of 300 -> 0.9067, 0.8684-0.9346);
  * BUCKET BOUNDARIES are local wall-clock instants in the viewer's zone, so a
    day can be 23 h or 25 h, a fall-back hour appears twice with two offsets, a
    skipped hour does not appear, a midnight that does not exist starts the day
    at the first instant after it, and an ambiguous one at its first occurrence;
  * the CLASSIFIER puts every failure in exactly one fixed class, and exit 78 is
    the only exit code it trusts on its own;
  * the FOLD never reports a partial sum: a bucket touched by a tenant-day that
    could not be read carries no numbers, in either scope.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from swarm_api.errors import UpstreamUnavailable, ValidationFailed
from swarm_api.outcomes import (
    ENDED_COLS,
    VOCAB,
    DayResult,
    bucket_edges,
    cancel_cause,
    classify_failure,
    floor_boundary,
    fold,
    next_boundary,
    parse_params,
    percentile,
    platform_requested,
    previous_boundary,
    tuple_from_docs,
    wilson,
)

UTC = timezone.utc
#: 14:10:02 in Bucharest, the contract's worked example.
NOW = datetime(2026, 9, 25, 11, 10, 2, tzinfo=UTC)
BUCHAREST = ZoneInfo("Europe/Bucharest")


def _iso(moments, tz):
    return [m.astimezone(tz).isoformat() for m in moments]


def _refusal(raw, now=NOW) -> ValidationFailed:
    with pytest.raises(ValidationFailed) as caught:
        parse_params(raw, now=now)
    return caught.value


# --------------------------------------------------------------------------
# The rate and the percentiles
# --------------------------------------------------------------------------

def test_the_wilson_interval_is_the_contracts_check_value():
    assert wilson(272, 300) == {"k": 272, "n": 300, "p": 0.9067, "lo": 0.8684, "hi": 0.9346}


def test_a_small_sample_gets_a_wide_interval():
    assert wilson(9, 10) == {"k": 9, "n": 10, "p": 0.9, "lo": 0.5958, "hi": 0.9821}


def test_nothing_decided_is_no_rate_not_zero_percent():
    assert wilson(0, 0) is None


def test_the_interval_is_clamped_to_zero_and_one():
    none_succeeded = wilson(0, 5)
    assert none_succeeded["p"] == 0.0
    assert none_succeeded["lo"] == 0.0
    assert none_succeeded["hi"] == 0.4345
    assert wilson(5, 5)["hi"] == 1.0


def test_percentiles_are_exact_nearest_rank():
    twenty = [float(v) for v in range(1, 21)]
    assert percentile(twenty, 50) == 10.0
    # ceil(0.95 * 20) = 19, never 20 because a float came out 19.000000000000004.
    assert percentile(twenty, 95) == 19.0
    assert percentile([float(v) for v in range(1, 11)], 95) == 10.0
    assert percentile([5.0], 95) == 5.0


# --------------------------------------------------------------------------
# Boundaries, including every DST shape
# --------------------------------------------------------------------------

def test_the_fall_back_day_is_25_hours_long():
    since = floor_boundary(datetime(2026, 10, 24, 12, tzinfo=UTC), "day", BUCHAREST)
    edges = bucket_edges(since, datetime(2026, 10, 26, 12, tzinfo=UTC), "day", BUCHAREST)
    assert _iso(edges, BUCHAREST) == [
        "2026-10-24T00:00:00+03:00",
        "2026-10-25T00:00:00+03:00",
        "2026-10-26T00:00:00+02:00",
        "2026-10-27T00:00:00+02:00",
    ]
    assert edges[2] - edges[1] == timedelta(hours=25)


def test_the_spring_forward_day_is_23_hours_long():
    since = floor_boundary(datetime(2026, 3, 28, 12, tzinfo=UTC), "day", BUCHAREST)
    edges = bucket_edges(since, datetime(2026, 3, 30, 12, tzinfo=UTC), "day", BUCHAREST)
    assert _iso(edges, BUCHAREST) == [
        "2026-03-28T00:00:00+02:00",
        "2026-03-29T00:00:00+02:00",
        "2026-03-30T00:00:00+03:00",
        "2026-03-31T00:00:00+03:00",
    ]
    assert edges[2] - edges[1] == timedelta(hours=23)


def test_a_fall_back_hour_is_two_buckets_with_one_label_and_two_offsets():
    start = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # 02:00+03:00
    edges = bucket_edges(start, datetime(2026, 10, 25, 3, 0, tzinfo=UTC), "hour", BUCHAREST)
    assert _iso(edges[:-1], BUCHAREST) == [
        "2026-10-25T02:00:00+03:00",
        "2026-10-25T03:00:00+03:00",
        "2026-10-25T03:00:00+02:00",
        "2026-10-25T04:00:00+02:00",
    ]


def test_a_skipped_hour_has_no_bucket():
    start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)  # 02:00+02:00
    edges = bucket_edges(start, datetime(2026, 3, 29, 2, 0, tzinfo=UTC), "hour", BUCHAREST)
    assert _iso(edges[:-1], BUCHAREST) == [
        "2026-03-29T02:00:00+02:00",
        "2026-03-29T04:00:00+03:00",
    ]


def test_a_midnight_that_does_not_exist_starts_the_day_after_the_gap():
    """Santiago skips 00:00-01:00 on 6 Sep 2026."""
    santiago = ZoneInfo("America/Santiago")
    day = floor_boundary(datetime(2026, 9, 6, 12, tzinfo=UTC), "day", santiago)
    assert day.astimezone(santiago).isoformat() == "2026-09-06T01:00:00-03:00"
    following = next_boundary(day, "day", santiago)
    assert following.astimezone(santiago).isoformat() == "2026-09-07T00:00:00-03:00"
    assert following - day == timedelta(hours=23)
    assert day - previous_boundary(day, "day", santiago) == timedelta(hours=24)


def test_an_ambiguous_midnight_is_its_first_occurrence():
    """Havana's clocks go back from 01:00 to 00:00 on 1 Nov 2026."""
    havana = ZoneInfo("America/Havana")
    day = floor_boundary(datetime(2026, 11, 1, 12, tzinfo=UTC), "day", havana)
    assert day.astimezone(havana).isoformat() == "2026-11-01T00:00:00-04:00"
    assert next_boundary(day, "day", havana) - day == timedelta(hours=25)


def test_a_week_starts_on_monday_and_a_month_on_the_first():
    assert floor_boundary(NOW, "week", ZoneInfo("UTC")) == datetime(2026, 9, 21, tzinfo=UTC)
    assert next_boundary(datetime(2026, 1, 1, tzinfo=UTC), "month", ZoneInfo("UTC")) == datetime(
        2026, 2, 1, tzinfo=UTC
    )
    assert next_boundary(datetime(2026, 12, 1, tzinfo=UTC), "month", ZoneInfo("UTC")) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


# --------------------------------------------------------------------------
# Parameters: the server owns alignment
# --------------------------------------------------------------------------

def test_the_default_is_fourteen_local_days_to_now():
    params = parse_params({"tz": "Europe/Bucharest"}, now=NOW)
    assert params.requested == {"span": "14d", "since": None, "until": None, "bucket": "auto"}
    assert params.bucket == "day" and params.bucket_chosen_by == "server"
    assert params.since.astimezone(BUCHAREST).isoformat() == "2026-09-12T00:00:00+03:00"
    assert params.until == NOW
    assert len(params.edges) - 1 == 14
    assert params.scope == "tenant"


def test_24h_is_twenty_four_hourly_buckets_the_last_in_progress():
    params = parse_params({"tz": "UTC", "span": "24h"}, now=NOW)
    assert params.bucket == "hour"
    assert params.since == datetime(2026, 9, 24, 12, tzinfo=UTC)
    assert len(params.edges) - 1 == 24
    assert params.edges[-1] > NOW


def test_90d_is_weekly_from_a_monday():
    params = parse_params({"tz": "Europe/Bucharest", "span": "90d"}, now=NOW)
    assert params.bucket == "week"
    local = params.since.astimezone(BUCHAREST)
    assert local.weekday() == 0 and local.date() == date(2026, 6, 22)


def test_an_explicit_range_is_aligned_and_clamped_to_now():
    params = parse_params({"tz": "UTC", "since": "2026-09-12", "until": "2026-09-26"}, now=NOW)
    assert params.since == datetime(2026, 9, 12, tzinfo=UTC)
    assert params.until == NOW
    assert params.bucket == "day"
    assert len(params.edges) - 1 == 14
    assert params.requested == {
        "span": None, "since": "2026-09-12", "until": "2026-09-26", "bucket": "auto",
    }


def test_an_unencoded_plus_in_the_offset_is_read_as_a_plus():
    params = parse_params({"tz": "UTC", "since": "2026-09-12T00:00:00 03:00"}, now=NOW)
    assert params.since == datetime(2026, 9, 11, tzinfo=UTC)


def test_submitted_by_is_compared_lower_cased():
    params = parse_params({"tz": "UTC", "submitted_by": ["Alice@Saga.xyz"]}, now=NOW)
    assert params.submitted_by == ("alice@saga.xyz",)


def test_compare_previous_is_the_same_number_of_buckets_immediately_before():
    params = parse_params({"tz": "UTC", "span": "7d", "compare": "previous"}, now=NOW)
    assert params.since == datetime(2026, 9, 19, tzinfo=UTC)
    assert params.previous_edges[0] == datetime(2026, 9, 12, tzinfo=UTC)
    assert params.previous_edges[-1] == params.since
    assert len(params.previous_edges) == len(params.edges)


@pytest.mark.parametrize(
    ("raw", "parameter", "key", "value"),
    [
        ({}, "tz", "reason", "unknown_zone"),
        ({"tz": "Mars/Olympus_Mons"}, "tz", "reason", "unknown_zone"),
        ({"tz": "UTC", "span": "14d", "since": "2026-09-01"}, "span", "reason", "exclusive"),
        ({"tz": "UTC", "until": "2026-09-20"}, "until", "reason", "needs_since"),
        ({"tz": "UTC", "since": "2026-09-26"}, "since", "reason", "in_future"),
        ({"tz": "UTC", "since": "2026-09-12T00:00:00"}, "since", "reason", "needs_offset"),
        ({"tz": "UTC", "span": "30d", "bucket": "month"}, "bucket", "reason", "month_needs_60_days"),
        ({"tz": "UTC", "span": "90d", "bucket": "hour"}, "bucket", "max", 2000),
        ({"tz": "UTC", "scope": "tenant", "tenant": ["eng"]}, "scope", "reason", "conflicting_scope"),
        ({"tz": "UTC", "tenant": ["a"], "exclude_tenant": ["b"]}, "tenant", "reason", "exclusive"),
        ({"tz": "UTC", "group": "tenant_id"}, "group", "reason", "platform_only"),
    ],
)
def test_every_refusal_names_its_parameter(raw, parameter, key, value):
    refusal = _refusal(raw)
    assert refusal.status_code == 422
    assert refusal.detail["parameter"] == parameter, refusal.detail
    assert refusal.detail[key] == value, refusal.detail


def test_too_many_hourly_buckets_reports_how_many():
    refusal = _refusal({"tz": "UTC", "span": "90d", "bucket": "hour"})
    assert refusal.detail["value"] > 2000


def test_an_unknown_profile_is_refused_with_the_catalogue():
    refusal = _refusal({"tz": "UTC", "profile": ["nope"]})
    assert refusal.detail["parameter"] == "profile"
    assert "mock" in refusal.detail["known_runner_profiles"]


def test_a_disabled_profile_is_still_a_valid_filter():
    """codex is in the catalogue but disabled; its old runs are still history."""
    params = parse_params({"tz": "UTC", "profile": ["codex"]}, now=NOW)
    assert params.profile == ("codex",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"tz": "UTC"}, False),
        ({"scope": "tenant"}, False),
        ({"scope": "platform"}, True),
        ({"tenant": ["research"]}, True),
        ({"exclude_tenant": ["verify"]}, True),
        ({"group": "tenant_id"}, True),
        ({"group": "submitted_by"}, False),
    ],
)
def test_what_counts_as_asking_beyond_your_own_tenant(raw, expected):
    assert platform_requested(raw) is expected


def test_a_missing_time_zone_database_is_a_503_not_a_bad_zone(monkeypatch):
    import swarm_api.outcomes as outcomes

    monkeypatch.setattr(outcomes, "_tz_database_present", lambda: False)
    with pytest.raises(UpstreamUnavailable):
        parse_params({"tz": "Nowhere/Atall"}, now=NOW)


# --------------------------------------------------------------------------
# The classifier: one fixed class for every failure, in a fixed order
# --------------------------------------------------------------------------

def test_the_vocabulary_order_is_fixed():
    assert [c["key"] for c in VOCAB["failure_classes"]] == [
        "runner_error", "timeout", "lost_worker", "could_not_start",
        "outputs_missing", "dispatch_failed", "other", "no_reason",
    ]
    assert [c["key"] for c in VOCAB["cancel_causes"]] == [
        "requested", "after_failure", "workflow_sweep", "other",
    ]
    assert VOCAB["classifier_version"] == 1


@pytest.mark.parametrize(
    ("state", "last_error", "exit_code", "expected"),
    [
        ("FAILED", "anything at all", 78, "could_not_start"),
        ("FAILED", None, None, "no_reason"),
        ("FAILED", "   ", 1, "no_reason"),
        ("FAILED", "runner exceeded its 7200s timeout and was killed", -9, "timeout"),
        ("FAILED", "  runner exceeded its 60s timeout and was killed", None, "timeout"),
        ("FAILED", "worker could not start: bad config", None, "could_not_start"),
        ("FAILED", "worker exited 78: could not start (see execution logs: j)", None, "could_not_start"),
        ("FAILED", "reconciled: heartbeat silent for 300s", None, "lost_worker"),
        ("FAILED", "expected outputs missing (report.md).", 0, "outputs_missing"),
        ("FAILED", "gke_create_job_failed (attempt att_1)", None, "dispatch_failed"),
        ("FAILED", "Gke_create_job_failed (attempt att_1)", None, "other"),
        ("FAILED", "Traceback (most recent call last): ...", 1, "runner_error"),
        ("FAILED", "runner exited 0 without writing result.json", 0, "runner_error"),
        ("FAILED", "something nobody wrote a rule for", None, "other"),
        ("DEAD_LETTERED", None, None, "no_reason"),
        ("DEAD_LETTERED", "reconciled: lease expired", None, "lost_worker"),
        ("SUCCEEDED", "x", 1, None),
        ("CANCELLED", "x", 1, None),
    ],
)
def test_classify_failure(state, last_error, exit_code, expected):
    assert classify_failure(state, last_error, exit_code) == expected


def test_exit_76_is_not_trusted_to_mean_timeout():
    """No attempt ever records 76: a timeout records the killed CHILD's status."""
    assert classify_failure("FAILED", "the runner said goodbye", 76) == "runner_error"


@pytest.mark.parametrize(
    ("flag", "last_error", "expected"),
    [
        (True, None, "requested"),
        (True, "an upstream workflow step did not succeed", "requested"),
        (False, "cancelled on request; runner stopped on SIGTERM", "requested"),
        (False, "an upstream workflow step did not succeed", "after_failure"),
        (
            False,
            "workflow step synthesis is FAILED and on_step_failure is fail_workflow, "
            "so steps that had not started were cancelled",
            "workflow_sweep",
        ),
        (
            False,
            "workflow step a is DEAD_LETTERED and on_step_failure is fail_workflow, "
            "so steps that had not started were cancelled",
            "workflow_sweep",
        ),
        (False, "runner stopped on SIGTERM", "other"),
        (False, None, "other"),
    ],
)
def test_cancel_cause(flag, last_error, expected):
    assert cancel_cause(flag, last_error) == expected


# --------------------------------------------------------------------------
# The tuple
# --------------------------------------------------------------------------

T0 = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _task(**overrides):
    base = {
        "id": "t1",
        "tenant_id": "eng",
        "state": "FAILED",
        "created_at": T0,
        "completed_at": T0 + timedelta(hours=1),
        "runner_profile": "claude-code",
        "submitted_by": "Alice@saga.xyz",
        "workflow_id": "wf",
        "step_id": "b",
        "attempt_count": 3,
        "timeout_seconds": 600,
        "depends_on": ["p1"],
        "last_error": "gke_create_job_failed (attempt att_3)",
        "cancel_requested": False,
    }
    base.update(overrides)
    return base


ATTEMPTS = [
    {"generation": 3, "created_at": T0 + timedelta(minutes=30),
     "started_at": T0 + timedelta(minutes=31), "exit_code": None, "cost_usd": 0.0},
    {"generation": 1, "created_at": T0 + timedelta(minutes=5),
     "started_at": T0 + timedelta(minutes=6), "exit_code": 75, "cost_usd": 0.5},
    {"generation": 2, "created_at": T0 + timedelta(minutes=20),
     "started_at": None, "exit_code": None, "cost_usd": None},
]


def test_a_tuple_carries_what_the_fold_needs_and_nothing_else():
    t = tuple_from_docs(_task(), ATTEMPTS, {"p1": {"completed_at": T0 + timedelta(minutes=2)}})
    assert set(t) == set(ENDED_COLS)
    assert t["id"] == "t1" and t["state"] == "FAILED"
    assert t["created"] == _ms(T0)
    assert t["completed"] == _ms(T0 + timedelta(hours=1))
    # the earliest attempt START, never Task.started_at
    assert t["first_start"] == _ms(T0 + timedelta(minutes=6))
    # a step is eligible when its last parent finished
    assert t["eligible"] == _ms(T0 + timedelta(minutes=2))
    assert t["submitted_by"] == "alice@saga.xyz"
    assert t["failure_class"] == "dispatch_failed"
    assert t["cancel_cause"] is None
    # only attempts that STARTED can spend; $0.00 is a report
    assert t["att_docs"] == 3
    assert t["att_started"] == 2
    assert t["att_reporting"] == 2
    assert t["cost_usd"] == 0.5
    # retries: every attempt after the lowest generation
    assert t["retry_att_started"] == 1
    assert t["retry_att_reporting"] == 1
    assert t["retry_cost_usd"] == 0.0
    # every exit except the highest generation's, in generation order
    assert t["nonfinal_exits"] == [75, None]
    assert t["timeout_s"] == 600


def test_an_unreadable_parent_leaves_the_wait_unknown_rather_than_guessed():
    t = tuple_from_docs(_task(), ATTEMPTS, {"p1": None})
    assert t["eligible"] is None
    assert t["first_start"] is not None


def test_a_task_with_no_reported_cost_has_no_cost_not_zero():
    t = tuple_from_docs(
        _task(state="SUCCEEDED", depends_on=[]),
        [{"generation": 1, "created_at": T0, "started_at": T0, "exit_code": 0, "cost_usd": None}],
        {},
    )
    assert t["cost_usd"] is None
    assert t["att_started"] == 1 and t["att_reporting"] == 0
    assert t["eligible"] == t["created"]


# --------------------------------------------------------------------------
# The fold: an unread tenant-day empties its bucket, in either scope
# --------------------------------------------------------------------------

def _tuple(task_id: str, state: str, completed: datetime, **overrides):
    row = {col: None for col in ENDED_COLS}
    row.update(
        {
            "id": task_id,
            "state": state,
            "created": _ms(completed - timedelta(hours=1)),
            "completed": _ms(completed),
            "eligible": _ms(completed - timedelta(hours=1)),
            "profile": "mock",
            "submitted_by": "alice@saga.xyz",
            "attempt_count": 1,
            "att_docs": 1,
            "timeout_s": 600,
            "att_started": 0,
            "att_reporting": 0,
            "retry_att_started": 0,
            "retry_att_reporting": 0,
            "nonfinal_exits": [],
        }
    )
    if state == "CANCELLED":
        row["cancel_cause"] = "requested"
    if state in ("FAILED", "DEAD_LETTERED"):
        row["failure_class"] = "other"
    row.update(overrides)
    return row


SEVEN_DAYS = [date(2026, 9, 19) + timedelta(days=i) for i in range(7)]


def test_an_unread_day_is_a_bucket_with_no_numbers_not_zeros():
    params = parse_params({"tz": "UTC", "span": "7d"}, now=NOW)
    days = {("eng", d): DayResult("sealed") for d in SEVEN_DAYS}
    days[("eng", date(2026, 9, 21))] = DayResult(
        "sealed", ended=[_tuple("a", "SUCCEEDED", datetime(2026, 9, 21, 12, tzinfo=UTC))]
    )
    days[("eng", date(2026, 9, 22))] = DayResult("unread", "derive_budget")
    folded = fold(params=params, tenants=["eng"], days=days, main_days=SEVEN_DAYS, generated_at=NOW)

    unread = folded["buckets"][3]
    assert unread["state"] == "unread" and unread["unread_reason"] == "derive_budget"
    for key in ("submitted", "ended", "succeeded", "failed", "dead_lettered",
                "cancelled", "rate", "failure_classes", "cost"):
        assert unread[key] is None, key
    assert folded["buckets"][2]["succeeded"] == 1
    assert folded["buckets"][0]["succeeded"] == 0, "a day before any work is a measured zero"
    assert folded["buckets"][0]["rate"] is None, "and nothing decided there is no rate"
    assert folded["totals"]["complete"] is False
    assert folded["totals"]["buckets_read"] == 6
    assert folded["totals"]["succeeded"] == 1


def test_in_platform_scope_one_unread_tenant_empties_the_bucket_for_everyone():
    """Never a partial sum: eng's success on 21 Sep is not counted while
    research's 21 Sep could not be read."""
    params = parse_params({"tz": "UTC", "span": "7d", "scope": "platform"}, now=NOW)
    days = {(t, d): DayResult("sealed") for t in ("eng", "research") for d in SEVEN_DAYS}
    days[("eng", date(2026, 9, 21))] = DayResult(
        "sealed", ended=[_tuple("a", "SUCCEEDED", datetime(2026, 9, 21, 12, tzinfo=UTC))]
    )
    days[("research", date(2026, 9, 21))] = DayResult("unread", "read_failed")
    folded = fold(
        params=params, tenants=["eng", "research"], days=days, main_days=SEVEN_DAYS,
        generated_at=NOW,
    )
    assert folded["buckets"][2]["state"] == "unread"
    assert folded["buckets"][2]["unread_reason"] == "read_failed"
    assert folded["totals"]["succeeded"] == 0
    assert folded["totals"]["complete"] is False
    assert folded["groups"]["rows"] == [] or all(
        row["series"][2] is None for row in folded["groups"]["rows"]
    )


def test_the_rate_excludes_cancels_and_the_ended_count_does_not():
    params = parse_params({"tz": "UTC", "span": "7d"}, now=NOW)
    noon = datetime(2026, 9, 22, 12, tzinfo=UTC)
    ended = [_tuple("s", "SUCCEEDED", noon), _tuple("f", "FAILED", noon)]
    ended += [_tuple(f"c{i}", "CANCELLED", noon) for i in range(30)]
    days = {("eng", d): DayResult("sealed") for d in SEVEN_DAYS}
    days[("eng", date(2026, 9, 22))] = DayResult("sealed", ended=ended)
    folded = fold(params=params, tenants=["eng"], days=days, main_days=SEVEN_DAYS, generated_at=NOW)
    bucket = folded["buckets"][3]
    assert bucket["ended"] == 32
    assert bucket["cancelled"]["total"] == 30
    assert bucket["rate"]["k"] == 1 and bucket["rate"]["n"] == 2 and bucket["rate"]["p"] == 0.5


def test_a_reopened_task_is_counted_once_where_it_last_ended():
    params = parse_params({"tz": "UTC", "span": "7d"}, now=NOW)
    days = {("eng", d): DayResult("sealed") for d in SEVEN_DAYS}
    days[("eng", date(2026, 9, 21))] = DayResult(
        "sealed", ended=[_tuple("x", "FAILED", datetime(2026, 9, 21, 12, tzinfo=UTC))]
    )
    days[("eng", date(2026, 9, 23))] = DayResult(
        "sealed", ended=[_tuple("x", "SUCCEEDED", datetime(2026, 9, 23, 12, tzinfo=UTC))]
    )
    folded = fold(params=params, tenants=["eng"], days=days, main_days=SEVEN_DAYS, generated_at=NOW)
    assert folded["reopened"] == 1
    assert folded["buckets"][2]["failed"] == 0
    assert folded["buckets"][4]["succeeded"] == 1
    assert folded["totals"]["ended"] == 1
