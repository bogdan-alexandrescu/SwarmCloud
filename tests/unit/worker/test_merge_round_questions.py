"""The merge reads the owner questions of its own fix rounds, not only of the steps it depends on.

#2898. swarm-api's wake tick appends each CI-fix, conflict fix and pick-fix
round to the merge's workflow as a step (`ci-fix:<n>`, `conflict-fix:<n>`,
`pick-fix:<n>`) after the merge step, so the merge depends on none of them,
and its `depends_on` walk (`owner_questions.workflow_questions`) never read
their `questions.json`. Workflow wf_56bd7b2d203242bbba0a's ci-fix:1 added an
exemption to the installer's secret-read guard and asked whether to "Allow
the one read" (what it built) or "Forbid it; rework upgrade"; the owner
answered "Forbid it", and the merge step merged pull request 2878 with the
exemption in it.

Now a round's answered pick the branch does not build starts a pick-fix round
exactly as an implement step's does, a round's unanswered blocking question
holds the merge as a `needs_owner` one does, and a round that asked nothing,
or whose pick the branch builds, changes nothing.

`merge_world.MergeWorld` is green in every respect; each test adds the
round's records on the merge's own `merge_fix`, and the round's task.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_worker import merge
from agent_worker import owner_questions
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, PINNED, TASK, TENANT, MergeWorld

CIFIX = "task_cifix1"
CIFIX_WORKFLOW = "wf_cifix1"
#: The head the CI-fix round pushed: the pull request's head once it ended.
CIFIX_HEAD = "a" * 40
PICK = "task_pick1"
PICK_WORKFLOW = "wf_pick1"
PICK_HEAD = "7" * 40
ALLOW = "Allow the one read"
FORBID = "Forbid it; rework upgrade"

#: Spelled out, not imported: the codes are what the tick and the console read.
PENDING = "owner_pick_fix_pending"
HOLD = "owner_questions_open"


def _question(**extra: Any) -> dict[str, Any]:
    return {
        "question": "May the upgrade path read the App key once?",
        "options": [{"label": ALLOW, "description": "an exemption in the guard"},
                    {"label": FORBID, "description": "the guard stays absolute"}],
        "recommended": FORBID,
        "context": "the installer's secret-read guard",
        **extra,
    }


def _upload(world: MergeWorld, task_id: str, name: str, payload: Any) -> dict[str, Any]:
    key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_{task_id}/artifacts/{name}"
    data = json.dumps(payload).encode("utf-8")
    world.store.upload_bytes(key, data)
    return {"name": name, "uri": world.store.uri(key), "bytes": len(data)}


def _ci_round(world: MergeWorld, *, key: str = "rounds",
              questions: list[dict[str, Any]] | None = None,
              answers: list[dict[str, Any]] | None = None,
              continues: str = OPENER) -> None:
    """The merge's CI-fix round 1 (or a conflict round, by `key`) ran, pushed
    CIFIX_HEAD, and asked `questions`; the owner gave `answers`."""
    world.docs[TASK].setdefault("metadata", {}).setdefault(
        merge.MERGE_FIX_METADATA_KEY, {})[key] = [
            {"round": 1, "head": PINNED, "claimed_at": "2026-10-31T03:00:00+00:00",
             "task_id": CIFIX, "workflow_id": CIFIX_WORKFLOW}]
    summary: dict[str, Any] = {"git": {"pushed_head": CIFIX_HEAD}, "artifacts": []}
    if questions:
        summary["questions"] = len(questions)
        summary["artifacts"].append(_upload(world, CIFIX, "questions.json", questions))
    world.docs[CIFIX] = {
        "tenant_id": TENANT, "workflow_id": CIFIX_WORKFLOW, "state": TaskState.SUCCEEDED.value,
        "step_id": "fix",
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": continues},
                     "owner_answers": list(answers or [])},
        "result_summary": summary,
    }
    world.pr["head"]["sha"] = CIFIX_HEAD
    world.serve_checks(CIFIX_HEAD)


def _forbid() -> list[dict[str, Any]]:
    return [{"question_index": 0, "choice": FORBID, "text": None}]


# --------------------------------------------------------------------------
# 1. the pull request 2878 sequence
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key, step", [("rounds", "ci-fix:1"),
                                       (merge.CONFLICT_ROUNDS_KEY, "conflict-fix:1")])
def test_a_rounds_answer_the_branch_does_not_build_starts_a_pick_fix_round(tmp_path, key, step):
    """ci-fix:1 built 'Allow', the owner recorded 'Forbid': no merge, a pick-fix round."""
    world = MergeWorld(tmp_path)
    _ci_round(world, key=key, questions=[_question(built=ALLOW)], answers=_forbid())

    outcome = merge.run_merge(world.context())

    # MUTATION: read only the steps the merge depends on and this merges PR 2878.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.ci_wait is not None and outcome.ci_wait["code"] == PENDING, outcome.ci_wait
    assert outcome.ci_wait["pull_request"] == NUMBER
    assert world.merge_calls() == []
    (line,) = outcome.ci_wait["pending"]
    assert line.startswith(f"pick {CIFIX} question 0 (step {step}): ")
    assert repr(FORBID) in line and repr(ALLOW) in line
    (row,) = outcome.summary["unbuilt_picks"]
    assert (row["task_id"], row["step_id"], row["picked"], row["built"]) == (
        CIFIX, step, FORBID, ALLOW)
    # Decided before any credential, as the hold is.
    assert world.token_reads == 0
    # The round was held to its signed spec, against its own workflow.
    assert (CIFIX, CIFIX_WORKFLOW) in world.verified


def test_the_pick_fix_round_that_builds_the_rounds_pick_lets_the_merge_go_ahead(tmp_path):
    world = MergeWorld(tmp_path)
    _ci_round(world, questions=[_question(built=ALLOW)], answers=_forbid())
    world.docs[TASK]["metadata"][merge.MERGE_FIX_METADATA_KEY][merge.PICK_ROUNDS_KEY] = [
        {"round": 1, "head": CIFIX_HEAD, "pull_head": CIFIX_HEAD,
         "claimed_at": "2026-10-31T04:00:00+00:00", "task_id": PICK,
         "workflow_id": PICK_WORKFLOW, "picks": [{"task_id": CIFIX, "question_index": 0}]}]
    report = _upload(world, PICK, owner_questions.PICKS_BUILT_NAME, [
        {"task_id": CIFIX, "question_index": 0, "picked": FORBID, "builds_pick": True}])
    world.docs[PICK] = {
        "tenant_id": TENANT, "workflow_id": PICK_WORKFLOW, "state": TaskState.SUCCEEDED.value,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": OPENER}},
        "result_summary": {"git": {"pushed_head": PICK_HEAD}, "artifacts": [report]},
    }
    world.pr["head"]["sha"] = PICK_HEAD
    world.serve_checks(PICK_HEAD)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PICK_HEAD
    assert outcome.summary["picks_built_by_rounds"] == [
        {"task_id": CIFIX, "question_index": 0, "picked": FORBID}]


# --------------------------------------------------------------------------
# 2. what does not change
# --------------------------------------------------------------------------

def test_a_ci_fix_round_with_no_questions_changes_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    _ci_round(world)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == CIFIX_HEAD
    assert "unbuilt_picks" not in outcome.summary


@pytest.mark.parametrize("built", [FORBID, None])
def test_an_answered_pick_the_branch_builds_lets_the_merge_go_ahead(tmp_path, built):
    """The round built the owner's option, or said the answer changes nothing it builds."""
    world = MergeWorld(tmp_path)
    _ci_round(world, questions=[_question(built=built)], answers=_forbid())

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == CIFIX_HEAD


def test_a_round_entry_that_is_not_this_branchs_round_adds_no_question(tmp_path):
    """`merge_fix` is tenant-writable: a task that does not continue the
    branch's root is no round of this merge, and its questions bind nothing."""
    world = MergeWorld(tmp_path)
    _ci_round(world, questions=[_question(built=ALLOW)], answers=_forbid(),
              continues="task_elsewhere")
    world.pr["head"]["sha"] = PINNED
    world.serve_checks(PINNED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED


# --------------------------------------------------------------------------
# 3. an unanswered blocking question holds the merge
# --------------------------------------------------------------------------

def test_a_rounds_unanswered_blocking_question_parks_the_merge_for_the_owner(tmp_path):
    world = MergeWorld(tmp_path)
    _ci_round(world, questions=[_question(built=ALLOW, blocking=True)])

    outcome = merge.run_merge(world.context())

    # MUTATION: read the rounds' picks but not their open questions and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.owner_wait is not None and outcome.owner_wait["code"] == HOLD
    assert outcome.owner_wait["questions"] == [
        {"task_id": CIFIX, "step_id": "ci-fix:1", "question_index": 0,
         "question": _question()["question"]}]
    assert "step ci-fix:1 question 0" in outcome.message
    assert world.merge_calls() == []
    assert world.token_reads == 0


def test_a_rounds_unanswered_question_that_is_not_blocking_does_not_hold(tmp_path):
    world = MergeWorld(tmp_path)
    _ci_round(world, questions=[_question(built=ALLOW)])

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.owner_wait is None
