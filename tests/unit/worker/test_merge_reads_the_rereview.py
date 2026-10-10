"""The merge `on_merge_verdict` derives reads the LATEST review's verdict (WF-MERGE-API).

implement -> review -> fix, with `metadata.merge` "on_merge_verdict": swarm-api
appends a re-review of the fix's pushed head and a merge step whose signed
target names the re-review as its review (`validation.rereview_step_for`,
`merge_sources(..., rereview=)`). This runs the worker's real merge action on
that derived target:

  * the re-review says MERGE, CI is green -> the pull request is merged at
    the head the integrator pushed, whatever the first review said;
  * the re-review says NOT_YET -> the merge refuses `verdict_not_merge` and
    makes no merge call: the one extra round is spent, and the workflow stops
    with the pull request open for a person.

The submission side (which steps exist, what they depend on) is
tests/unit/control_plane/test_merge_verdict_workflows.py. No credentials, no
network: GitHub is `merge_world`'s fake.
"""

from __future__ import annotations

import pytest

from agent_worker import merge
from swarm_api.validation import (
    StepSpec,
    merge_sources,
    merge_step_for,
    rereview_step_for,
)
from swarm_common.states import TaskState

from merge_world import OPENER, PINNED, REVIEW, VERDICT_FILE, MergeWorld

REVIEW_STEP = {
    "step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
    "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
    "input": {"prompt": "review; write verdict.json"},
}


def _specs() -> list[StepSpec]:
    return [
        StepSpec("implement", ()),
        StepSpec("review", ("implement",), input_from={"implement": "swarm-work.patch"},
                 builds_on="implement"),
        StepSpec("fix", ("review",), input_from={"review": VERDICT_FILE},
                 when_step="review", when_verdicts=("NOT_YET",), builds_on="implement"),
    ]


def _derived_target(world: MergeWorld) -> str:
    """The merge target swarm-api derives, with its step ids mapped onto the
    world's task ids. Returns the re-review's step id."""
    specs = _specs()
    rereview = rereview_step_for(specs, REVIEW_STEP, "integrate")
    assert rereview is not None
    specs.append(StepSpec(rereview["step_id"], tuple(rereview["depends_on"]),
                          input_from=rereview["input_from"], builds_on=rereview["builds_on"]))
    sources = merge_sources(specs, "integrate", rereview=rereview["step_id"])
    assert sources is not None
    step = merge_step_for(specs, "integrate", rereview=rereview["step_id"])
    # The merge stages exactly the re-review's verdict file, and nothing of
    # the first review's: that is what makes it read the latest verdict.
    assert step["input_from"] == {rereview["step_id"]: VERDICT_FILE}
    assert sources.pull_request == "fix" and sources.review == rereview["step_id"]
    task_of = {"fix": OPENER, rereview["step_id"]: REVIEW}
    world.target = {"pull_request": task_of[sources.pull_request],
                    "review": task_of[sources.review],
                    "verdict_file": sources.verdict_file}
    return rereview["step_id"]


def test_a_rereview_merge_verdict_merges_at_the_integrators_head(tmp_path):
    world = MergeWorld(tmp_path)
    _derived_target(world)
    world.verdict = {"verdict": "MERGE", "findings": []}
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED


def test_a_rereview_not_yet_stops_at_the_merge_with_no_merge_call(tmp_path):
    world = MergeWorld(tmp_path)
    _derived_target(world)
    world.verdict = {"verdict": "NOT_YET", "findings": ["still broken"]}
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert world.merge_calls() == []


@pytest.mark.parametrize("strategy", ["direct-pr", "collect"])
def test_no_rereview_is_derived_outside_integrate(strategy):
    assert rereview_step_for(_specs(), REVIEW_STEP, strategy) is None
