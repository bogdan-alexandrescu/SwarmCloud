"""A capped selection that would hold no test widens hop by hop of importers
(RELATED-CAP, owner answer 2026-11-23: "Widen by hops until non-empty").

Both caps -- the fallback cap and the size cap -- select the change's own
tests, its direct importers, the graph's tests_for and the tests naming a
changed path. A module whose tests all reach it through another module
(the agent worker's checkpointing module, through its lifecycle module) capped at 0 files. Now an
empty capped selection adds the next hop of importers inside the closure,
stops at the first hop holding a test and names the hop in its one note; a
widened hop heavier than one 540 s run is kept and the note says so. A change
with direct test importers selects what it did; `--closure` and CI's own
selection (select_unit_tests.py) are unchanged.

Held against a synthetic tree in tmp_path, so the answers are exact. The
checkpointing example (the worker's module whose tests all reach it through
its lifecycle module) is held by the synthetic hop tests below: the same shape,
no test importing the change, tests two and three imports away. A run of the
selector over the live repository took 25 to 50 s, and went red the day a
test imported the module, though the selector was right.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SELECTOR = REPO / "scripts" / "lib" / "related_tests.py"
CI_SELECTOR = REPO / "scripts" / "lib" / "select_unit_tests.py"
DURATIONS = "tests/unit/scripts/unit_test_durations.json"

# Built from words so this file names no contract path: changed-guards.sh
# and CI select a test that names one for every contract change.
CONTRACT = "/".join("apps common swarm_common".split())
CORE = f"{CONTRACT}/core.py"
LONE = f"{CONTRACT}/lone.py"

MID = "apps/hub/hub/mid.py"
HOP2 = [f"tests/unit/hub/test_hop2_{i:02d}.py" for i in range(4)]
HOP3 = [f"tests/unit/hub/test_hop3_{i:02d}.py" for i in range(6)]
DIRECT = ["tests/unit/hub/test_direct_a.py", "tests/unit/hub/test_direct_b.py"]
PADDING = [f"tests/unit/other/test_pad_{i:02d}.py" for i in range(20)]


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path, weights: dict[str, float], direct: bool = False) -> None:
    """hub/mid.py is imported by no test: hub/deep.py imports it, four tests
    import hub.deep (hop 2), hub/deeper.py imports hub.deep and six tests
    import hub.deeper (hop 3). `direct` adds two tests importing hub.mid."""
    _write(root, "apps/hub/hub/__init__.py")
    _write(root, MID, "X = 1\n")
    _write(root, "apps/hub/hub/deep.py", "from hub.mid import X\n")
    _write(root, "apps/hub/hub/deeper.py", "from hub.deep import X\n")
    for test in HOP2:
        _write(root, test, "from hub.deep import X\n")
    for test in HOP3:
        _write(root, test, "from hub.deeper import X\n")
    if direct:
        _write(root, DIRECT[0], "from hub.mid import X\n")
        _write(root, DIRECT[1], "import hub.mid\n")
    for pad in PADDING:
        _write(root, pad, "Z = 3\n")
    _write(root, DURATIONS, json.dumps(weights))


def _weights(hop2: float, hop3: float) -> dict[str, float]:
    weights = {t: hop2 for t in HOP2}
    weights.update({t: hop3 for t in HOP3})
    weights.update({t: 10.0 for t in DIRECT})
    weights.update({t: 1.0 for t in PADDING})
    return weights


# 4 x 50 + 6 x 60 = 560 CI-seconds: more than one run; hop 2 alone is 200 s.
OVER = _weights(hop2=50.0, hop3=60.0)
# 4 x 150 + 6 x 1 = 606 CI-seconds; hop 2 alone is 600 s, more than one run.
HEAVY_HOP = _weights(hop2=150.0, hop3=1.0)


def _select(root: Path, *changed: str, closure: bool = False) -> dict[str, list[str]]:
    listing = root / "changed.txt"
    listing.write_text("".join(f"{c}\n" for c in changed))
    env = dict(os.environ)
    env["RELATED_TESTS_CACHE_DIR"] = "off"
    env.pop("SWARM_CONTEXT_DIR", None)
    result = subprocess.run(
        [sys.executable, str(SELECTOR), "--root", str(root), "--changed", str(listing),
         *(["--closure"] if closure else [])],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 0, result.stderr
    out: dict[str, list[str]] = {"pytest": [], "vitest": [], "node": [], "note": [], "raw": [result.stdout]}
    for line in result.stdout.splitlines():
        kind, _, rest = line.partition("\t")
        out[kind].append(rest)
    return out


def _files(runs: list[str]) -> list[str]:
    return sorted(f for run in runs for f in run.split())


def _closure_output(tests: list[str], weights: dict[str, float]) -> str:
    """What the selector prints for an uncapped selection: no note, the files
    cut longest first onto the lightest run, ceil(total / 540) runs. Written
    out here, not computed by the code under test."""
    total = sum(weights[t] for t in tests)
    runs: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(max(1, -(-int(total) // 540)))]
    for test in sorted(tests, key=lambda t: (-weights[t], t)):
        i = min(range(len(runs)), key=lambda j: runs[j][0])
        runs[i] = (runs[i][0] + weights[test], [*runs[i][1], test])
    return "".join("pytest\t" + " ".join(sorted(files)) + "\n" for _, files in runs if files)


def test_size_cap_with_no_direct_test_importer_selects_the_second_hop(tmp_path):
    _tree(tmp_path, OVER)
    got = _select(tmp_path, MID)
    # MUTATION: no widening -> 0 files; widening past the first hop with a
    # test, or to the whole closure -> the hop-3 tests come back too.
    assert _files(got["pytest"]) == sorted(HOP2)
    assert len(got["note"]) == 1, got["note"]
    note = got["note"][0]
    assert "capped at 4 test file(s) at hop 2 of the 10" in note, note
    assert "560" in note and "CI (the merge train) runs the closure" in note, note
    assert "more than one 540 s run, kept" not in note, note
    # The narrow selection is part of the closure CI runs, not beside it.
    assert set(_files(got["pytest"])) <= set(_files(_select(tmp_path, MID, closure=True)["pytest"]))


def test_fallback_cap_with_no_direct_test_importer_widens_hop_by_hop(tmp_path):
    """The contract's core.py, a fallback path, reaches its tests only three
    imports away: core <- hub/mid <- hub/deep <- tests."""
    _tree(tmp_path, OVER)
    _write(tmp_path, f"{CONTRACT}/__init__.py")
    _write(tmp_path, CORE, "X = 1\n")
    _write(tmp_path, MID, "from swarm_common.core import X\n")
    got = _select(tmp_path, CORE)
    assert _files(got["pytest"]) == sorted(HOP2)
    capped = [n for n in got["note"] if "capped" in n]
    assert len(capped) == 1, got["note"]
    assert "capped at 4 test file(s) at hop 3 of the 10" in capped[0], capped[0]
    assert "frozen contract" in capped[0], capped[0]


def test_a_widened_hop_heavier_than_one_run_is_kept_and_the_note_says_so(tmp_path):
    _tree(tmp_path, HEAVY_HOP)
    got = _select(tmp_path, MID)
    # MUTATION: drop a hop heavier than one run -> 0 files.
    assert _files(got["pytest"]) == sorted(HOP2)
    note = got["note"][0]
    assert "at hop 2 of the 10" in note, note
    assert "its recorded CI weight is 600 s, more than one 540 s run, kept" in note, note


def test_a_change_with_direct_test_importers_selects_what_it_did(tmp_path):
    _tree(tmp_path, OVER, direct=True)
    got = _select(tmp_path, MID)
    assert _files(got["pytest"]) == DIRECT
    # The note this branch printed before the widening, word for word.
    assert got["note"] == [
        "the closure's recorded CI weight is 580 s, more than one 540 s run: the selection is capped "
        "at 2 test file(s) -- the change's own tests, the direct importers of each changed module, the "
        "code graph's tests_for and the tests that name a changed path -- of the 12 its full closure "
        "reaches; CI (the merge train) runs the closure. --closure prints all of them"
    ]


def test_an_unimported_change_with_no_tests_reports_an_empty_selection(tmp_path):
    _tree(tmp_path, OVER)
    _write(tmp_path, f"{CONTRACT}/__init__.py")
    _write(tmp_path, LONE, "Y = 2\n")
    got = _select(tmp_path, LONE)
    assert got["pytest"] == []
    capped = [n for n in got["note"] if "capped" in n]
    assert len(capped) == 1, got["note"]
    assert "capped at 0 test file(s) of the 0 its full closure reaches" in capped[0], capped[0]
    assert "no test runs for it here" in capped[0], capped[0]
    assert _select(tmp_path, LONE, closure=True)["pytest"] == []


def test_closure_output_is_unchanged_by_the_widening(tmp_path):
    _tree(tmp_path, OVER)
    got = _select(tmp_path, MID, closure=True)
    assert got["raw"] == [_closure_output(sorted([*HOP2, *HOP3]), OVER)]


def test_ci_selection_is_unchanged_by_the_widening(tmp_path):
    """select_unit_tests.py as application.yml calls it selects the closure."""
    _tree(tmp_path, OVER)
    listing = tmp_path / "changed.txt"
    listing.write_text(f"{MID}\n")
    result = subprocess.run(
        [sys.executable, str(CI_SELECTOR), "--changed", str(listing), "--root", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    expected = ("python=selected areas=(none): 1 changed path(s) reach 10 of 30 test files\n"
                + "".join(f"  {t}\n" for t in sorted([*HOP2, *HOP3])))
    assert result.stdout == expected


# An import cycle between the hops: ROOT <- CYC_A <-> CYC_B <- CYC_C <-> CYC_D
# <- the CYC_TESTS, and a pair importing each other that no test reaches.
ROOT = "apps/hub/hub/cyc_root.py"
CYC_TESTS = [f"tests/unit/hub/test_cyc_{i}.py" for i in range(3)]
CYC_PAD = [f"tests/unit/other/test_cyc_pad_{i:02d}.py" for i in range(10)]
CYC_LONE = f"{CONTRACT}/cyc_lone.py"
CYCLES = {
    "cyc_a": "from hub.cyc_root import X\nfrom hub.cyc_b import Y\n",
    "cyc_b": "from hub.cyc_a import X\nY = 2\n",
    "cyc_c": "from hub.cyc_b import Y\nfrom hub.cyc_d import Z\n",
    "cyc_d": "from hub.cyc_c import Y\nZ = 3\n",
    "dead_a": "from hub.cyc_root import X\nfrom hub.dead_b import W\n",
    "dead_b": "from hub.dead_a import X\nW = 4\n",
}


def _cycle_tree(root: Path) -> None:
    """Three tests import cyc_d, five imports from cyc_root through two pairs
    that import each other; dead_a and dead_b import cyc_root and each other
    and no test imports either. The closure is 3 tests of 600 CI-seconds
    each, over one 540 s run, so the selection is capped."""
    _write(root, "apps/hub/hub/__init__.py")
    _write(root, ROOT, "X = 1\n")
    for name, text in CYCLES.items():
        _write(root, f"apps/hub/hub/{name}.py", text)
    for test in CYC_TESTS:
        _write(root, test, "from hub.cyc_d import Z\n")
    for pad in CYC_PAD:
        _write(root, pad, "Q = 5\n")
    weights = {t: 200.0 for t in CYC_TESTS}
    weights.update({t: 1.0 for t in CYC_PAD})
    _write(root, DURATIONS, json.dumps(weights))


def test_hops_through_import_cycles_terminate_at_the_first_test(tmp_path):
    _cycle_tree(tmp_path)
    got = _select(tmp_path, ROOT)
    # MUTATION: a hop loop with no seen set never ends on cyc_a <-> cyc_b.
    assert _files(got["pytest"]) == sorted(CYC_TESTS)
    assert len(got["note"]) == 1, got["note"]
    assert "capped at 3 test file(s) at hop 5 of the 3 its full closure reaches" in got["note"][0], got["note"]


def test_a_cycle_that_reaches_no_test_selects_nothing_and_ends(tmp_path):
    """The dead pair is imported by the changed module only: the loop leaves
    it on the first hop and goes on to none."""
    _cycle_tree(tmp_path)
    _write(tmp_path, f"{CONTRACT}/__init__.py")
    _write(tmp_path, CYC_LONE, "V = 6\n")
    _write(tmp_path, "apps/hub/hub/dead_c.py", "from swarm_common.cyc_lone import V\nfrom hub.dead_d import U\n")
    _write(tmp_path, "apps/hub/hub/dead_d.py", "from hub.dead_c import V\nU = 7\n")
    got = _select(tmp_path, CYC_LONE)
    assert got["pytest"] == []
    capped = [n for n in got["note"] if "capped" in n]
    assert len(capped) == 1, got["note"]
    assert "capped at 0 test file(s) of the 0 its full closure reaches" in capped[0], capped[0]


def test_a_cycle_beside_the_chain_to_the_tests_is_not_selected(tmp_path):
    """dead_a and dead_b import cyc_root like cyc_a does: they are in hop 1
    and in no test's way, so no test is chosen for them, closure or not."""
    _cycle_tree(tmp_path)
    assert _files(_select(tmp_path, ROOT, closure=True)["pytest"]) == sorted(CYC_TESTS)


def test_a_cycle_between_a_test_and_a_changed_module_ends(tmp_path):
    """The changed module and the helper that imports it import each other."""
    _write(tmp_path, "apps/hub/hub/__init__.py")
    _write(tmp_path, ROOT, "from hub.cyc_a import Y\nX = 1\n")
    _write(tmp_path, "apps/hub/hub/cyc_a.py", "from hub.cyc_root import X\nY = 2\n")
    _write(tmp_path, "apps/hub/hub/cyc_b.py", "from hub.cyc_a import Y\n")
    _write(tmp_path, CYC_TESTS[0], "from hub.cyc_b import Y\n")
    _write(tmp_path, DURATIONS, json.dumps({CYC_TESTS[0]: 600.0}))
    got = _select(tmp_path, ROOT)
    assert _files(got["pytest"]) == [CYC_TESTS[0]]
    assert "capped at 1 test file(s) at hop 3 of the 1 its full closure reaches" in got["note"][0], got["note"]


def _load_related_tests():
    spec = importlib.util.spec_from_file_location("related_tests_capped_hops", SELECTOR)
    assert spec and spec.loader
    sys.path.insert(0, str(SELECTOR.parent))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SELECTOR.parent))
    return module


def test_a_failed_cache_write_leaves_no_part_file_behind(tmp_path, monkeypatch):
    related_tests = _load_related_tests()
    cache_dir = tmp_path / "cache"
    cache = related_tests.EdgeCache(cache_dir)
    assert cache.path is not None
    cache("apps/hub/hub/core.py", "X = 1\n")
    real_write_text = Path.write_text

    def failing_write_text(self, data, *args, **kwargs):
        real_write_text(self, data[:4], *args, **kwargs)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    cache.save()
    monkeypatch.undo()
    assert sorted(p.name for p in cache_dir.iterdir()) == []


def test_a_selection_that_is_the_whole_closure_prints_no_capped_note(tmp_path):
    """With no readable unit_test_durations.json every selection is capped,
    even a small one that holds the whole closure: 'capped at 2 of the 2'."""
    _write(tmp_path, "apps/hub/hub/__init__.py")
    _write(tmp_path, MID, "X = 1\n")
    for test in DIRECT:
        _write(tmp_path, test, "from hub.mid import X\n")
    _write(tmp_path, PADDING[0], "Z = 3\n")
    got = _select(tmp_path, MID)
    assert _files(got["pytest"]) == DIRECT
    assert got["note"] == [], got["note"]
