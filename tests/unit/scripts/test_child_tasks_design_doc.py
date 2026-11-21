"""Child tasks, phase 1: every citation in the design and the contract request
resolves (lane CITD, slice 1 of issue #71).

Line NUMBERS are not cited at all: lifecycle.py, loop.py and store.py are
edited by other lanes every wave, so a citation is `path::qualname` or `path`
(`anchor text`), which fails only when the symbol or the text is gone, not
when a merge above it moves it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_docs_spec_amendments import _assert_cites_resolve

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "design" / "child-tasks.md"
REQUESTS = REPO / "docs" / "contract-change-requests.md"


@pytest.mark.parametrize("doc", [DESIGN, REQUESTS], ids=lambda p: p.name)
def test_every_cited_file_exists(doc: Path):
    """Every citation names a file that exists and resolves as a symbol or an anchor, never a line (lane CITD)."""
    _assert_cites_resolve(doc)
