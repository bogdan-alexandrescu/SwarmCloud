"""swarm-graph's tools, transport, freshness and paging on a small extracted repository
(docs/design/knowledge-graph.md §4.2, §5.2, §7.6, §7.7; lane KG4).

The fixture repository is `repo_index_fixtures.PYTHON_APP` (a route calling
`load_user`, which calls `store.fetch` and `normalise`, with tests), the
ambiguous `render` pair, and one function nothing calls. Its snapshot is built
by KG2's extractor and writer, so every assertion is against the format the
indexer writes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import graphmcp_fixtures as gf
import pytest
import repo_index_fixtures as fx

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.__main__ import main
from agent_worker.graphmcp.freshness import CHANGED, DIRTY, Freshness
from agent_worker.graphmcp.snapshot import Snapshot

FILES = {
    **fx.PYTHON_APP,
    **fx.PYTHON_AMBIGUOUS,
    "src/pkg/orphan.py": "def never_called():\n    return 0\n",
}
LOAD_USER = "src/pkg/users.py#load_user"
GET_USER = "src/pkg/users.py#get_user"
FETCH = "src/pkg/store.py#fetch"
TEST_LOAD = "tests/test_users.py#test_load_user"


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, Path, dict]:
    """(the committed repository, its snapshot, its graph document)."""
    root = tmp_path_factory.mktemp("graphmcp")
    repo = fx.build_repo(root / "repo", FILES)
    document = gf.graph_of(repo)
    return repo, gf.write_snapshot(document, root / "snapshot"), document


@pytest.fixture
def repo(built, tmp_path) -> Path:
    """A copy of the repository this test may dirty."""
    return Path(shutil.copytree(built[0], tmp_path / "repo", symlinks=True))


@pytest.fixture
def snapshot(built, tmp_path) -> Path:
    """A copy of the snapshot this test may rewrite."""
    return Path(shutil.copytree(built[1], tmp_path / "snapshot"))


def ask(snapshot: Path, tool: str, arguments: dict, *, workdir: Path | None = None,
        record: dict | None = None) -> tuple[dict, bool]:
    """One answer, in process: the stdio path is covered by its own tests."""
    snap = Snapshot(snapshot)
    fresh = Freshness.establish(snap.commit_sha, workdir=workdir, record=record)
    text, failed = tools.call(snap, fresh.observe(), tool, arguments)
    assert len(text.encode()) <= tools.ANSWER_CAP
    answer = json.loads(text)
    assert next(iter(answer)) == "freshness", answer
    return answer, failed


def rows(answer: dict, rel: str | None = None) -> list[dict]:
    return [r for r in answer["rows"] if rel is None or r.get("rel") == rel]


# --- the transport ------------------------------------------------------------

def test_the_server_speaks_mcp_over_stdio_with_the_six_tools(built):
    repo, snapshot, document = built
    graph = gf.StdioServer(snapshot, workdir=repo)
    try:
        init = graph.request("initialize", {"protocolVersion": "2025-03-26",
                                            "capabilities": {}, "clientInfo": {"name": "t"}})
        assert init["result"]["protocolVersion"] == "2025-03-26"
        assert init["result"]["serverInfo"]["name"] == "swarm-graph"
        assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
        # A notification is not answered: the next line is the ping's.
        graph.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert graph.request("ping")["result"] == {}
        listed = graph.request("tools/list")["result"]["tools"]
        assert [t["name"] for t in listed] == ["search", "context", "impact", "tests_for",
                                               "neighbours", "territory"]
        text, failed = graph.call("context", {"symbol": LOAD_USER})
        assert not failed
        assert json.loads(text)["freshness"]["index_sha"] == document["commit_sha"]
    finally:
        graph.close()


def test_an_unknown_method_and_a_line_that_is_not_json_do_not_end_the_session(built):
    _repo, snapshot, _document = built
    graph = gf.StdioServer(snapshot)
    try:
        assert graph.request("resources/list")["error"]["code"] == -32601
        assert graph.proc.stdin is not None and graph.proc.stdout is not None
        graph.proc.stdin.write(b"{not json\n")
        graph.proc.stdin.flush()
        assert json.loads(graph.proc.stdout.readline())["error"]["code"] == -32700
        assert graph.request("ping")["result"] == {}
    finally:
        graph.close()


def test_an_initialize_with_an_unknown_revision_is_answered_with_the_newest(built):
    graph = gf.StdioServer(built[1])
    try:
        init = graph.request("initialize", {"protocolVersion": "1999-01-01"})
        assert init["result"]["protocolVersion"] == "2025-06-18"
    finally:
        graph.close()


# --- the tools ----------------------------------------------------------------

def test_search_ranks_the_symbol_named_by_the_query_first(snapshot):
    answer, failed = ask(snapshot, "search", {"query": "load user"})
    assert not failed
    assert answer["rows"][0]["symbol"] == LOAD_USER
    assert answer["rows"][0]["kind"] == "function" and answer["rows"][0]["line"] == 8


def test_context_gives_the_definition_callers_callees_and_tests(snapshot):
    answer, failed = ask(snapshot, "context", {"symbol": "load_user"})
    assert not failed
    assert answer["symbol"] == LOAD_USER
    assert answer["lines"] == [8, 10]
    assert answer["signature"] == "(user_id)"
    # A test that calls it directly is a caller too, as well as a covering test.
    assert {r["symbol"] for r in rows(answer, "caller")} == {GET_USER, TEST_LOAD}
    assert {r["symbol"] for r in rows(answer, "callee")} >= {FETCH,
                                                            "src/pkg/users.py#normalise"}
    assert TEST_LOAD in {r["test"] for r in rows(answer, "test")}


def test_zero_resolved_callers_is_unknown_never_safe(snapshot):
    """§7.6: a dynamic call is invisible, so no callers is not "safe"."""
    context, _ = ask(snapshot, "context", {"symbol": "never_called"})
    assert context["callers"] == "UNKNOWN" and "never safe" in context["note"]
    impact, _ = ask(snapshot, "impact", {"symbol": "never_called"})
    assert impact["callers"] == "UNKNOWN" and impact["fan_in"] == 0


def test_impact_walks_callers_to_the_depth_asked_and_names_the_tests(snapshot):
    deep, _ = ask(snapshot, "impact", {"symbol": FETCH, "depth": 3})
    callers = {r["symbol"]: r["depth"] for r in rows(deep, "caller")}
    assert callers[LOAD_USER] == 1 and callers[GET_USER] == 2
    assert deep["fan_in"] == 1 and deep["upstream"] == len(callers)
    assert TEST_LOAD in {r["test"] for r in rows(deep, "test")}
    assert "src/pkg" in {r["module"] for r in rows(deep, "module")}
    shallow, _ = ask(snapshot, "impact", {"symbol": FETCH, "depth": 1})
    assert [r["symbol"] for r in rows(shallow, "caller")] == [LOAD_USER]


def test_impact_of_a_file_starts_from_every_symbol_it_defines(snapshot):
    answer, failed = ask(snapshot, "impact", {"symbol": "src/pkg/store.py"})
    assert not failed
    assert answer["file"] == "src/pkg/store.py" and answer["symbols"] == 2
    assert LOAD_USER in {r["symbol"] for r in rows(answer, "caller")}


def test_tests_for_a_file_names_its_symbols_tests_and_its_file_level_tests(snapshot):
    answer, failed = ask(snapshot, "tests_for", {"files": ["src/pkg/store.py", "nowhere.py"],
                                                 "symbols": ["load_user"]})
    assert not failed
    found = {r["test"] for r in answer["rows"]}
    assert TEST_LOAD in found
    assert "tests/test_store.py" in found  # the naming-only file-level edge
    assert answer["unknown"] == ["nowhere.py"]
    confidences = [r["confidence"] for r in answer["rows"]]
    assert confidences == sorted(confidences, reverse=True)


def test_neighbours_are_same_kind_symbols_of_the_file_and_community_and_test_files(snapshot):
    answer, failed = ask(snapshot, "neighbours", {"symbol": LOAD_USER})
    assert not failed
    same_file = {r["symbol"] for r in rows(answer, "same_file")}
    assert "src/pkg/users.py#normalise" in same_file and LOAD_USER not in same_file
    assert FETCH in {r["symbol"] for r in rows(answer, "same_community")}
    assert {r["path"] for r in rows(answer, "test_file")} == {"tests/test_users.py"}


def test_territory_gives_community_fan_in_and_dependants_and_says_what_it_cannot(snapshot):
    answer, failed = ask(snapshot, "territory", {"files": ["src/pkg/store.py", "gone.py"]})
    assert not failed
    store = next(r for r in rows(answer, "file") if r["path"] == "src/pkg/store.py")
    assert store["fan_in"] >= 1 and store["community"] is not None
    assert {"rel": "dependant", "path": "src/pkg/users.py", "of": "src/pkg/store.py"} in \
        answer["rows"]
    assert {"rel": "file", "path": "gone.py", "known": False} in answer["rows"]
    assert answer["in_flight"] == tools.IN_FLIGHT


def test_an_ambiguous_name_lists_the_candidates_instead_of_guessing(snapshot):
    answer, failed = ask(snapshot, "context", {"symbol": "render"})
    assert failed
    assert set(answer["candidates"]) == {"lib/a.py#render", "lib/b.py#render"}


@pytest.mark.parametrize("tool, arguments", [
    ("context", {"symbol": LOAD_USER, "write": True}),
    ("impact", {"symbol": FETCH, "depth": 9}),
    ("tests_for", {}),
    ("territory", {"files": ["../outside.py"]}),
    ("search", {"query": "user", "cursor": "-1"}),
    ("delete", {}),
])
def test_bad_arguments_are_an_error_answer_with_freshness_first(snapshot, tool, arguments):
    answer, failed = ask(snapshot, tool, arguments)
    assert failed and answer["error"]


# --- the 4 KiB cap and paging ------------------------------------------------

def test_pages_under_a_small_cap_cover_every_row_exactly_once(snapshot, monkeypatch):
    whole, _ = ask(snapshot, "neighbours", {"symbol": LOAD_USER})
    assert whole["next_cursor"] is None and whole["total"] >= 3
    # Room for the head and the largest row, so no page holds them all.
    shell = len(tools.dumps({**whole, "rows": [], "next_cursor": str(whole["total"])}))
    cap = shell + max(len(tools.dumps(r)) for r in whole["rows"]) + 1
    monkeypatch.setattr(tools, "ANSWER_CAP", cap)
    seen: list[dict] = []
    cursor = None
    pages = 0
    while True:
        arguments = {"symbol": LOAD_USER, **({"cursor": cursor} if cursor else {})}
        text, failed = tools.call(Snapshot(snapshot), Freshness.establish(
            whole["freshness"]["index_sha"], workdir=None, record=None).observe(),
            "neighbours", arguments)
        assert not failed and len(text.encode()) <= cap
        answer = json.loads(text)
        assert answer["total"] == whole["total"]
        seen += answer["rows"]
        pages += 1
        cursor = answer["next_cursor"]
        if cursor is None:
            break
    assert pages > 1
    assert seen == whole["rows"]


def test_an_answer_is_never_over_four_kibibytes(snapshot, monkeypatch):
    """At the real cap: a head padded to leave room for one row pages the rest."""
    files = {"files": ["src/pkg/store.py", "src/pkg/users.py"]}
    whole, _ = ask(snapshot, "territory", files)
    assert whole["total"] >= 3 and whole["next_cursor"] is None
    shell = len(tools.dumps({**whole, "rows": [], "next_cursor": str(whole["total"])}))
    room = max(len(tools.dumps(r)) for r in whole["rows"]) + 1
    monkeypatch.setattr(tools, "IN_FLIGHT",
                        tools.IN_FLIGHT + "x" * (tools.ANSWER_CAP - shell - room))
    answer, failed = ask(snapshot, "territory", files)
    assert not failed
    assert answer["next_cursor"] is not None and 1 <= len(answer["rows"]) < whole["total"]
    assert len(tools.dumps(answer)) <= tools.ANSWER_CAP


# --- freshness ----------------------------------------------------------------

def test_rows_on_a_path_dirtied_in_this_step_are_stale(repo, snapshot):
    index = repo / ".git" / "index"
    before = index.read_bytes()
    (repo / "src/pkg/users.py").write_text(FILES["src/pkg/users.py"] + "\n# edited\n")
    answer, _ = ask(snapshot, "context", {"symbol": FETCH}, workdir=repo)
    assert answer["freshness"]["dirty"] == 1
    assert answer["freshness"]["behind_by"] == 0
    load_user = next(r for r in rows(answer, "caller") if r["symbol"] == LOAD_USER)
    assert load_user["stale"] == DIRTY
    assert "stale" not in answer  # store.py itself is untouched
    # Read-only: the agent's own index is not refreshed or rewritten by us.
    assert index.read_bytes() == before


def test_a_new_untracked_file_counts_as_dirty(repo, snapshot):
    (repo / "src/pkg/new.py").write_text("def made_here():\n    return 1\n")
    answer, _ = ask(snapshot, "territory", {"files": ["src/pkg/new.py"]}, workdir=repo)
    assert answer["freshness"]["dirty"] == 1
    assert answer["rows"][0]["stale"] == DIRTY


def test_a_staged_record_marks_paths_changed_since_the_index(built, snapshot):
    document = built[2]
    record = {"index_sha": document["commit_sha"], "base_sha": "f" * 40, "behind_by": 3,
              "changed": ["src/pkg/store.py"]}
    answer, _ = ask(snapshot, "context", {"symbol": FETCH}, record=record)
    assert answer["freshness"]["behind_by"] == 3
    assert answer["freshness"]["base_sha"] == "f" * 40
    assert answer["freshness"]["changed_since_index"] == 1
    assert answer["stale"] == CHANGED


def test_a_record_for_another_index_is_ignored_and_said(snapshot):
    answer, _ = ask(snapshot, "context", {"symbol": FETCH},
                    record={"index_sha": "0" * 40, "changed": ["src/pkg/store.py"]})
    assert "another index commit" in answer["freshness"]["note"]
    assert answer["freshness"]["changed_since_index"] == "unknown"
    assert "stale" not in answer


def test_without_a_record_or_a_checkout_staleness_is_unknown_not_zero(snapshot):
    answer, _ = ask(snapshot, "search", {"query": "fetch"})
    assert answer["freshness"]["changed_since_index"] == "unknown"
    assert answer["freshness"]["dirty"] == "unknown"
    assert "staleness unknown" in answer["freshness"]["note"]


# --- what it refuses ----------------------------------------------------------

def test_a_blob_that_does_not_match_its_name_is_refused(snapshot):
    manifest = json.loads((snapshot / "manifest.json").read_text())
    digest = manifest["shards"]["symbols"]["src/pkg"]["blob"].split(":", 1)[1]
    blob = snapshot / "blobs" / f"{digest}.jsonl.gz"
    blob.write_bytes(blob.read_bytes() + b"tampered")
    answer, failed = ask(snapshot, "context", {"symbol": FETCH})
    assert failed and "does not match its name" in answer["error"]


def test_a_manifest_the_stager_recorded_another_digest_for_does_not_start(snapshot, capsys):
    (snapshot / "freshness.json").write_text(json.dumps({"manifest_digest": "sha256:" + "0" * 64}))
    assert main(["--snapshot", str(snapshot)]) == 2
    assert "does not match the digest" in capsys.readouterr().err


def test_a_missing_snapshot_does_not_start(tmp_path, capsys):
    assert main(["--snapshot", str(tmp_path / "nothing")]) == 2
    assert "no manifest" in capsys.readouterr().err


def test_a_format_two_snapshot_answers_everything_but_search(snapshot):
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest.pop("format_version")
    manifest.pop("index_shards")
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    answer, failed = ask(snapshot, "search", {"query": "load user"})
    assert failed and "no term index" in answer["error"]
    answer, failed = ask(snapshot, "context", {"symbol": LOAD_USER})
    assert not failed and answer["community"] is None


def test_a_term_index_from_another_tokenizer_is_not_searched(snapshot):
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["term_stats"]["tokenizer"] = "2"
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    answer, failed = ask(snapshot, "search", {"query": "load user"})
    assert failed and "tokenizer" in answer["error"]
