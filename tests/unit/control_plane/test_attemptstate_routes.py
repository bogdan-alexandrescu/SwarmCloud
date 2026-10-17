"""The state routes' own behaviour, past the proofs (tenant-settings-integrity.md §4.2).

`test_attemptstate.py` holds the row's refusals; this holds what each route
writes or serves when it accepts, against the direct path it replaces: the
same documents, the same fields, every id derived from the tuple. Offline.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from swarm_api.attemptstate import entry_id_for, slot_id
from swarm_api.repositories import repo_id_for

from .conftest import seed_task
from .test_attemptstate import (  # noqa: F401 - fixtures
    ATTEMPT,
    LEASE,
    POST_BODIES,
    TASK,
    Worker,
    _events,
    client,
    opened,
    worker_tokens,
)


def test_context_serves_only_this_tenants_settings(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs["workspace_settings/eng"] = {"judgment_backend": "internal-jev"}
    db.docs["workspace_settings/research"] = {"judgment_backend": "typesafe"}
    db.docs["tenant_settings/eng"] = {"code_intelligence": {"context": "graph"}}
    db.docs["provider_keys/swarm-tenant-eng-typesafe"] = {"set": True}
    response = worker.get(client, "context")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant"]["tenant_id"] == "eng"
    assert body["workspace_settings"] == {"judgment_backend": "internal-jev"}
    assert body["tenant_settings"]["code_intelligence"]["context"] == "graph"
    assert body["provider_keys"]["swarm-tenant-eng-typesafe"] == {"set": True}
    assert "research" not in response.text


def test_context_serves_the_grant_of_the_signed_submitter_and_repository(client, db):
    from swarm_api.access import grant_id_for

    worker = Worker()
    opened(client, db, worker)
    grant = grant_id_for("eng", "alice@saga.xyz", repo_id_for("eng", "acme", "widgets"))
    db.docs[f"forge_grants/{grant}"] = {"tenant_id": "eng", "mode": "write"}
    body = worker.get(client, "context").json()
    assert body["forge_grant"]["id"] == grant and body["forge_grant"]["mode"] == "write"


def test_poll_reads_this_tenants_quota_and_the_fence(client, db):
    worker = Worker()
    opened(client, db, worker, cancel_requested=True)
    db.docs["quota/anthropic:eng"] = {"state": "COOLDOWN", "retry_after_seconds": 30}
    db.docs["quota/anthropic:research"] = {"state": "AVAILABLE"}
    body = worker.get(client, "poll", "provider=anthropic").json()
    assert body["quota"]["state"] == "COOLDOWN"
    assert body["task"]["cancel_requested"] is True and body["fenced"] is None
    assert body["lease"]["released"] is False


def test_repository_serves_the_signed_repositorys_registration_and_versions(client, db):
    worker = Worker()
    opened(client, db, worker)
    repo_id = repo_id_for("eng", "acme", "widgets")
    db.docs[f"repositories/{repo_id}"] = {"tenant_id": "eng", "index": {"current_sha": "c" * 40}}
    db.docs[f"repositories/{repo_id}/index_versions/{'c' * 40}"] = {"commit_sha": "c" * 40}
    body = worker.get(client, "repository").json()
    assert body["repo_id"] == repo_id
    assert body["registration"]["index"]["current_sha"] == "c" * 40
    assert [v["commit_sha"] for v in body["versions"]] == ["c" * 40]


def test_ci_fix_tasks_are_this_tenants_only(client, db):
    worker = Worker()
    opened(client, db, worker)
    for task_id, tenant in (("task_fix_mine", "eng"), ("task_fix_theirs", "research")):
        doc = seed_task(db, task_id=task_id, tenant_id=tenant)
        doc["metadata"] = {"ci_fix": {"pull_request": 7}, "dispatch": {"continues": TASK}}
    body = worker.post(client, "ci-fix-tasks", POST_BODIES["ci-fix-tasks"]).json()
    assert [t["id"] for t in body["tasks"]] == ["task_fix_mine"]


def test_checkpoint_records_the_digest_and_moves_the_pointer(client, db):
    worker = Worker()
    opened(client, db, worker)
    response = worker.post(client, "checkpoint", POST_BODIES["checkpoint"])
    assert response.status_code == 200, response.text
    attempt = db.docs[f"attempts/{ATTEMPT}"]
    assert attempt["checkpoints"] == ["ckpt-00001"]
    assert attempt["checkpoint_sha256"] == {"ckpt-00001": "a" * 64}
    assert db.docs[f"tasks/{TASK}"]["latest_checkpoint"] == POST_BODIES["checkpoint"]["uri"]
    assert len(_events(db, "checkpoint_completed")) == 1


def test_checkpoint_refuses_a_uri_outside_this_tasks_prefix(client, db):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    for uri in ("gs://bucket/tenants/research/tasks/x/c.tar",
                f"gs://bucket/elsewhere/tenants/eng/tasks/{TASK}/c.tar",
                f"https://example.com/tenants/eng/tasks/{TASK}/c.tar"):
        body = {**POST_BODIES["checkpoint"], "uri": uri}
        assert worker.post(client, "checkpoint", body).status_code == 422, uri
    assert db.docs == before


def test_retry_requeues_with_the_release(client, db):
    worker = Worker()
    opened(client, db, worker)
    response = worker.post(client, "retry", {**POST_BODIES["retry"], "retry_delay_seconds": 60})
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "READY" and task["current_lease_id"] is None
    assert task["next_eligible_at"] > datetime.now(timezone.utc)
    assert db.docs[f"leases/{LEASE}"]["release_reason"] == "retry:crash"
    assert db.docs["pools/global"]["active"] == 0
    (event,) = _events(db, "retrying")
    assert event["detail"]["attempt_count"] == 1


def test_retry_fails_the_task_once_its_attempts_are_spent(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=3)
    worker.post(client, "retry", POST_BODIES["retry"])
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "FAILED" and task["completed_at"] is not None


def test_usage_writes_named_fields_only(client, db):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)
    bad = worker.post(client, "usage", {"kind": "spend", "fields": {"attempt_count": 0}})
    assert bad.status_code == 422 and db.docs == before
    boolean = worker.post(client, "usage", {"kind": "spend", "fields": {"input_tokens": True}})
    assert boolean.status_code == 422
    ok = worker.post(client, "usage", {"kind": "cpu", "fields": {"cpu_seconds": 2.5,
                                                                 "cpu_limit_cores": 2,
                                                                 "cpu_limit_source": "cgroup"}})
    assert ok.status_code == 200, ok.text
    attempt = db.docs[f"attempts/{ATTEMPT}"]
    assert attempt["cpu_seconds"] == 2.5 and attempt["cpu_measured_at"] is not None


def test_an_event_is_written_once_per_id(client, db):
    worker = Worker()
    opened(client, db, worker)
    for _ in range(2):
        assert worker.post(client, "event", POST_BODIES["event"]).status_code == 200
    assert len(_events(db, "heartbeat")) == 1
    bad = worker.post(client, "event", {**POST_BODIES["event"], "event_id": "evt_x",
                                        "type": "heartbeat"})
    assert bad.status_code == 422


def test_heartbeat_extends_this_leases_expiry(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs[f"leases/{LEASE}"]["expires_at"] = datetime.now(timezone.utc)
    assert worker.post(client, "heartbeat", {}).status_code == 200
    lease = db.docs[f"leases/{LEASE}"]
    assert lease["heartbeat_at"] is not None
    assert lease["expires_at"] > datetime.now(timezone.utc) + timedelta(seconds=60)


def test_a_human_park_a_person_returned_requeues(client, db):
    worker = Worker()
    opened(client, db, worker, metadata={"human_returned": {
        "attempt_id": ATTEMPT, "generation": 1, "by": "alice@saga.xyz"}})
    response = worker.post(client, "park", {"request_id": "hp", "reason": "HUMAN_REQUIRED"})
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert task["state"] == "READY" and task["metadata"]["human_ready"]["by"] == "alice@saga.xyz"
    assert db.docs[f"leases/{LEASE}"]["release_reason"] == "requeued:human_returned"


def test_a_ci_park_counts_a_slot_wait_on_its_own_counter(client, db):
    worker = Worker()
    opened(client, db, worker)
    body = {"request_id": "ci", "reason": "CI_PENDING",
            "ci": {"code": "merge_slot_wait", "head": "d" * 40, "pull_request": 7}}
    assert worker.post(client, "park", body).status_code == 200
    wait = db.docs[f"tasks/{TASK}"]["metadata"]["merge_wait"]
    assert wait["slot_waits"] == 1 and wait["wakes"] == 0 and wait["first_parked_at"] is None


def test_merge_slot_acquire_then_release_hands_on_and_wakes_the_waiter(client, db):
    worker = Worker()
    opened(client, db, worker)
    held = worker.post(client, "merge-slot", POST_BODIES["merge-slot"]).json()
    assert held["held"]["generation"] == 1
    slot = slot_id("eng", "acme/widgets", "main")
    assert held["slot"] == slot
    now = datetime.now(timezone.utc)
    db.docs[f"merge_slots/{slot}"]["waiters"] = [
        {"task_id": "task_next", "submitted_at": now, "seen_at": now}]
    doc = seed_task(db, task_id="task_next", tenant_id="eng", state="PARKED",
                    park_reason="CI_PENDING")
    doc["metadata"] = {"merge_wait": {"code": "merge_slot_wait"}}
    released = worker.post(client, "merge-slot", {"op": "release", "base": "main",
                                                  "reason": "merged", "slot_generation": 1})
    assert released.status_code == 200, released.text
    assert released.json()["handed_to"] == "task_next" and released.json()["woke"] is True
    assert db.docs[f"merge_slots/{slot}"]["holder"]["task_id"] == "task_next"


def test_merge_train_register_derives_the_entry_from_the_tuple(client, db):
    worker = Worker()
    opened(client, db, worker)
    first = worker.post(client, "merge-train", POST_BODIES["merge-train"])
    assert first.status_code == 200, first.text
    entry = db.docs[f"merge_train_entries/{entry_id_for('eng', TASK)}"]
    assert entry["state"] == "queued" and entry["repository"] == "acme/widgets"
    assert entry["tenant_id"] == "eng" and entry["task_id"] == TASK
    again = worker.post(client, "merge-train", POST_BODIES["merge-train"]).json()
    assert again["entry"]["enqueued_at"] == first.json()["entry"]["enqueued_at"]
