"""Reading a staged graph snapshot: the manifest, its blobs, lazily (§5.1).

THE FORMAT IS KG2's. `images/agent-runtime-indexer/repo-index/repo_graph_shards.py`
writes a manifest naming one content-addressed blob per (layer, module):
`shards` holds the five format-2 layers (symbols, callers, callees, tests,
files) and `index_shards` format 3's (communities, terms, signatures,
signature_changes, flows). A format-2 manifest is read with the index layers
empty, so `search` says it has no term index rather than finding nothing.

LAZY, SHARD BY SHARD. A question about one symbol reads its module's shard
and nothing else, so memory stays near the size of the shards touched. Decoded
shards are kept in a cache bounded by their raw bytes (`CACHE_RAW_BYTES`);
the whole graph of this repository is about 70 MB of JSON, which decoded is
several times that, and the step's agent needs the memory more than we do.

CHECKED BEFORE IT IS READ. A blob is named by the sha256 of its stored bytes,
so it is checked against its name before it is decompressed, and a blob that
does not match is refused (`SnapshotError`), never answered from. A manifest
digest the stager recorded (`freshness.json`'s `manifest_digest`) is checked
the same way.

THE TOKENIZER IS RESTATED HERE, AND HELD EQUAL BY A TEST. KG2 says
`repo_graph_shards.tokenize` is the one implementation the postings are built
with and queried with. That module ships in the indexer image; this one ships
in `agent-runtime-base`, which does not carry it, so the worker cannot import
it. `tokenize` and `bm25_search` below are a copy, and
tests/unit/worker/test_graphmcp_tokenizer_parity.py fails if a word splits
differently or a constant moves. The manifest records the tokenizer version:
a snapshot built with another version is refused for search rather than
queried with a tokenizer that splits words differently.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import math
import posixpath
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

MANIFEST_SCHEMA = "swarm.repo-graph-manifest/v1"
MANIFEST_NAME = "manifest.json"
FRESHNESS_NAME = "freshness.json"
BLOBS_DIR = "blobs"
COMPRESSION = "gzip"
BLOB_SUFFIX = ".jsonl.gz"
#: The format-2 layers, under `shards`.
LAYERS = ("symbols", "callers", "callees", "tests", "files")
#: Format 3's layers, under `index_shards`.
INDEX_LAYERS = ("communities", "terms", "signatures", "signature_changes", "flows")
#: The key of a layer stored as one shard (`repo_graph_shards.WHOLE`).
WHOLE = "*"
#: `repo_graph_shards.TERM_BUCKET_CHARS`: the hex characters of a term's bucket.
TERM_BUCKET_CHARS = 2
#: Decoded shards kept, by their raw (uncompressed) bytes. This repository's
#: largest shard is 6.8 MB raw (the `tests` layer of apps/swarm-mcp), so the
#: cache holds the few largest at once; a miss costs one decompress and parse.
CACHE_RAW_BYTES = 48 * 1024 * 1024

_HEX = re.compile(r"^sha256:([0-9a-f]{64})$")


class SnapshotError(Exception):
    """The snapshot cannot be read as it is. Never answered from."""


# --- the tokenizer and BM25, restated from repo_graph_shards ------------------
# Keep byte-for-byte equal in behaviour: test_graphmcp_tokenizer_parity.py.

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


def _stem(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


@functools.lru_cache(maxsize=1 << 16)
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
    out: list[str] = []
    for word in _WORD.findall(text or ""):
        out.extend(_word_terms(word))
    return out


def term_bucket(term: str) -> str:
    return hashlib.sha256(term.encode("utf-8")).hexdigest()[:TERM_BUCKET_CHARS]


def bm25_search(rows: Iterable[dict], stats: dict, query: str,
                limit: int = 20) -> list[tuple[str, float]]:
    """(symbol id, score), best first: `repo_graph_shards.bm25_search`."""
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


# --- naming -------------------------------------------------------------------

def path_of(symbol_id: str) -> str:
    """The file a symbol id (`<path>#<name>`) or a file id lives in."""
    return symbol_id.split("#", 1)[0]


def module_of(symbol_id: str) -> str:
    """`repo_graph_shards.module_of`: the file's directory, `.` at the root."""
    return posixpath.dirname(path_of(symbol_id)) or "."


def name_of(symbol_id: str) -> str:
    return symbol_id.split("#", 1)[1] if "#" in symbol_id else ""


# --- the snapshot -------------------------------------------------------------

class _Shard:
    """One decoded shard and the lookups built over it on first use."""

    __slots__ = ("rows", "raw_bytes", "_by")

    def __init__(self, rows: list[dict], raw_bytes: int) -> None:
        self.rows = rows
        self.raw_bytes = raw_bytes
        self._by: dict[str, dict[str, list[dict]]] = {}

    def by(self, field: str) -> dict[str, list[dict]]:
        index = self._by.get(field)
        if index is None:
            index = {}
            for row in self.rows:
                index.setdefault(str(row.get(field)), []).append(row)
            self._by[field] = index
        return index


class Snapshot:
    """A staged snapshot directory, read lazily and checked blob by blob."""

    def __init__(self, root: Path | str, *, manifest_digest: str | None = None,
                 cache_raw_bytes: int = CACHE_RAW_BYTES) -> None:
        self.root = Path(root)
        try:
            raw = (self.root / MANIFEST_NAME).read_bytes()
        except OSError as exc:
            raise SnapshotError(f"no manifest in the snapshot: {exc.strerror}") from None
        if manifest_digest is not None:
            actual = "sha256:" + hashlib.sha256(raw).hexdigest()
            if actual != manifest_digest:
                raise SnapshotError("the manifest does not match the digest the stager "
                                    "recorded; it is not read")
        try:
            manifest = json.loads(raw)
        except ValueError:
            raise SnapshotError("the manifest is not JSON") from None
        if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
            raise SnapshotError(f"the manifest is not a {MANIFEST_SCHEMA} document")
        if manifest.get("compression", COMPRESSION) != COMPRESSION:
            raise SnapshotError(f"compression {manifest.get('compression')!r} is not "
                                f"{COMPRESSION!r}")
        if manifest.get("blob_suffix", BLOB_SUFFIX) != BLOB_SUFFIX:
            raise SnapshotError(f"blob suffix {manifest.get('blob_suffix')!r} is not "
                                f"{BLOB_SUFFIX!r}")
        self.manifest = manifest
        self.commit_sha = str(manifest.get("commit_sha") or "")
        self.format_version = int(manifest.get("format_version") or 2)
        self._tables = {layer: dict((manifest.get("shards") or {}).get(layer) or {})
                        for layer in LAYERS}
        index_shards = (manifest.get("index_shards") or {}) if self.format_version >= 3 else {}
        self._tables.update({layer: dict(index_shards.get(layer) or {})
                             for layer in INDEX_LAYERS})
        self._cache: OrderedDict[tuple[str, str], _Shard] = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = cache_raw_bytes
        self._communities: dict[str, dict] | None = None

    # -- blobs and shards ----------------------------------------------------

    def keys(self, layer: str) -> list[str]:
        """The modules (or buckets) a layer has a shard for."""
        return sorted(self._tables.get(layer) or {})

    def shard(self, layer: str, key: str) -> _Shard:
        """The decoded shard of `layer` for `key`; an empty one if there is none."""
        cached = self._cache.get((layer, key))
        if cached is not None:
            self._cache.move_to_end((layer, key))
            return cached
        entry = (self._tables.get(layer) or {}).get(key)
        if entry is None:
            return _Shard([], 0)
        shard = self._decode(entry)
        self._cache[(layer, key)] = shard
        self._cache_bytes += shard.raw_bytes
        while self._cache_bytes > self._cache_limit and len(self._cache) > 1:
            _key, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.raw_bytes
        return shard

    def _decode(self, entry: dict) -> _Shard:
        match = _HEX.match(str(entry.get("blob") or ""))
        if not match:
            raise SnapshotError(f"the manifest names a blob {entry.get('blob')!r} that is "
                                "not a sha256 digest")
        hexdigest = match.group(1)
        path = self.root / BLOBS_DIR / f"{hexdigest}{BLOB_SUFFIX}"
        try:
            data = path.read_bytes()
        except OSError:
            raise SnapshotError(f"blob {hexdigest[:12]} is named by the manifest but not "
                                "staged") from None
        if hashlib.sha256(data).hexdigest() != hexdigest:
            raise SnapshotError(f"blob {hexdigest[:12]} does not match its name; refused")
        raw = gzip.decompress(data)
        # One parse of the whole shard as a JSON array: a line is a canonical
        # JSON object, which never holds a raw newline, and one `loads` over
        # the lot is about twice as fast as one per line on the largest shards.
        body = raw.strip()
        try:
            rows = json.loads(b"[" + body.replace(b"\n", b",") + b"]") if body else []
        except ValueError:
            raise SnapshotError(f"blob {hexdigest[:12]} is not JSON lines") from None
        if not all(isinstance(row, dict) for row in rows):
            raise SnapshotError(f"blob {hexdigest[:12]} holds a line that is not a JSON "
                                "object")
        return _Shard(rows, len(raw))

    # -- lookups ---------------------------------------------------------------

    def symbol(self, symbol_id: str) -> dict | None:
        rows = self.shard("symbols", module_of(symbol_id)).by("id").get(symbol_id)
        return rows[0] if rows else None

    def symbols_in(self, path: str) -> list[dict]:
        return list(self.shard("symbols", module_of(path)).by("path").get(path) or [])

    def file(self, path: str) -> dict | None:
        rows = self.shard("files", module_of(path)).by("path").get(path)
        return rows[0] if rows else None

    def callers(self, symbol_id: str) -> list[dict]:
        """Edges whose callee is `symbol_id` (a symbol or a file, for imports)."""
        return list(self.shard("callers", module_of(symbol_id)).by("to").get(symbol_id) or [])

    def callees(self, symbol_id: str) -> list[dict]:
        return list(self.shard("callees", module_of(symbol_id)).by("from").get(symbol_id)
                    or [])

    def tests(self, symbol_id: str) -> list[dict]:
        return list(self.shard("tests", module_of(symbol_id)).by("symbol").get(symbol_id)
                    or [])

    def signature(self, symbol_id: str) -> dict | None:
        rows = self.shard("signatures", module_of(symbol_id)).by("symbol").get(symbol_id)
        return rows[0] if rows else None

    def community_of(self, path: str) -> dict | None:
        """The community row whose files hold `path`, if the snapshot has communities."""
        if self._communities is None:
            self._communities = {}
            for row in self.shard("communities", WHOLE).rows:
                for member in row.get("files") or []:
                    self._communities[str(member)] = row
        return self._communities.get(path)

    @property
    def has_terms(self) -> bool:
        return bool(self._tables.get("terms"))

    @property
    def term_stats(self) -> dict:
        return dict(self.manifest.get("term_stats") or {})

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """BM25 over the buckets of the query's own terms, and nothing else."""
        if not self.has_terms:
            raise SnapshotError("this snapshot has no term index (a format-2 graph); "
                                "search is unavailable, the other tools are not")
        version = str(self.term_stats.get("tokenizer") or "")
        if version != TOKENIZER_VERSION:
            raise SnapshotError(f"the snapshot's term index was built with tokenizer "
                                f"{version!r}, this server's is {TOKENIZER_VERSION!r}; "
                                "search refuses rather than split words differently")
        terms = set(tokenize(query))
        rows: list[dict] = []
        for bucket in sorted({term_bucket(term) for term in terms}):
            rows.extend(r for r in self.shard("terms", bucket).rows if r.get("term") in terms)
        return bm25_search(rows, self.term_stats, query, limit=limit)


def load_freshness_record(root: Path | str) -> dict[str, Any] | None:
    """The stager's `freshness.json`, or None when it staged none."""
    path = Path(root) / FRESHNESS_NAME
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SnapshotError(f"{FRESHNESS_NAME} is not readable JSON") from None
    if not isinstance(record, dict):
        raise SnapshotError(f"{FRESHNESS_NAME} is not a JSON object")
    return record
