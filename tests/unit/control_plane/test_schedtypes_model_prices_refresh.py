"""The `model-prices-refresh` schedule type: the weekly read of the provider's pricing page (#1481).

WHAT IS HELD HERE
-----------------
* THE PARSER reads the saved page (`pricing_page_2026-10-19.md`, the
  markdown the provider serves at `PRICING_PAGE_URL`, fetched 2026-10-19) to
  exactly its figures: input, output, cache hits (`cache_read`) and the
  5-minute cache write (`cache_write`), USD per million tokens.
* A FIGURE IT CANNOT PARSE IS LEFT AS IT WAS. A row whose cell is not
  `$N / MTok`, rows of one model that are not one chain of prompt-length
  tiers, or a table whose header changed writes nothing for that model -- or
  nothing at all -- and the firing says so with a FAILED item. It never
  writes a guess.
* A MODEL PRICED IN TIERS (Haiku 5.5, by prompt length) is read as one entry
  with its `tiers` (PRICES-FOLLOWUP, part of #1481).
* WHAT CHANGED IS WRITTEN with `source: pricing-page` and the day it was read;
  an entry the page agrees with is not touched.
* A DRY RUN reads the page and writes nothing.

No cloud and no emulator: the in-memory Firestore, and the page from a file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from swarm_api import modelprices, schedulefire, scheduletypes, schedules
from swarm_api.schedtypes import model_prices_refresh as refresh

from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    NOW,
    SCHEDULE_ID,
    ctx,
    firing_for,
    github,
    make_schedule,
    writes,
)

PAGE = (Path(__file__).parent / "pricing_page_2026-10-19.md").read_text()
DAY = NOW.date().isoformat()
OPUS_ROW = "| Claude Opus 5.5 "
HAIKU_55_OVER_ROW = "| Claude Haiku 5.5 (for prompts over"
#: Opus 5.5 with an output cell that is no figure: a page with one unread row.
UNREAD_OPUS = ("| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok | "
               "$20 / MTok until November, then $24 / MTok |")
#: Haiku 5.5 as the saved page prices it, USD per million tokens.
HAIKU_55_TIERS = [
    {"up_to_prompt_tokens": 100_000, "input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    {"up_to_prompt_tokens": None, "input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625},
]


def schedule(db) -> dict[str, Any]:
    return make_schedule(db, type_="model-prices-refresh", scope_mode="platform",
                         cron="0 6 * * 1", max_concurrent=2)


def serve(monkeypatch, text: str | None = PAGE, *, error: str | None = None) -> list[int]:
    """Serve `text` as the page (or fail with `error`); returns a list counting the reads."""
    reads: list[int] = []

    def fetch() -> str:
        reads.append(1)
        if error is not None:
            raise refresh.PageUnreadable(error)
        assert text is not None
        return text

    monkeypatch.setattr(refresh, "fetch_page", fetch)
    return reads


def prices(ctx) -> dict[str, dict[str, Any]]:
    return ctx.store.get_platform_settings()["model_prices"]


def rates(entry: dict[str, Any]) -> tuple[float, ...]:
    return tuple(entry[name] for name in modelprices.RATES)


def with_row(page: str, start: str, row: str) -> str:
    """`page` with the model-table row beginning `start` replaced by `row`: the
    FIRST such row, because the batch and tool-use tables below it name the
    same models."""
    lines = page.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(start))
    lines[index] = row
    return "\n".join(lines) + "\n"


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("model-prices-refresh")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is refresh
    assert entry.executor == "api" and entry.profile is None
    # Prices are a platform setting: an admin's platform schedule, nothing else.
    assert sorted(entry.creators) == ["platform"]
    assert scheduletypes.creator_role(entry, "platform") == "admin"
    assert schedules.resolve_budget(entry, None)["per_run_usd"] > 0


def test_the_page_url_is_code_and_https_on_the_providers_docs_host() -> None:
    # A caller never names a URL (invariant 10): the type takes no parameter.
    assert refresh.PRICING_PAGE_URL.startswith("https://platform.claude.com/")
    assert scheduletypes.get("model-prices-refresh").params_model.model_fields == {}


# ---------------------------------------------------------------------------
# The parser, on the saved page
# ---------------------------------------------------------------------------


def test_the_parser_reads_the_saved_page_to_its_figures() -> None:
    parsed = refresh.parse_page(PAGE)

    # input, output, cache_read, cache_write: the 5-minute write, as the seed holds it.
    assert rates(parsed.prices["claude-opus-5-5"]) == (4.0, 20.0, 0.20, 5.0)
    assert rates(parsed.prices["claude-fable-5-1"]) == (10.0, 50.0, 0.25, 12.5)
    assert rates(parsed.prices["claude-sonnet-5-5"]) == (2.0, 10.0, 0.10, 2.5)
    assert rates(parsed.prices["claude-haiku-4-5"]) == (1.0, 5.0, 0.10, 1.25)
    # A footnote marker is not part of the figure (Sonnet 5's `<sup>3</sup>`).
    assert rates(parsed.prices["claude-sonnet-5"]) == (2.0, 10.0, 0.20, 2.5)
    # A link-only note ("limited availability", "retired") does not change the model.
    assert rates(parsed.prices["claude-mythos-5-1"]) == (10.0, 50.0, 0.25, 12.5)
    assert rates(parsed.prices["claude-opus-4-1"]) == (15.0, 75.0, 1.5, 18.75)
    # Only the model table: the batch table's Opus 5.5 ($2/$10) never leaks in.
    # 20: the 19 single-rate rows, and Haiku 5.5's two rows as one tiered entry.
    assert len(parsed.prices) == 20
    for name in parsed.prices:
        assert modelprices.MODEL_NAME.fullmatch(name), name


def test_every_seeded_model_is_on_the_page_at_its_seeded_figures() -> None:
    parsed = refresh.parse_page(PAGE)
    for name, seed in modelprices.SEED.items():
        assert rates(parsed.prices[name]) == rates(seed), name


def test_the_parser_reads_haiku_5_5s_two_tiers_from_the_saved_page() -> None:
    parsed = refresh.parse_page(PAGE)

    # Two rows by prompt length are one entry with two tiers: up to 100,000
    # tokens, and over it. Its own rates are the first tier's.
    haiku = parsed.prices["claude-haiku-5-5"]
    assert haiku["tiers"] == HAIKU_55_TIERS
    assert rates(haiku) == (0.10, 0.50, 0.01, 0.125)
    assert parsed.unparsed == [], "every row of the saved page is read"
    # Every other model is untiered, exactly as before.
    assert [name for name, p in parsed.prices.items() if "tiers" in p] == ["claude-haiku-5-5"]
    # And the setting accepts what the parser read.
    assert modelprices.entry({**haiku, "currency": "USD", "source": refresh.SOURCE,
                              "as_of": DAY})["tiers"] == HAIKU_55_TIERS


@pytest.mark.parametrize("over", [
    # Two `up to` rows and no `over`: a prompt past 100,000 tokens is priced by nothing.
    "| Claude Haiku 5.5 (for prompts up to 200,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok "
    "| $0.05 / MTok | $2.50 / MTok |",
    # `over` a ceiling no row has: a gap between 100,000 and 150,000.
    "| Claude Haiku 5.5 (for prompts over 150,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok "
    "| $0.05 / MTok | $2.50 / MTok |",
    # A qualifier that is no tier.
    "| Claude Haiku 5.5 (batch only) | $0.50 / MTok | $0.625 / MTok | $1 / MTok "
    "| $0.05 / MTok | $2.50 / MTok |",
])
def test_rows_that_are_not_one_chain_of_tiers_are_not_parsed_and_say_why(over: str) -> None:
    parsed = refresh.parse_page(with_row(PAGE, HAIKU_55_OVER_ROW, over))

    assert "claude-haiku-5-5" not in parsed.prices
    tiers = [row for row in parsed.unparsed if row["model"] == "claude-haiku-5-5"]
    assert len(tiers) == 2 and all(row["reason"] == refresh.TIERED for row in tiers)


def test_a_cell_that_is_not_a_figure_leaves_that_model_unparsed() -> None:
    page = with_row(PAGE, OPUS_ROW,
                    "| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok | "
                    "$20 / MTok until November, then $24 / MTok |")

    parsed = refresh.parse_page(page)

    assert "claude-opus-5-5" not in parsed.prices
    (row,) = [row for row in parsed.unparsed if row["model"] == "claude-opus-5-5"]
    assert row["reason"] == refresh.UNREADABLE_CELL and row["column"] == "Output tokens"
    # The other rows are read as they were.
    assert rates(parsed.prices["claude-opus-5"]) == (5.0, 25.0, 0.5, 6.25)


def test_a_changed_header_is_an_unreadable_page() -> None:
    page = PAGE.replace("| Base input tokens ", "| Input (per MTok) ", 1)
    with pytest.raises(refresh.PageUnreadable, match="Base input tokens"):
        refresh.parse_page(page)


def test_a_page_without_the_model_table_is_unreadable() -> None:
    with pytest.raises(refresh.PageUnreadable):
        refresh.parse_page("<html><body>Something went wrong</body></html>")


# ---------------------------------------------------------------------------
# The firing
# ---------------------------------------------------------------------------


def test_a_firing_writes_what_changed_with_source_pricing_page_and_the_day_read(db, ctx, monkeypatch) -> None:
    serve(monkeypatch)
    before = prices(ctx)

    work = refresh.create(firing_for(ctx, schedule(db), room=2))

    after = prices(ctx)
    # New on the page: written, sourced and dated.
    assert rates(after["claude-sonnet-5-5"]) == (2.0, 10.0, 0.10, 2.5)
    assert after["claude-sonnet-5-5"]["source"] == refresh.SOURCE == "pricing-page"
    assert after["claude-sonnet-5-5"]["as_of"] == DAY
    assert after["claude-sonnet-5-5"]["updated_by"] == f"schedule:{SCHEDULE_ID}"
    # The page agrees with the seed's figures, so those entries are not touched.
    for name in modelprices.SEED:
        assert after[name] == before[name], name
    # Haiku 5.5 is written with its two tiers, sourced and dated like the rest.
    haiku = after["claude-haiku-5-5"]
    assert haiku["tiers"] == HAIKU_55_TIERS
    assert (haiku["source"], haiku["as_of"]) == ("pricing-page", DAY)
    assert "tiers" not in after["claude-sonnet-5-5"]
    # Every row was read, so the firing is one succeeded item.
    assert [(w["id"], w["outcome"]) for w in work] == [(refresh.REFRESH, "succeeded")]
    changed = work[0]["detail"]["changed"]
    assert "claude-sonnet-5-5" in changed
    assert changed["claude-haiku-5-5"]["rates"]["tiers"] == HAIKU_55_TIERS


def test_a_changed_figure_replaces_the_stored_one(db, ctx, monkeypatch) -> None:
    page = with_row(PAGE, OPUS_ROW,
                    "| Claude Opus 5.5 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.15 / MTok | $15 / MTok |")
    serve(monkeypatch, page)

    work = refresh.create(firing_for(ctx, schedule(db), room=2))

    opus = prices(ctx)["claude-opus-5-5"]
    assert rates(opus) == (3.0, 15.0, 0.15, 3.75)
    assert (opus["source"], opus["as_of"]) == ("pricing-page", DAY)
    changed = {w["id"]: w["detail"] for w in work}[refresh.REFRESH]["changed"]["claude-opus-5-5"]
    assert changed["was"] == {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}


def test_a_changed_tier_replaces_the_stored_tiers(db, ctx, monkeypatch) -> None:
    serve(monkeypatch)
    refresh.create(firing_for(ctx, schedule(db), room=2))
    serve(monkeypatch, with_row(PAGE, HAIKU_55_OVER_ROW,
                                "| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.60 / MTok | $0.75 / MTok "
                                "| $1.20 / MTok | $0.06 / MTok | $3 / MTok |"))

    work = refresh.create(firing_for(ctx, schedule(db), room=2, fid="again"))

    haiku = prices(ctx)["claude-haiku-5-5"]
    assert haiku["tiers"][1] == {"up_to_prompt_tokens": None, "input": 0.60, "output": 3.0,
                                 "cache_read": 0.06, "cache_write": 0.75}
    assert haiku["tiers"][0] == HAIKU_55_TIERS[0]
    changed = work[0]["detail"]["changed"]
    assert list(changed) == ["claude-haiku-5-5"], "only the model whose tier changed is written"
    assert changed["claude-haiku-5-5"]["was"]["tiers"] == HAIKU_55_TIERS


def test_a_figure_it_cannot_parse_is_left_as_it_was(db, ctx, monkeypatch) -> None:
    page = with_row(PAGE, OPUS_ROW,
                    "| Claude Opus 5.5 | about $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok | $20 / MTok |")
    serve(monkeypatch, page)
    before = prices(ctx)["claude-opus-5-5"]

    work = refresh.create(firing_for(ctx, schedule(db), room=2))

    assert prices(ctx)["claude-opus-5-5"] == before
    unparsed = {w["id"]: w for w in work}[refresh.UNPARSED]
    assert unparsed["outcome"] == "failed"
    assert "claude-opus-5-5" in {row["model"] for row in unparsed["detail"]["rows"]}


def test_an_unreadable_page_writes_nothing_and_fails_the_firing(db, ctx, monkeypatch) -> None:
    serve(monkeypatch, PAGE.replace("## Model pricing", "## Prices", 1))
    before = ctx.store.get_platform_settings()

    work = refresh.create(firing_for(ctx, schedule(db), room=2))

    assert ctx.store.get_platform_settings() == before
    assert [(w["id"], w["outcome"]) for w in work] == [(refresh.REFRESH, "failed")]
    assert work[0]["detail"]["code"] == refresh.PAGE_UNREADABLE
    assert schedulefire.combine([w["outcome"] for w in work], independent=False) == "failed"


def test_a_page_that_cannot_be_fetched_writes_nothing_and_fails_the_firing(db, ctx, monkeypatch) -> None:
    serve(monkeypatch, error="the pricing page answered 503")
    before = ctx.store.get_platform_settings()

    work = refresh.create(firing_for(ctx, schedule(db), room=2))

    assert ctx.store.get_platform_settings() == before
    assert work[0]["outcome"] == "failed" and "503" in work[0]["detail"]["message"]


def test_a_second_firing_on_the_same_page_changes_nothing(db, ctx, monkeypatch) -> None:
    serve(monkeypatch)
    refresh.create(firing_for(ctx, schedule(db), room=2))
    first = prices(ctx)

    work = refresh.create(firing_for(ctx, schedule(db), room=2, fid="again"))

    assert prices(ctx) == first
    assert {w["id"]: w["detail"] for w in work}[refresh.REFRESH]["changed"] == {}


def test_room_for_one_item_puts_the_unparsed_rows_on_the_failed_refresh(db, ctx, monkeypatch) -> None:
    serve(monkeypatch, with_row(PAGE, OPUS_ROW, UNREAD_OPUS))

    work = refresh.create(firing_for(ctx, schedule(db), room=1))

    (item,) = work
    assert item["outcome"] == "failed"
    assert {row["model"] for row in item["detail"]["unparsed"]} == {"claude-opus-5-5"}
    # What could be read was still written.
    assert "claude-sonnet-5-5" in prices(ctx)


def test_a_dry_run_reads_the_page_and_writes_nothing(db, ctx, monkeypatch) -> None:
    reads = serve(monkeypatch, with_row(PAGE, OPUS_ROW, UNREAD_OPUS))
    before = ctx.store.get_platform_settings()

    out = refresh.dry_run(firing_for(ctx, schedule(db), room=2))

    assert reads == [1]
    assert ctx.store.get_platform_settings() == before
    assert "claude-sonnet-5-5" in out["would_change"]
    assert out["would_change"]["claude-haiku-5-5"]["rates"]["tiers"] == HAIKU_55_TIERS
    assert {row["model"] for row in out["unparsed"]} == {"claude-opus-5-5"}
