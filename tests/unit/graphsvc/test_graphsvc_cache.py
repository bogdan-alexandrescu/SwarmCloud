"""The caches: tenant-leading keys, the bound, fairness, single-flight loads,
canonical arguments, and the sizing assertion (graph-service.md §5.1-§5.2, Q8).
"""

from __future__ import annotations

import gc
import gzip
import hashlib
import json
import threading
import tracemalloc

import pytest

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.snapshot import _Shard, decode_blob
from swarm_graph import cache
from swarm_graph.cache import ResultCache, ShardCache, canonical_args, result_key


def _shard(raw_bytes: int) -> _Shard:
    return _Shard([], raw_bytes)


# --- sizing (§5.1) -----------------------------------------------------------------

def test_the_caches_fit_the_memory_limit_at_the_measured_ratio():
    """§5.1: the shard bound times the measured decoded-to-raw ratio, plus the
    result cache, the manifests and 512 MiB of headroom, fits in the 3 GiB
    limit. If it does not, the BOUND shrinks; the limit does not grow."""
    assert cache.MEMORY_LIMIT_BYTES == 3 * 1024 ** 3
    assert cache.HEADROOM_BYTES == 512 * 1024 ** 2
    assert cache.RESULT_CACHE_BYTES == 256 * 1024 ** 2
    assert cache.resident_bytes() <= cache.MEMORY_LIMIT_BYTES
    assert cache.SHARD_CACHE_RAW_BYTES <= 768 * 1024 ** 2, "§5.1's bound is the ceiling"


def _edges(count: int) -> list[dict]:
    """Rows the shape of a real `callers`/`callees` shard (repo_graph_shards)."""
    return [{"from": f"apps/swarm-api/swarm_api/module_{i % 97}.py#caller_{i}",
             "to": f"apps/agent-worker/agent_worker/target_{i % 31}.py#callee_{i % 503}",
             "kind": "call", "line": i % 900 + 1, "confidence": 0.9}
            for i in range(count)]


def test_a_large_shard_decodes_within_the_recorded_ratio():
    """The ratio recorded in cache.py was measured over this repository's own
    graph (3.41x at most for a shard of 1 MiB or more), and over this shard:
    an edge shard of a real one's shape and size, with shorter strings, which
    decodes fatter (3.73x). This re-measures it with tracemalloc, with the
    lookup the tools build on it, so an interpreter that decodes fatter fails
    here before it fails in a pod."""
    rows = _edges(12000)
    raw = b"".join(json.dumps(r, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                   for r in rows)
    data = gzip.compress(raw, mtime=0)
    hexdigest = hashlib.sha256(data).hexdigest()
    del rows
    gc.collect()
    tracemalloc.start()
    try:
        shard = decode_blob(hexdigest, data)
        shard.by("to")
        decoded, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert shard.raw_bytes >= 1024 * 1024
    assert decoded / shard.raw_bytes <= cache.DECODED_RATIO


# --- the shard LRU ---------------------------------------------------------------------

def test_shard_keys_lead_with_the_tenant_and_are_never_shared():
    shards = ShardCache(bound=1000)
    shards.get_or_load(("a", "x" * 64), lambda: _shard(10))
    _, hit = shards.get_or_load(("b", "x" * 64), lambda: _shard(10))
    assert not hit, "an equal blob of another tenant is its own entry"
    assert sorted(shards.keys()) == [("a", "x" * 64), ("b", "x" * 64)]


def test_the_bound_evicts_least_recently_used():
    shards = ShardCache(bound=300)
    for name in "pqr":
        shards.get_or_load(("a", name), lambda: _shard(100))
    shards.get(("a", "p"))
    shards.get_or_load(("a", "s"), lambda: _shard(100))
    assert sorted(k[1] for k in shards.keys()) == ["p", "r", "s"]
    assert shards.bytes == 300


def test_a_tenant_over_half_evicts_its_own_shards_first():
    """§5.1 fairness: while more than one tenant is resident, no tenant holds
    more than half the bound, and the one over loses its own LRU shard."""
    shards = ShardCache(bound=1000)
    for i in range(9):
        shards.get_or_load(("big", f"b{i}"), lambda: _shard(100))
    assert shards.tenant_bytes("big") == 900, "alone, a tenant may use the whole bound"
    shards.get_or_load(("small", "s0"), lambda: _shard(100))
    assert shards.tenant_bytes("small") == 100
    assert shards.tenant_bytes("big") <= shards.share
    assert ("big", "b0") not in shards.keys() and ("big", "b8") in shards.keys()
    # The large tenant keeps asking: it evicts its own, never the small one's.
    for i in range(9, 20):
        shards.get_or_load(("big", f"b{i}"), lambda: _shard(100))
    assert ("small", "s0") in shards.keys()
    assert shards.tenant_bytes("big") <= shards.share
    assert shards.bytes <= shards.bound


def test_a_shard_over_a_tenants_share_is_answered_not_kept():
    shards = ShardCache(bound=100)
    shard, hit = shards.get_or_load(("a", "x"), lambda: _shard(60))
    assert shard.raw_bytes == 60 and not hit
    assert shards.keys() == []


def test_a_missing_shard_is_loaded_once_for_concurrent_askers():
    shards = ShardCache(bound=1000)
    started, release = threading.Event(), threading.Event()
    loads: list[int] = []

    def load():
        loads.append(1)
        started.set()
        release.wait(5)
        return _shard(10)

    results: list[tuple] = []
    first = threading.Thread(target=lambda: results.append(shards.get_or_load(("a", "x"), load)))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: results.append(shards.get_or_load(("a", "x"), load)))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert len(loads) == 1
    assert sorted(hit for _, hit in results) == [False, True]


def test_a_failed_load_is_not_kept_and_the_next_asker_loads():
    shards = ShardCache(bound=1000)

    def fail():
        raise OSError("gone")

    with pytest.raises(OSError):
        shards.get_or_load(("a", "x"), fail)
    shard, hit = shards.get_or_load(("a", "x"), lambda: _shard(5))
    assert not hit and shard.raw_bytes == 5


# --- the result cache --------------------------------------------------------------------

def test_result_keys_lead_with_tenant_and_digest_and_end_with_the_tools_version():
    key = result_key("eng", "sha256:" + "a" * 64, "impact", {"symbol": "x"})
    assert key is not None
    assert key[0] == "eng" and key[1] == "sha256:" + "a" * 64 and key[2] == "impact"
    assert key[-1] == tools.TOOLS_VERSION
    assert result_key("ops", "sha256:" + "a" * 64, "impact", {"symbol": "x"}) != key


def test_cursor_is_not_part_of_the_question():
    assert canonical_args("impact", {"symbol": "x", "cursor": "7"}) == \
        canonical_args("impact", {"symbol": "x"})


@pytest.mark.parametrize("tool,bare,explicit", [
    ("impact", {"symbol": "x"}, {"symbol": "x", "depth": tools.IMPACT_DEFAULT_DEPTH}),
    ("search", {"query": "q"}, {"query": "q", "limit": tools.SEARCH_DEFAULT}),
    ("call_graph", {"symbol": "x"},
     {"symbol": "x", "direction": "both", "depth": tools.CALL_GRAPH_DEFAULT_DEPTH}),
    ("tests_for", {"files": ["a.py"]}, {"files": ["a.py"], "symbols": []}),
])
def test_an_absent_default_is_keyed_as_the_default(tool, bare, explicit):
    assert canonical_args(tool, bare) == canonical_args(tool, explicit)


def test_an_explicit_null_is_not_the_default():
    assert canonical_args("impact", {"symbol": "x", "depth": None}) != \
        canonical_args("impact", {"symbol": "x"})


def test_unknown_tools_and_non_object_arguments_are_never_cached():
    assert result_key("eng", "sha256:" + "a" * 64, "nope", {}) is None
    assert result_key("eng", "sha256:" + "a" * 64, "impact", ["x"]) is None


def test_the_result_cache_is_bounded_and_skips_an_entry_over_its_cap():
    results = ResultCache(bound=100, max_entry=60)
    key = ("eng", "sha256:" + "a" * 64, "impact", "{}", tools.TOOLS_VERSION)
    assert not results.put_result(key, b"x" * 61)
    assert results.get(key) is None
    for i in range(3):
        results.put_result(key[:3] + (str(i),) + key[4:], b"y" * 40)
    assert results.bytes <= 100 and len(results) == 2


def test_a_result_key_without_a_tenant_is_refused():
    with pytest.raises(ValueError):
        ResultCache().put_result(("", "d", "impact", "{}", "1"), b"x")
