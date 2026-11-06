#!/usr/bin/env python3
"""Re-check the agent quality baseline against the tree it was read from.

docs/design/agent-quality/ is the agent quality program's baseline and
capability inventory (BGD, 3 November 2026). This script makes it
reproducible: it reads only files under --root (and `git rev-parse` for the
commit), never the network and never a cloud credential, and prints one JSON
report:

  commit            the checkout's HEAD, or null when --root is not a git
                    checkout (unknown, never a made-up value);
  runner_models     terraform/infra/locals.tf `runner_models`, class -> id;
  markers           skip / expected-failure markers under tests/, by kind,
                    counted in code only (comments and strings are not code);
  skip_table        every skip, expected failure or importorskip under tests/
                    (by file, enclosing test and kind) that known-failures.md
                    does not list, with the row to add, and every row it lists
                    that the tree no longer has or that gives no reason;
  quarantined       tests/flaky.txt's entries;
  quarantine_table  the same for tests/flaky.txt's entries;
  cited_pytest_ids  how many `tests/...py::test_name` ids the documents cite;
  cited_vitest_files how many console test files they cite;
  missing           every cited id whose file or test function is not there;
  matrix_rows       how many capability rows capability-matrix.md has;
  unnamed_live_verified  every row without its four columns, or whose
                    live-verified cell is a bare "none", "no", "yes" (and the
                    like) instead of a named record, "not read from here" or
                    "n/a: <why>".

known-failures.md lists skips and never counts them: two green changes that
each add a skip and its row pass together, where two that each bump a count
conflict or fail (BGD, 6 November 2026: merge-safe whole-tree checks).

It exits 1 when anything cited is missing, a row is unnamed or a skip or
quarantine row is missing or stale, so a renamed test can never keep
reading as "behaviourally tested". What it cannot check is named in the
documents themselves: a vitest case's title, a configuration value read from a
live deployment, and anything live-verified.

Usage: scripts/agent_quality_inventory.py [--root REPO]
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

DOCS = Path("docs") / "design" / "agent-quality"
LOCALS = Path("terraform") / "infra" / "locals.tf"
FLAKY = Path("tests") / "flaky.txt"
KNOWN = DOCS / "known-failures.md"

#: A test module is `test_*.py`; `apps/x/cache.py::ResultCache` cites code.
_PYTEST_ID = re.compile(
    r"`((?:tests|apps)/(?:[^`\s]+/)?test_[^`\s/]*\.py)::([A-Za-z_][A-Za-z0-9_]*)[^`]*`"
)
#: A glob (`jev.*.test.tsx`, an owned-path pattern) is not a cited file.
_VITEST_FILE = re.compile(r"`(apps/swarm-ui/[^`\s*]+\.test\.tsx?)`")
MATRIX = DOCS / "capability-matrix.md"
#: A live-verified cell that names nothing: an unknown must read as unknown.
_UNNAMED = {"", "-", "none", "no", "yes", "0", "false", "true", "unknown", "n/a"}

#: Kinds of marker, matched on code joined token by token with one space.
MARKERS = {
    "skipif": re.compile(r"\bpytest \. mark \. skipif\b"),
    "skip": re.compile(r"\bpytest \. mark \. skip\b"),
    "skip_call": re.compile(r"\bpytest \. skip \("),
    "xfail": re.compile(r"\bpytest \. (?:mark \. )?xfail\b"),
    "importorskip": re.compile(r"\bpytest \. importorskip \("),
}


def _unique(items: list) -> list:
    seen: set = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def cited_pytest_ids(text: str) -> list[tuple[str, str]]:
    """Every `path.py::test_name` cited in inline code, in order, once each."""
    return _unique([(m.group(1), m.group(2)) for m in _PYTEST_ID.finditer(text)])


def cited_vitest_files(text: str) -> list[str]:
    """Every console test file cited in inline code, in order, once each."""
    return _unique(_VITEST_FILE.findall(text))


def missing_pytest_ids(root: Path, ids: list[tuple[str, str]]) -> list[str]:
    """Each cited id whose file does not exist or does not define the test."""
    missing = []
    for path, name in ids:
        source = root / path
        if not source.is_file():
            missing.append(f"{path}::{name} (no such file)")
            continue
        defined = re.compile(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(name)}[ \t]*\(", re.M)
        if not defined.search(source.read_text(errors="replace")):
            missing.append(f"{path}::{name} (no such test function)")
    return missing


def runner_models(locals_tf: str) -> dict[str, str]:
    """The `runner_models = { class = "id" }` block of locals.tf, or {}."""
    match = re.search(r"^[ \t]*runner_models[ \t]*=[ \t]*\{\n(.*?)^[ \t]*\}", locals_tf, re.M | re.S)
    if not match:
        return {}
    return dict(re.findall(r'^[ \t]*([a-z]+)[ \t]*=[ \t]*"([^"]+)"', match.group(1), re.M))


def _code(source: str) -> str:
    """The source's tokens without comments and strings, joined by one space."""
    kept = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                continue
            if sys.version_info >= (3, 12) and tok.type in (
                tokenize.FSTRING_START,
                tokenize.FSTRING_MIDDLE,
                tokenize.FSTRING_END,
            ):
                continue
            kept.append(tok.string)
    except (tokenize.TokenError, SyntaxError):
        return " ".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
    return " ".join(kept)


def marker_counts(root: Path) -> dict[str, int]:
    """Skip and expected-failure markers in every .py file under tests/."""
    counts = dict.fromkeys(MARKERS, 0)
    for path in sorted((root / "tests").rglob("*.py")):
        code = _code(path.read_text(errors="replace"))
        for kind, pattern in MARKERS.items():
            counts[kind] += len(pattern.findall(code))
    return counts


#: A marker that is an attribute (a decorator or `pytestmark = ...`).
_MARK_KINDS = {
    "pytest.mark.skipif": "skipif",
    "pytest.mark.skip": "skip",
    "pytest.mark.xfail": "xfail",
}
#: A marker that is a call inside a test or at module level.
_CALL_KINDS = {
    "pytest.skip": "skip_call",
    "pytest.xfail": "xfail",
    "pytest.importorskip": "importorskip",
}
#: A known-failures.md skip row: `| `path` | `test` | `kind` | reason |`.
_SKIP_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([a-z_]+)`\s*\|(.*)\|\s*$")
#: A known-failures.md quarantine row: `| `path::test` | quarantined | why |`.
_QUARANTINE_ROW = re.compile(r"^\|\s*`([^`]+::[^`]+)`\s*\|\s*quarantined\s*\|")


#: A proposed row quotes at most this much of the source's reason.
_REASON_CAP = 160


class Site(tuple):
    """One skip: (path, test, kind, reason); test is `Class.test` or `<module>`."""

    __slots__ = ()

    def __new__(cls, path: str, scope: str, kind: str, reason: str):
        return super().__new__(cls, (path, scope, kind, reason))

    @property
    def key(self) -> tuple[str, str, str]:
        return self[0], self[1], self[2]

    def row(self) -> str:
        reason = " ".join(self[3].split())
        if len(reason) > _REASON_CAP:
            reason = reason[:_REASON_CAP].rsplit(" ", 1)[0] + " ..."
        reason = reason.replace("|", "\\|") or "(say why)"
        return f"| `{self[0]}` | `{self[1]}` | `{self[2]}` | {reason} |"


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _reason(node: ast.AST) -> str:
    """The reason a marker's source gives: `reason=`, else its first argument."""
    if not isinstance(node, ast.Call):
        return ""
    for kw in node.keywords:
        if kw.arg in ("reason", "msg"):
            return _text(kw.value)
    if node.args:
        # skipif's first argument is its condition, never its reason.
        if _dotted(node.func) == "pytest.mark.skipif":
            return ""
        if _dotted(node.func) == "pytest.importorskip":
            return f"{_text(node.args[0])} is not importable"
        return _text(node.args[0])
    return ""


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f"x {y}" reads as x {y}: the row says why, not how it is spelt.
        return ast.unparse(node)[2:-1]
    return ast.unparse(node)


def _sites_in(source: str) -> list[tuple[int, int, str, str, str]]:
    """(line, column, test, kind, reason) for each marker in code."""
    found = []

    def visit(node: ast.AST, scope: list[str], parent: ast.AST | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = scope + [node.name]
        name = _dotted(node) if isinstance(node, ast.Attribute) else ""
        at = ".".join(scope) or "<module>"
        if name in _MARK_KINDS:
            call = parent if isinstance(parent, ast.Call) and parent.func is node else None
            found.append((node.lineno, node.col_offset, at, _MARK_KINDS[name], _reason(call)))
        elif name in _CALL_KINDS and isinstance(parent, ast.Call) and parent.func is node:
            found.append((node.lineno, node.col_offset, at, _CALL_KINDS[name], _reason(parent)))
        for child in ast.iter_child_nodes(node):
            visit(child, scope, node)

    visit(ast.parse(source), [], None)
    return sorted(found)


def skip_sites(root: Path) -> list[Site]:
    """Every skip under tests/, once per (file, test, kind), in source order.

    A test that skips twice for the same kind is one site: the table says
    which tests may skip and why, not how often.
    """
    sites: list[Site] = []
    seen: set = set()
    tests = root / "tests"
    for path in sorted(tests.rglob("*.py")) if tests.is_dir() else []:
        rel = path.relative_to(root).as_posix()
        source = path.read_text(errors="replace")
        # Every marker's name holds "skip" or "xfail": a file without either
        # has none, and is not parsed.
        if "skip" not in source and "xfail" not in source:
            continue
        try:
            found = _sites_in(source)
        except SyntaxError:
            found = [(0, 0, "<unparsed>", kind, "") for kind, n in _counts(path).items() if n]
        for _line, _col, scope, kind, reason in found:
            if (rel, scope, kind) not in seen:
                seen.add((rel, scope, kind))
                sites.append(Site(rel, scope, kind, reason))
    return sites


def _counts(path: Path) -> dict[str, int]:
    code = _code(path.read_text(errors="replace"))
    return {kind: len(pattern.findall(code)) for kind, pattern in MARKERS.items()}


def listed_skips(text: str) -> list[tuple[int, Site]]:
    """(line number, site) for each skip row known-failures.md lists."""
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _SKIP_ROW.match(line.strip())
        if match and match.group(3) in MARKERS:
            path, scope, kind, reason = match.groups()
            rows.append((number, Site(path, scope, kind, reason.strip())))
    return rows


def skip_table_drift(root: Path, text: str) -> list[str]:
    """Each live skip the table does not list, and each row that is stale or
    gives no reason. Set against set, never a count, so two changes that each
    add a skip and its row pass together as each passed alone."""
    listed = listed_skips(text)
    keys = {site.key for _, site in listed}
    live = skip_sites(root)
    live_keys = {site.key for site in live}
    out = [
        f"unlisted skip {' '.join(site.key)}; add the row: {site.row()}"
        for site in live
        if site.key not in keys
    ]
    lines = text.splitlines()
    for number, site in listed:
        if site.key not in live_keys:
            out.append(
                f"stale row at line {number}, no such skip in the tree: {lines[number - 1].strip()}"
            )
        elif not site[3]:
            out.append(f"row at line {number} gives no reason: {lines[number - 1].strip()}")
    return out


def quarantine_drift(root: Path, text: str) -> list[str]:
    """tests/flaky.txt's entries against known-failures.md's quarantine rows."""
    entries = {}
    for entry in quarantined(root):
        test_id, _, why = entry.partition(" ")
        entries.setdefault(test_id, why.strip())
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _QUARANTINE_ROW.match(line.strip())
        if match:
            rows.append((number, match.group(1), line.strip()))
    listed = {test_id for _, test_id, _ in rows}
    out = [
        f"unlisted quarantine {test_id}; add the row: "
        f"| `{test_id}` | quarantined | {why or '(say why)'} |"
        for test_id, why in entries.items()
        if test_id not in listed
    ]
    out += [
        f"stale row at line {number}, not in tests/flaky.txt: {line}"
        for number, test_id, line in rows
        if test_id not in entries
    ]
    return out


def quarantined(root: Path) -> list[str]:
    """tests/flaky.txt's entries: every line that is not blank or a comment."""
    path = root / FLAKY
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def commit(root: Path) -> str | None:
    """HEAD of the checkout at root, or None when root is not its top level."""
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(top) != 2 or Path(top[0]).resolve() != root.resolve():
        return None
    return top[1]


def _matrix_cells(text: str) -> list[list[str]]:
    """Each capability row's cells; a `\\|` inside a cell does not split it."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| Capability |"):
            continue
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]])
    return rows


def matrix_rows(text: str) -> int:
    return len(_matrix_cells(text))


def unnamed_live_verified(text: str) -> list[str]:
    """Rows that are short a column or whose live-verified cell names nothing."""
    out = []
    for cells in _matrix_cells(text):
        name = cells[0] if cells else "?"
        if len(cells) != 5:
            out.append(f"{name}: {len(cells)} columns, not 5")
        elif cells[4].strip("*. ").lower() in _UNNAMED:
            out.append(f"{name}: live-verified is {cells[4]!r}")
    return out


def report(root: Path) -> dict:
    docs = sorted((root / DOCS).glob("*.md"))
    text = "\n".join(p.read_text() for p in docs)
    ids = cited_pytest_ids(text)
    vitest = cited_vitest_files(text)
    missing = missing_pytest_ids(root, ids)
    missing += [f"{f} (no such file)" for f in vitest if not (root / f).is_file()]
    locals_tf = root / LOCALS
    matrix = (root / MATRIX).read_text() if (root / MATRIX).is_file() else ""
    known = (root / KNOWN).read_text() if (root / KNOWN).is_file() else ""
    return {
        "commit": commit(root),
        "runner_models": runner_models(locals_tf.read_text()) if locals_tf.is_file() else {},
        "markers": marker_counts(root),
        "skip_table": skip_table_drift(root, known),
        "quarantined": quarantined(root),
        "quarantine_table": quarantine_drift(root, known),
        "cited_pytest_ids": len(ids),
        "cited_vitest_files": len(vitest),
        "missing": missing,
        "matrix_rows": matrix_rows(matrix),
        "unnamed_live_verified": unnamed_live_verified(matrix),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="the repository checkout to read (default: this one)",
    )
    args = parser.parse_args(argv)
    result = report(args.root)
    print(json.dumps(result, indent=2))
    failed = ("missing", "unnamed_live_verified", "skip_table", "quarantine_table")
    return 1 if any(result[key] for key in failed) else 0


if __name__ == "__main__":
    sys.exit(main())
