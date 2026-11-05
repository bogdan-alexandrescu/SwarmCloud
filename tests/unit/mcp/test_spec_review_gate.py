"""A workflow spec can set a review step's gate (`review_gate`).

FOUND 5 November 2026. swarm-api takes a step's `metadata.review_gate` --
`false` switches the gate off for that review, a block sets `min_lines` and
`risk_paths` (`validation.resolve_review_gate`) -- but `build_steps` refused
every step key outside `_STEP_KEYS`, so a spec written in Claude Code was
refused at the keyboard ("step[1] carries ['review_gate'], which this tool
does not send") and could not switch the gate off for a mock-up review the
gate skipped. These pin that `review_gate` is sent as the step's
`metadata.review_gate`, that a malformed one is refused before anything is
submitted, and that `metadata` itself is still refused (invariant 10).
"""

from __future__ import annotations

import pytest

from swarm_mcp import workflows
from swarm_mcp.client import SwarmError


def _steps(gate):
    return [
        {"step_id": "build", "prompt": "build the mock-up"},
        {"step_id": "review", "prompt": "review it", "depends_on": ["build"],
         "review_gate": gate},
    ]


def _review(gate):
    return {s["step_id"]: s for s in workflows.build_steps(_steps(gate))}["review"]


def test_false_switches_the_gate_off_as_metadata_review_gate():
    review = _review(False)
    assert review["metadata"] == {"review_gate": False}
    assert "review_gate" not in review


def test_a_block_passes_through_as_metadata_review_gate():
    gate = {"min_lines": 40, "risk_paths": ["apps/redaction/**", "terraform/**/iam*"]}
    assert _review(gate)["metadata"] == {"review_gate": gate}


def test_a_step_without_review_gate_sends_no_metadata():
    steps = {s["step_id"]: s for s in workflows.build_steps(_steps(False))}
    assert "metadata" not in steps["build"]


def test_review_gate_sits_beside_a_runtime_in_one_metadata():
    steps = _steps({"min_lines": 0})
    steps[1]["runtime"] = "browser_if_ui"
    review = {s["step_id"]: s for s in workflows.build_steps(steps)}["review"]
    assert review["metadata"]["review_gate"] == {"min_lines": 0}
    assert review["metadata"]["runtime"]["policy"] == "browser_if_ui"


BAD_GATES = [
    True,
    None,
    "off",
    0,
    [],
    {"min_lines": 300, "enabled": False},
    {"min_lines": -1},
    {"min_lines": True},
    {"min_lines": "300"},
    {"min_lines": 300.0},
    {"min_lines": 1_000_001},
    {"risk_paths": []},
    {"risk_paths": "apps/**"},
    {"risk_paths": ["/etc/passwd"]},
    {"risk_paths": ["  "]},
    {"risk_paths": ["apps/\nx"]},
    {"risk_paths": [7]},
    {"risk_paths": ["a" * 201]},
    {"risk_paths": ["apps/**"] * 101},
    {"requirements_check": "yes"},
]


@pytest.mark.parametrize("gate", BAD_GATES)
def test_a_malformed_review_gate_is_refused_before_submission(gate):
    # `build_steps` builds the body `submit` sends; refusing here sends nothing.
    with pytest.raises(SwarmError, match="review_gate"):
        workflows.build_steps(_steps(gate))


def test_metadata_itself_is_still_refused_with_the_sentence():
    step = {"step_id": "review", "prompt": "review it",
            "metadata": {"review_gate": False}}
    with pytest.raises(SwarmError) as refused:
        workflows.build_steps([step])
    message = str(refused.value)
    assert "carries ['metadata'], which this tool does not send" in message
    assert "'review_gate'" in message.split("Accepted:", 1)[1]
    assert "An image, a command, a backend or cpu and memory figures are never sent." in message


def test_the_copied_bounds_are_the_apis():
    from swarm_api import validation

    assert workflows.REVIEW_GATE_KEYS == validation.REVIEW_GATE_KEYS
    assert workflows.REVIEW_GATE_MAX_MIN_LINES == validation.REVIEW_GATE_MAX_MIN_LINES
    assert workflows.REVIEW_GATE_MAX_RISK_PATHS == validation.REVIEW_GATE_MAX_RISK_PATHS
    assert workflows.REVIEW_GATE_MAX_PATH_CHARS == validation.REVIEW_GATE_MAX_PATH_CHARS


GOOD_GATES = [
    False,
    {},
    {"min_lines": 0},
    {"min_lines": 300, "risk_paths": ["apps/redaction/"]},
    {"risk_paths": ["terraform/**/iam*"]},
    {"requirements_check": True},
]


@pytest.mark.parametrize("gate", GOOD_GATES + BAD_GATES)
def test_the_plugin_refuses_exactly_what_swarm_api_refuses(gate):
    """Every shape the bridge sends, swarm-api takes; every one it refuses, swarm-api would."""
    from swarm_api.validation import ReviewGateInvalid, _checked_review_gate

    try:
        workflows.check_review_gate(gate, where="step 'review'")
        plugin_ok = True
    except SwarmError:
        plugin_ok = False
    try:
        _checked_review_gate({"review_gate": gate}, step_id="review", workflow_step=True)
        api_ok = True
    except ReviewGateInvalid:
        api_ok = False
    assert plugin_ok == api_ok
