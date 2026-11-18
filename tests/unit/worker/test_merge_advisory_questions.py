"""Advisory owner questions do not hold a merge (owner decision, 2 November 2026).

Over the 14 days before, 251 hours of finished merges sat parked
HUMAN_REQUIRED on "Waiting for answers to N owner question before merging"
for questions the asking agent had itself marked non-blocking, 86 of those
hours ending only in a cancellation. Now the merge step goes ahead past an
unanswered `needs_owner` question that is not blocking, has a `recommended`
option the branch built, and is about nothing sensitive
(`owner_questions.hold_reason`). It records that on the task
(`result_summary.advisory_merged_past`) and on the pull request, and the
question stays open for the owner. It still holds for:

  * a blocking question;
  * a question with no recommended option;
  * a question whose branch built another option;
  * a question about IAM, secrets or credentials, tenant isolation, the
    frozen contract, cost or quota limits, or workflow files;
  * a question it cannot read (fail closed).

`merge_world.MergeWorld` is green in every respect; each test adds the
implement step's `questions.json` and the review's `needs_owner`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_worker import merge
from agent_worker import owner_questions
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from merge_world import API, NUMBER, TENANT, WORKFLOW, MergeWorld

IMPLEMENT = "task_impl"
PER_TASK = "per task"
PER_WORKFLOW = "per workflow"
#: Spelled out: the console and swarm-api's answer route read these.
HOLD = "owner_questions_open"
PAST = "advisory_merged_past"


def _question(**extra: Any) -> dict[str, Any]:
    question: dict[str, Any] = {
        "question": "Count the retries per task or per workflow?",
        "options": [{"label": PER_TASK, "description": "each task its own count"},
                    {"label": PER_WORKFLOW, "description": "one count for the lane"}],
        "recommended": PER_TASK,
        "context": "the retry counter's scope",
    }
    question.update(extra)
    return {k: v for k, v in question.items() if v is not _DROP}


_DROP = object()


def _implement(world: MergeWorld, questions: list[dict[str, Any]],
               answers: list[dict[str, Any]] | None = None) -> None:
    key = f"tenants/{TENANT}/tasks/{IMPLEMENT}/attempts/att_{IMPLEMENT}/artifacts/questions.json"
    data = json.dumps(questions).encode("utf-8")
    world.store.upload_bytes(key, data)
    world.docs[IMPLEMENT] = {
        "tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "implement",
        "state": TaskState.SUCCEEDED.value,
        "metadata": {"owner_answers": list(answers or [])},
        "result_summary": {
            "questions": len(questions),
            "artifacts": [{"name": "questions.json", "uri": world.store.uri(key),
                           "bytes": len(data)}],
        },
    }
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": i,
         "question": q["question"]} for i, q in enumerate(questions)]}


def _comments(world: MergeWorld) -> list[str]:
    return [c.body["body"] for c in world.github.calls("POST", f"{API}/issues/{NUMBER}/comments")]


def _assert_held(world: MergeWorld, outcome: Any) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.owner_wait is not None and outcome.owner_wait["code"] == HOLD
    assert world.merge_calls() == []
    assert PAST not in outcome.summary


def test_one_non_blocking_question_with_a_recommended_option_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, [_question(built=PER_TASK, blocking=False)])

    outcome = merge.run_merge(world.context())

    # MUTATION: hold on every needs_owner question (before 2 November 2026)
    # and this parks HUMAN_REQUIRED.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    # Recorded on the task...
    assert outcome.summary[PAST] == [{
        "task_id": IMPLEMENT, "step_id": "implement", "question_index": 0,
        "question": "Count the retries per task or per workflow?",
        "recommended": PER_TASK, "built": PER_TASK}]
    # ...and in the comment recording the merge; nothing answered it.
    listed = [b for b in _comments(world) if "advisory owner questions" in b]
    assert listed and "per task or per workflow" in listed[0], _comments(world)
    assert world.docs[IMPLEMENT]["metadata"]["owner_answers"] == []


def test_no_built_record_reads_as_the_recommended_option_built(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, [_question()])

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary[PAST][0]["built"] == PER_TASK


def test_a_blocking_question_still_holds(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, [_question(blocking=True)])

    _assert_held(world, merge.run_merge(world.context()))


def test_a_question_without_a_recommended_option_still_holds(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, [_question(recommended=_DROP)])

    _assert_held(world, merge.run_merge(world.context()))


def test_a_branch_that_built_another_option_still_holds(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, [_question(built=PER_WORKFLOW)])

    _assert_held(world, merge.run_merge(world.context()))


@pytest.mark.parametrize("field, words", [
    ("question", "Grant the scheduler's service account the IAM role it asks for?"),
    ("context", "The token is read from Secret Manager once per attempt."),
    ("question", "Share one namespace across tenants?"),
    ("context", "This adds a field to swarm_common, the frozen contract."),
    ("question", "Raise the pool's quota for the browser runner?"),
    ("context", "It changes .github/workflows/ci.yml."),
])
def test_a_question_about_a_sensitive_topic_still_holds(tmp_path, field, words):
    world = MergeWorld(tmp_path)
    _implement(world, [_question(**{field: words})])

    # MUTATION: drop the keyword match and this merges past an IAM question.
    _assert_held(world, merge.run_merge(world.context()))


def test_one_advisory_and_one_blocking_question_hold_for_the_blocking_one_alone(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, [_question(), _question(question="Ship the migration now?", blocking=True)])

    outcome = merge.run_merge(world.context())

    _assert_held(world, outcome)
    assert [q["question_index"] for q in outcome.owner_wait["questions"]] == [1]


def test_a_question_that_cannot_be_read_still_holds(tmp_path):
    """No questions.json to read the question from: fail closed, as before."""
    world = MergeWorld(tmp_path)
    world.docs[IMPLEMENT] = {"tenant_id": TENANT, "workflow_id": WORKFLOW,
                             "step_id": "implement", "metadata": {"owner_answers": []}}
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 0,
         "question": "Count the retries per task or per workflow?"}]}

    _assert_held(world, merge.run_merge(world.context()))


@pytest.mark.parametrize("row, reason", [
    (_question(), None),
    (_question(built=None), None),
    (_question(blocking=True), "blocking"),
    (_question(recommended=_DROP), "no recommended option"),
    (_question(built=PER_WORKFLOW), "built"),
    (_question(options=[{"label": PER_TASK, "description": "keeps the API key in memory"},
                        {"label": PER_WORKFLOW, "description": "x"}]), "secrets"),
    (_question(context="cross-tenant reads"), "tenant isolation"),
    (_question(question="Lift the budget cap?"), "cost or quota"),
])
def test_hold_reason_reads_only_the_questions_own_fields(row, reason):
    found = owner_questions.hold_reason(row)
    if reason is None:
        assert found is None
    else:
        assert found is not None and reason in found


def test_the_answer_routes_key_is_the_workers():
    from swarm_api import agent_questions

    assert agent_questions.ADVISORY_MERGED_PAST_KEY == merge.ADVISORY_MERGED_PAST_KEY == PAST
