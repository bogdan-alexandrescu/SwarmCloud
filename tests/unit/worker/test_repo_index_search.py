"""Symbol search: the BM25 term index of extractor version 3 (lane KG2).

knowledge-graph.md §1.2 measured the question "where is the planner prompt
built for issue runs" against both engines: GitNexus's BM25 put swarm-mcp's
flows first and `planner_prompt` nowhere, and our graph had no search
surface at all. Version 3 indexes every application symbol's name (weighted),
its class, its file, its signature and the first sentence of its docstring,
and stores the postings by term bucket so a reader fetches only its query's
buckets. What is held here:

* the tokenizer splits identifiers and prose the same way (snake_case,
  camelCase, acronyms, plurals), and the extractor and every reader share
  its one copy in repo_graph_shards.py;
* a query naming a symbol's words finds that symbol first, ahead of symbols
  that only mention them in a docstring;
* test-side symbols are not indexed (a test is found through the test map);
* the postings round-trip through the shard writer, and the buckets a query
  needs are the only ones it has to read.
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
    "api/issueruns.py": (
        '"""Issue runs."""\n\n\n'
        "def planner_prompt(ref, run_id=None):\n"
        '    """The prompt the planner step of an issue run is given."""\n'
        "    return str(ref)\n\n\n"
        "def planner_task(ref):\n"
        '    """Submit the planner step; it calls planner_prompt for its text."""\n'
        "    return planner_prompt(ref)\n"
    ),
    "api/forge.py": (
        "class ForgeClient:\n"
        '    """Reads open pull requests from the forge."""\n\n'
        "    def open_pull_requests(self, repo):\n"
        "        return []\n"
    ),
    "web/src/runs.ts": (
        "/** Draw the issue run's progress. */\n"
        "export function drawIssueRunProgress(runId: string): number {\n"
        "  return runId.length;\n"
        "}\n"
    ),
    "tests/test_issueruns.py": (
        "from api import issueruns\n\n\n"
        "def test_the_planner_prompt_names_the_issue():\n"
        "    assert issueruns.planner_prompt('x')\n\n\n"
        "def planner_prompt_helper():\n"
        "    return 1\n"
    ),
}


@pytest.fixture()
def facts(tool, tmp_path) -> dict:
    return tool.extract(fx.build_repo(tmp_path / "repo", FILES), tool.Budget())


# --------------------------------------------------------------------------
# the tokenizer
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, terms", [
    ("planner_prompt", ["planner", "prompt"]),
    ("plannerPrompt", ["planner", "prompt"]),
    ("PlannerPrompt", ["planner", "prompt"]),
    ("HTTPServerError", ["http", "server", "error"]),
    ("the planner's prompts", ["planner", "prompt"]),
    ("issue runs", ["issue", "run"]),
    ("route_v2_handler", ["route", "v2", "handler"]),
    ("this is a test of the class", ["test", "class"]),
])
def test_identifiers_and_prose_split_the_same_way(shards, text, terms):
    assert shards.tokenize(text) == terms


def test_the_extractor_tokenizes_with_the_writers_copy(tool, shards):
    """One tokenizer: a reader that split differently would miss postings."""
    assert tool.srg.tokenize is shards.tokenize
    assert tool.srg.TOKENIZER_VERSION == shards.TOKENIZER_VERSION


# --------------------------------------------------------------------------
# the index and the ranking
# --------------------------------------------------------------------------

def test_the_design_query_finds_planner_prompt_first(shards, facts):
    """§1.2's question, which GitNexus's BM25 answered without it."""
    hits = shards.bm25_search(facts["terms"], facts["term_stats"],
                              "where is the planner prompt built for issue runs")
    assert hits[0][0] == "api/issueruns.py#planner_prompt"
    # planner_task's docstring names planner_prompt too, so it ranks, below.
    assert "api/issueruns.py#planner_task" in [symbol for symbol, _score in hits]


def test_a_docstring_and_a_typescript_comment_are_searchable(shards, facts):
    hits = shards.bm25_search(facts["terms"], facts["term_stats"], "open pull requests forge")
    assert hits[0][0] == "api/forge.py#ForgeClient.open_pull_requests" or \
        hits[0][0] == "api/forge.py#ForgeClient"
    assert {"api/forge.py#ForgeClient.open_pull_requests", "api/forge.py#ForgeClient"} <= \
        {symbol for symbol, _score in hits}
    hits = shards.bm25_search(facts["terms"], facts["term_stats"], "draw progress")
    assert hits[0][0] == "web/src/runs.ts#drawIssueRunProgress"


def test_test_side_symbols_are_not_indexed(facts):
    indexed = {f"{path}#{name}" for row in facts["terms"]
               for path, entries in row["postings"].items() for name, _tf, _dl in entries}
    assert not any(symbol.startswith("tests/") for symbol in indexed)
    assert "api/issueruns.py#planner_prompt" in indexed


def test_the_stats_are_what_bm25_needs(tool, shards, facts):
    stats = facts["term_stats"]
    indexed = {f"{path}#{name}" for row in facts["terms"]
               for path, entries in row["postings"].items() for name, _tf, _dl in entries}
    assert stats["documents"] == len(indexed)
    assert stats["k1"] == shards.BM25_K1 and stats["b"] == shards.BM25_B
    assert stats["tokenizer"] == shards.TOKENIZER_VERSION
    assert stats["name_weight"] == tool.TERM_NAME_WEIGHT
    # A posting's tf counts the name TERM_NAME_WEIGHT times.
    prompt = next(row for row in facts["terms"] if row["term"] == "prompt")
    (entry,) = [e for e in prompt["postings"]["api/issueruns.py"] if e[0] == "planner_prompt"]
    assert entry[1] >= tool.TERM_NAME_WEIGHT


def test_a_query_of_unknown_words_finds_nothing(shards, facts):
    assert shards.bm25_search(facts["terms"], facts["term_stats"], "zebra quantum") == []
    assert shards.bm25_search(facts["terms"], facts["term_stats"], "the of and") == []


def test_the_limit_bounds_the_answer(shards, facts):
    assert len(shards.bm25_search(facts["terms"], facts["term_stats"], "planner issue run",
                                  limit=1)) == 1


# --------------------------------------------------------------------------
# stored
# --------------------------------------------------------------------------

def test_the_postings_round_trip_and_a_query_reads_only_its_buckets(tool, shards, facts,
                                                                    tmp_path):
    store = shards.LocalStore(str(tmp_path / "store"))
    written = shards.write_graph(tool.graph_document(facts), store, tenant_id=TENANT,
                                 repo_id=REPO_ID)
    manifest = shards.read_manifest(store, tenant_id=TENANT, repo_id=REPO_ID,
                                    commit_sha=facts["commit_sha"],
                                    manifest_digest=written["manifest_digest"])
    assert manifest["term_stats"] == facts["term_stats"]
    buckets = manifest["index_shards"]["terms"]
    assert set(buckets) == {shards.term_bucket(row["term"]) for row in facts["terms"]}

    query = "planner prompt"
    wanted = {shards.term_bucket(term) for term in shards.tokenize(query)}
    rows = []
    for bucket in sorted(wanted & set(buckets)):
        digest = buckets[bucket]["blob"].split(":", 1)[1]
        rows += shards.decode_shard(store.get(shards.blob_key(TENANT, REPO_ID, digest)))
    assert shards.bm25_search(rows, manifest["term_stats"], query) == \
        shards.bm25_search(facts["terms"], facts["term_stats"], query)

    back = shards.read_graph(store, tenant_id=TENANT, repo_id=REPO_ID,
                             commit_sha=facts["commit_sha"])
    assert back["terms"] == facts["terms"]
    assert back["term_stats"] == facts["term_stats"]
