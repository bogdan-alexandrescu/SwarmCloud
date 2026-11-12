"""A workflow spec step can start from its own ref (`repository_ref`, #2895).

swarm-api takes the key on `WorkflowStepCreate` and writes it as the step
task's own `repository_ref`; `build_steps` refuses any step key it does not
know, so without it the spec was refused at the keyboard before the API ever
saw it. These pin that a non-empty string passes through as given, that
anything else is refused naming the step, and that a step without it sends
exactly the body it did before. Whether the ref is an abbreviated sha, or
exists on the forge, is swarm-api's rule and is not tested here.
"""

from __future__ import annotations

import copy

import pytest

from swarm_mcp import workflows
from swarm_mcp.client import SwarmError


def _steps(raw):
    return {s["step_id"]: s for s in workflows.build_steps(raw)}


def test_a_step_repository_ref_reaches_the_api_body_as_given():
    steps = _steps(
        [
            {"step_id": "implement", "prompt": "implement it"},
            {
                "step_id": "fix-pr",
                "prompt": "fix the open pull request",
                "repository_ref": "feature/x",
            },
        ]
    )
    assert steps["fix-pr"]["repository_ref"] == "feature/x"
    assert "repository_ref" not in steps["implement"]


def test_the_step_ref_is_sent_through_submit_beside_the_workflows_ref():
    sent = []

    class Client:
        def request(self, method, path, payload=None):
            sent.append(copy.deepcopy(payload))
            return {"workflow": {"workflow_id": "wf_1", "steps": []}}

    spec = workflows.read_spec(
        {
            "steps": [
                {"step_id": "implement", "prompt": "implement it"},
                {"step_id": "fix-pr", "prompt": "fix it", "repository_ref": "feature/x"},
            ],
            "strategy": "direct-pr",
            "repository_url": "https://github.com/example/widgets",
            "repository_ref": "main",
        }
    )
    workflows.submit(Client(), **spec)
    assert len(sent) == 1
    assert sent[0]["repository_ref"] == "main"
    by_id = {s["step_id"]: s for s in sent[0]["steps"]}
    assert by_id["fix-pr"]["repository_ref"] == "feature/x"
    assert "repository_ref" not in by_id["implement"]


def test_a_full_sha_is_sent_unchanged():
    sha = "0123456789abcdef0123456789abcdef01234567"
    steps = _steps([{"step_id": "s", "prompt": "x", "repository_ref": sha}])
    assert steps["s"]["repository_ref"] == sha


@pytest.mark.parametrize("value", ["", 7, ["feature/x"], {"ref": "x"}, True])
def test_a_repository_ref_that_is_not_a_non_empty_string_is_refused_naming_the_step(value):
    raw = [
        {"step_id": "a", "prompt": "x"},
        {"step_id": "b", "prompt": "x", "repository_ref": value},
    ]
    with pytest.raises(SwarmError, match=r"step 'b'.*repository_ref"):
        workflows.build_steps(raw)


def test_a_step_without_it_sends_exactly_the_body_it_did_before():
    raw = [
        {"step_id": "implement", "prompt": "implement it"},
        {
            "step_id": "review",
            "prompt": "review it",
            "depends_on": ["implement"],
            "input_from": {"implement": "patch.diff"},
        },
    ]
    plain = workflows.build_steps([dict(s) for s in raw])
    with_null = workflows.build_steps([dict(s, repository_ref=None) for s in raw])
    assert with_null == plain
    assert all("repository_ref" not in s for s in plain)
