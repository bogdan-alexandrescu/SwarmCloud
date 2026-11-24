"""Forbidden-pattern rules for scripts/check_source_text.py (part of issue 73).

WHY A PACKAGE. A test that sweeps a tree for a pattern that must not appear
walked and read the tree itself, once per test file. Here each sweep is a
`Rule`, and the script reads and parses every file under the union of the
rules' roots ONCE, handing it to each rule whose roots cover it.

THE SHAPE. Every module in this directory whose name does not start with `_`
is discovered by name (no list to edit) and defines `RULES`, a tuple of
`Rule`:

    from source_text_rules import Rule

    RULES = (Rule(
        id="no-thing",                      # printed on every hit
        why="one line: the decision or issue it holds",
        roots=("tests", "apps/*/tests"),    # directories or fnmatch globs
        exempt=frozenset(),                 # exact paths allowed to match
        check=_check,                       # (path, text, tree) -> [(line, msg)]
        bad=(("tests/x.py", "..."),),       # samples it must hit
        good=(("tests/x.py", "..."),),      # samples it must pass
        requires="thing",                   # optional: a str or re.Pattern a
                                            # file must hold to be checked
    ),)

`tree` is the file's AST for .py (None for other files, or a .py that does not
parse). An `exempt` path that no longer exists or no longer matches fails the
check as a stale exemption. tests/unit/scripts/test_check_source_text.py runs
every rule's samples, so a new module is covered without editing it.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

SOURCE_SUFFIXES = (".py", ".ts", ".tsx")

Hit = tuple[int, str]
Check = Callable[[str, str, "ast.Module | None"], Iterable[Hit]]


@dataclass(frozen=True)
class Rule:
    """One forbidden-pattern sweep (see the module docstring)."""

    id: str
    why: str
    roots: tuple[str, ...]
    check: Check
    bad: tuple[tuple[str, str], ...]
    good: tuple[tuple[str, str], ...]
    exempt: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = SOURCE_SUFFIXES
    #: A substring or compiled pattern a file's text must hold for `check` to
    #: run on it, or None for every file. Parsing is most of a run's time
    #: (about 10 ms a test file); a file no rule needs is never parsed.
    requires: str | re.Pattern[str] | None = None

    def wants(self, text: str) -> bool:
        if self.requires is None:
            return True
        if isinstance(self.requires, str):
            return self.requires in text
        return self.requires.search(text) is not None

    def covers(self, path: str) -> bool:
        if not path.endswith(self.suffixes):
            return False
        for root in self.roots:
            if any(c in root for c in "*?["):
                if fnmatch.fnmatchcase(path, root) or fnmatch.fnmatchcase(path, root + "/*"):
                    return True
            elif path == root or path.startswith(root.rstrip("/") + "/"):
                return True
        return False
