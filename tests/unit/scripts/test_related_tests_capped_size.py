"""A selection that is large without being a fallback path is capped too
(RELATED-CAP on size, owner answer 2026-11-23: "Also on size").

The fallback cap left a widely imported module that is no fallback path
uncapped: agent_worker/lifecycle.py selected 829 test files, far more than one
540 s run holds, so `--run` stopped at its first overrun. Now a selection whose
closure's recorded CI weight (unit_test_durations.json, a file it does not
record counted at the median) is more than one PYTEST_RUN_WEIGHT run gets the
same capped selection and the same one note as a fallback path; `--closure`
prints the closure as before; a selection at or under one run is unchanged.
CI's own selection (select_unit_tests.py) is untouched.

Held against a synthetic tree in tmp_path, so the answers are exact; never
against this repository's own tree, which moves under the assertions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SELECTOR = REPO / "scripts" / "lib" / "related_tests.py"
CI_SELECTOR = REPO / "scripts" / "lib" / "select_unit_tests.py"

MID = "apps/hub/hub/mid.py"
DIRECT = ["tests/unit/hub/test_direct_a.py", "tests/unit/hub/test_direct_b.py"]
FAR = [f"tests/unit/hub/test_far_{i:02d}.py" for i in range(10)]
OWN = "tests/unit/hub/test_mid.py"
READS = "tests/unit/hub/test_reads.py"
CLOSURE = sorted([*DIRECT, *FAR, OWN, READS])
CAPPED = sorted([*DIRECT, OWN, READS])
PADDING = [f"tests/unit/other/test_pad_{i:02d}.py" for i in range(20)]
DURATIONS = "tests/unit/scripts/unit_test_durations.json"


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path, weights: dict[str, float]) -> None:
    """hub/mid.py, no path CI runs the whole suite for, is imported directly
    by two tests and, through hub/deep.py, by ten more; twenty tests are
    unrelated, so CI's selector selects rather than running everything."""
    _write(root, "apps/hub/hub/__init__.py")
    _write(root, MID, "X = 1\n")
    _write(root, "apps/hub/hub/deep.py", "from hub.mid import X\n")
    _write(root, DIRECT[0], "from hub.mid import X\n")
    _write(root, DIRECT[1], "import hub.mid\n")
    for far in FAR:
        _write(root, far, "from hub.deep import X\n")
    _write(root, OWN, "X = 0\n")
    _write(root, READS, f'SRC = "{MID}"\n')
    for pad in PADDING:
        _write(root, pad, "Z = 3\n")
    _write(root, DURATIONS, json.dumps(weights))


def _closure_weights(far: float, reads: float) -> dict[str, float]:
    """Every closure file recorded: ten FAR files at `far`, the other four at
    10 s but READS at `reads`; the padding at 1 s."""
    weights = {t: 10.0 for t in [*DIRECT, OWN]}
    weights.update({t: far for t in FAR})
    weights[READS] = reads
    weights.update({t: 1.0 for t in PADDING})
    return weights


# 10 x 50 + 3 x 10 + 9 = 539 CI-seconds: under one 540 s run.
UNDER = _closure_weights(far=50.0, reads=9.0)
# 10 x 50 + 3 x 10 + 11 = 541 CI-seconds: more than one 540 s run.
OVER = _closure_weights(far=50.0, reads=11.0)


def _select(root: Path, *changed: str, closure: bool = False,
            cache: Path | None = None) -> dict[str, list[str]]:
    listing = root / "changed.txt"
    listing.write_text("".join(f"{c}\n" for c in changed))
    env = dict(os.environ)
    env["RELATED_TESTS_CACHE_DIR"] = str(cache) if cache else "off"
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


def _closure_output(weights: dict[str, float]) -> str:
    """What the selector printed for this tree before the size cap (it was
    never capped): no note, the closure cut longest file first onto the
    lightest run, ceil(total / 540) runs. Written out here, not computed by
    the code under test."""
    total = sum(weights[t] for t in CLOSURE)
    runs: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(max(1, -(-int(total) // 540)))]
    for test in sorted(CLOSURE, key=lambda t: (-weights[t], t)):
        i = min(range(len(runs)), key=lambda j: runs[j][0])
        runs[i] = (runs[i][0] + weights[test], [*runs[i][1], test])
    return "".join("pytest\t" + " ".join(sorted(files)) + "\n" for _, files in runs if files)


def test_a_large_selection_with_no_fallback_reason_is_capped_and_says_so(tmp_path):
    _tree(tmp_path, OVER)
    got = _select(tmp_path, MID, OWN)
    # MUTATION: cap on fallback_reason alone -> all 14 closure files come back.
    assert _files(got["pytest"]) == CAPPED
    capped = [n for n in got["note"] if "capped" in n]
    assert len(capped) == 1 and len(got["note"]) == 1, got["note"]
    assert "capped at 4 test file(s)" in capped[0] and "of the 14" in capped[0], capped[0]
    assert "CI (the merge train) runs the closure" in capped[0] and "--closure" in capped[0], capped[0]
    assert "541" in capped[0] and "540" in capped[0], capped[0]


def test_closure_prints_the_large_selection_as_before(tmp_path):
    _tree(tmp_path, OVER)
    got = _select(tmp_path, MID, OWN, closure=True)
    assert got["raw"] == [_closure_output(OVER)]
    assert _files(got["pytest"]) == CLOSURE


def test_a_selection_just_under_one_run_selects_what_it_did_before(tmp_path):
    _tree(tmp_path, UNDER)
    got = _select(tmp_path, MID, OWN)
    # MUTATION: a threshold below 539 s, or ">=" one run -> this is capped.
    assert got["raw"] == [_closure_output(UNDER)]
    assert got["note"] == []
    assert _select(tmp_path, MID, OWN, closure=True)["raw"] == [_closure_output(UNDER)]


def test_a_file_with_no_recorded_weight_counts_at_the_median_not_zero(tmp_path):
    # Only the padding is recorded, at 60 s each: the 14 closure files count
    # 60 s each at the median, 840 s, more than one run.
    _tree(tmp_path, {t: 60.0 for t in PADDING})
    got = _select(tmp_path, MID, OWN)
    # MUTATION: count an unrecorded file as 0 -> 0 s, nothing is capped.
    assert _files(got["pytest"]) == CAPPED
    assert any("capped at 4 test file(s)" in n and "of the 14" in n for n in got["note"]), got["note"]
    assert _files(_select(tmp_path, MID, OWN, closure=True)["pytest"]) == CLOSURE


def test_ci_selection_is_not_capped_on_size(tmp_path):
    """select_unit_tests.py as application.yml calls it selects the whole
    closure for the same large change: the size cap is the agent's alone."""
    _tree(tmp_path, OVER)
    listing = tmp_path / "changed.txt"
    listing.write_text(f"{MID}\n{OWN}\n")
    result = subprocess.run(
        [sys.executable, str(CI_SELECTOR), "--changed", str(listing), "--root", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    expected = ("python=selected areas=(none): 2 changed path(s) reach 14 of 34 test files\n"
                + "".join(f"  {t}\n" for t in CLOSURE))
    assert result.stdout == expected


def test_a_large_change_is_capped_and_closure_keeps_every_file_with_the_cache_on(tmp_path):
    """The owner's example (agent_worker/lifecycle.py selected 829 files with
    no fallback reason) on the synthetic tree, through a warm edge cache: the
    capped run prints the direct importers, the own test and the text reader,
    exactly; --closure prints the whole closure, exactly, with no cap note."""
    _tree(tmp_path, OVER)
    cache = tmp_path / "cache"
    for _ in range(2):  # cold, then warm
        capped = _select(tmp_path, MID, OWN, cache=cache)
        full = _select(tmp_path, MID, OWN, closure=True, cache=cache)
        assert _files(capped["pytest"]) == CAPPED
        assert len(capped["note"]) == 1, capped["note"]
        assert "capped at 4 test file(s) -- " in capped["note"][0], capped["note"]
        assert "of the 14 its full closure reaches" in capped["note"][0], capped["note"]
        assert full["raw"] == [_closure_output(OVER)]
    assert list(cache.glob("import-edges-*.json"))


def test_with_no_readable_durations_a_selection_is_capped_and_says_why(tmp_path):
    """No unit_test_durations.json (or one that cannot be read) gives no
    weight to trust: every file at one second read a large selection as
    light. Now the selection is treated as over the size cap, and the note
    says why; --closure prints the closure as before."""
    for durations in (None, "{not json", "{}"):
        _tree(tmp_path, OVER)
        if durations is None:
            (tmp_path / DURATIONS).unlink()
        else:
            (tmp_path / DURATIONS).write_text(durations)
        got = _select(tmp_path, MID, OWN)
        # MUTATION: weigh each file one second -> 14 s, nothing is capped.
        assert _files(got["pytest"]) == CAPPED, durations
        assert len(got["note"]) == 1, got["note"]
        assert "capped at 4 test file(s)" in got["note"][0] and "of the 14" in got["note"][0]
        assert "no recorded CI weight is readable" in got["note"][0], got["note"]
        assert DURATIONS in got["note"][0], got["note"]
        full = _select(tmp_path, MID, OWN, closure=True)
        assert _files(full["pytest"]) == CLOSURE
        assert full["note"] == []
