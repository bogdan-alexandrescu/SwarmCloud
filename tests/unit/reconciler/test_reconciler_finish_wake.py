"""A task the reconciler ends rings the finish wake, once (#636).

Left by #741 ("Not done" (a)): the worker rings `task_finished` after its own
terminal write, swarm-api after a cancel it ends itself, and the reconciler
after none. A task it ended -- CANCELLED for an overdue cancel (#627), FAILED
for a requeue whose attempts are spent -- had no one to ring for it, so its
dependants and the capacity the same repair released waited for the
scheduler's safety tick.

Held here:

    an overdue cancel the reconciler finishes rings one CANCELLED wake
    a lost worker whose attempts are spent rings one FAILED wake
    a requeue to READY rings nothing (nothing ended)
    a dry run, and a kill the backend does not confirm, ring nothing
    the next pass, with nothing left to repair, rings nothing
    a wake that is refused or raises never fails the pass
    the reconciler's request is the worker's, field for field

The tick staying the safety net for a lost wake is the scheduler's half, held
in tests/unit/control_plane/test_finish_wake_gaps.py
(`test_the_tick_still_cancels_the_chain_when_no_event_arrives`).
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeTransactionRunner
from test_cancel_overdue import running_execution, seed_cancelled_running

from reconciler.config import ReconcilerConfig
from reconciler.finishwake import PubSubFinishAnnouncer
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import TaskState


class RecordingAnnouncer:
    def __init__(self, answer: bool | Exception = True) -> None:
        self.calls: list[dict] = []
        self.answer = answer

    def announce(self, *, task_id: str, tenant_id: str, state: TaskState) -> bool:
        self.calls.append({"task_id": task_id, "tenant_id": tenant_id, "state": state})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="swarm-unit-test",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
        cancel_enforce_after_seconds=600,
    )


def build(db, config, announcer, *backends) -> tuple[Reconciler, io.StringIO]:
    stream = io.StringIO()
    logger = build_logger(stream=stream)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    reconciler = Reconciler(
        store=store,
        backends=list(backends),
        config=config,
        logger=logger,
        hold_releaser=None,
        finish_announcer=announcer,
    )
    return reconciler, stream


def _lost_worker(db, *, attempt_count: int) -> None:
    """The overdue-cancel seed without the cancel, its worker gone: the lease
    stopped beating long ago and its execution FAILED."""
    seed_cancelled_running(db, cancel_age=None)
    now = utcnow()
    db.doc("tasks/task_1").update(
        {"cancel_requested": False, "attempt_count": attempt_count, "max_attempts": 3}
    )
    db.doc("leases/lease_1").update(
        {
            "expires_at": now - timedelta(minutes=30),
            "heartbeat_at": now - timedelta(minutes=40),
        }
    )


def _dead_execution():
    return replace(running_execution(), phase=ExecutionPhase.FAILED)


# -- CANCELLED: an overdue cancel ----------------------------------------------


def test_an_overdue_cancel_the_reconciler_finishes_rings_once(db, config):
    seed_cancelled_running(db, cancel_age=3600)
    announcer = RecordingAnnouncer()
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)
    reconciler, _ = build(db, config, announcer, backend)

    reconciler.run_once()

    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value
    assert announcer.calls == [
        {"task_id": "task_1", "tenant_id": "eng", "state": TaskState.CANCELLED}
    ]

    # The next pass has nothing to repair, and rings nothing more.
    reconciler.run_once()
    assert len(announcer.calls) == 1


# -- FAILED: a lost worker on its last attempt ---------------------------------


def test_a_lost_worker_on_its_last_attempt_rings_one_failed_wake(db, config):
    _lost_worker(db, attempt_count=3)
    announcer = RecordingAnnouncer()
    backend = FakeBackend(executions=[_dead_execution()], journal=db.writes)
    reconciler, _ = build(db, config, announcer, backend)

    reconciler.run_once()

    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value
    assert announcer.calls == [
        {"task_id": "task_1", "tenant_id": "eng", "state": TaskState.FAILED}
    ]
    reconciler.run_once()
    assert len(announcer.calls) == 1


def test_a_requeue_to_ready_rings_nothing(db, config):
    _lost_worker(db, attempt_count=1)
    announcer = RecordingAnnouncer()
    backend = FakeBackend(executions=[_dead_execution()], journal=db.writes)
    reconciler, _ = build(db, config, announcer, backend)

    reconciler.run_once()

    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value
    assert announcer.calls == []


# -- nothing ended, nothing rung -----------------------------------------------


def test_a_dry_run_rings_nothing(db, config):
    seed_cancelled_running(db, cancel_age=3600)
    announcer = RecordingAnnouncer()
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)
    reconciler, _ = build(db, replace(config, dry_run=True), announcer, backend)

    reconciler.run_once()

    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert announcer.calls == []


def test_an_unconfirmed_kill_rings_nothing(db, config):
    seed_cancelled_running(db, cancel_age=3600)
    announcer = RecordingAnnouncer()
    backend = FakeBackend(
        executions=[running_execution()], journal=db.writes, terminate_returns=False
    )
    reconciler, _ = build(db, config, announcer, backend)

    reconciler.run_once()

    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert announcer.calls == []


# -- a lost wake never fails the pass ------------------------------------------


@pytest.mark.parametrize("answer", [False, RuntimeError("pubsub unavailable")])
def test_a_wake_that_is_refused_or_raises_does_not_fail_the_pass(db, config, answer):
    seed_cancelled_running(db, cancel_age=3600)
    announcer = RecordingAnnouncer(answer)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)
    reconciler, stream = build(db, config, announcer, backend)

    report = reconciler.run_once()

    assert report.errors == [], report.errors
    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value
    assert len(announcer.calls) == 1
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    wakes = [line for line in lines if line.get("message") == "finish wake"]
    assert len(wakes) == 1, wakes
    assert wakes[0]["outcome"] != "published"
    # The type only: a transport error's text can name the request.
    assert "pubsub unavailable" not in stream.getvalue()


def test_no_topic_means_no_announcer(monkeypatch):
    monkeypatch.delenv("DISPATCH_TOPIC", raising=False)
    assert PubSubFinishAnnouncer.from_env() is None
    monkeypatch.setenv("PROJECT_ID", "swarm-unit-test")
    monkeypatch.setenv("DISPATCH_TOPIC", "swarm-scheduler-wake")
    announcer = PubSubFinishAnnouncer.from_env()
    assert announcer is not None
    assert announcer.topic == "projects/swarm-unit-test/topics/swarm-scheduler-wake"


def test_a_reconciler_with_no_wake_topic_says_so_once_at_start(db, config):
    """A deployment missing DISPATCH_TOPIC rings no wake; the log line is the
    only sign of it, as for a missing QUOTA_BROKER_URL."""
    _, stream = build(db, config, None)
    assert stream.getvalue().count("DISPATCH_TOPIC unset") == 1

    _, stream = build(db, config, RecordingAnnouncer())
    assert "DISPATCH_TOPIC unset" not in stream.getvalue()


# -- the same request as the worker's ------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, float]] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.posts.append((url, json, timeout))

        class _Response:
            status_code = 200

        return _Response()


def test_the_reconcilers_request_is_the_workers_field_for_field():
    from agent_worker.finishwake import PubSubFinishAnnouncer as WorkerAnnouncer

    topic = "projects/swarm-unit-test/topics/swarm-scheduler-wake"
    ours, theirs = _Session(), _Session()
    for cls, session in ((PubSubFinishAnnouncer, ours), (WorkerAnnouncer, theirs)):
        assert cls(topic, session=session).announce(
            task_id="task_1", tenant_id="eng", state=TaskState.CANCELLED
        )

    assert ours.posts == theirs.posts
    [(_, body, _)] = ours.posts
    [message] = body["messages"]
    decoded = json.loads(base64.b64decode(message["data"]))
    assert decoded == message["attributes"] == {
        "reason": "task_finished",
        "task_id": "task_1",
        "tenant_id": "eng",
        "state": "CANCELLED",
    }
