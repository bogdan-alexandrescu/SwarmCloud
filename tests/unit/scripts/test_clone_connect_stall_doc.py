"""The #721 clone connect stall document says what the egress probe's code does.

WHY THIS EXISTS. #721 measured a step's clone spending a median 36.6 s in one
TCP connect to GitHub. The owner chose (a), the egress probe, on 2026-10-06,
and it shipped (`agent_worker.egress`, `gitops.await_egress`, `gitops.PeerPin`)
with its reasons only in docstrings. docs/incidents/2026-10-06-clone-connect-stall.md
records why each constant has its value, so the next change to one has to
argue with the measurement. A document of numbers drifts the day a constant
moves, so each number it gives for a constant is read here against the code,
next to the constant's name; each `path::Symbol` it cites must still exist;
and it never cites the worker by line number, which moves with every merge
wave (#453).

It also holds what the document must not lose: the SYN-backoff reading stays
labelled an inference, the fields to read to confirm the estimated saving are
named, and (b) and (c) stay recorded as open and not started rather than
quietly dropped.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "incidents" / "2026-10-06-clone-connect-stall.md"
EARLIER = "2026-09-25-worker-startup-network.md"

EGRESS = "apps/agent-worker/agent_worker/egress.py"
GITOPS = "apps/agent-worker/agent_worker/gitops.py"
LIFECYCLE = "apps/agent-worker/agent_worker/lifecycle.py"

#: Each constant the document gives a number for, and the `agent_worker`
#: module that defines it. The module is imported inside the test, not here:
#: a top-level import makes every collection of tests/unit/scripts load the
#: worker (test_collection_stays_cheap).
CONSTANTS = {
    "EGRESS_PROBE_CAP_SECONDS": "egress",
    "EGRESS_PROBE_INTERVAL_SECONDS": "egress",
    "EGRESS_CONNECT_TIMEOUT_SECONDS": "egress",
    "EGRESS_CLONE_WAIT_SECONDS": "gitops",
    "CLONE_CONNECT_STALL_SECONDS": "gitops",
}

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


def _section(text: str, marker: str) -> str:
    """The body of the first `##` heading containing `marker`, to the next `##`."""
    match = re.search(r"^## [^\n]*" + re.escape(marker) + r"[^\n]*$", text, flags=re.M)
    assert match, f"no '## ...{marker}...' section in {DOC.name}"
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def test_the_document_exists():
    assert DOC.is_file(), f"{DOC.relative_to(REPO)} does not exist"


@pytest.mark.parametrize("name", sorted(CONSTANTS))
def test_every_number_given_for_a_constant_matches_the_code(name: str):
    """Each constant is given a number next to its name at least once, and
    every number given there is the code's: a moved constant fails here."""
    value = getattr(importlib.import_module("agent_worker." + CONSTANTS[name]), name)
    text = _text()
    given = [float(v) for v in _value_after(name).findall(text)]
    assert given, f"{DOC.name} gives no number next to `{name}` (want `{name}` = {value:g} s)"
    wrong = [v for v in given if v != float(value)]
    assert not wrong, f"{DOC.name} gives `{name}` as {wrong} s; the code says {value:g} s"


def test_the_value_pattern_reads_what_it_should():
    """Not a sweep that matches nothing: the pattern reads each written form."""
    pattern = _value_after("EGRESS_PROBE_CAP_SECONDS")
    assert pattern.findall("`EGRESS_PROBE_CAP_SECONDS` = 80 s") == ["80"]
    assert pattern.findall(f"`{EGRESS}::EGRESS_PROBE_CAP_SECONDS`, 80 s,") == ["80"]
    assert pattern.findall("`EGRESS_PROBE_CAP_SECONDS` (80.0 s)") == ["80.0"]
    assert pattern.findall("the cap `EGRESS_PROBE_CAP_SECONDS` stops it") == []


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


def test_the_line_pattern_catches_a_line_citation():
    assert _LINE.search("see `lifecycle.py:4160`") and _LINE.search("`gitops.py:~836`")
    assert not _LINE.search(f"`{GITOPS}::await_egress`")


@pytest.mark.parametrize(
    "name", ["egress_ready_seconds", "probe_attempts", "connect_seconds", "egress_ready", "clone_timed"]
)
def test_the_document_names_the_fields_and_marks_to_read(name: str):
    assert f"`{name}`" in _text(), f"{DOC.name} does not name `{name}`"


def test_the_syn_backoff_reading_stays_an_inference():
    """The doubling spacing fits SYN retransmission; the timings do not prove it."""
    paragraphs = [p for p in re.split(r"\n\s*\n", _text()) if "SYN" in p and "backoff" in p.lower()]
    assert paragraphs, f"{DOC.name} does not give the SYN-backoff reading"
    assert any("inference" in p for p in paragraphs), (
        f"{DOC.name} gives the SYN-backoff reading without calling it an inference"
    )


def test_the_saving_is_recorded_as_not_yet_confirmed():
    """The ~10 s per step is the issue's estimate; no post-probe figure is recorded."""
    confirm = _section(_text(), "confirm")
    assert "not yet confirmed" in confirm
    for field in ("egress_ready_seconds", "connect_seconds", "total_seconds"):
        assert f"`{field}`" in confirm, f"the confirmation section does not name `{field}`"


@pytest.mark.parametrize("item", ["(b)", "(c)"])
def test_b_and_c_are_recorded_open_and_not_started(item: str):
    section = _section(_text(), item)
    assert "**Status: not started.**" in section, f"{item} is not marked not started"
    assert "open" in section.lower(), f"{item} is not marked open"


def test_it_links_the_earlier_direct_vpc_egress_incident():
    assert f"]({EARLIER})" in _text()
    assert (DOC.parent / EARLIER).is_file()
