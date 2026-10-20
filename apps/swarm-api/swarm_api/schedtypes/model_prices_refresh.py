"""`model-prices-refresh`: the weekly read of the provider's pricing page (docs/schedules.md §3.14, #1481).

Owner decision, 2026-10-17: "A weekly schedule refreshes them from the
provider's pricing page. It writes changed figures with `source:
pricing-page` and the page's date. A figure it cannot parse is left as it
was, and the run says so (an alert, or the schedule's own failure); it never
writes a guess."

AN API ACTION, NO AGENT (SD8, §2.7). swarm-api reads the page itself -- it
already leaves the VPC for GitHub through Cloud NAT on 443 -- and writes the
platform's price setting through `Store.set_platform_settings`, the path
`PUT /v1/admin/settings` takes, so an entry it writes is checked by exactly
the rule an admin's edit is (`modelprices.entry`). No task, no lease, no
capacity (invariants 1-3); no profile, because nothing runs (10).

THE PAGE IS CODE, not a parameter: `PRICING_PAGE_URL`, the markdown the
provider serves for its pricing page. A caller never names a URL (invariant
10). Markdown, not the HTML page, because its model table is a plain
pipe table whose header names its columns: a change of layout is a header
that no longer matches, which is refused, rather than a CSS class that
quietly matches something else.

WHAT A FIRING DOES.

  1. FETCH the page (`fetch_page`): https, that host only, bounded in time
     and size, no credential of any kind sent.
  2. PARSE its "Model pricing" table (`parse_page`). Its columns are found by
     their header names; the four the setting holds are `Base input tokens`
     (input), `Output tokens` (output), `Cache hits and refreshes`
     (cache_read) and `5m cache writes` (cache_write -- the five-minute rate,
     which is what the seed holds, 1.25x input). A row is
     `Claude <Family> <version>`, optionally followed by a parenthesised
     markdown LINK ("limited availability", "retired"); every rate cell is
     `$N / MTok`, optionally with a footnote marker.
  3. WRITE what changed: every parsed model whose four rates differ from the
     stored entry, or that has none, with `source: pricing-page`, `as_of`
     the day the page was read and `updated_by: schedule:<id>`. An entry the
     page agrees with is NOT rewritten, so its `as_of` still says when its
     figure last changed.

WHAT IS NOT PARSED IS LEFT AS IT WAS, AND THE FIRING SAYS SO.
  * A row whose cell is anything but a figure (`UNREADABLE_CELL`), a model
    name that is not `Claude <Family> <version>` (`UNREADABLE_MODEL`), and a
    model on several rows that are not one reading of tiers (`TIERED`: a row
    qualified by anything but a link or a prompt-length tier, or tiers that
    leave a prompt size unpriced) write nothing for that model. Those rows
    are the `unparsed` item, FAILED, so the firing is `partial` and counts
    towards the schedule's failure pause (§4.4): the schedule's own failure
    is the alert.

TIERS (PRICES-FOLLOWUP, owner 2026-10-19). A model the page prices by prompt
length -- Haiku 5.5 on 2026-10-19, `(for prompts up to 100,000 tokens)` and
`(for prompts over 100,000 tokens)` -- is read as ONE entry with `tiers`
(`modelprices.tiers_problem`): each `up to N` row a tier with ceiling N, in
rising order, and exactly one `over N` row, N the largest ceiling, as the
last tier with none. The entry's own rates are its first tier's. Rows that
do not make that chain -- two `over` rows, an `over` that is not the last
ceiling, a gap -- are `TIERED`, written as nothing: which rate a prompt pays
between them would be a guess.
  * A page that cannot be fetched, or whose model table is missing or whose
    header changed (`PageUnreadable`), writes NOTHING: the `refresh` item is
    FAILED with `PAGE_UNREADABLE`, and the firing is `failed`.

THE PAGE'S DATE. Read 2026-10-19, the page carries no date of its own: no
`Last-Modified`, and no date in its text. `as_of` is therefore the day the
firing read it (its `now`, UTC) -- the day the figure was read from its
source, which is what `modelprices.AS_OF` says the field holds.

DRY RUN (§2.8): the same read and parse, and what would change. Nothing is
written.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from .. import modelprices
from ..scheduletypes import BudgetCaps

log = logging.getLogger(__name__)

TYPE = "model-prices-refresh"

#: SD4 leaves this type's caps to its lane. A firing runs no model and no
#: worker, so its own cost is $0.00: the dollar caps are the smallest a
#: schedule's budget accepts (`schedules.resolve_budget` refuses zero).
#: `max_concurrent` is 2 because a firing makes at most two items, the
#: refresh and the rows it could not parse.
BUDGET_CAPS = BudgetCaps(per_run_usd=0.01, per_day_usd=0.05, max_concurrent=2)

#: The provider's pricing page, as markdown (module docstring).
PRICING_PAGE_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
#: The page was 50 KB on 2026-10-19; past this it is not the page.
MAX_PAGE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0
USER_AGENT = "swarmcloud-swarm-api model-prices-refresh"

#: What every entry this type writes says it came from (`ModelPriceRequest`).
SOURCE = "pricing-page"

#: The section the table is under, and the header of each column read.
SECTION = "## Model pricing"
COLUMN_MODEL = "Model"
COLUMNS: dict[str, str] = {
    "input": "Base input tokens",
    "output": "Output tokens",
    "cache_read": "Cache hits and refreshes",
    "cache_write": "5m cache writes",
}

REFRESH = "refresh"
UNPARSED = "unparsed"

PAGE_UNREADABLE = "pricing_page_unreadable"
UNREADABLE_CELL = "unreadable_cell"
UNREADABLE_MODEL = "unreadable_model"
TIERED = "tiered"

#: `$4 / MTok`, `$0.20 / MTok<sup>2</sup>`.
_FIGURE = re.compile(r"^\$(\d+(?:\.\d+)?) / MTok(?:<sup>\d+</sup>)?$")
#: ` ([limited availability](https://...))`: a note that is only a link.
_LINK_NOTE = re.compile(r"\s*\(\[[^\]]*\]\([^)\s]*\)\)")
#: `(for prompts up to 100,000 tokens)`, `(for prompts over 100,000 tokens)`.
_TIER = re.compile(r"^\(for prompts (up to|over) (\d{1,3}(?:,\d{3})+|\d+) tokens\)$")
#: `Claude Opus 5.5` -> family `Opus`, version `5.5`.
_MODEL = re.compile(r"^Claude ([A-Za-z]+) (\d+(?:\.\d+)*)$")


class PageUnreadable(Exception):
    """The page could not be fetched, or holds no model table this parser can trust.

    Its message is a constant sentence about the page: it carries no
    credential, because none is ever sent.
    """


@dataclass
class Parsed:
    #: Model name -> its four rates, USD per million tokens, and `tiers` for
    #: a model the page prices by prompt length.
    prices: dict[str, dict[str, float]] = field(default_factory=dict)
    #: One entry per row left unread: `{row, model, reason, column?}`.
    unparsed: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def fetch_page() -> str:
    """The page's text. Raises `PageUnreadable` for anything but a 200 from its host."""
    host = urlsplit(PRICING_PAGE_URL).hostname
    request = urllib.request.Request(
        PRICING_PAGE_URL, headers={"User-Agent": USER_AGENT, "Accept": "text/markdown, text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310 -- a constant https URL
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != host:
                raise PageUnreadable(f"the pricing page redirected away from {host}")
            body = response.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PageUnreadable(f"the pricing page answered {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PageUnreadable(f"the pricing page could not be reached ({type(exc).__name__})") from None
    if len(body) > MAX_PAGE_BYTES:
        raise PageUnreadable(f"the pricing page is over {MAX_PAGE_BYTES} bytes")
    return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table(text: str) -> list[str]:
    """The pipe-table lines of the model pricing section, header first."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == SECTION)
    except StopIteration:
        raise PageUnreadable(f"the pricing page has no {SECTION!r} section") from None
    table: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.lstrip().startswith("|"):
            table.append(line)
        elif table:
            break
    if len(table) < 3:
        raise PageUnreadable(f"the pricing page's {SECTION!r} section holds no table")
    return table


def model_name(cell: str) -> tuple[str | None, str | None]:
    """`(name, qualifier)`: `claude-opus-5-5` from `Claude Opus 5.5`, and what
    the row says beyond a link-only note (None when nothing); `(None, None)`
    when the cell does not name a model this way."""
    text = _LINK_NOTE.sub("", cell).strip()
    qualifier = None
    if " (" in text:
        text, rest = text.split(" (", 1)
        qualifier = "(" + rest
    match = _MODEL.fullmatch(text.strip())
    if match is None:
        return None, None
    name = f"claude-{match.group(1).lower()}-{match.group(2).replace('.', '-')}"
    if not modelprices.MODEL_NAME.fullmatch(name):
        return None, None
    return name, qualifier


def figure(cell: str) -> float | None:
    match = _FIGURE.fullmatch(cell.strip())
    return float(match.group(1)) if match else None


def parse_page(text: str) -> Parsed:
    """The page's model prices, and every row it did not read, with why."""
    header, _rule, *rows = _table(text)
    names = _cells(header)
    missing = [title for title in (COLUMN_MODEL, *COLUMNS.values()) if title not in names]
    if missing:
        raise PageUnreadable(
            "the pricing page's model table no longer has the column(s) " + ", ".join(repr(m) for m in missing))
    index = {key: names.index(title) for key, title in COLUMNS.items()}
    model_at = names.index(COLUMN_MODEL)

    parsed = Parsed()
    rows_of: dict[str, list[dict[str, Any]]] = {}
    for line in rows:
        cells = _cells(line)
        if len(cells) != len(names):
            parsed.unparsed.append({"row": line.strip()[:300], "model": None, "reason": UNREADABLE_CELL,
                                    "column": None})
            continue
        name, qualifier = model_name(cells[model_at])
        if name is None:
            parsed.unparsed.append({"row": cells[model_at][:200], "model": None, "reason": UNREADABLE_MODEL})
            continue
        row: dict[str, Any] = {"row": cells[model_at][:200], "model": name, "qualifier": qualifier,
                               "rates": {}, "bad_column": None}
        for key, at in index.items():
            value = figure(cells[at])
            if value is None or modelprices._rate(value) is None:
                row["bad_column"] = row["bad_column"] or COLUMNS[key]
            else:
                row["rates"][key] = value
        rows_of.setdefault(name, []).append(row)

    for name, found in rows_of.items():
        bad = [row for row in found if row["bad_column"]]
        if bad:
            for row in bad:
                parsed.unparsed.append({"row": row["row"], "model": name, "reason": UNREADABLE_CELL,
                                        "column": row["bad_column"]})
            continue
        if len(found) == 1 and not found[0]["qualifier"]:
            parsed.prices[name] = {key: found[0]["rates"][key] for key in modelprices.RATES}
            continue
        tiers = _tiers(found)
        if tiers is None:
            for row in found:
                parsed.unparsed.append({"row": row["row"], "model": name, "reason": TIERED})
            continue
        parsed.prices[name] = {**{key: tiers[0][key] for key in modelprices.RATES}, "tiers": tiers}
    return parsed


def _tiers(found: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """One model's rows as its tiers, or None when they are not one reading.

    Every row names a prompt-length tier; the `up to` ceilings are distinct;
    exactly one row is `over` the largest of them. Anything else -- a lone
    tier, two `over` rows, a qualifier this does not know -- is None.
    """
    up_to: list[tuple[int, dict[str, float]]] = []
    over: list[tuple[int, dict[str, float]]] = []
    for row in found:
        match = _TIER.fullmatch(row["qualifier"] or "")
        if match is None:
            return None
        bound = int(match.group(2).replace(",", ""))
        (up_to if match.group(1) == "up to" else over).append((bound, row["rates"]))
    if not up_to or len(over) != 1 or len({bound for bound, _ in up_to}) != len(up_to):
        return None
    up_to.sort(key=lambda tier: tier[0])
    if over[0][0] != up_to[-1][0]:
        return None
    tiers = [{modelprices.UP_TO: bound, **rates} for bound, rates in up_to]
    tiers.append({modelprices.UP_TO: None, **over[0][1]})
    checked, _why = modelprices.tiers_problem(tiers, tiers[0])
    return checked



# --------------------------------------------------------------------------
# Comparing and writing
# --------------------------------------------------------------------------


def _rates(entry: Mapping[str, Any]) -> dict[str, Any]:
    """An entry's figures: its four rates, and its tiers when it has them."""
    out: dict[str, Any] = {key: float(entry[key]) for key in modelprices.RATES}
    if entry.get("tiers"):
        out["tiers"] = [{modelprices.UP_TO: tier[modelprices.UP_TO],
                         **{key: float(tier[key]) for key in modelprices.RATES}}
                        for tier in entry["tiers"]]
    return out


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if any(abs(a[k] - b[k]) >= 1e-9 for k in modelprices.RATES):
        return False
    tiers_a, tiers_b = a.get("tiers") or [], b.get("tiers") or []
    return len(tiers_a) == len(tiers_b) and all(
        x[modelprices.UP_TO] == y[modelprices.UP_TO] and _same(x, y) for x, y in zip(tiers_a, tiers_b))


def changes(parsed: Parsed, current: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each parsed model whose figures -- rates and tiers -- differ from its
    stored entry (or that has none): `{rates, was}`, `was` None for a model
    not priced before. `rates` carries `tiers` for a tiered model."""
    out: dict[str, dict[str, Any]] = {}
    for name, rates in sorted(parsed.prices.items()):
        stored = current.get(name)
        was = _rates(stored) if stored is not None else None
        if was is not None and _same(was, rates):
            continue
        out[name] = {"rates": _rates(rates), "was": was}
    return out


def read(ctx: Any, now: Any) -> dict[str, Any]:
    """Fetch, parse and compare. Writes nothing. Raises `PageUnreadable`."""
    parsed = parse_page(fetch_page())
    current = ctx.store.get_platform_settings()["model_prices"]
    return {
        "url": PRICING_PAGE_URL,
        "as_of": now.date().isoformat(),
        "read": sorted(parsed.prices),
        "changed": changes(parsed, current),
        "unparsed": parsed.unparsed,
        "not_on_page": sorted(set(current) - set(parsed.prices)
                              - {row["model"] for row in parsed.unparsed if row.get("model")}),
    }


def write(ctx: Any, reading: Mapping[str, Any], schedule_id: str) -> None:
    changed = reading["changed"]
    if not changed:
        return
    entries = {
        name: {**change["rates"], "currency": modelprices.CURRENCY, "source": SOURCE,
               "as_of": reading["as_of"]}
        for name, change in changed.items()
    }
    ctx.store.set_platform_settings({"model_prices": entries}, by=f"schedule:{schedule_id}")


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def _ids(firing: Any) -> tuple[str, str]:
    return str(firing.firing["firing_id"]), str(firing.schedule["schedule_id"])


def create(firing: Any) -> list[dict[str, Any]]:
    firing_id, schedule_id = _ids(firing)
    if firing.room < 1:
        log.info("schedule %s firing %s: no room for the price refresh", schedule_id, firing_id)
        return []
    try:
        reading = read(firing.ctx, firing.now)
    except PageUnreadable as exc:
        log.warning("schedule %s firing %s: %s; no price was changed", schedule_id, firing_id, exc)
        return [firing.api_action(REFRESH, outcome="failed", detail={
            "url": PRICING_PAGE_URL, "code": PAGE_UNREADABLE, "message": str(exc), "changed": {}})]
    write(firing.ctx, reading, schedule_id)
    unparsed = reading["unparsed"]
    refresh = {k: v for k, v in reading.items() if k != "unparsed"}
    log.info("schedule %s firing %s: pricing page read, %d models, %d changed, %d rows unparsed",
             schedule_id, firing_id, len(reading["read"]), len(reading["changed"]), len(unparsed))
    if not unparsed:
        return [firing.api_action(REFRESH, outcome="succeeded", detail=refresh)]
    if firing.room < 2:
        # No room for a second item: the rows are on the refresh's own, which fails.
        return [firing.api_action(REFRESH, outcome="failed", detail={**refresh, "unparsed": unparsed})]
    return [
        firing.api_action(REFRESH, outcome="succeeded", detail=refresh),
        firing.api_action(UNPARSED, outcome="failed", detail={
            "message": "these rows of the pricing page were not read; their models' prices were left as they were",
            "rows": unparsed,
        }),
    ]


def dry_run(firing: Any) -> dict[str, Any]:
    try:
        reading = read(firing.ctx, firing.now)
    except PageUnreadable as exc:
        return {"url": PRICING_PAGE_URL, "code": PAGE_UNREADABLE, "message": str(exc), "would_change": {}}
    return {
        "url": PRICING_PAGE_URL,
        "as_of": reading["as_of"],
        "would_change": reading["changed"],
        "unparsed": reading["unparsed"],
        "not_on_page": reading["not_on_page"],
    }
