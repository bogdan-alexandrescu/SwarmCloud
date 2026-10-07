"""The test map counts source, reaches what imports cannot, and survives its cut.

QA pass of 2026-10-07 on the live index of this repository (wave 16, lane C5B):

* G4-04: "20 of 83 modules have tests" counted 24 test directories and 9
  Dockerfile-only images in the 83, and 5 of the 20 "covered" were test
  directories whose helpers and conftest the tests import, read as source.
* G4-05: `scripts/lib`, `terraform/modules/*` and `kubernetes` had no edge
  although tests run, load or plan them by PATH, and `Scheduler._admit_one`
  showed "0 tests reach it" although 8 test files call it on a fixture.
* G4-07: over the 512 KiB budget the map went to directory globs and was then
  halved most-confident first, which cut every `naming` edge; and the
  file-level map with `confidence`/`also_evidence` was nowhere a reader could
  page it.

Each test below fails on the extractor as it was on main that night.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"


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


def _edges(index: dict, source: str, test: str) -> list[dict]:
    return [t for t in index["test_map"] if t["source"] == source and t["test"] == test]


def _one(index: dict, source: str, test: str) -> dict:
    found = _edges(index, source, test)
    assert len(found) == 1, (source, test, sorted((t["source"], t["test"]) for t in index["test_map"]))
    return found[0]


# A source package, a test that imports a helper beside it, a conftest, a
# Dockerfile-only image and a fixture directory.
HELPERS: dict[str, str] = {
    **fx.PYTHON_APP,
    "tests/helpers/__init__.py": "",
    "tests/helpers/fake_store.py": (
        "from pkg.store import fetch\n"
        "\n"
        "\n"
        "def fake_fetch(key):\n"
        "    return fetch(key)\n"
    ),
    "tests/conftest.py": (
        "from helpers.fake_store import fake_fetch\n"
        "\n"
        "\n"
        "def pytest_configure(config):\n"
        "    fake_fetch(1)\n"
    ),
    "tests/test_fake.py": (
        "from helpers.fake_store import fake_fetch\n"
        "\n"
        "\n"
        "def test_fake():\n"
        "    assert fake_fetch(1)\n"
    ),
    "images/web/Dockerfile": "FROM scratch\n",
    "qa/smoke/runner.py": "def run():\n    return 1\n",
}


# ---------------------------------------------------------------------------
# G4-04: helpers are test-side; the denominator counts source modules only
# ---------------------------------------------------------------------------


def test_a_helper_its_tests_import_is_never_a_test_map_source(tool: Any, tmp_path: Path) -> None:
    """MUTATION: drop `test_side` from `_test_map`'s source set and the
    import `tests/test_fake.py -> tests/helpers/fake_store.py` is a source edge."""
    index = tool.extract(fx.build_repo(tmp_path / "repo", HELPERS), tool.Budget())
    sources = {t["source"] for t in index["test_map"]}
    assert not [s for s in sources if s.startswith("tests/")], sorted(sources)
    # Nor does the symbol test map call a helper a covered symbol.
    reached = {m["symbol"].split("#", 1)[0] for m in index["symbol_test_map"]}
    assert not [p for p in reached if p.startswith("tests/")], sorted(reached)
    # What the helper reaches is still covered, through it.
    assert ("src/pkg/store.py#fetch" in {m["symbol"] for m in index["symbol_test_map"]
                                          if m["test"] == "tests/test_fake.py#test_fake"})


def test_a_declared_test_root_makes_its_files_test_side(tool: Any, tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path / "repo", {
        **HELPERS,
        "qa/smoke/test_runner.py": "from smoke.runner import run\n\n\ndef test_run():\n    assert run()\n",
    })
    plain = tool.extract(repo, tool.Budget())
    assert plain["test_coverage"]["source_modules"] > 0
    assert "qa/smoke" not in {m["path"] for m in plain["test_coverage"]["not_counted"]}
    layout = [{"root": "qa", "framework": "pytest", "command": "pytest qa"}]
    declared = tool.extract(repo, tool.Budget(), test_layout=layout)
    assert {"path": "qa/smoke", "reason": "test"} in declared["test_coverage"]["not_counted"]
    assert not [t for t in declared["test_map"] if t["source"].startswith("qa/")]


def test_coverage_counts_source_modules_and_says_why_the_rest_are_out(
    tool: Any, tmp_path: Path
) -> None:
    """The live 20/83: test directories and Dockerfile-only images are not
    modules a test maps to, so they are named, with why, outside the count."""
    facts = tool.extract(fx.build_repo(tmp_path / "repo", HELPERS), tool.Budget())
    coverage = facts["test_coverage"]
    not_counted = {row["path"]: row["reason"] for row in coverage["not_counted"]}
    assert not_counted["tests"] == "test"
    assert not_counted["tests/helpers"] == "test"
    assert not_counted["images/web"] == "build"
    assert coverage["modules"] == len(facts["modules"])
    assert coverage["source_modules"] == coverage["modules"] - len(coverage["not_counted"])
    # src/pkg has tests; qa/smoke has none, and is named.
    assert coverage["source_modules_with_tests"] >= 1
    assert "qa/smoke" in coverage["without_tests"]
    assert "src/pkg" not in coverage["without_tests"]
    # The extractor's index carries it for the agent.
    assert tool.index_document(facts)["test_coverage"] == coverage


# ---------------------------------------------------------------------------
# G4-05: declared, path-ref, HCL module and name-unique edges
# ---------------------------------------------------------------------------


PATHS: dict[str, str] = {
    "scripts/lib/common.sh": "#!/usr/bin/env bash\nset -euo pipefail\n",
    "scripts/release.sh": "#!/usr/bin/env bash\nset -euo pipefail\n",
    "kubernetes/base/deployment.yaml": "kind: Deployment\n",
    "images/tool/repo-index/extract_tool.py": "def main():\n    return 0\n",
    "tests/unit/scripts/test_common.py": (
        "from pathlib import Path\n"
        "\n"
        "REPO = Path(__file__).resolve().parents[3]\n"
        "\n"
        "\n"
        "def test_common_is_sourced():\n"
        "    text = (REPO / \"scripts\" / \"lib\" / \"common.sh\").read_text()\n"
        "    assert text\n"
    ),
    "tests/unit/test_manifests.py": (
        "MANIFESTS = \"kubernetes/\"\n"
        "\n"
        "\n"
        "def test_manifests():\n"
        "    assert MANIFESTS\n"
    ),
    "tests/unit/test_loader.py": (
        "import importlib.util\n"
        "\n"
        "SPEC = importlib.util.spec_from_file_location(\n"
        "    \"extract_tool\", \"images/tool/repo-index/extract_tool.py\")\n"
        "\n"
        "\n"
        "def test_loads():\n"
        "    assert SPEC\n"
    ),
    "tests/unit/test_fixture_only.py": (
        "DATA = \"tests/unit/test_manifests.py\"\n"
        "\n"
        "\n"
        "def test_data():\n"
        "    assert DATA\n"
    ),
}


def test_a_test_naming_a_repository_path_has_a_path_ref_edge_to_it(
    tool: Any, tmp_path: Path
) -> None:
    """A script run through subprocess, a manifest directory and a module
    loaded by `spec_from_file_location` have no import statement to follow."""
    index = tool.extract(fx.build_repo(tmp_path / "repo", PATHS), tool.Budget())
    common = _one(index, "scripts/lib/common.sh", "tests/unit/scripts/test_common.py")
    assert (common["evidence"], common["confidence"]) == ("path-ref", 0.35)
    assert _one(index, "kubernetes/**", "tests/unit/test_manifests.py")["evidence"] == "path-ref"
    loader = _one(index, "images/tool/repo-index/extract_tool.py", "tests/unit/test_loader.py")
    assert loader["evidence"] == "path-ref"
    # A test naming another test, or a test directory, is not coverage.
    assert not [t for t in index["test_map"] if t["test"] == "tests/unit/test_fixture_only.py"]


def test_a_declared_covers_glob_connects_every_test_under_its_root(
    tool: Any, tmp_path: Path
) -> None:
    layout = [
        {"root": "tests/unit/scripts", "framework": "pytest", "command": "pytest",
         "covers": ["scripts/**"]},
        # A glob matching nothing is stale, and gives no edge.
        {"root": "tests/unit/scripts", "framework": "pytest", "command": "pytest",
         "covers": ["gone/**"]},
    ]
    index = tool.extract(fx.build_repo(tmp_path / "repo", PATHS), tool.Budget(),
                         test_layout=layout)
    edge = _one(index, "scripts/**", "tests/unit/scripts/test_common.py")
    assert (edge["evidence"], edge["confidence"]) == ("declared", 0.2)
    assert not [t for t in index["test_map"] if t["source"] == "gone/**"]
    # The base index's layout is read when no other is given (an incremental run).
    base_index = {"schema": tool.SCHEMA, "test_layout": layout}
    repo = tmp_path / "repo"
    base = tool.Base(sha="0" * 40, graph=None, index=base_index)
    carried = tool.extract(repo, tool.Budget(), base=base)
    assert _one(carried, "scripts/**", "tests/unit/scripts/test_common.py")["evidence"] == "declared"


TERRAFORM: dict[str, str] = {
    "terraform/infra/main.tf": (
        "module \"iam\" {\n"
        "  source = \"../modules/iam\"\n"
        "}\n"
        "\n"
        "module \"network\" {\n"
        "  source = \"../modules/network\"\n"
        "}\n"
    ),
    "terraform/modules/iam/main.tf": (
        "output \"service_account\" {\n"
        "  value = \"sa\"\n"
        "}\n"
    ),
    "terraform/modules/network/main.tf": (
        "output \"id\" {\n"
        "  value = \"net\"\n"
        "}\n"
    ),
    "tests/terraform/iam.tftest.hcl": (
        "run \"plan_iam\" {\n"
        "  command = plan\n"
        "\n"
        "  module {\n"
        "    source = \"../../terraform/infra\"\n"
        "  }\n"
        "\n"
        "  assert {\n"
        "    condition     = module.iam.service_account != \"\"\n"
        "    error_message = \"no account\"\n"
        "  }\n"
        "}\n"
    ),
}


def test_a_tftest_reaches_the_root_it_runs_and_the_modules_it_asserts_on(
    tool: Any, tmp_path: Path
) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", TERRAFORM), tool.Budget())
    test = "tests/terraform/iam.tftest.hcl"
    root = _one(index, "terraform/infra/**", test)
    assert (root["evidence"], root["confidence"]) == ("path-ref", 0.35)
    iam = _one(index, "terraform/modules/iam/**", test)
    assert (iam["evidence"], iam["confidence"]) == ("path-ref", 0.3)
    # A module of that root the test never names is not reached.
    assert not _edges(index, "terraform/modules/network/**", test)


ADMIT: dict[str, str] = {
    "apps/scheduler/scheduler/loop.py": (
        "class Scheduler:\n"
        "    def _admit_one(self, task):\n"
        "        return task\n"
        "\n"
        "    def run(self):\n"
        "        return 1\n"
    ),
    "apps/scheduler/scheduler/other.py": (
        "class Other:\n"
        "    def run(self):\n"
        "        return 2\n"
    ),
    "tests/unit/control_plane/test_admission.py": (
        "def make():\n"
        "    return object()\n"
        "\n"
        "\n"
        "def test_admits(sched):\n"
        "    sched._admit_one(1)\n"
        "\n"
        "\n"
        "def test_admits_through_an_attribute(env):\n"
        "    env.scheduler._admit_one(1)\n"
        "\n"
        "\n"
        "def test_runs(sched):\n"
        "    sched.run()\n"
    ),
}


def test_a_method_called_on_a_fixture_resolves_when_its_name_is_unique(
    tool: Any, tmp_path: Path
) -> None:
    index = tool.extract(fx.build_repo(tmp_path / "repo", ADMIT), tool.Budget())
    target = "apps/scheduler/scheduler/loop.py#Scheduler._admit_one"
    test_file = "tests/unit/control_plane/test_admission.py"
    for test in ("test_admits", "test_admits_through_an_attribute"):
        found = [e for e in index["call_edges"]
                 if e["from"] == f"{test_file}#{test}" and e["to"] == target]
        assert len(found) == 1, test
        assert (found[0]["evidence"], found[0]["confidence"]) == ("ast", 0.3)
    reached = {m["test"] for m in index["symbol_test_map"] if m["symbol"] == target}
    assert reached == {f"{test_file}#test_admits", f"{test_file}#test_admits_through_an_attribute"}
    # `run` is defined on two classes: no type, no unique name, no edge.
    assert not [e for e in index["call_edges"] if e["from"] == f"{test_file}#test_runs"]
    assert _one(index, "apps/scheduler/scheduler/loop.py", test_file)["evidence"] == "ast"


# ---------------------------------------------------------------------------
# G4-07: the file-level map in the graph; the index's cut keeps every source
# ---------------------------------------------------------------------------


def test_the_graph_carries_the_file_level_map_with_confidence(
    tool: Any, shards: Any, tmp_path: Path
) -> None:
    facts = tool.extract(fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **PATHS}),
                         tool.Budget())
    graph = tool.graph_document(facts)
    rows = {f["path"]: f for f in graph["files"]}
    users = {t["test"]: t for t in rows["src/pkg/users.py"]["tests"]}
    edge = users["tests/test_users.py"]
    assert edge["evidence"] == "ast" and edge["confidence"] == 0.6
    assert edge["also_evidence"] == ["import", "naming"]
    assert rows["scripts/lib/common.sh"]["tests"][0]["evidence"] == "path-ref"
    assert "tests" not in rows["tests/test_users.py"]
    file_level = [t for t in facts["test_map"] if not t["source"].endswith("**")]
    assert sum(len(r.get("tests", [])) for r in graph["files"]) == len(file_level)
    assert tool.index_document(facts)["graph"]["test_map"] == len(file_level)
    # Through the shard writer and back, sharded by the file's module.
    document = json.loads(tool.dumps(graph))
    document["commit_sha"] = "a" * 40
    store = shards.LocalStore(str(tmp_path / "store"))
    shards.write_graph(document, store, tenant_id="tenant", repo_id="repo_" + "0" * 16)
    back = shards.read_graph(store, tenant_id="tenant", repo_id="repo_" + "0" * 16,
                             commit_sha="a" * 40)
    again = {f["path"]: f for f in back["files"]}
    assert again["src/pkg/users.py"]["tests"] == rows["src/pkg/users.py"]["tests"]


def test_the_index_serves_only_evidence_swarm_api_accepts(tool: Any, tmp_path: Path) -> None:
    """`path-ref` is not in `RepoIndexSpec`'s vocabulary: an agent copying it
    into repo-index.json would have the whole index refused at promotion."""
    from swarm_api import repoindex

    allowed = set(repoindex.EVIDENCE_ORDER)
    facts = tool.extract(fx.build_repo(tmp_path / "repo", PATHS), tool.Budget())
    assert "path-ref" in {t["evidence"] for t in facts["test_map"]}
    index = tool.index_document(facts)
    assert {t["evidence"] for t in index["test_map"]} <= allowed
    common = [t for t in index["test_map"] if t["source"] == "scripts/lib/common.sh"][0]
    assert common["evidence"] == "declared" and "path-ref" in common["also_evidence"]


def _synthetic(tool: Any, tmp_path: Path, entries: list[dict]) -> dict:
    facts = tool.extract(fx.build_repo(tmp_path / "repo", fx.PYTHON_APP), tool.Budget())
    facts["test_map"] = entries
    return facts


def test_the_cut_keeps_a_naming_only_source_and_trims_the_crowded_one(
    tool: Any, tmp_path: Path
) -> None:
    """MUTATION: put confidence-first back in `keep_first` and the one
    `naming` edge is the first to go, leaving `lonely/` with no test."""
    crowded = [{"source": f"crowded/mod{i:03d}.py", "test": f"tests/test_c{i:03d}.py",
                "evidence": "import", "confidence": 0.4, "also_evidence": []}
               for i in range(60)]
    lonely = [{"source": "lonely/only.py", "test": "tests/test_only.py",
               "evidence": "naming", "confidence": 0.3, "also_evidence": []}]
    facts = _synthetic(tool, tmp_path, crowded + lonely)
    full = tool.index_document(facts)
    cap = len(tool.dumps(full)) - len(tool.dumps(full["test_map"])) // 3
    cut = tool.index_document(facts, max_bytes=cap)
    assert "test_map" in cut["truncated"]
    assert len(cut["test_map"]) < len(full["test_map"])
    assert [t for t in cut["test_map"] if t["source"].startswith("lonely/")]


def test_over_its_bound_the_index_map_goes_to_directories_and_the_graph_keeps_files(
    tool: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "MAX_TEST_MAP", 10)
    entries = [{"source": f"pkg/m{i:02d}.py", "test": "tests/test_pkg.py",
                "evidence": "import", "confidence": 0.4, "also_evidence": []} for i in range(12)]
    entries.append({"source": "pkg/m00.py", "test": "tests/test_m00.py",
                    "evidence": "naming", "confidence": 0.3, "also_evidence": []})
    facts = _synthetic(tool, tmp_path, entries)
    index = tool.index_document(facts)
    assert "test_map" in index["truncated"]
    pairs = {(t["source"], t["test"]): t for t in index["test_map"]}
    # Twelve duplicate (directory, test) pairs are one edge; the naming one stays.
    assert set(pairs) == {("pkg/**", "tests/test_pkg.py"), ("pkg/**", "tests/test_m00.py")}
    assert pairs[("pkg/**", "tests/test_m00.py")]["evidence"] == "naming"
    # The graph's count is the file level, whole.
    assert index["graph"]["test_map"] == len(facts["test_map"]) == 13
