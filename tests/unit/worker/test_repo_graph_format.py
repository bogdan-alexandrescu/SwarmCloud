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
