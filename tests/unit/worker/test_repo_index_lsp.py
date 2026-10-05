"""The headless LSP pass of the repository index (lane RI10).

docs/repo-index.md §3.5 runs one language server per language as a child
process over stdio -- pyright, tsserver, gopls, terraform-ls -- and asks it
`textDocument/definition` for every candidate call site the tree-sitter pass
found, `callHierarchy/incomingCalls` for every exported function where the
server has call hierarchy, and `textDocument/references` where it does not.
A resolved site becomes an `lsp` edge; everything else stays `ast`.

These tests drive the driver in images/agent-runtime-base/repo-index/lsp/
against fake_lsp_server.py, which speaks the same wire format and answers
from a scenario: no real server runs here (the image's `--lsp-self-test`
does that at build time). What they hold, and why it matters:

* a resolved site upgrades the `ast` edge to `lsp` at §2.5's confidence,
  keeping `ast` in `also_evidence`, so "how does the graph know" is answered;
* a server that times out, hangs, crashes, exceeds its memory or is missing
  is stopped, its language is marked `timed_out` or `failing` with the
  reason, its edges stay `ast`, and the run still succeeds -- a timeout is a
  less certain index, never a failed one;
* a language with no server is `unsupported`, with §3.5's wording;
* the server runs with a minimal environment, so a credential in the
  indexer's environment never reaches a process that reads untrusted code.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-base" / "repo-index"
SCRIPT = TOOL_DIR / "repo_index_extract.py"
LSP_DIR = TOOL_DIR / "lsp"
DOCKERFILE = REPO_ROOT / "images" / "agent-runtime-base" / "Dockerfile"
FAKE = Path(__file__).resolve().parent / "fake_lsp_server.py"

EVIDENCE = {"lsp", "ast", "import", "naming", "co-change"}


@pytest.fixture(scope="module")
def tool() -> Any:
    spec = importlib.util.spec_from_file_location("repo_index_extract", SCRIPT)
    assert spec is not None and spec.loader is not None, f"no extractor at {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_index_extract"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lsp(tool: Any) -> Any:
    # The extractor puts its own directory on sys.path to import the package
    # (the image runs it under `python -I`, which adds nothing).
    return sys.modules["lsp"]


def _fake(lsp: Any, tmp_path: Path, scenario: dict, *, name: str = "fake",
          languages: tuple[str, ...] = ("python",), ids: dict | None = None,
          call_hierarchy: bool = True, references: bool = False,
          reference_kinds: tuple[str, ...] = (), settings: dict | None = None) -> Any:
    log = tmp_path / f"{name}.log"
    scenario = dict(scenario, log=str(log))
    path = tmp_path / f"{name}-scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    return lsp.ServerSpec(
        name=name,
        command=(sys.executable, str(FAKE), str(path)),
        languages=languages,
        language_ids=ids or {".py": "python"},
        call_hierarchy=call_hierarchy,
        references=references,
        reference_kinds=reference_kinds,
        settings=settings or {},
    )


def _options(lsp: Any, *specs: Any, **overrides: Any) -> Any:
    values = {"request_timeout_seconds": 5.0, "memory_limit_mib": 2048}
    values.update(overrides)
    return lsp.LspOptions(servers={s.name: s for s in specs}, **values)


def _log(tmp_path: Path, name: str = "fake") -> list[dict]:
    path = tmp_path / f"{name}.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _asked(log: list[dict], method: str) -> list[dict]:
    return [m["params"] for m in log if m.get("method") == method]


def _edge(index: dict, frm: str, to: str, kind: str = "call") -> dict:
    found = [e for e in index["call_edges"] if (e["from"], e["to"], e["kind"]) == (frm, to, kind)]
    assert len(found) == 1, f"no single {kind} edge {frm} -> {to}"
    return found[0]


def _language(index: dict, name: str) -> dict:
    found = [entry for entry in index["languages"] if entry["language"] == name]
    assert len(found) == 1, f"{name} not in languages table"
    return found[0]


def _no_lsp_edges(index: dict) -> None:
    assert [e for e in index["call_edges"] if e["evidence"] == "lsp" or "lsp" in e["also_evidence"]] == []


# ---------------------------------------------------------------------------
# The protocol paths: definition, call hierarchy, references
# ---------------------------------------------------------------------------


def test_a_resolved_definition_upgrades_the_ast_edge_to_lsp(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"definitions": {
        # users.py line 9, `    row = fetch(user_id)` -> store.py `def fetch`
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    edge = _edge(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch")
    assert (edge["evidence"], edge["confidence"], edge["also_evidence"]) == ("lsp", 0.95, ["ast"])
    # A site the server did not resolve stays `ast`, at its own confidence.
    other = _edge(index, "src/pkg/users.py#load_user", "src/pkg/users.py#normalise")
    assert (other["evidence"], other["confidence"]) == ("ast", 0.6)
    python = _language(index, "python")
    assert python["status"] == "ok" and python["server"] == "fake"
    assert python["fallback"] is None
    assert python["lsp"]["server_version"] == "0.0.1"
    assert python["lsp"]["resolved"] >= 1 and python["lsp"]["request_timeouts"] == 0
    # The position asked is the callee's name on the call's line.
    asked = [p for p in _asked(_log(tmp_path), "textDocument/definition")
             if p["textDocument"]["uri"].endswith("/src/pkg/users.py") and p["position"]["line"] == 8]
    assert [p["position"]["character"] for p in asked] == [10]
    assert index["extractor"]["lsp"]["servers"] == {"fake": "0.0.1"}


def test_the_server_picks_one_of_several_ast_candidates(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_AMBIGUOUS)
    spec = _fake(lsp, tmp_path, {"definitions": {
        "lib/use.py:6": {"path": "lib/b.py", "line": 0, "character": 4, "name": "render"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    chosen = _edge(index, "lib/use.py#main", "lib/b.py#render")
    assert (chosen["evidence"], chosen["confidence"]) == ("lsp", 0.95)
    # The candidate the server did not confirm keeps its ambiguous `ast` edge.
    other = _edge(index, "lib/use.py#main", "lib/a.py#render")
    assert (other["evidence"], other["confidence"]) == ("ast", 0.3)


RECEIVER = {
    "svc.py": (
        "class Store:\n"  # 1
        "    def get(self):\n"  # 2
        "        return 1\n"  # 3
        "\n"  # 4
        "\n"  # 5
        "def use():\n"  # 6
        "    s = Store()\n"  # 7
        "    return s.get()\n"  # 8
    ),
}


def test_a_call_through_an_inferred_receiver_is_lsp_at_0_8(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", RECEIVER)
    spec = _fake(lsp, tmp_path, {"definitions": {
        "svc.py:6": {"path": "svc.py", "line": 0, "character": 6, "name": "Store"},
        "svc.py:7": {"path": "svc.py", "line": 1, "character": 8, "name": "get"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    # `Store()` is a declared name: 0.95. `s.get()` resolved through the type
    # the server inferred for `s`: §2.5's 0.8.
    assert _edge(index, "svc.py#use", "svc.py#Store")["confidence"] == 0.95
    method = _edge(index, "svc.py#use", "svc.py#Store.get")
    assert (method["evidence"], method["confidence"]) == ("lsp", 0.8)
    asked = {p["position"]["line"]: p["position"]["character"]
             for p in _asked(_log(tmp_path), "textDocument/definition")}
    assert asked[7] == 13  # `    return s.get()`: the `get` after the dot


def test_incoming_calls_add_callers_the_syntax_could_not_see(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"incoming": {
        # store.py `def fetch` <- web.py `def health` (no call the AST saw)
        "src/pkg/store.py:0": [{"path": "src/pkg/web.py", "line": 6, "character": 4,
                                "name": "health", "call_line": 7}],
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    edge = _edge(index, "src/pkg/web.py#health", "src/pkg/store.py#fetch")
    assert (edge["evidence"], edge["confidence"], edge["also_evidence"]) == ("lsp", 0.95, [])
    assert edge["line"] == 8
    prepared = _asked(_log(tmp_path), "textDocument/prepareCallHierarchy")
    lines = {(p["textDocument"]["uri"].rsplit("/", 1)[1], p["position"]["line"]) for p in prepared}
    assert ("store.py", 0) in lines
    # Only exported functions and methods are asked: `_audit` is private.
    assert ("users.py", 26) not in lines
    assert ("users.py", 22) in lines  # UserService.create


def test_a_server_without_call_hierarchy_answers_references(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.HCL_APP)
    spec = _fake(lsp, tmp_path, {"references": {
        # `variable "region"` is referenced from the bucket's `location`.
        "infra/main.tf:0": [{"path": "infra/main.tf", "line": 6, "character": 17}],
    }}, name="tf", languages=("hcl",), ids={".tf": "terraform"}, call_hierarchy=False,
        references=True, reference_kinds=("variable", "resource", "module", "output"))
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    edge = _edge(index, "infra/main.tf#google_storage_bucket.artifacts", "infra/main.tf#var.region",
                 "reference")
    assert (edge["evidence"], edge["confidence"]) == ("lsp", 0.95)
    log = _log(tmp_path, "tf")
    assert _asked(log, "textDocument/prepareCallHierarchy") == []
    asked = [p for p in _asked(log, "textDocument/references")
             if p["textDocument"]["uri"].endswith("/infra/main.tf") and p["position"]["line"] == 0]
    assert [(p["position"]["character"], p["context"]["includeDeclaration"]) for p in asked] == [(10, False)]
    assert _language(index, "hcl")["status"] == "ok"


def test_positions_are_utf16_code_units(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", {
        "m.py": "def f():\n    s = '\U0001f642'; return g()\n\n\ndef g():\n    return 1\n",
    })
    spec = _fake(lsp, tmp_path, {"definitions": {
        "m.py:1": {"path": "m.py", "line": 4, "character": 4, "name": "g"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    asked = [p["position"] for p in _asked(_log(tmp_path), "textDocument/definition")]
    # 20 code points before `g`, one of them outside the BMP: 21 UTF-16 units.
    assert {"line": 1, "character": 21} in asked
    assert _edge(index, "m.py#f", "m.py#g")["evidence"] == "lsp"


def test_a_definition_outside_the_known_symbols_adds_no_edge(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"definitions": {
        "src/pkg/users.py:8": {"path": "../outside/store.py", "line": 0, "character": 4, "name": "fetch"},
        # A line inside `fetch` that is not its name: a local, not the symbol.
        "src/pkg/users.py:9": {"path": "src/pkg/store.py", "line": 1, "character": 4, "name": "return"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    _no_lsp_edges(index)
    python = _language(index, "python")
    assert python["status"] == "ok" and python["lsp"]["resolved"] == 0
    assert python["lsp"]["unresolved"] >= 2


def test_workspace_configuration_is_answered_from_the_spec(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.MIXED_UNSUPPORTED)
    spec = _fake(lsp, tmp_path, {"ask_config": True},
                 settings={"python.analysis": {"diagnosticMode": "openFilesOnly"}})
    tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    answers = [m for m in _log(tmp_path) if "id" in m and "method" not in m]
    assert answers and answers[0]["result"] == [{"diagnosticMode": "openFilesOnly"}]
    init = _asked(_log(tmp_path), "initialize")[0]
    assert init["processId"] == os.getpid()
    assert init["rootUri"] == repo.resolve().as_uri()


def test_the_server_gets_a_minimal_environment(tool: Any, lsp: Any, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    name = "SWARM_TEST_" + "TOKEN"
    monkeypatch.setenv(name, "x" * 24)
    repo = fx.build_repo(tmp_path / "repo", fx.MIXED_UNSUPPORTED)
    spec = _fake(lsp, tmp_path, {"log_env": True})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, bin_dir=bin_dir))
    env = _log(tmp_path)[0]["env"]
    assert name not in env
    assert env["PATH"].split(":")[0] == str(bin_dir)
    assert env["HOME"] != os.environ.get("HOME") and not Path(env["HOME"]).exists()


# ---------------------------------------------------------------------------
# The fallbacks: timed_out, failing, unsupported -- and the run still succeeds
# ---------------------------------------------------------------------------


def test_requests_that_time_out_mark_the_language_timed_out(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"mode": "hang"})
    started = time.monotonic()
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, spec, request_timeout_seconds=0.2, warmup_timeout_seconds=0.2))
    assert time.monotonic() - started < 30
    python = _language(index, "python")
    assert python["status"] == "timed_out"
    assert "consecutive requests" in python["reason"]
    assert python["fallback"] == "ast"
    assert python["lsp"]["request_timeouts"] == 3
    _no_lsp_edges(index)
    assert _edge(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch")["evidence"] == "ast"


def test_the_server_budget_stops_it_and_discards_its_edges(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"mode": "slow", "delay": 0.3, "definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, server_budget_seconds=1.0))
    python = _language(index, "python")
    assert python["status"] == "timed_out"
    assert "1-second budget" in python["reason"]
    # A stopped server's partial answers are not kept: the language is
    # uniformly `ast`, as its status says.
    _no_lsp_edges(index)


def test_a_server_over_its_memory_is_stopped(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"mode": "memory", "memory_mib": 256, "delay": 0.5})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, memory_limit_mib=96))
    python = _language(index, "python")
    assert python["status"] == "failing"
    assert "96 MiB memory limit" in python["reason"]
    _no_lsp_edges(index)


def test_a_server_that_crashes_is_failing(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, _fake(lsp, tmp_path, {"mode": "crash"})))
    python = _language(index, "python")
    assert python["status"] == "failing" and "exited with status 3" in python["reason"]
    _no_lsp_edges(index)


def test_a_server_that_refuses_initialize_is_failing(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, _fake(lsp, tmp_path, {"mode": "init_error"})))
    python = _language(index, "python")
    assert python["status"] == "failing"
    # The server's own message is not copied: it is untrusted text.
    assert python["reason"] == "initialize failed (error -32603)"


def test_a_missing_server_is_failing_not_a_failed_run(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = lsp.ServerSpec(name="pyright", command=("pyright-langserver", "--stdio"),
                          languages=("python",), language_ids={".py": "python"})
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, bin_dir=bin_dir))
    python = _language(index, "python")
    assert python["status"] == "failing"
    assert python["reason"] == f"pyright-langserver is not installed in {bin_dir}"
    _no_lsp_edges(index)


def test_a_language_without_a_server_is_unsupported(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.MIXED_UNSUPPORTED)
    spec = _fake(lsp, tmp_path, {}, name="gofake", languages=("go",), ids={".go": "go"})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    python = _language(index, "python")
    assert (python["status"], python["reason"], python["fallback"]) == \
        ("unsupported", "no language server; edges are syntactic", "ast")
    ruby = _language(index, "ruby")
    assert (ruby["status"], ruby["reason"]) == ("unsupported", "file level only")
    # A server for a language the repository does not have is never started.
    assert _log(tmp_path, "gofake") == []
    assert "ruby" not in {lang for s in lsp.SERVERS.values() for lang in s.languages}


def test_one_failing_server_leaves_the_others_ok(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.HCL_APP})
    python = _fake(lsp, tmp_path, {"mode": "crash"})
    hcl = _fake(lsp, tmp_path, {"references": {
        "infra/main.tf:0": [{"path": "infra/main.tf", "line": 6, "character": 17}],
    }}, name="tf", languages=("hcl",), ids={".tf": "terraform"}, call_hierarchy=False,
        references=True, reference_kinds=("variable",))
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, python, hcl))
    assert _language(index, "python")["status"] == "failing"
    assert _language(index, "hcl")["status"] == "ok"
    assert _edge(index, "infra/main.tf#google_storage_bucket.artifacts", "infra/main.tf#var.region",
                 "reference")["evidence"] == "lsp"


# ---------------------------------------------------------------------------
# Evidence labels and determinism
# ---------------------------------------------------------------------------


def test_lsp_edges_come_only_from_ok_languages(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.HCL_APP})
    python = _fake(lsp, tmp_path, {"definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }})
    hcl = _fake(lsp, tmp_path, {"mode": "hang"}, name="tf", languages=("hcl",),
                ids={".tf": "terraform"}, call_hierarchy=False, references=True,
                reference_kinds=("variable",))
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, hcl, request_timeout_seconds=0.2, warmup_timeout_seconds=0.2))
    language_of = {s["id"]: s["language"] for s in index["symbols"]}
    status = {entry["language"]: entry["status"] for entry in index["languages"]}
    assert status["python"] == "ok" and status["hcl"] == "timed_out"
    lsp_edges = [e for e in index["call_edges"] if e["evidence"] == "lsp"]
    assert lsp_edges
    for edge in index["call_edges"]:
        assert edge["evidence"] in EVIDENCE
        assert set(edge["also_evidence"]) <= EVIDENCE - {edge["evidence"]}
    for edge in lsp_edges:
        assert status[language_of[edge["from"]]] == "ok"
        assert edge["confidence"] in (0.95, 0.8)
    # The test map walks `lsp` edges like any other.
    reached = {(m["symbol"], m["test"]) for m in index["symbol_test_map"]}
    assert ("src/pkg/store.py#fetch", "tests/test_users.py#test_load_user") in reached


def test_the_same_answers_give_the_same_bytes(tool: Any, lsp: Any, tmp_path: Path) -> None:
    scenario = {"definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }}
    out = []
    for n in (1, 2):
        work = tmp_path / f"run{n}"
        work.mkdir()
        repo = fx.build_repo(work / "repo", fx.PYTHON_APP)
        spec = _fake(lsp, work, scenario)
        out.append(tool.dumps(tool.graph_document(tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec)))))
    assert out[0] == out[1]


def test_without_the_pass_nothing_changes(tool: Any, tmp_path: Path) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    _no_lsp_edges(index)
    python = _language(index, "python")
    assert python["status"] == "unsupported" and "lsp" not in python
    assert index["extractor"]["lsp"] is None


# ---------------------------------------------------------------------------
# Budgets, the default servers, the command line and the image
# ---------------------------------------------------------------------------


def test_the_server_budget_follows_the_size_table(lsp: Any) -> None:
    # §3.5: under 2,000 source files 10 min; 2,000 - 10,000 30 min; over 45.
    assert lsp.server_budget_seconds(0) == 600
    assert lsp.server_budget_seconds(1_999) == 600
    assert lsp.server_budget_seconds(2_000) == 1_800
    assert lsp.server_budget_seconds(10_000) == 1_800
    assert lsp.server_budget_seconds(10_001) == 2_700
    assert lsp.LspOptions().request_timeout_seconds == 10.0


def test_the_memory_limit_defaults_below_the_container_limit(lsp: Any, tmp_path: Path) -> None:
    limit = tmp_path / "memory.max"
    limit.write_text("8589934592\n")
    assert lsp.default_memory_limit_mib([limit]) == 6_144
    limit.write_text("max\n")
    assert lsp.default_memory_limit_mib([limit]) == 4_096
    assert lsp.default_memory_limit_mib([tmp_path / "absent"]) == 4_096


def test_the_four_servers_are_the_ones_section_3_5_names(lsp: Any) -> None:
    servers = lsp.SERVERS
    assert servers["pyright"].command == ("pyright-langserver", "--stdio")
    assert servers["pyright"].languages == ("python",)
    assert servers["tsserver"].command == ("typescript-language-server", "--stdio")
    assert servers["tsserver"].languages == ("typescript", "javascript")
    assert servers["gopls"].command == ("gopls", "serve")
    assert servers["terraform-ls"].command == ("terraform-ls", "serve")
    assert [name for name, s in servers.items() if s.call_hierarchy] == ["pyright", "tsserver", "gopls"]
    assert servers["terraform-ls"].references and not servers["terraform-ls"].call_hierarchy
    # gopls never downloads a module (§3.5 "no dependencies are installed").
    assert servers["gopls"].env["GOPROXY"] == "off"
    assert servers["gopls"].env["GOTOOLCHAIN"] == "local"


def test_the_command_line_runs_the_pass_under_isolated_mode(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    out = tmp_path / "repo-index.json"
    graph = tmp_path / "graph.json"
    empty = tmp_path / "bin"
    empty.mkdir()
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--repo", str(repo), "--out", str(out),
         "--graph-out", str(graph), "--lsp-bin-dir", str(empty), "--lsp-request-timeout-seconds", "2"],
        capture_output=True, text=True, timeout=120, env=fx.git_env(tmp_path),
    )
    assert done.returncode == 0, done.stderr
    index = json.loads(out.read_text(encoding="utf-8"))
    python = _language(index, "python")
    assert (python["status"], python["reason"]) == \
        ("failing", f"pyright-langserver is not installed in {empty}")
    assert index["extractor"]["lsp"]["request_timeout_seconds"] == 2.0
    assert re.search(r"lsp=python:failing", done.stderr)
    off = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--repo", str(repo), "--out", str(out), "--no-lsp"],
        capture_output=True, text=True, timeout=120, env=fx.git_env(tmp_path),
    )
    assert off.returncode == 0, off.stderr
    assert _language(json.loads(out.read_text(encoding="utf-8")), "python")["status"] == "unsupported"


def test_the_image_installs_the_four_servers_pinned(lsp: Any) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for arg in ("GO_VERSION", "GOPLS_VERSION", "TERRAFORM_LS_VERSION"):
        assert re.search(rf"^ARG {arg}=\d+\.\d+\.\d+$", text, re.M), arg
    for arg in ("GO_SHA256", "TERRAFORM_LS_SHA256"):
        assert re.search(rf"^ARG {arg}=[0-9a-f]{{64}}$", text, re.M), arg
    assert re.search(r'sha256sum -c -', text)
    # npm packages from the lockfile, its integrity hashes, no install scripts.
    assert "npm ci --ignore-scripts" in text
    package = json.loads((LSP_DIR / "package.json").read_text(encoding="utf-8"))
    pins = package["dependencies"]
    assert set(pins) == {"pyright", "typescript", "typescript-language-server"}
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in pins.values()), pins
    lock = json.loads((LSP_DIR / "package-lock.json").read_text(encoding="utf-8"))
    for name, version in pins.items():
        entry = lock["packages"][f"node_modules/{name}"]
        assert entry["version"] == version and entry["integrity"].startswith("sha512-")
    # tsserver comes from TypeScript 6: TypeScript 7's package has no tsserver.
    assert pins["typescript"].startswith("6.")
    # Every server command is linked into the directory the driver resolves from.
    bin_dir = str(lsp.DEFAULT_BIN_DIR)
    for spec in lsp.SERVERS.values():
        assert f"{bin_dir}/{spec.command[0]}" in text, spec.command[0]
    assert f"{bin_dir}/go" in text  # gopls runs `go list`
    # The build proves each server starts and resolves, as the agent user.
    assert "RUN swarm-repo-index --lsp-self-test" in text
    assert "COPY images/agent-runtime-base/repo-index/lsp/ /opt/repo-index/lsp/" in text
