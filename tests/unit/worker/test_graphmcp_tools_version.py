"""`tools.TOOLS_VERSION` moves with every handler's output (graph-service.md §5.2).

The shared service keys its result cache on (tenant, manifest digest, tool,
canonical arguments, TOOLS_VERSION). An image whose handlers answer
differently under an unchanged constant would be a cache whose hits are
wrong answers wherever two versions share a cache, so this test fingerprints
every tool's answers -- every page, under the empty observation the service
uses and under a marking one -- over the hand-written snapshot of
test_graphmcp_source.py, whose bytes depend on nothing outside the test.

WHEN THIS FAILS because you changed an answer on purpose: bump
`TOOLS_VERSION` in tools.py and add its new fingerprint to `FINGERPRINTS`
(the failure prints it). Never re-record the fingerprint of an unchanged
version: that is exactly the change this test exists to refuse.
"""

from __future__ import annotations

import hashlib
import json

from test_graphmcp_source import APP, CLI, COMMIT, STORE, TEST, write_hand_snapshot

from agent_worker.graphmcp import tools
from agent_worker.graphmcp.freshness import Observation
from agent_worker.graphmcp.snapshot import ALL_LAYERS, Snapshot

#: TOOLS_VERSION -> the sha256 of every answer below, joined.
#: "1" was recorded once more before it ever shipped, when the review of the
#: PR that introduced it put `context`'s `stale` back ahead of its `note`.
FINGERPRINTS = {
    "1": "6e49f1dcd02131125fcac182ae7ecbd1dfa1b0317a325fdaaf0b36b99141875d",
}

HANDLE = f"{APP}#handle"
SAVE = f"{STORE}#save_row"


def battery(snap: Snapshot) -> list[tuple[str, dict]]:
    """At least one question per tool, and the arguments that change its shape."""
    questions: list[tuple[str, dict]] = [
        ("search", {"query": "fetch row"}), ("search", {"query": "handle", "limit": 1}),
        ("context", {"symbol": HANDLE}), ("context", {"symbol": f"{CLI}#main"}),
        ("context", {"symbol": "nothing_named_this"}),
        ("impact", {"symbol": SAVE, "depth": 3}), ("impact", {"symbol": STORE}),
        ("tests_for", {"files": [APP], "symbols": [SAVE]}),
        ("neighbours", {"symbol": HANDLE}),
        ("territory", {"files": [APP, STORE, "src/missing.py"]}),
        ("similar_changes", {"files": [APP, STORE]}),
        ("rules_for", {"files": [APP, CLI]}),
        ("call_graph", {"symbol": HANDLE}),
        ("call_graph", {"symbol": SAVE, "direction": "callers", "depth": 3}),
        ("call_graph", {"symbol": STORE, "direction": "callees"}),
        ("index", {"layer": "files", "path": TEST}),
        ("nope", {}),
    ]
    for layer in ALL_LAYERS:
        questions.append(("index", {"layer": layer}))
        questions += [("index", {"layer": layer, "key": key}) for key in snap.keys(layer)]
    return questions


def answers(snap: Snapshot, obs: Observation) -> list[str]:
    out: list[str] = []
    for tool, arguments in battery(snap):
        cursor = None
        while True:
            asked = dict(arguments, **({"cursor": cursor} if cursor else {}))
            text, failed = tools.call(snap, obs, tool, asked)
            out.append(f"{tool} {json.dumps(asked, sort_keys=True)} {failed} {text}")
            cursor = json.loads(text).get("next_cursor")
            if cursor is None:
                break
    return out


def fingerprint(snap: Snapshot) -> str:
    marking = Observation(block={"index_sha": COMMIT, "dirty": 2},
                          changed=frozenset({STORE}), dirty=frozenset({APP, CLI}))
    digest = hashlib.sha256()
    for obs in (Observation.empty(COMMIT), marking):
        for line in answers(snap, obs):
            digest.update(line.encode() + b"\n")
    return digest.hexdigest()


def test_the_answers_are_the_ones_tools_version_names(tmp_path, monkeypatch):
    # A small cap, so the fingerprint covers paging too.
    monkeypatch.setattr(tools, "ANSWER_CAP", 600)
    snap = Snapshot(write_hand_snapshot(tmp_path / "s"))
    assert len(battery(snap)) > 30
    actual = fingerprint(snap)
    assert tools.TOOLS_VERSION in FINGERPRINTS, (
        f"TOOLS_VERSION {tools.TOOLS_VERSION!r} has no recorded fingerprint; add "
        f"{tools.TOOLS_VERSION!r}: {actual!r} to FINGERPRINTS")
    assert actual == FINGERPRINTS[tools.TOOLS_VERSION], (
        f"the tools answer differently under TOOLS_VERSION {tools.TOOLS_VERSION!r}. Bump "
        f"TOOLS_VERSION in tools.py and record the new fingerprint {actual!r}; never "
        "re-record an unchanged version's")


def test_no_two_versions_share_a_fingerprint():
    assert len(set(FINGERPRINTS.values())) == len(FINGERPRINTS)


def test_tools_version_is_a_non_empty_string():
    assert isinstance(tools.TOOLS_VERSION, str) and tools.TOOLS_VERSION
