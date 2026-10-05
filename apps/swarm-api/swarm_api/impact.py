"""The commit / pull-request impact query, and the graph reads (docs/repo-index.md §4.3a, lane RI11).

`tests:select` (`repoindex.select_tests`) answers from paths. This answers
from symbols:

    diff -> changed symbols -> transitive callers, bounded -> covering tests
         -> a test plan with a reason per test, and `fallback_triggers`

and serves the graph, symbol and language reads the console's graph explorer
and impact view draw (§6.1, PICKS.md: Graph A, Impact A).

WHY EACH RULE:

  * THE DIFF IS READ FROM THE FORGE, NEVER FROM THE CALLER. A request names a
    pull request, a commit or a base..head range and nothing else
    (`ImpactRequest`, extra="forbid"): the changed files and their hunks come
    from GitHub, so a caller cannot hand the gate a smaller diff than the one
    it is judging, and no image, command or path a caller sends reaches
    anything (invariant 10). Nothing is checked out: the hunks give line
    ranges, and that is all the query needs.
  * THE TOKEN IS THE ONE R2 RESOLVES (git-tokens.md §3.1, PICKS.md
    2026-10-05): the repository's own token, else the tenant default. A
    user's token is for attribution only and a diff read has no author, so
    it is never used here. The value lives in one frame (`ImpactService.
    _read_diff`), goes to the forge client's one header, is a literal the
    path masking redacts, and is never stored, logged or answered; the plan
    names the slot it was read with (`read_with`), never its value.
  * CHANGED SYMBOLS ARE THE INNERMOST SYMBOL PER LINE. A removed or modified
    line belongs to the narrowest symbol whose range holds it, so editing a
    method changes the method, not its class (whose callers are everyone who
    constructs one). Removed lines are matched against the BASE's symbols
    (what the change touched), added lines against the head's when the head
    is indexed. An added line no index has a symbol for is `unindexed`, and
    its file falls back to file level.
  * THE WALK IS BOUNDED THREE WAYS, AND SAYS WHERE IT STOPPED. Breadth-first
    over the reverse call edges (`callers` shards: one read per frontier
    module, so the cost scales with the change, not the repository), to
    `depth` (default 3, at most 6), stopping a path whose confidence product
    falls below the floor (0.2; `low_confidence_cut`) and the whole walk at
    `max_nodes` (`bound.node_cap_hit`). Every frontier symbol that still had
    callers at the depth limit is listed in `bound.stopped_at_depth`, so a
    short answer is never mistaken for a complete one.
  * A FALLBACK TRIGGER SELECTS THE FULL SUITE AND SAYS WHY. The kinds are
    §4.3a's list -- build or test configuration, a shared fixture, a changed
    symbol whose every test path has an edge below 0.4, a changed file in an
    `unsupported`/`failing`/`timed_out` language, an `unindexed` symbol, a
    stale index -- plus one the forge forces: GitHub cut the diff short
    (`diff_truncated`), and a plan over a diff it has not seen whole cannot
    speak for the rest. They are computed under every policy (§4.4), so the
    console shows them whichever policy is picked.
  * TESTS COME FROM THE REPOSITORY'S OWN INDEX. A test's command is composed
    from the index's `test_layout` (the suite whose root holds the test,
    narrowed to the test's node id) or the `test_map` entry's own command,
    never from a caller (invariant 10). The full suite is the widest suite.
  * DETERMINISTIC. Every list is sorted and every float rounded, so the same
    diff over the same graph is the same bytes whatever order GitHub listed
    it in; the plan is stored per (tenant, repository, base, head, depth,
    index, graph digest) and asking again overwrites the same document.

TENANT ISOLATION (invariant 9). Every read starts from the registration read
with the caller's tenant (another tenant's repo_id is a 404 before the
forge), every graph is opened through `repograph.RepoGraph` with the caller's
tenant (a key under another tenant's prefix is never built), the token is
resolved from the caller's own tenant's records only (`resolve_r2` filters
them again), and `impact_plans` documents carry `tenant_id`.

WHERE THE PLAN LIVES. `impact_plans/{plan_id}` (§6.2), in this module's own
collection, read and written here only, for the reason `issueruns` gives.
§6.2 names a `plan_object`; swarm-api's bucket grant is objectViewer and
writes nothing, so the plan is stored in the document itself, its lists cut
to `MAX_STORED_PLAN_BYTES` when it would not fit (`stored_whole: false`), and
`plan_object` stays null.

INVARIANTS 1-3: a query holds no capacity, submits nothing and creates no
infrastructure demand. It is a few forge GETs and shard reads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from swarm_common.models import Tenant

from . import forge as _forge
from .errors import NotFound, ValidationFailed
from .forge import (
    GITHUB_API_HOST,
    MAX_RESPONSE_BYTES,
    ForgeTokens,
    GitHubIssues,
    IssueReadFailed,
)
from .gittokens import GitTokens, Scope, resolve_r2
from .redaction import redact
from .repograph import Graph, NoGraph, RepoGraph, module_of
from .repoindex import RepoIndex, freshness, select_tests
from .repositories import GRAPH_DEPTH_DEFAULT, GRAPH_MIN_CONFIDENCE_DEFAULT

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# constants, each with the reason for its value
# --------------------------------------------------------------------------

PLANS_COLLECTION = "impact_plans"

#: §4.3a: "to `depth` (default 3, at most 6, per repository in Settings)".
DEPTH_MAX = 6
#: §4.3a: "stopping a path whose confidence product falls below 0.2".
MIN_CONFIDENCE = GRAPH_MIN_CONFIDENCE_DEFAULT
#: §4.3a and §4.4 P3: "a changed symbol with an edge below 0.4 on its path to
#: every test that reaches it" falls back. The same 0.4 as `import` evidence
#: and §2.5's ceiling cut: below it an edge is a guess.
LOW_CONFIDENCE = 0.4
#: The walk's node bound: changed and affected symbols together. A change
#: that reaches more than this many symbols is a hub whose plan would be most
#: of the suite anyway; the bound keeps one request's shard reads and memory
#: bounded, and `bound.node_cap_hit` says it was reached.
MAX_AFFECTED = 2_000
#: What the answer lists of each long list (`affected`, `low_confidence_cut`,
#: `unmapped`, `stopped_at_depth`); the counts are always whole.
MAX_LISTED = 500
#: GitHub's own caps: a pull request lists at most 3,000 files (30 pages of
#: 100), a compare at most 300. At the cap the diff may be longer than what
#: was read, and the plan says so (`diff_truncated`).
PR_FILE_PAGES = 30
MAX_PR_FILES = 3_000
COMPARE_FILE_CAP = 300
#: A file's changed lines matched one by one up to this; past it (a
#: generated file rewritten whole) every symbol overlapping a range counts.
MAX_LINES_PER_FILE = 50_000
#: Firestore's document limit is 1 MiB; the stored plan stays under it.
MAX_STORED_PLAN_BYTES = 900 * 1024
#: Symbol search and neighbourhood bounds for the graph explorer.
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 200
NEIGHBOURHOOD_DEPTH_DEFAULT = 2
MAX_NEIGHBOURHOOD_NODES = 300

#: The edge kinds a caller walk follows. `import` edges join files, not
#: symbols (repo_index_extract `_build_edges`), so they are a module
#: dependency, drawn by the graph route and never walked as a call.
WALKED_KINDS = frozenset({"call", "reference", "route_handler", "inherit"})

#: §4.3a: a changed file in a language marked any of these falls back.
UNTRUSTED_LANGUAGE_STATUSES = frozenset({"unsupported", "failing", "timed_out"})

#: A file's language when no graph lists it (a file the head adds). Restated
#: from the extractor (images/agent-runtime-base/repo-index/
#: repo_index_extract.py `SUPPORTED_EXTENSIONS`, `UNSUPPORTED_EXTENSIONS`,
#: `UNSUPPORTED_FILENAMES`), which runs in another image; a language missing
#: here is a file with no language, i.e. not source.
_GRAMMAR_EXTENSIONS = {
    ".py": "python", ".pyi": "python", ".ts": "typescript", ".mts": "typescript",
    ".cts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".go": "go", ".tf": "hcl", ".hcl": "hcl",
    ".tfvars": "hcl",
}
_OTHER_EXTENSIONS = {
    ".rb": "ruby", ".java": "java", ".rs": "rust", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".swift": "swift", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hpp": "cpp", ".cs": "csharp", ".php": "php", ".sh": "shell",
    ".bash": "shell", ".zsh": "shell", ".lua": "lua", ".pl": "perl", ".r": "r",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".clj": "clojure", ".dart": "dart",
    ".m": "objective-c", ".vue": "vue", ".svelte": "svelte", ".sql": "sql",
    ".proto": "protobuf", ".groovy": "groovy", ".fs": "fsharp", ".hs": "haskell",
    ".ml": "ocaml", ".zig": "zig", ".nim": "nim", ".ps1": "powershell", ".jl": "julia",
}
_OTHER_FILENAMES = {
    "Dockerfile": "dockerfile", "Makefile": "make", "GNUmakefile": "make",
    "Jenkinsfile": "groovy", "Rakefile": "ruby", "Gemfile": "ruby",
}

#: §4.3a "a build or test configuration file". Build: what decides which code
#: and dependencies a test runs against. Every `commands[].source` the index
#: names (the Makefile its `make test` came from) is added per repository.
BUILD_CONFIG_NAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock", "Pipfile",
    "Pipfile.lock", "requirements.txt", "constraints.txt", "package.json",
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "yarn.lock", ".npmrc", ".nvmrc", ".node-version", ".python-version", ".tool-versions",
    "tsconfig.json", "go.mod", "go.sum", "go.work", "go.work.sum", "Makefile", "GNUmakefile",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    ".terraform.lock.hcl", "pyrightconfig.json", "Cargo.toml", "Cargo.lock", "Gemfile",
    "Gemfile.lock", "build.gradle", "build.gradle.kts", "settings.gradle", "pom.xml",
    "CMakeLists.txt", ".gitlab-ci.yml",
})
_BUILD_CONFIG_PATTERNS = (
    re.compile(r"^(?:.*/)?requirements[^/]*\.(?:txt|in)$"),
    re.compile(r"^(?:.*/)?requirements/[^/]+\.(?:txt|in)$"),
    re.compile(r"^(?:.*/)?tsconfig[^/]*\.json$"),
    re.compile(r"^(?:.*/)?Dockerfile\.[^/]+$"),
    re.compile(r"^(?:.*/)?[^/]+\.dockerfile$"),
    re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"),
    re.compile(r"^\.github/actions/"),
    re.compile(r"^\.circleci/"),
)
#: Test configuration: what decides how, and which, tests run.
TEST_CONFIG_NAMES = frozenset({
    "pytest.ini", "tox.ini", "noxfile.py", ".coveragerc", ".mocharc.json", ".mocharc.yml",
    ".mocharc.yaml", ".mocharc.js", "phpunit.xml", ".rspec",
})
_TEST_CONFIG_PATTERNS = (
    re.compile(r"^(?:.*/)?(?:vitest|jest|karma|playwright|cypress|ava|wdio)"
               r"(?:\.[A-Za-z0-9_-]+)*\.(?:config|conf|workspace)\.[cm]?[jt]sx?$"),
    re.compile(r"^(?:.*/)?(?:vitest|jest)\.(?:config|workspace)\.json$"),
    re.compile(r"^(?:.*/)?jest\.setup\.[cm]?[jt]sx?$"),
)
#: §4.3a "a shared fixture (`conftest.py`, a `fixtures/` or `testutils/` module)".
SHARED_FIXTURE_NAMES = frozenset({"fixtures", "testutils"})

_TEST_FILE = re.compile(
    r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.py|[^/]+_test\.go|"
    r"[^/]+\.(?:test|spec)\.[cm]?[jt]sx?|[^/]+\.tftest\.hcl)$"
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")
_SYMBOL_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")

#: The order a test's reasons are preferred in, when several select it.
_SOURCE_RANK = {"changed": 0, "walk": 1, "map": 2, "file": 3, "always": 4}


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------

_SHA_PATTERN = r"^[0-9a-f]{40}$"


class ImpactRequest(BaseModel):
    """`POST .../impact`: exactly one change, and a depth. extra="forbid": a
    caller's `paths`, `command`, `image` or `tests` is refused, not ignored."""

    model_config = ConfigDict(extra="forbid")

    pull_request: StrictInt | None = Field(default=None, ge=1, le=2_000_000_000)
    commit: str | None = Field(default=None, pattern=_SHA_PATTERN)
    base: str | None = Field(default=None, pattern=_SHA_PATTERN)
    head: str | None = Field(default=None, pattern=_SHA_PATTERN)
    depth: StrictInt | None = Field(default=None, ge=1, le=DEPTH_MAX)

    @model_validator(mode="after")
    def _one_change(self) -> "ImpactRequest":
        if (self.base is None) != (self.head is None):
            raise ValueError("a range names both base and head")
        named = sum(x is not None for x in (self.pull_request, self.commit, self.base))
        if named != 1:
            raise ValueError("name exactly one of pull_request, commit, or base and head")
        return self


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffFile:
    """One changed file, as the forge lists it. `patch` None: GitHub sent no
    hunks (a binary file, or one too large to show), so the whole file counts."""

    path: str
    status: str
    previous_path: str | None = None
    patch: str | None = None


@dataclass(frozen=True)
class Diff:
    base_sha: str | None
    head_sha: str
    files: tuple[DiffFile, ...]
    pull_request: int | None = None
    commit: str | None = None
    #: GitHub listed as many files as it ever lists: there may be more.
    truncated: bool = False


@dataclass(frozen=True)
class Insertion:
    """Added lines with no removed line beside them, anchored between base
    lines `after` and `before`; `lines` are their head line numbers."""

    after: int
    before: int
    lines: tuple[int, ...]


@dataclass(frozen=True)
class AddedRun:
    """A run of added lines, and the base lines it replaced (empty: an insertion)."""

    lines: tuple[int, ...]
    replaced: tuple[int, ...]
    after: int
    before: int


@dataclass
class Patch:
    removed: list[int] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    insertions: list[Insertion] = field(default_factory=list)
    runs: list[AddedRun] = field(default_factory=list)


def parse_patch(patch: str) -> Patch:
    """A unified diff's hunks as base line numbers removed and head lines added.

    Only the line numbers are kept; the text of a line is never stored,
    answered or logged.
    """
    out = Patch()
    old = new = 0
    removed_run: list[int] = []
    added_run: list[int] = []
    run_after = 0

    def flush() -> None:
        nonlocal removed_run, added_run
        if added_run:
            out.runs.append(AddedRun(lines=tuple(added_run), replaced=tuple(removed_run),
                                     after=run_after, before=run_after + 1))
            if not removed_run:
                out.insertions.append(Insertion(after=run_after, before=run_after + 1,
                                                lines=tuple(added_run)))
        removed_run, added_run = [], []

    in_hunk = False
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            flush()
            old, new = int(match.group(1)), int(match.group(3))
            # `-0,0`: the file is new; `+0,0`: deleted. Either way the counts
            # below start from the first line.
            old, new = max(old, 1), max(new, 1)
            if match.group(2) == "0":
                old = int(match.group(1)) + 1
            if match.group(4) == "0":
                new = int(match.group(3)) + 1
            in_hunk = True
            continue
        if not in_hunk or not line:
            if in_hunk and not line:
                # An empty context line (some forges strip the leading space).
                flush()
                old += 1
                new += 1
            continue
        mark = line[0]
        if mark == "-":
            if added_run:
                flush()
            if not removed_run:
                run_after = old - 1
            removed_run.append(old)
            out.removed.append(old)
            old += 1
        elif mark == "+":
            if not removed_run and not added_run:
                run_after = old - 1
            added_run.append(new)
            out.added.append(new)
            new += 1
        elif mark == "\\":
            continue  # "\ No newline at end of file"
        else:
            flush()
            old += 1
            new += 1
    flush()
    return out


def _ranges(lines: Iterable[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for line in sorted(set(lines)):
        if out and line == out[-1][1] + 1:
            out[-1][1] = line
        else:
            out.append([line, line])
    return out


def _clean_path(path: Any, literals: tuple[str, ...]) -> str | None:
    """A forge path as data: masked like any forge text, one line, never resolved."""
    if not isinstance(path, str) or not path or len(path) > 1024 or _CONTROL.search(path):
        return None
    return redact(path, extra=literals).text


# --------------------------------------------------------------------------
# the graph, read lazily
# --------------------------------------------------------------------------

class _View:
    """A graph's symbols, files and edges by id and path, one shard read each."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self._by_id: dict[str, dict[str, dict]] = {}
        self._files: dict[str, dict[str, dict]] = {}

    def _module(self, module: str) -> dict[str, dict]:
        cached = self._by_id.get(module)
        if cached is None:
            cached = {str(s.get("id")): s for s in self.graph.symbols(module)}
            self._by_id[module] = cached
        return cached

    def symbol(self, symbol_id: str) -> dict | None:
        return self._module(module_of(symbol_id)).get(symbol_id)

    def in_file(self, path: str) -> list[dict]:
        return sorted(
            (s for s in self._module(module_of(path)).values() if s.get("path") == path),
            key=lambda s: (_int(s.get("start_line")), str(s.get("id"))),
        )

    def file(self, path: str) -> dict | None:
        module = module_of(path)
        rows = self._files.get(module)
        if rows is None:
            rows = {str(r.get("path")): r for r in self.graph.shard("files", module)}
            self._files[module] = rows
        return rows.get(path)

    def callers(self, symbol_id: str) -> list[dict]:
        return sorted(
            (e for e in self.graph.callers(symbol_id) if e.get("kind") in WALKED_KINDS
             and isinstance(e.get("from"), str)),
            key=lambda e: (str(e["from"]), str(e.get("kind"))),
        )

    def callees(self, symbol_id: str) -> list[dict]:
        return sorted(
            (e for e in self.graph.callees(symbol_id) if e.get("kind") in WALKED_KINDS
             and isinstance(e.get("to"), str)),
            key=lambda e: (str(e["to"]), str(e.get("kind"))),
        )

    def tests_for(self, symbol_id: str) -> list[dict]:
        return sorted(
            (t for t in self.graph.tests_for(symbol_id) if isinstance(t.get("test"), str)),
            key=lambda t: str(t["test"]),
        )

    def languages(self) -> dict[str, str]:
        return {str(row.get("language")): str(row.get("status"))
                for row in self.graph.manifest.get("languages") or []
                if isinstance(row, dict)}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _conf(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 1.0)


#: Test symbols per graph, by manifest digest: a digest names one exact
#: graph, so the count never goes stale. Bounded; a miss is a recount.
_TOTAL_TESTS: dict[str, int] = {}
_TOTAL_TESTS_KEPT = 64


def total_tests(graph: Graph) -> int:
    """Every test symbol the graph has: the "of 1,480" in "12 of 1,480 selected".

    The one read that touches every symbols shard, so it is cached by digest.
    """
    cached = _TOTAL_TESTS.get(graph.digest)
    if cached is not None:
        return cached
    count = sum(1 for module in graph.modules("symbols") for s in graph.symbols(module)
                if s.get("kind") == "test")
    if len(_TOTAL_TESTS) >= _TOTAL_TESTS_KEPT:
        _TOTAL_TESTS.pop(next(iter(_TOTAL_TESTS)))
    _TOTAL_TESTS[graph.digest] = count
    return count


def short(symbol_id: str) -> str:
    """`src/a.py#OrderService.total@12` -> `OrderService.total`."""
    name = symbol_id.split("#", 1)[1] if "#" in symbol_id else symbol_id
    return name.split("@", 1)[0]


# --------------------------------------------------------------------------
# files: language, configuration, fixtures, tests
# --------------------------------------------------------------------------

def language_of(path: str, views: Sequence[_View]) -> str | None:
    for view in views:
        row = view.file(path)
        if row is not None and row.get("language"):
            return str(row["language"])
    name = posixpath.basename(path)
    if name in _OTHER_FILENAMES:
        return _OTHER_FILENAMES[name]
    ext = posixpath.splitext(name)[1].lower()
    return _GRAMMAR_EXTENSIONS.get(ext) or _OTHER_EXTENSIONS.get(ext)


def is_build_config(path: str, sources: frozenset[str] = frozenset()) -> bool:
    return (posixpath.basename(path) in BUILD_CONFIG_NAMES or path in sources
            or any(p.search(path) for p in _BUILD_CONFIG_PATTERNS))


def is_test_config(path: str) -> bool:
    return (posixpath.basename(path) in TEST_CONFIG_NAMES
            or any(p.search(path) for p in _TEST_CONFIG_PATTERNS))


def is_shared_fixture(path: str) -> bool:
    parts = path.split("/")
    stem = posixpath.splitext(parts[-1])[0]
    return (parts[-1] == "conftest.py" or stem in SHARED_FIXTURE_NAMES
            or any(part in SHARED_FIXTURE_NAMES for part in parts[:-1]))


def is_test_file(path: str, views: Sequence[_View]) -> bool:
    if is_shared_fixture(path):
        return False
    for view in views:
        row = view.file(path)
        if row is not None and row.get("test") is True:
            return True
    return bool(_TEST_FILE.search(path))


# --------------------------------------------------------------------------
# commands, from the repository's own index (invariant 10)
# --------------------------------------------------------------------------

def _quote(text: str) -> str:
    return f"'{text}'" if "'" not in text else shlex.quote(text)


def full_suite_command(document: Mapping[str, Any] | None) -> str | None:
    """The widest suite the index names, else its `test` command."""
    if not document:
        return None
    suites = [s for s in document.get("test_layout") or [] if s.get("command")]
    if suites:
        return min(suites, key=lambda s: (len(str(s.get("root") or "")),
                                          str(s.get("root"))))["command"]
    return next((c["command"] for c in document.get("commands") or []
                 if c.get("kind") == "test" and c.get("command")), None)


def test_command(document: Mapping[str, Any] | None, test_id: str) -> str | None:
    """The command that runs one test, composed from the index's own suite.

    The suite whose root holds the test's file, its root argument narrowed to
    the test (`path::Class::name` for pytest, `-run '^Name$'` for Go, the file
    for anything else); a suite whose command does not name its root runs
    whole. With no suite, the test map's own command for the file.
    """
    if not document:
        return None
    path, _, qual = test_id.partition("#")
    qual = qual.split("@", 1)[0]
    holding = [s for s in document.get("test_layout") or []
               if s.get("command") and s.get("root")
               and (path + "/").startswith(str(s["root"]).rstrip("/") + "/")]
    if holding:
        suite = max(holding, key=lambda s: (len(str(s["root"])), str(s["root"])))
        root = str(suite["root"]).rstrip("/")
        words = str(suite["command"]).split(" ")
        for i, word in enumerate(words):
            if word.rstrip("/") not in (root, "./" + root):
                continue
            framework = str(suite.get("framework") or "").lower()
            if framework == "pytest" and qual:
                words[i] = _quote(f"{path}::{qual.replace('.', '::')}")
            elif framework in ("go", "gotest", "go test") and qual:
                directory = posixpath.dirname(path) or "."
                words[i] = f"./{directory} -run {_quote('^' + qual.split('.')[-1] + '$')}"
            else:
                words[i] = _quote(path)
            return " ".join(words)
        return str(suite["command"])
    for entry in document.get("test_map") or []:
        if entry.get("test") == path and entry.get("command"):
            return str(entry["command"])
    return None


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------

@dataclass
class _Reach:
    depth: int
    confidence: float
    #: The weakest edge on the path: what §4.3a's "an edge below 0.4 on its
    #: path" reads.
    floor: float
    #: The next symbol toward the changed one, and the edge's evidence.
    via: str | None
    evidence: str | None


@dataclass
class _Candidate:
    test: str
    seed: str
    source: str
    confidence: float | None
    depth: int | None
    floor: float
    chain: tuple[str, ...]
    evidence: tuple[str, ...]

    def key(self) -> tuple:
        return (-(self.confidence if self.confidence is not None else -1.0),
                self.depth if self.depth is not None else 0, _SOURCE_RANK[self.source],
                self.seed)


@dataclass
class _Changed:
    symbol: dict
    side: str
    view: _View
    lines: set[int] = field(default_factory=set)
    why: str | None = None


class _Walk:
    """The bounded reverse walk from every changed symbol, sharing one node bound."""

    def __init__(self, depth: int, floor: float, max_nodes: int) -> None:
        self.depth = depth
        self.floor = floor
        self.max_nodes = max_nodes
        self.nodes: set[str] = set()
        self.cuts: dict[tuple[str, str], dict] = {}
        self.stopped: set[str] = set()
        self.cap_hit = False

    def run(self, view: _View, seed: str) -> dict[str, _Reach]:
        reach = {seed: _Reach(0, 1.0, 1.0, None, None)}
        self._admit(seed)
        frontier = [seed]
        for d in range(1, self.depth + 1):
            reached: dict[str, _Reach] = {}
            for node in sorted(frontier):
                here = reach[node]
                for e in view.callers(node):
                    caller = e["from"]
                    confidence = here.confidence * _conf(e.get("confidence"))
                    if confidence < self.floor - 1e-9:
                        key = (caller, node)
                        cut = {"from": caller, "to": node, "depth": d,
                               "confidence": round(confidence, 3),
                               "evidence": e.get("evidence")}
                        if key not in self.cuts or self.cuts[key]["confidence"] < cut["confidence"]:
                            self.cuts[key] = cut
                        continue
                    if caller in reach:
                        continue
                    best = reached.get(caller)
                    if best is not None and best.confidence >= confidence:
                        continue
                    reached[caller] = _Reach(d, confidence,
                                             min(here.floor, _conf(e.get("confidence"))),
                                             node, str(e.get("evidence") or ""))
            admitted: list[str] = []
            for caller in sorted(reached, key=lambda c: (-reached[c].confidence, c)):
                if self._admit(caller):
                    reach[caller] = reached[caller]
                    admitted.append(caller)
            frontier = admitted
            if not frontier:
                break
        else:
            for node in frontier:
                if view.callers(node):
                    self.stopped.add(node)
        return reach

    def _admit(self, node: str) -> bool:
        if node in self.nodes:
            return True
        if len(self.nodes) >= self.max_nodes:
            self.cap_hit = True
            return False
        self.nodes.add(node)
        return True


def _innermost(symbols: list[dict], line: int) -> dict | None:
    best = None
    for s in symbols:
        start, end = _int(s.get("start_line")), _int(s.get("end_line"))
        if start <= line <= end and (
            best is None or end - start < _int(best["end_line"]) - _int(best["start_line"])
        ):
            best = s
    return best


def _strictly_inside(symbols: list[dict], after: int, before: int) -> dict | None:
    """The innermost symbol an insertion between base lines `after` and
    `before` lands inside of (both lines in its range)."""
    best = None
    for s in symbols:
        start, end = _int(s.get("start_line")), _int(s.get("end_line"))
        if start <= after and before <= end and (
            best is None or end - start < _int(best["end_line"]) - _int(best["start_line"])
        ):
            best = s
    return best


def _trigger(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, **extra, "reason": reason}


def plan_impact(
    diff: Diff,
    *,
    base: Graph | None,
    head: Graph | None,
    document: Mapping[str, Any] | None,
    fresh: Mapping[str, Any],
    index_sha: str | None,
    depth: int = GRAPH_DEPTH_DEFAULT,
    min_confidence: float = MIN_CONFIDENCE,
    max_nodes: int = MAX_AFFECTED,
    policy: str | None = None,
) -> dict[str, Any]:
    """The test plan for `diff` over the base's graph (and the head's, if indexed).

    Pure apart from the shard reads `base` and `head` make: no forge, no
    Firestore, no clock, so the same inputs give the same answer.
    """
    depth = max(1, min(int(depth), DEPTH_MAX))
    base_view = _View(base) if base is not None else None
    head_view = _View(head) if head is not None else None
    views = [v for v in (base_view, head_view) if v is not None]
    languages = base_view.languages() if base_view else {
        str(r.get("language")): str(r.get("status"))
        for r in (document or {}).get("languages") or [] if isinstance(r, dict)
    }
    command_sources = frozenset(
        str(c["source"]) for c in (document or {}).get("commands") or [] if c.get("source")
    )

    triggers: list[dict[str, Any]] = []
    changed: dict[str, _Changed] = {}
    file_level: dict[str, str] = {}
    unindexed: list[dict[str, Any]] = []
    changed_test_files: dict[str, str] = {}
    diff_rows: list[dict[str, Any]] = []

    if fresh.get("stale") or fresh.get("state") == "none" or index_sha is None:
        triggers.append(_trigger(
            "stale_index",
            f"the index is stale: {fresh.get('reason')}" if fresh.get("stale")
            else f"no index can speak for this change: {fresh.get('reason') or 'none promoted'}",
        ))
    if diff.truncated:
        triggers.append(_trigger(
            "diff_truncated",
            "GitHub listed as many changed files as it ever lists; the rest of the diff "
            "was not read, so no selection can speak for it",
        ))

    def mark(view: _View, symbol: dict, side: str, lines: Iterable[int], why: str | None) -> None:
        sid = str(symbol["id"])
        entry = changed.get(sid)
        if entry is None:
            entry = changed[sid] = _Changed(symbol=symbol, side=side, view=view, why=why)
        entry.lines.update(lines)

    for f in sorted(diff.files, key=lambda x: (x.path, x.previous_path or "", x.status,
                                               x.patch or "")):
        path = f.path
        base_path = f.previous_path or path
        lang = language_of(path, views) or (language_of(base_path, views)
                                            if base_path != path else None)
        if is_build_config(path, command_sources) or (
                base_path != path and is_build_config(base_path, command_sources)):
            triggers.append(_trigger(
                "build_config_changed", f"{path} decides what the tests build and run against",
                path=path))
        if is_test_config(path) or (base_path != path and is_test_config(base_path)):
            triggers.append(_trigger(
                "test_config_changed", f"{path} decides how and which tests run", path=path))
        if is_shared_fixture(path) or (base_path != path and is_shared_fixture(base_path)):
            triggers.append(_trigger(
                "shared_fixture_changed",
                f"{path} is a shared fixture: any test may use it, and the graph does not "
                "follow fixtures", path=path))
        if lang is not None:
            status = languages.get(lang, "unsupported")
            if status in UNTRUSTED_LANGUAGE_STATUSES:
                known = lang in languages
                triggers.append(_trigger(
                    "unsupported_language",
                    f"{path} is {lang}, marked {status} in the index's languages table"
                    if known else f"{path} is {lang}, which the index has never measured",
                    path=path, language=lang, status=status))

        parsed = parse_patch(f.patch) if f.patch is not None else None
        diff_rows.append({
            "path": path, "status": f.status, "previous_path": f.previous_path,
            "patch": parsed is not None,
            "removed": _ranges(parsed.removed) if parsed else [],
            "added": _ranges(parsed.added) if parsed else [],
        })
        base_symbols = (base_view.in_file(base_path)
                        if base_view and f.status != "added" else [])
        head_symbols = (head_view.in_file(path)
                        if head_view and f.status != "removed" else [])
        is_source = lang is not None and lang in _GRAMMAR_EXTENSIONS.values() or bool(
            base_symbols or head_symbols)
        outside = False

        if parsed is None or f.status == "removed":
            why = "file removed" if f.status == "removed" else "GitHub sent no patch"
            for s in base_symbols:
                mark(base_view, s, "base", (), why)
            for s in head_symbols:
                mark(head_view, s, "head", (), why)
            if f.status != "removed" and head_view is None and is_source:
                unindexed.append({"path": path, "lines": None, "reason": why})
            if not base_symbols and not head_symbols:
                outside = True
        else:
            removed = parsed.removed
            if len(removed) > MAX_LINES_PER_FILE:
                for s in base_symbols:
                    mark(base_view, s, "base", (), "rewritten whole")
            else:
                for line in removed:
                    hit = _innermost(base_symbols, line)
                    if hit is None:
                        outside = True
                    else:
                        mark(base_view, hit, "base", (line,), None)
            for insertion in parsed.insertions:
                hit = _strictly_inside(base_symbols, insertion.after, insertion.before)
                if hit is not None:
                    mark(base_view, hit, "base", (), None)
            if head_view is not None:
                if len(parsed.added) > MAX_LINES_PER_FILE:
                    for s in head_symbols:
                        mark(head_view, s, "head", (), "rewritten whole")
                else:
                    for line in parsed.added:
                        hit = _innermost(head_symbols, line)
                        if hit is None:
                            outside = True
                        elif str(hit["id"]) not in changed:
                            mark(head_view, hit, "head", (line,), None)
            else:
                for run in parsed.runs:
                    if run.replaced:
                        placed = any(_innermost(base_symbols, line) for line in run.replaced)
                    else:
                        placed = _strictly_inside(base_symbols, run.after, run.before) is not None
                    if placed:
                        continue
                    if is_source and f.status != "removed":
                        unindexed.append({"path": path, "lines": _ranges(run.lines),
                                          "reason": "added lines no index has a symbol for"})
                    else:
                        outside = True
        if outside or any(u["path"] == path for u in unindexed):
            file_level[path] = ("changed outside any indexed symbol"
                                if not any(u["path"] == path for u in unindexed)
                                else "holds lines no index has seen")
        if f.status != "removed" and is_test_file(path, views):
            changed_test_files[path] = path

    for entry in unindexed:
        triggers.append(_trigger(
            "unindexed_symbol",
            f"{entry['path']} adds code no index has a symbol for "
            f"({entry['reason']}); its file falls back to file level",
            path=entry["path"]))

    # -- the walk -------------------------------------------------------------
    walk = _Walk(depth, min_confidence, max_nodes)
    candidates: dict[str, _Candidate] = {}
    affected: dict[str, dict[str, Any]] = {}
    unmapped: list[dict[str, Any]] = []

    def offer(c: _Candidate) -> None:
        current = candidates.get(c.test)
        if current is None or c.key() < current.key():
            candidates[c.test] = c

    for sid in sorted(changed):
        entry = changed[sid]
        view = entry.view
        if entry.symbol.get("kind") == "test":
            offer(_Candidate(sid, sid, "changed", None, None, 1.0, (), ()))
            continue
        reach = walk.run(view, sid)
        per_seed: dict[str, _Candidate] = {}

        def chain_of(node: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
            nodes, evidence = [], []
            cursor: str | None = node
            while cursor is not None and cursor != sid:
                r = reach[cursor]
                nodes.append(cursor)
                evidence.append(r.evidence or "")
                cursor = r.via
            return tuple(nodes), tuple(evidence)

        def take(c: _Candidate) -> None:
            current = per_seed.get(c.test)
            if current is None or c.key() < current.key():
                per_seed[c.test] = c
            offer(c)

        for node in sorted(reach):
            r = reach[node]
            info = view.symbol(node)
            nodes, evidence = chain_of(node)
            if node != sid:
                if info is not None and info.get("kind") == "test":
                    take(_Candidate(node, sid, "walk", r.confidence, r.depth, r.floor,
                                    nodes[1:], evidence))
                    continue
                row = affected.get(node)
                if row is None or (-r.confidence, r.depth) < (-row["confidence"], row["depth"]):
                    affected[node] = {
                        "id": node, "kind": (info or {}).get("kind"),
                        "path": (info or {}).get("path") or node.split("#", 1)[0],
                        "depth": r.depth, "confidence": round(r.confidence, 3),
                        "via": r.via, "evidence": r.evidence, "reaches": sid,
                    }
            for row in view.tests_for(node):
                rc = _conf(row.get("confidence"))
                take(_Candidate(
                    str(row["test"]), sid, "map", r.confidence * rc,
                    r.depth + max(_int(row.get("depth")), 1), min(r.floor, rc),
                    nodes, evidence + ("test_map",)))
        if not per_seed:
            unmapped.append({"symbol": sid, "path": entry.symbol.get("path"),
                             "reason": "no test reaches this changed symbol within the bound"})
        elif max(c.floor for c in per_seed.values()) < LOW_CONFIDENCE - 1e-9:
            triggers.append(_trigger(
                "low_confidence_only",
                f"every test that reaches {short(sid)} does so through an edge below "
                f"{LOW_CONFIDENCE}", symbol=sid, path=entry.symbol.get("path")))

    # -- the tests -----------------------------------------------------------
    tests: dict[str, dict[str, Any]] = {}
    for test_id in sorted(candidates):
        c = candidates[test_id]
        if c.source == "changed":
            reason = "changed in this diff"
        else:
            via = " → ".join(short(n) for n in c.chain if n != c.seed)
            through = f" via {via}" if via else " directly"
            if c.source == "map":
                through += " and the index's test map" if via else " through the index's test map"
            reason = f"reaches {short(c.seed)} (changed){through}, depth {c.depth}"
        tests[test_id] = {
            "id": test_id, "command": test_command(document, test_id), "reason": reason,
            "evidence": list(c.evidence),
            "confidence": round(c.confidence, 3) if c.confidence is not None else None,
            "source": c.source, "reaches": c.seed if c.source != "changed" else None,
        }

    def add_file_test(target: str, reason: str, evidence: list[str], source: str,
                      command: str | None = None) -> None:
        # A file-level target is dropped when a test inside it is already selected:
        # the narrower command runs what the plan has a reason for.
        if target in tests or any(t.split("#", 1)[0] == target for t in tests):
            return
        tests[target] = {"id": target, "command": command or test_command(document, target),
                         "reason": reason, "evidence": evidence, "confidence": None,
                         "source": source, "reaches": None}

    for path in sorted(changed_test_files):
        touched = [s for s, e in changed.items()
                   if e.symbol.get("path") == path and e.symbol.get("kind") == "test"]
        if not touched or path in file_level:
            add_file_test(path, "changed in this diff", [], "changed")

    file_paths = sorted(p for p in file_level if p not in changed_test_files)
    if file_paths and document:
        selected = select_tests(document, file_paths)
        for t in sorted(selected["tests"], key=lambda t: t["target"]):
            because = ", ".join(t["because"])
            add_file_test(t["target"],
                          f"file-level: {because} changed outside any indexed symbol; the "
                          f"index's test map links it ({t['evidence']})",
                          [t["evidence"]], "file", command=t.get("command"))
        for path in selected["unmapped"]:
            unmapped.append({"path": path, "reason": f"{file_level[path]}, and the index's "
                                                     "test map has no edge for it"})
    else:
        for path in file_paths:
            unmapped.append({"path": path, "reason": file_level[path]})
    for always in sorted((document or {}).get("always_tests") or [],
                         key=lambda a: str(a.get("target"))):
        add_file_test(str(always["target"]), f"always: {always.get('because')}", ["always"],
                      "always", command=always.get("command"))

    # -- the answer ------------------------------------------------------------
    triggers = sorted(
        {json.dumps(t, sort_keys=True): t for t in triggers}.values(),
        key=lambda t: (t["kind"], str(t.get("path") or ""), str(t.get("symbol") or "")),
    )
    total = total_tests(base) if base is not None else None
    full = None
    if triggers:
        full = {"command": full_suite_command(document),
                "because": sorted({t["kind"] for t in triggers})}
    test_rows = [tests[k] for k in sorted(tests)]
    changed_rows = [
        {"id": sid, "kind": e.symbol.get("kind"), "path": e.symbol.get("path"),
         "start_line": e.symbol.get("start_line"), "end_line": e.symbol.get("end_line"),
         "side": e.side, "lines": _ranges(e.lines), "why": e.why}
        for sid, e in sorted(changed.items())
    ]
    affected_rows = [affected[k] for k in sorted(affected)]
    cut_rows = sorted(walk.cuts.values(), key=lambda c: (c["to"], c["from"]))
    unmapped = sorted(unmapped, key=lambda u: (str(u.get("path")), str(u.get("symbol") or "")))
    listed_cut = [name for name, rows in (("affected", affected_rows), ("low_confidence_cut",
                  cut_rows), ("unmapped", unmapped)) if len(rows) > MAX_LISTED]
    return {
        "pull_request": diff.pull_request,
        "commit": diff.commit,
        "base_sha": diff.base_sha,
        "head_sha": diff.head_sha,
        "index_sha": index_sha,
        "graph_digest": base.digest if base is not None else None,
        "head_indexed": head is not None,
        "stale": bool(fresh.get("stale")),
        "freshness": dict(fresh),
        "policy": policy,
        "depth": depth,
        "min_confidence": min_confidence,
        "changed_symbols": len(changed_rows),
        "affected_callers": len(affected_rows),
        "targeted": len(test_rows),
        "selected": (total if total is not None else len(test_rows)) if full else len(test_rows),
        "total_tests": total,
        "selection": "full_suite" if full else "targeted",
        "full_suite": full,
        "diff": diff_rows,
        "diff_truncated": diff.truncated,
        "changed": changed_rows,
        "affected": affected_rows[:MAX_LISTED],
        "tests": test_rows,
        "fallback_triggers": triggers,
        "unmapped": unmapped[:MAX_LISTED],
        "unindexed": sorted(unindexed, key=lambda u: (u["path"], json.dumps(u["lines"]))),
        "low_confidence_cut": cut_rows[:MAX_LISTED],
        "bound": {
            "depth": depth,
            "max_nodes": max_nodes,
            "node_cap_hit": walk.cap_hit,
            "stopped_at_depth": sorted(walk.stopped)[:MAX_LISTED],
        },
        "lists_cut": listed_cut,
    }


# --------------------------------------------------------------------------
# the graph explorer's reads (§6.1)
# --------------------------------------------------------------------------

def package_of(module: str) -> str:
    """`?cluster=package`: a module's first two path segments (`apps/swarm-api`,
    `src/shop`) -- one segment for a module that has only one (`src`)."""
    if module == ".":
        return "."
    parts = module.split("/")
    return "/".join(parts[:2]) if parts[0] in ("apps", "packages", "services", "libs",
                                               "cmd", "internal", "modules") else parts[0]


def module_graph(graph: Graph, document: Mapping[str, Any] | None,
                 cluster: str = "module") -> dict[str, Any]:
    """Modules and weighted module edges, with hot-spot and test-reach figures.

    Reads every symbols, tests and callees shard once: this is the whole
    repository's picture, drawn once per version.
    """
    group = package_of if cluster == "package" else (lambda m: m)
    modules: dict[str, dict[str, Any]] = {}
    reached: dict[str, set[str]] = {}
    for module in graph.modules("symbols"):
        rows = graph.symbols(module)
        key = group(module)
        entry = modules.setdefault(key, {"id": key, "modules": 0, "symbols": 0, "tests": 0,
                                         "hot_spot_changes": 0, "languages": set()})
        entry["modules"] += 1
        entry["symbols"] += len(rows)
        entry["tests"] += sum(1 for s in rows if s.get("kind") == "test")
        entry["languages"].update(str(s.get("language")) for s in rows if s.get("language"))
        reached.setdefault(key, set()).update(
            str(t.get("symbol")) for t in graph.shard("tests", module))
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for module in graph.modules("callees"):
        for e in graph.shard("callees", module):
            frm, to = e.get("from"), e.get("to")
            if not isinstance(frm, str) or not isinstance(to, str) or to.startswith("external:"):
                continue
            a, b = group(module_of(frm)), group(module_of(to))
            if a == b or b not in modules:
                continue
            row = edges.setdefault((a, b), {"from": a, "to": b, "weight": 0, "kinds": {},
                                            "max_confidence": 0.0})
            row["weight"] += 1
            kind = str(e.get("kind"))
            row["kinds"][kind] = row["kinds"].get(kind, 0) + 1
            row["max_confidence"] = max(row["max_confidence"], _conf(e.get("confidence")))
    for spot in (document or {}).get("hot_spots") or []:
        key = group(module_of(str(spot.get("path"))))
        if key in modules:
            modules[key]["hot_spot_changes"] += _int(spot.get("changes"))
    out = []
    for key in sorted(modules):
        entry = modules[key]
        non_tests = entry["symbols"] - entry["tests"]
        entry["test_reach"] = round(len(reached.get(key, set())) / non_tests, 3) \
            if non_tests > 0 else None
        entry["languages"] = sorted(entry["languages"])
        out.append(entry)
    return {
        "cluster": cluster,
        "modules": out,
        "edges": [dict(edges[k], kinds=dict(sorted(edges[k]["kinds"].items())),
                       max_confidence=round(edges[k]["max_confidence"], 3))
                  for k in sorted(edges)],
        "counts": dict(graph.manifest.get("counts") or {}),
        "truncated": list(graph.manifest.get("truncated") or []),
    }


def check_symbol_id(symbol_id: str) -> str:
    if not _SYMBOL_ID.match(symbol_id or ""):
        raise ValidationFailed("a symbol id is 1-500 printable characters")
    return symbol_id


def _symbol_row(s: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": s.get("id"), "kind": s.get("kind"), "path": s.get("path"),
            "start_line": s.get("start_line"), "end_line": s.get("end_line"),
            "language": s.get("language")}


def search_symbols(graph: Graph, q: str, limit: int = SEARCH_LIMIT_DEFAULT) -> dict[str, Any]:
    """Symbols whose id contains `q`, case-insensitively, in id order."""
    needle = q.lower()
    hits: list[dict[str, Any]] = []
    more = False
    for module in graph.modules("symbols"):
        for s in graph.symbols(module):
            if needle in str(s.get("id")).lower():
                hits.append(_symbol_row(s))
    hits.sort(key=lambda s: str(s["id"]))
    if len(hits) > limit:
        hits, more = hits[:limit], True
    return {"q": q, "symbols": hits, "more": more}


def neighbourhood(graph: Graph, symbol_id: str, *, depth: int = NEIGHBOURHOOD_DEPTH_DEFAULT,
                  direction: str = "both") -> dict[str, Any]:
    """The call graph centred on one symbol: callers, callees or both, to `depth`."""
    view = _View(graph)
    centre = view.symbol(symbol_id)
    if centre is None:
        raise NotFound(f"no symbol {symbol_id[:200]!r} in this graph")
    nodes: dict[str, int] = {symbol_id: 0}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    truncated = False
    frontier = [symbol_id]
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for node in sorted(frontier):
            steps: list[tuple[str, dict]] = []
            if direction in ("callers", "both"):
                steps += [(e["from"], e) for e in view.callers(node)]
            if direction in ("callees", "both"):
                steps += [(e["to"], e) for e in view.callees(node)]
            for other, e in steps:
                key = (str(e["from"]), str(e["to"]), str(e.get("kind")))
                if other not in nodes:
                    if len(nodes) >= MAX_NEIGHBOURHOOD_NODES:
                        truncated = True
                        continue
                    nodes[other] = d
                    nxt.append(other)
                edges[key] = {"from": key[0], "to": key[1], "kind": key[2],
                              "evidence": e.get("evidence"),
                              "also_evidence": sorted(e.get("also_evidence") or []),
                              "confidence": round(_conf(e.get("confidence")), 3)}
        frontier = nxt
        if not frontier:
            break
    node_rows = []
    for node in sorted(nodes):
        info = view.symbol(node)
        row = _symbol_row(info) if info is not None else {"id": node, "kind": "external"
                                                          if node.startswith("external:")
                                                          else None}
        row["distance"] = nodes[node]
        node_rows.append(row)
    return {"symbol": _symbol_row(centre), "depth": depth, "direction": direction,
            "nodes": node_rows, "edges": [edges[k] for k in sorted(edges)],
            "truncated": truncated}


def symbol_tests(graph: Graph, document: Mapping[str, Any] | None,
                 symbol_id: str) -> dict[str, Any]:
    """One symbol's test map: the tests that reach it, with depth and path confidence."""
    view = _View(graph)
    centre = view.symbol(symbol_id)
    if centre is None:
        raise NotFound(f"no symbol {symbol_id[:200]!r} in this graph")
    rows = [{"test": row["test"], "depth": row.get("depth"),
             "confidence": round(_conf(row.get("confidence")), 3),
             "command": test_command(document, str(row["test"]))}
            for row in view.tests_for(symbol_id)]
    return {"symbol": _symbol_row(centre), "tests": rows}


def languages_table(graph: Graph | None, document: Mapping[str, Any] | None
                    ) -> list[dict[str, Any]]:
    """Per language: grammar, server, status and fallback. The graph's table when
    there is one (the extractor's measurement), else the index document's."""
    rows = (graph.manifest.get("languages") if graph is not None else None) \
        or (document or {}).get("languages") or []
    keep = ("language", "files", "grammar", "server", "status", "reason", "fallback",
            "parsed", "timed_out", "failed", "too_large", "over_budget")
    return sorted(({k: row.get(k) for k in keep if k in row} for row in rows
                   if isinstance(row, dict)), key=lambda r: str(r.get("language")))


# --------------------------------------------------------------------------
# the service: the forge read, the versions, the stored plan
# --------------------------------------------------------------------------

def plan_id_for(tenant_id: str, repo_id: str, base: str | None, head: str, depth: int,
                index_sha: str | None, graph_digest: str | None) -> str:
    key = "|".join([tenant_id, repo_id, base or "", head, str(depth), index_sha or "",
                    graph_digest or ""])
    return "ip_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _repo_api(record: Mapping[str, Any]) -> str:
    return (f"https://{GITHUB_API_HOST}/repos/{quote(record['owner'], safe='')}/"
            f"{quote(record['repo'], safe='')}")


def _json(forge: GitHubIssues, url: str, token: str, what: str) -> Any:
    # `_get` is the client's own status mapping and host pin (repoindex does
    # the same); the token goes to its one header and nowhere else.
    raw = forge._get(url, token, what)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise IssueReadFailed(f"GitHub's answer for {what} is larger than this read holds")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise IssueReadFailed(f"GitHub's answer for {what} is not JSON") from None


def _sha_of(value: Any) -> str | None:
    sha = value.get("sha") if isinstance(value, dict) else None
    return sha if isinstance(sha, str) and _SHA.match(sha) else None


def _files(entries: Iterable[Any], literals: tuple[str, ...]) -> tuple[DiffFile, ...]:
    out: list[DiffFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = _clean_path(entry.get("filename"), literals)
        if path is None:
            continue
        previous = _clean_path(entry.get("previous_filename"), literals) \
            if entry.get("previous_filename") else None
        patch = entry.get("patch") if isinstance(entry.get("patch"), str) else None
        status = str(entry.get("status") or "modified")
        out.append(DiffFile(path=path, status=status, previous_path=previous, patch=patch))
    return tuple(out)


def read_diff(record: Mapping[str, Any], request: ImpactRequest, token: str, *,
              forge: GitHubIssues) -> Diff:
    """The change's files and hunks, from GitHub. Nothing is checked out."""
    api = _repo_api(record)
    name = f"{record['owner']}/{record['repo']}"
    literals = (token,)
    if request.pull_request is not None:
        number = request.pull_request
        pr = _json(forge, f"{api}/pulls/{number}", token, f"{name}#{number}")
        base, head = _sha_of((pr or {}).get("base")), _sha_of((pr or {}).get("head"))
        if head is None:
            raise IssueReadFailed(f"GitHub's answer for {name}#{number} names no head commit")
        entries, cut = forge._paged(
            f"{api}/pulls/{number}/files", token, f"the changed files of {name}#{number}",
            keep=lambda e: isinstance(e, dict) and isinstance(e.get("filename"), str),
            cap=MAX_PR_FILES, max_pages=PR_FILE_PAGES,
        )
        return Diff(base_sha=base, head_sha=head, files=_files(entries, literals),
                    pull_request=number, truncated=cut)
    if request.commit is not None:
        head = request.commit
        commit = _json(forge, f"{api}/commits/{head}", token, f"commit {head[:12]} of {name}")
        parents = [p for p in ((commit or {}).get("parents") or []) if _sha_of(p)]
        if not parents:
            # A root commit: its own files are the whole change.
            entries = (commit or {}).get("files") or []
            return Diff(base_sha=None, head_sha=head, files=_files(entries, literals),
                        commit=head, truncated=len(entries) >= COMPARE_FILE_CAP)
        base = _sha_of(parents[0])
    else:
        base, head = request.base, request.head
    assert base is not None and head is not None
    compare = _json(forge, f"{api}/compare/{base}...{head}", token,
                    f"the changes between {base[:12]} and {head[:12]} of {name}")
    entries = (compare or {}).get("files") or [] if isinstance(compare, dict) else []
    return Diff(base_sha=base, head_sha=head, files=_files(entries, literals),
                commit=request.commit, truncated=len(entries) >= COMPARE_FILE_CAP)


def resolve_read_token(db: Any, tenant: Tenant, tenant_doc: Tenant | None, repo_id: str, *,
                       tokens: ForgeTokens, now: datetime) -> tuple[str, dict[str, Any]]:
    """R2 for a read: the repository's token, else the tenant default.

    (value, label). The label names the slot -- scope, token id, secret name
    -- and never the value. A tenant with no registry record at all reads
    with its `-git` slot, which is what every other forge read in swarm-api
    uses; a tenant whose default is revoked or expired is refused, naming the
    scopes tried.
    """
    registry = GitTokens(db, now=lambda: now)
    registry.ensure_tenant_default(tenant_doc)
    records = registry.list(tenant.tenant_id)
    resolution = resolve_r2(records, tenant_id=tenant.tenant_id, repo_id=repo_id, user=None,
                            now=now)
    record = resolution.credential
    if record is None:
        if any(r.scope is Scope.TENANT for r in records if r.tenant_id == tenant.tenant_id):
            raise _forge.NoForgeCredential(
                "no usable git token for this repository: " + "; ".join(resolution.tried)
            )
        label = {"scope": "tenant", "token_id": None,
                 "secret_name": tenant.secret_name(_forge.GIT_PROVIDER)}
        return tokens.token_for(tenant), label
    label = {"scope": record.scope.value, "token_id": record.token_id,
             "secret_name": record.secret_name}
    if record.provider_suffix == _forge.GIT_PROVIDER:
        return tokens.token_for(tenant), label
    reader = getattr(tokens, "read_slot", None)
    if reader is None:
        raise IssueReadFailed(f"swarm-api has no reader for {record.secret_name}")
    return reader(tenant, record.provider_suffix).value, label


class ImpactService:
    """The impact query for one tenant's registration, and its stored plans."""

    def __init__(self, db: Any, *, index: RepoIndex, graphs: RepoGraph, tokens: ForgeTokens,
                 forge: GitHubIssues, now: Callable[[], datetime]) -> None:
        self._db = db
        self.index = index
        self.graphs = graphs
        self._tokens = tokens
        self._forge = forge
        self._now = now

    @classmethod
    def from_context(cls, ctx: Any) -> "ImpactService":
        return cls(ctx.db, index=RepoIndex.from_context(ctx),
                   graphs=RepoGraph.from_inspection(ctx.inspection),
                   tokens=ctx.forge_tokens, forge=ctx.forge, now=ctx.now)

    # -- versions ------------------------------------------------------------
    def kept_version(self, tenant_id: str, repo_id: str, sha: str | None
                     ) -> dict[str, Any] | None:
        if not sha:
            return None
        try:
            return self.index.version(tenant_id, repo_id, sha)
        except NotFound:
            return None

    def version_and_freshness(self, tenant_id: str, repo_id: str, sha: str | None = None
                              ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
        """(registration, version, freshness): the current version, or a kept one."""
        record = self.index.registrations.get(tenant_id, repo_id)
        version = self.index.version(tenant_id, repo_id, sha)
        state = dict(record.get("index") or {})
        if version is not None and version.get("commit_sha") != state.get("current_sha"):
            state.update(current_sha=version.get("commit_sha"),
                         current_built_at=version.get("built_at"))
        return record, version, freshness(state, now=self._now())

    def open(self, tenant_id: str, repo_id: str, version: Mapping[str, Any] | None
             ) -> Graph | None:
        if version is None or not version.get("graph_digest"):
            return None
        try:
            return self.graphs.open(tenant_id, repo_id, version)
        except NoGraph:
            return None

    # -- the query -------------------------------------------------------------
    def _read_diff(self, record: Mapping[str, Any], request: ImpactRequest, tenant: Tenant,
                   tenant_doc: Tenant | None) -> tuple[Diff, dict[str, Any]]:
        token, label = resolve_read_token(self._db, tenant, tenant_doc, record["repo_id"],
                                          tokens=self._tokens, now=self._now())
        try:
            return read_diff(record, request, token, forge=self._forge), label
        finally:
            token = ""

    def query(self, record: Mapping[str, Any], request: ImpactRequest, *, tenant: Tenant,
              tenant_doc: Tenant | None) -> dict[str, Any]:
        tenant_id, repo_id = tenant.tenant_id, record["repo_id"]
        diff, label = self._read_diff(record, request, tenant, tenant_doc)
        # The nearest indexed commit: the base's own kept version, else the
        # current one, whose freshness the plan carries either way.
        base_version = self.kept_version(tenant_id, repo_id, diff.base_sha)
        state = dict(record.get("index") or {})
        if base_version is None:
            base_version = self.index.version(tenant_id, repo_id)
        elif base_version.get("commit_sha") != state.get("current_sha"):
            state.update(current_sha=base_version.get("commit_sha"),
                         current_built_at=base_version.get("built_at"))
        fresh = freshness(state, now=self._now())
        head_version = self.kept_version(tenant_id, repo_id, diff.head_sha)
        base_graph = self.open(tenant_id, repo_id, base_version)
        head_graph = self.open(tenant_id, repo_id, head_version) if head_version else None
        document = self.index.read_version(tenant_id, base_version) if base_version else None
        settings = dict(record.get("graph") or {})
        depth = request.depth or settings.get("depth") or GRAPH_DEPTH_DEFAULT
        floor = settings.get("min_confidence") or MIN_CONFIDENCE
        index_sha = base_version.get("commit_sha") if base_version else None
        if base_version is not None and base_graph is None:
            # Promoted without a graph: every changed source file is code no
            # index has symbols for, which is §4.3a's `unindexed`.
            diff = Diff(base_sha=diff.base_sha, head_sha=diff.head_sha,
                        files=tuple(DiffFile(path=f.path, status="added"
                                             if f.status != "removed" else f.status,
                                             previous_path=f.previous_path, patch=None)
                                    for f in diff.files),
                        pull_request=diff.pull_request, commit=diff.commit,
                        truncated=diff.truncated)
        plan = plan_impact(
            diff, base=base_graph, head=head_graph, document=document, fresh=fresh,
            index_sha=index_sha, depth=int(depth), min_confidence=float(floor),
            policy=(record.get("selection_policy") or {}).get("policy"),
        )
        plan_id = plan_id_for(tenant_id, repo_id, diff.base_sha, diff.head_sha, plan["depth"],
                              index_sha, plan["graph_digest"])
        plan = {"plan_id": plan_id, **plan, "read_with": label}
        self._store(tenant_id, repo_id, plan)
        return plan

    def _store(self, tenant_id: str, repo_id: str, plan: Mapping[str, Any]) -> None:
        now = self._now()
        body = dict(plan)
        whole = True
        if len(json.dumps(body, sort_keys=True, default=str)) > MAX_STORED_PLAN_BYTES:
            whole = False
            for key in ("affected", "low_confidence_cut", "unmapped", "diff", "changed"):
                body[key] = []
        ref = self._db.collection(PLANS_COLLECTION).document(plan["plan_id"])
        previous = ref.get()
        created_at = (previous.to_dict() or {}).get("created_at") if previous.exists else None
        ref.set({
            "tenant_id": tenant_id,
            "repo_id": repo_id,
            "pull_request": plan["pull_request"],
            "commit": plan["commit"],
            "base_sha": plan["base_sha"],
            "head_sha": plan["head_sha"],
            "index_sha": plan["index_sha"],
            "graph_digest": plan["graph_digest"],
            "depth": plan["depth"],
            "policy": plan["policy"],
            "plan": body,
            "stored_whole": whole,
            # swarm-api writes no objects (objectViewer); the plan is above.
            "plan_object": None,
            "selected": plan["selected"],
            "total_tests": plan["total_tests"],
            "selection": plan["selection"],
            "fallback_triggers": plan["fallback_triggers"],
            # Set by the selected-tests check (lane RI12), never here.
            "check_run_id": None,
            "conclusion": None,
            "created_at": created_at or now,
            "updated_at": now,
        })


__all__ = [
    "DEPTH_MAX", "Diff", "DiffFile", "ImpactRequest", "ImpactService", "LOW_CONFIDENCE",
    "MAX_AFFECTED", "MIN_CONFIDENCE", "PLANS_COLLECTION", "UNTRUSTED_LANGUAGE_STATUSES",
    "check_symbol_id", "full_suite_command", "is_build_config", "is_shared_fixture",
    "is_test_config", "languages_table", "module_graph", "neighbourhood", "parse_patch",
    "plan_id_for", "plan_impact", "read_diff", "resolve_read_token", "search_symbols",
    "symbol_tests", "test_command", "total_tests",
]
