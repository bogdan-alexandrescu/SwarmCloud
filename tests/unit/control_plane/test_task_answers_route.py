"""`POST /v1/tasks/{id}/answers` -- the owner answers an agent's questions in the console.

THE DECISION (owner, 2026-10-13 ~05:25 PDT, "Yes, pick an option on the task";
lane TASK-ANSWERS, part of #1206). The "Questions for you" card showed what an
agent asked and said the answer went back through whoever launched it. Now
the person picks an option or writes their own answer on the task, it is
stored on the task, the bell stops asking, and whoever launched the work
reads it. These tests hold the route to:

  1. VALIDATION -- the index names one of the questions the worker counted,
     a choice is one of THAT question's option labels, free text is at most
     2,000 characters, and an answer says something;
  2. WHO MAY ANSWER -- the task's submitter or an admin; another member of the
     tenant is a 403 and another tenant's task the 404 a missing one is;
  3. ONE ANSWER PER QUESTION -- re-answering replaces it, and the replaced
     answer is kept in the entry's `history`, with who and when;
  4. WHAT IS SERVED -- `GET /questions` carries each question's answer and
     whether the caller may answer, the task carries `owner_answers`, and free
     text is redacted before it is stored;
  5. THE KEY IS THE PLATFORM'S -- a caller cannot plant `owner_answers` in a
     submission's metadata.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY, RESERVED_METADATA_KEYS

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"
TASK = "task_q"
OWNER = "alice@saga.xyz"


def _key(name: str, *, tenant: str, task: str, attempt: str = "att_1") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}/artifacts/{name}"


def _questions() -> list[dict]:
    return [
        {
            "question": "Per task or per workflow?",
            "options": [
                {"label": "per task", "description": "one file per task"},
                {"label": "per workflow", "description": "one file for the run"},
            ],
            "recommended": "per task",
            "context": "the step writes one file today",
        },
        {
            "question": "Keep the old key for a release?",
            "options": [
                {"label": "keep", "description": "readers keep working"},
                {"label": "drop", "description": "one key"},
            ],
            "recommended": "drop",
            "context": None,
        },
    ]


def _asked(db, objects, *, by: str = OWNER, tenant: str = "eng", task: str = TASK) -> dict:
    seed_tenant(db, tenant)
    doc = seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED")
    doc["submitted_by"] = by
    raw = json.dumps(_questions()).encode()
    objects.put(_key("questions.json", tenant=tenant, task=task), raw)
    doc["result_summary"] = {
        "artifacts": [{
            "name": "questions.json",
            "bytes": len(raw),
            "uri": f"gs://{BUCKET}/{_key('questions.json', tenant=tenant, task=task)}",
        }],
        "artifact_bytes": len(raw),
        "logs": {},
        "questions": 2,
    }
    doc["completed_at"] = datetime.now(timezone.utc)
    return doc


def _answer(client, body: dict, *, user: str = "alice", task: str = TASK):
    return client.post(f"/v1/tasks/{task}/answers", json=body, headers=auth_header(user))


def _stored(db, task: str = TASK) -> list[dict]:
    return db.docs[f"tasks/{task}"]["metadata"].get(OWNER_ANSWERS_METADATA_KEY) or []


# -- 1. validation -------------------------------------------------------------

def test_the_submitter_picks_an_option_and_it_is_stored_on_the_task(client, db, objects) -> None:
    _asked(db, objects)

    response = _answer(client, {"question_index": 0, "choice": "per workflow", "text": None})

    assert response.status_code == 200, response.text
    stored = _stored(db)
    assert len(stored) == 1
    entry = stored[0]
    assert entry["question_index"] == 0
    assert entry["question"] == "Per task or per workflow?"
    assert entry["choice"] == "per workflow"
    assert entry["text"] is None
    assert entry["by"] == OWNER
    assert entry["at"]
    assert entry["history"] == []
    # The route answers with the questions as `GET /questions` serves them now.
    body = response.json()
    assert body["questions"][0]["answer"]["choice"] == "per workflow"
    assert body["questions"][1]["answer"] is None


def test_a_free_text_answer_is_stored(client, db, objects) -> None:
    _asked(db, objects)

    response = _answer(client, {"question_index": 1, "choice": None, "text": "keep it for two releases"})

    assert response.status_code == 200, response.text
    assert _stored(db)[0]["choice"] is None
    assert _stored(db)[0]["text"] == "keep it for two releases"


def test_an_index_beyond_the_questions_is_refused(client, db, objects) -> None:
    _asked(db, objects)

    for index in (2, -1):
        response = _answer(client, {"question_index": index, "choice": "keep"})
        assert response.status_code == 422, response.text
    assert _stored(db) == []


def test_a_choice_that_is_not_one_of_that_questions_labels_is_refused(client, db, objects) -> None:
    _asked(db, objects)

    # `keep` is question 2's label, not question 1's.
    response = _answer(client, {"question_index": 0, "choice": "keep"})

    assert response.status_code == 422, response.text
    assert "per task" in response.text
    assert _stored(db) == []


def test_an_answer_with_neither_choice_nor_text_is_refused(client, db, objects) -> None:
    _asked(db, objects)

    for body in ({"question_index": 0}, {"question_index": 0, "choice": None, "text": "   "}):
        response = _answer(client, body)
        assert response.status_code == 422, response.text
    assert _stored(db) == []


def test_free_text_over_2000_characters_is_refused(client, db, objects) -> None:
    _asked(db, objects)

    response = _answer(client, {"question_index": 0, "text": "x" * 2001})

    assert response.status_code == 422, response.text
    assert _stored(db) == []
    assert _answer(client, {"question_index": 0, "text": "x" * 2000}).status_code == 200


def test_a_task_that_asked_none_takes_no_answer(client, db, objects) -> None:
    doc = _asked(db, objects)
    doc["result_summary"]["questions"] = 0

    response = _answer(client, {"question_index": 0, "choice": "per task"})

    assert response.status_code == 422, response.text
    assert _stored(db) == []


# -- 2. who may answer -----------------------------------------------------------

def test_an_admin_of_the_tenant_may_answer(client, db, objects) -> None:
    """`root` is an admin and a member of eng; the task is dana's."""
    _asked(db, objects, by="dana@saga.xyz")

    response = _answer(client, {"question_index": 0, "choice": "per task"}, user="root")

    assert response.status_code == 200, response.text
    assert _stored(db)[0]["by"] == "root@saga.xyz"


def test_another_member_of_the_tenant_may_not_answer(client, db, objects) -> None:
    """`alice` is in eng but did not submit dana's task and is not an admin."""
    _asked(db, objects, by="dana@saga.xyz")

    response = _answer(client, {"question_index": 0, "choice": "per task"})

    assert response.status_code == 403, response.text
    assert _stored(db) == []
    # She still reads the questions, and is told why she may not answer.
    read = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()
    assert read["status"] == "ok"
    assert read["may_answer"] is False
    assert "dana@saga.xyz" in read["may_answer_because"]


def test_another_tenants_task_is_a_404(client, db, objects) -> None:
    """`bob` is in research; the task is eng's."""
    _asked(db, objects)

    response = _answer(client, {"question_index": 0, "choice": "per task"}, user="bob")

    assert response.status_code == 404, response.text
    assert TASK in response.json()["message"]
    assert "Per task or per workflow" not in response.text
    assert _stored(db) == []


# -- 3. one answer per question, re-answering audited ---------------------------

def test_re_answering_replaces_the_answer_and_keeps_the_replaced_one(client, db, objects) -> None:
    _asked(db, objects, by="dana@saga.xyz")
    assert _answer(client, {"question_index": 0, "choice": "per task"}, user="root").status_code == 200
    assert _answer(client, {"question_index": 1, "choice": "drop"}, user="root").status_code == 200

    response = _answer(client, {"question_index": 0, "choice": "per workflow", "text": "after all"}, user="root")

    assert response.status_code == 200, response.text
    stored = _stored(db)
    assert sorted(e["question_index"] for e in stored) == [0, 1]
    first = next(e for e in stored if e["question_index"] == 0)
    assert first["choice"] == "per workflow"
    assert first["text"] == "after all"
    assert len(first["history"]) == 1
    replaced = first["history"][0]
    assert replaced["choice"] == "per task"
    assert replaced["by"] == "root@saga.xyz"
    assert replaced["at"]
    # The other question's answer is untouched.
    assert next(e for e in stored if e["question_index"] == 1)["choice"] == "drop"


# -- 4. what is served -------------------------------------------------------------

def test_get_questions_serves_each_questions_answer_and_who_may_answer(client, db, objects) -> None:
    _asked(db, objects)
    _answer(client, {"question_index": 1, "choice": "keep", "text": "one release"})

    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()

    assert body["status"] == "ok"
    assert body["may_answer"] is True
    assert body["questions"][0]["answer"] is None
    answer = body["questions"][1]["answer"]
    assert answer["choice"] == "keep"
    assert answer["text"] == "one release"
    assert answer["by"] == OWNER
    assert answer["at"]
    # The question's own four keys are still served as they were.
    assert body["questions"][1]["question"] == "Keep the old key for a release?"


def test_free_text_is_redacted_before_it_is_stored(client, db, objects) -> None:
    _asked(db, objects)
    secret = "ya" + "29." + "A0" + "q" * 40

    response = _answer(client, {"question_index": 0, "text": f"use Authorization: Bearer {secret}"})

    assert response.status_code == 200, response.text
    assert secret not in json.dumps(_stored(db), default=str)
    assert secret not in response.text
    assert "use Authorization" in _stored(db)[0]["text"]


def test_the_task_carries_owner_answers(client, db, objects) -> None:
    _asked(db, objects)
    _answer(client, {"question_index": 0, "choice": "per task"})

    task = client.get(f"/v1/tasks/{TASK}", headers=auth_header("alice")).json()["task"]

    assert [a["choice"] for a in task["owner_answers"]] == ["per task"]
    assert task["owner_answers"][0]["question"] == "Per task or per workflow?"


def test_a_task_with_no_answers_carries_an_empty_list(client, db, objects) -> None:
    _asked(db, objects)

    task = client.get(f"/v1/tasks/{TASK}", headers=auth_header("alice")).json()["task"]

    assert task["owner_answers"] == []


# -- 5. the key is the platform's ---------------------------------------------------

def test_a_caller_cannot_plant_owner_answers_at_submission(client, db) -> None:
    seed_tenant(db, "eng")
    assert OWNER_ANSWERS_METADATA_KEY in RESERVED_METADATA_KEYS

    response = client.post(
        "/v1/tasks",
        json={
            "runner_profile": "mock",
            "input": {"prompt": "hello"},
            "metadata": {OWNER_ANSWERS_METADATA_KEY: [{"question_index": 0, "choice": "x"}]},
        },
        headers=auth_header("alice"),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reserved_metadata_keys"] == [OWNER_ANSWERS_METADATA_KEY]
