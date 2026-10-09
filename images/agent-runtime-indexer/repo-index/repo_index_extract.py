#!/usr/bin/env python3
"""The mechanical half of a repository index: one tree-sitter pass.

docs/repo-index.md §3.4 and §3.5 (lane RI3). The indexer prompt runs this
first, as `swarm-repo-index --repo <checkout> --out <file> [--graph-out <file>]`, and the agent then
spends its tokens on what needs reading -- purposes, territory, notes, and
checking the edges this tool was unsure of. Everything here is deterministic
and cheap, which is what keeps a full run on a 2,000-file repository inside
its timeout.

WHAT IT EMITS. Two documents, because §2.2 (revised 2026-10-04) keeps the
graph out of the 512 KiB index:

  --out        repo-index.json, schema `swarm.repo-index/v1`, at most
               --max-index-bytes (512 KiB): commit_sha, branch, kind, base_sha,
               built_at, modules, routes, test_map, hot_spots, languages, and
               `graph`, the graph's summary -- counts per language, files by
               status, the 100 most-called symbols, and the sha256 digest of
               the graph document. Over the budget, lists give way in a fixed
               order and are named in `truncated`.
  --graph-out  the graph, schema `swarm.repo-graph/v1`, for the shard writer
               (lane RI9): symbols, call_edges, symbol_test_map and every file.

extract() returns the facts both are cut from:

  commit_sha, branch, kind, base_sha, built_at   what the index describes
  modules          per directory holding code: language, files, lines
  routes           HTTP routes (FastAPI, Flask, Express, net/http, gin/chi)
                   and Terraform resource/module/variable blocks
  symbols          functions, classes, methods, types, routes, tests, HCL
                   blocks: id `<path>#<qualified name>`, kind, path,
                   start_line, end_line, language, exported. A route also
                   carries `method` and `route` (the URL path: `path` is
                   already the file).
  call_edges       call / reference / inherit / route_handler / import edges,
                   each with `evidence`, `confidence` and `also_evidence`
  symbol_test_map  which test symbols reach which symbols over call_edges
  test_map         source file -> test file, each edge with its evidence
  hot_spots        the most-changed files of the 90 days before HEAD, with
                   the paths most often changed with them
  languages        per language: grammar, the server §3.5 names, status
  files            EVERY file, with language, lines, status and reason --
                   a file that was not parsed is listed with why, never
                   silently skipped
  truncated        which lists a budget cut short
  extractor        this tool's version, budget, and the history window

and, since version 3 (lane KG2, docs/design/knowledge-graph.md §6), five
graph-only lists the shard writer stores as format 3's index layers:

  communities      the application files grouped by Louvain over the
                   resolved `ast`/`lsp`/`import` edges (never a judged one),
                   each split into connected parts; an incremental run is
                   seeded with its base's partition and keeps its ids
  terms            BM25 postings over application symbols' names, classes,
                   files, signatures and first docstring sentence, with
                   `term_stats`; repo_graph_shards.bm25_search is the reader
  signatures       each callable's normalised signature and fingerprint
  signature_changes  on an incremental run, the fingerprints that moved
  flows            the bounded call flow from each route, Python
                   `__main__` guard and Go `main`

Version 3 also resolves calls through a module object (§2.2 item 6):
`from pkg import mod; mod.f()`, `import pkg.mod; pkg.mod.f()` and
`from mod import Cls; Cls.method()`.

The agent's keys (purposes, entry_points, test_layout, territory, commands,
notes) are not invented here. `built_at` is left null: the same commit must
give the same bytes (the index's digest identifies its content, §2.3), so the
wall clock stays out; whoever promotes the index stamps it.

EVIDENCE AND CONFIDENCE follow §2.5: `ast` 0.6 for a call whose name matches
exactly one definition in scope or in an imported module, 0.3 for each of
several; `import` 0.4; `naming` 0.3; `co-change` the pair's Jaccard support,
capped at 0.5. Only in-repository edges are resolved. The test map adds
`path-ref` (0.35; 0.3 for a Terraform module reached through the root a
`.tftest.hcl` runs) and `declared` (0.2, a `test_layout[].covers` glob), and
a test's `obj.method()` resolves by a unique method name (`ast` 0.3);
docs/repo-index.md §2.5, "The test map", says why.

THE LSP PASS (lane RI10, lsp/). After the tree-sitter pass each language's
server (pyright, tsserver, gopls, terraform-ls) is started headless over
stdio and asked about every candidate site; a resolved site becomes an `lsp`
edge at 0.95 (0.8 through an inferred receiver type), keeping `ast` in
`also_evidence`. Each language's row in `languages` says `ok`, `timed_out`,
`failing` or `unsupported`, with the reason; a language whose server was
stopped keeps only its `ast` edges, and the run still succeeds. `--no-lsp`
skips the pass: every language is then `unsupported` with "edges are
syntactic", as before RI10. lsp/driver.py documents the budget, the memory
stop and the server's environment.

BUDGET. A file over `--max-file-bytes` is listed `too_large`; once the
parsed bytes reach `--max-total-bytes` the rest are `over_budget`; files past
`--max-files` are counted, not listed. Each file gets `--file-timeout-seconds`
for its parse and walk; a file that runs out is `timed_out`, and the run
still succeeds. A timeout is a less certain index, never a failed one.

The 90-day window is anchored at the HEAD commit's committer time, not the
wall clock, so a rerun on the same commit counts the same commits.

INCREMENTAL (§3.4, lane IX2). `--base-sha`, `--base-index` and `--base-graph`
give the run the previous promoted index of an ancestor, as the worker staged
it. The run is incremental when `incremental_changes` allows it and full,
with the reason in `extractor.incremental`, when it does not; the section
"incremental runs" below says what is rewritten and what is carried.

Symlinks are listed and never followed: a checkout is untrusted input, and a
link to a file outside it must not put that file's contents in an artifact.
"""

from __future__ import annotations

import argparse
import fnmatch
import gc
import importlib.metadata
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import tree_sitter
import tree_sitter_go
import tree_sitter_hcl
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

# The LSP pass lives beside this script, in lsp/. The image runs the script
# under `python -I`, which puts nothing on sys.path, so its own directory is
# added here (root-owned and read-only in the image, like the script).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lsp as lsp_pass  # noqa: E402
# The shard writer's tokenizer and BM25 constants: one copy, so the index
# and every reader split text the same way (lane KG2).
import repo_graph_shards as srg  # noqa: E402

SCHEMA = "swarm.repo-index/v1"
# The graph's own document, for the shard writer (lane RI9). §2.2 keeps the
# graph out of repo-index.json, so it never travels under the index's schema.
GRAPH_SCHEMA = "swarm.repo-graph/v1"
# §2.2: repo-index.json is at most 512 KiB, whatever the repository's size.
MAX_INDEX_BYTES = 512 * 1024
# §2.2: the summary carries the 100 most-called symbols.
MOST_CALLED = 100
EXTRACTOR_NAME = "swarm-repo-index"
# "2" (QA G4-04/05): test-side classification under a test_layout root, the
# name-unique method fallback and the evidence ranking change what an edge
# means. An incremental run carries an unchanged file's edges verbatim, so a
# base extracted by "1" would keep the old edges until some full run; the
# version check below refuses it instead.
#
# "3" (lane KG2, docs/design/knowledge-graph.md §6): module-object calls
# resolve (`issueruns.planner_prompt(...)` after `from swarm_api import
# issueruns`, `a.b.f()` after `import a.b`, `Cls.method()` on an imported
# class), so a version-2 graph lacks edges this version finds, and the graph
# gains communities, a BM25 term index, signature fingerprints and
# entry-point flows. A version-2 base is a full run, by the same check.
EXTRACTOR_VERSION = "3"

# §2.5's confidences, by evidence.
AST_UNIQUE = 0.6
AST_AMBIGUOUS = 0.3
IMPORT_CONFIDENCE = 0.4
NAMING_CONFIDENCE = 0.3
CO_CHANGE_CAP = 0.5
# A test file that names a repository path in a string literal -- a script it
# runs through subprocess, a manifest it reads, a module it loads with
# `spec_from_file_location`, a `.tftest.hcl` `source = "../../terraform/x"`:
# stronger than a name that merely matches, weaker than an import.
PATH_REF_CONFIDENCE = 0.35
# A `.tftest.hcl` reaching a Terraform module through the root it runs
# (`module.iam` in an assertion, `module "iam" { source = "../modules/iam" }`
# in that root): one step removed from the path the test names.
HCL_MODULE_CONFIDENCE = 0.3
# A suite's `test_layout[].covers` glob, as the agent last wrote it: the
# weakest evidence there is, but it is what connects shell, Terraform and
# manifests that no import reaches.
DECLARED_CONFIDENCE = 0.2
# A test's `obj.method()` whose name is defined as a method on exactly one
# class in the repository: the type is not known, the name is unique.
NAME_UNIQUE_CONFIDENCE = 0.3
EVIDENCE_RANK = {"lsp": 6, "ast": 5, "import": 4, "path-ref": 3, "naming": 2, "declared": 1,
                 "co-change": 0}
# The test-map evidence swarm-api's RepoIndexSpec accepts (`repoindex.Evidence`).
# `path-ref` is reported to it as `declared` -- the test names the path it
# exercises -- and stays `path-ref` in the graph's file-level map, so an index
# the agent copies this map into is never refused for a value it does not know.
SERVED_EVIDENCE = {"path-ref": "declared"}

# A path is TEST-SIDE -- a test, or a helper, conftest or fixture a test
# imports -- when a directory on it has one of these names, when it is one of
# these files, or when it lies under a `test_layout[].root`. A test-side file
# is never a test map source and never counts as a source module: a helper
# imported by its tests would otherwise read as "covered source".
TEST_DIRECTORY_NAMES = frozenset({"tests", "test", "__tests__", "testdata"})
TEST_SIDE_FILENAMES = frozenset({"conftest.py"})
# Languages whose every file is build or packaging, not source to map tests to.
NON_SOURCE_LANGUAGES = frozenset({"dockerfile", "make"})
# One test naming hundreds of paths is a fixture listing, not coverage.
PATH_REF_MAX_PER_TEST = 50

# §3.4: git log --numstat --since=90.days; §2.5: at least 5 commits together.
HISTORY_DAYS = 90
CO_CHANGE_MIN_COMMITS = 5
# A commit touching more files than this (a mass rename, a reformat) says
# nothing about which files move together, so it counts for hot spots only.
CO_CHANGE_MAX_FILES_PER_COMMIT = 50
CHANGED_WITH_PER_FILE = 10

# §3.5 step 3: the test walk's depth and confidence floor.
TEST_WALK_MAX_DEPTH = 6
TEST_WALK_MIN_CONFIDENCE = 0.2

# §2.2's per-list bounds where the doc names one; the graph lists are capped
# at a size the shard writer (RI9) can take, and say so when cut.
MAX_MODULES = 400
MAX_ROUTES = 1_000
MAX_TEST_MAP = 4_000
MAX_HOT_SPOTS = 50
MAX_SYMBOLS = 200_000
MAX_EDGES = 500_000
MAX_SYMBOL_TEST_MAP = 200_000
# The file-level test map the graph carries (§2.5, G4-07). repo-index.json
# holds at most MAX_TEST_MAP edges, at directory granularity when the file
# level does not fit; every file-level edge is in the graph, on the source
# file's row (`files[].tests`), so a reader pages it by the file's module.
MAX_FILE_TEST_MAP = 200_000

# How often the walk looks at the clock. Cheap enough to be frequent.
_CLOCK_EVERY = 256

# §3.4: an incremental run over this many changed files or more is a full
# run. GitHub's compare lists at most 300 files, which is where swarm-api
# reads the same rule (`swarm_api.repoindex.MAX_INCREMENTAL_CHANGES`); the
# two are held equal by tests/unit/worker/test_repo_index_incremental.py.
MAX_INCREMENTAL_CHANGES = 300

# §3.4 and §3.5: a change to any of these changes what `commands`, `test_map`
# or a language server's resolution mean EVERYWHERE, not only in the changed
# file, so carrying the other entries forward would carry stale meaning. Such
# a change makes the run full. By file name, by base-name glob, and by
# directory. swarm-api applies the same lists before it submits
# (`swarm_api.repoindex.CONFIG_FILENAMES` and friends), held equal by the
# same test; this side applies them again to the diff it actually reads.
CONFIG_FILENAMES = frozenset({
    "Makefile", "GNUmakefile", "makefile",
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "noxfile.py",
    "conftest.py", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock",
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lockb",
    "go.mod", "go.sum", "go.work",
    "pyrightconfig.json", ".terraform.lock.hcl",
})
CONFIG_GLOBS = (
    "requirements*.txt", "tsconfig*.json", "jsconfig*.json", "jest.config.*",
    "vitest.config.*", "vitest.workspace.*", "playwright.config.*", "karma.conf.*",
    ".mocharc*",
)
CONFIG_DIRECTORIES = (".github/workflows/",)


def is_config_path(path: str) -> bool:
    """Whether a change to `path` forces a full run (§3.4, §3.5)."""
    name = posixpath.basename(path)
    if name in CONFIG_FILENAMES:
        return True
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in CONFIG_GLOBS):
        return True
    return any(path.startswith(prefix) for prefix in CONFIG_DIRECTORIES)

# --- languages --------------------------------------------------------------

GRAMMAR_PACKAGES = {
    "python": "tree-sitter-python",
    "typescript": "tree-sitter-typescript",
    "javascript": "tree-sitter-javascript",
    "go": "tree-sitter-go",
    "hcl": "tree-sitter-hcl",
}

# The language server §3.5 assigns each language (lsp/servers.py starts them).
LANGUAGE_SERVERS = {
    "python": "pyright",
    "typescript": "tsserver",
    "javascript": "tsserver",
    "go": "gopls",
    # "hcl": terraform-ls is out of the image for now (lsp/servers.py
    # DISABLED_SERVERS); HCL is tree-sitter only.
}

# extension -> (language, grammar key)
SUPPORTED_EXTENSIONS = {
    ".py": ("python", "python"),
    ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"),
    ".mts": ("typescript", "typescript"),
    ".cts": ("typescript", "typescript"),
    ".tsx": ("typescript", "tsx"),
    ".js": ("javascript", "javascript"),
    ".jsx": ("javascript", "javascript"),
    ".mjs": ("javascript", "javascript"),
    ".cjs": ("javascript", "javascript"),
    ".go": ("go", "go"),
    ".tf": ("hcl", "hcl"),
    ".hcl": ("hcl", "hcl"),
    ".tfvars": ("hcl", "hcl"),
}

# Source languages this extractor has no grammar for. They are listed as
# `unsupported`, by name, rather than lumped in with documentation.
UNSUPPORTED_EXTENSIONS = {
    ".rb": "ruby", ".java": "java", ".rs": "rust", ".kt": "kotlin",
    ".kts": "kotlin", ".scala": "scala", ".swift": "swift", ".c": "c",
    ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".sh": "shell", ".bash": "shell",
    ".zsh": "shell", ".lua": "lua", ".pl": "perl", ".r": "r", ".ex": "elixir",
    ".exs": "elixir", ".erl": "erlang", ".clj": "clojure", ".dart": "dart",
    ".m": "objective-c", ".vue": "vue", ".svelte": "svelte", ".sql": "sql",
    ".proto": "protobuf", ".groovy": "groovy", ".fs": "fsharp",
    ".hs": "haskell", ".ml": "ocaml", ".zig": "zig", ".nim": "nim",
    ".ps1": "powershell", ".jl": "julia",
}
UNSUPPORTED_FILENAMES = {
    "Dockerfile": "dockerfile", "Makefile": "make", "GNUmakefile": "make",
    "Jenkinsfile": "groovy", "Rakefile": "ruby", "Gemfile": "ruby",
}
_SHEBANG_LANGUAGES = (
    (re.compile(rb"^#![^\n]*\bpython[0-9.]*\b"), "python"),
    (re.compile(rb"^#![^\n]*\bnode\b"), "javascript"),
    (re.compile(rb"^#![^\n]*\b(?:ba|z|k|da)?sh\b"), "shell"),
    (re.compile(rb"^#![^\n]*\bruby\b"), "ruby"),
    (re.compile(rb"^#![^\n]*\bperl\b"), "perl"),
)

UNSUPPORTED_REASON = "file level only"
UNSUPPORTED_FALLBACK = "line counts, co-change and hot spots"
# The reason when the LSP pass is skipped (--no-lsp).
SERVER_REASON = "no language server run by this extractor; edges are syntactic"

_LANGUAGE_FACTORIES: dict[str, Callable[[], Any]] = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "javascript": tree_sitter_javascript.language,
    "go": tree_sitter_go.language,
    "hcl": tree_sitter_hcl.language,
}

VCS_DIRS = {".git", ".hg", ".svn"}


@dataclass(frozen=True)
class Budget:
    """The size budget and the per-file timeout (§3.5)."""

    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 268_435_456
    max_files: int = 50_000
    file_timeout_seconds: float = 10.0


class _FileTimeout(Exception):
    """The per-file deadline passed during the parse or the walk."""


def _grammar_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# --- per-file facts ---------------------------------------------------------


@dataclass
class Facts:
    """What one parse yields, before cross-file resolution."""

    path: str
    language: str
    test_file: bool
    symbols: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    # (caller id, name, qualifier or None, line)
    calls: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    inherits: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    # (route id, handler name, qualifier or None, line)
    handlers: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    # (caller id, terraform address, line)
    references: list[tuple[str, str, int]] = field(default_factory=list)
    # language-specific import records, each with a "line"
    imports: list[dict] = field(default_factory=list)
    # local name -> (import index, imported name)
    bindings: dict[str, tuple[int, str]] = field(default_factory=dict)
    # local name -> import index: a module, namespace or package alias
    aliases: dict[str, int] = field(default_factory=dict)
    # import indexes whose every name is in scope (`from m import *`)
    wildcards: list[int] = field(default_factory=list)
    # CommonJS `module.exports = { a }` names
    commonjs_exports: set[str] = field(default_factory=set)
    # (caller id, name, line) of a Python call through an attribute whose
    # object is not a plain name (`self.scheduler._admit_one()`): recorded
    # with no qualifier above, and told apart from a bare call here.
    attribute_calls: set[tuple[str, str, int]] = field(default_factory=set)
    # HCL: every `module.<name>` the file names, wherever the expression sits
    # (an assertion's `module.iam.x != ""` is an operation, which `references`
    # does not walk into).
    module_refs: set[str] = field(default_factory=set)
    # (caller id, name, line) -> `a.b` of a Python call `a.b.name()` whose
    # object is a dotted chain of plain names: `import a.b` then `a.b.f()`
    # (lane KG2, module-object calls).
    dotted_calls: dict[tuple[str, str, int], str] = field(default_factory=dict)
    # Version 3 (lane KG2): symbol id -> its normalised signature text, for
    # callables; symbol id -> the first lines of its docstring or leading
    # comment, for search.
    signatures: dict[str, str] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)
    # Python: the line span of `if __name__ == "__main__":`, an entry point.
    main_guard: tuple[int, int] | None = None
    package: str | None = None
    has_error: bool = False
    _ids: set[str] = field(default_factory=set)

    def add_symbol(self, qual: str, kind: str, node: Any, exported: bool,
                   start_node: Any = None, **extra: Any) -> str:
        start = (start_node or node).start_point[0] + 1
        symbol_id = f"{self.path}#{qual}"
        if symbol_id in self._ids:
            symbol_id = f"{symbol_id}@{start}"
        self._ids.add(symbol_id)
        record = {
            "id": symbol_id,
            "kind": kind,
            "path": self.path,
            "start_line": start,
            "end_line": node.end_point[0] + 1,
            "language": self.language,
            "exported": bool(exported),
        }
        record.update(extra)
        self.symbols.append(record)
        return symbol_id


class _Clock:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.count = 0

    def tick(self) -> None:
        self.count += 1
        if self.count % _CLOCK_EVERY == 0 and time.monotonic() > self.deadline:
            raise _FileTimeout()


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _children(node: Any, *types: str) -> list[Any]:
    return [c for c in node.named_children if c.type in types]


# --- signatures and docs (version 3) ------------------------------------------

#: A docstring or leading comment is cut to its first sentence, and to this
#: many characters: the search index wants what a symbol is for, which the
#: first sentence says, not its whole manual.
DOC_CHARS = 200
#: A signature longer than this is stored cut; its fingerprint is of the whole.
SIGNATURE_CHARS = 500
_SPACE = re.compile(r"\s+")
_COMMENT_MARKS = re.compile(r"^\s*(?:/\*\*?|\*/|\*|//+|#+)\s?", re.MULTILINE)


def _record_signature(facts: Facts, symbol_id: str, node: Any, src: bytes,
                      fields: tuple[str, ...]) -> None:
    """A callable's signature: its parameter list and return type, whitespace
    collapsed, so a reformat is not a change and a new parameter is."""
    parts = []
    for name in fields:
        child = node.child_by_field_name(name)
        if child is None:
            continue
        text = _SPACE.sub(" ", _text(child, src)).strip()
        # TypeScript's return type node is the annotation, colon included.
        if name in ("return_type", "result") and not text.startswith(":"):
            text = "-> " + text
        parts.append(text)
    if parts:
        facts.signatures[symbol_id] = _normalise_signature(" ".join(parts))


_OPEN_SPACE = re.compile(r"([(\[{])\s+")
_SPACE_CLOSE = re.compile(r"\s+([)\]}])")
_TRAILING_COMMA = re.compile(r",([)\]}])")


def _normalise_signature(text: str) -> str:
    """One spelling per signature: no space inside brackets and no trailing
    comma, so a parameter list split over lines fingerprints like the same
    list on one line."""
    text = _SPACE_CLOSE.sub(r"\1", _OPEN_SPACE.sub(r"\1", text))
    return _TRAILING_COMMA.sub(r"\1", text)


def _clean_doc(text: str) -> str:
    text = _SPACE.sub(" ", _COMMENT_MARKS.sub("", text)).strip()
    end = text.find(". ")
    return (text if end < 0 else text[:end + 1])[:DOC_CHARS]


def _record_doc(facts: Facts, symbol_id: str, text: str | None) -> None:
    if text:
        cleaned = _clean_doc(text)
        if cleaned:
            facts.docs[symbol_id] = cleaned


def _py_docstring(body: Any, src: bytes) -> str | None:
    first = next((c for c in body.named_children if c.type != "comment"), None) \
        if body is not None else None
    if first is None or first.type != "expression_statement" or not first.named_children:
        return None
    string = first.named_children[0]
    if string.type != "string":
        return None
    return "".join(_text(c, src) for c in string.named_children if c.type == "string_content")


def _leading_comment(node: Any, src: bytes) -> str | None:
    """The comment block directly above a definition (JSDoc, Go doc comments)."""
    anchor = node
    while anchor.parent is not None and anchor.parent.type in (
            "export_statement", "lexical_declaration", "variable_declaration"):
        anchor = anchor.parent
    lines: list[str] = []
    previous = anchor.prev_sibling
    expected_row = anchor.start_point[0]
    while previous is not None and previous.type == "comment" \
            and previous.end_point[0] >= expected_row - 1:
        lines.insert(0, _text(previous, src))
        expected_row = previous.start_point[0]
        previous = previous.prev_sibling
    return "\n".join(lines) or None


_DOTTED_NAMES = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_PY_MAIN_GUARD = re.compile(r"""^__name__\s*==\s*["']__main__["']$|^["']__main__["']\s*==\s*__name__$""")


def _py_dotted(node: Any, src: bytes) -> str | None:
    """`a.b.c` when `node` is an attribute chain of plain names, else None.

    `self.x.f()` and `cls.x.f()` are the common case and never a module, so
    the chain's root is checked before any of it is decoded."""
    chain = node
    while chain is not None and chain.type == "attribute":
        chain = chain.child_by_field_name("object")
    if chain is None or chain.type != "identifier" or _text(chain, src) in ("self", "cls"):
        return None
    text = _text(node, src)
    return text if _DOTTED_NAMES.match(text) else None


# --- Python -----------------------------------------------------------------

_PY_ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options",
                     "websocket"}
_PY_ROUTE_ANY = {"route", "api_route"}


def _py_string(node: Any, src: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    return "".join(_text(c, src) for c in node.named_children if c.type == "string_content")


def _py_call_target(fn: Any, src: bytes) -> tuple[str, str | None] | None:
    if fn.type == "identifier":
        return _text(fn, src), None
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        if attr is None:
            return None
        qualifier = _text(obj, src) if obj is not None and obj.type == "identifier" else ""
        return _text(attr, src), qualifier
    return None


def _py_routes(decorated: Any, src: bytes) -> list[tuple[str, str, int]]:
    """(method, path, line) for each route decorator on a definition."""
    found: list[tuple[str, str, int]] = []
    for decorator in _children(decorated, "decorator"):
        call = next((c for c in decorator.named_children if c.type == "call"), None)
        if call is None:
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        attr = fn.child_by_field_name("attribute")
        name = _text(attr, src) if attr is not None else ""
        if name not in _PY_ROUTE_METHODS and name not in _PY_ROUTE_ANY:
            continue
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        positional = [a for a in args.named_children if a.type not in ("keyword_argument", "comment")]
        route = _py_string(positional[0], src) if positional else None
        if route is None or not route.startswith("/"):
            continue
        methods: list[str]
        if name in _PY_ROUTE_ANY:
            methods = ["GET"]
            for kw in _children(args, "keyword_argument"):
                key = kw.child_by_field_name("name")
                value = kw.child_by_field_name("value")
                if key is not None and _text(key, src) == "methods" and value is not None:
                    listed = [_py_string(v, src) for v in value.named_children]
                    methods = [m.upper() for m in listed if m]
        else:
            methods = [name.upper()]
        for method in methods:
            found.append((method, route, decorator.start_point[0] + 1))
    return found


def _extract_python(facts: Facts, root: Any, src: bytes, clock: _Clock) -> None:
    # (node, prefix, owner id, class qual or None, in function, exported)
    stack: list[tuple[Any, str, str, str | None, bool, bool]] = [
        (root, "", facts.path, None, False, True)
    ]
    while stack:
        node, prefix, owner, cls, in_func, exported = stack.pop()
        clock.tick()
        kind = node.type
        if kind == "decorated_definition":
            definition = node.child_by_field_name("definition")
            for decorator in _children(node, "decorator"):
                stack.append((decorator, prefix, owner, cls, in_func, exported))
            if definition is not None:
                stack.append((definition, prefix, owner, cls, in_func, exported))
            continue
        if kind in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node, src)
            qual = prefix + name
            parent = node.parent
            decorated = parent if parent is not None and parent.type == "decorated_definition" else None
            is_exported = exported and not in_func and not name.startswith("_")
            if kind == "class_definition":
                symbol_id = facts.add_symbol(qual, "class", node, is_exported, start_node=decorated)
                _record_doc(facts, symbol_id, _py_docstring(node.child_by_field_name("body"), src))
                bases = node.child_by_field_name("superclasses")
                if bases is not None:
                    for base in bases.named_children:
                        target = _py_call_target(base, src)
                        if target is not None:
                            facts.inherits.append((symbol_id, target[0], target[1] or None,
                                                   base.start_point[0] + 1))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, qual + ".", symbol_id, qual, in_func, is_exported))
                continue
            if facts.test_file and name.startswith("test") and (cls is None or cls.split(".")[-1].startswith("Test")):
                symbol_kind = "test"
            elif cls is not None:
                symbol_kind = "method"
            else:
                symbol_kind = "function"
            symbol_id = facts.add_symbol(qual, symbol_kind, node, is_exported, start_node=decorated)
            if symbol_kind != "test":
                _record_signature(facts, symbol_id, node, src, ("parameters", "return_type"))
            _record_doc(facts, symbol_id, _py_docstring(node.child_by_field_name("body"), src))
            if decorated is not None:
                for method, route, line in _py_routes(decorated, src):
                    route_id = facts.add_symbol(f"{method} {route}", "route", decorated, True,
                                                method=method, route=route)
                    facts.routes.append({"id": route_id, "method": method, "path": route,
                                         "file": facts.path, "handler": symbol_id,
                                         "start_line": decorated.start_point[0] + 1,
                                         "end_line": decorated.end_point[0] + 1,
                                         "language": facts.language})
                    facts.handlers.append((route_id, "\x00" + symbol_id, None, line))
            for child in (node.child_by_field_name("parameters"), node.child_by_field_name("body")):
                if child is not None:
                    stack.append((child, qual + ".", symbol_id, None, True, False))
            continue
        if kind == "call":
            fn = node.child_by_field_name("function")
            target = _py_call_target(fn, src) if fn is not None else None
            if target is not None:
                facts.calls.append((owner, target[0], target[1] if target[1] else None,
                                    node.start_point[0] + 1))
                if target[1] == "":
                    facts.attribute_calls.add((owner, target[0], node.start_point[0] + 1))
                    obj = fn.child_by_field_name("object")
                    dotted = _py_dotted(obj, src)
                    if dotted is not None:
                        facts.dotted_calls[(owner, target[0], node.start_point[0] + 1)] = dotted
        elif kind == "if_statement" and owner == facts.path and facts.main_guard is None:
            condition = node.child_by_field_name("condition")
            if condition is not None and _PY_MAIN_GUARD.match(
                    _SPACE.sub(" ", _text(condition, src)).strip()):
                facts.main_guard = (node.start_point[0] + 1, node.end_point[0] + 1)
        elif kind == "import_statement":
            for name_node in node.named_children:
                if name_node.type == "dotted_name":
                    module = _text(name_node, src)
                    facts.imports.append({"module": module, "level": 0, "names": [],
                                          "line": node.start_point[0] + 1})
                    facts.aliases[module.split(".")[0]] = len(facts.imports) - 1
                elif name_node.type == "aliased_import":
                    real = name_node.child_by_field_name("name")
                    alias = name_node.child_by_field_name("alias")
                    if real is None or alias is None:
                        continue
                    facts.imports.append({"module": _text(real, src), "level": 0, "names": [],
                                          "line": node.start_point[0] + 1})
                    facts.aliases[_text(alias, src)] = len(facts.imports) - 1
            continue
        elif kind == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            level = 0
            module = ""
            if module_node is not None:
                if module_node.type == "relative_import":
                    prefix_node = next((c for c in module_node.children if c.type == "import_prefix"), None)
                    level = len(_text(prefix_node, src)) if prefix_node is not None else 1
                    dotted = next((c for c in module_node.named_children if c.type == "dotted_name"), None)
                    module = _text(dotted, src) if dotted is not None else ""
                else:
                    module = _text(module_node, src)
            names: list[tuple[str, str]] = []
            wildcard = False
            for child in node.children_by_field_name("name"):
                if child.type == "dotted_name":
                    text = _text(child, src)
                    names.append((text, text))
                elif child.type == "aliased_import":
                    real = child.child_by_field_name("name")
                    alias = child.child_by_field_name("alias")
                    if real is not None and alias is not None:
                        names.append((_text(real, src), _text(alias, src)))
            if any(c.type == "wildcard_import" for c in node.named_children):
                wildcard = True
            facts.imports.append({"module": module, "level": level,
                                  "names": [n for n, _ in names],
                                  "line": node.start_point[0] + 1})
            index = len(facts.imports) - 1
            for real, alias in names:
                facts.bindings[alias] = (index, real)
            if wildcard:
                facts.wildcards.append(index)
            continue
        for child in reversed(node.named_children):
            # A leaf (a name, a number, a string's text) is none of the node
            # types above, so it is not walked: about half of all nodes, and
            # the time version 3's extra layers are paid for with (lane KG2).
            if child.named_child_count:
                stack.append((child, prefix, owner, cls, in_func, exported))


# --- JavaScript / TypeScript ------------------------------------------------

_JS_ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "all"}
_JS_FUNCTION_VALUES = {"arrow_function", "function_expression", "function",
                       "generator_function"}
_JS_CLASSES = {"class_declaration", "abstract_class_declaration", "class"}
_JS_TEST_CALLS = {"it", "test"}
_JS_SUITE_CALLS = {"describe", "suite"}


def _js_string(node: Any, src: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return "".join(_text(c, src) for c in node.named_children if c.type in ("string_fragment", "escape_sequence"))
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        return _text(node, src)[1:-1]
    return None


def _js_call_target(fn: Any, src: bytes) -> tuple[str, str | None] | None:
    if fn.type == "identifier":
        return _text(fn, src), None
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop is None:
            return None
        if obj is not None and obj.type in ("identifier", "this"):
            return _text(prop, src), _text(obj, src)
        return _text(prop, src), ""
    return None


def _js_require(node: Any, src: bytes) -> str | None:
    """The module a `require("...")` call names, else None."""
    if node is None or node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None or fn.type != "identifier" or _text(fn, src) != "require":
        return None
    args = node.child_by_field_name("arguments")
    if args is None or not args.named_children:
        return None
    return _js_string(args.named_children[0], src)


def _js_heritage(node: Any, src: bytes) -> list[tuple[str, str | None, int]]:
    out: list[tuple[str, str | None, int]] = []
    heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
    if heritage is None:
        return out
    candidates: list[Any] = []
    for child in heritage.named_children:
        if child.type == "extends_clause":
            candidates.extend(child.named_children[:1])
        elif child.type in ("identifier", "member_expression"):
            candidates.append(child)
    for cand in candidates:
        target = _js_call_target(cand, src)
        if target is not None:
            out.append((target[0], target[1] or None, cand.start_point[0] + 1))
    return out


def _extract_js(facts: Facts, root: Any, src: bytes, clock: _Clock) -> None:
    # (node, prefix, owner id, class qual, in function, exported, suites)
    stack: list[tuple[Any, str, str, str | None, bool, bool, tuple[str, ...]]] = [
        (root, "", facts.path, None, False, False, ())
    ]

    def add_import(source: str, line: int) -> int:
        facts.imports.append({"source": source, "line": line})
        return len(facts.imports) - 1

    while stack:
        node, prefix, owner, cls, in_func, exported, suites = stack.pop()
        clock.tick()
        kind = node.type
        if kind == "export_statement":
            source = node.child_by_field_name("source")
            spec = _js_string(source, src) if source is not None else None
            if spec is not None:
                add_import(spec, node.start_point[0] + 1)
            for child in reversed(node.named_children):
                stack.append((child, prefix, owner, cls, in_func, not in_func, suites))
            continue
        if kind in ("function_declaration", "generator_function_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                qual = prefix + _text(name_node, src)
                symbol_id = facts.add_symbol(qual, "function", node, exported and not in_func)
                _record_signature(facts, symbol_id, node, src,
                                  ("type_parameters", "parameters", "return_type"))
                _record_doc(facts, symbol_id, _leading_comment(node, src))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, qual + ".", symbol_id, None, True, False, suites))
                continue
        if kind in _JS_CLASSES:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                qual = prefix + _text(name_node, src)
                symbol_id = facts.add_symbol(qual, "class", node, exported and not in_func)
                _record_doc(facts, symbol_id, _leading_comment(node, src))
                for name, qualifier, line in _js_heritage(node, src):
                    facts.inherits.append((symbol_id, name, qualifier, line))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, qual + ".", symbol_id, qual, in_func,
                                  exported and not in_func, suites))
                continue
        if kind == "method_definition" and cls is not None:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                qual = prefix + _text(name_node, src)
                symbol_id = facts.add_symbol(qual, "method", node, exported)
                _record_signature(facts, symbol_id, node, src,
                                  ("type_parameters", "parameters", "return_type"))
                _record_doc(facts, symbol_id, _leading_comment(node, src))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, qual + ".", symbol_id, None, True, False, suites))
                continue
        if kind == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            required = _js_require(value, src)
            if required is not None and name_node is not None:
                index = add_import(required, node.start_point[0] + 1)
                if name_node.type == "identifier":
                    facts.aliases[_text(name_node, src)] = index
                elif name_node.type == "object_pattern":
                    for prop in name_node.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            facts.bindings[_text(prop, src)] = (index, _text(prop, src))
                        elif prop.type == "pair_pattern":
                            key = prop.child_by_field_name("key")
                            val = prop.child_by_field_name("value")
                            if key is not None and val is not None and val.type == "identifier":
                                facts.bindings[_text(val, src)] = (index, _text(key, src))
                continue
            if (name_node is not None and name_node.type == "identifier" and value is not None
                    and value.type in _JS_FUNCTION_VALUES):
                qual = prefix + _text(name_node, src)
                symbol_id = facts.add_symbol(qual, "function", node, exported and not in_func)
                _record_signature(facts, symbol_id, value, src,
                                  ("type_parameters", "parameters", "parameter", "return_type"))
                _record_doc(facts, symbol_id, _leading_comment(node, src))
                body = value.child_by_field_name("body")
                if body is not None:
                    stack.append((body, qual + ".", symbol_id, None, True, False, suites))
                continue
        if kind == "import_statement":
            source = node.child_by_field_name("source")
            spec = _js_string(source, src) if source is not None else None
            if spec is not None:
                index = add_import(spec, node.start_point[0] + 1)
                clause = next((c for c in node.named_children if c.type == "import_clause"), None)
                if clause is not None:
                    for part in clause.named_children:
                        if part.type == "identifier":
                            facts.bindings[_text(part, src)] = (index, "default")
                        elif part.type == "namespace_import":
                            ident = next((c for c in part.named_children if c.type == "identifier"), None)
                            if ident is not None:
                                facts.aliases[_text(ident, src)] = index
                        elif part.type == "named_imports":
                            for spec_node in _children(part, "import_specifier"):
                                real = spec_node.child_by_field_name("name")
                                alias = spec_node.child_by_field_name("alias")
                                if real is not None:
                                    local = _text(alias if alias is not None else real, src)
                                    facts.bindings[local] = (index, _text(real, src))
            continue
        if kind == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and _text(left, src) == "module.exports" and right is not None \
                    and right.type == "object":
                for prop in right.named_children:
                    if prop.type == "shorthand_property_identifier":
                        facts.commonjs_exports.add(_text(prop, src))
                    elif prop.type == "pair":
                        val = prop.child_by_field_name("value")
                        if val is not None and val.type == "identifier":
                            facts.commonjs_exports.add(_text(val, src))
            elif left is not None and left.type == "member_expression":
                obj = left.child_by_field_name("object")
                prop = left.child_by_field_name("property")
                if obj is not None and prop is not None and _text(obj, src) in ("exports", "module.exports"):
                    facts.commonjs_exports.add(_text(prop, src))
        if kind == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            arg_list = list(args.named_children) if args is not None else []
            target = _js_call_target(fn, src) if fn is not None else None
            line = node.start_point[0] + 1
            required = _js_require(node, src)
            if required is not None:
                add_import(required, line)
                continue
            first = _js_string(arg_list[0], src) if arg_list else None
            # A test or a suite, by the vitest/jest convention.
            if facts.test_file and target is not None and target[1] is None and first is not None:
                if target[0] in _JS_SUITE_CALLS:
                    for child in reversed(arg_list[1:]):
                        stack.append((child, prefix, owner, cls, in_func, False, suites + (first,)))
                    continue
                if target[0] in _JS_TEST_CALLS:
                    qual = " > ".join(suites + (first,))
                    symbol_id = facts.add_symbol(qual, "test", node, False)
                    for child in reversed(arg_list[1:]):
                        stack.append((child, qual + ".", symbol_id, None, True, False, suites))
                    continue
            # An Express-shaped route: <identifier>.<method>("/path", ..., handler).
            if (fn is not None and fn.type == "member_expression" and target is not None
                    and target[1] and target[0] in _JS_ROUTE_METHODS
                    and first is not None and first.startswith("/") and len(arg_list) >= 2):
                method = target[0].upper()
                route_id = facts.add_symbol(f"{method} {first}", "route", node, True,
                                            method=method, route=first)
                record = {"id": route_id, "method": method, "path": first, "file": facts.path,
                          "handler": None, "start_line": line,
                          "end_line": node.end_point[0] + 1, "language": facts.language}
                facts.routes.append(record)
                handler = arg_list[-1]
                if handler.type in _JS_FUNCTION_VALUES:
                    body = handler.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, f"{method} {first}.", route_id, None, True, False, suites))
                else:
                    handler_target = _js_call_target(handler, src)
                    if handler_target is not None:
                        facts.handlers.append((route_id, handler_target[0],
                                               handler_target[1] or None, line))
                for child in reversed(arg_list[1:-1]):
                    stack.append((child, prefix, owner, cls, in_func, False, suites))
                continue
            if target is not None:
                facts.calls.append((owner, target[0], target[1] if target[1] else None, line))
        for child in reversed(node.named_children):
            stack.append((child, prefix, owner, cls, in_func,
                          exported and kind in ("lexical_declaration", "variable_declaration"),
                          suites))


# --- Go ---------------------------------------------------------------------

_GO_HANDLE = {"HandleFunc", "Handle"}
_GO_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
               "Get", "Post", "Put", "Delete", "Patch", "Head", "Options"}
_GO_PATTERN = re.compile(r"^([A-Z]+)\s+(\S.*)$")
_GO_TEST = re.compile(r"^(Test|Benchmark|Fuzz|Example)([^a-z]|$)")


def _go_string(node: Any, src: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "interpreted_string_literal":
        return "".join(_text(c, src) for c in node.named_children
                       if c.type in ("interpreted_string_literal_content", "escape_sequence"))
    if node.type == "raw_string_literal":
        return _text(node, src)[1:-1]
    return None


def _go_target(node: Any, src: bytes) -> tuple[str, str | None] | None:
    if node.type == "identifier":
        return _text(node, src), None
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        fld = node.child_by_field_name("field")
        if fld is None:
            return None
        qualifier = _text(operand, src) if operand is not None and operand.type == "identifier" else ""
        return _text(fld, src), qualifier
    return None


def _go_receiver_type(node: Any, src: bytes) -> str:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    for param in _children(receiver, "parameter_declaration"):
        typ = param.child_by_field_name("type")
        while typ is not None and typ.type in ("pointer_type", "generic_type"):
            inner = next((c for c in typ.named_children
                          if c.type in ("type_identifier", "generic_type", "pointer_type")), None)
            typ = inner
        if typ is not None and typ.type == "type_identifier":
            return _text(typ, src)
    return ""


def _go_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _extract_go(facts: Facts, root: Any, src: bytes, clock: _Clock) -> None:
    stack: list[tuple[Any, str]] = [(root, facts.path)]
    while stack:
        node, owner = stack.pop()
        clock.tick()
        kind = node.type
        if kind == "package_clause":
            ident = next((c for c in node.named_children if c.type == "package_identifier"), None)
            if ident is not None:
                facts.package = _text(ident, src)
            continue
        if kind == "import_spec":
            path_node = node.child_by_field_name("path")
            name_node = node.child_by_field_name("name")
            spec = _go_string(path_node, src)
            if spec is not None:
                facts.imports.append({"path": spec, "line": node.start_point[0] + 1,
                                      "alias": _text(name_node, src) if name_node is not None else None})
            continue
        if kind in ("function_declaration", "method_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node, src)
            if kind == "method_declaration":
                receiver = _go_receiver_type(node, src)
                qual = f"{receiver}.{name}" if receiver else name
                symbol_kind = "method"
            else:
                qual = name
                symbol_kind = "test" if facts.test_file and _GO_TEST.match(name) else "function"
            symbol_id = facts.add_symbol(qual, symbol_kind, node, _go_exported(name))
            if symbol_kind != "test":
                _record_signature(facts, symbol_id, node, src,
                                  ("type_parameters", "parameters", "result"))
            _record_doc(facts, symbol_id, _leading_comment(node, src))
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append((body, symbol_id))
            continue
        if kind == "type_spec":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, src)
                type_id = facts.add_symbol(name, "type", node, _go_exported(name))
                _record_doc(facts, type_id, _leading_comment(node.parent or node, src))
            continue
        if kind == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            arg_list = [a for a in args.named_children if a.type != "comment"] if args is not None else []
            target = _go_target(fn, src) if fn is not None else None
            line = node.start_point[0] + 1
            first = _go_string(arg_list[0], src) if arg_list else None
            method: str | None = None
            route: str | None = None
            if target is not None and target[1] is not None and first is not None and len(arg_list) >= 2:
                if target[0] in _GO_HANDLE:
                    matched = _GO_PATTERN.match(first)
                    method, route = (matched.group(1), matched.group(2)) if matched else ("ANY", first)
                elif target[0] in _GO_METHODS and first.startswith("/"):
                    method, route = target[0].upper(), first
            if method is not None and route is not None:
                route_id = facts.add_symbol(f"{method} {route}", "route", node, True,
                                            method=method, route=route)
                facts.routes.append({"id": route_id, "method": method, "path": route,
                                     "file": facts.path, "handler": None, "start_line": line,
                                     "end_line": node.end_point[0] + 1,
                                     "language": facts.language})
                handler = arg_list[-1]
                if handler.type == "func_literal":
                    body = handler.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, route_id))
                else:
                    handler_target = _go_target(handler, src)
                    if handler_target is not None:
                        facts.handlers.append((route_id, handler_target[0],
                                               handler_target[1] or None, line))
                continue
            if target is not None:
                facts.calls.append((owner, target[0], target[1] if target[1] else None, line))
        for child in reversed(node.named_children):
            stack.append((child, owner))


# --- HCL (Terraform) --------------------------------------------------------

_HCL_NON_RESOURCE_ROOTS = {"var", "local", "module", "data", "each", "count", "self",
                           "path", "terraform"}


def _hcl_labels(block: Any, src: bytes) -> tuple[str, list[str]]:
    ident = next((c for c in block.named_children if c.type == "identifier"), None)
    labels: list[str] = []
    for child in block.named_children:
        if child.type == "string_lit":
            labels.append("".join(_text(c, src) for c in child.named_children
                                  if c.type == "template_literal"))
        elif child.type == "identifier" and child is not ident:
            labels.append(_text(child, src))
    return (_text(ident, src) if ident is not None else ""), labels


def _hcl_address(expr: Any, src: bytes) -> str | None:
    """The Terraform address a `variable_expr` + `get_attr` chain names."""
    var = next((c for c in expr.named_children if c.type == "variable_expr"), None)
    if var is None:
        return None
    root_ident = next((c for c in var.named_children if c.type == "identifier"), None)
    if root_ident is None:
        return None
    rootname = _text(root_ident, src)
    attrs: list[str] = []
    seen_var = False
    for child in expr.named_children:
        if child is var:
            seen_var = True
            continue
        if not seen_var:
            continue
        if child.type != "get_attr":
            break
        ident = next((c for c in child.named_children if c.type == "identifier"), None)
        if ident is None:
            break
        attrs.append(_text(ident, src))
    if rootname in ("var", "local", "module"):
        return f"{rootname}.{attrs[0]}" if attrs else None
    if rootname == "data":
        return f"data.{attrs[0]}.{attrs[1]}" if len(attrs) >= 2 else None
    if rootname in _HCL_NON_RESOURCE_ROOTS:
        return None
    return f"{rootname}.{attrs[0]}" if attrs else None


def _hcl_collect_refs(facts: Facts, owner: str, node: Any, src: bytes, clock: _Clock) -> None:
    stack = [node]
    while stack:
        current = stack.pop()
        clock.tick()
        if current.type == "expression":
            address = _hcl_address(current, src)
            if address is not None:
                facts.references.append((owner, address, current.start_point[0] + 1))
        elif current.type == "variable_expr" and current.parent is not None:
            ident = next((c for c in current.named_children if c.type == "identifier"), None)
            after = current.next_named_sibling
            if ident is not None and _text(ident, src) == "module" and after is not None \
                    and after.type == "get_attr":
                name = next((c for c in after.named_children if c.type == "identifier"), None)
                if name is not None:
                    facts.module_refs.add(_text(name, src))
        for child in reversed(current.named_children):
            stack.append(child)


def _extract_hcl(facts: Facts, root: Any, src: bytes, clock: _Clock) -> None:
    body = next((c for c in root.named_children if c.type == "body"), None)
    if body is None:
        return
    for block in body.named_children:
        clock.tick()
        if block.type != "block":
            continue
        block_type, labels = _hcl_labels(block, src)
        inner = next((c for c in block.named_children if c.type == "body"), None)
        qual: str | None = None
        kind = block_type
        exported = False
        route = False
        if block_type == "resource" and len(labels) >= 2:
            qual, route = f"{labels[0]}.{labels[1]}", True
        elif block_type == "data" and len(labels) >= 2:
            qual = f"data.{labels[0]}.{labels[1]}"
        elif block_type == "module" and labels:
            qual, route = f"module.{labels[0]}", True
        elif block_type == "variable" and labels:
            qual, route, exported = f"var.{labels[0]}", True, True
        elif block_type == "output" and labels:
            qual, exported = f"output.{labels[0]}", True
        elif block_type == "run" and labels and facts.test_file:
            qual, kind = f"run.{labels[0]}", "test"
        elif block_type == "locals" and inner is not None:
            for attribute in _children(inner, "attribute"):
                ident = next((c for c in attribute.named_children if c.type == "identifier"), None)
                if ident is None:
                    continue
                local_id = facts.add_symbol(f"local.{_text(ident, src)}", "local", attribute, False)
                _hcl_collect_refs(facts, local_id, attribute, src, clock)
            continue
        if qual is None:
            if inner is not None:
                _hcl_collect_refs(facts, facts.path, inner, src, clock)
            continue
        symbol_id = facts.add_symbol(qual, kind, block, exported)
        if route:
            facts.routes.append({"id": symbol_id, "method": block_type, "path": qual,
                                 "file": facts.path, "handler": None,
                                 "start_line": block.start_point[0] + 1,
                                 "end_line": block.end_point[0] + 1,
                                 "language": facts.language})
        if inner is None:
            continue
        if block_type == "module":
            for attribute in _children(inner, "attribute"):
                ident = next((c for c in attribute.named_children if c.type == "identifier"), None)
                if ident is None or _text(ident, src) != "source":
                    continue
                literal = attribute.named_children[-1]
                text = _text(literal, src).strip()
                if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
                    facts.imports.append({"source": text[1:-1], "module": labels[0],
                                          "line": attribute.start_point[0] + 1})
        _hcl_collect_refs(facts, symbol_id, inner, src, clock)


_EXTRACTORS: dict[str, Callable[[Facts, Any, bytes, _Clock], None]] = {
    "python": _extract_python,
    "typescript": _extract_js,
    "tsx": _extract_js,
    "javascript": _extract_js,
    "go": _extract_go,
    "hcl": _extract_hcl,
}


# --- the file tree ----------------------------------------------------------


def _is_test_file(path: str, language: str | None) -> bool:
    name = posixpath.basename(path)
    if language == "python":
        return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")
    if language in ("typescript", "javascript"):
        return bool(re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name)) or "/__tests__/" in f"/{path}"
    if language == "go":
        return name.endswith("_test.go")
    if language == "hcl":
        return name.endswith(".tftest.hcl")
    return False


def _under(path: str, root: str) -> bool:
    root = root.strip("/")
    return bool(root) and root != "." and (path == root or path.startswith(root + "/"))


def _is_test_side(path: str, test_roots: Iterable[str] = ()) -> bool:
    """A test, or a helper, conftest or fixture beside tests (G4-04).

    By a directory named like a test root on the path, by `conftest.py`, or
    by a `test_layout[].root` the agent declared. Never a test map source.
    """
    parts = path.split("/")
    if any(part in TEST_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if parts[-1] in TEST_SIDE_FILENAMES:
        return True
    return any(_under(path, root) for root in test_roots)


def _test_layout_rows(index: Any) -> list[dict]:
    """The `test_layout` rows of a staged index (the agent's last reading)."""
    rows = index.get("test_layout") if isinstance(index, dict) else None
    return [row for row in rows or [] if isinstance(row, dict) and isinstance(row.get("root"), str)]


def _classify(path: str, head: bytes) -> tuple[str | None, str | None]:
    """(language, grammar key or None) for a path and its first bytes."""
    name = posixpath.basename(path)
    if name.endswith(".tftest.hcl"):
        return "hcl", "hcl"
    ext = posixpath.splitext(name)[1]
    if ext in SUPPORTED_EXTENSIONS:
        return SUPPORTED_EXTENSIONS[ext]
    if ext.lower() in UNSUPPORTED_EXTENSIONS:
        return UNSUPPORTED_EXTENSIONS[ext.lower()], None
    if name in UNSUPPORTED_FILENAMES:
        return UNSUPPORTED_FILENAMES[name], None
    if not ext and head.startswith(b"#!"):
        for pattern, language in _SHEBANG_LANGUAGES:
            if pattern.match(head):
                grammar = language if language in ("python", "javascript") else None
                return language, grammar
    return None, None


def _count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _git(root: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotepath=off", *args],
        capture_output=True, timeout=timeout, check=False,
    )


def _git_toplevel(root: Path) -> str | None:
    try:
        done = _git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace").strip()


def _list_paths(root: Path, in_git: bool) -> list[str]:
    if in_git:
        done = _git(root, "ls-files", "-z", "--cached")
        if done.returncode == 0:
            paths = [p.decode("utf-8", errors="surrogateescape")
                     for p in done.stdout.split(b"\x00") if p]
            return sorted(set(paths))
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for dirname in sorted(dirnames):
            if dirname in VCS_DIRS:
                continue
            full = os.path.join(dirpath, dirname)
            if os.path.islink(full):
                # Listed as a symlink below, never descended into.
                found.append(posixpath.normpath(posixpath.join(rel_dir.replace(os.sep, "/"), dirname)))
                continue
            keep.append(dirname)
        dirnames[:] = keep
        for filename in filenames:
            rel = posixpath.normpath(posixpath.join(rel_dir.replace(os.sep, "/"), filename))
            found.append(rel)
    return sorted(set(found))


# --- incremental runs (§3.4, §3.5) -------------------------------------------
#
# An incremental run is given the previous promoted index of an ancestor
# commit: its repo-index.json and its graph, reassembled from the shards by
# `swarm-repo-graph read`. The worker stages both (agent_worker/indexrun.py);
# this tool decides, from what it can measure itself, whether the run may be
# incremental, and runs full -- saying why -- whenever it may not.
#
# WHAT CHANGED is measured per file, by git's blob id of every tracked file
# (`git ls-files -s`), against the blob id the base graph recorded for it.
# Not `git diff <base>..HEAD`: the worker's checkout is one commit deep, so
# the base commit is not in it, while the head's tree -- every blob id -- is.
#
# WHAT IS REWRITTEN. The tree-sitter pass still parses every file: it takes
# seconds, and resolving a changed file's calls needs every other file's
# definitions. The LSP pass -- the minutes -- is asked only about the
# AFFECTED files: the changed ones, every file whose base edges point into a
# changed or deleted file (a renamed or deleted function's callers must be
# re-resolved, §3.5), and every file whose fresh edges point into a changed
# one (a new definition can capture an old call). Every other file keeps its
# base edges verbatim, `lsp` evidence included, and its symbols and file row
# come out byte-identical, so its shards are the same blobs (§2.5) and the
# writer stores nothing new for them. Deleted files are gone from every list.
#
# The agent's reading -- module purposes, entry points, territory, commands,
# notes -- is carried from the base index, minus what names a deleted file;
# each module says which commit its entry was read at (`commit_sha`): the
# base's for an untouched module, the head's for one the diff touched.

#: The base index's keys the agent wrote, carried forward for it to revise.
CARRIED_KEYS = ("entry_points", "test_layout", "always_tests", "territory", "commands", "notes")
#: A carried row that names a deleted file in any of these is dropped.
_ROW_PATH_KEYS = ("path", "file", "source", "root", "target")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Base:
    """The previous promoted index of an ancestor commit, as the worker staged it."""

    sha: str
    graph: Any
    index: Any = None


@dataclass(frozen=True)
class Changes:
    """The files that differ from the base, by blob id."""

    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)

    @property
    def changed(self) -> set[str]:
        return set(self.added) | set(self.modified)


def _budget_record(budget: Budget) -> dict:
    return {"max_file_bytes": budget.max_file_bytes,
            "max_total_bytes": budget.max_total_bytes,
            "max_files": budget.max_files,
            "file_timeout_seconds": budget.file_timeout_seconds}


def _blob_ids(root: Path) -> dict[str, str] | None:
    """Every tracked path's git blob id, from the index of the checkout."""
    try:
        done = _git(root, "ls-files", "-s", "-z")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    found: dict[str, str] = {}
    for entry in done.stdout.split(b"\x00"):
        meta, tab, path = entry.partition(b"\t")
        parts = meta.split()
        if not tab or len(parts) < 2:
            continue
        found[path.decode("utf-8", errors="surrogateescape")] = parts[1].decode("ascii", "replace")
    return found


def _path_of(node_id: str) -> str:
    """The file a symbol id (`<path>#<name>`) or a file id names."""
    return node_id.split("#", 1)[0]


def incremental_changes(base: Base, head_blobs: dict[str, str] | None, budget: Budget,
                        commit_sha: str | None) -> Changes | str:
    """The diff an incremental run rewrites, or why this run has to be full."""
    graph, index = base.graph, base.index
    if not isinstance(base.sha, str) or not _SHA_RE.match(base.sha):
        return "the base is not a 40-hex commit sha"
    if not isinstance(graph, dict) or graph.get("schema") != GRAPH_SCHEMA:
        return f"no base graph ({GRAPH_SCHEMA}) was staged"
    if graph.get("commit_sha") != base.sha:
        return "the base graph describes another commit than the base"
    if not isinstance(index, dict) or index.get("schema") != SCHEMA:
        return f"no base index ({SCHEMA}) was staged"
    if index.get("commit_sha") != base.sha:
        return "the base index describes another commit than the base"
    if commit_sha is None or head_blobs is None:
        return "the checkout is not a git repository, so no file can be compared"
    if commit_sha == base.sha:
        return "the base is the commit being indexed"
    extractor = graph.get("extractor") if isinstance(graph.get("extractor"), dict) else {}
    if extractor.get("version") != EXTRACTOR_VERSION:
        return (f"the base was extracted by version {extractor.get('version')!r} of "
                f"{EXTRACTOR_NAME}, this is version {EXTRACTOR_VERSION!r}")
    if extractor.get("budget") != _budget_record(budget):
        return "the base was extracted under another size budget"
    if extractor.get("files_not_listed") or graph.get("truncated"):
        return ("the base graph was truncated "
                f"({', '.join(graph.get('truncated') or ['files'])}), so it cannot be carried")
    rows = graph.get("files")
    if not isinstance(rows, list):
        return "the base graph lists no files"
    base_blobs: dict[str, str] = {}
    for row in rows:
        blob = row.get("blob") if isinstance(row, dict) else None
        if not isinstance(blob, str) or not blob:
            return ("the base graph records no per-file blob id (it was extracted before "
                    "incremental runs existed)")
        base_blobs[str(row.get("path"))] = blob
    changes = Changes(
        added=tuple(sorted(set(head_blobs) - set(base_blobs))),
        modified=tuple(sorted(p for p in set(head_blobs) & set(base_blobs)
                              if head_blobs[p] != base_blobs[p])),
        deleted=tuple(sorted(set(base_blobs) - set(head_blobs))),
    )
    if changes.count >= MAX_INCREMENTAL_CHANGES:
        return (f"{changes.count} files changed since the base, at or over the "
                f"{MAX_INCREMENTAL_CHANGES} an incremental run takes")
    config = sorted(p for p in (*changes.added, *changes.modified, *changes.deleted)
                    if is_config_path(p))
    if config:
        more = f" and {len(config) - 1} more" if len(config) > 1 else ""
        return (f"a build, test or language-server configuration changed ({config[0]}{more}), "
                "which changes what every entry means")
    return changes


def _affected(changes: Changes, base_graph: dict, fresh: "_Edges") -> set[str]:
    """The files whose edges are re-resolved: changed, and what points into the change."""
    changed = changes.changed
    gone = set(changes.modified) | set(changes.deleted)
    affected = set(changed)
    for edge in base_graph.get("call_edges") or []:
        if isinstance(edge, dict) and _path_of(str(edge.get("to"))) in gone:
            affected.add(_path_of(str(edge.get("from"))))
    for frm, to, _kind in fresh.edges:
        if _path_of(to) in changed:
            affected.add(_path_of(frm))
    return affected - set(changes.deleted)


def _carry_edges(fresh: "_Edges", base_graph: dict, affected: set[str],
                 deleted: set[str]) -> "_Edges":
    """The affected files' fresh edges, and every other file's base edges verbatim."""
    merged = _Edges()
    for key, edge in fresh.edges.items():
        if _path_of(key[0]) in affected:
            merged.edges[key] = edge
    for edge in base_graph.get("call_edges") or []:
        if not isinstance(edge, dict):
            continue
        frm, to, kind = str(edge.get("from")), str(edge.get("to")), str(edge.get("kind"))
        source = _path_of(frm)
        if source in affected or source in deleted or _path_of(to) in deleted:
            continue
        merged.edges.setdefault((frm, to, kind), dict(edge))
    return merged


def _module_for(path: str, module_paths: set[str]) -> str | None:
    """The module a file counts under: its directory, or the deepest module above it."""
    directory = posixpath.dirname(path) or "."
    if directory in module_paths:
        return directory
    above = [m for m in module_paths if m != "." and directory.startswith(m + "/")]
    return max(above, key=len) if above else None


def carry_modules(modules: list[dict], base_index: dict, changes: Changes,
                  base_sha: str, commit_sha: str) -> list[dict]:
    """Each module with the base's purpose, and the commit its entry was read at."""
    before = {m["path"]: m for m in base_index.get("modules") or []
              if isinstance(m, dict) and isinstance(m.get("path"), str)}
    now_paths = {m["path"] for m in modules}
    touched: set[str] = set()
    for path in (*changes.added, *changes.modified, *changes.deleted):
        for found in (_module_for(path, now_paths), _module_for(path, set(before))):
            if found is not None:
                touched.add(found)
    carried = []
    for module in modules:
        entry = dict(module)
        old = before.get(module["path"])
        if old is not None and isinstance(old.get("purpose"), str) and old["purpose"]:
            entry["purpose"] = old["purpose"]
        if old is None or module["path"] in touched:
            entry["commit_sha"] = commit_sha
        else:
            read_at = old.get("commit_sha")
            entry["commit_sha"] = read_at if isinstance(read_at, str) and _SHA_RE.match(
                read_at) else base_sha
        carried.append(entry)
    return carried


def carried_reading(base_index: dict, deleted: Iterable[str]) -> dict[str, list[dict]]:
    """The base index's agent-written lists, without the rows that name a deleted file."""
    gone = set(deleted)
    reading: dict[str, list[dict]] = {}
    for key in CARRIED_KEYS:
        rows = base_index.get(key)
        if not isinstance(rows, list):
            continue
        reading[key] = [row for row in rows if isinstance(row, dict) and not any(
            row.get(name) in gone for name in _ROW_PATH_KEYS)]
    return reading


# --- import resolution ------------------------------------------------------


class _Resolver:
    """Cross-file lookups built once from every parsed file."""

    def __init__(self, files: list[dict], facts: dict[str, Facts]) -> None:
        self.facts = facts
        self.existing = {f["path"] for f in files}
        self.by_dir: dict[str, list[str]] = {}
        for f in files:
            self.by_dir.setdefault(posixpath.dirname(f["path"]), []).append(f["path"])
        # Python: every dotted suffix of every module path -> files.
        self.py_modules: dict[str, list[str]] = {}
        for f in files:
            if f["language"] != "python" or not f["path"].endswith((".py", ".pyi")):
                continue
            parts = f["path"].rsplit(".", 1)[0].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            for start in range(len(parts)):
                dotted = ".".join(parts[start:])
                self.py_modules.setdefault(dotted, []).append(f["path"])
        self.py_full = {}
        for dotted, paths in self.py_modules.items():
            for path in paths:
                parts = path.rsplit(".", 1)[0].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if ".".join(parts) == dotted:
                    self.py_full.setdefault(dotted, []).append(path)
        # Go: go.mod module paths, by directory.
        self.go_modules: list[tuple[str, str]] = []
        # Definitions by file and simple name.
        self.defs: dict[str, dict[str, list[str]]] = {}
        self.kinds: dict[str, str] = {}
        self.hcl_addresses: dict[str, dict[str, list[str]]] = {}
        # Python methods by simple name, repository-wide: the name-unique
        # fallback for a test's `obj.method()` (`unique_method`).
        self.py_methods: dict[str, list[str]] = {}
        for path, fact in facts.items():
            names: dict[str, list[str]] = {}
            for symbol in fact.symbols:
                self.kinds[symbol["id"]] = symbol["kind"]
                if symbol["kind"] == "method" and fact.language == "python":
                    simple = symbol["id"].split("#", 1)[1].split("@", 1)[0].rsplit(".", 1)[-1]
                    self.py_methods.setdefault(simple, []).append(symbol["id"])
                if symbol["kind"] in ("route", "test"):
                    continue
                qual = symbol["id"].split("#", 1)[1].split("@", 1)[0]
                if fact.language == "hcl":
                    self.hcl_addresses.setdefault(posixpath.dirname(path), {}).setdefault(
                        qual, []).append(symbol["id"])
                    continue
                names.setdefault(qual.rsplit(".", 1)[-1], []).append(symbol["id"])
            self.defs[path] = names

    def unique_method(self, name: str) -> str | None:
        """The one Python method named `name` in the repository, or None (G4-05 d)."""
        if name.startswith("__") and name.endswith("__"):
            return None
        found = self.py_methods.get(name, [])
        return found[0] if len(found) == 1 else None

    def load_go_mods(self, root: Path) -> None:
        for path in sorted(self.existing):
            if posixpath.basename(path) != "go.mod":
                continue
            full = root / path
            if full.is_symlink() or not full.is_file():
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            matched = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
            if matched:
                self.go_modules.append((posixpath.dirname(path), matched.group(1).strip('"')))

    # Each resolver returns the in-repository files an import names.

    def python(self, path: str, imp: dict) -> list[str]:
        module, level = imp["module"], imp["level"]
        if level:
            base = posixpath.dirname(path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            stem = posixpath.join(base, *module.split(".")) if module else base
            found = self._py_path(stem)
            extra = [self._py_path(posixpath.join(stem, name)) for name in imp["names"]]
            return sorted({p for p in [*found, *(p for e in extra for p in e)]})
        found = self._py_dotted(module, path)
        for name in imp["names"]:
            found = found + self._py_dotted(f"{module}.{name}", path)
        return sorted(set(found))

    def python_member_module(self, path: str, imp: dict, member: str) -> list[str]:
        """The module file `from <imp> import <member>` names, when the member
        is a module rather than a name defined in one: `from swarm_api import
        issueruns`. Empty when it is not a module of this repository."""
        module, level = imp["module"], imp["level"]
        if level:
            base = posixpath.dirname(path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            stem = posixpath.join(base, *module.split(".")) if module else base
            return self._py_path(posixpath.join(stem, member))
        return self._py_dotted(f"{module}.{member}", path) if module else []

    def python_dotted_module(self, fact: "Facts", dotted: str) -> list[str] | None:
        """The module file a dotted chain `a.b` names through an `import a.b`
        (or `import a.b as ab`, chain `ab`) of `fact`'s, or None when the
        chain does not start at an imported module."""
        head, _dot, rest = dotted.partition(".")
        index = fact.aliases.get(head)
        if index is None:
            return None
        imported = fact.imports[index]["module"]
        real = head if imported.split(".")[0] == head else imported
        return self._py_dotted(f"{real}.{rest}" if rest else real, fact.path)

    def _py_path(self, stem: str) -> list[str]:
        stem = posixpath.normpath(stem)
        for candidate in (stem + ".py", stem + ".pyi", posixpath.join(stem, "__init__.py")):
            if candidate in self.existing:
                return [candidate]
        return []

    def _py_dotted(self, dotted: str, importer: str) -> list[str]:
        if not dotted:
            return []
        top = dotted.split(".")[0]
        # A standard-library name only matches a file at exactly that path
        # from the root: `import json` is not `tools/json.py`.
        pool = self.py_full if top in sys.stdlib_module_names else self.py_modules
        candidates = pool.get(dotted, [])
        if len(candidates) <= 1:
            return list(candidates)
        importer_parts = importer.split("/")[:-1]

        def shared(candidate: str) -> int:
            n = 0
            for a, b in zip(importer_parts, candidate.split("/")[:-1]):
                if a != b:
                    break
                n += 1
            return n

        best = max(shared(c) for c in candidates)
        return sorted(c for c in candidates if shared(c) == best)

    def js(self, path: str, imp: dict) -> list[str]:
        spec = imp["source"]
        if not spec.startswith("."):
            return []
        base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        candidates = [base]
        stem = base
        if base.endswith((".js", ".jsx", ".mjs", ".cjs")):
            stem = base.rsplit(".", 1)[0]
            candidates += [stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts"]
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"):
            candidates.append(base + ext)
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(posixpath.join(base, "index" + ext))
        for candidate in candidates:
            if candidate in self.existing and SUPPORTED_EXTENSIONS.get(
                    posixpath.splitext(candidate)[1], ("",))[0] in ("typescript", "javascript"):
                return [candidate]
        return []

    def go(self, path: str, imp: dict) -> list[str]:
        spec = imp["path"]
        for mod_dir, module in self.go_modules:
            if spec == module or spec.startswith(module + "/"):
                rel = spec[len(module):].lstrip("/")
                target_dir = posixpath.normpath(posixpath.join(mod_dir, rel)) if (mod_dir or rel) else ""
                if target_dir == ".":
                    target_dir = ""
                return sorted(p for p in self.by_dir.get(target_dir, [])
                              if p.endswith(".go") and not p.endswith("_test.go"))
        return []

    def hcl(self, path: str, imp: dict) -> list[str]:
        spec = imp["source"]
        if not spec.startswith(("./", "../")):
            return []
        target_dir = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        return sorted(p for p in self.by_dir.get(target_dir, []) if p.endswith(".tf"))

    def resolve(self, path: str, imp: dict) -> list[str]:
        language = self.facts[path].language
        if language == "python":
            targets = self.python(path, imp)
        elif language in ("typescript", "javascript"):
            targets = self.js(path, imp)
        elif language == "go":
            targets = self.go(path, imp)
        else:
            targets = self.hcl(path, imp)
        return [t for t in targets if t != path]


# --- edges ------------------------------------------------------------------


class _Edges:
    """Edges keyed by (from, to, kind); a repeat keeps the strongest evidence."""

    def __init__(self) -> None:
        self.edges: dict[tuple[str, str, str], dict] = {}

    def add(self, frm: str, to: str, kind: str, evidence: str, confidence: float,
            path: str, line: int) -> None:
        key = (frm, to, kind)
        confidence = round(confidence, 3)
        current = self.edges.get(key)
        if current is None:
            self.edges[key] = {"from": frm, "to": to, "kind": kind, "evidence": evidence,
                               "confidence": confidence, "also_evidence": [],
                               "path": path, "line": line, "sites": 1}
            return
        if current["evidence"] == evidence:
            current["sites"] += 1
            current["confidence"] = max(current["confidence"], confidence)
            current["line"] = min(current["line"], line)
            return
        _merge_evidence(current, evidence, confidence)


def _merge_evidence(current: dict, evidence: str, confidence: float) -> None:
    others = set(current["also_evidence"]) | {current["evidence"], evidence}
    if (confidence, EVIDENCE_RANK[evidence]) > (current["confidence"], EVIDENCE_RANK[current["evidence"]]):
        current["evidence"], current["confidence"] = evidence, confidence
    others.discard(current["evidence"])
    current["also_evidence"] = sorted(others)


def _resolve_name(resolver: _Resolver, fact: Facts, caller: str, name: str,
                  qualifier: str | None, import_targets: list[list[str]]) -> list[str]:
    """The definitions a call or reference by `name` may mean."""
    path = fact.path

    def defs_in(paths: Iterable[str], wanted: str, kinds: set[str] | None = None) -> list[str]:
        out: list[str] = []
        for p in sorted(set(paths)):
            for symbol_id in resolver.defs.get(p, {}).get(wanted, []):
                if kinds is None or resolver.kinds.get(symbol_id) in kinds:
                    out.append(symbol_id)
        return out

    def package_files() -> list[str]:
        same_dir = resolver.by_dir.get(posixpath.dirname(path), [])
        return [p for p in same_dir
                if p.endswith(".go") and (fact.test_file or not p.endswith("_test.go"))
                and p in resolver.facts and resolver.facts[p].package == fact.package]

    if fact.language == "go":
        if qualifier:
            for index, imp in enumerate(fact.imports):
                local = imp.get("alias") or _go_package_name(resolver, import_targets[index], imp["path"])
                if local == qualifier:
                    return defs_in(import_targets[index], name)
            return defs_in(package_files(), name, {"method"})
        return defs_in(package_files(), name)

    if qualifier in ("self", "this", "cls"):
        own_class = caller.split("#", 1)[1].rsplit(".", 1)[0] if "#" in caller else ""
        mine = [s for s in defs_in([path], name, {"method"})
                if s.split("#", 1)[1].startswith(own_class + ".")]
        return mine or defs_in([path], name, {"method"})
    if qualifier is not None and qualifier in fact.aliases:
        index = fact.aliases[qualifier]
        return defs_in(import_targets[index], name)
    if qualifier and qualifier in fact.bindings:
        # Version 3 (lane KG2, knowledge-graph.md §2.2 item 6): a call through
        # an imported name. `from swarm_api import issueruns` then
        # `issueruns.planner_prompt(...)` is a call into the module the name
        # is; `from m import Store` then `Store.open()` is a method of that
        # class. Before, both fell through to "a method of this file" and
        # resolved to nothing, which is why planner_prompt's six tests could
        # not be selected by symbol.
        index, real = fact.bindings[qualifier]
        if fact.language == "python":
            module = resolver.python_member_module(path, fact.imports[index], real)
            if module:
                return defs_in(module, name)
        if real != "default":
            owned = [s for s in defs_in(import_targets[index], name, {"method"})
                     if s.split("#", 1)[1].split("@", 1)[0].startswith(real + ".")]
            if owned:
                return owned
    if qualifier is None and name in fact.bindings:
        index, real = fact.bindings[name]
        targets = import_targets[index]
        # A default import is matched by its local name, the usual case of
        # `import App from "./App"`; a renamed default stays unresolved.
        return defs_in(targets, name if real == "default" else real)
    if qualifier is not None:
        return defs_in([path], name, {"method"})
    local = defs_in([path], name)
    if local:
        return local
    wild: list[str] = []
    for index in fact.wildcards:
        wild.extend(import_targets[index])
    return defs_in(wild, name)


def _go_package_name(resolver: _Resolver, targets: list[str], spec: str) -> str:
    for target in targets:
        fact = resolver.facts.get(target)
        if fact is not None and fact.package:
            return fact.package
    return spec.rstrip("/").rsplit("/", 1)[-1]


def _build_edges(resolver: _Resolver, facts: dict[str, Facts],
                 test_side: frozenset[str] | set[str] = frozenset()
                 ) -> tuple[_Edges, dict[str, set[str]]]:
    edges = _Edges()
    imports_of: dict[str, set[str]] = {}
    for path in sorted(facts):
        fact = facts[path]
        targets = [resolver.resolve(path, imp) for imp in fact.imports]
        imports_of[path] = {t for group in targets for t in group}
        for imp, group in zip(fact.imports, targets):
            for target in group:
                edges.add(path, target, "import", "import", IMPORT_CONFIDENCE, path, imp["line"])
        for kind, items in (("call", fact.calls), ("inherit", fact.inherits),
                            ("route_handler", fact.handlers)):
            for caller, name, qualifier, line in items:
                dotted = fact.dotted_calls.get((caller, name, line)) if kind == "call" else None
                module = resolver.python_dotted_module(fact, dotted) if dotted else None
                if name.startswith("\x00"):
                    found = [name[1:]]
                elif module is not None:
                    # `import a.b` then `a.b.f()`: f in a/b.py and nowhere else;
                    # a chain into a module outside the repository (`os.path`)
                    # is no call of ours, not one to guess a target for.
                    if not module:
                        continue
                    found = sorted({s for p in module if p != path
                                    for s in resolver.defs.get(p, {}).get(name, [])})
                else:
                    found = _resolve_name(resolver, fact, caller, name, qualifier, targets)
                if not found and kind == "call" and path in test_side \
                        and fact.language == "python" \
                        and (qualifier is not None or (caller, name, line) in fact.attribute_calls):
                    # A test's `obj.method()` on a fixture-built instance: no
                    # type to resolve it by, but a name defined on one class
                    # only (G4-05 d; `Scheduler._admit_one`).
                    unique = resolver.unique_method(name)
                    if unique is not None and _path_of(unique) != path:
                        edges.add(caller, unique, kind, "ast", NAME_UNIQUE_CONFIDENCE, path, line)
                    continue
                if not found:
                    continue
                confidence = AST_UNIQUE if len(found) == 1 else AST_AMBIGUOUS
                for target in found:
                    edges.add(caller, target, kind, "ast", confidence, path, line)
        if fact.language == "hcl":
            addresses = resolver.hcl_addresses.get(posixpath.dirname(path), {})
            for caller, address, line in fact.references:
                found = addresses.get(address, [])
                if not found or caller in found:
                    continue
                confidence = AST_UNIQUE if len(found) == 1 else AST_AMBIGUOUS
                for target in found:
                    edges.add(caller, target, "reference", "ast", confidence, path, line)
    return edges, imports_of


# --- tests ------------------------------------------------------------------

# A string literal, and a run of them joined by `/` (pathlib's
# `REPO / "scripts" / "lib" / "common.sh"`), as one candidate path.
_LITERAL = r"""(?:"([^"\n\\]{1,300})"|'([^'\n\\]{1,300})'|`([^`\n\\$]{1,300})`)"""
_JOINED_LITERALS = re.compile(_LITERAL + r"(?:\s*/\s*" + _LITERAL + r")*")
_ONE_LITERAL = re.compile(_LITERAL)
_PATH_CHARS = re.compile(r"^[A-Za-z0-9_.@+\-/]+$")


def _path_literals(data: bytes) -> list[str]:
    """The string literals in a test file that could name a repository path."""
    text = data.decode("utf-8", errors="replace")
    found: list[str] = []
    seen: set[str] = set()
    for match in _JOINED_LITERALS.finditer(text):
        pieces = ["".join(groups) for groups in _ONE_LITERAL.findall(match.group(0))]
        candidates = ["/".join(p.strip("/") for p in pieces)] if len(pieces) > 1 else []
        candidates += pieces
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate in seen or not _PATH_CHARS.match(candidate):
                continue
            if "/" not in candidate and "." not in candidate.lstrip("."):
                continue  # a bare word, not a path
            seen.add(candidate)
            found.append(candidate)
    return found


class _Paths:
    """Every listed path and every directory above one, to resolve a literal against."""

    def __init__(self, files: list[dict], test_side: set[str]) -> None:
        self.files = {f["path"] for f in files}
        self.test_side = test_side
        # Only a directory holding a file that is not test-side is a source
        # directory: `tests/fixtures/` named by a test is not coverage.
        self.dirs: set[str] = set()
        for path in self.files - test_side:
            parent = posixpath.dirname(path)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)

    def resolve(self, test: str, literal: str) -> str | None:
        """The file, or `<dir>/**`, a literal in `test` names; None when it names none.

        Tried from the repository root first, then from the test's own
        directory (a `.tftest.hcl`'s `source = "../../terraform/infra"`).
        """
        bases = [""] if literal.startswith("/") else ["", posixpath.dirname(test)]
        for base in bases:
            path = posixpath.normpath(posixpath.join(base, literal.lstrip("/")))
            if path in ("", ".") or path.startswith("../") or path == "..":
                continue
            if path in self.files:
                return None if path in self.test_side else path
            if path in self.dirs:
                return f"{path}/**"
        return None


def _path_ref_edges(path_literals: dict[str, list[str]],
                    paths: _Paths) -> list[tuple[str, str]]:
    """(source file or `<dir>/**`, test): what each test names by path (G4-05 b, c)."""
    pairs: list[tuple[str, str]] = []
    for test in sorted(path_literals):
        targets: list[str] = []
        for literal in path_literals[test]:
            target = paths.resolve(test, literal)
            if target is None or target in targets:
                continue
            targets.append(target)
            if len(targets) >= PATH_REF_MAX_PER_TEST:
                break
        pairs.extend((target, test) for target in targets)
    return pairs


def _hcl_module_edges(facts: dict[str, Facts], resolver: "_Resolver",
                      path_refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(`<module dir>/**`, test) for a `.tftest.hcl` naming `module.<name>` of a root it runs.

    The root is a directory the test names by path (`source = "../../terraform/infra"`,
    G4-05 c); the module is that root's `module "<name>" { source = "./..." }`.
    """
    roots_of: dict[str, list[str]] = {}
    for target, test in path_refs:
        if target.endswith("/**"):
            roots_of.setdefault(test, []).append(target[:-3])
    pairs: list[tuple[str, str]] = []
    for test in sorted(roots_of):
        fact = facts.get(test)
        if fact is None or fact.language != "hcl":
            continue
        names = set(fact.module_refs)
        found: set[str] = set()
        for root in roots_of[test]:
            for path in resolver.by_dir.get(root, []):
                block_fact = facts.get(path)
                if block_fact is None or block_fact.language != "hcl":
                    continue
                for imp in block_fact.imports:
                    if imp.get("module") not in names:
                        continue
                    spec = imp["source"]
                    if not spec.startswith(("./", "../")):
                        continue
                    directory = posixpath.normpath(posixpath.join(root, spec))
                    if directory in resolver.by_dir and not directory.startswith(".."):
                        found.add(f"{directory}/**")
        pairs.extend((target, test) for target in sorted(found))
    return pairs


def _declared_edges(test_layout: list[dict], files: list[dict],
                    test_side: set[str]) -> list[tuple[str, str]]:
    """(covers glob, test) for every test under a `test_layout[].root` (G4-05 a).

    A glob that matches no listed source file is stale and gives no edge.
    """
    sources = [f["path"] for f in files if f["path"] not in test_side]
    tests = sorted(f["path"] for f in files if f["test"])
    pairs: list[tuple[str, str]] = []
    for row in test_layout:
        root = row["root"]
        covers = [c for c in row.get("covers") or [] if isinstance(c, str) and c.strip()]
        under = [t for t in tests if _under(t, root)]
        if not under:
            continue
        for glob in covers:
            if not any(fnmatch.fnmatchcase(p, glob) for p in sources):
                continue
            pairs.extend((glob, test) for test in under)
    return pairs


def _naming_edges(files: list[dict], test_side: set[str] | frozenset[str] = frozenset()
                  ) -> list[tuple[str, str]]:
    """(source, test) pairs by the naming conventions of §2.5."""
    sources = [f for f in files if f["language"] in GRAMMAR_PACKAGES and not f["test"]
               and f["path"] not in test_side]
    by_stem: dict[tuple[str, str], list[str]] = {}
    for f in sources:
        name = posixpath.basename(f["path"])
        stem = name.split(".", 1)[0] if f["language"] in ("typescript", "javascript") \
            else posixpath.splitext(name)[0]
        family = "js" if f["language"] in ("typescript", "javascript") else f["language"]
        by_stem.setdefault((family, stem), []).append(f["path"])
    pairs: list[tuple[str, str]] = []
    for f in files:
        if not f["test"]:
            continue
        name = posixpath.basename(f["path"])
        language = f["language"]
        test_dir = posixpath.dirname(f["path"])
        if language == "python":
            base = posixpath.splitext(name)[0]
            stem = base[5:] if base.startswith("test_") else base[:-5]
            candidates = by_stem.get(("python", stem), [])
        elif language in ("typescript", "javascript"):
            stem = re.sub(r"\.(test|spec)\.[cm]?[jt]sx?$", "", name)
            candidates = by_stem.get(("js", stem), [])
            near = [c for c in candidates if posixpath.dirname(c) == test_dir]
            candidates = near or candidates
        elif language == "go":
            stem = name[:-len("_test.go")]
            candidates = [c for c in by_stem.get(("go", stem), []) if posixpath.dirname(c) == test_dir]
        else:
            continue
        if not stem or not candidates:
            continue
        test_parts = test_dir.split("/")

        def overlap(candidate: str) -> int:
            return len(set(posixpath.dirname(candidate).split("/")) & set(test_parts))

        best = max(overlap(c) for c in candidates)
        for candidate in sorted(c for c in candidates if overlap(c) == best):
            pairs.append((candidate, f["path"]))
    return pairs


def _symbol_test_map(symbols: list[dict], edges: _Edges, test_files: set[str]) -> list[dict]:
    forward: dict[str, list[tuple[str, float]]] = {}
    for (frm, to, kind), edge in edges.edges.items():
        if kind in ("call", "reference", "route_handler"):
            forward.setdefault(frm, []).append((to, edge["confidence"]))
    for targets in forward.values():
        targets.sort()
    path_of = {s["id"]: s["path"] for s in symbols}
    out: list[dict] = []
    for test in sorted(s["id"] for s in symbols if s["kind"] == "test"):
        best: dict[str, tuple[int, float]] = {test: (0, 1.0)}
        frontier = {test: 1.0}
        for depth in range(1, TEST_WALK_MAX_DEPTH + 1):
            reached: dict[str, float] = {}
            for node in sorted(frontier):
                for target, confidence in forward.get(node, []):
                    product = frontier[node] * confidence
                    if product < TEST_WALK_MIN_CONFIDENCE - 1e-9:
                        continue
                    if target in best and best[target][0] < depth:
                        continue
                    if product > reached.get(target, 0.0):
                        reached[target] = product
            for target, product in reached.items():
                best[target] = (depth, product)
            frontier = reached
            if not frontier:
                break
        for symbol, (depth, confidence) in best.items():
            if symbol == test or path_of.get(symbol) is None or path_of[symbol] in test_files:
                continue
            out.append({"symbol": symbol, "test": test, "depth": depth,
                        "confidence": round(confidence, 3)})
    return out


# --- version 3: communities, search, fingerprints, flows (lane KG2) ----------
#
# docs/design/knowledge-graph.md §3 option C, chosen as D (owner decision
# 2026-10-08): clean-room implementations of published techniques, written
# here from the papers, no third-party code. None of these reads an edge with
# `declared`, `path-ref`, `naming` or `co-change` evidence: the call graph
# holds `ast`, `lsp` and `import` only, so nothing below is built on the index
# agent's judgement, and a gate that reads it reads facts (§7.8).

#: Louvain's resolution (Blondel, Guillaume, Lambiotte and Lefebvre, "Fast
#: unfolding of communities in large networks", 2008). 1.0 is plain
#: modularity, which on this repository (2026-10-08, 490 application files
#: with a cross-file edge) drew 27 communities, the two largest of 104 and 103
#: files: all of swarm-api and all of the console, too coarse to be anyone's
#: territory. 2.0 drew 35, the largest 61 and most 10-50 files.
COMMUNITY_RESOLUTION = 2.0
#: Local-moving passes per level. Each pass visits every node; a level that
#: has not settled by then is aggregated anyway, which bounds the run on an
#: adversarial graph without changing the result on this one (it settles in
#: under ten).
COMMUNITY_MAX_PASSES = 32
#: The edges a community is drawn from: what the code says, never judgement.
COMMUNITY_EVIDENCE = frozenset({"ast", "lsp", "import"})
COMMUNITY_KINDS = frozenset({"call", "inherit", "route_handler", "reference", "import"})
#: Search: a symbol's own name counts this many times over the rest of its
#: text (its class, file, signature and docstring), a simple BM25F field
#: weight: a query naming the symbol should find it before its callers' docs.
TERM_NAME_WEIGHT = 3
#: A flow follows call edges at or above this confidence: unique `ast` (0.6),
#: `lsp`, and not the ambiguous 0.3 guesses, which fan a flow out into every
#: same-named function in the repository.
FLOW_MIN_CONFIDENCE = 0.4
FLOW_MAX_DEPTH = 6
FLOW_MAX_STEPS = 32
#: The signature fingerprint's length in hex: 64 bits, so two signatures of
#: one symbol collide about never, and 30k of them stay small.
FINGERPRINT_HEX = 16


def _file_graph(edges: Iterable[dict], test_side: set[str] | frozenset[str]
                ) -> dict[str, dict[str, float]]:
    """Application files and the summed confidence of the edges between each pair.

    Undirected: a call either way ties two files equally. Same-file edges,
    test-side files and edges of other evidence are left out (a test reaches
    everything it covers and would glue unrelated modules together; the test
    map is how a test is placed)."""
    graph: dict[str, dict[str, float]] = {}
    for edge in edges:
        if edge["kind"] not in COMMUNITY_KINDS or edge["evidence"] not in COMMUNITY_EVIDENCE:
            continue
        a, b = _path_of(edge["from"]), _path_of(edge["to"])
        if a == b or a in test_side or b in test_side:
            continue
        weight = float(edge["confidence"])
        row_a, row_b = graph.setdefault(a, {}), graph.setdefault(b, {})
        row_a[b] = row_a.get(b, 0.0) + weight
        row_b[a] = row_b.get(a, 0.0) + weight
    return graph


def _local_moving(adjacency: list[dict[int, float]], start: list[int],
                  resolution: float) -> list[int]:
    """Louvain's first phase: move each node to the neighbouring community with
    the best modularity gain until no move improves it. Nodes are visited in
    index order and ties keep the current community, then the lowest label:
    the same graph and start always give the same partition."""
    degree = [sum(row.values()) for row in adjacency]
    total = sum(degree)
    community = list(start)
    if total <= 0:
        return community
    weight_of: dict[int, float] = {}
    for node, label in enumerate(community):
        weight_of[label] = weight_of.get(label, 0.0) + degree[node]
    for _pass in range(COMMUNITY_MAX_PASSES):
        moved = False
        for node, row in enumerate(adjacency):
            current = community[node]
            links: dict[int, float] = {}
            for other, weight in row.items():
                if other != node:
                    links[community[other]] = links.get(community[other], 0.0) + weight
            weight_of[current] -= degree[node]
            scale = resolution * degree[node] / total
            best = current
            best_gain = links.get(current, 0.0) - scale * weight_of[current]
            for label in sorted(links):
                gain = links[label] - scale * weight_of[label]
                if gain > best_gain + 1e-12:
                    best, best_gain = label, gain
            weight_of[best] = weight_of.get(best, 0.0) + degree[node]
            if best != current:
                community[node] = best
                moved = True
        if not moved:
            break
    return community


def louvain(graph: dict[str, dict[str, float]], seed: dict[str, str] | None = None,
            resolution: float = COMMUNITY_RESOLUTION) -> list[list[str]]:
    """The communities of `graph` (node -> neighbour -> weight), each a sorted list.

    `seed` (node -> a previous community's id) starts the first level from
    that partition instead of from singletons: an incremental run starts from
    its base's communities, so a small change moves a few files rather than
    redrawing the map (the stability §6 measures). Every community is then
    split into its connected parts, the defect Leiden (Traag, Waltman and van
    Eck, 2019) was designed to remove: Louvain can leave a community whose
    members are joined only through a node that has since moved away.
    """
    nodes = sorted(graph)
    position = {node: i for i, node in enumerate(nodes)}
    adjacency = [{position[o]: w for o, w in graph[node].items()} for node in nodes]
    labels: dict[str, int] = {}
    start = []
    for i, node in enumerate(nodes):
        previous = (seed or {}).get(node)
        start.append(labels.setdefault(previous, len(labels)) if previous is not None
                     else len(nodes) + i)
    assign = list(range(len(nodes)))
    level_adjacency, level_start = adjacency, start
    while True:
        moved = _local_moving(level_adjacency, level_start, resolution)
        renumber: dict[int, int] = {}
        packed = [renumber.setdefault(label, len(renumber)) for label in moved]
        assign = [packed[a] for a in assign]
        if len(renumber) == len(level_adjacency):
            break  # no two nodes merged: this level is the answer
        # Louvain's second phase: each community becomes one node, its
        # internal weight a self-loop, and the first phase runs again on that.
        aggregated: list[dict[int, float]] = [{} for _ in renumber]
        for node, row in enumerate(level_adjacency):
            for other, weight in row.items():
                a, b = packed[node], packed[other]
                aggregated[a][b] = aggregated[a].get(b, 0.0) + weight
        level_adjacency, level_start = aggregated, list(range(len(aggregated)))
    # One more first phase on the files themselves, from the partition the
    # levels reached. Louvain's answer is optimal per community, not per file:
    # without this a file can gain by moving, and the first seeded run after a
    # full one moved about 4% of this repository's files though only two had
    # changed (measured 2026-10-08). With it, full and seeded runs end in the
    # same kind of partition, and a change moves only what it touched.
    assign = _local_moving(adjacency, assign, resolution)
    groups: dict[int, list[int]] = {}
    for node, label in enumerate(assign):
        groups.setdefault(label, []).append(node)
    out: list[list[str]] = []
    for members in groups.values():
        inside = set(members)
        unseen = set(members)
        while unseen:
            first = min(unseen)
            part, frontier = [first], [first]
            unseen.discard(first)
            while frontier:
                node = frontier.pop()
                for other in adjacency[node]:
                    if other in inside and other in unseen:
                        unseen.discard(other)
                        part.append(other)
                        frontier.append(other)
            out.append(sorted(nodes[i] for i in part))
    return _absorb_singletons(sorted(out, key=lambda group: group[0]), graph)


def _absorb_singletons(groups: list[list[str]], graph: dict[str, dict[str, float]]
                       ) -> list[list[str]]:
    """A file alone is not a community: each one joins the community it has the
    most edge weight to (the lowest-numbered on a tie). Above 1.0 the
    resolution can leave a heavily-called small group as single files on a
    small graph; this repository's had none at 2.0 (2026-10-08)."""
    label = {node: i for i, group in enumerate(groups) for node in group}
    size = {i: len(group) for i, group in enumerate(groups)}
    # Each move removes a community, so this ends; a file that joined another
    # is no longer alone, so it never moves twice.
    for node in sorted(label):
        own = label[node]
        if size[own] != 1:
            continue
        weights: dict[int, float] = {}
        for other, weight in graph[node].items():
            if label[other] != own:
                weights[label[other]] = weights.get(label[other], 0.0) + weight
        if weights:
            target = min(weights, key=lambda j: (-weights[j], j))
            label[node] = target
            size[own] -= 1
            size[target] += 1
    merged: dict[int, list[str]] = {}
    for node, j in label.items():
        merged.setdefault(j, []).append(node)
    return sorted((sorted(members) for members in merged.values()), key=lambda g: g[0])


def _community_label(files: list[str]) -> str:
    """The directory most of a community's files are in, the shortest on a tie."""
    counts: dict[str, int] = {}
    for path in files:
        directory = posixpath.dirname(path) or "."
        counts[directory] = counts.get(directory, 0) + 1
    return min(counts, key=lambda d: (-counts[d], len(d), d))


def _community_ids(groups: list[list[str]], base: dict[str, str]) -> list[str]:
    """An id per group: the base community it shares the most files with, when
    no other group shares more with it; otherwise the next unused number."""
    overlaps = []
    for index, group in enumerate(groups):
        counts: dict[str, int] = {}
        for path in group:
            if path in base:
                counts[base[path]] = counts.get(base[path], 0) + 1
        overlaps.extend((-count, cid, index) for cid, count in counts.items())
    ids: list[str | None] = [None] * len(groups)
    taken: set[str] = set()
    for _negative, cid, index in sorted(overlaps):
        if ids[index] is None and cid not in taken:
            ids[index] = cid
            taken.add(cid)
    numbers = [int(cid[1:]) for cid in set(base.values()) | taken
               if re.match(r"^c\d+$", cid)]
    next_number = max(numbers, default=0) + 1
    for index in range(len(groups)):
        if ids[index] is None:
            ids[index] = f"c{next_number:04d}"
            next_number += 1
    return [str(cid) for cid in ids]


def community_membership(communities: Iterable[dict]) -> dict[str, str]:
    """file path -> community id, from a graph's `communities` list."""
    return {path: str(row["id"]) for row in communities if isinstance(row, dict)
            for path in row.get("files") or [] if isinstance(path, str)}


def community_stability(before: Iterable[dict], after: Iterable[dict]) -> float | None:
    """How far a partition held between two runs: for each earlier community,
    its best Jaccard overlap with any later one, weighted by its size, over
    the files both runs placed. 1.0 is unchanged; None when nothing is shared."""
    old, new = community_membership(before), community_membership(after)
    shared = set(old) & set(new)
    if not shared:
        return None
    old_groups: dict[str, set[str]] = {}
    new_groups: dict[str, set[str]] = {}
    for path in shared:
        old_groups.setdefault(old[path], set()).add(path)
        new_groups.setdefault(new[path], set()).add(path)
    total = 0.0
    for members in old_groups.values():
        candidates = {new[path] for path in members}
        best = max(len(members & new_groups[c]) / len(members | new_groups[c])
                   for c in candidates)
        total += best * len(members)
    return round(total / len(shared), 4)


def communities(edges: list[dict], symbols: list[dict], test_side: set[str] | frozenset[str],
                base: Iterable[dict] | None = None) -> list[dict]:
    """The module communities of the application files: §6's `communities`."""
    graph = _file_graph(edges, test_side)
    seed = community_membership(base or [])
    groups = louvain(graph, seed={p: c for p, c in seed.items() if p in graph} or None)
    ids = _community_ids(groups, seed)
    symbols_in: dict[str, int] = {}
    for symbol in symbols:
        symbols_in[symbol["path"]] = symbols_in.get(symbol["path"], 0) + 1
    out = []
    for cid, files in zip(ids, groups):
        members = set(files)
        inside = outside = 0.0
        for path in files:
            for other, weight in graph[path].items():
                if other in members:
                    inside += weight
                else:
                    outside += weight
        out.append({
            "id": cid,
            "label": _community_label(files),
            "files": files,
            "size": len(files),
            "symbols": sum(symbols_in.get(p, 0) for p in files),
            # The share of its files' edge weight that stays inside: 1.0 is a
            # community nothing outside calls or is called by.
            "cohesion": round(inside / (inside + outside), 3) if inside + outside else 0.0,
        })
    return sorted(out, key=lambda row: row["id"])


def _symbol_text(symbol: dict, fact: Facts | None) -> list[str]:
    """A symbol's search terms, its name weighted (BM25F's field weight)."""
    qual = symbol["id"].split("#", 1)[1].split("@", 1)[0]
    owner, _dot, name = qual.rpartition(".") if symbol["kind"] != "route" else ("", "", qual)
    if symbol["kind"] == "route":
        name = f"{symbol.get('method', '')} {symbol.get('route', '')}"
    stem = posixpath.splitext(posixpath.basename(symbol["path"]))[0]
    terms = srg.tokenize(name) * TERM_NAME_WEIGHT
    terms += srg.tokenize(owner) + srg.tokenize(stem)
    if fact is not None:
        terms += srg.tokenize(fact.signatures.get(symbol["id"], ""))
        terms += srg.tokenize(fact.docs.get(symbol["id"], ""))
    return terms


def term_index(symbols: list[dict], facts: dict[str, Facts],
               test_side: set[str] | frozenset[str] = frozenset()) -> tuple[list[dict], dict]:
    """(`terms` rows, `term_stats`): BM25 postings over every application
    symbol's text. Test-side files are left out: a test is found from what it
    covers (`symbol_test_map`), and tests and their helpers were more than
    half the postings while answering no "where is X built" question.

    A row's postings map a file to `[name, tf, dl]` lists; the reader computes
    idf from the posting count and `term_stats`, so one changed symbol
    rewrites only the term buckets its own terms are in
    (repo_graph_shards.term_bucket)."""
    postings: dict[str, dict[str, list[list]]] = {}
    lengths = 0
    documents = 0
    for symbol in symbols:
        if symbol["kind"] == "test" or symbol["path"] in test_side:
            continue
        terms = _symbol_text(symbol, facts.get(symbol["path"]))
        if not terms:
            continue
        documents += 1
        lengths += len(terms)
        counts: dict[str, int] = {}
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
        name = symbol["id"].split("#", 1)[1]
        for term, count in counts.items():
            postings.setdefault(term, {}).setdefault(symbol["path"], []).append(
                [name, count, len(terms)])
    rows = [{"term": term, "postings": {path: sorted(entries) for path, entries
                                        in sorted(postings[term].items())}}
            for term in sorted(postings)]
    stats = {"documents": documents,
             "average_length": round(lengths / documents, 4) if documents else 0.0,
             "k1": srg.BM25_K1, "b": srg.BM25_B, "tokenizer": srg.TOKENIZER_VERSION,
             "name_weight": TERM_NAME_WEIGHT}
    return rows, stats


def signature_rows(symbols: list[dict], facts: dict[str, Facts]) -> list[dict]:
    """Each callable's signature and its fingerprint: the sha256 of the
    normalised parameter list and return type, so a reformat is no change and
    a parameter added, removed, renamed, retyped or re-defaulted is one."""
    out = []
    for symbol in symbols:
        fact = facts.get(symbol["path"])
        text = fact.signatures.get(symbol["id"]) if fact is not None else None
        if text is None:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_HEX]
        out.append({"symbol": symbol["id"], "signature": text[:SIGNATURE_CHARS],
                    "fingerprint": "sha256:" + digest})
    return sorted(out, key=lambda row: row["symbol"])


def _entry_points(symbols: list[dict], facts: dict[str, Facts], resolver: "_Resolver",
                  edges: list[dict]) -> list[tuple[str, str, list[str]]]:
    """(entry id, kind, first hops) for every route, Python `__main__` guard and
    Go `main`. A JavaScript/TypeScript CLI has no syntactic mark this
    extractor trusts, so it has no flow; its routes do."""
    handlers: dict[str, list[str]] = {}
    for edge in edges:
        if edge["kind"] == "route_handler":
            handlers.setdefault(edge["from"], []).append(edge["to"])
    entries: list[tuple[str, str, list[str]]] = []
    for symbol in symbols:
        if symbol["kind"] == "route":
            entries.append((symbol["id"], "route", sorted(handlers.get(symbol["id"], []))))
        elif symbol["kind"] == "function" and symbol["language"] == "go" \
                and symbol["id"].endswith("#main"):
            fact = facts.get(symbol["path"])
            if fact is not None and fact.package == "main":
                entries.append((symbol["id"], "main", [symbol["id"]]))
    for path in sorted(facts):
        fact = facts[path]
        if fact.main_guard is None:
            continue
        first, last = fact.main_guard
        targets = [resolver.resolve(path, imp) for imp in fact.imports]
        hops: set[str] = set()
        for caller, name, qualifier, line in fact.calls:
            if caller == path and first <= line <= last:
                hops.update(_resolve_name(resolver, fact, caller, name, qualifier, targets))
        entries.append((path, "main", sorted(hops)))
    return entries


def flows(symbols: list[dict], edges: list[dict], facts: dict[str, Facts],
          resolver: "_Resolver", test_side: set[str] | frozenset[str]) -> list[dict]:
    """The bounded call flow from each entry point: breadth first over confident
    call edges, at most FLOW_MAX_DEPTH deep and FLOW_MAX_STEPS long, each step
    naming the step it was reached from. `truncated` says a bound cut it."""
    forward: dict[str, list[str]] = {}
    for edge in edges:
        if edge["kind"] == "call" and edge["confidence"] >= FLOW_MIN_CONFIDENCE \
                and _path_of(edge["to"]) not in test_side:
            forward.setdefault(edge["from"], []).append(edge["to"])
    for targets in forward.values():
        targets.sort()
    out = []
    for entry, kind, hops in _entry_points(symbols, facts, resolver, edges):
        if _path_of(entry) in test_side:
            continue
        steps: list[dict] = []
        # A Go `main` is its own first step; a route or a `__main__` guard is
        # never a call target, so nothing else can meet the entry again.
        seen: set[str] = set()
        frontier: list[tuple[str, str]] = [(hop, entry) for hop in hops]
        truncated = False
        for depth in range(1, FLOW_MAX_DEPTH + 1):
            following: list[tuple[str, str]] = []
            for node, via in frontier:
                if node in seen:
                    continue
                if len(steps) >= FLOW_MAX_STEPS:
                    truncated = True
                    break
                seen.add(node)
                steps.append({"symbol": node, "depth": depth, "via": via})
                following.extend((target, node) for target in forward.get(node, []))
            frontier = following
            if truncated or not frontier:
                break
        else:
            truncated = truncated or any(node not in seen for node, _via in frontier)
        if not steps:
            continue
        out.append({"entry": entry, "kind": kind, "path": _path_of(entry), "steps": steps,
                    "files": sorted({_path_of(step["symbol"]) for step in steps}),
                    "truncated": truncated})
    return sorted(out, key=lambda row: row["entry"])



# --- history ----------------------------------------------------------------


def _shallow_boundary(root: Path) -> set[str]:
    """The commits a shallow checkout's history stops at (`.git/shallow`)."""
    where = _git(root, "rev-parse", "--git-path", "shallow")
    if where.returncode != 0:
        return set()
    path = Path(where.stdout.decode().strip())
    path = path if path.is_absolute() else root / path
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if _SHA_RE.match(line.strip())}


def _history(root: Path, tracked: set[str]) -> tuple[dict, list[dict], dict[tuple[str, str], float]]:
    """Hot spots and co-change supports from the 90 days before HEAD.

    A SHALLOW checkout's boundary commit is not counted: git shows it as
    adding every file it holds, so a one-commit-deep clone read as "every
    file changed once" (G4-06). Without it, such a clone has no history,
    and says so (`available` false, with the reason), rather than reporting
    a change count of 1 for every file. `window_covered` says whether the
    history reaches back past the window's start; when it does not, the
    counts are a lower bound.
    """
    meta: dict[str, Any] = {"available": False, "reason": None, "window_days": HISTORY_DAYS,
                            "window_start": None, "window_end": None, "commits": 0,
                            "shallow": None, "boundary_commits": 0, "window_covered": None}
    try:
        head = _git(root, "show", "-s", "--format=%ct", "HEAD")
    except (OSError, subprocess.TimeoutExpired) as exc:
        meta["reason"] = f"git could not run: {type(exc).__name__}"
        return meta, [], {}
    if head.returncode != 0:
        meta["reason"] = "git could not read HEAD"
        return meta, [], {}
    head_time = int(head.stdout.decode().strip() or 0)
    start = head_time - HISTORY_DAYS * 86_400
    since = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    log = _git(root, "log", "--no-renames", "--numstat", "--format=%x1e%H%x1f%ct",
               f"--since={since}", "HEAD", timeout=600.0)
    if log.returncode != 0:
        meta["reason"] = "git log failed"
        return meta, [], {}
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    boundary = _shallow_boundary(root)
    meta.update({
        "available": True,
        "window_start": since,
        "window_end": datetime.fromtimestamp(head_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "shallow": shallow.stdout.decode().strip() == "true" if shallow.returncode == 0 else None,
    })
    changes: dict[str, int] = {}
    added: dict[str, int] = {}
    deleted: dict[str, int] = {}
    pairs: dict[tuple[str, str], int] = {}
    commits = 0
    boundary_in_window = 0
    for record in log.stdout.decode("utf-8", errors="replace").split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        lines = record.split("\n")
        header = lines[0].split("\x1f")
        if len(header) != 2 or not header[1].isdigit() or int(header[1]) < start:
            continue
        if header[0] in boundary:
            # Diffed against nothing: its numstat is the whole tree.
            boundary_in_window += 1
            continue
        commits += 1
        touched: set[str] = set()
        for line in lines[1:]:
            parts = line.split("\t", 2)
            if len(parts) != 3 or parts[2] not in tracked:
                continue
            path = parts[2]
            touched.add(path)
            added[path] = added.get(path, 0) + (int(parts[0]) if parts[0].isdigit() else 0)
            deleted[path] = deleted.get(path, 0) + (int(parts[1]) if parts[1].isdigit() else 0)
        for path in touched:
            changes[path] = changes.get(path, 0) + 1
        if len(touched) <= CO_CHANGE_MAX_FILES_PER_COMMIT:
            ordered = sorted(touched)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1
    meta["commits"] = commits
    meta["boundary_commits"] = boundary_in_window
    # The window is whole when the history is not cut short inside it.
    meta["window_covered"] = boundary_in_window == 0
    if commits == 0 and boundary_in_window:
        meta.update(available=False,
                    reason="the checkout is shallow and holds no commit inside the "
                           f"{HISTORY_DAYS}-day window but the one it stops at, whose change "
                           "counts would be the whole tree; hot spots and co-change are not known")
        return meta, [], {}
    strong = {pair: n for pair, n in pairs.items() if n >= CO_CHANGE_MIN_COMMITS}
    supports: dict[tuple[str, str], float] = {}
    partners: dict[str, list[dict]] = {}
    for (a, b), n in strong.items():
        # Jaccard: commits touching both over commits touching either.
        support = round(n / (changes[a] + changes[b] - n), 3)
        supports[(a, b)] = support
        partners.setdefault(a, []).append({"path": b, "commits": n, "support": support})
        partners.setdefault(b, []).append({"path": a, "commits": n, "support": support})
    ranked = sorted(changes, key=lambda p: (-changes[p], p))
    spots = []
    for path in ranked[:MAX_HOT_SPOTS]:
        together = sorted(partners.get(path, []), key=lambda c: (-c["commits"], c["path"]))
        spots.append({"path": path, "changes": changes[path],
                      "lines_added": added.get(path, 0), "lines_deleted": deleted.get(path, 0),
                      "changed_with": together[:CHANGED_WITH_PER_FILE]})
    return meta, spots, supports


# --- the pass ---------------------------------------------------------------


class _Parsers:
    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}

    def get(self, grammar: str) -> Any:
        parser = self._parsers.get(grammar)
        if parser is None:
            parser = tree_sitter.Parser(tree_sitter.Language(_LANGUAGE_FACTORIES[grammar]()))
            self._parsers[grammar] = parser
        return parser


def _parse_file(parsers: _Parsers, grammar: str, data: bytes, facts: Facts,
                timeout: float) -> None:
    deadline = time.monotonic() + timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _FileTimeout()
    parser = parsers.get(grammar)
    # timeout_micros is deprecated in favour of a progress callback, but the
    # callback crashes the interpreter in py-tree-sitter 0.25.2 (measured
    # 2026-10-05: SIGSEGV on the first cancellation), so the parser's own
    # timeout bounds the parse and the walk checks the clock itself.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        parser.timeout_micros = max(1, int(remaining * 1_000_000))
        try:
            tree = parser.parse(data)
        except ValueError as exc:
            if time.monotonic() >= deadline:
                raise _FileTimeout() from exc
            raise
        finally:
            parser.timeout_micros = 0
    if tree is None:
        raise _FileTimeout()
    facts.has_error = tree.root_node.has_error
    _EXTRACTORS[grammar](facts, tree.root_node, data, _Clock(deadline))


def extract(root: Path, budget: Budget | None = None,
            lsp: "lsp_pass.LspOptions | None" = None, base: Base | None = None,
            test_layout: list[dict] | None = None) -> dict:
    """The mechanical index of the checkout at `root`.

    With `lsp` the LSP pass runs after the tree-sitter pass and its resolved
    sites become `lsp` edges; without it no server is started. With `base`
    the run is incremental when `incremental_changes` allows it, and full,
    with the reason in `extractor.incremental`, when it does not.

    `test_layout` is the agent's last reading of the suites (`root`,
    `covers`); without it, the base index's, when one was staged. Its roots
    make files test-side and its `covers` globs give `declared` edges.
    """
    budget = budget or Budget()
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    toplevel = _git_toplevel(root)
    in_git = toplevel is not None and Path(toplevel).resolve() == root
    truncated: set[str] = set()
    if test_layout is None:
        test_layout = _test_layout_rows(base.index) if base is not None else []
    test_roots = [row["root"] for row in test_layout]

    all_paths = _list_paths(root, in_git)
    head_blobs = _blob_ids(root) if in_git else None
    commit_sha = branch = None
    if in_git:
        rev = _git(root, "rev-parse", "HEAD")
        if rev.returncode == 0:
            commit_sha = rev.stdout.decode().strip()
        ref = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
        if ref.returncode == 0:
            branch = ref.stdout.decode().strip() or None
    changes: Changes | None = None
    fell_back: str | None = None
    if base is not None:
        planned = incremental_changes(base, head_blobs, budget, commit_sha)
        if isinstance(planned, str):
            fell_back = planned
        else:
            changes = planned
    listed = all_paths[:budget.max_files]
    not_listed = len(all_paths) - len(listed)
    if not_listed:
        truncated.add("files")

    parsers = _Parsers()
    files: list[dict] = []
    facts: dict[str, Facts] = {}
    parsed_bytes = 0
    # A test file's string literals that could name a path (G4-05 b).
    path_literals: dict[str, list[str]] = {}
    for rel in listed:
        full = root / rel
        record: dict[str, Any] = {"path": rel, "language": None, "lines": 0, "bytes": 0,
                                  "status": "", "reason": None, "test": False}
        # git's blob id: what the next incremental run compares this file by.
        if head_blobs is not None and rel in head_blobs:
            record["blob"] = head_blobs[rel]
        files.append(record)
        try:
            info = os.lstat(full)
        except OSError as exc:
            record.update(status="unreadable", reason=f"cannot stat: {exc.strerror}")
            continue
        if os.path.islink(full):
            record.update(status="symlink", reason="symbolic link; not followed")
            continue
        if not stat.S_ISREG(info.st_mode):
            record.update(status="not_regular", reason="not a regular file (a submodule or a directory)")
            continue
        record["bytes"] = info.st_size
        try:
            # O_NOFOLLOW: a path swapped for a link after the lstat is
            # refused, not followed out of the checkout.
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                head = handle.read(8192)
                language, grammar = _classify(rel, head)
                record["language"] = language
                record["test"] = _is_test_file(rel, language)
                if info.st_size > budget.max_file_bytes:
                    # Counted, not read whole: line counts stay true for big files.
                    lines = head.count(b"\n")
                    last = head[-1:] if head else b""
                    while True:
                        chunk = handle.read(1 << 20)
                        if not chunk:
                            break
                        lines += chunk.count(b"\n")
                        last = chunk[-1:]
                    record["lines"] = lines + (1 if last and last != b"\n" else 0)
                    record.update(status="too_large",
                                  reason=f"{info.st_size} bytes is over the {budget.max_file_bytes}-byte per-file budget")
                    continue
                data = head + handle.read()
        except OSError as exc:
            record.update(status="unreadable", reason=f"cannot read: {exc.strerror}")
            continue
        if b"\x00" in head:
            record.update(status="binary", reason="binary content", language=None, test=False)
            continue
        record["lines"] = _count_lines(data)
        if record["test"]:
            path_literals[rel] = _path_literals(data)
        if language is None:
            ext = posixpath.splitext(rel)[1] or "no extension"
            record.update(status="not_source", reason=f"not a source file ({ext})")
            continue
        if grammar is None:
            record.update(status="unsupported",
                          reason=f"no tree-sitter grammar for {language} in this extractor; {UNSUPPORTED_REASON}")
            continue
        if parsed_bytes + len(data) > budget.max_total_bytes:
            record.update(status="over_budget",
                          reason=f"the {budget.max_total_bytes}-byte parse budget was spent")
            truncated.add("files")
            continue
        parsed_bytes += len(data)
        fact = Facts(path=rel, language=language, test_file=record["test"])
        try:
            _parse_file(parsers, grammar, data, fact, budget.file_timeout_seconds)
        except _FileTimeout:
            record.update(status="timed_out",
                          reason=f"over the {budget.file_timeout_seconds:g}-second per-file timeout")
            continue
        except Exception as exc:  # one bad file never fails the run
            record.update(status="failed", reason=f"extraction failed: {type(exc).__name__}")
            continue
        record["status"] = "parsed"
        if fact.has_error:
            record["reason"] = "the grammar reported syntax errors; symbols may be partial"
        if fact.commonjs_exports:
            for symbol in fact.symbols:
                qual = symbol["id"].split("#", 1)[1]
                if qual in fact.commonjs_exports:
                    symbol["exported"] = True
        facts[rel] = fact

    test_side = {f["path"] for f in files if f["test"] or _is_test_side(f["path"], test_roots)}
    resolver = _Resolver(files, facts)
    resolver.load_go_mods(root)
    edges, imports_of = _build_edges(resolver, facts, test_side)

    symbols = [s for path in sorted(facts) for s in facts[path].symbols]
    affected: set[str] | None = None
    if changes is not None and base is not None:
        affected = _affected(changes, base.graph, edges)
        edges = _carry_edges(edges, base.graph, affected, set(changes.deleted))
    lsp_result = None
    if lsp is not None:
        lsp_files = {path: facts[path].language for path in facts}
        sites = _lsp_sites(facts)
        if affected is not None:
            # Only the affected files' sites are asked, and only their
            # languages' servers started; every other file keeps its edges.
            wanted = {facts[path].language for path in affected if path in facts}
            lsp_files = {path: lang for path, lang in lsp_files.items() if lang in wanted}
            sites = [site for site in sites if site.path in affected]
        if affected is None or lsp_files:
            lsp_result = lsp_pass.run_pass(root, lsp_files, symbols, sites, lsp)
    if lsp_result is not None:
        for edge in lsp_result.edges:
            edges.add(edge.frm, edge.to, edge.kind, "lsp", edge.confidence, edge.path, edge.line)
    routes = [r for path in sorted(facts) for r in facts[path].routes]
    by_route = {}
    for (frm, to, kind), edge in edges.edges.items():
        if kind == "route_handler":
            by_route.setdefault(frm, []).append(to)
    for route in routes:
        if route["handler"] is None and len(by_route.get(route["id"], [])) == 1:
            route["handler"] = by_route[route["id"]][0]

    test_files = {f["path"] for f in files if f["test"]}
    # A helper a test reaches is not a symbol the test covers (G4-04).
    symbol_test_map = _symbol_test_map(symbols, edges, test_side)

    if in_git:
        history, hot_spots, co_pairs = _history(root, {f["path"] for f in files})
    else:
        history = {"available": False, "reason": "not the top of a git repository",
                   "window_days": HISTORY_DAYS, "window_start": None, "window_end": None,
                   "commits": 0, "shallow": None}
        hot_spots, co_pairs = [], {}

    path_refs = _path_ref_edges(path_literals, _Paths(files, test_side))
    test_edges = _test_map(files, imports_of, symbol_test_map, co_pairs, test_files,
                           test_side=test_side, path_refs=path_refs,
                           hcl_modules=_hcl_module_edges(facts, resolver, path_refs),
                           declared=_declared_edges(test_layout, files, test_side))

    edge_list = list(edges.edges.values())
    symbols, edge_list, symbol_test_map, routes, test_edges = _apply_caps(
        symbols, edge_list, symbol_test_map, routes, test_edges, truncated)
    # Version 3's layers (lane KG2), over the edges as stored: after the LSP
    # pass, the carry and the caps, so they describe exactly the graph a
    # reader gets.
    base_graph = base.graph if changes is not None and base is not None else None
    base_communities = (base_graph or {}).get("communities") or []
    community_rows = communities(edge_list, symbols, test_side, base=base_communities)
    term_rows, term_stats = term_index(symbols, facts, test_side)
    signature_list = signature_rows(symbols, facts)
    changed_signatures = srg.signature_changes(
        (base_graph or {}).get("signatures") or [], signature_list) if base_graph else []
    flow_rows = flows(symbols, edge_list, facts, resolver, test_side)

    modules = _modules(files, truncated)
    incremental = None
    if base is not None:
        incremental = {"base_sha": base.sha if isinstance(base.sha, str) else None,
                       "ran": changes is not None, "reason": fell_back}
    carried: dict[str, Any] = {}
    if changes is not None and base is not None and commit_sha is not None:
        modules = carry_modules(modules, base.index, changes, base.sha, commit_sha)
        carried = {
            "changes": {"added": list(changes.added), "modified": list(changes.modified),
                        "deleted": list(changes.deleted),
                        "affected": sorted(affected or ())},
            "carried": carried_reading(base.index, changes.deleted),
        }
    carried_languages = None
    if changes is not None and base is not None:
        carried_languages = {row["language"]: row for row in base.graph.get("languages") or []
                             if isinstance(row, dict) and isinstance(row.get("language"), str)}

    return {
        **carried,
        "kind": "incremental" if changes is not None else "full",
        "commit_sha": commit_sha,
        "branch": branch,
        "base_sha": base.sha if changes is not None and base is not None else None,
        "built_at": None,
        "modules": modules,
        "routes": sorted(routes, key=lambda r: (r["file"], r["start_line"], r["method"], r["path"])),
        "symbols": sorted(symbols, key=lambda s: (s["path"], s["start_line"], s["id"])),
        "call_edges": sorted(edge_list, key=lambda e: (e["from"], e["to"], e["kind"])),
        "symbol_test_map": sorted(symbol_test_map, key=lambda m: (m["symbol"], m["test"])),
        "test_map": sorted(test_edges, key=lambda t: (t["source"], t["test"])),
        "test_coverage": test_coverage(modules, files, test_edges, test_side),
        "hot_spots": hot_spots,
        "languages": _languages(files, lsp_result, carried_languages),
        "files": files,
        "communities": community_rows,
        "terms": term_rows,
        "term_stats": term_stats,
        "signatures": signature_list,
        "signature_changes": changed_signatures,
        "flows": flow_rows,
        "truncated": sorted(truncated),
        "extractor": {
            "name": EXTRACTOR_NAME,
            "version": EXTRACTOR_VERSION,
            "communities": {
                "algorithm": "louvain", "resolution": COMMUNITY_RESOLUTION,
                "split": "connected components",
                "seeded_from_base": bool(base_communities),
                "stability_vs_base": community_stability(base_communities, community_rows)
                if base_communities else None,
            },
            "grammars": {lang: f"{pkg} {_grammar_version(pkg)}" for lang, pkg in sorted(GRAMMAR_PACKAGES.items())},
            "budget": _budget_record(budget),
            "files_not_listed": not_listed,
            # Every listed file carries its git blob id, so a later run can
            # carry this graph (§3.4). Promotion records it on the version and
            # the API's `choose_kind` submits a run full, with the full
            # timeout, when the promoted graph lacks it.
            "blob_ids": head_blobs is not None and all(
                isinstance(row.get("blob"), str) and bool(row["blob"]) for row in files),
            "history": history,
            "lsp": None if lsp_result is None else {
                "servers": dict(sorted(lsp_result.servers.items())),
                "request_timeout_seconds": lsp_result.request_timeout_seconds,
                "server_budget_seconds": lsp_result.server_budget_seconds,
                "total_budget_seconds": lsp_result.total_budget_seconds,
                "memory_limit_mib": lsp_result.memory_limit_mib,
            },
            **({} if incremental is None else {"incremental": incremental}),
        },
    }


_SAME_SCOPE_QUALIFIERS = {"self", "this", "cls", "super"}


def _lsp_sites(facts: dict[str, Facts]) -> list:
    """Every candidate call, base class, route handler and HCL reference.

    `via_receiver` marks a call made through an object (`s.get()`) rather
    than through a module, an imported name, a class of this file or the
    enclosing class: a server can only resolve that through the type it
    inferred for the object, which §2.5 trusts at 0.8, not 0.95.
    """
    sites = []
    for path in sorted(facts):
        fact = facts[path]
        declared = set(_SAME_SCOPE_QUALIFIERS) | set(fact.aliases) | set(fact.bindings)
        declared |= {lsp_pass.simple_name(s["id"]) for s in fact.symbols}
        for imp in fact.imports:
            if imp.get("alias"):
                declared.add(imp["alias"])
            if isinstance(imp.get("path"), str):
                declared.add(imp["path"].rstrip("/").rsplit("/", 1)[-1])
        for kind, items in (("call", fact.calls), ("inherit", fact.inherits),
                            ("route_handler", fact.handlers)):
            for caller, name, qualifier, line in items:
                sites.append(lsp_pass.Site(
                    language=fact.language, path=path, caller=caller, name=name, line=line,
                    kind=kind, qualified=qualifier is not None,
                    via_receiver=qualifier is not None and qualifier not in declared))
        # A Terraform reference (`var.region`, `module.network.id`) is asked
        # at its last resolved label, which terraform-ls answers with the
        # block that declares it.
        for caller, address, line in fact.references:
            sites.append(lsp_pass.Site(
                language=fact.language, path=path, caller=caller,
                name=address.rsplit(".", 1)[-1], line=line, kind="reference", qualified=True))
    return sites


def _test_map(files: list[dict], imports_of: dict[str, set[str]], symbol_test_map: list[dict],
              co_pairs: dict[tuple[str, str], float], test_files: set[str], *,
              test_side: set[str] | None = None,
              path_refs: Iterable[tuple[str, str]] = (),
              hcl_modules: Iterable[tuple[str, str]] = (),
              declared: Iterable[tuple[str, str]] = ()) -> list[dict]:
    """Source -> test file edges: naming, import, ast, co-change, path-ref and declared.

    A source is a file that is not test-side (G4-04): a helper, conftest or
    fixture its tests import is never one. `path-ref` and `declared` sources
    may be a directory glob (`<dir>/**`, a covers glob); the rest are files.
    """
    entries: dict[tuple[str, str], dict] = {}
    test_side = test_files if test_side is None else test_side | test_files
    source_files = {f["path"] for f in files
                    if f["language"] is not None and f["path"] not in test_side
                    and f["status"] != "not_source"}

    def add(source: str, test: str, evidence: str, confidence: float) -> None:
        confidence = round(confidence, 3)
        key = (source, test)
        current = entries.get(key)
        if current is None:
            entries[key] = {"source": source, "test": test, "evidence": evidence,
                            "confidence": confidence, "also_evidence": []}
            return
        if current["evidence"] == evidence:
            current["confidence"] = max(current["confidence"], confidence)
            return
        _merge_evidence(current, evidence, confidence)

    for source, test in _naming_edges(files, test_side):
        add(source, test, "naming", NAMING_CONFIDENCE)
    for source, test in path_refs:
        add(source, test, "path-ref", PATH_REF_CONFIDENCE)
    for source, test in hcl_modules:
        add(source, test, "path-ref", HCL_MODULE_CONFIDENCE)
    for source, test in declared:
        add(source, test, "declared", DECLARED_CONFIDENCE)
    for test in sorted(test_files):
        for target in sorted(imports_of.get(test, ())):
            if target in source_files:
                add(target, test, "import", IMPORT_CONFIDENCE)
    for entry in symbol_test_map:
        source = entry["symbol"].split("#", 1)[0]
        test = entry["test"].split("#", 1)[0]
        if source in source_files:
            add(source, test, "ast", entry["confidence"])
    for (a, b), support in sorted(co_pairs.items()):
        if a in test_files and b in source_files:
            add(b, a, "co-change", min(support, CO_CHANGE_CAP))
        elif b in test_files and a in source_files:
            add(a, b, "co-change", min(support, CO_CHANGE_CAP))
    return list(entries.values())


def _apply_caps(symbols: list[dict], edges: list[dict], test_map_entries: list[dict],
                routes: list[dict], test_edges: list[dict],
                truncated: set[str]) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Cut each list to its bound, keeping the most certain, and say so."""

    def cap(items: list[dict], limit: int, name: str, key: Callable[[dict], Any]) -> list[dict]:
        if len(items) <= limit:
            return items
        truncated.add(name)
        return sorted(items, key=key)[:limit]

    symbols = cap(symbols, MAX_SYMBOLS, "symbols",
                  lambda s: (s["kind"] not in ("route", "test", "class"), s["path"], s["start_line"]))
    edges = cap(edges, MAX_EDGES, "call_edges",
                lambda e: (-e["confidence"], e["from"], e["to"], e["kind"]))
    test_map_entries = cap(test_map_entries, MAX_SYMBOL_TEST_MAP, "symbol_test_map",
                           lambda m: (m["depth"], -m["confidence"], m["symbol"], m["test"]))
    routes = cap(routes, MAX_ROUTES, "routes", lambda r: (r["file"], r["start_line"]))
    # The file-level map goes to the graph whole up to its own bound;
    # repo-index.json's 4,000 is applied where that document is cut
    # (`index_document`), at directory granularity first.
    test_edges = cap(test_edges, MAX_FILE_TEST_MAP, "file_test_map",
                     lambda t: (-t["confidence"], t["source"], t["test"]))
    return symbols, edges, test_map_entries, routes, test_edges


def test_coverage(modules: list[dict], files: list[dict], test_edges: list[dict],
                  test_side: set[str]) -> dict:
    """How many SOURCE modules have a test map edge, and why the rest are not counted (G4-04).

    A module whose every file is test-side is test code; one whose every
    file is build or packaging (a Dockerfile-only image directory) or not
    source is not a place tests map to. Neither belongs in the denominator.
    """
    members: dict[str, list[dict]] = {m["path"]: [] for m in modules}
    paths = sorted(members, key=lambda m: (-len(m), m))
    for f in files:
        if f["language"] is None:
            continue
        directory = posixpath.dirname(f["path"]) or "."
        for module in paths:
            if directory == module or (module != "." and directory.startswith(module + "/")):
                members[module].append(f)
                break
    # Every directory a test map source lies in or under, and every
    # directory a glob source spans: a module is covered when it is one of
    # the first, or lies under one of the second (`scripts/**` covers
    # `scripts/lib`).
    reached: set[str] = set()
    spans: set[str] = set()
    for edge in test_edges:
        source = edge["source"]
        if source == "**":
            spans.add(".")
            continue
        directory = source[:-3] if source.endswith("/**") else posixpath.dirname(source)
        if source.endswith("/**"):
            spans.add(directory)
        if not directory:
            reached.add(".")
        while directory:
            reached.add(directory)
            directory = posixpath.dirname(directory)

    def covered(module: str) -> bool:
        if module in reached or "." in spans:
            return True
        directory = module
        while directory and directory != ".":
            if directory in spans:
                return True
            directory = posixpath.dirname(directory)
        return False
    excluded: list[dict] = []
    counted: list[str] = []
    for module in sorted(members):
        rows = members[module]
        if rows and all(f["path"] in test_side for f in rows):
            excluded.append({"path": module, "reason": "test"})
        elif rows and all(f["language"] in NON_SOURCE_LANGUAGES or f["status"] == "not_source"
                          for f in rows if f["path"] not in test_side):
            excluded.append({"path": module, "reason": "build"})
        else:
            counted.append(module)

    with_tests = [m for m in counted if covered(m)]
    return {"modules": len(members), "source_modules": len(counted),
            "source_modules_with_tests": len(with_tests),
            "not_counted": excluded,
            "without_tests": [m for m in counted if m not in with_tests]}


def _modules(files: list[dict], truncated: set[str]) -> list[dict]:
    """One entry per directory holding code, coarsened to fit MAX_MODULES."""
    code = [f for f in files if f["language"] is not None]

    def group(depth: int | None) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for f in code:
            directory = posixpath.dirname(f["path"]) or "."
            if depth is not None and directory != ".":
                directory = "/".join(directory.split("/")[:depth])
            groups.setdefault(directory, []).append(f)
        return groups

    groups = group(None)
    if len(groups) > MAX_MODULES:
        truncated.add("modules")
        deepest = max(len(d.split("/")) for d in groups)
        for depth in range(deepest, 0, -1):
            groups = group(depth)
            if len(groups) <= MAX_MODULES:
                break
    modules = []
    for directory in sorted(groups):
        members = groups[directory]
        weight: dict[str, tuple[int, int]] = {}
        for f in members:
            lines, count = weight.get(f["language"], (0, 0))
            weight[f["language"]] = (lines + f["lines"], count + 1)
        language = sorted(weight, key=lambda lang: (-weight[lang][0], -weight[lang][1], lang))[0]
        modules.append({"path": directory, "language": language, "files": len(members),
                        "lines": sum(f["lines"] for f in members)})
    return modules[:MAX_MODULES]


def _languages(files: list[dict], lsp_result: Any = None,
               carried: dict[str, dict] | None = None) -> list[dict]:
    """The `languages` table: what each language got, and why.

    Without the LSP pass every language with a grammar is `unsupported`
    ("edges are syntactic"). With it, each takes its server's status and
    reason, `fallback` is "ast" unless the status is `ok`, and `lsp` carries
    the server's counts: requests, resolved and unresolved sites, request
    timeouts, errors, edges, and the version the server reported.
    """
    table: dict[str, dict] = {}
    for f in files:
        language = f["language"]
        if language is None:
            continue
        entry = table.get(language)
        if entry is None:
            supported = language in GRAMMAR_PACKAGES
            package = GRAMMAR_PACKAGES.get(language)
            entry = {
                "language": language,
                "files": 0,
                "grammar": f"{package} {_grammar_version(package)}" if package else None,
                "server": LANGUAGE_SERVERS.get(language),
                "status": "unsupported",
                "reason": SERVER_REASON if supported else UNSUPPORTED_REASON,
                "fallback": "ast" if supported else UNSUPPORTED_FALLBACK,
                "parsed": 0, "timed_out": 0, "failed": 0, "too_large": 0, "over_budget": 0,
            }
            ran = None if lsp_result is None else lsp_result.languages.get(language)
            before = (carried or {}).get(language)
            if supported and ran is not None:
                entry.update(status=ran.status, reason=ran.reason,
                             fallback=None if ran.status == "ok" else "ast")
                if ran.server is not None:
                    entry.update(server=ran.server, lsp=dict(sorted(ran.counts.items())))
            elif supported and before is not None:
                # An incremental run asked no server about this language: its
                # edges are the base's, and so is what the base's server said.
                for key in ("status", "reason", "fallback", "server", "lsp"):
                    if key in before:
                        entry[key] = before[key]
            table[language] = entry
        entry["files"] += 1
        if f["status"] in ("parsed", "timed_out", "failed", "too_large", "over_budget"):
            entry[f["status"]] += 1
    return [table[name] for name in sorted(table)]


def dumps(index: dict) -> bytes:
    """The canonical bytes: sorted keys, no whitespace, one trailing newline."""
    text = json.dumps(index, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)
    return (text + "\n").encode("utf-8")


# --- the two documents (§2.2) -----------------------------------------------

# The keys of extract()'s result that are the graph, and go only to the graph
# document. Everything else is small and per-list bounded.
_GRAPH_LISTS = ("symbols", "call_edges", "symbol_test_map", "files")
# Version 3's (lane KG2): stored by the shard writer as format 3's index
# layers. Optional, so a version-2 facts document still makes a graph.
_GRAPH_INDEX_KEYS = ("communities", "terms", "term_stats", "signatures",
                     "signature_changes", "flows")
_GRAPH_TRUNCATIONS = {"symbols", "call_edges", "symbol_test_map", "files", "file_test_map"}
# The lists repo-index.json may cut when it is over its byte budget, least
# load-bearing first (the tie-break when two weigh the same). `test_map` goes
# to directory granularity before any entry is cut, and `modules` comes last,
# because §2.2 says a repository too large for the budget keeps its module map.
_INDEX_CUT_ORDER = ("hot_spots", "graph.most_called", "routes", "test_map", "modules")


def _file_tests(test_map: list[dict]) -> dict[str, list[dict]]:
    """The file-level test map by source FILE (a glob source is not a file)."""
    by_source: dict[str, list[dict]] = {}
    for edge in test_map:
        if edge["source"].endswith("**"):
            continue
        by_source.setdefault(edge["source"], []).append(
            {"test": edge["test"], "evidence": edge["evidence"],
             "confidence": edge["confidence"], "also_evidence": list(edge["also_evidence"])})
    for rows in by_source.values():
        rows.sort(key=lambda t: t["test"])
    return by_source


def graph_document(facts: dict) -> dict:
    """The graph for the shard writer: symbols, edges, the symbol test map and
    every file with its status and reason. Never repo-index.json (§2.2).

    Each source file's row carries its file-level test map edges as `tests`
    (G4-07): the whole map, with `confidence` and `also_evidence`, sharded
    by module with the file, so a reader pages it by path. repo-index.json
    keeps the map at directory granularity when it has to cut."""
    graph = {
        "schema": GRAPH_SCHEMA,
        "kind": facts["kind"],
        "commit_sha": facts["commit_sha"],
        "branch": facts["branch"],
        "base_sha": facts["base_sha"],
        "languages": facts["languages"],
        "truncated": sorted(set(facts["truncated"]) & _GRAPH_TRUNCATIONS),
        "extractor": facts["extractor"],
    }
    for key in _GRAPH_LISTS:
        graph[key] = facts[key]
    for key in _GRAPH_INDEX_KEYS:
        if key in facts:
            graph[key] = facts[key]
    tests = _file_tests(facts.get("test_map") or [])
    if tests:
        graph["files"] = [dict(row, tests=tests[row["path"]]) if row["path"] in tests else row
                          for row in facts["files"]]
    return graph


def _graph_summary(facts: dict, graph_bytes: bytes) -> dict:
    """What repo-index.json says about the graph: counts and the most called."""
    language_of = {s["id"]: s["language"] for s in facts["symbols"]}
    by_language: dict[str, dict[str, int]] = {}
    for symbol in facts["symbols"]:
        entry = by_language.setdefault(symbol["language"], {"symbols": 0, "call_edges": 0})
        entry["symbols"] += 1
    callers: dict[str, set[str]] = {}
    for edge in facts["call_edges"]:
        language = language_of.get(edge["from"]) or language_of.get(edge["to"])
        if language is not None:
            by_language.setdefault(language, {"symbols": 0, "call_edges": 0})["call_edges"] += 1
        if edge["kind"] != "import" and edge["to"] in language_of and edge["from"] != edge["to"]:
            callers.setdefault(edge["to"], set()).add(edge["from"])
    by_id = {s["id"]: s for s in facts["symbols"]}
    ranked = sorted(callers, key=lambda sid: (-len(callers[sid]), sid))[:MOST_CALLED]
    statuses: dict[str, int] = {}
    for f in facts["files"]:
        statuses[f["status"]] = statuses.get(f["status"], 0) + 1
    return {
        "schema": GRAPH_SCHEMA,
        "digest": "sha256:" + hashlib.sha256(graph_bytes).hexdigest(),
        "bytes": len(graph_bytes),
        "symbols": len(facts["symbols"]),
        "call_edges": len(facts["call_edges"]),
        "symbol_test_map": len(facts["symbol_test_map"]),
        # File-level test map edges the graph's file rows carry (`files[].tests`).
        "test_map": sum(len(rows) for rows in _file_tests(facts.get("test_map") or []).values()),
        "files": len(facts["files"]),
        # Version 3's layers, counted (lane KG2).
        **{key: len(facts.get(key) or []) for key in _GRAPH_INDEX_KEYS if key != "term_stats"},
        "files_by_status": statuses,
        "by_language": {name: by_language[name] for name in sorted(by_language)},
        "most_called": [
            {"symbol": sid, "kind": by_id[sid]["kind"], "path": by_id[sid]["path"],
             "start_line": by_id[sid]["start_line"], "callers": len(callers[sid])}
            for sid in ranked
        ],
    }


def _test_map_by_directory(entries: list[dict]) -> list[dict]:
    """§2.2's fallback: the test map with each source at directory granularity."""
    merged: dict[tuple[str, str], dict] = {}
    for entry in entries:
        directory = posixpath.dirname(entry["source"])
        source = f"{directory}/**" if directory else "**"
        key = (source, entry["test"])
        current = merged.get(key)
        if current is None:
            merged[key] = {"source": source, "test": entry["test"], "evidence": entry["evidence"],
                           "confidence": entry["confidence"],
                           "also_evidence": list(entry["also_evidence"])}
            continue
        others = set(current["also_evidence"]) | set(entry["also_evidence"])
        if (entry["confidence"], EVIDENCE_RANK[entry["evidence"]]) > \
                (current["confidence"], EVIDENCE_RANK[current["evidence"]]):
            others.add(current["evidence"])
            current["evidence"], current["confidence"] = entry["evidence"], entry["confidence"]
        else:
            others.add(entry["evidence"])
        others.discard(current["evidence"])
        current["also_evidence"] = sorted(others)
    return sorted(merged.values(), key=lambda t: (t["source"], t["test"]))


def _served_edge(entry: dict) -> dict:
    """A test map edge in the evidence vocabulary swarm-api accepts (`SERVED_EVIDENCE`)."""
    evidence = SERVED_EVIDENCE.get(entry["evidence"], entry["evidence"])
    also = {SERVED_EVIDENCE.get(e, e) for e in entry["also_evidence"]} | (
        {entry["evidence"]} if evidence != entry["evidence"] else set())
    also.discard(evidence)
    return {**entry, "evidence": evidence, "also_evidence": sorted(also)}


def _test_map_keep_order(items: list[dict]) -> list[dict]:
    """The test map, most worth keeping first: every source's best edge, then
    every source's second, and so on (G4-07).

    Not confidence first: that cut every `naming`, `path-ref` and
    `declared` edge before any `import`, and those are what connect a module
    no import reaches. Round by round, a source keeps its strongest edges and
    a source with one edge keeps it longest.
    """
    by_source: dict[str, list[dict]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    ranked: list[tuple[int, float, int, str, str, int]] = []
    position = {id(item): n for n, item in enumerate(items)}
    for source, rows in by_source.items():
        rows.sort(key=lambda t: (-t["confidence"], -EVIDENCE_RANK.get(t["evidence"], 0), t["test"]))
        for rank, row in enumerate(rows):
            ranked.append((rank, -row["confidence"], -EVIDENCE_RANK.get(row["evidence"], 0),
                           source, row["test"], position[id(row)]))
    ranked.sort()
    return [items[entry[-1]] for entry in ranked]


def index_document(facts: dict, graph_bytes: bytes | None = None,
                   max_bytes: int = MAX_INDEX_BYTES) -> dict:
    """repo-index.json (§2.2): the mechanical keys and the graph's summary,
    held to `max_bytes`. Over the budget, the test map goes to directory
    granularity and the hot spots lose their partners; then the heaviest list
    is halved (most certain kept) until the document fits. Every list that
    gave way is named in `truncated`. A truncated index says so; it is
    never padded to look complete."""
    if graph_bytes is None:
        graph_bytes = dumps(graph_document(facts))
    truncated = set(facts["truncated"]) - _GRAPH_TRUNCATIONS
    index: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": facts["kind"],
        "commit_sha": facts["commit_sha"],
        "branch": facts["branch"],
        "base_sha": facts["base_sha"],
        "built_at": facts["built_at"],
        "modules": facts["modules"],
        "routes": facts["routes"],
        "test_map": [_served_edge(t) for t in facts["test_map"]],
        "hot_spots": facts["hot_spots"],
        "languages": facts["languages"],
        "graph": _graph_summary(facts, graph_bytes),
        "extractor": dict(facts["extractor"], max_index_bytes=max_bytes),
    }
    # The graph's own cuts are named too, so the index never hides them.
    index["graph"]["truncated"] = sorted(set(facts["truncated"]) & _GRAPH_TRUNCATIONS)
    # An incremental run's diff and the base's carried reading, for the
    # agent to start from (§3.4). Neither is a key of the artifact's shape:
    # the agent copies the carried rows into their own keys.
    for key in ("changes", "carried"):
        if key in facts:
            index[key] = facts[key]
    # Which modules are source, which are not and why, and how many source
    # modules have a test (G4-04). Not a key of the artifact's shape: the
    # agent reads it, and says it in `notes`.
    if "test_coverage" in facts:
        index["test_coverage"] = facts["test_coverage"]

    def size() -> int:
        index["truncated"] = sorted(truncated)
        return len(dumps(index))

    def get(name: str) -> list[dict]:
        if name == "graph.most_called":
            return index["graph"]["most_called"]
        return index[name]

    def put(name: str, items: list[dict]) -> None:
        if name == "graph.most_called":
            index["graph"]["most_called"] = items
        else:
            index[name] = items

    # Which entries a cut keeps: the ones a consumer leans on most.
    keep_first: dict[str, Callable[[dict], Any]] = {
        "hot_spots": lambda h: (-h["changes"], h["path"]),
        "graph.most_called": lambda m: (-m["callers"], m["symbol"]),
        "routes": lambda r: (r["file"], r["start_line"], r["method"], r["path"]),
        "modules": lambda m: (-m["lines"], m["path"]),
    }

    def keep_order(name: str, items: list[dict]) -> list[dict]:
        if name == "test_map":
            return _test_map_keep_order(items)
        return sorted(items, key=keep_first[name])

    # The index's own bound on the test map (§2.2: 4,000 edges): the graph
    # holds the file level whole, so over it the index goes to directory
    # granularity, which merges a directory's duplicate (source, test)
    # pairs, before any edge is cut.
    if len(index["test_map"]) > MAX_TEST_MAP:
        index["test_map"] = _test_map_by_directory(index["test_map"])
        truncated.add("test_map")
    if len(index["test_map"]) > MAX_TEST_MAP:
        kept_edges = keep_order("test_map", index["test_map"])[:MAX_TEST_MAP]
        index["test_map"] = sorted(kept_edges, key=lambda t: (t["source"], t["test"]))
    # First the cuts that lose detail, not entries: the test map at directory
    # granularity (§2.2), then the hot spots without their partners.
    if size() > max_bytes and index["test_map"]:
        coarse = _test_map_by_directory(index["test_map"])
        if coarse != index["test_map"]:
            index["test_map"] = coarse
            truncated.add("test_map")
    if size() > max_bytes and any(h["changed_with"] for h in index["hot_spots"]):
        index["hot_spots"] = [dict(h, changed_with=[]) for h in index["hot_spots"]]
        truncated.add("hot_spots")
    # Then entries: always halve the list spending the most bytes, so one huge
    # list never empties the small ones; ties go by _INDEX_CUT_ORDER.
    while size() > max_bytes:
        weights = [(len(dumps(get(name))), -rank, name)
                   for rank, name in enumerate(_INDEX_CUT_ORDER) if get(name)]
        if not weights:
            break
        name = max(weights)[2]
        items = get(name)
        kept = keep_order(name, items)[: len(items) // 2]
        # Back to the list's own order, so a cut list reads like an uncut one.
        position = {id(item): n for n, item in enumerate(items)}
        put(name, sorted(kept, key=lambda item: position[id(item)]))
        truncated.add(name)
    size()
    return index


_SELF_TEST_FILES = {
    "a.py": "def alpha():\n    return 1\n",
    "b.ts": "export function beta(): number {\n  return 1;\n}\n",
    "c.tsx": "export function Gamma() {\n  return <div />;\n}\n",
    "d.js": "function delta() {\n  return 1;\n}\n",
    "e.go": "package e\n\nfunc Epsilon() int {\n\treturn 1\n}\n",
    "f.tf": "variable \"zeta\" {\n  type = string\n}\n",
}


def self_test() -> int:
    """Parse one file per grammar; non-zero, naming the grammar, if any fails.

    The image build runs this, so an image whose grammars cannot load never
    ships to an indexer step.
    """
    with tempfile.TemporaryDirectory(prefix="repo-index-self-test-") as scratch:
        root = Path(scratch)
        for name, text in _SELF_TEST_FILES.items():
            (root / name).write_text(text, encoding="utf-8")
        index = extract(root, Budget(file_timeout_seconds=30.0))
    by_path: dict[str, int] = {}
    for symbol in index["symbols"]:
        by_path[symbol["path"]] = by_path.get(symbol["path"], 0) + 1
    statuses = {f["path"]: f["status"] for f in index["files"]}
    broken = sorted(name for name in _SELF_TEST_FILES
                    if statuses.get(name) != "parsed" or not by_path.get(name))
    if broken:
        print(f"repo-index self-test: failed for {', '.join(broken)}", file=sys.stderr)
        return 1
    print(f"repo-index self-test: {len(_SELF_TEST_FILES)} grammars ok")
    return 0


# One small workspace per server: a caller in one file, its callee in
# another, so a pass that resolves nothing is caught, not just one that
# cannot start.
_LSP_SELF_TEST_FILES = {
    "py/a.py": "def alpha():\n    return 1\n",
    "py/b.py": "from a import alpha\n\n\ndef beta():\n    return alpha()\n",
    "ts/a.ts": "export function alpha(): number {\n  return 1;\n}\n",
    "ts/b.ts": "import { alpha } from \"./a\";\n\nexport function beta(): number {\n  return alpha();\n}\n",
    "go/go.mod": "module example.com/selftest\n\ngo 1.22\n",
    "go/a.go": "package selftest\n\nfunc Alpha() int {\n\treturn 1\n}\n",
    "go/b.go": "package selftest\n\nfunc Beta() int {\n\treturn Alpha()\n}\n",
}
_LSP_SELF_TEST_EDGES = {
    "python": ("py/b.py#beta", "py/a.py#alpha"),
    "typescript": ("ts/b.ts#beta", "ts/a.ts#alpha"),
    "go": ("go/b.go#Beta", "go/a.go#Alpha"),
}


def lsp_self_test(bin_dir: Path, request_timeout_seconds: float = 30.0) -> int:
    """Start each installed server and require one `lsp` edge from each.

    The image build runs this as the agent user, so an image whose servers
    cannot start, or start and resolve nothing, never ships to an indexer.
    Each language is its own workspace, as a repository's would be.
    """
    broken: list[str] = []
    for language, (frm, to) in sorted(_LSP_SELF_TEST_EDGES.items()):
        top = frm.split("/", 1)[0]
        with tempfile.TemporaryDirectory(prefix="repo-lsp-self-test-") as scratch:
            root = Path(scratch)
            for name, text in _LSP_SELF_TEST_FILES.items():
                if name.startswith(top + "/"):
                    (root / top).mkdir(exist_ok=True)
                    (root / name).write_text(text, encoding="utf-8")
            options = lsp_pass.LspOptions(bin_dir=bin_dir, request_timeout_seconds=request_timeout_seconds,
                                          server_budget_seconds=300.0)
            started = time.monotonic()
            index = extract(root / top, Budget(file_timeout_seconds=30.0), lsp=options)
        row = next((r for r in index["languages"] if r["language"] == language), None)
        relative = (frm.split("/", 1)[1], to.split("/", 1)[1])
        found = [e for e in index["call_edges"]
                 if (e["from"], e["to"]) == relative and e["evidence"] == "lsp"]
        status = row["status"] if row else "absent"
        print(f"repo-index lsp self-test: {language}: {status}"
              f"{'' if not row or not row.get('reason') else ' (' + row['reason'] + ')'}, "
              f"{len(found)} lsp edge(s), {time.monotonic() - started:.1f}s")
        if status != "ok" or not found:
            broken.append(language)
    if broken:
        print(f"repo-index lsp self-test: failed for {', '.join(broken)}", file=sys.stderr)
        return 1
    print(f"repo-index lsp self-test: {len(_LSP_SELF_TEST_EDGES)} servers ok")
    return 0


def _read_json(path: str | None) -> Any:
    """A staged base document, or None when it is absent or not JSON."""
    if not path:
        return None
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=EXTRACTOR_NAME,
        description="The mechanical half of a repository index (docs/repo-index.md §3.5).",
    )
    parser.add_argument("--repo", default=".", help="the checkout to index (default: .)")
    parser.add_argument("--out", help="write repo-index.json here (default: stdout)")
    parser.add_argument("--graph-out",
                        help="also write the graph (symbols, call_edges, symbol_test_map, "
                             "files) here, for the shard writer; never the artifact")
    parser.add_argument("--max-index-bytes", type=int, default=MAX_INDEX_BYTES,
                        help="repo-index.json's byte budget (default: §2.2's 512 KiB)")
    defaults = Budget()
    parser.add_argument("--max-file-bytes", type=int, default=defaults.max_file_bytes)
    parser.add_argument("--max-total-bytes", type=int, default=defaults.max_total_bytes)
    parser.add_argument("--max-files", type=int, default=defaults.max_files)
    parser.add_argument("--file-timeout-seconds", type=float, default=defaults.file_timeout_seconds)
    parser.add_argument("--self-test", action="store_true",
                        help="parse one file per grammar and exit")
    parser.add_argument("--no-lsp", action="store_true",
                        help="skip the LSP pass: tree-sitter edges only, every language unsupported")
    parser.add_argument("--lsp-bin-dir", default=str(lsp_pass.DEFAULT_BIN_DIR),
                        help="where the language servers are installed (default: the image's)")
    parser.add_argument("--lsp-request-timeout-seconds", type=float,
                        default=lsp_pass.LspOptions().request_timeout_seconds)
    parser.add_argument("--lsp-server-budget-seconds", type=float, default=None,
                        help="per language server (default: §3.5's table, by repository size)")
    parser.add_argument("--lsp-total-budget-seconds", type=float, default=None,
                        help="the whole LSP pass, every server together (default: half of "
                             "§3.5's full-run budget, by repository size; lsp/driver.py says why)")
    parser.add_argument("--lsp-memory-mib", type=int, default=None,
                        help="stop a server above this resident memory "
                             "(default: 3/4 of the container's limit, or 4096)")
    parser.add_argument("--lsp-self-test", action="store_true",
                        help="start each language server on a scratch workspace and exit")
    parser.add_argument("--base-sha",
                        help="incremental (§3.4): the commit of the previous promoted index")
    parser.add_argument("--base-index", help="incremental: that index's repo-index.json")
    parser.add_argument("--base-graph",
                        help="incremental: its graph, as `swarm-repo-graph read` wrote it")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.lsp_self_test:
        return lsp_self_test(Path(args.lsp_bin_dir))
    root = Path(args.repo)
    if not root.is_dir():
        print(f"{EXTRACTOR_NAME}: {args.repo} is not a directory", file=sys.stderr)
        return 2
    budget = Budget(max_file_bytes=args.max_file_bytes, max_total_bytes=args.max_total_bytes,
                    max_files=args.max_files, file_timeout_seconds=args.file_timeout_seconds)
    lsp_options = None if args.no_lsp else lsp_pass.LspOptions(
        bin_dir=Path(args.lsp_bin_dir), request_timeout_seconds=args.lsp_request_timeout_seconds,
        server_budget_seconds=args.lsp_server_budget_seconds,
        total_budget_seconds=args.lsp_total_budget_seconds, memory_limit_mib=args.lsp_memory_mib)
    base = None
    if args.base_sha:
        # A base that cannot be read makes the run full and says why; it
        # never fails the run (§3.5: a less certain index, never a lost one).
        base = Base(sha=args.base_sha, graph=_read_json(args.base_graph),
                    index=_read_json(args.base_index))
    # The cyclic collector off for the run (lane KG2): the pass allocates
    # millions of small acyclic objects (facts, edges, postings) that reference
    # counting frees, and the collector's repeated full scans of them were
    # about 1.5 s of a 16 s run on this repository, which version 3's extra
    # layers would otherwise have spent twice over. Nothing here builds a
    # reference cycle worth collecting before the process exits.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        facts = extract(root, budget, lsp=lsp_options, base=base)
        graph_payload = dumps(graph_document(facts))
        index = index_document(facts, graph_payload, max_bytes=args.max_index_bytes)
        payload = dumps(index)
    finally:
        if gc_was_enabled:
            gc.enable()
    if args.graph_out:
        graph_out = Path(args.graph_out)
        graph_out.parent.mkdir(parents=True, exist_ok=True)
        graph_out.write_bytes(graph_payload)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
    else:
        sys.stdout.buffer.write(payload)
    summary = index["graph"]
    incremental = facts["extractor"].get("incremental") or {}
    print(
        f"{EXTRACTOR_NAME}: kind={facts['kind']}"
        + (f" base={str(incremental.get('base_sha'))[:12]}" if incremental else "")
        + (f" changed={sum(len(v) for k, v in facts['changes'].items() if k != 'affected')}"
           f" affected={len(facts['changes']['affected'])}" if "changes" in facts else "")
        + (f" full_because={incremental['reason']!r}" if incremental.get("reason") else "")
        + f" files={summary['files']} symbols={summary['symbols']} "
        f"edges={summary['call_edges']} routes={len(index['routes'])} "
        f"statuses={json.dumps(summary['files_by_status'], sort_keys=True)} "
        f"index_bytes={len(payload)} graph_bytes={len(graph_payload)} "
        f"truncated={','.join(index['truncated']) or 'none'} "
        f"graph_truncated={','.join(summary['truncated']) or 'none'} "
        f"lsp={','.join(r['language'] + ':' + r['status'] for r in index['languages'] if 'lsp' in r) or 'off'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
