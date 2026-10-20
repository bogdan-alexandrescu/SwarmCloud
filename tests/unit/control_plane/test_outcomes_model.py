"""The ledger's `model` and `model_class` groups (MODEL-LEDGER, part of #1435).

docs/design/model-per-step.md §4.4: once a step picks its model class, the
`runner_profile` group no longer separates tiers -- an opus step and a haiku
step on `claude-code` land in one row. These two groups are how cost by model
is read:

* `model` is keyed by `Attempt.model` over the attempts that STARTED. An
  attempt from before contract request 103, or on a runner with no model,
  records none, and is its own row, "not recorded" -- never folded into a
  real model's row and never dropped.
* `model_class` is keyed by the task's RESOLVED class
  (`profiles.step_model_class`): a step that named none ran its profile's
  default (R3: opus) and is counted there, not under a "none" row.

Pure: documents in, `tuple_from_docs` and `fold` out. No store, no clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from swarm_api.outcomes import (
    ARRIVED_COLS,
    ENDED_COLS,
    GROUPS,
    NO_MODEL_CLASS,
    NOT_RECORDED,
    NOT_STARTED,
    DayResult,
    _Fold,
    arrival_from_doc,
    fold,
    parse_params,
    tuple_from_docs,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
DAY = date(2026, 10, 17)
T0 = datetime(2026, 10, 17, 9, tzinfo=UTC)
SEVEN_DAYS = [DAY - timedelta(days=6 - i) for i in range(7)]


def _task(task_id: str, *, state: str = "SUCCEEDED", **overrides):
    base = {
        "id": task_id,
        "tenant_id": "eng",
        "state": state,
        "created_at": T0,
        "completed_at": T0 + timedelta(hours=1),
        "runner_profile": "claude-code",
        "submitted_by": "alice@saga.xyz",
        "workflow_id": "wf",
        "step_id": task_id,
        "attempt_count": 1,
        "timeout_seconds": 600,
        "depends_on": [],
        "last_error": None if state == "SUCCEEDED" else "runner exited 1",
        "cancel_requested": False,
    }
    base.update(overrides)
    return base


def _attempt(generation: int, *, model, cost, started: bool = True):
    attempt = {
        "generation": generation,
        "created_at": T0 + timedelta(minutes=generation),
        "started_at": T0 + timedelta(minutes=generation, seconds=5) if started else None,
        "exit_code": 0,
        "cost_usd": cost,
    }
    if model is not None:
        attempt["model"] = model
    return attempt


def _fold(group: str, ended, arrived=()):
    params = parse_params({"tz": "UTC", "span": "7d", "group": group}, now=NOW)
    days = {("eng", d): DayResult("sealed") for d in SEVEN_DAYS}
    days[("eng", DAY)] = DayResult("sealed", ended=list(ended), arrived=list(arrived))
    return fold(params=params, tenants=["eng"], days=days, main_days=SEVEN_DAYS, generated_at=NOW)


def _rows(folded):
    return {row["key"]: row for row in folded["groups"]["rows"]}


def test_both_groups_are_offered_and_their_columns_are_stored():
    assert "model" in GROUPS and "model_class" in GROUPS
    assert {"model", "model_spend", "model_class"} <= set(ENDED_COLS)
    assert "model_class" in ARRIVED_COLS


def test_the_model_group_separates_two_models_and_lists_not_recorded():
    opus = tuple_from_docs(
        _task("a"), [_attempt(1, model="claude-opus-5-5", cost=1.25)], {}
    )
    haiku = tuple_from_docs(
        _task("b", model_class="haiku", state="FAILED"),
        [_attempt(1, model="claude-haiku-5-5", cost=0.05)],
        {},
    )
    legacy = tuple_from_docs(_task("c"), [_attempt(1, model=None, cost=0.75)], {})
    rows = _rows(_fold("model", [opus, haiku, legacy]))

    assert set(rows) == {"claude-opus-5-5", "claude-haiku-5-5", NOT_RECORDED}
    assert NOT_RECORDED == "not recorded"
    assert rows["claude-opus-5-5"]["succeeded"] == 1 and rows["claude-opus-5-5"]["failed"] == 0
    assert rows["claude-haiku-5-5"]["failed"] == 1 and rows["claude-haiku-5-5"]["succeeded"] == 0
    assert rows["claude-opus-5-5"]["cost"]["sum_usd"] == 1.25
    assert rows["claude-haiku-5-5"]["cost"]["sum_usd"] == 0.05
    assert rows[NOT_RECORDED]["cost"]["sum_usd"] == 0.75
    assert rows[NOT_RECORDED]["succeeded"] == 1


def test_a_retry_on_another_pin_bills_each_model_its_own_attempts():
    """R6: a class's CURRENT pin runs, so a pin moved between two attempts
    puts the task's attempts on two models. Each model's row carries its own
    attempts' cost; the outcome is the final attempt's model's."""
    moved = tuple_from_docs(
        _task("a", attempt_count=2),
        [
            _attempt(1, model="claude-opus-5-5", cost=2.0),
            _attempt(2, model="claude-opus-5-6", cost=0.5),
        ],
        {},
    )
    rows = _rows(_fold("model", [moved]))

    assert rows["claude-opus-5-6"]["succeeded"] == 1
    assert rows["claude-opus-5-6"]["cost"]["sum_usd"] == 0.5
    assert rows["claude-opus-5-5"]["ended"] == 0
    assert rows["claude-opus-5-5"]["cost"]["sum_usd"] == 2.0
    assert rows["claude-opus-5-5"]["cost"]["attempts"] == 1


def test_a_task_no_attempt_of_which_started_is_not_started_not_a_model():
    never = tuple_from_docs(
        _task("a", state="CANCELLED", cancel_requested=True),
        [_attempt(1, model=None, cost=None, started=False)],
        {},
    )
    rows = _rows(_fold("model", [never]))
    assert set(rows) == {NOT_STARTED}


def test_the_model_group_counts_no_submissions_rather_than_guessing_them():
    """A task's model is known only once an attempt starts, so an arrival is
    in no model's row, and `submitted` is unknown rather than a zero."""
    ended = tuple_from_docs(_task("a"), [_attempt(1, model="claude-opus-5-5", cost=1.0)], {})
    arrived = arrival_from_doc(_task("a"))
    rows = _rows(_fold("model", [ended], [arrived]))
    assert set(rows) == {"claude-opus-5-5"}
    assert rows["claude-opus-5-5"]["submitted"] is None


def test_the_model_class_group_counts_a_defaulted_step_as_opus():
    defaulted = tuple_from_docs(
        _task("a"), [_attempt(1, model="claude-opus-5-5", cost=1.0)], {}
    )
    named = tuple_from_docs(
        _task("b", model_class="opus"), [_attempt(1, model="claude-opus-5-5", cost=1.0)], {}
    )
    haiku = tuple_from_docs(
        _task("c", model_class="haiku"), [_attempt(1, model="claude-haiku-5-5", cost=0.1)], {}
    )
    arrived = [arrival_from_doc(_task(i)) for i in ("a", "b")] + [
        arrival_from_doc(_task("c", model_class="haiku"))
    ]
    rows = _rows(_fold("model_class", [defaulted, named, haiku], arrived))

    assert set(rows) == {"opus", "haiku"}
    assert rows["opus"]["succeeded"] == 2 and rows["opus"]["submitted"] == 2
    assert rows["haiku"]["succeeded"] == 1 and rows["haiku"]["submitted"] == 1
    assert rows["opus"]["cost"]["sum_usd"] == 2.0


def test_a_profile_with_no_classes_is_its_own_row():
    mock = tuple_from_docs(_task("a", runner_profile="mock"), [_attempt(1, model=None, cost=0.0)], {})
    rows = _rows(_fold("model_class", [mock], [arrival_from_doc(_task("a", runner_profile="mock"))]))
    assert set(rows) == {NO_MODEL_CLASS}
    assert rows[NO_MODEL_CLASS]["submitted"] == 1


def test_a_stored_class_its_profile_does_not_accept_is_shown_as_stored():
    """Written past the API: the dispatcher refuses it, so it never ran as
    any class. The ledger shows what was stored rather than a class it did
    not run."""
    odd = tuple_from_docs(
        _task("a", runner_profile="mock", model_class="haiku", state="FAILED"), [], {}
    )
    rows = _rows(_fold("model_class", [odd]))
    assert set(rows) == {"haiku"}


def test_rows_for_one_model_list_that_models_tasks():
    opus = tuple_from_docs(_task("a"), [_attempt(1, model="claude-opus-5-5", cost=1.0)], {})
    legacy = tuple_from_docs(_task("b"), [_attempt(1, model=None, cost=1.0)], {})
    params = parse_params(
        {"tz": "UTC", "span": "7d", "group": "model", "rows": "succeeded", "rows_key": NOT_RECORDED},
        now=NOW,
    )
    days = {("eng", d): DayResult("sealed") for d in SEVEN_DAYS}
    days[("eng", DAY)] = DayResult("sealed", ended=[opus, legacy])
    made = _Fold(params=params, tenants=["eng"], days=days, main_days=SEVEN_DAYS, generated_at=NOW)
    assert [row["id"] for row in made.ended_rows(params)["rows"]] == ["b"]


def test_a_day_stored_before_the_model_columns_is_derived_again():
    """A day stored under DERIVE_VERSION 7 has no model columns: served as
    is, every task in it would read "not recorded". It is re-derived."""
    from swarm_api import outcomes

    base = {
        "derive_version": 7,
        "classifier_version": outcomes.CLASSIFIER_VERSION,
        "built_at": T0,
        "sealed": True,
        "ended": '{"cols":[],"rows":[]}',
        "arrived": '{"cols":[],"rows":[]}',
    }
    assert outcomes.DERIVE_VERSION == 8
    assert outcomes._decode_stored(base, []) is None
