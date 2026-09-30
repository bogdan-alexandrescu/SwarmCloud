"""`GET /v1/outcomes?section=…`: one Timeline card's part of the ledger (#377).

Owner decision 2026-09-30: every Timeline card reads its own part of the
ledger when it scrolls into view, instead of seven cards sharing one eager
read. The route takes a repeatable `section` naming the output keys a card
needs; with none it serves exactly what it served before, so the CLI and every
other caller are untouched.

What is held here, through the real app over FakeFirestore with #196's own
fixture week (`test_outcomes_route.seed_week`):

  * each section alone is THAT KEY of the full response for the same query,
    and the response carries the envelope and nothing else;
  * no `section` is the full response, key for key and in order, and naming
    every section is the same payload;
  * an unknown section is the one 422 envelope, and the admin gate still runs
    before it, so a non-admin learns nothing by naming sections;
  * a section that needs no extra read costs none: the previous span, the
    terminal count and the workflow enrichment are read only for the section
    that shows them;
  * (owner decision 2026-09-30, the review of #391) the cache holds the FOLD,
    keyed by the query without its sections, so the headline and all seven
    cards of one page pay for one scan -- counted at the store, not trusted
    to the service's own meter -- and a section is the same value alone as
    in the full response on a COLD store too.

Each comparison is between two FRESH services over the same warmed store, so
neither the payload cache nor a first-read derive makes the two differ.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.outcomes import SECTIONS

from .conftest import auth_header
from .fakes import FakeDocumentRef, FakeFirestore, FakeQuery
from .test_outcomes_route import Clock, _context, seed_week

WEEK = {"tz": "UTC", "span": "7d", "compare": "previous"}

#: What `GET /v1/outcomes` served before `section` existed, in its order. A
#: caller with no `section` must see exactly this.
FULL_KEYS = [
    "scope", "tz", "requested", "since", "until", "bucket", "bucket_chosen_by",
    "basis", "filters", "vocab", "buckets", "totals", "retries", "latency",
    "groups", "workflows_failed", "coverage", "previous", "reads", "cached",
    "generated_at",
]

#: What every sectioned response carries whatever it asked for: the resolved
#: query, the vocabulary, and what the read cost.
ENVELOPE = [
    "scope", "tz", "requested", "since", "until", "bucket", "bucket_chosen_by",
    "basis", "filters", "vocab", "reads", "cached", "generated_at",
]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def warmed(db, tokens, group_map, clock):
    """The store after one full read has written every rollup day, so a fresh
    service reads stored days and derives nothing."""
    seed_week(db)
    first = TestClient(create_app(_context(db, tokens, StaticGroups(group_map), clock)))
    for user, extra in (("alice", {}), ("root", {"scope": "platform"})):
        response = first.get("/v1/outcomes", params={**WEEK, **extra}, headers=auth_header(user))
        assert response.status_code == 200, response.text
    return db


def fresh(db, tokens, group_map, clock) -> TestClient:
    """A new service with an empty payload cache, over the same store."""
    return TestClient(create_app(_context(db, tokens, StaticGroups(group_map), clock)), raise_server_exceptions=False)


def read(api: TestClient, user: str, **params: Any):
    return api.get("/v1/outcomes", params=params, headers=auth_header(user))


def ok(api: TestClient, user: str, **params: Any) -> dict[str, Any]:
    response = read(api, user, **params)
    assert response.status_code == 200, response.text
    return response.json()


REPO = Path(__file__).resolve().parents[3]


def test_the_page_asks_for_exactly_the_routes_sections():
    """`OUTCOME_SECTIONS` in api.ts is the list the Timeline's cards name; a
    section the route does not know is a 422 on every card that asks for it."""
    source = (REPO / "apps/swarm-ui/src/api.ts").read_text()
    m = re.search(r"export const OUTCOME_SECTIONS = \[(.*?)\] as const", source, flags=re.DOTALL)
    assert m, "OUTCOME_SECTIONS is not in api.ts; this check would be vacuous"
    assert re.findall(r"'(\w+)'", m.group(1)) == list(SECTIONS)


def test_the_sections_are_the_keys_the_full_response_computes():
    assert set(SECTIONS) == set(FULL_KEYS) - set(ENVELOPE)
    assert [k for k in FULL_KEYS if k in SECTIONS] == list(SECTIONS), "SECTIONS is in the response's order"


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize(
    ("user", "extra"),
    [("alice", {}), ("root", {"scope": "platform"}), ("root", {"scope": "platform", "group": "tenant_id", "kind": "steps"})],
)
def test_each_section_alone_is_that_key_of_the_full_response(warmed, tokens, group_map, clock, section, user, extra):
    full = ok(fresh(warmed, tokens, group_map, clock), user, **WEEK, **extra)
    part = ok(fresh(warmed, tokens, group_map, clock), user, **WEEK, **extra, section=section)
    assert list(part) == [k for k in FULL_KEYS if k in ENVELOPE or k == section]
    assert part[section] == full[section]
    for key in ENVELOPE:
        if key != "reads":
            assert part[key] == full[key], key


def test_several_sections_are_served_together_in_the_responses_order(warmed, tokens, group_map, clock):
    full = ok(fresh(warmed, tokens, group_map, clock), "alice", **WEEK)
    part = ok(
        fresh(warmed, tokens, group_map, clock), "alice", **WEEK,
        section=["retries", "totals", "buckets", "retries", " "],
    )
    assert list(part) == [k for k in FULL_KEYS if k in ENVELOPE or k in ("buckets", "totals", "retries")]
    for key in ("buckets", "totals", "retries"):
        assert part[key] == full[key]


def test_no_section_is_the_full_response_unchanged(warmed, tokens, group_map, clock):
    plain = read(fresh(warmed, tokens, group_map, clock), "alice", **WEEK)
    assert plain.status_code == 200, plain.text
    assert list(plain.json()) == FULL_KEYS
    empty = read(fresh(warmed, tokens, group_map, clock), "alice", **WEEK, section="")
    assert empty.content == plain.content, "an empty section is no section"
    every = read(fresh(warmed, tokens, group_map, clock), "alice", **WEEK, section=list(SECTIONS))
    assert every.content == plain.content, "naming every section is the full response, byte for byte"


def test_an_unknown_section_is_the_one_validation_envelope(warmed, tokens, group_map, clock):
    response = read(fresh(warmed, tokens, group_map, clock), "alice", **WEEK, section=["totals", "reads"])
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["detail"]["parameter"] == "section"
    assert body["detail"]["value"] == "reads", "the envelope is always sent and is not a section"
    assert body["detail"]["allowed"] == list(SECTIONS)
    assert set(body) >= {"code", "message", "detail"}


@pytest.mark.parametrize(
    "params",
    [
        {"scope": "platform", "section": "totals"},
        {"tenant": "research", "section": "nope"},
        {"group": "tenant_id", "section": "groups"},
    ],
)
def test_a_non_admin_still_cannot_ask_for_platform_scope_by_naming_a_section(warmed, tokens, group_map, clock, params):
    """No tz on purpose: the 403 comes before any parameter -- `section` included -- is validated."""
    response = read(fresh(warmed, tokens, group_map, clock), "alice", **params)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_every_section_of_one_query_is_served_from_one_fold(warmed, tokens, group_map, clock):
    """Owner decision 2026-09-30: the cache holds the FOLD, keyed by the query
    without its sections, and every section is a projection of it. A card
    arriving in the same minute as another reuses its scan; it never pays for
    the day documents again, and what it is served is still only what it asked
    for."""
    api = fresh(warmed, tokens, group_map, clock)
    part = ok(api, "alice", **WEEK, section="retries")
    assert "totals" not in part and part["cached"] is False and part["reads"] > 0
    full = ok(api, "alice", **WEEK)
    assert list(full) == FULL_KEYS
    assert full["generated_at"] == part["generated_at"], "the fold keeps its real age"
    reference = ok(fresh(warmed, tokens, group_map, clock), "alice", **WEEK)
    for key in SECTIONS:
        assert full[key] == reference[key], key
    again = ok(api, "alice", **WEEK, section="retries")
    assert list(again) == [k for k in FULL_KEYS if k in ENVELOPE or k == "retries"]
    assert again["cached"] is True and again["reads"] == 0
    latency = ok(api, "alice", **WEEK, section=["latency"])
    assert latency["cached"] is True and latency["reads"] == 0, "a section of a fold already made is free"
    assert latency["latency"] == reference["latency"]


def test_the_fold_is_never_shared_across_tenants_or_queries(warmed, tokens, group_map, clock):
    api = fresh(warmed, tokens, group_map, clock)
    mine = ok(api, "alice", **WEEK, section="totals")
    theirs = ok(api, "bob", **WEEK, section="totals")
    assert theirs["cached"] is False, "another tenant's fold is never served"
    assert theirs["scope"] != mine["scope"]
    other = ok(api, "alice", **{**WEEK, "kind": "steps"}, section="totals")
    assert other["cached"] is False, "a different filter is a different fold"


def test_the_latency_section_carries_the_wait_it_leaves_out(warmed, tokens, group_map, clock):
    """The latency card prints `wait_excluded`; it gets it with `latency`
    rather than asking for the whole of `coverage` (a count() per terminal
    state per tenant) to read one number."""
    full = ok(fresh(warmed, tokens, group_map, clock), "alice", **WEEK)
    part = ok(fresh(warmed, tokens, group_map, clock), "alice", **WEEK, section="latency")
    assert part["latency"]["wait_excluded"] == full["coverage"]["wait_excluded"]
    source = (REPO / "apps/swarm-ui/src/Activity.tsx").read_text()
    assert "coverage" not in _page_sections(source)[1]["latency"]


def _reads(db, tokens, group_map, clock, **params: Any) -> int:
    return ok(fresh(db, tokens, group_map, clock), "alice", **WEEK, **params)["reads"]


def test_a_section_reads_only_what_it_shows(warmed, tokens, group_map, clock):
    """The day documents of the span are the one scan every section needs; the
    previous span's days, the terminal count and the workflow enrichment are
    read only for the section that shows them."""
    scan = _reads(warmed, tokens, group_map, clock, section="totals")
    assert scan > 0
    for section in ("buckets", "retries", "latency", "groups"):
        assert _reads(warmed, tokens, group_map, clock, section=section) == scan, section
    assert _reads(warmed, tokens, group_map, clock, section="previous") > scan
    assert _reads(warmed, tokens, group_map, clock, section="coverage") == scan + 4, "one count() per terminal state"
    assert _reads(warmed, tokens, group_map, clock, section="workflows_failed") > scan
    full = _reads(warmed, tokens, group_map, clock)
    assert full == (
        _reads(warmed, tokens, group_map, clock, section="previous")
        + (_reads(warmed, tokens, group_map, clock, section="coverage") - scan)
        + (_reads(warmed, tokens, group_map, clock, section="workflows_failed") - scan)
    ), "the full read is the scan plus each section's own extra reads, and nothing else"


# --------------------------------------------------------------------------
# A cold store: a section is the same value alone as in the full response
# --------------------------------------------------------------------------

def _cold(tokens, group_map, clock) -> TestClient:
    """A new store, seeded and never read: every day is derived by the read."""
    db = FakeFirestore()
    seed_week(db)
    return fresh(db, tokens, group_map, clock)


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize(("user", "extra"), [("alice", {}), ("root", {"scope": "platform"})])
def test_each_section_alone_is_that_key_of_the_full_response_on_a_cold_store(tokens, group_map, clock, section, user, extra):
    """The review of #391: `coverage` alone on a cold store reported a smaller
    `derived_now` than the full read, which also derived the previous span. A
    section's value may not depend on what else was asked with it."""
    full = ok(_cold(tokens, group_map, clock), user, **WEEK, **extra)
    part = ok(_cold(tokens, group_map, clock), user, **WEEK, **extra, section=section)
    assert full["coverage"]["derived_now"] > 0, "the store was not cold; this check would be vacuous"
    assert part[section] == full[section], section
    for key in ENVELOPE:
        if key not in ("reads", "cached"):
            assert part[key] == full[key], key


# --------------------------------------------------------------------------
# What the whole Timeline page costs, counted at the store
# --------------------------------------------------------------------------

def _page_sections(source: str) -> tuple[list[str], dict[str, list[str]]]:
    """LEDGER_SECTIONS and CARD_SECTIONS as Activity.tsx states them -- read
    from the page, so a card that starts asking for more is priced here."""
    ledger = re.search(r"const LEDGER_SECTIONS = \[(.*?)\] as const", source, flags=re.DOTALL)
    cards = re.search(r"const CARD_SECTIONS = \{(.*?)\} as const", source, flags=re.DOTALL)
    assert ledger and cards, "the page's section lists moved; this check would be vacuous"
    parsed = {
        name: re.findall(r"'(\w+)'", body)
        for name, body in re.findall(r"(\w+): \[(.*?)\]", cards.group(1), flags=re.DOTALL)
    }
    assert len(parsed) == 7, parsed
    return re.findall(r"'(\w+)'", ledger.group(1)), parsed


class Counter:
    """Every document the store hands back, as Firestore bills it: one per
    document fetched, one per row a query streams (at least one per query),
    one per count() aggregation."""

    def __init__(self, monkeypatch) -> None:
        self.n = 0
        counter = self
        doc_get = FakeDocumentRef.get
        rows = FakeQuery._rows
        count = FakeQuery.count

        def get(ref, *a, **k):
            counter.n += 1
            return doc_get(ref, *a, **k)

        def stream(query, *a, **k):
            out = list(rows(query))
            counter.n += max(1, len(out))
            return iter(out)

        def listed(query, *a, **k):
            return list(stream(query))

        def counted(query, *a, **k):
            counter.n += 1
            return count(query, *a, **k)

        monkeypatch.setattr(FakeDocumentRef, "get", get)
        monkeypatch.setattr(FakeQuery, "stream", stream)
        monkeypatch.setattr(FakeQuery, "get", listed)
        monkeypatch.setattr(FakeQuery, "count", counted)

    def cost(self, fn) -> int:
        before = self.n
        fn()
        return self.n - before


@pytest.mark.parametrize("order", ["headline_first", "cards_first"])
@pytest.mark.parametrize(("user", "extra"), [("alice", {}), ("root", {"scope": "platform"})])
def test_the_whole_timeline_page_costs_one_full_read(warmed, tokens, group_map, clock, monkeypatch, order, user, extra):
    """Owner decision 2026-09-30: cut the cost of #391 before merging it. The
    review measured a fully scrolled page at 73 reads (identical asks shared)
    and 91 (not shared) against 24 for the one full read it replaced.

    THE BOUND: headline + all seven cards on one query, in one minute, cost at
    most the old full read plus, for each of the seven extra requests, what a
    request costs that the fold serves entirely -- authentication, the tenant
    check and, in platform scope, the tenant listing that is part of the
    cache key. Those are per request by design (the listing is what keeps one
    tenant set's fold from being served for another) and do not grow with the
    span. Measured here, not assumed: `overhead` is a repeat of the full read
    in the same minute. Everything the outcome service reads beyond that --
    the day documents, the previous span, the terminal counts, the workflow
    lookups -- is paid once per page, which the per-card `reads` pins too."""
    ledger, cards = _page_sections((REPO / "apps/swarm-ui/src/Activity.tsx").read_text())
    counter = Counter(monkeypatch)
    old = fresh(warmed, tokens, group_map, clock)
    full = counter.cost(lambda: ok(old, user, **WEEK, **extra))
    overhead = counter.cost(lambda: ok(old, user, **WEEK, **extra))
    assert 0 <= overhead < full, (overhead, full)

    page = fresh(warmed, tokens, group_map, clock)
    asks = [ledger, *cards.values()]
    if order == "cards_first":
        asks = asks[::-1]
    served = []

    def scroll_the_whole_page() -> None:
        for sections in asks:
            served.append(ok(page, user, **WEEK, **extra, section=sections))

    cost = counter.cost(scroll_the_whole_page)
    assert cost <= full + 7 * overhead, (cost, full, overhead)
    assert sum(1 for body in served if body["cached"] is False) == 1, "one scan per page"
    if extra.get("scope") != "platform":
        # The service's own meter: nothing it reads is paid twice.
        assert sum(body["reads"] for body in served) == ok(fresh(warmed, tokens, group_map, clock), user, **WEEK)["reads"]
