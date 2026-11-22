"""The archived issue 939 egress-open-delay document cites real code symbols.

WHY THIS EXISTS. docs/incidents/2026-10-09-egress-open-delay.md records the
egress probe's measurements and cites `path::Symbol` references into the
worker. A citation that no longer resolves sends the next reader looking for
code that moved or was renamed.

Reads files only: no network, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "incidents" / "2026-10-09-egress-open-delay.md"

EGRESS = "apps/agent-worker/agent_worker/egress.py"
GITOPS = "apps/agent-worker/agent_worker/gitops.py"
LIFECYCLE = "apps/agent-worker/agent_worker/lifecycle.py"

#: The symbols a reader of the probe needs; each must be cited, and resolve.
REQUIRED_CITES = [
    (EGRESS, "EgressProbe"),
    (GITOPS, "await_egress"),
    (LIFECYCLE, "Worker._start_egress_probe"),
    (LIFECYCLE, "Worker._mark_egress_ready"),
    (LIFECYCLE, "Worker._mark_clone_timed"),
]

_SYMBOL = re.compile(r"\b(apps/[\w./-]+\.py)::([\w.]+)")


def _value_after(name: str) -> re.Pattern[str]:
    """`NAME` followed by its number in seconds: "`NAME` = 80.0 s", "`NAME` (1 s)"."""
    return re.compile(r"`" + re.escape(name) + r"`\s*(?:=|\()\s*(\d+(?:\.\d+)?)\s*s\b")


def _text() -> str:
    assert DOC.is_file(), f"{DOC.relative_to(REPO)} does not exist"
    return DOC.read_text(encoding="utf-8")


def test_the_value_pattern_reads_what_it_should():
    """Not a sweep that matches nothing: the pattern reads each written form."""
    pattern = _value_after("EGRESS_PROBE_CAP_SECONDS")
    assert pattern.findall("`EGRESS_PROBE_CAP_SECONDS` = 80.0 s from") == ["80.0"]
    assert pattern.findall("`EGRESS_PROBE_CAP_SECONDS` (80 s)") == ["80"]
    assert pattern.findall("the cap `EGRESS_PROBE_CAP_SECONDS` stops it") == []


def test_every_symbol_citation_resolves():
    cites = _SYMBOL.findall(_text())
    assert cites, f"{DOC.name} cites no code"
    for path, qualname in cites:
        _cited_symbol(path, qualname)
    missing = [f"{p}::{q}" for p, q in REQUIRED_CITES if (p, q) not in cites]
    assert not missing, f"{DOC.name} does not cite {missing}"
