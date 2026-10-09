"""Signature fingerprints: extractor version 3 (lane KG2).

knowledge-graph.md §3 option C: "a hash of parameters and return type per
symbol, so a diff knows when callers must change". The reviewer's question
after a change is which callers may now be broken; a body edit breaks none,
a new required parameter can break all of them. What is held here:

* every callable (Python, TypeScript, JavaScript, Go) gets its normalised
  signature and a fingerprint of it; a test, a class or a Terraform block
  gets none;
* a reformat or a body edit keeps the fingerprint; a parameter added,
  renamed, retyped or re-defaulted, or a new return type, changes it;
* an incremental run lists the callables whose fingerprint moved since its
  base (`signature_changes`), and a full run lists none;
* fingerprints and changes round-trip through the shard writer.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
EXTRACTOR = TOOL_DIR / "repo_index_extract.py"
WRITER = TOOL_DIR / "repo_graph_shards.py"

TENANT = "eng"
REPO_ID = "repo_0123456789abcdef"
DAY = 86_400


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"nothing at {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> Any:
    return _load("repo_index_extract", EXTRACTOR)


@pytest.fixture(scope="module")
def shards() -> Any:
    return _load("repo_graph_shards", WRITER)


@pytest.fixture(autouse=True)
def _no_ambient_configuration(shards, tmp_path, monkeypatch):
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", tmp_path / "no-such-environ")
    for name in ("TENANT_ID", "ARTIFACT_BUCKET"):
        monkeypatch.delenv(name, raising=False)


FILES = {
    "app/store.py": (
        "class Store:\n"
        "    def get(self, key: str, default=None) -> bytes:\n"
        "        return b''\n\n\n"
        "def open_store(path: str) -> Store:\n"
        "    return Store()\n"
    ),
    "web/api.ts": (
        "export function fetchRun<T>(id: string, retries = 3): Promise<T> {\n"
        "  return fetch(id) as any;\n"
        "}\n"
        "export const label = (run: { id: string }) => run.id;\n"
        "export class Client {\n"
        "  send(body: string): void {}\n"
        "}\n"
    ),
    "web/plain.js": "function add(a, b) {\n  return a + b;\n}\n",
    "svc/main.go": (
        "package main\n\n"
        "func Serve(addr string, port int) (int, error) {\n\treturn 0, nil\n}\n\n"
        "func main() {\n\tServe(\"x\", 1)\n}\n"
    ),
    "infra/main.tf": 'resource "google_storage_bucket" "b" {\n  name = "b"\n}\n',
    "tests/test_store.py": "def test_get():\n    assert True\n",
}


def _signatures(facts: dict) -> dict[str, dict]:
    return {row["symbol"]: row for row in facts["signatures"]}


def _extract(tool: Any, root: Path, files: dict[str, str]) -> dict:
    return tool.extract(fx.build_repo(root, files), tool.Budget())


def test_every_callable_has_its_signature_and_a_fingerprint(tool, tmp_path):
    rows = _signatures(_extract(tool, tmp_path / "repo", FILES))
    assert rows["app/store.py#Store.get"]["signature"] == \
        "(self, key: str, default=None) -> bytes"
    assert rows["app/store.py#open_store"]["signature"] == "(path: str) -> Store"
    assert rows["web/api.ts#fetchRun"]["signature"] == \
        "<T> (id: string, retries = 3) : Promise<T>"
    assert rows["web/api.ts#label"]["signature"] == "(run: {id: string})"
    assert rows["web/api.ts#Client.send"]["signature"] == "(body: string) : void"
    assert rows["web/plain.js#add"]["signature"] == "(a, b)"
    assert rows["svc/main.go#Serve"]["signature"] == "(addr string, port int) -> (int, error)"
    for row in rows.values():
        assert row["fingerprint"].startswith("sha256:")
        assert len(row["fingerprint"]) == len("sha256:") + tool.FINGERPRINT_HEX
    # Tests, classes and Terraform blocks are not callables with a signature.
    assert "tests/test_store.py#test_get" not in rows
    assert "app/store.py#Store" not in rows
    assert not any(symbol.startswith("infra/") for symbol in rows)


@pytest.mark.parametrize("after, moved", [
    # Reformatted and a body edit: no caller can break.
    ("def open_store(\n    path: str,\n) -> Store:\n    x = 1\n    return Store()\n", False),
    ("def open_store(path: str) -> Store:\n    return None\n", False),
    # Every one of these can break a caller.
    ("def open_store(path: str, mode: str) -> Store:\n    return Store()\n", True),
    ("def open_store(location: str) -> Store:\n    return Store()\n", True),
    ("def open_store(path: bytes) -> Store:\n    return Store()\n", True),
    ("def open_store(path: str = '.') -> Store:\n    return Store()\n", True),
    ("def open_store(path: str) -> None:\n    return Store()\n", True),
])
def test_a_fingerprint_moves_exactly_when_the_signature_does(tool, tmp_path, after, moved):
    before = _signatures(_extract(tool, tmp_path / "a", FILES))
    changed = {**FILES, "app/store.py": FILES["app/store.py"].split("def open_store")[0] + after}
    now = _signatures(_extract(tool, tmp_path / "b", changed))
    symbol = "app/store.py#open_store"
    assert (before[symbol]["fingerprint"] != now[symbol]["fingerprint"]) is moved
    assert before["app/store.py#Store.get"] == now["app/store.py#Store.get"]


def test_signature_changes_names_changed_removed_and_added(shards):
    before = [{"symbol": "a#f", "signature": "(x)", "fingerprint": "sha256:1"},
              {"symbol": "a#g", "signature": "()", "fingerprint": "sha256:2"},
              {"symbol": "a#h", "signature": "()", "fingerprint": "sha256:3"}]
    after = [{"symbol": "a#f", "signature": "(x, y)", "fingerprint": "sha256:4"},
             {"symbol": "a#h", "signature": "()", "fingerprint": "sha256:3"},
             {"symbol": "a#k", "signature": "(z)", "fingerprint": "sha256:5"}]
    assert shards.signature_changes(before, after) == [
        {"symbol": "a#f", "change": "changed", "before": "(x)", "after": "(x, y)"},
        {"symbol": "a#g", "change": "removed", "before": "()", "after": None},
        {"symbol": "a#k", "change": "added", "before": None, "after": "(z)"},
    ]


def test_an_incremental_run_lists_the_signatures_that_moved_and_stores_them(tool, shards,
                                                                            tmp_path):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    facts = tool.extract(repo, tool.Budget())
    assert facts["signature_changes"] == []
    store = shards.LocalStore(str(tmp_path / "store"))
    written = shards.write_graph(tool.graph_document(facts), store, tenant_id=TENANT,
                                 repo_id=REPO_ID)
    graph = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                              commit_sha=facts["commit_sha"],
                              manifest_digest=written["manifest_digest"])
    assert graph["signatures"] == facts["signatures"]
    base = tool.Base(sha=facts["commit_sha"], graph=graph, index=tool.index_document(facts))

    env = fx.git_env(repo.parent / (repo.name + "-home"))
    fx.write_files(repo, {"app/store.py": FILES["app/store.py"].replace(
        "def open_store(path: str)", "def open_store(path: str, *, create: bool)")})
    stamp = f"@{1_780_000_000 + DAY} +0000"
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "change"],
                   env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
                   check=True)
    after = tool.extract(repo, tool.Budget(), base=base)
    assert after["kind"] == "incremental"
    assert after["signature_changes"] == [{
        "symbol": "app/store.py#open_store", "change": "changed",
        "before": "(path: str) -> Store", "after": "(path: str, *, create: bool) -> Store"}]
    assert tool.index_document(after)["graph"]["signature_changes"] == 1

    written = shards.write_graph(tool.graph_document(after), store, tenant_id=TENANT,
                                 repo_id=REPO_ID)
    back = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                             commit_sha=after["commit_sha"])
    assert back["signature_changes"] == after["signature_changes"]
    manifest = shards.read_manifest(store, tenant_id=TENANT, repo_id=REPO_ID,
                                    commit_sha=after["commit_sha"])
    assert set(manifest["index_shards"]["signature_changes"]) == {"app"}
