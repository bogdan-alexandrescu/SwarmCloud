# source-text: fixture - copies apps/swarm-ui/src and plants a line in help.ts to prove the ui-copy-says-no-real-zero rule's scanner names it
"""The console never says "real zero" to a user.

OWNER DECISION, 2026-10-11. "Real zero" is the console's design term for "the
read succeeded and the answer is genuinely nothing", kept apart from `read
failed`, `partial` and `admin only` (design-system.md §6.9, `.ctl-mark
is-zero`). The distinction stays; the words go. The mark now reads `none`, and
every sentence that said "a real zero, not a failed read" says what happened
instead. The term was coined for the people building the console, and a user
reading it had to learn the vocabulary to understand the sentence.

WHERE THE SCAN RUNS. The sweep of the real console is the lint rule
`ui-copy-says-no-real-zero` in scripts/source_text_rules/scripts_tree.py, run
once per pull request by scripts/check_source_text.py (issue 73). The scanner
below is that rule's own `strip_comments` and `real_zero_hits`, and this file
proves it.

WHAT IS SCANNED. Every `.ts`, `.tsx` and `.css` file under apps/swarm-ui/src,
except `__tests__/`, which asserts the old words are gone and so has to name
them. Code comments, JSX `{/* */}` comments and CSS comments are stripped
first: the term is still the design vocabulary there, and a comment is not
shown to anyone. What remains is everything a user can be shown -- string and
template literals (help.ts entries, `say` sentences, `title` and `aria-label`
values) and JSX text -- and "real zero" anywhere in it fails, naming
file:line.

WHAT IT CANNOT SEE. The stripper is a lexer for strings and comments, not a
TypeScript parser. A `//` in JSX text (a bare URL between tags) is read as a
comment start, so the rest of that line goes unscanned; quotes do not end
strings across a newline, so an apostrophe in JSX text can hide at most the
rest of its own line. Both are cut to the line on purpose. The phrase is
matched across a line break as well (`real\nzero` in wrapped JSX text). A
hyphenated `real-zero` is not matched: no sentence spells it that way, and
class names and test ids do.

BREAK IT: put `zero: 'real zero'` back in primitives.tsx, or a help.ts entry
that says "a real zero". The rule names the file and line. The planted-string
tests below prove the scanner finds each place copy can live, and ignores the
comments, on a temp copy rather than by trusting it.
"""

from __future__ import annotations

import importlib
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "apps" / "swarm-ui" / "src"
SUFFIXES = (".ts", ".tsx", ".css")
RULES_DIR = REPO / "scripts" / "source_text_rules"


def _rules():
    """scripts/source_text_rules/scripts_tree.py, loaded the way
    scripts/check_source_text.py loads it (the package under its fixed name)."""
    if "source_text_rules" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "source_text_rules", RULES_DIR / "__init__.py", submodule_search_locations=[str(RULES_DIR)])
        assert spec and spec.loader
        package = importlib.util.module_from_spec(spec)
        sys.modules["source_text_rules"] = package
        spec.loader.exec_module(package)
    return importlib.import_module("source_text_rules.scripts_tree")


def findings(root: Path) -> list[str]:
    """Every `file:line` under ``root`` (tests excluded) that shows the phrase,
    found by the rule's `real_zero_hits`."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in SUFFIXES or "__tests__" in path.relative_to(root).parts:
            continue
        source = path.read_text(encoding="utf-8")
        for line, text in _rules().real_zero_hits(source, css=path.suffix == ".css"):
            found.append(f"{path.relative_to(root)}:{line}: {text}")
    return found


def test_the_scan_visits_the_console() -> None:
    """An empty result means nothing if nothing was read."""
    visited = [
        p
        for p in SRC.rglob("*")
        if p.suffix in SUFFIXES and "__tests__" not in p.relative_to(SRC).parts
    ]
    assert len(visited) > 100, f"the scan read {len(visited)} files under {SRC}"
    assert SRC / "primitives.tsx" in visited
    assert SRC / "help.ts" in visited


# ---------------------------------------------------------------------------
# The scanner, proven on planted copy
# ---------------------------------------------------------------------------

PLANTED = """\
// a real zero, in a line comment: ignored
/* a real zero,
   in a block comment: ignored */
const URL = 'https://example.invalid/a//b' // a real zero after a string
export const WORD = { zero: 'Real Zero' }
export const SAY = "the read came back a real zero"
export const TPL = `pool ${name} is a real ${'x'.length ? '' : ''}zero`
export const TPL2 = `nested ${`a real zero`} here`
export function A() {
  return (
    <p title="real zero" aria-label='a real zero'>
      {/* a real zero in a JSX comment: ignored */}
      No pools · real
      zero
    </p>
  )
}
"""


def test_the_scanner_finds_every_kind_of_planted_copy(tmp_path: Path) -> None:
    (tmp_path / "Planted.tsx").write_text(PLANTED, encoding="utf-8")
    lines = [int(f.split(":")[1]) for f in findings(tmp_path)]
    # Line 11 carries two (title and aria-label); line 7's template is split by
    # an expression, so no literal there says it and it is rightly not found.
    assert lines == [5, 6, 8, 11, 11, 13]


def test_the_scanner_reads_css_strings_but_not_css_comments(tmp_path: Path) -> None:
    (tmp_path / "x.css").write_text(
        "/* .is-zero  a real zero */\n.a::after { content: 'real zero'; }\n", encoding="utf-8"
    )
    assert [f.split(":")[1] for f in findings(tmp_path)] == ["2"]


def test_the_scanner_skips_tests_and_other_files(tmp_path: Path) -> None:
    (tmp_path / "__tests__").mkdir()
    (tmp_path / "__tests__" / "a.test.tsx").write_text("expect(x).not.toContain('real zero')\n")
    (tmp_path / "README.md").write_text("a real zero\n")
    assert findings(tmp_path) == []


def test_a_real_file_with_a_planted_string_is_named(tmp_path: Path) -> None:
    """The scan of a copy of the real console, plus one planted line, names it."""
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__tests__", "node_modules"))
    before = findings(copy)
    help_ts = copy / "help.ts"
    original = help_ts.read_text(encoding="utf-8")
    help_ts.write_text(original + "export const PLANTED = 'This is a real zero.'\n", encoding="utf-8")
    planted_line = original.count("\n") + 1
    after = findings(copy)
    assert [f for f in after if f not in before] == [
        f"help.ts:{planted_line}: export const PLANTED = 'This is a real zero.'"
    ]


@pytest.mark.parametrize(
    "src",
    [
        "// real zero\n",
        "/* real zero */\n",
        "x = 1 /* real\nzero */\n",
        "<p>{/* the real zero mark */}</p>\n",
        "const t = `a ${b /* real zero */} c`\n",
    ],
)
def test_comments_are_not_copy(tmp_path: Path, src: str) -> None:
    (tmp_path / "C.tsx").write_text(src, encoding="utf-8")
    assert findings(tmp_path) == []
