"""An answer to a question a merge went past starts a pick-fix round against main.

Owner decision, 2 November 2026: advisory questions stop holding merges. A
merge step goes ahead past an unanswered non-blocking question with a
recommended option its branch built (`agent_worker.merge`), records it as
`result_summary.advisory_merged_past`, and the question stays open. These
tests hold `POST /v1/tasks/{id}/answers` to:

  1. A DIFFERENT PICK STARTS A ROUND -- one pick-fix lane from the default
     branch, submitted as the answering member, claimed on the merge's
     `metadata.advisory_pick_fix` with its workflow, and merged by its own
     review and merge steps (owner pick, 2 November 2026: `integrate`,
     pick-fix -> review -> fix, `metadata.merge` "on_merge_verdict");
  2. THE SAME ANSWER STARTS NOTHING -- the recommended option, or the same
     different pick answered again;
  3. A MERGE THAT DID NOT GO PAST THE QUESTION STARTS NOTHING;
  4. A REFUSED SUBMISSION IS RECORDED, and the answer still stands.

Nothing is leased by an answer (invariant 1). No credentials, no network,
no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"
OWNER = "alice@example.com"
WORKFLOW = "wf_lane"
MERGE = "task_merge"
IMPLEMENT = "task_implement"
REPO = "https://github.com/acme/widgets"
PER_TASK = "per task"
PER_WORKFLOW = "per workflow"


def _key(tenant: str, task: str) -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/att_1/artifacts/questions.json"


def _asked(db, objects) -> dict:
    questions = [{
        "question": "Count the retries per task or per workflow?",
        "options": [{"label": PER_TASK, "description": "each task"},
                    {"label": PER_WORKFLOW, "description": "the lane"}],
        "recommended": PER_TASK,
        "context": None,
        "built": PER_TASK,
    }]
    doc = seed_task(db, task_id=IMPLEMENT, tenant_id="eng", state="SUCCEEDED",
                    workflow_id=WORKFLOW)
    doc["submitted_by"] = OWNER
    raw = json.dumps(questions).encode()
    objects.put(_key("eng", IMPLEMENT), raw)
    doc["result_summary"] = {
        "artifacts": [{"name": "questions.json", "bytes": len(raw),
                       "uri": f"gs://{BUCKET}/{_key('eng', IMPLEMENT)}"}],
        "artifact_bytes": len(raw),
        "logs": {},
        "questions": 1,
    }
    doc["completed_at"] = datetime.now(timezone.utc)
    return doc


def _merged(db, *, past: bool = True) -> dict:
    doc = seed_task(db, task_id=MERGE, tenant_id="eng", state="SUCCEEDED",
                    runner_profile="merge", workflow_id=WORKFLOW)
    doc["submitted_by"] = OWNER
    doc["repository_url"] = REPO
    doc["result_summary"] = {"action": "merge", "pull_request": 41, "merged": True}
    if past:
        doc["result_summary"]["advisory_merged_past"] = [{
            "task_id": IMPLEMENT, "step_id": "implement", "question_index": 0,
            "question": "Count the retries per task or per workflow?",
            "recommended": PER_TASK, "built": PER_TASK}]
    return doc


class RecordingSubmissions:
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[tuple] = []
        self.refuse = refuse

    def submit_workflow(self, auth, spec):
        self.calls.append((auth, spec))
        if self.refuse:
            raise ValueError("the repository is not registered")
        n = len(self.calls)
        return SimpleNamespace(workflow=SimpleNamespace(
            workflow_id=f"wf_pick{n}",
            steps=[SimpleNamespace(task_id=f"task_pick{n}")]))


@pytest.fixture
def submissions(api_context, monkeypatch) -> RecordingSubmissions:
    """Only the submission is recorded: the route's tenant scope is the real one."""
    recording = RecordingSubmissions()
    monkeypatch.setattr(api_context.submissions, "submit_workflow", recording.submit_workflow)
    return recording


def _answer(client, choice: str):
    return client.post(f"/v1/tasks/{IMPLEMENT}/answers",
                       json={"question_index": 0, "choice": choice},
                       headers=auth_header("alice"))


def _claims(db) -> list[dict]:
    return db.docs[f"tasks/{MERGE}"].get("metadata", {}).get("advisory_pick_fix", [])


def test_a_later_different_pick_starts_a_pick_fix_round_against_main(
        client, db, objects, submissions) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects)
    _merged(db)

    response = _answer(client, PER_WORKFLOW)

    assert response.status_code == 200, response.text
    # MUTATION: leave the trigger out of the answer route and nothing is submitted.
    ((auth, spec),) = submissions.calls
    assert auth.email == OWNER
    assert spec.strategy == "integrate"
    # Against the default branch: the pull request is merged, nothing to continue.
    assert spec.continues_task is None
    assert spec.repository_url == REPO
    step, review, fix = spec.steps
    assert (step.step_id, review.step_id, fix.step_id) == ("pick-fix", "review", "fix")
    prompt = step.input["prompt"]
    assert f"pick {IMPLEMENT} question 0" in prompt and repr(PER_WORKFLOW) in prompt
    assert "pull request #41" in prompt and "picks-built.json" in prompt
    # MUTATION: leave the round's merge "off" and it waits for a person.
    assert spec.metadata["merge"] == "on_merge_verdict"
    assert fix.when.step == "review" and fix.when.verdict_in == ["NOT_YET"]
    assert spec.metadata["advisory_pick_fix_round"]["merge_task"] == MERGE
    (claim,) = _claims(db)
    assert (claim["task_id"], claim["question_index"], claim["picked"], claim["built"]) == (
        IMPLEMENT, 0, PER_WORKFLOW, PER_TASK)
    assert (claim["round_workflow_id"], claim["round_task_id"]) == ("wf_pick1", "task_pick1")
    # The merge stays as it ended; nothing is leased (invariant 1).
    merge = db.docs[f"tasks/{MERGE}"]
    assert merge["state"] == "SUCCEEDED" and merge["current_lease_id"] is None
    # The owner's answer is theirs, stored as given.
    answers = db.docs[f"tasks/{IMPLEMENT}"]["metadata"]["owner_answers"]
    assert [a["choice"] for a in answers] == [PER_WORKFLOW]


def test_the_recommended_answer_starts_nothing(client, db, objects, submissions) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects)
    _merged(db)

    assert _answer(client, PER_TASK).status_code == 200

    # MUTATION: start a round on any answer and this submits one.
    assert submissions.calls == []
    assert _claims(db) == []


def test_the_same_different_pick_answered_again_starts_nothing_more(
        client, db, objects, submissions) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects)
    _merged(db)

    assert _answer(client, PER_WORKFLOW).status_code == 200
    assert _answer(client, PER_WORKFLOW).status_code == 200

    # MUTATION: claim without reading the merge's record and this submits twice.
    assert len(submissions.calls) == 1
    assert len(_claims(db)) == 1


def test_a_merge_that_did_not_go_past_the_question_starts_nothing(
        client, db, objects, submissions) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects)
    _merged(db, past=False)

    assert _answer(client, PER_WORKFLOW).status_code == 200

    assert submissions.calls == []
    assert _claims(db) == []


def test_a_refused_round_is_recorded_and_the_answer_stands(client, db, objects,
                                                           submissions) -> None:
    submissions.refuse = True
    seed_tenant(db, "eng")
    _asked(db, objects)
    _merged(db)

    response = _answer(client, PER_WORKFLOW)

    assert response.status_code == 200, response.text
    (claim,) = _claims(db)
    assert "not registered" in claim["error"]
    answers = db.docs[f"tasks/{IMPLEMENT}"]["metadata"]["owner_answers"]
    assert [a["choice"] for a in answers] == [PER_WORKFLOW]


def test_the_round_is_merged_by_its_own_review_and_merge_steps(client, db) -> None:
    """The owner's pick: submitted for real, the round gains a re-review and a merge."""
    from swarm_api.mergewake import advisory_pick_fix_round

    seed_tenant(db, "eng")
    spec = advisory_pick_fix_round(
        repository_url=REPO,
        merge={"id": MERGE, "workflow_id": WORKFLOW,
               "result_summary": {"pull_request": 41}},
        claim={"task_id": IMPLEMENT, "question_index": 0, "question": "Where?",
               "picked": PER_WORKFLOW, "built": PER_TASK})

    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=spec.model_dump(mode="json", exclude_none=True))

    assert response.status_code == 201, response.text
    steps = {s["step_id"]: s for s in response.json()["workflow"]["steps"]}
    # MUTATION: submit the round with merge "off" and no merge step is appended.
    assert list(steps) == ["pick-fix", "review", "fix", "re-review", "merge"]
    assert steps["merge"]["runner_profile"] == "merge"
    assert steps["merge"]["input_from"] == {"re-review": "verdict.json"}
