"""PHONE-380 (part of #2136): every list of the live gate's device presets
names the same presets.

`agent_worker.livegate.presets.PRESETS` is the source: the size, scale, mobile
flag and user agent. swarm-api cannot import the worker, so it restates the
names twice (`swarm_api.qaspec.PRESETS`, what a QA target's `widths` may name,
and `swarm_api.live.LIVE_PRESETS`, the console's switcher catalogue); the
`swarm-handoff device` help in `agent_worker.handoff.AGENT_GUIDE` is read off
the source; `swarm_qa`'s TO_THE_END_PRESETS names a subset; and two docs write
the list out for a reader. A preset added to one copy only is a width a target
is refused, a device the console never offers, or one the agent is never told
about -- so this names the copy and the preset it is missing or adds.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from agent_worker import handoff as handoff_mod
from agent_worker.livegate.presets import PRESETS
from swarm_api import live as live_module
from swarm_api import qaspec

REPO = Path(__file__).resolve().parents[3]
SWARM_QA = REPO / "images" / "agent-runtime-browser" / "swarm_qa.py"
BROWSER_QA_DOC = REPO / "docs" / "browser-qa.md"
LIVE_BROWSER_DOC = REPO / "docs" / "design" / "live-browser.md"


def _swarm_qa():
    spec = importlib.util.spec_from_file_location("swarm_qa_preset_parity", SWARM_QA)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _guide_list() -> list[str]:
    guide = " ".join(handoff_mod.AGENT_GUIDE.split())
    found = re.search(r"switches the browser to a device preset by name \(([^)]*)\)", guide)
    assert found is not None, "AGENT_GUIDE no longer lists the presets"
    return [n.strip() for n in found.group(1).split(",")]


def _browser_qa_doc_list() -> dict[str, tuple[int, int]]:
    """docs/browser-qa.md's sentence: "switches between `desktop` (1440×900), ..."."""
    text = " ".join(BROWSER_QA_DOC.read_text(encoding="utf-8").split())
    found = re.search(r"switches between (.*?), and `browser_resize` is refused", text)
    assert found is not None, "docs/browser-qa.md no longer lists the presets"
    return {m[0]: (int(m[1]), int(m[2]))
            for m in re.findall(r"`([a-z0-9-]+)` \((\d+)×(\d+)\)", found.group(1))}


def _live_browser_doc_list() -> dict[str, tuple[int, int]]:
    """docs/design/live-browser.md §6.1's bullet list under "The presets"."""
    text = LIVE_BROWSER_DOC.read_text(encoding="utf-8")
    start = text.index("* **The presets** are a fixed, named list in the gate:")
    block = text[start:text.index("\n\n", start)]
    return {m[0]: (int(m[1]), int(m[2]))
            for m in re.findall(r"^  - `([a-z0-9-]+)` (\d+)×(\d+)", block, re.MULTILINE)}


def test_every_list_names_the_gates_presets_and_no_other() -> None:
    gate = list(PRESETS)
    copies = {
        "swarm_api.qaspec.PRESETS": list(qaspec.PRESETS),
        "swarm_api.live.LIVE_PRESETS": [p["name"] for p in live_module.LIVE_PRESETS],
        "agent_worker.handoff.AGENT_GUIDE (swarm-handoff device)": _guide_list(),
        "docs/browser-qa.md": list(_browser_qa_doc_list()),
        "docs/design/live-browser.md §6.1": list(_live_browser_doc_list()),
    }
    # MUTATION: add a preset to presets.py alone, or to any one copy, and this
    # names the copy and the preset.
    drift = {where: {"missing": [n for n in gate if n not in names],
                     "not_the_gates": [n for n in names if n not in gate]}
             for where, names in copies.items() if sorted(names) != sorted(gate)}
    assert drift == {}, drift
    for where, names in copies.items():
        assert names == gate, f"{where} lists the presets out of the gate's order: {names}"


def test_the_docs_give_each_preset_the_gates_size() -> None:
    gate = {p.name: (p.width, p.height) for p in PRESETS.values()}
    assert _browser_qa_doc_list() == gate
    assert _live_browser_doc_list() == gate


def test_the_capture_tools_to_the_end_presets_are_the_gates() -> None:
    to_the_end = _swarm_qa().TO_THE_END_PRESETS
    assert to_the_end <= set(PRESETS), sorted(to_the_end - set(PRESETS))
    assert set(qaspec.DEFAULT_WIDTHS) <= set(PRESETS)


def test_phone_380_is_in_every_list_at_380_by_800() -> None:
    """BGD, 2026-10-26: AI Studio's phone mock-up frame."""
    assert (PRESETS["phone-380"].width, PRESETS["phone-380"].height) == (380, 800)
    assert "phone-380" in qaspec.PRESETS
    assert {"name": "phone-380", "width": 380, "height": 800, "scale": 3.0,
            "mobile": True} in live_module.LIVE_PRESETS
    assert "phone-380" in _guide_list()
    assert "phone-380" in _swarm_qa().TO_THE_END_PRESETS
