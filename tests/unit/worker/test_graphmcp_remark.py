"""The compute/page split and `remark` (docs/design/graph-service.md §2.4 seams 2-3, §4.4).

The shared service computes every answer under the EMPTY observation, so one
answer serves every agent; the pod's shim then calls `tools.remark` with its
own observation. The property held here, for every tool over two snapshots
(KG2's extract of the KG4 fixture repository, and the hand-written one with
the `document` layer) across random dirty and changed sets and several answer
caps: the pages the shim returns, cursor by cursor, are byte for byte the
pages the local server returns. So the concatenated pages are the same rows,
in the same order, with the same marks.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import graphmcp_fixtures as gf
import pytest
import repo_index_fixtures as fx
from test_graphmcp_source import write_hand_snapshot

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import CHANGED, DIRTY, Observation
from agent_worker.graphmcp.snapshot import ALL_LAYERS, Snapshot

FILES = {
    **fx.PYTHON_APP,
    **fx.PYTHON_AMBIGUOUS,
    "src/pkg/orphan.py": "def never_called():\n    return 0\n",
}
SEEDS = range(6)
CAPS = (520, 900, 4096)


@pytest.fixture(scope="module")
def snapshots(tmp_path_factory) -> dict[str, Snapshot]:
    root = tmp_path_factory.mktemp("remark")
    repo = fx.build_repo(root / "repo", FILES)
    extracted = gf.write_snapshot(gf.graph_of(repo), root / "extracted")
    return {"extracted": Snapshot(extracted),
            "hand": Snapshot(write_hand_snapshot(root / "hand"))}


def _all(snap: Snapshot, layer: str, field: str) -> list[str]:
    return sorted({str(r[field]) for key in snap.keys(layer)
                   for r in snap.shard(layer, key).rows if r.get(field)})


def battery(snap: Snapshot) -> list[tuple[str, dict]]:
    symbols = [s for s in _all(snap, "symbols", "id") if "@" not in s]
    paths = _all(snap, "files", "path")
    questions: list[tuple[str, dict]] = [
        ("search", {"query": "load user"}), ("search", {"query": "fetch row"}),
        ("context", {"symbol": "render"}), ("context", {"symbol": "src/nowhere.py#gone"}),
        ("nope", {}), ("impact", {"symbol": "x", "depth": 9}),
    ]
    for symbol in symbols:
        questions += [("context", {"symbol": symbol}),
                      ("impact", {"symbol": symbol, "depth": 3}),
                      ("neighbours", {"symbol": symbol}),
                      ("tests_for", {"symbols": [symbol]}),
                      ("similar_changes", {"symbols": [symbol]}),
                      ("call_graph", {"symbol": symbol, "depth": 3})]
    for path in paths:
        questions += [("impact", {"symbol": path}), ("territory", {"files": [path]}),
                      ("rules_for", {"files": [path]}), ("tests_for", {"files": [path]}),
                      ("call_graph", {"symbol": path, "direction": "callers"}),
                      ("index", {"layer": "symbols", "path": path})]
    questions.append(("territory", {"files": paths[:12]}))
    for layer in ALL_LAYERS:
        questions.append(("index", {"layer": layer}))
        questions += [("index", {"layer": layer, "key": key}) for key in snap.keys(layer)]
    return questions


def observation(snap: Snapshot, seed: int) -> Observation:
    """A random observation shaped as `Freshness.observe` shapes one."""
    rng = random.Random(seed)
    paths = _all(snap, "files", "path")
    dirty = {p for p in paths if rng.random() < 0.3}
    changed = {p for p in paths if rng.random() < 0.3}
    dirs = tuple(sorted({p.rsplit("/", 1)[0] + "/" for p in paths if "/" in p
                         and rng.random() < 0.1}))
    block = {"index_sha": snap.commit_sha, "base_sha": "b" * 40, "behind_by": rng.randint(0, 9),
             "changed_since_index": len(changed), "dirty": len(dirty) + len(dirs)}
    return Observation(block=block, changed=frozenset(changed), dirty=frozenset(dirty),
                       dirty_dirs=dirs)


def pages(snap: Snapshot, tool: str, arguments: dict, obs: Observation, *,
          shim: bool) -> list[tuple[str, bool]]:
    """Every page of one question, following next_cursor: from the local
    server, or as the shim returns them (service under the empty observation,
    then remarked)."""
    out: list[tuple[str, bool]] = []
    cursor = None
    for _ in range(500):
        asked = dict(arguments, **({"cursor": cursor} if cursor is not None else {}))
        if shim:
            text, failed = tools.call(snap, Observation.empty(snap.commit_sha), tool, asked)
            text = tools.remark(text, obs)
            # `remark` returns text, as §2.4 names it; a page that no longer
            # fits once marked becomes an error answer, so the shim's isError
            # is the service's or the remarked answer's.
            failed = failed or "error" in json.loads(text)
        else:
            text, failed = tools.call(snap, obs, tool, asked)
        assert len(text.encode()) <= tools.ANSWER_CAP
        out.append((text, failed))
        cursor = json.loads(text).get("next_cursor")
        if cursor is None:
            return out
    raise AssertionError(f"{tool} {arguments} never reached its last page")


@pytest.mark.parametrize("which", ["extracted", "hand"])
@pytest.mark.parametrize("cap", CAPS)
def test_the_shims_pages_are_the_local_servers_pages(snapshots, which, cap, monkeypatch):
    monkeypatch.setattr(tools, "ANSWER_CAP", cap)
    snap = snapshots[which]
    questions = battery(snap)
    assert len(questions) > 40, len(questions)
    compared = marked = trimmed = 0
    for seed in SEEDS:
        obs = observation(snap, seed)
        for tool, arguments in questions:
            local = pages(snap, tool, arguments, obs, shim=False)
            shim = pages(snap, tool, arguments, obs, shim=True)
            assert [t for t, _ in shim] == [t for t, _ in local], (seed, tool, arguments)
            assert [f for _, f in shim] == [f for _, f in local], (seed, tool, arguments)
            compared += len(local)
            marked += sum(t.count('"stale"') for t, _ in local)
            service = pages(snap, tool, arguments, Observation.empty(snap.commit_sha),
                            shim=False)
            trimmed += len(service) < len(local)
    # The property is only worth something if marks were made and they moved
    # page boundaries somewhere.
    assert compared > 400 and marked > 50, (compared, marked)
    if cap < 4096:
        assert trimmed > 0, "no remark ever had to drop a row"


def test_call_is_compute_then_page_and_compute_is_pure(snapshots):
    snap = snapshots["extracted"]
    obs = observation(snap, 1)
    for tool, arguments in battery(snap):
        try:
            first = tools.compute(tool, snap, arguments)
        except Exception:  # noqa: BLE001 -- error answers are covered by `call`
            continue
        again = tools.compute(tool, snap, arguments)
        assert first.head == again.head and first.materialised().rows == \
            again.materialised().rows, tool
        expected = tools.page(obs, first.head, first.rows, 0, first.expand)
        assert tools.call(snap, obs, tool, arguments) == (expected, False)
        kept = first.materialised()
        assert kept.expand is None
        assert tools.page(obs, kept.head, kept.rows, 0) == expected


def test_remark_replaces_the_block_and_marks_the_head_and_rows():
    empty = Observation.empty("i" * 40)
    head = {"symbol": "a/x.py#f"}
    rows = [{"rel": "caller", "symbol": "b/y.py#g"}, {"rel": "caller", "symbol": "c/z.py#h"},
            {"from": "d/w.py#k", "to": "a/x.py#f"}]
    service = tools.page(empty, head, rows, 0)
    obs = Observation(block={"index_sha": "i" * 40, "dirty": 1},
                      changed=frozenset({"c/z.py"}), dirty=frozenset({"a/x.py"}))
    answer = json.loads(tools.remark(service, obs))
    assert answer["freshness"] == obs.block
    assert answer["stale"] == DIRTY
    assert [r.get("stale") for r in answer["rows"]] == [None, CHANGED, DIRTY]
    assert tools.remark(service, obs) == tools.page(obs, head, rows, 0)


def test_remark_drops_rows_from_the_end_and_points_the_cursor_at_the_first_dropped(
        monkeypatch):
    empty = Observation.empty("i" * 40)
    rows = [{"path": f"pkg/f{n}.py", "pad": "x" * 60} for n in range(120)]
    service = json.loads(tools.page(empty, {"query": "q"}, rows, 5))
    assert service["next_cursor"] is not None
    obs = Observation(block={"index_sha": "i" * 40}, changed=frozenset(),
                      dirty=frozenset(r["path"] for r in rows))
    marked = json.loads(tools.remark(json.dumps(service), obs))
    assert len(marked["rows"]) < len(service["rows"])
    assert int(marked["next_cursor"]) == 5 + len(marked["rows"])
    assert marked["rows"] == [dict(r, stale=DIRTY) for r in rows[5:5 + len(marked["rows"])]]
    assert tools.remark(json.dumps(service), obs) == tools.page(obs, {"query": "q"}, rows, 5)


def test_a_row_that_only_fits_unmarked_becomes_the_placeholder_and_paging_advances():
    empty = Observation.empty("i" * 40)
    shell = len(tools.dumps({"freshness": empty.block, "total": 2, "rows": [],
                             "next_cursor": "2"}))
    row = {"path": "pkg/big.py", "pad": ""}
    row["pad"] = "x" * (tools.ANSWER_CAP - shell - len(tools.dumps(row)) - 1)
    rows = [row, {"path": "pkg/small.py"}]
    service = tools.page(empty, {}, rows, 0)
    assert json.loads(service)["rows"] == [row]
    obs = Observation(block={"index_sha": "i" * 40, "dirty": 1}, changed=frozenset(),
                      dirty=frozenset({"pkg/big.py"}))
    marked = json.loads(tools.remark(service, obs))
    assert marked["rows"] == [tools.ONE_ROW_OVER]
    assert marked["next_cursor"] == "1"
    assert tools.remark(service, obs) == tools.page(obs, {}, rows, 0)


def test_remark_of_an_error_keeps_the_error_and_what_candidates_fit():
    empty = Observation.empty("i" * 40)
    candidates = [f"pkg/m{n}.py#render" for n in range(30)]
    service = tools.error_answer(empty, "3 symbols match 'render'; pass one id", candidates)
    obs = Observation(block={"index_sha": "i" * 40, "dirty": 0}, changed=frozenset(),
                      dirty=frozenset())
    assert tools.remark(service, obs) == tools.error_answer(
        obs, "3 symbols match 'render'; pass one id", candidates)


def test_a_head_that_no_longer_fits_once_marked_is_the_local_servers_error(monkeypatch):
    empty = Observation.empty("i" * 40)
    head = {"symbol": "a/x.py#f", "pad": "x" * 3950}
    service = tools.page(empty, head, [], 0)
    obs = Observation(block={"index_sha": "i" * 40, "base_sha": "b" * 40, "dirty": 1},
                      changed=frozenset(), dirty=frozenset({"a/x.py"}))
    assert tools.remark(service, obs) == tools.error_answer(obs, tools.HEAD_OVER_CAP)


def test_the_empty_observation_names_only_the_index(tmp_path: Path):
    obs = Observation.empty("abc")
    assert obs.block == {"index_sha": "abc"}
    assert obs.stale("anything.py") is None
