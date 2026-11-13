"""A merge's owner-question park names each questions.json the platform rejected.

ISSUE #3086. An integrate workflow's build step wrote a questions.json the
worker's validator rejected: `questions: 0` with `questions_rejected`. The
review was staged no questions, could not cite a `{task_id, question_index}`
reference, and wrote a free-text `needs_owner` entry; the merge parked with
`owner_questions: {code: owner_questions_open, unreadable: 1, questions: []}`
and nobody could tell which file was behind it. Now:

  * the park records `rejected_files`, one `{task_id, step_id, file, reason}`
    for each SUCCEEDED earlier step whose questions.json was not taken, and
    its message names each one's step, task and reason;
  * the review is told which earlier step's file was not taken and why;
  * a build step's registered question reaches the review through the
    integrator's fix, holds the merge, and its answer lets the merge go on;
  * a workflow with no rejected file parks exactly as before.

`merge_world.MergeWorld` is green in every respect. No credentials, no
network, no emulator.
"""

from __future__ import annotations

import json
from typing import Any

from agent_worker import merge, owner_questions, questions, verdict
from agent_worker.errors import ExitCode, InputUnavailable
from swarm_common.states import TaskState

from merge_world import OPENER, REVIEW, TASK, TENANT, WORKFLOW, MergeWorld

BUILD = "task_build"
REASON = "question 0 option 1: 'label' is missing"
SENTENCE = "The owner must answer the build step's question about the export format."
QUESTION = {
    "question": "Does the export keep the legacy column order?",
    "options": [{"label": "keep", "description": "old readers keep working"},
                {"label": "reorder", "description": "the columns follow the new schema"}],
    "recommended": "keep",
    "context": "two readers still parse by position",
    "blocking": True,
    "built": "keep",
    # A key of the agent's own: dropped by the validator, not a rejection.
    "rationale": "the agent's own note",
}


def _integrate(world: MergeWorld, build_summary: dict[str, Any]) -> None:
    """build -> integrator fix (the opener) -> review -> merge, one workflow."""
    world.docs[BUILD] = {"tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "build",
                         "state": TaskState.SUCCEEDED.value,
                         "result_summary": build_summary, "metadata": {"owner_answers": []}}
    world.docs[OPENER].update(step_id="integrate", state=TaskState.SUCCEEDED.value,
                              depends_on=[BUILD])
    world.docs[REVIEW].update(step_id="review", state=TaskState.SUCCEEDED.value,
                              depends_on=[OPENER],
                              metadata={"expected_outputs": ["verdict.json"]})
    world.docs[TASK].update(step_id="merge", depends_on=[REVIEW, OPENER])


def _rejected_build(world: MergeWorld) -> None:
    _integrate(world, {questions.SUMMARY_KEY: 0, questions.REJECTED_KEY: REASON})


def _asking_build(world: MergeWorld) -> None:
    key = f"tenants/{TENANT}/tasks/{BUILD}/attempts/att_b/artifacts/{questions.NAME}"
    data = json.dumps([QUESTION]).encode("utf-8")
    world.store.upload_bytes(key, data)
    _integrate(world, {questions.SUMMARY_KEY: 1, "artifacts": [
        {"name": questions.NAME, "uri": world.store.uri(key), "bytes": len(data)}]})


def test_a_park_on_an_unreadable_entry_names_the_build_steps_rejected_file(tmp_path):
    world = MergeWorld(tmp_path)
    _rejected_build(world)
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [SENTENCE]}

    outcome = merge.run_merge(world.context())

    # Invariant 4: still a park that releases and exits 75.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert world.merge_calls() == []
    wait = outcome.owner_wait
    assert wait["code"] == merge.OWNER_QUESTIONS_OPEN
    assert wait["unreadable"] == 1 and wait["questions"] == []
    # MUTATION: park with the bare count (the #3086 record) and this fails.
    assert wait["rejected_files"] == [{"task_id": BUILD, "step_id": "build",
                                       "file": questions.NAME, "reason": REASON}]
    assert (f"step build (task {BUILD}) wrote questions.json the worker did not take: "
            f"{REASON}") in outcome.message
    # The ready's basis is untouched.
    assert wait["acknowledges"]["source"] == merge.OWNER_READY_REVIEW


def test_a_park_on_a_held_question_names_the_rejected_file_too(tmp_path):
    world = MergeWorld(tmp_path)
    _rejected_build(world)
    world.docs["task_other"] = {"tenant_id": TENANT, "workflow_id": WORKFLOW,
                                "step_id": "docs", "state": TaskState.SUCCEEDED.value,
                                "metadata": {"owner_answers": []}}
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": "task_other", "step_id": "docs", "question_index": 0}]}

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait["unreadable"] == 0
    assert [r["task_id"] for r in outcome.owner_wait["rejected_files"]] == [BUILD]
    assert "did not take" in outcome.message


def test_an_unreadable_ancestor_leaves_the_list_out_and_still_parks(tmp_path):
    world = MergeWorld(tmp_path)
    _rejected_build(world)
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [SENTENCE]}
    ctx = world.context()
    read = ctx.fetch_upstream
    world.docs[TASK]["depends_on"] = ["task_gone"]

    def gone(task_id: str) -> dict[str, Any]:
        if task_id == "task_gone":
            raise InputUnavailable("not this tenant's")
        return read(task_id)

    # A read that fails while the files are read never fails the park.
    ctx.fetch_upstream = gone
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.owner_wait["unreadable"] == 1
    assert "rejected_files" not in outcome.owner_wait


def test_a_build_steps_registered_question_reaches_the_review_and_holds_until_answered(tmp_path):
    world = MergeWorld(tmp_path)
    _asking_build(world)

    # The review, two steps on through the integrator's fix, is staged it.
    rows = owner_questions.collect(world.docs[REVIEW], fetch=world.docs.__getitem__,
                                   store=world.store, tenant_id=TENANT)
    assert [(r["task_id"], r["step_id"], r["question_index"]) for r in rows] == [
        (BUILD, "build", 0)]
    assert rows[0]["question"] == QUESTION["question"] and rows[0]["answer"] is None
    assert owner_questions.rejected_files(world.docs[REVIEW], world.docs.__getitem__) == []

    # It names the question by reference; the merge holds on it.
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": BUILD, "step_id": "build", "question_index": 0}]}
    parked = merge.run_merge(world.context())
    assert parked.state is TaskState.PARKED, parked.message
    assert [(q["task_id"], q["question_index"]) for q in parked.owner_wait["questions"]] == [
        (BUILD, 0)]
    assert parked.owner_wait["unreadable"] == 0
    assert "rejected_files" not in parked.owner_wait
    assert world.merge_calls() == []

    # The owner answers on the build task; the next attempt goes ahead.
    world.docs[BUILD]["metadata"]["owner_answers"] = [
        {"question_index": 0, "choice": "keep", "text": None}]
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_no_rejected_file_leaves_the_record_as_it_was(tmp_path):
    world = MergeWorld(tmp_path)
    _integrate(world, {questions.SUMMARY_KEY: 0})
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [SENTENCE]}

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    # MUTATION: write the key empty and the record changes for every park.
    assert set(outcome.owner_wait) == {"code", "questions", "unreadable", "acknowledges"}
    assert "did not take" not in outcome.message


def test_rejected_files_walks_the_workflow_in_order_and_clips_the_reason():
    long = "question 3: 'context' is longer than 8000 characters " * 20
    docs = {
        "task_a": {"workflow_id": WORKFLOW, "step_id": "build", "depends_on": ["task_b"],
                   "state": TaskState.SUCCEEDED.value,
                   "result_summary": {questions.SUMMARY_KEY: 0, questions.REJECTED_KEY: long}},
        "task_b": {"workflow_id": WORKFLOW, "step_id": "plan",
                   "state": TaskState.SUCCEEDED.value,
                   "result_summary": {questions.SUMMARY_KEY: 0,
                                      questions.REJECTED_KEY: "not a JSON list"}},
        # Not SUCCEEDED: its summary is no step's outcome yet.
        "task_c": {"workflow_id": WORKFLOW, "step_id": "docs", "state": "RUNNING",
                   "result_summary": {questions.REJECTED_KEY: "not a JSON list"}},
        # Another workflow's step is neither kept nor walked.
        "task_d": {"workflow_id": "wf_other", "step_id": "x",
                   "state": TaskState.SUCCEEDED.value,
                   "result_summary": {questions.REJECTED_KEY: "not a JSON list"}},
    }
    task = {"workflow_id": WORKFLOW, "depends_on": ["task_a", "task_c", "task_d"]}

    found = owner_questions.rejected_files(task, docs.__getitem__)

    assert [(f["task_id"], f["step_id"], f["file"]) for f in found] == [
        ("task_a", "build", questions.NAME), ("task_b", "plan", questions.NAME)]
    assert len(found[0]["reason"]) <= owner_questions.MAX_REJECTED_REASON_CHARS
    assert found[0]["reason"].startswith("question 3: 'context'")
    assert found[1]["reason"] == "not a JSON list"


def test_the_review_is_told_which_steps_file_was_not_taken(tmp_path):
    rejected = [{"task_id": BUILD, "step_id": "build", "file": questions.NAME,
                 "reason": REASON}]
    payload: dict[str, Any] = {"prompt": "review the patch"}

    # No question reached it, and still it is told.
    assert owner_questions.stage(tmp_path, payload, [], rejected=rejected) == 0
    assert not (tmp_path / owner_questions.NAME).exists()
    prompt = payload["prompt"]
    assert f"step build (task {BUILD})" in prompt and REASON in prompt
    assert "needs_owner" in prompt
    # The questions' guidance keeps its reference example.
    text = owner_questions.guidance(tmp_path / owner_questions.NAME, [{"answer": None}])
    assert json.dumps(verdict.NEEDS_OWNER_EXAMPLE) in text

    plain: dict[str, Any] = {"prompt": "review the patch"}
    owner_questions.stage(tmp_path, plain, [])
    assert plain["prompt"] == "review the patch"
