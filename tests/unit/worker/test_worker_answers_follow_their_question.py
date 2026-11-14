"""The worker pairs an owner answer with the question it answered, not its index (#3349).

An answer is stored under its `question_index`, and a later attempt of the
same task may ask another question at that index. swarm-api's reads keep an
answer with its question (`agent_questions.answers_to`); the worker's readers
paired by index alone, so a fix step (`owner_questions.questions_of`), a
parked wait's continuation (`questions.answers_for`), the merge's
owner-question hold (`merge._unanswered`) and the security review's answer
rows (`merge._security_answers`) took an answer given to the earlier question
as the answer to the new one:

* an answer stamped (`asked_digest`) for another question reads unanswered;
* an unstamped answer recording another question's text reads unanswered;
* the same question asked again reads answered, by stamp or by text;
* an answer recording neither is kept by its index, as before.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from agent_worker import merge, owner_questions, questions
from agent_worker.errors import InputUnavailable
from agent_worker.objectstore import LocalObjectStore
from agent_worker.verdict import OwnerQuestionRef
from swarm_common.states import TaskState

from worker_seeds import TENANT

WORKFLOW = "wf_answers"
TASK = "task_asker"


def _asked(text: str) -> dict[str, Any]:
    return {"question": text,
            "options": [{"label": "yes", "description": "do it"},
                        {"label": "no", "description": "leave it"}],
            "recommended": "yes", "context": "a lane question"}


EARLIER = _asked("Which model path writes the description?")
LATER = _asked("Should a workflow's read say why a description is missing?")


def _answer(*, stamp: str | None = None, text: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"question_index": 0, "choice": "yes", "text": None,
                             "by": "owner@example.com"}
    if stamp is not None:
        entry[questions.ASKED_DIGEST_KEY] = stamp
    if text is not None:
        entry["question"] = text
    return entry


def _doc(store: LocalObjectStore, asked: dict[str, Any], answer: dict[str, Any], *,
         digests: bool = True) -> dict[str, Any]:
    """The asking task, SUCCEEDED, whose latest attempt asked `asked` at index 0."""
    key = f"tenants/{TENANT}/tasks/{TASK}/attempts/att_2/artifacts/questions.json"
    data = json.dumps([asked]).encode("utf-8")
    store.upload_bytes(key, data)
    summary: dict[str, Any] = {
        "questions": 1,
        "artifacts": [{"name": "questions.json", "uri": store.uri(key), "bytes": len(data)}]}
    if digests:
        summary[questions.DIGESTS_KEY] = [questions.digest(asked)]
    return {"id": TASK, "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
            "workflow_id": WORKFLOW, "step_id": "implement", "result_summary": summary,
            "metadata": {"owner_answers": [answer]}}


def _ctx(store: LocalObjectStore, doc: dict[str, Any]) -> Any:
    def fetch(task_id: str) -> dict[str, Any]:
        if task_id != TASK:
            raise InputUnavailable(f"no task {task_id}")
        return doc
    return SimpleNamespace(store=store, tenant_id=TENANT, workflow_id=WORKFLOW,
                           fetch_upstream=fetch)


def _rows(store: LocalObjectStore, doc: dict[str, Any]) -> list[Any]:
    return [row["answer"] for row in owner_questions.questions_of(
        TASK, doc, store=store, tenant_id=TENANT)]


def _merge_reads(store: LocalObjectStore, doc: dict[str, Any]) -> tuple[bool, Any]:
    """`(answered for the hold, the security row's answer)` of question 0."""
    ctx = _ctx(store, doc)
    ref = OwnerQuestionRef(task_id=TASK, question_index=0, step_id="implement", question="q")
    held = merge._unanswered(ctx, (ref,))
    (row,) = merge._security_answers(ctx, (ref,))
    return not held, row["answer"]


ANSWERED = {"choice": "yes", "text": None}


# -- questions_of (a fix step's owner answers, the merge's pick checks) --------


def test_questions_of_reads_a_reasked_index_unanswered_by_stamp_and_by_text(store):
    for answer in (_answer(stamp=questions.digest(EARLIER)), _answer(text=EARLIER["question"])):
        # MUTATION: pairing by index alone serves the earlier answer here.
        assert _rows(store, _doc(store, LATER, answer)) == [None], answer


def test_questions_of_reads_the_same_question_answered(store):
    for answer in (_answer(stamp=questions.digest(LATER)), _answer(text=LATER["question"])):
        assert _rows(store, _doc(store, LATER, answer)) == [ANSWERED], answer


def test_questions_of_keeps_an_answer_recording_no_question_by_index(store):
    assert _rows(store, _doc(store, LATER, _answer())) == [ANSWERED]


# -- answers_for (a parked wait's continuation) --------------------------------


def _waited(answer: dict[str, Any], asked: dict[str, Any]) -> Any:
    task = {"metadata": {questions.QUESTION_WAIT_KEY: {"questions": [asked], "blocking": [0]},
                         "owner_answers": [answer]}}
    return [row["answer"] for row in questions.answers_for(task)]


def test_answers_for_reads_a_reasked_index_unanswered():
    assert _waited(_answer(stamp=questions.digest(EARLIER)), LATER) == [None]
    assert _waited(_answer(text=EARLIER["question"]), LATER) == [None]


def test_answers_for_reads_the_same_question_and_an_unrecorded_answer_answered():
    assert _waited(_answer(stamp=questions.digest(LATER)), LATER) == [ANSWERED]
    assert _waited(_answer(text=LATER["question"]), LATER) == [ANSWERED]
    assert _waited(_answer(), LATER) == [ANSWERED]


# -- the merge's owner-question hold and the security review's rows ------------


def test_the_merge_holds_on_a_reasked_index(store):
    cases = [
        # Stamped: compared with the task's question_digests.
        _doc(store, LATER, _answer(stamp=questions.digest(EARLIER))),
        # Stamped, summary without digests: compared with the file's question.
        _doc(store, LATER, _answer(stamp=questions.digest(EARLIER)), digests=False),
        # Unstamped with its text (every answer stored before the stamp).
        _doc(store, LATER, _answer(text=EARLIER["question"])),
    ]
    for doc in cases:
        # MUTATION: the index-only `_answered` lets the merge through here.
        assert _merge_reads(store, doc) == (False, None), doc["metadata"]


def test_the_merge_takes_the_same_question_answered(store):
    cases = [
        _doc(store, LATER, _answer(stamp=questions.digest(LATER))),
        _doc(store, LATER, _answer(stamp=questions.digest(LATER)), digests=False),
        _doc(store, LATER, _answer(text=LATER["question"])),
        _doc(store, LATER, _answer()),
    ]
    for doc in cases:
        assert _merge_reads(store, doc) == (True, ANSWERED), doc["metadata"]


def test_the_merge_fails_closed_when_the_question_cannot_be_read(store):
    doc = _doc(store, LATER, _answer(text=LATER["question"]))
    doc["result_summary"]["questions"] = 2  # the file no longer holds what was counted
    assert _merge_reads(store, doc) == (False, None)
    # An answer recording no question needs no read, and is kept by its index.
    doc["metadata"] = {"owner_answers": [_answer()]}
    assert _merge_reads(store, doc) == (True, ANSWERED)
