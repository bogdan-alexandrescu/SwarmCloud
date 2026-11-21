"""The merge-step design's 2026-10-06 revision cites code that still resolves
(lane CITD, slice 1 of issue #71). Citations in that section are symbols or
anchors that must resolve, never line numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_docs_spec_amendments import (
    _ANCHOR_CITE,
    _LINE_CITE,
    _SYMBOL_CITE,
    _cited_anchor,
    _cited_symbol,
)

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "merge-step.md"

REVISION = "## Revised 2026-10-06 (owner): merging is its own step, parked while CI runs"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under `heading`, up to the next heading of the same or a higher level."""
    assert heading in text, f"no heading {heading!r}"
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def test_the_revision_cites_symbols_and_anchors_that_resolve_and_no_line_numbers():
    revision = _section(_text(DESIGN), REVISION)
    assert not _LINE_CITE.findall(revision), "cite a symbol or an anchor, never a line number"
    symbols = _SYMBOL_CITE.findall(revision)
    assert len(symbols) >= 8, "the revision ties its claims to code"
    for path, qualname in symbols:
        assert (REPO / path).is_file(), f"cites {path}, which does not exist"
        _cited_symbol(path, qualname)
    for path, anchor in _ANCHOR_CITE.findall(revision):
        _cited_anchor(path, anchor)
