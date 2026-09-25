"""`GET /v1/tasks/{id}/answer` -- the agent's final answer, first in Outputs.

#184. On the reference task (task_73b5f4d9ca3641fbb914) the answer was a
7,124-character Markdown document inside `claude-code.stdout.log`, and the
only copy the API served was `result_summary.runner.summary` -- which the
runner cuts at 2,000 characters. This route finds the LAST `result` event in
the agent's stdout (final copy, then the pre-change artifact, then the live
tail) and serves its `result`, redacted.

The four answers must stay apart: `ok` (here it is -- an agent that reported
an error is still ok, with the flag), `not_yet` (running, no result yet),
`absent` (ended with nothing), `unreadable` (a read failed, and nothing further
down is tried).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
GH_TOKEN = "ghp_" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMm"

ANSWER = ("# Findings\n\n" + "- a finding about the code base that matters\n" * 200)[:7123] + "\n"
REFERENCE = {
    "type": "result", "subtype": "success", "is_error": False, "result": ANSWER,
    "num_turns": 7, "duration_ms": 89579, "stop_reason": "end_turn",
    "terminal_reason": "completed", "total_cost_usd": 0.61, "uuid": "u-ref",
}
ASSISTANT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}


def base(attempt="att_1") -> str:
    return f"tenants/eng/tasks/task_a/attempts/{attempt}"


def final_key(attempt="att_1") -> str:
    return f"{base(attempt)}/logs/agent_stdout.log"


def live_key(attempt="att_1") -> str:
    return f"{base(attempt)}/logs/live/agent_stdout.tail.log"


def artifact_key(name, attempt="att_1") -> str:
    return f"{base(attempt)}/artifacts/{name}"


def _attempt(db, attempt_id="att_1", *, completed=True):
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": NOW, "started_at": NOW + timedelta(seconds=5),
        "completed_at": NOW + timedelta(minutes=2) if completed else None,
        "exit_code": 0 if completed else None, "checkpoints": [],
    })


def a_task(db, *, state="SUCCEEDED", runner_profile="claude-code", summary=None,
           completed=True, attempt=True):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state=state, runner_profile=runner_profile)
    # A running task holds its attempt's lease; that, not the clock, is what
    # says the attempt has not ended.
    db.docs["tasks/task_a"]["current_lease_id"] = "lease_att_1" if state == "RUNNING" else None
    if summary is not None:
        db.docs["tasks/task_a"]["result_summary"] = summary
    if attempt:
        _attempt(db, completed=completed)


def summary_of(*, artifacts: dict[str, str] | None = None, runner_summary: str | None = None,
               agent_streams=...):
    """A `result_summary` for att_1, as `_upload_outputs` and `_finalise` write it."""
    entries = [
        {"name": name, "bytes": len(body), "uri": f"gs://{BUCKET}/{artifact_key(name)}"}
        for name, body in (artifacts or {}).items()
    ]
    summary: dict = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {"stderr": f"gs://{BUCKET}/{base()}/logs/stderr.log"},
    }
    if runner_summary is not None:
        summary["runner"] = {"status": "succeeded", "summary": runner_summary}
    if agent_streams is not ...:
        summary["agent_streams"] = agent_streams
    return summary


def get(client, query="", user="alice"):
    return client.get(f"/v1/tasks/task_a/answer{query}", headers=auth_header(user))


def test_the_reference_task_answers_with_its_whole_markdown_from_the_legacy_artifact(
    client, db, objects
):
    stdout = json.dumps(REFERENCE) + "\n"
    objects.put(artifact_key("claude-code.stdout.log"), stdout)
    a_task(db, summary=summary_of(
        artifacts={"claude-code.stdout.log": stdout},
        runner_summary=ANSWER[:2000],
    ))

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["source"] == "agent_result_event"
    assert body["object"]["source"] == "artifact"
    assert body["object"]["stream"] == "agent_stdout"
    assert body["format"] == "markdown"
    assert body["content"] == ANSWER, "the whole answer, not the runner's 2,000-character cut"
    assert body["bytes"] == 7124
    assert body["complete"] is True
    assert body["is_error"] is False
    assert (body["subtype"], body["num_turns"]) == ("success", 7)
    assert (body["stop_reason"], body["terminal_reason"]) == ("end_turn", "completed")
    assert body["redacted"] is False


def test_the_last_result_event_of_a_stream_is_the_answer(client, db, objects):
    first = {"type": "result", "result": "an earlier result", "is_error": False}
    last = {"type": "result", "result": "the final answer", "is_error": False, "num_turns": 3}
    objects.put(final_key(), "\n".join(json.dumps(e) for e in [ASSISTANT, first, ASSISTANT, last]) + "\n")
    a_task(db)

    body = get(client).json()
    assert body["status"] == "ok"
    assert body["object"]["source"] == "final"
    assert body["content"] == "the final answer"
    assert body["num_turns"] == 3


def test_a_running_attempt_with_no_result_yet_is_not_yet(client, db, objects):
    objects.put(live_key(), "#swarm-tail offset=0 size=60\n" + json.dumps(ASSISTANT) + "\n")
    a_task(db, state="RUNNING", completed=False)

    body = get(client).json()
    assert body["status"] == "not_yet"
    assert body["content"] is None


def test_a_result_in_the_live_tail_is_served_before_the_upload(client, db, objects):
    result = {"type": "result", "result": "done already", "is_error": False}
    objects.put(
        live_key(),
        "#swarm-tail offset=0 size=90 at=2026-09-25T12:02:00Z\n"
        + json.dumps(ASSISTANT) + "\n" + json.dumps(result) + "\n",
    )
    a_task(db, state="RUNNING", completed=False)

    body = get(client).json()
    assert body["status"] == "ok"
    assert body["object"]["source"] == "live"
    assert body["content"] == "done already"


def test_an_agent_that_reported_an_error_is_ok_with_the_flag(client, db, objects):
    result = {"type": "result", "subtype": "error_max_turns", "is_error": True,
              "result": "I ran out of turns.", "num_turns": 30}
    objects.put(final_key(), json.dumps(result) + "\n")
    a_task(db, state="FAILED")

    body = get(client).json()
    assert body["status"] == "ok"
    assert body["is_error"] is True
    assert body["subtype"] == "error_max_turns"
    assert body["content"] == "I ran out of turns."


def test_without_a_result_event_the_runner_summary_is_served_and_marked_cut(client, db, objects):
    objects.put(final_key(), json.dumps(ASSISTANT) + "\n")
    a_task(db, summary=summary_of(
        artifacts={"output.txt": "x"}, runner_summary="S" * 2000,
    ))

    body = get(client).json()
    assert body["status"] == "ok"
    assert body["source"] == "runner_summary"
    assert body["format"] == "text"
    assert body["content"] == "S" * 2000
    assert body["complete"] is False, "at the runner's cap it may have been cut"


def test_a_short_runner_summary_is_not_claimed_complete_or_cut(client, db, objects):
    a_task(db, runner_profile="mock", summary=summary_of(
        artifacts={"output.txt": "x"},
        runner_summary="mock runner completed 2/2 steps",
        agent_streams=None,
    ))

    body = get(client).json()
    assert body["status"] == "ok"
    assert body["source"] == "runner_summary"
    assert body["content"] == "mock runner completed 2/2 steps"
    assert body["complete"] is None


def test_the_runner_summary_is_only_the_manifests_attempts(client, db, objects):
    """An earlier attempt must not be shown the final attempt's summary."""
    a_task(db, summary=summary_of(artifacts={"output.txt": "x"}, runner_summary="final attempt"))
    _attempt(db, "att_0", completed=True)

    body = get(client, "?attempt_id=att_0").json()
    assert body["status"] == "absent"
    assert body["content"] is None


def test_an_attempt_that_ended_with_nothing_is_absent(client, db, objects):
    a_task(db, state="FAILED")

    body = get(client).json()
    assert body["status"] == "absent"
    assert body["content"] is None
    assert body["detail"]


def test_an_unreadable_object_does_not_fall_back_to_the_summary(client, db, objects):
    objects.put(final_key(), json.dumps(REFERENCE) + "\n")
    objects.fail_on(final_key())
    a_task(db, summary=summary_of(artifacts={"output.txt": "x"}, runner_summary="a summary"))

    body = get(client).json()
    assert body["status"] == "unreadable"
    assert body["content"] is None
    assert body["source"] is None
    assert body["detail"]


def test_the_answer_is_redacted_after_decoding(client, db, objects):
    result = {"type": "result", "is_error": False,
              "result": f"push with {GH_TOKEN.replace('g', chr(92) + 'u0067', 1)}"}
    raw_line = json.dumps(result).replace("\\\\u0067", "\\u0067")
    assert GH_TOKEN not in raw_line
    objects.put(final_key(), raw_line + "\n")
    a_task(db)

    response = get(client)
    body = response.json()
    assert "Aa1Bb2Cc3" not in response.text
    assert body["content"].startswith("push with ghp_")
    assert body["redacted"] is True
    assert body["redaction_count"] >= 1


def test_a_task_that_has_not_started_is_not_yet_and_one_that_ended_unstarted_is_absent(
    client, db, objects
):
    a_task(db, state="QUEUED", attempt=False)
    assert get(client).json()["status"] == "not_yet"

    db.docs["tasks/task_a"]["state"] = "CANCELLED"
    assert get(client).json()["status"] == "absent"


def test_another_tenants_answer_is_the_404_a_missing_task_gets(client, db, objects):
    objects.put(final_key(), json.dumps(REFERENCE) + "\n")
    a_task(db)

    response = get(client, user="bob")
    assert response.status_code == 404, response.text
    assert "Findings" not in response.text
