"""An answer is served only under the question it answered (#3349).

Answers are stored by `question_index`. On task_0ea41221d0eb45428bee the first
attempt asked at index 0 which model writes a generated description and was
answered; the next attempt's questions.json asked a different question at
index 0, and the read served the old answer under it. These tests hold
swarm-api to:

  1. THE ISSUE'S CASE -- an answer, then a new attempt asking a different
     question at the same index: that question reads unanswered; the same
     question asked again still reads answered;
  2. COUNTS AGREE WITH WHAT IS SERVED -- a wait's blocking and unanswered
     indices leave out an answer to another question, so the last answer to
     the question actually asked is what promotes the task;
  3. HISTORY -- re-answering the new question keeps the earlier answer in
     `history`, served with its own question;
  4. OLD ANSWERS -- an answer stored before the record holds only its
     question's text, which is compared; one with no record at all cannot be
     checked and is served as it always was.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from swarm_api import agent_questions
from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY, QUESTION_WAIT_METADATA_KEY

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-example-project"
TASK = "task_q"
OWNER = "alice@example.com"
MODEL = "Which model writes a generated description?"
OTHER = "Which branch does the fix land on?"


def _key(name: str) -> str:
    return f"tenants/eng/tasks/{TASK}/attempts/att_1/artifacts/{name}"


def _q(text: str, labels: tuple[str, str] = ("Vertex AI Claude Haiku", "none")) -> dict:
    return {
        "question": text,
        "options": [{"label": labels[0], "description": "first"},
                    {"label": labels[1], "description": "second"}],
        "recommended": labels[0],
        "context": None,
    }


def _ask(db, objects, questions: list[dict]) -> dict:
    """The task with `questions` as its (latest attempt's) questions.json."""
    seed_tenant(db, "eng")
    path = f"tasks/{TASK}"
    if path in db.docs:
        doc = db.docs[path]
    else:
        doc = seed_task(db, task_id=TASK, tenant_id="eng", state="SUCCEEDED")
        doc["submitted_by"] = OWNER
        doc["completed_at"] = datetime.now(timezone.utc)
    raw = json.dumps(questions).encode()
    objects.put(_key("questions.json"), raw)
    doc["result_summary"] = {
        "artifacts": [{"name": "questions.json", "bytes": len(raw),
                       "uri": f"gs://{BUCKET}/{_key('questions.json')}"}],
        "artifact_bytes": len(raw),
        "logs": {},
        "questions": len(questions),
    }
    return doc


def _answer(client, index: int, choice: str):
    return client.post(f"/v1/tasks/{TASK}/answers", json={"question_index": index, "choice": choice},
                       headers=auth_header("alice"))


def _read(client) -> dict:
    return client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()


# -- 1. the issue's case ------------------------------------------------------------

def test_a_new_attempt_asking_another_question_at_the_index_reads_unanswered(client, db, objects) -> None:
    _ask(db, objects, [_q(MODEL)])
    assert _answer(client, 0, "Vertex AI Claude Haiku").status_code == 200

    _ask(db, objects, [_q(OTHER, ("main", "a release branch"))])
    body = _read(client)

    assert body["status"] == "ok", body
    # MUTATION: join answers by index alone and the branch question reads
    # "Vertex AI Claude Haiku", an answer nobody gave to it.
    assert body["questions"][0]["question"] == OTHER
    assert body["questions"][0]["answer"] is None


def test_the_same_question_asked_again_still_reads_answered(client, db, objects) -> None:
    _ask(db, objects, [_q(MODEL)])
    assert _answer(client, 0, "Vertex AI Claude Haiku").status_code == 200

    _ask(db, objects, [_q(MODEL)])
    body = _read(client)

    assert body["questions"][0]["answer"]["choice"] == "Vertex AI Claude Haiku"


def test_the_same_text_with_other_options_is_another_question(client, db, objects) -> None:
    _ask(db, objects, [_q(MODEL)])
    assert _answer(client, 0, "Vertex AI Claude Haiku").status_code == 200

    # The choice was one of the old options; it does not answer these.
    _ask(db, objects, [_q(MODEL, ("Gemini Flash", "none"))])

    assert _read(client)["questions"][0]["answer"] is None


def test_an_answer_records_the_question_it_answered(client, db, objects) -> None:
    _ask(db, objects, [_q(MODEL)])
    assert _answer(client, 0, "none").status_code == 200

    entry = db.docs[f"tasks/{TASK}"]["metadata"][OWNER_ANSWERS_METADATA_KEY][0]

    assert entry["question"] == MODEL
    # MUTATION: store no digest and options changed under the same text pass as answered.
    assert entry["question_digest"] == agent_questions.question_digest(_q(MODEL))


# -- 2. counts agree with what is served -------------------------------------------------

def _wait(first: str) -> dict:
    return {
        "questions": [{**_q(first), "blocking": True}, {**_q(OTHER), "blocking": False}],
        "blocking": [0],
        "attempt_id": "att_2",
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }


def _stale_answer() -> dict:
    return {"question_index": 0, "question": MODEL,
            "question_digest": agent_questions.question_digest(_q(MODEL)),
            "choice": "Vertex AI Claude Haiku", "text": None, "by": OWNER,
            "at": "2026-11-04T10:00:00+00:00", "history": []}


def test_blocking_unanswered_leaves_out_an_answer_to_another_question() -> None:
    metadata = {OWNER_ANSWERS_METADATA_KEY: [_stale_answer()]}

    # MUTATION: count by index and this wait reads as answered, so the agent resumes.
    assert agent_questions.blocking_unanswered(_wait("Which test proves it?"), metadata) == [0]
    assert agent_questions.blocking_unanswered(_wait(MODEL), metadata) == []


def test_unanswered_indices_given_the_questions_agree_with_the_read() -> None:
    metadata = {OWNER_ANSWERS_METADATA_KEY: [_stale_answer()]}

    assert agent_questions.unanswered_indices(1, metadata, [_q(OTHER)]) == [0]
    assert agent_questions.unanswered_indices(1, metadata, [_q(MODEL)]) == []
    assert agent_questions.unanswered(1, metadata, [_q(OTHER)]) == 1


def test_a_parked_wait_serves_and_counts_an_answer_to_another_question_as_open(client, db) -> None:
    seed_tenant(db, "eng")
    moment = datetime.now(timezone.utc)
    wait = _wait("Which test proves it?")
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=moment)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = 1
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait", "question_wait": wait}]
    # Adopted for this attempt, holding an answer given to another question at index 0.
    doc["metadata"] = {QUESTION_WAIT_METADATA_KEY: dict(wait), OWNER_ANSWERS_METADATA_KEY: [_stale_answer()]}

    body = _read(client)

    assert body["questions"][0]["answer"] is None
    assert body["waiting"]["unanswered"] == [0]

    # The answer to the question asked is the last blocking one: it promotes.
    response = _answer(client, 0, "none")
    assert response.status_code == 200, response.text
    assert db.docs[f"tasks/{TASK}"]["state"] == "READY"


# -- 3. history ---------------------------------------------------------------------------

def test_the_earlier_answer_stays_in_history_with_its_own_question(client, db, objects) -> None:
    _ask(db, objects, [_q(MODEL)])
    assert _answer(client, 0, "Vertex AI Claude Haiku").status_code == 200
    _ask(db, objects, [_q(OTHER, ("main", "a release branch"))])

    response = _answer(client, 0, "main")

    assert response.status_code == 200, response.text
    answer = response.json()["questions"][0]["answer"]
    assert (answer["question"], answer["choice"]) == (OTHER, "main")
    # MUTATION: keep only the choice in history and the earlier answer reads as one to OTHER.
    assert [(h["question"], h["choice"]) for h in answer["history"]] == [(MODEL, "Vertex AI Claude Haiku")]


# -- 4. answers stored before the record --------------------------------------------------

def test_an_old_answer_with_only_its_text_is_compared_by_text(client, db, objects) -> None:
    doc = _ask(db, objects, [_q(OTHER, ("main", "a release branch"))])
    doc["metadata"] = {OWNER_ANSWERS_METADATA_KEY: [
        {"question_index": 0, "question": MODEL, "choice": "Vertex AI Claude Haiku", "text": None,
         "by": OWNER, "at": "2026-11-04T10:00:00+00:00", "history": []}]}

    assert _read(client)["questions"][0]["answer"] is None


def test_an_old_answer_with_no_record_is_served_as_before(client, db, objects) -> None:
    doc = _ask(db, objects, [_q(OTHER, ("main", "a release branch"))])
    doc["metadata"] = {OWNER_ANSWERS_METADATA_KEY: [
        {"question_index": 0, "choice": "main", "text": None,
         "by": OWNER, "at": "2026-11-04T10:00:00+00:00", "history": []}]}

    body = _read(client)

    # It cannot be checked, so it is served under its index as it always was.
    assert body["questions"][0]["answer"]["choice"] == "main"
    assert agent_questions.unanswered_indices(1, doc["metadata"], [_q(OTHER)]) == []
