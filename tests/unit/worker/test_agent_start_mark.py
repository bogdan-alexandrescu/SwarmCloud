"""The worker records when the agent process actually started (#667, lane OB1).

Measured by the chunk-1 observer on 2026-10-06: dispatch -> container start
was 62-65 s, while dispatch -> agent running was 107-141 s. The attempt's
`started_at` is written BEFORE the checkpoint restore, the clone, the
credential fetch and the account hold, so nothing recorded the moment the
agent itself began, and the 45-75 s between could only be inferred.

`agent_started` is a RUNNING event, written once per attempt at the first
runner start -- an in-place restart is not a second start -- and its `at` is
that moment.
"""

from __future__ import annotations

from datetime import datetime

from agent_worker.errors import ExitCode
from swarm_common.states import EventType, TaskState

from conftest import seed_attempt

AGENT_STARTED = "agent_started"


def _marks(db) -> list[dict]:
    return [
        event for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == AGENT_STARTED
    ]


def _at(event: dict) -> datetime:
    at = event["at"]
    return at if isinstance(at, datetime) else datetime.fromisoformat(str(at))


def test_the_agent_start_is_one_running_event_after_the_attempt_started(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value

    (mark,) = _marks(db)
    assert mark["type"] == EventType.RUNNING.value
    assert mark["attempt_id"] == "att_1" and mark["generation"] == 1
    detail = mark["detail"]
    assert isinstance(detail["seconds_since_process_start"], float)
    assert detail["seconds_since_process_start"] >= 0
    assert detail["cloned"] is False

    attempt = db.doc("attempts/att_1")
    started = attempt["started_at"]
    started = started if isinstance(started, datetime) else datetime.fromisoformat(str(started))
    assert _at(mark) >= started, "the agent cannot start before the attempt did"

    # After the walk to RUNNING: the state's own event comes first.
    running = [
        event for event in db.events("task_1")
        if event["type"] == EventType.RUNNING.value and not (event.get("detail") or {})
    ]
    assert running and _at(running[0]) <= _at(mark)


def test_an_in_place_restart_is_not_a_second_agent_start(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._mark_agent_started()
    worker._mark_agent_started()
    assert len(_marks(db)) == 1


def test_a_fenced_worker_writes_no_agent_start(db, worker_factory):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._fenced_exit = True
    worker._mark_agent_started()
    assert _marks(db) == []
