"""The graph shard writer and the blob sweep (docs/repo-index.md §2.5, lane RI9).

RI3's extractor writes the graph as one document (`--graph-out`, schema
`swarm.repo-graph/v1`). §2.5 stores it per commit under the tenant's own
prefix as content-addressed blobs and a manifest:

    tenants/<tenant>/repos/<repo_id>/graph/<commit_sha>/manifest.json
    tenants/<tenant>/repos/<repo_id>/graph/blobs/<sha256>.jsonl.gz

What each group holds, and why it matters:

* identical input gives identical blobs and an identical manifest digest,
  because the digest is what promotion records and every later read checks:
  a rerun that came out different would read as a rewritten graph;
* edges are sharded twice, by the callee's module ("who calls X" reads one
  shard) and by the caller's;
* an incremental commit writes only the shards whose content changed;
* the 256 MiB ceiling drops low-confidence symbol edges first and says so;
* the sweep never removes a blob a manifest names, removes an old orphan,
  keeps a young one (a writer may be between its blobs and its manifest),
  and deletes nothing at all when it cannot read every manifest.

Offline: the store is a directory; the GCS store is driven through a fake
`gcloud` runner, so no credential and no network is needed.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-base" / "repo-index"
WRITER = TOOL_DIR / "repo_graph_shards.py"
EXTRACTOR = TOOL_DIR / "repo_index_extract.py"
DOCKERFILE = REPO_ROOT / "images" / "agent-runtime-base" / "Dockerfile"

TENANT = "eng"
REPO_ID = "gh-saga-xyz-widgets"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"nothing at {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shards() -> Any:
    return _load("repo_graph_shards", WRITER)


def _sha(word: str) -> str:
    return hashlib.sha1(word.encode("utf-8")).hexdigest()


def _symbol(path: str, name: str, line: int = 1) -> dict:
    return {"id": f"{path}#{name}", "kind": "function", "path": path, "start_line": line,
            "end_line": line + 2, "language": "python", "exported": True}


def _edge(frm: str, to: str, confidence: float = 0.6, kind: str = "call",
          evidence: str = "ast") -> dict:
    return {"from": frm, "to": to, "kind": kind, "evidence": evidence,
            "confidence": confidence, "also_evidence": [], "path": frm.split("#")[0],
            "line": 2, "sites": 1}


def graph_doc(commit: str = "one", *, extra_api_symbol: bool = False) -> dict:
    """A small `swarm.repo-graph/v1` document over three modules."""
    symbols = [
        _symbol("src/api/users.py", "get_user", 10),
        _symbol("src/api/users.py", "load_user", 20),
        _symbol("src/store/db.py", "fetch", 1),
        _symbol("tests/api/test_users.py", "test_get_user", 3),
    ]
    if extra_api_symbol:
        symbols.append(_symbol("src/api/users.py", "delete_user", 40))
    edges = [
        _edge("src/api/users.py#get_user", "src/api/users.py#load_user", 0.6),
        _edge("src/api/users.py#load_user", "src/store/db.py#fetch", 0.6),
        _edge("tests/api/test_users.py#test_get_user", "src/api/users.py#get_user", 0.6),
        _edge("src/api/users.py#load_user", "src/store/db.py#fetch_many", 0.3),
        _edge("src/api/users.py", "src/store/db.py", 0.4, kind="import", evidence="import"),
    ]
    tests = [
        {"symbol": "src/api/users.py#get_user", "test": "tests/api/test_users.py#test_get_user",
         "depth": 1, "confidence": 0.6},
        {"symbol": "src/store/db.py#fetch", "test": "tests/api/test_users.py#test_get_user",
         "depth": 3, "confidence": 0.216},
    ]
    files = [
        {"path": "src/api/users.py", "language": "python", "lines": 50, "bytes": 900,
         "status": "parsed", "reason": None, "test": False},
        {"path": "src/store/db.py", "language": "python", "lines": 10, "bytes": 200,
         "status": "parsed", "reason": None, "test": False},
        {"path": "tests/api/test_users.py", "language": "python", "lines": 8, "bytes": 150,
         "status": "parsed", "reason": None, "test": True},
    ]
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": _sha(commit),
        "branch": "main", "base_sha": None,
        "languages": [{"language": "python", "files": 3}],
        "truncated": [], "extractor": {"name": "swarm-repo-index", "version": "1"},
        "symbols": symbols, "call_edges": edges, "symbol_test_map": tests, "files": files,
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def _write(shards: Any, root: Path, document: dict, **kwargs: Any) -> dict:
    store = shards.LocalStore(root)
    return shards.write_graph(document, store, tenant_id=TENANT, repo_id=REPO_ID, **kwargs)


def _read_shard(shards: Any, root: Path, manifest: dict, kind: str, module: str) -> list[dict]:
    entry = manifest["shards"][kind][module]
    digest = entry["blob"].split(":", 1)[1]
    raw = (root / shards.graph_root(TENANT, REPO_ID) / "blobs" / f"{digest}{shards.BLOB_SUFFIX}"
           ).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    return [json.loads(line) for line in gzip.decompress(raw).decode().splitlines()]


# --------------------------------------------------------------------------
# determinism: identical input -> identical blobs and digest
# --------------------------------------------------------------------------

def test_identical_input_gives_identical_blobs_and_digest(shards, tmp_path):
    first = _write(shards, tmp_path / "a", graph_doc())
    # The same graph, every object's keys in another order.
    reordered = graph_doc()
    for key in ("symbols", "call_edges", "symbol_test_map", "files"):
        reordered[key] = [dict(reversed(list(row.items()))) for row in reordered[key]]
    second = _write(shards, tmp_path / "b", dict(reversed(list(reordered.items()))))
    assert first["manifest_digest"] == second["manifest_digest"]
    assert first["manifest_digest"].startswith("sha256:")
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
    manifest_bytes = (tmp_path / "a" / first["manifest"]).read_bytes()
    assert first["manifest_digest"] == "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


def test_the_order_of_the_graphs_lists_does_not_change_a_blob(shards, tmp_path):
    first = _write(shards, tmp_path / "a", graph_doc())
    shuffled = graph_doc()
    for key in ("symbols", "call_edges", "symbol_test_map", "files"):
        shuffled[key] = list(reversed(shuffled[key]))
    second = _write(shards, tmp_path / "b", shuffled)
    m1 = json.loads((tmp_path / "a" / first["manifest"]).read_text())
    m2 = json.loads((tmp_path / "b" / second["manifest"]).read_text())
    assert m1["shards"] == m2["shards"]
    assert _blob_names(shards, tmp_path / "a") == _blob_names(shards, tmp_path / "b")


def test_every_blob_is_named_by_the_sha256_of_its_bytes(shards, tmp_path):
    report = _write(shards, tmp_path, graph_doc())
    blobs = sorted((tmp_path / shards.graph_root(TENANT, REPO_ID) / "blobs").iterdir())
    assert blobs and len(blobs) == report["blobs_written"]
    for blob in blobs:
        digest = blob.name[: -len(shards.BLOB_SUFFIX)]
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == digest


def test_a_different_graph_gives_a_different_digest(shards, tmp_path):
    one = _write(shards, tmp_path, graph_doc())
    other = graph_doc()
    other["call_edges"][0]["confidence"] = 0.95
    two = _write(shards, tmp_path / "other", other)
    assert one["manifest_digest"] != two["manifest_digest"]


# --------------------------------------------------------------------------
# the layout §2.5 names
# --------------------------------------------------------------------------

def test_the_manifest_lives_at_the_commit_under_the_tenant_prefix(shards, tmp_path):
    report = _write(shards, tmp_path, graph_doc())
    assert report["manifest"] == (
        f"tenants/{TENANT}/repos/{REPO_ID}/graph/{_sha('one')}/manifest.json"
    )
    manifest = json.loads((tmp_path / report["manifest"]).read_text())
    assert manifest["schema"] == shards.MANIFEST_SCHEMA
    assert manifest["tenant_id"] == TENANT and manifest["repo_id"] == REPO_ID
    assert manifest["commit_sha"] == _sha("one")
    assert manifest["counts"] == {"symbols": 4, "call_edges": 5, "symbol_test_map": 2,
                                  "files": 3}
    assert manifest["languages"] == [{"language": "python", "files": 3}]
    assert manifest["truncated"] == []


def test_edges_are_sharded_by_the_callee_module_and_by_the_caller_module(shards, tmp_path):
    report = _write(shards, tmp_path, graph_doc())
    manifest = json.loads((tmp_path / report["manifest"]).read_text())
    callers_of_store = _read_shard(shards, tmp_path, manifest, "callers", "src/store")
    assert {(e["from"], e["to"]) for e in callers_of_store} == {
        ("src/api/users.py#load_user", "src/store/db.py#fetch"),
        ("src/api/users.py#load_user", "src/store/db.py#fetch_many"),
        ("src/api/users.py", "src/store/db.py"),
    }
    callees_of_api = _read_shard(shards, tmp_path, manifest, "callees", "src/api")
    assert {e["to"] for e in callees_of_api} == {
        "src/api/users.py#load_user", "src/store/db.py#fetch", "src/store/db.py#fetch_many",
        "src/store/db.py",
    }
    symbols = _read_shard(shards, tmp_path, manifest, "symbols", "src/api")
    assert [s["id"] for s in symbols] == ["src/api/users.py#get_user",
                                          "src/api/users.py#load_user"]
    tests = _read_shard(shards, tmp_path, manifest, "tests", "src/store")
    assert tests == [{"symbol": "src/store/db.py#fetch",
                      "test": "tests/api/test_users.py#test_get_user", "depth": 3,
                      "confidence": 0.216}]
    assert set(manifest["shards"]["files"]) == {"src/api", "src/store", "tests/api"}


def test_a_file_at_the_root_is_in_the_dot_module(shards):
    assert shards.module_of("setup.py#main") == "."
    assert shards.module_of("Makefile") == "."
    assert shards.module_of("a/b/c.py#X.y") == "a/b"


def test_an_unsafe_tenant_or_repo_id_is_refused(shards, tmp_path):
    for tenant, repo in (("../other", REPO_ID), (TENANT, "a/b"), ("", REPO_ID)):
        with pytest.raises(ValueError):
            _write_as(shards, tmp_path, tenant, repo)


def _write_as(shards, root, tenant, repo):
    return shards.write_graph(graph_doc(), shards.LocalStore(root), tenant_id=tenant,
                              repo_id=repo)


def test_a_document_that_is_not_a_graph_is_refused(shards, tmp_path):
    document = graph_doc()
    document["schema"] = "swarm.repo-index/v1"
    with pytest.raises(ValueError, match="swarm.repo-graph/v1"):
        _write(shards, tmp_path, document)
    document = graph_doc()
    document["commit_sha"] = "main"
    with pytest.raises(ValueError, match="commit_sha"):
        _write(shards, tmp_path, document)


# --------------------------------------------------------------------------
# incremental: only what changed is written
# --------------------------------------------------------------------------

def test_an_incremental_commit_writes_only_the_changed_shards(shards, tmp_path):
    first = _write(shards, tmp_path, graph_doc("one"))
    second_doc = graph_doc("two", extra_api_symbol=True)
    second = _write(shards, tmp_path, second_doc)
    assert second["blobs_written"] == 1, second
    assert second["blobs_reused"] == first["blobs_written"] + first["blobs_reused"] - 1
    m1 = json.loads((tmp_path / first["manifest"]).read_text())
    m2 = json.loads((tmp_path / second["manifest"]).read_text())
    assert m1["shards"]["symbols"]["src/api"] != m2["shards"]["symbols"]["src/api"]
    assert m1["shards"]["callers"] == m2["shards"]["callers"]
    # Rewriting the same commit writes nothing new.
    again = _write(shards, tmp_path, second_doc)
    assert again["blobs_written"] == 0 and again["manifest_digest"] == second["manifest_digest"]


# --------------------------------------------------------------------------
# the 256 MiB ceiling
# --------------------------------------------------------------------------

def test_over_the_ceiling_low_confidence_symbol_edges_go_first_and_it_says_so(shards, tmp_path):
    full = _write(shards, tmp_path / "full", graph_doc())
    ceiling = full["stored_bytes"] - 1
    cut = _write(shards, tmp_path / "cut", graph_doc(), max_commit_bytes=ceiling)
    manifest = json.loads((tmp_path / "cut" / cut["manifest"]).read_text())
    assert "call_edges:below_0.4" in manifest["truncated"]
    kept = [e for kind in ("callers",) for module in manifest["shards"][kind]
            for e in _read_shard(shards, tmp_path / "cut", manifest, kind, module)]
    assert all(e["confidence"] >= 0.4 or e["kind"] == "import" for e in kept)
    assert ("src/api/users.py", "src/store/db.py") in {(e["from"], e["to"]) for e in kept}
    assert cut["stored_bytes"] <= ceiling


def test_far_over_the_ceiling_only_module_level_edges_are_kept(shards, tmp_path):
    modules_only = graph_doc()
    modules_only["call_edges"] = [e for e in modules_only["call_edges"] if e["kind"] == "import"]
    ceiling = _write(shards, tmp_path / "floor", modules_only)["stored_bytes"]
    cut = _write(shards, tmp_path / "cut", graph_doc(), max_commit_bytes=ceiling)
    manifest = json.loads((tmp_path / "cut" / cut["manifest"]).read_text())
    assert "call_edges:below_0.4" in manifest["truncated"]
    assert "call_edges:symbol" in manifest["truncated"]
    kept = [e for module in manifest["shards"]["callers"]
            for e in _read_shard(shards, tmp_path / "cut", manifest, "callers", module)]
    assert kept and all(e["kind"] == "import" for e in kept)
    assert manifest["counts"]["call_edges"] == len(kept)


def test_a_graph_still_over_the_ceiling_is_refused_and_nothing_is_written(shards, tmp_path):
    with pytest.raises(ValueError, match="ceiling"):
        _write(shards, tmp_path, graph_doc(), max_commit_bytes=1)
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

def _blob_names(shards, root: Path) -> set[str]:
    blobs = root / shards.graph_root(TENANT, REPO_ID) / "blobs"
    return {p.name for p in blobs.iterdir()} if blobs.is_dir() else set()


def _age(path: Path, days: float) -> None:
    moment = time.time() - days * 86_400
    os.utime(path, (moment, moment))


def _referenced(shards, root: Path, report: dict) -> set[str]:
    manifest = json.loads((root / report["manifest"]).read_text())
    return {entry["blob"].split(":", 1)[1] + shards.BLOB_SUFFIX
            for kind in manifest["shards"].values() for entry in kind.values()}


def test_the_sweep_keeps_referenced_blobs_and_removes_orphans(shards, tmp_path):
    report = _write(shards, tmp_path, graph_doc())
    blobs = tmp_path / shards.graph_root(TENANT, REPO_ID) / "blobs"
    referenced = _referenced(shards, tmp_path, report)
    orphan = blobs / (hashlib.sha256(b"orphan").hexdigest() + shards.BLOB_SUFFIX)
    orphan.write_bytes(b"orphan")
    for blob in blobs.iterdir():
        _age(blob, 3)
    now = datetime.now(timezone.utc)
    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID, now=now)
    assert result["deleted"] == 1 and result["kept_referenced"] == len(referenced)
    assert _blob_names(shards, tmp_path) == referenced
    assert result["refused"] is None


def test_the_sweep_keeps_a_blob_any_manifest_references(shards, tmp_path):
    one = _write(shards, tmp_path, graph_doc("one"))
    two = _write(shards, tmp_path, graph_doc("two", extra_api_symbol=True))
    for blob in (tmp_path / shards.graph_root(TENANT, REPO_ID) / "blobs").iterdir():
        _age(blob, 30)
    shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                 now=datetime.now(timezone.utc))
    kept = _blob_names(shards, tmp_path)
    assert _referenced(shards, tmp_path, one) <= kept
    assert _referenced(shards, tmp_path, two) <= kept


def test_the_sweep_keeps_a_young_orphan(shards, tmp_path):
    _write(shards, tmp_path, graph_doc())
    blobs = tmp_path / shards.graph_root(TENANT, REPO_ID) / "blobs"
    young = blobs / (hashlib.sha256(b"in flight").hexdigest() + shards.BLOB_SUFFIX)
    young.write_bytes(b"in flight")
    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          now=datetime.now(timezone.utc))
    assert young.exists()
    assert result["deleted"] == 0 and result["kept_young"] == 1


def test_the_sweep_deletes_nothing_when_a_manifest_cannot_be_read(shards, tmp_path):
    _write(shards, tmp_path, graph_doc("one"))
    broken = tmp_path / shards.graph_root(TENANT, REPO_ID) / _sha("two") / "manifest.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json")
    blobs = tmp_path / shards.graph_root(TENANT, REPO_ID) / "blobs"
    orphan = blobs / (hashlib.sha256(b"orphan").hexdigest() + shards.BLOB_SUFFIX)
    orphan.write_bytes(b"orphan")
    for blob in blobs.iterdir():
        _age(blob, 30)
    before = _blob_names(shards, tmp_path)
    result = shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                          now=datetime.now(timezone.utc))
    assert result["deleted"] == 0 and result["refused"]
    assert _sha("two") in result["refused"]
    assert _blob_names(shards, tmp_path) == before


def test_the_sweep_leaves_alone_what_it_did_not_write(shards, tmp_path):
    _write(shards, tmp_path, graph_doc())
    blobs = tmp_path / shards.graph_root(TENANT, REPO_ID) / "blobs"
    stranger = blobs / "README.txt"
    stranger.write_text("not a blob")
    _age(stranger, 30)
    shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                 now=datetime.now(timezone.utc))
    assert stranger.exists()


def test_the_sweep_stays_inside_one_registration(shards, tmp_path):
    _write(shards, tmp_path, graph_doc())
    other = tmp_path / shards.graph_root(TENANT, "gh-saga-xyz-other") / "blobs"
    other.mkdir(parents=True)
    foreign = other / (hashlib.sha256(b"x").hexdigest() + shards.BLOB_SUFFIX)
    foreign.write_bytes(b"x")
    _age(foreign, 30)
    shards.sweep(shards.LocalStore(tmp_path), tenant_id=TENANT, repo_id=REPO_ID,
                 now=datetime.now(timezone.utc))
    assert foreign.exists()


# --------------------------------------------------------------------------
# the GCS store, through a fake gcloud
# --------------------------------------------------------------------------

class FakeGcloud:
    """`gcloud storage` over a dict, recording every argv it was given."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.created: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], data: bytes | None = None) -> bytes:
        self.calls.append(list(argv))
        assert argv[:2] == ["gcloud", "storage"], argv
        verb = argv[2]
        if verb == "cp":
            source, target = argv[-2], argv[-1]
            assert source == "-"
            if "--no-clobber" in argv and target in self.objects:
                return b""
            self.objects[target] = data or b""
            self.created[target] = "2026-10-05T09:00:00Z"
            return b""
        if verb == "cat":
            target = argv[-1]
            if target not in self.objects:
                raise FileNotFoundError(target)
            return self.objects[target]
        if verb == "ls":
            prefix = argv[-1].rstrip("*")
            rows = [{"url": k, "creation_time": self.created[k]}
                    for k in sorted(self.objects) if k.startswith(prefix)]
            return json.dumps(rows).encode()
        if verb == "rm":
            self.objects.pop(argv[-1], None)
            return b""
        raise AssertionError(f"unexpected gcloud call {argv}")


def test_the_gcs_store_writes_blobs_without_clobbering_and_the_manifest_last(shards):
    fake = FakeGcloud()
    store = shards.GcsStore("swarm-artifacts-test", run=fake)
    report = shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)
    uploads = [c for c in fake.calls if c[2] == "cp"]
    assert uploads[-1][-1] == f"gs://swarm-artifacts-test/{report['manifest']}"
    assert all("--no-clobber" in c for c in uploads[:-1])
    assert f"gs://swarm-artifacts-test/{report['manifest']}" in fake.objects
    for call in fake.calls:
        assert all(part.startswith(f"gs://swarm-artifacts-test/tenants/{TENANT}/")
                   for part in call if part.startswith("gs://")), call


def test_the_gcs_sweep_reads_creation_times_and_removes_old_orphans(shards):
    fake = FakeGcloud()
    store = shards.GcsStore("swarm-artifacts-test", run=fake)
    shards.write_graph(graph_doc(), store, tenant_id=TENANT, repo_id=REPO_ID)
    root = f"gs://swarm-artifacts-test/{shards.graph_root(TENANT, REPO_ID)}"
    orphan = f"{root}/blobs/{hashlib.sha256(b'o').hexdigest()}{shards.BLOB_SUFFIX}"
    fake.objects[orphan] = b"o"
    fake.created[orphan] = "2026-09-01T00:00:00Z"
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    result = shards.sweep(store, tenant_id=TENANT, repo_id=REPO_ID, now=now)
    assert orphan not in fake.objects
    # The blobs just written are under a day old: kept even if unreferenced.
    assert result["deleted"] == 1


# --------------------------------------------------------------------------
# the CLI, on the extractor's real output
# --------------------------------------------------------------------------

def test_the_cli_shards_the_extractors_graph_and_records_the_digest_in_the_index(
    shards, tmp_path
):
    extractor = _load("repo_index_extract", EXTRACTOR)
    repo = fx.build_repo(tmp_path / "repo", fx.PYTHON_APP)
    index_file, graph_file = tmp_path / "out" / "repo-index.json", tmp_path / "out" / "graph.json"
    assert extractor.main(["--repo", str(repo), "--out", str(index_file),
                           "--graph-out", str(graph_file)]) == 0
    store = tmp_path / "bucket"
    code = shards.main(["write", "--graph", str(graph_file), "--store", str(store),
                        "--tenant", TENANT, "--repo-id", REPO_ID, "--index", str(index_file)])
    assert code == 0
    graph = json.loads(graph_file.read_text())
    manifest_key = (f"tenants/{TENANT}/repos/{REPO_ID}/graph/{graph['commit_sha']}/"
                    "manifest.json")
    manifest_bytes = (store / manifest_key).read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["counts"]["symbols"] == len(graph["symbols"])
    assert manifest["counts"]["call_edges"] == len(graph["call_edges"])
    assert manifest["graph_digest"] == "sha256:" + hashlib.sha256(
        graph_file.read_bytes()).hexdigest()
    index = json.loads(index_file.read_text())
    assert index["graph"]["manifest_digest"] == (
        "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    )


def test_the_image_ships_the_shard_writer():
    text = DOCKERFILE.read_text()
    assert "repo-index/repo_graph_shards.py" in text
    assert "/usr/local/bin/swarm-repo-graph" in text
    assert "swarm-repo-graph --self-test" in text


def test_the_self_test_passes(shards, capsys):
    assert shards.main(["--self-test"]) == 0
    assert "repo-graph self-test: ok" in capsys.readouterr().out


def test_the_writer_never_mutates_its_input(shards, tmp_path):
    document = graph_doc()
    before = copy.deepcopy(document)
    full = _write(shards, tmp_path / "full", graph_doc())
    _write(shards, tmp_path, document, max_commit_bytes=full["stored_bytes"] - 1)
    assert document == before
