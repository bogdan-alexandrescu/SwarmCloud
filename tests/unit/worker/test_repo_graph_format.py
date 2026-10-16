"""The shard format's version 3 (lane KG2), and readers that still take 2.

Extractor version 3 adds five layers (communities, terms, signatures,
signature_changes, flows). They are stored under the manifest's own key,
`index_shards`, with `format_version: 3`, and NOT under `shards`: swarm-api's
reader (`swarm_api/repograph.py::parse_manifest`) refuses a manifest whose
`shards` names a layer it does not know, so a new layer there would make every
graph unreadable to the explorer and the impact route until swarm-api moved
too. What is held here:

* a format-3 manifest keeps `shards` to the five format-2 layers, says its
  format, and is accepted by swarm-api's reader as it stands today;
* a format-2 manifest (no `format_version`, no `index_shards`) is still read,
  with the version-3 lists empty, and a version-2 graph document is still
  written;
* the sweep counts an index layer's blobs as referenced (a blob it did not
  count would be deleted under a live manifest);
* an unknown index layer is refused, and an incremental write counts the
  index layers it carried.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
WRITER = TOOL_DIR / "repo_graph_shards.py"

TENANT = "eng"
REPO_ID = "repo_0123456789abcdef"


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


@pytest.fixture(autouse=True)
def _no_ambient_configuration(shards, tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", tmp_path / "no-such-environ")
    for name in ("TENANT_ID", "ARTIFACT_BUCKET"):
        monkeypatch.delenv(name, raising=False)


def _sha(word: str) -> str:
    return hashlib.sha1(word.encode("utf-8")).hexdigest()


def _version_2(commit: str = "one") -> dict:
    """A graph document as extractor version 2 wrote it: no index lists."""
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": _sha(commit),
        "branch": "main", "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "2"},
        "symbols": [{"id": "src/a.py#f", "kind": "function", "path": "src/a.py",
                     "start_line": 1},
                    {"id": "lib/b.py#g", "kind": "function", "path": "lib/b.py",
                     "start_line": 1}],
        "call_edges": [{"from": "src/a.py#f", "to": "lib/b.py#g", "kind": "call",
                        "evidence": "ast", "confidence": 0.6}],
        "symbol_test_map": [],
        "files": [{"path": "src/a.py"}, {"path": "lib/b.py"}],
    }


def _version_3(commit: str = "one", fingerprint: str = "sha256:00") -> dict:
    return {
        **_version_2(commit),
        "extractor": {"name": "swarm-repo-index", "version": "3"},
        "communities": [{"id": "c0001", "label": "src", "files": ["lib/b.py", "src/a.py"],
                         "size": 2, "symbols": 2, "cohesion": 1.0}],
        "terms": [{"term": "alpha", "postings": {"src/a.py": [["f", 3, 3]]}},
                  {"term": "beta", "postings": {"lib/b.py": [["g", 3, 3]]}}],
        "term_stats": {"documents": 2, "average_length": 3.0, "k1": 1.2, "b": 0.75,
                       "tokenizer": "1", "name_weight": 3},
        "signatures": [{"symbol": "lib/b.py#g", "signature": "()", "fingerprint": "sha256:01"},
                       {"symbol": "src/a.py#f", "signature": "(x)", "fingerprint": fingerprint}],
        "signature_changes": [],
        "flows": [{"entry": "src/a.py", "kind": "main", "path": "src/a.py",
                   "steps": [{"symbol": "src/a.py#f", "depth": 1, "via": "src/a.py"}],
                   "files": ["src/a.py"], "truncated": False}],
    }


def _manifest(store: Any, commit: str) -> dict:
    return json.loads(store.get(shards_manifest_key(commit)))


def shards_manifest_key(commit: str) -> str:
    return f"tenants/{TENANT}/repos/{REPO_ID}/graph/{_sha(commit)}/manifest.json"


def test_a_format_3_manifest_keeps_the_five_layers_and_says_its_format(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID)
    manifest = _manifest(store, "one")
    assert shards.FORMAT_VERSION == 3
    assert manifest["format_version"] == 3
    assert set(manifest["shards"]) == set(shards.LAYERS) == {
        "symbols", "callers", "callees", "tests", "files"}
    assert set(manifest["index_shards"]) == set(shards.INDEX_LAYERS)
    assert set(manifest["index_shards"]["communities"]) == {shards.WHOLE}
    assert set(manifest["index_shards"]["signatures"]) == {"lib", "src"}
    assert set(manifest["index_shards"]["flows"]) == {"src"}
    assert manifest["index_shards"]["signature_changes"] == {}
    assert manifest["counts"]["communities"] == 1 and manifest["counts"]["terms"] == 2
    assert manifest["term_stats"]["documents"] == 2
    assert manifest["writer"] == {"name": "swarm-repo-graph", "version": shards.TOOL_VERSION}


def test_swarm_apis_reader_accepts_a_format_3_manifest_as_it_stands(shards, tmp_path):
    """The reason the new layers are not under `shards`."""
    from swarm_api import repograph

    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID)
    raw = store.get(shards_manifest_key("one"))
    parsed = repograph.parse_manifest(raw, tenant_id=TENANT, repo_id=REPO_ID,
                                      commit_sha=_sha("one"))
    assert set(parsed["shards"]) == set(repograph.LAYERS)
    # Had the layers gone under `shards`, the same reader would refuse it.
    broken = json.loads(raw)
    broken["shards"]["terms"] = broken["index_shards"]["terms"]
    with pytest.raises(repograph.InvalidGraph):
        repograph.parse_manifest(json.dumps(broken).encode(), tenant_id=TENANT,
                                 repo_id=REPO_ID, commit_sha=_sha("one"))


def test_everything_round_trips(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    document = _version_3()
    shards.write_graph(document, store, tenant_id=TENANT, repo_id=REPO_ID)
    back = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=_sha("one"))
    for key in ("communities", "terms", "term_stats", "signatures", "signature_changes",
                "flows"):
        assert back[key] == document[key], key


def test_a_version_2_graph_is_still_written_and_read(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_2(), store, tenant_id=TENANT, repo_id=REPO_ID)
    manifest = _manifest(store, "one")
    assert manifest["format_version"] == 3
    assert all(layer == {} for layer in manifest["index_shards"].values())
    back = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=_sha("one"))
    assert back["communities"] == [] and back["terms"] == [] and back["flows"] == []


def test_a_format_2_manifest_is_read_with_the_version_3_lists_empty(shards, tmp_path):
    """What every graph promoted before this lane is."""
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_2(), store, tenant_id=TENANT, repo_id=REPO_ID)
    manifest = _manifest(store, "one")
    for key in ("format_version", "index_shards", "term_stats"):
        del manifest[key]
    store.put(shards_manifest_key("one"), shards.canonical(manifest) + b"\n",
              no_clobber=False)
    assert shards.manifest_format(manifest) == 2
    back = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=_sha("one"))
    assert [s["id"] for s in back["symbols"]] == ["lib/b.py#g", "src/a.py#f"]
    for key in ("communities", "terms", "signatures", "signature_changes", "flows"):
        assert back[key] == [], key
    assert "term_stats" not in back
    assert shards.referenced_blobs(manifest)


def test_the_sweep_keeps_every_index_layer_blob(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID)
    manifest = _manifest(store, "one")
    index_blobs = {entry["blob"].split(":", 1)[1]
                   for layer in manifest["index_shards"].values() for entry in layer.values()}
    assert index_blobs and index_blobs <= shards.referenced_blobs(manifest)
    swept = shards.sweep(store, tenant_id=TENANT, repo_id=REPO_ID, grace=timedelta(0))
    assert swept["deleted"] == 0, swept
    # Still readable after the sweep: nothing it relies on was removed.
    back = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=_sha("one"))
    assert back["flows"] == _version_3()["flows"]


def test_an_unknown_index_layer_is_refused(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID)
    manifest = _manifest(store, "one")
    manifest["index_shards"]["embeddings"] = {}
    with pytest.raises(ValueError, match="unknown index layer"):
        shards.referenced_blobs(manifest)


def test_an_incremental_write_counts_the_index_shards_it_carried(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3("one"), store, tenant_id=TENANT, repo_id=REPO_ID)
    report = shards.write_graph(_version_3("two", fingerprint="sha256:ff"), store,
                                tenant_id=TENANT, repo_id=REPO_ID, base_commit=_sha("one"))
    first, second = _manifest(store, "one"), _manifest(store, "two")
    changed = sum(1 for key in ("shards", "index_shards")
                  for layer, modules in second[key].items()
                  for module, entry in modules.items()
                  if first[key][layer].get(module, {}).get("blob") != entry["blob"])
    total = sum(len(modules) for key in ("shards", "index_shards")
                for modules in second[key].values())
    # Only src's signature shard holds the moved fingerprint: an index shard,
    # counted apart from the five format-2 layers, which all carried.
    assert changed == 1
    assert report["index_shards_rewritten"] == 1
    assert report["index_shards_carried"] == sum(
        len(modules) for modules in second["index_shards"].values()) - 1
    assert report["shards_rewritten"] == 0
    assert report["shards_carried"] + report["index_shards_carried"] == total - 1


# --------------------------------------------------------------------------
# the repo-index document as the `document` layer (lane GS-INDEXDOC,
# docs/design/graph-service.md §2.3, owner decision Q7)
# --------------------------------------------------------------------------

def _index(commit: str = "one") -> dict:
    """A `swarm.repo-index/v1` document, its lists deliberately out of sorted order:
    the extractor's order means something (hot spots by change count, modules by
    size), so the layer keeps it."""
    return {
        "schema": "swarm.repo-index/v1", "commit_sha": _sha(commit), "branch": "main",
        "built_at": "2026-10-16T00:00:00Z", "kind": "full", "base_sha": None,
        "extractor": {"name": "swarm-repo-index", "version": "3"},
        "modules": [{"path": "src", "language": "python", "purpose": "the app",
                     "files": 1, "lines": 90},
                    {"path": "lib", "language": "python", "purpose": "helpers",
                     "files": 1, "lines": 10}],
        "entry_points": [{"path": "src/a.py", "kind": "cli", "how": "python -m src.a"}],
        "routes": [],
        "test_layout": [{"root": "tests", "framework": "pytest", "run": "pytest tests"}],
        "test_map": [{"source": "src/**", "test": "tests/test_a.py", "evidence": "naming"}],
        "always_tests": [],
        "territory": [{"path": "lib/", "rule": "frozen", "source": "CLAUDE.md"}],
        "commands": [{"name": "test", "command": "pytest", "source": "Makefile"}],
        "hot_spots": [{"path": "src/a.py", "changes": 9}, {"path": "lib/b.py", "changes": 2}],
        "notes": [{"text": "lib/ is frozen", "source": "CLAUDE.md"}],
        "languages": [],
        "graph": {"symbols": 2},
        "truncated": ["test_map"],
    }


def _document_blob_keys(manifest: dict) -> set[str]:
    return {f"tenants/{TENANT}/repos/{REPO_ID}/graph/blobs/"
            + entry["blob"].split(":", 1)[1] + ".jsonl.gz"
            for layer in manifest["document_shards"].values() for entry in layer.values()}


def test_the_repo_index_document_is_one_document_layer_shard_per_section(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=_index())
    manifest = _manifest(store, "one")
    assert shards.DOCUMENT_LAYER == "document"
    assert shards.DOCUMENT_SECTIONS == ("modules", "entry_points", "routes", "test_layout",
                                        "test_map", "territory", "commands", "hot_spots",
                                        "notes")
    assert set(manifest["document_shards"]) == {"document"}
    # An empty section has no shard, as an empty module has none; its count says 0.
    assert set(manifest["document_shards"]["document"]) == set(shards.DOCUMENT_SECTIONS) - {
        "routes"}
    meta = manifest["document"]
    assert meta["available"] is True and meta["reason"] is None
    assert meta["counts"] == {section: len(_index()[section])
                              for section in shards.DOCUMENT_SECTIONS}
    assert meta["truncated"] == ["test_map"]
    # The index layers are exactly the five they were, and every document blob
    # is under this tenant's own prefix (invariant 9).
    assert set(manifest["index_shards"]) == set(shards.INDEX_LAYERS)
    assert _document_blob_keys(manifest) and all(
        (tmp_path / key).is_file() for key in _document_blob_keys(manifest))


def test_the_document_reads_back_section_by_section_in_the_stored_order(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    index = _index()
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=index)
    back = shards.read_document(store, tenant_id=TENANT, repo_id=REPO_ID,
                                commit_sha=_sha("one"))
    assert back == {section: index[section] for section in shards.DOCUMENT_SECTIONS}
    # Not resorted: the biggest module and the hottest file stay first.
    assert [m["path"] for m in back["modules"]] == ["src", "lib"]
    assert shards.read_document(store, tenant_id=TENANT, repo_id=REPO_ID,
                                commit_sha=_sha("one"), section="hot_spots") == {
        "hot_spots": index["hot_spots"]}
    with pytest.raises(ValueError, match="not a section"):
        shards.read_document(store, tenant_id=TENANT, repo_id=REPO_ID,
                             commit_sha=_sha("one"), section="graph")


def test_the_index_graph_block_is_not_stored_so_recording_the_digest_changes_nothing(
    shards, tmp_path
):
    """The writer records the manifest's digest in the index's `graph` after the
    write. Were `graph` stored, the digest would name a manifest that names it."""
    store = shards.LocalStore(str(tmp_path))
    index = _index()
    first = shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID,
                               index=index)
    index["graph"]["manifest_digest"] = first["manifest_digest"]
    index["built_at"] = "2026-10-17T00:00:00Z"
    second = shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID,
                                index=index)
    assert second["manifest_digest"] == first["manifest_digest"]
    assert second["blobs_written"] == 0


def test_swarm_apis_readers_accept_the_manifest_with_its_document_layer(shards, tmp_path):
    """Why the layer is under `document_shards` and not `index_shards`: swarm-api's
    territory reader refuses an `index_shards` layer it does not know, which would
    turn every new graph away from the territory, impact and search routes."""
    from types import SimpleNamespace

    from swarm_api import repograph, territory

    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=_index())
    raw = store.get(shards_manifest_key("one"))
    repograph.parse_manifest(raw, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=_sha("one"))
    manifest = json.loads(raw)
    assert set(territory._index_table(SimpleNamespace(manifest=manifest))) == set(
        territory.INDEX_LAYERS)
    broken = json.loads(raw)
    broken["index_shards"]["document"] = broken["document_shards"]["document"]
    with pytest.raises(territory.InvalidGraph):
        territory._index_table(SimpleNamespace(manifest=broken))


def test_the_sweep_keeps_every_document_blob(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=_index())
    manifest = _manifest(store, "one")
    document_blobs = {entry["blob"].split(":", 1)[1]
                      for entry in manifest["document_shards"]["document"].values()}
    assert document_blobs and document_blobs <= shards.referenced_blobs(manifest)
    swept = shards.sweep(store, tenant_id=TENANT, repo_id=REPO_ID, grace=timedelta(0))
    assert swept["deleted"] == 0, swept
    assert shards.read_document(store, tenant_id=TENANT, repo_id=REPO_ID,
                                commit_sha=_sha("one"))["notes"] == _index()["notes"]


def test_an_unknown_document_layer_is_refused(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=_index())
    manifest = _manifest(store, "one")
    manifest["document_shards"]["prose"] = {}
    with pytest.raises(ValueError, match="unknown document layer"):
        shards.referenced_blobs(manifest)


@pytest.mark.parametrize("index, reason", [
    (None, "no repo-index document"),
    ({**_index("two")}, "another commit"),
    ({**_index(), "schema": "swarm.plan/v1"}, "not a swarm.repo-index/v1"),
    ({**_index(), "notes": "lib/ is frozen"}, "notes is not a list"),
    ({**_index(), "commands": ["make test"]}, "commands holds a row that is not an object"),
])
def test_a_missing_or_wrong_document_is_not_stored_and_the_graph_still_is(
    shards, tmp_path, index, reason
):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=index)
    manifest = _manifest(store, "one")
    assert manifest["document_shards"] == {}
    assert manifest["document"]["available"] is False
    assert reason in manifest["document"]["reason"]
    assert manifest["document"]["counts"] == {}
    assert shards.read_document(store, tenant_id=TENANT, repo_id=REPO_ID,
                                commit_sha=_sha("one")) == {}
    assert shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                             commit_sha=_sha("one"))["flows"] == _version_3()["flows"]


def test_a_document_over_the_index_budget_is_not_stored(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    index = {**_index(), "notes": [{"text": "x" * shards.MAX_DOCUMENT_BYTES}]}
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=index)
    manifest = _manifest(store, "one")
    assert manifest["document_shards"] == {}
    assert "over the" in manifest["document"]["reason"]


def test_a_manifest_written_before_the_document_layer_reads_as_no_document(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3(), store, tenant_id=TENANT, repo_id=REPO_ID, index=_index())
    manifest = _manifest(store, "one")
    for key in ("document", "document_shards"):
        del manifest[key]
    store.put(shards_manifest_key("one"), shards.canonical(manifest) + b"\n", no_clobber=False)
    assert shards.read_document(store, tenant_id=TENANT, repo_id=REPO_ID,
                                commit_sha=_sha("one")) == {}
    assert shards.referenced_blobs(manifest)


def test_an_incremental_write_counts_the_document_shards_it_carried(shards, tmp_path):
    store = shards.LocalStore(str(tmp_path))
    shards.write_graph(_version_3("one"), store, tenant_id=TENANT, repo_id=REPO_ID,
                       index=_index("one"))
    index = _index("two")
    index["hot_spots"] = [{"path": "src/a.py", "changes": 10}]
    report = shards.write_graph(_version_3("two"), store, tenant_id=TENANT, repo_id=REPO_ID,
                                index=index, base_commit=_sha("one"))
    assert report["document_shards_rewritten"] == 1
    assert report["document_shards_carried"] == len(shards.DOCUMENT_SECTIONS) - 2
