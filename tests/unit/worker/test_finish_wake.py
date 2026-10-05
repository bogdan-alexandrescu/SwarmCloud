"""The worker rings the scheduler when it ends a task (#636).

Parent done -> child READY took p50 27 s (2026-10-05 history report §2.4)
because nothing told the scheduler a task had ended: its dependants waited for
the one-minute safety tick. `ControlPlane.finish`, and `fail_retryably` when it
ends the task, now publish a `task_finished` wake on the scheduler's existing
wake topic, AFTER the terminal write and the lease release -- so the scheduler,
woken, sees the parent ended and the capacity back.

Pinned here:

    a success publishes one wake, naming the task, after the lease is released
    a failure publishes one too: its dependants are cancelled, not left parked
    a retryable failure that returns the task to READY publishes nothing
    a fenced attempt publishes nothing
    a wake that fails changes nothing the worker did; it is logged, not raised
    the Pub/Sub request carries identifiers only, to a full topic path
    DISPATCH_TOPIC reaches WorkerConfig; unset means no wake is published
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agent_worker.config import WorkerConfig
from agent_worker.control import ControlPlane, FencedWriteRefused
from agent_worker.errors import ExitCode
from agent_worker.finishwake import PubSubFinishAnnouncer, topic_path
from agent_worker.logs import build_logger
from swarm_common.states import TaskState

from conftest import PROJECT, TENANT, seed_attempt
from fakes import FakeTransactionRunner


class RecordingAnnouncer:
    def __init__(self, db: Any | None = None, *, fail: bool = False) -> None:
        self._db = db
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    def announce(self, *, task_id: str, tenant_id: str, state: TaskState) -> bool:
        lease = self._db.doc("leases/lease_1") if self._db is not None else None
        self.calls.append(
            {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "state": state,
                "lease_released": bool(lease and lease.get("released_at") is not None),
            }
        )
        if self._fail:
            raise RuntimeError("pubsub unavailable")
        return True


def _control(db: Any, announcer: Any, log_stream: Any) -> ControlPlane:
    logger = build_logger(
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=1,
        runner_profile="mock",
        stream=log_stream,
    )
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=1,
        logger=logger,
        txn_runner=FakeTransactionRunner(db),
        finish_announcer=announcer,
    )


def test_a_success_publishes_one_wake_after_the_lease_is_released(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    announcer = RecordingAnnouncer(db)
    worker, _, _ = worker_factory(finish_announcer=announcer)

    assert worker.run() == ExitCode.OK

    assert announcer.calls == [
        {
            "task_id": "task_1",
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED,
            "lease_released": True,
        }
    ]


def test_a_failure_publishes_a_wake_too(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01,
                                 "fail": True, "fail_message": "gave up"},
                 attempt_count=3)
    announcer = RecordingAnnouncer(db)
    worker, _, _ = worker_factory(finish_announcer=announcer)

    assert worker.run() == ExitCode.FAILED

    assert [call["state"] for call in announcer.calls] == [TaskState.FAILED]
    assert announcer.calls[0]["lease_released"] is True


def test_a_retryable_failure_back_to_ready_publishes_nothing(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=1)
    announcer = RecordingAnnouncer(db)
    control = _control(db, announcer, log_stream)

    target = control.fail_retryably(exit_code=1, error="flaky", cause="runner_error")

    assert target is TaskState.READY
    assert announcer.calls == []


def test_a_retryable_failure_that_ends_the_task_publishes_one_wake(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING, attempt_count=3)
    announcer = RecordingAnnouncer(db)
    control = _control(db, announcer, log_stream)

    target = control.fail_retryably(exit_code=1, error="flaky", cause="runner_error")

    assert target is TaskState.FAILED
    assert [call["state"] for call in announcer.calls] == [TaskState.FAILED]
    assert announcer.calls[0]["lease_released"] is True


def test_a_fenced_attempt_publishes_nothing(db, log_stream):
    seed_attempt(db, state=TaskState.RUNNING, task_generation=2)
    announcer = RecordingAnnouncer(db)
    control = _control(db, announcer, log_stream)

    with pytest.raises(FencedWriteRefused):
        control.finish(state=TaskState.SUCCEEDED, exit_code=0)
    assert announcer.calls == []


def test_a_wake_that_fails_changes_nothing_the_worker_did(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    announcer = RecordingAnnouncer(db, fail=True)
    worker, _, _ = worker_factory(finish_announcer=announcer)

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert len(announcer.calls) == 1


# -- the Pub/Sub request -------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    def __init__(self, status_code: int = 200, *, raises: bool = False) -> None:
        self.status_code = status_code
        self.raises = raises
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, json: Any, timeout: float) -> FakeResponse:
        self.requests.append({"url": url, "json": json, "timeout": timeout})
        if self.raises:
            raise ConnectionError("no route")
        return FakeResponse(self.status_code)


def test_the_topic_path_is_built_from_a_bare_name():
    assert topic_path("p1", "swarm-scheduler-wake") == "projects/p1/topics/swarm-scheduler-wake"
    assert topic_path("p1", "projects/p2/topics/t") == "projects/p2/topics/t"
    assert topic_path("p1", "  ") == ""


def test_the_wake_carries_identifiers_only_to_the_publish_endpoint():
    session = FakeSession()
    announcer = PubSubFinishAnnouncer("projects/p1/topics/wake", session=session)

    assert announcer.announce(task_id="task_1", tenant_id="eng", state=TaskState.SUCCEEDED)

    (request,) = session.requests
    assert request["url"] == "https://pubsub.googleapis.com/v1/projects/p1/topics/wake:publish"
    (message,) = request["json"]["messages"]
    expected = {
        "reason": "task_finished",
        "task_id": "task_1",
        "tenant_id": "eng",
        "state": "SUCCEEDED",
    }
    assert message["attributes"] == expected
    assert json.loads(base64.b64decode(message["data"])) == expected
    assert 0 < request["timeout"] <= 10


@pytest.mark.parametrize("session", [FakeSession(403), FakeSession(raises=True)])
def test_a_refused_or_unreachable_publish_returns_false_and_never_raises(session):
    announcer = PubSubFinishAnnouncer("projects/p1/topics/wake", session=session)
    assert announcer.announce(task_id="task_1", tenant_id="eng", state=TaskState.FAILED) is False


def test_for_topic_builds_nothing_without_a_topic():
    assert PubSubFinishAnnouncer.for_topic(PROJECT, None) is None
    assert PubSubFinishAnnouncer.for_topic(PROJECT, "") is None
    built = PubSubFinishAnnouncer.for_topic(PROJECT, "swarm-scheduler-wake")
    assert built is not None
    assert built.topic == f"projects/{PROJECT}/topics/swarm-scheduler-wake"


def test_dispatch_topic_reaches_the_worker_config(monkeypatch):
    for name, value in {
        "TASK_ID": "task_1",
        "ATTEMPT_ID": "att_1",
        "LEASE_ID": "lease_1",
        "TENANT_ID": TENANT,
        "RUNNER_PROFILE": "mock",
        "PROJECT_ID": PROJECT,
        "GENERATION": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DISPATCH_TOPIC", raising=False)
    assert WorkerConfig.from_env().wake_topic is None
    monkeypatch.setenv("DISPATCH_TOPIC", " swarm-scheduler-wake ")
    assert WorkerConfig.from_env().wake_topic == "swarm-scheduler-wake"
