"""What an attempt used: its token counts and cost, summed across its runs.

Moved out of `lifecycle.py` unchanged (issue 68, the first slice of splitting
that file by concern). `lifecycle` binds only the names it or a test still
reads through it -- `_add_spend`, `_stream_usage`, `_unpriced_sessions`,
`_cost_incomplete` and `_usage_summary` (issue 193); read every other name
here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .runners.base import SPEND_KEYS
from .runners.cliagent import carries_session_totals
from .runners.ceiling import COST_INCOMPLETE_KEY as CEILING_COST_INCOMPLETE_KEY
from .runners.ceiling import SESSIONS_METRIC as CEILING_SESSIONS_METRIC
from .runners.ceiling import UNPRICED_KEY as CEILING_UNPRICED_KEY

#: The keys _usage_summary reads. Used to decide WHICH level of a possibly
#: nested result actually carries the numbers, rather than assuming one.
#:
#: Imported, not restated: the runner lifts exactly these out of a FAILED run
#: into result.json (runners/base.py), and a second copy here would be a key the
#: runner carries and this never reads.
_USAGE_KEYS = SPEND_KEYS

#: The fields of a usage summary that ADD UP across the runs of one attempt.
#: Everything `_usage_summary` emits except `models`, which is a set, and the
#: per-model rows and sub-agent share, which `_add_spend` merges by model.
_SUMMED_SPEND = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "thinking_tokens",
    "total_cost_usd",
    "num_turns",
    "duration_ms",
    "duration_api_ms",
)


#: The fields of a usage summary that are a CLI session's RUNNING TOTALS
#: (`cliagent.SESSION_SPEND_KEYS`, archived issue 667 observer P14): a runner that resumed
#: an earlier runner's session reports them for the whole session.
_SESSION_SPEND = ("total_cost_usd", "duration_api_ms")


def _add_spend(
    total: dict[str, Any],
    more: dict[str, Any],
    *,
    session_base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Two usage summaries as one: numbers summed, model lists unioned.

    `session_base` is given for a runner that RESUMED an earlier runner's
    session (`--resume`): `total` as it stood when that session began. Its
    `_SESSION_SPEND` figures are then the session's running totals, so the
    attempt's figure is what was spent before the session plus the later
    total -- not plus every figure the session reported along the way. A
    later total below the session's earlier one was not carried over
    (`cliagent.carries_session_totals`), and is added like any other.
    Tokens, turns and `duration_ms` are each invocation's own and always add.
    Per-model rows (`MODEL_USAGE_KEY`) add model by model, and are a session's
    running totals like its cost; the sub-agent share (`SUBAGENT_COST_KEY`)
    adds, and for a resumed session is read over the session as a whole.

    A key absent from BOTH stays absent. "Not reported" is not zero (see
    `control.record_spend`), and a sum must not turn the one into the other.
    """
    out = dict(total)
    session = False
    if session_base is not None:
        session_so_far = {
            key: total[key] - session_base.get(key, 0)
            for key in _SESSION_SPEND
            if key in total
        }
        session = carries_session_totals(session_so_far, more)
    for key in _SUMMED_SPEND:
        if key not in more:
            continue
        if session and session_base is not None and key in _SESSION_SPEND:
            out[key] = session_base.get(key, 0) + more[key]
        else:
            out[key] = out.get(key, 0) + more[key]
    if "total_cost_usd" in out:
        # Summing binary floats accumulates noise (0.1 + 0.2); no cost figure
        # means anything past the tenth decimal place of a dollar.
        out["total_cost_usd"] = round(out["total_cost_usd"], 10)
    models = set(out.get("models") or ()) | set(more.get("models") or ())
    if models:
        out["models"] = sorted(models)
    more_rows = more.get(MODEL_USAGE_KEY)
    if isinstance(more_rows, list) and more_rows:
        if session and session_base is not None:
            # `modelUsage` is a session running total like the cost
            # (`cliagent.SESSION_SPEND_KEYS`), so the resumed runner's rows
            # replace the session's earlier ones; and its sub-agent share is
            # read against the parent's tokens over the WHOLE session, since
            # `usage` is per invocation and the rows are not.
            out[MODEL_USAGE_KEY] = _add_model_usage(
                session_base.get(MODEL_USAGE_KEY) or [], more_rows)
            session_tokens = {
                key: out[key] - session_base.get(key, 0)
                for key, _ in _TOKEN_WEIGHTS
                if key in out
            }
            subagent = _subagent_cost_usd(
                session_tokens, more_rows, more.get("total_cost_usd"))
            if subagent is not None:
                out[SUBAGENT_COST_KEY] = round(
                    session_base.get(SUBAGENT_COST_KEY, 0) + subagent, 10)
            else:
                out.pop(SUBAGENT_COST_KEY, None)
                if SUBAGENT_COST_KEY in session_base:
                    out[SUBAGENT_COST_KEY] = session_base[SUBAGENT_COST_KEY]
        else:
            out[MODEL_USAGE_KEY] = _add_model_usage(out.get(MODEL_USAGE_KEY) or [], more_rows)
            if SUBAGENT_COST_KEY in more:
                out[SUBAGENT_COST_KEY] = round(
                    out.get(SUBAGENT_COST_KEY, 0) + more[SUBAGENT_COST_KEY], 10)
    return out


#: The `usage_by_model` key of a stream message that named no model. Not a
#: model name swarm-api can price (`modelprices.MODEL_NAME` refuses `<`), so
#: a run with one is "cost not reported", as it was when the worker priced.
_UNNAMED_MODEL = "<unnamed>"

#: The width, in prompt tokens, of the buckets a stopped run's requests are
#: counted in per model (`prompt_buckets`, PRICES-TIER-PROMPT, archived issue 1481). A model
#: priced in tiers is priced per REQUEST by that request's own prompt, and
#: the worker holds no price table, so it cannot split the counts at the tier
#: ceiling itself. It counts each request in bucket `ceil(prompt / width)`
#: instead: every request in one bucket falls on the same side of any ceiling
#: that is a multiple of the width, and swarm-api maps each bucket to its
#: tier (`modelprices.bucket_rates`). Every ceiling a provider lists today --
#: 100,000, 128,000, 200,000, 272,000 -- is a multiple of 1,000. MUST equal
#: `swarm_api.modelprices.PROMPT_BUCKET_TOKENS`; a test holds the two equal.
_PROMPT_BUCKET_TOKENS = 1_000

#: The most buckets one model's usage is sent in: a prompt sweep over a
#: million-token context, at one bucket per 1,000 tokens. A model past it is
#: sent as its sums alone, which swarm-api prices by the summed rule and marks
#: `tier_estimated` -- the body stays bounded, never refused.
_MAX_PROMPT_BUCKETS = 1_024

#: The most of a capture file read for an estimate. The capture is itself
#: capped (`max_stdout_bytes`), so this only bounds a misconfigured cap.
_ESTIMATE_READ_LIMIT = 64 * 1024 * 1024


def _stream_usage(path: Path) -> dict[str, Any]:
    """What a stopped CLI run used, from the per-message usage its stream carried.

    A CLI stopped on SIGTERM -- a cancel, a SIGTERM to the worker, a fence, a
    timeout -- prints no `result` event, and that event is the only place it
    reports `total_cost_usd`; every such attempt used to read "not reported"
    however long it had worked (archived issue 627: cancelled attempts priced 0 of 87). Each
    `assistant` event carries its message's `usage` and `model`, so the tokens
    are summed per message -- the LAST copy of each message id, because the
    stream repeats a message once per content block with the output count
    growing. The totals come back as the attempt's token counts, and
    `usage_by_model` holds the same counts per model the stream named, for
    swarm-api to price (`spendprice`, archived issue 1481): the worker prices nothing, so
    no `total_cost_usd` comes back from here.

    Each model's counts also carry `prompt_buckets` (PRICES-TIER-PROMPT): the
    same counts split by each request's OWN prompt size -- its input, cache
    written and cache read -- as `[bucket, input, output, cache written,
    cache read]` rows in rising bucket order, a request whose prompt is `p`
    tokens in bucket `ceil(p / _PROMPT_BUCKET_TOKENS)`. A provider prices a
    tiered model per request, so a long attempt of short requests is the
    lower tier for every one of them; the attempt's summed prompt is not the
    size any request had. The rows add up to the model's sums exactly.

    A FLOOR, not an exact figure: a capture past its cap keeps its head and
    tail, so a long run's middle is not counted. The caller marks a figure
    priced from it as an estimate. {} comes back when the stream carried no
    usage at all.

    The capture is in `artifacts/`, which the agent can write, so a link put
    in its place is refused at the open, not followed (as `_publish_tail`
    does). Never raises.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_ESTIMATE_READ_LIMIT)
    except OSError:
        return {}
    messages: dict[str, tuple[Any, dict[str, Any]]] = {}
    anonymous = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        key = message.get("id")
        if not isinstance(key, str) or not key:
            anonymous += 1
            key = f"\x00{anonymous}"
        messages[key] = (message.get("model"), message["usage"])
    if not messages:
        return {}
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    totals = dict.fromkeys(fields, 0)
    by_model: dict[str, dict[str, int]] = {}
    buckets: dict[str, dict[int, list[int]]] = {}
    for model, usage in messages.values():
        counts = {
            k: usage[k]
            if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and usage[k] >= 0
            else 0
            for k in fields
        }
        for k in fields:
            totals[k] += counts[k]
        # A message with no model name is counted in the totals and in a
        # model nobody prices, so swarm-api answers "not reported" for the
        # whole run rather than a figure that left those tokens out.
        name = model if isinstance(model, str) and model else _UNNAMED_MODEL
        mine = by_model.setdefault(name, dict.fromkeys(fields, 0))
        for k in fields:
            mine[k] += counts[k]
        prompt = (counts["input_tokens"] + counts["cache_creation_input_tokens"]
                  + counts["cache_read_input_tokens"])
        bucket = -(-prompt // _PROMPT_BUCKET_TOKENS)
        row = buckets.setdefault(name, {}).setdefault(bucket, [0, 0, 0, 0])
        for at, k in enumerate(_BUCKET_FIELDS):
            row[at] += counts[k]
    estimate: dict[str, Any] = dict(totals)
    models = sorted(m for m in by_model if m != _UNNAMED_MODEL)
    if models:
        estimate["models"] = models
    out: dict[str, dict[str, Any]] = {}
    for name, sums in by_model.items():
        out[name] = dict(sums)
        rows = buckets.get(name) or {}
        if len(rows) <= _MAX_PROMPT_BUCKETS:
            out[name]["prompt_buckets"] = [[b, *rows[b]] for b in sorted(rows)]
    estimate["usage_by_model"] = out
    return estimate


#: The counts of a `prompt_buckets` row, after its bucket, in this order.
_BUCKET_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _has_usage_keys(candidate: dict[str, Any]) -> bool:
    return any(key in candidate for key in _USAGE_KEYS)


def _unpriced_sessions(output: Any) -> dict[str, Any]:
    """The per-model counts of the sessions a context-ceiling handoff ended
    (`ceiling.UNPRICED_KEY`, archived issue 2896), from a runner's output or its nested
    `structured_output`, or {}."""
    if not isinstance(output, dict):
        return {}
    for source in (output, output.get("structured_output")):
        if isinstance(source, dict) and isinstance(source.get(CEILING_UNPRICED_KEY), dict):
            return {
                str(model): counts
                for model, counts in source[CEILING_UNPRICED_KEY].items()
                if isinstance(counts, dict)
            }
    return {}


def _cost_incomplete(result: Any) -> bool:
    """Whether a runner's result says a session's usage was not read: the
    spend's `ceiling.COST_INCOMPLETE_KEY`, on its output or nested
    `structured_output`, or `metrics.sessions.cost_complete` false
    (COST-ACROSS-SESSIONS, part of archived issue 2688)."""
    if not isinstance(result, dict):
        return False
    output = result.get("output")
    if isinstance(output, dict):
        for source in (output, output.get("structured_output")):
            if isinstance(source, dict) and source.get(CEILING_COST_INCOMPLETE_KEY) is True:
                return True
    for holder in (result, output):
        metrics = holder.get("metrics") if isinstance(holder, dict) else None
        sessions = metrics.get(CEILING_SESSIONS_METRIC) if isinstance(metrics, dict) else None
        if isinstance(sessions, dict) and sessions.get("cost_complete") is False:
            return True
    return False


def _usage_summary(output: Any) -> dict[str, Any]:
    """Token and cost numbers, pulled out of a CLI agent's result BEFORE truncation.

    `_truncate_json` replaces the whole result with a preview STRING once it
    exceeds its limit, so the runs that consumed the most tokens were exactly the
    runs whose token counts were discarded. The raw file still reaches GCS, but
    nothing queryable kept the numbers, and a per-tenant spend figure assembled by
    reading one GCS object per attempt does not survive real volume.

    This is deliberately a small, flat dict of scalars: it stays far below any
    truncation limit, so it survives whatever the rest of the result does.

    It does NOT add a field to `Attempt` -- `apps/common/swarm_common/` is frozen.
    These land inside the free-form runner summary. A typed field is a contract
    change request; see docs/contract-change-requests.md.
    """
    if not isinstance(output, dict):
        return {}

    # WHERE THE NUMBERS ACTUALLY ARE.
    #
    # A CLI runner does not return the agent's JSON. It returns its own
    # envelope -- {summary, provider, model, exit_code, structured_output,
    # limits, metrics} (runners/cliagent.py:342-357) -- and the CLI's own JSON,
    # which is where `usage`, `total_cost_usd`, `num_turns` and `modelUsage`
    # live, is nested one level down under `structured_output`.
    #
    # This function read only the top level, so on the production path it found
    # none of those keys and returned {} for every attempt -- while its unit
    # test, which feeds the raw CLI shape, passed. A test exercising a shape
    # production never produces is worse than no test: it reports a working
    # extractor when there is none. The test now also feeds the real envelope.
    #
    # Preferring whichever level actually carries the keys, rather than always
    # descending, keeps this correct for a runner that returns the CLI JSON
    # directly and for one that wraps it.
    source = output
    if not _has_usage_keys(source):
        nested = output.get("structured_output")
        if isinstance(nested, dict) and _has_usage_keys(nested):
            source = nested

    summary: dict[str, Any] = {}

    usage = source.get("usage")
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                summary[key] = value
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), int):
            summary["thinking_tokens"] = details["thinking_tokens"]

    # bool is a subclass of int, so it is excluded explicitly -- `is_error: true`
    # arriving as a cost of 1 would be a quietly wrong number, which is the whole
    # class of bug this file is being edited to avoid.
    for key in ("total_cost_usd", "num_turns", "duration_ms", "duration_api_ms"):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value

    models = source.get("modelUsage")
    if isinstance(models, dict) and models:
        summary["models"] = sorted(str(m) for m in models)
        # SUBAGENT-SPEND (part of archived issue 2524, item 8): each model's own counts and
        # cost, not only the list of names. `usage` above is the parent
        # agent's alone; `modelUsage` is every call the session paid for,
        # sub-agents and the CLI's own housekeeping included.
        rows = _model_usage_rows(models)
        if rows:
            summary[MODEL_USAGE_KEY] = rows
            subagent = _subagent_cost_usd(summary, rows, summary.get("total_cost_usd"))
            if subagent is not None:
                summary[SUBAGENT_COST_KEY] = subagent

    return summary


#: SUBAGENT-SPEND (part of archived issue 2524, item 8; the owner, 2026-10-27). The usage summary's
#: per-model rows -- `[{model, input_tokens, output_tokens,
#: cache_read_input_tokens, cache_creation_input_tokens, cost_usd}]`, sorted by
#: model -- and the share of `total_cost_usd` the parent agent's own token
#: counts do not explain. Both land in `result_summary.runner.usage`, which
#: swarm-api serves as written; neither is an `Attempt` field
#: (`control.record_spend` writes only the frozen columns). Measured over
#: 2026-10-20..27: about 11% of cost had no token count behind it, because
#: `usage` counts the parent alone while `total_cost_usd` pays for every
#: sub-agent the parent started.
MODEL_USAGE_KEY = "model_usage"
SUBAGENT_COST_KEY = "subagent_cost_usd"

#: A CLI `modelUsage` entry's key -> the usage summary's name for it.
_MODEL_USAGE_FIELDS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheReadInputTokens", "cache_read_input_tokens"),
    ("cacheCreationInputTokens", "cache_creation_input_tokens"),
    ("costUSD", "cost_usd"),
)

#: What one token of each kind costs relative to an input token, on every
#: Claude model's price list (output 5x, a cache write 1.25x, a cache read
#: 0.1x). The worker holds no price table (archived issue 1481); these ratios are all it
#: needs to split ONE model's cost between the parent's tokens and the rest,
#: because that model's own `costUSD` fixes the scale. A one-hour cache write
#: costs 2x, not 1.25x, so a parent writing those is credited slightly less
#: than it spent and the sub-agent share reads that much high.
_TOKEN_WEIGHTS = (
    ("input_tokens", 1.0),
    ("output_tokens", 5.0),
    ("cache_creation_input_tokens", 1.25),
    ("cache_read_input_tokens", 0.1),
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _model_usage_rows(models: dict[str, Any]) -> list[dict[str, Any]]:
    """A CLI's `modelUsage` as the usage summary's per-model rows. Pure.

    A field the CLI did not report, or reported as something other than a
    number, is left out of its row rather than written as zero; a model with
    no number at all has no row.
    """
    rows = []
    for name in sorted(models, key=str):
        entry = models[name]
        if not isinstance(entry, dict):
            continue
        row: dict[str, Any] = {}
        for source, target in _MODEL_USAGE_FIELDS:
            value = entry.get(source)
            if target == "cost_usd" and _is_number(value):
                row[target] = round(float(value), 10)
            elif isinstance(value, int) and not isinstance(value, bool):
                row[target] = value
        if row:
            rows.append({"model": str(name), **row})
    return rows


def _weighted_tokens(counts: dict[str, Any]) -> float:
    return sum(float(counts.get(key) or 0) * weight for key, weight in _TOKEN_WEIGHTS)


def _subagent_cost_usd(
    parent: dict[str, Any], rows: list[dict[str, Any]], total_cost: Any
) -> float | None:
    """The share of `total_cost` the parent's token counts do not explain. Pure.

    `parent` carries the parent agent's own token counts (the CLI's `usage`),
    `rows` every model's counts and cost (`_model_usage_rows`). The parent's
    model is the row whose four counts each reach the parent's, the costliest
    if several do; the parent is credited that row's cost in proportion to
    its share of the row's tokens, weighted by `_TOKEN_WEIGHTS`. Everything
    else -- the rest of that row, every other model's row, and any of
    `total_cost` the rows do not account for -- is spend with no parent token
    behind it: sub-agents, and the small model the CLI runs for its own
    housekeeping.

    None, "not known", when there is no total, the parent reported no token,
    or no row is complete enough to hold the parent: a guess is not a figure.
    """
    if not _is_number(total_cost):
        return None
    keys = [key for key, _ in _TOKEN_WEIGHTS]
    if not any(_is_number(parent.get(key)) for key in keys):
        return None
    candidates = [
        row for row in rows
        if _is_number(row.get("cost_usd"))
        and all(_is_number(row.get(key)) for key in keys)
        and all(row[key] >= (parent.get(key) or 0) for key in keys)
    ]
    if not candidates:
        return None
    home = max(candidates, key=lambda row: row["cost_usd"])
    row_weight = _weighted_tokens(home)
    explained = home["cost_usd"] * (_weighted_tokens(parent) / row_weight) if row_weight else 0.0
    return round(min(max(float(total_cost) - explained, 0.0), float(total_cost)), 10)


def _add_model_usage(
    earlier: list[dict[str, Any]], later: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Two runs' per-model rows as one: each model's numbers summed. Pure."""
    by_model: dict[str, dict[str, Any]] = {}
    for row in [*earlier, *later]:
        if not isinstance(row, dict) or "model" not in row:
            continue
        into = by_model.setdefault(str(row["model"]), {"model": str(row["model"])})
        for _, key in _MODEL_USAGE_FIELDS:
            if _is_number(row.get(key)):
                into[key] = into.get(key, 0) + row[key]
        if _is_number(into.get("cost_usd")):
            into["cost_usd"] = round(into["cost_usd"], 10)
    return [by_model[name] for name in sorted(by_model)]
