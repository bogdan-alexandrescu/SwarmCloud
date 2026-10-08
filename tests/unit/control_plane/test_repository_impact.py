"""The commit / pull-request impact query (docs/repo-index.md §4.3a, lane RI11).

diff -> changed symbols (by line range) -> bounded transitive callers ->
covering tests -> a test plan with a reason per test, and `fallback_triggers`:
the conditions under which the graph cannot speak for the change, any one of
which selects the full suite and says why.

What is held here:

  * a LEAF change selects exactly the tests that reach it, with no trigger;
  * a HUB change stops at the depth bound and at the node bound, and records
    where it stopped (`bound`) and where a path fell below the confidence
    floor (`low_confidence_cut`), so a short answer never reads as complete;
  * EACH fallback trigger of §4.3a, one test each, selects the full suite;
  * the same diff gives byte-identical output, whatever order GitHub lists it;
  * the routes are tenant-scoped: another tenant's repo_id is a 404 before any
    forge read, and the diff is read with the R2-resolved token
    (repository > tenant).

Fixture graphs are written by the shipped shard writer and read back through
`repograph`, so the query runs over exactly the shards an index run stores.
Every token is built at runtime; every sha is made from a word.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import pytest

from swarm_api import forge, impact, repograph
from swarm_api.gittokens import Scope, record_for_slot
from swarm_api.objects import InMemoryObjectReader

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

BASE, HEAD, OTHER = sha("impact-base"), sha("impact-head"), sha("impact-other")
REPO = "repo_" + "c" * 16
WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")
FULL_SUITE = "uv run pytest tests -q"
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# fixture graphs
# --------------------------------------------------------------------------

def sym(path: str, qual: str, start: int, end: int, kind: str = "function") -> dict:
    return {"id": f"{path}#{qual}", "kind": kind, "path": path, "start_line": start,
            "end_line": end, "language": "python", "exported": True}


def edge(frm: str, to: str, confidence: float = 0.95, evidence: str = "lsp",
         kind: str = "call") -> dict:
    return {"from": frm, "to": to, "kind": kind, "evidence": evidence,
            "confidence": confidence, "also_evidence": [], "path": frm.split("#")[0],
            "line": 1, "sites": 1}


def language_row(status: str = "ok", language: str = "python") -> dict:
    return {"language": language, "files": 1, "grammar": f"tree-sitter-{language}",
            "server": "pyright", "status": status, "reason": None, "fallback": "ast",
            "parsed": 1, "timed_out": 0, "failed": 0, "too_large": 0, "over_budget": 0}


def graph_document(commit: str, symbols: list[dict], edges: list[dict],
                   test_map: list[dict] | None = None,
                   languages: list[dict] | None = None) -> dict:
    paths = sorted({s["path"] for s in symbols})
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": commit,
        "branch": "main", "base_sha": None,
        "languages": languages if languages is not None else [language_row()],
        "truncated": [], "extractor": {"name": "swarm-repo-index", "version": "1"},
        "symbols": symbols, "call_edges": edges, "symbol_test_map": test_map or [],
        "files": [{"path": p, "language": "python", "lines": 100, "bytes": 1000,
                   "status": "parsed", "reason": None, "test": p.startswith("tests/")}
                  for p in paths],
    }


ORDERS = "src/shop/orders.py"
CHECKOUT = "src/shop/checkout.py"
FORMAT = "src/shop/format.py"
T_ORDERS = "tests/shop/test_orders.py"
T_FORMAT = "tests/shop/test_format.py"

TOTAL = f"{ORDERS}#OrderService.total"
ADD = f"{ORDERS}#OrderService.add"
REFUND = f"{ORDERS}#OrderService.refund"
CHECKOUT_TOTAL = f"{CHECKOUT}#checkout_total"
FMT = f"{FORMAT}#fmt"
TEST_TOTAL = f"{T_ORDERS}#test_empty_cart_total"
TEST_ADD = f"{T_ORDERS}#test_add"
TEST_REFUND = f"{T_ORDERS}#test_refund"
TEST_FMT = f"{T_FORMAT}#test_fmt"


def shop_graph(commit: str = BASE, languages: list[dict] | None = None) -> dict:
    """A small shop: OrderService.total is reached at depth 2, fmt is a leaf,
    refund is reached only through an `ast` edge at 0.3."""
    symbols = [
        sym(ORDERS, "OrderService", 1, 40, kind="class"),
        sym(ORDERS, "OrderService.total", 10, 20, kind="method"),
        sym(ORDERS, "OrderService.add", 22, 30, kind="method"),
        sym(ORDERS, "OrderService.refund", 32, 38, kind="method"),
        sym(CHECKOUT, "checkout_total", 5, 15),
        sym(FORMAT, "fmt", 1, 5),
        sym(T_ORDERS, "test_empty_cart_total", 3, 8, kind="test"),
        sym(T_ORDERS, "test_add", 10, 14, kind="test"),
        sym(T_ORDERS, "test_refund", 16, 20, kind="test"),
        sym(T_FORMAT, "test_fmt", 1, 4, kind="test"),
    ]
    edges = [
        edge(TEST_TOTAL, CHECKOUT_TOTAL),
        edge(CHECKOUT_TOTAL, TOTAL),
        edge(TEST_ADD, ADD, 0.6, "ast"),
        edge(TEST_REFUND, REFUND, 0.3, "ast"),
        edge(TEST_FMT, FMT),
        edge(CHECKOUT, ORDERS, 0.4, "import", kind="import"),
    ]
    test_map = [
        {"symbol": TOTAL, "test": TEST_TOTAL, "depth": 2, "confidence": 0.902},
        {"symbol": CHECKOUT_TOTAL, "test": TEST_TOTAL, "depth": 1, "confidence": 0.95},
        {"symbol": ADD, "test": TEST_ADD, "depth": 1, "confidence": 0.6},
        {"symbol": REFUND, "test": TEST_REFUND, "depth": 1, "confidence": 0.3},
        {"symbol": FMT, "test": TEST_FMT, "depth": 1, "confidence": 0.95},
    ]
    return graph_document(commit, symbols, edges, test_map, languages)


def hub_graph(commit: str = BASE) -> dict:
    """A hub with a chain of eight callers, twelve direct callers, and a weak branch."""
    hub = "src/core/hub.py#hub"
    symbols = [sym("src/core/hub.py", "hub", 1, 30)]
    edges = []
    previous = hub
    for i in range(1, 9):
        path = f"src/chain/c{i}.py"
        symbols.append(sym(path, f"c{i}", 1, 10))
        edges.append(edge(f"{path}#c{i}", previous))
        previous = f"{path}#c{i}"
    symbols.append(sym("tests/chain/test_c5.py", "test_c5", 1, 5, kind="test"))
    edges.append(edge("tests/chain/test_c5.py#test_c5", "src/chain/c5.py#c5"))
    for i in range(1, 13):
        path = f"src/fan/f{i:02d}.py"
        symbols.append(sym(path, f"f{i:02d}", 1, 10))
        symbols.append(sym(f"tests/fan/test_f{i:02d}.py", f"test_f{i:02d}", 1, 5, kind="test"))
        edges.append(edge(f"{path}#f{i:02d}", hub))
        edges.append(edge(f"tests/fan/test_f{i:02d}.py#test_f{i:02d}", f"{path}#f{i:02d}"))
    symbols += [sym("src/weak/w.py", "weak", 1, 10), sym("src/weak/w2.py", "weak2", 1, 10)]
    edges += [edge("src/weak/w.py#weak", hub, 0.3, "ast"),
              edge("src/weak/w2.py#weak2", "src/weak/w.py#weak", 0.6, "ast")]
    return graph_document(commit, symbols, edges)


def open_graph(writer, objects: InMemoryObjectReader, tmp_path: Path, document: dict,
               tenant: str = "eng", repo_id: str = REPO) -> repograph.Graph:
    root = tmp_path / f"store-{tenant}-{document['commit_sha'][:8]}-{secrets.token_hex(4)}"
    report = writer.write_graph(document, writer.LocalStore(root), tenant_id=tenant,
                                repo_id=repo_id)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    version = {"commit_sha": document["commit_sha"], "graph_manifest": report["manifest"],
               "graph_digest": report["manifest_digest"]}
    return repograph.RepoGraph(lambda: objects).open(tenant, repo_id, version)


def impact_index(commit: str = BASE, **overrides: Any) -> dict:
    document = fixture_index(
        commit,
        test_layout=[
            {"root": "tests/shop", "framework": "pytest",
             "command": "uv run pytest tests/shop -q", "covers": ["src/shop/**"]},
            {"root": "tests", "framework": "pytest", "command": FULL_SUITE,
             "covers": ["src/**"]},
        ],
        test_map=[
            {"source": "src/shop/*.py", "test": "tests/shop/test_orders.py",
             "evidence": "import"},
        ],
        always_tests=[],
        commands=[{"name": "test", "kind": "test", "command": "make test",
                   "source": "Makefile"}],
    )
    document.update(overrides)
    return document


CURRENT = {"state": "current", "stale": False, "index_sha": BASE, "head_sha": BASE,
           "behind_by": 0, "reason": None}
STALE = {"state": "stale", "stale": True, "index_sha": BASE, "head_sha": OTHER,
         "behind_by": 240, "reason": "more than 200 commits behind the head"}


def hunk(old_start: int, new_start: int, lines: list[str]) -> str:
    """One unified-diff hunk; each line starts with ' ', '-' or '+'."""
    old = sum(1 for line in lines if line[0] in " -")
    new = sum(1 for line in lines if line[0] in " +")
    return f"@@ -{old_start},{old} +{new_start},{new} @@\n" + "\n".join(lines) + "\n"


def changed_line(path: str, line: int) -> impact.DiffFile:
    """`line` of `path` rewritten in place."""
    return impact.DiffFile(path=path, status="modified",
                           patch=hunk(line, line, ["-old", "+new"]))


def plan(diff_files: list[impact.DiffFile], graph: repograph.Graph | None, *,
         document: dict | None = None, fresh: dict | None = None, head=None,
         truncated: bool = False, **kwargs: Any) -> dict:
    diff = impact.Diff(base_sha=BASE, head_sha=HEAD, files=tuple(diff_files),
                       truncated=truncated)
    return impact.plan_impact(diff, base=graph, head=head,
                              document=document if document is not None else impact_index(),
                              fresh=fresh or CURRENT, index_sha=BASE, **kwargs)


def ids(entries: list[dict], key: str = "id") -> list[str]:
    return [entry[key] for entry in entries]


def kinds(answer: dict) -> list[str]:
    return [t["kind"] for t in answer["fallback_triggers"]]


@pytest.fixture
def shop(writer, objects, tmp_path) -> repograph.Graph:
    return open_graph(writer, objects, tmp_path, shop_graph())


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------

def test_a_patch_gives_removed_base_lines_and_added_head_lines():
    parsed = impact.parse_patch(
        hunk(10, 10, [" keep", "-gone", "-gone too", "+new", " keep"])
        + hunk(40, 39, [" keep", "+inserted", "+inserted too", " keep"])
    )
    assert parsed.removed == [11, 12]
    assert parsed.added == [11, 40, 41]
    # A pure insertion is anchored between two base lines: after 40, before 41.
    assert [(i.after, i.before) for i in parsed.insertions] == [(40, 41)]


# --------------------------------------------------------------------------
# a leaf change, and a change reached at depth 2
# --------------------------------------------------------------------------

def test_a_leaf_change_selects_exactly_the_test_that_reaches_it(shop):
    answer = plan([changed_line(FORMAT, 3)], shop)
    assert ids(answer["changed"]) == [FMT]
    assert answer["changed_symbols"] == 1
    assert answer["affected"] == [] and answer["affected_callers"] == 0
    assert ids(answer["tests"]) == [TEST_FMT]
    test = answer["tests"][0]
    assert test["command"] == "uv run pytest 'tests/shop/test_format.py::test_fmt' -q"
    assert "fmt (changed)" in test["reason"]
    assert test["evidence"] == ["lsp"]
    # The control: nothing here is one the graph cannot speak for.
    assert answer["fallback_triggers"] == []
    assert answer["selection"] == "targeted" and answer["full_suite"] is None
    assert answer["selected"] == 1
    assert answer["total_tests"] == 4


def test_a_method_change_is_the_method_not_its_class_and_reaches_through_a_caller(shop):
    answer = plan([changed_line(ORDERS, 12)], shop)
    assert ids(answer["changed"]) == [TOTAL]
    assert ids(answer["affected"]) == [CHECKOUT_TOTAL]
    assert answer["affected"][0]["depth"] == 1
    test = {t["id"]: t for t in answer["tests"]}[TEST_TOTAL]
    assert test["reason"] == "reaches OrderService.total (changed) via checkout_total, depth 2"
    assert test["evidence"] == ["lsp", "lsp"]
    assert test["confidence"] == pytest.approx(0.9025, abs=0.001)
    assert test["command"] == (
        "uv run pytest 'tests/shop/test_orders.py::test_empty_cart_total' -q"
    )
    assert answer["fallback_triggers"] == []


def test_a_change_outside_every_symbol_falls_back_to_the_file_level_test_map(shop):
    # Line 45 of orders.py is past every symbol: module-level code.
    answer = plan([changed_line(ORDERS, 45)], shop)
    assert answer["changed"] == []
    file_level = {t["id"]: t for t in answer["tests"]}["tests/shop/test_orders.py"]
    assert "file-level" in file_level["reason"] and ORDERS in file_level["reason"]
    assert file_level["evidence"] == ["import"]


def test_a_changed_symbol_no_test_reaches_is_unmapped_never_silently_dropped(
    writer, objects, tmp_path
):
    document = shop_graph()
    document["symbols"].append(sym(FORMAT, "orphan", 10, 12))
    graph = open_graph(writer, objects, tmp_path, document)
    answer = plan([changed_line(FORMAT, 11)], graph)
    assert {"symbol": f"{FORMAT}#orphan", "path": FORMAT} == {
        k: v for k, v in answer["unmapped"][0].items() if k in ("symbol", "path")
    }


def test_a_changed_test_file_is_selected_itself(shop):
    answer = plan([changed_line(T_FORMAT, 2)], shop)
    test = {t["id"]: t for t in answer["tests"]}[TEST_FMT]
    assert test["reason"] == "changed in this diff"


# --------------------------------------------------------------------------
# a hub change: the depth bound, the node bound, the confidence floor
# --------------------------------------------------------------------------

@pytest.fixture
def hub(writer, objects, tmp_path) -> repograph.Graph:
    return open_graph(writer, objects, tmp_path, hub_graph())


def test_a_hub_change_stops_at_the_depth_bound_and_says_where(hub):
    answer = plan([changed_line("src/core/hub.py", 5)], hub, depth=3)
    affected = set(ids(answer["affected"]))
    assert {"src/chain/c1.py#c1", "src/chain/c2.py#c2", "src/chain/c3.py#c3"} <= affected
    assert "src/chain/c4.py#c4" not in affected
    assert answer["bound"]["depth"] == 3
    assert "src/chain/c3.py#c3" in answer["bound"]["stopped_at_depth"]
    assert answer["bound"]["node_cap_hit"] is False
    # Every fan-in caller's test is reached at depth 2; c5's test is past the bound.
    tests = set(ids(answer["tests"]))
    assert {f"tests/fan/test_f{i:02d}.py#test_f{i:02d}" for i in range(1, 13)} <= tests
    assert "tests/chain/test_c5.py#test_c5" not in tests


def test_a_path_below_the_confidence_floor_is_cut_and_recorded(hub):
    answer = plan([changed_line("src/core/hub.py", 5)], hub, depth=3)
    assert "src/weak/w.py#weak" in ids(answer["affected"])  # 0.3 >= 0.2
    cut = [(c["from"], c["to"]) for c in answer["low_confidence_cut"]]
    assert ("src/weak/w2.py#weak2", "src/weak/w.py#weak") in cut  # 0.3 * 0.6 < 0.2
    assert "src/weak/w2.py#weak2" not in ids(answer["affected"])


def test_a_hub_change_stops_at_the_node_bound_and_says_so(hub):
    answer = plan([changed_line("src/core/hub.py", 5)], hub, depth=6, max_nodes=5)
    assert answer["bound"]["node_cap_hit"] is True
    assert len(answer["affected"]) + len(answer["changed"]) <= 5 + 1


def test_a_deeper_bound_reaches_further(hub):
    answer = plan([changed_line("src/core/hub.py", 5)], hub, depth=6)
    assert "tests/chain/test_c5.py#test_c5" in ids(answer["tests"])


# --------------------------------------------------------------------------
# every fallback trigger of §4.3a selects the full suite and says why
# --------------------------------------------------------------------------

def assert_full_suite(answer: dict, kind: str) -> dict:
    assert kind in kinds(answer), answer["fallback_triggers"]
    assert answer["selection"] == "full_suite"
    assert answer["full_suite"]["command"] == FULL_SUITE
    assert kind in answer["full_suite"]["because"]
    trigger = next(t for t in answer["fallback_triggers"] if t["kind"] == kind)
    assert trigger["reason"]
    return trigger


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "package.json", "go.mod",
                                  "Makefile", ".github/workflows/ci.yml"])
def test_a_build_configuration_change_falls_back(shop, path):
    trigger = assert_full_suite(plan([changed_line(path, 1)], shop), "build_config_changed")
    assert trigger["path"] == path


@pytest.mark.parametrize("path", ["pytest.ini", "tox.ini", "vitest.config.ts",
                                  "jest.config.js"])
def test_a_test_configuration_change_falls_back(shop, path):
    trigger = assert_full_suite(plan([changed_line(path, 1)], shop), "test_config_changed")
    assert trigger["path"] == path


@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/fixtures/carts.py",
                                  "src/testutils/factory.py", "tests/shop/fixtures.py"])
def test_a_shared_fixture_change_falls_back(shop, path):
    trigger = assert_full_suite(plan([changed_line(path, 1)], shop), "shared_fixture_changed")
    assert trigger["path"] == path


def test_a_symbol_reached_only_through_a_low_confidence_edge_falls_back(shop):
    trigger = assert_full_suite(plan([changed_line(ORDERS, 34)], shop), "low_confidence_only")
    assert trigger["symbol"] == REFUND
    # The control: add's only test is at 0.6, above the 0.4 line.
    assert "low_confidence_only" not in kinds(plan([changed_line(ORDERS, 25)], shop))


@pytest.mark.parametrize("status", ["unsupported", "failing", "timed_out"])
def test_a_change_in_an_unsupported_failing_or_timed_out_language_falls_back(
    writer, objects, tmp_path, status
):
    graph = open_graph(writer, objects, tmp_path, shop_graph(languages=[language_row(status)]))
    trigger = assert_full_suite(plan([changed_line(FORMAT, 3)], graph), "unsupported_language")
    assert (trigger["path"], trigger["language"], trigger["status"]) == (FORMAT, "python", status)


def test_a_change_in_a_language_the_index_has_never_measured_falls_back(shop):
    trigger = assert_full_suite(plan([changed_line("scripts/tool.rb", 1)], shop),
                                "unsupported_language")
    assert trigger["language"] == "ruby"


def test_a_language_marked_ok_is_not_a_trigger(shop):
    assert "unsupported_language" not in kinds(plan([changed_line(FORMAT, 3)], shop))


def test_an_added_symbol_no_index_has_seen_falls_back(shop):
    added = impact.DiffFile(path=FORMAT, status="modified",
                            patch=hunk(6, 6, [" end", "+def brand_new():", "+    return 1"]))
    trigger = assert_full_suite(plan([added], shop), "unindexed_symbol")
    assert trigger["path"] == FORMAT
    assert ids(plan([added], shop)["unindexed"], "path") == [FORMAT]


def test_a_new_source_file_no_index_has_seen_falls_back(shop):
    new = impact.DiffFile(path="src/shop/coupons.py", status="added",
                          patch=hunk(0, 1, ["+def apply():", "+    return 0"]))
    trigger = assert_full_suite(plan([new], shop), "unindexed_symbol")
    assert trigger["path"] == "src/shop/coupons.py"


def test_an_added_symbol_the_head_index_has_seen_is_not_unindexed(writer, objects, tmp_path,
                                                                  shop):
    head_doc = shop_graph(commit=HEAD)
    head_doc["symbols"].append(sym(FORMAT, "brand_new", 7, 8))
    head = open_graph(writer, objects, tmp_path, head_doc)
    added = impact.DiffFile(path=FORMAT, status="modified",
                            patch=hunk(6, 6, [" end", "+def brand_new():", "+    return 1"]))
    answer = plan([added], shop, head=head)
    assert "unindexed_symbol" not in kinds(answer)
    assert f"{FORMAT}#brand_new" in ids(answer["changed"])


def test_a_stale_index_falls_back(shop):
    trigger = assert_full_suite(plan([changed_line(FORMAT, 3)], shop, fresh=STALE),
                                "stale_index")
    assert "200 commits" in trigger["reason"]


def test_no_index_at_all_falls_back_as_stale(shop):
    answer = impact.plan_impact(
        impact.Diff(base_sha=BASE, head_sha=HEAD, files=(changed_line(FORMAT, 3),)),
        base=None, head=None, document=None,
        fresh={"state": "none", "stale": False, "index_sha": None, "head_sha": None,
               "reason": "no index has been promoted for this repository yet"},
        index_sha=None,
    )
    assert "stale_index" in kinds(answer)
    assert answer["selection"] == "full_suite"


def test_a_diff_github_cut_short_falls_back(shop):
    assert_full_suite(plan([changed_line(FORMAT, 3)], shop, truncated=True), "diff_truncated")


def arrays_inside_arrays(value: Any, where: str = "$") -> list[str]:
    """Every place a list sits directly inside a list: Firestore refuses such a
    write, and the in-memory fake db does not, so the shape is checked here."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += arrays_inside_arrays(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for n, item in enumerate(value):
            if isinstance(item, (list, tuple)):
                found.append(f"{where}[{n}]")
            found += arrays_inside_arrays(item, f"{where}[{n}]")
    return found


def test_line_ranges_are_maps_so_firestore_can_store_the_plan(shop):
    files = [
        impact.DiffFile(path=FORMAT, status="modified",
                        patch=hunk(3, 3, ["-old", "-old too", "+new", "+new too"])),
        impact.DiffFile(path="src/shop/coupons.py", status="added",
                        patch=hunk(0, 1, ["+def apply():", "+    return 0"])),
    ]
    answer = plan(files, shop)
    # every range-bearing list was exercised, so an empty walk proves something
    assert answer["diff"] and answer["changed"] and answer["unindexed"]
    assert {"start": 3, "end": 4} in next(d for d in answer["diff"] if d["path"] == FORMAT)[
        "removed"]
    assert arrays_inside_arrays(answer) == []


# --------------------------------------------------------------------------
# deterministic output
# --------------------------------------------------------------------------

def test_the_same_diff_gives_byte_identical_output_in_any_order(writer, objects, tmp_path):
    files = [changed_line(FORMAT, 3), changed_line(ORDERS, 12), changed_line(ORDERS, 34),
             changed_line("pyproject.toml", 1)]
    first = plan(files, open_graph(writer, objects, tmp_path, shop_graph()))
    second = plan(list(reversed(files)), open_graph(writer, objects, tmp_path, shop_graph()))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert ids(first["tests"]) == sorted(ids(first["tests"]))


# --------------------------------------------------------------------------
# the routes
# --------------------------------------------------------------------------

class ImpactGitHub(IndexGitHub):
    """`IndexGitHub`, plus a pull request, its files, a commit and a compare with patches."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pulls: dict[int, dict[str, Any]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.diffs: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "repos" and parts[3] == "pulls":
            self.calls.append((url, dict(headers)))
            found = self.pulls.get(int(parts[4]))
            if found is None:
                return 404, b'{"message": "Not Found"}'
            if len(parts) == 6 and parts[5] == "files":
                page = int((urlparse(url).query.split("page=")[-1] or "1").split("&")[0])
                return 200, json.dumps(found["files"] if page == 1 else []).encode()
            body = {"number": int(parts[4]), "base": {"sha": found["base"]},
                    "head": {"sha": found["head"]}}
            return 200, json.dumps(body).encode()
        if len(parts) == 5 and parts[0] == "repos" and parts[3] == "commits":
            self.calls.append((url, dict(headers)))
            found = self.commits.get(parts[4])
            if found is None:
                return 404, b'{"message": "Not Found"}'
            return 200, json.dumps(found).encode()
        if len(parts) == 5 and parts[0] == "repos" and parts[3] == "compare":
            base, _, head = unquote(parts[4]).partition("...")
            if (base, head) in self.diffs:
                self.calls.append((url, dict(headers)))
                body = {"status": "ahead", "ahead_by": 1, "behind_by": 0,
                        "merge_base_commit": {"sha": base},
                        "files": self.diffs[(base, head)]}
                return 200, json.dumps(body).encode()
        return super().__call__(url, headers, timeout)


def gh_file(path: str, patch: str, status: str = "modified") -> dict:
    return {"filename": path, "status": status, "patch": patch}


class SlotTokens(TenantTokens):
    """`TenantTokens` that can also read a narrower slot, as Secret Manager does."""

    def __init__(self) -> None:
        super().__init__()
        self.slots: dict[str, str] = {}

    def read_slot(self, tenant, provider: str) -> forge.SlotValue:
        name = tenant.secret_name(provider)
        self.asked.append(name)
        if provider == forge.GIT_PROVIDER:
            return forge.SlotValue(value=self.token_for(tenant), version="1")
        if name not in self.slots:
            raise forge.NoForgeCredential(f"no value in {name}")
        return forge.SlotValue(value=self.slots[name], version="1")


@pytest.fixture
def github() -> ImpactGitHub:
    return ImpactGitHub(heads={"main": BASE})


@pytest.fixture
def forge_tokens() -> SlotTokens:
    return SlotTokens()


@pytest.fixture
def client(db, tokens, group_map, objects, github, forge_tokens):
    return make_client(db, tokens, group_map, objects, forge_tokens=forge_tokens,
                       transport=github)


@pytest.fixture
def indexed(client, db, objects, writer, tmp_path) -> dict:
    """Alice's tenant registers the repository and promotes the shop graph at BASE."""
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    task_id = started.json()["run"]["task_id"]
    tenant = db.docs[f"tasks/{task_id}"]["tenant_id"]
    root = tmp_path / "promoted"
    report = writer.write_graph(shop_graph(), writer.LocalStore(root), tenant_id=tenant,
                                repo_id=repo_id)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    finish_index_task(db, objects, task_id, impact_index(
        BASE, graph={"manifest_digest": report["manifest_digest"], "symbols": 10, "edges": 6,
                     "top_symbols": []}))
    settled = client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    assert settled.status_code == 200, settled.text
    assert db.docs[f"repositories/{repo_id}"]["index"]["current_sha"] == BASE
    return {"repo_id": repo_id, "tenant": tenant}


def _forge_calls(github: ImpactGitHub, word: str) -> list[tuple[str, dict[str, str]]]:
    return [call for call in github.calls if f"/{word}" in call[0]]


def test_a_pull_request_impact_reads_its_diff_and_stores_the_plan_per_sha(
    client, db, github, forge_tokens, indexed
):
    github.pulls[57] = {"base": BASE, "head": HEAD,
                        "files": [gh_file(FORMAT, hunk(3, 3, ["-old", "+new"]))]}
    repo_id = indexed["repo_id"]
    answer = client.post(f"/v1/repositories/{repo_id}/impact", json={"pull_request": 57},
                         headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["base_sha"], body["head_sha"], body["index_sha"]) == (BASE, HEAD, BASE)
    assert body["pull_request"] == 57
    assert ids(body["tests"]) == [TEST_FMT]
    assert body["fallback_triggers"] == [] and body["stale"] is False
    # Read with the tenant default under R2 (no repository token registered).
    assert body["read_with"]["scope"] == "tenant"
    issued = forge_tokens.issued[f"swarm-tenant-{indexed['tenant']}-git"]
    assert all(h["Authorization"] == f"Bearer {issued}" for _u, h in _forge_calls(github, "pulls"))
    assert issued not in answer.text
    stored = db.docs[f"impact_plans/{body['plan_id']}"]
    assert stored["tenant_id"] == indexed["tenant"] and stored["repo_id"] == repo_id
    assert (stored["head_sha"], stored["base_sha"], stored["pull_request"]) == (HEAD, BASE, 57)
    assert stored["selected"] == 1 and stored["fallback_triggers"] == []
    assert stored["plan"]["diff"] and arrays_inside_arrays(stored) == []
    # The same sha asked again is the same plan, not a second one.
    again = client.post(f"/v1/repositories/{repo_id}/impact", json={"pull_request": 57},
                        headers=auth_header("alice"))
    assert again.json()["plan_id"] == body["plan_id"]
    assert len([k for k in db.docs if k.startswith("impact_plans/")]) == 1


def test_a_commit_impact_compares_against_its_first_parent(client, github, indexed):
    github.commits[HEAD] = {"sha": HEAD, "parents": [{"sha": BASE}]}
    github.diffs[(BASE, HEAD)] = [gh_file(ORDERS, hunk(12, 12, ["-old", "+new"]))]
    answer = client.post(f"/v1/repositories/{indexed['repo_id']}/impact",
                         json={"commit": HEAD}, headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["base_sha"], body["head_sha"], body["commit"]) == (BASE, HEAD, HEAD)
    assert ids(body["changed"]) == [TOTAL]


def test_a_range_impact_reads_the_compare(client, github, indexed):
    github.diffs[(BASE, HEAD)] = [gh_file("pyproject.toml", hunk(1, 1, ["-a", "+b"]))]
    answer = client.post(f"/v1/repositories/{indexed['repo_id']}/impact",
                         json={"base": BASE, "head": HEAD, "depth": 2},
                         headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["depth"] == 2
    assert body["selection"] == "full_suite" and "build_config_changed" in kinds(body)


def test_a_repository_token_is_used_for_the_diff_before_the_tenant_default(
    client, db, github, forge_tokens, indexed
):
    repo_id, tenant = indexed["repo_id"], indexed["tenant"]
    record = record_for_slot(tenant, Scope.REPOSITORY, repo_id=repo_id, repository=REPOSITORY,
                             registered_by="alice@saga.xyz", now=NOW)
    db.docs[f"git_tokens/{record.token_id}"] = record.to_firestore()
    repo_value = "github_pat_" + secrets.token_hex(20)
    forge_tokens.slots[record.secret_name] = repo_value
    github.pulls[58] = {"base": BASE, "head": HEAD,
                        "files": [gh_file(FORMAT, hunk(3, 3, ["-old", "+new"]))]}
    answer = client.post(f"/v1/repositories/{repo_id}/impact", json={"pull_request": 58},
                         headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["read_with"] == {"scope": "repository", "token_id": record.token_id,
                                          "secret_name": record.secret_name}
    calls = _forge_calls(github, "pulls")
    assert calls and all(h["Authorization"] == f"Bearer {repo_value}" for _u, h in calls)
    assert repo_value not in answer.text


@pytest.mark.parametrize("body", [
    {"pull_request": 57, "commit": HEAD},
    {"base": BASE},
    {},
    {"pull_request": 57, "depth": 7},
    {"pull_request": 57, "command": "rm -rf /"},
    {"commit": "main"},
])
def test_an_impact_request_names_exactly_one_change_and_nothing_that_runs(
    client, github, indexed, body
):
    before = len(github.calls)
    answer = client.post(f"/v1/repositories/{indexed['repo_id']}/impact", json=body,
                         headers=auth_header("alice"))
    assert answer.status_code in (400, 422), answer.text
    assert len(github.calls) == before


def test_another_tenants_repository_is_a_404_before_any_forge_read(client, db, github, indexed):
    github.pulls[57] = {"base": BASE, "head": HEAD,
                        "files": [gh_file(FORMAT, hunk(3, 3, ["-old", "+new"]))]}
    repo_id = indexed["repo_id"]
    before = len(github.calls)
    for method, path, kwargs in (
        ("post", f"/v1/repositories/{repo_id}/impact", {"json": {"pull_request": 57}}),
        ("get", f"/v1/repositories/{repo_id}/graph", {}),
        ("get", f"/v1/repositories/{repo_id}/symbols?q=total", {}),
        ("get", f"/v1/repositories/{repo_id}/symbols?id={quote(TOTAL, safe='')}&depth=2", {}),
        ("get", f"/v1/repositories/{repo_id}/languages", {}),
    ):
        answer = getattr(client, method)(path, headers=auth_header("bob"), **kwargs)
        assert answer.status_code == 404, (path, answer.text)
        assert "OrderService" not in answer.text
    assert len(github.calls) == before
    assert not [k for k in db.docs if k.startswith("impact_plans/")]


def test_another_tenants_graph_is_never_read_for_a_same_named_repository(
    client, github, objects, indexed
):
    # Bob's tenant registers the same repository: its own repo_id, no index.
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("bob"))
    assert created.status_code == 201, created.text
    bob_repo = created.json()["repository"]["repo_id"]
    assert bob_repo != indexed["repo_id"]
    github.pulls[57] = {"base": BASE, "head": HEAD,
                        "files": [gh_file(FORMAT, hunk(3, 3, ["-old", "+new"]))]}
    answer = client.post(f"/v1/repositories/{bob_repo}/impact", json={"pull_request": 57},
                         headers=auth_header("bob"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["index_sha"] is None and body["changed"] == []
    assert body["selection"] == "full_suite" and "stale_index" in kinds(body)
    graph = client.get(f"/v1/repositories/{bob_repo}/graph", headers=auth_header("bob"))
    assert graph.status_code == 404 and "OrderService" not in graph.text


def test_the_graph_route_draws_modules_and_weighted_edges(client, indexed):
    answer = client.get(f"/v1/repositories/{indexed['repo_id']}/graph",
                        headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["index_sha"] == BASE and body["freshness"]["state"] == "current"
    modules = {m["id"]: m for m in body["modules"]}
    assert set(modules) == {"src/shop", "tests/shop"}
    assert modules["src/shop"]["symbols"] == 6
    # Five of src/shop's six symbols are reached by a test (the class is not).
    assert modules["src/shop"]["test_reach"] == pytest.approx(5 / 6, abs=0.01)
    edges = {(e["from"], e["to"]): e for e in body["edges"]}
    assert edges[("tests/shop", "src/shop")]["weight"] == 4
    clustered = client.get(f"/v1/repositories/{indexed['repo_id']}/graph?cluster=package",
                           headers=auth_header("alice")).json()
    assert {m["id"] for m in clustered["modules"]} == {"src", "tests"}


# --------------------------------------------------------------------------
# G4-10: the graph route's cost (11 s for 24 KB, QA 2026-10-07)
# --------------------------------------------------------------------------
# Reading the code path: one draw read every symbols, tests and callees shard
# ONE AT A TIME (impact.module_graph through Graph.shard), and every
# GcsObjectReader.read_range is two round trips (get_blob, then a
# generation-pinned download). 64 modules x 3 layers is ~190 shards, ~380
# serial round trips: the 11 s. The 419 KB index document (1 s) was read on
# every request too, only for its hot spots. These tests hold the fix.

def _recording(objects: InMemoryObjectReader, monkeypatch, delay: float = 0.0) -> dict:
    """Every key read, and the most reads ever in flight at once."""
    import threading
    import time

    seen: dict = {"keys": [], "inflight": 0, "peak": 0}
    lock = threading.Lock()
    real = objects.read_range

    def read_range(key: str, *, offset: int, length: int):
        with lock:
            seen["keys"].append(key)
            seen["inflight"] += 1
            seen["peak"] = max(seen["peak"], seen["inflight"])
        try:
            if delay:
                time.sleep(delay)
            return real(key, offset=offset, length=length)
        finally:
            with lock:
                seen["inflight"] -= 1

    monkeypatch.setattr(objects, "read_range", read_range)
    return seen


def test_a_cold_graph_read_fetches_its_shards_concurrently(client, objects, indexed,
                                                           monkeypatch):
    seen = _recording(objects, monkeypatch, delay=0.05)
    answer = client.get(f"/v1/repositories/{indexed['repo_id']}/graph",
                        headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    blobs = [k for k in seen["keys"] if "/graph/blobs/" in k]
    # The control: this graph has several shards, so one-at-a-time is observable.
    assert len(blobs) >= 4
    assert seen["peak"] > 1, "every shard was read one after another"


def test_a_second_graph_read_of_one_version_reads_only_its_manifest(client, objects, indexed,
                                                                    monkeypatch):
    path = f"/v1/repositories/{indexed['repo_id']}/graph"
    first = client.get(path, headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    seen = _recording(objects, monkeypatch)
    second = client.get(path, headers=auth_header("alice"))
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    # The manifest is still read and digest-checked on every request; no
    # shard and not the index document.
    assert seen["keys"] and all(k.endswith("/manifest.json") for k in seen["keys"]), \
        seen["keys"]
    # Another cluster is another drawing, not the cached one.
    package = client.get(f"{path}?cluster=package", headers=auth_header("alice")).json()
    assert {m["id"] for m in package["modules"]} == {"src", "tests"}


def test_a_manifest_rewritten_after_the_drawing_was_cached_is_still_refused(
    client, objects, indexed
):
    path = f"/v1/repositories/{indexed['repo_id']}/graph"
    assert client.get(path, headers=auth_header("alice")).status_code == 200
    key = next(k for k in objects.objects if k.endswith(f"/graph/{BASE}/manifest.json"))
    objects.put(key, objects.objects[key] + b" ")
    refused = client.get(path, headers=auth_header("alice"))
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "graph_digest_mismatch"


def test_the_graph_route_carries_an_etag_and_answers_a_match_with_304(client, db, indexed):
    path = f"/v1/repositories/{indexed['repo_id']}/graph"
    first = client.get(path, headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    etag = first.headers.get("etag")
    assert etag and etag.startswith('"') and etag.endswith('"')
    assert "private" in first.headers.get("cache-control", "")
    same = client.get(path, headers={**auth_header("alice"), "If-None-Match": etag})
    assert same.status_code == 304 and same.content == b""
    assert same.headers.get("etag") == etag
    weak = client.get(path, headers={**auth_header("alice"),
                                     "If-None-Match": f'"other", W/{etag}'})
    assert weak.status_code == 304
    # The control: another etag is a full answer.
    other = client.get(path, headers={**auth_header("alice"), "If-None-Match": '"other"'})
    assert other.status_code == 200 and other.json() == first.json()
    # The body carries the freshness, so a moved head is a new etag.
    db.docs[f"repositories/{indexed['repo_id']}"]["index"]["head_sha"] = HEAD
    moved = client.get(path, headers={**auth_header("alice"), "If-None-Match": etag})
    assert moved.status_code == 200, moved.text
    assert moved.json()["head_sha"] == HEAD and moved.headers.get("etag") != etag


def test_the_symbols_route_searches_walks_and_maps_tests(client, indexed):
    base = f"/v1/repositories/{indexed['repo_id']}/symbols"
    found = client.get(f"{base}?q=total", headers=auth_header("alice"))
    assert found.status_code == 200, found.text
    assert {TOTAL, CHECKOUT_TOTAL, TEST_TOTAL} <= set(ids(found.json()["symbols"]))
    walked = client.get(f"{base}?id={quote(TOTAL, safe='')}&depth=2&direction=callers",
                        headers=auth_header("alice"))
    assert walked.status_code == 200, walked.text
    graph = walked.json()
    assert {TOTAL, CHECKOUT_TOTAL, TEST_TOTAL} == set(ids(graph["nodes"]))
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    assert edges[(CHECKOUT_TOTAL, TOTAL)]["evidence"] == "lsp"
    assert edges[(CHECKOUT_TOTAL, TOTAL)]["confidence"] == 0.95
    tests = client.get(f"{base}?id={quote(TOTAL, safe='')}&tests=1",
                       headers=auth_header("alice")).json()
    assert [(t["test"], t["depth"]) for t in tests["tests"]] == [(TEST_TOTAL, 2)]
    missing = client.get(f"{base}?id=src/nowhere.py%23x&depth=1", headers=auth_header("alice"))
    assert missing.status_code == 404


def test_the_languages_route_serves_the_table(client, indexed):
    answer = client.get(f"/v1/repositories/{indexed['repo_id']}/languages",
                        headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [(row["language"], row["status"]) for row in body["languages"]] == [("python", "ok")]
    assert body["index_sha"] == BASE and body["freshness"]["state"] == "current"
