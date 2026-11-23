"""Where CI would run the whole suite, related-tests prints a capped selection
(RELATED-CAP, owner plan 2026-11-23).

Measured over 30 days, model time was a fifth of an agent task's wall-clock,
and most of the rest was tests: scripts/lib/related_tests.py named 800 to
2,500 test files for a five-file change whenever one changed file was widely
imported (a conftest.py, the frozen contract, a module most tests import), so
`--run` stopped at its first 540 s overrun with "narrow it", which an agent
cannot do, and the pre-publish gate inherited the same list. Now, on the
condition CI's selector (select_unit_tests.py) runs everything for, the
selection is the change's own tests, the DIRECT importers of each changed
module, the graph's tests_for and the tests naming a changed path; a note says
it was capped and how many files the closure holds; `--closure` prints the
closure; any other change selects exactly what it did before.

Held against a synthetic tree in tmp_path, so the answers are exact.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "related-tests.sh"
SELECTOR = REPO / "scripts" / "lib" / "related_tests.py"

# Built from words so this file names no contract path: changed-guards.sh
# and CI select a test that names one for every contract change.
CONTRACT = "/".join("apps common swarm_common".split())
CORE = f"{CONTRACT}/core.py"
DIRECT = ["tests/unit/hub/test_direct_a.py", "tests/unit/hub/test_direct_b.py"]
FAR = [f"tests/unit/hub/test_far_{i:02d}.py" for i in range(10)]
OWN = "tests/unit/hub/test_core.py"
READS = "tests/unit/hub/test_reads.py"


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path) -> None:
    """The contract's core.py, a path CI runs the whole suite for, is imported
    directly by two tests and, through hub/mid.py, by ten more."""
    _write(root, f"{CONTRACT}/__init__.py")
    _write(root, CORE, "X = 1\n")
    _write(root, "apps/hub/hub/__init__.py")
    _write(root, "apps/hub/hub/mid.py", "from swarm_common.core import X\n")
    _write(root, DIRECT[0], "from swarm_common.core import X\n")
    _write(root, DIRECT[1], "import swarm_common.core\n")
    for far in FAR:
        _write(root, far, "from hub.mid import X\n")
    _write(root, OWN, "X = 0\n")
    _write(root, READS, f'SRC = "{CORE}"\n')
    _write(root, "apps/other/other/__init__.py")
    _write(root, "apps/other/other/lone.py", "Y = 2\n")
    _write(root, "tests/unit/other/test_lone.py", "from other.lone import Y\n")
    _write(root, "tests/unit/other/test_unrelated.py", "Z = 3\n")
    # A recorded weight, light: with none readable the selection is capped.
    _write(root, "tests/unit/scripts/unit_test_durations.json", '{"tests/unit/other/test_lone.py": 1.0}\n')


def _select(root: Path, *changed: str, closure: bool = False, cache: Path | None = None) -> dict[str, list[str]]:
    listing = root / "changed.txt"
    listing.write_text("".join(f"{c}\n" for c in changed))
    env = dict(os.environ)
    env["RELATED_TESTS_CACHE_DIR"] = str(cache) if cache else "off"
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


def test_a_widely_imported_change_selects_its_direct_importers_not_the_closure(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, CORE, OWN)
    # MUTATION: keep the import closure -> the ten FAR tests come back; drop
    # the one-hop importers, the own test or the text rule -> one is lost.
    assert _files(got["pytest"]) == sorted([*DIRECT, OWN, READS])
    capped = [n for n in got["note"] if "capped" in n]
    assert len(capped) == 1, got["note"]
    # The note names the closure's size, the capped size, CI and the flag.
    assert "frozen contract" in capped[0] and "capped at 4 test file(s)" in capped[0], capped[0]
    assert "of the 14" in capped[0] and "CI (the merge train) runs the closure" in capped[0], capped[0]
    assert "--closure" in capped[0]


def test_closure_restores_the_full_list(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, CORE, OWN, closure=True)
    assert _files(got["pytest"]) == sorted([*DIRECT, *FAR, OWN, READS])
    assert not any("capped" in n for n in got["note"]), got["note"]


def test_a_fallback_path_caps_however_few_tests_it_reaches(tmp_path):
    _tree(tmp_path)
    contract = CONTRACT
    _write(tmp_path, f"{contract}/schema.py", "class Tenant:\n    pass\n")
    _write(tmp_path, "apps/other/other/model_user.py", "from swarm_common.schema import Tenant\n")
    _write(tmp_path, "tests/unit/other/test_schema.py", "from swarm_common.schema import Tenant\n")
    _write(tmp_path, "tests/unit/other/test_model_user.py", "from other.model_user import Tenant\n")
    got = _select(tmp_path, f"{contract}/schema.py")
    assert _files(got["pytest"]) == ["tests/unit/other/test_schema.py"]
    assert any("frozen contract" in n and "capped at 1 test file(s)" in n and "of the 2" in n
               for n in got["note"]), got["note"]
    got = _select(tmp_path, f"{contract}/schema.py", closure=True)
    assert _files(got["pytest"]) == ["tests/unit/other/test_model_user.py", "tests/unit/other/test_schema.py"]
    assert any("wide by design" in n for n in got["note"]), got["note"]


def test_a_change_with_no_widely_imported_file_selects_what_it_did_before(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, "apps/other/other/lone.py")
    assert _files(got["pytest"]) == ["tests/unit/other/test_lone.py"]
    assert got["note"] == []
    # MUTATION: cap every selection -> --closure would differ from the default.
    assert got["raw"] == _select(tmp_path, "apps/other/other/lone.py", closure=True)["raw"]


def test_the_edge_cache_answers_as_a_parse_does_and_misses_on_an_edited_file(tmp_path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    _tree(root)
    cold = _select(root, CORE, OWN, closure=True, cache=cache)
    assert list(cache.glob("import-edges-*.json")), "the cache was not written"
    warm = _select(root, CORE, OWN, closure=True, cache=cache)
    assert warm["raw"] == cold["raw"]
    # MUTATION: key the cache on the path alone -> test_direct_b.py stays.
    _write(root, DIRECT[1], "X = 9\n")
    edited = _select(root, CORE, OWN, closure=True, cache=cache)
    assert DIRECT[1] not in _files(edited["pytest"])


def test_the_script_passes_closure_through(tmp_path):
    diff = tmp_path / "diff.txt"
    diff.write_text("nothing/names/this-file.txt\n")
    env = {k: v for k, v in os.environ.items() if k != "SWARM_CLONE_BASE"}
    env["RELATED_TESTS_CACHE_DIR"] = "off"
    result = subprocess.run(["bash", str(SCRIPT), "--closure", "--files", str(diff)],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert "unknown option" not in result.stderr


def test_a_malformed_cache_entry_is_parsed_again_not_trusted(tmp_path):
    root = tmp_path / "repo"
    cache = tmp_path / "cache"
    _tree(root)
    cold = _select(root, CORE, OWN, closure=True, cache=cache)
    (path,) = cache.glob("import-edges-*.json")
    entries = json.loads(path.read_text())
    # MUTATION: accept any dict -> frozenset(1) raises and the run exits 1.
    path.write_text(json.dumps({key: {"swarm_common.core": 1} for key in entries}))
    assert _select(root, CORE, OWN, closure=True, cache=cache)["raw"] == cold["raw"]
