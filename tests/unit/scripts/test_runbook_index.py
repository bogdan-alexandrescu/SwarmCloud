"""docs/runbooks/README.md indexes every runbook, in the order an operator meets
them, and these tests hold it to the directory (issue #879).

Before the index, finding the runbook for a situation (a GKE re-dispatch, an
IAM refusal probe, offboarding a tenant) meant `ls docs/runbooks/` and opening
files until one matched. The index fixes that, but it is a mirrored copy of the
directory, and a mirrored copy drifts the first time someone adds a runbook and
forgets the page. So:

  * every runbook file in docs/runbooks/ has a line in the index, and the index
    links no runbook that does not exist (and not itself) -- two tests, so a red
    run says which way it drifted;
  * no runbook is listed twice: a set-up runbook that also covers a recurring
    task says so in its one line instead;
  * the lines sit under three headings in the order an operator meets them:
    set-up, then day-to-day, then incidents;
  * every line says when to reach for its runbook, not just its name.

The sets are built from the directory at runtime; no runbook name is written
here. No credentials, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUNBOOKS = REPO / "docs" / "runbooks"
INDEX = RUNBOOKS / "README.md"

# A markdown link whose target is a bare `<name>.md`, optionally with an anchor.
# A path with a slash (`../ci.md`, `../../tests/...`) is not a runbook link.
LINK_RE = re.compile(r"\]\(([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?:#[^)\s]*)?\)")
# A list item whose first thing is a runbook link; group 2 is the rest of the line.
ENTRY_RE = re.compile(r"^\s*[-*]\s+\[[^\]]+\]\(([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?:#[^)\s]*)?\)(.*)$")
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")

# The sections, in the order an operator meets them, each matched by a pattern
# so the heading can be worded naturally ("Set-up", "Setting up", ...).
SECTIONS = [
    ("set-up", re.compile(r"\bset[- ]?up\b|\bsetting up\b", re.IGNORECASE)),
    ("day-to-day", re.compile(r"\bday[- ]to[- ]day\b", re.IGNORECASE)),
    ("incidents", re.compile(r"\bincidents?\b", re.IGNORECASE)),
]


def _index_text() -> str:
    assert INDEX.is_file(), f"{INDEX.relative_to(REPO)} does not exist: docs/runbooks has no index"
    return INDEX.read_text()


def _runbooks_on_disk() -> set[str]:
    return {p.name for p in RUNBOOKS.glob("*.md") if p.name != INDEX.name}


def _linked() -> list[str]:
    """Every bare `<name>.md` link target in the index, anchors stripped, in order."""
    return LINK_RE.findall(_index_text())


def _entries() -> list[tuple[str | None, str, str]]:
    """(section heading or None, linked file, text after the link) per list item."""
    section: str | None = None
    entries = []
    for line in _index_text().splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            section = heading.group(2)
            continue
        entry = ENTRY_RE.match(line)
        if entry:
            entries.append((section, entry.group(1), entry.group(2)))
    return entries


def test_index_lists_every_runbook() -> None:
    missing = sorted(_runbooks_on_disk() - set(_linked()))
    assert not missing, f"docs/runbooks/README.md does not list: {', '.join(missing)}"


def test_index_lists_no_missing_runbook() -> None:
    linked = set(_linked())
    assert INDEX.name not in linked, "docs/runbooks/README.md lists itself"
    dangling = sorted(linked - _runbooks_on_disk())
    assert not dangling, f"docs/runbooks/README.md lists runbooks that do not exist: {', '.join(dangling)}"


def test_each_runbook_listed_once() -> None:
    linked = _linked()
    twice = sorted({name for name in linked if linked.count(name) > 1})
    assert not twice, f"docs/runbooks/README.md lists more than once: {', '.join(twice)}"


def test_index_covers_the_current_runbooks() -> None:
    # Ten when the index was written (#879); the comparison above is what holds
    # it to the directory, this only stops an emptied directory passing.
    assert len(set(_linked()) & _runbooks_on_disk()) >= 10


def test_sections_in_operator_order() -> None:
    text = _index_text()
    headings = [m.group(2) for m in map(HEADING_RE.match, text.splitlines()) if m]
    positions = []
    for name, pattern in SECTIONS:
        found = [i for i, heading in enumerate(headings) if pattern.search(heading)]
        assert len(found) == 1, f"docs/runbooks/README.md needs exactly one {name} heading, found {len(found)}"
        positions.append(found[0])
    assert positions == sorted(positions), "the sections are not in the order set-up, day-to-day, incidents"

    section_headings = {headings[i] for i in positions}
    entries = _entries()
    assert entries, "docs/runbooks/README.md has no runbook list items"
    stray = sorted(name for section, name, _ in entries if section not in section_headings)
    assert not stray, f"runbook entries outside the set-up, day-to-day and incidents sections: {', '.join(stray)}"
    # Every runbook link is a list item, so none hides in prose outside a section.
    assert sorted(name for _, name, _ in entries) == sorted(_linked()), (
        "every runbook link in docs/runbooks/README.md must be its own list item"
    )


def test_each_entry_says_when() -> None:
    bare = sorted(name for _, name, rest in _entries() if not re.search(r"\w", rest))
    assert not bare, f"index entries with no when-to-use after the link: {', '.join(bare)}"
