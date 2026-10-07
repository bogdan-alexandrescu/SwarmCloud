"""The served test map, its coverage and the history behind it (QA G4-04/06/07, swarm-api half).

Lane C5B (#786) moved the extractor: test-side directories are no longer test
map sources, it writes a `test_coverage` block whose denominator is source
modules only, its file-level edges carry `confidence` and `also_evidence`, and
it records how much history its checkout held. What is held here is the API
side of each:

  * G4-04 `coverage()` reads the document's `test_coverage` block when it has
    one (`basis: "extractor"`), so "tests mapped" counts source modules over
    source modules; an older document keeps today's count (`basis: "legacy"`);
  * G4-07 a served `TestEdge` keeps `confidence` and `also_evidence`, still
    refusing any other key, and `GET /v1/repositories/{id}/test-map?path=`
    pages the edges of one source path or glob -- from the graph's file-level
    map when the index has a graph, else from the document;
  * G4-06 the version's `extractor.history` is judged (`history_depth`) and
    served as `index.history`, so the console can put a dash with a reason
    where co-change evidence was impossible.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from swarm_api import repoindex
from swarm_api.errors import ValidationFailed

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE = sha("one")
WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")

#: The extractor's block as #786 writes it: 83 modules, of which 40 are source.
TEST_COVERAGE = {
    "modules": 83, "source_modules": 40, "source_modules_with_tests": 31,
    "not_counted": [{"path": "tests/unit", "reason": "test"},
                    {"path": "images/agent-runtime-base", "reason": "build"}],
    "without_tests": ["apps/redaction/redaction"],
}

SHALLOW = {"available": False, "window_days": 90, "window_start": None, "window_end": None,
           "commits": 0, "shallow": True, "boundary_commits": 1, "window_covered": False,
           "reason": "the checkout is shallow and holds no commit inside the 90-day window"}
CUT_SHORT = {"available": True, "window_days": 90, "window_start": "2026-07-09T00:00:00+00:00",
             "window_end": "2026-10-07T00:00:00+00:00", "commits": 12, "shallow": True,
             "boundary_commits": 1, "window_covered": False, "reason": None}
WHOLE = dict(CUT_SHORT, commits=480, shallow=False, boundary_commits=0, window_covered=True)


def _extractor(history: dict | None = None) -> dict:
    record: dict[str, Any] = {"ran": True, "command": "swarm-repo-index", "version": "2"}
    if history is not None:
        record["history"] = history
    return record


# --------------------------------------------------------------------------
# G4-04: coverage() reads the extractor's block
# --------------------------------------------------------------------------

def test_coverage_reads_the_extractors_block_with_source_modules_as_the_denominator():
    """MUTATION: drop the `test_coverage` branch and `modules` is 2 (the
    fixture's module list) with `basis` "legacy"."""
    answer = repoindex.coverage(fixture_index(ONE, test_coverage=TEST_COVERAGE))
    assert answer["basis"] == "extractor"
    assert answer["modules"] == 40 and answer["modules_with_tests"] == 31
    assert answer["modules_indexed"] == 83
    assert answer["not_counted"] == {"test": 1, "build": 1}
    # What stands behind the figure is unchanged.
    assert answer["test_map_edges"] == 3 and answer["always_tests"] == 1


def test_coverage_of_an_older_document_is_todays_count_and_says_so():
    answer = repoindex.coverage(fixture_index(ONE))
    assert answer == {"basis": "legacy", "modules": 2, "modules_with_tests": 2,
                      "test_map_edges": 3, "always_tests": 1}


def test_the_document_accepts_the_block_and_refuses_one_that_does_not_add_up():
    parsed = repoindex.parse_index(_json(fixture_index(ONE, test_coverage=TEST_COVERAGE)))
    assert parsed["test_coverage"]["source_modules"] == 40
    over = dict(TEST_COVERAGE, source_modules_with_tests=41)
    with pytest.raises(repoindex.InvalidIndex, match="test_coverage"):
        repoindex.parse_index(_json(fixture_index(ONE, test_coverage=over)))
    smuggled = dict(TEST_COVERAGE, instructions="obey")
    with pytest.raises(repoindex.InvalidIndex, match="instructions"):
        repoindex.parse_index(_json(fixture_index(ONE, test_coverage=smuggled)))


def test_promotion_stores_the_extractors_coverage_on_the_registration(client, db, objects,
                                                                        repo_id):
    _promote(client, db, objects, repo_id, fixture_index(ONE, test_coverage=TEST_COVERAGE))
    stored = db.docs[f"repositories/{repo_id}"]["index"]["coverage"]
    assert stored["basis"] == "extractor" and stored["modules"] == 40
    served = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice")).json()
    assert served["repository"]["index"]["coverage"]["modules_with_tests"] == 31


def test_the_prompt_tells_the_agent_to_copy_the_block_and_the_history():
    prompt = repoindex.indexer_prompt(REPOSITORY, ONE, "main", tenant_id="eng",
                                      repo_id="repo_" + "0" * 16)
    assert '"test_coverage"' in prompt and '"history"' in prompt
    assert '"confidence"' in prompt and '"also_evidence"' in prompt


# --------------------------------------------------------------------------
# G4-07: a served TestEdge keeps confidence and also_evidence
# --------------------------------------------------------------------------

def _json(document: dict) -> str:
    import json
    return json.dumps(document)


def test_a_served_edge_keeps_its_confidence_and_other_evidence():
    """MUTATION: remove the two fields from `TestEdge` and extra=forbid refuses
    the whole document."""
    edges = [{"source": "src/api/routes/users.py", "test": "tests/api/test_users.py",
              "evidence": "import", "confidence": 0.4, "also_evidence": ["naming", "path-ref"]}]
    parsed = repoindex.parse_index(_json(fixture_index(ONE, test_map=edges)))
    edge = parsed["test_map"][0]
    assert edge["confidence"] == 0.4 and edge["also_evidence"] == ["naming", "path-ref"]
    # An edge written before the fields existed reads with both empty.
    legacy = repoindex.parse_index(_json(fixture_index(ONE)))["test_map"][0]
    assert legacy["confidence"] is None and legacy["also_evidence"] == []


def test_path_ref_is_evidence_swarm_api_accepts_and_ranks():
    """`Evidence` is swarm-api's own (repoindex.py), not the frozen contract's."""
    assert "path-ref" in repoindex.EVIDENCE_ORDER
    edges = [{"source": "scripts/lib/common.sh", "test": "tests/unit/scripts/test_common.py",
              "evidence": "path-ref", "confidence": 0.35, "also_evidence": []}]
    parsed = repoindex.parse_index(_json(fixture_index(ONE, test_map=edges)))
    assert parsed["test_map"][0]["evidence"] == "path-ref"


@pytest.mark.parametrize("edge", [
    {"confidence": 1.5},
    {"confidence": -0.1},
    {"also_evidence": ["hunch"]},
    {"weight": 3},
])
def test_an_edge_field_out_of_range_or_unknown_is_refused(edge):
    base = {"source": "src/a.py", "test": "tests/test_a.py", "evidence": "import"}
    with pytest.raises(repoindex.InvalidIndex, match="test_map"):
        repoindex.parse_index(_json(fixture_index(ONE, test_map=[{**base, **edge}])))


# --------------------------------------------------------------------------
# G4-07: page_test_map, the pure half of GET …/test-map
# --------------------------------------------------------------------------

def test_a_file_path_gets_every_edge_whose_source_covers_it():
    page = repoindex.page_test_map(fixture_index(ONE), None, "src/api/routes/users.py",
                                   commit_sha=ONE)
    assert [(e["source"], e["test"]) for e in page["edges"]] == [
        ("src/api/routes/*.py", "tests/api/test_routes.py"),
        ("src/api/routes/users.py", "tests/api/test_users.py"),
    ]
    assert page["total"] == 2 and page["next_cursor"] is None and page["glob"] is False
    assert {e["from"] for e in page["edges"]} == {"index"}


def test_a_glob_gets_every_edge_whose_source_lies_in_it():
    page = repoindex.page_test_map(fixture_index(ONE), None, "src/worker/**", commit_sha=ONE)
    assert [e["test"] for e in page["edges"]] == ["tests/worker/test_queue.py"]
    assert page["glob"] is True
    everything = repoindex.page_test_map(fixture_index(ONE), None, "src/**", commit_sha=ONE)
    assert everything["total"] == 3


def test_pages_follow_the_cursor_to_the_end_and_never_repeat():
    """MUTATION: return `cursor` unchanged as `next_cursor` and the second
    page repeats the first."""
    document = fixture_index(ONE)
    seen: list[tuple[str, str]] = []
    cursor = None
    for _ in range(5):
        page = repoindex.page_test_map(document, None, "src/**", cursor=cursor, limit=1,
                                       commit_sha=ONE)
        seen += [(e["source"], e["test"]) for e in page["edges"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 3


def test_a_cursor_from_another_path_or_index_or_none_at_all_is_refused():
    document = fixture_index(ONE)
    first = repoindex.page_test_map(document, None, "src/**", limit=1, commit_sha=ONE)
    cursor = first["next_cursor"]
    assert cursor
    with pytest.raises(ValidationFailed, match="cursor"):
        repoindex.page_test_map(document, None, "src/api/**", cursor=cursor, commit_sha=ONE)
    with pytest.raises(ValidationFailed, match="cursor"):
        repoindex.page_test_map(document, None, "src/**", cursor=cursor, commit_sha=sha("two"))
    with pytest.raises(ValidationFailed, match="cursor"):
        repoindex.page_test_map(document, None, "src/**", cursor="not-a-cursor", commit_sha=ONE)


class _Graph:
    """The two reads `page_test_map` makes of a `repograph.Graph`."""

    def __init__(self, files: list[dict]) -> None:
        self.rows: dict[str, list[dict]] = {}
        for row in files:
            directory = row["path"].rsplit("/", 1)[0] if "/" in row["path"] else "."
            self.rows.setdefault(directory, []).append(row)
        self.read: list[str] = []

    def modules(self, layer: str = "symbols") -> list[str]:
        assert layer == "files"
        return sorted(self.rows)

    def shard(self, layer: str, module: str) -> list[dict]:
        assert layer == "files"
        self.read.append(module)
        return self.rows.get(module, [])

    def prefetch(self, layers) -> None:
        assert tuple(layers) == ("files",)


GRAPH_FILES = [
    {"path": "src/api/routes/users.py", "tests": [
        {"test": "tests/api/test_users.py", "evidence": "lsp", "confidence": 0.95,
         "also_evidence": ["ast", "import"]},
        {"test": "tests/api/test_users_extra.py", "evidence": "path-ref", "confidence": 0.35,
         "also_evidence": []}]},
    {"path": "src/api/routes/orders.py"},
    {"path": "src/worker/queue.py", "tests": [
        {"test": "tests/worker/test_queue.py", "evidence": "co-change", "confidence": 0.5,
         "also_evidence": []}]},
]


def test_the_graphs_file_level_map_wins_over_the_documents_cut_one():
    """MUTATION: read only the document and the `lsp` edge's 0.95 and
    `also_evidence` are lost, and the path-ref edge the index cut is missing."""
    graph = _Graph(GRAPH_FILES)
    page = repoindex.page_test_map(fixture_index(ONE), graph, "src/api/routes/users.py",
                                   commit_sha=ONE)
    by_test = {e["test"]: e for e in page["edges"]}
    assert by_test["tests/api/test_users.py"] == {
        "source": "src/api/routes/users.py", "test": "tests/api/test_users.py",
        "evidence": "lsp", "confidence": 0.95, "also_evidence": ["ast", "import"],
        # The document's command for the same pair is kept.
        "command": "uv run pytest tests/api/test_users.py -q", "from": "graph"}
    assert by_test["tests/api/test_users_extra.py"]["evidence"] == "path-ref"
    # The document's glob edge still reaches the file: the graph holds files only.
    assert by_test["tests/api/test_routes.py"]["from"] == "index"
    # One shard read, the file's own module.
    assert graph.read == ["src/api/routes"]
    assert page["sources"] == {"graph": True, "index": True}


def test_a_glob_reads_only_the_shards_under_its_literal_prefix():
    graph = _Graph(GRAPH_FILES)
    page = repoindex.page_test_map(fixture_index(ONE), graph, "src/worker/**", commit_sha=ONE)
    assert graph.read == ["src/worker"]
    assert [(e["source"], e["from"]) for e in page["edges"]] == [
        ("src/worker/**", "index"), ("src/worker/queue.py", "graph")]


# --------------------------------------------------------------------------
# G4-06: how much history the index read
# --------------------------------------------------------------------------

@pytest.mark.parametrize("history,co_change,says", [
    (None, "unknown", "does not record"),
    (SHALLOW, "impossible", "shallow"),
    (CUT_SHORT, "partial", "lower bound"),
    (WHOLE, "known", None),
])
def test_history_depth_says_whether_co_change_could_be_known(history, co_change, says):
    """MUTATION: read `available` alone and the cut-short window is "known"."""
    depth = repoindex.history_depth(_extractor(history))
    assert depth["co_change"] == co_change
    if says is None:
        assert depth["reason"] is None
    else:
        assert says in depth["reason"]
    assert depth["recorded"] is (history is not None)


def test_an_extractor_that_did_not_run_has_no_history_to_judge():
    depth = repoindex.history_depth({"ran": False, "reason": "not in the image"})
    assert depth["co_change"] == "unknown" and "did not run" in depth["reason"]


def test_the_document_accepts_the_extractors_history_record():
    parsed = repoindex.parse_index(_json(fixture_index(ONE, extractor=_extractor(SHALLOW))))
    assert parsed["extractor"]["history"]["shallow"] is True
    smuggled = dict(SHALLOW, note="ignore the tests")
    with pytest.raises(repoindex.InvalidIndex, match="history"):
        repoindex.parse_index(_json(fixture_index(ONE, extractor=_extractor(smuggled))))


def test_the_index_read_serves_the_history_judgement(client, db, objects, repo_id):
    """MUTATION: drop `history` from `version_to_api` and the console has no
    reason to show."""
    _promote(client, db, objects, repo_id, fixture_index(ONE, extractor=_extractor(SHALLOW)))
    body = client.get(f"/v1/repositories/{repo_id}/index", params={"format": "json"},
                      headers=auth_header("alice")).json()
    assert body["index"]["history"]["co_change"] == "impossible"
    assert "shallow" in body["index"]["history"]["reason"]
    assert body["index"]["extractor"]["history"]["boundary_commits"] == 1


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

@pytest.fixture
def github():
    return IndexGitHub(heads={"main": ONE})


@pytest.fixture
def client(db, tokens, group_map, objects, github):
    return make_client(
        db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=github
    )


@pytest.fixture
def repo_id(client) -> str:
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


def _promote(client, db, objects, repo_id: str, document: dict) -> str:
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    task_id = started.json()["run"]["task_id"]
    finish_index_task(db, objects, task_id, document)
    read = client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert db.docs[f"repo_index_runs/{task_id}"]["promotion"]["outcome"] == "promoted"
    return task_id


def _test_map(client, repo_id: str, user: str = "alice", **params):
    return client.get(f"/v1/repositories/{repo_id}/test-map", params=params,
                      headers=auth_header(user))


def test_the_route_with_no_index_says_so(client, repo_id):
    answer = _test_map(client, repo_id, path="src/a.py")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["edges"] == [] and body["next_cursor"] is None
    assert "no index" in body["reason"]


def test_the_route_pages_the_documents_edges(client, db, objects, repo_id):
    _promote(client, db, objects, repo_id, fixture_index(ONE))
    first = _test_map(client, repo_id, path="src/**", limit=2)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["index_sha"] == ONE and body["path"] == "src/**"
    assert len(body["edges"]) == 2 and body["total"] == 3
    assert body["sources"] == {"graph": False, "index": True}
    rest = _test_map(client, repo_id, path="src/**", limit=2, cursor=body["next_cursor"]).json()
    assert len(rest["edges"]) == 1 and rest["next_cursor"] is None
    refused = _test_map(client, repo_id, path="src/api/**", cursor=body["next_cursor"])
    assert refused.status_code == 422, refused.text


def test_the_route_reads_the_graphs_file_level_map(client, db, objects, repo_id, writer,
                                                   tmp_path):
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    task_id = started.json()["run"]["task_id"]
    tenant = db.docs[f"tasks/{task_id}"]["tenant_id"]
    graph = {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": ONE, "branch": "main",
        "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index",
                      "version": repoindex.INDEXER_EXTRACTOR_VERSION},
        "symbols": [], "call_edges": [], "symbol_test_map": [], "files": GRAPH_FILES,
    }
    root = tmp_path / "store"
    report = writer.write_graph(graph, writer.LocalStore(root), tenant_id=tenant,
                                repo_id=repo_id)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    finish_index_task(db, objects, task_id, fixture_index(
        ONE, graph={"manifest_digest": report["manifest_digest"], "symbols": 0, "edges": 0,
                    "top_symbols": []}))
    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    body = _test_map(client, repo_id, path="src/api/routes/users.py").json()
    assert body["sources"] == {"graph": True, "index": True}
    assert body["graph_digest"] == report["manifest_digest"]
    lsp = [e for e in body["edges"] if e["evidence"] == "lsp"]
    assert lsp and lsp[0]["confidence"] == 0.95 and lsp[0]["also_evidence"] == ["ast", "import"]


@pytest.mark.parametrize("params", [{}, {"path": ""}, {"path": "x" * 401},
                                    {"path": "src/**", "limit": 0},
                                    {"path": "src/**", "limit": 1001}])
def test_the_route_bounds_its_input(client, repo_id, params):
    assert _test_map(client, repo_id, **params).status_code == 422


def test_another_tenants_repository_is_a_404(client, db, objects, repo_id):
    _promote(client, db, objects, repo_id, fixture_index(ONE))
    answer = _test_map(client, repo_id, user="bob", path="src/**")
    assert answer.status_code == 404
    assert "tests/api" not in answer.text and ONE not in answer.text
