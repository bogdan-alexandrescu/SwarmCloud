"""The repository-index extractor: the mechanical half of the index (RI3).

docs/repo-index.md §3.4 and §3.5 make the file tree, symbols, routes,
`import` and candidate `ast` edges, naming test edges, co-change pairs and
hot-spot counts one deterministic tree-sitter pass, shipped in the
agent-runtime-base image (images/agent-runtime-base/repo-index/), which the
indexer prompt runs before the agent reads anything. These tests drive that
script on small fixture repositories (repo_index_fixtures.py), offline: no
network, no language server, nothing installed but the pinned grammars.

What each group holds, and why it matters to a consumer of the index:

* symbols and routes, per language: the impact question starts from "which
  symbols did this diff touch", so a wrong line range is a wrong answer;
* edges carry their evidence and the confidence §2.5 assigns it, so "no test
  reaches this" can always be qualified by how the graph knows;
* a file the extractor cannot parse is LISTED with the reason, never
  silently skipped, because a silent skip reads as "nothing here";
* the size budget and per-file timeout make a hostile or huge repository a
  less complete index, never a failed or endless run;
* the output is byte-identical for the same input, so an index's digest
  (§2.3) identifies its content and a rerun does not look like a change.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-base" / "repo-index"
SCRIPT = TOOL_DIR / "repo_index_extract.py"
REQUIREMENTS = TOOL_DIR / "requirements.txt"
DOCKERFILE = REPO_ROOT / "images" / "agent-runtime-base" / "Dockerfile"
PYPROJECT = REPO_ROOT / "pyproject.toml"

DAY = 86_400


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("repo_index_extract", SCRIPT)
    assert spec is not None and spec.loader is not None, f"no extractor at {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_index_extract"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> Any:
    return _load_tool()


def _symbol(index: dict, symbol_id: str) -> dict:
    found = [s for s in index["symbols"] if s["id"] == symbol_id]
    ids = sorted(s["id"] for s in index["symbols"])
    assert len(found) == 1, f"{symbol_id} not found once in {ids}"
    return found[0]


def _edges(index: dict, frm: str, to: str, kind: str | None = None) -> list[dict]:
    return [
        e
        for e in index["call_edges"]
        if e["from"] == frm and e["to"] == to and (kind is None or e["kind"] == kind)
    ]


def _file(index: dict, path: str) -> dict:
    found = [f for f in index["files"] if f["path"] == path]
    assert len(found) == 1, f"{path} not listed once in files"
    return found[0]


def _language(index: dict, name: str) -> dict:
    found = [entry for entry in index["languages"] if entry["language"] == name]
    assert len(found) == 1, f"{name} not in languages table"
    return found[0]


def _test_edge(index: dict, source: str, test: str) -> dict:
    found = [t for t in index["test_map"] if t["source"] == source and t["test"] == test]
    assert len(found) == 1, f"no single test_map edge {source} -> {test}"
    return found[0]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_output_carries_the_mechanical_keys_of_section_2(tool: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index = tool.extract(repo, tool.Budget())
    assert index["schema"] == "swarm.repo-index/v1"
    assert index["kind"] == "full"
    for key in (
        "commit_sha", "branch", "modules", "routes", "symbols", "call_edges",
        "symbol_test_map", "test_map", "hot_spots", "languages", "files",
        "truncated", "extractor",
    ):
        assert key in index, key
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
        env=fx.git_env(tmp_path),
    ).stdout.strip()
    assert index["commit_sha"] == head
    assert index["branch"] == "main"
    # The agent's reading is not the tool's to invent.
    for agent_key in ("territory", "notes", "entry_points", "commands"):
        assert agent_key not in index


def test_line_counts_and_module_languages(tool: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index = tool.extract(repo, tool.Budget())
    users = _file(index, "src/pkg/users.py")
    assert users["language"] == "python"
    assert users["lines"] == 32
    assert users["status"] == "parsed"
    module = [m for m in index["modules"] if m["path"] == "src/pkg"]
    assert module == [
        {"path": "src/pkg", "language": "python", "files": 4,
         "lines": 0 + 32 + 6 + 13}
    ]


# ---------------------------------------------------------------------------
# Symbols and routes, per language
# ---------------------------------------------------------------------------


def test_python_symbols_with_line_ranges(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    load = _symbol(index, "src/pkg/users.py#load_user")
    assert (load["kind"], load["start_line"], load["end_line"]) == ("function", 8, 10)
    assert load["language"] == "python" and load["exported"] is True
    assert load["path"] == "src/pkg/users.py"
    get_user = _symbol(index, "src/pkg/users.py#get_user")
    # A decorated function's range starts at its decorator.
    assert (get_user["start_line"], get_user["end_line"]) == (17, 19)
    service = _symbol(index, "src/pkg/users.py#UserService")
    assert (service["kind"], service["start_line"], service["end_line"]) == ("class", 22, 28)
    create = _symbol(index, "src/pkg/users.py#UserService.create")
    assert (create["kind"], create["start_line"], create["end_line"]) == ("method", 23, 25)
    audit = _symbol(index, "src/pkg/users.py#UserService._audit")
    assert audit["exported"] is False
    test = _symbol(index, "tests/test_users.py#test_load_user")
    assert test["kind"] == "test"


def test_python_fastapi_and_flask_routes(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    routes = {(r["method"], r["path"]): r for r in index["routes"]}
    assert routes[("GET", "/users/{user_id}")]["handler"] == "src/pkg/users.py#get_user"
    assert routes[("GET", "/users/{user_id}")]["file"] == "src/pkg/users.py"
    assert routes[("GET", "/health")]["handler"] == "src/pkg/web.py#health"
    assert routes[("HEAD", "/health")]["handler"] == "src/pkg/web.py#health"
    # Flask's default method is GET.
    assert routes[("GET", "/ping")]["handler"] == "src/pkg/web.py#ping"
    route = _symbol(index, "src/pkg/users.py#GET /users/{user_id}")
    assert route["kind"] == "route" and route["method"] == "GET"
    assert route["route"] == "/users/{user_id}"
    handler_edges = _edges(
        index, "src/pkg/users.py#GET /users/{user_id}", "src/pkg/users.py#get_user",
        "route_handler",
    )
    assert len(handler_edges) == 1


def test_typescript_tsx_and_javascript_symbols(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.TS_APP), tool.Budget())
    fmt = _symbol(index, "web/src/format.ts#formatName")
    assert (fmt["kind"], fmt["start_line"], fmt["end_line"], fmt["exported"]) == (
        "function", 1, 3, True,
    )
    assert fmt["language"] == "typescript"
    shout = _symbol(index, "web/src/format.ts#shout")
    assert (shout["kind"], shout["start_line"], shout["exported"]) == ("function", 5, True)
    assert _symbol(index, "web/src/format.ts#internalOnly")["exported"] is False
    klass = _symbol(index, "web/src/format.ts#Formatter")
    assert (klass["kind"], klass["start_line"], klass["end_line"]) == ("class", 11, 15)
    method = _symbol(index, "web/src/format.ts#Formatter.run")
    assert (method["kind"], method["start_line"], method["end_line"]) == ("method", 12, 14)
    app = _symbol(index, "web/src/App.tsx#App")
    assert (app["kind"], app["language"], app["exported"]) == ("function", "typescript", True)
    js = _symbol(index, "web/src/users.js#listUsers")
    assert (js["language"], js["start_line"], js["end_line"]) == ("javascript", 1, 3)
    test = _symbol(index, "web/src/format.test.ts#formatName > joins")
    assert (test["kind"], test["start_line"], test["end_line"]) == ("test", 4, 6)
    assert _file(index, "web/src/App.tsx")["status"] == "parsed"


def test_express_routes(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.TS_APP), tool.Budget())
    routes = {(r["method"], r["path"]): r for r in index["routes"]}
    get = routes[("GET", "/api/users")]
    assert get["file"] == "web/src/server.js" and get["start_line"] == 6
    # A named handler resolves through the require() binding to its definition.
    assert get["handler"] == "web/src/users.js#listUsers"
    post = routes[("POST", "/api/users")]
    # An inline handler is the route itself; calls inside it belong to it.
    assert post["handler"] is None
    assert (post["start_line"], post["end_line"]) == (7, 9)
    assert _edges(index, "web/src/server.js#POST /api/users", "web/src/users.js#listUsers", "call")


def test_go_symbols_and_routes(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.GO_APP), tool.Budget())
    total = _symbol(index, "internal/cart/cart.go#Cart.Total")
    assert (total["kind"], total["start_line"], total["end_line"], total["exported"]) == (
        "method", 9, 11, True,
    )
    cart = _symbol(index, "internal/cart/cart.go#Cart")
    assert (cart["kind"], cart["start_line"], cart["end_line"]) == ("type", 5, 7)
    assert _symbol(index, "internal/cart/cart.go#count")["exported"] is False
    assert _symbol(index, "internal/cart/cart.go#Show")["exported"] is True
    assert _symbol(index, "internal/cart/cart_test.go#TestCount")["kind"] == "test"
    routes = {(r["method"], r["path"]): r for r in index["routes"]}
    # Go 1.22's "METHOD /path" pattern is split; the handler resolves through
    # the aliased import to the package's file.
    assert routes[("GET", "/cart")]["handler"] == "internal/cart/cart.go#Show"
    assert routes[("POST", "/cart/items")]["handler"] == "internal/cart/cart.go#Add"


def test_hcl_blocks_routes_and_references(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.HCL_APP), tool.Budget())
    bucket = _symbol(index, "infra/main.tf#google_storage_bucket.artifacts")
    assert (bucket["kind"], bucket["start_line"], bucket["end_line"]) == ("resource", 5, 11)
    assert bucket["language"] == "hcl"
    region = _symbol(index, "infra/main.tf#var.region")
    assert (region["kind"], region["exported"]) == ("variable", True)
    assert _symbol(index, "infra/main.tf#module.network")["kind"] == "module"
    assert _symbol(index, "infra/main.tf#output.bucket")["kind"] == "output"
    routes = {(r["method"], r["path"]) for r in index["routes"]}
    assert ("resource", "google_storage_bucket.artifacts") in routes
    assert ("module", "module.network") in routes
    assert ("variable", "var.region") in routes
    ref = _edges(index, "infra/main.tf#google_storage_bucket.artifacts", "infra/main.tf#var.region")
    assert [(e["kind"], e["evidence"], e["confidence"]) for e in ref] == [("reference", "ast", 0.6)]
    assert _edges(
        index, "infra/main.tf#module.network", "infra/main.tf#google_storage_bucket.artifacts",
        "reference",
    )
    # `source = "./modules/network"` is the module's import.
    imp = _edges(index, "infra/main.tf", "infra/modules/network/main.tf", "import")
    assert [(e["evidence"], e["confidence"]) for e in imp] == [("import", 0.4)]


# ---------------------------------------------------------------------------
# Edges, with evidence and confidence (§2.5)
# ---------------------------------------------------------------------------


def test_import_edges_carry_evidence_and_confidence(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    rel = _edges(index, "src/pkg/users.py", "src/pkg/store.py", "import")
    assert len(rel) == 1
    assert (rel[0]["evidence"], rel[0]["confidence"], rel[0]["line"]) == ("import", 0.4, 3)
    absolute = _edges(index, "tests/test_users.py", "src/pkg/users.py", "import")
    assert len(absolute) == 1
    # `fastapi` is not in the repository and gets no in-repository edge.
    assert not [e for e in index["call_edges"] if "fastapi" in e["to"]]


def test_js_ts_and_go_import_edges(tool: Any, tmp_path: Path) -> None:
    ts_index = tool.extract(fx.build_repo(tmp_path / "ts", fx.TS_APP), tool.Budget())
    assert _edges(ts_index, "web/src/App.tsx", "web/src/format.ts", "import")
    assert _edges(ts_index, "web/src/server.js", "web/src/users.js", "import")
    go_index = tool.extract(fx.build_repo(tmp_path / "go", fx.GO_APP), tool.Budget())
    # The module path from go.mod maps the import to the package directory's
    # non-test files.
    go_edges = _edges(go_index, "cmd/server/main.go", "internal/cart/cart.go", "import")
    assert [(e["evidence"], e["confidence"]) for e in go_edges] == [("import", 0.4)]
    assert not _edges(go_index, "cmd/server/main.go", "internal/cart/cart_test.go")


def test_ast_call_edges_unique_and_ambiguous(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    # Same-file and imported-name calls each match exactly one definition.
    local = _edges(index, "src/pkg/users.py#load_user", "src/pkg/users.py#normalise", "call")
    assert [(e["evidence"], e["confidence"], e["line"]) for e in local] == [("ast", 0.6, 10)]
    imported = _edges(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch", "call")
    assert [(e["evidence"], e["confidence"]) for e in imported] == [("ast", 0.6)]
    method = _edges(
        index, "src/pkg/users.py#UserService.create", "src/pkg/users.py#UserService._audit",
        "call",
    )
    assert method and method[0]["confidence"] == 0.6
    inherit = _edges(index, "src/pkg/users.py#UserService", "src/pkg/users.py#BaseService")
    assert [e["kind"] for e in inherit] == ["inherit"]

    amb = tool.extract(fx.build_repo(tmp_path / "amb", fx.PYTHON_AMBIGUOUS), tool.Budget())
    to_a = _edges(amb, "lib/use.py#main", "lib/a.py#render", "call")
    to_b = _edges(amb, "lib/use.py#main", "lib/b.py#render", "call")
    assert [(e["evidence"], e["confidence"]) for e in to_a + to_b] == [("ast", 0.3), ("ast", 0.3)]
    unique = _edges(amb, "lib/use.py#main", "lib/c.py#only_here", "call")
    assert [e["confidence"] for e in unique] == [0.6]


def test_go_and_ts_call_edges(tool: Any, tmp_path: Path) -> None:
    go_index = tool.extract(fx.build_repo(tmp_path / "go", fx.GO_APP), tool.Budget())
    # Same package, another file: in scope without an import.
    assert _edges(go_index, "cmd/server/main.go#main", "cmd/server/router.go#newRouter", "call")
    assert _edges(go_index, "internal/cart/cart.go#Show", "internal/cart/cart.go#count", "call")
    ts_index = tool.extract(fx.build_repo(tmp_path / "ts", fx.TS_APP), tool.Budget())
    assert _edges(ts_index, "web/src/App.tsx#App", "web/src/format.ts#formatName", "call")
    assert _edges(ts_index, "web/src/format.ts#Formatter", "web/src/format.ts#Base", "inherit")


def test_naming_convention_test_edges(tool: Any, tmp_path: Path) -> None:
    py = tool.extract(fx.build_repo(tmp_path / "py", fx.PYTHON_APP), tool.Budget())
    store = _test_edge(py, "src/pkg/store.py", "tests/test_store.py")
    assert (store["evidence"], store["confidence"], store["also_evidence"]) == ("naming", 0.3, [])
    # Found three ways, the edge keeps the strongest and lists the others.
    users = _test_edge(py, "src/pkg/users.py", "tests/test_users.py")
    assert users["evidence"] == "ast"
    assert users["also_evidence"] == ["import", "naming"]
    ts = tool.extract(fx.build_repo(tmp_path / "ts", fx.TS_APP), tool.Budget())
    assert "naming" in [
        _test_edge(ts, "web/src/format.ts", "web/src/format.test.ts")["evidence"],
        *_test_edge(ts, "web/src/format.ts", "web/src/format.test.ts")["also_evidence"],
    ]
    go = tool.extract(fx.build_repo(tmp_path / "go", fx.GO_APP), tool.Budget())
    edge = _test_edge(go, "internal/cart/cart.go", "internal/cart/cart_test.go")
    assert "naming" in [edge["evidence"], *edge["also_evidence"]]


def test_symbol_test_map_walks_call_edges(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    reached = {
        (m["symbol"], m["depth"], m["confidence"])
        for m in index["symbol_test_map"]
        if m["test"] == "tests/test_users.py#test_load_user"
    }
    assert ("src/pkg/users.py#load_user", 1, 0.6) in reached
    assert ("src/pkg/users.py#normalise", 2, 0.36) in reached
    # A path's confidence is the product along it, floored at 0.2.
    assert all(m["confidence"] >= 0.2 for m in index["symbol_test_map"])
    assert ("src/pkg/store.py#fetch", 2, 0.36) in reached


# ---------------------------------------------------------------------------
# Co-change and hot spots, from git log --numstat over 90 days
# ---------------------------------------------------------------------------


def test_co_change_pairs_and_hot_spots(tool: Any, tmp_path: Path) -> None:
    start = 1_780_000_000
    history: list[tuple[int, dict[str, str]]] = []
    # users.py and test_users.py change together six times...
    for i in range(6):
        history.append((start + (i + 1) * DAY, {
            "src/pkg/users.py": fx.PYTHON_APP["src/pkg/users.py"] + f"# {i}\n",
            "tests/test_users.py": fx.PYTHON_APP["tests/test_users.py"] + f"# {i}\n",
        }))
    # ...store.py alone twice...
    for i in range(2):
        history.append((start + (10 + i) * DAY, {
            "src/pkg/store.py": fx.PYTHON_APP["src/pkg/store.py"] + f"# {i}\n",
        }))
    # ...and web.py with users.py three times: with the initial commit, four,
    # below the 5-commit floor.
    for i in range(3):
        history.append((start + (20 + i) * DAY, {
            "src/pkg/web.py": fx.PYTHON_APP["src/pkg/web.py"] + f"# {i}\n",
            "src/pkg/users.py": fx.PYTHON_APP["src/pkg/users.py"] + f"# w{i}\n",
        }))
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP, history=history,
                         first_commit_at=start)
    index = tool.extract(repo, tool.Budget())
    spots = {h["path"]: h for h in index["hot_spots"]}
    # The initial commit counts too: users.py = 1 + 6 + 3.
    assert spots["src/pkg/users.py"]["changes"] == 10
    assert spots["src/pkg/store.py"]["changes"] == 3
    partners = {c["path"]: c for c in spots["src/pkg/users.py"]["changed_with"]}
    assert partners["tests/test_users.py"]["commits"] == 7
    # Jaccard: 7 together / (10 + 7 - 7) = 0.7; capped at 0.5 as a confidence.
    assert partners["tests/test_users.py"]["support"] == 0.7
    assert "src/pkg/web.py" not in partners
    edge = _test_edge(index, "src/pkg/users.py", "tests/test_users.py")
    assert "co-change" in edge["also_evidence"]
    co = [e for e in index["test_map"] if e["evidence"] == "co-change"]
    assert all(e["confidence"] <= 0.5 for e in co)
    order = [h["path"] for h in index["hot_spots"]]
    assert order[0] == "src/pkg/users.py"


def test_the_90_day_window_is_anchored_at_the_head_commit(tool: Any, tmp_path: Path) -> None:
    start = 1_780_000_000
    old = [(start + i * DAY, {"src/pkg/store.py": f"# old {i}\n"}) for i in range(1, 4)]
    # The head is 200 days after the old changes: they fall outside the window.
    head = [(start + 200 * DAY, {"src/pkg/web.py": "# head\n"})]
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP, history=old + head,
                         first_commit_at=start)
    index = tool.extract(repo, tool.Budget())
    spots = {h["path"]: h["changes"] for h in index["hot_spots"]}
    assert "src/pkg/store.py" not in spots
    assert spots == {"src/pkg/web.py": 1}
    window = index["extractor"]["history"]
    assert window["window_days"] == 90 and window["commits"] == 1


# ---------------------------------------------------------------------------
# Unsupported files, the size budget and the per-file timeout
# ---------------------------------------------------------------------------


def test_unsupported_language_is_listed_with_the_reason(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.MIXED_UNSUPPORTED), tool.Budget())
    ruby = _file(index, "tools/release.rb")
    assert (ruby["language"], ruby["status"], ruby["lines"]) == ("ruby", "unsupported", 3)
    assert "no tree-sitter grammar" in ruby["reason"]
    table = _language(index, "ruby")
    assert table["status"] == "unsupported" and table["files"] == 1
    assert table["reason"] == "file level only"
    assert table["grammar"] is None
    readme = _file(index, "README.md")
    assert readme["status"] == "not_source" and readme["reason"]
    py = _language(index, "python")
    assert py["grammar"].startswith("tree-sitter-python ")
    assert py["server"] == "pyright"
    assert py["status"] == "unsupported"
    assert py["reason"] == "no language server run by this extractor; edges are syntactic"
    # Every file in the tree is listed: nothing is silently skipped.
    assert sorted(f["path"] for f in index["files"]) == sorted(fx.MIXED_UNSUPPORTED)
    assert _symbol(index, "app/main.py#run")


def test_a_symlink_is_listed_and_not_followed(tool: Any, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def secret_shape():\n    return 1\n", encoding="utf-8")
    repo = tmp_path / "repo"
    fx.write_files(repo, {"app/main.py": "def run():\n    return 1\n"})
    (repo / "app" / "link.py").symlink_to(outside)
    index = tool.extract(repo, tool.Budget())
    link = _file(index, "app/link.py")
    assert link["status"] == "symlink" and link["lines"] == 0
    assert not [s for s in index["symbols"] if "secret_shape" in s["id"]]
    # Not a git repository: no history, and it says so rather than failing.
    assert index["commit_sha"] is None
    assert index["hot_spots"] == []
    assert index["extractor"]["history"]["available"] is False


def test_the_size_budget_lists_what_it_did_not_parse(tool: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index = tool.extract(repo, tool.Budget(max_file_bytes=200))
    big = _file(index, "src/pkg/users.py")
    assert big["status"] == "too_large" and "200" in big["reason"]
    # Still counted: a file over the budget keeps its line count.
    assert big["lines"] == 32
    assert not [s for s in index["symbols"] if s["path"] == "src/pkg/users.py"]

    total = tool.extract(repo, tool.Budget(max_total_bytes=150))
    statuses = {f["path"]: f["status"] for f in total["files"]}
    assert "over_budget" in statuses.values()
    assert "files" in total["truncated"]

    capped = tool.extract(repo, tool.Budget(max_files=3))
    assert len(capped["files"]) == 3
    assert capped["extractor"]["files_not_listed"] == len(fx.PYTHON_APP) - 3
    assert "files" in capped["truncated"]


def test_a_per_file_timeout_marks_the_file_and_the_run_still_succeeds(
    tool: Any, tmp_path: Path
) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index = tool.extract(repo, tool.Budget(file_timeout_seconds=0.0))
    users = _file(index, "src/pkg/users.py")
    assert users["status"] == "timed_out"
    assert users["lines"] == 32
    assert index["symbols"] == []
    assert _language(index, "python")["timed_out"] == len(
        [p for p in fx.PYTHON_APP if p.endswith(".py")]
    )


# ---------------------------------------------------------------------------
# Determinism and the command line
# ---------------------------------------------------------------------------


def _all_fixtures() -> dict[str, str]:
    merged: dict[str, str] = {}
    for part in (fx.PYTHON_APP, fx.TS_APP, fx.GO_APP, fx.HCL_APP, fx.MIXED_UNSUPPORTED):
        merged.update(part)
    return merged


def test_the_same_input_gives_byte_identical_json(tool: Any, tmp_path: Path) -> None:
    files = _all_fixtures()
    history = [(1_780_000_000 + DAY * i, {"src/pkg/users.py": f"# {i}\n"}) for i in range(1, 7)]
    first = fx.build_repo(tmp_path / "one", files, history=history)
    # The second copy is written in the reverse order, at another path.
    second = tmp_path / "two"
    fx.build_repo(second, dict(reversed(list(files.items()))), history=history)
    out_one = tool.dumps(tool.extract(first, tool.Budget()))
    out_two = tool.dumps(tool.extract(second, tool.Budget()))
    assert isinstance(out_one, bytes)
    assert out_one == out_two
    assert tool.dumps(tool.extract(first, tool.Budget())) == out_one
    assert str(tmp_path) not in out_one.decode("utf-8")
    assert json.loads(out_one)["schema"] == "swarm.repo-index/v1"


def test_the_command_line_writes_the_json(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.GO_APP)
    out = tmp_path / "artifacts" / "repo-index.json"
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--out", str(out),
         "--file-timeout-seconds", "5"],
        capture_output=True, text=True, timeout=120,
        env=fx.git_env(tmp_path),
    )
    assert done.returncode == 0, done.stderr
    index = json.loads(out.read_text(encoding="utf-8"))
    assert index["schema"] == "swarm.repo-index/v1"
    assert _symbol(index, "internal/cart/cart.go#Show")
    # A one-line summary with the counts the run actually produced.
    assert re.search(r"files=\d+ symbols=\d+ edges=\d+", done.stderr)
    selftest = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test"],
        capture_output=True, text=True, timeout=120, env=fx.git_env(tmp_path),
    )
    assert selftest.returncode == 0, selftest.stderr
    assert "6 grammars ok" in selftest.stdout
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(tmp_path / "missing")],
        capture_output=True, text=True, timeout=60,
    )
    assert bad.returncode == 2


# ---------------------------------------------------------------------------
# Pins: the image and the tests parse with one grammar build
# ---------------------------------------------------------------------------


def _pins(text: str) -> dict[str, str]:
    return dict(re.findall(r"^\s*\"?(tree-sitter[a-z-]*)==([0-9.]+)", text, re.MULTILINE))


def test_the_tree_sitter_packages_are_pinned_identically(tool: Any) -> None:
    image_pins = _pins(REQUIREMENTS.read_text(encoding="utf-8"))
    test_pins = _pins(PYPROJECT.read_text(encoding="utf-8"))
    expected = {
        "tree-sitter", "tree-sitter-python", "tree-sitter-javascript",
        "tree-sitter-typescript", "tree-sitter-go", "tree-sitter-hcl",
    }
    assert set(image_pins) == expected
    assert image_pins == test_pins
    # Every requirement is hash-pinned, so a rebuild installs the same bytes.
    entries = re.split(r"^(?=[a-z][a-z0-9-]*==)", REQUIREMENTS.read_text(encoding="utf-8"),
                       flags=re.MULTILINE)[1:]
    assert len(entries) == len(expected)
    for entry in entries:
        assert "--hash=sha256:" in entry, entry.splitlines()[0]
    # The script reports the grammar versions it parsed with.
    assert tool.GRAMMAR_PACKAGES["python"] == "tree-sitter-python"


def test_the_image_installs_the_extractor_and_proves_it_runs() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY images/agent-runtime-base/repo-index/" in text
    assert "--require-hashes" in text
    assert "/usr/local/bin/swarm-repo-index" in text
    # The build runs it, so an image whose grammars cannot load never ships.
    assert "swarm-repo-index --self-test" in text
    # Its own environment: never the worker's venv nor the agent's pip target.
    assert "/opt/repo-index" in text


def test_the_script_has_no_dependency_outside_its_pins() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    imported = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", source, re.MULTILINE))
    third_party = {name for name in imported if name not in sys.stdlib_module_names}
    allowed = {
        "tree_sitter", "tree_sitter_python", "tree_sitter_javascript",
        "tree_sitter_typescript", "tree_sitter_go", "tree_sitter_hcl", "__future__",
    }
    assert third_party <= allowed, third_party - allowed
    assert shutil.which("git") is not None
