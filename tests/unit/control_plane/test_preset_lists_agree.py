"""PHONE-380 (part of archived issue 2136): every list of the live gate's device presets
names the same presets.

`agent_worker.livegate.presets.PRESETS` is the source: the size, scale, mobile
flag and user agent. swarm-api cannot import the worker, so it restates the
names twice (`swarm_api.qaspec.PRESETS`, what a QA target's `widths` may name,
and `swarm_api.live.LIVE_PRESETS`, the console's switcher catalogue); the
`swarm-handoff device` help in `agent_worker.handoff.AGENT_GUIDE` is read off
the source; and `swarm_qa`'s TO_THE_END_PRESETS names a subset. A preset added to one copy only is a width a target
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


def test_every_list_names_the_gates_presets_and_no_other() -> None:
    gate = list(PRESETS)
    copies = {
        "swarm_api.qaspec.PRESETS": list(qaspec.PRESETS),
        "swarm_api.live.LIVE_PRESETS": [p["name"] for p in live_module.LIVE_PRESETS],
        "agent_worker.handoff.AGENT_GUIDE (swarm-handoff device)": _guide_list(),
    }
    # MUTATION: add a preset to presets.py alone, or to any one copy, and this
    # names the copy and the preset.
    drift = {where: {"missing": [n for n in gate if n not in names],
                     "not_the_gates": [n for n in names if n not in gate]}
             for where, names in copies.items() if sorted(names) != sorted(gate)}
    assert drift == {}, drift
    for where, names in copies.items():
        assert names == gate, f"{where} lists the presets out of the gate's order: {names}"


def test_the_capture_tools_to_the_end_presets_are_the_gates() -> None:
    to_the_end = _swarm_qa().TO_THE_END_PRESETS
    assert to_the_end <= set(PRESETS), sorted(to_the_end - set(PRESETS))
    assert set(qaspec.DEFAULT_WIDTHS) <= set(PRESETS)


def test_phone_380_is_in_every_list_at_380_by_800() -> None:
    """the owner, 2026-10-26: A peer application's phone mock-up frame."""
    assert (PRESETS["phone-380"].width, PRESETS["phone-380"].height) == (380, 800)
    assert "phone-380" in qaspec.PRESETS
    assert {"name": "phone-380", "width": 380, "height": 800, "scale": 3.0,
            "mobile": True} in live_module.LIVE_PRESETS
    assert "phone-380" in _guide_list()
    assert "phone-380" in _swarm_qa().TO_THE_END_PRESETS
