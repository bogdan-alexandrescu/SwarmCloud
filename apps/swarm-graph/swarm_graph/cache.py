"""The service's in-memory caches (docs/design/graph-service.md §5).

NOTHING HERE IS EVER INVALIDATED. A commit's index is immutable: a manifest
is named by its digest and every blob by the sha256 of its stored bytes,
checked before it is decoded (`snapshot.decode_blob`). A new commit is a new
manifest and a new key.

EVERY KEY STARTS WITH `tenant_id` (owner decision 3, invariant 9). Nothing is
shared across workspaces, even when two of them register the same public
repository and their blobs have equal digests: each pays for its own copy.

THREE CACHES, per replica:

  * `ShardCache` -- decoded shards, keyed `(tenant_id, blob_sha256)`, bounded
    by the shards' RAW (decompressed JSON) bytes as `Snapshot` counts them
    (`_Shard.raw_bytes`), least recently used out. Within a tenant it is
    shared across commits and repositories, because a blob's name is its
    content. FAIRNESS (§5.1): while more than one tenant is resident, no tenant
    holds more than half the bound; a tenant over its share loses its own
    least recently used shard first, so one large repository cannot starve
    every other workspace's agents. Loads are single-flight per key, so the
    warmer and a query asking for the same shard read GCS once. It also counts
    every read of a key, hit or load, in hourly buckets kept READ_HOURS
    (graph-answer-cache.md §5.3): the console's per-shard "hits, 24 h" and
    "last read". Memory only, keyed like the entries, so a tenant's counts
    are read only by that tenant's stats ticket.
  * `ResultCache` -- a tool's `Computed` (head and every row, materialised),
    serialised compactly, keyed `(tenant_id, manifest_digest, tool,
    canonical_args, TOOLS_VERSION)` (§5.2). Every page of one question is
    served from one computation. An entry over RESULT_ENTRY_MAX_BYTES is
    computed and not kept.
  * `ManifestCache` -- the opened `Snapshot` of a manifest, keyed `(tenant_id,
    repo_id, manifest_digest)`. §3.3 names the key `(tenant_id,
    manifest_digest)`; `repo_id` is added because the snapshot's blob source
    reads under that repository's `graph/blobs/`, and two repositories of one
    tenant are not guaranteed to hold each other's blobs.

WHY A HIT IS ALWAYS CORRECT (§5.2). An answer is a function of the manifest
bytes (verified against `manifest_digest`, which is in the key), the blob bytes
(named in the manifest by sha256 and checked), the tool and its arguments
(both in the key; `cursor` only slices rows and is left out), and the handler
code (`TOOLS_VERSION` is in the key, and a new image starts empty). The service
computes every answer under the EMPTY observation; the pod's shim applies the
real one after the hit (§4.4).

SIZING (§5.1, Q8), MEASURED BY THIS LANE on 2026-10-16 at 71ec64c. This
repository's graph, built by the indexer's own extractor and shard writer
(tests/unit/worker/graphmcp_fixtures.py's `graph_of` + `repo_graph_shards.build`),
is 795 blobs, 9.4 MB stored and 116 MB raw (the snapshot.py docstring's
"about 70 MB" is from an older tree). Every shard was decoded with
`decode_blob` under `tracemalloc`, with the `by(field)` lookups the tools build
on that layer: decoded is 2.93x raw over the whole graph, and at most 3.41x
for any shard of 1 MiB or more (a `callees` shard). The `terms` layer runs
5.53x, but is 6.4 MB raw of 116. A manifest opened as a `Snapshot` is 3.11x
its 122 KB. Shorter strings decode fatter: a 1.9 MB synthetic edge shard with
shorter paths than this repository's measured 3.73x
(test_graphsvc_cache.py re-measures it). DECODED_RATIO is 4.0, above all of
those but the small `terms` shards.

At 4.0x, §5.1's 768 MiB raw bound is 3 GiB decoded on its own and does not fit
the limit. THE BOUND SHRINKS, THE LIMIT DOES NOT GROW: SHARD_CACHE_RAW_BYTES is
512 MiB, and `resident_bytes()` (held under MEMORY_LIMIT_BYTES by
tests/unit/graphsvc/test_graphsvc_cache.py) is 2944 MiB. 512 MiB raw holds
about four repositories this size, hot (§5.1 estimated ten, from 70 MB at 2x).
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Generic, Hashable, TypeVar

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.snapshot import Snapshot, _Shard

MiB = 1024 * 1024
#: The Deployment's memory, requests == limits (§2.1, invariant 7).
MEMORY_LIMIT_BYTES = 3 * 1024 * MiB
#: What the interpreter, the HTTP threads, a request's working set and the
#: warmer's in-flight blobs may use beyond the caches.
HEADROOM_BYTES = 512 * MiB
#: Decoded bytes per raw byte, measured (the module docstring), rounded up.
DECODED_RATIO = 4.0
#: The decoded-shard LRU's bound in raw bytes: 768 MiB in §5.1, shrunk to fit
#: the measured ratio (the module docstring).
SHARD_CACHE_RAW_BYTES = 512 * MiB
#: The query-result cache's bound in serialised bytes (§5.2).
RESULT_CACHE_BYTES = 256 * MiB
#: A single result over this is computed but not kept (§5.2).
RESULT_ENTRY_MAX_BYTES = 4 * MiB
#: Opened manifests, by their raw bytes: about 270 of this repository's
#: (counted at DECODED_RATIO, above their measured 3.11x).
MANIFEST_CACHE_RAW_BYTES = 32 * MiB


def resident_bytes() -> int:
    """The most the three caches may hold, decoded, plus the headroom."""
    return int(SHARD_CACHE_RAW_BYTES * DECODED_RATIO + RESULT_CACHE_BYTES
               + MANIFEST_CACHE_RAW_BYTES * DECODED_RATIO + HEADROOM_BYTES)


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class _Lru(Generic[K, V]):
    """A thread-safe LRU bounded by a size each entry declares."""

    def __init__(self, bound: int, *, max_entry: int | None = None) -> None:
        self.bound = bound
        self.max_entry = bound if max_entry is None else max_entry
        self._lock = threading.Lock()
        self._entries: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    def keys(self) -> list[K]:
        with self._lock:
            return list(self._entries)

    def peek(self, key: K) -> V | None:
        """The entry, without counting as a use: a stats read does not keep
        an entry hot."""
        with self._lock:
            held = self._entries.get(key)
            return None if held is None else held[0]

    def get(self, key: K) -> V | None:
        with self._lock:
            held = self._entries.get(key)
            if held is None:
                return None
            self._entries.move_to_end(key)
            return held[0]

    def put(self, key: K, value: V, size: int) -> bool:
        """Keep `value`; False when it is over `max_entry` and is not kept."""
        if size > self.max_entry:
            return False
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._entries[key] = (value, size)
            self._bytes += size
            while self._bytes > self.bound and len(self._entries) > 1:
                _key, (_value, dropped) = self._entries.popitem(last=False)
                self._bytes -= dropped
        return True


# --- decoded shards (§5.1) -----------------------------------------------------

ShardKey = tuple[str, str]
#: Hours of per-blob reads kept: the console's "hits, 24 h" (graph-answer-cache.md §5.2).
READ_HOURS = 24
HOUR_SECONDS = 3600


class _BlobReads:
    """Reads per `(tenant_id, blob_sha256)` in hourly buckets, and the last
    read's time. A key whose every bucket is older than READ_HOURS is dropped
    at the next sweep, so this holds at most the blobs read in a day."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reads: dict[ShardKey, tuple[dict[int, int], float]] = {}
        self._swept = 0

    def record(self, key: ShardKey, now: float) -> None:
        hour = int(now // HOUR_SECONDS)
        with self._lock:
            buckets, _last = self._reads.get(key, ({}, now))
            buckets[hour] = buckets.get(hour, 0) + 1
            self._reads[key] = (buckets, now)
            if hour != self._swept:
                self._swept = hour
                self._sweep(hour)

    def _sweep(self, hour: int) -> None:
        oldest = hour - READ_HOURS + 1
        for key in list(self._reads):
            buckets, last = self._reads[key]
            for old in [h for h in buckets if h < oldest]:
                del buckets[old]
            if not buckets:
                del self._reads[key]

    def read(self, key: ShardKey, now: float) -> tuple[int, float | None]:
        """(reads in the last READ_HOURS hourly buckets, the last read's time)."""
        oldest = int(now // HOUR_SECONDS) - READ_HOURS + 1
        with self._lock:
            held = self._reads.get(key)
            if held is None:
                return 0, None
            buckets, last = held
            count = sum(n for h, n in buckets.items() if h >= oldest)
            return count, (last if count else None)


class ShardCache:
    """Decoded shards keyed `(tenant_id, blob_sha256)`, raw-byte bounded, fair."""

    def __init__(self, bound: int = SHARD_CACHE_RAW_BYTES, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.bound = bound
        self._clock = clock
        self._reads = _BlobReads()
        self._lock = threading.Lock()
        self._entries: OrderedDict[ShardKey, _Shard] = OrderedDict()
        self._bytes = 0
        self._tenant_bytes: dict[str, int] = {}
        self._loading: dict[ShardKey, threading.Event] = {}

    @property
    def share(self) -> int:
        """The most one tenant may hold while another is resident."""
        return self.bound // 2

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    def tenant_bytes(self, tenant_id: str) -> int:
        with self._lock:
            return self._tenant_bytes.get(tenant_id, 0)

    def keys(self) -> list[ShardKey]:
        with self._lock:
            return list(self._entries)

    def hot(self, tenant_id: str, blob_sha256: str) -> bool:
        """Whether this tenant's copy of the blob is held decoded."""
        with self._lock:
            return (tenant_id, blob_sha256) in self._entries

    def reads(self, tenant_id: str, blob_sha256: str) -> tuple[int, float | None]:
        """(this tenant's reads of the blob over READ_HOURS, its last read's
        time or None)."""
        return self._reads.read((tenant_id, blob_sha256), self._clock())

    def get(self, key: ShardKey) -> _Shard | None:
        with self._lock:
            shard = self._entries.get(key)
            if shard is not None:
                self._entries.move_to_end(key)
            return shard

    def get_or_load(self, key: ShardKey, load: Callable[[], _Shard]) -> tuple[_Shard, bool]:
        """(the shard, whether it was a hit). One caller loads a missing key;
        any other asking for it meanwhile waits for that load. A failed load
        is not kept, and a waiter then loads for itself. Every call is one
        read of the key in its hourly bucket."""
        self._reads.record(key, self._clock())
        while True:
            with self._lock:
                shard = self._entries.get(key)
                if shard is not None:
                    self._entries.move_to_end(key)
                    return shard, True
                waiting = self._loading.get(key)
                if waiting is None:
                    mine = threading.Event()
                    self._loading[key] = mine
                    break
            waiting.wait()
        try:
            shard = load()
        except BaseException:
            with self._lock:
                self._loading.pop(key, None)
            mine.set()
            raise
        with self._lock:
            self._put(key, shard)
            self._loading.pop(key, None)
        mine.set()
        return shard, False

    def _put(self, key: ShardKey, shard: _Shard) -> None:
        tenant = key[0]
        size = shard.raw_bytes
        if size > self.share or key in self._entries:
            # Over any tenant's share: answered from, never kept.
            return
        self._entries[key] = shard
        self._bytes += size
        self._tenant_bytes[tenant] = self._tenant_bytes.get(tenant, 0) + size
        # Fairness first: while another tenant is resident, a tenant over half
        # the bound gives up its own least recently used shards.
        for over in [t for t, held in self._tenant_bytes.items() if held > self.share]:
            while len(self._tenant_bytes) > 1 and self._tenant_bytes.get(over, 0) > self.share:
                self._evict(next(k for k in self._entries if k[0] == over))
        while self._bytes > self.bound and len(self._entries) > 1:
            self._evict(next(iter(self._entries)))

    def _evict(self, key: ShardKey) -> None:
        shard = self._entries.pop(key)
        self._bytes -= shard.raw_bytes
        tenant = key[0]
        left = self._tenant_bytes[tenant] - shard.raw_bytes
        if left > 0:
            self._tenant_bytes[tenant] = left
        else:
            del self._tenant_bytes[tenant]


# --- query results (§5.2) ------------------------------------------------------

#: Arguments a handler fills in when they are absent, so a question asked with
#: and without them is one key (§5.2: "`impact` without `depth` is keyed as
#: `depth: 2`"). Only an ABSENT key is filled, which is exactly when the
#: handler uses its default: an explicit null is a different question (an
#: error) and stays one. `files`/`symbols` read `args.get(k) or []`.
DEFAULTS: dict[str, dict[str, Any]] = {
    "search": {"limit": tools.SEARCH_DEFAULT},
    "impact": {"depth": tools.IMPACT_DEFAULT_DEPTH},
    "tests_for": {"files": [], "symbols": []},
    "similar_changes": {"files": [], "symbols": []},
    "call_graph": {"direction": "both", "depth": tools.CALL_GRAPH_DEFAULT_DEPTH},
}

ResultKey = tuple[str, str, str, str, str]


def canonical_args(tool: str, arguments: Any) -> str | None:
    """The arguments without `cursor`, defaults filled, `sort_keys`; None when
    they are not an object (the handler refuses them, and a refusal is not
    cached)."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None
    canonical = {k: v for k, v in arguments.items() if k != "cursor"}
    for name, default in DEFAULTS.get(tool, {}).items():
        canonical.setdefault(name, default)
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def result_key(tenant_id: str, manifest_digest: str, tool: str,
               arguments: Any) -> ResultKey | None:
    """`(tenant_id, manifest_digest, tool, canonical_args, TOOLS_VERSION)`, or
    None for a question that is never cached (an unknown tool, bad arguments)."""
    if tool not in tools.HANDLERS:
        return None
    args = canonical_args(tool, arguments)
    if args is None:
        return None
    return (tenant_id, manifest_digest, tool, args, tools.TOOLS_VERSION)


def encode_computed(computed: tools.Computed) -> bytes:
    """A `Computed` with every row expanded, as compact ASCII JSON."""
    whole = computed.materialised()
    return tools.dumps({"head": whole.head, "rows": whole.rows}).encode("ascii")


def encode_unresolved(message: str, candidates: list[str]) -> bytes:
    """An ambiguous or unknown name: as deterministic as an answer (§5.2)."""
    return tools.dumps({"unresolved": message, "candidates": candidates}).encode("ascii")


def decode_result(data: bytes) -> tools.Computed | tuple[str, list[str]]:
    """A `Computed`, or `(message, candidates)` for a cached unresolved name."""
    body = json.loads(data)
    if "unresolved" in body:
        return str(body["unresolved"]), [str(c) for c in body["candidates"]]
    return tools.Computed(body["head"], body["rows"])


class ResultCache(_Lru[ResultKey, bytes]):
    def __init__(self, bound: int = RESULT_CACHE_BYTES,
                 max_entry: int = RESULT_ENTRY_MAX_BYTES) -> None:
        super().__init__(bound, max_entry=max_entry)

    def put_result(self, key: ResultKey, data: bytes) -> bool:
        if key[0] == "" or key[1] == "":
            raise ValueError("a result key leads with the tenant and the manifest digest")
        return self.put(key, data, len(data))

    def held_for(self, tenant_id: str, manifest_digest: str) -> int:
        """How many answers this tenant holds for one manifest digest. The
        tenant leads the key, so another tenant's equal digest is not counted."""
        with self._lock:
            return sum(1 for k in self._entries if k[0] == tenant_id and k[1] == manifest_digest)


# --- opened manifests ------------------------------------------------------------

ManifestKey = tuple[str, str, str]


class ManifestCache(_Lru[ManifestKey, Snapshot]):
    def __init__(self, bound: int = MANIFEST_CACHE_RAW_BYTES) -> None:
        super().__init__(bound)


__all__ = [
    "DECODED_RATIO",
    "DEFAULTS",
    "HEADROOM_BYTES",
    "HOUR_SECONDS",
    "MANIFEST_CACHE_RAW_BYTES",
    "MEMORY_LIMIT_BYTES",
    "READ_HOURS",
    "RESULT_CACHE_BYTES",
    "RESULT_ENTRY_MAX_BYTES",
    "SHARD_CACHE_RAW_BYTES",
    "ManifestCache",
    "ResultCache",
    "ShardCache",
    "canonical_args",
    "decode_result",
    "encode_computed",
    "encode_unresolved",
    "resident_bytes",
    "result_key",
]
