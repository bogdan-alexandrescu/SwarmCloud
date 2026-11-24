# source-text: fixture - appends a line to a scratch copy of select_unit_tests.py so the edge cache must notice the selector changed
"""related-tests' import-edge cache (EdgeCache) reads a corrupt file as no
cache, writes a repaired entry back, never serves an edge across a change of
the selector's rules, and keeps its default directory private to the user.

Review of the RELATED-CAP pull request, 2026-11-23: a deeply nested cache file
raised RecursionError past the `except (OSError, ValueError)` and the script
exited 2; a malformed entry parsed again in memory stayed on disk, so it was
parsed again on every run; nothing held that an edit of select_unit_tests.py
misses the cache; and with TMPDIR unset the directory was one fixed name under
the system temp directory, shared by every user of the host.

Held against a synthetic tree in tmp_path, so the answers are exact.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib"
SELECTOR = LIB / "related_tests.py"

CORE = "apps/hub/hub/core.py"
DIRECT = ["tests/unit/hub/test_direct_a.py", "tests/unit/hub/test_direct_b.py"]
OWN = "tests/unit/hub/test_core.py"


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path) -> None:
    """hub/core.py is imported by two tests; one test is unrelated."""
    _write(root, "apps/hub/hub/__init__.py")
    _write(root, CORE, "X = 1\n")
    _write(root, DIRECT[0], "from hub.core import X\n")
    _write(root, DIRECT[1], "import hub.core\n")
    _write(root, OWN, "X = 0\n")
    _write(root, "tests/unit/other/test_unrelated.py", "Z = 3\n")
    _write(root, "tests/unit/scripts/unit_test_durations.json",
           json.dumps({t: 1.0 for t in [*DIRECT, OWN]}))


def _run(root: Path, cache: Path, selector: Path = SELECTOR) -> subprocess.CompletedProcess:
    listing = root / "changed.txt"
    listing.write_text(f"{CORE}\n{OWN}\n")
    env = dict(os.environ)
    env["RELATED_TESTS_CACHE_DIR"] = str(cache)
    env.pop("SWARM_CONTEXT_DIR", None)
    return subprocess.run(
        [sys.executable, str(selector), "--root", str(root), "--changed", str(listing)],
        capture_output=True, text=True, timeout=120, env=env,
    )


EXPECTED = "pytest\t" + " ".join(sorted([*DIRECT, OWN])) + "\n"


def test_a_deeply_nested_cache_file_reads_as_no_cache(tmp_path):
    root, cache = tmp_path / "repo", tmp_path / "cache"
    _tree(root)
    assert _run(root, cache).stdout == EXPECTED
    (path,) = cache.glob("import-edges-*.json")
    path.write_text("[" * 200_000)
    ran = _run(root, cache)
    # MUTATION: catch only OSError and ValueError -> RecursionError, exit 1.
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == EXPECTED
    # The corrupt file is replaced by this run's entries.
    assert isinstance(json.loads(path.read_text()), dict)


def test_a_repaired_entry_is_written_back(tmp_path):
    root, cache = tmp_path / "repo", tmp_path / "cache"
    _tree(root)
    _run(root, cache)
    (path,) = cache.glob("import-edges-*.json")
    good = json.loads(path.read_text())
    bad_key = sorted(good)[0]
    path.write_text(json.dumps({**good, bad_key: {"hub.core": 1}}))
    ran = _run(root, cache)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == EXPECTED
    # MUTATION: return early on equal keys -> the bad entry stays on disk and
    # is parsed again on every run.
    assert json.loads(path.read_text()) == good


def test_an_edited_selector_does_not_serve_a_cached_edge(tmp_path):
    """The cache file is named by the hash of select_unit_tests.py: an edit of
    the rules writes a second file, never reads the first."""
    root, cache = tmp_path / "repo", tmp_path / "cache"
    _tree(root)
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("related_tests.py", "select_unit_tests.py"):
        shutil.copy(LIB / name, lib / name)
    assert _run(root, cache, lib / "related_tests.py").stdout == EXPECTED
    (first,) = cache.glob("import-edges-*.json")
    # Poison the first file: were it read after the edit, test_direct_b.py
    # would import nothing and drop out.
    entries = json.loads(first.read_text())
    poisoned = {key: {} for key in entries}
    first.write_text(json.dumps(poisoned))
    with (lib / "select_unit_tests.py").open("a") as fh:
        fh.write("\n# an edited rule\n")
    ran = _run(root, cache, lib / "related_tests.py")
    assert ran.returncode == 0, ran.stderr
    # MUTATION: name the cache file by anything but the selector's hash -> the
    # poisoned entries are served and only the changed test is selected.
    assert ran.stdout == EXPECTED
    files = sorted(cache.glob("import-edges-*.json"))
    assert len(files) == 2 and first in files, files
    assert json.loads(first.read_text()) == poisoned


def _cache_dir(env: dict[str, str]) -> Path | None:
    sys.path.insert(0, str(LIB))
    try:
        import related_tests
    finally:
        sys.path.remove(str(LIB))
    return related_tests.cache_dir(env)


def test_the_default_cache_directory_names_the_user(tmp_path):
    got = _cache_dir({"TMPDIR": str(tmp_path)})
    # MUTATION: one fixed name -> every user of the host shares one directory.
    assert got == tmp_path / f"swarm-related-tests-{os.getuid()}"
    assert _cache_dir({"RELATED_TESTS_CACHE_DIR": "off"}) is None
    assert _cache_dir({"RELATED_TESTS_CACHE_DIR": str(tmp_path / "named")}) == tmp_path / "named"


def test_the_default_cache_directory_is_created_private(tmp_path):
    root = tmp_path / "repo"
    _tree(root)
    listing = root / "changed.txt"
    listing.write_text(f"{CORE}\n{OWN}\n")
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "RELATED_TESTS_CACHE_DIR"}
    env["TMPDIR"] = str(temp)
    env.pop("SWARM_CONTEXT_DIR", None)
    ran = subprocess.run(
        [sys.executable, str(SELECTOR), "--root", str(root), "--changed", str(listing)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == EXPECTED
    directory = temp / f"swarm-related-tests-{os.getuid()}"
    # MUTATION: mkdir with the default mode -> 0755 under the usual umask.
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert list(directory.glob("import-edges-*.json"))


def test_a_cache_directory_another_user_could_write_is_not_used(tmp_path):
    root, cache = tmp_path / "repo", tmp_path / "cache"
    _tree(root)
    cache.mkdir()
    cache.chmod(0o777)
    ran = _run(root, cache)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == EXPECTED
    # MUTATION: use any existing directory -> a cache file another user of
    # the host could have planted or can rewrite.
    assert not list(cache.glob("import-edges-*.json"))
