"""One task's whole life over the state routes: checkpoint, park, resume, cancel, publish.

WHY (docs/design/agent-quality/trusted-state.md §4). `test_remote_control_plane.py`
holds each `RemoteControlPlane` method on its own; the owner's agent quality
program (3 November 2026) asks that the CYCLE works through the routes --
that what one attempt wrote is what the next one resumes from, that a
cancellation reaches the worker and ends the task, that publication (the
merge slot, the merge train) is this generation's alone -- with a stale
generation refused at every step after a newer one took over. The worker's
own `ChildApi` and `StateCall` speak to swarm-api's real app in process
(`state_routes_harness`); nothing here reaches Firestore except through a
route. Offline: no credentials, no emulator.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_worker.errors import FencedWriteRefused
from agent_worker.mergeslot import Held, RemoteMergeSlots
from control_plane.fakes import FakeFirestore as ApiFirestore
from state_routes_harness import (
    ATTEMPT,
    LEASE,
    TASK,
    TENANT,
    RoutesOpener,
    api_client,
    events,
    remote_control,
    seed_attempt,
)
from swarm_common.models import utcnow
from swarm_common.states import ParkReason, TaskState

DIGEST = "cd" * 32
REPO_URL = "https://github.com/acme/widgets.git"
NEXT_ATTEMPT = "att_remote_2"
NEXT_LEASE = "lease_remote_2"
CHECKPOINT = f"gs://bucket/tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/checkpoints/c1/"


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


@pytest.fixture
def opener(api_db) -> RoutesOpener:
    return RoutesOpener(api_client(api_db))


def _task(db: ApiFirestore) -> dict:
    return db.docs[f"tasks/{TASK}"]


def _first_attempt(api_db, opener, **extra):
    seed_attempt(api_db, repository_url=REPO_URL, **extra)
    control = remote_control(opener)
    control.validate_generation()
    control.advance_to_running()
    control.record_attempt_start(backend="cloud_run", execution_name=None)
    return control


def _requeued(api_db, opener, **extra):
    """The scheduler's next lease of the parked task: generation 2, its own
    attempt and lease, `latest_checkpoint` as the park left it."""
    seed_attempt(api_db, attempt_id=NEXT_ATTEMPT, lease_id=NEXT_LEASE, generation=2,
                 repository_url=REPO_URL, latest_checkpoint=_task(api_db)["latest_checkpoint"],
                 metadata=dict(_task(api_db).get("metadata") or {}), **extra)
    control = remote_control(opener, attempt_id=NEXT_ATTEMPT, lease_id=NEXT_LEASE, generation=2)
    control.validate_generation()
    return control


def test_a_checkpointed_park_is_resumed_by_the_next_generation_from_what_it_wrote(
        api_db, opener):
    first = _first_attempt(api_db, opener)
    first.record_checkpoint(checkpoint_id="c1", uri=CHECKPOINT, size_bytes=10, seq=1,
                            archive_sha256=DIGEST)
    uploads = [{"name": "notes.md", "key": f"tenants/{TENANT}/tasks/{TASK}/notes.md"}]
    first.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
               next_eligible_at=utcnow() + timedelta(minutes=5),
               detail={"provider": "anthropic", "artifacts": uploads})
    assert _task(api_db)["state"] == "PARKED"
    assert _task(api_db)["latest_checkpoint"] == CHECKPOINT

    second = _requeued(api_db, opener)

    # The restore's binding: the digest the first attempt recorded, served to
    # the second by a second `open` naming the attempt it restores.
    restored = second.fetch_attempt(ATTEMPT)
    assert restored is not None and restored["checkpoint_sha256"] == {"c1": DIGEST}
    assert second.parked_uploads() == [(ATTEMPT, uploads)]
    second.advance_to_running()
    assert _task(api_db)["state"] == "RUNNING" and _task(api_db)["current_generation"] == 2


def test_after_the_resume_the_parked_generation_is_refused_at_every_write(api_db, opener):
    first = _first_attempt(api_db, opener)
    first.record_checkpoint(checkpoint_id="c1", uri=CHECKPOINT, size_bytes=10, seq=1,
                            archive_sha256=DIGEST)
    first.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
               next_eligible_at=utcnow() + timedelta(minutes=5))
    second = _requeued(api_db, opener)
    second.advance_to_running()
    task_before = dict(_task(api_db))
    events_before = len(events(api_db))

    with pytest.raises(FencedWriteRefused):
        first.record_checkpoint(checkpoint_id="c2", uri=CHECKPOINT, size_bytes=1, seq=2,
                                archive_sha256=DIGEST)
    with pytest.raises(FencedWriteRefused):
        first.finish(state=TaskState.SUCCEEDED, exit_code=0)
    assert first.heartbeat() is False

    assert _task(api_db) == task_before
    assert len(events(api_db)) == events_before


def test_a_cancellation_reaches_the_worker_and_ends_the_task_cancelled(api_db, opener):
    control = _first_attempt(api_db, opener)
    # What swarm-api's cancel route sets on the task.
    _task(api_db)["cancel_requested"] = True

    signals = control.poll()
    assert signals.cancel_requested is True and not signals.is_fenced(1)
    control.finish(state=TaskState.CANCELLED, exit_code=None, error="cancelled",
                   end_cause=control.cancel_cause())

    task = _task(api_db)
    assert task["state"] == "CANCELLED"
    assert api_db.docs[f"leases/{LEASE}"]["released_at"] is not None
    assert events(api_db, kind="cancelled")


def test_a_human_park_counts_its_resume_and_the_next_generation_opens(api_db, opener):
    first = _first_attempt(api_db, opener, attempt_count=1)
    refunded, _requeued_now = first.park_human_required(
        detail={"question_wait": {"questions": 1}})
    assert refunded is True
    assert _task(api_db)["park_reason"] == "HUMAN_REQUIRED"
    assert _task(api_db)["metadata"]["human_wait_resumes"] == 1

    second = _requeued(api_db, opener)

    second.advance_to_running()
    assert _task(api_db)["metadata"]["human_wait_resumes"] == 1


def test_publication_is_the_current_generations_alone(api_db, opener):
    first = _first_attempt(api_db, opener)
    first.park(reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
               next_eligible_at=utcnow() + timedelta(minutes=5))
    second = _requeued(api_db, opener)
    second.advance_to_running()
    slots_before = {p: dict(d) for p, d in api_db.docs.items() if p.startswith("merge_slots/")}

    with pytest.raises(FencedWriteRefused):
        RemoteMergeSlots(first._calls, tenant_id=TENANT).acquire("main", updates=0)
    assert {p: dict(d) for p, d in api_db.docs.items()
            if p.startswith("merge_slots/")} == slots_before

    held, _woken = RemoteMergeSlots(second._calls, tenant_id=TENANT).acquire("main", updates=0)
    assert isinstance(held, Held)

