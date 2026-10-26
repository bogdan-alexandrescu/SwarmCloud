"""The Outcomes headline never waits on `coverage` or `previous` (#2422).

VQ-021 (#2391) measured `GET /v1/outcomes`, the read behind Work › Timeline ›
Outcomes' headline, at 22.6 s at p95; VQ-TIMELINE found the slow part was the
two sections the headline asked for beside its figure: `coverage`, a count()
per terminal state per tenant, run one after another, and `previous`, a second
read of the span before. Two changes, both held here through the real app over
FakeFirestore, counting queries rather than timing them:

  * the page's first read (`LEDGER_SECTIONS` in Activity.tsx) asks for neither,
    and reads them in a second request after first paint
    (`LEDGER_LATE_SECTIONS`), which the fold cache serves from the same scan;
  * `coverage`'s terminal count is one count() per chunk of seven tenants, not
    one per terminal state per tenant -- the same number, as a reference
    implementation of the old loop shows on the same store.

And the cache that makes the second read cheap never hands one tenant's
coverage or previous span to another.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_api import outcomes as outcomes_module
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.store import TASKS

from .conftest import auth_header, seed_tenant
from .fakes import FakeFirestore, FakeQuery
from .test_outcomes_route import Clock, _context, at, seed_week, task

REPO = Path(__file__).resolve().parents[3]
WEEK = {"tz": "UTC", "span": "7d", "compare": "previous"}
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")


@pytest.fixture
def clock() -> Clock:
    return Clock()


def fresh(db, tokens, group_map, clock) -> TestClient:
    """A new service with an empty fold cache, over the same store."""
    return TestClient(create_app(_context(db, tokens, StaticGroups(group_map), clock)), raise_server_exceptions=False)


def ok(api: TestClient, user: str, **params: Any) -> dict[str, Any]:
    response = api.get("/v1/outcomes", params=params, headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


class Counts:
    """Every count() aggregation the store runs, with the filters it ran under."""

    def __init__(self, monkeypatch) -> None:
        self.queries: list[list[tuple[str, Any, Any]]] = []
        count = FakeQuery.count
        seen = self

        def counted(query, *a, **k):
            seen.queries.append(list(query._filters))
            return count(query, *a, **k)

        monkeypatch.setattr(FakeQuery, "count", counted)

    def during(self, fn) -> list[list[tuple[str, Any, Any]]]:
        before = len(self.queries)
        fn()
        return self.queries[before:]


def page_sections() -> tuple[list[str], list[str]]:
    """LEDGER_SECTIONS and LEDGER_LATE_SECTIONS as Activity.tsx states them."""
    source = (REPO / "apps/swarm-ui/src/Activity.tsx").read_text()
    first = re.search(r"const LEDGER_SECTIONS = \[(.*?)\] as const", source, flags=re.DOTALL)
    late = re.search(r"const LEDGER_LATE_SECTIONS = \[(.*?)\] as const", source, flags=re.DOTALL)
    assert first, "LEDGER_SECTIONS moved; this check would be vacuous"
    assert late, "the page has no second read for coverage and previous (LEDGER_LATE_SECTIONS)"
    return re.findall(r"'(\w+)'", first.group(1)), re.findall(r"'(\w+)'", late.group(1))


# --------------------------------------------------------------------------
# A store with many tenants, each with tasks the ledger cannot place
# --------------------------------------------------------------------------

MANY = [f"w{i:02d}" for i in range(12)]


def seed_many(db: FakeFirestore) -> None:
    """`seed_week`, plus twelve tenants each holding a different number of
    terminal tasks with no completed_at, across every terminal state, and
    open tasks with none (which are not counted)."""
    seed_week(db)
    task(db, "r_orphan", tenant="research", state="DEAD_LETTERED", created=at(10, 9), completed=None)
    for n, tenant in enumerate(MANY):
        seed_tenant(db, tenant)
        for i in range(n % 5 + 1):
            task(db, f"{tenant}_orphan_{i}", tenant=tenant, state=TERMINAL[(n + i) % 4],
                 created=at(10, 9, i), completed=None)
        task(db, f"{tenant}_open", tenant=tenant, state="RUNNING", created=at(24, 9), completed=None)
        task(db, f"{tenant}_done", tenant=tenant, state="SUCCEEDED", created=at(23, 9), completed=at(23, 10))


def old_terminal_count(db: FakeFirestore, tenants: list[str] | None) -> int:
    """The implementation #2422 replaced, verbatim in effect: one count() per
    terminal state per tenant (or per state alone for the whole platform)."""
    total = 0
    scopes: list[str | None] = [None] if tenants is None else list(tenants)
    for tenant_id in scopes:
        for state in sorted(TERMINAL):
            query: Any = db.collection(TASKS)
            if tenant_id is not None:
                query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            query = query.where(filter=FieldFilter("state", "==", state)).where(
                filter=FieldFilter("completed_at", "==", None)
            )
            total += int(query.count().get()[0][0].value)
    return total


def disjunctions(filters: list[tuple[str, Any, Any]]) -> int:
    product = 1
    for _field, op, value in filters:
        if op == "in":
            product *= len(value)
    return product


# --------------------------------------------------------------------------
# (1) The headline's reads do not grow with terminal states x workspaces
# --------------------------------------------------------------------------

def test_the_headline_asks_for_neither_coverage_nor_previous(db, tokens, group_map, clock, monkeypatch):
    """The page's first read is only what the figure and the drawing need; the
    two sections that cost reads of their own come in the second."""
    first, late = page_sections()
    assert "coverage" not in first and "previous" not in first, first
    assert sorted(late) == ["coverage", "previous"], late
    seed_many(db)
    fresh(db, tokens, group_map, clock).get("/v1/outcomes", params={**WEEK, "scope": "platform"}, headers=auth_header("root"))
    counts = Counts(monkeypatch)
    for user, extra in (("alice", {}), ("root", {"scope": "platform", "exclude_tenant": "research"})):
        scan = ok(fresh(db, tokens, group_map, clock), user, **WEEK, **extra, section="totals")["reads"]
        api = fresh(db, tokens, group_map, clock)
        ran = counts.during(lambda: ok(api, user, **WEEK, **extra, section=first))
        assert ran == [], "the headline ran a count()"
        assert ok(fresh(db, tokens, group_map, clock), user, **WEEK, **extra, section=first)["reads"] == scan, (
            "the headline read more than the span's own scan"
        )


@pytest.mark.parametrize(
    ("user", "extra", "tenants"),
    [
        ("alice", {}, 1),
        ("root", {"scope": "platform"}, None),
        ("root", {"scope": "platform", "exclude_tenant": "research"}, len(MANY) + 1),
        ("root", {"scope": "platform", "tenant": ["eng", "w03"]}, 2),
    ],
)
def test_the_terminal_count_is_one_query_per_chunk_of_tenants(db, tokens, group_map, clock, monkeypatch, user, extra, tenants):
    """It was four count()s per tenant, one after another: 52 for this
    platform view of 13 tenants. Now it is one per seven tenants, one in
    tenant scope and one for the unfiltered platform -- and no query asks
    Firestore for more than its 30 disjunctions."""
    seed_many(db)
    counts = Counts(monkeypatch)
    api = fresh(db, tokens, group_map, clock)
    ran = counts.during(lambda: ok(api, user, **WEEK, **extra, section="coverage"))
    expected = 1 if tenants is None else math.ceil(tenants / 7)
    assert len(ran) == expected, ran
    for filters in ran:
        assert disjunctions(filters) <= 30, filters
        if tenants is not None:
            assert any(field == "tenant_id" for field, _, _ in filters), "a tenant-scoped count dropped its tenant filter"


# --------------------------------------------------------------------------
# (2) `previous` is one fold over the span before
# --------------------------------------------------------------------------

def test_previous_is_computed_with_one_fold_and_one_read_of_its_days(db, tokens, group_map, clock, monkeypatch):
    """Asked for after the headline (the page's second read), `previous` folds
    the span before once, reads its stored days in one batch, and never
    refolds the main span."""
    seed_many(db)
    ok(fresh(db, tokens, group_map, clock), "alice", **WEEK)  # warm: every day stored
    api = fresh(db, tokens, group_map, clock)
    ok(api, "alice", **WEEK, section=["buckets", "totals"])
    calls = {"gather": 0, "fold": 0, "stored": 0}
    gather, fold_init = outcomes_module._gather, outcomes_module._Fold.__init__
    read_stored = outcomes_module.Outcomes._read_stored

    def spy_gather(*a, **k):
        calls["gather"] += 1
        return gather(*a, **k)

    def spy_fold(self, *a, **k):
        calls["fold"] += 1
        return fold_init(self, *a, **k)

    def spy_stored(self, *a, **k):
        calls["stored"] += 1
        return read_stored(self, *a, **k)

    monkeypatch.setattr(outcomes_module, "_gather", spy_gather)
    monkeypatch.setattr(outcomes_module._Fold, "__init__", spy_fold)
    monkeypatch.setattr(outcomes_module.Outcomes, "_read_stored", spy_stored)
    body = ok(api, "alice", **WEEK, section=["coverage", "previous"])
    assert body["previous"] is not None and body["previous"]["complete"] is True
    assert calls == {"gather": 1, "fold": 0, "stored": 1}, calls


# --------------------------------------------------------------------------
# (3) The figures are unchanged
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("user", "extra", "tenants"),
    [
        ("alice", {}, ["eng"]),
        ("bob", {}, ["research"]),
        ("root", {"scope": "platform"}, None),
        ("root", {"scope": "platform", "exclude_tenant": "research"}, None),
        ("root", {"scope": "platform", "tenant": ["eng", "w01", "w04"]}, ["eng", "w01", "w04"]),
    ],
)
def test_the_terminal_count_is_the_number_the_per_state_count_gave(db, tokens, group_map, clock, user, extra, tenants):
    seed_many(db)
    if tenants is None and "exclude_tenant" in extra:
        tenants = sorted(["eng", *MANY])
    body = ok(fresh(db, tokens, group_map, clock), user, **WEEK, **extra, section="coverage")
    expected = old_terminal_count(db, tenants)
    assert expected > 0, "nothing to count; this check would be vacuous"
    assert body["coverage"]["terminal_without_completed_at"] == expected


def test_the_split_read_serves_the_figures_the_one_read_served(db, tokens, group_map, clock):
    """The headline then its second read, on one service, carry the same
    sections as one full read on another: the split changes when a figure
    arrives, never what it is."""
    seed_many(db)
    ok(fresh(db, tokens, group_map, clock), "root", **WEEK, scope="platform")
    first, late = page_sections()
    for user, extra in (("alice", {}), ("root", {"scope": "platform", "exclude_tenant": "w02"})):
        whole = ok(fresh(db, tokens, group_map, clock), user, **WEEK, **extra)
        api = fresh(db, tokens, group_map, clock)
        head = ok(api, user, **WEEK, **extra, section=first)
        rest = ok(api, user, **WEEK, **extra, section=late)
        for name in first:
            assert head[name] == whole[name], name
        for name in late:
            assert rest[name] == whole[name], name
        assert rest["generated_at"] == head["generated_at"], "the second read is a projection of the same fold"


def test_a_failed_count_is_null_never_zero(db, tokens, group_map, clock, monkeypatch):
    seed_many(db)

    def refuse(query, *a, **k):
        raise RuntimeError("count refused")

    monkeypatch.setattr(FakeQuery, "count", refuse)
    body = ok(fresh(db, tokens, group_map, clock), "alice", **WEEK, section="coverage")
    assert body["coverage"]["terminal_without_completed_at"] is None


# --------------------------------------------------------------------------
# (4) Two tenants never share a cached figure
# --------------------------------------------------------------------------

def test_two_tenants_never_share_a_cached_coverage_or_previous(db, tokens, group_map, clock, monkeypatch):
    """eng and research hold different orphans and different spans before. On
    ONE service (one fold cache), in the same minute, each reads its own
    second-read sections; each matches what a fresh service serves that tenant
    alone, and neither matches the other's."""
    seed_week(db)
    for i in range(3):
        task(db, f"r_orphan_{i}", tenant="research", state="CANCELLED", created=at(10, 9, i), completed=None)
    task(db, "r_prev", tenant="research", state="FAILED", created=at(14, 9), completed=at(14, 10))
    task(db, "r_prev2", tenant="research", state="FAILED", created=at(15, 9), completed=at(15, 10))
    first, late = page_sections()
    for user in ("alice", "bob"):  # warm: every day stored, so `derived_now` is 0 on every service below
        ok(fresh(db, tokens, group_map, clock), user, **WEEK)
    alone = {
        user: ok(fresh(db, tokens, group_map, clock), user, **WEEK, section=late)
        for user in ("alice", "bob")
    }
    assert alone["alice"]["coverage"] != alone["bob"]["coverage"]
    assert alone["alice"]["previous"] != alone["bob"]["previous"]
    assert alone["alice"]["coverage"]["terminal_without_completed_at"] == 1
    assert alone["bob"]["coverage"]["terminal_without_completed_at"] == 3

    shared = fresh(db, tokens, group_map, clock)
    for order in (("alice", "bob"), ("bob", "alice")):
        for user in order:
            ok(shared, user, **WEEK, section=first)
            got = ok(shared, user, **WEEK, section=late)
            assert got["scope"] == {"kind": "tenant", "tenant_id": "eng" if user == "alice" else "research"}
            for name in late:
                assert got[name] == alone[user][name], (user, name)

    # The platform view of one tenant counts that tenant's orphans only.
    only = ok(fresh(db, tokens, group_map, clock), "root", **WEEK, scope="platform", tenant="research", section="coverage")
    assert only["coverage"]["terminal_without_completed_at"] == 3
