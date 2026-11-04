"""swarm-api's segment bound is the capture tool's, and the report counts by it (part of #2688).

On 2026-11-04 AI Studio's staging run cut its /docs page (about 49,600 px at
the desktop, 65,300 px at 390 px) at the capture's segment cap, and the
capture now takes every segment of a page up to `MAX_SEGMENTS`. swarm-api
restates that bound to refuse a screen's `segments` at registration, so:

* the two statements are equal (docs/mirrored-values.md's rule: a restated
  value is held equal by a test);
* a screen may name the whole bound, and one past it is refused;
* the report no longer counts a missing capture's images at four segments a
  route, which a page captured to its end does not take.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from swarm_api import qaspec

REPO = Path(__file__).resolve().parents[3]
SWARM_QA = REPO / "images" / "agent-runtime-browser" / "swarm_qa.py"


def _swarm_qa():
    spec = importlib.util.spec_from_file_location("swarm_qa_segment_bound", SWARM_QA)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _target(**extra: Any) -> dict[str, Any]:
    return {
        "id": "qat_3f9a", "revision": 4, "name": "Studio", "tenant_id": "tenant-a",
        "base_url": "https://app.example.com", "repository": "https://github.com/example-org/studio",
        "sign_in": {"kind": "none"}, "screens": [{"id": "home", "path": "/"}], "flows": [],
        "environment": "production", **extra,
    }


def test_swarm_api_states_the_capture_tools_segment_bound():
    # MUTATION: raise one MAX_SEGMENTS and not the other and this fails.
    assert qaspec.MAX_SEGMENTS == _swarm_qa().MAX_SEGMENTS


def test_the_bound_holds_the_measured_pages_whole():
    # 65,300 px at phone-390's 844 px viewport is 78 viewports; 49,600 px at
    # the desktop's 900 px is 56.
    # MUTATION: leave the bound at 12 and this fails.
    assert qaspec.MAX_SEGMENTS * 844 >= 65_300 and qaspec.MAX_SEGMENTS * 900 >= 49_600


def test_a_screen_may_name_the_whole_bound_and_no_more():
    qaspec.render(_target(screens=[{"id": "docs", "path": "/docs", "segments": qaspec.MAX_SEGMENTS}]))
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(screens=[{"id": "docs", "path": "/docs", "segments": qaspec.MAX_SEGMENTS + 1}]))


def test_the_report_does_not_count_a_missing_capture_at_four_segments_a_route():
    report = {s["step_id"]: s["prompt"] for s in qaspec.render(_target())["steps"]}["report"]
    # MUTATION: keep "(4 when unset)" and a missing tall capture is counted short.
    assert "4 when unset" not in report
    assert "at least" in report and "to its end" in report
    # MUTATION: drop the `truncated` sentence and a page cut at the bound reads as whole.
    assert "`truncated`" in report
