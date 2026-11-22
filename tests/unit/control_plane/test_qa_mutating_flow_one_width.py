"""A mutating QA flow runs at one width; a read-only flow at every width (a QA user's report, 2026-11-21).

A QA run executed each flow once per viewport width (desktop and phone-390),
so a flow with `mutating: true` and `writes` performed its declared writes
once a width: a send-once flow sent at desktop and again at phone-390, the
declared budget doubled without anyone saying so, and at phone-390 its typing
was cut short before Send. The decided fix:

  * a mutating flow runs at the leg's first width only (desktop by default),
    so its brief records exactly one `flow-<id>-<width>.gif`;
  * a read-only flow keeps running, and recording, at every width;
  * the run's description says the writing flows run at that one width, and
    the flow report shows that width's run, never a missing phone result.

No credentials, no network: the specs are rendered through `qaspec.render`.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qaspec

WRITES = "Sends one message to the test inbox."


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_0123456789abcdef", "name": "Site", "base_url": "https://site.example.com",
        "repository": "https://github.com/example-org/site", "environment": "non_production",
        "screens": [{"id": "home", "path": "/"}],
        "flows": [
            {"id": "send", "goal": "send one message", "start_path": "/inbox", "mutating": True,
             "writes": WRITES},
            {"id": "browse", "goal": "open the newest message", "start_path": "/inbox"},
        ],
    }
    target.update(over)
    return target


def _steps(spec: dict[str, Any]) -> dict[str, str]:
    return {s["step_id"]: s.get("prompt", "") for s in spec["steps"]}


def _recorded(prompt: str, fid: str, widths: tuple[str, ...]) -> list[str]:
    return [w for w in widths if f"`swarm-handoff record start flow-{fid}-{w}`" in prompt]


def test_flow_widths_is_the_first_width_for_a_mutating_flow_and_every_width_otherwise() -> None:
    widths = ("desktop", "phone-390", "ipad-7")
    assert qaspec.flow_widths({"mutating": True, "writes": WRITES}, widths) == ("desktop",)
    assert qaspec.flow_widths({"mutating": False}, widths) == widths
    assert qaspec.flow_widths({}, widths) == widths


def test_a_mutating_flow_records_exactly_one_width() -> None:
    steps = _steps(qaspec.render(_target(), file="none"))
    prompt = steps["flow-send"]
    # MUTATION: iterate every width in `_flow_prompt` and phone-390 is recorded too.
    assert _recorded(prompt, "send", qaspec.DEFAULT_WIDTHS) == ["desktop"]
    assert "swarm-handoff device phone-390" not in prompt
    assert "once per width" not in prompt
    assert "at one width only" in prompt


@pytest.mark.parametrize("widths", [["phone-390", "desktop"], ["ipad-7", "desktop", "phone-390"]])
def test_a_mutating_flow_runs_at_the_legs_first_width(widths) -> None:
    prompt = _steps(qaspec.render(_target(widths=widths), file="none"))["flow-send"]
    assert _recorded(prompt, "send", qaspec.PRESETS) == [widths[0]]


def test_a_read_only_flow_still_records_every_width() -> None:
    steps = _steps(qaspec.render(_target(), file="none"))
    batch = steps["flows-1"]
    assert "`swarm-handoff record start flow-<id>-<width>`" in batch
    assert f"at each width of {', '.join(qaspec.DEFAULT_WIDTHS)}" in batch
    assert "`send`" not in batch, "a mutating flow is never batched"


def test_the_single_flow_brief_keeps_every_width_for_a_read_only_flow() -> None:
    target = _target()
    flow = {"id": "browse", "goal": "open the newest message", "start_path": "/inbox", "mutating": False}
    prompt = qaspec._flow_prompt(target, qaspec.base_url(target), flow, qaspec.DEFAULT_WIDTHS, False,
                                 qaspec.flows_browser_tool(target, None))
    assert _recorded(prompt, "browse", qaspec.DEFAULT_WIDTHS) == list(qaspec.DEFAULT_WIDTHS)
    assert "once per width" in prompt


def test_the_flow_report_shows_the_mutating_flows_single_width() -> None:
    report = _steps(qaspec.render(_target(), file="none"))["flow-report"]
    # MUTATION: drop the line and the report reads the missing phone run as a failure.
    assert "Run at desktop only: this flow writes, so it runs at one width" in report
    assert "never report its other widths as missing" in report


def test_the_run_description_says_the_writing_flow_runs_at_one_width() -> None:
    spec = qaspec.render(_target(), file="none")
    assert "2 flows are run (1 writing flow at desktop only)" in spec["description"]


def test_a_leg_with_no_writing_flow_describes_its_flows_as_before() -> None:
    target = _target(flows=[{"id": "browse", "goal": "open the newest message", "start_path": "/inbox"}])
    description = qaspec.render(target, file="none")["description"]
    assert "1 flows are run." in description
    assert "writing flow" not in description
