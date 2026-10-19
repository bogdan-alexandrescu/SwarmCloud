"""Model list prices: a platform setting, and the stopped-attempt estimate made from it (#1481).

Owner decisions, 2026-10-17:

* **Prices are data, not code.** They are the `model_prices` key of the
  platform settings document (`control/settings`, `Store.get_platform_settings`),
  one entry per model: `input`, `output`, `cache_read`, `cache_write` (USD per
  million tokens), `currency`, `source`, `as_of` and `updated_by`. Changed
  through `PUT /v1/admin/settings` like every other platform setting, and read
  afresh on every estimate, so a change takes effect at once, with no deploy.
* **Nothing is invented.** A model with no entry prices nothing: the estimate
  is absent, and the attempt reads "cost not reported". A stored entry that is
  malformed is dropped, never repaired into a figure.
* **swarm-api owns the estimate.** Workers are moving off direct Firestore
  access (#1457, design B), so a worker whose CLI was stopped before it
  reported a cost sends the per-model usage its stream showed to
  `POST /v1/attempts/{attempt_id}/spend-estimate`, and swarm-api prices it
  here. The worker holds no price table.

THE SEED is the table `agent_worker.lifecycle._LIST_PRICES` held when it was
removed: every model in it, at exactly its figures, `source: code`, dated the
day those figures were last checked against the Claude API reference's model
table (2026-10-05). A cache WRITE was priced there at 1.25x input, the
five-minute write rate, and is written out here as that figure. It is what an
unset document reads as; the first PUT writes the whole effective table, so
the seed is never half-applied.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

#: The one currency an estimate is made in: it lands in the attempt's
#: `cost_usd`. A price in any other currency could not be added to it.
CURRENCY = "USD"
#: The per-million-token rates of an entry, in the order the seed lists them.
RATES = ("input", "output", "cache_read", "cache_write")
#: A model name as a provider spells it: `claude-opus-5-5`, `claude-haiku-4-5`.
MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")
#: `as_of`: the day a figure was read from its source.
AS_OF = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: The most models one table holds, and one estimate request names.
MAX_MODELS = 64
#: The highest rate accepted, USD per million tokens. Far above any list
#: price; it exists so a typo of a few orders of magnitude is refused.
MAX_RATE = 10_000.0

#: `claude-haiku-4-5-20251001`, `claude-opus-4-8[1m]`: the priced name, then
#: at most a date and a context marker. Anything else is not that model.
_MODEL_SUFFIX = re.compile(r"(?:-\d{8})?(?:\[[a-z0-9]+\])?")

#: The token counts an estimate prices, as the CLI's stream names them.
USAGE_COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _seed(inp: float, out: float, read: float) -> dict[str, Any]:
    return {
        "input": inp,
        "output": out,
        "cache_read": read,
        "cache_write": round(inp * 1.25, 10),
        "currency": CURRENCY,
        "source": "code",
        "as_of": "2026-10-05",
        "updated_by": "agent_worker.lifecycle._LIST_PRICES",
    }


#: Today's figures, exactly as `_LIST_PRICES` held them (module docstring).
#: Two that look wrong are not: `claude-mythos-5-1` is the Project Glasswing
#: counterpart of Fable 5.1, at Fable 5.1's price, and `claude-opus-5-5` is
#: priced BELOW Opus 5 ($4/$20).
SEED: dict[str, dict[str, Any]] = {
    "claude-fable-5-1": _seed(10.0, 50.0, 0.25),
    "claude-mythos-5-1": _seed(10.0, 50.0, 0.25),
    "claude-fable-5": _seed(10.0, 50.0, 1.0),
    "claude-opus-5-5": _seed(4.0, 20.0, 0.20),
    "claude-opus-5": _seed(5.0, 25.0, 0.50),
    "claude-opus-4-8": _seed(5.0, 25.0, 0.50),
    "claude-opus-4-7": _seed(5.0, 25.0, 0.50),
    "claude-opus-4-6": _seed(5.0, 25.0, 0.50),
    "claude-sonnet-5": _seed(2.0, 10.0, 0.20),
    "claude-sonnet-4-6": _seed(3.0, 15.0, 0.30),
    "claude-haiku-4-5": _seed(1.0, 5.0, 0.10),
}


def _rate(value: Any) -> float | None:
    """A rate as stored, or None: a bool, a string, NaN or a negative is no rate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= MAX_RATE:
        return None
    return value


def entry(raw: Any) -> dict[str, Any] | None:
    """One stored entry, normalised, or None when any part of it is malformed."""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for name in RATES:
        rate = _rate(raw.get(name))
        if rate is None:
            return None
        out[name] = rate
    if raw.get("currency") != CURRENCY:
        return None
    source, as_of, by = raw.get("source"), raw.get("as_of"), raw.get("updated_by")
    if not isinstance(source, str) or not 1 <= len(source) <= 64:
        return None
    if not isinstance(as_of, str) or not AS_OF.fullmatch(as_of):
        return None
    out.update(currency=CURRENCY, source=source, as_of=as_of,
               updated_by=by if isinstance(by, str) else None)
    return out


def table(stored: Any, *, present: bool) -> dict[str, dict[str, Any]]:
    """The effective price table: the seed when the setting was never written,
    else every well-formed entry stored. A malformed entry is DROPPED -- its
    model prices nothing -- never replaced by the seed's figure."""
    if not present:
        return {name: dict(price) for name, price in SEED.items()}
    if not isinstance(stored, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, raw in stored.items():
        if isinstance(name, str) and MODEL_NAME.fullmatch(name):
            parsed = entry(raw)
            if parsed is not None:
                out[name] = parsed
    return out


def price_for(model: Any, prices: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The entry that prices `model`: its own, or a dated or context-marked
    spelling of one (`claude-haiku-4-5-20251001`, `claude-opus-4-8[1m]`)."""
    if not isinstance(model, str):
        return None
    if model in prices:
        return prices[model]
    for name, price in prices.items():
        if model.startswith(name) and _MODEL_SUFFIX.fullmatch(model[len(name):]):
            return price
    return None


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def estimate_cost(
    by_model: Mapping[str, Mapping[str, Any]], prices: Mapping[str, Mapping[str, Any]]
) -> float | None:
    """USD for the usage of each model, or None when ANY model has no price.

    None is "cost not reported": a sum that left out a model's tokens would be
    a wrong figure presented as a right one, and a guessed price is invented.
    An empty usage map is None too -- nothing was reported to price.
    """
    if not by_model:
        return None
    cost = 0.0
    for model, usage in by_model.items():
        price = price_for(model, prices)
        if price is None:
            return None
        cost += (
            _count(usage, "input_tokens") * price["input"]
            + _count(usage, "output_tokens") * price["output"]
            + _count(usage, "cache_creation_input_tokens") * price["cache_write"]
            + _count(usage, "cache_read_input_tokens") * price["cache_read"]
        ) / 1_000_000
    return round(cost, 10)
