"""A caller cannot plant an agent's question wait at submission (lane AU7-WAITS, part of #1933).

`metadata.question_wait` and `metadata.question_waits_earlier` are written
only by swarm-api, while the task is parked on a blocking question
(`agent_questions.adopt_wait`), and the worker hands the first to the resumed
agent. They are not inside the step-spec signature (written after
submission by design), so the refusal at submission is the only thing that
proves swarm-api wrote them. A caller who could set them would hand the
resumed agent a wait, and answers, nobody asked or gave.

These tests hold swarm-api to:

  1. REFUSED LIKE EVERY RESERVED KEY -- the same 422 `invalid_dispatch`,
     the key named in `detail.reserved_metadata_keys`, nothing created;
  2. STILL WRITTEN BY swarm-api -- an answer to a parked wait adopts it
     into `metadata` and keeps the earlier one, as before;
  3. STILL MASKED WHEN SERVED -- the wait's text is the agent's, stored
     unredacted, so it is not served raw as a platform key is.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest

from swarm_api import agent_questions
from swarm_api.errors import ValidationFailed
from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY, reject_reserved_metadata

from .conftest import auth_header, seed_task, seed_tenant

TASK = "task_asked"
OWNER = "alice@example.com"
KEYS = ("question_wait", "question_waits_earlier")


def _wait(attempt: str, *, context: str | None = None) -> dict:
    return {
        "questions": [{
            "question": "Keep the old route?",
            "options": [{"label": "keep", "description": "a release"},
                        {"label": "drop", "description": "one route"}],
            "recommended": "keep",
            "context": context,
            "blocking": True,
        }],
        "blocking": [0],
        "attempt_id": attempt,
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }


# -- 1. refused at submission ---------------------------------------------------------

@pytest.mark.parametrize("key", KEYS)
def test_a_submission_carrying_a_question_wait_key_is_refused_and_creates_nothing(
    client, db, key
) -> None:
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x"},
              "metadata": {"unit": "payments", key: _wait("att_forged")}},
    )

    # MUTATION: drop `key` from the refused keys in `reject_reserved_metadata`
    # and the forged wait is stored for the resumed agent to act on.
    assert db.paths("tasks/") == []
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["reserved_metadata_keys"] == [key]
    assert f"metadata.{key} is reserved" in body["message"]


@pytest.mark.parametrize("key", KEYS)
def test_the_refusal_is_the_one_owner_answers_gets(key) -> None:
    with pytest.raises(ValidationFailed) as ours:
        reject_reserved_metadata({key: {}})
    with pytest.raises(ValidationFailed) as theirs:
        reject_reserved_metadata({OWNER_ANSWERS_METADATA_KEY: []})
    assert type(ours.value) is type(theirs.value)
    assert ours.value.code == theirs.value.code
    assert ours.value.detail == {"reserved_metadata_keys": [key]}


def test_every_reserved_key_a_caller_sent_is_named_in_one_refusal() -> None:
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({"question_waits_earlier": [], "owner_answers": [],
                                  "question_wait": {}})
    assert exc.value.detail == {"reserved_metadata_keys": [
        "owner_answers", "question_wait", "question_waits_earlier"]}


def test_swarm_api_and_the_refusal_name_the_same_keys() -> None:
    assert (agent_questions.QUESTION_WAIT_KEY, agent_questions.EARLIER_WAITS_KEY) == KEYS


# -- 2. swarm-api's own park path still writes them ------------------------------------

def test_an_answer_to_a_parked_wait_still_writes_both_keys(client, db) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=datetime.now(timezone.utc))
    doc.update({"submitted_by": OWNER, "attempt_count": 1,
                "metadata": {"question_wait": _wait("att_1")},
                "blocked_by": [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait",
                                "question_wait": _wait("att_2")}]})

    response = client.post(f"/v1/tasks/{TASK}/answers", json={"question_index": 0, "choice": "keep"},
                           headers=auth_header("alice"))

    # MUTATION: run `reject_reserved_metadata` over the park path's writes and
    # the answer is refused; the reservation is at submission only.
    assert response.status_code == 200, response.text
    metadata = db.docs[f"tasks/{TASK}"]["metadata"]
    assert metadata["question_wait"]["attempt_id"] == "att_2"
    assert metadata["question_waits_earlier"][0]["wait"]["attempt_id"] == "att_1"


# -- 3. served masked -----------------------------------------------------------------

def test_a_stored_wait_is_not_served_raw_with_the_task(client, db) -> None:
    seed_tenant(db, "eng")
    leaked = "ghp_" + secrets.token_hex(18)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="READY")
    doc.update({"submitted_by": OWNER,
                "metadata": {"question_wait": _wait("att_1", context=f"token {leaked} in the log"),
                             "question_waits_earlier": [
                                 {"wait": _wait("att_0", context=f"again {leaked}"), "answers": []}]}})

    response = client.get(f"/v1/tasks/{TASK}", headers=auth_header("alice"))

    # MUTATION: put the two keys in `RESERVED_METADATA_KEYS`, which
    # `task_input.PLATFORM_METADATA_KEYS` serves as stored, and the agent's
    # unredacted question text is on the page.
    assert response.status_code == 200, response.text
    assert leaked not in response.text
