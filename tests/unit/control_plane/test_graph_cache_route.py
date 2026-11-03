"""The console's Cache section, swarm-api's route (docs/design/graph-answer-cache.md
§5.3; lane CACHE-3-ROUTE, part of #1835).

swarm-graph is a stubbed transport under the shipped `StatsReader`; the
registration goes through the real route, and the graphs are written by the
shipped writer, so the manifest comparison reads real manifests. No cloud
credentials, no emulator; every key is built at runtime.
"""

from __future__ import annotations

import importlib.util
import json
import math
import secrets
import socket
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, repograph
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.graph_ticket import (
    STATS_DOMAIN,
    TICKET_DOMAIN,
    TicketRefused,
    verify_graph_ticket,
    verify_stats_ticket,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import graph_cache
from swarm_api.routes.graph_cache import StatsReader, compare_manifests
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header
from .repo_fakes import TenantTokens
from .repo_index_fakes import REPOSITORY, IndexGitHub, sha

ONE, TWO = sha("one"), sha("two")
WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")
STATS_URL = "http://10.0.0.9:8081"


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ticket_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def client(db, tokens, group_map, objects, ticket_key, monkeypatch) -> TestClient:
    monkeypatch.delenv("SWARM_GRAPH_STATS_URL", raising=False)
    ctx = build_context(
        settings=api_settings(graph_ticket_key=ticket_key),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=TenantTokens(),
        forge=forge.GitHubIssues(send=IndexGitHub(heads={"main": ONE})),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


class Graph:
    """swarm-graph's `/v1/stats`, stubbed: records each request, answers `reply`."""

    def __init__(self, reply: Any = None, *, raises: Exception | None = None,
                 status: int = 200) -> None:
        self.reply = reply if reply is not None else {"replicas": []}
        self.raises = raises
        self.status = status
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers), timeout))
        if self.raises is not None:
            raise self.raises
        return self.status, json.dumps(self.reply).encode()

    def ticket(self) -> str:
        assert len(self.calls) == 1, self.calls
        return self.calls[0][1]["Authorization"].removeprefix("Bearer ")


def _use(client: TestClient, graph: Graph) -> None:
    client.app.dependency_overrides[graph_cache.stats_reader] = (
        lambda: StatsReader(STATS_URL, send=graph))


def _graph_doc(commit: str, *, changed: bool) -> dict:
    def sym(path: str, name: str) -> dict:
        return {"id": f"{path}#{name}", "kind": "function", "path": path, "start_line": 1,
                "end_line": 3, "language": "python", "exported": True}

    symbols = [sym("src/api/users.py", "get_user"), sym("src/store/db.py", "fetch")]
    files = [{"path": "src/api/users.py"}, {"path": "src/store/db.py"}]
    if changed:
        # db.py gains a symbol (its blob changes); src/new is a key that is new.
        symbols += [sym("src/store/db.py", "fetch_many"), sym("src/new/jobs.py", "run")]
        files += [{"path": "src/new/jobs.py"}]
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": commit,
        "branch": "main", "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "test"},
        "symbols": symbols, "call_edges": [], "symbol_test_map": [], "files": files,
    }


def _promote(db, objects, writer, tmp_path: Path, tenant: str, repo_id: str, commit: str, *,
             changed: bool, recorded_at: str, base_sha: str | None = None,
             current: bool = True) -> dict:
    root = tmp_path / f"store-{commit[:6]}"
    report = writer.write_graph(_graph_doc(commit, changed=changed), writer.LocalStore(root),
                                tenant_id=tenant, repo_id=repo_id)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    version = {"commit_sha": commit, "recorded_at": recorded_at, "base_sha": base_sha,
               "graph_digest": report["manifest_digest"],
               "graph_manifest": repograph.manifest_key(tenant, repo_id, commit),
               "answer_latency": {"state": "measured", "p95_ms": 152, "reference_ms": 20}}
    db.docs[f"repositories/{repo_id}/index_versions/{commit}"] = version
    if current:
        record = db.docs[f"repositories/{repo_id}"]
        record["index"] = {**(record.get("index") or {}), "current_sha": commit}
    return version


@pytest.fixture
def repo(client, db, objects, writer, tmp_path) -> dict:
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]
    tenant = db.docs[f"repositories/{repo_id}"]["tenant_id"]
    before = _promote(db, objects, writer, tmp_path, tenant, repo_id, ONE, changed=False,
                      recorded_at="2026-10-27T10:00:00+00:00", current=False)
    now = _promote(db, objects, writer, tmp_path, tenant, repo_id, TWO, changed=True,
                   recorded_at="2026-10-28T10:00:00+00:00", base_sha=ONE)
    return {"repo_id": repo_id, "tenant": tenant, "before": before, "now": now}


def _get(client, repo_id: str, user: str = "alice", query: str = ""):
    return client.get(f"/v1/repositories/{repo_id}/graph-cache{query}",
                      headers=auth_header(user))


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------

def test_another_tenants_repo_id_is_a_404_and_swarm_graph_is_never_called(client, repo):
    graph = Graph()
    _use(client, graph)
    answer = _get(client, repo["repo_id"], user="bob")
    assert answer.status_code == 404, answer.text
    assert repo["repo_id"] in answer.text and repo["now"]["graph_digest"] not in answer.text
    assert graph.calls == []


def test_an_unregistered_repo_id_is_a_404(client, repo):
    _use(client, Graph())
    assert _get(client, "repo_" + "0" * 16).status_code == 404


# --------------------------------------------------------------------------
# swarm-graph not answering
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raised, reason", [
    (ConnectionRefusedError(), "refused"),
    (urllib.error.URLError(ConnectionRefusedError()), "refused"),
    (socket.timeout(), "timeout"),
    (urllib.error.URLError(TimeoutError()), "timeout"),
])
def test_swarm_graph_down_is_a_200_not_read_and_the_latency_tile_still_shows(
    client, repo, raised, reason
):
    graph = Graph(raises=raised)
    _use(client, graph)
    answer = _get(client, repo["repo_id"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["replicas"] == []
    assert body["stats"] == {"state": "not_read", "reason": reason}
    assert body["latency"] == {"state": "measured", "p95_ms": 152, "reference_ms": 20}
    # Not read is never zeros (§5.4).
    assert body["facts"]["answers"] is None and body["facts"]["hit_rate"] is None
    assert graph.calls[0][2] == graph_cache.STATS_TIMEOUT_SECONDS == 3.0


def test_a_refused_ticket_or_a_body_that_is_not_the_document_is_not_read(client, repo):
    _use(client, Graph(status=401))
    assert _get(client, repo["repo_id"]).json()["stats"]["reason"] == "ticket_refused"
    _use(client, Graph(reply={"rows": 1}))
    assert _get(client, repo["repo_id"]).json()["stats"]["reason"] == "bad_response"


def test_the_stats_url_unset_is_a_200_not_read_with_nothing_called(client, repo):
    # No override: the shipped dependency reads settings.graph_stats_url, unset here.
    answer = _get(client, repo["repo_id"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["replicas"] == []
    assert body["stats"] == {"state": "not_read", "reason": "not_configured"}
    assert body["latency"]["p95_ms"] == 152


def test_the_stats_url_is_the_settings_graph_stats_url():
    assert graph_cache.stats_url(api_settings(graph_stats_url=STATS_URL + "/")) == STATS_URL
    # Not an http(s) URL is no URL at all: not_read, never a guess.
    assert graph_cache.stats_url(api_settings(graph_stats_url="10.0.0.9:8081")) == ""
    assert graph_cache.stats_url(api_settings()) == ""


def test_api_settings_reads_swarm_graph_stats_url_from_the_environment(monkeypatch):
    # CACHE-FOLLOWUPS (part of #1835): ApiSettings.graph_stats_url, read from
    # SWARM_GRAPH_STATS_URL as graph_ticket_key is read from its variable.
    monkeypatch.setenv("PROJECT_ID", "swarm-test-project")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("SWARM_GRAPH_STATS_URL", raising=False)
    assert ApiSettings.__dataclass_fields__["graph_stats_url"].default == ""
    assert ApiSettings.from_env().graph_stats_url == ""
    monkeypatch.setenv("SWARM_GRAPH_STATS_URL", " " + STATS_URL + " ")
    assert ApiSettings.from_env().graph_stats_url == STATS_URL


def test_the_setting_unset_is_not_read_even_with_the_variable_in_the_process(client, repo, monkeypatch):
    # The setting is the one source: a SWARM_GRAPH_STATS_URL in swarm-api's
    # process that ApiSettings did not read (this client's settings were built
    # without one) is not a second way in, and the section is not_read.
    monkeypatch.setenv("SWARM_GRAPH_STATS_URL", STATS_URL)
    body = _get(client, repo["repo_id"]).json()
    assert body["replicas"] == []
    assert body["stats"] == {"state": "not_read", "reason": "not_configured"}


# --------------------------------------------------------------------------
# the ticket
# --------------------------------------------------------------------------

def test_the_ticket_is_a_stats_ticket_never_an_attempt_one(client, repo, ticket_key):
    graph = Graph()
    _use(client, graph)
    assert _get(client, repo["repo_id"]).status_code == 200
    ticket = graph.ticket()
    assert graph.calls[0][0] == f"{STATS_URL}/v1/stats"
    assert STATS_DOMAIN == b"swarm-graph-stats/v1\0" and STATS_DOMAIN != TICKET_DOMAIN
    claims = verify_stats_ticket([ticket_key], ticket, now=time.time())
    assert claims["tenant_id"] == repo["tenant"]
    assert claims["repo_id"] == repo["repo_id"]
    assert claims["manifest_digest"] == repo["now"]["graph_digest"]
    assert claims["previous_digest"] == repo["before"]["graph_digest"]
    assert time.time() < claims["exp"] <= time.time() + 61
    # Its MAC is over the stats domain: the attempt verifier refuses it.
    with pytest.raises(TicketRefused):
        verify_graph_ticket([ticket_key], ticket, now=time.time())


def test_an_attempt_ticket_does_not_verify_as_a_stats_ticket(ticket_key):
    from swarm_api.graph_ticket import mint_graph_ticket

    attempt = mint_graph_ticket(ticket_key, tenant_id="eng", repo_id="repo_" + "a" * 16,
                                task_id="t", attempt_id="a", generation=1,
                                index_sha=ONE, manifest_digest="sha256:" + "b" * 64,
                                exp=int(time.time()) + 60)
    with pytest.raises(TicketRefused):
        verify_stats_ticket([ticket_key], attempt, now=time.time())


# --------------------------------------------------------------------------
# what the replicas said
# --------------------------------------------------------------------------

def _replica(name: str, repo: dict, **repository: Any) -> dict:
    return {"replica": name, "started_at": "2026-10-28T08:00:00+00:00",
            "result_bytes": 1024, "result_bound": 256 << 20,
            "shard_bytes": 2048, "shard_bound": 512 << 20,
            "repository": {"answers_hot": 3, "shards_hot": 2, **repository}}


def test_replicas_are_summed_into_the_facts_and_a_peer_not_read_keeps_its_reason(
    client, repo
):
    now_digest = repo["now"]["graph_digest"]
    graph = Graph(reply={"replicas": [
        _replica("swarm-graph-0", repo,
                 outcomes=[{"hit": 8, "miss": 2}], served_ms={"10": 9, "50": 1},
                 last_warm={"digest": now_digest, "ms": 2100, "shards": 7, "replayed": 0}),
        _replica("swarm-graph-1", repo, outcomes=[{"hit": 1, "miss": 1}],
                 last_warm={"digest": "sha256:" + "c" * 64, "ms": 9, "shards": 1,
                            "replayed": 0}),
        {"replica": "swarm-graph-2", "state": "not_read", "reason": "timeout"},
    ]})
    _use(client, graph)
    body = _get(client, repo["repo_id"]).json()
    assert body["stats"] == {"state": "read"}
    facts = body["facts"]
    assert (facts["answers"], facts["hit"], facts["miss"]) == (12, 9, 3)
    assert facts["hit_rate"] == pytest.approx(0.75)
    # No replica counts `carried`: the reuse tile is a dash, never 0%.
    assert facts["carried"] is None and facts["reused_share"] is None
    assert facts["last_warm"]["ms"] == 2100
    assert body["replicas"][2] == {"replica": "swarm-graph-2", "state": "not_read",
                                   "reason": "timeout"}
    # A warm of a digest that is not this repository's is never named back.
    assert body["replicas"][1]["last_warm"] is None
    assert "c" * 64 not in json.dumps(body)


# --------------------------------------------------------------------------
# the manifest comparison
# --------------------------------------------------------------------------

def test_the_comparison_marks_a_changed_blob_and_a_new_key_as_changed():
    blob = lambda c: "sha256:" + c * 64  # noqa: E731
    before = {"shards": {"symbols": {"src/api": {"blob": blob("a")},
                                     "src/store": {"blob": blob("b")}}}}
    after = {"shards": {"symbols": {"src/api": {"blob": blob("a"), "raw_bytes": 10},
                                    "src/store": {"blob": blob("c")},
                                    "src/new": {"blob": blob("d")}}}}
    marks = {r["module"]: r["last_merge"] for r in compare_manifests(after, before)}
    assert marks == {"src/api": "unchanged", "src/store": "changed", "src/new": "changed"}
    # No previous manifest: nothing is claimed either way.
    assert {r["last_merge"] for r in compare_manifests(after, None)} == {None}


def test_shards_compare_the_promoted_manifests_and_merge_each_blobs_marks(
    client, repo, objects
):
    tenant, repo_id = repo["tenant"], repo["repo_id"]
    manifest = json.loads(objects.objects[repo["now"]["graph_manifest"]])
    db_blob = manifest["shards"]["symbols"]["src/store"]["blob"]
    graph = Graph(reply={"replicas": [_replica("swarm-graph-0", repo, blobs={
        db_blob: {"hot": True, "hits_24h": 5, "last_read": "2026-10-28T11:00:00+00:00"},
        # A hash the manifest does not name is never passed on.
        "sha256:" + "e" * 64: {"hot": True, "hits_24h": 99},
    })]})
    _use(client, graph)
    answer = _get(client, repo_id, query="?shards=1")
    assert answer.status_code == 200, answer.text
    assert graph.calls[0][0] == f"{STATS_URL}/v1/stats?shards=1"
    shards = answer.json()["shards"]
    rows = {(r["layer"], r["module"]): r for r in shards["rows"]}
    assert rows[("symbols", "src/store")]["last_merge"] == "changed"
    assert rows[("symbols", "src/new")]["last_merge"] == "changed"
    assert rows[("symbols", "src/api")]["last_merge"] == "unchanged"
    assert rows[("symbols", "src/store")]["replicas"] == [{"replica": "swarm-graph-0",
                                                           "hot": True}]
    assert rows[("symbols", "src/store")]["hits_24h"] == 5
    assert shards["changed"] == sum(1 for r in shards["rows"] if r["last_merge"] == "changed")
    # Changed first.
    seen = [r["last_merge"] for r in shards["rows"]]
    assert seen == sorted(seen, key=lambda m: m != "changed")
    assert "e" * 64 not in answer.text
    assert all(f"tenants/{tenant}/" not in str(r) for r in shards["rows"])


def test_no_promoted_index_reads_nothing(client, db):
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    repo_id = created.json()["repository"]["repo_id"]
    graph = Graph()
    _use(client, graph)
    body = _get(client, repo_id).json()
    assert body["stats"] == {"state": "not_read", "reason": "no_index"}
    assert graph.calls == []


# --------------------------------------------------------------------------
# the served answer latency (lane graph-cache-panel-latency, part of #2688)
# --------------------------------------------------------------------------

def _group(count: int, p50: int | None = None, p95: int | None = None,
           p99: int | None = None, top: int | None = None) -> dict:
    return {"count": count, "p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "max_ms": top}


def _latency(overall: dict, *, by_outcome: dict | None = None,
             by_temperature: dict | None = None, cells: dict | None = None) -> dict:
    # swarm_graph/stats.py `_latency`'s shape: every outcome and temperature,
    # a count of 0 with null percentiles where nothing was seen.
    empty = _group(0)
    return {"window_hours": 24, "bounds_ms": [1, 2, 5, 10], **overall,
            "by_outcome": {o: (by_outcome or {}).get(o, empty)
                           for o in ("hit", "carried", "miss", "partial", "uncached")},
            "by_temperature": {t: (by_temperature or {}).get(t, empty) for t in ("warm", "cold")},
            "cells": cells or {}}


def _served(name: str, digest: str, latency: dict | None, **top: Any) -> dict:
    """One replica as swarm-graph's `stats.local_document` serves it: the
    ticket's repository's figures at the top of the document."""
    document = {"replica": name, "state": "read", "started_at": "2026-10-28T08:00:00Z",
                "window_hours": 24, "result_bytes": 1024, "result_bound": 256 << 20,
                "shard_bytes": 2048, "shard_bound": 512 << 20,
                "outcomes": [{"hour": "2026-10-28T09:00:00Z", "hit": 6, "carried": 2, "miss": 2}],
                "served_ms": {"bounds_ms": [1, 2, 4, 8, 16, 32],
                              "counts": [0, 0, 0, 9, 0, 1, 0]},
                "answers_hot": 4, "shards_hot": None,
                "last_warm": {"digest": digest, "ms": 900, "shards": 3, "replayed": 1,
                              "cause": "promotion", "at": "2026-10-28T09:00:00Z"},
                **top}
    if latency is not None:
        document["answer_latency"] = latency
    return document


def test_two_replicas_latencies_are_merged_into_the_facts(client, repo):
    digest = repo["now"]["graph_digest"]
    zero = _latency(_group(10, 4, 40, 60, 75),
                    by_outcome={"hit": _group(6, 3, 8, 8, 9), "carried": _group(2, 5, 6, 6, 6),
                                "miss": _group(2, 40, 75, 75, 75)},
                    by_temperature={"warm": _group(8, 3, 8, 8, 9), "cold": _group(2, 40, 75, 75, 75)},
                    cells={"hit/warm": _group(6, 3, 8, 8, 9), "miss/cold": _group(2, 40, 75, 75, 75)})
    one = _latency(_group(5, 6, 120, 120, 130),
                   by_outcome={"hit": _group(4, 5, 10, 10, 12), "partial": _group(1, 130, 130, 130, 130)},
                   by_temperature={"warm": _group(4, 5, 10, 10, 12), "cold": _group(1, 130, 130, 130, 130)})
    _use(client, Graph(reply={"replicas": [_served("swarm-graph-0", digest, zero),
                                           _served("swarm-graph-1", digest, one)]}))
    body = _get(client, repo["repo_id"]).json()
    served = body["facts"]["answer_latency"]
    assert served["state"] == "read"
    assert (served["replicas_read"], served["not_read"]) == (2, [])
    # Counts add; the slowest is the slowest of the two; with two replicas
    # answering, each percentile is the slower replica's, an upper bound.
    assert (served["count"], served["max_ms"]) == (15, 130)
    assert (served["p50_ms"], served["p95_ms"], served["p99_ms"]) == (6, 120, 120)
    assert served["exact"] is False
    outcomes = served["by_outcome"]
    assert outcomes["hit"] == {"count": 10, "p50_ms": 5, "p95_ms": 10, "p99_ms": 10,
                               "max_ms": 12, "exact": False}
    # Only one replica answered carried, miss or partial: those are exact.
    assert outcomes["carried"] == {"count": 2, "p50_ms": 5, "p95_ms": 6, "p99_ms": 6,
                                   "max_ms": 6, "exact": True}
    assert outcomes["partial"]["p95_ms"] == 130 and outcomes["partial"]["exact"] is True
    # Nothing uncached was seen: a count of 0 with no figure, never a 0 ms.
    assert outcomes["uncached"] == {"count": 0, "p50_ms": None, "p95_ms": None,
                                    "p99_ms": None, "max_ms": None, "exact": True}
    temperature = served["by_temperature"]
    assert (temperature["warm"]["count"], temperature["warm"]["p95_ms"]) == (12, 10)
    assert (temperature["cold"]["count"], temperature["cold"]["max_ms"]) == (3, 130)
    assert served["cells"]["miss/cold"]["count"] == 2 and "partial/cold" not in served["cells"]
    # The indexer's uncached measurement still sits beside it, unchanged.
    assert body["latency"] == {"state": "measured", "p95_ms": 152, "reference_ms": 20}
    # Each replica's panel carries its own figure.
    assert body["replicas"][1]["answer_latency"]["p95_ms"] == 120
    assert body["replicas"][1]["answer_latency"]["by_temperature"]["cold"]["count"] == 1


#: swarm_graph/stats.py's LATENCY_BOUNDS_MS head, enough for the samples below.
_BOUNDS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150]


def _counted(*samples: int) -> dict:
    """A group as a swarm-graph that serves its histogram counts: the counts
    over `_BOUNDS` and the percentiles stats._percentiles reads off them."""
    counts = [0] * (len(_BOUNDS) + 1)
    for ms in samples:
        counts[next((i for i, b in enumerate(_BOUNDS) if ms <= b), len(_BOUNDS))] += 1
    slowest = max(samples, default=0)

    def at(q: float) -> int | None:
        if not samples:
            return None
        rank, seen = max(1, math.ceil(round(q * len(samples), 6))), 0
        for index, n in enumerate(counts):
            seen += n
            if seen >= rank:
                return min(_BOUNDS[index] if index < len(_BOUNDS) else slowest, slowest)
        return None

    return {"count": len(samples), "p50_ms": at(0.5), "p95_ms": at(0.95), "p99_ms": at(0.99),
            "max_ms": slowest if samples else None, "counts": counts}


def _counted_latency(*samples: int, outcome: str = "hit") -> dict:
    overall = _counted(*samples)
    return {**_latency(overall, by_outcome={outcome: overall},
                       by_temperature={"warm": overall},
                       cells={f"{outcome}/warm": overall}),
            "bounds_ms": _BOUNDS}


def test_two_replicas_histograms_merge_into_the_exact_percentile(client, repo):
    # The owner's pick (3 November 2026): every replica served its counts,
    # so the merged figure is read off the summed histogram, exact.
    digest = repo["now"]["graph_digest"]
    zero = _counted_latency(*([3] * 90))                  # p95 3 ms alone
    one = _counted_latency(*([3] * 5 + [40] * 5))         # p95 40 ms alone
    _use(client, Graph(reply={"replicas": [_served("swarm-graph-0", digest, zero),
                                           _served("swarm-graph-1", digest, one)]}))
    body = _get(client, repo["repo_id"]).json()
    served = body["facts"]["answer_latency"]
    # The histograms stay in the route: the console gets figures.
    assert "counts" not in served and "counts" not in served["by_outcome"]["hit"]
    panel = body["replicas"][1]["answer_latency"]
    assert "counts" not in panel and "counts" not in panel["by_temperature"]["warm"]
    # 100 answers, 95 of them at 3 ms: the merged p95 is 3 ms, where the
    # slowest replica's bound would say 40 ms; the p99 is in the 40 ms bucket.
    assert (served["count"], served["max_ms"]) == (100, 40)
    assert (served["p50_ms"], served["p95_ms"], served["p99_ms"]) == (3, 3, 40)
    assert served["exact"] is True
    hit = served["by_outcome"]["hit"]
    assert (hit["count"], hit["p95_ms"], hit["exact"]) == (100, 3, True)
    assert served["by_temperature"]["warm"]["p95_ms"] == 3
    assert served["cells"]["hit/warm"]["exact"] is True
    # A group neither replica saw stays a count of 0 with no figure.
    assert served["by_outcome"]["miss"] == {"count": 0, "p50_ms": None, "p95_ms": None,
                                            "p99_ms": None, "max_ms": None, "exact": True}


def test_a_replica_without_counts_keeps_the_merge_an_upper_bound(client, repo):
    # Mixed versions during a roll-out: one replica serves percentiles only,
    # so the merge falls back to the slowest replica's figure, not exact.
    digest = repo["now"]["graph_digest"]
    counted = _counted_latency(*([3] * 90))
    older = _counted_latency(*([3] * 5 + [40] * 5))
    for group in (older, *older["by_outcome"].values(), *older["by_temperature"].values(),
                  *older["cells"].values()):
        group.pop("counts", None)
    _use(client, Graph(reply={"replicas": [_served("swarm-graph-0", digest, counted),
                                           _served("swarm-graph-1", digest, older)]}))
    served = _get(client, repo["repo_id"]).json()["facts"]["answer_latency"]
    assert served["count"] == 100
    assert (served["p50_ms"], served["p95_ms"], served["exact"]) == (3, 40, False)
    assert served["by_outcome"]["hit"]["exact"] is False


def test_counts_that_do_not_add_up_are_not_merged_as_exact(client, repo):
    # Counts are only trusted when they are the served count over the served
    # bounds: a histogram that disagrees with its own count is a bound.
    digest = repo["now"]["graph_digest"]
    zero = _counted_latency(*([3] * 90))
    one = _counted_latency(*([3] * 5 + [40] * 5))
    one["counts"] = one["counts"][:-1]
    one["by_outcome"]["hit"] = {**one["by_outcome"]["hit"], "count": 11}
    _use(client, Graph(reply={"replicas": [_served("swarm-graph-0", digest, zero),
                                           _served("swarm-graph-1", digest, one)]}))
    served = _get(client, repo["repo_id"]).json()["facts"]["answer_latency"]
    assert (served["p95_ms"], served["exact"]) == (40, False)
    assert served["by_outcome"]["hit"]["exact"] is False
    assert served["by_temperature"]["warm"]["exact"] is True


def test_the_document_swarm_graph_serves_is_read_at_its_top_level(client, repo):
    # stats.local_document puts the repository's figures at the top of each
    # replica's document, its served_ms as bounds and counts.
    digest = repo["now"]["graph_digest"]
    _use(client, Graph(reply={"replicas": [_served("swarm-graph-0", digest,
                                                   _latency(_group(10, 4, 40, 40, 40)))]}))
    body = _get(client, repo["repo_id"]).json()
    facts = body["facts"]
    assert (facts["answers"], facts["hit"], facts["carried"], facts["miss"]) == (10, 6, 2, 2)
    assert facts["served_p95_ms"] == 32
    assert facts["last_warm"]["ms"] == 900
    assert body["replicas"][0]["answers_hot"] == 4
    assert facts["answer_latency"]["p95_ms"] == 40
    assert facts["answer_latency"]["exact"] is True


def test_a_replica_that_did_not_answer_reads_not_read_never_zero(client, repo):
    digest = repo["now"]["graph_digest"]
    _use(client, Graph(reply={"replicas": [
        _served("swarm-graph-0", digest, _latency(_group(5, 4, 9, 9, 9))),
        {"replica": "10.0.0.12:8081", "state": "not_read", "reason": "timeout"},
        # An older swarm-graph read but serving no latency is not a zero either.
        _served("swarm-graph-2", digest, None),
    ]}))
    body = _get(client, repo["repo_id"]).json()
    served = body["facts"]["answer_latency"]
    assert served["state"] == "read" and served["replicas_read"] == 1
    assert served["not_read"] == ["10.0.0.12:8081", "swarm-graph-2"]
    assert served["count"] == 5 and served["exact"] is True
    assert body["replicas"][1] == {"replica": "10.0.0.12:8081", "state": "not_read",
                                   "reason": "timeout"}
    assert body["replicas"][2]["answer_latency"] is None


@pytest.mark.parametrize("replicas, reason", [
    ([{"replica": "swarm-graph-0", "state": "not_read", "reason": "refused"}], "no_replica_read"),
    ([], "no_replica_read"),
])
def test_no_replica_answering_is_not_read_with_no_figure(client, repo, replicas, reason):
    _use(client, Graph(reply={"replicas": replicas}))
    served = _get(client, repo["repo_id"]).json()["facts"]["answer_latency"]
    assert served["state"] == "not_read" and served["reason"] == reason
    assert "count" not in served and "p95_ms" not in served


def test_replicas_read_without_the_figure_are_not_served(client, repo):
    _use(client, Graph(reply={"replicas": [_served("swarm-graph-0", repo["now"]["graph_digest"],
                                                   None)]}))
    served = _get(client, repo["repo_id"]).json()["facts"]["answer_latency"]
    assert served == {"state": "not_read", "reason": "not_served", "not_read": ["swarm-graph-0"]}


def test_swarm_graph_not_answering_leaves_the_served_latency_not_read(client, repo):
    _use(client, Graph(raises=socket.timeout()))
    body = _get(client, repo["repo_id"]).json()
    assert body["facts"]["answer_latency"] == {"state": "not_read", "reason": "timeout"}
    assert body["latency"]["p95_ms"] == 152


def test_no_promoted_index_has_the_served_latency_not_read(client, db):
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    repo_id = created.json()["repository"]["repo_id"]
    _use(client, Graph())
    facts = _get(client, repo_id).json()["facts"]
    assert facts["answer_latency"] == {"state": "not_read", "reason": "no_index"}
    assert facts["answers"] is None


def test_a_figure_that_is_not_a_number_is_not_passed_on():
    from swarm_api.routes.graph_cache import replica_latency

    assert replica_latency({"count": "5"}) is None
    assert replica_latency("fast") is None
    got = replica_latency({"count": 3, "p50_ms": "4", "p95_ms": -1, "p99_ms": True,
                           "max_ms": 9, "by_outcome": {"hit": _group(3, 1, 2, 2, 2),
                                                       "someone_else": _group(9, 9, 9, 9, 9)}})
    assert got is not None
    assert (got["p50_ms"], got["p95_ms"], got["p99_ms"], got["max_ms"]) == (None, None, None, 9)
    assert list(got["by_outcome"]) == ["hit"]
