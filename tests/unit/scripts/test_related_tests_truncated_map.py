"""A truncated test map never narrows what related-tests or the pre-publish gate select.

The owner's agent quality program (the owner, 2026-11-03): a graph promoted with
its test maps cut (`truncated: symbol_test_map, file_test_map`) is carried
since the incremental-carry fix, so a staged snapshot may name only part of
a file's covering tests. scripts/lib/related_tests.py ADDS the graph's
tests_for to the import map's pick (owner pick on PR 3099), and the worker's
pre-publish gate runs the selection related-tests.sh prints
(agent_worker/prepublish.py, RELATED_SCRIPT), so the cut can only fail to
add a test, never remove one the imports reach. Held here with a graph the
real extractor and shard writer cut, staged where the stager puts it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SELECTOR = REPO / "scripts" / "lib" / "related_tests.py"
TOOL_DIR = REPO / "images" / "agent-runtime-indexer" / "repo-index"
sys.path.insert(0, str(REPO / "tests" / "unit" / "worker"))

import repo_index_fixtures as fx  # noqa: E402

TENANT, REPO_ID = "eng", "repo_00112233445566ff"

#: core.py is imported by four tests and by edge.py, which one more imports.
TREE = {
    "apps/foo/foo/__init__.py": "",
    "apps/foo/foo/core.py": "def make():\n    return 1\n",
    "apps/foo/foo/edge.py": "from foo.core import make\n\n\ndef use():\n    return make()\n",
    **{f"tests/unit/foo/test_core_{n}.py":
       f"from foo.core import make\n\n\ndef test_make_{n}():\n    assert make()\n"
       for n in range(4)},
    "tests/unit/foo/test_edge.py": "from foo.edge import use\n\n\ndef test_use():\n"
                                   "    assert use()\n",
}


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"nothing at {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> Any:
    return _load("repo_index_extract", TOOL_DIR / "repo_index_extract.py")


@pytest.fixture(scope="module")
def shards() -> Any:
    return _load("repo_graph_shards", TOOL_DIR / "repo_graph_shards.py")


def _stage_cut_graph(tool: Any, shards: Any, root: Path, context: Path, monkeypatch) -> dict:
    """Extract `root` with both test maps capped at one row, write the graph
    through the shard writer, and stage it as the stager does: the manifest
    and the blobs it names under <context>/snapshot."""
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", root.parent / "no-such-environ")
    for name in ("TENANT_ID", "ARTIFACT_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tool, "MAX_SYMBOL_TEST_MAP", 1)
    monkeypatch.setattr(tool, "MAX_FILE_TEST_MAP", 1)
    facts = tool.extract(root, tool.Budget())
    store = shards.LocalStore(root.parent / "bucket")
    shards.write_graph(tool.graph_document(facts), store, tenant_id=TENANT, repo_id=REPO_ID)
    graph_root = root.parent / "bucket" / shards.graph_root(TENANT, REPO_ID)
    manifest = json.loads((graph_root / facts["commit_sha"] / "manifest.json").read_text())
    snapshot = context / "snapshot"
    shutil.copytree(graph_root / "blobs", snapshot / "blobs")
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def _select(root: Path, context: Path | None, *changed: str) -> list[str]:
    listing = root.parent / "changed.txt"
    listing.write_text("".join(f"{c}\n" for c in changed))
    env = {"PATH": "/usr/bin:/bin", "SWARM_CONTEXT_DIR": str(context or root.parent / "none")}
    result = subprocess.run(
        [sys.executable, str(SELECTOR), "--root", str(root), "--changed", str(listing)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 0, result.stderr
    picked = [rest for kind, _, rest in (line.partition("\t")
                                         for line in result.stdout.splitlines())
              if kind == "pytest"]
    return sorted(f for run in picked for f in run.split())


def test_a_cut_test_map_widens_to_the_import_pick(tool, shards, tmp_path, monkeypatch):
    root = fx.build_repo(tmp_path / "repo", TREE)
    context = tmp_path / "context"
    manifest = _stage_cut_graph(tool, shards, root, context, monkeypatch)
    assert {"symbol_test_map", "file_test_map"} <= set(manifest["truncated"]), (
        "the fixture's graph is cut in its test maps")
    selector = _load("related_tests_cut", SELECTOR)
    graph = selector.Graph(context / "snapshot")
    named = graph.tests_for("apps/foo/foo/core.py")
    imports = {f"tests/unit/foo/test_core_{n}.py" for n in range(4)} | {
        "tests/unit/foo/test_edge.py"}
    assert 0 < len(named) < len(imports), "the cut graph names some of the tests, not all"

    import_pick = _select(root, None, "apps/foo/foo/core.py")
    with_cut_graph = _select(root, context, "apps/foo/foo/core.py")

    assert set(import_pick) == imports
    # MUTATION: let the staged graph's tests_for replace the import pick, and
    # the cut map drops the tests it no longer names.
    assert set(with_cut_graph) >= set(import_pick)
    assert set(with_cut_graph) == set(import_pick) | {t for t in named if t in imports}
