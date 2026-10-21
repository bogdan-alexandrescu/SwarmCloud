"""A contended admission transaction skips one task, not the whole drain (DRAIN-ABORTED; #1822).

In the 48 h the 2026-10-21 system analysis read, 199 drains answered 503: 198
on `google.api_core.exceptions.Aborted` raised by a transactional READ (which
`firestore.transactional` does not retry) and 1 on the `ValueError` it raises
once every attempt was aborted. `_admit_one` caught only `AdmissionDenied`, so
one contended task ended the drain, and Pub/Sub redelivered the wake into the
same contention.

Pinned here, against the production `SchedulerStore` over the in-memory
Firestore the control-plane suite uses, with only `acquire_lease` made to fail:

  C-1  `Aborted` on the second of three tasks: the first and third are leased,
       the drain counts `contended == 1` and finishes.
  C-2  The `ValueError` retry-exhaustion form is treated the same, by its
       cause and by its message.
  C-3  `DeadlineExceeded` still propagates: a deadline may have committed.
  C-4  A pass where every candidate was contended ends normally and says so,
       and the HTTP drain answers 200 with that report.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    RecordingDispatcher,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from scheduler import loop as loop_mod  # noqa: E402
from scheduler.dispatch import BackendRouter  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

TENANT = "eng"
TASKS = ("task_c1", "task_c2", "task_c3")


def _exhausted_by_cause() -> ValueError:
    try:
        try:
            raise Aborted("409 too much contention on these documents")
        except Aborted as aborted:
            raise ValueError("Failed to commit transaction in 5 attempts.") from aborted
    except ValueError as exc:
        return exc


def _exhausted_by_message() -> ValueError:
    # The last attempt's error is not always an Aborted (a retried commit can
    # end on another retryable code); the message is what is left to read.
    return ValueError("Failed to commit transaction in 5 attempts.")


class ContendedStore(SchedulerStore):
    """The production store, with `acquire_lease` raising for chosen tasks.

    `failures` maps a task id to the exceptions its next admissions raise, one
    per call; once a task's list is empty it is admitted normally.
    """

    def __init__(self, db: FakeFirestore, failures: dict[str, list[BaseException]]) -> None:
        super().__init__(db)
        self.failures = failures
        self.calls: list[str] = []

    def acquire_lease(self, task, *, units, backend, config):  # type: ignore[override]
        self.calls.append(task.id)
        pending = self.failures.get(task.id)
        if pending:
            raise pending.pop(0)
        return super().acquire_lease(task, units=units, backend=backend, config=config)


@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    seed_tenant(store, TENANT, max_active=10)
    seed_pool(store, "global", hard_limit=10)
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for i, task_id in enumerate(TASKS):
        # Oldest first, so the drain tries them in this order.
        seed_task(db=store, task_id=task_id, tenant_id=TENANT, created_at=base + timedelta(seconds=i))
    return store


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(loop_mod, "_sleep", slept.append, raising=False)
    return slept


def _scheduler(store: SchedulerStore) -> tuple[Scheduler, RecordingDispatcher]:
    settings = scheduler_settings()
    dispatcher = RecordingDispatcher()
    scheduler = Scheduler(
        settings=settings,
        store=store,
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )
    return scheduler, dispatcher


def _state(db: FakeFirestore, task_id: str) -> str:
    return db.docs[f"tasks/{task_id}"]["state"]


# --------------------------------------------------------------------------
# C-1
# --------------------------------------------------------------------------

def test_an_aborted_admission_skips_that_task_and_the_drain_goes_on(db, pauses):
    store = ContendedStore(db, {"task_c2": [Aborted("409 contention")]})
    scheduler, dispatcher = _scheduler(store)

    report = scheduler.drain()

    # MUTATION: drop the `Aborted` catch in `_admit_one` and drain() raises
    # here, before task_c3 is ever tried -- the 198 503s of #1822.
    assert store.calls[:3] == list(TASKS), store.calls
    # MUTATION: return before counting, and contended stays 0.
    assert report.contended == 1
    started = {entry["task_id"] for entry in dispatcher.dispatched}
    # MUTATION: `break` instead of moving on, and task_c3 is never dispatched.
    assert {"task_c1", "task_c3"} <= started, started
    # MUTATION: write the task off (park, fail) instead of leaving it, and the
    # next pass never admits it. An aborted transaction committed nothing.
    assert "task_c2" in started, "the contended task was not left READY for a later pass"
    assert report.leased == 3
    # MUTATION: drop the backoff, or make it unbounded.
    assert len(pauses) == 1
    assert 0 < pauses[0] <= loop_mod.CONTENTION_BACKOFF_MAX_SECONDS <= 0.1
    assert report.to_dict()["contended"] == 1


# --------------------------------------------------------------------------
# C-2
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "make_error", [_exhausted_by_cause, _exhausted_by_message], ids=["cause", "message"]
)
def test_retry_exhaustion_is_contention_too(db, pauses, make_error):
    store = ContendedStore(db, {"task_c2": [make_error()]})
    scheduler, dispatcher = _scheduler(store)

    report = scheduler.drain()

    # MUTATION: catch `Aborted` alone and this ValueError ends the drain.
    assert report.contended == 1
    started = {entry["task_id"] for entry in dispatcher.dispatched}
    # MUTATION: stop the pass on it, and task_c3 is not dispatched.
    assert {"task_c1", "task_c2", "task_c3"} == started, started


def test_an_unrelated_value_error_still_propagates(db, pauses):
    store = ContendedStore(db, {"task_c2": [ValueError("some other defect")]})
    scheduler, _ = _scheduler(store)

    # MUTATION: catch every ValueError and a real defect is hidden as contention.
    with pytest.raises(ValueError, match="some other defect"):
        scheduler.drain()
    assert pauses == []


# --------------------------------------------------------------------------
# C-3
# --------------------------------------------------------------------------

def test_a_deadline_still_fails_the_drain(db, pauses):
    store = ContendedStore(db, {"task_c2": [DeadlineExceeded("504 deadline")]})
    scheduler, dispatcher = _scheduler(store)

    # MUTATION: widen the catch to DeadlineExceeded (or a bare Exception) and
    # this drain finishes: a transaction that may have committed is skipped as
    # if it wrote nothing.
    with pytest.raises(DeadlineExceeded):
        scheduler.drain()
    # MUTATION: a deadline is not contention, so it is never slept on.
    assert pauses == []
    assert [entry["task_id"] for entry in dispatcher.dispatched] == ["task_c1"]


# --------------------------------------------------------------------------
# C-4
# --------------------------------------------------------------------------

def _all_contended(db: FakeFirestore) -> ContendedStore:
    return ContendedStore(
        db, {task_id: [Aborted("409 contention")] for task_id in TASKS}
    )


def test_an_all_contended_pass_ends_normally_and_says_so(db, pauses):
    scheduler, dispatcher = _scheduler(_all_contended(db))

    report = scheduler.drain()

    # MUTATION: let the last contended task raise and drain() never returns.
    assert report.contended == 3
    assert report.leased == 0
    # MUTATION: report it as `no_admissible_work` and the report no longer
    # says the pass was lost to contention rather than to capacity.
    assert report.stop_reason == "all_contended"
    assert dispatcher.dispatched == []
    # Nothing committed: every task is still READY for the next wake or tick.
    assert {_state(db, task_id) for task_id in TASKS} == {"READY"}


def test_an_all_contended_drain_answers_200_with_its_report(db, pauses):
    from fastapi.testclient import TestClient

    from scheduler.main import create_app

    scheduler, _ = _scheduler(_all_contended(db))
    client = TestClient(create_app(scheduler))
    payload = base64.b64encode(json.dumps({"reason": "task_submitted"}).encode()).decode()

    response = client.post(
        "/pubsub/push",
        json={"message": {"data": payload, "messageId": "1"}, "subscription": "s"},
    )

    # MUTATION: let contention escape drain() and this is the 503 that made
    # Pub/Sub redeliver the wake into the same contention.
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["status"] == "ok"
    assert body["report"]["contended"] == 3
    assert body["report"]["stop_reason"] == "all_contended"
