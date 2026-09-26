"""What a task collected about itself is masked by the input's masker, on every route.

THE PR #229 REVIEW. The owner's decision of 2026-09-26 on #184 was "the API
never serves a credential-shaped string back, even to the submitter". PR #229
masked the input and the metadata and left the text the task COLLECTED served
as stored:

  * `last_error` is `_tail_text(stderr)`, scrubbed by the worker for the
    secrets IT registered and nothing else (`redact.scrub_text` matches
    literals only);
  * an attempt's `error` and a FAILED event's `detail.error` are the same
    string (`control.finish`);
  * `result_summary.runner.summary` is up to 4,000 characters of the agent's
    own final text.

The review's scenario: submit `run ./migrate with DB_PASSWORD=<v>`, the CLI
fails and prints `auth failed: DB_PASSWORD=<v>` to stderr. The task route
served the prompt masked, count 1, and `last_error` in the clear beside it;
`swarm result` printed `masked 1` and then the value on the next line.

What is pinned: each of those fields is masked on every route that serves it
-- the task, the task list, both attempt routes, the events route, the admin
lease rows, `/answer`'s runner-summary fallback -- by the SAME masker as the
input, so a literal named only in the metadata is masked there too, and each
carries a count. The platform's keys inside an event's detail stay readable,
and an artifact's name in `result_summary` is served as stored.

MUTATIONS: serve `task.last_error` again from `task_to_api`; mask it with the
rules only (the literal case goes red); mask `result_summary` by key, as a
caller document is (the `credential` detail and the artifact name go red);
drop the masking from `_event_to_api`, from either attempt route, or from the
admin lease rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)

#: No recognisable prefix: caught by the key/value rule beside `DB_PASSWORD=`.
ASSIGNED = "hunter2-very-secret"
#: Caught ONLY as a literal the metadata named: no rule would ever mask it.
LITERAL = "zork-grue-lantern-brass-4471"
#: A name the JWT rule mistakes for a token; it must stay findable.
LOOKALIKE_NAME = "eye-tracking-summary.md"


def _task(db, *, task_id: str = "task_a", **fields: Any) -> dict[str, Any]:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="FAILED", runner_profile="mock")
    doc.update(
        {
            "input": {"prompt": f"run ./migrate with DB_PASSWORD={ASSIGNED}"},
            "metadata": {"deploy_secret": LITERAL},
            "last_error": f"auth failed: DB_PASSWORD={ASSIGNED}, then retried with {LITERAL}",
            "result_summary": {
                "runner": {"summary": f"I could not log in with {LITERAL}.", "exit_code": 1},
                "artifacts": [{"name": LOOKALIKE_NAME, "bytes": 12,
                               "uri": f"gs://bucket/tenants/eng/tasks/{task_id}/attempts/att_1/artifacts/{LOOKALIKE_NAME}"}],
            },
        }
    )
    doc.update(fields)
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": task_id, "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_1", "backend": "CLOUD_RUN_JOB", "created_at": NOW,
        "started_at": NOW, "completed_at": NOW + timedelta(minutes=1), "exit_code": 1,
        "error": f"auth failed: DB_PASSWORD={ASSIGNED}, then retried with {LITERAL}",
    })
    db.docs[f"tasks/{task_id}/events/ev_failed"] = {
        "event_id": "ev_failed", "task_id": task_id, "tenant_id": "eng", "type": "failed",
        "at": NOW + timedelta(minutes=1), "attempt_id": "att_1", "lease_id": "lease_1",
        "generation": 1,
        "detail": {"error": f"auth failed: DB_PASSWORD={ASSIGNED}, then retried with {LITERAL}",
                   "exit_code": 1},
    }
    db.docs[f"tasks/{task_id}/events/ev_promoted"] = {
        "event_id": "ev_promoted", "task_id": task_id, "tenant_id": "eng", "type": "ready",
        "at": NOW, "attempt_id": None, "lease_id": None, "generation": None,
        # `scheduler.credentials.promote_detail`: `credential` is WHICH kind,
        # written by the platform, and must stay readable.
        "detail": {"reason": "credential_available", "provider": "anthropic",
                   "credential": "tenant_secret"},
    }
    return doc


def _get(client, path: str, user: str = "alice") -> dict[str, Any]:
    response = client.get(path, headers=auth_header(user))
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def _clean(body: Any, where: str) -> None:
    text = json.dumps(body, default=str)
    assert ASSIGNED not in text, f"{where} served the value the prompt assigned"
    assert LITERAL not in text, f"{where} served the literal the metadata named"


def test_the_reviews_scenario_last_error_is_masked_beside_the_input(client, db):
    _task(db)
    task = _get(client, "/v1/tasks/task_a")["task"]

    _clean(task, "GET /v1/tasks/{id}")
    assert task["input_redaction_count"] == 1
    assert task["last_error"] == f"auth failed: DB_PASSWORD={MASK}, then retried with {MASK}"
    assert task["last_error_redaction_count"] == 2


def test_a_literal_named_only_in_the_metadata_is_masked_in_the_agents_summary(client, db):
    """The rules cannot see LITERAL; only the input's masker knows it."""
    _task(db)
    task = _get(client, "/v1/tasks/task_a")["task"]

    runner = task["result_summary"]["runner"]
    assert runner["summary"] == f"I could not log in with {MASK}."
    assert runner["exit_code"] == 1, "numbers in the summary are served as stored"
    assert task["result_summary_redaction_count"] == 1


def test_an_artifacts_name_in_the_summary_is_served_as_stored(client, db):
    """A name is looked up EXACTLY (Artifacts' `upstreamEntry`); the JWT rule
    would take this one for a token and the file would be unfindable."""
    _task(db)
    task = _get(client, "/v1/tasks/task_a")["task"]

    entry = task["result_summary"]["artifacts"][0]
    assert entry["name"] == LOOKALIKE_NAME
    assert entry["uri"].endswith(LOOKALIKE_NAME)


def test_the_list_route_masks_the_same_fields(client, db):
    _task(db)
    page = _get(client, "/v1/tasks?limit=50")

    _clean(page, "GET /v1/tasks")
    (row,) = page["tasks"]
    assert row["last_error_redaction_count"] == 2
    assert row["result_summary_redaction_count"] == 1


def test_both_attempt_routes_mask_the_error_with_the_tasks_masker(client, db):
    _task(db)
    own = _get(client, "/v1/tasks/task_a/attempts")
    across = _get(client, "/v1/attempts")

    _clean(own, "GET /v1/tasks/{id}/attempts")
    _clean(across, "GET /v1/attempts")
    (a,) = own["attempts"]
    (b,) = [r for r in across["attempts"] if r["attempt_id"] == "att_1"]
    assert a["error"] == f"auth failed: DB_PASSWORD={MASK}, then retried with {MASK}"
    assert a["error_redaction_count"] == 2
    assert (b["error"], b["error_redaction_count"]) == (a["error"], a["error_redaction_count"]), (
        "the two routes serve one attempt differently"
    )


def test_the_events_route_masks_every_detail_string_and_keeps_the_platforms_keys(client, db):
    _task(db)
    body = _get(client, "/v1/tasks/task_a/events")

    _clean(body, "GET /v1/tasks/{id}/events")
    by_id = {e["event_id"]: e for e in body["events"]}
    failed = by_id["ev_failed"]
    assert failed["detail"]["error"] == f"auth failed: DB_PASSWORD={MASK}, then retried with {MASK}"
    assert failed["detail"]["exit_code"] == 1
    assert failed["detail_redaction_count"] == 2
    promoted = by_id["ev_promoted"]
    assert promoted["detail"] == {
        "reason": "credential_available", "provider": "anthropic", "credential": "tenant_secret",
    }, "a platform key naming a KIND of credential was masked as if it held one"
    assert promoted["detail_redaction_count"] == 0


def test_the_admin_lease_rows_mask_last_error(client, db):
    _task(db)
    db.collection("leases").document("lease_1").set({
        "lease_id": "lease_1", "task_id": "task_a", "attempt_id": "att_1", "tenant_id": "eng",
        "generation": 1, "pools": ["global"], "units": 1, "state": "DISPATCHED",
        "created_at": NOW, "dispatch_deadline": NOW + timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=30), "heartbeat_at": NOW,
        "released_at": None, "release_reason": None,
    })
    body = _get(client, "/v1/admin/leases", user="root")

    _clean(body, "GET /v1/admin/leases")
    (row,) = [r for r in body["leases"] if r["lease_id"] == "lease_1"]
    assert row["last_error"] == f"auth failed: DB_PASSWORD={MASK}, then retried with {MASK}"


def test_the_answer_routes_runner_summary_fallback_uses_the_same_masker(client, db):
    """With no agent stdout, `/answer` serves `result_summary.runner.summary`,
    and served it through the rules only: the task route masked LITERAL there
    and `/answer` served it."""
    _task(db)
    body = _get(client, "/v1/tasks/task_a/answer?attempt_id=att_1")

    _clean(body, "GET /v1/tasks/{id}/answer")
    assert (body["status"], body["source"]) == ("ok", "runner_summary"), body
    assert body["content"] == f"I could not log in with {MASK}."
    assert body["redaction_count"] == 1


def test_a_task_that_collected_nothing_serves_nulls_and_zero_counts(client, db):
    """The control: nothing is invented where nothing was collected."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_quiet", tenant_id="eng", state="QUEUED")
    task = _get(client, "/v1/tasks/task_quiet")["task"]

    assert task["last_error"] is None and task["last_error_redaction_count"] == 0
    assert task["result_summary"] is None and task["result_summary_redaction_count"] == 0
