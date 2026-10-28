"""A red head whose fix is a workflow edit nobody here can push parks for the operator (#2046).

When the publish that wrote the pull request's head -- the opener's, or the
CI-fix round's that ended at it -- touched `.github/workflows/` and swarm-api
refused the push `workflow_scope_missing` (the repository's registration
does not have `workflow_files` switched on), the worker left the workflow
lines as `workflow-lines.patch` and recorded `git.workflow_refused` and
`git.workflow_patch` on its result. CI is then red by construction: the tests
assert the workflow half the branch does not have. A CI-fix round can only
make the same edit and be refused again -- task_58815d23 and task_d91ca0c9
were, and the merge step then ended `merge_refused` at the unchanged head.

So a red reading at such a head parks WORKFLOW_PATCH_PENDING, naming the
patch and the refused paths, and never CI_FIX_PENDING, which is what asks the
wake tick for a round. The controls: no refusal, a refusal for another
reason, or a refusal at a head the pull request has moved past, park
CI_FIX_PENDING or refuse exactly as before.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from typing import Any

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import OPENER, PINNED, TASK, TENANT, MergeWorld

FIX_TASK = "task_fix1"
FIXED = "f" * 40
PATHS = [".github/workflows/auto-merge.yml", ".github/workflows/merge-train.yml"]
PATCH_URI = "gs://bucket/tenants/eng/tasks/task_int/attempts/att_1/artifacts/workflow-lines.patch"
#: The park code, as the wake tick reads it (`swarm_api.mergewake`).
PATCH_PENDING = "workflow_patch_pending"


def _refused(code: str = "workflow_scope_missing", *, patch: bool = True) -> dict[str, Any]:
    """The git block of a publish the broker refused (lifecycle `_workflow_push_refused`)."""
    git: dict[str, Any] = {
        "published": False,
        "workflow_paths": list(PATHS),
        "workflow_refused": {"code": code, "message": (
            "example-org/widgets's registration does not have workflow_files switched on")},
    }
    if patch:
        git["workflow_patch"] = {"name": "workflow-lines.patch", "uri": PATCH_URI, "bytes": 812}
    return git


def _red(world: MergeWorld) -> None:
    world.runs[0].update(conclusion="failure")


def _rounds(world: MergeWorld, asked: int, *entries: dict[str, Any]) -> None:
    world.docs[TASK]["metadata"] = {
        "merge_fix_rounds": asked,
        **({merge.MERGE_FIX_METADATA_KEY: {"rounds": list(entries)}} if entries else {}),
    }


def _opener_refused(world: MergeWorld, git: dict[str, Any]) -> None:
    world.docs[OPENER]["result_summary"]["git"].update(git)


def _fix_task(world: MergeWorld, git: dict[str, Any], *, state: str = "FAILED") -> None:
    world.docs[FIX_TASK] = {
        "tenant_id": TENANT, "workflow_id": "wf_fix", "state": state,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": OPENER}},
        "result_summary": {"git": git},
    }


def _parked(outcome, code: str, head: str = PINNED) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.ci_wait["code"] == code, outcome.ci_wait
    assert outcome.ci_wait["head"] == head
    assert "refusal" not in outcome.summary


def _names_the_patch(outcome) -> None:
    message = outcome.summary["wait"]["message"]
    assert "workflow-lines.patch" in message, message
    assert PATCH_URI in message, message
    for path in PATHS:
        assert path in message, message
    assert "workflow_scope_missing" in message, message


# ---------------------------------------------------------------------------
# 1. the opener's publish was refused: no round is asked for
# ---------------------------------------------------------------------------

def test_red_at_the_openers_refused_head_parks_for_the_operator_not_for_a_round(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2)
    _opener_refused(world, _refused())

    outcome = merge.run_merge(world.context())

    # The defect: CI_FIX_PENDING, which the wake tick answers with a round
    # whose only fix is the same refused workflow edit.
    _parked(outcome, PATCH_PENDING)
    _names_the_patch(outcome)
    assert world.merge_calls() == []


def test_red_at_the_openers_refused_head_parks_even_with_no_rounds_asked(tmp_path):
    """No rounds asked: today `checks_failed` at once. The fix is the
    operator's push, so the step waits for it rather than ending."""
    world = MergeWorld(tmp_path)
    _red(world)
    _opener_refused(world, _refused())

    outcome = merge.run_merge(world.context())

    _parked(outcome, PATCH_PENDING)


# ---------------------------------------------------------------------------
# 2. the last CI-fix round's publish was refused at this head
# ---------------------------------------------------------------------------

def test_red_after_a_round_refused_its_workflow_push_parks_for_the_operator(tmp_path):
    """task_58815d23's shape: round 1 ran at the pinned head, its publish was
    refused, it ended FAILED, the head did not move. Today: `checks_failed
    ... after CI fix round 1 ended at this head`."""
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2, {"round": 1, "head": PINNED, "task_id": FIX_TASK})
    _fix_task(world, _refused())

    outcome = merge.run_merge(world.context())

    _parked(outcome, PATCH_PENDING)
    _names_the_patch(outcome)
    assert FIX_TASK in outcome.summary["wait"]["message"]


def test_a_round_still_running_is_waited_on_as_before(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2, {"round": 1, "head": PINNED, "task_id": FIX_TASK})
    _fix_task(world, {}, state="RUNNING")
    _opener_refused(world, _refused())

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_RUNNING)


# ---------------------------------------------------------------------------
# 3. the controls: a round is still asked for, or the refusal stands
# ---------------------------------------------------------------------------

def test_red_with_no_refused_publish_still_asks_for_a_round(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING)


def test_a_refusal_for_another_reason_still_asks_for_a_round(tmp_path):
    """`fenced` or `api_unavailable` is not a missing switch: a round may push."""
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2)
    _opener_refused(world, _refused("api_unavailable"))

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING)


def test_a_refusal_with_no_recorded_patch_still_asks_for_a_round(tmp_path):
    """Nothing for the operator to push: parking would wait on nothing."""
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2)
    _opener_refused(world, _refused(patch=False))

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING)


def test_a_round_that_ended_at_this_head_without_a_refusal_is_checks_failed(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2, {"round": 1, "head": PINNED, "task_id": FIX_TASK})
    _fix_task(world, {})

    outcome = merge.run_merge(world.context())

    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_another_tenants_round_document_is_not_read_as_a_refusal(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rounds(world, 2, {"round": 1, "head": PINNED, "task_id": FIX_TASK})
    _fix_task(world, _refused())
    world.docs[FIX_TASK]["tenant_id"] = "research"

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_the_openers_refusal_is_not_read_at_a_head_past_it(tmp_path):
    """A round fixed what it could and pushed FIXED; red there is a round's
    again, not the opener's refused publish."""
    world = MergeWorld(tmp_path)
    _rounds(world, 2, {"round": 1, "head": PINNED, "task_id": FIX_TASK})
    _fix_task(world, {"pushed_head": FIXED}, state="SUCCEEDED")
    _opener_refused(world, _refused())
    world.pr["head"]["sha"] = FIXED
    world.serve_checks(FIXED)
    _red(world)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING, FIXED)


def test_green_at_the_openers_refused_head_merges_as_before(tmp_path):
    world = MergeWorld(tmp_path)
    _rounds(world, 2)
    _opener_refused(world, _refused())

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
