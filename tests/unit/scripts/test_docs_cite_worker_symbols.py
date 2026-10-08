"""Docs cite the worker's lifecycle.py and control.py by symbol, never by line.

#453 (reported by the #522 conflict fixer, 2026-10-02): the 2026-10-02 merge
wave moved code in `lifecycle.py` and `control.py` by 100-300 lines, and every
`lifecycle.py:NNNN` citation in docs/web-ui, docs/design/child-tasks.md and
docs/contract-change-requests.md went on pointing at whatever landed there.
Only the docs the spec-amendment tests read were held. Those citations now
name the function (`path::Worker._upload_outputs`), which moves with the code
and fails here only when it is renamed or deleted -- when the doc is wrong.

Both checks are needed: the first stops a line number coming back, the second
stops a symbol citation from naming something that no longer exists.

The rest of docs/web-ui still cites other files by line (store.py, App.tsx,
...); that is outside #453's box and not held here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]

WORKER = "apps/agent-worker/agent_worker"

DOCS = sorted(
    [
        *(REPO / "docs" / "web-ui").rglob("*.md"),
        *(REPO / "docs" / "web-ui").rglob("*.html"),
        REPO / "docs" / "design" / "child-tasks.md",
        REPO / "docs" / "contract-change-requests.md",
        # #453 (lane A-api-docs): its limits example cited control.py and
        # routes/admin.py by line, and both had moved by hundreds.
        REPO / "docs" / "multi-tenancy.md",
    ]
)

#: `lifecycle.py:123`, `control.py:~45-67`, with or without a path before it.
_LINE = re.compile(r"\b(?:lifecycle|control)\.py:~?\d")

#: `lifecycle.py::Worker._x` or `.../agent_worker/control.py::ControlPlane.y`.
_SYMBOL = re.compile(r"(?:\bapps/agent-worker/agent_worker/)?\b(lifecycle|control)\.py::([\w.]+)")


def _rel(doc: Path) -> str:
    return str(doc.relative_to(REPO))


@pytest.mark.parametrize("doc", DOCS, ids=_rel)
def test_no_line_number_into_lifecycle_or_control(doc: Path):
    text = doc.read_text(encoding="utf-8")
    stale = [
        f"{n}: {line.strip()[:120]}"
        for n, line in enumerate(text.splitlines(), 1)
        if _LINE.search(line)
    ]
    assert not stale, f"{_rel(doc)} cites lifecycle.py/control.py by line number, which drifts: {stale}"


@pytest.mark.parametrize("doc", DOCS, ids=_rel)
def test_every_symbol_cited_in_lifecycle_or_control_exists(doc: Path):
    for module, qualname in _SYMBOL.findall(doc.read_text(encoding="utf-8")):
        _cited_symbol(f"{WORKER}/{module}.py", qualname)


def test_the_docs_are_found_and_cite_by_symbol():
    """Not an empty sweep: the docs exist and the symbol form is read from them."""
    assert len(DOCS) > 10, DOCS
    cites = [m for doc in DOCS for m in _SYMBOL.findall(doc.read_text(encoding="utf-8"))]
    assert len(cites) > 50, cites
    assert _LINE.search("see `lifecycle.py:474`") and _LINE.search("(`control.py:~410-414`)")
    assert not _LINE.search("`lifecycle.py::Worker._collect_spend`")


#: Every `path::Qualname` cite in docs/multi-tenancy.md, whatever the file: its
#: limits example cites the admin route as well as the worker.
_ANY_SYMBOL = re.compile(r"\b(apps/[\w./-]+\.py)::([\w.]+)")


def test_multi_tenancy_cites_the_budget_refusal_and_record_spend_by_symbol():
    """Both cites #453 converted resolve, and the refusal is in the one it names."""
    text = (REPO / "docs" / "multi-tenancy.md").read_text(encoding="utf-8")
    cites = _ANY_SYMBOL.findall(text)
    assert ("apps/swarm-api/swarm_api/routes/admin.py", "set_tenant_limits") in cites, cites
    assert ("apps/agent-worker/agent_worker/control.py", "ControlPlane.record_spend") in cites, cites
    for path, qualname in cites:
        _cited_symbol(path, qualname)
    assert "monthly_budget_usd" in _cited_symbol("apps/swarm-api/swarm_api/routes/admin.py", "set_tenant_limits")
    assert not re.search(r"routes/admin\.py:~?\d", text), "multi-tenancy.md cites routes/admin.py by line"
