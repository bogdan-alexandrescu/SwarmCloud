"""The archived issue 721 clone connect stall document cites real code, never by line.

WHY THIS EXISTS. docs/incidents/2026-10-06-clone-connect-stall.md cites the
egress probe's symbols (`path::Symbol`); each must still exist, and none may
cite the worker by line number, which drifts with every merge wave (archived
issue 453). It also links the earlier direct-VPC-egress incident document.
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "incidents" / "2026-10-06-clone-connect-stall.md"
EARLIER = "2026-09-25-worker-startup-network.md"

EGRESS = "apps/agent-worker/agent_worker/egress.py"
GITOPS = "apps/agent-worker/agent_worker/gitops.py"
LIFECYCLE = "apps/agent-worker/agent_worker/lifecycle.py"

#: The symbols a reader of the probe needs; each must be cited, and resolve.
REQUIRED_CITES = [
    (EGRESS, "EgressProbe"),
    (EGRESS, "EgressProbe.result"),
    (EGRESS, "probe_targets"),
    (GITOPS, "await_egress"),
    (GITOPS, "PeerPin"),
    (GITOPS, "connect_stalled"),
    (GITOPS, "clone_phase_timings"),
    (LIFECYCLE, "Worker._start_egress_probe"),
    (LIFECYCLE, "Worker._add_egress_target"),
    (LIFECYCLE, "Worker._mark_egress_ready"),
]

#: `path::Qualname`, as the document cites code.
_SYMBOL = re.compile(r"\b(apps/[\w./-]+\.py)::([\w.]+)")

#: `lifecycle.py:123`, `gitops.py:~45`: a line number, which drifts.
_LINE = re.compile(r"\b(?:lifecycle|gitops|egress)\.py:~?\d")


def _value_after(name: str) -> re.Pattern[str]:
    """`NAME` (optionally `path::NAME`) followed by its number in seconds:
    "`EGRESS_PROBE_CAP_SECONDS` = 80 s", "`...::NAME`, 80 s", "`NAME` (80 s)"."""
    return re.compile(
        r"`(?:[\w./-]+::)?" + re.escape(name) + r"`\s*(?:=|is|,|\()\s*(\d+(?:\.\d+)?)\s*s\b"
    )


def _text() -> str:
    assert DOC.is_file(), f"{DOC.relative_to(REPO)} does not exist"
    return DOC.read_text(encoding="utf-8")


def test_every_symbol_citation_resolves_and_none_is_by_line():
    text = _text()
    cites = _SYMBOL.findall(text)
    for path, qualname in cites:
        _cited_symbol(path, qualname)
    missing = [f"{p}::{q}" for p, q in REQUIRED_CITES if (p, q) not in cites]
    assert not missing, f"{DOC.name} does not cite {missing}"
    stale = [
        f"{n}: {line.strip()[:120]}"
        for n, line in enumerate(text.splitlines(), 1)
        if _LINE.search(line)
    ]
    assert not stale, f"{DOC.name} cites the worker by line number, which drifts: {stale}"


def test_the_value_pattern_reads_what_it_should():
    """Not a sweep that matches nothing: the pattern reads each written form."""
    pattern = _value_after("EGRESS_PROBE_CAP_SECONDS")
    assert pattern.findall("`EGRESS_PROBE_CAP_SECONDS` = 80 s") == ["80"]
    assert pattern.findall(f"`{EGRESS}::EGRESS_PROBE_CAP_SECONDS`, 80 s,") == ["80"]
    assert pattern.findall("`EGRESS_PROBE_CAP_SECONDS` (80.0 s)") == ["80.0"]
    assert pattern.findall("the cap `EGRESS_PROBE_CAP_SECONDS` stops it") == []


def test_the_line_pattern_catches_a_line_citation():
    assert _LINE.search("see `lifecycle.py:4160`") and _LINE.search("`gitops.py:~836`")
    assert not _LINE.search(f"`{GITOPS}::await_egress`")


def test_it_links_the_earlier_direct_vpc_egress_incident():
    assert f"]({EARLIER})" in _text()
    assert (DOC.parent / EARLIER).is_file()
