"""A pick the branch did not build starts a pick-fix round instead of ending the merge FAILED.

PICK-FIX-ROUND (part of #2441; BGD's answers to PR 2451, 2026-10-26).
ANSWER-CONTENT-CHECK made a merge woken by the owner's answers refuse
`owner_pick_not_built` when the owner picked an option the branch did not
build, leaving the operator to send the lane back by hand. BGD decided:

  1. The merge PARKS instead, on a park code swarm-api's wake tick reads as it
     reads a CI or conflict park (`owner_pick_fix_pending`). The tick submits a
     claude-code round on the pull request's branch whose brief names each
     unbuilt pick; the round builds it, pushes to the same pull request and
     writes `picks-built.json`; the merge runs again. Bounded like the CI and
     conflict rounds: a round that pushes nothing, or ends with a pick it was
     sent for still unbuilt, refuses `owner_pick_not_built` -- never a loop.
  2. EVERY answered question that names `built` is compared, not only those in
     `verdict.needs_owner`: one the owner answered before the review ran, which
     the review then listed nowhere, too.

`merge_world.MergeWorld` is green in every respect; each test adds the asking
step's questions and answers, and the round's records.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_worker import merge
from agent_worker import owner_questions
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, PINNED, REVIEW, TASK, TENANT, WORKFLOW, MergeWorld

IMPLEMENT = "task_impl"
ROUND_TASK = "task_pick1"
ROUND_WORKFLOW = "wf_pick1"
BUILT = "7" * 40

#: Spelled out, not imported: the codes are what the tick and the console read.
PENDING = "owner_pick_fix_pending"
RUNNING = "owner_pick_fix_running"
REFUSED = "owner_pick_not_built"


def _question(**extra: Any) -> dict[str, Any]:
    return {
        "question": "Which tile does a full pool show?",
        "options": [{"label": "Needs a look", "description": "amber"},
                    {"label": "Needs action", "description": "red"}],
        "recommended": "Needs action",
        "context": "the tile reads the pool's ceiling",
        **extra,
    }


def _asked(world: MergeWorld, question: dict[str, Any], answer: dict[str, Any], *,
           needs_owner: bool = True) -> None:
    """The implement step asked `question` and the owner gave `answer`.

    `needs_owner` False: the review listed it nowhere (it was answered before
    the review ran), so only the merge's own walk of its workflow finds it.
    """
    key = f"tenants/{TENANT}/tasks/{IMPLEMENT}/attempts/att_i/artifacts/questions.json"
    data = json.dumps([question]).encode("utf-8")
    world.store.upload_bytes(key, data)
    world.docs[IMPLEMENT] = {
        "tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "implement",
        "state": TaskState.SUCCEEDED.value,
        "result_summary": {"questions": 1, "artifacts": [
            {"name": "questions.json", "uri": world.store.uri(key), "bytes": len(data)}]},
        "metadata": {"owner_answers": [{"question_index": 0, **answer}]},
    }
    # The workflow's graph: merge <- review <- implement.
    world.docs[TASK]["depends_on"] = [REVIEW]
    world.docs[REVIEW].update(step_id="review", state=TaskState.SUCCEEDED.value,
                              depends_on=[IMPLEMENT])
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 0,
         "question": question["question"]}] if needs_owner else []}


def _unbuilt(world: MergeWorld, **kw: Any) -> None:
    _asked(world, _question(built="Needs action"), {"choice": "Needs a look", "text": None}, **kw)


def _round(world: MergeWorld, *, task_id: str | None = ROUND_TASK, **extra: Any) -> None:
    entry: dict[str, Any] = {"round": 1, "head": PINNED, "pull_head": PINNED,
                             "picks": [{"task_id": IMPLEMENT, "question_index": 0}], **extra}
    if task_id is not None:
        entry.update(task_id=task_id, workflow_id=ROUND_WORKFLOW)
    world.docs[TASK].setdefault("metadata", {})[merge.MERGE_FIX_METADATA_KEY] = {
        merge.PICK_ROUNDS_KEY: [entry]}


def _round_task(world: MergeWorld, *, state: str = "SUCCEEDED", pushed: str | None = BUILT,
                report: list[dict[str, Any]] | None = None, continues: str = OPENER) -> None:
    summary: dict[str, Any] = {"git": {"pushed_head": pushed} if pushed else {}}
    if report is not None:
        key = (f"tenants/{TENANT}/tasks/{ROUND_TASK}/attempts/att_p/artifacts/"
               f"{owner_questions.PICKS_BUILT_NAME}")
        data = json.dumps(report).encode("utf-8")
        world.store.upload_bytes(key, data)
        summary["artifacts"] = [{"name": owner_questions.PICKS_BUILT_NAME,
                                 "uri": world.store.uri(key), "bytes": len(data)}]
    world.docs[ROUND_TASK] = {
        "tenant_id": TENANT, "workflow_id": ROUND_WORKFLOW, "state": state,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": continues}},
        "result_summary": summary,
    }


def _built_report(picked: str | None = "Needs a look", builds: bool = True) -> list[dict[str, Any]]:
    return [{"task_id": IMPLEMENT, "question_index": 0, "picked": picked, "builds_pick": builds}]


def _parked(outcome, code: str) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None
    assert outcome.ci_wait is not None and outcome.ci_wait["code"] == code, outcome.ci_wait
    assert outcome.ci_wait["pull_request"] == NUMBER and outcome.ci_wait["head"] == PINNED
    assert "refusal" not in outcome.summary


def _refused(outcome) -> str:
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == REFUSED, outcome.summary
    return outcome.summary["refusal"]["message"]


# --------------------------------------------------------------------------
# 1. an unbuilt pick parks for a round
# --------------------------------------------------------------------------

def test_an_unbuilt_pick_parks_for_a_pick_fix_round_naming_the_pick(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: refuse as ANSWER-CONTENT-CHECK did and the merge ends FAILED, for a
    # person to send the lane back by hand.
    _parked(outcome, PENDING)
    assert world.merge_calls() == []
    # The brief's facts ride the park's `pending` lines, which the tick reads:
    # the question, what was picked, what the branch builds, and where to report.
    (line,) = outcome.ci_wait["pending"]
    assert line.startswith(f"pick {IMPLEMENT} question 0 ")
    assert "'Needs a look'" in line and "'Needs action'" in line
    assert "Which tile does a full pool show?" in line
    assert outcome.summary["unbuilt_picks"][0]["picked"] == "Needs a look"
    # Decided before any credential, as the hold is.
    assert world.token_reads == 0


def test_free_text_that_names_no_option_parks_for_a_round_too(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs action"),
           {"choice": None, "text": "neither; make it a third tile"})

    outcome = merge.run_merge(world.context())

    _parked(outcome, PENDING)
    (line,) = outcome.ci_wait["pending"]
    assert "neither; make it a third tile" in line


def test_a_round_still_running_parks_on_it(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, state="RUNNING", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, RUNNING)
    assert world.merge_calls() == []


def test_a_round_claimed_and_not_yet_submitted_parks_on_it(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world, task_id=None)

    _parked(merge.run_merge(world.context()), RUNNING)


# --------------------------------------------------------------------------
# 2. the round's end: built, or a bounded refusal
# --------------------------------------------------------------------------

def test_a_round_that_built_the_pick_and_pushed_is_merged_at_its_head(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, report=_built_report())
    world.pr["head"]["sha"] = BUILT
    world.serve_checks(BUILT)

    outcome = merge.run_merge(world.context())

    # MUTATION: ignore the round's report and the pick stays unbuilt for ever.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    # MUTATION: leave pick rounds out of the accepted heads and this is head_moved.
    assert call.body["sha"] == BUILT
    assert (ROUND_TASK, ROUND_WORKFLOW) in world.verified


@pytest.mark.parametrize("report", [None, _built_report(builds=False)])
def test_a_round_that_pushed_nothing_and_built_nothing_refuses_and_never_loops(tmp_path, report):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, pushed=None, report=report)

    message = _refused(merge.run_merge(world.context()))

    # MUTATION: park for another round and the merge loops on a round that does nothing.
    assert "pick fix round 1" in message and "changed nothing" in message
    assert world.merge_calls() == []


def test_a_round_that_pushed_the_head_it_started_from_changed_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, pushed=PINNED, report=[])

    assert "changed nothing" in _refused(merge.run_merge(world.context()))


@pytest.mark.parametrize("pushed", [None, PINNED])
def test_a_round_that_found_its_pick_already_built_merges_at_the_head_it_started_from(
        tmp_path, pushed):
    """#2858: PR 2855's pick-fix round found 'apply' already built (a fix step
    had built it after the question recorded 'follow-up') and pushed nothing;
    the merge refused `owner_pick_not_built` and the workflow ended FAILED."""
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, pushed=pushed, report=_built_report())

    outcome = merge.run_merge(world.context())

    # MUTATION: count only a round that pushed and this refuses "changed nothing".
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED
    assert outcome.summary["picks_already_built"] == [
        {"task_id": IMPLEMENT, "question_index": 0, "picked": "Needs a look"}]
    assert outcome.summary["picks_built_by_rounds"] == []


def test_a_round_that_pushed_nothing_counts_only_for_the_picks_it_was_sent_for(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world, picks=[{"task_id": IMPLEMENT, "question_index": 5}])
    _round_task(world, pushed=None, report=_built_report())

    message = _refused(merge.run_merge(world.context()))

    # MUTATION: take a no-push round's word on a pick it was not sent for and
    # this merges a pick nobody was asked to look at.
    assert "changed nothing" in message
    assert world.merge_calls() == []


@pytest.mark.parametrize("report", [
    None,  # wrote no report
    _built_report(builds=False),  # said it could not build it
    _built_report(picked="Needs action"),  # reported another pick than the owner's
])
def test_a_round_that_left_its_pick_unbuilt_refuses_naming_it(tmp_path, report):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, report=report)
    world.pr["head"]["sha"] = BUILT
    world.serve_checks(BUILT)

    message = _refused(merge.run_merge(world.context()))

    # MUTATION: send the same pick to round after round and the merge never ends.
    assert "still" in message and "after pick fix round 1" in message
    assert "step implement question 0" in message
    assert world.merge_calls() == []


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_a_round_that_did_not_succeed_refuses(tmp_path, state):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, state=state, pushed=None)

    message = _refused(merge.run_merge(world.context()))
    assert "pick fix round 1" in message and state in message


def test_a_round_whose_submission_was_refused_refuses(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world, task_id=None, error="the submitter left the tenant")

    message = _refused(merge.run_merge(world.context()))
    assert "was not run" in message and "the submitter left the tenant" in message


def test_a_round_continuing_another_branch_is_no_round_of_this_merge(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world)
    _round_task(world, report=_built_report(), continues="task_other")
    world.pr["head"]["sha"] = BUILT
    world.serve_checks(BUILT)

    outcome = merge.run_merge(world.context())

    # Its report is not this branch's: the pick is still unbuilt.
    assert _refused(outcome)
    assert world.merge_calls() == []


def test_a_new_pick_after_a_round_gets_the_next_round_and_the_budget_bounds_it(tmp_path):
    """A question answered after round 1 is a new pick: round 2. With every
    round spent, a new pick refuses rather than parking again."""
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _round(world, picks=[{"task_id": IMPLEMENT, "question_index": 5}])
    _round_task(world, report=[])
    world.pr["head"]["sha"] = BUILT
    world.serve_checks(BUILT)

    _parked(merge.run_merge(world.context()), PENDING)

    entries = [{"round": n, "head": PINNED, "pull_head": PINNED, "task_id": ROUND_TASK,
                "workflow_id": ROUND_WORKFLOW, "picks": []}
               for n in range(1, merge.MERGE_PICK_ROUNDS_MAX + 1)]
    world.docs[TASK]["metadata"][merge.MERGE_FIX_METADATA_KEY] = {merge.PICK_ROUNDS_KEY: entries}

    message = _refused(merge.run_merge(world.context()))
    assert f"after {merge.MERGE_PICK_ROUNDS_MAX} pick fix round(s)" in message


# --------------------------------------------------------------------------
# 3. every answered question that names `built`
# --------------------------------------------------------------------------

FIX = "task_fix"


def _fix_asked_again(world: MergeWorld, built: str) -> None:
    """A fix step after implement asked the same question again, recording `built`.

    The graph becomes merge <- review <- fix <- implement, so the fix is the
    latest step that recorded what the branch builds.
    """
    question = _question(built=built)
    question["question"] = "  which TILE does a full pool show? "  # spacing and case differ
    key = f"tenants/{TENANT}/tasks/{FIX}/attempts/att_f/artifacts/questions.json"
    data = json.dumps([question]).encode("utf-8")
    world.store.upload_bytes(key, data)
    world.docs[FIX] = {
        "tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "fix",
        "state": TaskState.SUCCEEDED.value, "depends_on": [IMPLEMENT],
        "result_summary": {"questions": 1, "artifacts": [
            {"name": "questions.json", "uri": world.store.uri(key), "bytes": len(data)}]},
        "metadata": {},
    }
    world.docs[REVIEW]["depends_on"] = [FIX]


def test_a_pick_a_later_fix_step_built_merges(tmp_path):
    """#2858, PR 2855: implement recorded 'follow-up', the fix step built
    'apply', the owner picked 'apply'; the merge read implement's stale
    `built` and refused."""
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _fix_asked_again(world, built="Needs a look")

    outcome = merge.run_merge(world.context())

    # MUTATION: read only the asking step's `built` and this parks for a round.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    assert "unbuilt_picks" not in outcome.summary


def test_a_pick_the_later_fix_step_still_does_not_build_starts_a_round(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs a look"), {"choice": "Needs a look", "text": None})
    # The fix step moved the branch to the other option after the question.
    _fix_asked_again(world, built="Needs action")

    outcome = merge.run_merge(world.context())

    # MUTATION: keep the asking step's `built` and this merges the option the
    # branch no longer builds.
    _parked(outcome, PENDING)
    assert outcome.summary["unbuilt_picks"][0]["built"] == "Needs action"

def test_a_question_answered_before_the_review_is_compared_too(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world, needs_owner=False)

    outcome = merge.run_merge(world.context())

    # MUTATION: compare only `verdict.needs_owner` (PR 2451) and this merges the
    # option the owner did not pick.
    _parked(outcome, PENDING)
    assert world.merge_calls() == []


def test_a_question_answered_before_the_review_with_the_built_pick_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs action"), {"choice": "Needs action", "text": None},
           needs_owner=False)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_an_unanswered_question_the_review_let_pass_holds_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs action"), {"choice": "Needs a look", "text": None},
           needs_owner=False)
    world.docs[IMPLEMENT]["metadata"]["owner_answers"] = []

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message


# --------------------------------------------------------------------------
# 3b. a NOT_YET verdict that names needs_owner gets the round too (#2764)
# --------------------------------------------------------------------------

def _not_yet(world: MergeWorld) -> None:
    world.verdict = {**world.verdict, "verdict": "NOT_YET", "findings": ["waits on the owner"]}


def test_a_not_yet_verdict_answered_with_an_unbuilt_pick_parks_for_a_pick_fix_round(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _not_yet(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: refuse a NOT_YET before the pick check (the #2764 bug) and the
    # owner's pick is never built.
    _parked(outcome, PENDING)
    assert world.merge_calls() == []
    (line,) = outcome.ci_wait["pending"]
    assert line.startswith(f"pick {IMPLEMENT} question 0 ")
    assert "'Needs a look'" in line and "'Needs action'" in line


def test_a_not_yet_verdict_whose_pick_round_built_the_pick_refuses_verdict_not_merge(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    _not_yet(world)
    _round(world)
    _round_task(world, report=_built_report())
    world.pr["head"]["sha"] = BUILT
    world.serve_checks(BUILT)

    outcome = merge.run_merge(world.context())

    # MUTATION: merge once the pick is built and a NOT_YET head merges unreviewed.
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "verdict_not_merge", outcome.summary
    assert world.merge_calls() == []


# --------------------------------------------------------------------------
# 4. a pick park whose pull request ended outside the workflow
# --------------------------------------------------------------------------

def test_a_pick_park_whose_pull_request_was_merged_outside_finishes(tmp_path):
    world = MergeWorld(tmp_path)
    _unbuilt(world)
    world.docs[TASK]["metadata"][merge.MERGE_WAIT_METADATA_KEY] = {
        "code": PENDING, "head": PINNED, "pull_request": NUMBER,
        "wake_requested_at": "2026-10-26T13:00:00Z", "wake_reason": "pull_request_merged"}
    world.pr.update(state="closed", merged=True, merged_by={"login": "bgd"},
                    merged_at="2026-10-26T12:59:00Z", merge_commit_sha="d" * 40)

    outcome = merge.run_merge(world.context())

    # MUTATION: read only the owner hold's mark and this parks for a round on a
    # pull request that is already merged.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["merged_outside_workflow"] is True
    assert world.merge_calls() == []


# --------------------------------------------------------------------------
# 5. the tick and the worker read one record
# --------------------------------------------------------------------------

def test_the_tick_and_the_worker_agree_on_the_pick_rounds():
    from swarm_api import mergewake

    assert merge.OWNER_PICK_FIX_PENDING == mergewake.OWNER_PICK_FIX_PENDING == PENDING
    assert merge.OWNER_PICK_FIX_RUNNING == mergewake.OWNER_PICK_FIX_RUNNING == RUNNING
    assert merge.OWNER_PICK_NOT_BUILT == mergewake.OWNER_PICK_NOT_BUILT
    assert merge.PICK_ROUNDS_KEY == mergewake.PICK_ROUNDS_KEY
    assert merge.MERGE_PICK_ROUNDS_MAX == mergewake.MERGE_PICK_ROUNDS_MAX
    assert merge.PICK_LINE_PREFIX == mergewake.PICK_LINE_PREFIX
    assert owner_questions.PICKS_BUILT_NAME == mergewake.PICKS_BUILT_NAME
