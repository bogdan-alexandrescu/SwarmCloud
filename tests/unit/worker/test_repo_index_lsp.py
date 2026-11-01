"""The headless LSP pass of the repository index (lane RI10).

docs/repo-index.md §3.5 runs one language server per language as a child
process over stdio -- pyright, tsserver, gopls, terraform-ls -- and asks it
`textDocument/definition` for every candidate call site the tree-sitter pass
found, `callHierarchy/incomingCalls` for every exported function where the
server has call hierarchy, and `textDocument/references` where it does not.
A resolved site becomes an `lsp` edge; everything else stays `ast`.

These tests drive the driver in images/agent-runtime-indexer/repo-index/lsp/
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
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
SCRIPT = TOOL_DIR / "repo_index_extract.py"
LSP_DIR = TOOL_DIR / "lsp"
DOCKERFILE = REPO_ROOT / "images" / "agent-runtime-indexer" / "Dockerfile"
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


# ---------------------------------------------------------------------------
# Less work: sites no answer could resolve, and paths the pass never opens
# ---------------------------------------------------------------------------


# use.py asks four call sites: `rows.append` and `os.getcwd` name nothing in
# the repository and nothing this file imports, so no answer to them could
# be kept (`_symbol_named_at` drops a location that is not a known symbol's
# name); `get_row` is bound by an import (an alias of store.fetch) and
# `helper` is a repository symbol, so both are asked.
EXTERNAL_APP: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/store.py": "def fetch(key):\n    return key\n",
    "pkg/use.py": (
        "import os\n"  # 1
        "from .store import fetch as get_row\n"  # 2
        "\n"  # 3
        "\n"  # 4
        "def load(rows):\n"  # 5
        "    rows.append(1)\n"  # 6
        "    os.getcwd()\n"  # 7
        "    get_row(rows)\n"  # 8
        "    return helper(rows)\n"  # 9
        "\n"  # 10
        "\n"  # 11
        "def helper(rows):\n"  # 12
        "    return rows\n"  # 13
    ),
}


def test_a_site_naming_nothing_in_the_repository_or_its_imports_is_not_asked(
        tool: Any, lsp: Any, tmp_path: Path) -> None:
    # #1693: pyright had >=86,805 sites on ai-studio and asked every one, a
    # call on a list or into the standard library included, though no
    # answer to those could ever become an edge.
    repo = fx.build_repo(tmp_path / "repo", EXTERNAL_APP)
    spec = _fake(lsp, tmp_path, {"definitions": {
        "pkg/use.py:7": {"path": "pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
        "pkg/use.py:8": {"path": "pkg/use.py", "line": 11, "character": 4, "name": "helper"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    asked = sorted(p["position"]["line"] for p in _asked(_log(tmp_path), "textDocument/definition")
                   if p["textDocument"]["uri"].endswith("/pkg/use.py"))
    # MUTATION: ask every site again in `_ServerRun._session()` (drop the
    # `_external` filter) and lines 5 and 6 are asked too.
    assert asked == [7, 8]
    python = _language(index, "python")
    # MUTATION: drop the `sites_skipped_external` count in `_ServerRun` and
    # this is a KeyError.
    assert python["lsp"]["sites_skipped_external"] == 2
    assert python["status"] == "ok"
    # MUTATION: skip on "not a repository symbol" alone, ignoring the
    # file's imports (`Site.imported`), and `get_row` is never asked: this
    # edge is not `lsp`.
    assert _edge(index, "pkg/use.py#load", "pkg/store.py#fetch")["evidence"] == "lsp"
    assert _edge(index, "pkg/use.py#load", "pkg/use.py#helper")["evidence"] == "lsp"
    # A skipped site was never a request: it is not counted as unresolved.
    assert python["lsp"]["unresolved"] == 0



# The platform's LSP exclusions (servers.py LSP_EXCLUDED_PATHS): migrations,
# alembic revisions and docs/ are parsed by tree-sitter like any file, but
# no server is asked about them -- each call in a migration is a site the
# pass paid for and impact never selects a test by.
EXCLUDED_APP: dict[str, str] = {
    "app/__init__.py": "",
    "app/models.py": "def save(row):\n    return row\n",
    "app/use.py": "from app.models import save\n\n\ndef run():\n    return save(5)\n",
    "app/migrations/__init__.py": "",
    "app/migrations/0001_initial.py": "from app.models import save\n\n\ndef forwards():\n    return save(1)\n",
    "migrations/0002_top.py": "from app.models import save\n\n\ndef top():\n    return save(2)\n",
    "alembic/versions/abc_add.py": "from app.models import save\n\n\ndef upgrade():\n    return save(3)\n",
    "docs/conf.py": "from app.models import save\n\n\ndef setup():\n    return save(4)\n",
}
EXCLUDED = ("app/migrations/__init__.py", "app/migrations/0001_initial.py", "migrations/0002_top.py",
            "alembic/versions/abc_add.py", "docs/conf.py")


def test_the_lsp_exclusions_match_as_gitignore_globs(lsp: Any) -> None:
    # `**/` is zero or more directories, so a top-level migrations/ is one;
    # `docs/**` is the root's docs/ only.
    # MUTATION: match with fnmatch in `lsp_excluded()` and the top-level
    # migrations/ and alembic/ paths read False.
    assert [lsp.lsp_excluded(path) for path in (
        "migrations/0001.py", "app/migrations/0001.py", "a/b/migrations/x/y.py",
        "alembic/versions/abc.py", "svc/alembic/versions/abc.py", "docs/conf.py", "docs/a/b.py",
    )] == [True] * 7
    # MUTATION: anchor nothing in `_glob_regex()` and src/docs/ reads True.
    assert [lsp.lsp_excluded(path) for path in (
        "app/migrations.py", "app/migrations_util/x.py", "alembic/env.py", "src/docs/conf.py",
        "docsite/x.py", "app/use.py",
    )] == [False] * 6
    assert lsp.LSP_EXCLUDED_PATHS == ("**/migrations/**", "**/alembic/versions/**", "docs/**")


def test_excluded_paths_are_parsed_but_never_opened_in_the_server(tool: Any, lsp: Any,
                                                                   tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", EXCLUDED_APP)
    spec = _fake(lsp, tmp_path, {"definitions": {
        "app/use.py:4": {"path": "app/models.py", "line": 0, "character": 4, "name": "save"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    log = _log(tmp_path)
    opened = {p["textDocument"]["uri"].split(repo.resolve().as_uri() + "/", 1)[1]
              for p in _asked(log, "textDocument/didOpen")}
    # MUTATION: drop the `lsp_excluded` test from `_ServerRun._session()`'s
    # paths and every excluded file with a site or an exported function is
    # opened (phase 1 and phase 2 alike).
    assert opened & set(EXCLUDED) == set()
    assert "app/use.py" in opened  # the control: a file outside them is asked
    asked = {p["textDocument"]["uri"] for p in _asked(log, "textDocument/definition")
             + _asked(log, "textDocument/prepareCallHierarchy")}
    assert not any(uri.endswith(path) for uri in asked for path in EXCLUDED)
    rows = _file_rows(index)
    # Tree-sitter still parses them and their `ast` edges stand.
    assert {path: rows[path]["status"] for path in EXCLUDED} == {path: "parsed" for path in EXCLUDED}
    assert _edge(index, "docs/conf.py#setup", "app/models.py#save")["evidence"] == "ast"
    assert _edge(index, "alembic/versions/abc_add.py#upgrade", "app/models.py#save")["evidence"] == "ast"
    assert _edge(index, "app/use.py#run", "app/models.py#save")["evidence"] == "lsp"
    # No server answered them, so no row says it did.
    assert [path for path in EXCLUDED if rows[path]["lsp"]] == []
    python = _language(index, "python")
    assert python["status"] == "ok"
    # MUTATION: drop the `files_excluded` / `sites_excluded` counts in
    # `_ServerRun._session()` and these are KeyErrors.
    assert (python["lsp"]["files_excluded"], python["lsp"]["sites_excluded"]) == (5, 4)


def test_an_incremental_run_does_not_turn_partial_over_an_excluded_file(tool: Any, lsp: Any,
                                                                        tmp_path: Path) -> None:
    # An excluded file is never covered, by design. An incremental run turns
    # an `ok` language `partial` while a carried file is uncovered, so
    # without this every incremental run of a repository with a migration
    # would report `partial` -- the banner flipping #1693 already names.
    repo = fx.build_repo(tmp_path / "repo", EXCLUDED_APP)
    spec = _fake(lsp, tmp_path, {}, name="basefake")
    full = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec))
    assert _language(full, "python")["status"] == "ok"
    base = tool.Base(sha=full["commit_sha"], graph=tool.graph_document(full),
                     index=tool.index_document(full))
    _commit(repo, {"app/use.py": "from app.models import save\n\n\ndef run():\n    return save(6)\n"})
    index = tool.extract(repo, tool.Budget(), base=base, lsp=_options(lsp, _fake(lsp, tmp_path, {})))
    assert index["kind"] == "incremental" and index["changes"]["affected"] == ["app/use.py"]
    # MUTATION: count excluded files as uncovered in `_languages()` and this
    # reads `partial`.
    assert _language(index, "python")["status"] == "ok", _language(index, "python")["reason"]


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
    # The warm-up window covers `initialize`, which the fake does answer: 0.2s
    # lost that race to a cold interpreter start on a loaded runner, and the
    # language failed on initialize instead of on the hung queries. The first
    # query then waits out this window too, so it stays well inside 30s.
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, spec, request_timeout_seconds=0.2, warmup_timeout_seconds=5))
    assert time.monotonic() - started < 30
    python = _language(index, "python")
    assert python["status"] == "timed_out"
    assert "consecutive requests" in python["reason"]
    assert python["fallback"] == "ast"
    assert python["lsp"]["request_timeouts"] == 3
    _no_lsp_edges(index)
    # A hung server may have been failing before it stopped, so a hang keeps
    # nothing even where whole files were answered: no file is covered.
    # MUTATION: keep the finished files' edges on `_Hung` as on a budget cut
    # and this reads `partial` with files covered.
    assert python["files_covered"] == 0
    assert not any(row["lsp"] for row in index["files"])
    assert _edge(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch")["evidence"] == "ast"


# Three files the server meets in this order: a.py (one site, answered at
# once), b.py (no site) and z.py (ten sites, each answered slowly), so a
# phase-1 budget cut lands inside z.py after a.py and b.py are finished.
CUT_APP: dict[str, str] = {
    "src/__init__.py": "",
    "src/a.py": "from .b import helper\n\n\ndef first():\n    return helper()\n",
    "src/b.py": "def helper():\n    return 1\n",
    "src/z.py": "from .b import helper\n\n\ndef late():\n" + "    helper()\n" * 10,
}
# a.py line 5 and z.py line 5, `helper()` -> b.py `def helper`.
CUT_DEFINITIONS = {
    "src/a.py:4": {"path": "src/b.py", "line": 0, "character": 4, "name": "helper"},
    "src/z.py:4": {"path": "src/b.py", "line": 0, "character": 4, "name": "helper"},
}


def _file_rows(index: dict) -> dict[str, dict]:
    return {row["path"]: row for row in index["files"]}


def test_a_phase_1_budget_cut_keeps_the_files_it_finished(tool: Any, lsp: Any, tmp_path: Path) -> None:
    # #1693: pyright, healthy, ran out of its 270 s share inside phase 1 on
    # ai-studio and `_stop` threw away 7,106 resolved sites. A cut is the
    # driver's own decision, taken while the server was still answering, so
    # every file whose sites were all asked is complete and kept.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    spec = _fake(lsp, tmp_path, {"slow_paths": {"src/z.py": 0.6}, "definitions": CUT_DEFINITIONS})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, server_budget_seconds=3.0))
    python = _language(index, "python")
    # MUTATION: restore `_stop(STATUS_TIMED_OUT, ...)` on every phase-1
    # BudgetExceeded in `_ServerRun.run()` and this reads `timed_out`.
    assert python["status"] == "partial", python["reason"]
    # MUTATION: drop the reason `_ServerRun.run()` writes for a cut and this
    # reads "over the 3-second budget for fake".
    assert re.fullmatch(r"fake reached 3 of 4 files \(1 of \d+ sites\) in its 3-second budget",
                        python["reason"]), python["reason"]
    assert python["fallback"] == "ast"
    # MUTATION: count every file the server serves, not only the finished
    # ones, in `_languages()` and this reads 4.
    assert python["files_covered"] == 3
    # MUTATION: keep `edges = []` in `_stop` for a cut, or drop `partial`
    # from run_pass's kept statuses, and a.py's edge is `ast` again.
    assert _edge(index, "src/a.py#first", "src/b.py#helper")["evidence"] == "lsp"
    # z.py was cut mid-file: its answered sites are not kept, so nothing in
    # the graph is `lsp` in a file whose row says it is not.
    assert [e for e in index["call_edges"] if e["from"].startswith("src/z.py")
            and (e["evidence"] == "lsp" or "lsp" in e["also_evidence"])] == []
    rows = _file_rows(index)
    # MUTATION: drop the `lsp` flag `extract()` sets on each file row and
    # these are KeyErrors; set it from the language's status alone and a.py
    # reads False.
    assert {path: rows[path]["lsp"] for path in ("src/__init__.py", "src/a.py", "src/b.py", "src/z.py")} \
        == {"src/__init__.py": True, "src/a.py": True, "src/b.py": True, "src/z.py": False}


def test_a_budget_cut_before_any_file_is_finished_is_timed_out(tool: Any, lsp: Any,
                                                                 tmp_path: Path) -> None:
    # The control for the test above: a cut that finished no file with a
    # site has nothing to keep, so the language is `timed_out`, as before.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    spec = _fake(lsp, tmp_path, {"slow_paths": {"src/a.py": 30.0}, "definitions": CUT_DEFINITIONS})
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, spec, server_budget_seconds=1.5, request_timeout_seconds=30.0,
        warmup_timeout_seconds=30.0))
    python = _language(index, "python")
    # MUTATION: report `partial` whenever a file was passed, site or not,
    # and the empty __init__.py makes this `partial`.
    assert python["status"] == "timed_out"
    assert "1.5-second budget" in python["reason"]
    assert python["files_covered"] == 0
    _no_lsp_edges(index)
    assert not any(row["lsp"] for row in index["files"])


def test_an_ok_language_covers_every_file_it_parsed(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS})))
    python = _language(index, "python")
    assert python["status"] == "ok"
    # MUTATION: leave `lsp` False on an ok language's rows and impact
    # distrusts every file of a language whose server finished.
    assert python["files_covered"] == 4
    assert all(row["lsp"] for row in index["files"] if row["language"] == "python")


def test_without_the_pass_no_file_is_covered(tool: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    index = tool.extract(repo, tool.Budget())
    assert [row["lsp"] for row in index["files"]] == [False] * len(index["files"])


def _commit(repo: Path, changes: dict[str, str]) -> None:
    env = fx.git_env(repo.parent / (repo.name + "-home"))
    fx.write_files(repo, changes)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=env, check=True)
    stamp = "@1780086400 +0000"
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"],
                   env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}, check=True)


def _partial_base(tool: Any, lsp: Any, tmp_path: Path, repo: Path) -> Any:
    spec = _fake(lsp, tmp_path, {"slow_paths": {"src/z.py": 0.6}, "definitions": CUT_DEFINITIONS},
                 name="cutfake")
    facts = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, server_budget_seconds=3.0))
    assert _language(facts, "python")["status"] == "partial", _language(facts, "python")["reason"]
    return tool.Base(sha=facts["commit_sha"], graph=tool.graph_document(facts),
                     index=tool.index_document(facts))


def test_an_incremental_run_carries_an_untouched_files_coverage(tool: Any, lsp: Any,
                                                                 tmp_path: Path) -> None:
    # An incremental run asks the server about the affected files only, and
    # every other file keeps its base edges -- so it keeps its base coverage
    # too. A server that answered every affected file does not make the
    # language whole while a carried file was never covered.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    base = _partial_base(tool, lsp, tmp_path, repo)
    _commit(repo, {"src/a.py": "from .b import helper\n\n\ndef first():\n    return helper()\n\n\n"
                               "def second():\n    return 2\n"})
    facts = tool.extract(repo, tool.Budget(), base=base, lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS})))
    assert facts["kind"] == "incremental" and facts["changes"]["affected"] == ["src/a.py"]
    rows = _file_rows(facts)
    # MUTATION: set every row's `lsp` from this run's server alone and z.py,
    # which no server has finished, reads True.
    assert (rows["src/a.py"]["lsp"], rows["src/b.py"]["lsp"], rows["src/z.py"]["lsp"]) == (True, True, False)
    python = _language(facts, "python")
    # MUTATION: take the run's `ok` without counting the carried rows in
    # `_languages()` and impact trusts z.py.
    assert (python["status"], python["files_covered"], python["fallback"]) == ("partial", 3, "ast")
    assert python["reason"] == ("fake answered every file this run asked about; 3 of 4 files are "
                                "covered, the rest carried uncovered from the base index")


def test_a_base_written_before_file_coverage_takes_its_languages_status(tool: Any, lsp: Any,
                                                                        tmp_path: Path) -> None:
    # A graph written before files[].lsp existed has no flag to carry. Its
    # language's status said it then: `ok` was every file, anything else none.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    facts = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS}, name="okfake")))
    for row in facts["files"]:
        del row["lsp"]
    base = tool.Base(sha=facts["commit_sha"], graph=tool.graph_document(facts),
                     index=tool.index_document(facts))
    _commit(repo, {"src/a.py": "from .b import helper\n\n\ndef first():\n    return helper()\n\n\n"
                               "def second():\n    return 2\n"})
    index = tool.extract(repo, tool.Budget(), base=base, lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS})))
    # MUTATION: read a missing flag as False and the language turns partial.
    assert _language(index, "python")["status"] == "ok"
    assert all(row["lsp"] for row in index["files"] if row["language"] == "python")


A_CHANGED = ("from .b import helper\n\n\ndef first():\n    return helper()\n\n\n"
             "def second():\n    return 2\n")


def _bad_base(tool: Any, lsp: Any, tmp_path: Path, repo: Path, scenario: dict,
              **options: Any) -> tuple[Any, dict]:
    spec = _fake(lsp, tmp_path, dict(scenario, definitions=CUT_DEFINITIONS), name="badfake")
    facts = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, **options))
    return (tool.Base(sha=facts["commit_sha"], graph=tool.graph_document(facts),
                      index=tool.index_document(facts)), _language(facts, "python"))


@pytest.mark.parametrize("scenario,options,status", [
    ({"slow_paths": {"src/a.py": 30.0}},
     {"server_budget_seconds": 1.5, "request_timeout_seconds": 30.0,
      "warmup_timeout_seconds": 30.0}, "timed_out"),
    ({"mode": "crash"}, {}, "failing"),
], ids=["timed_out", "failing"])
def test_an_incremental_run_keeps_its_bases_worse_status(tool: Any, lsp: Any, tmp_path: Path,
                                                         scenario: dict, options: dict,
                                                         status: str) -> None:
    # #1693: every full run of ai-studio timed out, every incremental run
    # between them asked its server about a handful of files, got `ok`, and
    # the banner flipped to "partial" and back with each run. An
    # incremental run that did not re-ask every file of a language carries
    # the rest from the base, so it is no better than the base said.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    base, before = _bad_base(tool, lsp, tmp_path, repo, scenario, **options)
    assert before["status"] == status, before["reason"]
    _commit(repo, {"src/a.py": A_CHANGED})
    facts = tool.extract(repo, tool.Budget(), base=base, lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS})))
    assert facts["kind"] == "incremental" and facts["changes"]["affected"] == ["src/a.py"]
    python = _language(facts, "python")
    # MUTATION: take the run's own `ok` in `_languages()` (or downgrade it
    # only to `partial`) and this reads `partial`, the flip.
    assert python["status"] == status, python["reason"]
    assert python["fallback"] == "ast"
    # Merged with coverage: what this run answered still counts, and
    # impact trusts it under any coverable status.
    # MUTATION: count only the base's covered rows and this reads 0.
    assert python["files_covered"] == 1
    assert _file_rows(facts)["src/a.py"]["lsp"] is True
    # MUTATION: keep the base's reason unchanged and the coverage the run
    # added is not said.
    assert python["reason"] == (f"{before['reason']} (the base index's pass; this run asked "
                                "fake about 1 of 4 files and 1 of 4 are covered)")


def test_an_incremental_run_that_re_asked_every_file_takes_its_own_status(
        tool: Any, lsp: Any, tmp_path: Path) -> None:
    # The control: a change that reaches every file of the language asks
    # the server about all of them, so nothing is carried and the base's
    # status has nothing left to describe.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    base, before = _bad_base(tool, lsp, tmp_path, repo, {"mode": "crash"})
    assert before["status"] == "failing"
    _commit(repo, {"src/__init__.py": "# package\n", "src/a.py": A_CHANGED,
                   "src/b.py": "def helper():\n    return 3\n",
                   "src/z.py": CUT_APP["src/z.py"] + "\n\ndef later():\n    return 1\n"})
    facts = tool.extract(repo, tool.Budget(), base=base, lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS})))
    assert facts["kind"] == "incremental"
    assert facts["changes"]["affected"] == ["src/__init__.py", "src/a.py", "src/b.py", "src/z.py"]
    python = _language(facts, "python")
    # MUTATION: take the worse status even when every file was re-asked
    # and this reads `failing` for an index no failing server touched.
    assert (python["status"], python["files_covered"], python["fallback"]) == ("ok", 4, None)


def test_an_incremental_run_whose_server_did_worse_than_the_base_says_so(
        tool: Any, lsp: Any, tmp_path: Path) -> None:
    # The other half of "the worse of the two": a base whose server finished
    # and an incremental run whose server crashed reads `failing`, with the
    # carried files still covered.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    good = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS}, name="okfake")))
    base = tool.Base(sha=good["commit_sha"], graph=tool.graph_document(good),
                     index=tool.index_document(good))
    _commit(repo, {"src/a.py": A_CHANGED})
    facts = tool.extract(repo, tool.Budget(), base=base, lsp=_options(
        lsp, _fake(lsp, tmp_path, {"mode": "crash"})))
    python = _language(facts, "python")
    assert python["status"] == "failing" and python["files_covered"] == 3
    assert python["reason"].startswith("fake exited with status 3")
    assert python["reason"].endswith("(this run asked fake about 1 of 4 files; 3 of 4 are "
                                     "covered, carried from the base index)")


def test_slow_workspace_queries_are_not_a_hung_server(tool: Any, lsp: Any, tmp_path: Path) -> None:
    # pyright's incomingCalls searches the whole workspace and took up to 60 s
    # a symbol on this repository while its definitions took milliseconds. A
    # timed-out workspace query is a slow answer, not a server that stopped
    # answering, so it never counts toward max_consecutive_timeouts.
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"slow_methods": {"textDocument/prepareCallHierarchy": 0.5},
                                 "definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, request_timeout_seconds=0.2))
    python = _language(index, "python")
    assert python["status"] == "ok", python["reason"]
    assert python["lsp"]["request_timeouts"] >= 3
    assert _edge(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch")["evidence"] == "lsp"


def test_every_definition_is_asked_before_any_workspace_query(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    tool.extract(repo, tool.Budget(), lsp=_options(lsp, _fake(lsp, tmp_path, {})))
    methods = [m.get("method") for m in _log(tmp_path) if m.get("method", "").startswith(
        ("textDocument/definition", "textDocument/prepareCallHierarchy"))]
    first_hierarchy = methods.index("textDocument/prepareCallHierarchy")
    assert "textDocument/definition" not in methods[first_hierarchy:]


def test_a_budget_cut_in_the_workspace_queries_keeps_the_definitions(tool: Any, lsp: Any,
                                                                      tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    spec = _fake(lsp, tmp_path, {"slow_methods": {"textDocument/prepareCallHierarchy": 0.4},
                                 "definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, server_budget_seconds=1.5))
    python = _language(index, "python")
    # Every site was asked; only the supplementary callers were cut, and the
    # row says how far they got.
    assert python["status"] == "ok"
    assert re.fullmatch(r"call hierarchy and references cut at their 0.8-second share "
                        r"of the 1.5-second budget: \d+ of \d+ symbols asked", python["reason"])
    assert python["lsp"]["symbols_skipped"] > 0
    assert _edge(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch")["evidence"] == "lsp"


def _two_slow_servers(lsp: Any, tmp_path: Path, scenario: dict | None = None) -> tuple[Any, Any]:
    """A Python and a TypeScript fake whose workspace queries are each slow."""
    slow = {"textDocument/prepareCallHierarchy": 0.5}
    python = _fake(lsp, tmp_path, scenario or {"slow_methods": slow, "definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4, "name": "fetch"},
    }}, name="pyfake")
    typescript = _fake(lsp, tmp_path, {"slow_methods": slow, "definitions": {
        # App.tsx line 4, `formatName("a", "b")` -> format.ts `export function formatName`
        "web/src/App.tsx:3": {"path": "web/src/format.ts", "line": 0, "character": 16,
                              "name": "formatName"},
    }}, name="tsfake", languages=("typescript", "javascript"),
        ids={".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript"})
    return python, typescript


def test_the_whole_pass_ends_inside_its_total_budget(tool: Any, lsp: Any, tmp_path: Path) -> None:
    # The servers run one after another inside one indexer step, which
    # swarm-api kills at its full-run timeout: a step killed there leaves no
    # index at all. Each server's §3.5 budget here (60 s) is far more than
    # the total (3 s), and both servers' workspace queries would run past it
    # (0.5 s a symbol), so only the total and the workspace share can end
    # the pass in time -- and the second server must still get its turn.
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.TS_APP})
    python, typescript = _two_slow_servers(lsp, tmp_path)
    started = time.monotonic()
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, typescript, server_budget_seconds=60.0, total_budget_seconds=3.0))
    # The total, plus the shutdown of each server and the tree-sitter pass.
    assert time.monotonic() - started < 3.0 + 2.5
    assert index["extractor"]["lsp"]["total_budget_seconds"] == 3.0
    rows = {name: _language(index, name) for name in ("python", "typescript")}
    assert {name: row["status"] for name, row in rows.items()} == {"python": "ok", "typescript": "ok"}
    # The first server's workspace queries were cut at their share, not run to the total.
    assert re.fullmatch(r"call hierarchy and references cut at their [\d.]+-second share of the "
                        r"[\d.]+-second budget: \d+ of \d+ symbols asked", rows["python"]["reason"])
    assert rows["python"]["lsp"]["symbols_skipped"] > 0
    # The second server still ran phase 1, and its definition edge is kept.
    assert _asked(_log(tmp_path, "tsfake"), "textDocument/definition")
    assert _edge(index, "web/src/App.tsx#App", "web/src/format.ts#formatName")["evidence"] == "lsp"
    assert _edge(index, "src/pkg/users.py#load_user", "src/pkg/store.py#fetch")["evidence"] == "lsp"


# ---------------------------------------------------------------------------
# Grants by measured cost (lane INDEX-BUDGETS, part of #1693)
# ---------------------------------------------------------------------------


def test_with_no_history_every_server_needs_an_equal_share(lsp: Any) -> None:
    # The control: no previous run, or one that measured nothing, is today's
    # equal split.
    assert lsp.planned_needs(["gopls", "pyright", "tsserver"], None, 1_260.0) == [420.0] * 3
    assert lsp.planned_needs(["gopls", "pyright"], {}, 600.0) == [300.0, 300.0]


def test_a_measured_server_needs_its_cost_and_an_unmeasured_one_the_equal_share(lsp: Any) -> None:
    # MUTATION: ignore `costs` in planned_needs() and pyright needs 420
    # again, the share it ran out of on ai-studio.
    assert lsp.planned_needs(["gopls", "pyright", "tsserver"], {"pyright": 890, "tsserver": 120},
                             1_260.0) == [420.0, 890.0, 120.0]


def test_the_cheapest_server_runs_first(lsp: Any) -> None:
    # #2991: on SwarmCloud's own registration pyright (need several times
    # the whole pass) ran first and left tsserver 71 s and terraform-ls 10 s.
    # Cheapest first, so the servers that can finish do, and the one that
    # cannot takes what they leave. Ties keep the name order.
    # MUTATION: return range(len(names)) from run_order() and pyright runs first.
    assert lsp.run_order(["pyright", "terraform-ls", "tsserver"], [4_500.0, 30.0, 240.0]) \
        == [1, 2, 0]
    assert lsp.run_order(["gopls", "pyright"], [300.0, 300.0]) == [0, 1]


def test_a_grant_is_an_equal_share_of_what_is_left_capped_at_its_budget(lsp: Any) -> None:
    # tsserver first, pyright after: 1,000 s left, costs 100 and 900. The
    # first gets an equal share and passes on what it does not use, so a
    # server that turns out slower than it measured can take no more than
    # that share from the ones after it (#2991).
    # MUTATION: grant `left * needs[0] / sum(needs)` (the old proportion)
    # and this reads 100, the seconds tsserver ran out of.
    assert lsp.grant_seconds(1_000.0, [100.0, 900.0], 1_800.0) == pytest.approx(500.0)
    # MUTATION: same, and pyright first would take 900 of the 1,000.
    assert lsp.grant_seconds(1_000.0, [900.0, 100.0], 1_800.0) == pytest.approx(500.0)
    # §3.5's per-server budget is still the cap.
    assert lsp.grant_seconds(1_000.0, [100.0, 900.0], 400.0) == 400.0
    # The last server gets all that is left.
    assert lsp.grant_seconds(250.0, [100.0], 1_800.0) == pytest.approx(250.0)
    assert lsp.grant_seconds(-3.0, [100.0], 1_800.0) == 0.0


def test_the_workspace_queries_get_their_share_of_the_servers_proportion(lsp: Any) -> None:
    # Phase 2 is supplementary: a cheap server's equal grant (500 of 1,000)
    # must not become 250 s of incomingCalls taken from pyright's
    # definitions. Its limit is the workspace share of the proportion its
    # need is of what is left -- 0.5 x 1,000 x 100 / 1,000 -- and never more
    # than the share of its own grant.
    # MUTATION: return `grant * share` and this reads 250.
    assert lsp.workspace_seconds(1_000.0, [100.0, 900.0], 1_800.0, 0.5) == pytest.approx(50.0)
    # Equal needs: the share of the equal grant, as before.
    assert lsp.workspace_seconds(1_000.0, [7.0, 7.0], 1_800.0, 0.5) == pytest.approx(250.0)
    assert lsp.workspace_seconds(1_000.0, [100.0, 900.0], 60.0, 0.5) == pytest.approx(30.0)
    assert lsp.workspace_seconds(-1.0, [100.0], 1_800.0, 0.5) == 0.0


def test_a_cut_servers_cost_is_extrapolated_from_its_requests_and_grant(lsp: Any) -> None:
    # #1693's measurement: pyright asked 26,562 sites in its 270 s and had
    # 86,805; at that rate it needed ~882 s, recorded in 10-second steps.
    # MUTATION: record the grant itself as a cut server's cost and this
    # reads 270, so the next run grants pyright no more than before.
    assert lsp.measured_cost("partial", elapsed=270.4, grant=270.0, requests=26_562,
                             sites=86_805) == 890
    assert lsp.measured_cost("timed_out", elapsed=270.1, grant=270.0, requests=3_000,
                             sites=6_000) == 540
    # A server that finished needed what it used, never less than one step.
    assert lsp.measured_cost("ok", elapsed=33.2, grant=270.0, requests=900, sites=800) == 40
    assert lsp.measured_cost("ok", elapsed=0.4, grant=270.0, requests=3, sites=1) == 10
    # Nothing measured: a failing server, or a cut one that asked nothing.
    assert lsp.measured_cost("failing", elapsed=1.0, grant=270.0, requests=10, sites=10) is None
    assert lsp.measured_cost("timed_out", elapsed=270.0, grant=270.0, requests=0,
                             sites=10) is None


def test_a_slow_server_no_longer_leaves_the_next_one_seconds(tool: Any, lsp: Any,
                                                             tmp_path: Path) -> None:
    # #2991, measured 2026-11-01 on SwarmCloud's own registration: pyright's
    # need took 1,175.5 s of the 1,260-second pass and tsserver reached 304
    # of 1,130 files in the 71.4 s left. Here pyfake is slow on every query
    # and measured to need 900 s; tsfake needs 100. tsfake runs first, on an
    # equal share, finishes, and passes what it did not use to pyfake.
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.TS_APP})
    python, typescript = _two_slow_servers(lsp, tmp_path, {"mode": "slow", "delay": 0.4})
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, typescript, server_budget_seconds=60.0, total_budget_seconds=3.0,
        request_timeout_seconds=5.0, server_costs={"pyfake": 900, "tsfake": 100}))
    rows = {name: _language(index, name) for name in ("python", "typescript")}
    # MUTATION: run the servers in name order with the proportional grant
    # and tsfake gets 0.3 s of the 3.0, after pyfake has used its 2.7.
    assert rows["typescript"]["lsp"]["grant_seconds"] >= 1.4
    assert rows["typescript"]["status"] == "ok", rows["typescript"]["reason"]
    assert _edge(index, "web/src/App.tsx#App", "web/src/format.ts#formatName")["evidence"] == "lsp"
    # What tsfake left -- its phase 2 cut at 0.5 x 3.0 x 100 / 1,000 -- went to pyfake.
    assert rows["python"]["lsp"]["grant_seconds"] >= 2.0
    # The counts stay truthful: `ok` only when every file was covered, and a
    # cut names exactly the files its rows say are covered.
    python_rows = [row for row in index["files"] if row["language"] == "python"]
    covered = sum(1 for row in python_rows if row["lsp"])
    assert rows["python"]["files_covered"] == covered
    if rows["python"]["status"] == "ok":
        assert covered == len(python_rows)
    else:
        assert rows["python"]["status"] in ("partial", "timed_out"), rows["python"]["reason"]
        assert covered < len(python_rows)
    if rows["python"]["status"] == "partial":
        found = re.fullmatch(r"pyfake reached (\d+) of (\d+) files \(\d+ of \d+ sites\) in its "
                             r"[\d.]+-second budget", rows["python"]["reason"])
        assert found, rows["python"]["reason"]
        assert int(found.group(1)) == covered
    assert all(row["lsp"] for row in index["files"] if row["language"] == "typescript"
               and row["path"].startswith("web/"))
    # Each row still records what its server cost, for the next run.
    assert rows["typescript"]["lsp"]["cost_seconds"] >= 10
    assert index["extractor"]["lsp"]["server_costs"] == {"pyfake": 900, "tsfake": 100}


def test_each_server_records_its_peak_memory_for_the_next_decision(tool: Any, lsp: Any,
                                                                  tmp_path: Path) -> None:
    # #2991: running the servers side by side, rather than cheapest first,
    # is a memory question (the indexer has 8 GiB, each server may hold
    # three quarters of it), and no run recorded what a server holds. Each
    # row now says, in 256 MiB steps so timing noise does not move the
    # graph's bytes.
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, _fake(lsp, tmp_path, {"definitions": CUT_DEFINITIONS})))
    counts = _language(index, "python")["lsp"]
    # MUTATION: leave `peak_rss_mib` out of run_pass()'s counts and this is a KeyError.
    assert counts["peak_rss_mib"] >= lsp.RSS_STEP_MIB
    assert counts["peak_rss_mib"] % lsp.RSS_STEP_MIB == 0
    assert lsp.rss_mib(0) == 0
    assert lsp.rss_mib(1) == 256
    assert lsp.rss_mib(256 * 1024 * 1024) == 256
    assert lsp.rss_mib(256 * 1024 * 1024 + 1) == 512


def test_a_cheap_servers_workspace_queries_are_cut_at_its_proportion(tool: Any, lsp: Any,
                                                                    tmp_path: Path) -> None:
    # The other half of the rule above: tsfake's equal grant (2.0 of 4.0)
    # is not spent on its slow call hierarchy; that is held to the workspace
    # share of its proportion, 0.5 x 4.0 x 100 / 1,000 = 0.2 s.
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.TS_APP})
    python, typescript = _two_slow_servers(lsp, tmp_path)
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, typescript, server_budget_seconds=60.0, total_budget_seconds=4.0,
        server_costs={"pyfake": 900, "tsfake": 100}))
    typescript_row = _language(index, "typescript")
    assert typescript_row["status"] == "ok"
    # MUTATION: give phase 2 `grant * workspace_share` and this reads
    # "their 1-second share of the 2-second budget".
    assert re.fullmatch(r"call hierarchy and references cut at their 0\.2-second share of the "
                        r"2-second budget: \d+ of \d+ symbols asked", typescript_row["reason"]), \
        typescript_row["reason"]
    assert _language(index, "python")["lsp"]["grant_seconds"] >= 3.0


def test_without_history_the_pass_is_split_equally(tool: Any, lsp: Any, tmp_path: Path) -> None:
    # The control for the test above: the same servers, no costs.
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.TS_APP})
    python, typescript = _two_slow_servers(lsp, tmp_path)
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, typescript, server_budget_seconds=60.0, total_budget_seconds=2.0))
    assert _language(index, "python")["lsp"]["grant_seconds"] <= 1.0
    assert index["extractor"]["lsp"]["server_costs"] == {}


def test_a_cut_server_records_a_cost_above_its_grant(tool: Any, lsp: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", CUT_APP)
    spec = _fake(lsp, tmp_path, {"slow_paths": {"src/z.py": 0.6}, "definitions": CUT_DEFINITIONS})
    index = tool.extract(repo, tool.Budget(), lsp=_options(lsp, spec, server_budget_seconds=3.0))
    python = _language(index, "python")
    assert python["status"] == "partial", python["reason"]
    assert python["lsp"]["grant_seconds"] == 3.0
    # MUTATION: leave `cost_seconds` out of a cut server's counts and the
    # next run has no measurement for exactly the server that ran out.
    assert python["lsp"]["cost_seconds"] >= 10


def test_a_server_whose_turn_comes_after_the_total_is_not_started(tool: Any, lsp: Any,
                                                                  tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.TS_APP})
    # The first server never answers, not even `shutdown`: it uses its
    # grant (half the total) and then the shutdown grace the client allows
    # past any budget, which leaves nothing for the second.
    python, typescript = _two_slow_servers(lsp, tmp_path, {"mode": "hang", "ignore_shutdown": True})
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, typescript, server_budget_seconds=60.0, total_budget_seconds=0.6))
    assert _language(index, "python")["status"] == "timed_out"
    typescript_row = _language(index, "typescript")
    assert (typescript_row["status"], typescript_row["reason"], typescript_row["fallback"]) == \
        ("timed_out", "the LSP pass's 0.6-second total budget ran out before tsfake started", "ast")
    assert _log(tmp_path, "tsfake") == []
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
    # Wide enough that the answering python fake never times out on a loaded
    # runner (0.2s did, and python came back timed_out); hcl still hangs.
    index = tool.extract(repo, tool.Budget(), lsp=_options(
        lsp, python, hcl, request_timeout_seconds=1, warmup_timeout_seconds=5))
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


def test_the_total_budget_is_half_the_full_run_row(lsp: Any) -> None:
    # §3.5's full run: 20 / 60 / 120 minutes. Half goes to the LSP pass, the
    # rest to the agent and the tree-sitter pass; under 2,000 files that is
    # also well inside swarm-api's 1,800-second step timeout.
    assert lsp.total_budget_seconds(0) == 600
    assert lsp.total_budget_seconds(1_999) == 600
    assert lsp.total_budget_seconds(2_000) == 1_800
    assert lsp.total_budget_seconds(10_001) == 3_600
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
    assert [name for name, s in servers.items() if s.call_hierarchy] == ["pyright", "tsserver", "gopls"]
    # terraform-ls is back in the image (lane INDEX-LANGS, owner 2026-10-20):
    # a source build linked with Go 1.27.1 answers references, which the
    # build's self-test now requires for HCL as for the other four.
    # MUTATION: keep terraform-ls in DISABLED_SERVERS and this is red.
    tfls = servers["terraform-ls"]
    assert tfls.command == ("terraform-ls", "serve")
    assert tfls.languages == ("hcl",)
    assert tfls.references and not tfls.call_hierarchy
    assert lsp.DISABLED_SERVERS == {}
    # gopls never downloads a module (§3.5 "no dependencies are installed").
    assert servers["gopls"].env["GOPROXY"] == "off"
    assert servers["gopls"].env["GOTOOLCHAIN"] == "local"


def test_tsserver_runs_as_one_full_server_so_a_first_definition_follows_an_import(lsp: Any) -> None:
    # typescript-language-server's default `useSyntaxServer: "auto"` answers
    # definition requests from a partial-semantic syntax server while the
    # project is still loading, which cannot resolve an import into another
    # file; the driver's first request always lands in that window.
    options = lsp.SERVERS["tsserver"].initialization_options
    assert options["tsserver"]["useSyntaxServer"] == "never"
    assert options["disableAutomaticTypingAcquisition"] is True


def test_the_command_line_runs_the_pass_under_isolated_mode(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    out = tmp_path / "repo-index.json"
    graph = tmp_path / "graph.json"
    empty = tmp_path / "bin"
    empty.mkdir()
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--repo", str(repo), "--out", str(out),
         "--graph-out", str(graph), "--lsp-bin-dir", str(empty), "--lsp-request-timeout-seconds", "2",
         "--lsp-total-budget-seconds", "30", "--lsp-server-cost", "pyright=890",
         "--lsp-server-cost", "tsserver=120"],
        capture_output=True, text=True, timeout=120, env=fx.git_env(tmp_path),
    )
    assert done.returncode == 0, done.stderr
    index = json.loads(out.read_text(encoding="utf-8"))
    python = _language(index, "python")
    assert (python["status"], python["reason"]) == \
        ("failing", f"pyright-langserver is not installed in {empty}")
    assert index["extractor"]["lsp"]["request_timeout_seconds"] == 2.0
    assert index["extractor"]["lsp"]["total_budget_seconds"] == 30.0
    # MUTATION: drop `--lsp-server-cost` from main() and this is an argparse error.
    assert index["extractor"]["lsp"]["server_costs"] == {"pyright": 890, "tsserver": 120}
    assert re.search(r"lsp=python:failing", done.stderr)
    off = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--repo", str(repo), "--out", str(out), "--no-lsp"],
        capture_output=True, text=True, timeout=120, env=fx.git_env(tmp_path),
    )
    assert off.returncode == 0, off.stderr
    assert _language(json.loads(out.read_text(encoding="utf-8")), "python")["status"] == "unsupported"


def test_the_image_installs_the_four_servers_pinned(lsp: Any) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for arg in ("GO_VERSION",):
        assert re.search(rf"^ARG {arg}=\d+\.\d+\.\d+$", text, re.M), arg
    # gopls may be pinned to a pre-release (v0.24.0-pre.1 carries the fixed
    # golang.org/x modules); still an exact version, never a range or latest.
    assert re.search(r"^ARG GOPLS_VERSION=\d+\.\d+\.\d+(-pre\.\d+)?$", text, re.M)
    assert re.search(r"^ARG GO_SHA256=[0-9a-f]{64}$", text, re.M)
    assert re.search(r'sha256sum -c -', text)
    # gopls and terraform-ls are compiled here with the golang.org/x modules
    # trivy flagged raised to their fixed versions (release 37289476429 was
    # refused on them), never taken as a vendor binary, and the build fails
    # if a raised version is not what the binary was linked with.
    for arg in ("X_MOD_VERSION", "X_TEXT_VERSION", "X_CRYPTO_VERSION", "X_NET_VERSION"):
        assert re.search(rf"^ARG {arg}=\d+\.\d+\.\d+$", text, re.M), arg
    assert "releases.hashicorp.com/terraform-ls" not in text
    assert 'go version -m "${out}"' in text
    builds = dict(re.findall(r'build_server (\S+) "\$\{(\w+)\}" /usr/local/bin/', text))
    # MUTATION: drop the terraform-ls build_server line and this is red.
    assert builds == {
        "golang.org/x/tools/gopls": "GOPLS_VERSION",
        "github.com/hashicorp/terraform-ls": "TERRAFORM_LS_VERSION",
    }, builds
    assert re.search(r"^ARG TERRAFORM_LS_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert re.search(r"^ARG TERRAFORM_LS_SHA256=[0-9a-f]{64}$", text, re.M)
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
    # Every server command is in the directory the driver resolves from, and
    # the build fails if one is not executable there.
    bin_dir = str(lsp.DEFAULT_BIN_DIR)
    check = re.search(r"for s in ([^;]+); do \\\n\s*test -x \"" + re.escape(bin_dir) + r"/\$s\"", text)
    assert check, "the Dockerfile does not check the servers' bin directory"
    checked = set(check.group(1).split())
    for spec in lsp.SERVERS.values():
        assert spec.command[0] in checked, spec.command[0]
    assert "go" in checked  # gopls runs `go list`
    # The build proves each server starts and resolves, as the agent user.
    # Part of the indexer's one self-test RUN (test_image_streaming.py says why).
    assert " && swarm-repo-index --lsp-self-test" in text
    assert "COPY images/agent-runtime-indexer/repo-index/lsp/ /opt/repo-index/lsp/" in text
