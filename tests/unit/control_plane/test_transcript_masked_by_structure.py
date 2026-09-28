"""`/v1/tasks/{id}/transcript`: a tool's input and an event's record are masked by their structure (#221).

THE DEFECT, on main at 8d65b58. A `tool_use` block's input was served as
`json.dumps(input, indent=2)` redacted as ONE decoded string, and the opt-in
`raw` record as `json.dumps(event)` redacted the same way. Both are JSON
TEXT, where a quote inside a string is `\\"`, and the key/value rule read a
quote as a quote. An agent's Bash call `export DB_PASSWORD="<v>" &&
./deploy.sh` was served with its backslash masked, `<v>` in clear, under a
count of 1; a curl body's `{"api_key": "<v>"}` was not matched at all.

THE OWNER'S DECISION (2026-09-26, on #221): the transcript's `tool.input` and
raw lines go through `redact_json` -- `redaction.JsonMasker`, which masks every
string decoded and a value under a credential's name whole -- as `/input`
does with a task's input.

MUTATIONS: serve `tool.input` as `redact(json.dumps(...))` again; mask `raw`
as text again; count the raw record's masks once per window instead of once
per step (the count it always had); change the served text's indentation, so a
clean input no longer reads as it did.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)

#: No recognisable prefix: only the key/value rule, or the key's name, catches them.
SECRET = "hunter2-very-secret"
BARE = "q8Zr7Lm2Xv9T"
NAMED = "correct-horse-battery-staple-8812"


def _key(attempt="att_1") -> str:
    return f"tenants/eng/tasks/task_a/attempts/{attempt}/logs/agent_stdout.log"


def a_task(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED", runner_profile="claude-code")
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": "lease_att_1", "backend": "CLOUD_RUN_JOB",
        "created_at": NOW, "started_at": NOW + timedelta(seconds=5),
        "completed_at": NOW + timedelta(minutes=2), "exit_code": 0, "checkpoints": [],
    })


def tool_call(uuid: str, tool_id: str, tool_input: dict) -> dict:
    return {
        "type": "assistant", "uuid": uuid,
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": tool_input}]},
    }


def get(client, query=""):
    return client.get(f"/v1/tasks/task_a/transcript{query}", headers=auth_header("alice"))


def test_a_quoted_assignment_in_a_bash_call_is_masked_and_counted_once(client, db, objects):
    a_task(db)
    event = tool_call("u-1", "toolu_1", {"command": f'export DB_PASSWORD="{SECRET}" && ./deploy.sh'})
    objects.put(_key(), json.dumps(event) + "\n")

    response = get(client)
    assert response.status_code == 200, response.text
    assert SECRET not in response.text, "the quoted value was served in clear"
    body = response.json()
    (step,) = body["steps"]
    served = json.loads(step["tool"]["input"])
    assert served == {"command": 'export DB_PASSWORD="********" && ./deploy.sh'}, served
    assert body["redaction_count"] == 1


def test_a_json_pair_inside_a_curl_body_is_masked(client, db, objects):
    a_task(db)
    event = tool_call(
        "u-2", "toolu_2",
        {"command": f"curl -d '{{\"api_key\": \"{BARE}\"}}' https://example.com"},
    )
    objects.put(_key(), json.dumps(event) + "\n")

    response = get(client)
    assert BARE not in response.text, "a quoted JSON pair in a tool input was served in clear"
    step = response.json()["steps"][0]
    assert '"api_key\\": \\"********' in step["tool"]["input"], step["tool"]["input"]
    assert response.json()["redaction_count"] == 1


def test_a_value_under_a_credential_name_in_the_input_is_masked_whole(client, db, objects):
    """What the text rule could never see: the value's key is the input's own
    key, not text beside it."""
    a_task(db)
    event = tool_call("u-3", "toolu_3", {"url": "https://example.com", "auth_token": NAMED})
    objects.put(_key(), json.dumps(event) + "\n")

    response = get(client)
    assert NAMED not in response.text
    served = json.loads(response.json()["steps"][0]["tool"]["input"])
    assert served == {"url": "https://example.com", "auth_token": "********"}


def test_a_clean_input_reads_exactly_as_it_did(client, db, objects):
    """Masking by structure must not change what a clean input looks like:
    the same indented `json.dumps` the step always carried, and no count."""
    a_task(db)
    tool_input = {"file_path": "CONTRACT.md", "limit": 40, "nested": {"a": [1, "two"]}}
    objects.put(_key(), json.dumps(tool_call("u-4", "toolu_4", tool_input)) + "\n")

    body = get(client).json()
    assert body["steps"][0]["tool"]["input"] == json.dumps(tool_input, indent=2, ensure_ascii=False)
    assert body["redaction_count"] == 0


def test_the_raw_record_is_masked_by_its_structure_and_stays_one_line(client, db, objects):
    a_task(db)
    event = tool_call("u-5", "toolu_5", {"command": f'export DB_PASSWORD="{SECRET}"'})
    objects.put(_key(), json.dumps(event) + "\n")

    plain = get(client).json()
    body = get(client, "?include_raw=true").json()
    assert SECRET not in json.dumps(body), "the raw record served the quoted value"
    (step,) = body["steps"]
    assert "\n" not in step["raw"], "the raw record is the event's one-line text"
    record = json.loads(step["raw"])
    assert record["message"]["content"][0]["input"] == {"command": 'export DB_PASSWORD="********"'}
    # The record's mask is counted on the step that carries it, as before:
    # one in the input, one in the record.
    assert body["redaction_count"] == plain["redaction_count"] + 1 == 2


def test_a_clean_raw_record_is_the_events_own_text(client, db, objects):
    a_task(db)
    event = {"type": "system", "subtype": "hook_started", "uuid": "u-hook"}
    objects.put(_key(), json.dumps(event) + "\n")

    step = get(client, "?include_raw=true").json()["steps"][0]
    assert step["raw"] == json.dumps(event, ensure_ascii=False)
