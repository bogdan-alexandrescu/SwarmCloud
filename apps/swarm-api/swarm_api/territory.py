"""Search, communities and territory over a promoted graph (docs/design/knowledge-graph.md §6, lane KG3).

KG2's extractor version 3 stores five more layers beside the graph's own,
under the manifest's `index_shards` (repo_graph_shards.py, "FORMAT 3"):
`communities`, `terms` (BM25 postings by term bucket), `signatures`,
`signature_changes` and `flows`. This module is the API's reader of them, and
the three routes in `routes/repositories.py` answer from it:

  POST /v1/repositories/{repo_id}/search       BM25 symbol search (§4.1)
  GET  /v1/repositories/{repo_id}/communities  the module communities (§4.7)
  POST /v1/repositories/{repo_id}/territory    files -> expanded territory,
                                               seams, overlap with open pull
                                               requests and in-flight lanes
                                               (§4.6, §4.8)

WHY EACH RULE:

  * EVERY READ IS THE CALLER'S TENANT'S (invariant 9, §5.3). The graph is
    opened through `repograph.RepoGraph.open`, which builds every key from the
    tenant and repository the caller is scoped to and checks the manifest's
    digest; an index shard is a blob that manifest names, read through the
    same digest-checked loader. The open pull requests are read with the
    tenant's own R2-resolved token (`impact.resolve_read_token`), and the
    lanes are the tenant's own live issue runs (`IssueRuns.live` filters by
    tenant twice). Nothing here takes a bucket, a key, a token or another
    tenant's id from the caller.
  * A TERRITORY IS BUILT FROM FACTS, NEVER FROM JUDGEMENT (§7.8). A caller
    edge counts only with `ast`, `lsp` or `import` evidence -- the edges
    KG2's communities are drawn from -- and at or above
    FORCED_MIN_CONFIDENCE. A `declared`, `path-ref`, `naming` or `co-change`
    edge, or a 0.3 ambiguous guess, is counted in `cut` and never widens the
    territory, because the orchestrator's dispatch warning (KG7) is a gate.
  * "NOT READ" IS NEVER "NO OVERLAP". A pull request whose files GitHub did
    not give is listed in `pull_requests_unread`; a forge failure leaves
    `pull_requests_read.ok` false with the failure's code; a named file the
    graph has no record of is in `unknown`. A short answer says it is short.
  * SEAMS ARE RANKED, NOT LISTED BY HAND (§4.1 "seams"). A seam is a file
    nearly every lane must touch: one everything registers in (`main.py`
    imports every router: high fan-OUT), one every shape lives in
    (`schemas.py`: high fan-IN), or one changed with many others
    (`hot_spots`' co-change). `hot_spots` alone counts edits, not
    dependants, which is why §4.1 replaces it.
  * THE TOKENIZER IS RESTATED FROM THE WRITER, AND A TEST HOLDS THEM EQUAL.
    The writer runs in the indexer image, which swarm-api does not import;
    `repograph.py` restates the shard format the same way. A query split
    into other terms than the postings were built with finds nothing, so a
    manifest whose `term_stats.tokenizer` is not TOKENIZER_VERSION is
    searched by substring and says so. tests/unit/control_plane/
    test_repository_territory.py runs both tokenizers and both scorers over
    the same inputs.
  * A WHOLE-REPOSITORY TABLE IS COMPUTED ONCE PER DIGEST (`RepoGraph.view`),
    as the module graph is: the seam ranks read every `callees` and `files`
    shard and the index document, and a promoted graph cannot change under
    its digest. The manifest is still read and checked on every request.
  * THE OPEN PULL REQUESTS ARE KEPT OPEN_PULLS_TTL_SECONDS. One forge read is
    an issue listing, a pull listing and up to 30 files reads
    (`forge.GitHubIssues.open_work`): seconds, against a graph answer of
    milliseconds. A dispatch of a batch asks once per brief, within a minute.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import math
import re
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import forge as _forge
from .errors import NotFound, ValidationFailed
from .impact import is_test_file, resolve_read_token
from .issueruns import MAX_STEP_FILES, TERMINAL_RUN_STATES, IssueRuns
from .repograph import PREFETCH_WORKERS, Graph, InvalidGraph, module_of
from .validation import IssueRef

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# format 3 (restated from images/agent-runtime-indexer/repo-index/repo_graph_shards.py)
# --------------------------------------------------------------------------

#: The first shard format that carries `index_shards` (repo_graph_shards.FORMAT_VERSION).
INDEX_FORMAT = 3
#: repo_graph_shards.INDEX_LAYERS: the layers under `index_shards`.
INDEX_LAYERS = ("communities", "terms", "signatures", "signature_changes", "flows")
#: repo_graph_shards.WHOLE: the key of a layer stored as one shard.
WHOLE = "*"
#: repo_graph_shards.TERM_BUCKET_CHARS.
TERM_BUCKET_CHARS = 2
#: repo_graph_shards.TOKENIZER_VERSION, BM25_K1, BM25_B.
TOKENIZER_VERSION = "1"
BM25_K1 = 1.2
BM25_B = 0.75
_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*|[0-9]+")
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "if", "in", "into",
    "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were",
    "where", "which", "with", "what", "when", "how", "def", "self", "cls", "none",
    "return", "returns", "true", "false", "str", "int", "dict", "list", "any",
    "not", "no", "so", "one", "every", "but", "than", "then", "there", "these", "those",
    "has", "have", "can", "may", "must", "do", "does", "here", "only", "also", "all",
})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _stem(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


@functools.lru_cache(maxsize=1 << 14)
def _word_terms(word: str) -> tuple[str, ...]:
    out = []
    for part in _CAMEL.findall(word):
        lowered = part.lower()
        if lowered in STOPWORDS:
            continue
        token = _stem(lowered)
        if len(token) >= 2 and token not in STOPWORDS:
            out.append(token)
    return tuple(out)


def tokenize(text: str) -> list[str]:
    """repo_graph_shards.tokenize: `planner_prompt` and "the planner's prompt"
    both give `planner`, `prompt`."""
    out: list[str] = []
    for word in _WORD.findall(text or ""):
        out.extend(_word_terms(word))
    return out


def term_bucket(term: str) -> str:
    """repo_graph_shards.term_bucket: the `terms` shard a term's postings live in."""
    return hashlib.sha256(term.encode("utf-8")).hexdigest()[:TERM_BUCKET_CHARS]


def bm25_search(rows: Iterable[dict], stats: Mapping[str, Any], query: str,
                limit: int = 20) -> list[tuple[str, float]]:
    """repo_graph_shards.bm25_search: (symbol id, score), best first."""
    wanted = set(tokenize(query))
    documents = max(int(stats.get("documents") or 0), 1)
    average = float(stats.get("average_length") or 1.0) or 1.0
    k1 = float(stats.get("k1") or BM25_K1)
    b = float(stats.get("b") if stats.get("b") is not None else BM25_B)
    scores: dict[str, float] = {}
    for row in rows:
        if row.get("term") not in wanted:
            continue
        postings = [(f"{path}#{name}", tf, dl)
                    for path, entries in sorted((row.get("postings") or {}).items())
                    for name, tf, dl in entries]
        df = len(postings)
        idf = math.log(1.0 + (documents - df + 0.5) / (df + 0.5))
        for symbol, tf, dl in postings:
            norm = tf + k1 * (1.0 - b + b * dl / average)
            scores[symbol] = scores.get(symbol, 0.0) + idf * tf * (k1 + 1.0) / norm
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(symbol, round(score, 4)) for symbol, score in ranked[:max(limit, 0)]]


# --------------------------------------------------------------------------
# reading the index layers
# --------------------------------------------------------------------------

def format_version(graph: Graph) -> int:
    """3 for a manifest KG2's writer wrote; 2 for one without `format_version`."""
    value = graph.manifest.get("format_version")
    return value if isinstance(value, int) and not isinstance(value, bool) else 2


def _index_table(graph: Graph) -> dict[str, dict[str, Any]]:
    if format_version(graph) < INDEX_FORMAT:
        return {}
    table = graph.manifest.get("index_shards")
    if not isinstance(table, dict):
        raise InvalidGraph("a format-3 graph manifest has no index_shards")
    if set(table) - set(INDEX_LAYERS):
        raise InvalidGraph("the graph manifest's index_shards are not the writer's layers")
    for layer in table.values():
        if not isinstance(layer, dict):
            raise InvalidGraph("a graph manifest index layer is not an object")
        for entry in layer.values():
            if not isinstance(entry, dict) or not _DIGEST.match(str(entry.get("blob") or "")):
                raise InvalidGraph("a graph manifest index shard names no sha256 blob")
    return table


def index_rows(graph: Graph, layer: str, keys: Iterable[str]) -> list[dict[str, Any]]:
    """The rows of `layer`'s shards named `keys`, read concurrently.

    Each blob goes through `Graph._load`, the one reader that checks a blob
    against its name before it is inflated, within the same bounds as the
    graph's own layers; `repograph.py` gives that loader no public name yet.
    """
    if layer not in INDEX_LAYERS:
        raise InvalidGraph(f"{layer!r} is not a graph index layer")
    entries = _index_table(graph).get(layer) or {}
    blobs = list(dict.fromkeys(entries[k]["blob"] for k in sorted(set(keys)) if k in entries))
    if not blobs:
        return []
    if len(blobs) == 1:
        return list(graph._load(blobs[0]))
    with ThreadPoolExecutor(max_workers=min(PREFETCH_WORKERS, len(blobs)),
                            thread_name_prefix="repo-index-layer") as pool:
        futures = [pool.submit(graph._load, blob) for blob in blobs]
    return [row for future in futures for row in future.result()]


def _shards(graph: Graph, layer: str, modules: Iterable[str]) -> dict[str, list[dict]]:
    """`graph.shard(layer, m)` for each module, the cold ones read concurrently."""
    wanted = sorted(set(modules))
    if len(wanted) > 1:
        with ThreadPoolExecutor(max_workers=min(PREFETCH_WORKERS, len(wanted)),
                                thread_name_prefix="repo-graph") as pool:
            futures = [pool.submit(graph.shard, layer, m) for m in wanted]
        return {m: f.result() for m, f in zip(wanted, futures)}
    return {m: graph.shard(layer, m) for m in wanted}


def community_rows(graph: Graph) -> list[dict[str, Any]]:
    return [r for r in index_rows(graph, "communities", [WHOLE]) if isinstance(r, dict)]


def membership(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """file path -> {id, label} of its community."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        for path in row.get("files") or []:
            out[str(path)] = {"id": str(row.get("id")), "label": row.get("label")}
    return out


# --------------------------------------------------------------------------
# GET .../communities
# --------------------------------------------------------------------------

def communities_table(graph: Graph, community_id: str | None = None) -> dict[str, Any]:
    """KG2's communities, one row each with its files; or the one `community_id` names."""
    if format_version(graph) < INDEX_FORMAT:
        return {"communities": [], "format_version": format_version(graph),
                "reason": "this graph was stored before extractor version 3, which draws "
                          "communities; index the repository again"}
    rows = sorted(community_rows(graph), key=lambda r: str(r.get("id")))
    if not rows:
        # The writer stores a version-2 document as format 3 with empty index
        # layers: the format alone does not say the partition was drawn.
        return {"communities": [], "format_version": format_version(graph),
                "reason": "this graph has no communities: its index was extracted before "
                          "version 3, or found no connected application files"}
    out = [{"id": str(r.get("id")), "label": r.get("label"),
            "size": int(r.get("size") or len(r.get("files") or [])),
            "symbols": int(r.get("symbols") or 0), "cohesion": r.get("cohesion"),
            "files": sorted(str(p) for p in r.get("files") or [])} for r in rows]
    if community_id is not None:
        out = [r for r in out if r["id"] == community_id]
        if not out:
            raise NotFound(f"no community {community_id[:100]!r} in this graph")
    return {"communities": out, "format_version": format_version(graph), "reason": None}


# --------------------------------------------------------------------------
# POST .../search
# --------------------------------------------------------------------------

SEARCH_LIMIT_DEFAULT = 20
SEARCH_LIMIT_MAX = 100
#: An issue's title and body is the planner's query (§4.1); a few KB of it is
#: plenty to rank by, and bounds the term buckets one query reads.
MAX_QUERY_CHARS = 4_000


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(default=SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX)
    #: A kept version's commit; the current one when absent.
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


def _symbol_row(s: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": s.get("id"), "kind": s.get("kind"), "path": s.get("path"),
            "start_line": s.get("start_line"), "end_line": s.get("end_line"),
            "language": s.get("language")}


def search(graph: Graph, q: str, limit: int = SEARCH_LIMIT_DEFAULT) -> dict[str, Any]:
    """The symbols that best match `q`, by BM25 over KG2's term index.

    Reads only the term buckets of the query's own terms, then the symbols
    shards of the hits' modules. A graph without the index (format 2), or one
    indexed with another tokenizer, is searched by substring over symbol ids,
    as `GET .../symbols?q=` does, and `method` and `reason` say so.
    """
    terms = sorted(set(tokenize(q)))
    stats = graph.manifest.get("term_stats") or {}
    reason = None
    if format_version(graph) < INDEX_FORMAT:
        reason = ("this graph was stored before extractor version 3, which builds the "
                  "term index; searched by substring instead")
    elif str(stats.get("tokenizer")) != TOKENIZER_VERSION:
        reason = (f"the term index was built with tokenizer {str(stats.get('tokenizer'))[:20]!r},"
                  f" not {TOKENIZER_VERSION!r}; searched by substring instead")
    if reason is not None:
        return {**_substring(graph, q, limit), "terms": terms, "reason": reason}
    rows = index_rows(graph, "terms", [term_bucket(t) for t in terms])
    ranked = bm25_search(rows, stats, q, limit + 1)
    more = len(ranked) > limit
    ranked = ranked[:limit]
    by_module = _shards(graph, "symbols", [module_of(sid) for sid, _ in ranked])
    rows_by_id = {str(s.get("id")): s for shard in by_module.values() for s in shard}
    member = membership(community_rows(graph)) if ranked else {}
    hits = []
    for symbol_id, score in ranked:
        row = rows_by_id.get(symbol_id) or {"id": symbol_id, "path": symbol_id.split("#", 1)[0]}
        hit = {**_symbol_row(row), "id": symbol_id, "score": score,
               "module": module_of(symbol_id),
               "community": member.get(str(row.get("path")))}
        hits.append(hit)
    return {"method": "bm25", "terms": terms, "symbols": hits, "more": more, "reason": None}


def _substring(graph: Graph, q: str, limit: int) -> dict[str, Any]:
    needle = q.strip().lower()
    graph.prefetch(["symbols"])
    hits = [_symbol_row(s) for m in graph.modules("symbols") for s in graph.symbols(m)
            if needle and needle in str(s.get("id")).lower()]
    hits.sort(key=lambda s: str(s["id"]))
    rows = [{**h, "score": None, "module": module_of(str(h["id"])), "community": None}
            for h in hits[:limit]]
    return {"method": "substring", "symbols": rows, "more": len(hits) > limit}


# --------------------------------------------------------------------------
# POST .../territory
# --------------------------------------------------------------------------

#: A caller edge widens a territory only with this evidence: what the code
#: says (KG2's COMMUNITY_EVIDENCE), never a judged edge (§7.8).
FACT_EVIDENCE = frozenset({"ast", "lsp", "import"})
#: The edge kinds a change to the callee can force a change at.
FORCED_KINDS = frozenset({"call", "reference", "inherit", "route_handler", "import"})
#: A unique `ast` match (0.6), `lsp` and `import` (0.4) count; the ambiguous
#: 0.3 guesses do not -- the same floor KG2's flows and the 256 MiB ceiling
#: use, so a common method name does not pull half the repository in.
FORCED_MIN_CONFIDENCE = 0.4
#: Caller depth. 1 is the call sites a signature change forces (§4.8); 2 is
#: the callers of those, for a caller that wants the wider blast radius.
TERRITORY_DEPTH_DEFAULT = 1
TERRITORY_DEPTH_MAX = 2
#: Expanded paths kept, callers and tests together. This repository's most
#: depended-on file (`swarm_common/models.py`) has a few hundred dependants;
#: past the bound the answer says `truncated`.
MAX_EXPANDED_FILES = 400
#: Symbols one walk may visit, so a prefix of the whole tree stays bounded.
MAX_WALK_SYMBOLS = 5_000
#: Shared paths listed per overlapping pull request or lane.
MAX_SHARED_PER_ITEM = 50
#: A seam ranks in the top SEAM_TOP_SHARE of application files by fan-in or
#: fan-out, with at least SEAM_MIN_DEGREE distinct dependants (or
#: dependencies). On this repository's 490 connected application files that
#: is about ten of each: the registration files and the shared shapes.
SEAM_TOP_SHARE = 0.02
SEAM_MIN_DEGREE = 5
#: ...or is one of the index's SEAM_HOT_SPOTS most-changed files and was
#: changed together with at least SEAM_MIN_CHANGED_WITH others.
SEAM_HOT_SPOTS = 10
SEAM_MIN_CHANGED_WITH = 3
#: Live issue runs read for the lane overlap; past it, `lanes_truncated`.
MAX_LIVE_RUNS = 100
#: How long one tenant's open pull requests are reused (see the module doc).
OPEN_PULLS_TTL_SECONDS = 60.0

_UNSAFE = re.compile(r"[\x00-\x1f\x7f*?\[\]{}\\]")


def normalise_entry(value: str) -> str:
    """A repository path, or a directory prefix ending in `/` (lane-queue.md §4.1)."""
    raw = (value or "").strip()
    if not raw or len(raw) > 400 or _UNSAFE.search(raw):
        raise ValueError("a territory entry is a repository path or a directory prefix "
                         "ending in /, 1-400 characters, with no glob or control character")
    prefix = raw.endswith("/")
    path = raw[2:] if raw.startswith("./") else raw
    if path.startswith("/") or any(part in ("", ".", "..") for part in path.rstrip("/").split("/")):
        raise ValueError(f"{raw[:100]!r} is not a normalised repository path")
    return path.rstrip("/") + "/" if prefix else path


def conflicts(a: str, b: str) -> bool:
    """Two entries conflict when equal, or when one is a directory prefix of the other."""
    if a == b:
        return True
    return (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


class TerritoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The paths a step, a lane or a brief will edit: paths or `dir/` prefixes.
    files: list[str] = Field(min_length=1, max_length=MAX_STEP_FILES)
    depth: int = Field(default=TERRITORY_DEPTH_DEFAULT, ge=1, le=TERRITORY_DEPTH_MAX)
    #: Read the repository's open pull requests and report the overlap.
    pull_requests: bool = True
    #: Read the tenant's live issue runs on this repository and report the overlap.
    lanes: bool = True
    #: The caller's own run and pull request, left out of the overlap.
    exclude_runs: list[str] = Field(default_factory=list, max_length=20)
    exclude_pull_requests: list[int] = Field(default_factory=list, max_length=20)
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @field_validator("files")
    @classmethod
    def _normalised(cls, files: list[str]) -> list[str]:
        return sorted(dict.fromkeys(normalise_entry(f) for f in files))

    @field_validator("exclude_runs")
    @classmethod
    def _run_ids(cls, runs: list[str]) -> list[str]:
        for run_id in runs:
            if not re.match(r"^[A-Za-z0-9_-]{1,64}$", run_id or ""):
                raise ValueError("a run id is 1-64 letters, digits, - or _")
        return runs


def _fact(edge: Mapping[str, Any]) -> bool:
    try:
        confidence = float(edge.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (edge.get("kind") in FORCED_KINDS and edge.get("evidence") in FACT_EVIDENCE
            and confidence >= FORCED_MIN_CONFIDENCE)


def _path(node: Any) -> str:
    return str(node).split("#", 1)[0]


def seam_table(graph: Graph, document: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """path -> {fan_in, fan_out, changes, reasons} for every seam of the repository.

    Whole-repository: every `files` and `callees` shard, and the index
    document's `hot_spots`. Computed once per digest by the caller's
    `RepoGraph.view`.
    """
    graph.prefetch(["files", "callees"])
    tests: set[str] = set()
    for module in graph.modules("files"):
        for row in graph.shard("files", module):
            path = str(row.get("path"))
            if row.get("test") is True or is_test_file(path, []):
                tests.add(path)
    fan_in: dict[str, set[str]] = {}
    fan_out: dict[str, set[str]] = {}
    for module in graph.modules("callees"):
        for edge in graph.shard("callees", module):
            to = edge.get("to")
            if not isinstance(to, str) or to.startswith("external:") or not _fact(edge):
                continue
            a, b = _path(edge.get("from")), _path(to)
            if a == b or a in tests or b in tests:
                continue
            fan_in.setdefault(b, set()).add(a)
            fan_out.setdefault(a, set()).add(b)
    seams: dict[str, dict[str, Any]] = {}

    def rank(degrees: dict[str, set[str]], reason: str) -> None:
        files = sorted({*fan_in, *fan_out})
        if not files:
            return
        top = max(1, math.ceil(len(files) * SEAM_TOP_SHARE))
        counts = sorted((len(v) for v in degrees.values()), reverse=True)
        floor = max(SEAM_MIN_DEGREE, counts[min(top, len(counts)) - 1] if counts else 0)
        for path, others in degrees.items():
            if len(others) >= floor:
                seams.setdefault(path, {"reasons": []})["reasons"].append(reason)

    rank(fan_in, "fan_in")
    rank(fan_out, "fan_out")
    spots = sorted((s for s in (document or {}).get("hot_spots") or [] if isinstance(s, dict)),
                   key=lambda s: (-int(s.get("changes") or 0), str(s.get("path"))))
    changes: dict[str, int] = {}
    for spot in spots[:SEAM_HOT_SPOTS]:
        path = str(spot.get("path"))
        changes[path] = int(spot.get("changes") or 0)
        # The promoted index names the partners `co_changed` (repoindex.HotSpot);
        # the extractor's own document, `changed_with`.
        partners = spot.get("co_changed") or spot.get("changed_with") or []
        if len(partners) >= SEAM_MIN_CHANGED_WITH and path not in tests:
            seams.setdefault(path, {"reasons": []})["reasons"].append("co_change")
    return {path: {"path": path, "fan_in": len(fan_in.get(path, ())),
                   "fan_out": len(fan_out.get(path, ())), "changes": changes.get(path),
                   "reasons": seams[path]["reasons"]} for path in sorted(seams)}


def _modules_for(graph: Graph, entry: str) -> list[str]:
    known = set(graph.modules("symbols")) | set(graph.modules("files"))
    if not entry.endswith("/"):
        return [module_of(entry)] if module_of(entry) in known else []
    root = entry.rstrip("/")
    return sorted(m for m in known if m == root or m.startswith(entry))


def expand(graph: Graph, entries: list[str], *, depth: int,
           seams: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """`entries` -> the territory a change to them reaches, from facts only.

    `named`: the entries themselves. `callers`: application files holding a
    call site (or import) of a symbol in them, to `depth`. `tests`: test files
    that call them or that `symbol_test_map` maps to them. `seams`: the
    seams among the named entries and callers. `communities`: the communities
    the named files and callers sit in.
    """
    modules = sorted({m for e in entries for m in _modules_for(graph, e)})
    files_by_module = _shards(graph, "files", modules)
    symbols_by_module = _shards(graph, "symbols", modules)

    def inside(path: str) -> bool:
        return any(conflicts(e, path) for e in entries)

    known_files = {str(r.get("path")) for rows in files_by_module.values() for r in rows}
    known_files |= {str(s.get("path")) for rows in symbols_by_module.values() for s in rows}
    unknown = [e for e in entries if not (any(conflicts(e, p) for p in known_files))]
    test_files = {str(r.get("path")) for rows in files_by_module.values() for r in rows
                  if r.get("test") is True}
    seeds = sorted({str(s.get("id")) for rows in symbols_by_module.values() for s in rows
                    if inside(str(s.get("path")))}
                   | {p for p in known_files if inside(p)})
    callers: dict[str, dict[str, Any]] = {}
    tests: dict[str, dict[str, Any]] = {}
    cut = {"below_floor": 0, "judged": 0}
    truncated: list[str] = []
    seen = set(seeds)
    frontier = seeds
    for level in range(1, depth + 1):
        following: list[str] = []
        shards = _shards(graph, "callers", [module_of(n) for n in frontier])
        targets = set(frontier)
        for rows in shards.values():
            for edge in rows:
                if edge.get("to") not in targets:
                    continue
                if not _fact(edge):
                    if edge.get("evidence") in FACT_EVIDENCE:
                        cut["below_floor"] += 1
                    else:
                        cut["judged"] += 1
                    continue
                source = str(edge.get("from"))
                path = _path(source)
                if inside(path):
                    continue
                into = tests if (path in test_files or is_test_file(path, [])) else callers
                row = into.setdefault(path, {"path": path, "depth": level, "via": []})
                if len(row["via"]) < 5 and str(edge.get("to")) not in row["via"]:
                    row["via"].append(str(edge.get("to")))
                if into is callers and source not in seen:
                    seen.add(source)
                    following.append(source)
        if len(seen) > MAX_WALK_SYMBOLS:
            truncated.append("walk")
            break
        frontier = sorted(following)
        if not frontier:
            break
    # The test map: tests that reach the named symbols through a call chain.
    for rows in _shards(graph, "tests", modules).values():
        for row in rows:
            symbol = str(row.get("symbol"))
            if symbol not in seeds or not inside(_path(symbol)):
                continue
            path = _path(row.get("test"))
            entry = tests.setdefault(path, {"path": path, "depth": int(row.get("depth") or 1),
                                            "via": []})
            entry["depth"] = min(entry["depth"], int(row.get("depth") or 1))
            if len(entry["via"]) < 5 and symbol not in entry["via"]:
                entry["via"].append(symbol)
    caller_rows = sorted(callers.values(), key=lambda r: (r["depth"], r["path"]))
    test_rows = sorted(tests.values(), key=lambda r: (r["depth"], r["path"]))
    if len(caller_rows) + len(test_rows) > MAX_EXPANDED_FILES:
        truncated.append("expanded")
        caller_rows = caller_rows[:MAX_EXPANDED_FILES]
        test_rows = test_rows[:max(0, MAX_EXPANDED_FILES - len(caller_rows))]
    for row in caller_rows + test_rows:
        row["via"] = sorted(row["via"])
    application = [*entries, *(r["path"] for r in caller_rows)]
    seam_rows = [dict(seams[p], where="named" if inside(p) else "caller")
                 for p in sorted(seams) if any(conflicts(a, p) for a in application)]
    member = membership(community_rows(graph)) if format_version(graph) >= INDEX_FORMAT else {}
    touched: dict[str, dict[str, Any]] = {}
    for path in sorted(known_files | set(callers)):
        if path not in member or path in test_files or not (inside(path) or path in callers):
            continue
        c = member[path]
        row = touched.setdefault(c["id"], {"id": c["id"], "label": c["label"], "files": []})
        row["files"].append(path)
    return {"named": entries, "unknown": unknown, "callers": caller_rows, "tests": test_rows,
            "seams": seam_rows, "communities": [touched[k] for k in sorted(touched)],
            "cut": cut, "truncated": truncated}


def territory_paths(expanded: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(entry, why) for every path the overlap is checked against."""
    out = [(e, "named") for e in expanded["named"]]
    out += [(r["path"], "caller") for r in expanded["callers"]]
    out += [(r["path"], "test") for r in expanded["tests"]]
    return out


def _shared(paths: Iterable[str], territory: list[tuple[str, str]],
            seams: set[str]) -> tuple[list[dict[str, Any]], int]:
    shared: list[dict[str, Any]] = []
    for path in sorted(set(paths)):
        for entry, why in territory:
            if conflicts(entry, path):
                shared.append({"path": path, "entry": entry, "why": why,
                               "seam": path in seams or entry in seams})
                break
    order = {"named": 0, "caller": 1, "test": 2}
    shared.sort(key=lambda s: (order[s["why"]], s["path"]))
    return shared[:MAX_SHARED_PER_ITEM], len(shared)


def overlap_pull_requests(snapshot: Mapping[str, Any], territory: list[tuple[str, str]],
                          seams: set[str], exclude: Iterable[int]) -> dict[str, Any]:
    skip = set(exclude)
    rows, unread = [], []
    for pull in snapshot.get("pull_requests") or []:
        number = pull.get("number")
        if number in skip:
            continue
        if pull.get("files") is None:
            unread.append(number)
            continue
        shared, total = _shared(pull["files"], territory, seams)
        if shared:
            rows.append({"number": number, "title": pull.get("title"), "shared": shared,
                         "shared_total": total,
                         "files_truncated": bool(pull.get("files_truncated"))})
    return {"pull_requests": rows, "pull_requests_unread": sorted(unread),
            "pull_requests_truncated": bool(snapshot.get("pull_requests_truncated"))}


def live_lanes(db: Any, tenant_id: str, owner: str, repo: str, *, now: Callable[[], Any],
               exclude: Iterable[str] = ()) -> tuple[list[dict[str, Any]], bool]:
    """The tenant's live issue runs on this repository, one row per planned step.

    A lane today is an issue run's step: its `files` are what the planner
    declared it will edit (lane-queue.md §4.1). A run still PLANNING has no
    steps yet and is listed with `files: null` -- not read, not "no overlap".
    """
    runs, more = IssueRuns(db, now=now).live(tenant_id, limit=MAX_LIVE_RUNS)
    skip = set(exclude)
    repository = f"{owner}/{repo}".lower()
    lanes: list[dict[str, Any]] = []
    for run in runs:
        if run.tenant_id != tenant_id or run.state in TERMINAL_RUN_STATES or run.id in skip:
            continue
        if run.issue.repository.lower() != repository:
            continue
        pull = (run.pull_request or {}).get("number")
        steps = ((run.plan or {}).get("steps") or []) if isinstance(run.plan, dict) else []
        if not steps:
            lanes.append({"run_id": run.id, "issue": run.issue.number, "state": run.state.value,
                          "step_id": None, "pull_request": pull, "files": None})
        for step in steps:
            files = step.get("files") if isinstance(step, dict) else None
            lanes.append({"run_id": run.id, "issue": run.issue.number, "state": run.state.value,
                          "step_id": step.get("step_id") if isinstance(step, dict) else None,
                          "pull_request": pull,
                          "files": [str(f) for f in files] if isinstance(files, list) else None})
    return lanes, more


def overlap_lanes(lanes: list[dict[str, Any]], territory: list[tuple[str, str]],
                  seams: set[str]) -> dict[str, Any]:
    rows, unread = [], []
    for lane in lanes:
        if lane["files"] is None:
            unread.append({k: lane[k] for k in ("run_id", "issue", "state", "step_id")})
            continue
        shared, total = _shared(lane["files"], territory, seams)
        if shared:
            rows.append({**{k: lane[k] for k in ("run_id", "issue", "state", "step_id",
                                                 "pull_request")},
                         "shared": shared, "shared_total": total})
    return {"lanes": rows, "lanes_undeclared": unread}


# --------------------------------------------------------------------------
# the open pull requests, read with the tenant's own token
# --------------------------------------------------------------------------

class _PullCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


#: Keyed by the forge client, so a deployment's one client has one cache and
#: a test's fresh client never sees another test's listing. Within it, keyed
#: by tenant AND registration: one tenant's listing is never another's.
_PULLS: "weakref.WeakKeyDictionary[Any, _PullCache]" = weakref.WeakKeyDictionary()
_PULLS_LOCK = threading.Lock()


def open_pull_requests(ctx: Any, record: Mapping[str, Any], tenant: Any, tenant_doc: Any,
                       *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The repository's open pull requests and their files, masked, or why not.

    `{ok, read_at, pull_requests, pull_requests_truncated}` on success;
    `{ok: false, code}` when the forge refused or failed -- the code only,
    never the forge's text, which can quote the request.
    """
    tenant_id, repo_id = tenant.tenant_id, str(record["repo_id"])
    with _PULLS_LOCK:
        cache = _PULLS.get(ctx.forge)
        if cache is None:
            cache = _PULLS[ctx.forge] = _PullCache()
    with cache.lock:
        hit = cache.rows.get((tenant_id, repo_id))
    if hit is not None and clock() - hit[0] < OPEN_PULLS_TTL_SECONDS:
        return hit[1]
    try:
        token, _label = resolve_read_token(ctx.db, tenant, tenant_doc, repo_id,
                                           tokens=ctx.forge_tokens, now=ctx.now())
        try:
            # number=1 names the repository only; the issues it lists are dropped.
            ref = IssueRef(owner=str(record["owner"]), repo=str(record["repo"]), number=1)
            work = ctx.forge.open_work(ref, token)
            literals = (token,)
            snapshot = {
                "ok": True, "code": None, "read_at": ctx.now().isoformat(),
                "pull_requests": [
                    {"number": item.number,
                     "title": _forge.neutral_line(item.title, _forge.MAX_ITEM_TITLE_CHARS,
                                                  literals=literals),
                     "files": None if item.files is None else [
                         _forge.neutral_line(p, _forge.MAX_FILE_PATH_CHARS, literals=literals)
                         for p in item.files],
                     "files_truncated": item.files_truncated}
                    for item in work.pull_requests
                ],
                "pull_requests_truncated": work.pull_requests_truncated,
            }
        finally:
            token = ""
    except _forge.ForgeReadError as refused:
        log.info("repository territory tenant=%s repo_id=%s pulls=%s", tenant_id, repo_id,
                 refused.code)
        return {"ok": False, "code": refused.code, "read_at": None, "pull_requests": [],
                "pull_requests_truncated": False}
    with cache.lock:
        cache.rows[(tenant_id, repo_id)] = (clock(), snapshot)
    return snapshot


def check_community_id(value: str) -> str:
    if not re.match(r"^[A-Za-z0-9_.:/-]{1,200}$", value or ""):
        raise ValidationFailed("a community id is 1-200 letters, digits or _ . : / -")
    return value


__all__ = [
    "BM25_B", "BM25_K1", "FACT_EVIDENCE", "FORCED_MIN_CONFIDENCE", "INDEX_FORMAT",
    "INDEX_LAYERS", "OPEN_PULLS_TTL_SECONDS", "SearchRequest", "TOKENIZER_VERSION",
    "TerritoryRequest", "bm25_search", "check_community_id", "communities_table", "conflicts",
    "expand", "format_version", "index_rows", "live_lanes", "membership", "normalise_entry",
    "open_pull_requests", "overlap_lanes", "overlap_pull_requests", "search", "seam_table",
    "term_bucket", "territory_paths", "tokenize",
]
