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
