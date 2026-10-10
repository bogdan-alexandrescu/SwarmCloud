"""merge.py's citations of `issueci.<name>` name code that exists (issue #976).

THE DEFECT. `_verify_opener`'s docstring said swarm-api bound the merge
target in `issueci.merge_target_bound`; the function is
`merge_target_unbound`. A docstring citing a function that does not exist
sends a reader -- often one checking a security argument, as this one is --
to code that is not there, and nothing failed when the name drifted.

WHAT IS HELD. Every `issueci.<identifier>` written anywhere in
apps/agent-worker/agent_worker/merge.py (docstrings and comments included)
is a top-level function, class or assignment in
apps/swarm-api/swarm_api/issueci.py. Both files are read as text and the
latter parsed with `ast`, so the test imports nothing across packages and
needs no credentials. The scan must find at least one reference: an empty
scan (a renamed file, a broken regex) must not pass as clean.

MUTATION: restore `issueci.merge_target_bound` in `_verify_opener`'s
docstring -- the test fails naming it and its merge.py line.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MERGE_PY = REPO / "apps" / "agent-worker" / "agent_worker" / "merge.py"
ISSUECI_PY = REPO / "apps" / "swarm-api" / "swarm_api" / "issueci.py"

_REFERENCE = re.compile(r"\bissueci\.([A-Za-z_][A-Za-z0-9_]*)")


def _issueci_top_level_names() -> set[str]:
    names: set[str] = set()
    for node in ast.parse(ISSUECI_PY.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for leaf in ast.walk(target):
                    if isinstance(leaf, ast.Name):
                        names.add(leaf.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _merge_py_references() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    lines = MERGE_PY.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        found.extend((number, match.group(1)) for match in _REFERENCE.finditer(line))
    return found


def test_every_issueci_name_merge_py_cites_exists() -> None:
    references = _merge_py_references()
    assert references, f"found no `issueci.<name>` in {MERGE_PY}: the scan did not run"

    defined = _issueci_top_level_names()
    missing = [f"merge.py:{number} issueci.{name}" for number, name in references if name not in defined]
    assert not missing, "merge.py cites issueci names that issueci.py does not define:\n" + "\n".join(missing)
