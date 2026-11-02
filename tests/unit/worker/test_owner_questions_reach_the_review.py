"""A review step receives the owner questions every earlier step of its workflow asked.

#2070, owner decision 2026-10-23 (option (c)). `input_from` stages ONE file per
parent, and the lane template stages `swarm-work.patch`, so an implement
step's `questions.json` never reached the review: three lanes reached their
merge step with questions open, and each review wrote "the implementer left no
questions.json". Now a step whose dependants stage `verdict.json` from it -- a
review -- finds every earlier step's validated questions, with the answers
recorded so far, in `owner-questions.json` beside its staged inputs, and its
prompt says what to do with them.

Run through the production worker over in-memory Firestore and a local object
store, so the file is proved from the review's own checkpoint of `work/`, as
`test_input_from.py` proves a staged input.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any

from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt

WORKFLOW = "wf_questions"
IMPLEMENT = "task_impl"
REVIEW = "task_2"
FILE = "owner-questions.json"

QUESTIONS = [
    {
        "question": "Per task or per workflow?",
        "options": [{"label": "per task", "description": "one record a task"},
                    {"label": "per workflow", "description": "one record a workflow"}],
        "recommended": "per task",
        "context": "the API counts both",
    },
    {
        "question": "Does the new route join UNCAPTURED_ROUTES?",
        "options": [{"label": "yes", "description": "it carries a credential"},
                    {"label": "no", "description": "it does not"}],
        "recommended": "yes",
        "context": "a credential-bearing route",
        "blocking": True,
    },
]


def _seed_implement(db: Any, store: LocalObjectStore, *, questions: list[dict[str, Any]]) -> None:
    """The implement step, SUCCEEDED, that wrote `questions` (or none)."""
    artifacts: list[dict[str, Any]] = []
    if questions:
        key = f"tenants/{TENANT}/tasks/{IMPLEMENT}/attempts/att_i/artifacts/questions.json"
        data = json.dumps(questions).encode("utf-8")
        store.upload_bytes(key, data)
        artifacts.append({"name": "questions.json", "uri": store.uri(key), "bytes": len(data)})
    db.seed(f"tasks/{IMPLEMENT}", {
        "id": IMPLEMENT, "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "workflow_id": WORKFLOW, "step_id": "implement",
        "result_summary": {"questions": len(questions), "artifacts": artifacts},
        # The owner answered the first one on the task (`swarm_task_answer`).
        "metadata": {"owner_answers": [
            {"question_index": 0, "question": QUESTIONS[0]["question"],
             "choice": "per task", "text": None, "by": "owner@example.com"},
        ]} if questions else {},
    })


def _run_review(db: Any, worker_factory: Any) -> int:
    seed_attempt(db, task_id=REVIEW, attempt_id="att_2", lease_id="lease_2", task_input={
        "prompt": "review the patch", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "verdict.json", "artifact_text": '{"verdict": "MERGE", "findings": []}',
    })
    doc = db.doc(f"tasks/{REVIEW}")
    doc["workflow_id"] = WORKFLOW
    doc["step_id"] = "review"
    doc["depends_on"] = [IMPLEMENT]
    # A dependant stages `verdict.json` from it: this step IS a review.
    doc["metadata"] = {"expected_outputs": ["verdict.json"]}
    worker, _config, _exporter = worker_factory(
        task_id=REVIEW, attempt_id="att_2", lease_id="lease_2")
    return worker.run()


def _work_file(store: LocalObjectStore, name: str) -> bytes | None:
    """`name` in the review's last checkpoint of `work/`, None when it is not there."""
    keys = sorted(k for k in store.list_keys(f"tenants/{TENANT}/tasks/{REVIEW}/attempts/att_2/")
                  if k.endswith("/archive.tar.gz"))
    assert keys, "the review wrote no checkpoint"
    with tarfile.open(fileobj=io.BytesIO(store.download_bytes(keys[-1])), mode="r:gz") as archive:
        if name not in archive.getnames():
            return None
        member = archive.extractfile(name)
        assert member is not None
        return member.read()


def test_a_review_receives_both_implement_questions_with_the_recorded_answer(
    db, store, worker_factory, runner_inputs
):
    _seed_implement(db, store, questions=QUESTIONS)

    assert _run_review(db, worker_factory) == ExitCode.OK
    assert db.doc(f"tasks/{REVIEW}")["state"] == TaskState.SUCCEEDED.value

    raw = _work_file(store, FILE)
    # MUTATION: stage nothing for a review (the #2070 bug) and this is None.
    assert raw is not None, f"the review was not given {FILE}"
    rows = json.loads(raw)
    # MUTATION: stage only unanswered questions, or only the first, and this is one row.
    assert [(r["task_id"], r["step_id"], r["question_index"]) for r in rows] == [
        (IMPLEMENT, "implement", 0), (IMPLEMENT, "implement", 1)]
    # MUTATION: drop the owner_answers read and the first answer is None.
    assert rows[0]["answer"] == {"choice": "per task", "text": None}
    assert rows[1]["answer"] is None
    # MUTATION: drop a field from the row and the review cannot judge the question.
    assert rows[1]["question"] == QUESTIONS[1]["question"]
    assert rows[1]["options"] == QUESTIONS[1]["options"]
    assert rows[1]["recommended"] == "yes"
    assert rows[1]["context"] == "a credential-bearing route"
    # MUTATION: lose the asker's `blocking` and the review cannot tell it held.
    assert rows[1]["blocking"] is True and rows[0]["blocking"] is False

    prompt = runner_inputs[-1]["prompt"]
    # MUTATION: stage the file and say nothing, and the review never reads it.
    assert FILE in prompt
    assert "needs_owner" in prompt and "open_questions" in prompt
    assert prompt.startswith("review the patch")


def test_a_workflow_with_no_questions_reviews_exactly_as_today(
    db, store, worker_factory, runner_inputs
):
    _seed_implement(db, store, questions=[])

    assert _run_review(db, worker_factory) == ExitCode.OK
    assert db.doc(f"tasks/{REVIEW}")["state"] == TaskState.SUCCEEDED.value
    # MUTATION: write an empty list for a workflow that asked nothing and this fails.
    assert _work_file(store, FILE) is None
    # MUTATION: append the guidance unconditionally and this fails.
    assert FILE not in runner_inputs[-1]["prompt"]
    assert "needs_owner" not in runner_inputs[-1]["prompt"]


def test_a_step_that_is_not_a_review_is_given_no_questions(
    db, store, worker_factory, runner_inputs
):
    _seed_implement(db, store, questions=QUESTIONS)
    seed_attempt(db, task_id=REVIEW, attempt_id="att_2", lease_id="lease_2", task_input={
        "prompt": "fix it", "steps": 1, "sleep_seconds": 0.01})
    doc = db.doc(f"tasks/{REVIEW}")
    doc["workflow_id"] = WORKFLOW
    doc["depends_on"] = [IMPLEMENT]
    worker, _config, _exporter = worker_factory(
        task_id=REVIEW, attempt_id="att_2", lease_id="lease_2")

    assert worker.run() == ExitCode.OK
    # MUTATION: stage for every workflow step and this fails.
    assert _work_file(store, FILE) is None
    assert FILE not in runner_inputs[-1]["prompt"]


def test_questions_two_steps_back_reach_a_re_review(db, store, worker_factory, runner_inputs):
    """implement -> review -> fix -> re-review: the re-review depends on the fix alone."""
    _seed_implement(db, store, questions=QUESTIONS)
    db.seed("tasks/task_fix", {
        "id": "task_fix", "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "workflow_id": WORKFLOW, "step_id": "fix", "depends_on": [IMPLEMENT],
        "result_summary": {"questions": 0, "artifacts": []}, "metadata": {},
    })
    # A step of ANOTHER workflow the fix names is never walked into.
    db.seed("tasks/task_other", {
        "id": "task_other", "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "workflow_id": "wf_other", "step_id": "implement",
        "result_summary": {"questions": 1, "artifacts": []}, "metadata": {},
    })
    db.doc("tasks/task_fix")["depends_on"] = [IMPLEMENT, "task_other"]
    seed_attempt(db, task_id=REVIEW, attempt_id="att_2", lease_id="lease_2", task_input={
        "prompt": "review again", "steps": 1, "sleep_seconds": 0.01,
        "artifact_name": "verdict.json", "artifact_text": '{"verdict": "MERGE", "findings": []}',
    })
    doc = db.doc(f"tasks/{REVIEW}")
    doc.update(workflow_id=WORKFLOW, step_id="re-review", depends_on=["task_fix"],
               metadata={"expected_outputs": ["verdict.json"]})
    worker, _config, _exporter = worker_factory(
        task_id=REVIEW, attempt_id="att_2", lease_id="lease_2")

    assert worker.run() == ExitCode.OK
    raw = _work_file(store, FILE)
    # MUTATION: read only the direct parents and this is None.
    assert raw is not None
    rows = json.loads(raw)
    # MUTATION: walk out of the workflow and task_other's unreadable count fails the step
    # or adds a row.
    assert {r["task_id"] for r in rows} == {IMPLEMENT}
    assert len(rows) == 2


def test_the_guidance_puts_an_owner_only_finding_in_needs_owner_under_merge():
    from agent_worker import owner_questions

    text = " ".join(owner_questions.guidance(
        "/work/owner-questions.json", [{"answer": None}]).split())

    # MUTATION: drop the sentence and a review records a finding that waits only on
    # the owner as NOT_YET, which the merge cannot hold for (#2764, wf_2a8fba8b).
    assert ("A finding that waits only on an unanswered owner question is not a "
            "NOT_YET finding") in text
    assert "give MERGE if nothing else is open" in text
    assert "give NOT_YET only for findings a fix step can act on now" in text
