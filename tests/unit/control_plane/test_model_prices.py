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
