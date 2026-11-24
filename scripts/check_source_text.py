#!/usr/bin/env python3
"""Find the tests that read repository source as text, hold their markers, and
run the forbidden-pattern rules once over the tree (part of issue 73).

    check_source_text.py [--root DIR] [--list [PATH ...]]

WHY. A test that reads a .py or .ts file as text pins what the code SAYS, not
what it does: it can hold a contract that has no other observable form, it can
forbid a pattern anywhere in a tree, or it can parse a sample it wrote -- and
nothing in the file said which, so a reader could not tell a deliberate text
pin from a behaviour test written the cheap way. And every forbidden-pattern
sweep walked and read the tree again, once per test file. So this script:

  1. DETECTS a source-text test: a tracked (or new, unignored) .py file under
     tests/ or apps/*/tests/, of any name, whose AST holds any of
       * glob       a `.glob(...)`/`.rglob(...)` call (glob.glob too) with a
                    string argument ending `*.py`, `*.ts` or `*.tsx`;
       * ast-parse  a call to `ast.parse`;
       * getsource  a call to `inspect.getsource` or a bare `getsource`;
       * read       a `.read_text()`, `.read_bytes()`, `.open()` or `open()`
                    whose path expression -- followed through plain
                    `NAME = ...` assignments in the same file, at most three
                    hops -- holds a string constant ending `.py`, `.ts` or
                    `.tsx`, or a `<expr>.__file__` attribute (a module's
                    file; the bare `__file__`, the test's own file, is not).
     A file that reads only its own `__file__`, docs, YAML, JSON or shell is
     not one: issue 71 covers those.

  2. HOLDS THE MARKER. A detected file's first line (the second after a
     shebang) is

         # source-text: <class> - <reason>

     class `contract` (the text IS the contract: a name, a table, a call shape
     with no behaviour to observe), `forbidden-pattern` (a pattern must not
     appear, or appears only where allowed -- such a sweep belongs in a rule
     below), or `fixture` (the source read is a sample the test parses or
     builds, not the repository's code); the reason says why THIS file reads
     text. A malformed marker, an unknown class, a marker below the first line
     or a marker on a file the detector does not select fails, and so does a
     detected file with no marker: the next source-text test is classified
     when it is written.

  3. RUNS THE RULES, every module of scripts/source_text_rules/ (found by
     name; the package docstring gives a rule's shape). Each file under the
     union of the rules' roots is read once and, for .py, parsed once, and
     handed to every rule whose roots cover it. A hit prints
     `path:line: rule-id: message`. A rule's `exempt` path that no longer
     exists or no longer matches is itself a failure, so an exemption cannot
     outlive its reason.

It runs as a whole-tree guard (tests/unit/scripts/test_check_source_text.py,
in changed-guards.sh's GUARD_TESTS, which pull-request CI always runs) and as
`make lint`'s `source-text tests` step. Standard library only.

  exit 0  every marker well formed, no rule hit
  exit 1  a failure; each is printed as `path:line: message`
  exit 2  usage error
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import pkgutil
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_DIR = HERE / "source_text_rules"
RULES_PACKAGE = "source_text_rules"

CLASSES = ("contract", "forbidden-pattern", "fixture")
SOURCE_SUFFIXES = (".py", ".ts", ".tsx")
GLOB_SUFFIXES = tuple("*" + s for s in SOURCE_SUFFIXES)
MAX_HOPS = 3

_MARKER = re.compile(r"^# source-text: (?P<cls>\S+) - (?P<reason>\S.*)$")
_MARKER_LIKE = re.compile(r"^\s*#\s*source-text\b")
_TEST_TREE = re.compile(r"^(tests/|apps/[^/]+/tests/)")

Hit = tuple[int, str]


# ------------------------------------------------------------------- rules

def _load_package():
    # Loaded under a fixed name from this directory, so a rule module writes
    # `from source_text_rules import Rule` wherever the script is run from.
    if RULES_PACKAGE in sys.modules:
        return sys.modules[RULES_PACKAGE]
    spec = importlib.util.spec_from_file_location(
        RULES_PACKAGE, RULES_DIR / "__init__.py", submodule_search_locations=[str(RULES_DIR)])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[RULES_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


# The one definition of a rule is the package's, so a rule module and this
# script agree on it however either was loaded.
Rule = _load_package().Rule


def discover_rules() -> list:
    """Every `RULES` of every module in the package, by module name order."""
    _load_package()
    rules: list = []
    for info in sorted(pkgutil.iter_modules([str(RULES_DIR)]), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{RULES_PACKAGE}.{info.name}")
        found = getattr(module, "RULES", None)
        if not found or not all(isinstance(r, Rule) for r in found):
            raise SystemExit(f"{RULES_DIR / info.name}.py: defines no RULES tuple of Rule")
        rules.extend(found)
    ids = [r.id for r in rules]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"duplicate rule ids: {', '.join(dupes)}")
    return rules


# ----------------------------------------------------------------- sources

def parse_python(text: str, path: str) -> ast.Module | None:
    try:
        return ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return None


def git_files(root: Path) -> list[str]:
    # Tracked and untracked-but-not-ignored: the gate runs before the publish's
    # `git add --all`, so a lane's new files are not tracked yet.
    names: set[str] = set()
    for extra in ((), ("--others", "--exclude-standard")):
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", *extra],
                             check=True, capture_output=True).stdout.decode()
        names.update(p for p in out.split("\0") if p)
    return sorted(n for n in names if (root / n).is_file())


class Sources:
    """Each file read at most once and each .py parsed at most once, shared by
    the detector and every rule; a file is parsed only when one of them needs
    its tree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: list[str] | None = None
        self._text: dict[str, str] = {}
        self._tree: dict[str, ast.Module | None] = {}

    @property
    def files(self) -> list[str]:
        if self._files is None:
            self._files = git_files(self.root)
        return self._files

    @property
    def read_paths(self) -> set[str]:
        return set(self._text)

    def text(self, rel: str) -> str:
        if rel not in self._text:
            self._text[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        return self._text[rel]

    def tree(self, rel: str) -> ast.Module | None:
        if not rel.endswith(".py"):
            return None
        if rel not in self._tree:
            self._tree[rel] = parse_python(self.text(rel), rel)
        return self._tree[rel]


@dataclass
class RuleResult:
    failures: list[str] = field(default_factory=list)
    visited: dict[str, int] = field(default_factory=dict)


def apply_rule(rule, path: str, text: str, tree: ast.Module | None = None) -> list[Hit]:
    if not rule.wants(text):
        return []
    if tree is None and path.endswith(".py"):
        tree = parse_python(text, path)
    return sorted(rule.check(path, text, tree))


def run_rules(root: Path, rules: Sequence, sources: Sources) -> RuleResult:
    result = RuleResult(visited={r.id: 0 for r in rules})
    matched: dict[str, set[str]] = {r.id: set() for r in rules}
    for rel in sources.files:
        covering = [r for r in rules if r.covers(rel)]
        if not covering:
            continue
        text = sources.text(rel)
        for rule in covering:
            result.visited[rule.id] += 1
            if not rule.wants(text):
                continue
            for line, message in apply_rule(rule, rel, text, sources.tree(rel)):
                if rel in rule.exempt:
                    matched[rule.id].add(rel)
                else:
                    result.failures.append(f"{rel}:{line}: {rule.id}: {message}")
    present = set(sources.files)
    for rule in rules:
        for rel in sorted(rule.exempt):
            if rel not in present:
                result.failures.append(
                    f"{rel}:1: {rule.id}: stale exemption: the file no longer exists")
            elif rel not in matched[rule.id]:
                result.failures.append(
                    f"{rel}:1: {rule.id}: stale exemption: the file no longer matches")
    return result


# ---------------------------------------------------------------- detector

def _assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    found: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None \
                and isinstance(node.target, ast.Name):
            found.setdefault(node.target.id, []).append(node.value)
    return found


def _names_source(expr: ast.expr, assigned: dict[str, list[ast.expr]], hops: int,
                  seen: frozenset[str] = frozenset()) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.endswith(SOURCE_SUFFIXES):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "__file__":
            return True
        if isinstance(node, ast.Name) and hops > 0 and node.id not in seen:
            for value in assigned.get(node.id, ()):
                if _names_source(value, assigned, hops - 1, seen | {node.id}):
                    return True
    return False


def _call_name(func: ast.expr) -> tuple[str | None, str]:
    """(qualifier, name) of a call: `ast.parse` -> ("ast", "parse"),
    `x.y.glob` -> ("", "glob"), `open` -> (None, "open")."""
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        qual = func.value.id if isinstance(func.value, ast.Name) else ""
        return qual, func.attr
    return "", ""


def detect(text: str, tree: ast.Module | None) -> list[str]:
    """The techniques by which a file reads source as text, sorted."""
    if tree is None:
        return []
    assigned: dict[str, list[ast.expr]] | None = None
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qual, name = _call_name(node.func)
        if name in ("glob", "rglob"):
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.endswith(GLOB_SUFFIXES):
                    found.add("glob")
        elif name == "parse" and qual == "ast":
            found.add("ast-parse")
        elif name == "getsource":
            found.add("getsource")
        elif name in ("read_text", "read_bytes", "open") and qual is not None:
            assert isinstance(node.func, ast.Attribute)
            if "read" not in found:
                assigned = assigned if assigned is not None else _assignments(tree)
                if _names_source(node.func.value, assigned, MAX_HOPS):
                    found.add("read")
        elif name == "open" and qual is None and node.args and "read" not in found:
            assigned = assigned if assigned is not None else _assignments(tree)
            if _names_source(node.args[0], assigned, MAX_HOPS):
                found.add("read")
    return sorted(found)


#: Text every technique needs; a file without it is not parsed. A glob's
#: pattern, `getsource`, `ast.parse`, or a read call together with a string
#: ending in a source suffix or a `.__file__`.
_MAY_GLOB_OR_PARSE = re.compile(r"\*\.(?:py|tsx?)['\"]|getsource|\bast\.parse\b")
_READ_CALL = re.compile(r"\b(?:read_text|read_bytes|open)\(")
_SOURCE_NAME = re.compile(r"\.(?:py|tsx?)['\"]|\.__file__")


def may_read_source(text: str) -> bool:
    if _MAY_GLOB_OR_PARSE.search(text):
        return True
    return bool(_READ_CALL.search(text) and _SOURCE_NAME.search(text))


def in_test_tree(rel: str) -> bool:
    return rel.endswith(".py") and bool(_TEST_TREE.match(rel))


def detect_tree(root: Path, sources: Sources) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for rel in sources.files:
        if in_test_tree(rel) and may_read_source(sources.text(rel)):
            techniques = detect(sources.text(rel), sources.tree(rel))
            if techniques:
                found[rel] = techniques
    return found


# ------------------------------------------------------------------ marker

@dataclass(frozen=True)
class Marker:
    cls: str | None      # None: no marker
    error: str | None    # a message when the marker is wrong
    line: int = 1


def read_marker(text: str) -> Marker:
    lines = text.splitlines()
    at = 1 if lines and lines[0].startswith("#!") else 0
    first = lines[at] if len(lines) > at else ""
    match = _MARKER.match(first)
    if match:
        cls = match.group("cls")
        if cls not in CLASSES:
            return Marker(cls, f"unknown source-text class {cls!r}; one of "
                               f"{', '.join(CLASSES)}", at + 1)
        return Marker(cls, None, at + 1)
    if _MARKER_LIKE.match(first):
        return Marker(None, "malformed marker; write `# source-text: <class> - <reason>` "
                            f"with class one of {', '.join(CLASSES)} and a reason", at + 1)
    for number, line in enumerate(lines, 1):
        if _MARKER_LIKE.match(line):
            return Marker(None, "the source-text marker must be the first line "
                                "(the second after a shebang)", number)
    return Marker(None, None)


def check_markers(sources: Sources, detected: dict[str, list[str]]) -> list[str]:
    failures: list[str] = []
    for rel in sources.files:
        if not in_test_tree(rel):
            continue
        marker = read_marker(sources.text(rel))
        if marker.error:
            failures.append(f"{rel}:{marker.line}: {marker.error}")
        elif marker.cls and rel not in detected:
            failures.append(f"{rel}:{marker.line}: a source-text marker on a file that "
                            "reads no source as text; remove it")
        elif marker.cls is None and rel in detected:
            failures.append(f"{rel}:1: no source-text marker (reads source by "
                            f"{', '.join(detected[rel])}); add `# source-text: <class> - <reason>`")
    return failures


# -------------------------------------------------------------------- main

@dataclass
class TreeResult:
    detected: dict[str, list[str]]
    failures: list[str]
    rules: RuleResult


def check_tree(root: Path, sources: Sources) -> TreeResult:
    """The whole check: markers and every discovered rule, one read a file."""
    detected = detect_tree(root, sources)
    failures = check_markers(sources, detected)
    rules = run_rules(root, discover_rules(), sources)
    return TreeResult(detected, failures + rules.failures, rules)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--list", nargs="*", metavar="PATH", default=None,
                        help="print each detected file (of PATHs, or all) with its "
                             "techniques and marker, and exit 0")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    sources = Sources(root)

    if args.list is not None:
        detected = detect_tree(root, sources)
        wanted = {p.removeprefix("./") for p in args.list}
        for rel, techniques in detected.items():
            if wanted and rel not in wanted:
                continue
            marker = read_marker(sources.text(rel))
            state = f"marked {marker.cls}" if marker.cls else "UNMARKED"
            print(f"{rel}  {','.join(techniques)}  {state}")
        return 0

    result = check_tree(root, sources)
    for line in result.failures:
        print(line)
    marked = sum(1 for rel in result.detected if read_marker(sources.text(rel)).cls)
    print(f"source-text: {len(result.detected)} source-text tests, {marked} marked, "
          f"{len(sources.read_paths)} files read once, {len(result.failures)} failures",
          file=sys.stderr)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
