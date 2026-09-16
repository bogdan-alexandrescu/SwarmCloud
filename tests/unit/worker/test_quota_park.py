"""Quota: the worker must never sleep through a long provider wait.

A worker blocked on a 30-minute rate limit holds a concurrency slot, 8 GiB of
memory and 4 vCPU, and on Cloud Run it is billed for all of it. Invariant 4 says
it checkpoints, parks the task with `next_eligible_at`, releases the lease and
exits -- and the release is the part these tests are really about, because a
parked task that kept its slot would be worse than one that slept.
"""

from __future__ import annotations

import pytest

from agent_worker.errors import ExitCode
from swarm_common.models import ProviderState
from swarm_common.states import EventType, ParkReason, TaskState

from conftest import TENANT, seed_attempt, seed_tenant


def test_quota_park_releases_the_lease(db, store, worker_factory):
    seed_attempt(
        db,
        pool_active=4,
        task_input={
            "prompt": "burn quota",
            "steps": 1,
            "sleep_seconds": 0.05,
            "quota_exhausted": True,
            "provider": "anthropic",
            "retry_after_seconds": 1800,
        },
    )
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.PARKED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    assert task["next_eligible_at"] is not None
    assert task["current_lease_id"] is None

    # The lease is released exactly once and every pool it held came back down.
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None
    assert lease["release_reason"].startswith("parked:")
    for pool in lease["pools"]:
        assert db.doc(f"pools/{pool}")["active"] == 3, pool

    # What the worker learned about the provider is published for the broker.
    quota = db.doc(f"quota/anthropic:{TENANT}")
    assert quota["state"] == ProviderState.EXHAUSTED.value
    assert quota["retry_after_seconds"] == 1800

    types = db.event_types("task_1")
    assert EventType.QUOTA_EXHAUSTED.value in types
    assert EventType.PARKED.value in types
    assert EventType.LEASE_RELEASED.value in types

    # Parking is only safe because the work was checkpointed first.
    manifests = [k for k in store.list_keys("tenants/") if k.endswith("manifest.json")]
    assert manifests, "a parked attempt must leave a checkpoint behind"


def test_park_happens_before_the_slot_is_returned(db, worker_factory):
    """Ordering: the task must leave the concurrency states before the pools
    are decremented, so no document ever claims a released slot is in use."""
    seed_attempt(
        db,
        task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05,
                    "quota_exhausted": True, "retry_after_seconds": 3600},
    )
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.PARKED

    paths = [path for _, path, _ in db.writes]
    state_write = max(
        i for i, (op, path, data) in enumerate(db.writes)
        if path == "tasks/task_1" and data.get("state") == TaskState.PARKED.value
    )
    pool_write = min(
        i for i, (op, path, data) in enumerate(db.writes)
        if path.startswith("pools/") and "active" in data
    )
    assert state_write < pool_write, "the slot was returned before the task stopped holding it"


def test_short_wait_retries_in_place_then_parks_when_it_keeps_failing(db, worker_factory):
    """A wait under the threshold is slept through; a provider that keeps
    saying no still ends in a park rather than an endless loop."""
    seed_attempt(
        db,
        task_input={
            "prompt": "flaky provider",
            "steps": 1,
            "sleep_seconds": 0.02,
            "quota_exhausted": True,
            "retry_after_seconds": 0,
        },
    )
    worker, _, _ = worker_factory(max_in_worker_retry_delay_seconds=45)

    assert worker.run() == ExitCode.PARKED
    retrying = [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]
    assert len(retrying) == 3, "bounded in-worker retries"
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_backpressure_from_the_control_plane_parks_a_running_task(db, worker_factory):
    """Another worker on the same tenant key exhausted the provider."""
    seed_attempt(
        db,
        runner_profile="claude-code",
        task_input={"prompt": "long running", "steps": 40, "sleep_seconds": 10.0},
    )
    seed_tenant(db, credentials=["anthropic"])
    db.seed(
        f"quota/anthropic:{TENANT}",
        {
            "provider": "anthropic",
            "tenant_id": TENANT,
            "state": ProviderState.EXHAUSTED.value,
            "retry_after_seconds": 2400,
            "updated_at": None,
        },
    )
    worker, _, _ = worker_factory(runner_profile="claude-code", timeout_seconds=30)

    # The pre-flight check fires before the agent is ever started, so no
    # provider call is made against a provider that already said no.
    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value][0]
    assert parked["detail"]["park_phase"] in ("preflight", "backpressure")
    assert parked["detail"]["source"] == "control_plane"


def test_missing_tenant_credential_parks_instead_of_failing(db, worker_factory):
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "needs a key", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=[])       # admin has not registered the key yet
    worker, _, _ = worker_factory(runner_profile="claude-code")

    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.CREDENTIAL_MISSING.value
    assert db.doc("leases/lease_1")["released_at"] is not None


def test_release_is_idempotent_under_a_double_call(db, worker_factory):
    seed_attempt(db, pool_active=2,
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05,
                             "quota_exhausted": True, "retry_after_seconds": 900})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.PARKED
    assert db.doc("pools/global")["active"] == 1

    # The reconciler racing the worker must not decrement a second time.
    worker.control._lease_released = False
    assert worker.control.release_lease("reconciler-sweep") is False
    assert db.doc("pools/global")["active"] == 1
