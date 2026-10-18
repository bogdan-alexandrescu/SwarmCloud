"""`GET /v1/tasks/{id}/questions` -- what a task's agent asked its owner.

THE REPORT (owner, 2026-10-13, part of #1206): the bell said "Your agent asked
1 question" and nothing on the task said what it was. Nothing in swarm-api
served the questions; only the MCP bridge read `questions.json`. These tests
hold the route to:

  1. THE TENANT BOUNDARY -- another tenant's task is the 404 a missing one is,
     with nothing of the questions in it;
  2. ONLY WHAT THE WORKER VALIDATED -- served only when `result_summary.questions`
     counts it, and only the four keys of a question and two of an option;
  3. REDACTION -- the artifact read's, so a credential an agent quoted in a
     question's context is masked on the way out;
  4. `[]` IS NOT A 404 -- a task that asked none answers `status: "none"`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"


def _key(name: str, *, tenant: str, task: str, attempt: str = "att_1") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}/artifacts/{name}"


def _questions(**extra) -> list[dict]:
    return [
        {
            "question": "Per task or per workflow?",
            "options": [
                {"label": "per task", "description": "one file per task"},
                {"label": "per workflow", "description": "one file for the run"},
            ],
            "recommended": "per task",
            "context": "the step writes one file today",
            **extra,
        }
    ]


def _finished(db, objects, *, files: dict[str, bytes], summary: dict, tenant="eng", task="task_q"):
    seed_tenant(db, tenant)
    seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED")
    entries = []
    for name, raw in files.items():
        objects.put(_key(name, tenant=tenant, task=task), raw)
        entries.append({
            "name": name,
            "bytes": len(raw),
            "uri": f"gs://{BUCKET}/{_key(name, tenant=tenant, task=task)}",
        })
    db.docs[f"tasks/{task}"]["result_summary"] = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
        **summary,
    }
    db.docs[f"tasks/{task}"]["completed_at"] = datetime.now(timezone.utc)


def _get(client, task="task_q", user="alice"):
    return client.get(f"/v1/tasks/{task}/questions", headers=auth_header(user))


def test_the_validated_questions_are_served_with_their_options_and_recommendation(
    client, db, objects
) -> None:
    raw = json.dumps(_questions(), indent=2).encode()
    _finished(db, objects, files={"questions.json": raw}, summary={"questions": 1})

    response = _get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["count"] == 1
    assert body["attempt_id"] == "att_1"
    # Each question also carries the owner's answer: none yet (TASK-ANSWERS).
    assert body["questions"] == [{**q, "answer": None} for q in _questions()]
    assert body["redacted"] is False


def test_a_credential_in_a_question_is_redacted(client, db, objects) -> None:
    """The artifact read's masking, applied to the file before it is parsed."""
    secret = "ya" + "29." + "A0" + "q" * 40
    raw = json.dumps(_questions(context=f"Authorization: Bearer {secret}")).encode()
    _finished(db, objects, files={"questions.json": raw}, summary={"questions": 1})

    response = _get(client)

    assert response.status_code == 200, response.text
    assert secret not in response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["redacted"] is True
    assert body["redaction_count"] >= 1
    assert "********" in body["questions"][0]["context"]
    # The rest of the question is served as written.
    assert body["questions"][0]["question"] == "Per task or per workflow?"


def test_a_task_that_asked_none_answers_an_empty_list_not_a_404(client, db, objects) -> None:
    _finished(db, objects, files={"synthesis.md": b"# done\n"}, summary={"questions": 0})

    response = _get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "none"
    assert body["questions"] == []
    assert body["count"] == 0


def test_a_file_the_worker_rejected_is_not_served_as_questions(client, db, objects) -> None:
    """The worker's count is the validation verdict: 0 with a reason is none."""
    raw = json.dumps(_questions()).encode()
    _finished(
        db, objects, files={"questions.json": raw},
        summary={"questions": 0, "questions_rejected": "question 1: a key other than ..."},
    )

    body = _get(client).json()

    assert body["status"] == "none"
    assert body["questions"] == []


def test_a_counted_file_that_disagrees_with_the_count_is_unavailable(client, db, objects) -> None:
    raw = json.dumps(_questions()).encode()
    _finished(db, objects, files={"questions.json": raw}, summary={"questions": 2})

    body = _get(client).json()

    assert body["status"] == "unavailable"
    assert body["questions"] == []
    assert "2" in body["unavailable_because"]


def test_a_counted_file_missing_from_the_manifest_is_unavailable_not_a_404(
    client, db, objects
) -> None:
    _finished(db, objects, files={"synthesis.md": b"# done\n"}, summary={"questions": 1})

    response = _get(client)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unavailable"


def test_keys_beyond_the_shape_are_not_served(client, db, objects) -> None:
    """Only the four keys of a question and two of an option leave the API."""
    entry = _questions()[0]
    entry["extra"] = "not part of the shape"
    entry["options"][0]["extra"] = "nor this"
    _finished(db, objects, files={"questions.json": json.dumps([entry]).encode()},
              summary={"questions": 1})

    body = _get(client).json()

    assert body["status"] == "ok"
    # The four keys, and the owner's `answer` beside them (TASK-ANSWERS).
    assert set(body["questions"][0]) == {"question", "options", "recommended", "context", "answer"}
    assert set(body["questions"][0]["options"][0]) == {"label", "description"}
    assert "not part of the shape" not in json.dumps(body)


def test_another_tenants_questions_are_a_404(client, db, objects) -> None:
    """`bob` is in research; the task and its questions are eng's."""
    raw = json.dumps(_questions()).encode()
    _finished(db, objects, files={"questions.json": raw}, summary={"questions": 1})

    response = _get(client, user="bob")

    assert response.status_code == 404, response.text
    assert "task_q" in response.json()["message"]
    assert "Per task or per workflow" not in response.text
    assert BUCKET not in response.text
