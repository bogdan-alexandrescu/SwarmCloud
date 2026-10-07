"""Graph shards: promotion records them, swarm-api reads them (repo-index.md §2.5, lane RI9).

The indexer stores the graph with `swarm-repo-graph` (images/agent-runtime-
base/repo-index/repo_graph_shards.py) under the tenant's own prefix and puts
the manifest's digest in `repo-index.json` as `graph.manifest_digest`. What
is held here:

  * promotion reads the manifest at the commit's own key under the tenant's
    prefix, checks its bytes against that digest, and records
    `graph_manifest` and `graph_digest` on the version -- by the same
    sha-order rule as the index (RI2), in the same transaction;
  * promotion REFUSES a manifest whose digest does not match, one that is
    missing, and one that describes another tenant, repository or commit; an
    unreadable bucket leaves the run to be promoted on a later settle;
  * `repograph.RepoGraph` reads a version's shards for the API: the manifest
    digest-checked against the version, every blob checked against its name
    before it is decompressed, every key built from the CALLER's tenant, so
    another tenant's graph is unreachable rather than filtered.

The manifests and blobs here are written by the shipped writer itself, so a
drift between the writer's format and the reader's is a red test.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from swarm_api import repograph, repoindex
from swarm_api.errors import UpstreamUnavailable
from swarm_api.objects import InMemoryObjectReader

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE, TWO = sha("one"), sha("two")
WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def github():
    return IndexGitHub(heads={"main": ONE})


@pytest.fixture
def client(db, tokens, group_map, objects, github):
    return make_client(
        db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=github
    )


def _register(client, user: str = "alice") -> str:
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header(user))
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


@pytest.fixture
def repo_id(client) -> str:
    return _register(client)


def graph_doc(commit: str) -> dict:
    def sym(path: str, name: str) -> dict:
        return {"id": f"{path}#{name}", "kind": "function", "path": path, "start_line": 1,
                "end_line": 3, "language": "python", "exported": True}

    def edge(frm: str, to: str, confidence: float = 0.6) -> dict:
        return {"from": frm, "to": to, "kind": "call", "evidence": "ast",
                "confidence": confidence, "also_evidence": [], "path": frm.split("#")[0],
                "line": 2, "sites": 1}

    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": commit,
        "branch": "main", "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "1"},
        "symbols": [sym("src/api/users.py", "get_user"), sym("src/api/users.py", "load_user"),
                    sym("src/store/db.py", "fetch"),
                    sym("tests/api/test_users.py", "test_get_user")],
        "call_edges": [edge("src/api/users.py#get_user", "src/api/users.py#load_user"),
                       edge("src/api/users.py#load_user", "src/store/db.py#fetch"),
                       edge("tests/api/test_users.py#test_get_user",
                            "src/api/users.py#get_user")],
        "symbol_test_map": [{"symbol": "src/store/db.py#fetch",
                             "test": "tests/api/test_users.py#test_get_user", "depth": 3,
                             "confidence": 0.216}],
        "files": [{"path": "src/api/users.py"}, {"path": "src/store/db.py"},
                  {"path": "tests/api/test_users.py"}],
    }


def _upload(writer, objects: InMemoryObjectReader, tmp_path: Path, tenant: str, repo: str,
            commit: str = ONE) -> dict:
    """Write a graph with the shipped writer, then put every object in the bucket."""
    root = tmp_path / f"store-{tenant}-{commit[:6]}"
    report = writer.write_graph(graph_doc(commit), writer.LocalStore(root),
                                tenant_id=tenant, repo_id=repo)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    return report


def _start(client, repo_id) -> str:
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    return started.json()["run"]["task_id"]


def _tenant(db, task_id: str) -> str:
    return db.docs[f"tasks/{task_id}"]["tenant_id"]


def _settle(client, repo_id):
    return client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))


def _index_with_graph(commit: str, digest: str | None) -> dict:
    return fixture_index(commit, graph={"manifest_digest": digest, "symbols": 4, "edges": 3,
                                        "top_symbols": []})


# --------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------

def test_promotion_records_the_graph_manifest_and_its_digest(
    client, db, objects, repo_id, writer, tmp_path
):
    task_id = _start(client, repo_id)
    tenant = _tenant(db, task_id)
    report = _upload(writer, objects, tmp_path, tenant, repo_id)
    finish_index_task(db, objects, task_id, _index_with_graph(ONE, report["manifest_digest"]))
    assert _settle(client, repo_id).status_code == 200
    version = db.docs[f"repositories/{repo_id}/index_versions/{ONE}"]
    assert version["graph_manifest"] == (
        f"tenants/{tenant}/repos/{repo_id}/graph/{ONE}/manifest.json"
    )
    assert version["graph_manifest"] == report["manifest"]
    assert version["graph_digest"] == report["manifest_digest"]
    # Lane IX2 review: what the next run's `choose_kind` reads. This graph's
    # files carry no blob id (it is shaped like one promoted before IX2), so
    # the next run is submitted full, with the full timeout.
    assert version["graph_extractor"] == {"version": "1", "blob_ids": False,
                                          "files_not_listed": 0, "truncated": []}
    assert "blob id" in repoindex.graph_carry_refusal(version["graph_extractor"])
    run = db.docs[f"repo_index_runs/{task_id}"]
    assert run["promotion"]["outcome"] == "promoted"
    assert db.docs[f"repositories/{repo_id}"]["index"]["current_sha"] == ONE


def test_an_index_with_no_graph_promotes_with_no_graph_fields(client, db, objects, repo_id):
    task_id = _start(client, repo_id)
    finish_index_task(db, objects, task_id, fixture_index(ONE))
    assert _settle(client, repo_id).status_code == 200
    version = db.docs[f"repositories/{repo_id}/index_versions/{ONE}"]
    assert version["graph_manifest"] is None and version["graph_digest"] is None
    assert version["graph_extractor"] is None


def _refused(db, repo_id: str, task_id: str) -> str:
    run = db.docs[f"repo_index_runs/{task_id}"]
    assert run["promotion"]["outcome"] == "refused", run
    assert db.docs[f"repositories/{repo_id}"]["index"]["current_sha"] is None
    assert f"repositories/{repo_id}/index_versions/{ONE}" not in db.docs
    return run["promotion"]["reason"]


def test_promotion_refuses_a_graph_manifest_whose_digest_does_not_match(
    client, db, objects, repo_id, writer, tmp_path
):
    task_id = _start(client, repo_id)
    report = _upload(writer, objects, tmp_path, _tenant(db, task_id), repo_id)
    named = "sha256:" + hashlib.sha256(b"another manifest").hexdigest()
    assert named != report["manifest_digest"]
    finish_index_task(db, objects, task_id, _index_with_graph(ONE, named))
    _settle(client, repo_id)
    reason = _refused(db, repo_id, task_id)
    assert "graph manifest" in reason and "digest" in reason


def test_promotion_refuses_a_graph_manifest_rewritten_after_the_index_named_it(
    client, db, objects, repo_id, writer, tmp_path
):
    task_id = _start(client, repo_id)
    report = _upload(writer, objects, tmp_path, _tenant(db, task_id), repo_id)
    manifest = json.loads(objects.objects[report["manifest"]])
    manifest["shards"]["callers"] = {}
    objects.put(report["manifest"], json.dumps(manifest))
    finish_index_task(db, objects, task_id, _index_with_graph(ONE, report["manifest_digest"]))
    _settle(client, repo_id)
    assert "digest" in _refused(db, repo_id, task_id)


def test_promotion_refuses_a_named_graph_manifest_that_is_not_there(
    client, db, objects, repo_id
):
    task_id = _start(client, repo_id)
    named = "sha256:" + hashlib.sha256(b"never written").hexdigest()
    finish_index_task(db, objects, task_id, _index_with_graph(ONE, named))
    _settle(client, repo_id)
    assert "not in the bucket" in _refused(db, repo_id, task_id)


def test_promotion_refuses_a_manifest_written_for_another_tenant_or_commit(
    client, db, objects, repo_id, writer, tmp_path
):
    task_id = _start(client, repo_id)
    tenant = _tenant(db, task_id)
    # A manifest describing another tenant's graph (and one describing another
    # commit), copied to this commit's key with a digest that matches its bytes.
    for other_tenant, commit, word in (("research", ONE, "tenant"), (tenant, TWO, "commit")):
        report = _upload(writer, InMemoryObjectReader(), tmp_path, other_tenant, repo_id,
                         commit=commit)
        source = tmp_path / f"store-{other_tenant}-{commit[:6]}" / report["manifest"]
        raw = source.read_bytes()
        objects.put(f"tenants/{tenant}/repos/{repo_id}/graph/{ONE}/manifest.json", raw)
        finish_index_task(db, objects, task_id,
                          _index_with_graph(ONE, "sha256:" + hashlib.sha256(raw).hexdigest()))
        # The run back to unfinished, so the next settle promotes it again.
        db.docs[f"repo_index_runs/{task_id}"].update(promotion=None, ended_at=None)
        _settle(client, repo_id)
        assert word in _refused(db, repo_id, task_id)


def test_an_unreadable_bucket_leaves_the_run_to_a_later_settle(
    client, db, objects, repo_id, writer, tmp_path
):
    task_id = _start(client, repo_id)
    tenant = _tenant(db, task_id)
    report = _upload(writer, objects, tmp_path, tenant, repo_id)
    finish_index_task(db, objects, task_id, _index_with_graph(ONE, report["manifest_digest"]))
    objects.fail_on(f"tenants/{tenant}/repos/")
    _settle(client, repo_id)
    assert db.docs[f"repo_index_runs/{task_id}"].get("promotion") is None
    assert db.docs[f"repositories/{repo_id}"]["index"]["current_sha"] is None
    objects.fail_prefixes = ()
    _settle(client, repo_id)
    assert db.docs[f"repo_index_runs/{task_id}"]["promotion"]["outcome"] == "promoted"


# --------------------------------------------------------------------------
# reading the shards (repograph)
# --------------------------------------------------------------------------

@pytest.fixture
def promoted(client, db, objects, repo_id, writer, tmp_path) -> dict:
    task_id = _start(client, repo_id)
    tenant = _tenant(db, task_id)
    report = _upload(writer, objects, tmp_path, tenant, repo_id)
    finish_index_task(db, objects, task_id, _index_with_graph(ONE, report["manifest_digest"]))
    _settle(client, repo_id)
    version = db.docs[f"repositories/{repo_id}/index_versions/{ONE}"]
    return {"tenant": tenant, "repo_id": repo_id, "version": version, "report": report}


def test_the_reader_answers_callers_callees_symbols_and_tests_from_the_shards(
    objects, promoted
):
    graph = repograph.RepoGraph(lambda: objects).open(
        promoted["tenant"], promoted["repo_id"], promoted["version"]
    )
    assert graph.digest == promoted["report"]["manifest_digest"]
    callers = graph.callers("src/store/db.py#fetch")
    assert [(e["from"], e["evidence"], e["confidence"]) for e in callers] == [
        ("src/api/users.py#load_user", "ast", 0.6)
    ]
    assert [e["to"] for e in graph.callees("src/api/users.py#get_user")] == [
        "src/api/users.py#load_user"
    ]
    assert [s["id"] for s in graph.symbols("src/api")] == [
        "src/api/users.py#get_user", "src/api/users.py#load_user"
    ]
    assert [t["test"] for t in graph.tests_for("src/store/db.py#fetch")] == [
        "tests/api/test_users.py#test_get_user"
    ]
    assert graph.callers("src/nowhere/x.py#y") == []
    assert graph.manifest["counts"]["symbols"] == 4


def test_another_tenant_cannot_read_the_graph(objects, promoted):
    reader = repograph.RepoGraph(lambda: objects)
    # Tenant eng's version record, asked for by research: its manifest key is
    # not under research's prefix, so it is refused before anything is read.
    with pytest.raises(repograph.InvalidGraph):
        reader.open("research", promoted["repo_id"], promoted["version"])
    # The same record with its key re-rooted to research's prefix: the key is
    # research's own, and eng's graph is not there to be found.
    as_research = dict(promoted["version"], graph_manifest=promoted["version"]["graph_manifest"]
                       .replace(f"tenants/{promoted['tenant']}/", "tenants/research/"))
    with pytest.raises(repograph.GraphUnavailable):
        reader.open("research", promoted["repo_id"], as_research)
    # And eng cannot be pointed into research's prefix by its own record.
    with pytest.raises(repograph.InvalidGraph):
        reader.open(promoted["tenant"], promoted["repo_id"], as_research)


def test_a_manifest_rewritten_after_promotion_is_refused_not_served(objects, promoted):
    key = promoted["version"]["graph_manifest"]
    manifest = json.loads(objects.objects[key])
    manifest["counts"]["symbols"] = 0
    objects.put(key, json.dumps(manifest))
    with pytest.raises(repograph.GraphDigestMismatch) as refused:
        repograph.RepoGraph(lambda: objects).open(
            promoted["tenant"], promoted["repo_id"], promoted["version"]
        )
    assert refused.value.code == "graph_digest_mismatch"


def test_a_rewritten_blob_is_refused_before_it_is_decompressed(objects, promoted):
    graph = repograph.RepoGraph(lambda: objects).open(
        promoted["tenant"], promoted["repo_id"], promoted["version"]
    )
    entry = graph.manifest["shards"]["callers"]["src/store"]
    key = repograph.blob_key(promoted["tenant"], promoted["repo_id"], entry["blob"])
    objects.put(key, gzip.compress(b'{"from":"x","to":"src/store/db.py#fetch"}\n'))
    with pytest.raises(repograph.GraphDigestMismatch):
        graph.callers("src/store/db.py#fetch")


def test_prefetch_reads_every_shard_of_a_layer_and_answers_from_them(objects, promoted):
    graph = repograph.RepoGraph(lambda: objects).open(
        promoted["tenant"], promoted["repo_id"], promoted["version"]
    )
    graph.prefetch(("symbols", "callers"))
    # Read once: the shards are served from memory, so a removed blob is not re-read.
    for key in [k for k in objects.objects if "/graph/blobs/" in k]:
        del objects.objects[key]
    assert [s["id"] for s in graph.symbols("src/api")] == [
        "src/api/users.py#get_user", "src/api/users.py#load_user"
    ]
    assert [e["from"] for e in graph.callers("src/store/db.py#fetch")] == [
        "src/api/users.py#load_user"
    ]
    # The control: a layer not prefetched is still read on demand, and is gone.
    with pytest.raises(repograph.GraphUnavailable):
        graph.tests_for("src/store/db.py#fetch")
    with pytest.raises(repograph.InvalidGraph):
        graph.prefetch(("nonsense",))


def test_prefetch_refuses_a_rewritten_blob_as_a_single_read_does(objects, promoted):
    graph = repograph.RepoGraph(lambda: objects).open(
        promoted["tenant"], promoted["repo_id"], promoted["version"]
    )
    entry = graph.manifest["shards"]["callers"]["src/store"]
    key = repograph.blob_key(promoted["tenant"], promoted["repo_id"], entry["blob"])
    objects.put(key, gzip.compress(b'{"from":"x","to":"src/store/db.py#fetch"}\n'))
    with pytest.raises(repograph.GraphDigestMismatch):
        graph.prefetch(("symbols", "callers"))


def test_prefetch_says_an_unreadable_store_is_unavailable(objects, promoted):
    graph = repograph.RepoGraph(lambda: objects).open(
        promoted["tenant"], promoted["repo_id"], promoted["version"]
    )
    objects.fail_on(f"tenants/{promoted['tenant']}/repos/{promoted['repo_id']}/graph/blobs/")
    with pytest.raises(UpstreamUnavailable):
        graph.prefetch(("callees",))


def test_a_cached_view_is_kept_per_store_and_per_key(objects, promoted):
    graphs = repograph.RepoGraph(lambda: objects)
    calls: list[str] = []

    def compute(name: str):
        return lambda: calls.append(name) or {"name": name}

    key = (promoted["tenant"], promoted["repo_id"], "sha256:" + "a" * 64, "module")
    assert graphs.view(key, compute("one")) == {"name": "one"}
    assert graphs.view(key, compute("two")) == {"name": "one"}
    assert graphs.view(key[:3] + ("package",), compute("three")) == {"name": "three"}
    # Another store (another deployment's bucket, another test) has its own views.
    assert repograph.RepoGraph(lambda: InMemoryObjectReader()).view(key, compute("four")) \
        == {"name": "four"}
    assert calls == ["one", "three", "four"]


def test_a_version_with_no_graph_says_so(objects, promoted):
    version = dict(promoted["version"], graph_manifest=None, graph_digest=None)
    with pytest.raises(repograph.NoGraph):
        repograph.RepoGraph(lambda: objects).open(
            promoted["tenant"], promoted["repo_id"], version
        )


def test_an_unreadable_bucket_is_unavailable_not_absent(objects, promoted):
    objects.fail_on(f"tenants/{promoted['tenant']}/repos/")
    with pytest.raises(UpstreamUnavailable):
        repograph.RepoGraph(lambda: objects).open(
            promoted["tenant"], promoted["repo_id"], promoted["version"]
        )


def test_unsafe_identifiers_never_reach_a_key():
    for tenant, repo in (("../research", "r"), ("eng", "a/b"), ("eng", "")):
        with pytest.raises(repograph.InvalidGraph):
            repograph.manifest_key(tenant, repo, ONE)
    with pytest.raises(repograph.InvalidGraph):
        repograph.manifest_key("eng", "r", "main")
