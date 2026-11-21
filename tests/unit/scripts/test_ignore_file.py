"""The root `.ignore` keeps bulk tracked files out of Grep, rg and Glob.

Issue 67: with no `.ignore`, every search returned lockfiles, the contract
change log, fixtures, rendered manifests and HTML mock-ups, and every later
turn re-read them. ripgrep reads `.ignore` in gitignore syntax, so git's own
matcher (`git ls-files --ignored --exclude-from`) answers what each pattern
hides.

What is held here:
  * every pattern still matches at least one tracked file, so a rename or a
    deletion does not leave a dead line that hides nothing;
  * the files agents are sent to (CLAUDE.md, CONTRACT.md, docs/ci.md, the
    working doc) stay searchable;
  * the lanes section says how a census sees every occurrence anyway.

BREAK IT: add `docs/ci.md` or a pattern for a file that does not exist to
`.ignore`, or delete the census note from the lanes section. Each turns this
red, naming what.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
IGNORE = REPO / ".ignore"
DOC = REPO / "docs" / "working-in-this-repository.md"

#: Files CLAUDE.md or the working doc send agents to search.
MUST_STAY_SEARCHABLE = (
    "CLAUDE.md",
    "CONTRACT.md",
    "docs/ci.md",
    "docs/working-in-this-repository.md",
)


def _patterns() -> list[str]:
    lines = IGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _hidden(pattern_file: Path) -> list[str]:
    """Tracked files the patterns in `pattern_file` hide, by git's gitignore matcher."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--ignored", f"--exclude-from={pattern_file}"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def test_the_ignore_file_exists_and_lists_patterns():
    assert IGNORE.is_file(), "no root .ignore: Grep, rg and Glob return bulk tracked files (issue 67)"
    assert _patterns(), ".ignore lists no patterns"


@pytest.mark.parametrize("pattern", _patterns() if IGNORE.is_file() else [])
def test_every_pattern_matches_a_tracked_file(pattern, tmp_path):
    one = tmp_path / "one.ignore"
    one.write_text(pattern + "\n", encoding="utf-8")
    assert _hidden(one), f".ignore pattern {pattern!r} matches no tracked file: drop or fix it"


def test_files_agents_are_sent_to_stay_searchable():
    hidden = set(_hidden(IGNORE))
    blocked = [path for path in MUST_STAY_SEARCHABLE if path in hidden]
    assert not blocked, f".ignore hides {blocked}, which CLAUDE.md sends agents to search"


def test_the_lanes_section_says_how_a_census_sees_every_occurrence():
    text = DOC.read_text(encoding="utf-8")
    lanes = text.split("## Lanes", 1)[1].split("\n## ", 1)[0]
    for needle in (".ignore", "git grep", "rg --no-ignore", "Read"):
        assert needle in lanes, f"the lanes section does not mention {needle!r} for a census"
