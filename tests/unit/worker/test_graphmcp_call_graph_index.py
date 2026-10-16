"""`call_graph` and `index`, the two tools the shared graph service adds
(docs/design/graph-service.md §2.3, lane GS-CORE).

`call_graph` lists call edges `{from, to, kind, depth, confidence}`
breadth-first from a symbol or every symbol of a file, bounded like `impact`,
with zero callers said as UNKNOWN. `index` pages any layer: its keys with
their row counts, or one shard's rows in stored order -- the whole index page
by page -- and `document` is one of its layers.

Over the hand-written snapshot of test_graphmcp_source.py, so every edge and
row below is one that file wrote.
"""

from __future__ import annotations

import json

import pytest
from test_graphmcp_source import (
    APP,
    CLI,
    COMMIT,
    DOCUMENT,
    STORE,
    TEST,
    write_hand_snapshot,
)

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import DIRTY, Observation
from agent_worker.graphmcp.snapshot import ALL_LAYERS, DOCUMENT_LAYER, Snapshot

HANDLE = f"{APP}#handle"
VALIDATE = f"{APP}#validate"
FETCH = f"{STORE}#fetch_row"
SAVE = f"{STORE}#save_row"
MAIN = f"{CLI}#main"
TEST_HANDLE = f"{TEST}#test_handle"


@pytest.fixture
def snap(tmp_path) -> Snapshot:
    return Snapshot(write_hand_snapshot(tmp_path / "s"))


def ask(snap: Snapshot, tool: str, arguments: dict,
        obs: Observation | None = None) -> tuple[dict, bool]:
    text, failed = tools.call(snap, obs or Observation.empty(COMMIT), tool, arguments)
    assert len(text.encode()) <= tools.ANSWER_CAP
    answer = json.loads(text)
    assert next(iter(answer)) == "freshness", answer
    return answer, failed


def edges(answer: dict) -> list[tuple[str, str, int]]:
    return [(r["from"], r["to"], r["depth"]) for r in answer["rows"]]


def dirty(*paths: str) -> Observation:
    return Observation(block={"index_sha": COMMIT, "dirty": len(paths)},
                       changed=frozenset(), dirty=frozenset(paths))


# --- the schemas -------------------------------------------------------------

def test_both_tools_are_listed_and_served():
    assert tools.TOOL_NAMES[-2:] == ("call_graph", "index")
    assert set(tools.HANDLERS) == set(tools.TOOL_NAMES)
    schema = {t["name"]: t["inputSchema"] for t in tools.TOOLS}
    assert schema["call_graph"]["required"] == ["symbol"]
    assert schema["call_graph"]["properties"]["direction"]["enum"] == ["callers", "callees",
                                                                       "both"]
    assert schema["index"]["required"] == ["layer"]
    assert schema["index"]["properties"]["layer"]["enum"] == list(ALL_LAYERS)
    assert "document" in schema["index"]["properties"]["layer"]["enum"]


# --- call_graph --------------------------------------------------------------

def test_call_graph_walks_callers_then_callees_breadth_first(snap):
    answer, failed = ask(snap, "call_graph", {"symbol": HANDLE})
    assert not failed, answer
    assert edges(answer) == [(MAIN, HANDLE, 1), (TEST_HANDLE, HANDLE, 1),
                             (HANDLE, VALIDATE, 1), (HANDLE, FETCH, 1),
                             (VALIDATE, SAVE, 2)]
    assert answer["rows"][0] == {"from": MAIN, "to": HANDLE, "kind": "call", "depth": 1,
                                 "confidence": 1.0}
    assert (answer["direction"], answer["depth"]) == ("both", 2)
    assert (answer["nodes"], answer["edges"], answer["truncated"]) == (6, 5, False)
    assert "note" not in answer


def test_call_graph_takes_a_direction_and_a_depth(snap):
    callers, _ = ask(snap, "call_graph", {"symbol": SAVE, "direction": "callers", "depth": 3})
    assert edges(callers) == [(VALIDATE, SAVE, 1), (HANDLE, VALIDATE, 2), (MAIN, HANDLE, 3),
                              (TEST_HANDLE, HANDLE, 3)]
    shallow, _ = ask(snap, "call_graph", {"symbol": SAVE, "direction": "callers", "depth": 1})
    assert edges(shallow) == [(VALIDATE, SAVE, 1)]
    callees, _ = ask(snap, "call_graph", {"symbol": MAIN, "direction": "callees", "depth": 3})
    assert edges(callees) == [(MAIN, HANDLE, 1), (HANDLE, VALIDATE, 2), (HANDLE, FETCH, 2),
                              (VALIDATE, SAVE, 3)]


def test_call_graph_of_a_file_starts_from_every_symbol_it_defines(snap):
    answer, failed = ask(snap, "call_graph", {"symbol": STORE, "direction": "callers"})
    assert not failed, answer
    assert (answer["file"], answer["symbols"]) == (STORE, 2)
    assert edges(answer) == [(HANDLE, FETCH, 1), (VALIDATE, SAVE, 1),
                             (MAIN, HANDLE, 2), (TEST_HANDLE, HANDLE, 2), (HANDLE, VALIDATE, 2)]
    # An import is not a call.
    assert all(r["kind"] != "import" for r in answer["rows"])


def test_zero_callers_is_unknown_never_safe(snap):
    answer, _ = ask(snap, "call_graph", {"symbol": MAIN})
    assert answer["callers"] == "UNKNOWN"
    assert answer["note"] == tools.UNKNOWN_CALLERS
    # Asking only for callees makes no claim about callers.
    callees, _ = ask(snap, "call_graph", {"symbol": FETCH, "direction": "callees"})
    assert callees["edges"] == 0 and "callers" not in callees


def test_call_graph_is_bounded_like_impact(snap, monkeypatch):
    monkeypatch.setattr(tools, "IMPACT_MAX_NODES", 1)
    answer, _ = ask(snap, "call_graph", {"symbol": SAVE, "direction": "callers", "depth": 3})
    # The first level is always read whole; the walk stops past the bound.
    assert edges(answer) == [(VALIDATE, SAVE, 1)]
    assert answer["truncated"] is True


def test_call_graph_marks_an_edge_stale_when_either_end_is(snap):
    answer, _ = ask(snap, "call_graph", {"symbol": HANDLE, "direction": "callers"},
                    obs=dirty(CLI))
    marks = {(r["from"], r["to"]): r.get("stale") for r in answer["rows"]}
    assert marks == {(MAIN, HANDLE): DIRTY, (TEST_HANDLE, HANDLE): None}
    assert "stale" not in answer
    head, _ = ask(snap, "call_graph", {"symbol": HANDLE}, obs=dirty(APP))
    assert head["stale"] == DIRTY
    assert all(r.get("stale") == DIRTY for r in head["rows"])


@pytest.mark.parametrize("arguments, message", [
    ({"symbol": HANDLE, "direction": "up"}, "direction must be one of"),
    ({"symbol": HANDLE, "depth": 4}, "depth must be an integer from 1 to 3"),
    ({"symbol": HANDLE, "colour": "red"}, "unknown argument(s): colour"),
    ({}, "missing argument(s): symbol"),
    ({"symbol": f"{APP}#gone"}, "no symbol"),
])
def test_call_graph_bad_arguments_are_an_error_answer(snap, arguments, message):
    answer, failed = ask(snap, "call_graph", arguments)
    assert failed
    assert message in answer["error"]


# --- index -------------------------------------------------------------------

def test_index_without_a_key_lists_the_layers_keys_with_row_counts(snap):
    answer, failed = ask(snap, "index", {"layer": "symbols"})
    assert not failed, answer
    assert answer["rows"] == [{"key": "src/app", "rows": 4}, {"key": "src/cli", "rows": 1},
                              {"key": "tests", "rows": 1}]
    assert (answer["layer"], answer["keys"]) == ("symbols", 3)


def test_index_with_a_key_gives_that_shards_rows_in_stored_order(snap):
    answer, _ = ask(snap, "index", {"layer": "symbols", "key": "src/app"})
    assert [r["id"] for r in answer["rows"]] == [HANDLE, VALIDATE, FETCH, SAVE]
    assert (answer["layer"], answer["key"]) == ("symbols", "src/app")


def test_index_with_a_path_reads_the_module_that_holds_it(snap):
    answer, _ = ask(snap, "index", {"layer": "files", "path": STORE})
    assert (answer["path"], answer["key"]) == (STORE, "src/app")
    assert [r["path"] for r in answer["rows"]] == [APP, STORE]


def test_index_reads_the_document_layer_by_section(snap):
    listed, _ = ask(snap, "index", {"layer": DOCUMENT_LAYER})
    assert listed["rows"] == [{"key": "commands", "rows": 1}, {"key": "modules", "rows": 2}]
    commands, _ = ask(snap, "index", {"layer": DOCUMENT_LAYER, "key": "commands"})
    assert commands["rows"] == DOCUMENT["commands"]


def test_a_layer_the_snapshot_lacks_is_said_never_an_empty_list(tmp_path):
    snap = Snapshot(write_hand_snapshot(tmp_path / "s", document=False))
    listed, failed = ask(snap, "index", {"layer": DOCUMENT_LAYER})
    assert not failed
    assert listed["keys"] == 0 and "no document layer" in listed["note"]
    keyed, failed = ask(snap, "index", {"layer": DOCUMENT_LAYER, "key": "modules"})
    assert failed and "no document layer" in keyed["error"]


def test_index_rows_are_marked_stale_by_path_symbol_or_edge(snap):
    files, _ = ask(snap, "index", {"layer": "files", "key": "src/app"}, obs=dirty(STORE))
    assert [r.get("stale") for r in files["rows"]] == [None, DIRTY]
    callers, _ = ask(snap, "index", {"layer": "callers", "key": "src/app"}, obs=dirty(CLI))
    stale = [(r["from"], r["to"]) for r in callers["rows"] if r.get("stale")]
    assert stale == [(MAIN, HANDLE)]


@pytest.mark.parametrize("arguments, message", [
    ({"layer": "everything"}, "layer must be one of"),
    ({"layer": "symbols", "key": "src/app", "path": APP}, "key or path, not both"),
    ({"layer": "symbols", "key": "src/nowhere"}, "has no shard 'src/nowhere'"),
    ({"layer": DOCUMENT_LAYER, "path": APP}, "keyed by section"),
    ({"layer": "files", "path": "../outside.py"}, "not a repository-relative path"),
])
def test_index_bad_arguments_are_an_error_answer(snap, arguments, message):
    answer, failed = ask(snap, "index", arguments)
    assert failed
    assert message in answer["error"]


def test_the_whole_index_is_readable_page_by_page(snap, monkeypatch):
    monkeypatch.setattr(tools, "ANSWER_CAP", 400)
    read = expected = pages = shards = 0
    for layer in ALL_LAYERS:
        for key in snap.keys(layer):
            shards += 1
            expected += snap.entry(layer, key)["records"]
            rows: list[dict] = []
            cursor = None
            while True:
                arguments = {"layer": layer, "key": key}
                if cursor is not None:
                    arguments["cursor"] = cursor
                answer, failed = ask(snap, "index", arguments)
                assert not failed, answer
                pages += 1
                rows += answer["rows"]
                cursor = answer["next_cursor"]
                if cursor is None:
                    break
            # Every row exactly once, in stored order, none omitted.
            assert rows == snap.shard(layer, key).rows, (layer, key)
            read += len(rows)
    assert read == expected > 20
    assert pages > shards, "the cap never made a shard page"
