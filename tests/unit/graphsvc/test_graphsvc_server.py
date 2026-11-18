"""The service end to end: hit correctness, tenant isolation, every §10 status,
the HTTP server, and warming on first sight (graph-service.md §2.2, §4.4,
§5.2, §5.4, §10).

HIT CORRECTNESS. Every answer the service gives, first asked (a miss) and asked
again (a hit), page after page, is byte for byte what the local server gives
under the empty observation, `tools.call(snap, Observation.empty(sha), ...)`,
over GS-CORE's whole question battery (test_graphmcp_tools_version.battery).
The pod's shim applies the real observation with `tools.remark`; that half is
GS-CORE's and GS-SHIM's to hold.
"""

from __future__ import annotations

import http.client
import json
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
    hand_graph,
    inline_warmer,
    mint,
    new_key,
    post,
    service,
)
from test_graphmcp_source import APP, COMMIT, STORE, MemorySource
from test_graphmcp_tools_version import battery

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.server import SERVER_NAME
from agent_worker.graphmcp.snapshot import Snapshot
from swarm_graph import server as server_mod
from swarm_graph.cache import ResultCache, ShardCache
from swarm_graph.server import MAX_BODY_BYTES, make_server
from swarm_graph.warm import HOT_LAYERS, Warmer


def _rpc(method: str, params: dict | None = None, ident: int | None = 1) -> bytes:
    message: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if ident is not None:
        message["id"] = ident
    return json.dumps(message).encode()


def _call(tool: str, arguments: dict) -> bytes:
    return _rpc("tools/call", {"name": tool, "arguments": arguments})


def _text(reply) -> tuple[str, bool]:
    assert reply.status == 200, (reply.status, reply.body)
    result = json.loads(reply.body)["result"]
    return result["content"][0]["text"], result["isError"]


@pytest.fixture
def world():
    key = new_key()
    raw, blobs = hand_graph()
    store = MemoryStore()
    store.put_graph(TENANT, REPO_ID, raw, blobs)
    return key, raw, blobs, store


def _local(raw: bytes, blobs: dict) -> Snapshot:
    return Snapshot.from_manifest(raw, manifest_digest=digest(raw), source=MemorySource(blobs))


# --- hit correctness (§5.2) ------------------------------------------------------------

def test_every_answer_is_the_local_servers_on_a_miss_and_on_a_hit(world):
    key, raw, blobs, store = world
    results = ResultCache()
    svc = service(key, store, results=results)
    ticket = mint(key, raw=raw)
    local = _local(raw, blobs)
    empty = Observation.empty(COMMIT)
    asked = 0
    for round_ in ("miss", "hit"):
        for tool, arguments in battery(local):
            cursor = None
            while True:
                args = dict(arguments, **({"cursor": cursor} if cursor else {}))
                want = tools.call(local, empty, tool, args)
                assert _text(post(svc, _call(tool, args), ticket)) == want, (round_, tool, args)
                asked += 1
                cursor = json.loads(want[0]).get("next_cursor")
                if cursor is None:
                    break
    assert asked > 2 * 40, "the battery ran"
    assert len(results) > 0


def test_a_second_page_and_a_repeat_are_served_from_one_computation(world, monkeypatch):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    computed: list[str] = []
    real = tools.compute

    def counting(name, snap, arguments):
        computed.append(name)
        return real(name, snap, arguments)

    monkeypatch.setattr(tools, "compute", counting)
    first, _ = _text(post(svc, _call("index", {"layer": "files"}), ticket))
    post(svc, _call("impact", {"symbol": f"{STORE}#save_row", "depth": 3}), ticket)
    post(svc, _call("impact", {"symbol": f"{STORE}#save_row", "depth": 3}), ticket)
    post(svc, _call("impact", {"symbol": f"{STORE}#save_row", "depth": 3, "cursor": "1"}),
         ticket)
    assert computed == ["index", "impact"]


def test_a_default_and_its_explicit_value_share_one_entry(world, monkeypatch):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    a = post(svc, _call("impact", {"symbol": f"{APP}#handle"}), ticket)
    b = post(svc, _call("impact", {"symbol": f"{APP}#handle",
                                   "depth": tools.IMPACT_DEFAULT_DEPTH}), ticket)
    assert _text(a) == _text(b)
    assert len(svc.results) == 1


def test_an_unresolved_name_is_cached_and_a_bad_argument_is_not(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    text, failed = _text(post(svc, _call("context", {"symbol": "nothing_named_this"}), ticket))
    assert failed and "error" in json.loads(text)
    assert len(svc.results) == 1
    _text(post(svc, _call("impact", {"symbol": f"{APP}#handle", "depth": 99}), ticket))
    assert len(svc.results) == 1


# --- tenant-leading keys (§3.3 step 5, owner decision 3) -----------------------------------

def test_two_workspaces_with_the_same_graph_share_no_cache_entry(world):
    key, raw, blobs, store = world
    store.put_graph(OTHER_TENANT, OTHER_REPO_ID, raw, blobs)
    shards, results = ShardCache(), ResultCache()
    svc = service(key, store, shards=shards, results=results)
    question = _call("impact", {"symbol": f"{APP}#handle"})
    one = post(svc, question, mint(key, raw=raw))
    reads = len(store.reads)
    two = post(svc, question, mint(key, raw=raw, tenant_id=OTHER_TENANT, repo_id=OTHER_REPO_ID))
    assert _text(one) == _text(two)
    assert len(store.reads) > reads, "the second workspace read its own copy"
    assert {k[0] for k in shards.keys()} == {TENANT, OTHER_TENANT}
    assert {k[0] for k in results.keys()} == {TENANT, OTHER_TENANT}
    assert {k[0] for k in svc.manifests.keys()} == {TENANT, OTHER_TENANT}
    assert all(k[1] == digest(raw) for k in results.keys())


def test_a_ticket_reads_only_its_own_tenants_prefix(world):
    key, raw, blobs, store = world
    svc = service(key, store)
    reply = post(svc, _call("search", {"query": "handle"}),
                 mint(key, raw=raw, tenant_id=OTHER_TENANT))
    assert reply.status == 404
    assert all(name.startswith(f"tenants/{OTHER_TENANT}/") for name in store.reads)


# --- every §10 status ------------------------------------------------------------------

def _refused(reply, status: int, code: str) -> dict:
    assert reply.status == status, (reply.status, reply.body)
    body = json.loads(reply.body)
    assert body["error"] == code
    assert ("X-Swarm-Graph-Refused", code) in reply.headers
    return body


def test_no_ticket_is_401(world):
    key, raw, _blobs, store = world
    body = _refused(post(service(key, store), _rpc("ping"), None), 401, "ticket_refused")
    assert body["reason"] == "invalid"


def test_an_expired_ticket_is_401_expired(world):
    key, raw, _blobs, store = world
    ticket = mint(key, raw=raw, exp=int(time.time()) - 1)
    body = _refused(post(service(key, store), _rpc("ping"), ticket), 401, "ticket_refused")
    assert body == {"error": "ticket_refused", "reason": "expired"}


def test_another_keys_ticket_is_401_invalid_and_reads_nothing(world):
    key, raw, _blobs, store = world
    body = _refused(post(service(key, store), _rpc("ping"), mint(new_key(), raw=raw)),
                    401, "ticket_refused")
    assert body["reason"] == "invalid"
    assert store.reads == []


def test_a_missing_index_is_404(world):
    key, raw, _blobs, store = world
    store.objects.clear()
    _refused(post(service(key, store), _rpc("initialize"), mint(key, raw=raw)),
             404, "index_missing")


def test_a_blob_swept_mid_attempt_is_404_never_a_partial_answer(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    assert post(svc, _rpc("tools/list"), ticket).status == 200
    for name in [n for n in store.objects if "/blobs/" in n]:
        del store.objects[name]
    _refused(post(svc, _call("impact", {"symbol": f"{APP}#handle"}), ticket),
             404, "index_missing")
    assert len(svc.results) == 0


def test_a_rewritten_manifest_is_409_and_logged_with_ids_only(world, caplog):
    key, raw, _blobs, store = world
    name = next(n for n in store.objects if n.endswith("manifest.json"))
    store.objects[name] = raw + b"\n"
    ticket = mint(key, raw=raw)
    with caplog.at_level("INFO", logger="swarm_graph"):
        _refused(post(service(key, store), _rpc("initialize"), ticket),
                 409, "manifest_mismatch")
    lines = [json.loads(r.getMessage()) for r in caplog.records]
    security = [line for line in lines if line["event"] == "graph_security"]
    assert security == [{"event": "graph_security", "kind": "manifest_mismatch",
                         "repo_id": REPO_ID, "tenant_id": TENANT}]
    assert all(ticket not in r.getMessage() for r in caplog.records)


def test_a_verified_document_that_is_not_a_manifest_is_409_invalid(world):
    key, _raw, _blobs, store = world
    bogus = b'{"schema": "something else"}'
    name = next(n for n in store.objects if n.endswith("manifest.json"))
    store.objects[name] = bogus
    _refused(post(service(key, store), _rpc("initialize"), mint(key, raw=bogus)),
             409, "manifest_invalid")


def test_the_switch_off_is_503_disabled(world):
    key, raw, _blobs, store = world
    svc = service(key, store, enabled=False)
    _refused(post(svc, _rpc("ping"), mint(key, raw=raw)), 503, "disabled")
    _refused(svc.handle("GET", "/healthz", [], lambda n: b""), 503, "disabled")


def test_the_store_unreachable_is_503(world):
    key, raw, _blobs, store = world
    store.down = True
    _refused(post(service(key, store), _rpc("initialize"), mint(key, raw=raw)),
             503, "store_unavailable")


def test_the_store_lost_mid_attempt_is_503_not_an_answer(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    assert post(svc, _rpc("tools/list"), ticket).status == 200
    store.down = True
    _refused(post(svc, _call("context", {"symbol": f"{APP}#handle"}), ticket),
             503, "store_unavailable")


def test_a_body_over_the_cap_is_413_and_never_read(world):
    key, raw, _blobs, store = world
    read: list[int] = []
    headers = [("Content-Length", str(MAX_BODY_BYTES + 1)),
               ("Authorization", f"Bearer {mint(key, raw=raw)}")]
    reply = service(key, store).handle("POST", "/mcp", headers,
                                       lambda n: read.append(n) or b"")
    _refused(reply, 413, "too_large")
    assert read == []


def test_a_cold_cache_is_slower_never_different(world, caplog):
    """§10 "cache cold": the answer is the same; `cache` is in the log only."""
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    question = _call("call_graph", {"symbol": f"{APP}#handle"})
    with caplog.at_level("INFO", logger="swarm_graph"):
        cold = post(svc, question, ticket)
        warm = post(svc, question, ticket)
    assert cold.body == warm.body
    assert b"cache" not in cold.body
    caches = [json.loads(r.getMessage()).get("cache") for r in caplog.records
              if json.loads(r.getMessage())["event"] == "graph_answer"]
    assert caches == ["miss", "hit"]


# --- JSON-RPC over HTTP (§2.2) ---------------------------------------------------------------

def test_initialize_and_tools_list_are_the_local_servers(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    init = json.loads(post(svc, _rpc("initialize", {"protocolVersion": "2025-06-18"}),
                           ticket).body)["result"]
    assert init["serverInfo"]["name"] == SERVER_NAME
    listed = json.loads(post(svc, _rpc("tools/list"), ticket).body)["result"]["tools"]
    assert listed == tools.TOOLS


def test_protocol_errors_are_a_200_carrying_the_error(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    ticket = mint(key, raw=raw)
    cases = [(b"{not json", -32700), (b"[]", -32600), (_rpc("nope"), -32601),
             (_rpc("tools/call", {"arguments": {}}), -32602)]
    for body, code in cases:
        reply = post(svc, body, ticket)
        assert reply.status == 200
        assert json.loads(reply.body)["error"]["code"] == code


def test_a_notification_is_202_with_no_body(world):
    key, raw, _blobs, store = world
    reply = post(service(key, store), _rpc("notifications/initialized", ident=None),
                 mint(key, raw=raw))
    assert reply.status == 202 and reply.body == b""


def test_other_routes_and_methods(world):
    key, raw, _blobs, store = world
    svc = service(key, store)
    assert svc.handle("GET", "/healthz", [], lambda n: b"").status == 200
    _refused(svc.handle("GET", "/mcp", [], lambda n: b""), 405, "method_not_allowed")
    _refused(svc.handle("POST", "/v1/blobs/x", [], lambda n: b""), 404, "not_found")
    _refused(svc.handle("POST", "/mcp", [], lambda n: b""), 400, "bad_request")


def test_every_status_the_service_can_refuse_with_is_in_one_table():
    assert server_mod.STATUSES["ticket_refused"] == 401
    assert server_mod.STATUSES["index_missing"] == 404
    assert server_mod.STATUSES["manifest_mismatch"] == 409
    assert server_mod.STATUSES["disabled"] == 503
    assert server_mod.STATUSES["store_unavailable"] == 503


def test_the_http_server_round_trip(world):
    key, raw, _blobs, store = world
    httpd = make_server(service(key, store), "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        body = _call("search", {"query": "fetch row"})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/mcp", body=body,
                     headers={"Authorization": f"Bearer {mint(key, raw=raw)}",
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        assert response.status == 200
        assert response.getheader("Server") is None, "no Python version is advertised"
        assert json.loads(data)["result"]["isError"] is False
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("PATCH", "/mcp")
        response = conn.getresponse()
        assert response.status == 405
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- warming on first sight (§5.4) ------------------------------------------------------------

def test_the_first_sight_of_a_digest_warms_once_and_by_tenant(world):
    key, raw, blobs, store = world
    warmer = inline_warmer()
    shards = ShardCache()
    svc = service(key, store, shards=shards, warmer=warmer)
    ticket = mint(key, raw=raw)
    post(svc, _rpc("ping"), ticket)
    post(svc, _rpc("tools/list"), ticket)
    assert len(warmer.jobs) == 1, "one warm per digest per replica"
    assert shards.keys() == [], "the request itself is not held back by a warm"
    warmer.jobs[0]()
    local = _local(raw, blobs)
    hot = {local.entry(layer, k)["blob"].split(":")[1] for layer in HOT_LAYERS
           for k in local.keys(layer)}
    assert {k[1] for k in shards.keys()} == hot
    assert {k[0] for k in shards.keys()} == {TENANT}
    reads = len(store.reads)
    post(svc, _call("context", {"symbol": f"{APP}#handle"}), ticket)
    assert not [n for n in store.reads[reads:] if any(
        n.endswith(f"{h}.jsonl.gz") for h in hot)], "a warmed shard is not read again"


def test_a_warm_is_bounded_by_the_staging_budget(world):
    key, raw, blobs, store = world
    from swarm_graph.warm import warm

    local = _local(raw, blobs)
    first = local.entry(HOT_LAYERS[0], local.keys(HOT_LAYERS[0])[0])
    result = warm(local, budget=first["bytes"])
    assert result.shards == 1 and result.cause == "budget"


def test_a_warm_that_fails_stops_and_reports(world):
    key, raw, _blobs, store = world
    from swarm_graph.warm import warm

    result = warm(_local(raw, {}))
    assert result.shards == 0 and result.cause == "SnapshotError"


def test_the_warmer_runs_in_the_background_and_reports():
    done = threading.Event()
    seen: list = []

    class _Snap:
        def keys(self, layer):
            return []

    warmer = Warmer(on_done=lambda *a: (seen.append(a), done.set()))
    try:
        assert warmer.saw("eng", REPO_ID, "sha256:" + "a" * 64, _Snap())
        assert not warmer.saw("eng", REPO_ID, "sha256:" + "a" * 64, _Snap())
        assert done.wait(5)
        assert seen[0][0] == "eng" and seen[0][2].cause == "done"
    finally:
        warmer.close()
