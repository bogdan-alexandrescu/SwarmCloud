"""`/logs` masks a line that is a JSON document by its structure, and the task's literals everywhere.

THE PR #229 REVIEW of #221. `/logs` ran the rules over every line's TEXT, and a
stream-json line is JSON text, where the key/value rule can only guess where a
value ends. Measured on the pure function at aa00107:

  * `bash -c "export DB_PASSWORD=\\"<v>\\""` -- two escapes deep -- served
    `<v>` under a count of 1, while `/transcript` masked the same event;
  * a list under a credential's name served every element after the first,
    and an object under one served its strings;
  * `export DB_PASSWORD="ab\\cd-rest"` served `\\cd-rest` onward.

And the runner's `child started` line printed the prompt inside argv, so a value
only the task's METADATA named as a secret was masked by the task route and
printed by `/logs`.

What is pinned: each of those, through the route; a JSON line nothing was
masked in is served byte for byte; a plain-text line keeps the text rule; and
the task's literals are masked in both kinds of line.

MUTATIONS: go back to `redact(text)` over the window in `inspect._served`
(the list, space and backslash cases go red); stop passing the task's literals
(the argv case goes red); re-serialise every JSON line (the byte-for-byte
control goes red).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
SECRET = "hunter2-very-secret"
TOKENS = ["tok-first-element-aaaa", "tok-second-element-bbbb"]
LITERAL = "zork-grue-lantern-brass-4471"


def _seed(db, objects, *, stream: str, body: str, metadata: dict[str, Any] | None = None) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    doc["metadata"] = dict(metadata or {})
    created = NOW - timedelta(minutes=5)
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_att_1", "backend": "CLOUD_RUN_JOB", "execution_name": "x",
        "created_at": created, "started_at": created,
        "completed_at": created + timedelta(minutes=1), "exit_code": 0, "error": None,
        "peak_rss_bytes": 1, "oom_near_miss": False, "checkpoints": [],
    })
    objects.put(f"tenants/eng/tasks/task_a/attempts/att_1/logs/{stream}.log", body)


def _stream(client, stream: str) -> dict[str, Any]:
    response = client.get(f"/v1/tasks/task_a/logs?stream={stream}", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return next(s for s in response.json()["streams"] if s["stream"] == stream)


def _tool_line(command: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}
    )


def test_a_value_two_escapes_deep_is_masked(client, db, objects):
    line = _tool_line(f'bash -c "export DB_PASSWORD=\\"{SECRET}\\" && ./deploy.sh"')
    assert f'\\\\\\"{SECRET}' in line, "the fixture must hold the value two escapes deep"
    _seed(db, objects, stream="stdout", body=line + "\n")

    entry = _stream(client, "stdout")
    assert SECRET not in entry["content"]
    assert entry["redaction_count"] == 1, entry["content"]
    assert json.loads(entry["content"])["type"] == "assistant", "still one JSON document per line"


def test_every_element_of_a_list_under_a_credentials_name_is_masked(client, db, objects):
    line = json.dumps({"type": "tool_use", "input": {"tokens": TOKENS, "db": {"password": [SECRET]}}})
    _seed(db, objects, stream="stdout", body=line + "\n")

    entry = _stream(client, "stdout")
    for secret in (*TOKENS, SECRET):
        assert secret not in entry["content"], secret
    served = json.loads(entry["content"])
    assert served["input"]["tokens"] == MASK and served["input"]["db"]["password"] == MASK


def test_a_value_holding_a_space_or_a_backslash_is_masked_to_its_end(client, db, objects):
    lines = [
        json.dumps({"input": {"password": "two words here"}}),
        _tool_line('export DB_PASSWORD="ab\\cd-rest-of-secret"'),
    ]
    _seed(db, objects, stream="stdout", body="\n".join(lines) + "\n")

    entry = _stream(client, "stdout")
    assert "words here" not in entry["content"]
    assert "cd-rest-of-secret" not in entry["content"]
    assert entry["redaction_count"] == 2, entry["content"]


def test_a_json_line_with_nothing_to_mask_is_served_byte_for_byte_and_text_keeps_the_rule(
    client, db, objects
):
    """The control: only a line that was masked is written back, and a plain
    line is masked as it always was."""
    clean = '{"type": "system",  "subtype": "init", "cwd": "/work"}'
    text = f"export GH_TOKEN={SECRET}"
    _seed(db, objects, stream="stdout", body=f"{clean}\n{text}\n")

    entry = _stream(client, "stdout")
    assert entry["content"] == f"{clean}\nexport GH_TOKEN={MASK}\n"
    assert entry["redaction_count"] == 1


def test_a_value_only_the_tasks_metadata_names_is_masked_in_the_runners_argv(client, db, objects):
    """The review's second case: the runner logged the prompt inside argv, and
    the prompt used a value the metadata named as a secret. The runner logs the
    prompt's length now; a log written before it is masked here."""
    started = json.dumps({"severity": "INFO", "message": "child started",
                          "argv": ["claude", "-p", f"use {LITERAL} to deploy"]})
    _seed(db, objects, stream="stderr", body=f"{started}\nplain echo of {LITERAL}\n",
          metadata={"deploy_secret": LITERAL})

    entry = _stream(client, "stderr")
    assert LITERAL not in entry["content"]
    assert entry["redaction_count"] == 2
    assert json.loads(entry["content"].splitlines()[0])["argv"][-1] == f"use {MASK} to deploy"


def test_the_pure_function_keeps_a_line_ending_and_treats_a_cut_line_as_text():
    """A window cut mid-line holds a line that is not a document: the text rule."""
    from swarm_api.redaction import redact_lines

    cut ='{"password": "abc'  # the rest of the line is in the next window
    got = redact_lines(f"{cut}\r\n" + json.dumps({"api_key": "q8Zr7Lm2Xv9T"}) + "\r\n")
    assert got.text.endswith('{"api_key":"********"}\r\n'), got.text
    assert got.text.startswith('{"password": "********'), got.text
    assert got.count == 2
