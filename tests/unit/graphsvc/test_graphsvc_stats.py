"""swarm-graph's read-only stats endpoint (docs/design/graph-answer-cache.md
§5.3-§5.4; lane CACHE-1-STATS, part of #1835).

What these hold, each the way it could break:

  * TENANT-SCOPED (invariant 9). A second tenant's answers, warms and shards,
    on the same replica, under the SAME repository id and digest the ticket
    names, never show in the first tenant's figures, and the response never
    carries the second tenant's name, keys or shard hashes;
  * A PEER THAT IS NOT READ says why: `not_read` with `timeout`, `refused`,
    `disabled`, `ticket_refused` or `bad_response`, and never a panel of zeros;
  * TWO TICKETS, TWO DOMAINS. An attempt ticket is refused on the stats routes
    and a stats ticket on `/mcp`; the 401 carries the reason and nothing more;
  * THE COUNTERS ARE THE LOG. The outcome buckets and the served-ms histogram
    move exactly where `graph_answer` is logged, so the console's tile and the
    log-based metric of §3.1 cannot disagree;
  * THE STATS PORT NEVER SERVES `/mcp`, and the MCP port never serves stats.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from graphsvc_fixtures import (
    OTHER_REPO_ID,
    OTHER_TENANT,
    REPO_ID,
    TENANT,
    MemoryStore,
    digest,
    get,
    hand_graph,
    inline_warmer,
    mint,
    mint_stats,
    new_key,
    post,
    service,
)
from test_graphmcp_source import APP

from swarm_graph import stats as stats_mod
from swarm_graph.cache import ShardCache
from swarm_graph.server import make_server, make_stats_server
from swarm_graph.ticket import MAX_STATS_TICKET_SECONDS, STATS_DOMAIN, TICKET_DOMAIN

NOT_READ_KEYS = {"replica", "state", "reason"}


def _call(tool: str, arguments: dict) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()


QUESTION = _call("impact", {"symbol": f"{APP}#handle"})
OTHER_QUESTION = _call("search", {"query": "handle"})


@pytest.fixture
def world():
    key = new_key()
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    store.put_graph(OTHER_TENANT, OTHER_REPO_ID, raw, blobs)
    store.put_graph(OTHER_TENANT, REPO_ID, raw, blobs)
    return key, raw, blobs, store


def _doc(reply) -> dict:
    assert reply.status == 200, (reply.status, reply.body)
    return json.loads(reply.body)


def _totals(document: dict) -> dict[str, int]:
    totals: dict[str, int] = {}
    for bucket in document["outcomes"]:
        for name, count in bucket.items():
            if name != "hour":
                totals[name] = totals.get(name, 0) + count
    return totals


# --- the shape lanes 3 and 4 build against (§5.3) ------------------------------------

def test_the_local_document_has_every_figure_of_section_5_3(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    post(svc, QUESTION, mint(key, raw=raw))
    document = _doc(get(svc, "/v1/stats/local", mint_stats(key, raw=raw)))
    assert document["replica"] == "replica-0" and document["state"] == "read"
    assert document["started_at"].endswith("Z")
    assert document["result_bound"] == svc.results.bound and document["result_bytes"] > 0
    assert document["shard_bound"] == svc.shards.bound and document["shard_bytes"] > 0
    assert len(document["outcomes"]) == stats_mod.HOURS == 24
    assert [set(b) for b in document["outcomes"]] == [{"hour", "hit", "miss"}] * 24, \
        "no `carried` count while carry forward is not built (the tile is a dash)"
    hours = [b["hour"] for b in document["outcomes"]]
    assert hours == sorted(hours) and len(set(hours)) == 24
    assert _totals(document) == {"hit": 0, "miss": 1}
    histogram = document["served_ms"]
    assert histogram["bounds_ms"] == list(stats_mod.MS_BOUNDS)
    assert len(histogram["counts"]) == len(stats_mod.MS_BOUNDS) + 1
    assert sum(histogram["counts"]) == 1
    assert document["answers_hot"] == 1
    assert document["shards_hot"] >= 1
    assert document["window_hours"] == 1
    assert "shards" not in document, "the shard map is read only when asked for"


def test_the_fan_out_answers_for_this_replica_first(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    whole = _doc(get(svc, "/v1/stats", mint_stats(key, raw=raw)))
    assert whole["peers_resolved"] is True and set(whole) == {"replicas", "peers_resolved"}
    assert [r["replica"] for r in whole["replicas"]] == ["replica-0"]
    assert whole["replicas"][0] == _doc(get(svc, "/v1/stats/local", mint_stats(key, raw=raw)))


# --- tenant-scoped (§5.3, §5.4, invariant 9) -------------------------------------------

def test_a_second_tenants_entries_never_appear(world):
    key, raw, _blobs, store = world
    warmer = inline_warmer()
    svc = service(key, store, warmer=warmer)
    # The other tenant asks, twice, under the very repository id and digest
    # the first tenant's ticket names, and its warm runs.
    other = mint(key, raw=raw, tenant_id=OTHER_TENANT, repo_id=REPO_ID)
    post(svc, QUESTION, other)
    post(svc, QUESTION, other)
    post(svc, OTHER_QUESTION, mint(key, raw=raw, tenant_id=OTHER_TENANT,
                                   repo_id=OTHER_REPO_ID))
    for job in list(warmer.jobs):
        job()
    assert svc.shards.bytes > 0 and len(svc.results) == 2
    reply = get(svc, "/v1/stats/local?shards=1", mint_stats(key, raw=raw, previous=digest(raw)))
    document = _doc(reply)
    assert _totals(document) == {"hit": 0, "miss": 0}
    assert sum(document["served_ms"]["counts"]) == 0
    assert document["answers_hot"] == 0
    assert document["shards_hot"] is None, "this tenant's manifest is not held here"
    assert document["shards"] == {}
    assert document["last_warm"] is None
    # The replica-wide byte totals are numbers, not keys (§5.2), and stay.
    assert document["shard_bytes"] == svc.shards.bytes
    body = reply.body.decode()
    assert OTHER_TENANT not in body and OTHER_REPO_ID not in body
    assert not any(blob in body for _t, blob in svc.shards.keys())

    # The first tenant's own question is counted, and only it.
    post(svc, QUESTION, mint(key, raw=raw))
    document = _doc(get(svc, "/v1/stats/local?shards=1", mint_stats(key, raw=raw)))
    assert _totals(document) == {"hit": 0, "miss": 1}
    assert document["answers_hot"] == 1
    assert all(blob in {b for t, b in svc.shards.keys() if t == TENANT} or not row["hot"]
               for blob, row in document["shards"].items())
    assert OTHER_TENANT not in json.dumps(document)


# --- the shard map (§5.2) ----------------------------------------------------------------

def test_the_shard_map_is_the_blobs_of_the_tickets_digest(world):
    key, raw, _blobs, store = world
    warmer = inline_warmer()
    svc = service(key, store, warmer=warmer)
    post(svc, QUESTION, mint(key, raw=raw))
    for job in list(warmer.jobs):
        job()
    document = _doc(get(svc, "/v1/stats/local?shards=1", mint_stats(key, raw=raw)))
    snap = svc.manifests.get((TENANT, REPO_ID, digest(raw)))
    expected = set(stats_mod.manifest_blobs(snap))
    assert set(document["shards"]) == expected and expected
    hot = {b for t, b in svc.shards.keys() if t == TENANT}
    for blob, row in document["shards"].items():
        assert set(row) == {"hot", "hits_24h", "last_read"}
        assert row["hot"] is (blob in hot)
        if row["hot"]:
            assert row["hits_24h"] >= 1 and row["last_read"].endswith("Z")
    assert document["shards_hot"] == len(hot & expected)
    last = document["last_warm"]
    assert last["digest"] == digest(raw) and last["shards"] > 0
    assert last["replayed"] is None, "nothing is replayed until §2.3 a is built"
    assert set(last) == {"digest", "ms", "shards", "replayed", "cause", "at"}


def test_shard_reads_are_counted_in_hourly_buckets_and_age_out():
    now = [1_800_000_000.0]
    cache = ShardCache(clock=lambda: now[0])

    class _S:
        raw_bytes = 10

    cache.get_or_load((TENANT, "a" * 64), lambda: _S())  # type: ignore[arg-type, return-value]
    cache.get_or_load((TENANT, "a" * 64), lambda: _S())  # type: ignore[arg-type, return-value]
    assert cache.reads(TENANT, "a" * 64)[0] == 2
    assert cache.reads(OTHER_TENANT, "a" * 64) == (0, None), "keyed by tenant"
    now[0] += 23 * 3600
    cache.get_or_load((TENANT, "a" * 64), lambda: _S())  # type: ignore[arg-type, return-value]
    assert cache.reads(TENANT, "a" * 64) == (3, now[0])
    now[0] += 2 * 3600
    assert cache.reads(TENANT, "a" * 64) == (1, now[0] - 2 * 3600), \
        "the first hour's two reads left the 24 h window"


# --- a peer that does not answer (§5.3, §5.4) -------------------------------------------

def _serve(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"127.0.0.1:{httpd.server_address[1]}"


def test_a_peer_that_times_out_is_not_read_with_its_reason(world):
    key, raw, _blobs, store = world
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)  # accepts into the backlog and never answers
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    refused_at = f"127.0.0.1:{closed.getsockname()[1]}"
    closed.close()
    peer = service(key, store)
    peer.replica = "replica-1"
    off = service(key, store, enabled=False)
    other_key = service(new_key(), store)
    servers = [make_stats_server(s, "127.0.0.1", 0) for s in (peer, off, other_key)]
    try:
        live, disabled, wrong_key = (_serve(s) for s in servers)
        silent_at = f"127.0.0.1:{silent.getsockname()[1]}"
        svc = service(key, store, peers=[live, silent_at, refused_at, disabled, wrong_key],
                      peer_timeout=0.5)
        started = time.monotonic()
        whole = _doc(get(svc, "/v1/stats", mint_stats(key, raw=raw)))
        assert time.monotonic() - started < 3, "peers are read side by side, each bounded"
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        silent.close()
    replicas = whole["replicas"]
    assert [r["replica"] for r in replicas[:2]] == ["replica-0", "replica-1"]
    assert replicas[1]["state"] == "read" and "outcomes" in replicas[1]
    assert replicas[2:] == [
        {"replica": silent_at, "state": "not_read", "reason": "timeout"},
        {"replica": refused_at, "state": "not_read", "reason": "refused"},
        {"replica": disabled, "state": "not_read", "reason": "disabled"},
        {"replica": wrong_key, "state": "not_read", "reason": "ticket_refused"},
    ]
    assert all(set(r) == NOT_READ_KEYS for r in replicas[2:]), "never a panel of zeros"


def test_a_peer_that_answers_something_else_is_a_bad_response(world):
    key, raw, _blobs, store = world
    mcp = make_server(service(key, store), "127.0.0.1", 0)  # the MCP port: no stats route
    try:
        at = _serve(mcp)
        whole = _doc(get(service(key, store, peers=[at]), "/v1/stats", mint_stats(key, raw=raw)))
    finally:
        mcp.shutdown()
        mcp.server_close()
    assert whole["replicas"][1] == {"replica": at, "state": "not_read", "reason": "bad_response"}


def test_peers_are_the_headless_services_addresses_but_this_replicas(monkeypatch):
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 8081))
             for ip in ("10.0.0.5", "10.0.0.6", "10.0.0.5")]
    monkeypatch.setattr(stats_mod.socket, "getaddrinfo", lambda *a, **k: infos)
    assert stats_mod.Peers(own=lambda: {"10.0.0.6"})() == ["10.0.0.5:8081"]

    def unresolvable(*_a, **_k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(stats_mod.socket, "getaddrinfo", unresolvable)
    assert stats_mod.Peers(own=lambda: set())() is None, "unresolved, never an empty list"


# --- two tickets, two domains (§5.3) --------------------------------------------------------

def test_an_attempt_ticket_is_refused_on_the_stats_routes(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    assert STATS_DOMAIN != TICKET_DOMAIN
    for path in ("/v1/stats", "/v1/stats/local"):
        reply = get(svc, path, mint(key, raw=raw))
        assert reply.status == 401
        assert json.loads(reply.body) == {"error": "ticket_refused", "reason": "invalid"}
        # The attempt ticket's own domain, with the stats claims, is refused too.
        reply = get(svc, path, mint_stats(key, raw=raw, domain=TICKET_DOMAIN))
        assert json.loads(reply.body) == {"error": "ticket_refused", "reason": "invalid"}
    assert get(svc, "/v1/stats", None).status == 401


def test_a_stats_ticket_is_refused_on_mcp(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    reply = post(svc, QUESTION, mint_stats(key, raw=raw))
    assert reply.status == 401
    assert json.loads(reply.body) == {"error": "ticket_refused", "reason": "invalid"}
    assert store.reads == [], "a refused ticket reads nothing"


def test_a_stats_ticket_lives_sixty_seconds_and_names_one_repository(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    now = int(time.time())
    assert MAX_STATS_TICKET_SECONDS == 60
    expired = get(svc, "/v1/stats", mint_stats(key, raw=raw, exp=now - 1))
    assert json.loads(expired.body) == {"error": "ticket_refused", "reason": "expired"}
    too_long = get(svc, "/v1/stats", mint_stats(key, raw=raw, exp=now + 3600))
    assert json.loads(too_long.body) == {"error": "ticket_refused", "reason": "invalid"}
    for bad in ({"repo_id": "../x"}, {"tenant_id": "Eng/"}, {"manifest_digest": "sha256:x"},
                {"previous_manifest_digest": "nope"}, {"extra": 1}):
        reply = get(svc, "/v1/stats", mint_stats(key, raw=raw, **bad))
        assert reply.status == 401, bad
    assert get(svc, "/v1/stats", mint_stats(key, raw=raw, previous=digest(b"x"))).status == 200


# --- the counters are the log (§5.3, §3.1) ------------------------------------------------

def test_the_counters_move_at_the_graph_answer_line(world, caplog, monkeypatch):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    with caplog.at_level("INFO", logger="swarm_graph"):
        post(svc, QUESTION, ticket)
        post(svc, QUESTION, ticket)
        post(svc, OTHER_QUESTION, ticket)
        post(svc, _call("search", ["not", "an", "object"]), ticket)  # cache: none
    lines = [json.loads(r.getMessage()) for r in caplog.records]
    answers = [line for line in lines if line["event"] == "graph_answer"]
    logged = {c: sum(1 for line in answers if line["cache"] == c) for c in ("hit", "miss")}
    document = _doc(get(svc, "/v1/stats/local", mint_stats(key, raw=raw)))
    assert logged == {"hit": 1, "miss": 2}
    assert _totals(document) == logged
    assert sum(document["served_ms"]["counts"]) == 3, "`none` is not an answer outcome (§3.2)"

    # And it is that line: an answer whose log line is not written is not counted.
    def silent(name, **fields):
        if name == "graph_answer":
            raise RuntimeError("the log line")

    monkeypatch.setattr("swarm_graph.server.event", silent)
    with pytest.raises(RuntimeError):
        svc.handle("POST", "/mcp", [("Content-Length", str(len(QUESTION))),
                                    ("Authorization", f"Bearer {ticket}")],
                   lambda n: QUESTION[:n])
    monkeypatch.undo()
    assert _totals(_doc(get(svc, "/v1/stats/local", mint_stats(key, raw=raw)))) == logged


def test_the_histogram_buckets_by_milliseconds():
    counters = stats_mod.AnswerCounters()
    now = 1_800_000_000.0
    for ms in (0, 1, 2, 3, 900, 10 ** 9):
        counters.record(TENANT, REPO_ID, "miss", ms, now=now)
    view = counters.view(TENANT, REPO_ID, now=now)
    counts = view["served_ms"]["counts"]
    assert counts[0] == 2 and counts[1] == 1 and counts[2] == 1
    assert counts[stats_mod.MS_BOUNDS.index(1024)] == 1 and counts[-1] == 1
    assert counters.view(TENANT, REPO_ID, now=now + 24 * 3600)["served_ms"]["counts"] == \
        [0] * (len(stats_mod.MS_BOUNDS) + 1), "an answer older than 24 h has left the window"


# --- the stats port never serves /mcp (§5.3) ------------------------------------------------

def test_the_stats_port_never_serves_mcp_and_the_mcp_port_never_serves_stats(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    reply = svc.handle_stats("POST", "/mcp", [("Authorization", f"Bearer {mint(key, raw=raw)}")])
    assert reply.status == 404
    assert post(svc, b"", mint_stats(key, raw=raw), path="/v1/stats", method="GET").status == 404
    assert get(svc, "/v1/stats", mint_stats(key, raw=raw), method="POST").status == 405
    assert get(svc, "/v1/stats", mint_stats(key, raw=raw), method="GET").status == 200
    off = service(key, store, enabled=False)
    assert json.loads(get(off, "/v1/stats", mint_stats(key, raw=raw)).body) == {"error": "disabled"}


def test_the_stats_http_round_trip(world):
    import http.client

    key, raw, _blobs, store = world
    httpd = make_stats_server(service(key, store), "127.0.0.1", 0)
    try:
        port = int(_serve(httpd).rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/v1/stats",
                     headers={"Authorization": f"Bearer {mint_stats(key, raw=raw)}"})
        response = conn.getresponse()
        body = json.loads(response.read())
        assert response.status == 200 and body["replicas"][0]["replica"] == "replica-0"
        assert response.getheader("Cache-Control") == "no-store"
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
