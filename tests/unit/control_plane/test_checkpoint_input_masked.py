"""A checkpoint's `input.json` is served as the task route serves the input.

THE PR #229 REVIEW, measured on the pure functions at aa00107 with the input

    {"prompt": "connect using <v>", "db_password": "<v>",
     "tokens": ["tok-first-element-aaaa", "tok-second-element-bbbb"]}

`GET /v1/tasks/{id}` masked all three, count 3. The checkpoint file view of
`input.json` -- which the UI's CheckpointBrowser opens -- ran only the rules
over its TEXT, and served the prompt's `<v>` and both token elements in the
clear, count 1. The worker no longer archives the file
(`test_checkpoint_leaves_out_input.py`); an archive written before still holds
it, and this holds how it is served: decoded, masked by the task's own masker,
whole, with the task route's count.

Any OTHER file in a checkpoint is masked as `/logs` masks a window: a JSON line
by its structure, and the task's literals everywhere.

MUTATIONS: drop the `input.json` branch from `_serve_member` (every secret
below comes back); build the masker without the task's metadata (the
metadata-literal case goes red); mask other files without the task's
literals.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant
from .test_checkpoint_content import put_checkpoint, tar_gz

ASSIGNED = "hunter2-very-secret"
TOKENS = ["tok-first-element-aaaa", "tok-second-element-bbbb"]
#: Named as a secret only by the task's metadata; no rule masks it.
LITERAL = "zork-grue-lantern-brass-4471"

INPUT = {
    "prompt": f"connect using {ASSIGNED} then {LITERAL}",
    "db_password": ASSIGNED,
    "tokens": TOKENS,
}


def _seed(db, objects, files: list[tuple]) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")
    doc["input"] = dict(INPUT)
    doc["metadata"] = {"deploy_secret": LITERAL}
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_1", "backend": "CLOUD_RUN_JOB", "created_at": doc["created_at"],
    })
    put_checkpoint(objects, archive=tar_gz(files), file_count=len(files))


def _file(client, path: str) -> dict[str, Any]:
    response = client.get(
        f"/v1/tasks/task_a/checkpoints/ckpt-00001/files/{path}", headers=auth_header("alice")
    )
    assert response.status_code == 200, response.text
    return response.json()


def _clean(text: str, where: str) -> None:
    for secret in (ASSIGNED, LITERAL, *TOKENS):
        assert secret not in text, f"{where} served {secret!r}"


def test_an_archived_input_json_is_masked_by_the_tasks_masker_whole(client, db, objects):
    # As `_prepare` writes it: the task's input plus the worker's own keys, indent=2.
    written = {**INPUT, "task_id": "task_a", "attempt_id": "att_1", "resumed_from_checkpoint": False}
    _seed(db, objects, [("input.json", "file", json.dumps(written, indent=2))])

    body = _file(client, "input.json")
    task = client.get("/v1/tasks/task_a", headers=auth_header("alice")).json()["task"]

    _clean(body["content"], "the checkpoint's input.json")
    served = json.loads(body["content"])
    assert served["db_password"] == MASK
    assert served["tokens"] == MASK, "a list under a credential's name is masked whole"
    assert served["prompt"] == f"connect using {MASK} then {MASK}"
    assert served["task_id"] == "task_a", "the worker's own keys are served as written"
    assert body["redaction_count"] == task["input_redaction_count"] == 4
    assert (body["truncated"], body["next_offset"]) == (False, None), "served whole"
    assert body["content"] == json.dumps(served, indent=2, ensure_ascii=False)


def test_a_later_offset_of_it_is_the_end_not_a_second_window(client, db, objects):
    _seed(db, objects, [("input.json", "file", json.dumps(INPUT, indent=2))])
    body = _file(client, "input.json?offset=40")

    assert body["content"] == ""
    assert body["next_offset"] is None


def test_another_file_is_masked_by_structure_and_with_the_tasks_literals(client, db, objects):
    """An agent's session log in the workspace is NDJSON; its notes are text."""
    session = json.dumps({"type": "tool_use", "input": {"tokens": TOKENS}}) + "\n"
    notes = f"the deploy key was {LITERAL}\n"
    _seed(db, objects, [("session.jsonl", "file", session), ("notes.txt", "file", notes)])

    jsonl = _file(client, "session.jsonl")
    text = _file(client, "notes.txt")

    _clean(jsonl["content"], "session.jsonl")
    _clean(text["content"], "notes.txt")
    assert json.loads(jsonl["content"])["input"]["tokens"] == MASK
    assert text["content"] == f"the deploy key was {MASK}\n"
    assert text["redaction_count"] == 1


def test_a_file_with_nothing_in_it_to_mask_is_served_byte_for_byte(client, db, objects):
    """The control: structural masking re-writes only a line it masked."""
    line = '{"type": "note",  "text": "nothing secret here"}\n'
    _seed(db, objects, [("plain.jsonl", "file", line)])

    body = _file(client, "plain.jsonl")
    assert body["content"] == line
    assert body["redaction_count"] == 0
