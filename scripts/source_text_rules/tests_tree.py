"""Rules over the repository's tests/ tree.

no-bare-conftest-import. Every directory under tests/unit has its own
conftest.py, and under pytest's default `prepend` import mode each one is
imported as the top-level module `conftest`. So a bare import of that name
means whichever directory's conftest was imported FIRST in the session: in a
run of tests/unit/worker and tests/unit/reconciler together it was the
reconciler's, and collection of ~90 worker files failed with `cannot import
name 'seed_tenant' from 'conftest'` (owner decision 2026-10-06, observer P24).
The worker's helpers live in `worker_seeds.py`, a name nothing else under
tests/ uses, and are imported from there. A package-relative
`from .conftest import` (tests/unit/control_plane) names its own package and
is not what this holds.
"""

from __future__ import annotations

import ast
import re

from source_text_rules import Rule

#: The same condition read from text, for a file that does not parse. Spelled
#: with `\s+` so it is no match for itself.
_BARE_CONFTEST_IMPORT = re.compile(
    r"^\s*(?:from\s+conftest\s+import\b|import\s+conftest\b)", re.MULTILINE)
#: What a file must hold to be parsed for the rule at all: an import line
#: naming the bare `conftest` (`import os, conftest` included). Most files
#: that say `conftest` say it in prose or as `x.conftest`, and parsing them
#: all was half the run.
_MAY_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+conftest\b|import[ \t][^\n]*(?<![\w.])conftest\b)", re.MULTILINE)

_MESSAGE = ("import this from worker_seeds (or the helper's own module), never from "
            "the ambiguous top-level name `conftest`")


def _bare_conftest(name: str | None) -> bool:
    return name == "conftest" or bool(name and name.startswith("conftest."))


def _check_conftest(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return [(text.count("\n", 0, m.start()) + 1, _MESSAGE)
                for m in _BARE_CONFTEST_IMPORT.finditer(text)]
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and _bare_conftest(node.module):
            hits.append((node.lineno, _MESSAGE))
        elif isinstance(node, ast.Import) and any(_bare_conftest(a.name) for a in node.names):
            hits.append((node.lineno, _MESSAGE))
    return hits


_C = "conf" + "test"  # split, so these samples are no hit in this file

RULES = (
    Rule(
        id="no-bare-conftest-import",
        why="a bare `conftest` import names whichever directory's conftest pytest "
            "imported first (owner decision 2026-10-06, observer P24)",
        roots=("tests",),
        requires=_MAY_IMPORT,
        check=_check_conftest,
        bad=(
            ("tests/unit/worker/test_a.py", f"from {_C} import seed_attempt\n"),
            ("tests/unit/worker/test_b.py",
             f"def f():\n    from {_C} import (\n        TENANT,\n    )\n"),
            ("tests/unit/worker/test_c.py", f"import {_C}\n"),
            ("tests/unit/worker/test_d.py", f"import os, {_C}\n"),
            ("tests/unit/worker/test_e.py", f"x = (\nfrom {_C} import broken\n"),
        ),
        good=(
            ("tests/unit/control_plane/test_a.py", f"from .{_C} import auth_header\n"),
            ("tests/unit/worker/test_b.py", f"import {_C}_extra\n"),
            ("tests/unit/worker/test_c.py", "from worker_seeds import seed_tenant\n"),
            ("tests/unit/worker/test_d.py", f'"""Never `from {_C} import x`."""\n'),
        ),
    ),
)
