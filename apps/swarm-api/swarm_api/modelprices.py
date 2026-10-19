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
* **A model priced in tiers keeps them** (PRICES-FOLLOWUP, owner 2026-10-19).
  Claude Haiku 5.5 is priced by prompt length: one set of rates for a prompt
  up to 100,000 tokens, another over it. Its entry carries `tiers`, a list of
  `{up_to_prompt_tokens, input, output, cache_read, cache_write}` in rising
  order, the last with `up_to_prompt_tokens: null` (no ceiling). The entry's
  own four rates are its FIRST tier's, so every reader that knows nothing of
  tiers -- an older console, the observed-rate check's issue text -- still
  reads a figure the provider lists. A model without `tiers` is unchanged.
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

#: The most tiers one entry holds. The page listed two on 2026-10-19.
MAX_TIERS = 8
#: The highest tier ceiling accepted, in prompt tokens: far past any context
#: window, so a typo of a few orders of magnitude is refused.
MAX_TIER_CEILING = 100_000_000
#: The key of a tier's ceiling: the largest prompt, in tokens, it prices.
#: None on the last tier, which has no ceiling.
UP_TO = "up_to_prompt_tokens"

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


def _rates_of(raw: Mapping[str, Any]) -> dict[str, float] | None:
    out: dict[str, float] = {}
    for name in RATES:
        rate = _rate(raw.get(name))
        if rate is None:
            return None
        out[name] = rate
    return out


def tiers_problem(raw: Any, rates: Mapping[str, float]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """`(tiers, None)` normalised, or `(None, why)` when `raw` is no tier list.

    A tier list is 2 to `MAX_TIERS` tiers, each its four rates and a ceiling
    (`UP_TO`): whole numbers of prompt tokens from 1 to `MAX_TIER_CEILING`,
    strictly rising, and None on the last tier alone, so every prompt size
    falls in exactly one tier. The first tier's rates are the entry's own
    (`rates`), the figures a reader that knows nothing of tiers reads. A
    missing ceiling is None: a request body dumped without its nulls still
    names the last tier.
    """
    if not isinstance(raw, (list, tuple)) or not 2 <= len(raw) <= MAX_TIERS:
        return None, f"tiers is a list of 2 to {MAX_TIERS} tiers"
    out: list[dict[str, Any]] = []
    for at, tier in enumerate(raw):
        if not isinstance(tier, Mapping):
            return None, f"tier {at} is not an object"
        unknown = sorted(set(tier) - {UP_TO, *RATES})
        if unknown:
            return None, f"tier {at} has unknown keys: {', '.join(map(str, unknown))}"
        tier_rates = _rates_of(tier)
        if tier_rates is None:
            return None, f"tier {at} needs each of {', '.join(RATES)} as a number from 0 to {MAX_RATE:g}"
        ceiling = tier.get(UP_TO)
        last = at == len(raw) - 1
        if last:
            if ceiling is not None:
                return None, f"the last tier has no ceiling: its {UP_TO} is null"
        elif isinstance(ceiling, bool) or not isinstance(ceiling, int) \
                or not 1 <= ceiling <= MAX_TIER_CEILING:
            return None, f"tier {at}'s {UP_TO} is a whole number from 1 to {MAX_TIER_CEILING}"
        elif out and ceiling <= out[-1][UP_TO]:
            return None, f"tier {at}'s {UP_TO} is above the tier before it"
        out.append({UP_TO: ceiling, **tier_rates})
    if any(out[0][name] != rates[name] for name in RATES):
        return None, "the entry's own rates are its first tier's"
    return out, None


def entry(raw: Any) -> dict[str, Any] | None:
    """One stored entry, normalised, or None when any part of it is malformed."""
    if not isinstance(raw, Mapping):
        return None
    rates = _rates_of(raw)
    if rates is None:
        return None
    out: dict[str, Any] = dict(rates)
    if raw.get("currency") != CURRENCY:
        return None
    source, as_of, by = raw.get("source"), raw.get("as_of"), raw.get("updated_by")
    if not isinstance(source, str) or not 1 <= len(source) <= 64:
        return None
    if not isinstance(as_of, str) or not AS_OF.fullmatch(as_of):
        return None
    out.update(currency=CURRENCY, source=source, as_of=as_of,
               updated_by=by if isinstance(by, str) else None)
    if raw.get("tiers") is not None:
        # A malformed tier list drops the WHOLE entry, as a malformed rate
        # does: its first tier's figure alone would under-price every prompt
        # past its ceiling.
        tiers, _why = tiers_problem(raw["tiers"], rates)
        if tiers is None:
            return None
        out["tiers"] = tiers
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


def prompt_tokens(usage: Mapping[str, Any]) -> int:
    """The prompt size a tier is picked by: every input token, cached or not.

    The provider counts a request's prompt as all of its input tokens,
    including cache reads and cache writes (pricing page, "Long context
    pricing"). An estimate is given each model's counts summed over the
    attempt, not per request, so this is the ATTEMPT's prompt size -- never
    smaller than its largest single request's. A tier picked by it is never
    cheaper than the dearest tier any of its requests paid: an estimate of a
    long attempt on a tiered model leans high, never low.
    """
    return (_count(usage, "input_tokens") + _count(usage, "cache_creation_input_tokens")
            + _count(usage, "cache_read_input_tokens"))


def rates_for(price: Mapping[str, Any], prompt: int) -> Mapping[str, Any]:
    """The rates `price` charges a prompt of `prompt` tokens: the first tier
    whose ceiling it does not pass (a prompt AT a ceiling is in that tier, as
    "up to 100,000 tokens" reads), or the entry's own when it has no tiers."""
    for tier in price.get("tiers") or ():
        if tier[UP_TO] is None or prompt <= tier[UP_TO]:
            return tier
    return price


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
        rates = rates_for(price, prompt_tokens(usage))
        cost += (
            _count(usage, "input_tokens") * rates["input"]
            + _count(usage, "output_tokens") * rates["output"]
            + _count(usage, "cache_creation_input_tokens") * rates["cache_write"]
            + _count(usage, "cache_read_input_tokens") * rates["cache_read"]
        ) / 1_000_000
    return round(cost, 10)
