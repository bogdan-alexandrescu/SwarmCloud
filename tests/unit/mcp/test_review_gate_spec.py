"""A workflow spec can say "review, then fix only if the review says so" (#264).

`build_steps` refuses any step key it does not know, so a spec carrying the
review shape's `when` and `builds_on` was refused at the keyboard before the
API ever saw it. These pin that both keys pass through to the
`WorkflowStepCreate` body unchanged, that their shape is checked here, and
that `swarm_workflow` advertises them. Whether a gate or a base is LEGAL for a
given DAG and strategy is `swarm_api.validation`'s rule, and is not restated.
"""

from __future__ import annotations

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmError

_SHAPE = [
    {"step_id": "implement", "prompt": "implement it"},
    {"step_id": "review", "prompt": "review it", "depends_on": ["implement"],
     "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"}},
    {"step_id": "fix", "prompt": "fix it", "depends_on": ["review"],
     "builds_on": "implement", "input_from": {"review": "verdict.json"},
     "when": {"step": "review", "verdict_in": ["NOT_YET"]}},
]


def test_the_review_shape_passes_through_to_the_api_body():
    steps = {s["step_id"]: s for s in workflows.build_steps(_SHAPE)}
    assert steps["fix"]["when"] == {"step": "review", "verdict_in": ["NOT_YET"]}
    assert steps["fix"]["builds_on"] == "implement"
    assert steps["review"]["builds_on"] == "implement"
    assert "when" not in steps["implement"]
    assert "builds_on" not in steps["implement"]


@pytest.mark.parametrize(
    "when",
    [
        "review",
        {"step": "review"},
        {"verdict_in": ["NOT_YET"]},
        {"step": "review", "verdict_in": "NOT_YET"},
        {"step": "review", "verdict_in": [1]},
        {"step": "review", "verdict_in": ["NOT_YET"], "file": "x.json"},
    ],
)
def test_a_malformed_when_is_refused_here(when):
    step = {"step_id": "fix", "prompt": "fix", "when": when}
    with pytest.raises(SwarmError, match="when"):
        workflows.build_steps([step])


def test_a_builds_on_that_is_not_a_step_id_string_is_refused_here():
    with pytest.raises(SwarmError, match="builds_on"):
        workflows.build_steps([{"step_id": "fix", "prompt": "fix", "builds_on": ["a"]}])


def test_swarm_workflow_advertises_both_keys():
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")["inputSchema"]
    step = schema["properties"]["steps"]["items"]["properties"]
    assert "when" in step
    assert "builds_on" in step
