"""The LSP pass: ask each language server what the tree-sitter pass guessed.

docs/repo-index.md §3.5 step 2 (revised 2026-10-04, owner). For each
language the repository has, its server (servers.py) is started headless
and asked, file by file:

  textDocument/definition         at every candidate call site step 1 found
                                  (a call, a base class, a route's handler).
                                  A location that is a known symbol's name
                                  becomes an `lsp` edge from the site's
                                  caller to it
  callHierarchy/incomingCalls     for every exported function and method,
                                  where the server has call hierarchy
                                  (pyright, tsserver, gopls): callers the
                                  syntax could not see -- a call through a
                                  re-export, an alias, a variable
  textDocument/references         for terraform-ls, which has no call
                                  hierarchy: who references each variable,
                                  resource, data source, module and output

A site the server cannot resolve -- no answer, an answer outside the
repository, a location that is not a symbol's name -- stays `ast`, at its
lower confidence. Confidence follows §2.5: 0.95, or 0.8 for a call made
through a receiver (`obj.method()` where `obj` is neither the enclosing
class nor an imported module), which the server can only have resolved
through the type it inferred for `obj`.

THE FALLBACKS. One server per language, run one at a time, cheapest first
(the indexer's memory is sized for the largest server, not for four at
once):

  ok           the server answered to the end; its edges are kept
  partial      its budget ran out in phase 1 (below) after it had finished
               some files: the edges of every file whose sites were all
               asked are kept, the rest of the language is `ast`, and the
               language's `covered` names the finished files (#1693)
  timed_out    it ran past its budget before it finished any file with a
               site (§3.5's table, by repository size), or stopped
               answering -- `max_consecutive_timeouts` requests in a row
               each over the per-request timeout
  failing      it is not installed, could not start, refused `initialize`,
               exited or crashed, or exceeded its memory limit
  unsupported  the repository has the language and no server is listed

A server that is stopped -- hung, crashed, over its memory -- keeps NONE of
its answers: the language is then uniformly `ast`, which is what its status
tells a consumer. A budget cut is different, and keeps what it finished: the
files are asked one at a time in a fixed order, so the files before the one
the budget ran out in were answered whole, by a server that was still
answering, and the language says which they are. That is what stops a graph
being `lsp` in the files the server reached and `ast` in the rest with
nothing to say which: the extractor marks each file row `lsp: true` or
`false` from `covered`, and impact trusts a changed file of a `partial`
language only when its row says it was covered. On ai-studio on 2026-10-20
the old rule threw away 7,106 resolved sites from a healthy pyright that had
simply run out of its 270-second share (#1693). The run still succeeds; a
timeout is never a failed index, it is a less certain one, and says so.

THE TOTAL BUDGET. §3.5's per-server budget is a cap, not a grant: the
servers run one after another inside ONE indexer step, which swarm-api
kills at the full-run timeout (apps/swarm-api/swarm_api/repoindex.py), and
that timeout also has to cover the agent and the tree-sitter pass. A step
killed there produces no index at all, which is the one outcome §3.5 rules
out. So the pass as a whole gets `total_budget_seconds` -- the extractor's
`--lsp-total-budget-seconds`, which the worker sizes from the run's
timeout (agent_worker/indexrun.py `budgets`) -- and each server, when its
turn comes, is granted the smaller of its §3.5 budget and an equal share of
what is left of the total among the servers still to run. Time a server
does not use passes to the ones after it. A server whose turn comes after
the total has run out is not started: its language is `timed_out`, with
that reason.

THE CHEAPEST SERVER RUNS FIRST (#2991). Each server's NEED
(`planned_needs`) is what it cost in the previous full run
(`LspOptions.server_costs`, which swarm-api reads from the promoted graph's
manifest and names in the signed prompt), or, for a server with no
measurement, the equal share of the total. The needs set the ORDER
(`run_order`), cheapest first, not the size of the grants. The grants
were once in proportion to the needs (lane INDEX-BUDGETS, #1693), so a need
several times the whole pass took nearly all of it whichever server it
was: on SwarmCloud's own registration on 2026-11-01 pyright, first by
name, was granted 1,175.5 s of the 1,260-second pass and reached 588 of
2,334 files, tsserver had 71.4 s for 304 of 1,130 files and terraform-ls
10.1 s for 56 of 173, and every language was `partial`. Cheapest first on
equal shares is max-min fair: a server that can finish in its share does,
and leaves the rest to the next; a server slower than it measured takes at
most its share from the ones after it; and the most expensive, last, takes
everything the others left -- 1,260 s less tsserver's and terraform-ls's
few minutes, against the 1,175.5 s it had. With no history at all every
need is equal, the order is by name and this is the equal split exactly.
Every run records each server's `grant_seconds` and `cost_seconds` in its
counts for the next one (`measured_cost`): what it used when it finished,
or, when its budget cut it, its grant scaled by the sites it had over the
requests it managed -- pyright's 270 s x 86,805 / 26,562 is ~882 s. The
cost is recorded in 10-second steps, so two runs that got the same answers
still write the same bytes.

TWO PHASES. Every site's definition is asked first, across all files; the
workspace queries (call hierarchy, references) run after. A workspace query
searches the whole repository -- pyright's incomingCalls took up to 60 s a
symbol on this repository on 2026-10-05, against milliseconds a definition
-- so a timed-out one is a slow answer and never counts toward
`max_consecutive_timeouts`. Phase 2 is supplementary, and on a large
workspace it would run until the budget ran out every time, so it gets at
most `workspace_share` of the server's PROPORTION of what is left -- its
need over the needs still to run -- and never more than that share of its
grant (`workspace_seconds`): an equal grant is for a cheap server's
definitions, not for its incomingCalls, and what phase 2 leaves unused goes
to the next server's phase 1, the per-site answer §3.5 is for. Its limit
running out ends phase 2 only: the language stays `ok`, keeps every
definition edge, and its reason says how many symbols were asked ("call
hierarchy and references cut at their N-second share of the M-second
budget: a of b symbols asked"). The budget running out in phase 1 is
`partial` as above ("pyright reached N of M files (K of S sites) in its
270-second budget"), or `timed_out` when no file with a site was finished.

Why a budget cut in phase 2 keeps phase 1's edges while a crash or a
memory stop in phase 2 keeps none: a cut is the driver's own decision,
taken after every definition was answered by a server that was still
healthy, so those answers are complete and trustworthy. A server that
crashed or was stopped over its memory may have been failing before it
died -- a pyright out of memory answers from a half-built program -- and
nothing in the protocol says from which answer on, so its language falls
back to `ast` whole, as above.

LESS WORK (lane INDEX-SITES, part of #1693). Two kinds of request are
never sent, because no answer to them could be kept or used:

  a site whose name is neither the simple name of a symbol anywhere in the
  repository nor bound by an import in its own file (`Site.imported`):
  `rows.append()`, `os.getcwd()`, `len()`. A definition becomes an edge
  only at a location that is a known symbol's name (`_symbol_named_at`),
  and a call's name reaches a symbol of another name only through a
  renaming import (`fetch as get_row`), so such a site's answer is
  discarded every time. It stays `ast` and is counted
  as `sites_skipped_external`, never as `unresolved`
  a file under the platform's exclusions (servers.py LSP_EXCLUDED_PATHS:
  migrations, alembic revisions, docs/): tree-sitter parsed it and its
  `ast` edges stand, but it is never opened -- neither phase asks about
  it, and it is not `covered`. Counted as `files_excluded` and
  `sites_excluded`

Neither is counted in a cut's "N of M files (K of S sites)", nor in the
sites `measured_cost` scales a cut server's grant by: they are not work
the server was given.

THE ENVIRONMENT. A server reads the checkout, which is untrusted input, so
it gets a minimal environment built here -- PATH, a throwaway HOME and
caches, the locale and the spec's own variables -- and none of the
indexer's: a credential in the step's environment never reaches it. Its
stderr is discarded and its messages are never copied into the index; a
reason in `languages` is written here, from codes and counts.

THE FIRST QUERY. A server loads the whole workspace before it answers its
first query (§3.5: "most of their cost"), so that one query is bounded by
`warmup_timeout_seconds` (default: the rest of the server's budget), not by
the 10-second per-request limit, or every large repository would time out
on its first question.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .client import (BudgetExceeded, LspClient, RequestTimeout, ServerError,
                     ServerExited)
from .servers import DEFAULT_BIN_DIR, LSP_EXCLUDED_PATHS, SERVERS, ServerSpec, lsp_excluded

# §2.5's confidences for `lsp` evidence.
LSP_DECLARED = 0.95
LSP_INFERRED = 0.8

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_TIMED_OUT = "timed_out"
STATUS_FAILING = "failing"
STATUS_UNSUPPORTED = "unsupported"
NO_SERVER_REASON = "no language server; edges are syntactic"

# §3.5's budget table: source files -> seconds per language server.
SERVER_BUDGETS = ((2_000, 600), (10_001, 1_800))
LARGEST_SERVER_BUDGET = 2_700
# §3.5's full-run budget, same rows. The LSP pass gets half of it by default:
# the step also runs the agent and the tree-sitter pass, and under 2,000 files
# swarm-api's 1,800-second step timeout is already past the 20-minute row, so
# the other half is the margin that keeps a slow server from costing the
# whole index (module doc, "the total budget").
FULL_RUN_BUDGETS = ((2_000, 1_200), (10_001, 3_600))
LARGEST_FULL_RUN_BUDGET = 7_200
LSP_TOTAL_SHARE = 0.5
# `cost_seconds` is recorded rounded up to this step (module doc, "the
# shares follow what each server cost last time"): fine enough for budgets
# of 600-2,700 s, coarse enough that the timing noise of a run that got the
# same answers does not change the graph's bytes.
COST_STEP_SECONDS = 10
# A server's peak resident memory is recorded rounded up to this step
# (`rss_mib`): what deciding to run servers side by side needs (#2991),
# coarse enough that two runs that got the same answers write the same bytes.
RSS_STEP_MIB = 256
# Phase 2's share of a server's grant: half, so a slow incomingCalls never
# takes more than half of what a server was granted and the rest passes on.
WORKSPACE_SHARE = 0.5
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_CONSECUTIVE_TIMEOUTS = 3

# Without a container limit to read, a server gets 4 GiB: pyright on a large
# repository holds several. With one, three quarters of it, so the server is
# stopped here, with a reason, before the kernel's OOM killer stops the whole
# step without one.
DEFAULT_MEMORY_MIB = 4_096
MEMORY_SHARE = 0.75
CGROUP_LIMIT_PATHS = (Path("/sys/fs/cgroup/memory.max"),
                      Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))

CALLABLE_KINDS = ("function", "method")
_VERSION = re.compile(r"[\w.+-]{1,40}")
_WORD = re.compile(r"[\w$][\w$-]*")
_DEFINING = re.compile(
    r"(?:\bdef|\bfunction\*?|\bfunc(?:\s*\([^)]*\))?|\bclass|\btype|\binterface|\benum"
    r"|\bconst|\blet|\bvar|\bstruct)\s+$")


def server_budget_seconds(source_files: int) -> int:
    """§3.5's per-server budget for a repository of `source_files`."""
    for below, seconds in SERVER_BUDGETS:
        if source_files < below:
            return seconds
    return LARGEST_SERVER_BUDGET


def total_budget_seconds(source_files: int) -> int:
    """The whole LSP pass's default budget: LSP_TOTAL_SHARE of §3.5's full run."""
    full = LARGEST_FULL_RUN_BUDGET
    for below, seconds in FULL_RUN_BUDGETS:
        if source_files < below:
            full = seconds
            break
    return int(full * LSP_TOTAL_SHARE)


def planned_needs(names: Sequence[str], costs: Mapping[str, float] | None,
                  total: float) -> list[float]:
    """Each server's need, in run order: its measured cost, or the equal share."""
    equal = total / len(names) if names else 0.0
    needs = []
    for name in names:
        cost = (costs or {}).get(name)
        known = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0
        needs.append(float(cost) if known else equal)
    return needs


def run_order(names: Sequence[str], needs: Sequence[float]) -> list[int]:
    """The servers' run order, as indices: the cheapest need first, ties by name.

    Module doc, "the cheapest server runs first": the servers that can
    finish do, and the one that cannot takes what they leave.
    """
    return sorted(range(len(names)), key=lambda i: (needs[i], names[i]))


def grant_seconds(left: float, needs: Sequence[float], budget: float) -> float:
    """The grant of the first of `needs`: an equal share of `left`, capped at `budget`.

    `needs` are the servers still to run, cheapest first (`run_order`); the
    first gets `left` over how many they are, and what it does not use
    passes on, so the last gets everything left.
    """
    if left <= 0 or not needs:
        return 0.0
    return min(budget, left / len(needs))


def workspace_seconds(left: float, needs: Sequence[float], budget: float, share: float) -> float:
    """Phase 2's limit for the first of `needs`: `share` of its proportion of `left`.

    The proportion is the server's need over all the needs still to run,
    capped at `budget` and at the equal grant (`grant_seconds`): phase 2
    is supplementary, and a cheap server's equal grant is not spent on
    workspace queries that the expensive server's definitions need.
    """
    if left <= 0 or not needs:
        return 0.0
    whole = sum(needs)
    proportion = left * needs[0] / whole if whole > 0 else left / len(needs)
    return share * min(proportion, grant_seconds(left, needs, budget))


def measured_cost(status: str | None, *, elapsed: float, grant: float, requests: int,
                  sites: int) -> int | None:
    """What a server needed, in COST_STEP_SECONDS steps; None when nothing was measured.

    Finished (`ok`): the time it used. Cut by its budget (`partial`,
    `timed_out`): its grant scaled by the sites it had over the requests it
    made -- in phase 1 every request is a site's definition -- never less
    than the grant. `failing`, or a cut that asked nothing: no measurement.
    """
    if status == STATUS_OK:
        need = elapsed
    elif status in (STATUS_PARTIAL, STATUS_TIMED_OUT) and requests > 0 and grant > 0:
        need = grant * max(1.0, sites / requests)
    else:
        return None
    steps = max(1, -(-int(need * 1000) // (COST_STEP_SECONDS * 1000)))
    return steps * COST_STEP_SECONDS


def rss_mib(peak_bytes: int) -> int:
    """A server's peak resident memory in MiB, rounded up to RSS_STEP_MIB; 0 when none was seen."""
    if peak_bytes <= 0:
        return 0
    step = RSS_STEP_MIB * 1024 * 1024
    return -(-int(peak_bytes) // step) * RSS_STEP_MIB


def _seconds(value: float) -> str:
    return f"{round(value, 1):g}"


def default_memory_limit_mib(paths: Iterable[Path] = CGROUP_LIMIT_PATHS) -> int:
    """Three quarters of the container's memory limit, or 4 GiB without one."""
    for path in paths:
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if not raw.isdigit():
            continue
        limit = int(raw)
        # cgroup v1 reports "no limit" as a huge number, not as "max".
        if 0 < limit < (1 << 50):
            return max(1, int(limit * MEMORY_SHARE) // (1024 * 1024))
    return DEFAULT_MEMORY_MIB


@dataclass(frozen=True)
class LspOptions:
    servers: Mapping[str, ServerSpec] | None = None
    bin_dir: Path = DEFAULT_BIN_DIR
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    warmup_timeout_seconds: float | None = None
    server_budget_seconds: float | None = None
    total_budget_seconds: float | None = None
    #: What each server (by name) cost in the previous full run, in seconds:
    #: the shares of the total follow it (`planned_needs`). None: the equal split.
    server_costs: Mapping[str, float] | None = None
    workspace_share: float = WORKSPACE_SHARE
    memory_limit_mib: int | None = None
    max_consecutive_timeouts: int = MAX_CONSECUTIVE_TIMEOUTS
    #: Gitignore-style globs of the files no server opens (module doc, "less work").
    excluded_paths: tuple[str, ...] = LSP_EXCLUDED_PATHS


@dataclass(frozen=True)
class Site:
    """One candidate site from the tree-sitter pass."""

    language: str
    path: str
    caller: str
    name: str
    line: int  # 1-based
    kind: str  # call, inherit or route_handler
    qualified: bool = False
    via_receiver: bool = False
    #: `name` is bound by an import in this file, so a definition may land
    #: on a symbol of another name (module doc, "less work").
    imported: bool = False


@dataclass(frozen=True)
class LspEdge:
    frm: str
    to: str
    kind: str
    confidence: float
    path: str
    line: int


@dataclass
class LanguageResult:
    status: str
    reason: str | None
    server: str | None
    counts: dict[str, Any] = field(default_factory=dict)
    #: The files whose every site was asked and whose edges are kept: every
    #: file the server served when `ok`, the finished ones when `partial`,
    #: none otherwise.
    covered: frozenset[str] = frozenset()


@dataclass
class PassResult:
    edges: list[LspEdge]
    languages: dict[str, LanguageResult]
    servers: dict[str, str]
    request_timeout_seconds: float
    server_budget_seconds: float
    total_budget_seconds: float
    memory_limit_mib: int
    #: The costs the shares followed, as given (`LspOptions.server_costs`).
    server_costs: dict[str, float] = field(default_factory=dict)
    #: The globs no server opened (`LspOptions.excluded_paths`).
    excluded_paths: tuple[str, ...] = ()


def run_pass(root: Path, files: Mapping[str, str], symbols: Sequence[Mapping[str, Any]],
             sites: Sequence[Site], options: LspOptions | None = None) -> PassResult:
    """Run every server the repository's languages need; see the module doc.

    `files` maps each parsed file to its language; `symbols` are the
    extractor's symbol records; `sites` its candidate call sites.
    """
    options = options or LspOptions()
    servers = SERVERS if options.servers is None else options.servers
    budget = options.server_budget_seconds or server_budget_seconds(len(files))
    total = options.total_budget_seconds or total_budget_seconds(len(files))
    pass_deadline = time.monotonic() + total
    memory_mib = options.memory_limit_mib or default_memory_limit_mib()
    present = set(files.values())
    languages: dict[str, LanguageResult] = {}
    edges: list[LspEdge] = []
    versions: dict[str, str] = {}
    planned: list[tuple[ServerSpec, list[str]]] = []
    claimed: set[str] = set()
    for name in sorted(servers):
        spec = servers[name]
        served = [lang for lang in spec.languages if lang in present and lang not in claimed]
        if served:
            planned.append((spec, served))
            claimed.update(served)
    needs = planned_needs([spec.name for spec, _ in planned], options.server_costs, total)
    order = run_order([spec.name for spec, _ in planned], needs)
    planned = [planned[i] for i in order]
    needs = [needs[i] for i in order]
    for turn, (spec, served) in enumerate(planned):
        left = pass_deadline - time.monotonic()
        run = _ServerRun(Path(root), spec, set(served), files, symbols, sites, options,
                         grant_seconds(left, needs[turn:], budget), memory_mib,
                         workspace_seconds(left, needs[turn:], budget, options.workspace_share))
        started = time.monotonic()
        if left <= 0:
            run._stop(STATUS_TIMED_OUT, f"the LSP pass's {_seconds(total)}-second total budget "
                                        f"ran out before {spec.name} started")
        else:
            run.run()
        if run.server_version is not None:
            versions[spec.name] = run.server_version
        # What the next run's shares follow (module doc): the grant this
        # server had, and what it needed, when anything was measured.
        # A server stopped as hung, crashed or over its memory measured
        # nothing about what it needs; only a finish or a budget cut did.
        measured = run.status if run.status == STATUS_OK or run.budget_cut else None
        cost = measured_cost(measured, elapsed=time.monotonic() - started, grant=run.budget,
                             requests=run.counts["requests"], sites=run._sites_total)
        counts = dict(run.counts, server_version=run.server_version,
                      grant_seconds=round(run.budget, 1), peak_rss_mib=rss_mib(run.peak_rss_bytes))
        if cost is not None:
            counts["cost_seconds"] = cost
        for lang in served:
            languages[lang] = LanguageResult(
                run.status, run.reason, spec.name, dict(counts),
                frozenset(path for path in run.covered if files.get(path) == lang))
        if run.status in (STATUS_OK, STATUS_PARTIAL):
            edges.extend(run.edges)
    for lang in sorted(present - set(languages)):
        languages[lang] = LanguageResult(STATUS_UNSUPPORTED, NO_SERVER_REASON, None)
    costs = {name: cost for name, cost in sorted((options.server_costs or {}).items())
             if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0}
    return PassResult(edges=edges, languages=languages, servers=versions,
                      request_timeout_seconds=options.request_timeout_seconds,
                      server_budget_seconds=budget, total_budget_seconds=total,
                      memory_limit_mib=memory_mib, server_costs=costs,
                      excluded_paths=tuple(options.excluded_paths))


# --- positions --------------------------------------------------------------


def _utf16(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def _from_utf16(text: str, units: int) -> int:
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _name_matches(text: str, name: str) -> list[re.Match]:
    return list(re.finditer(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text))


def _site_column(text: str, name: str, qualified: bool) -> int | None:
    """Where `name` is called on this line: after a dot when qualified."""
    matches = _name_matches(text, name)
    if not matches:
        return None
    for match in matches:
        dotted = text[:match.start()].rstrip().endswith((".", "?."))
        if dotted == qualified:
            return match.start()
    return matches[0].start()


def simple_name(symbol_id: str) -> str:
    qual = symbol_id.split("#", 1)[1] if "#" in symbol_id else symbol_id
    qual = re.sub(r"@\d+$", "", qual)
    return qual.rsplit(".", 1)[-1]


class _Hung(Exception):
    """The server stopped answering; the message is the reason."""


class _InitFailed(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _ServerRun:
    """One server over the files of the languages it serves."""

    def __init__(self, root: Path, spec: ServerSpec, languages: set[str],
                 files: Mapping[str, str], symbols: Sequence[Mapping[str, Any]],
                 sites: Sequence[Site], options: LspOptions, budget: float,
                 memory_mib: int, workspace_budget: float | None = None) -> None:
        self.root = root
        self.spec = spec
        self.languages = languages
        self.files = files
        self.options = options
        self.budget = budget
        #: Phase 2's limit (`workspace_seconds`); by default its share of the grant.
        self.workspace_budget = (budget * options.workspace_share if workspace_budget is None
                                 else workspace_budget)
        self.memory_mib = memory_mib
        self.status = STATUS_OK
        self.reason: str | None = None
        self.server_version: str | None = None
        self.edges: list[LspEdge] = []
        # Phase 1's progress, file by file (module doc, "the fallbacks"): the
        # files finished, how many edges and sites they account for, and the
        # totals a cut's reason is measured against.
        self.covered: list[str] = []
        self._covered_edges = 0
        self._covered_sites = 0
        self._files_total = 0
        self._sites_total = 0
        #: Whether the budget, not the server, ended phase 1 (`_cut`).
        self.budget_cut = False
        #: The server's peak resident memory, as the client's watchdog saw it.
        self.peak_rss_bytes = 0
        self.counts = {"requests": 0, "resolved": 0, "unresolved": 0, "request_timeouts": 0,
                       "errors": 0, "edges": 0, "symbols_skipped": 0, "sites_skipped_external": 0,
                       "files_excluded": 0, "sites_excluded": 0}
        self._warming = True
        self._workspace_limit: float | None = None
        self._consecutive = 0
        self._lines: dict[str, list[str] | None] = {}
        self.symbols_by_path: dict[str, list[Mapping[str, Any]]] = {}
        for symbol in symbols:
            self.symbols_by_path.setdefault(symbol["path"], []).append(symbol)
        # Every name an answer could be kept at (`_symbol_named_at`).
        self._known_names = {simple_name(symbol["id"]) for symbol in symbols}
        self.sites_by_path: dict[str, list[Site]] = {}
        for site in sites:
            if site.language in languages:
                self.sites_by_path.setdefault(site.path, []).append(site)
        self._hierarchy = spec.call_hierarchy
        self._references = spec.references

    # --- the run ------------------------------------------------------------

    def _argv(self) -> list[str] | None:
        command = list(self.spec.command)
        program = Path(command[0])
        if not program.is_absolute():
            program = Path(self.options.bin_dir) / command[0]
        if not (program.is_file() and os.access(program, os.X_OK)):
            return None
        return [str(program)] + command[1:]

    def _env(self, home: Path) -> dict[str, str]:
        for sub in ("cache", "config", "data", "tmp", "go", "go-build"):
            (home / sub).mkdir()
        env = {
            "PATH": f"{self.options.bin_dir}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "TMPDIR": str(home / "tmp"),
            "GOPATH": str(home / "go"),
            "GOCACHE": str(home / "go-build"),
            "GOMODCACHE": str(home / "go" / "pkg" / "mod"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
        }
        env.update(self.spec.env)
        return env

    def run(self) -> None:
        argv = self._argv()
        if argv is None:
            self._stop(STATUS_FAILING, f"{self.spec.command[0]} is not installed in {self.options.bin_dir}")
            return
        with tempfile.TemporaryDirectory(prefix="repo-lsp-") as scratch:
            home = Path(scratch)
            deadline = time.monotonic() + self.budget
            try:
                client = LspClient(argv, self.root, self._env(home), deadline=deadline,
                                   memory_limit_bytes=self.memory_mib * 1024 * 1024,
                                   settings=self.spec.settings,
                                   workspace_folders=[self._folder()])
            except OSError as exc:
                self._stop(STATUS_FAILING, f"{self.spec.name} could not be started ({type(exc).__name__})")
                return
            try:
                self._session(client)
            except BudgetExceeded:
                # Phase 2 catches its own cut, so this is initialize or phase 1.
                self._cut()
            except _Hung as exc:
                self._stop(STATUS_TIMED_OUT, str(exc))
            except _InitFailed as exc:
                self._stop(STATUS_FAILING, f"initialize failed (error {exc.code})")
            except ServerExited:
                if client.memory_exceeded:
                    self._stop(STATUS_FAILING,
                               f"over the {self.memory_mib} MiB memory limit; {self.spec.name} was stopped")
                else:
                    try:
                        code = client.process.wait(timeout=2.0)
                    except Exception:  # still dying: it is being killed below
                        code = None
                    self._stop(STATUS_FAILING, f"{self.spec.name} exited with status {code}")
            finally:
                client.close()
                self.peak_rss_bytes = client.peak_rss_bytes
            if client.memory_exceeded and self.status in (STATUS_OK, STATUS_PARTIAL):
                # It finished its last answer as the watchdog stopped it.
                self._stop(STATUS_FAILING,
                           f"over the {self.memory_mib} MiB memory limit; {self.spec.name} was stopped")

    def _stop(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason
        self.edges = []
        self.covered = []

    def _cut(self) -> None:
        """The budget ran out in phase 1: keep the files it finished, if any."""
        self.budget_cut = True
        if self._covered_sites == 0:
            self._stop(STATUS_TIMED_OUT, f"over the {_seconds(self.budget)}-second budget for {self.spec.name}")
            return
        self.status = STATUS_PARTIAL
        self.reason = (f"{self.spec.name} reached {len(self.covered)} of {self._files_total} files "
                       f"({self._covered_sites} of {self._sites_total} sites) in its "
                       f"{_seconds(self.budget)}-second budget")
        # The file the budget ran out in was answered in part: its edges go.
        del self.edges[self._covered_edges:]
        self.counts["edges"] = len(self.edges)

    def _folder(self) -> dict:
        return {"uri": self.root.as_uri(), "name": self.root.name or "workspace"}

    def _session(self, client: LspClient) -> None:
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "swarm-repo-index", "version": "1"},
            "locale": "en",
            "rootPath": str(self.root),
            "rootUri": self.root.as_uri(),
            "workspaceFolders": [self._folder()],
            "initializationOptions": self.spec.initialization_options,
            "trace": "off",
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "workspace": {"configuration": True, "workspaceFolders": True},
                "window": {"workDoneProgress": False},
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False, "didSave": False},
                    "definition": {"dynamicRegistration": False, "linkSupport": True},
                    "references": {"dynamicRegistration": False},
                    "callHierarchy": {"dynamicRegistration": False},
                    "publishDiagnostics": {"relatedInformation": False},
                },
            },
        }
        try:
            init = client.request("initialize", params, timeout=self._timeout())
        except ServerError as exc:
            raise _InitFailed(exc.code) from exc
        except RequestTimeout as exc:
            raise _Hung(f"{self.spec.name} did not answer initialize in {self._timeout():g} seconds") from exc
        init = init if isinstance(init, dict) else {}
        info = init.get("serverInfo") if isinstance(init.get("serverInfo"), dict) else {}
        version = info.get("version")
        self.server_version = version if isinstance(version, str) and _VERSION.fullmatch(version) else "unknown"
        capabilities = init.get("capabilities") if isinstance(init.get("capabilities"), dict) else {}
        self._hierarchy = self._hierarchy and bool(capabilities.get("callHierarchyProvider"))
        self._references = self._references and bool(capabilities.get("referencesProvider"))
        client.notify("initialized", {})
        paths = []
        for path in sorted(self.files):
            if self.files[path] not in self.languages:
                continue
            language_id = self.spec.language_ids.get(posixpath.splitext(path)[1])
            if language_id is None:
                continue
            if lsp_excluded(path, self.options.excluded_paths):
                # Parsed, never opened (module doc, "less work").
                self.counts["files_excluded"] += 1
                self.counts["sites_excluded"] += len(self.sites_by_path.get(path, []))
                continue
            paths.append((path, language_id))
        # Only the sites an answer could become an edge at are asked.
        asked: dict[str, list[Site]] = {}
        for path, _ in paths:
            own = self.sites_by_path.get(path, [])
            kept = [site for site in own if not self._external(site)]
            self.counts["sites_skipped_external"] += len(own) - len(kept)
            asked[path] = sorted(kept, key=lambda s: (s.line, s.name, s.caller, s.kind))
        # Phase 1, every site's definition: the per-site answer §3.5 is for.
        # A file counts as covered once every one of its sites was asked, so
        # a cut keeps exactly the files before the one it landed in.
        self._files_total = len(paths)
        self._sites_total = sum(len(sites) for sites in asked.values())
        for path, language_id in paths:
            sites = asked[path]
            if sites:
                self._open(client, path, language_id,
                           lambda document, lines: [self._definition(client, document, lines, site)
                                                    for site in sites])
            self.covered.append(path)
            self._covered_edges = len(self.edges)
            self._covered_sites += len(sites)
        # Phase 2, the workspace queries: callers the syntax could not see.
        # Each one searches the whole workspace (pyright took up to 60 s a
        # symbol on this repository, against milliseconds a definition), so
        # they run after every definition, a timeout here is a slow answer
        # rather than a hung server, and the budget running out here cuts
        # this phase only: phase 1's edges are complete and are kept, and
        # the row's reason says how many symbols were asked.
        todo = []
        for path, language_id in paths:
            own = self.symbols_by_path.get(path, [])
            symbols = [s for s in own if self._hierarchy and s["kind"] in CALLABLE_KINDS and s["exported"]]
            symbols += [s for s in own if self._references and s["kind"] in self.spec.reference_kinds]
            if symbols:
                todo.append((path, language_id, symbols))
        total = sum(len(symbols) for _, _, symbols in todo)
        asked = 0
        # Phase 2's own limit (module doc): at most its share of the
        # server's proportion of the pass (`workspace_seconds`), counted
        # from the server's first answer when phase 1 asked nothing, so the
        # workspace load is not charged to it.
        share = self.workspace_budget
        self._workspace_limit = share
        if not self._warming:
            self._limit_workspace(client)
        try:
            for path, language_id, symbols in todo:
                def workspace(document: dict, lines: list[str], symbols: list = symbols) -> None:
                    nonlocal asked
                    for symbol in symbols:
                        if self._hierarchy and symbol["kind"] in CALLABLE_KINDS:
                            self._incoming(client, document, lines, symbol)
                        else:
                            self._referenced(client, document, lines, symbol)
                        asked += 1
                self._open(client, path, language_id, workspace)
        except BudgetExceeded:
            self.counts["symbols_skipped"] = total - asked
            self.reason = (f"call hierarchy and references cut at their {_seconds(share)}-second share "
                           f"of the {_seconds(self.budget)}-second budget: {asked} of {total} symbols asked")

    def _external(self, site: Site) -> bool:
        """No answer about `site` could be kept: its name is no repository symbol's
        and no import in its file binds it (module doc, "less work")."""
        return not site.imported and site.name not in self._known_names

    def _open(self, client: LspClient, path: str, language_id: str, queries: Any) -> None:
        lines = self._read(path)
        if lines is None:
            return
        uri = (self.root / path).as_uri()
        client.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language_id, "version": 1, "text": "\n".join(lines)}})
        document = {"uri": uri}
        queries(document, lines)
        client.notify("textDocument/didClose", {"textDocument": document})

    # --- the three queries -------------------------------------------------

    def _definition(self, client: LspClient, document: dict, lines: list[str], site: Site) -> None:
        if site.name.startswith("\x00") or not 1 <= site.line <= len(lines):
            return
        text = lines[site.line - 1]
        column = _site_column(text, site.name, site.qualified)
        if column is None:
            return
        result = self._ask(client, "textDocument/definition", {
            "textDocument": document,
            "position": {"line": site.line - 1, "character": _utf16(text, column)}})
        targets = []
        for uri, start in _locations(result):
            target = self._symbol_named_at(uri, start)
            if target is not None and target not in targets:
                targets.append(target)
        if not targets:
            self.counts["unresolved"] += 1
            return
        self.counts["resolved"] += 1
        confidence = LSP_INFERRED if site.via_receiver else LSP_DECLARED
        for target in targets:
            self._edge(site.caller, target, site.kind, confidence, site.path, site.line)

    def _incoming(self, client: LspClient, document: dict, lines: list[str],
                  symbol: Mapping[str, Any]) -> None:
        position = self._name_position(lines, symbol)
        if position is None:
            return
        items = self._ask(client, "textDocument/prepareCallHierarchy",
                          {"textDocument": document, "position": position}, workspace=True)
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return
        calls = self._ask(client, "callHierarchy/incomingCalls", {"item": items[0]}, workspace=True)
        if not isinstance(calls, list):
            return
        for call in calls:
            try:
                caller = call["from"]
                start = caller["selectionRange"]["start"]
                found = self._symbol_named_at(caller["uri"], start)
                ranges = call.get("fromRanges") or [caller["range"]]
                line = int(ranges[0]["start"]["line"]) + 1
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if found is None:
                continue
            caller_path = self._relative(caller["uri"])
            self._edge(found, symbol["id"], "call", LSP_DECLARED, caller_path or symbol["path"], line)

    def _referenced(self, client: LspClient, document: dict, lines: list[str],
                    symbol: Mapping[str, Any]) -> None:
        position = self._name_position(lines, symbol)
        if position is None:
            return
        result = self._ask(client, "textDocument/references", {
            "textDocument": document, "position": position,
            "context": {"includeDeclaration": False}}, workspace=True)
        for uri, start in _locations(result):
            path = self._relative(uri)
            if path is None:
                continue
            enclosing = self._enclosing(path, int(start["line"]) + 1)
            if enclosing is None or enclosing == symbol["id"]:
                continue
            self._edge(enclosing, symbol["id"], "reference", LSP_DECLARED, path, int(start["line"]) + 1)

    def _edge(self, frm: str, to: str, kind: str, confidence: float, path: str, line: int) -> None:
        self.edges.append(LspEdge(frm, to, kind, confidence, path, line))
        self.counts["edges"] += 1

    def _timeout(self) -> float:
        if self._warming:
            if self.options.warmup_timeout_seconds is not None:
                return self.options.warmup_timeout_seconds
            return self.budget
        return self.options.request_timeout_seconds

    def _limit_workspace(self, client: LspClient) -> None:
        if self._workspace_limit is not None:
            client.deadline = min(client.deadline, time.monotonic() + self._workspace_limit)

    def _ask(self, client: LspClient, method: str, params: dict, workspace: bool = False) -> Any:
        """One request. A timed-out `workspace` query never counts as hung."""
        timeout = self._timeout()
        warmup, self._warming = self._warming, False
        self.counts["requests"] += 1
        try:
            result = client.request(method, params, timeout=timeout)
        except RequestTimeout:
            if warmup:
                self._limit_workspace(client)
            self.counts["request_timeouts"] += 1
            if workspace:
                return None
            self._consecutive += 1
            if self._consecutive >= self.options.max_consecutive_timeouts:
                raise _Hung(f"{self.spec.name} stopped answering: {self._consecutive} consecutive requests "
                            f"each over {self.options.request_timeout_seconds:g} seconds") from None
            return None
        except ServerError:
            if warmup:
                self._limit_workspace(client)
            self.counts["errors"] += 1
            self._consecutive = 0
            return None
        if warmup:
            self._limit_workspace(client)
        self._consecutive = 0
        return result

    # --- mapping answers to symbols ----------------------------------------

    def _read(self, path: str) -> list[str] | None:
        if path not in self._lines:
            try:
                fd = os.open(self.root / path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as handle:
                    data = handle.read()
                self._lines[path] = data.decode("utf-8", errors="replace").split("\n")
            except OSError:
                self._lines[path] = None
        return self._lines[path]

    def _relative(self, uri: Any) -> str | None:
        if not isinstance(uri, str):
            return None
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path)
        root = str(self.root)
        if not path.startswith(root.rstrip("/") + "/"):
            return None
        relative = posixpath.normpath(path[len(root.rstrip("/")) + 1:])
        if relative.startswith("../") or relative == ".." or relative not in self.files:
            return None
        return relative

    def _symbol_named_at(self, uri: Any, start: Any) -> str | None:
        """The innermost symbol whose name is at this location, or None."""
        path = self._relative(uri)
        if path is None or not isinstance(start, dict):
            return None
        lines = self._read(path)
        try:
            line = int(start["line"])
            units = int(start["character"])
        except (KeyError, TypeError, ValueError):
            return None
        if lines is None or not 0 <= line < len(lines):
            return None
        text = lines[line]
        index = _from_utf16(text, units)
        while index < len(text) and text[index] in "\"'`":
            index += 1
        match = _WORD.match(text, index)
        if match is None:
            return None
        word = match.group(0)
        found = [s for s in self.symbols_by_path.get(path, [])
                 if s["start_line"] <= line + 1 <= s["end_line"] and simple_name(s["id"]) == word]
        if not found:
            return None
        return max(found, key=lambda s: (s["start_line"], -s["end_line"], s["kind"] != "route"))["id"]

    def _enclosing(self, path: str, line: int) -> str | None:
        found = [s for s in self.symbols_by_path.get(path, [])
                 if s["start_line"] <= line <= s["end_line"]]
        if not found:
            return None
        return max(found, key=lambda s: (s["start_line"], -s["end_line"], s["kind"] != "route"))["id"]

    def _name_position(self, lines: list[str], symbol: Mapping[str, Any]) -> dict | None:
        """Where a symbol's own name is: a defining keyword before it wins."""
        name = simple_name(symbol["id"])
        best: tuple[int, int, int] | None = None
        for line in range(symbol["start_line"], min(symbol["end_line"], len(lines)) + 1):
            text = lines[line - 1]
            for match in _name_matches(text, name):
                before = text[:match.start()]
                if _DEFINING.search(before):
                    rank = 0
                elif before.endswith(("\"", "'", "/", "`")):
                    rank = 2
                else:
                    rank = 1
                candidate = (rank, line, match.start())
                if best is None or candidate < best:
                    best = candidate
            if best is not None and best[0] == 0:
                break
        if best is None:
            return None
        _, line, column = best
        return {"line": line - 1, "character": _utf16(lines[line - 1], column)}


def _locations(result: Any) -> list[tuple[Any, Any]]:
    """(uri, start) of a Location, a Location[] or a LocationLink[]."""
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "targetUri" in item:
            span = item.get("targetSelectionRange") or item.get("targetRange")
            uri = item["targetUri"]
        else:
            span, uri = item.get("range"), item.get("uri")
        if isinstance(span, dict) and isinstance(span.get("start"), dict):
            out.append((uri, span["start"]))
    return out
