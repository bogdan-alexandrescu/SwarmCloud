"""The visual-QA design states the gate's sign-in bounds as the code has them (part of #2688).

docs/design/visual-qa-any-app.md §2.2 names each of the gate's sign-in
constants with its value and its reason: `PAGE_SETTLED_SECONDS` (3 s),
`SIGNED_IN_SECONDS` (30 s), `SIGNED_IN_MAX_SECONDS` (60 s), `FORM_SECONDS`
(30 s), `START_CLICK_SECONDS` (10 s), `RECLICK_SECONDS` (5 s),
`MAX_RECLICKS` (2) and the worker's `FIRST_WAIT_SECONDS` (95 s). A change to
one of them in livegate/qatarget.py or qa_target.py that leaves the doc's
number behind fails here, and so does a doc that stops naming one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_worker import qa_target as worker_qt
from agent_worker.livegate import qatarget as gate_qt

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "design" / "visual-qa-any-app.md"

CONSTANTS = {
    "PAGE_SETTLED_SECONDS": gate_qt.PAGE_SETTLED_SECONDS,
    "SIGNED_IN_SECONDS": gate_qt.SIGNED_IN_SECONDS,
    "SIGNED_IN_MAX_SECONDS": gate_qt.SIGNED_IN_MAX_SECONDS,
    "FORM_SECONDS": gate_qt.FORM_SECONDS,
    "START_CLICK_SECONDS": gate_qt.START_CLICK_SECONDS,
    "RECLICK_SECONDS": gate_qt.RECLICK_SECONDS,
    "MAX_RECLICKS": gate_qt.MAX_RECLICKS,
    "FIRST_WAIT_SECONDS": worker_qt.FIRST_WAIT_SECONDS,
}

#: "`NAME` (3 s: ...", "`NAME` (30 s, ...", "`NAME` (10 s)" and
#: "`MAX_RECLICKS` times (2: ...", on the doc's text with its line breaks
#: flattened.
STATED = re.compile(r"`([A-Z][A-Z_]+)`(?: times)? \((\d+(?:\.\d+)?)(?: s)?[:,)]")


def _stated() -> list[tuple[str, float]]:
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    return [(name, float(value)) for name, value in STATED.findall(text) if name in CONSTANTS]


@pytest.mark.parametrize("name", sorted(CONSTANTS))
def test_the_doc_states_each_constant_with_its_value(name: str) -> None:
    values = [value for stated, value in _stated() if stated == name]
    assert values, f"{DOC.name} no longer states `{name}` with its value"
    assert all(value == CONSTANTS[name] for value in values), (
        f"{DOC.name} states `{name}` as {values}; the code has {CONSTANTS[name]}")

