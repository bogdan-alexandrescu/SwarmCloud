"""Model list prices are a platform setting, and swarm-api prices a stopped attempt from it (#1481).

Owner decisions, 2026-10-17: prices are DATA -- the `model_prices` platform
setting, served and set by `/v1/admin/settings` (PUT admin-only, validated,
stamped with who changed it) and read afresh on every estimate, so a change
takes effect at once. Nothing is invented: a model with no entry prices
nothing, "cost not reported". swarm-api owns the estimate: a worker sends the
per-model usage of a run stopped before it reported a cost to
`POST /v1/attempts/{attempt_id}/spend-estimate` and swarm-api prices it.

The seed is exactly what `agent_worker.lifecycle._LIST_PRICES` held, so the
first test pins that an Opus 5.5 attempt costs today's figure.

Offline: FakeFirestore, StaticTokenVerifier, an attempt key registered as a
worker registers one (`test_child_tasks_api`). No credentials, no emulator.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import modelprices

from .conftest import auth_header
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    _client,
    ready_parent,
    worker_tokens,
)

#: The stopped run `tests/unit/worker/test_cancelled_attempt_cost.py` makes:
#: 1200 in, 800 out, 2000 cache written, 22000 cache read.
USAGE = {"input_tokens": 1200, "output_tokens": 800,
         "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 22000}
#: claude-opus-5-5 at today's list prices: $4 in / $20 out / $0.20 cache read
#: per million, a cache write at 1.25x input ($5).
OPUS_TODAY = (1200 * 4 + 800 * 20 + 2000 * 5 + 22000 * 0.20) / 1_000_000


@pytest.fixture
def worker_client(db, worker_tokens, group_map, objects):  # noqa: F811
    return _client(db, worker_tokens, group_map, objects)


def _estimate(client, attempt: Attempt, by_model: dict[str, Any], *,
              token: str = WORKER_TOKEN, sign: bool = True):
    path = f"/v1/attempts/{attempt.tuple.attempt_id}/spend-estimate"
    body = json.dumps({**attempt.fields(), "task_id": attempt.tuple.task_id,
                       "usage_by_model": by_model}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sign:
        headers.update(attempt.sign("POST", path, body))
    return client.post(path, content=body, headers=headers)


def _entry(**overrides: Any) -> dict[str, Any]:
    entry = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25,
             "currency": "USD", "source": "console", "as_of": "2026-10-18"}
    entry.update(overrides)
    return entry


def _put(client, who: str, prices: dict[str, Any]):
    return client.put("/v1/admin/settings", headers=auth_header(who),
                      json={"model_prices": prices})


# --------------------------------------------------------------------------
# The estimate
# --------------------------------------------------------------------------


def test_an_opus_attempts_estimate_is_todays_figure(worker_client, db):
    attempt = Attempt()
    ready_parent(worker_client, db, attempt)

    response = _estimate(worker_client, attempt, {"claude-opus-5-5": USAGE})

    assert response.status_code == 200, response.text
    assert response.json()["cost_usd"] == pytest.approx(OPUS_TODAY)
    assert response.json()["unpriced_models"] == []
    # The same figure from the pure function, over the seeded table, and for
    # the context-marked spelling the CLI reports.
    seeded = modelprices.table(None, present=False)
    assert modelprices.estimate_cost({"claude-opus-5-5[1m]": USAGE}, seeded) == pytest.approx(
        OPUS_TODAY)


def test_a_model_with_no_entry_estimates_nothing(worker_client, db):
    attempt = Attempt()
    ready_parent(worker_client, db, attempt)

    alone = _estimate(worker_client, attempt, {"some-model-nobody-priced": USAGE})
    beside = _estimate(worker_client, attempt,
                       {"claude-opus-5-5": USAGE, "some-model-nobody-priced": USAGE})

    assert alone.status_code == 200, alone.text
    assert alone.json()["cost_usd"] is None, "a guessed price is a wrong figure"
    assert alone.json()["unpriced_models"] == ["some-model-nobody-priced"]
    # Not the priced model's share either: that would be a floor presented as the cost.
    assert beside.json()["cost_usd"] is None
    assert modelprices.estimate_cost({"claude-opus-5-5-preview": USAGE},
                                     modelprices.table(None, present=False)) is None


def test_an_admins_price_change_prices_the_next_estimate_with_no_deploy(client, db,
                                                                        worker_client):
    attempt = Attempt()
    ready_parent(worker_client, db, attempt)
    before = _estimate(worker_client, attempt, {"claude-haiku-4-5": USAGE}).json()["cost_usd"]

    response = _put(client, "root", {"claude-haiku-4-5": _entry(input=2.0)})

    assert response.status_code == 200, response.text
    haiku = response.json()["model_prices"]["claude-haiku-4-5"]
    assert haiku["input"] == 2.0 and haiku["source"] == "console"
    assert haiku["updated_by"] == "root@saga.xyz"
    assert response.json()["updated_by"] == "root@saga.xyz"
    # Every other seeded model is kept as it was.
    assert response.json()["model_prices"]["claude-opus-5-5"]["source"] == "code"
    after = _estimate(worker_client, attempt, {"claude-haiku-4-5": USAGE}).json()["cost_usd"]
    assert after == pytest.approx(before + 1200 * (2.0 - 1.0) / 1_000_000)


def test_the_setting_is_seeded_with_todays_figures_from_code(client):
    body = client.get("/v1/admin/settings", headers=auth_header("root")).json()

    opus = body["model_prices"]["claude-opus-5-5"]
    assert (opus["input"], opus["output"], opus["cache_read"], opus["cache_write"]) == (
        4.0, 20.0, 0.20, 5.0)
    assert opus["currency"] == "USD" and opus["source"] == "code"
    assert opus["as_of"] == "2026-10-05"


def test_a_malformed_stored_entry_prices_nothing():
    stored = {"claude-opus-5-5": _entry(input="4"), "claude-haiku-4-5": _entry()}

    prices = modelprices.table(stored, present=True)

    assert set(prices) == {"claude-haiku-4-5"}
    assert modelprices.estimate_cost({"claude-opus-5-5": USAGE}, prices) is None


def test_an_unproven_caller_is_refused_and_priced_nothing(worker_client, db):
    attempt = Attempt()
    ready_parent(worker_client, db, attempt)

    unsigned = _estimate(worker_client, attempt, {"claude-opus-5-5": USAGE}, sign=False)
    other = _estimate(worker_client, attempt, {"claude-opus-5-5": USAGE},
                      token=OTHER_WORKER_TOKEN)
    stranger = _estimate(worker_client, Attempt(task="task_x", attempt="att_x"),
                         {"claude-opus-5-5": USAGE})

    for response in (unsigned, other, stranger):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "child_submit_unproven"
        assert "cost_usd" not in response.json()


def test_a_worker_cannot_send_a_price(worker_client, db):
    attempt = Attempt()
    ready_parent(worker_client, db, attempt)

    response = _estimate(worker_client, attempt,
                         {"claude-opus-5-5": {**USAGE, "input_price": 0.0}})

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# PUT /v1/admin/settings
# --------------------------------------------------------------------------


def test_a_put_by_a_non_admin_is_refused(client):
    response = _put(client, "alice", {"claude-haiku-4-5": _entry(input=0.0)})

    assert response.status_code == 403
    body = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert body["model_prices"]["claude-haiku-4-5"]["input"] == 1.0
    assert body["settings_document"] == "missing"


@pytest.mark.parametrize("figure", [
    {"input": -1.0},
    {"output": -0.01},
    {"cache_read": "0.20"},
    {"cache_write": "free"},
    {"input": True},
    {"output": None},
    {"currency": "EUR"},
    {"as_of": "last week"},
])
def test_a_put_with_a_negative_or_non_numeric_figure_is_refused(client, figure):
    response = _put(client, "root", {"claude-haiku-4-5": _entry(**figure)})

    assert response.status_code == 422, response.text
    body = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert body["model_prices"]["claude-haiku-4-5"]["input"] == 1.0
    assert body["settings_document"] == "missing", "a refused change writes nothing"


def test_a_put_naming_a_malformed_model_is_refused(client):
    response = _put(client, "root", {"Claude Haiku!": _entry()})

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# PRICES-FOLLOWUP (part of #1481): everyone signed in reads the table, and a
# model priced in tiers by prompt length is held, served and priced by them.
# --------------------------------------------------------------------------

#: Haiku 5.5 as the pricing page listed it on 2026-10-19: one set of rates for
#: a prompt up to 100,000 tokens, another over it.
HAIKU_55_LOW = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}
HAIKU_55_HIGH = {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625}


def _tiered(**overrides: Any) -> dict[str, Any]:
    tiers = [{"up_to_prompt_tokens": 100_000, **HAIKU_55_LOW},
             {"up_to_prompt_tokens": None, **HAIKU_55_HIGH}]
    return _entry(**HAIKU_55_LOW, tiers=tiers, **overrides)


def _cost(usage: dict[str, int], rates: dict[str, float]) -> float:
    return (usage["input_tokens"] * rates["input"] + usage["output_tokens"] * rates["output"]
            + usage["cache_creation_input_tokens"] * rates["cache_write"]
            + usage["cache_read_input_tokens"] * rates["cache_read"]) / 1_000_000


def _prompt_of(size: int) -> dict[str, int]:
    """A usage whose prompt -- input, cache written and cache read -- is `size` tokens."""
    return {"input_tokens": size - 30_000, "output_tokens": 800,
            "cache_creation_input_tokens": 10_000, "cache_read_input_tokens": 20_000}


def test_a_non_admin_reads_the_price_table_and_still_cannot_write_it(client):
    read = client.get("/v1/settings/model-prices", headers=auth_header("alice"))

    assert read.status_code == 200, read.text
    opus = read.json()["model_prices"]["claude-opus-5-5"]
    assert (opus["input"], opus["output"], opus["cache_read"], opus["cache_write"]) == (
        4.0, 20.0, 0.20, 5.0)
    # The price table, not the settings document: no other setting is served.
    assert set(read.json()) == {"model_prices"}

    write = _put(client, "alice", {"claude-opus-5-5": _entry(input=0.0)})

    assert write.status_code == 403, write.text
    after = client.get("/v1/settings/model-prices", headers=auth_header("alice")).json()
    assert after["model_prices"]["claude-opus-5-5"]["input"] == 4.0


def test_the_price_table_is_not_served_to_a_caller_who_is_not_signed_in(client):
    assert client.get("/v1/settings/model-prices").status_code == 401


def test_a_tiered_model_is_stored_and_served_with_its_tiers(client):
    response = _put(client, "root", {"claude-haiku-5-5": _tiered()})

    assert response.status_code == 200, response.text
    served = client.get("/v1/settings/model-prices", headers=auth_header("alice")).json()
    haiku = served["model_prices"]["claude-haiku-5-5"]
    assert haiku["tiers"] == [{"up_to_prompt_tokens": 100_000, **HAIKU_55_LOW},
                              {"up_to_prompt_tokens": None, **HAIKU_55_HIGH}]
    assert haiku["input"] == HAIKU_55_LOW["input"], "the entry's own rates are its first tier's"
    # A model without tiers is served as it was, with no `tiers` key at all.
    assert "tiers" not in served["model_prices"]["claude-opus-5-5"]


def test_a_tiered_models_estimate_uses_the_tier_its_prompt_size_falls_in(worker_client, db, client):
    assert _put(client, "root", {"claude-haiku-5-5": _tiered()}).status_code == 200
    attempt = Attempt()
    ready_parent(worker_client, db, attempt)
    at, past = _prompt_of(100_000), _prompt_of(100_001)

    at_bound = _estimate(worker_client, attempt, {"claude-haiku-5-5": at})
    over = _estimate(worker_client, attempt, {"claude-haiku-5-5": past})

    assert at_bound.status_code == 200, at_bound.text
    # "Up to 100,000 tokens": a prompt of exactly 100,000 is the lower tier.
    assert at_bound.json()["cost_usd"] == pytest.approx(_cost(at, HAIKU_55_LOW))
    # One token over is the higher tier, for every token of it.
    assert over.json()["cost_usd"] == pytest.approx(_cost(past, HAIKU_55_HIGH))
    # The same through the pure function, for a dated spelling of the model.
    prices = {"claude-haiku-5-5": modelprices.entry(_tiered())}
    assert modelprices.estimate_cost({"claude-haiku-5-5-20261001": past}, prices) == pytest.approx(
        _cost(past, HAIKU_55_HIGH))


def test_an_untiered_models_estimate_is_unchanged_at_any_prompt_size():
    prices = modelprices.table(None, present=False)
    big = _prompt_of(900_000)

    assert "tiers" not in modelprices.entry(_entry())
    assert modelprices.estimate_cost({"claude-opus-5-5": big}, prices) == pytest.approx(
        _cost(big, {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}))
    assert modelprices.estimate_cost({"claude-opus-5-5": USAGE}, prices) == pytest.approx(OPUS_TODAY)


@pytest.mark.parametrize("tiers", [
    # One tier is not tiers.
    [{"up_to_prompt_tokens": None, **HAIKU_55_LOW}],
    # The last tier has a ceiling: a prompt past it would be priced by nothing.
    [{"up_to_prompt_tokens": 100_000, **HAIKU_55_LOW},
     {"up_to_prompt_tokens": 200_000, **HAIKU_55_HIGH}],
    # A tier before the last has none.
    [{"up_to_prompt_tokens": None, **HAIKU_55_LOW},
     {"up_to_prompt_tokens": None, **HAIKU_55_HIGH}],
    # Ceilings that do not rise.
    [{"up_to_prompt_tokens": 100_000, **HAIKU_55_LOW},
     {"up_to_prompt_tokens": 100_000, **HAIKU_55_LOW},
     {"up_to_prompt_tokens": None, **HAIKU_55_HIGH}],
    # A figure that is not one.
    [{"up_to_prompt_tokens": 100_000, **HAIKU_55_LOW},
     {"up_to_prompt_tokens": None, **HAIKU_55_HIGH, "output": "2.50"}],
    # The first tier is not the entry's own rates.
    [{"up_to_prompt_tokens": 100_000, **HAIKU_55_HIGH},
     {"up_to_prompt_tokens": None, **HAIKU_55_HIGH}],
])
def test_a_malformed_tier_list_is_refused_and_a_stored_one_prices_nothing(client, tiers):
    response = _put(client, "root", {"claude-haiku-5-5": _entry(**HAIKU_55_LOW, tiers=tiers)})

    assert response.status_code == 422, response.text
    body = client.get("/v1/settings/model-prices", headers=auth_header("root")).json()
    assert "claude-haiku-5-5" not in body["model_prices"]
    # Written straight to Firestore, it is dropped whole, never priced at its first tier.
    stored = modelprices.table({"claude-haiku-5-5": _entry(**HAIKU_55_LOW, tiers=tiers)},
                               present=True)
    assert stored == {}
