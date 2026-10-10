"""The #939 egress-open-delay document keeps its numbers, caveats and open decision.

WHY THIS EXISTS. docs/incidents/2026-10-09-egress-open-delay.md records why a
new Cloud Run instance's internet path opens a median 20 s after process start
against about 1 s on GKE, ranks the causes, and leaves the decision to the
owner. What would quietly rot: the probe's constants moving under the numbers
the document argues from; a `path::Symbol` it cites disappearing; the n < 30
caveat or the "awaiting the owner" status being edited away so the record reads
as a passed bar or a made decision; and the shell wrapper's 302 defect being
dropped before its fix lands.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "incidents" / "2026-10-09-egress-open-delay.md"

EGRESS = "apps/agent-worker/agent_worker/egress.py"
GITOPS = "apps/agent-worker/agent_worker/gitops.py"
LIFECYCLE = "apps/agent-worker/agent_worker/lifecycle.py"

#: Each constant the document gives a number for, and its `agent_worker`
#: module. Imported inside the test: a top-level import makes every
#: collection of tests/unit/scripts load the worker.
CONSTANTS = {
    "EGRESS_PROBE_CAP_SECONDS": "egress",
    "EGRESS_PROBE_INTERVAL_SECONDS": "egress",
    "EGRESS_CONNECT_TIMEOUT_SECONDS": "egress",
    "EGRESS_CLONE_WAIT_SECONDS": "gitops",
}

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


def _section(text: str, marker: str) -> str:
    match = re.search(r"^## [^\n]*" + re.escape(marker) + r"[^\n]*$", text, flags=re.M)
    assert match, f"no '## ...{marker}...' section in {DOC.name}"
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


@pytest.mark.parametrize("name", sorted(CONSTANTS))
def test_every_number_given_for_a_constant_matches_the_code(name: str):
    value = getattr(importlib.import_module("agent_worker." + CONSTANTS[name]), name)
    given = [float(v) for v in _value_after(name).findall(_text())]
    assert given, f"{DOC.name} gives no number next to `{name}` (want `{name}` = {value:g} s)"
    wrong = [v for v in given if v != float(value)]
    assert not wrong, f"{DOC.name} gives `{name}` as {wrong} s; the code says {value:g} s"


def test_the_value_pattern_reads_what_it_should():
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


def test_the_measurement_keeps_its_sample_size_caveat():
    """19 Cloud Run marks is under the bar's 30: the verdict is insufficient n,
    and the doc must still say the median is far from passing."""
    measured = _section(_text(), "What was measured")
    for needle in ("19", "insufficient n", "20.2", "1.17", "under the issue's bar of 30"):
        assert needle in measured, f"§1 of {DOC.name} lost {needle!r}"


def test_the_decision_is_left_to_the_owner():
    decision = _section(_text(), "Decision")
    assert "Recommended, awaiting the owner" in decision
    options = re.findall(r"^### Option [A-Z]\b", decision, flags=re.M)
    assert 2 <= len(options) <= 5, f"the Decision section has {len(options)} options"
    assert "(recommended)" in decision


def test_the_syn_backoff_reading_stays_an_inference():
    assert re.search(r"SYN-backoff reading[^.]*\*\*inference\*\*", _text())


def test_the_wrapper_defect_is_recorded_with_its_fix():
    text = _text()
    assert "HTTP 302" in text and "page.json" in text
    assert "trap 'exit 143' TERM" in text, "the wrapper's fix is no longer written out"
