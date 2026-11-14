"""A single follow-up task's head is a person's lane, as a workflow one's is.

The owner, 2026-11-13: "workflows now, single tasks next". That night two
merge steps refused heads that single tasks pushed through `swarm_dispatch`
with `publish_to_pr` -- `head_moved ... its last line says worker task
task_... made it, which is not a person's plain lane on this branch`.
`_person_lane` held every lane to "its own signed workflow", and a single
task has none: its signed `workflow_id` is null, so the upstream read never
verified it. A single task whose SIGNED spec verifies, whose signed
`workflow_id` is null and whose signed `publish_to_pr.task_id` is this pull
request's branch task is now a person's lane: its commit, published as a
person on the pull request, ends `superseded_by_person_push`, as a workflow
follow-up's does (test_merge_person_push.py). Nothing merges either way.

The controls: a single task published onto ANOTHER pull request, a single
task with no `publish_to_pr`, and a single task whose spec is unsigned or
forged still end `head_moved`.

The single task is verified by the worker's REAL verifier
(`specverify.upstream_verifier`) with the test keys: the world's fake
verifier admits a null workflow, the real one does not, and the defect
lived in that difference.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_worker import merge, specverify
from swarm_common.models import EndCause

import merge_world
import spec_keys
from merge_world import MergeWorld
from test_merge_person_push import LANE_HEAD, PAT_LOGIN, _lane_commit, _world

#: The single task the incident's last line named (a `swarm_dispatch` task, no workflow).
SINGLE = "task_5e1f0a2b3c4d5e6f7a8b"


@pytest.fixture
def cfg(worker_factory):
    _, config, _ = worker_factory(task_id=merge_world.TASK, runner_profile="merge",
                                  sign_spec=False)
    return config


def _single(world: MergeWorld, *, root: str | None = merge_world.OPENER,
            sign: str = "signed", **metadata: Any) -> str:
    """Seed a single task as `POST /v1/tasks` stores it: no workflow, its
    `publish_to_pr` (when `root` is given) and the follow-up marker in its
    metadata, signed as swarm-api signs it."""
    dispatch: dict[str, Any] = {"strategy": "direct-pr", "carrier": "checkpoints"}
    if root is not None:
        dispatch["publish_to_pr"] = {"task_id": root, "number": merge_world.NUMBER}
    doc = {"tenant_id": merge_world.TENANT, "workflow_id": None, "step_id": None,
           "runner_profile": "claude-code", "state": "SUCCEEDED",
           "repository_url": merge_world.REPO_URL,
           "input": {"prompt": "apply the review"},
           "metadata": {"dispatch": dispatch, **metadata}}
    if root is not None:
        doc["metadata"]["pr_fix_head"] = {"kind": "follow_up",
                                          "pull_request": merge_world.NUMBER,
                                          "state": "recorded"}
    if sign != "unsigned":
        spec_keys.sign_document(doc, SINGLE, forged=sign == "forged")
    world.docs[SINGLE] = doc
    return SINGLE


def _run(world: MergeWorld, cfg) -> Any:
    """`run_merge` with the real verifier for the single task, the world's for the rest."""
    ctx = world.context()
    fake = ctx.verify_upstream
    real = specverify.upstream_verifier(cfg, merge_world.WORKFLOW)

    def verify(task_id: str, doc: Any, *, of_workflow: str | None = None) -> Any:
        if task_id == SINGLE:
            return real(task_id, doc, of_workflow=of_workflow)
        return fake(task_id, doc, of_workflow=of_workflow)

    return merge.run_merge(replace(ctx, verify_upstream=verify))


def test_a_single_follow_ups_head_is_a_persons_lane_not_head_moved(tmp_path, cfg):
    """The 2026-11-13 refusal, reproduced: a single `publish_to_pr` task's
    commit, published as the pull request's author, ending with the worker's
    line naming the task. It ended `head_moved`, 'not a person's plain lane
    on this branch'; it is the person's lane now, as a workflow follow-up's is."""
    world = _world(tmp_path)
    _single(world)
    world.pr["head"]["sha"] = _lane_commit(world, SINGLE)
    outcome = _run(world, cfg)

    refusal = outcome.summary["refusal"]
    assert refusal["code"] == merge.SUPERSEDED_BY_PERSON_PUSH, refusal
    assert "not a person's plain lane" not in refusal["message"]
    assert refusal["person"] == PAT_LOGIN
    assert refusal["commit"]["sha"] == LANE_HEAD
    assert refusal["commit"]["worker_task"] == SINGLE
    assert world.merge_calls() == []
    assert outcome.end_cause is EndCause.MERGE_REFUSED


@pytest.mark.parametrize("seed", [
    pytest.param(dict(root="task_other"), id="published-onto-another-pull-request"),
    pytest.param(dict(root=None), id="not-published-onto-a-pull-request"),
    pytest.param(dict(sign="unsigned"), id="unsigned"),
    pytest.param(dict(sign="forged"), id="forged"),
    pytest.param(dict(issue_run={"run_id": "crun_1"}), id="a-writers-mark"),
])
def test_a_single_task_not_published_onto_this_pull_request_still_ends_head_moved(
        tmp_path, cfg, seed):
    world = _world(tmp_path)
    _single(world, **seed)
    world.pr["head"]["sha"] = _lane_commit(world, SINGLE)
    outcome = _run(world, cfg)

    refusal = outcome.summary["refusal"]
    assert refusal["code"] == "head_moved", refusal
    assert f"worker task {SINGLE} made it, which is not a person's plain lane" in (
        refusal["message"])
    assert world.merge_calls() == []
