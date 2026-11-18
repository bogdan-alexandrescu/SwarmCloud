"""The flaky-test quarantine: tests/flaky.txt, read and applied to collection.

WHY THIS EXISTS. Owner decision, 2026-10-14: "Fix the known flakes and
quarantine new ones". In the 1,000 application.yml runs from 2026-10-10 to
2026-10-14, eight failed and then passed at the same commit (docs/ci.md,
"Flaky tests and the quarantine"). Each one held a merge for a rerun. A
known flake is fixed at its cause; a NEW one, found before its cause is, goes on the list so it
stops holding the gate while its issue is worked.

A quarantined test still runs, on every pull request, in a separate step of
the unit job that reports and never fails the gate (application.yml,
`quarantined unit tests`). It is NOT skipped: a skip would also hide the day it
starts failing every time, which is a real defect, not a flake.

THE LIST is checked in and edited by the operator, never by CI. One test per
line, three whitespace-separated fields:

    <pytest node id>   #<issue>   <YYYY-MM-DD the entry was added>

A node id with a parameter (`test_x[a-b]`) quarantines that one case; the bare
id (`test_x`) quarantines every case of it. A node id containing whitespace
cannot be listed. Blank lines and lines starting with `#` are comments.

This module is pure -- no pytest import -- so its parser is tested directly
(tests/unit/scripts/test_flaky_quarantine.py), and tests/unit/conftest.py is a
thin wiring over it.
"""

from __future__ import annotations

import ast
import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

REPO = Path(__file__).resolve().parents[2]
LIST = REPO / "tests" / "flaky.txt"
MARKER = "flaky"

_ISSUE = re.compile(r"#[1-9][0-9]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Entry:
    nodeid: str
    issue: int
    added: _dt.date
    line: int


class QuarantineListError(ValueError):
    """tests/flaky.txt does not parse. Collection stops rather than run a gate
    that silently quarantines nothing, or the wrong thing."""


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 3:
            raise QuarantineListError(
                f"tests/flaky.txt:{number}: expected `<node id> #<issue> <YYYY-MM-DD>`, "
                f"got {len(fields)} field(s): {line!r}")
        nodeid, issue, added = fields
        if not nodeid.startswith("tests/") or "::" not in nodeid:
            raise QuarantineListError(
                f"tests/flaky.txt:{number}: {nodeid!r} is not a pytest node id from the "
                "repository root (tests/<path>.py::<test>)")
        path = nodeid.split("::", 1)[0]
        if not path.endswith(".py"):
            raise QuarantineListError(f"tests/flaky.txt:{number}: {path!r} is not a .py file")
        if not _ISSUE.fullmatch(issue):
            raise QuarantineListError(
                f"tests/flaky.txt:{number}: {issue!r} is not an issue number (#<n>); every "
                "quarantined test has an issue that tracks its fix")
        if not _DATE.fullmatch(added):
            raise QuarantineListError(f"tests/flaky.txt:{number}: {added!r} is not a YYYY-MM-DD date")
        try:
            day = _dt.date.fromisoformat(added)
        except ValueError as exc:
            raise QuarantineListError(f"tests/flaky.txt:{number}: {added!r}: {exc}") from None
        if nodeid in seen:
            raise QuarantineListError(
                f"tests/flaky.txt:{number}: {nodeid} is already listed on line {seen[nodeid]}")
        seen[nodeid] = number
        entries.append(Entry(nodeid=nodeid, issue=int(issue[1:]), added=day, line=number))
    return entries


def load(path: Path = LIST) -> list[Entry]:
    """The checked-in list. A missing file is an empty quarantine."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse(text)


def is_quarantined(nodeid: str, entries: Iterable[Entry]) -> bool:
    """`nodeid` is a collected item's id. An entry matches it exactly, or
    matches every parameter of it when the entry has none."""
    bare = nodeid.split("[", 1)[0]
    return any(e.nodeid == nodeid or e.nodeid == bare for e in entries)


class _Item(Protocol):
    nodeid: str

    def add_marker(self, marker: object) -> None: ...


def apply(items: Iterable[_Item], entries: list[Entry], marker: object) -> list[str]:
    """Add `marker` to every collected item the list names. Returns the ids marked."""
    marked = []
    if not entries:
        return marked
    for item in items:
        if is_quarantined(item.nodeid, entries):
            item.add_marker(marker)
            marked.append(item.nodeid)
    return marked


def _defines(tree: ast.Module, parts: list[str]) -> bool:
    """`parts` (`["Class", "test_x"]` or `["test_x"]`) is defined in `tree`."""
    body: list[ast.stmt] = tree.body
    for i, name in enumerate(parts):
        last = i == len(parts) - 1
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef) if last else (ast.ClassDef,)
        found = next((n for n in body if isinstance(n, kinds) and n.name == name), None)
        if found is None:
            return False
        body = found.body  # type: ignore[union-attr]
    return True


def missing(entries: Iterable[Entry], repo: Path = REPO) -> list[str]:
    """The listed node ids that name no test: a file that is not there, or a
    test (or its class) that the file does not define. A renamed or deleted
    test would otherwise sit on the list quarantining nothing."""
    gone = []
    for entry in entries:
        path, *names = entry.nodeid.split("::")
        names = [n.split("[", 1)[0] for n in names]
        source = repo / path
        if not names or not source.is_file():
            gone.append(entry.nodeid)
            continue
        if not _defines(ast.parse(source.read_text(encoding="utf-8")), names):
            gone.append(entry.nodeid)
    return gone
