"""A `path::Qualname` citation in the docs names a symbol that exists.

archived issue 453 (2026-10-02): a merge wave moved code in `lifecycle.py` and
`control.py` by 100-300 lines, and line citations into them went on pointing at
whatever landed there. The docs now cite by symbol, which moves with the code;
this resolves each citation, so it fails only when the symbol is renamed or
deleted -- a link that no longer resolves. How a doc words its citations
(line numbers included) is not held here (issue 71: no test pins docs prose).
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
        # archived issue 453 (lane A-api-docs): its limits example cited control.py and
        # routes/admin.py by line, and both had moved by hundreds.
        REPO / "docs" / "multi-tenancy.md",
    ]
)

#: `lifecycle.py::Worker._x` or `.../agent_worker/control.py::ControlPlane.y`.
_SYMBOL = re.compile(r"(?:\bapps/agent-worker/agent_worker/)?\b(lifecycle|control)\.py::([\w.]+)")


def _rel(doc: Path) -> str:
    return str(doc.relative_to(REPO))


@pytest.mark.parametrize("doc", DOCS, ids=_rel)
def test_every_symbol_cited_in_lifecycle_or_control_exists(doc: Path):
    for module, qualname in _SYMBOL.findall(doc.read_text(encoding="utf-8")):
        _cited_symbol(f"{WORKER}/{module}.py", qualname)


def test_the_docs_are_found_and_cite_by_symbol():
    """Not an empty sweep: the docs exist and symbol citations are read from them."""
    assert all(doc.is_file() for doc in DOCS), DOCS
    cites = [m for doc in DOCS for m in _SYMBOL.findall(doc.read_text(encoding="utf-8"))]
    assert cites, "no lifecycle.py/control.py symbol citation found: the sweep checked nothing"


#: Every `path::Qualname` cite in docs/multi-tenancy.md, whatever the file.
_ANY_SYMBOL = re.compile(r"\b(apps/[\w./-]+\.py)::([\w.]+)")


def test_every_symbol_multi_tenancy_cites_exists():
    """Each `apps/...py::Qualname` citation in docs/multi-tenancy.md resolves."""
    text = (REPO / "docs" / "multi-tenancy.md").read_text(encoding="utf-8")
    for path, qualname in _ANY_SYMBOL.findall(text):
        _cited_symbol(path, qualname)


def test_a_worker_method_moved_into_a_mixin_still_resolves_and_a_missing_one_does_not():
    """`Worker._park_for_quota` lives in `parking.ParkMixin` (issue 68), which
    `Worker` inherits: a `lifecycle.py::Worker._park_for_quota` citation is
    still true. A member no base defines still fails."""
    source = _cited_symbol(f"{WORKER}/lifecycle.py", "Worker._park_for_quota")
    assert source.lstrip().startswith("def _park_for_quota(")
    with pytest.raises(AssertionError, match="no Worker._no_such_park"):
        _cited_symbol(f"{WORKER}/lifecycle.py", "Worker._no_such_park")
