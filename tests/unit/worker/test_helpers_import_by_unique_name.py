"""No test module imports a helper from a module named `conftest`.

Every directory under tests/unit has its own conftest.py, and under pytest's
default `prepend` import mode each one is imported as the top-level module
`conftest`. So a bare import of that name means whichever directory's
conftest was imported FIRST in the session: in a run of tests/unit/worker and
tests/unit/reconciler together it was the reconciler's, and collection of ~90
worker files failed with `cannot import name 'seed_tenant' from 'conftest'`
(owner decision 2026-10-06, observer P24). The worker's helpers live in
`worker_seeds.py`, a name nothing else under tests/ uses, and are imported
from there. A package-relative `from .conftest import` (tests/unit/
control_plane) names its own package and is not what this holds.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]

#: A bare import of the module `conftest`, at any indentation: `from conftest
#: import X` and `import conftest`. Spelled with `\s+` so this file, which
#: names the pattern, is not itself a match.
_BARE_CONFTEST_IMPORT = re.compile(
    r"^\s*(?:from\s+conftest\s+import\b|import\s+conftest\b)", re.MULTILINE
)


def _python_files() -> list[Path]:
    return sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_sweep_visits_the_worker_tree() -> None:
    files = _python_files()
    # The control: a sweep that found nothing because it read nothing would
    # pass the test below vacuously.
    assert TESTS / "unit" / "worker" / "conftest.py" in files
    assert TESTS / "unit" / "reconciler" / "conftest.py" in files
    assert len(files) > 100


def test_the_pattern_matches_what_it_forbids() -> None:
    bare = "from " + "conftest import seed_attempt\n"
    indented = "def f():\n    from " + "conftest import (\n        TENANT,\n    )\n"
    plain = "import " + "conftest\n"
    relative = "from ." + "conftest import auth_header\n"
    assert _BARE_CONFTEST_IMPORT.search(bare)
    assert _BARE_CONFTEST_IMPORT.search(indented)
    assert _BARE_CONFTEST_IMPORT.search(plain)
    assert not _BARE_CONFTEST_IMPORT.search(relative)
    assert not _BARE_CONFTEST_IMPORT.search("import conftest_extra\n")


def test_no_test_module_imports_conftest_by_its_bare_name() -> None:
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for match in _BARE_CONFTEST_IMPORT.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(TESTS)}:{line}")
    assert not offenders, (
        "import these from worker_seeds (or the helper's own module), never "
        "from the ambiguous top-level name `conftest`:\n" + "\n".join(offenders)
    )


def test_the_helper_module_name_is_unique_under_tests() -> None:
    found = [p for p in _python_files() if p.name == "worker_seeds.py"]
    assert found == [TESTS / "unit" / "worker" / "worker_seeds.py"]
