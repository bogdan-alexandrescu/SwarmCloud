"""The server-side deadline (store.ANSWER_DEADLINE_SECONDS) and the answer
latency the stats route serves (`answer_latency`).

A cold replica reads the manifest, then shard after shard, each read up to
store.OBJECT_TIMEOUT_SECONDS; the pod's shim gives a call 10 s. Below that,
the service answers with what it has read, marked partial, and never caches
it; the reads go on into the caches, so the same question asked again is
whole. The deadline here is shortened so the tests run in a fraction of a
second; the shipped value is held at 7 s by its own test."""

from __future__ import annotations

import json
import threading
import time

import pytest
from graphsvc_fixtures import (
    REPO_ID,
    TENANT,
    MemoryStore,
    digest,
    get,
    hand_graph,
    mint,
    mint_stats,
    new_key,
    post,
    service,
)
from test_graphmcp_source import APP, COMMIT, MemorySource

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import Snapshot
from swarm_graph import server as server_mod
from swarm_graph import stats as stats_mod
from swarm_graph import store as store_mod
from swarm_graph.server import GraphService
from swarm_graph.store import load_pool

DEADLINE = 0.3
#: How long a slow read holds before the test lets it go: far past DEADLINE.
HELD = 10.0


class SlowStore(MemoryStore):
    """Reads whose name contains `slow` wait for `gate` (at most HELD s)."""

    def __init__(self, slow: str) -> None:
        super().__init__()
        self.slow = slow
        self.gate = threading.Event()

    def read(self, name: str) -> bytes:
        if self.slow in name:
            self.gate.wait(HELD)
        return super().read(name)


def _call(tool: str, arguments: dict) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()


def _text(reply) -> tuple[str, bool]:
    assert reply.status == 200, (reply.status, reply.body)
    result = json.loads(reply.body)["result"]
    return result["content"][0]["text"], result["isError"]


def _drain(svc: GraphService) -> None:
    """Every background read finished, then a fresh pool."""
    svc.pool.shutdown(wait=True)
    svc.pool = load_pool()


def _local(raw: bytes, blobs: dict) -> Snapshot:
    return Snapshot.from_manifest(raw, manifest_digest=digest(raw), source=MemorySource(blobs))


def _slow_world(slow: str):
    key = new_key()
    raw, blobs = hand_graph()
    store = SlowStore(slow)
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    svc = service(key, store)
    svc.deadline_seconds = DEADLINE
    return key, raw, blobs, store, svc


def test_the_shipped_deadline_is_seven_seconds_below_the_shims_ten():
    assert store_mod.ANSWER_DEADLINE_SECONDS == 7.0
    assert store_mod.ANSWER_DEADLINE_SECONDS < store_mod.OBJECT_TIMEOUT_SECONDS
    svc = GraphService(keys=None, store=MemoryStore(), enabled=lambda: True,  # type: ignore[arg-type]
                       replica="r", peers=lambda: [])
    assert svc.deadline_seconds == 7.0


def test_a_slow_shard_read_returns_a_partial_page_before_the_deadline():
    raw, blobs = hand_graph()
    # The callers shard of src/app: what `context` on handle reads for its rows.
    snap = _local(raw, blobs)
    slow = snap.entry("callers", "src/app")["blob"].split(":", 1)[1]
    key, raw, blobs, store, svc = _slow_world(slow)
    ticket = mint(key, raw=raw)
    question = {"symbol": f"{APP}#handle"}
    began = time.monotonic()
    text, failed = _text(post(svc, _call("context", question), ticket))
    took = time.monotonic() - began
    try:
        assert took < DEADLINE + 0.5, f"answered in {took:.2f}s, past the deadline"
        answer = json.loads(text)
        assert not failed
        assert list(answer)[0] == "freshness" and "next_cursor" in answer
        # The slow one, and every cold one after it: no read starts past the deadline.
        assert answer[server_mod.PARTIAL]["shards_unread"] >= 1
        assert answer[server_mod.PARTIAL]["after_ms"] == int(DEADLINE * 1000)
        assert len(text) <= tools.ANSWER_CAP
        assert len(svc.results) == 0, "a partial answer is never cached"
    finally:
        store.gate.set()
    _drain(svc)
    # The read finished into the shard cache: asked again, the answer is whole.
    whole = _text(post(svc, _call("context", question), ticket))
    assert whole == tools.call(snap, Observation.empty(COMMIT), "context", question)
    assert server_mod.PARTIAL not in json.loads(whole[0])


def test_a_partial_answer_is_logged_and_counted_as_partial_and_cold(caplog):
    raw, blobs = hand_graph()
    slow = _local(raw, blobs).entry("callers", "src/app")["blob"].split(":", 1)[1]
    key, raw, blobs, store, svc = _slow_world(slow)
    try:
        with caplog.at_level("INFO", logger="swarm_graph"):
            post(svc, _call("context", {"symbol": f"{APP}#handle"}), mint(key, raw=raw))
    finally:
        store.gate.set()
    _drain(svc)
    line = [json.loads(r.getMessage()) for r in caplog.records
            if json.loads(r.getMessage())["event"] == "graph_answer"][-1]
    assert line["partial"] is True and line["temperature"] == "cold"
    latency = json.loads(get(svc, "/v1/stats/local", mint_stats(key, raw=raw)).body)[
        "answer_latency"]
    assert latency["by_outcome"]["partial"]["count"] == 1
    assert latency["by_temperature"]["cold"]["count"] == 1


def test_a_slow_manifest_read_is_an_error_answer_before_the_deadline():
    key, raw, _blobs, store, svc = _slow_world("manifest.json")
    ticket = mint(key, raw=raw)
    began = time.monotonic()
    try:
        text, failed = _text(post(svc, _call("context", {"symbol": f"{APP}#handle"}), ticket))
        assert time.monotonic() - began < DEADLINE + 0.5
        assert failed and json.loads(text)["error"] == server_mod.LATE_OPEN
        assert list(json.loads(text))[0] == "freshness"
    finally:
        store.gate.set()
    _drain(svc)
    text, failed = _text(post(svc, _call("context", {"symbol": f"{APP}#handle"}), ticket))
    assert not failed and server_mod.PARTIAL not in json.loads(text)


def test_an_unresolved_name_under_a_cut_is_not_cached_as_absent():
    raw, blobs = hand_graph()
    slow = _local(raw, blobs).entry("symbols", "src/app")["blob"].split(":", 1)[1]
    key, raw, blobs, store, svc = _slow_world(slow)
    try:
        text, failed = _text(post(svc, _call("context", {"symbol": f"{APP}#handle"}),
                                  mint(key, raw=raw)))
        assert failed and json.loads(text)["error"] == server_mod.LATE_UNRESOLVED
        assert len(svc.results) == 0
    finally:
        store.gate.set()
    _drain(svc)


def test_the_stats_route_carries_answer_latency_percentiles():
    key = new_key()
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    post(svc, _call("context", {"symbol": f"{APP}#handle"}), ticket)   # miss, cold
    for _ in range(3):
        post(svc, _call("context", {"symbol": f"{APP}#handle"}), ticket)   # hit, warm
    post(svc, _call("search", ["not", "an", "object"]), ticket)        # uncached
    document = json.loads(get(svc, "/v1/stats/local", mint_stats(key, raw=raw)).body)
    latency = document["answer_latency"]
    assert latency["window_hours"] == stats_mod.HOURS
    assert latency["bounds_ms"] == list(stats_mod.LATENCY_BOUNDS_MS)
    assert latency["count"] == 5
    for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
        assert isinstance(latency[name], int), name
    assert latency["p50_ms"] <= latency["p95_ms"] <= latency["p99_ms"] <= latency["max_ms"]
    assert set(latency["by_outcome"]) == set(stats_mod.LATENCY_OUTCOMES)
    assert latency["by_outcome"]["hit"]["count"] == 3
    assert latency["by_outcome"]["miss"]["count"] == 1
    assert latency["by_outcome"]["uncached"]["count"] == 1
    assert latency["by_outcome"]["carried"] == {"count": 0, "p50_ms": None, "p95_ms": None,
                                                "p99_ms": None, "max_ms": None}
    assert latency["by_temperature"]["cold"]["count"] == 1
    assert latency["by_temperature"]["warm"]["count"] == 4
    assert latency["cells"]["miss/cold"]["count"] == 1
    assert latency["cells"]["hit/warm"]["count"] == 3


@pytest.mark.parametrize("samples, want", [
    ([5] * 100, {"p50_ms": 5, "p95_ms": 5, "p99_ms": 5, "max_ms": 5}),
    ([1] * 94 + [900] * 6, {"p50_ms": 1, "p95_ms": 900, "p99_ms": 900, "max_ms": 900}),
    ([1] * 95 + [900] * 5, {"p50_ms": 1, "p95_ms": 1, "p99_ms": 900, "max_ms": 900}),
    ([70000], {"p50_ms": 70000, "p95_ms": 70000, "p99_ms": 70000, "max_ms": 70000}),
])
def test_the_percentiles_are_read_off_the_histogram(samples, want):
    counters = stats_mod.AnswerCounters()
    now = 1_800_000_000.0
    for ms in samples:
        counters.record(TENANT, REPO_ID, "hit", ms, now=now)
    latency = counters.view(TENANT, REPO_ID, now=now)["answer_latency"]
    assert {k: latency[k] for k in want} == want
    assert latency["count"] == len(samples)


def test_a_tenants_latency_is_read_only_under_its_own_ticket():
    counters = stats_mod.AnswerCounters()
    now = 1_800_000_000.0
    counters.record(TENANT, REPO_ID, "miss", 40, now=now, cold=True)
    assert counters.view("ops", REPO_ID, now=now)["answer_latency"]["count"] == 0
    assert counters.view(TENANT, REPO_ID, now=now)["answer_latency"]["count"] == 1
