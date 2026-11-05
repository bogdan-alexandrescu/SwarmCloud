"""The list paths count an answer only when it answers the question asked now.

#3349 made the questions read serve an answer only under the question it
answered (`agent_questions.answers_to`). Three callers read only the task
document, never its questions file, so they still counted an answer by its
index alone: the sc plugin's check (`GET /v1/tasks/open-questions`,
`agent_questions.open_question_tasks`), a workflow step's `questions_unread`
(`codec.step_needs`) and the bell's question item
(`notifications.agent_questions`). A later attempt asking another question
at index 0 then read as answered on all three, and nobody was told.

The worker now writes a digest of each question beside the count
(`result_summary.question_digests`), an answer is stamped with the digest
of the question it answered, and the three compare the two:

  1. an answer given to question A at index 0, while index 0 now asks B,
     leaves index 0 open;
  2. an answer to the same question still counts it answered;
  3. a task with no digests (written before them) counts by index as before.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_worker import questions as worker_questions
from swarm_api import agent_questions
from swarm_api import notifications as notif
from swarm_api.codec import step_needs, task_from_dict
from swarm_api.store import Store
from swarm_api.task_input import masking_for

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"
TASK = "task_q"
OWNER = "alice@example.com"
SESSION = "0f0e0d0c-aaaa-4bbb-8ccc-0000000000b1"
A = {"question": "Which model writes a generated description?",
     "options": [{"label": "Vertex AI Claude Haiku", "description": "first"},
                 {"label": "none", "description": "second"}],
     "recommended": "none", "context": None}
B = {"question": "Which branch does the fix land on?",
     "options": [{"label": "main", "description": "first"},
                 {"label": "a release branch", "description": "second"}],
     "recommended": "main", "context": None}


def _key(name: str) -> str:
    return f"tenants/eng/tasks/{TASK}/attempts/att_1/artifacts/{name}"


def _ask(db, objects, asked: list[dict], *, digests: bool = True) -> dict:
    """The task with `asked` as its latest attempt's questions.json, summarised
    as the worker summarises it (with the digests, or as an older worker did)."""
    seed_tenant(db, "eng")
    path = f"tasks/{TASK}"
    if path in db.docs:
        doc = db.docs[path]
    else:
        doc = seed_task(db, task_id=TASK, tenant_id="eng", state="SUCCEEDED")
        doc["submitted_by"] = OWNER
        doc["completed_at"] = datetime.now(timezone.utc)
        doc["metadata"] = {"claude_session": SESSION}
    raw = json.dumps(asked).encode()
    objects.put(_key("questions.json"), raw)
    summary = {
        "artifacts": [{"name": "questions.json", "bytes": len(raw),
                       "uri": f"gs://{BUCKET}/{_key('questions.json')}"}],
        "artifact_bytes": len(raw),
        "logs": {},
        "questions": len(asked),
    }
    if digests:
        summary["question_digests"] = [worker_questions.digest(q) for q in asked]
    doc["result_summary"] = summary
    return doc


def _answer(client, index: int, choice: str) -> None:
    response = client.post(f"/v1/tasks/{TASK}/answers", json={"question_index": index, "choice": choice},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text


def _open_question_ids(client) -> list[str]:
    response = client.get("/v1/tasks/open-questions", params={"claude_session": SESSION},
                          headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return [t["id"] for t in response.json()["tasks"]]


def _questions_unread(db) -> list[int]:
    task = task_from_dict(db.docs[f"tasks/{TASK}"])
    return step_needs(task, masking_for(task))["questions_unread"]


def _bell_ids(db) -> list[str]:
    viewer = notif.Viewer(email=OWNER, tenant_id="eng", is_admin=False, is_owner=False)
    reading = notif.Reading(db=db, store=Store(db), viewer=viewer, now=datetime.now(timezone.utc))
    return [item["id"] for item in notif.agent_questions(reading)]


def _answered_a_then_asked(client, db, objects, now_asked: dict, *, digests: bool = True) -> None:
    _ask(db, objects, [A], digests=digests)
    _answer(client, 0, "Vertex AI Claude Haiku")
    _ask(db, objects, [now_asked], digests=digests)


# -- 1. an answer to A while index 0 now asks B leaves index 0 open ---------------

def test_the_plugin_check_lists_a_task_whose_index_now_asks_another_question(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B)
    # MUTATION: count `metadata.owner_answers` by index alone and the answer
    # given to the model question hides the branch question from the check.
    assert _open_question_ids(client) == [TASK]


def test_step_needs_counts_an_index_that_now_asks_another_question_unread(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B)
    assert _questions_unread(db) == [0]


def test_the_bell_rings_for_an_index_that_now_asks_another_question(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B)
    assert _bell_ids(db) == [f"task:questions:{TASK}"]


def test_the_answer_records_the_digest_of_the_question_it_answered(client, db, objects) -> None:
    _ask(db, objects, [A])
    _answer(client, 0, "Vertex AI Claude Haiku")
    entry = db.docs[f"tasks/{TASK}"]["metadata"]["owner_answers"][0]
    assert entry["asked_digest"] == worker_questions.digest(A)


# -- 2. an answer to the same question still counts it answered --------------------

def test_the_plugin_check_leaves_out_a_task_asking_the_same_question_again(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, A)
    assert _open_question_ids(client) == []


def test_step_needs_counts_the_same_question_asked_again_answered(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, A)
    assert _questions_unread(db) == []


def test_the_bell_is_quiet_for_the_same_question_asked_again(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, A)
    assert _bell_ids(db) == []


# -- 3. a task with no digests counts by index, as it always did -------------------

def test_the_plugin_check_counts_by_index_without_digests(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B, digests=False)
    assert _open_question_ids(client) == []


def test_step_needs_counts_by_index_without_digests(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B, digests=False)
    assert _questions_unread(db) == []


def test_the_bell_counts_by_index_without_digests(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B, digests=False)
    assert _bell_ids(db) == []


def test_an_answer_stored_before_the_stamp_counts_by_index_on_a_task_with_digests(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B, digests=False)
    # A later worker writes digests; the answer has no stamp to compare.
    _ask(db, objects, [B])
    assert _questions_unread(db) == []
    assert _bell_ids(db) == []
    assert _open_question_ids(client) == []


def test_digests_that_do_not_match_the_count_are_not_read(client, db, objects) -> None:
    _answered_a_then_asked(client, db, objects, B)
    db.docs[f"tasks/{TASK}"]["result_summary"]["question_digests"] = []
    # Malformed digests are no digests: counted by index.
    assert _questions_unread(db) == []


# -- the worker's digest is the read's notion of "the same question" ---------------

def test_the_workers_digest_reads_the_question_text_and_options_only() -> None:
    same = {**A, "recommended": "Vertex AI Claude Haiku", "context": "other context", "blocking": True}
    assert worker_questions.digest(same) == worker_questions.digest(A)
    relabelled = {**A, "options": [{"label": "Gemini Flash", "description": "first"},
                                   {"label": "none", "description": "second"}]}
    assert worker_questions.digest(relabelled) != worker_questions.digest(A)
    # The worker hashes what `question_digest` hashes, shortened.
    assert agent_questions.question_digest(A).startswith(worker_questions.digest(A))
