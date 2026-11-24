#!/usr/bin/env python3
"""Refuse a comment or docstring in non-test code under apps/ that points at a
test (part of issue 69).

    check_test_pointers.py [--root DIR] [FILE ...]

WHY. A comment such as "see tests/unit/x/test_y.py::test_z" or "pinned by
test_foo.py" rots: tests move and get renamed, and the pointer keeps naming
the old place. Find-references already answers "what covers this", so a
comment carries the WHY of the code and never the name of the test that holds
it. Issue 69 removed 200-odd such pointers; this keeps new ones out. It runs
as a whole-tree guard (tests/unit/scripts/test_check_test_pointers.py, in
changed-guards.sh's GUARD_TESTS, which pull-request CI always runs).

WHAT IS A POINTER, in a Python comment or docstring, a `//` or `/* */`
comment of a JavaScript or TypeScript file (.ts, .tsx, .js, .jsx, .mjs, .cjs;
read string- and regex-aware, so a URL in a string is no comment), or a `#`
comment of any other text file under apps/:

  * a path to a file in the repository's tests/ tree
    (`tests/unit/x/test_y.py`, `tests/fixtures/live/*.json`: tests/, an entry
    it holds, and a file name) or in an app's own tests
    (`apps/<app>/tests/x.py`). A directory alone (`tests/unit`) names no test;
  * a Python test file's name: test_*.py, *_test.py;
  * a JavaScript test file's name (*.test.tsx, *.spec.ts, ...) that a
    tracked test file bears, so `X.test.tsx` as a placeholder is not one;
  * a test id: `::test_name`, or a bare `test_name` identifier that is a test
    function defined somewhere in test code (so `test_map`, a document key, is
    not one unless a test is called that).

Code under apps/ that works ON tests -- the worker's test selection, the
redaction's test-path rules -- may still describe a `tests/` directory or a
`conftest.py` in general; only a named test is a pointer.

WHAT IS SCANNED: every tracked file under apps/ except test code (files under
a tests/, test/ or __tests__/ directory, test_*.py, *_test.py, conftest.py,
*.test.* and *.spec.*), lock files, and the frozen contract
(apps/common/swarm_common/, ALLOWLIST_PREFIXES).

  exit 0  no pointer
  exit 1  a pointer; each is printed as `path:line: text`
  exit 2  usage error
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

# The files whose pointers issue 69 left for a follow-up while other open pull
# requests held them. Empty: issue 191 cleaned the console, and issue 69's
# last follow-up cleaned the worker's lifecycle.py and swarm-api's
# wire_types.py: every non-test file under apps/ but the frozen contract is
# scanned. Nothing is ever added to it (issue 69): a new pointer is reworded,
# never allowlisted.
# apps/common/swarm_common/ stays a prefix: the frozen contract, which no lane
# edits, so a pointer there is reported as a change request instead.
ALLOWLIST_PREFIXES = (
    "apps/common/swarm_common/",
)
ALLOWLIST: frozenset[str] = frozenset()

_TEST_DIR = re.compile(r"(^|/)(tests?|__tests__)/")
_TEST_FILE = re.compile(
    r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py|[^/]*\.(test|spec)\.[cm]?[jt]sx?)$")
_SKIP_SUFFIXES = (".lock",)

_POINTERS = (
    re.compile(r"(?<![\w.-])apps/[\w-]+/(?:tests?|__tests__)/[\w/.*-]*\.\w+"),
    re.compile(r"\b(?:test_\w+|\w+_test)\.py\b"),
    re.compile(r"::test_\w+"),
)
_TESTS_PATH = re.compile(r"(?<![\w.-])tests/([\w.-]+)(?:/[\w.*-]+)*\.\w+")
_JS_TEST_FILE = re.compile(r"(?<![\w.-])[\w.-]+\.(?:test|spec)\.[cm]?[jt]sx?\b")
_BARE_ID = re.compile(r"\btest_\w+")
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.M)


def is_test_code(path: str) -> bool:
    return bool(_TEST_DIR.search(path) or _TEST_FILE.search(path))


def is_allowlisted(path: str) -> bool:
    return path in ALLOWLIST or path.startswith(ALLOWLIST_PREFIXES)


def tracked_files(root: Path, under: str) -> list[str]:
    # Tracked and untracked-but-not-ignored: the gate runs before the publish's
    # `git add --all`, so a lane's new files are not tracked yet.
    names: list[str] = []
    for extra in ((), ("--others", "--exclude-standard")):
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", *extra, "--", under],
                             check=True, capture_output=True).stdout.decode()
        names.extend(p for p in out.split("\0") if p)
    return names


class Known:
    """What names a test in this tree: the entries of tests/, the basenames
    of JavaScript test files and the names of test functions."""

    def __init__(self, tests_entries: frozenset[str], js_files: frozenset[str],
                 functions: frozenset[str]) -> None:
        self.tests_entries = tests_entries
        self.js_files = js_files
        self.functions = functions

    @classmethod
    def of(cls, root: Path) -> Known:
        entries: set[str] = set()
        js: set[str] = set()
        functions: set[str] = set()
        for rel in tracked_files(root, "tests") + tracked_files(root, "apps"):
            if rel.startswith("tests/"):
                entry = rel.split("/")[1]
                entries.update({entry, entry.split(".")[0]})
            if not is_test_code(rel):
                continue
            name = rel.rsplit("/", 1)[-1]
            if _JS_TEST_FILE.fullmatch(name):
                js.add(name)
            if rel.endswith(".py"):
                try:
                    functions.update(_TEST_DEF.findall((root / rel).read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError):
                    continue
        return cls(frozenset(entries), frozenset(js), frozenset(functions))


def python_comments(text: str) -> list[tuple[int, str]]:
    """(line, text) of every comment and docstring line of Python source."""
    found: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                found.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return hash_comments(text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return found
    all_lines = text.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            first = body[0].lineno
            lines = all_lines[first - 1:body[0].end_lineno]
            found.extend((first + i, line) for i, line in enumerate(lines))
    return found


def hash_comments(text: str) -> list[tuple[int, str]]:
    """(line, text) of every `#` comment of a shell, TOML, Dockerfile or unit file."""
    found: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            found.append((n, stripped))
        elif " #" in line:
            found.append((n, line[line.index(" #"):]))
    return found


JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# After one of these words a `/` opens a regular expression, not a division.
_REGEX_AFTER_WORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
    "case", "do", "else", "yield", "await",
})


def js_comments(text: str) -> list[tuple[int, str]]:
    """(line, text) of every line of every `//` and `/* */` comment of
    JavaScript or TypeScript source.

    String-aware: a `//` or `/*` inside a '...', "..." or `...` string (with
    `${...}` nested to any depth) or inside a regular-expression literal opens
    no comment. A `/` opens a regular expression where an operand may start
    (after an operator, an opening bracket, a `}` closing a block, or a word
    such as `return`) and is a division after a name, a number, a string or a
    closing `)` or `]`. JSX text is read as code: a `//` in it is reported as
    a comment, and an apostrophe or a closing tag in it reads as a string or a
    regular expression that ends, at the latest, with its line.
    """
    found: list[tuple[int, str]] = []
    n = len(text)
    i = 0
    line = 1
    # Each entry is the brace depth at which a `${` of a template opened.
    templates: list[int] = []
    depth = 0
    regex_ok = True

    def comment(start: int, end: int, first_line: int) -> None:
        for k, part in enumerate(text[start:end].split("\n")):
            found.append((first_line + k, part))

    def skip_template(j: int) -> int:
        """From inside a template, return the index after its closing
        backtick, or the index after a `${` (pushing it)."""
        nonlocal line
        while j < n:
            c = text[j]
            if c == "\\":
                if j + 1 < n and text[j + 1] == "\n":
                    line += 1
                j += 2
                continue
            if c == "\n":
                line += 1
            elif c == "`":
                return j + 1
            elif c == "$" and j + 1 < n and text[j + 1] == "{":
                templates.append(depth)
                return j + 2
            j += 1
        return j

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r\f\v":
            i += 1
        elif c == "/" and nxt == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            comment(i, end, line)
            i = end
        elif c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            comment(i, end, line)
            line += text.count("\n", i, end)
            i = end
        elif c == "/" and regex_ok:
            j = i + 1
            in_class = False
            while j < n and text[j] != "\n":
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "[":
                    in_class = True
                elif d == "]":
                    in_class = False
                elif d == "/" and not in_class:
                    j += 1
                    break
                j += 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            line += text.count("\n", i, j)
            i = j
            regex_ok = False
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            # A backslash before a newline continues the string onto the next
            # line; an unescaped newline ends it unclosed (an apostrophe in JSX
            # text) and is left for the main loop to count.
            line += text.count("\n", i, j)
            i = j + 1 if j < n and text[j] == c else j
            regex_ok = False
        elif c == "`":
            before = len(templates)
            i = skip_template(i + 1)
            if len(templates) > before:
                depth += 1
                regex_ok = True
            else:
                regex_ok = False
        elif c == "{":
            depth += 1
            i += 1
            regex_ok = True
        elif c == "}":
            depth -= 1
            if templates and templates[-1] == depth:
                templates.pop()
                before = len(templates)
                i = skip_template(i + 1)
                if len(templates) > before:
                    depth += 1
                    regex_ok = True
                else:
                    regex_ok = False
            else:
                i += 1
                regex_ok = True
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            regex_ok = text[i:j] in _REGEX_AFTER_WORDS
            i = j
        elif c in ")]":
            i += 1
            regex_ok = False
        else:
            i += 1
            regex_ok = True
    return found


def comments_of(rel: str, text: str) -> list[tuple[int, str]]:
    if rel.endswith(".py"):
        return python_comments(text)
    if rel.endswith(JS_SUFFIXES):
        return js_comments(text)
    return hash_comments(text)


def pointers_in(text: str, known: Known) -> bool:
    # The substring tests only skip a regex that cannot match: they keep the
    # whole-tree run inside a guard's budget.
    if "test" not in text:
        return False
    if any(p.search(text) for p in _POINTERS):
        return True
    if "tests/" in text and any(m.group(1) in known.tests_entries
                                for m in _TESTS_PATH.finditer(text)):
        return True
    if (".test." in text or ".spec." in text) and any(
            m.group(0) in known.js_files for m in _JS_TEST_FILE.finditer(text)):
        return True
    return any(m.group(0) in known.functions for m in _BARE_ID.finditer(text))


def scan_file(root: Path, rel: str, known: Known) -> list[tuple[int, str]]:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    # Most files name no test anywhere: only those that do are parsed.
    if not pointers_in(text, known):
        return []
    return sorted({(n, c.strip()) for n, c in comments_of(rel, text) if pointers_in(c, known)})


def scanned(path: str) -> bool:
    return (path.startswith("apps/") and not is_test_code(path)
            and not is_allowlisted(path) and not path.endswith(_SKIP_SUFFIXES))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("files", nargs="*", help="repo-relative files (default: every tracked file)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    files = args.files or tracked_files(root, "apps")
    known = Known.of(root)
    bad = 0
    for rel in sorted(f for f in files if scanned(f)):
        for line, text in scan_file(root, rel, known):
            print(f"{rel}:{line}: {text}")
            bad += 1
    if bad:
        print(f"{bad} comment line(s) under apps/ point at a test: keep the WHY, drop the "
              "pointer (issue 69; scripts/check_test_pointers.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
