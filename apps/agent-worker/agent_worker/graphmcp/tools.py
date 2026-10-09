"""The six tools, their schemas, and the 4 KiB answer with paging (§4.2, §7.7).

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

`territory` answers what a local snapshot can know: each file's community,
how many other files depend on it and which. Which in-flight steps and lanes
hold a file is phase 3's (§4.8, served by swarm-api) and is not in a snapshot;
the answer says so in `in_flight` rather than leaving it out silently.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any, Callable

from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import Snapshot, SnapshotError, module_of, name_of, path_of

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

UNKNOWN_CALLERS = ("zero resolved callers is UNKNOWN, never safe: a call through a "
                   "dynamic dispatch, a string or a module object can be invisible")
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

def _mark(row: dict, obs: Observation) -> dict:
    key = row.get("path") or row.get("symbol") or row.get("test") or ""
    why = obs.stale(path_of(str(key))) if key else None
    if why:
        row = {**row, "stale": why}
    return row


def page(obs: Observation, head: dict, rows: list[Any], offset: int,
         expand: Callable[[Any], dict] | None = None) -> str:
    """The answer for rows[offset:], as many as fit in `ANSWER_CAP` bytes."""
    total = len(rows)
    if offset > total:
        raise ToolInputError(f"cursor {offset} is past the last row ({total})")
    shell = {"freshness": obs.block, **head, "total": total, "rows": [],
             "next_cursor": str(total)}
    size = len(dumps(shell))
    if size > ANSWER_CAP:
        raise ToolInputError("the answer's head alone is over the 4 KiB cap; ask for less")
    out: list[dict] = []
    index = offset
    while index < total:
        row = _mark(expand(rows[index]) if expand else rows[index], obs)
        piece = len(dumps(row)) + (1 if out else 0)
        if size + piece > ANSWER_CAP:
            break
        out.append(row)
        size += piece
        index += 1
    if not out and index < total:
        # One row over the cap on its own: say so in its place and move on,
        # so paging always advances.
        out.append({"omitted": "one row over the 4 KiB answer cap"})
        index += 1
    answer = {"freshness": obs.block, **head, "total": total, "rows": out,
              "next_cursor": str(index) if index < total else None}
    text = dumps(answer)
    assert len(text) <= ANSWER_CAP, len(text)
    return text


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


def tool_search(snap: Snapshot, obs: Observation, args: dict) -> str:
    args = _args(args, {"query", "limit", "cursor"}, {"query"})
    query = _string(args, "query")
    limit = _int(args, "limit", SEARCH_DEFAULT, 1, SEARCH_MAX)
    hits = snap.search(query, limit)

    def expand(hit: tuple[str, float]) -> dict:
        symbol, score = hit
        return {**_describe(snap, symbol), "score": score,
                "community": _community(snap, path_of(symbol))}

    return page(obs, {"query": query}, hits, _cursor(args), expand)


def tool_context(snap: Snapshot, obs: Observation, args: dict) -> str:
    args = _args(args, {"symbol", "cursor"}, {"symbol"})
    symbol = resolve(snap, _string(args, "symbol"))
    row = snap.symbol(symbol) or {}
    callers = sorted((e for e in snap.callers(symbol) if e.get("kind") != "import"),
                     key=lambda e: (str(e.get("from")), e.get("line") or 0))
    callees = sorted(snap.callees(symbol), key=lambda e: (str(e.get("to")), e.get("line") or 0))
    tests = sorted(snap.tests(symbol), key=lambda t: (-(t.get("confidence") or 0),
                                                      str(t.get("test"))))
    signature = snap.signature(symbol)
    head = _mark({"symbol": symbol, "kind": row.get("kind"),
                  "lines": [row.get("start_line"), row.get("end_line")],
                  "signature": signature.get("signature") if signature else None,
                  "community": _community(snap, path_of(symbol)),
                  "callers": len(callers) if callers else "UNKNOWN",
                  "callees": len(callees), "tests": len(tests)}, obs)
    if not callers:
        head["note"] = UNKNOWN_CALLERS
    rows = ([_edge_row("caller", e, "from") for e in callers]
            + [_edge_row("callee", e, "to") for e in callees]
            + [_test_row(t) for t in tests])
    return page(obs, head, rows, _cursor(args))


def _is_test(snap: Snapshot, symbol_id: str) -> bool:
    row = snap.symbol(symbol_id)
    if row is not None and row.get("kind") == "test":
        return True
    file = snap.file(path_of(symbol_id))
    return bool(file and file.get("test"))


def tool_impact(snap: Snapshot, obs: Observation, args: dict) -> str:
    args = _args(args, {"symbol", "depth", "cursor"}, {"symbol"})
    target = _string(args, "symbol")
    depth = _int(args, "depth", IMPACT_DEFAULT_DEPTH, 1, IMPACT_MAX_DEPTH)
    if "#" not in target and snap.file(_file_arg(target)) is not None:
        target = _file_arg(target)
        roots = [s["id"] for s in snap.symbols_in(target) if s.get("kind") != "local"]
        head: dict[str, Any] = {"file": target, "symbols": len(roots)}
    else:
        roots = [resolve(snap, target)]
        head = {"symbol": roots[0]}
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
    why = obs.stale(path_of(target if "file" in head else roots[0]))
    if why:
        head["stale"] = why
    module_rows = [{"rel": "module", "module": m} for m in sorted(modules)]
    test_rows = sorted(tests.values(), key=lambda t: (-(t.get("confidence") or 0),
                                                      str(t.get("test"))))
    return page(obs, head, found + module_rows + test_rows, _cursor(args))


def tool_tests_for(snap: Snapshot, obs: Observation, args: dict) -> str:
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
    return page(obs, head, rows, _cursor(args))


def _nearness(origin: str, other: str) -> tuple[int, str]:
    """Files sharing more leading directories with `origin` first."""
    common = posixpath.commonpath([origin, other])
    return (-(len(common.split("/")) if common else 0), other)


def tool_neighbours(snap: Snapshot, obs: Observation, args: dict) -> str:
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
    head = _mark({"symbol": symbol, "kind": kind,
                  "community": str(community.get("id")) if community else None,
                  "community_label": community.get("label") if community else None}, obs)
    return page(obs, head, rows, _cursor(args))


def tool_territory(snap: Snapshot, obs: Observation, args: dict) -> str:
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
    return page(obs, head, file_rows + dependants, _cursor(args))


HANDLERS: dict[str, Callable[[Snapshot, Observation, Any], str]] = {
    "search": tool_search,
    "context": tool_context,
    "impact": tool_impact,
    "tests_for": tool_tests_for,
    "neighbours": tool_neighbours,
    "territory": tool_territory,
}


def call(snap: Snapshot, obs: Observation, name: str, arguments: Any) -> tuple[str, bool]:
    """(answer text, is_error). Every outcome is an answer with freshness first."""
    handler = HANDLERS.get(name)
    if handler is None:
        return error_answer(obs, f"no tool {name!r}; the tools are {', '.join(TOOL_NAMES)}"), True
    try:
        return handler(snap, obs, arguments), False
    except _Unresolved as exc:
        return error_answer(obs, str(exc), exc.candidates), True
    except (ToolInputError, SnapshotError) as exc:
        return error_answer(obs, str(exc)), True
