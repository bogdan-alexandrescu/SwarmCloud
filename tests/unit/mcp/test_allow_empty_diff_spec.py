"""A workflow spec can say a step may end with no change (`allow_empty_diff`).

The API has accepted the key on `WorkflowStepCreate` since #644, but
`build_steps` refuses any step key it does not know, so a spec carrying it was
refused at the keyboard before the API ever saw it (2026-10-06: the chunk-2
issue lanes could not set it from /sc:swarmcloud). These pin that it passes
through to the API body, that only a real boolean does, and that
`swarm_workflow` advertises it.
"""

from __future__ import annotations

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmError


def test_allow_empty_diff_passes_through_to_the_api_body():
    steps = {
        s["step_id"]: s
        for s in workflows.build_steps(
            [
                {"step_id": "implement", "prompt": "implement it", "allow_empty_diff": True},
                {"step_id": "review", "prompt": "review it", "depends_on": ["implement"]},
            ]
        )
    }
    assert steps["implement"]["allow_empty_diff"] is True
    # A step that does not set it sends nothing, so its body reads as it did before.
    assert "allow_empty_diff" not in steps["review"]


@pytest.mark.parametrize("value", ["true", 1, 0, [True]])
def test_an_allow_empty_diff_that_is_not_a_boolean_is_refused_here(value):
    step = {"step_id": "implement", "prompt": "x", "allow_empty_diff": value}
    with pytest.raises(SwarmError, match="allow_empty_diff"):
        workflows.build_steps([step])


def test_swarm_workflow_advertises_allow_empty_diff():
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")["inputSchema"]
    step = schema["properties"]["steps"]["items"]["properties"]
    assert step["allow_empty_diff"]["type"] == "boolean"
