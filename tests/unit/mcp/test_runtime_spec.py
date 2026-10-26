"""A workflow spec can ask for "browser only if the change is UI" (#2322).

`runtime: "browser_if_ui"` on a step is sent as the step's `metadata.runtime`,
naming both profiles it may resolve to, on the browser profile (what it runs
when nothing resolves it). swarm-api is the authority on the pair; this pins
that the plugin spells the block, refuses any other value, and advertises it.
"""

from __future__ import annotations

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmError

POLICY = {"policy": "browser_if_ui", "browser": "claude-code-browser", "code": "claude-code"}


def _steps(review: dict) -> dict[str, dict]:
    return {
        s["step_id"]: s
        for s in workflows.build_steps([
            {"step_id": "implement", "prompt": "implement it"},
            {"step_id": "review", "prompt": "review it", "depends_on": ["implement"], **review},
        ])
    }


def test_browser_if_ui_is_sent_as_the_steps_runtime_block_on_the_browser():
    steps = _steps({"runtime": "browser_if_ui"})
    assert steps["review"]["runner_profile"] == "claude-code-browser"
    assert steps["review"]["metadata"] == {"runtime": POLICY}
    # A step that does not ask sends nothing new.
    assert "metadata" not in steps["implement"]
    assert steps["implement"]["runner_profile"] == "claude-code"


def test_naming_the_browser_profile_beside_it_is_accepted():
    steps = _steps({"runtime": "browser_if_ui", "runner_profile": "claude-code-browser"})
    assert steps["review"]["metadata"] == {"runtime": POLICY}


@pytest.mark.parametrize("value", ["browser", "always", True, {"policy": "browser_if_ui"}])
def test_any_other_runtime_is_refused_here(value):
    with pytest.raises(SwarmError, match="runtime must be"):
        _steps({"runtime": value})


def test_a_runtime_step_naming_another_profile_is_refused_here():
    with pytest.raises(SwarmError, match="names no runner_profile"):
        _steps({"runtime": "browser_if_ui", "runner_profile": "claude-code"})


def test_swarm_workflow_advertises_runtime():
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")["inputSchema"]
    step = schema["properties"]["steps"]["items"]["properties"]
    assert step["runtime"]["enum"] == ["browser_if_ui"]
