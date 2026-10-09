"""The frozen-contract findings of the 2026-10-09 epic triage are requests, not edits.

docs/epic-triage-2026-10-09.md (PR 927) found four boxes whose fix needs
`apps/common/swarm_common/`, which is frozen (CLAUDE.md rule 1). The owner
decided on 2026-10-09 that each gets a contract change REQUEST: #346 box 53
(int/float in the spec signature), #346 box 55 (deep nesting), #453 box 87
(worker-action profile checks), and whatever #346 box 42 (url_refusal) names
that request 57 left open. This holds each entry to the house shape, its
index row, and its citations into the frozen code to symbols that exist --
so an entry cannot be filed half-written, or point at a function since renamed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"

#: number -> (epic comment id, the frozen symbol the request changes).
ENTRIES = {
    58: ("5901303466", "apps/common/swarm_common/specsign.py", "canonical_step_spec"),
    59: ("5901303857", "apps/common/swarm_common/specsign.py", "_encode"),
    60: ("5944064436", "apps/common/swarm_common/profiles.py", "RunnerProfile.__post_init__"),
    61: ("5895194106", "apps/common/swarm_common/profiles.py", "url_refusal"),
}

SECTIONS = (
    "**Status:** proposed",
    "### What is true today",
    "### Why",
    "### The requested change",
    "```diff",
    "### What it would break if accepted",
    "### If it is declined",
    "### Invariants",
)


def _text() -> str:
    return REQUESTS.read_text(encoding="utf-8")


def _entry(text: str, number: int) -> str:
    match = re.search(rf"^## {number}\. .*$", text, flags=re.M)
    assert match is not None, f"contract-change-requests.md has no entry {number}"
    rest = text[match.end():]
    nxt = re.search(r"^## \d+\. ", rest, flags=re.M)
    return match.group(0) + (rest[: nxt.start()] if nxt else rest)


@pytest.mark.parametrize("number", sorted(ENTRIES))
def test_each_finding_is_an_entry_in_the_house_shape(number: int):
    body = _entry(_text(), number)
    for part in SECTIONS:
        assert part in body, f"request {number} has no {part!r}"
    comment, path, symbol = ENTRIES[number]
    assert f"issuecomment-{comment}" in body, f"request {number} does not link its epic box"
    assert f"`{path}::{symbol}`" in body, f"request {number} does not cite {path}::{symbol}"
    _cited_symbol(path, symbol)


@pytest.mark.parametrize("number", sorted(ENTRIES))
def test_each_entry_has_an_open_index_row(number: int):
    assert re.search(rf"^\| {number} \| .+ \| proposed \|$", _text(), flags=re.M), (
        f"request {number} has no index row with status proposed"
    )


def test_the_entries_are_numbered_once():
    numbers = [int(n) for n in re.findall(r"^## (\d+)\. ", _text(), flags=re.M)]
    assert len(numbers) == len(set(numbers)), "an entry number is used twice"
    rows = [int(n) for n in re.findall(r"^\| (\d+) \| ", _text(), flags=re.M)]
    for number in ENTRIES:
        assert rows.count(number) == 1, f"request {number} has {rows.count(number)} index rows"
