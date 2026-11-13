"""A questions.json the worker rejected is never served as "asked no questions" (#3086, step 3).

A build step whose `questions.json` the worker's validator rejected is
summarised `questions: 0` with `questions_rejected: <reason>`
(`agent_worker.questions.summarise`). `GET /v1/tasks/{id}/questions` read
that as `status: "none"`, and `POST /v1/tasks/{id}/answers` answered 422
"this task's agent asked no questions": the owner was told there was nothing
to answer while the file sat in the task's artifacts. These tests hold:

  1. A REJECTED FILE IS UNAVAILABLE, WITH ITS REASON -- the read serves
     `status: "unavailable"`, `unavailable_because` naming the worker's
     reason, and `rejected: {file, reason}`; the answer route answers 409
     naming questions.json and the reason, never "asked no questions";
  2. THE REASON IS REDACTED as every other served text is;
  3. NO FILE AT ALL IS STILL NONE -- `questions: 0` alone reads `none` and
     the answer route still 422s;
  4. A REGISTERED BUILD-STEP QUESTION IS ANSWERABLE -- one the worker took
     although its raw artifact carries a key outside the shape is served
     without that key, and its answer wakes the merge holding on it;
  5. THE TENANT CHECK COMES FIRST -- another tenant's rejected task is a 404
     that says nothing of the reason.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-example-project"
OWNER = "alice@example.com"
WORKFLOW = "wf_integrate"
IMPLEMENT = "task_implement"
MERGE = "task_merge"
REASON = "question 1: a key other than question, options, recommended, context, blocking, built: 'severity'"


def _key(task: str, name: str = "questions.json") -> str:
    return f"tenants/eng/tasks/{task}/attempts/att_1/artifacts/{name}"


def _questions(**extra) -> list[dict]:
    return [{
        "question": "Keep the old flag?",
        "options": [
            {"label": "keep", "description": "one more release", **extra},
            {"label": "drop", "description": "one flag"},
        ],
        "recommended": "drop",
        "context": "the build step found both",
        **extra,
    }]


def _finished(db, objects, *, summary: dict, files: dict[str, bytes], task: str = IMPLEMENT,
              tenant: str = "eng", workflow: str | None = WORKFLOW) -> dict:
    seed_tenant(db, tenant)
    doc = seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED", workflow_id=workflow)
    doc["submitted_by"] = OWNER
    entries = []
    for name, raw in files.items():
        key = _key(task, name).replace("tenants/eng/", f"tenants/{tenant}/")
        objects.put(key, raw)
        entries.append({"name": name, "bytes": len(raw), "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
        **summary,
    }
    doc["completed_at"] = datetime.now(timezone.utc)
    return doc


def _rejected(db, objects, reason: str = REASON, **kwargs) -> dict:
    raw = json.dumps(_questions(severity="high")).encode()
    return _finished(db, objects, files={"questions.json": raw},
                     summary={"questions": 0, "questions_rejected": reason}, **kwargs)


def _get(client, task: str = IMPLEMENT, user: str = "alice"):
    return client.get(f"/v1/tasks/{task}/questions", headers=auth_header(user))


def _answer(client, task: str = IMPLEMENT, index: int = 0, choice: str = "drop", user: str = "alice"):
    return client.post(f"/v1/tasks/{task}/answers",
                       json={"question_index": index, "choice": choice},
                       headers=auth_header(user))


def _merge_parked_on(db, held: list[tuple[str, int]]) -> dict:
    """The merge's owner-question park, as the worker writes it."""
    moment = datetime.now(timezone.utc)
    doc = seed_task(db, task_id=MERGE, tenant_id="eng", state="PARKED",
                    park_reason="HUMAN_REQUIRED", next_eligible_at=moment,
                    workflow_id=WORKFLOW)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = 1
    doc["blocked_by"] = [{
        "reason": "HUMAN_REQUIRED",
        "park_phase": "owner_questions",
        "owner_questions": {
            "code": "owner_questions_open",
            "questions": [{"task_id": task, "step_id": task.removeprefix("task_"),
                           "question_index": index, "question": "q"} for task, index in held],
            "unreadable": 0,
        },
        "expires_at": moment.isoformat(),
    }]
    return doc


def test_a_rejected_file_reads_unavailable_with_the_workers_reason(client, db, objects) -> None:
    _rejected(db, objects)

    response = _get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: keep reading `questions: 0` as none and the owner is told
    # the agent asked nothing while its file sits in the artifacts.
    assert body["status"] == "unavailable"
    assert body["questions"] == [] and body["count"] == 0
    assert body["unavailable_because"] == (
        f"the worker did not take questions.json as questions: {REASON}; "
        "it is in the task's artifacts"
    )
    assert body["rejected"] == {"file": "questions.json", "reason": REASON}


def test_an_answer_to_a_rejected_file_is_a_conflict_naming_it(client, db, objects) -> None:
    _rejected(db, objects)

    response = _answer(client)

    # MUTATION: leave the 422 and the owner is told there is nothing to answer.
    assert response.status_code == 409, response.text
    message = response.json()["message"]
    assert "asked no questions" not in message
    assert "questions.json" in message and REASON in message
    assert "metadata" not in db.docs[f"tasks/{IMPLEMENT}"] or not (
        db.docs[f"tasks/{IMPLEMENT}"]["metadata"] or {}).get("owner_answers")


def test_the_rejection_reason_is_redacted(client, db, objects) -> None:
    secret = "ya" + "29." + "A0" + "q" * 40
    _rejected(db, objects, reason=f"question 1: not JSON near 'Authorization: Bearer {secret}'")

    read = _get(client)
    answered = _answer(client)

    assert read.status_code == 200 and answered.status_code == 409
    # MUTATION: serve the worker's reason as it came and a token the agent
    # wrote into the file is shown to everyone who opens the task.
    assert secret not in read.text and secret not in answered.text
    assert read.json()["status"] == "unavailable"


def test_a_rejected_file_not_uploaded_does_not_point_at_the_artifacts(client, db, objects) -> None:
    _finished(db, objects, files={"synthesis.md": b"# done\n"},
              summary={"questions": 0, "questions_rejected": "written but not uploaded"})

    body = _get(client).json()

    assert body["status"] == "unavailable"
    assert body["rejected"] == {"file": "questions.json", "reason": "written but not uploaded"}
    assert "it is not in the task's artifacts" in body["unavailable_because"]


def test_no_file_at_all_still_reads_none_and_answers_422(client, db, objects) -> None:
    _finished(db, objects, files={"synthesis.md": b"# done\n"}, summary={"questions": 0})

    body = _get(client).json()
    response = _answer(client)

    # MUTATION: read every count of 0 as unavailable and a task that asked
    # nothing shows a broken questions card.
    assert body["status"] == "none" and "rejected" not in body
    assert response.status_code == 422, response.text
    assert "asked no questions" in response.json()["message"]


def test_another_tenants_rejected_file_is_a_404_saying_nothing_of_it(client, db, objects) -> None:
    """`bob` is in research; the task is eng's."""
    _rejected(db, objects)

    read = _get(client, user="bob")
    answered = _answer(client, user="bob")

    assert read.status_code == 404 and answered.status_code == 404
    assert REASON not in read.text and REASON not in answered.text


def test_a_registered_build_step_question_with_an_extra_key_is_answerable_and_wakes_the_merge(
        client, db, objects) -> None:
    """The worker dropped `severity` when it validated; the raw artifact still holds it."""
    raw = json.dumps(_questions(severity="high")).encode()
    _finished(db, objects, files={"questions.json": raw}, summary={"questions": 1})
    _merge_parked_on(db, [(IMPLEMENT, 0)])

    body = _get(client).json()

    assert body["status"] == "ok" and body["count"] == 1
    assert set(body["questions"][0]) == {"question", "options", "recommended", "context", "answer"}
    assert set(body["questions"][0]["options"][0]) == {"label", "description"}
    assert "severity" not in json.dumps(body)

    response = _answer(client)

    assert response.status_code == 200, response.text
    merge = db.docs[f"tasks/{MERGE}"]
    # The merge held on the build step's question goes ahead once it is answered.
    assert merge["state"] == "READY"
    assert merge["park_reason"] is None and merge["blocked_by"] == []
    # Invariant 1: an answer leases nothing.
    assert merge["current_lease_id"] is None
    stored = db.docs[f"tasks/{IMPLEMENT}"]["metadata"]["owner_answers"]
    assert [a["question_index"] for a in stored] == [0]
