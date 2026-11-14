"""Contract request 135: swarm-api picks `builder` from the stage, never from a caller (issue #3100).

  * `_step_resource_class`: with no class from the step, the stage the
    compiler wrote chooses (`resource_class_for_stage`); a caller's own
    `resource_class` still only shrinks, so `builder` from a caller is
    refused as larger (invariant 10);
  * `issueruns.build_step_stages` names the compiled plan's implementers
    "implement" and its fix "fix", in both the chain and the staged shape,
    and no other step;
  * a workflow a caller posts, whatever its step ids, stores the profile's
    class: only the issue-run compilers pass `step_stages`;
  * a merge or fix-loop round that continues a `builder` step runs the
    profile's class, where copying `builder` would be refused as larger.

The approved run's stored classes are held in test_issue_runs.py
(`test_approving_the_digest_shown_submits_one_new_workflow`) and the CI fix
round's in test_issue_run_ci.py. No cloud, no emulator.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api import issueruns, mergewake
from swarm_api.errors import NotFound, ValidationFailed
from swarm_api.issueruns import compile_plan, plan_digest
from swarm_api.service import _step_resource_class

from .conftest import auth_header
from .test_issue_runs import PLAN, _stored_run

CLAUDE = RUNNER_PROFILES["claude-code"]


@pytest.mark.parametrize("stage", ["implement", "build", "fix"])
def test_no_class_from_the_step_and_a_build_stage_is_builder(stage):
    assert _step_resource_class(CLAUDE, None, stage, sizes=None) == "builder"


@pytest.mark.parametrize("stage", [None, "review", "requirements-check"])
def test_no_class_from_the_step_and_any_other_stage_is_the_profiles_class(stage):
    assert _step_resource_class(CLAUDE, None, stage, sizes=None) == "standard"


@pytest.mark.parametrize("stage", [None, "implement", "fix"])
def test_a_caller_naming_builder_is_refused_as_larger_at_every_stage(stage):
    # MUTATION: let the stage widen what a caller may name and `builder` on
    # claude-code is accepted from the request body.
    with pytest.raises(ValidationFailed):
        _step_resource_class(CLAUDE, "builder", stage, sizes=None)


def test_a_callers_own_smaller_or_equal_class_still_wins_over_the_stage():
    assert _step_resource_class(CLAUDE, "standard", "implement", sizes=None) == "standard"


def _staged(plan: dict) -> dict:
    staged = json.loads(json.dumps(plan))
    staged["steps"][0]["depends_on"] = []
    staged["steps"][1]["depends_on"] = [staged["steps"][0]["step_id"]]
    return staged


@pytest.mark.parametrize("shape", [lambda p: p, _staged], ids=["chain", "stages"])
def test_the_compiled_plans_implementers_and_fix_are_its_build_stages(shape):
    plan = shape(json.loads(json.dumps(PLAN)))
    spec = compile_plan(_stored_run(plan=plan, plan_digest=plan_digest(plan)))
    stages = issueruns.build_step_stages(spec)
    assert stages == {"impl-sort-key": "implement", "impl-ui": "implement", "fix": "fix"}
    # The review and every later step have none, so they keep their class.
    assert {s.step_id for s in spec.steps} - set(stages) >= {"review"}


def test_a_callers_workflow_stores_the_profiles_class_whatever_its_step_ids(client, db):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json={
        "strategy": "integrate",
        "repository_url": "https://github.com/example-org/example.git",
        "steps": [
            {"step_id": "impl-x", "runner_profile": "claude-code", "input": {"prompt": "build"}},
            {"step_id": "fix", "runner_profile": "claude-code", "depends_on": ["impl-x"],
             "builds_on": "impl-x", "input": {"prompt": "fix"}},
        ],
    })
    assert response.status_code in (200, 201, 202), response.text
    task_ids = {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}
    assert {step: db.docs[f"tasks/{task_id}"]["resource_class"]
            for step, task_id in task_ids.items()} == {"impl-x": "standard", "fix": "standard"}


def _round_ctx(task: Any) -> Any:
    def get_task(tenant_id: str, task_id: str, *, submitted_by: Any) -> Any:
        if task is None:
            raise NotFound("no such task")
        return task
    return SimpleNamespace(store=SimpleNamespace(get_task=get_task))


def test_a_round_continuing_a_builder_step_runs_the_profiles_class():
    task = SimpleNamespace(runner_profile="claude-code", resource_class="builder")
    # MUTATION: return the task's class unchanged and the round's workflow is
    # refused (`builder` is larger than claude-code's own class).
    assert mergewake.round_resource_class(_round_ctx(task), "eng", "task_1") is None


def test_a_round_continuing_a_smaller_class_still_keeps_it():
    task = SimpleNamespace(runner_profile="claude-code", resource_class="standard")
    assert mergewake.round_resource_class(_round_ctx(task), "eng", "task_1") == "standard"
