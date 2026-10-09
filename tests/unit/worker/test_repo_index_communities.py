"""Module communities: extractor version 3 (lane KG2, knowledge-graph.md §6).

The extractor groups the application's files into communities with Louvain
over the resolved call/import graph, each community split into its connected
parts, and an incremental run starts from its base's partition. What a
consumer leans on, and so what is held here:

* two groups of files that call within themselves and barely across are two
  communities, and the same graph always gives the same partition (an index
  is byte-identical for the same commit, §2.3);
* a community is never two islands: a member is reachable from every other
  member inside the community;
* the partition is drawn from facts only -- `ast`, `lsp` and `import` edges
  between application files. A test-side file, or an edge of `declared`,
  `path-ref`, `naming` or `co-change` evidence, moves nothing (§7.8: gates
  never read judged edges);
* an incremental run keeps its base's ids and, after a one-file change, at
  least 0.9 of its partition (Jaccard, §6's acceptance), and says how much
  it kept;
* the communities survive the shard writer and read back into the base of
  the next run.
"""

from __future__ import annotations

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
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", tmp_path / "no-such-environ")
    for name in ("TENANT_ID", "ARTIFACT_BUCKET"):
        monkeypatch.delenv(name, raising=False)


def _edge(frm: str, to: str, *, kind: str = "call", evidence: str = "ast",
          confidence: float = 0.6) -> dict:
    return {"from": frm, "to": to, "kind": kind, "evidence": evidence,
            "confidence": confidence, "also_evidence": [], "path": frm.split("#")[0],
            "line": 1, "sites": 1}


def _clique(prefix: str, size: int) -> list[dict]:
    files = [f"{prefix}/m{i}.py" for i in range(size)]
    return [_edge(f"{a}#f", f"{b}#f") for a in files for b in files if a < b]


def _two_cliques() -> list[dict]:
    """Two groups of five files, every pair inside calling, one call across."""
    return [*_clique("billing", 5), *_clique("search", 5),
            _edge("billing/m0.py#f", "search/m0.py#f")]


def _members(rows: list[dict]) -> list[list[str]]:
    return sorted(sorted(row["files"]) for row in rows)


# --------------------------------------------------------------------------
# the algorithm
# --------------------------------------------------------------------------

def test_two_dense_groups_joined_by_one_call_are_two_communities(tool):
    rows = tool.communities(_two_cliques(), [], set())
    assert _members(rows) == [[f"billing/m{i}.py" for i in range(5)],
                              [f"search/m{i}.py" for i in range(5)]]
    by_label = {row["label"]: row for row in rows}
    assert set(by_label) == {"billing", "search"}
    for row in rows:
        assert row["size"] == 5
        # Ten internal edges each way against one across: nearly closed.
        assert 0.9 < row["cohesion"] < 1.0


def test_the_same_graph_always_gives_the_same_partition_and_ids(tool):
    edges = _two_cliques()
    first = tool.communities(edges, [], set())
    assert tool.communities(list(reversed(edges)), [], set()) == first
    assert [row["id"] for row in first] == ["c0001", "c0002"]


def test_a_community_is_never_two_islands(tool):
    """Louvain can leave a community joined only through a node that moved
    away; the split into connected parts (Leiden's guarantee) removes it."""
    graph = {"a": {"b": 1.0}, "b": {"a": 1.0}, "c": {"d": 1.0}, "d": {"c": 1.0}}
    # Seeded as one community, the two pairs have nothing between them.
    groups = tool.louvain(graph, seed={node: "c0001" for node in graph})
    assert groups == [["a", "b"], ["c", "d"]]
    for rows in (tool.communities(_two_cliques(), [], set()),):
        for row in rows:
            members = set(row["files"])
            adjacency: dict[str, set[str]] = {}
            for edge in _two_cliques():
                a, b = edge["from"].split("#")[0], edge["to"].split("#")[0]
                if a in members and b in members:
                    adjacency.setdefault(a, set()).add(b)
                    adjacency.setdefault(b, set()).add(a)
            seen, frontier = {row["files"][0]}, [row["files"][0]]
            while frontier:
                for other in adjacency.get(frontier.pop(), ()):
                    if other not in seen:
                        seen.add(other)
                        frontier.append(other)
            assert seen == members, row


def test_judged_edges_and_test_files_move_nothing(tool):
    """§7.8: the partition is facts only. Bridges of every judged evidence, and
    a test file calling into both groups, leave it exactly as it was."""
    plain = tool.communities(_two_cliques(), [], set())
    bridges = [
        _edge(f"billing/m{i}.py#f", f"search/m{j}.py#f", evidence=evidence, confidence=1.0)
        for i in range(5) for j in range(5)
        for evidence in ("declared", "path-ref", "naming", "co-change")
    ]
    test_calls = [_edge("tests/test_both.py#test_x", f"{group}/m{i}.py#f", confidence=1.0)
                  for group in ("billing", "search") for i in range(5)]
    noisy = tool.communities([*_two_cliques(), *bridges, *test_calls], [],
                             {"tests/test_both.py"})
    assert noisy == plain
    assert all("tests/test_both.py" not in row["files"] for row in noisy)


def test_lsp_and_import_edges_draw_communities_too(tool):
    edges = [_edge(e["from"].split("#")[0], e["to"].split("#")[0], kind="import",
                   evidence="import", confidence=0.4) for e in _clique("a", 4)]
    edges += [{**e, "evidence": "lsp", "confidence": 0.95} for e in _clique("b", 4)]
    assert _members(tool.communities(edges, [], set())) == [
        [f"a/m{i}.py" for i in range(4)], [f"b/m{i}.py" for i in range(4)]]


def test_a_community_counts_the_symbols_its_files_define(tool):
    symbols = [{"id": f"billing/m{i}.py#f", "path": f"billing/m{i}.py"} for i in range(5)]
    symbols.append({"id": "billing/m0.py#g", "path": "billing/m0.py"})
    by_label = {row["label"]: row for row in tool.communities(_two_cliques(), symbols, set())}
    assert by_label["billing"]["symbols"] == 6
    assert by_label["search"]["symbols"] == 0


# --------------------------------------------------------------------------
# stability and ids across runs
# --------------------------------------------------------------------------

def test_a_seeded_run_keeps_its_bases_ids(tool):
    base = [{"id": "c0007", "files": [f"search/m{i}.py" for i in range(5)]},
            {"id": "c0003", "files": [f"billing/m{i}.py" for i in range(5)]}]
    rows = tool.communities(_two_cliques(), [], set(), base=base)
    assert {row["label"]: row["id"] for row in rows} == {"search": "c0007", "billing": "c0003"}


def test_a_new_community_takes_the_next_unused_number(tool):
    base = [{"id": "c0004", "files": [f"billing/m{i}.py" for i in range(5)]}]
    edges = [*_two_cliques(), *_clique("ledger", 4)]
    rows = tool.communities(edges, [], set(), base=base)
    ids = {row["label"]: row["id"] for row in rows}
    assert ids["billing"] == "c0004"
    assert sorted([ids["search"], ids["ledger"]]) == ["c0005", "c0006"]


def test_stability_is_one_for_the_same_partition_and_falls_with_moves(tool):
    rows = tool.communities(_two_cliques(), [], set())
    assert tool.community_stability(rows, rows) == 1.0
    moved = [{"id": "x", "files": rows[0]["files"][:4]},
             {"id": "y", "files": [rows[0]["files"][4], *rows[1]["files"]]}]
    # billing kept 4 of 5 (Jaccard 0.8), search 5 of its 6 (5/6), weighted by size.
    assert tool.community_stability(rows, moved) == pytest.approx((0.8 * 5 + 5 / 6 * 5) / 10,
                                                                  abs=1e-4)
    assert tool.community_stability([], rows) is None


def _repo_files() -> dict[str, str]:
    """Two packages, each a chain of modules calling one another, one call across."""
    files: dict[str, str] = {}
    for package, other in (("billing", "search"), ("search", "billing")):
        files[f"{package}/__init__.py"] = ""
        for i in range(6):
            calls = "\n".join(f"    m{j}.f{j}()" for j in range(6) if j != i)
            files[f"{package}/m{i}.py"] = (
                f"from {package} import " + ", ".join(f"m{j}" for j in range(6) if j != i)
                + f"\n\n\ndef f{i}():\n{calls}\n"
            )
    files["billing/m0.py"] += "\n\ndef bridge():\n    from search import m0\n    m0.f0()\n"
    files["tests/test_billing.py"] = (
        "from billing import m1\nfrom search import m2\n\n\n"
        "def test_both():\n    m1.f1()\n    m2.f2()\n"
    )
    return files


def test_an_incremental_run_keeps_at_least_nine_tenths_of_its_partition(tool, shards,
                                                                        tmp_path):
    """§6's acceptance, on a real extract: full run, store, read back as the
    base, change one file, run incrementally."""
    repo = fx.build_repo(tmp_path / "repo", _repo_files())
    facts = tool.extract(repo, tool.Budget())
    assert len(facts["communities"]) == 2, facts["communities"]
    store = shards.LocalStore(str(tmp_path / "store"))
    written = shards.write_graph(tool.graph_document(facts), store, tenant_id=TENANT,
                                 repo_id=REPO_ID)
    graph = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                              commit_sha=facts["commit_sha"],
                              manifest_digest=written["manifest_digest"])
    assert graph["communities"] == facts["communities"]
    base = tool.Base(sha=facts["commit_sha"], graph=graph, index=tool.index_document(facts))

    env = fx.git_env(repo.parent / (repo.name + "-home"))
    fx.write_files(repo, {"search/m3.py": "from search import m4\n\n\ndef f3():\n"
                                          "    m4.f4()\n\n\ndef extra():\n    return 3\n"})
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "change"],
                   env={**env, "GIT_AUTHOR_DATE": f"@{1_780_000_000 + DAY} +0000",
                        "GIT_COMMITTER_DATE": f"@{1_780_000_000 + DAY} +0000"}, check=True)
    after = tool.extract(repo, tool.Budget(), base=base)
    assert after["kind"] == "incremental"
    record = after["extractor"]["communities"]
    assert record["seeded_from_base"] is True
    assert record["stability_vs_base"] >= 0.9
    assert record["stability_vs_base"] == tool.community_stability(facts["communities"],
                                                                   after["communities"])
    assert {r["id"] for r in after["communities"]} == {r["id"] for r in facts["communities"]}


def test_a_full_run_says_it_was_not_seeded(tool, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", _repo_files())
    record = tool.extract(repo, tool.Budget())["extractor"]["communities"]
    assert record == {"algorithm": "louvain", "resolution": tool.COMMUNITY_RESOLUTION,
                      "split": "connected components", "seeded_from_base": False,
                      "stability_vs_base": None}


def test_the_index_summary_counts_the_communities(tool, tmp_path):
    repo = fx.build_repo(tmp_path / "repo", _repo_files())
    facts = tool.extract(repo, tool.Budget())
    index = tool.index_document(facts)
    assert index["graph"]["communities"] == len(facts["communities"]) == 2
    graph = json.loads(tool.dumps(tool.graph_document(facts)))
    assert graph["communities"] == facts["communities"]
