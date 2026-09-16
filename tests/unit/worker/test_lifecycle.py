"""The ordinary path, and the two ways it is interrupted.

These are the tests that would catch a regression in the boring half of the
worker: that a successful attempt persists a terminal state, uploads what it
produced, reports what it used and gives the slot back -- in that order.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fakes import FakeSecretClient

from agent_worker.errors import ExitCode
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_attempt, seed_tenant


def test_successful_attempt_persists_state_uploads_and_releases(db, store, worker_factory):
    seed_attempt(
        db,
        pool_active=1,
        task_input={"prompt": "do the thing", "steps": 2, "sleep_seconds": 0.05},
    )
    worker, config, exporter = worker_factory()

    assert worker.run() == ExitCode.OK

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value
    assert task["completed_at"] is not None
    assert task["current_lease_id"] is None
    assert task["result_summary"]["runner"]["status"] == "succeeded"
    assert "mock runner completed 2/2 steps" in task["result_summary"]["runner"]["summary"]

    # Capacity came back.
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 0

    # The artifact and both logs are in the bucket under the deterministic path.
    keys = store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/")
    assert any(k.endswith("/artifacts/output.txt") for k in keys), keys
    assert any(k.endswith("/logs/stdout.log") for k in keys), keys
    assert any(k.endswith("/logs/stderr.log") for k in keys), keys

    # Ordered events, ending with the release.
    types = db.event_types("task_1")
    for expected in (EventType.STARTING.value, EventType.RUNNING.value,
                     EventType.CHECKPOINT_COMPLETED.value, EventType.SUCCEEDED.value,
                     EventType.LEASE_RELEASED.value):
        assert expected in types, f"{expected} missing from {types}"
    assert types.index(EventType.SUCCEEDED.value) < types.index(EventType.LEASE_RELEASED.value)


def test_attempt_records_resource_usage_for_the_sizing_report(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "measure me", "steps": 2, "sleep_seconds": 0.2,
                                 "cpu_burn_seconds": 0.2})
    worker, config, exporter = worker_factory()
    assert worker.run() == ExitCode.OK

    attempt = db.doc("attempts/att_1")
    assert attempt["peak_rss_bytes"] and attempt["peak_rss_bytes"] > 0
    assert attempt["peak_disk_bytes"] and attempt["peak_disk_bytes"] > 0
    assert attempt["oom_near_miss"] is False
    assert attempt["exit_code"] == 0
    assert attempt["backend"] == "CLOUD_RUN_JOB"

    assert exporter.exports, "resource usage must reach Cloud Monitoring"
    usage, labels = exporter.exports[-1]
    assert labels["runner_profile"] == "mock"
    assert labels["resource_class"] == "standard"
    assert labels["tenant_id"] == TENANT
    assert usage.peak_rss_bytes > 0


def test_cancellation_mid_run_stops_the_agent_and_releases(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "cancel me", "steps": 40, "sleep_seconds": 8.0})
    worker, _, _ = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def cancel() -> None:
        time.sleep(1.5)
        db.doc("tasks/task_1")["cancel_requested"] = True

    thread = threading.Thread(target=cancel)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.CANCELLED
    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("pools/global")["active"] == 0
    assert EventType.CANCELLED.value in db.event_types("task_1")


def test_cancellation_before_start_never_runs_the_agent(db, worker_factory, tmp_path):
    seed_attempt(db, cancel_requested=True)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.CANCELLED
    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value
    assert not (tmp_path / "workspace" / "att_1").exists()
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_deterministic_runner_failure_is_recorded_not_swallowed(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05,
                                 "fail": True, "fail_message": "the agent gave up"})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert "the agent gave up" in task["last_error"]
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_worker_env_does_not_leak_into_the_runner(db, worker_factory, monkeypatch, tmp_path):
    """A runner sees its workspace and its own credential -- nothing else."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/var/secrets/worker-sa.json")
    monkeypatch.setenv("SOME_OTHER_TENANT_KEY", "sk-not-yours")
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": "sk-ant-test-0123456789"})
    worker, config, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)

    from agent_worker import workspace as workspace_mod

    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    env = worker._build_child_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-0123456789"
    assert secrets.accessed == [f"swarm-tenant-{TENANT}-anthropic"]
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "SOME_OTHER_TENANT_KEY" not in env
    assert env["SWARM_WORK_DIR"] == str(worker.ws.work)
    assert env["HOME"] == str(worker.ws.work)


def test_provider_key_never_reaches_the_logs(db, worker_factory, log_stream, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({f"swarm-tenant-{TENANT}-anthropic": "sk-ant-supersecret-value"})
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)

    from agent_worker import workspace as workspace_mod

    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._build_child_env()
    worker.log.info("about to run with sk-ant-supersecret-value in the message")

    output = log_stream.getvalue()
    assert "sk-ant-supersecret-value" not in output
    assert "***REDACTED***" in output


def test_runner_input_is_the_task_input_plus_identifiers(db, worker_factory, tmp_path):
    seed_attempt(db, task_input={"prompt": "check the input", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    # The workspace is destroyed on exit, so assert through what the runner saw.
    summary = db.doc("tasks/task_1")["result_summary"]
    assert summary["runner"]["output"]["prompt"] == "check the input"
