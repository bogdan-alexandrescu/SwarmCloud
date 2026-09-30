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
    that shows them.

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


def test_a_cached_full_payload_is_never_served_for_a_section_or_the_reverse(warmed, tokens, group_map, clock):
    api = fresh(warmed, tokens, group_map, clock)
    part = ok(api, "alice", **WEEK, section="retries")
    assert "totals" not in part
    full = ok(api, "alice", **WEEK)
    assert list(full) == FULL_KEYS and full["cached"] is False
    again = ok(api, "alice", **WEEK, section="retries")
    assert list(again) == [k for k in FULL_KEYS if k in ENVELOPE or k == "retries"]
    assert again["cached"] is True, "the same section set, in the same minute, is a cache hit"
    assert ok(api, "alice", **WEEK, section=["latency"])["cached"] is False


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
