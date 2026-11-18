"""The compute/page split changed no answer (graph-service.md §2.4 seam 2).

"Every existing answer is byte-identical afterwards, and a test holds this
over the fixture snapshot for every tool." This is that test. `GOLDEN` is a
sha256 per pre-existing tool over every answer -- every page -- it gives for
every symbol, file and a few searches, on the KG2 fixture snapshot
(graphmcp_fixtures, with the history and rules layers
test_graphmcp_context_tools adds) and on the hand-written one of
test_graphmcp_source, under the empty observation and under a marking one,
at a small cap (so paging and marks meet the cap) and at the real one.

The digests were recorded from the PRE-SPLIT handlers (the agent_worker
package the image shipped before GS-CORE), not from today's code, so a
reorder that keeps the JSON equal but moves bytes (a head's `stale` written
after its `note`, say) fails here. Comparing new code with new code, as
test_graphmcp_remark and test_graphmcp_tools_version do, cannot see that.

One intended difference is excluded by construction: the pre-split `page`
hit its own `assert len(text) <= ANSWER_CAP` on a one-digit total's last
page and on a head that left no room for the one-row placeholder; `_fits`
answers those instead. No question below reaches either case under the
pre-split code (the recorder would have raised).

To re-record (only for a change that is MEANT to alter an existing answer,
which also bumps TOOLS_VERSION): `python <this file> build DIR` under this
tree, then `python <this file> digest DIR` with the code whose answers are
the reference first on PYTHONPATH.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import Snapshot, path_of

#: The tools that answered before the split; `call_graph` and `index` are new.
PRE_SPLIT = ("search", "context", "impact", "tests_for", "neighbours", "territory",
             "similar_changes", "rules_for")
CAPS = (600, 4096)
SNAPSHOTS = ("fixture", "hand")

#: tool -> sha256 of its answers, recorded from the pre-split handlers.
GOLDEN = {
    "search": "1bb560a13246ef25da05ff5c2ed5eac779c2d7bc0d6f2fe7e9fd594fe9aab3a6",
    "context": "ac259ce5553917faa915a209cab906b39ab29d959478647fdb6725089497d531",
    "impact": "64d14a37c4e77e9452d87252473274ca83b2dd312f4be841c0260dfc4e7edf37",
    "tests_for": "d44dc12d5976e380c84171e078bd9411325b1e12024fb55fe3d3d4f80a7ca827",
    "neighbours": "35c968e859173a1a7d626218aec200ec9228981c90bbcd03a641d6f13962b164",
    "territory": "ddf1e4a9c720bcfd19b8fdba4e3f054cc5836b9275ef970edb0af24cb3afc124",
    "similar_changes": "76169e55e0ba76cb72ba26b907d94fcdecbe34b2e54489228058ad069d1ccc41",
    "rules_for": "79d757322bb8bb4e50de511039a280ee81577761b30465f24b3ddb17234ea0a9",
}


def build_snapshots(root: Path) -> dict[str, Path]:
    """The two snapshots, written under `root` with this tree's fixtures."""
    import graphmcp_fixtures as gf
    import repo_index_fixtures as fx
    from test_graphmcp_context_tools import FILES, HISTORY, RULES, add_layer, by_module
    from test_graphmcp_source import write_hand_snapshot

    repo = fx.build_repo(root / "repo", FILES)
    fixture = gf.write_snapshot(gf.graph_of(repo), root / "fixture")
    add_layer(fixture, "history", by_module(HISTORY))
    add_layer(fixture, "rules", {"*": RULES})
    return {"fixture": fixture, "hand": write_hand_snapshot(root / "hand")}


def _rows(snap: Snapshot, layer: str) -> list[dict]:
    return [row for key in snap.keys(layer) for row in snap.shard(layer, key).rows]


def questions(snap: Snapshot) -> list[tuple[str, dict]]:
    symbols = sorted({str(r["id"]) for r in _rows(snap, "symbols")})
    files = sorted({str(r["path"]) for r in _rows(snap, "files")})
    out: list[tuple[str, dict]] = [
        ("search", {"query": q}) for q in ("load user", "store", "handle", "main", "zzz")]
    out.append(("search", {"query": "user", "limit": 2}))
    for symbol in symbols + ["no_such_symbol"]:
        out += [("context", {"symbol": symbol}), ("impact", {"symbol": symbol}),
                ("impact", {"symbol": symbol, "depth": 3}),
                ("neighbours", {"symbol": symbol}),
                ("tests_for", {"symbols": [symbol]})]
    for file in files:
        out += [("context", {"symbol": file}), ("tests_for", {"files": [file]}),
                ("similar_changes", {"files": [file]})]
    out += [("territory", {"files": files + ["src/missing.py"]}),
            ("territory", {"files": files[:1]}),
            ("tests_for", {"files": files}),
            ("similar_changes", {"files": files}),
            ("rules_for", {"files": files}),
            ("rules_for", {"files": files[:1]})]
    return out


def observations(snap: Snapshot) -> list[Observation]:
    files = sorted({str(r["path"]) for r in _rows(snap, "files")})
    marking = Observation(block={"index_sha": snap.commit_sha, "dirty": 2},
                          changed=frozenset(files[1::3]),
                          dirty=frozenset(files[::3]),
                          dirty_dirs=(path_of(files[-1]).rsplit("/", 1)[0] + "/",))
    # `Observation.empty`'s value, written out: the pre-split code lacks it.
    empty = Observation(block={"index_sha": snap.commit_sha}, changed=frozenset(),
                        dirty=frozenset())
    return [empty, marking]


def digests(roots: dict[str, Path]) -> dict[str, str]:
    """tool -> the sha256 of every answer it gives below, in order."""
    found = {tool: hashlib.sha256() for tool in PRE_SPLIT}
    saved = tools.ANSWER_CAP
    try:
        for cap in CAPS:
            tools.ANSWER_CAP = cap
            for name in SNAPSHOTS:
                snap = Snapshot(roots[name])
                for obs in observations(snap):
                    for tool, arguments in questions(snap):
                        cursor = None
                        while True:
                            asked = dict(arguments, **({"cursor": cursor} if cursor else {}))
                            text, failed = tools.call(snap, obs, tool, asked)
                            found[tool].update(f"{cap} {name} {json.dumps(asked, sort_keys=True)}"
                                               f" {failed} {text}\n".encode())
                            cursor = json.loads(text).get("next_cursor")
                            if cursor is None:
                                break
    finally:
        tools.ANSWER_CAP = saved
    return {tool: digest.hexdigest() for tool, digest in found.items()}


def test_every_pre_split_tool_answers_byte_for_byte_as_before(tmp_path):
    actual = digests(build_snapshots(tmp_path))
    differ = sorted(tool for tool in PRE_SPLIT if actual[tool] != GOLDEN.get(tool))
    assert not differ, (
        f"{differ} answer differently from the pre-split handlers "
        f"(graph-service.md §2.4 seam 2): {json.dumps(actual, indent=1)}")


def test_the_questions_reach_marks_paging_and_every_tool(tmp_path):
    """A control: the digests above would be weak if no answer were marked
    or paged, or a tool were never asked."""
    roots = build_snapshots(tmp_path)
    snap = Snapshot(roots["fixture"])
    asked = {tool for tool, _ in questions(snap)}
    assert asked == set(PRE_SPLIT)
    marking = observations(snap)[1]
    saved = tools.ANSWER_CAP
    try:
        tools.ANSWER_CAP = CAPS[0]
        texts = [tools.call(snap, marking, t, a)[0] for t, a in questions(snap)]
    finally:
        tools.ANSWER_CAP = saved
    assert any('"stale":' in t for t in texts)
    assert any('"stale":' in t and '"note":' in t for t in texts)
    assert any(json.loads(t).get("next_cursor") for t in texts)


if __name__ == "__main__":
    command, where = sys.argv[1], Path(sys.argv[2])
    if command == "build":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        print(json.dumps({k: str(v) for k, v in build_snapshots(where).items()}))
    else:
        print(json.dumps(digests({name: where / name for name in SNAPSHOTS}), indent=1))
