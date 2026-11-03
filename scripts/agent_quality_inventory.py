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
  quarantined       tests/flaky.txt's entries;
  cited_pytest_ids  how many `tests/...py::test_name` ids the documents cite;
  cited_vitest_files how many console test files they cite;
  missing           every cited id whose file or test function is not there;
  matrix_rows       how many capability rows capability-matrix.md has;
  unnamed_live_verified  every row without its four columns, or whose
                    live-verified cell is a bare "none", "no", "yes" (and the
                    like) instead of a named record, "not read from here" or
                    "n/a: <why>".

It exits 1 when anything cited is missing or a row is unnamed, so a renamed test can never keep
reading as "behaviourally tested". What it cannot check is named in the
documents themselves: a vitest case's title, a configuration value read from a
live deployment, and anything live-verified.

Usage: scripts/agent_quality_inventory.py [--root REPO]
"""

from __future__ import annotations

import argparse
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
    return {
        "commit": commit(root),
        "runner_models": runner_models(locals_tf.read_text()) if locals_tf.is_file() else {},
        "markers": marker_counts(root),
        "quarantined": quarantined(root),
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
    return 1 if result["missing"] or result["unnamed_live_verified"] else 0


if __name__ == "__main__":
    sys.exit(main())
