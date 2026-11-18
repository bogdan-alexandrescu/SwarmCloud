"""Module-object calls and entry-point flows: extractor version 3 (lane KG2).

knowledge-graph.md §2.2 item 6: "Both engines miss calls made through a
module object. The six `issueruns.planner_prompt(...)` calls in the tests
resolve to nothing in either tool." A test that imports a module and calls
through it is how most of this repository's tests are written, and an edge
missing there is a test the impact engine cannot select by symbol. Version 3
resolves:

* `from pkg import mod` then `mod.f()`, absolute and relative;
* `import pkg.mod` then `pkg.mod.f()`, and `import pkg.mod as m` then `m.f()`
  (the alias case worked before and must still);
* `from mod import Cls` then `Cls.method()`;

and does NOT invent an edge for a chain into a module outside the repository
(`os.path.join()`), nor for a name the module does not define.

Entry-point flows: from each route, Python `__main__` guard and Go `main`,
the bounded breadth-first call flow over confident edges, every step naming
the step it came from.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
EXTRACTOR = TOOL_DIR / "repo_index_extract.py"


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


APP = {
    "src/swarm_api/__init__.py": "",
    "src/swarm_api/issueruns.py": (
        "def planner_prompt(ref):\n    return render(ref)\n\n\n"
        "def render(ref):\n    return str(ref)\n\n\n"
        "class Plan:\n    @classmethod\n    def parse(cls, text):\n        return cls()\n"
    ),
    "src/swarm_api/forge.py": "def open_work():\n    return []\n",
    "src/swarm_api/sub/__init__.py": "",
    "src/swarm_api/sub/helpers.py": (
        "from .. import issueruns\n\n\n"
        "def helper():\n    return issueruns.render('x')\n"
    ),
}


def _calls(facts: dict, frm: str) -> dict[str, dict]:
    return {e["to"]: e for e in facts["call_edges"] if e["from"] == frm and e["kind"] == "call"}


def _extract(tool: Any, root: Path, files: dict[str, str]) -> dict:
    return tool.extract(fx.build_repo(root, files), tool.Budget())


def test_a_call_through_a_from_imported_module_resolves(tool, tmp_path):
    """The six planner_prompt tests' shape: `from swarm_api import issueruns`."""
    facts = _extract(tool, tmp_path / "repo", {**APP, "tests/test_issue_runs.py": (
        "from swarm_api import forge, issueruns\n\n\n"
        "def test_the_prompt():\n"
        "    assert issueruns.planner_prompt('x')\n"
        "    assert forge.open_work() == []\n"
    )})
    calls = _calls(facts, "tests/test_issue_runs.py#test_the_prompt")
    prompt = calls["src/swarm_api/issueruns.py#planner_prompt"]
    assert (prompt["evidence"], prompt["confidence"]) == ("ast", tool.AST_UNIQUE)
    assert "src/swarm_api/forge.py#open_work" in calls
    # And so the test is selected by the symbol, and by what it reaches.
    covered = {(row["symbol"], row["test"]) for row in facts["symbol_test_map"]}
    test = "tests/test_issue_runs.py#test_the_prompt"
    assert ("src/swarm_api/issueruns.py#planner_prompt", test) in covered
    assert ("src/swarm_api/issueruns.py#render", test) in covered


def test_a_relative_from_import_of_a_module_resolves(tool, tmp_path):
    facts = _extract(tool, tmp_path / "repo", APP)
    assert "src/swarm_api/issueruns.py#render" in _calls(
        facts, "src/swarm_api/sub/helpers.py#helper")


def test_a_dotted_chain_through_import_resolves(tool, tmp_path):
    facts = _extract(tool, tmp_path / "repo", {**APP, "tools/cli.py": (
        "import swarm_api.issueruns\n"
        "import swarm_api.forge as fg\n\n\n"
        "def run():\n"
        "    swarm_api.issueruns.planner_prompt('x')\n"
        "    fg.open_work()\n"
    )})
    calls = _calls(facts, "tools/cli.py#run")
    assert "src/swarm_api/issueruns.py#planner_prompt" in calls
    assert "src/swarm_api/forge.py#open_work" in calls


def test_a_method_called_on_an_imported_class_resolves(tool, tmp_path):
    facts = _extract(tool, tmp_path / "repo", {**APP, "tools/load.py": (
        "from swarm_api.issueruns import Plan\n\n\n"
        "def load(text):\n    return Plan.parse(text)\n"
    )})
    assert "src/swarm_api/issueruns.py#Plan.parse" in _calls(facts, "tools/load.py#load")


def test_no_edge_is_invented_for_an_outside_module_or_a_missing_name(tool, tmp_path):
    facts = _extract(tool, tmp_path / "repo", {**APP, "tools/paths.py": (
        "import os.path\n"
        "from swarm_api import issueruns\n\n\n"
        "def join(a, b):\n    return a + b\n\n\n"
        "def build():\n"
        "    os.path.join('a', 'b')\n"
        "    issueruns.no_such_function()\n"
    )})
    # Before version 3, `os.path.join()` resolved to this file's own `join`.
    assert _calls(facts, "tools/paths.py#build") == {}


# --------------------------------------------------------------------------
# entry-point flows
# --------------------------------------------------------------------------

FLOW_APP = {
    "svc/__init__.py": "",
    "svc/api.py": (
        "from fastapi import APIRouter\n"
        "from svc import store\n\n"
        "router = APIRouter()\n\n\n"
        "@router.get('/v1/runs')\n"
        "def list_runs():\n    return store.load_runs()\n"
    ),
    "svc/store.py": (
        "def load_runs():\n    return _read()\n\n\n"
        "def _read():\n    return []\n"
    ),
    "svc/cli.py": (
        "from svc import store\n\n\n"
        "def main():\n    return store.load_runs()\n\n\n"
        "if __name__ == '__main__':\n    main()\n"
    ),
    "cmd/main.go": "package main\n\nfunc main() {\n\trun()\n}\n\nfunc run() {}\n",
    "tests/test_api.py": "from svc import api\n\n\ndef test_list():\n    api.list_runs()\n",
}


def _flows(facts: dict) -> dict[str, dict]:
    return {row["entry"]: row for row in facts["flows"]}


def test_a_route_flows_through_its_handler_to_what_it_calls(tool, tmp_path):
    flows = _flows(_extract(tool, tmp_path / "repo", FLOW_APP))
    flow = flows["svc/api.py#GET /v1/runs"]
    assert flow["kind"] == "route" and flow["path"] == "svc/api.py"
    assert flow["steps"] == [
        {"symbol": "svc/api.py#list_runs", "depth": 1, "via": "svc/api.py#GET /v1/runs"},
        {"symbol": "svc/store.py#load_runs", "depth": 2, "via": "svc/api.py#list_runs"},
        {"symbol": "svc/store.py#_read", "depth": 3, "via": "svc/store.py#load_runs"},
    ]
    assert flow["files"] == ["svc/api.py", "svc/store.py"]
    assert flow["truncated"] is False


def test_a_python_main_guard_and_a_go_main_are_entry_points(tool, tmp_path):
    flows = _flows(_extract(tool, tmp_path / "repo", FLOW_APP))
    cli = flows["svc/cli.py"]
    assert cli["kind"] == "main"
    assert [step["symbol"] for step in cli["steps"]] == [
        "svc/cli.py#main", "svc/store.py#load_runs", "svc/store.py#_read"]
    go = flows["cmd/main.go#main"]
    assert go["kind"] == "main"
    assert [step["symbol"] for step in go["steps"]] == ["cmd/main.go#main", "cmd/main.go#run"]
    # A test is not an entry point, and no flow steps into a test file.
    assert not any(entry.startswith("tests/") for entry in flows)
    assert not any(step["symbol"].startswith("tests/")
                   for flow in flows.values() for step in flow["steps"])


def test_a_flow_is_bounded_and_says_so(tool, tmp_path, monkeypatch):
    chain = "".join(f"def f{i}():\n    return f{i + 1}()\n\n\n" for i in range(10))
    files = {"deep.py": chain + "def f10():\n    return 0\n\n\n"
                                "if __name__ == '__main__':\n    f0()\n"}
    monkeypatch.setattr(tool, "FLOW_MAX_DEPTH", 4)
    flow = _flows(_extract(tool, tmp_path / "a", files))["deep.py"]
    assert [step["depth"] for step in flow["steps"]] == [1, 2, 3, 4]
    assert flow["truncated"] is True
    monkeypatch.setattr(tool, "FLOW_MAX_DEPTH", 20)
    monkeypatch.setattr(tool, "FLOW_MAX_STEPS", 3)
    flow = _flows(_extract(tool, tmp_path / "b", files))["deep.py"]
    assert len(flow["steps"]) == 3 and flow["truncated"] is True
    monkeypatch.setattr(tool, "FLOW_MAX_STEPS", 100)
    flow = _flows(_extract(tool, tmp_path / "c", files))["deep.py"]
    assert len(flow["steps"]) == 11 and flow["truncated"] is False
