"""A merge-only continuation verifies its task against THAT task's workflow (#900).

An issue run merges from its CI loop: a separate workflow, ONE `merge` step,
`continues_task` naming the run's integrator (or its last CI-fix round's
task). Verifying that task's signed spec against the merge's OWN workflow
made every issue run's auto-merge end `spec_unverified ... workflow_mismatch`
(run_a90b74445a0a495695e7). The merge's signed `merge_target` now names
the task's workflow too, and the worker verifies the task against it.

These run the REAL verifier (`specverify.upstream_verifier`, the one
lifecycle binds) over documents signed with the test keys, so a forged or
edited spec is refused by the signature, not by a fake.

MUTATIONS: verify the opener against the merge's own workflow again -- the
first test reads `workflow_mismatch`. Drop the `continues` binding in
`merge._verify_opener` -- the unbound-branch test merges. Let
`of_workflow` skip the signature -- the tampered tests merge.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_worker import merge, specverify
from swarm_common.states import TaskState

import spec_keys
from merge_world import NUMBER, OPENER, PINNED, TENANT, WORKFLOW, MergeWorld

#: The issue run's workflow, the one its integrator ran in; WORKFLOW
#: (merge_world's) is the merge's own.
RUN_WORKFLOW = "wf_run"


@pytest.fixture
def cfg(worker_factory):
    _, config, _ = worker_factory(task_id="task_merge", runner_profile="merge", sign_spec=False)
    return config


def _opener(workflow_id: str = RUN_WORKFLOW) -> dict[str, Any]:
    """The integrator's document, signed as swarm-api signed it at submission."""
    doc = {
        "tenant_id": TENANT, "workflow_id": workflow_id, "step_id": "fix",
        "runner_profile": "claude-code", "input": {"prompt": "fix the widget sort"},
        "metadata": {"dispatch": {"strategy": "integrate", "role": "integrator"}},
    }
    return spec_keys.sign_document(doc, OPENER)


def _with_result(doc: dict[str, Any]) -> dict[str, Any]:
    # Written after the run, outside the signed fields.
    doc["result_summary"] = {"git": {"pushed_head": PINNED, "pull_request": {"number": NUMBER}}}
    return doc


def _continuation(world: MergeWorld, cfg, *, opener: dict[str, Any],
                  target_workflow: str | None = RUN_WORKFLOW,
                  continues: str | None = OPENER):
    world.docs[OPENER] = _with_result(opener)
    world.verdict = None
    world.target = {"pull_request": OPENER}
    if target_workflow is not None:
        world.target["pull_request_workflow"] = target_workflow
    ctx = world.context()
    dispatch = dict(ctx.dispatch)
    if continues is not None:
        dispatch["continues"] = continues
    return replace(ctx, dispatch=dispatch,
                   verify_upstream=specverify.upstream_verifier(cfg, WORKFLOW))


def _refused(outcome, why: str) -> None:
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "spec_unverified", outcome.summary
    assert outcome.spec_check["reason"] == f"upstream:{OPENER}:{why}"


def test_an_issue_runs_merge_of_its_own_integrators_pull_request_verifies_and_merges(
    tmp_path, cfg
):
    world = MergeWorld(tmp_path)
    ctx = _continuation(world, cfg, opener=_opener())
    assert ctx.workflow_id == WORKFLOW != RUN_WORKFLOW

    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_an_upstream_of_a_workflow_other_than_the_one_the_target_names_is_refused(
    tmp_path, cfg
):
    world = MergeWorld(tmp_path)
    ctx = _continuation(world, cfg, opener=_opener(workflow_id="wf_elsewhere"))

    _refused(merge.run_merge(ctx), "workflow_mismatch")
    assert world.merge_calls() == [] and world.token_reads == 0


def test_a_continuation_that_does_not_continue_the_openers_branch_is_refused(tmp_path, cfg):
    """Authority is the merge's own signed spec, not only the opener's."""
    for continues in (None, "task_someone_else"):
        world = MergeWorld(tmp_path / str(continues))
        ctx = _continuation(world, cfg, opener=_opener(), continues=continues)

        _refused(merge.run_merge(ctx), "workflow_mismatch")
        assert world.merge_calls() == [] and world.token_reads == 0


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: spec_keys.sign_document(d, OPENER, forged=True),
        lambda d: {**d, "input": {"prompt": "merge anything"}},
        lambda d: {**d, "workflow_id": "wf_elsewhere"},
        lambda d: {k: v for k, v in d.items() if k != "spec_signature"},
    ],
    ids=["forged", "edited-input", "edited-workflow", "unsigned"],
)
def test_a_tampered_upstream_spec_is_refused(tmp_path, cfg, tamper):
    world = MergeWorld(tmp_path)
    ctx = _continuation(world, cfg, opener=tamper(_opener()))

    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "spec_unverified"
    assert outcome.spec_check["reason"] in (
        f"upstream:{OPENER}:signature_mismatch", f"upstream:{OPENER}:unsigned",
    )
    assert world.merge_calls() == [] and world.token_reads == 0


def test_a_merge_inside_one_workflow_still_refuses_an_opener_of_another(tmp_path, cfg):
    """The existing protection: no `pull_request_workflow`, the merge's own workflow."""
    world = MergeWorld(tmp_path)
    ctx = _continuation(world, cfg, opener=_opener(), target_workflow=None)

    _refused(merge.run_merge(ctx), "workflow_mismatch")
    assert world.merge_calls() == []


def test_the_bound_verifier_checks_its_own_workflow_unless_merge_names_one(cfg):
    verify = specverify.upstream_verifier(cfg, WORKFLOW)
    with pytest.raises(specverify.UpstreamSpecUnverified) as raised:
        verify(OPENER, _opener())
    assert raised.value.why == "workflow_mismatch"
    assert verify(OPENER, _opener(), of_workflow=RUN_WORKFLOW).reason == "verified"
    with pytest.raises(specverify.UpstreamSpecUnverified) as raised:
        verify(OPENER, _opener(workflow_id=WORKFLOW), of_workflow=RUN_WORKFLOW)
    assert raised.value.why == "workflow_mismatch"


def test_swarm_apis_continued_target_is_one_the_worker_reads():
    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="direct-pr", carrier="patches").with_merge_target(
        pull_request=OPENER, pull_request_workflow=RUN_WORKFLOW,
    ).to_metadata()
    assert merge.parse_merge_target(block) == merge.MergeTarget(
        OPENER, pull_request_workflow=RUN_WORKFLOW,
    )


@pytest.mark.parametrize(
    "target",
    [
        {"pull_request": OPENER, "pull_request_workflow": ""},
        {"pull_request": OPENER, "pull_request_workflow": 7},
        {"pull_request": OPENER, "pull_request_workflow": RUN_WORKFLOW,
         "review": "task_rev", "verdict_file": "verdict.json"},
    ],
    ids=["empty", "not-a-string", "beside-a-review"],
)
def test_a_malformed_continued_target_is_invalid(target):
    with pytest.raises(merge.TargetInvalid):
        merge.parse_merge_target({"merge_target": target})
