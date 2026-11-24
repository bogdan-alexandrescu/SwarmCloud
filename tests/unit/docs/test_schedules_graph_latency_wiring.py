"""docs/schedules.md §3.20 says how `index.measure_latency_hours` is wired.

Part of issue 9. The section once said the registration setting "is not wired
yet", and so that every firing reads "no promoted version in the window
carries a measurement". It is wired: `repositories.measure_latency_hours_of`
reads it and `repoindex.py` passes it, through the signed prompt's
`MEASURE_LATENCY_LINE`, to the worker's `swarm-repo-graph write
--measure-latency-hours`. An operator who believed the doc would never turn
it on, and would read an unmeasured registration as a platform gap.

The test reads §3.20 alone, from its heading to the next `### ` or `## `
heading, and fails rather than passing over nothing when the heading is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCHEDULES = ROOT / "docs" / "schedules.md"
HEADING = "### 3.20 `graph-latency-report`"


def _section() -> str:
    text = SCHEDULES.read_text(encoding="utf-8")
    start = text.find(HEADING)
    assert start >= 0, f"{HEADING!r} is not in {SCHEDULES}"
    body = text[start + len(HEADING):]
    end = re.search(r"^#{2,3} ", body, flags=re.MULTILINE)
    return body[: end.start()] if end else body


def test_section_does_not_say_the_setting_is_unwired():
    section = " ".join(_section().split())
    assert "not wired yet" not in section


def test_section_names_what_wires_the_setting():
    section = " ".join(_section().split())
    for name in ("index.measure_latency_hours", "measure_latency_hours_of",
                 "MEASURE_LATENCY_HOURS_RANGE", "repoindex.py",
                 "--measure-latency-hours"):
        assert name in section, name
