"""The blob-source seam of `graphmcp.snapshot` (docs/design/graph-service.md §2.4, seam 1).

A `Snapshot` reads its shards through a `BlobSource`. `Snapshot(root)` keeps
its signature and reads a staged directory through `DirectorySource`; the
shared graph service builds `Snapshot.from_manifest(raw, manifest_digest=...,
source=...)` over its own source. Whichever source supplies the bytes, a blob
is checked against its name before it is decoded (`decode_blob`) and a
mismatch is refused, never answered from.

The snapshot here is written by hand (`write_hand_snapshot`), not by the
indexer: its bytes depend on nothing but this file, so
test_graphmcp_tools_version.py can fingerprint answers over it without a
change to the extractor or the shard writer moving the fingerprint.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from agent_worker.graphmcp import snapshot as snapshot_mod
from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import (
    ALL_LAYERS,
    DOCUMENT_LAYER,
    DOCUMENT_SECTIONS,
    DirectorySource,
    Snapshot,
    SnapshotError,
    decode_blob,
    term_bucket,
    tokenize,
)

COMMIT = "c0ffee" + "0" * 34
APP = "src/app/service.py"
STORE = "src/app/store.py"
CLI = "src/cli/main.py"
TEST = "tests/test_service.py"

SYMBOLS = [
    {"id": f"{APP}#handle", "path": APP, "name": "handle", "kind": "function",
     "start_line": 3, "end_line": 9},
    {"id": f"{APP}#validate", "path": APP, "name": "validate", "kind": "function",
     "start_line": 11, "end_line": 14},
    {"id": f"{STORE}#fetch_row", "path": STORE, "name": "fetch_row", "kind": "function",
     "start_line": 1, "end_line": 4},
    {"id": f"{STORE}#save_row", "path": STORE, "name": "save_row", "kind": "function",
     "start_line": 6, "end_line": 8},
    {"id": f"{CLI}#main", "path": CLI, "name": "main", "kind": "function",
     "start_line": 1, "end_line": 5},
    {"id": f"{TEST}#test_handle", "path": TEST, "name": "test_handle", "kind": "test",
     "start_line": 1, "end_line": 3},
]
EDGES = [
    {"from": f"{CLI}#main", "to": f"{APP}#handle", "kind": "call", "line": 2,
     "confidence": 1.0},
    {"from": f"{APP}#handle", "to": f"{APP}#validate", "kind": "call", "line": 4,
     "confidence": 1.0},
    {"from": f"{APP}#handle", "to": f"{STORE}#fetch_row", "kind": "call", "line": 5,
     "confidence": 0.9},
    {"from": f"{APP}#validate", "to": f"{STORE}#save_row", "kind": "call", "line": 12,
     "confidence": 0.8},
    {"from": f"{TEST}#test_handle", "to": f"{APP}#handle", "kind": "call", "line": 2,
     "confidence": 1.0},
    {"from": APP, "to": STORE, "kind": "import", "line": 1, "confidence": 1.0},
]
TESTS = [{"symbol": f"{APP}#handle", "test": f"{TEST}#test_handle", "confidence": 0.9,
          "depth": 1}]
FILES = [{"path": APP}, {"path": STORE}, {"path": CLI}, {"path": TEST, "test": True}]
COMMUNITIES = [{"id": "c1", "label": "app", "files": [APP, STORE]},
               {"id": "c2", "label": "cli", "files": [CLI]}]
HISTORY = [{"path": APP, "kind": "change", "pr": 7, "title": "Handle retries",
            "sha": "a" * 40, "merged_at": "2026-09-01", "files": [APP, STORE]}]
RULES = [{"glob": "src/app/**", "kind": "guard", "ref": TEST, "why": "covers the app",
          "confidence": 0.7}]
DOCUMENT = {
    "modules": [{"path": "src/app", "summary": "the service"},
                {"path": "src/cli", "summary": "the command line"}],
    "commands": [{"name": "test", "run": "uv run pytest"}],
}


def _module(path: str) -> str:
    return snapshot_mod.module_of(path)


def _terms() -> tuple[dict[str, list[dict]], dict]:
    postings: dict[str, dict[str, list]] = {}
    lengths = []
    for symbol in SYMBOLS:
        if symbol["kind"] == "test":
            continue
        words = tokenize(f"{symbol['name']} {symbol['path']}")
        lengths.append(len(words))
        for term in sorted(set(words)):
            postings.setdefault(term, {}).setdefault(symbol["path"], []).append(
                [symbol["name"], words.count(term), len(words)])
    buckets: dict[str, list[dict]] = {}
    for term in sorted(postings):
        buckets.setdefault(term_bucket(term), []).append({"term": term,
                                                          "postings": postings[term]})
    stats = {"documents": len(lengths), "average_length": sum(lengths) / len(lengths),
             "tokenizer": snapshot_mod.TOKENIZER_VERSION}
    return buckets, stats


def _grouped(rows: list[dict], key) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(key(row), []).append(row)
    return out


def hand_layers() -> tuple[dict[str, dict[str, list[dict]]], dict[str, dict[str, list[dict]]],
                           dict]:
    """(format-2 layers, index layers, term stats), each layer {key: rows}."""
    calls = list(EDGES)
    shards = {
        "symbols": _grouped(SYMBOLS, lambda r: _module(r["path"])),
        "callers": _grouped(calls, lambda e: _module(e["to"])),
        "callees": _grouped(calls, lambda e: _module(e["from"])),
        "tests": _grouped(TESTS, lambda t: _module(t["symbol"])),
        "files": _grouped(FILES, lambda f: _module(f["path"])),
    }
    terms, stats = _terms()
    index = {
        "communities": {snapshot_mod.WHOLE: COMMUNITIES},
        "terms": terms,
        "signatures": {_module(APP): [{"symbol": f"{APP}#handle",
                                       "signature": "def handle(request)"}]},
        "history": _grouped(HISTORY, lambda r: _module(r["path"])),
        "rules": {snapshot_mod.WHOLE: RULES},
        DOCUMENT_LAYER: DOCUMENT,
    }
    return shards, index, stats


def encode(rows: list[dict]) -> bytes:
    raw = b"".join(json.dumps(r, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                   for r in rows)
    return gzip.compress(raw, mtime=0)


def build_manifest(*, document: bool = True) -> tuple[bytes, dict[str, bytes]]:
    """(manifest bytes, blobs by hex digest) of the hand-written graph."""
    shards, index, stats = hand_layers()
    if not document:
        index.pop(DOCUMENT_LAYER)
    blobs: dict[str, bytes] = {}

    def table(layers: dict[str, dict[str, list[dict]]]) -> dict:
        out: dict[str, dict] = {}
        for layer, keyed in layers.items():
            out[layer] = {}
            for key in sorted(keyed):
                data = encode(keyed[key])
                hexdigest = hashlib.sha256(data).hexdigest()
                blobs[hexdigest] = data
                out[layer][key] = {"blob": "sha256:" + hexdigest,
                                   "records": len(keyed[key]), "bytes": len(data)}
        return out

    manifest = {"schema": snapshot_mod.MANIFEST_SCHEMA, "format_version": 3,
                "commit_sha": COMMIT, "compression": "gzip", "blob_suffix": ".jsonl.gz",
                "shards": table(shards), "index_shards": table(index), "term_stats": stats}
    return json.dumps(manifest, sort_keys=True).encode(), blobs


def write_hand_snapshot(dest: Path, *, document: bool = True) -> Path:
    raw, blobs = build_manifest(document=document)
    (dest / "blobs").mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_bytes(raw)
    for hexdigest, data in blobs.items():
        (dest / "blobs" / f"{hexdigest}.jsonl.gz").write_bytes(data)
    return dest


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class MemorySource:
    """A `BlobSource` over blobs in memory, decoded through the one checked reader."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.reads: list[str] = []

    def decoded(self, hexdigest: str):
        self.reads.append(hexdigest)
        if hexdigest not in self.blobs:
            raise SnapshotError(f"blob {hexdigest[:12]} is not stored")
        return decode_blob(hexdigest, self.blobs[hexdigest])


BATTERY = [
    ("search", {"query": "fetch row"}),
    ("context", {"symbol": f"{APP}#handle"}),
    ("impact", {"symbol": f"{STORE}#fetch_row", "depth": 3}),
    ("tests_for", {"files": [APP]}),
    ("neighbours", {"symbol": f"{APP}#handle"}),
    ("territory", {"files": [STORE]}),
    ("similar_changes", {"files": [APP]}),
    ("rules_for", {"files": [APP]}),
    ("call_graph", {"symbol": f"{APP}#handle"}),
    ("index", {"layer": "callers"}),
    ("index", {"layer": DOCUMENT_LAYER, "key": "modules"}),
]


# --- the seam ----------------------------------------------------------------

def test_a_directory_snapshot_reads_through_a_directory_source(tmp_path):
    snap = Snapshot(write_hand_snapshot(tmp_path / "s"))
    assert isinstance(snap.source, DirectorySource)
    assert snap.root == tmp_path / "s"
    assert snap.symbol(f"{APP}#handle")["kind"] == "function"
    assert [e["from"] for e in snap.callers(f"{APP}#handle")] == [f"{CLI}#main",
                                                                 f"{TEST}#test_handle"]


def test_directory_and_memory_sources_answer_every_tool_identically(tmp_path):
    raw, blobs = build_manifest()
    staged = Snapshot(write_hand_snapshot(tmp_path / "s"), manifest_digest=digest(raw))
    source = MemorySource(blobs)
    held = Snapshot.from_manifest(raw, manifest_digest=digest(raw), source=source)
    assert held.root is None
    obs = Observation.empty(COMMIT)
    for name, arguments in BATTERY:
        assert tools.call(held, obs, name, arguments) == tools.call(staged, obs, name,
                                                                    arguments), name
    assert source.reads, "the memory source was never read"


def test_from_manifest_checks_the_digest_before_it_parses(tmp_path):
    raw, blobs = build_manifest()
    with pytest.raises(SnapshotError, match="does not match the digest"):
        Snapshot.from_manifest(raw, manifest_digest="sha256:" + "0" * 64,
                               source=MemorySource(blobs))
    with pytest.raises(SnapshotError, match="does not match the digest"):
        Snapshot.from_manifest(b"not json", manifest_digest=digest(raw),
                               source=MemorySource(blobs))
    with pytest.raises(SnapshotError, match="not JSON"):
        Snapshot.from_manifest(b"not json", manifest_digest=None, source=MemorySource(blobs))


def test_a_blob_that_does_not_match_its_name_is_refused_by_either_source(tmp_path):
    raw, blobs = build_manifest()
    victim = json.loads(raw)["shards"]["symbols"][_module(APP)]["blob"].split(":", 1)[1]
    forged = encode([{"id": f"{APP}#evil", "path": APP, "kind": "function"}])

    tampered = dict(blobs, **{victim: forged})
    held = Snapshot.from_manifest(raw, manifest_digest=digest(raw),
                                  source=MemorySource(tampered))
    with pytest.raises(SnapshotError, match="does not match its name"):
        held.symbol(f"{APP}#handle")

    root = write_hand_snapshot(tmp_path / "s")
    (root / "blobs" / f"{victim}.jsonl.gz").write_bytes(forged)
    with pytest.raises(SnapshotError, match="does not match its name"):
        Snapshot(root).symbol(f"{APP}#handle")


def test_a_missing_blob_is_an_error_answer_never_an_empty_one(tmp_path):
    raw, blobs = build_manifest()
    victim = json.loads(raw)["shards"]["callers"][_module(APP)]["blob"].split(":", 1)[1]
    held = Snapshot.from_manifest(raw, manifest_digest=digest(raw),
                                  source=MemorySource({k: v for k, v in blobs.items()
                                                       if k != victim}))
    text, failed = tools.call(held, Observation.empty(COMMIT), "context",
                              {"symbol": f"{APP}#handle"})
    assert failed
    assert "is not stored" in json.loads(text)["error"]


def test_a_source_is_asked_once_per_blob_by_a_caching_source(tmp_path):
    class Counting(DirectorySource):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.misses = 0

        def decoded(self, hexdigest: str):
            if hexdigest not in self._cache:
                self.misses += 1
            return super().decoded(hexdigest)

    root = write_hand_snapshot(tmp_path / "s")
    raw = (root / "manifest.json").read_bytes()
    source = Counting(root)
    snap = Snapshot.from_manifest(raw, manifest_digest=digest(raw), source=source)
    for _ in range(3):
        snap.callers(f"{APP}#handle")
    assert source.misses == 1


def test_the_directory_source_lru_is_bounded_by_raw_bytes(tmp_path):
    root = write_hand_snapshot(tmp_path / "s")
    snap = Snapshot(root, cache_raw_bytes=1)
    for key in snap.keys("symbols"):
        snap.shard("symbols", key)
    # Over its bound, the cache keeps only the shard read last.
    assert len(snap.source._cache) == 1  # noqa: SLF001


def test_the_checked_reader_contextpack_calls_still_answers(tmp_path):
    snap = Snapshot(write_hand_snapshot(tmp_path / "s"))
    entry = snap.manifest["shards"]["files"][_module(APP)]
    shard = snap._decode(entry)  # noqa: SLF001 -- contextpack/retrieve.py's call
    assert [r["path"] for r in shard.rows] == [APP, STORE]
    with pytest.raises(SnapshotError, match="not a sha256 digest"):
        snap._decode({"blob": "md5:abc"})  # noqa: SLF001


# --- the document layer ------------------------------------------------------

def test_the_document_layer_is_read_from_index_shards_by_section(tmp_path):
    snap = Snapshot(write_hand_snapshot(tmp_path / "s"))
    assert DOCUMENT_LAYER == "document"
    assert DOCUMENT_LAYER in ALL_LAYERS
    assert set(snap.keys(DOCUMENT_LAYER)) == {"commands", "modules"}
    assert set(snap.keys(DOCUMENT_LAYER)) <= set(DOCUMENT_SECTIONS)
    assert snap.shard(DOCUMENT_LAYER, "commands").rows == DOCUMENT["commands"]


def test_a_snapshot_without_the_document_layer_reads_as_having_none(tmp_path):
    snap = Snapshot(write_hand_snapshot(tmp_path / "s", document=False))
    assert not snap.has_layer(DOCUMENT_LAYER)
    assert snap.keys(DOCUMENT_LAYER) == []
