"""Incremental index runs: the extractor and the shard writer (docs/repo-index.md §3.4, lane IX2).

Owner decision 2026-10-06: every trigger was a full ~20-minute rebuild; on
2026-10-06 a run for 36ac73bd re-indexed all 1,685 files two commits after
the previous index. An incremental run is given the previous promoted index
of an ancestor (its repo-index.json, and its graph read back from the
shards) and rewrites only what changed. What is held here:

* after a two-file change the run is `incremental`, names exactly those two
  files, comes out equal to a full extraction of the same commit, and the
  writer stores only the changed modules' shards -- every other shard is the
  base's blob;
* untouched modules carry the base's purpose and the commit they were read
  at; touched ones are re-dated to the head;
* deleted files disappear from every list, and their callers are re-resolved;
* a build or test configuration change, too many changes, or a base the run
  cannot compare against makes the run full, saying why;
* the graph read back from the shards is the graph that was written, and a
  rewritten manifest or blob is refused, not read;
* swarm-api applies the same full-run rules before it submits.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
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
    # The machine running the tests may be a worker whose PID 1 names a tenant.
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", tmp_path / "no-such-environ")
    for name in ("TENANT_ID", "ARTIFACT_BUCKET"):
        monkeypatch.delenv(name, raising=False)


FILES: dict[str, str] = {
    **fx.PYTHON_APP,
    **fx.PYTHON_AMBIGUOUS,
    "pyproject.toml": "[project]\nname = \"fixture\"\n",
}


def _env(repo: Path) -> dict[str, str]:
    return fx.git_env(repo.parent / (repo.name + "-home"))


def _commit(repo: Path, changes: dict[str, str | None], when: int) -> str:
    """One commit: a text writes a file, None deletes it. Returns the new head."""
    env = _env(repo)
    for rel, text in changes.items():
        if text is None:
            subprocess.run(["git", "-C", str(repo), "rm", "-q", rel], env=env, check=True)
        else:
            fx.write_files(repo, {rel: text})
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=env, check=True)
    stamp = f"@{when} +0000"
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"],
                   env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
                   check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], env=_env(repo),
                          capture_output=True, text=True, check=True).stdout.strip()


def _base_index(tool: Any, facts: dict) -> dict:
    """The promoted base index: the extractor's fields plus the agent's reading."""
    index = tool.index_document(facts)
    for module in index["modules"]:
        module["purpose"] = f"what {module['path']} does"
    index["territory"] = [
        {"path": "src/pkg/store.py", "rule": "storage: ask first", "source": "CLAUDE.md"},
        {"path": "lib/", "rule": "shared helpers", "source": "CLAUDE.md"},
    ]
    index["notes"] = [{"text": "Never build a client at import time.", "source": "CLAUDE.md"}]
    return index


def _base(tool: Any, shards: Any, store: Any, repo: Path) -> tuple[Any, dict]:
    """Extract the head in full, store its graph, and stage it back as a base."""
    facts = tool.extract(repo, tool.Budget())
    written = shards.write_graph(tool.graph_document(facts), store, tenant_id=TENANT,
                                 repo_id=REPO_ID)
    graph = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                              commit_sha=facts["commit_sha"],
                              manifest_digest=written["manifest_digest"])
    return tool.Base(sha=facts["commit_sha"], graph=graph,
                     index=_base_index(tool, facts)), facts


def _manifest(store: Any, sha: str) -> dict:
    return json.loads(store.get(f"tenants/{TENANT}/repos/{REPO_ID}/graph/{sha}/manifest.json"))


def _mentions(document: dict, path: str) -> list[str]:
    found = []
    for key in ("symbols", "call_edges", "symbol_test_map", "files", "routes", "test_map",
                "hot_spots", "modules"):
        for row in document.get(key) or []:
            if path in json.dumps(row):
                found.append(key)
    return found


# --------------------------------------------------------------------------
# a two-file change
# --------------------------------------------------------------------------

def test_incremental_after_a_two_file_change_rewrites_only_those_entries(
    tool, shards, tmp_path
):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, _ = _base(tool, shards, store, repo)
    head = _commit(repo, {
        "src/pkg/web.py": fx.PYTHON_APP["src/pkg/web.py"] + (
            "\n\n@app.route(\"/ready\")\ndef ready():\n    return ping()\n"),
        "lib/c.py": "def only_here():\n    return 2\n\n\ndef also_here():\n    return 3\n",
    }, 1_780_000_000 + DAY)

    facts = tool.extract(repo, tool.Budget(), base=base)

    assert facts["kind"] == "incremental" and facts["base_sha"] == base.sha
    assert facts["commit_sha"] == head
    assert facts["changes"]["modified"] == ["lib/c.py", "src/pkg/web.py"]
    assert facts["changes"]["added"] == [] and facts["changes"]["deleted"] == []
    assert facts["extractor"]["incremental"] == {"base_sha": base.sha, "ran": True,
                                                 "reason": None}
    # Untouched files' edges are the base's, so the graph is what a full
    # extraction of the same commit gives (no LSP pass here to differ).
    full = tool.extract(repo, tool.Budget())
    for key in ("symbols", "call_edges", "symbol_test_map", "files", "routes", "test_map"):
        assert facts[key] == full[key], key
    assert any(s["id"] == "src/pkg/web.py#ready" for s in facts["symbols"])

    # The writer stores only the changed modules' shards; the rest are the
    # base's blobs, by digest.
    report = shards.write_graph(tool.graph_document(facts), store, tenant_id=TENANT,
                                repo_id=REPO_ID, base_commit=base.sha)
    before, after = _manifest(store, base.sha), _manifest(store, head)
    assert after["kind"] == "incremental" and after["base_sha"] == base.sha
    rewritten = {(layer, module) for layer, modules in after["shards"].items()
                 for module, entry in modules.items()
                 if before["shards"][layer].get(module, {}).get("blob") != entry["blob"]}
    assert rewritten, "the two changed files' shards are new"
    assert {module for _layer, module in rewritten} <= {"lib", "src/pkg"}, rewritten
    untouched = [(layer, module) for layer, modules in after["shards"].items()
                 for module in modules if module not in ("lib", "src/pkg")]
    assert ("symbols", "tests") in untouched and ("files", ".") in untouched
    for layer, module in untouched:
        assert after["shards"][layer][module] == before["shards"][layer][module], (layer, module)
    assert report["shards_carried"] > 0
    assert report["shards_rewritten"] == len(rewritten)
    # Format 3's index layers (lane KG2) are new blobs too when the change
    # touched their terms, signatures or communities; counted apart.
    assert report["blobs_written"] <= len(rewritten) + report["index_shards_rewritten"]

    # The index: untouched modules keep the base's purpose and read-at commit.
    index = tool.index_document(facts)
    modules = {m["path"]: m for m in index["modules"]}
    assert modules["tests"]["commit_sha"] == base.sha
    assert modules["tests"]["purpose"] == "what tests does"
    assert modules["src/pkg"]["commit_sha"] == head and modules["lib"]["commit_sha"] == head
    assert modules["src/pkg"]["purpose"] == "what src/pkg does"
    assert index["kind"] == "incremental" and index["base_sha"] == base.sha
    assert index["changes"]["modified"] == ["lib/c.py", "src/pkg/web.py"]
    assert index["carried"]["notes"] == base.index["notes"]
    assert index["carried"]["territory"] == base.index["territory"]


def test_untouched_files_keep_their_base_edges_verbatim(tool, shards, tmp_path):
    """An LSP edge of an untouched file is carried, not re-asked of a server."""
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, _ = _base(tool, shards, store, repo)
    resolved = next(e for e in base.graph["call_edges"]
                    if e["from"] == "tests/test_users.py#test_load_user")
    resolved.update(evidence="lsp", confidence=0.95, also_evidence=["ast"])
    _commit(repo, {"lib/c.py": "def only_here():\n    return 2\n"}, 1_780_000_000 + DAY)

    facts = tool.extract(repo, tool.Budget(), base=base)

    assert facts["kind"] == "incremental"
    carried = [e for e in facts["call_edges"]
               if (e["from"], e["to"], e["kind"]) == (resolved["from"], resolved["to"],
                                                      resolved["kind"])]
    assert carried == [resolved]
    assert "tests/test_users.py" not in facts["changes"]["affected"]
    assert "lib/use.py" in facts["changes"]["affected"], "it calls into the changed file"


# --------------------------------------------------------------------------
# deleted files
# --------------------------------------------------------------------------

def test_deleted_files_disappear_and_their_callers_are_re_resolved(tool, shards, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, base_facts = _base(tool, shards, store, repo)
    assert _mentions(base_facts, "src/pkg/store.py"), "the fixture names the file first"
    _commit(repo, {"src/pkg/store.py": None}, 1_780_000_000 + DAY)

    facts = tool.extract(repo, tool.Budget(), base=base)

    assert facts["kind"] == "incremental"
    assert facts["changes"]["deleted"] == ["src/pkg/store.py"]
    assert _mentions(facts, "src/pkg/store.py") == []
    # users.py imported and called into it: its edges are re-resolved.
    assert "src/pkg/users.py" in facts["changes"]["affected"]
    full = tool.extract(repo, tool.Budget())
    assert facts["call_edges"] == full["call_edges"]
    # A carried reading row about the deleted file goes with it.
    carried = tool.index_document(facts)["carried"]["territory"]
    assert [row["path"] for row in carried] == ["lib/"]
    graph = tool.graph_document(facts)
    assert _mentions(graph, "src/pkg/store.py") == []


# --------------------------------------------------------------------------
# what makes a run full
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,text", [
    ("pyproject.toml", "[project]\nname = \"fixture\"\nversion = \"2\"\n"),
    ("Makefile", "test:\n\tpytest\n"),
    (".github/workflows/ci.yml", "on: push\n"),
    ("tests/conftest.py", "import pytest\n"),
    ("tsconfig.base.json", "{}\n"),
])
def test_a_build_or_test_configuration_change_forces_a_full_run(
    tool, shards, tmp_path, path, text
):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, _ = _base(tool, shards, store, repo)
    _commit(repo, {path: text, "lib/c.py": "def only_here():\n    return 2\n"},
            1_780_000_000 + DAY)

    facts = tool.extract(repo, tool.Budget(), base=base)

    assert facts["kind"] == "full" and facts["base_sha"] is None
    assert "changes" not in facts and "carried" not in facts
    reason = facts["extractor"]["incremental"]["reason"]
    assert facts["extractor"]["incremental"]["ran"] is False
    assert "configuration changed" in reason and path in reason
    assert not any("commit_sha" in m for m in facts["modules"])


def test_too_many_changed_files_force_a_full_run(tool, shards, tmp_path, monkeypatch):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, _ = _base(tool, shards, store, repo)
    _commit(repo, {"lib/c.py": "def only_here():\n    return 2\n",
                   "lib/a.py": "def render(x):\n    return [x]\n"}, 1_780_000_000 + DAY)
    monkeypatch.setattr(tool, "MAX_INCREMENTAL_CHANGES", 2)

    facts = tool.extract(repo, tool.Budget(), base=base)

    assert facts["kind"] == "full"
    assert "2 files changed" in facts["extractor"]["incremental"]["reason"]


def test_a_base_graph_without_per_file_blob_ids_forces_a_full_run(tool, shards, tmp_path):
    """A graph extracted before incremental runs existed cannot be compared."""
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, _ = _base(tool, shards, store, repo)
    for row in base.graph["files"]:
        row.pop("blob", None)
    _commit(repo, {"lib/c.py": "def only_here():\n    return 2\n"}, 1_780_000_000 + DAY)

    facts = tool.extract(repo, tool.Budget(), base=base)

    assert facts["kind"] == "full"
    assert "blob id" in facts["extractor"]["incremental"]["reason"]


def test_a_base_index_of_another_commit_forces_a_full_run(tool, shards, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    base, _ = _base(tool, shards, store, repo)
    _commit(repo, {"lib/c.py": "def only_here():\n    return 2\n"}, 1_780_000_000 + DAY)
    other = dict(base.index, commit_sha=hashlib.sha1(b"other").hexdigest())

    facts = tool.extract(repo, tool.Budget(), base=tool.Base(base.sha, base.graph, other))

    assert facts["kind"] == "full"
    assert "base index describes another commit" in facts["extractor"]["incremental"]["reason"]


def test_a_full_run_records_each_files_blob_id(tool, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    facts = tool.extract(repo, tool.Budget())
    blob = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD:lib/c.py"],
                          env=_env(repo), capture_output=True, text=True,
                          check=True).stdout.strip()
    row = next(f for f in facts["files"] if f["path"] == "lib/c.py")
    assert row["blob"] == blob
    assert "incremental" not in facts["extractor"]
    # The marker promotion records, so the API can tell a carriable graph.
    assert facts["extractor"]["blob_ids"] is True


def test_a_checkout_that_is_not_git_says_its_graph_cannot_be_carried(tool, tmp_path):
    plain = tmp_path / "plain"
    (plain / "lib").mkdir(parents=True)
    (plain / "lib" / "c.py").write_text("def only_here():\n    return 1\n")
    facts = tool.extract(plain, tool.Budget())
    assert facts["extractor"]["blob_ids"] is False


def test_the_api_reads_a_full_runs_manifest_as_carriable(tool, shards, tmp_path):
    """The extractor's marker survives the writer into the manifest promotion reads."""
    from swarm_api import repoindex

    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    _base_found, facts = _base(tool, shards, store, repo)
    record = repoindex.graph_extractor_record(_manifest(store, facts["commit_sha"]))
    assert record["blob_ids"] is True and record["version"] == tool.EXTRACTOR_VERSION
    assert repoindex.graph_carry_refusal(record) is None


# --------------------------------------------------------------------------
# the command lines the worker runs
# --------------------------------------------------------------------------

def test_the_command_lines_read_the_base_and_extract_incrementally(
    tool, shards, tmp_path
):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    bucket = tmp_path / "bucket"
    store = shards.LocalStore(bucket)
    work = tmp_path / "work"
    assert tool.main(["--repo", str(repo), "--no-lsp", "--out", str(work / "base-index.json"),
                      "--graph-out", str(work / "base-graph.json")]) == 0
    base_sha = _head(repo)
    assert shards.main(["write", "--graph", str(work / "base-graph.json"), "--store",
                        str(bucket), "--tenant", TENANT, "--repo-id", REPO_ID,
                        "--no-sweep"]) == 0
    digest = "sha256:" + hashlib.sha256(store.get(
        f"tenants/{TENANT}/repos/{REPO_ID}/graph/{base_sha}/manifest.json")).hexdigest()
    _commit(repo, {"lib/c.py": "def only_here():\n    return 2\n"}, 1_780_000_000 + DAY)

    assert shards.main(["read", "--commit", base_sha, "--manifest-digest", digest,
                        "--store", str(bucket), "--tenant", TENANT, "--repo-id", REPO_ID,
                        "--out", str(work / "repo-graph.base.json")]) == 0
    done = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--repo", str(repo), "--no-lsp",
         "--out", str(work / "extract.json"), "--graph-out", str(work / "graph.json"),
         "--base-sha", base_sha, "--base-index", str(work / "base-index.json"),
         "--base-graph", str(work / "repo-graph.base.json")],
        capture_output=True, text=True, timeout=120, env=_env(repo),
    )
    assert done.returncode == 0, done.stderr
    index = json.loads((work / "extract.json").read_text())
    assert index["kind"] == "incremental" and index["base_sha"] == base_sha
    assert index["changes"]["modified"] == ["lib/c.py"]
    assert "kind=incremental" in done.stderr

    # A base that cannot be read is a full run that says so, never a failure.
    broken = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--repo", str(repo), "--no-lsp",
         "--out", str(work / "extract2.json"), "--base-sha", base_sha,
         "--base-index", str(work / "missing.json"),
         "--base-graph", str(work / "repo-graph.base.json")],
        capture_output=True, text=True, timeout=120, env=_env(repo),
    )
    assert broken.returncode == 0, broken.stderr
    fallback = json.loads((work / "extract2.json").read_text())
    assert fallback["kind"] == "full"
    assert "no base index" in fallback["extractor"]["incremental"]["reason"]


# --------------------------------------------------------------------------
# reading a stored graph back
# --------------------------------------------------------------------------

def test_the_graph_read_back_is_the_graph_written(tool, shards, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    facts = tool.extract(repo, tool.Budget())
    graph = tool.graph_document(facts)
    written = shards.write_graph(graph, store, tenant_id=TENANT, repo_id=REPO_ID)

    read = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                             commit_sha=graph["commit_sha"],
                             manifest_digest=written["manifest_digest"])

    for key in ("symbols", "call_edges", "symbol_test_map", "files", "languages",
                "extractor", "kind", "commit_sha"):
        assert read[key] == graph[key], key


def test_a_rewritten_manifest_or_blob_is_refused_not_read(tool, shards, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", FILES)
    store = shards.LocalStore(tmp_path / "bucket")
    graph = tool.graph_document(tool.extract(repo, tool.Budget()))
    written = shards.write_graph(graph, store, tenant_id=TENANT, repo_id=REPO_ID)
    sha = graph["commit_sha"]

    with pytest.raises(ValueError, match="does not match the digest"):
        shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=sha,
                          manifest_digest="sha256:" + "0" * 64)

    manifest = _manifest(store, sha)
    blob = manifest["shards"]["symbols"]["lib"]["blob"].split(":", 1)[1]
    key = f"tenants/{TENANT}/repos/{REPO_ID}/graph/blobs/{blob}.jsonl.gz"
    (tmp_path / "bucket" / key).write_bytes(b"not the blob")
    with pytest.raises(ValueError, match="does not hold the bytes"):
        shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID, commit_sha=sha,
                          manifest_digest=written["manifest_digest"])


# --------------------------------------------------------------------------
# one rule, two places
# --------------------------------------------------------------------------

def test_swarm_api_applies_the_extractors_full_run_rules(tool):
    from swarm_api import repoindex

    assert repoindex.MAX_INCREMENTAL_CHANGES == tool.MAX_INCREMENTAL_CHANGES
    assert repoindex.INDEXER_EXTRACTOR_VERSION == tool.EXTRACTOR_VERSION
    assert repoindex.CONFIG_FILENAMES == tool.CONFIG_FILENAMES
    assert repoindex.CONFIG_GLOBS == tool.CONFIG_GLOBS
    assert repoindex.CONFIG_DIRECTORIES == tool.CONFIG_DIRECTORIES
    for path in ("Makefile", "web/package.json", ".github/workflows/x.yml",
                 "tests/conftest.py", "src/app.py", "requirements-dev.txt", "docs/x.md"):
        assert repoindex.is_config_path(path) == tool.is_config_path(path), path
