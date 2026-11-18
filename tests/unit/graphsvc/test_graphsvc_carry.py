"""Carry forward (docs/design/graph-answer-cache.md §2.3 b, §2.5): an answer
whose inputs a promotion did not change is served from the cache at the new
digest, byte for byte the answer a computation there would give; an answer
whose inputs moved (a symbol added, a listed layer grown) is computed again.

The second commit is the hand-written graph one incremental index later: the
same blobs, plus one new module's `symbols` shard, as an incremental index
writes only the shards of the modules a merge touched."""

from __future__ import annotations

import hashlib
import json

import pytest
from graphsvc_fixtures import (
    OTHER_REPO_ID,
    REPO_ID,
    TENANT,
    MemoryStore,
    digest,
    hand_graph,
    mint,
    mint_stats,
    new_key,
    service,
    get,
    post,
)
from test_graphmcp_source import APP, COMMIT, MemorySource, encode
from test_graphmcp_tools_version import battery

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import Snapshot, module_of
from swarm_graph import carry
from swarm_graph.cache import ResultCache, carry_key, result_key

COMMIT2 = "c0ffee" + "2" * 34
FRESH = "src/extra/fresh.py"
FRESH_ID = f"{FRESH}#fresh"


def promoted(raw: bytes, blobs: dict[str, bytes]) -> tuple[bytes, dict[str, bytes]]:
    """The next commit's manifest: every entry kept, one module added."""
    manifest = json.loads(raw)
    manifest["commit_sha"] = COMMIT2
    data = encode([{"id": FRESH_ID, "path": FRESH, "name": "fresh", "kind": "function",
                    "start_line": 1, "end_line": 2}])
    hexdigest = hashlib.sha256(data).hexdigest()
    manifest["shards"]["symbols"][module_of(FRESH)] = {
        "blob": "sha256:" + hexdigest, "records": 1, "bytes": len(data)}
    return json.dumps(manifest, sort_keys=True).encode(), {**blobs, hexdigest: data}


@pytest.fixture
def two():
    key = new_key()
    raw, blobs = hand_graph()
    raw2, blobs2 = promoted(raw, blobs)
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    store.put_graph(TENANT, REPO_ID, raw2, blobs2, commit=COMMIT2)
    return key, (raw, blobs), (raw2, blobs2), store


def _call(tool: str, arguments: dict) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()


def _text(reply) -> tuple[str, bool]:
    assert reply.status == 200, (reply.status, reply.body)
    result = json.loads(reply.body)["result"]
    return result["content"][0]["text"], result["isError"]


def _local(raw: bytes, blobs: dict) -> Snapshot:
    return Snapshot.from_manifest(raw, manifest_digest=digest(raw), source=MemorySource(blobs))


def _counting(monkeypatch) -> list[str]:
    computed: list[str] = []
    real = tools.compute

    def counting(name, snap, arguments):
        computed.append(name)
        return real(name, snap, arguments)

    monkeypatch.setattr(tools, "compute", counting)
    return computed


def _caches(caplog) -> list[str]:
    return [json.loads(r.getMessage())["cache"] for r in caplog.records
            if json.loads(r.getMessage())["event"] == "graph_answer"]


def test_an_answer_for_an_unchanged_symbol_is_served_from_cache_after_a_promotion(
        two, monkeypatch, caplog):
    key, (raw, _blobs), (raw2, blobs2), store = two
    svc = service(key, store)
    computed = _counting(monkeypatch)
    question = _call("context", {"symbol": f"{APP}#handle"})
    with caplog.at_level("INFO", logger="swarm_graph"):
        post(svc, question, mint(key, raw=raw))
        after = _text(post(svc, question, mint(key, raw=raw2, index_sha=COMMIT2)))
    assert computed == ["context"], "the promoted digest's answer was not computed again"
    assert _caches(caplog) == ["miss", "carried"]
    want = tools.call(_local(raw2, blobs2), Observation.empty(COMMIT2), "context",
                      {"symbol": f"{APP}#handle"})
    assert after == want
    assert json.loads(after[0])["freshness"]["index_sha"] == COMMIT2
    # Kept under the new digest's exact key: the next ask is an exact hit.
    with caplog.at_level("INFO", logger="swarm_graph"):
        caplog.clear()
        post(svc, question, mint(key, raw=raw2, index_sha=COMMIT2))
    assert _caches(caplog) == ["hit"]
    document = json.loads(get(svc, "/v1/stats/local",
                              mint_stats(key, raw=raw2)).body)
    assert sum(b["carried"] for b in document["outcomes"]) == 1


def test_a_symbol_added_by_the_promotion_is_never_carried_as_absent(two, monkeypatch):
    key, (raw, _blobs), (raw2, blobs2), store = two
    svc = service(key, store)
    computed = _counting(monkeypatch)
    question = _call("context", {"symbol": FRESH_ID})
    before, failed = _text(post(svc, question, mint(key, raw=raw)))
    assert failed and "no symbol" in json.loads(before)["error"]
    after, failed = _text(post(svc, question, mint(key, raw=raw2, index_sha=COMMIT2)))
    assert computed == ["context", "context"]
    assert not failed and json.loads(after)["symbol"] == FRESH_ID
    assert (after, failed) == tools.call(_local(raw2, blobs2), Observation.empty(COMMIT2),
                                         "context", {"symbol": FRESH_ID})


def test_a_listing_of_a_layer_the_promotion_grew_is_computed_again(two, monkeypatch):
    key, (raw, _blobs), (raw2, blobs2), store = two
    svc = service(key, store)
    computed = _counting(monkeypatch)
    post(svc, _call("index", {"layer": "symbols"}), mint(key, raw=raw))
    after = _text(post(svc, _call("index", {"layer": "symbols"}),
                       mint(key, raw=raw2, index_sha=COMMIT2)))
    assert computed == ["index", "index"]
    assert after == tools.call(_local(raw2, blobs2), Observation.empty(COMMIT2), "index",
                               {"layer": "symbols"})


def test_every_carried_answer_is_the_computed_one_byte_for_byte(two, caplog):
    """§2.3 b's property test: the whole battery at the first commit, then at
    the second; every page at the second is what a computation there gives."""
    key, (raw, blobs), (raw2, blobs2), store = two
    svc = service(key, store)
    local, local2 = _local(raw, blobs), _local(raw2, blobs2)
    first, second = mint(key, raw=raw), mint(key, raw=raw2, index_sha=COMMIT2)
    for tool, arguments in battery(local):
        post(svc, _call(tool, arguments), first)
    caplog.clear()
    pages = 0
    with caplog.at_level("INFO", logger="swarm_graph"):
        for tool, arguments in battery(local2):
            cursor = None
            while True:
                args = dict(arguments, **({"cursor": cursor} if cursor else {}))
                want = tools.call(local2, Observation.empty(COMMIT2), tool, args)
                assert _text(post(svc, _call(tool, args), second)) == want, (tool, args)
                pages += 1
                cursor = json.loads(want[0]).get("next_cursor")
                if cursor is None:
                    break
    caches = _caches(caplog)
    assert pages > 40, "the battery ran"
    assert caches.count("carried") >= 10, caches
    assert caches.count("miss") >= 1, "the grown layer's questions were computed"


def test_an_answer_is_never_carried_between_two_repositories(two, monkeypatch, caplog):
    key, (raw, _blobs), (raw2, blobs2), store = two
    store.put_graph(TENANT, OTHER_REPO_ID, raw2, blobs2, commit=COMMIT2)
    svc = service(key, store)
    computed = _counting(monkeypatch)
    question = _call("context", {"symbol": f"{APP}#handle"})
    with caplog.at_level("INFO", logger="swarm_graph"):
        post(svc, question, mint(key, raw=raw))
        post(svc, question, mint(key, raw=raw2, index_sha=COMMIT2, repo_id=OTHER_REPO_ID))
    # The carry key holds `repo_id`: the other repository's promoted digest,
    # whose reads would all match, is computed, never carried across.
    assert computed == ["context", "context"]
    assert _caches(caplog) == ["miss", "miss"]


def test_the_carry_pointer_and_read_set_go_with_their_entry():
    reads = {("entry", "symbols", "src/app"): "a" * 32}
    results = ResultCache(bound=4096)
    key1 = result_key(TENANT, "sha256:" + "1" * 64, "context", {"symbol": "x"})
    assert key1 is not None
    assert results.put_result(key1, b"x" * 100, repo_id=REPO_ID, reads=reads)
    assert results.bytes == 100 + carry.read_set_bytes(reads)
    key2 = result_key(TENANT, "sha256:" + "2" * 64, "context", {"symbol": "x"})
    assert results.carry_candidate(key2, REPO_ID) == (key1, b"x" * 100, reads)
    # Pushed out by a larger answer: its pointer and read set leave with it.
    other = result_key(TENANT, "sha256:" + "3" * 64, "impact", {"symbol": "y"})
    results.put_result(other, b"y" * 4000)
    assert results.carry_candidate(key2, REPO_ID) is None
    assert results.read_set(key1) is None
    assert carry_key(key1, REPO_ID) not in results._carry


def test_the_recorder_records_absences_listings_and_manifest_fields():
    raw, blobs = hand_graph()
    view = carry.Recorder.over(_local(raw, blobs))
    view.symbol(FRESH_ID)
    view.keys("files")
    view.has_layer("history")
    assert view.has_terms
    view.term_stats
    assert view.reads[("entry", "symbols", module_of(FRESH))] == carry.ABSENT
    assert ("keys", "files", "") in view.reads
    assert view.reads[("has", "history", "")] == "1"
    assert ("term_stats", "", "") in view.reads
    assert carry.unchanged(view.reads, _local(raw, blobs))
    raw2, blobs2 = promoted(raw, blobs)
    assert not carry.unchanged(view.reads, _local(raw2, blobs2))
    assert COMMIT != COMMIT2
