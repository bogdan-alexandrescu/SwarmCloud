"""docs/design/live-browser.md §3.2's test-runner exception cites real code.

WHY THIS EXISTS (PW-TESTRUNNER-DOCS). The subsection documenting the
BROWSER-ENFORCE exception for a sanctioned test runner (`swarm-test-browsers`)
cites `path::Symbol` references into the worker. A citation that stops
resolving sends the next reader looking for code that moved or was renamed.
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "design" / "live-browser.md"
RUNNER = "apps/agent-worker/agent_worker/runners/claude_code.py"

SECTION = "### 3.2 Chromium is launched by the platform, and the agent connects to it"
NEXT = "### 3.2.1 dev-browser (opt-in)"
EXCEPTION = "#### The test-runner exception (PW-TESTRUNNER)"

_SYMBOL = re.compile(r"\b(apps/[\w./-]+\.py)::([\w.]+)")


def _flat(markdown: str) -> str:
    return re.sub(r"\s+", " ", markdown)


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(SECTION)
    return text[start: text.index(NEXT, start)]


def _exception() -> str:
    section = _section()
    assert EXCEPTION in section, (
        f"live-browser.md §3.2 has no {EXCEPTION!r} subsection: the sanctioned "
        "test-runner path (PW-TESTRUNNER) is undocumented where BROWSER-ENFORCE is"
    )
    return _flat(section[section.index(EXCEPTION):])


def test_every_symbol_the_exception_cites_exists():
    cited = _SYMBOL.findall(_exception())
    assert (RUNNER, "prepare_test_browsers") in cited, cited
    for path, qualname in cited:
        _cited_symbol(path, qualname)
