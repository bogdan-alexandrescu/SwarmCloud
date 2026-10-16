"""The ten tools, their schemas, and the 4 KiB answer with paging (§4.2, §7.7).

KG4's six, and two from docs/design/context-quality.md §4.4 (lane CTXQ-T),
both offline, over the snapshot's layers H and R (`snapshot.py` documents
their rows): `similar_changes` lists the merged PRs, fix commits and
co-changing files of the files asked about -- precedent, and fixes not to
make a second time -- and `rules_for` the paths a lane cannot push, the guard
tests that police the files and the docs that name them. Zero rows from
either is `UNKNOWN`, as zero callers is: a history the indexer did not read
that far back, or a rule nobody wrote down, is not "no precedent" or
"unconstrained". A snapshot indexed before those layers says so in its note.

EVERY ANSWER IS ONE JSON OBJECT, AT MOST `ANSWER_CAP` BYTES:

    {"freshness": {...}, <the head>, "total": N, "rows": [...], "next_cursor": "K"}

`freshness` comes first, on errors too (§5.2). The head is the answer's
summary and is small by construction; `rows` holds as many of the `total`
rows as fit under the cap, and `next_cursor` (null on the last page) is
passed back as `cursor` for the next. The rows are in a fixed order over an
immutable snapshot, so a cursor is just an offset. Serialisation is ASCII-only
JSON, so a character is a byte and the cap is exact.

Rows on a path changed since the index or dirtied in this step carry
`"stale": <why>` (`freshness.Observation.stale`).

THE SCHEMAS ARE THE BUDGET (§7.7: under 2k tokens; GitNexus's ≈ 17.9k). The
agent pays for them on every turn, so the descriptions say what a tool
answers and nothing about how; the module docstrings carry the why.

TWO TOOLS FOR THE SHARED GRAPH SERVICE (docs/design/graph-service.md §2.3).
`call_graph` walks callers, callees or both from a symbol (or every symbol of
a file) and lists the edges breadth-first, bounded like `impact`; `index`
pages any layer of the snapshot -- its keys with their row counts, or one
shard's rows in stored order -- which is the whole index, page by page, and
"the index for X" is `index(layer, path=X)`. `document` is repo-index.md
§2.1's document, one shard per section (`snapshot.DOCUMENT_LAYER`).

COMPUTE, THEN PAGE (graph-service.md §2.4, seam 2). Each handler (`tool_*`)
computes a `Computed` -- head and rows -- from the snapshot and the
arguments alone, with no observation, so the service can cache it and serve
every page of one question from one computation. `page` applies the
observation: it marks the head (by its `symbol` or `file`) and every row
stale. `remark` turns an answer computed under the empty observation into the
one the local server would have given under a real one: the pod's shim does
nothing else. `TOOLS_VERSION` names the handlers' output, and is part of
every cached answer's key; bump it whenever an answer changes
(tests/unit/worker/test_graphmcp_tools_version.py fails when one changes and
it does not).

`territory` answers what a local snapshot can know: each file's community,
how many other files depend on it and which. Which in-flight steps and lanes
hold a file is phase 3's (§4.8, served by swarm-api) and is not in a snapshot;
the answer says so in `in_flight` rather than leaving it out silently.
"""

from __future__ import annotations

import functools
import json
import posixpath
import re
from dataclasses import dataclass
from typing import Any, Callable

from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import (
    ALL_LAYERS,
    DOCUMENT_LAYER,
    Snapshot,
    SnapshotError,
    module_of,
    name_of,
    path_of,
)

#: The version of every handler's output (graph-service.md §5.2): part of the
#: shared service's result-cache key. Bump it in the same change as any edit
#: that changes an answer; test_graphmcp_tools_version.py holds the two together.
TOOLS_VERSION = "1"
#: §7.7: an answer is capped at 4 KiB, paged beyond.
ANSWER_CAP = 4096
#: `search`'s default and largest result count.
SEARCH_DEFAULT = 20
SEARCH_MAX = 100
#: `impact`'s default and deepest upstream walk (§4.2: depth ≤ 3).
IMPACT_DEFAULT_DEPTH = 2
IMPACT_MAX_DEPTH = 3
#: Upstream symbols one `impact` walks before it stops and says `truncated`:
#: a depth-3 walk from a hub reaches thousands, and an answer's p95 is
#: budgeted at 300 ms. The direct callers are always all counted.
IMPACT_MAX_NODES = 400
#: Files or symbols one `tests_for` or `territory` takes.
MAX_INPUTS = 50
#: Other files of the community `neighbours` reads, nearest by path first.
NEIGHBOUR_FILES = 12
#: Candidates a name is resolved from, and listed when it is ambiguous.
RESOLVE_CANDIDATES = 200
SHOWN_CANDIDATES = 10

#: `call_graph`'s directions, and its default and deepest walk.
CALL_GRAPH_DIRECTIONS = ("callers", "callees", "both")
CALL_GRAPH_DEFAULT_DEPTH = 2
#: Files of a past PR one `similar_changes` row lists; the rest are counted.
SHOWN_FILES = 8
#: Characters of a rule's `why` or a commit's subject one row carries.
SHOWN_TEXT = 200
#: `rules_for`'s order: what the lane must not do, then what will police it.
RULE_ORDER = {"cannot_push": 0, "guard": 1, "doc": 2}

UNKNOWN_CALLERS = ("zero resolved callers is UNKNOWN, never safe: a call through a "
                   "dynamic dispatch, a string or a module object can be invisible")
NO_HISTORY = ("zero recorded changes is UNKNOWN, never 'no precedent': the history "
              "holds merged PRs only, as far back as the indexer read")
NO_RULES = ("zero matching rules is UNKNOWN, never 'unconstrained': CLAUDE.md and the "
            "guard set still apply")
NO_HISTORY_LAYER = "this snapshot has no history layer (indexed before layer H)"
NO_RULES_LAYER = "this snapshot has no rules layer (indexed before layer R)"
NO_LAYER = "this snapshot has no {layer} layer (indexed before it was written)"
IN_FLIGHT = "not in a snapshot: in-flight steps and lanes come from swarm-api (phase 3)"

_CURSOR = {"type": "string", "description": "next_cursor of the previous page"}
_SYMBOL = {"type": "string", "description": "symbol id path#name, or a name"}
_FILES = {"type": "array", "items": {"type": "string"}, "description": "repo-relative paths"}

TOOLS: list[dict[str, Any]] = [
    {"name": "search",
     "description": "Find application symbols by keywords (BM25 over names, paths, "
                    "signatures, docstrings). Tests are not indexed: use tests_for.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX},
         "cursor": _CURSOR}}},
    {"name": "context",
     "description": "A symbol's definition, direct callers, callees and covering tests.",
     "inputSchema": {"type": "object", "required": ["symbol"], "properties": {
         "symbol": _SYMBOL, "cursor": _CURSOR}}},
    {"name": "impact",
     "description": "Blast radius: transitive callers of a symbol or every symbol of a "
                    "file, with modules and covering tests. fan_in is the risk.",
     "inputSchema": {"type": "object", "required": ["symbol"], "properties": {
         "symbol": {"type": "string", "description": "symbol id, name, or file path"},
         "depth": {"type": "integer", "minimum": 1, "maximum": IMPACT_MAX_DEPTH},
         "cursor": _CURSOR}}},
    {"name": "tests_for",
     "description": "Tests covering files or symbols, most confident first: run these "
                    "before pushing.",
     "inputSchema": {"type": "object", "properties": {
         "files": _FILES, "symbols": {"type": "array", "items": {"type": "string"}},
         "cursor": _CURSOR}}},
    {"name": "neighbours",
     "description": "Symbols of the same kind in the same file and community, and the "
                    "symbol's test files: the local idiom to follow.",
     "inputSchema": {"type": "object", "required": ["symbol"], "properties": {
         "symbol": _SYMBOL, "cursor": _CURSOR}}},
    {"name": "territory",
     "description": "For files: community, fan-in and the files depending on them.",
     "inputSchema": {"type": "object", "required": ["files"], "properties": {
         "files": _FILES, "cursor": _CURSOR}}},
    {"name": "similar_changes",
     "description": "Earlier merged PRs, fix commits and co-changing files for files or "
                    "symbols: precedent to follow, fixes not to repeat.",
     "inputSchema": {"type": "object", "properties": {
         "files": _FILES, "symbols": {"type": "array", "items": {"type": "string"}},
         "cursor": _CURSOR}}},
    {"name": "rules_for",
     "description": "For files: paths a lane cannot push, guard tests policing them, "
                    "docs naming them.",
     "inputSchema": {"type": "object", "required": ["files"], "properties": {
         "files": _FILES, "cursor": _CURSOR}}},
    {"name": "call_graph",
     "description": "Call edges from a symbol or file, breadth-first.",
     "inputSchema": {"type": "object", "required": ["symbol"], "properties": {
         "symbol": {"type": "string", "description": "symbol id, name, or file path"},
         "direction": {"type": "string", "enum": list(CALL_GRAPH_DIRECTIONS)},
         "depth": {"type": "integer", "minimum": 1, "maximum": IMPACT_MAX_DEPTH},
         "cursor": _CURSOR}}},
    {"name": "index",
     "description": "Page a graph layer: its keys with row counts, or one key's rows "
                    "(path: the module holding it).",
     "inputSchema": {"type": "object", "required": ["layer"], "properties": {
         "layer": {"type": "string", "enum": list(ALL_LAYERS)}, "key": {"type": "string"},
         "path": {"type": "string"}, "cursor": _CURSOR}}},
]
TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)


class ToolInputError(Exception):
    """The arguments are not what the tool takes; said back to the agent."""


class _Unresolved(Exception):
    """A name that matched no symbol or several; the candidates go in the answer."""

    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


# --- the answer ---------------------------------------------------------------

@dataclass(frozen=True)
class Computed:
    """A handler's answer before paging: its head and every row, in order.

    `expand` (when set) turns a row into what is shown, page by page, so a
    question that only reads its first page does not expand the rest.
    """

    head: dict
    rows: list
    expand: Callable[[Any], dict] | None = None

    def materialised(self) -> "Computed":
        """The same answer with every row expanded: what a cache keeps."""
        if self.expand is None:
            return self
        return Computed(self.head, [self.expand(row) for row in self.rows])


def _stale_of(key: Any, obs: Observation) -> str | None:
    return obs.stale(path_of(str(key))) if key else None


def _mark(row: dict, obs: Observation) -> dict:
    key = row.get("path") or row.get("symbol") or row.get("test") or ""
    why = _stale_of(key, obs)
    if not key:
        # An edge (`call_graph`, the `callers`/`callees` layers): stale when
        # either end's file is.
        why = _stale_of(row.get("from"), obs) or _stale_of(row.get("to"), obs)
    if why:
        row = {**row, "stale": why}
    return row


def _mark_head(head: dict, obs: Observation) -> dict:
    """A head about one symbol or one file is stale when that file is."""
    key = head.get("symbol") or head.get("file")
    why = _stale_of(key, obs) if isinstance(key, str) else None
    return {**head, "stale": why} if why else head


HEAD_OVER_CAP = "the answer's head alone is over the 4 KiB cap; ask for less"
ONE_ROW_OVER = {"omitted": "one row over the 4 KiB answer cap"}


def _fits(size: int, piece: int, after: int, total: int) -> bool:
    """Whether a row of `piece` bytes fits an answer of `size` bytes so far,
    the shell's `next_cursor` counted as `str(total)`. When the row is the last
    one the cursor is written `null`, a byte longer than a one-digit total."""
    extra = max(4 - len(dumps(str(total))), 0) if after >= total else 0
    return size + piece + extra <= ANSWER_CAP


def page(obs: Observation, head: dict, rows: list[Any], offset: int,
         expand: Callable[[Any], dict] | None = None) -> str:
    """The answer for rows[offset:], as many as fit in `ANSWER_CAP` bytes."""
    total = len(rows)
    if offset > total:
        raise ToolInputError(f"cursor {offset} is past the last row ({total})")
    head = _mark_head(head, obs)
    shell = {"freshness": obs.block, **head, "total": total, "rows": [],
             "next_cursor": str(total)}
    size = len(dumps(shell))
    if size > ANSWER_CAP:
        raise ToolInputError(HEAD_OVER_CAP)
    out: list[dict] = []
    index = offset
    while index < total:
        row = _mark(expand(rows[index]) if expand else rows[index], obs)
        piece = len(dumps(row)) + (1 if out else 0)
        if not _fits(size, piece, index + 1, total):
            break
        out.append(row)
        size += piece
        index += 1
    if not out and index < total:
        # One row over the cap on its own: say so in its place and move on,
        # so paging always advances. A head that leaves no room even for that
        # is the head over the cap.
        if not _fits(size, len(dumps(ONE_ROW_OVER)), index + 1, total):
            raise ToolInputError(HEAD_OVER_CAP)
        out.append(ONE_ROW_OVER)
        index += 1
    answer = {"freshness": obs.block, **head, "total": total, "rows": out,
              "next_cursor": str(index) if index < total else None}
    text = dumps(answer)
    assert len(text) <= ANSWER_CAP, len(text)
    return text


_PAGE_KEYS = ("freshness", "total", "rows", "next_cursor")


def remark(text: str, obs: Observation) -> str:
    """`text`, an answer computed under the empty observation, as the local
    server would have given it under `obs` (graph-service.md §4.4).

    The freshness block becomes `obs`'s, and the head and every row are marked
    as `page` marks them. Marks add bytes, so when the page no longer fits it
    loses rows from the end and `next_cursor` points at the first one dropped:
    a cursor is an offset into a fixed row order, so the next page starts
    exactly there. The pages, concatenated, are the local server's.
    """
    answer = json.loads(text)
    if not isinstance(answer, dict):
        raise ValueError("an answer is a JSON object")
    if "error" in answer:
        return error_answer(obs, str(answer["error"]),
                            [str(c) for c in answer.get("candidates") or []])
    if not all(key in answer for key in _PAGE_KEYS[1:]):
        return dumps({"freshness": obs.block,
                      **{k: v for k, v in answer.items() if k != "freshness"}})
    head = _mark_head({k: v for k, v in answer.items() if k not in _PAGE_KEYS}, obs)
    total = int(answer["total"])
    rows = list(answer["rows"])
    following = answer["next_cursor"]
    start = (int(following) if following is not None else total) - len(rows)
    size = len(dumps({"freshness": obs.block, **head, "total": total, "rows": [],
                      "next_cursor": str(total)}))
    if size > ANSWER_CAP:
        return error_answer(obs, HEAD_OVER_CAP)
    out: list[dict] = []
    for row in rows:
        row = _mark(row, obs)
        piece = len(dumps(row)) + (1 if out else 0)
        if not _fits(size, piece, start + len(out) + 1, total):
            break
        out.append(row)
        size += piece
    if not out and rows:
        if not _fits(size, len(dumps(ONE_ROW_OVER)), start + 1, total):
            return error_answer(obs, HEAD_OVER_CAP)
        out.append(ONE_ROW_OVER)
    index = start + len(out)
    marked = {"freshness": obs.block, **head, "total": total, "rows": out,
              "next_cursor": str(index) if index < total else None}
    result = dumps(marked)
    assert len(result) <= ANSWER_CAP, len(result)
    return result


def error_answer(obs: Observation, message: str, candidates: list[str] | None = None) -> str:
    """An error, freshness first, under the cap like any answer."""
    body: dict[str, Any] = {"freshness": obs.block, "error": message[:1024]}
    if candidates:
        body["candidates"] = []
        for candidate in candidates[:SHOWN_CANDIDATES]:
            trial = {**body, "candidates": [*body["candidates"], candidate]}
            if len(dumps(trial)) > ANSWER_CAP:
                break
            body = trial
    return dumps(body)


# --- arguments ----------------------------------------------------------------

def _args(arguments: Any, allowed: set[str], required: set[str]) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolInputError("arguments must be an object")
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(unknown)}")
    missing = sorted(required - set(arguments))
    if missing:
        raise ToolInputError(f"missing argument(s): {', '.join(missing)}")
    return arguments


def _string(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} must be a non-empty string")
    return value.strip()


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"{key} must be an integer from {low} to {high}")
    return value


def _strings(args: dict, key: str) -> list[str]:
    value = args.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ToolInputError(f"{key} must be a list of non-empty strings")
    if len(value) > MAX_INPUTS:
        raise ToolInputError(f"{key} takes at most {MAX_INPUTS} entries")
    return [v.strip() for v in value]


def _cursor(args: dict) -> int:
    value = args.get("cursor")
    if value is None:
        return 0
    if not isinstance(value, str) or not value.isdigit():
        raise ToolInputError("cursor must be a next_cursor this server returned")
    return int(value)


def _file_arg(path: str) -> str:
    clean = posixpath.normpath(path.strip())
    if clean.startswith("/") or clean == ".." or clean.startswith("../"):
        raise ToolInputError(f"{path!r} is not a repository-relative path")
    return clean


# --- resolving a symbol -------------------------------------------------------

def _forms(symbol_id: str) -> tuple[str, str]:
    name = name_of(symbol_id)
    stem = posixpath.splitext(posixpath.basename(path_of(symbol_id)))[0]
    return name, f"{stem}.{name}"


def resolve(snap: Snapshot, text: str) -> str:
    """A symbol id for `text`: an id as given, or a name matched exactly.

    A name is matched against `name`, `Owner.name` and `module.name` (the
    file's stem), and a dotted suffix of either. Several matches are
    ambiguous and are listed, never guessed between.
    """
    if "#" in text:
        if snap.symbol(text) is not None:
            return text
        name = name_of(text)
        raise _Unresolved(f"no symbol {text!r} in the index at this commit; it may be new "
                          "since the index", _candidates(snap, name, text))
    found = _candidates(snap, text, text)
    exact = [c for c in found if _matches(c, text)]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        raise _Unresolved(f"no symbol named {text!r} in the index; pass an id path#name "
                          "(tests are reached through tests_for)", found)
    raise _Unresolved(f"{len(exact)} symbols match {text!r}; pass one id", exact)


def _matches(symbol_id: str, text: str) -> bool:
    return any(form == text or form.endswith("." + text) for form in _forms(symbol_id))


def _candidates(snap: Snapshot, query: str, text: str) -> list[str]:
    if not snap.has_terms:
        return []
    hits = [symbol for symbol, _score in snap.search(query, RESOLVE_CANDIDATES)]
    exact = [h for h in hits if _matches(h, text)]
    return exact or hits[:SHOWN_CANDIDATES]


# --- the tools ----------------------------------------------------------------

def _community(snap: Snapshot, path: str) -> str | None:
    row = snap.community_of(path)
    return str(row.get("id")) if row else None


def _describe(snap: Snapshot, symbol_id: str) -> dict:
    row = snap.symbol(symbol_id) or {}
    out: dict[str, Any] = {"symbol": symbol_id}
    if row:
        out["kind"] = row.get("kind")
        out["line"] = row.get("start_line")
    return out


def _edge_row(rel: str, edge: dict, other: str) -> dict:
    row = {"rel": rel, "symbol": edge.get(other), "kind": edge.get("kind"),
           "line": edge.get("line"), "confidence": edge.get("confidence")}
    if edge.get("evidence") not in (None, "ast"):
        row["evidence"] = edge.get("evidence")
    return row


def _test_row(test: dict) -> dict:
    return {"rel": "test", "test": test.get("test"), "confidence": test.get("confidence"),
            "depth": test.get("depth")}


def tool_search(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"query", "limit", "cursor"}, {"query"})
    query = _string(args, "query")
    limit = _int(args, "limit", SEARCH_DEFAULT, 1, SEARCH_MAX)
    hits = snap.search(query, limit)

    def expand(hit: tuple[str, float]) -> dict:
        symbol, score = hit
        return {**_describe(snap, symbol), "score": score,
                "community": _community(snap, path_of(symbol))}

    return Computed({"query": query}, hits, expand)


def tool_context(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"symbol", "cursor"}, {"symbol"})
    symbol = resolve(snap, _string(args, "symbol"))
    row = snap.symbol(symbol) or {}
    callers = sorted((e for e in snap.callers(symbol) if e.get("kind") != "import"),
                     key=lambda e: (str(e.get("from")), e.get("line") or 0))
    callees = sorted(snap.callees(symbol), key=lambda e: (str(e.get("to")), e.get("line") or 0))
    tests = sorted(snap.tests(symbol), key=lambda t: (-(t.get("confidence") or 0),
                                                      str(t.get("test"))))
    signature = snap.signature(symbol)
    head = {"symbol": symbol, "kind": row.get("kind"),
            "lines": [row.get("start_line"), row.get("end_line")],
            "signature": signature.get("signature") if signature else None,
            "community": _community(snap, path_of(symbol)),
            "callers": len(callers) if callers else "UNKNOWN",
            "callees": len(callees), "tests": len(tests)}
    if not callers:
        head["note"] = UNKNOWN_CALLERS
    rows = ([_edge_row("caller", e, "from") for e in callers]
            + [_edge_row("callee", e, "to") for e in callees]
            + [_test_row(t) for t in tests])
    return Computed(head, rows)


def _is_test(snap: Snapshot, symbol_id: str) -> bool:
    row = snap.symbol(symbol_id)
    if row is not None and row.get("kind") == "test":
        return True
    file = snap.file(path_of(symbol_id))
    return bool(file and file.get("test"))


def tool_impact(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"symbol", "depth", "cursor"}, {"symbol"})
    target = _string(args, "symbol")
    depth = _int(args, "depth", IMPACT_DEFAULT_DEPTH, 1, IMPACT_MAX_DEPTH)
    roots, head = _roots(snap, target)
    seen = set(roots)
    found: list[dict] = []
    tests: dict[str, dict] = {}
    frontier = list(roots)
    truncated = False
    for level in range(1, depth + 1):
        following: list[str] = []
        for node in frontier:
            for edge in sorted(snap.callers(node), key=lambda e: str(e.get("from"))):
                caller = str(edge.get("from"))
                if edge.get("kind") == "import" or caller in seen:
                    continue
                if level > 1 and len(seen) - len(roots) >= IMPACT_MAX_NODES:
                    truncated = True
                    break
                seen.add(caller)
                if _is_test(snap, caller):
                    tests.setdefault(caller, {"rel": "test", "test": caller,
                                              "confidence": edge.get("confidence"),
                                              "depth": level})
                    continue
                found.append({"rel": "caller", "symbol": caller, "depth": level,
                              "via": node})
                following.append(caller)
        frontier = following
    for root in roots:
        for row in snap.tests(root):
            test = str(row.get("test"))
            if test not in tests:
                tests[test] = _test_row(row)
    modules = {module_of(row["symbol"]) for row in found}
    head.update(depth=depth,
                fan_in=sum(1 for row in found if row["depth"] == 1),
                upstream=len(found), modules=len(modules), tests=len(tests),
                truncated=truncated)
    if not found:
        head["callers"] = "UNKNOWN"
        head["note"] = UNKNOWN_CALLERS
    module_rows = [{"rel": "module", "module": m} for m in sorted(modules)]
    test_rows = sorted(tests.values(), key=lambda t: (-(t.get("confidence") or 0),
                                                      str(t.get("test"))))
    return Computed(head, found + module_rows + test_rows)


def tool_tests_for(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"files", "symbols", "cursor"}, set())
    files = [_file_arg(f) for f in _strings(args, "files")]
    names = _strings(args, "symbols")
    if not files and not names:
        raise ToolInputError("give files, symbols or both")
    tests: dict[str, dict] = {}
    unknown: list[str] = []

    def add(row: dict) -> None:
        test = str(row.get("test"))
        have = tests.get(test)
        confidence = row.get("confidence") or 0
        if have is None:
            tests[test] = {"test": test, "confidence": confidence,
                           "depth": row.get("depth"), "covers": 1}
            if row.get("evidence"):
                tests[test]["evidence"] = row["evidence"]
            return
        have["covers"] += 1
        have["confidence"] = max(have["confidence"] or 0, confidence)
        if row.get("depth") is not None and (have.get("depth") is None
                                             or row["depth"] < have["depth"]):
            have["depth"] = row["depth"]

    for name in names:
        try:
            symbol = resolve(snap, name)
        except _Unresolved:
            unknown.append(name)
            continue
        for row in snap.tests(symbol):
            add(row)
    for path in files:
        file = snap.file(path)
        if file is None:
            unknown.append(path)
            continue
        for symbol in snap.symbols_in(path):
            for row in snap.tests(str(symbol.get("id"))):
                add(row)
        for row in file.get("tests") or []:
            add(row)
    rows = sorted(tests.values(), key=lambda t: (-(t["confidence"] or 0),
                                                 t.get("depth") or 0, t["test"]))
    head: dict[str, Any] = {"inputs": len(files) + len(names),
                            "test_files": len({path_of(t) for t in tests})}
    if unknown:
        head["unknown"] = unknown[:SHOWN_CANDIDATES]
        head["unknown_count"] = len(unknown)
    return Computed(head, rows)


def _nearness(origin: str, other: str) -> tuple[int, str]:
    """Files sharing more leading directories with `origin` first."""
    common = posixpath.commonpath([origin, other])
    return (-(len(common.split("/")) if common else 0), other)


def tool_neighbours(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"symbol", "cursor"}, {"symbol"})
    symbol = resolve(snap, _string(args, "symbol"))
    row = snap.symbol(symbol) or {}
    kind = row.get("kind")
    path = path_of(symbol)

    def same_kind(rows: list[dict]) -> list[dict]:
        return sorted((r for r in rows if r.get("kind") == kind and r.get("id") != symbol),
                      key=lambda r: (str(r.get("path")), r.get("start_line") or 0))

    rows: list[dict] = [{"rel": "same_file", "symbol": r["id"], "line": r.get("start_line")}
                        for r in same_kind(snap.symbols_in(path))]
    community = snap.community_of(path)
    if community:
        others = sorted((str(f) for f in community.get("files") or [] if f != path),
                        key=lambda f: _nearness(path, f))[:NEIGHBOUR_FILES]
        for other in others:
            rows += [{"rel": "same_community", "symbol": r["id"], "line": r.get("start_line")}
                     for r in same_kind(snap.symbols_in(other))]
    test_files = sorted({path_of(str(t.get("test"))) for t in snap.tests(symbol)})
    rows += [{"rel": "test_file", "path": f} for f in test_files]
    head = {"symbol": symbol, "kind": kind,
            "community": str(community.get("id")) if community else None,
            "community_label": community.get("label") if community else None}
    return Computed(head, rows)


def tool_territory(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"files", "cursor"}, {"files"})
    files = [_file_arg(f) for f in _strings(args, "files")]
    if not files:
        raise ToolInputError("files must name at least one path")
    file_rows: list[dict] = []
    dependants: list[dict] = []
    communities: set[str] = set()
    for path in files:
        file = snap.file(path)
        if file is None:
            file_rows.append({"rel": "file", "path": path, "known": False})
            continue
        callers: set[str] = set()
        for target in [path] + [str(s.get("id")) for s in snap.symbols_in(path)]:
            for edge in snap.callers(target):
                source = path_of(str(edge.get("from")))
                if source != path:
                    callers.add(source)
        community = _community(snap, path)
        if community:
            communities.add(community)
        file_rows.append({"rel": "file", "path": path, "community": community,
                          "fan_in": len(callers), "test": bool(file.get("test"))})
        dependants += [{"rel": "dependant", "path": c, "of": path} for c in sorted(callers)]
    head = {"files": len(files), "communities": sorted(communities)[:SHOWN_CANDIDATES],
            "in_flight": IN_FLIGHT}
    return Computed(head, file_rows + dependants)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)[:SHOWN_TEXT]


def tool_similar_changes(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"files", "symbols", "cursor"}, set())
    files = [_file_arg(f) for f in _strings(args, "files")]
    names = _strings(args, "symbols")
    if not files and not names:
        raise ToolInputError("give files, symbols or both")
    unknown: list[str] = []
    for name in names:
        try:
            files.append(path_of(resolve(snap, name)))
        except _Unresolved:
            unknown.append(name)
    inputs = list(dict.fromkeys(files))
    head: dict[str, Any] = {"inputs": len(inputs) + len(unknown)}
    if unknown:
        head["unknown"] = unknown[:SHOWN_CANDIDATES]
    if not snap.has_layer("history"):
        head.update(history="UNKNOWN", note=f"{NO_HISTORY_LAYER}: {NO_HISTORY}")
        return Computed(head, [])
    changes: dict[str, dict] = {}
    fixes: dict[str, dict] = {}
    co_changes: dict[str, dict] = {}
    silent: list[str] = []
    for path in inputs:
        found = snap.history(path)
        if not found:
            silent.append(path)
        for row in found:
            kind = row.get("kind")
            if kind == "change":
                key = str(row.get("pr") if row.get("pr") is not None else row.get("sha"))
                have = changes.get(key)
                if have is None:
                    touched = [str(f) for f in row.get("files") or []]
                    have = changes[key] = {
                        "rel": "change", "pr": row.get("pr"), "title": _text(row.get("title")),
                        "merged_at": row.get("merged_at"),
                        "fix_round": bool(row.get("fix_round")), "touches": [],
                        "files": touched[:SHOWN_FILES], "files_total": len(touched)}
                    if row.get("pr") is None:
                        have["sha"] = row.get("sha")
                if path not in have["touches"]:
                    have["touches"].append(path)
            elif kind == "fix":
                key = str(row.get("sha"))
                if key not in fixes:
                    fixes[key] = {"rel": "fix", "sha": row.get("sha"),
                                  "subject": _text(row.get("subject")), "path": path}
                    for extra in ("pr", "test"):
                        if row.get(extra) is not None:
                            fixes[key][extra] = row[extra]
            elif kind == "co_change":
                other = str(row.get("other"))
                support = int(row.get("support") or 0)
                if other in inputs or support <= co_changes.get(other, {}).get("support", -1):
                    continue
                co_changes[other] = {"rel": "co_change", "path": other, "with": path,
                                     "support": support, "confidence": row.get("confidence")}
    for change in changes.values():
        change["touches"].sort()
    change_rows = sorted(changes.values(), reverse=True,
                         key=lambda r: (len(r["touches"]), str(r.get("merged_at") or ""),
                                        int(r.get("pr") or 0)))
    co_rows = sorted(co_changes.values(), key=lambda r: (-r["support"],
                                                         -(r.get("confidence") or 0),
                                                         r["path"]))
    rows = list(fixes.values()) + change_rows + co_rows
    head.update(changes=len(change_rows), fixes=len(fixes), co_changes=len(co_rows))
    if silent:
        head["no_history"] = silent[:SHOWN_CANDIDATES]
    if not rows:
        head.update(history="UNKNOWN", note=NO_HISTORY)
    return Computed(head, rows)


@functools.lru_cache(maxsize=4096)
def _glob(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` and `?` do not; a trailing `/` is the directory."""
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out))


def glob_match(pattern: str, path: str) -> bool:
    clean = pattern.strip()
    while clean.startswith("./"):
        clean = clean[2:]
    return bool(clean) and _glob(clean.lstrip("/")).fullmatch(path) is not None


def tool_rules_for(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"files", "cursor"}, {"files"})
    files = list(dict.fromkeys(_file_arg(f) for f in _strings(args, "files")))
    if not files:
        raise ToolInputError("files must name at least one path")
    head: dict[str, Any] = {"files": len(files)}
    if not snap.has_layer("rules"):
        head.update(rules="UNKNOWN", note=f"{NO_RULES_LAYER}: {NO_RULES}")
        return Computed(head, [])
    found: dict[tuple[str, str], dict] = {}
    matched: set[str] = set()
    for rule in snap.rules():
        pattern = str(rule.get("glob") or rule.get("path") or "")
        hits = [f for f in files if glob_match(pattern, f)]
        if not hits:
            continue
        kind = str(rule.get("kind") or "rule")
        key = (kind, str(rule.get("ref")))
        have = found.get(key)
        if have is None:
            have = found[key] = {"rel": kind, "ref": rule.get("ref"),
                                 "why": _text(rule.get("why")), "glob": pattern,
                                 "files": [], "confidence": rule.get("confidence")}
            if rule.get("evidence") is not None:
                have["evidence"] = rule["evidence"]
        have["files"] += [f for f in hits if f not in have["files"]]
        have["confidence"] = max(have.get("confidence") or 0, rule.get("confidence") or 0)
        matched.update(hits)
    blocked = sorted({f for row in found.values() if row["rel"] == "cannot_push"
                      for f in row["files"]})
    rows = sorted(found.values(), key=lambda r: (RULE_ORDER.get(r["rel"], len(RULE_ORDER)),
                                                 -len(r["files"]),
                                                 -(r.get("confidence") or 0), str(r["ref"])))
    for row in rows:
        row["files_total"] = len(row["files"])
        row["files"] = sorted(row["files"])[:SHOWN_FILES]
    head.update(cannot_push=blocked[:SHOWN_CANDIDATES],
                guards=sum(1 for r in rows if r["rel"] == "guard"),
                docs=sum(1 for r in rows if r["rel"] == "doc"))
    if len(blocked) > SHOWN_CANDIDATES:
        head["cannot_push_count"] = len(blocked)
    silent = [f for f in files if f not in matched]
    if silent:
        head["no_rules"] = silent[:SHOWN_CANDIDATES]
    if not rows:
        head.update(rules="UNKNOWN", note=NO_RULES)
    return Computed(head, rows)


def _roots(snap: Snapshot, target: str) -> tuple[list[str], dict[str, Any]]:
    """(the symbols a walk starts from, the head naming them): every symbol a
    file defines when `target` is an indexed file, else the one it resolves to."""
    if "#" not in target and snap.file(_file_arg(target)) is not None:
        target = _file_arg(target)
        roots = [s["id"] for s in snap.symbols_in(target) if s.get("kind") != "local"]
        return roots, {"file": target, "symbols": len(roots)}
    root = resolve(snap, target)
    return [root], {"symbol": root}


def _walk(snap: Snapshot, roots: list[str], depth: int, upstream: bool,
          budget: int) -> tuple[list[dict], int, bool]:
    """(edges breadth-first, symbols reached, truncated) of one direction.

    Import edges are not calls and are left out, as `impact` leaves them. As in
    `impact`, the first level is always read whole and deeper levels stop once
    `budget` symbols have been reached.
    """
    seen = set(roots)
    edges: list[dict] = []
    frontier = list(roots)
    reached = 0
    truncated = False
    near, far = ("to", "from") if upstream else ("from", "to")
    for level in range(1, depth + 1):
        following: list[str] = []
        for node in frontier:
            found = snap.callers(node) if upstream else snap.callees(node)
            for edge in sorted(found, key=lambda e: (str(e.get(far)), e.get("line") or 0,
                                                     str(e.get("kind")))):
                if edge.get("kind") == "import":
                    continue
                other = str(edge.get(far))
                if other not in seen:
                    if level > 1 and reached >= budget:
                        truncated = True
                        break
                    seen.add(other)
                    reached += 1
                    following.append(other)
                edges.append({near: node, far: other, "kind": edge.get("kind"),
                              "depth": level, "confidence": edge.get("confidence")})
        frontier = following
    return [{"from": e["from"], "to": e["to"], "kind": e["kind"], "depth": e["depth"],
             "confidence": e["confidence"]} for e in edges], reached, truncated


def tool_call_graph(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"symbol", "direction", "depth", "cursor"}, {"symbol"})
    target = _string(args, "symbol")
    direction = args.get("direction", "both")
    if direction not in CALL_GRAPH_DIRECTIONS:
        raise ToolInputError(f"direction must be one of {', '.join(CALL_GRAPH_DIRECTIONS)}")
    depth = _int(args, "depth", CALL_GRAPH_DEFAULT_DEPTH, 1, IMPACT_MAX_DEPTH)
    roots, head = _roots(snap, target)
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    budget = IMPACT_MAX_NODES
    truncated = False
    callers = 0
    for upstream in ((True, False) if direction == "both" else (direction == "callers",)):
        edges, reached, cut = _walk(snap, roots, depth, upstream, budget)
        budget = max(budget - reached, 0)
        truncated = truncated or cut
        for edge in edges:
            key = (edge["from"], edge["to"], str(edge["kind"]))
            if key not in seen:
                seen.add(key)
                rows.append(edge)
        if upstream:
            callers = len(edges)
    nodes = set(roots) | {e["from"] for e in rows} | {e["to"] for e in rows}
    head.update(direction=direction, depth=depth, nodes=len(nodes), edges=len(rows),
                truncated=truncated)
    if direction != "callees" and not callers:
        head["callers"] = "UNKNOWN"
        head["note"] = UNKNOWN_CALLERS
    return Computed(head, rows)


def tool_index(snap: Snapshot, args: dict) -> Computed:
    args = _args(args, {"layer", "key", "path", "cursor"}, {"layer"})
    layer = args.get("layer")
    if layer not in ALL_LAYERS:
        raise ToolInputError(f"layer must be one of {', '.join(ALL_LAYERS)}")
    if "key" in args and "path" in args:
        raise ToolInputError("give key or path, not both")
    head: dict[str, Any] = {"layer": layer}
    if "path" in args:
        if layer == DOCUMENT_LAYER:
            raise ToolInputError("the document layer is keyed by section: pass key")
        path = _file_arg(_string(args, "path"))
        key = module_of(path)
        head.update(path=path, key=key)
    elif "key" in args:
        key = _string(args, "key")
        head["key"] = key
    else:
        if not snap.has_layer(layer):
            head.update(keys=0, note=NO_LAYER.format(layer=layer))
            return Computed(head, [])
        keys = snap.keys(layer)
        head["keys"] = len(keys)

        def count(key: str) -> dict:
            records = (snap.entry(layer, key) or {}).get("records")
            return {"key": key, "rows": records if isinstance(records, int) else "UNKNOWN"}

        return Computed(head, keys, count)
    if not snap.has_layer(layer):
        raise ToolInputError(NO_LAYER.format(layer=layer))
    if snap.entry(layer, key) is None:
        raise ToolInputError(f"the {layer} layer has no shard {key!r}; call index with "
                             "the layer alone for its keys")
    return Computed(head, snap.shard(layer, key).rows)


HANDLERS: dict[str, Callable[[Snapshot, Any], Computed]] = {
    "search": tool_search,
    "context": tool_context,
    "impact": tool_impact,
    "tests_for": tool_tests_for,
    "neighbours": tool_neighbours,
    "territory": tool_territory,
    "similar_changes": tool_similar_changes,
    "rules_for": tool_rules_for,
    "call_graph": tool_call_graph,
    "index": tool_index,
}


def compute(name: str, snap: Snapshot, arguments: Any) -> Computed:
    """One tool's answer before paging, from the snapshot and the arguments
    alone. Raises `ToolInputError`, `_Unresolved` or `SnapshotError`."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolInputError(f"no tool {name!r}; the tools are {', '.join(TOOL_NAMES)}")
    return handler(snap, arguments)


def call(snap: Snapshot, obs: Observation, name: str, arguments: Any) -> tuple[str, bool]:
    """(answer text, is_error). Every outcome is an answer with freshness first."""
    try:
        computed = compute(name, snap, arguments)
        return page(obs, computed.head, computed.rows, _cursor(arguments or {}),
                    computed.expand), False
    except _Unresolved as exc:
        return error_answer(obs, str(exc), exc.candidates), True
    except (ToolInputError, SnapshotError) as exc:
        return error_answer(obs, str(exc)), True
