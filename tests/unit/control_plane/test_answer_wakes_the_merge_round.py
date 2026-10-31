"""An answer on a merge's fix round wakes the merge it held (#2913, part of #2898).

A merge step parks PARKED(HUMAN_REQUIRED) on `owner_questions_open` when one
of its CI-fix, conflict-fix or pick-fix rounds ended with an unanswered
blocking question (`agent_worker.merge._round_questions`). A round runs as a
workflow of its own, so its task carries its own `workflow_id`: the answer
route found a held merge only in the answered task's workflow and never
woke it. These tests hold `POST /v1/tasks/{id}/answers` to:

  1. A ROUND'S LAST ANSWER WAKES IT -- a merge held on a `ci-fix:1` round's
     question goes READY when that question is answered;
  2. ANOTHER OPEN QUESTION HOLDS IT -- a step's or another round's;
  3. AN IMPLEMENT STEP'S ANSWER BEHAVES AS BEFORE, beside a round's;
  4. ONLY THE MERGE'S OWN ROUND COUNTS -- a round that names another merge,
     a round the merge's `merge_fix` record does not list, or a merge of
     another tenant is never woken by it.

Nothing is leased by an answer (invariant 1). No credentials, no network,
no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"
OWNER = "alice@example.com"
WORKFLOW = "wf_lane"
MERGE = "task_merge"
IMPLEMENT = "task_implement"
ROUND = "task_round1"
ROUND_WORKFLOW = "wf_round1"
ROUND2 = "task_round2"
ROUND2_WORKFLOW = "wf_round2"


def _key(tenant: str, task: str) -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/att_1/artifacts/questions.json"


def _questions() -> list[dict]:
    return [
        {
            "question": "Exempt the flaky check?",
            "options": [
                {"label": "exempt", "description": "skip it"},
                {"label": "forbid", "description": "fix it"},
            ],
            "recommended": "forbid",
            "context": None,
        },
        {
            "question": "Keep the old flag?",
            "options": [
                {"label": "keep", "description": "one more release"},
                {"label": "drop", "description": "one flag"},
            ],
            "recommended": "drop",
            "context": None,
        },
    ]


def _asked(db, objects, task: str, *, workflow: str, tenant: str = "eng",
           metadata: dict | None = None) -> dict:
    doc = seed_task(db, task_id=task, tenant_id=tenant, state="SUCCEEDED", workflow_id=workflow)
    doc["submitted_by"] = OWNER
    raw = json.dumps(_questions()).encode()
    objects.put(_key(tenant, task), raw)
    doc["result_summary"] = {
        "artifacts": [{"name": "questions.json", "bytes": len(raw),
                       "uri": f"gs://{BUCKET}/{_key(tenant, task)}"}],
        "artifact_bytes": len(raw),
        "logs": {},
        "questions": 2,
    }
    doc["completed_at"] = datetime.now(timezone.utc)
    if metadata is not None:
        doc["metadata"] = metadata
    return doc


def _round(db, objects, task: str, workflow: str, *, merge: str = MERGE, round_no: int = 1,
           key: str = "merge_fix_round", tenant: str = "eng") -> dict:
    """A fix round's task, in a workflow of its own, as mergewake submits it."""
    return _asked(db, objects, task, workflow=workflow, tenant=tenant, metadata={
        key: {"merge_task": merge, "round": round_no, "head_sha": "a" * 40,
              "pull_request": 7},
    })


def _merge_parked_on(db, held: list[tuple[str, str, int]], *, tenant: str = "eng",
                     task_id: str = MERGE, rounds: dict | None = None) -> dict:
    """The merge's owner-question park, as the worker writes it, and its `merge_fix`."""
    moment = datetime.now(timezone.utc)
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state="PARKED",
                    park_reason="HUMAN_REQUIRED", next_eligible_at=moment,
                    workflow_id=WORKFLOW)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = 1
    doc["metadata"] = {"merge_fix": rounds if rounds is not None else {}}
    doc["blocked_by"] = [{
        "reason": "HUMAN_REQUIRED",
        "park_phase": "owner_questions",
        "owner_questions": {
            "code": "owner_questions_open",
            "questions": [{"task_id": task, "step_id": step, "question_index": index,
                           "question": "q"} for task, step, index in held],
            "unreadable": 0,
        },
        "expires_at": moment.isoformat(),
    }]
    return doc


def _entry(round_no: int, task: str, workflow: str) -> dict:
    return {"round": round_no, "claimed_at": "2026-10-31T08:00:00+00:00",
            "task_id": task, "workflow_id": workflow}


def _answer(client, task: str, index: int, choice: str):
    return client.post(f"/v1/tasks/{task}/answers",
                       json={"question_index": index, "choice": choice},
                       headers=auth_header("alice"))


def _doc(db, task: str = MERGE) -> dict:
    return db.docs[f"tasks/{task}"]


def _events(db, task: str = MERGE) -> list[dict]:
    prefix = f"tasks/{task}/events/"
    return [doc for path, doc in db.docs.items() if path.startswith(prefix)]


def _assert_held(db, task: str = MERGE) -> None:
    merge = _doc(db, task)
    assert merge["state"] == "PARKED" and merge["park_reason"] == "HUMAN_REQUIRED"
    assert merge["blocked_by"][0]["park_phase"] == "owner_questions"
    assert [e for e in _events(db, task) if e["type"] == "ready"] == []


def test_answering_a_ci_fix_rounds_question_returns_the_merge_to_ready(client, db, objects) -> None:
    seed_tenant(db, "eng")
    _round(db, objects, ROUND, ROUND_WORKFLOW)
    _merge_parked_on(db, [(ROUND, "ci-fix:1", 0)],
                     rounds={"rounds": [_entry(1, ROUND, ROUND_WORKFLOW)]})

    response = _answer(client, ROUND, 0, "forbid")

    assert response.status_code == 200, response.text
    merge = _doc(db)
    # MUTATION: look for held merges in the answered task's workflow alone and
    # the round's answer never finds the merge; it waits for a `human-ready`.
    assert merge["state"] == "READY"
    assert merge["park_reason"] is None and merge["blocked_by"] == []
    # Invariant 1: an answer leases nothing.
    assert merge["current_lease_id"] is None
    ready = [e for e in _events(db) if e["type"] == "ready"]
    assert len(ready) == 1 and ready[0]["detail"]["by"] == OWNER
    assert [a["question_index"] for a in _doc(db, ROUND)["metadata"]["owner_answers"]] == [0]


def test_a_conflict_rounds_answer_wakes_the_merge_too(client, db, objects) -> None:
    seed_tenant(db, "eng")
    _round(db, objects, ROUND, ROUND_WORKFLOW, key="merge_conflict_round")
    _merge_parked_on(db, [(ROUND, "conflict-fix:1", 1)],
                     rounds={"conflict_rounds": [_entry(1, ROUND, ROUND_WORKFLOW)]})

    assert _answer(client, ROUND, 1, "drop").status_code == 200

    assert _doc(db)["state"] == "READY"


def test_a_rounds_answer_leaves_the_merge_held_while_a_steps_question_is_open(
        client, db, objects) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects, IMPLEMENT, workflow=WORKFLOW)
    _round(db, objects, ROUND, ROUND_WORKFLOW)
    _merge_parked_on(db, [(IMPLEMENT, "implement", 0), (ROUND, "ci-fix:1", 0)],
                     rounds={"rounds": [_entry(1, ROUND, ROUND_WORKFLOW)]})

    assert _answer(client, ROUND, 0, "forbid").status_code == 200
    # MUTATION: count the round's questions alone and the merge merges past
    # the implement step's open question.
    _assert_held(db)

    # The step's answer, the last one open, wakes it: the round's question
    # counts as the merge's own (MUTATION: count only same-workflow tasks in
    # the clear and the step's answer never wakes it either).
    assert _answer(client, IMPLEMENT, 0, "exempt").status_code == 200
    assert _doc(db)["state"] == "READY"


def test_a_rounds_answer_leaves_the_merge_held_while_another_rounds_question_is_open(
        client, db, objects) -> None:
    seed_tenant(db, "eng")
    _round(db, objects, ROUND, ROUND_WORKFLOW)
    _round(db, objects, ROUND2, ROUND2_WORKFLOW, round_no=2)
    _merge_parked_on(db, [(ROUND, "ci-fix:1", 0), (ROUND2, "ci-fix:2", 1)],
                     rounds={"rounds": [_entry(1, ROUND, ROUND_WORKFLOW),
                                        _entry(2, ROUND2, ROUND2_WORKFLOW)]})

    assert _answer(client, ROUND, 0, "forbid").status_code == 200
    _assert_held(db)

    assert _answer(client, ROUND2, 1, "drop").status_code == 200
    assert _doc(db)["state"] == "READY"


def test_an_implement_steps_answer_wakes_the_merge_as_before(client, db, objects) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects, IMPLEMENT, workflow=WORKFLOW)
    _merge_parked_on(db, [(IMPLEMENT, "implement", 1)])

    assert _answer(client, IMPLEMENT, 1, "keep").status_code == 200

    assert _doc(db)["state"] == "READY"


def test_a_round_of_another_merge_never_clears_it(client, db, objects) -> None:
    seed_tenant(db, "eng")
    # The round names another merge, although this merge's hold and record
    # name the round: both sides must agree before it counts.
    _round(db, objects, ROUND, ROUND_WORKFLOW, merge="task_other_merge")
    before = _merge_parked_on(db, [(ROUND, "ci-fix:1", 0)],
                              rounds={"rounds": [_entry(1, ROUND, ROUND_WORKFLOW)]})
    snapshot = json.loads(json.dumps(before, default=str))

    assert _answer(client, ROUND, 0, "forbid").status_code == 200

    assert json.loads(json.dumps(_doc(db), default=str)) == snapshot
    assert _events(db) == []


def test_a_round_the_merges_record_does_not_list_never_clears_it(client, db, objects) -> None:
    seed_tenant(db, "eng")
    _round(db, objects, ROUND, ROUND_WORKFLOW)
    # The record lists another task as round 1, and none in this workflow.
    _merge_parked_on(db, [(ROUND, "ci-fix:1", 0)],
                     rounds={"rounds": [_entry(1, ROUND2, ROUND_WORKFLOW),
                                        _entry(2, ROUND, "wf_elsewhere")]})

    assert _answer(client, ROUND, 0, "forbid").status_code == 200

    # MUTATION: trust the round's `merge_task` alone (it is tenant-writable)
    # and a task that claims a merge it is not a round of wakes it.
    _assert_held(db)


def test_another_tenants_merge_is_never_woken_by_a_round(client, db, objects) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "ops")
    _round(db, objects, ROUND, ROUND_WORKFLOW)
    _merge_parked_on(db, [(ROUND, "ci-fix:1", 0)], tenant="ops",
                     rounds={"rounds": [_entry(1, ROUND, ROUND_WORKFLOW)]})

    assert _answer(client, ROUND, 0, "forbid").status_code == 200

    # MUTATION: read the round's merge by id without its tenant and eng's
    # answer wakes ops's merge.
    _assert_held(db)


def test_another_tenants_round_never_clears_the_merge(client, db, objects) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "ops")
    _asked(db, objects, IMPLEMENT, workflow=WORKFLOW)
    # ops's task names eng's merge and is listed in its record, but it is not
    # eng's: its answers answer nothing of eng's merge.
    foreign = _round(db, objects, ROUND, ROUND_WORKFLOW, tenant="ops")
    foreign["metadata"]["owner_answers"] = [{"question_index": 0, "choice": "forbid"}]
    _merge_parked_on(db, [(IMPLEMENT, "implement", 0), (ROUND, "ci-fix:1", 0)],
                     rounds={"rounds": [_entry(1, ROUND, ROUND_WORKFLOW)]})

    assert _answer(client, IMPLEMENT, 0, "exempt").status_code == 200

    _assert_held(db)


def test_the_round_keys_are_mergewakes_own() -> None:
    from swarm_api import agent_questions, mergewake

    assert agent_questions.MERGE_FIX_METADATA_KEY == mergewake.MERGE_FIX_METADATA_KEY
    assert set(agent_questions.MERGE_ROUND_LISTS) == set(mergewake.ROUND_KIND)
