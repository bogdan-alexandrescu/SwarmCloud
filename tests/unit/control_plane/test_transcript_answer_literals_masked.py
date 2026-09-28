"""`/transcript` and `/answer` apply the TASK's masker, not a document-only one.

THE DEFECT (#229 follow-up, owner decision 2026-09-26 recorded on #229). Every
other route serving a task -- `GET /v1/tasks/{id}`, its list, `/input` -- masks
a value the task's OWN input or metadata named as secret wherever it appears,
even when no rule would ever recognise the string on its own
(`task_input.TaskMasking`, `redaction._learned_literals`). `/transcript` and
`/answer` never learned that lesson: each built a masker (or called module-level
`redact()`) from nothing but the bytes in front of it, so a value named ONLY in
the task's metadata or input, and echoed back by the agent in a transcript step
or in the result event `/answer` reads, was served in clear on the two routes
built to redact the agent's own words.

THE FIX. `agent_output.read_transcript` and `.read_answer` now fetch the task's
one masker (`task_input.masking_for`, the SAME masker `/input` uses) and hand
its LEARNED LITERALS down to `transcript.parse_window` / `_Scrubber` and to the
result event's `redact()` call, exactly as `redaction.redact_lines` already
does for `/logs` (`literals=` there is this same tuple). No second masker is
built: the rules a step's text and a tool's structured input already go
through are unchanged, and the task's literals are applied the same way
`_mask_literals` already applies them inside `JsonMasker.text()` -- after the
rules, so a literal the rules already caught is never counted twice.

WHAT IS PINNED, per field the review named:

  * a literal named only in the metadata, echoed in a transcript step's plain
    text (final window, live tail, and the opt-in raw record) is masked;
  * the same literal, echoed in `/answer`'s agent-result-event content
    (`_fill_from_event`, the primary path -- the runner-summary fallback was
    already fixed and is covered by `test_task_collected_text_masked.py`), is
    masked;
  * each masked occurrence is counted once, matching how the raw record and
    the tool input it duplicates were already counted (`test_
    transcript_masked_by_structure.py`);
  * THE CONTROL: a transcript or an answer that never echoes the literal is
    byte-for-byte what it was before this fix -- same text, zero count -- so
    merely naming a secret in the metadata does not cause the routes to mask
    something else.

MUTATIONS: stop threading `literals` into `parse_window` (the metadata
scenarios go red, the controls stay green); stop threading them into
`_fill_from_event` (the answer scenario goes red); apply the literals BEFORE
the rules instead of after, or count a literal already caught by a rule again
(the once-counted assertions go red).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)

#: Caught ONLY as a literal the metadata names: no rule matches this shape.
LITERAL = "yeti-compass-violet-90417"
#: Unrelated text that must pass through untouched everywhere LITERAL is used,
#: proving the fix is precise rather than a blunter, wider mask.
CONTROL_TEXT = "reading the deploy config now"


def base(attempt: str = "att_1") -> str:
    return f"tenants/eng/tasks/task_a/attempts/{attempt}"


def final_key(attempt: str = "att_1") -> str:
    return f"{base(attempt)}/logs/agent_stdout.log"


def live_key(attempt: str = "att_1") -> str:
    return f"{base(attempt)}/logs/live/agent_stdout.tail.log"


def _attempt(db, attempt_id: str = "att_1", *, completed: bool = True) -> None:
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": NOW, "started_at": NOW + timedelta(seconds=5),
        "completed_at": NOW + timedelta(minutes=2) if completed else None,
        "exit_code": 0 if completed else None, "checkpoints": [],
    })


def a_task(db, *, state: str = "SUCCEEDED", completed: bool = True) -> None:
    """A task whose METADATA names `LITERAL` as a credential (`deploy_secret`,
    the same `_masks_whole` convention `test_task_collected_text_masked.py`
    uses) -- the only place anything here says it is secret.
    """
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state=state, runner_profile="claude-code")
    doc.update({"metadata": {"deploy_secret": LITERAL}})
    _attempt(db, completed=completed)


def transcript(client, query: str = "", user: str = "alice"):
    return client.get(f"/v1/tasks/task_a/transcript{query}", headers=auth_header(user))


def answer(client, query: str = "", user: str = "alice"):
    return client.get(f"/v1/tasks/task_a/answer{query}", headers=auth_header(user))


# --------------------------------------------------------------------------
# /transcript
# --------------------------------------------------------------------------

def test_a_metadata_literal_echoed_in_the_final_transcript_is_masked(client, db, objects):
    a_task(db)
    event = {
        "type": "assistant", "uuid": "u-1",
        "message": {"content": [
            {"type": "text", "text": f"the value is {LITERAL}. {CONTROL_TEXT}"},
        ]},
    }
    objects.put(final_key(), json.dumps(event) + "\n")

    body = transcript(client).json()
    assert LITERAL not in json.dumps(body), "the metadata's literal was served in clear"
    (step,) = body["steps"]
    assert step["text"] == f"the value is {MASK}. {CONTROL_TEXT}"
    assert body["redaction_count"] == 1


def test_a_metadata_literal_echoed_in_the_live_transcript_is_masked(client, db, objects):
    a_task(db, state="RUNNING", completed=False)
    event = {
        "type": "assistant", "uuid": "u-2",
        "message": {"content": [{"type": "text", "text": f"leaked: {LITERAL}"}]},
    }
    objects.put(live_key(), "#swarm-tail offset=0 size=0\n" + json.dumps(event) + "\n")

    body = transcript(client).json()
    assert body["stream"]["source"] == "live"
    assert LITERAL not in json.dumps(body)
    (step,) = body["steps"]
    assert step["text"] == f"leaked: {MASK}"
    assert body["redaction_count"] == 1


def test_a_metadata_literal_echoed_in_the_raw_record_is_masked_once_per_representation(
    client, db, objects
):
    a_task(db)
    event = {
        "type": "assistant", "uuid": "u-3",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash",
             "input": {"command": f"echo {LITERAL}"}},
        ]},
    }
    objects.put(final_key(), json.dumps(event) + "\n")

    plain = transcript(client).json()
    body = transcript(client, "?include_raw=true").json()
    assert LITERAL not in json.dumps(body)
    (step,) = body["steps"]
    assert json.loads(step["tool"]["input"]) == {"command": f"echo {MASK}"}
    record = json.loads(step["raw"])
    assert record["message"]["content"][0]["input"] == {"command": f"echo {MASK}"}
    # One mask in the input, one in the raw record it duplicates -- each
    # counted once, as `test_transcript_masked_by_structure.py` already pins
    # for a rule-caught value.
    assert body["redaction_count"] == plain["redaction_count"] + 1 == 2


def test_the_control_a_transcript_with_no_literal_present_is_unchanged(client, db, objects):
    a_task(db)
    event = {
        "type": "assistant", "uuid": "u-4",
        "message": {"content": [{"type": "text", "text": CONTROL_TEXT}]},
    }
    objects.put(final_key(), json.dumps(event) + "\n")

    body = transcript(client).json()
    (step,) = body["steps"]
    assert step["text"] == CONTROL_TEXT, "naming a secret in metadata must not mask unrelated text"
    assert body["redaction_count"] == 0


# --------------------------------------------------------------------------
# /answer
# --------------------------------------------------------------------------

def test_a_metadata_literal_echoed_in_the_answers_result_event_is_masked(client, db, objects):
    result = {"type": "result", "is_error": False, "result": f"done: {LITERAL}. {CONTROL_TEXT}"}
    objects.put(final_key(), json.dumps(result) + "\n")
    a_task(db)

    body = answer(client).json()
    assert body["status"] == "ok"
    assert body["source"] == "agent_result_event"
    assert LITERAL not in json.dumps(body)
    assert body["content"] == f"done: {MASK}. {CONTROL_TEXT}"
    assert body["redacted"] is True
    assert body["redaction_count"] == 1


def test_the_control_an_answer_with_no_literal_present_is_unchanged(client, db, objects):
    result = {"type": "result", "is_error": False, "result": CONTROL_TEXT}
    objects.put(final_key(), json.dumps(result) + "\n")
    a_task(db)

    body = answer(client).json()
    assert body["content"] == CONTROL_TEXT, "naming a secret in metadata must not mask unrelated text"
    assert body["redacted"] is False
    assert body["redaction_count"] == 0
