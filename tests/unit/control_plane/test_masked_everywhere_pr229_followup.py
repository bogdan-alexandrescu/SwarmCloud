"""Two follow-up findings from the PR #229 review, on top of #229 itself.

FINDING 1 (`redaction.py` `redact_lines`). Plain `json.loads` keeps only the
LAST value of a repeated key: `{"note":"export PASSWORD=<v>","note":"ok"}`
decodes to `{"note": "ok"}`. The structural walk then finds nothing to mask
in `"ok"`, the document's masking changed nothing, and the line -- the
SECRET half still in it -- was served byte for byte, count 0. `main` (the
plain text-rule pass every line used to get) masked it. A line whose JSON
holds the same key twice, at any level, is now detected with an
`object_pairs_hook` before it is trusted as a document at all, and falls
back to the text rule over the STORED bytes, where the secret still is. The
same fallback also runs when a document decodes cleanly but the structural
walk masks nothing -- it only touches string leaves and credential-named
keys' whole values, so a shape it cannot see is still given to the text rule
before the line is called clean.

FINDING 2 (`agent_output.py`). `GET /v1/tasks/{id}/artifacts/raw`'s text
download masked with `redact(text)` alone: no per-line JSON structure (a
list under a credential's name served every element after the first, the
same bug #229 fixed for `/logs` and `/transcript`) and no
`masking_for(task).literals` (a value only the task's own metadata named as
secret was served in the clear). `GET /v1/tasks/{id}/artifacts/content`
already runs `redaction.redact_lines(text, literals=masking_for(task)
.literals)`. The two routes read the SAME bytes and must mask them the SAME
way.

Offline: FakeFirestore and the in-memory object reader. No credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from swarm_api.redaction import MASK, redact_lines

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"

#: A duplicate `"note"` key: `json.loads` keeps only the SECOND value ("ok"),
#: so a check over the decoded document alone never sees the secret half.
DUP_SECRET = "hunter2-duplicate-key-review"
DUP_LINE = '{"note":"export PASSWORD=' + DUP_SECRET + '","note":"ok"}'

#: A value only the task's own metadata names as secret (`_learned_literals`).
METADATA_LITERAL = "brass-lantern-grue-window-58211"


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------

def _artifact_key(name: str, *, tenant: str = "eng", task: str = "task_a", attempt: str = "att_1") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}/artifacts/{name}"


def _a_finished_task(
    db, objects, *, files: dict[str, Any], metadata: dict[str, Any] | None = None
) -> None:
    """A terminal task whose manifest and bucket agree, with a caller metadata literal."""
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    doc["metadata"] = dict(metadata or {})
    entries = []
    for name, body in files.items():
        raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
        key = _artifact_key(name)
        objects.put(key, raw_bytes)
        entries.append({"name": name, "bytes": len(raw_bytes), "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
    }
    doc["completed_at"] = datetime.now(timezone.utc)


def _content(client, name: str, user: str = "alice") -> dict[str, Any]:
    return client.get(
        "/v1/tasks/task_a/artifacts/content",
        params={"name": name},
        headers=auth_header(user),
    ).json()


def _raw(client, name: str, user: str = "alice"):
    return client.get(
        "/v1/tasks/task_a/artifacts/raw",
        params={"name": name},
        headers=auth_header(user),
    )


def _seed_log(db, objects, *, stream: str, body: str, metadata: dict[str, Any] | None = None) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    doc["metadata"] = dict(metadata or {})
    moment = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_att_1", "backend": "CLOUD_RUN_JOB", "execution_name": "x",
        "created_at": moment, "started_at": moment,
        "completed_at": moment, "exit_code": 0, "error": None,
        "peak_rss_bytes": 1, "oom_near_miss": False, "checkpoints": [],
    })
    objects.put(f"tenants/eng/tasks/task_a/attempts/att_1/logs/{stream}.log", body)


def _log_stream(client, stream: str) -> dict[str, Any]:
    response = client.get(f"/v1/tasks/task_a/logs?stream={stream}", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return next(s for s in response.json()["streams"] if s["stream"] == stream)


# --------------------------------------------------------------------------
# Finding 1: a duplicate JSON key falls back to the text rule
# --------------------------------------------------------------------------

def test_the_pure_function_falls_back_to_text_on_a_duplicate_key():
    """The control first: plain `json.loads` silently keeps only the last
    value of a repeated key, so a check over the decoded document alone would
    see nothing wrong with this line at all -- the secret is gone before any
    masking logic runs."""
    assert json.loads(DUP_LINE) == {"note": "ok"}, "control: json.loads alone loses the secret half"

    got = redact_lines(DUP_LINE + "\n")
    assert DUP_SECRET not in got.text, got.text
    assert got.count >= 1


def test_a_duplicate_json_key_is_masked_through_logs(client, db, objects):
    _seed_log(db, objects, stream="stdout", body=DUP_LINE + "\n")

    entry = _log_stream(client, "stdout")
    assert DUP_SECRET not in entry["content"], entry["content"]
    assert entry["redaction_count"] >= 1


def test_a_duplicate_json_key_is_masked_through_artifacts_content(client, db, objects):
    _a_finished_task(db, objects, files={"events.jsonl": DUP_LINE + "\n"})

    body = _content(client, "events.jsonl")
    assert body["status"] == "ok", body
    assert DUP_SECRET not in body["content"], body["content"]
    assert body["redaction_count"] >= 1
    assert body["redacted"] is True


# --------------------------------------------------------------------------
# Finding 2: /artifacts/raw must mask exactly as /artifacts/content does
# --------------------------------------------------------------------------

def test_raw_matches_content_for_a_metadata_literal_and_a_masked_list(client, db, objects):
    """At the PR's head, both of these leaked from `/artifacts/raw` while
    `/artifacts/content` already masked them: a list under a credential's
    name (the text rule only masks its first element) and a value only the
    task's metadata named as secret (the raw route never saw the task's
    literals at all). Once both routes share `redact_lines` plus the task's
    literals, they must serve the SAME bytes for the SAME object."""
    body_text = "\n".join([
        json.dumps({"password": ["a", "b"]}),
        'PASSWORD = "has space"',
        f"deployed with {METADATA_LITERAL} in play",
    ]) + "\n"
    _a_finished_task(
        db, objects,
        files={"agent.log": body_text},
        metadata={"deploy_secret": METADATA_LITERAL},
    )

    content_body = _content(client, "agent.log")
    assert content_body["status"] == "ok", content_body
    assert METADATA_LITERAL not in content_body["content"]
    first_content_line = content_body["content"].split("\n", 1)[0]
    assert json.loads(first_content_line) == {"password": MASK}, first_content_line

    raw_response = _raw(client, "agent.log")
    assert raw_response.status_code == 200, raw_response.text
    assert raw_response.headers["x-swarm-redaction"] == "applied"
    assert METADATA_LITERAL not in raw_response.text, raw_response.text
    first_raw_line = raw_response.text.split("\n", 1)[0]
    assert json.loads(first_raw_line) == {"password": MASK}, first_raw_line

    # PARITY: not just "nothing leaked from either", but byte for byte the
    # same masked text out of both routes.
    assert raw_response.text == content_body["content"], (raw_response.text, content_body["content"])
