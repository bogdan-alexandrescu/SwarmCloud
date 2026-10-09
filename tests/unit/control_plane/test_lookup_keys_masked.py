"""Lookup-key strings in `result_summary` and an event's `detail` are masked by `name` (#296).

THE HOLE. `TaskMasking.leaves` served any string directly under one of
`task_input.LOOKUP_KEYS` (`name`, `uri`, `key`, `filename`, `path`,
`attempt_id`, `task_id`, `checkpoint_id`) exactly as stored, so a client could
match a staged input or an artifact by name. But a staged input's `filename`
carrying the value the prompt assigned to `DB_PASSWORD`, or an artifact named
after an `sk-` key, was then served in clear inside `result_summary` -- right
beside the masked copy of the very same filename under `metadata.input_from` /
`metadata.expected_outputs` (#227). The owner's decision of 2026-09-26: the
API never serves a credential-shaped string back, even to the submitter.

THE FIX: mask, then match. Each lookup-key string is masked by
`TaskMasking.name`, the same deterministic, anchored function that masks the
declared filenames, so:

  * a clean name (`eye-tracking-summary.md`, `task-report-final.md`) and the
    platform's ids are served byte for byte, with a zero count;
  * a credential-shaped one, or one carrying a literal the task named, is
    masked, and masked IDENTICALLY to its declared copy, so the staged or
    produced name still equals the declared one;
  * each mask is counted in `result_summary_redaction_count` /
    `detail_redaction_count`;
  * on every route that serves a task: the task, the list, events, a
    workflow read's tasks, the cancel response.

Every fake credential here is built at runtime: the worker's publish scan
refuses a diff with a credential-shaped literal.

MUTATIONS: return a lookup-key string as stored again (every secret test
fails); mask it with `masker.text` instead of `name` (the clean-name test and
the pairing assertions fail: the JWT rule takes `eye-tracking-summary.md`);
leave the mask uncounted (the count assertions fail).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from swarm_api.redaction import MASK
from swarm_api.task_input import LOOKUP_KEYS, TaskMasking

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)

#: Assigned in the prompt, so the masker learns it as a literal; no rule
#: masks it on its own.
SECRET = "zebra-quartz-lantern-" + "7731"
PROMPT = f"deploy with DB_PASSWORD={SECRET}"
#: Shaped like the `sk-` family, built at runtime. Not a real credential.
SK_KEY = "sk-proj-" + "x" * 40
SK_NAME = SK_KEY + ".md"
STAGED = f"notes-{SECRET}.md"
URI_PREFIX = "gs://bucket/tenants/eng/tasks/task_a/attempts/att_1/artifacts/"

#: The platform's id shapes (`swarm_common.models.new_id`: prefix + 20 hex).
TASK_ID = "task_" + "0f3a9c1b2d4e5f60718a"
ATTEMPT_ID = "att_" + "9e8d7c6b5a4f3e2d1c0b"
CHECKPOINT_ID = "ckpt_" + "a1b2c3d4e5f60718293a"
CLEAN_NAMES = ["eye-tracking-summary.md", "task-report-final.md"]


def _get(client, path: str, user: str = "alice") -> dict[str, Any]:
    response = client.get(path, headers=auth_header(user))
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def _clean(body: Any, where: str) -> None:
    text = json.dumps(body, default=str)
    assert SECRET not in text, f"{where} served the value the prompt assigned"
    assert SK_KEY not in text, f"{where} served the sk- shaped key"


def _seed(db, *, state: str = "SUCCEEDED", result_summary: dict[str, Any] | None = None,
          detail: dict[str, Any] | None = None) -> dict[str, Any]:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state=state, runner_profile="mock")
    doc["input"] = {"prompt": PROMPT}
    doc["metadata"] = {"input_from": {"task_up": STAGED}, "expected_outputs": [SK_NAME]}
    doc["result_summary"] = result_summary if result_summary is not None else {
        "staged_inputs": [{"task_id": "task_up", "filename": STAGED,
                           "path": f"work/inputs/{STAGED}", "bytes": 1}],
        "artifacts": [{"name": SK_NAME, "uri": URI_PREFIX + SK_NAME, "bytes": 3}],
    }
    if detail is not None:
        db.docs["tasks/task_a/events/ev_1"] = {
            "event_id": "ev_1", "task_id": "task_a", "tenant_id": "eng", "type": "succeeded",
            "at": NOW, "attempt_id": "att_1", "lease_id": "lease_1", "generation": 1,
            "detail": detail,
        }
    return doc


def _assert_summary_masked(task: dict[str, Any], where: str) -> None:
    _clean(task, where)
    declared_staged = task["metadata"]["input_from"]["task_up"]
    (declared_output,) = task["metadata"]["expected_outputs"]
    assert declared_staged == f"notes-{MASK}.md"
    assert MASK in declared_output and declared_output.endswith(".md")

    (staged,) = task["result_summary"]["staged_inputs"]
    assert staged["filename"] == declared_staged, "masked, then matched against input_from"
    assert staged["path"] == f"work/inputs/{declared_staged}"
    assert staged["task_id"] == "task_up", "the join key is unchanged"
    assert staged["bytes"] == 1

    (artifact,) = task["result_summary"]["artifacts"]
    assert artifact["name"] == declared_output, "masked, then matched against expected_outputs"
    assert artifact["uri"] == URI_PREFIX + declared_output
    assert task["result_summary_redaction_count"] == 4


def test_a_staged_inputs_filename_and_path_carrying_the_prompts_literal_are_masked(client, db):
    _seed(db, result_summary={
        "staged_inputs": [{"task_id": "task_up", "filename": STAGED,
                           "path": f"work/inputs/{STAGED}", "bytes": 1}],
    })
    task = _get(client, "/v1/tasks/task_a")["task"]

    _clean(task, "GET /v1/tasks/{id}")
    (staged,) = task["result_summary"]["staged_inputs"]
    assert staged["filename"] == task["metadata"]["input_from"]["task_up"] == f"notes-{MASK}.md"
    assert staged["path"] == f"work/inputs/notes-{MASK}.md"
    assert staged["task_id"] == "task_up"
    assert task["result_summary_redaction_count"] == 2


def test_an_sk_shaped_artifact_name_and_uri_are_masked_and_match_expected_outputs(client, db):
    _seed(db, result_summary={
        "artifacts": [{"name": SK_NAME, "uri": URI_PREFIX + SK_NAME, "bytes": 3}],
    })
    task = _get(client, "/v1/tasks/task_a")["task"]

    _clean(task, "GET /v1/tasks/{id}")
    (artifact,) = task["result_summary"]["artifacts"]
    (declared,) = task["metadata"]["expected_outputs"]
    assert MASK in declared
    assert artifact["name"] == declared
    assert SK_KEY not in artifact["uri"]
    assert artifact["uri"] == URI_PREFIX + declared
    assert task["result_summary_redaction_count"] == 2


def test_the_same_values_under_an_events_detail_are_masked_and_counted(client, db):
    _seed(db, detail={
        "name": SK_NAME,
        "uri": URI_PREFIX + SK_NAME,
        "filename": STAGED,
        "path": f"work/inputs/{STAGED}",
        "key": f"tenants/eng/{SK_NAME}",
        "attempt_id": "att_1",
    })
    task = _get(client, "/v1/tasks/task_a")["task"]
    body = _get(client, "/v1/tasks/task_a/events")

    _clean(body, "GET /v1/tasks/{id}/events")
    (event,) = [e for e in body["events"] if e["event_id"] == "ev_1"]
    detail = event["detail"]
    (declared_output,) = task["metadata"]["expected_outputs"]
    declared_staged = task["metadata"]["input_from"]["task_up"]
    assert detail["name"] == declared_output
    assert detail["uri"] == URI_PREFIX + declared_output
    assert detail["filename"] == declared_staged
    assert detail["path"] == f"work/inputs/{declared_staged}"
    assert detail["key"] == f"tenants/eng/{declared_output}"
    assert detail["attempt_id"] == "att_1"
    assert event["detail_redaction_count"] == 5


def test_the_task_list_serves_the_masked_values(client, db):
    _seed(db)
    page = _get(client, "/v1/tasks?limit=50")

    (row,) = [r for r in page["tasks"] if r["id"] == "task_a"]
    _assert_summary_masked(row, "GET /v1/tasks")


def test_the_cancel_response_serves_the_masked_values(client, db):
    _seed(db, state="READY")
    response = client.post("/v1/tasks/task_a/cancel", headers=auth_header("alice"))
    assert response.status_code == 200, response.text

    _assert_summary_masked(response.json()["task"], "POST /v1/tasks/{id}/cancel")


def test_a_workflow_reads_tasks_serve_the_masked_values(client, db):
    body = {
        "steps": [
            {"step_id": "research", "runner_profile": "mock", "input": {"prompt": "gather"},
             "depends_on": []},
            {
                "step_id": "report",
                "runner_profile": "mock",
                "input": {"prompt": PROMPT},
                "depends_on": ["research"],
                "input_from": {"research": STAGED},
            },
        ],
    }
    created = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    (report_path,) = [
        path for path, doc in db.docs.items()
        if path.count("/") == 1 and path.startswith("tasks/")
        and doc.get("workflow_id") == workflow_id and doc.get("step_id") == "report"
    ]
    (research_path,) = [
        path for path, doc in db.docs.items()
        if path.count("/") == 1 and path.startswith("tasks/")
        and doc.get("workflow_id") == workflow_id and doc.get("step_id") == "research"
    ]
    research_id = db.docs[research_path]["id"]
    db.docs[report_path]["result_summary"] = {
        "staged_inputs": [{"task_id": research_id, "filename": STAGED,
                           "path": f"work/inputs/{STAGED}", "bytes": 1}],
    }

    fetched = _get(client, f"/v1/workflows/{workflow_id}")
    (report,) = [t for t in fetched["tasks"] if t["id"] == db.docs[report_path]["id"]]
    # The report task only: its prompt is what names SECRET. The research
    # task's masker never sees that prompt, so its own `expected_outputs`
    # copy is a separate question from this lookup-key fix.
    _clean(report, "GET /v1/workflows/{id} tasks[report]")
    declared = report["metadata"]["input_from"][research_id]
    assert MASK in declared
    (staged,) = report["result_summary"]["staged_inputs"]
    assert staged["filename"] == declared
    assert staged["path"] == f"work/inputs/{declared}"
    assert staged["task_id"] == research_id
    assert report["result_summary_redaction_count"] == 2


def test_clean_names_and_platform_ids_are_served_byte_for_byte(client, db):
    summary = {
        "staged_inputs": [{"task_id": TASK_ID, "filename": name, "path": f"work/inputs/{name}",
                           "bytes": 1} for name in CLEAN_NAMES],
        "artifacts": [{"name": name, "uri": URI_PREFIX + name, "key": f"tenants/eng/{name}"}
                      for name in CLEAN_NAMES],
        "attempt_id": ATTEMPT_ID,
        "checkpoint_id": CHECKPOINT_ID,
        "task_id": TASK_ID,
    }
    detail = {"name": CLEAN_NAMES[0], "uri": URI_PREFIX + CLEAN_NAMES[1],
              "filename": CLEAN_NAMES[1], "path": f"work/inputs/{CLEAN_NAMES[0]}",
              "attempt_id": ATTEMPT_ID, "task_id": TASK_ID, "checkpoint_id": CHECKPOINT_ID}
    _seed(db, result_summary=summary, detail=detail)

    task = _get(client, "/v1/tasks/task_a")["task"]
    assert task["result_summary"] == summary
    assert task["result_summary_redaction_count"] == 0

    (event,) = [e for e in _get(client, "/v1/tasks/task_a/events")["events"]
                if e["event_id"] == "ev_1"]
    assert event["detail"] == detail
    assert event["detail_redaction_count"] == 0


def test_leaves_and_metadata_value_mask_the_same_filename_identically():
    for name in (STAGED, SK_NAME, *CLEAN_NAMES):
        masking = TaskMasking({"prompt": PROMPT},
                              {"input_from": {"task_up": name}, "expected_outputs": [name]})
        metadata, _ = masking.metadata_value()
        declared = metadata["input_from"]["task_up"]
        for key in sorted(LOOKUP_KEYS):
            served, count = masking.leaves({"entry": {key: name}})
            assert served["entry"][key] == declared, f"{key}: {name!r}"
            assert count == (0 if declared == name else 1), f"{key}: {name!r}"
