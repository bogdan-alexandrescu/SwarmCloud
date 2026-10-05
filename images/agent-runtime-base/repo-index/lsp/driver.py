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

THE FALLBACKS. One server per language, run one at a time (the indexer's
memory is sized for the largest server, not for four at once):

  ok           the server answered to the end; its edges are kept
  timed_out    it ran past its budget (§3.5's table, by repository size),
               or stopped answering -- `max_consecutive_timeouts` requests
               in a row each over the per-request timeout
  failing      it is not installed, could not start, refused `initialize`,
               exited or crashed, or exceeded its memory limit
  unsupported  the repository has the language and no server is listed

A server that is stopped keeps NONE of its answers: the language is then
uniformly `ast`, which is what its status tells a consumer, rather than a
graph that is `lsp` in the files the server reached and `ast` in the rest
with nothing to say which. The run still succeeds; a timeout is never a
failed index, it is a less certain one, and says so.

TWO PHASES. Every site's definition is asked first, across all files; the
workspace queries (call hierarchy, references) run after. A workspace query
searches the whole repository -- pyright's incomingCalls took up to 60 s a
symbol on this repository on 2026-10-05, against milliseconds a definition
-- so a timed-out one is a slow answer and never counts toward
`max_consecutive_timeouts`, and the budget running out in phase 2 ends
phase 2 only: the language stays `ok`, keeps every definition edge, and its
reason says how many symbols were asked ("call hierarchy and references cut
at the N-second budget: a of b symbols asked"). The budget running out in
phase 1 is `timed_out` as above.

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
from .servers import DEFAULT_BIN_DIR, SERVERS, ServerSpec

# §2.5's confidences for `lsp` evidence.
LSP_DECLARED = 0.95
LSP_INFERRED = 0.8

STATUS_OK = "ok"
STATUS_TIMED_OUT = "timed_out"
STATUS_FAILING = "failing"
STATUS_UNSUPPORTED = "unsupported"
NO_SERVER_REASON = "no language server; edges are syntactic"

# §3.5's budget table: source files -> seconds per language server.
SERVER_BUDGETS = ((2_000, 600), (10_001, 1_800))
LARGEST_SERVER_BUDGET = 2_700
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
    memory_limit_mib: int | None = None
    max_consecutive_timeouts: int = MAX_CONSECUTIVE_TIMEOUTS


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


@dataclass
class PassResult:
    edges: list[LspEdge]
    languages: dict[str, LanguageResult]
    servers: dict[str, str]
    request_timeout_seconds: float
    server_budget_seconds: float
    memory_limit_mib: int


def run_pass(root: Path, files: Mapping[str, str], symbols: Sequence[Mapping[str, Any]],
             sites: Sequence[Site], options: LspOptions | None = None) -> PassResult:
    """Run every server the repository's languages need; see the module doc.

    `files` maps each parsed file to its language; `symbols` are the
    extractor's symbol records; `sites` its candidate call sites.
    """
    options = options or LspOptions()
    servers = SERVERS if options.servers is None else options.servers
    budget = options.server_budget_seconds or server_budget_seconds(len(files))
    memory_mib = options.memory_limit_mib or default_memory_limit_mib()
    present = set(files.values())
    languages: dict[str, LanguageResult] = {}
    edges: list[LspEdge] = []
    versions: dict[str, str] = {}
    for name in sorted(servers):
        spec = servers[name]
        served = [lang for lang in spec.languages if lang in present and lang not in languages]
        if not served:
            continue
        run = _ServerRun(Path(root), spec, set(served), files, symbols, sites, options,
                         budget, memory_mib)
        run.run()
        if run.server_version is not None:
            versions[spec.name] = run.server_version
        counts = dict(run.counts, server_version=run.server_version)
        for lang in served:
            languages[lang] = LanguageResult(run.status, run.reason, spec.name, dict(counts))
        if run.status == STATUS_OK:
            edges.extend(run.edges)
    for lang in sorted(present - set(languages)):
        languages[lang] = LanguageResult(STATUS_UNSUPPORTED, NO_SERVER_REASON, None)
    return PassResult(edges=edges, languages=languages, servers=versions,
                      request_timeout_seconds=options.request_timeout_seconds,
                      server_budget_seconds=budget, memory_limit_mib=memory_mib)


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
                 memory_mib: int) -> None:
        self.root = root
        self.spec = spec
        self.languages = languages
        self.files = files
        self.options = options
        self.budget = budget
        self.memory_mib = memory_mib
        self.status = STATUS_OK
        self.reason: str | None = None
        self.server_version: str | None = None
        self.edges: list[LspEdge] = []
        self.counts = {"requests": 0, "resolved": 0, "unresolved": 0, "request_timeouts": 0,
                       "errors": 0, "edges": 0, "symbols_skipped": 0}
        self._warming = True
        self._consecutive = 0
        self._lines: dict[str, list[str] | None] = {}
        self.symbols_by_path: dict[str, list[Mapping[str, Any]]] = {}
        for symbol in symbols:
            self.symbols_by_path.setdefault(symbol["path"], []).append(symbol)
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
                self._stop(STATUS_TIMED_OUT, f"over the {self.budget:g}-second budget for {self.spec.name}")
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
            if client.memory_exceeded and self.status == STATUS_OK:
                # It finished its last answer as the watchdog stopped it.
                self._stop(STATUS_FAILING,
                           f"over the {self.memory_mib} MiB memory limit; {self.spec.name} was stopped")

    def _stop(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason
        self.edges = []

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
            if language_id is not None:
                paths.append((path, language_id))
        # Phase 1, every site's definition: the per-site answer §3.5 is for.
        for path, language_id in paths:
            sites = sorted(self.sites_by_path.get(path, []),
                           key=lambda s: (s.line, s.name, s.caller, s.kind))
            if sites:
                self._open(client, path, language_id,
                           lambda document, lines: [self._definition(client, document, lines, site)
                                                    for site in sites])
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
            self.reason = (f"call hierarchy and references cut at the {self.budget:g}-second budget: "
                           f"{asked} of {total} symbols asked")

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

    def _ask(self, client: LspClient, method: str, params: dict, workspace: bool = False) -> Any:
        """One request. A timed-out `workspace` query never counts as hung."""
        timeout = self._timeout()
        self._warming = False
        self.counts["requests"] += 1
        try:
            result = client.request(method, params, timeout=timeout)
        except RequestTimeout:
            self.counts["request_timeouts"] += 1
            if workspace:
                return None
            self._consecutive += 1
            if self._consecutive >= self.options.max_consecutive_timeouts:
                raise _Hung(f"{self.spec.name} stopped answering: {self._consecutive} consecutive requests "
                            f"each over {self.options.request_timeout_seconds:g} seconds") from None
            return None
        except ServerError:
            self.counts["errors"] += 1
            self._consecutive = 0
            return None
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
