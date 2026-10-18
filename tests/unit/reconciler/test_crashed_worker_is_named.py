"""A worker that crashed is reclaimed as crashed, with its exit code, not as "lost" (#1438).

WHAT WAS WRONG. On 2026-10-16 every claude-code and indexer step that hit #1438
ended FAILED reading "reconciled: lease silent for ~100 s ... its execution has
ended (failed): the worker is lost". The execution had exited 1 ("container
worker: Error") -- the reconciler's own "read how an execution ended" line said
so, in the same pass -- and the worker had logged `worker crashed`. "Lost" told
the owner nothing; "exit code 1 ... crashed" would have pointed at the bug.

WHAT EACH TEST PINS:

  * a FAILED execution whose termination read holds a non-zero exit code is
    named by that code and the backend's detail, and "the worker is lost" is
    gone; the cause stays `lost_worker` (the frozen EndCause has no better one);
  * a read with no exit code, an exit code of 0, a CANCELLED execution, an
    eviction, or no read at all keeps today's words;
  * the exit code is on the finding's detail, from the read already made.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore,
with the scripted GKE backend of the eviction suite; the rule's helper is also
called directly for the cases no backend lists as a cannot-start candidate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_worker_evicted import (
    ATTEMPT,
    TASK,
    reconcile,
    seed_a_running_attempt,
)

from reconciler.config import ReconcilerConfig
from reconciler.detect import _crash_exit, _ended_words
from reconciler.model import ExecutionPhase, ExecutionView, Termination
from swarm_common.models import EndCause, utcnow
from swarm_common.states import TaskState


@pytest.fixture
def reconciler_config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def test_a_worker_that_exited_1_is_reclaimed_as_crashed(db, reconciler_config):
    """R10's attempt 1, as the reconciler read it on 2026-10-16."""
    seed_a_running_attempt(db, heartbeat_seconds_ago=101)

    report, _ = reconcile(
        db, reconciler_config, Termination(exit_code=1, detail="container worker: Error")
    )

    assert [o.kind for o in report.outcomes] == ["stale_lease"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    error = task["last_error"]
    assert (
        "its execution ended with exit code 1 (container worker: Error): "
        "the worker crashed before writing its end"
    ) in error, error
    assert "the worker is lost" not in error, error
    assert task["state"] == TaskState.READY.value, task
    assert task.get("end_cause") in (None, EndCause.LOST_WORKER.value), task


def test_a_crash_that_spends_the_last_attempt_keeps_the_lost_worker_cause(
    db, reconciler_config
):
    seed_a_running_attempt(db, attempt_count=3, max_attempts=3, heartbeat_seconds_ago=101)

    reconcile(db, reconciler_config, Termination(exit_code=1, detail="container worker: Error"))

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    assert task["end_cause"] == EndCause.LOST_WORKER.value, task
    assert "exit code 1" in task["last_error"], task["last_error"]


def test_a_read_with_no_exit_code_keeps_the_lost_words(db, reconciler_config):
    seed_a_running_attempt(db, heartbeat_seconds_ago=101)

    reconcile(
        db,
        reconciler_config,
        Termination(exit_code=None, detail="the backend recorded no exit code for this execution"),
    )

    error = db.doc(f"tasks/{TASK}")["last_error"]
    assert "its execution has ended (failed): the worker is lost" in error, error
    assert "crashed" not in error, error


# ---------------------------------------------------------------------------
# The words, pure
# ---------------------------------------------------------------------------


def _ended(phase: ExecutionPhase) -> ExecutionView:
    return ExecutionView(
        name="swarm-ddfff9b902a44333b294-1",
        backend="GKE_AUTOPILOT",
        phase=phase,
        created_at=utcnow(),
        task_id=TASK,
        attempt_id=ATTEMPT,
        generation=1,
        completed_at=utcnow(),
        ended=True,
    )


def _snapshot(termination: Termination | None) -> SimpleNamespace:
    return SimpleNamespace(terminations={} if termination is None else {ATTEMPT: termination})


def test_the_crash_words_carry_the_exit_code_and_the_backends_detail():
    ended = _ended(ExecutionPhase.FAILED)
    crash = _crash_exit(
        _snapshot(Termination(exit_code=1, detail="container worker: Error")), ATTEMPT, ended
    )

    assert crash == (1, "container worker: Error")
    assert _ended_words(ended, crash) == (
        "; its execution ended with exit code 1 (container worker: Error): "
        "the worker crashed before writing its end"
    )


def test_a_crash_with_no_detail_says_only_its_exit_code():
    ended = _ended(ExecutionPhase.FAILED)
    crash = _crash_exit(_snapshot(Termination(exit_code=137, detail="")), ATTEMPT, ended)

    assert _ended_words(ended, crash) == (
        "; its execution ended with exit code 137: the worker crashed before writing its end"
    )


@pytest.mark.parametrize(
    ("phase", "termination"),
    [
        (ExecutionPhase.FAILED, None),
        (ExecutionPhase.FAILED, Termination(exit_code=None, detail="no exit code")),
        (ExecutionPhase.FAILED, Termination(exit_code=0, detail="Completed")),
        (
            ExecutionPhase.FAILED,
            Termination(exit_code=137, detail="ContainerStatusUnknown", eviction="Evicted"),
        ),
        (ExecutionPhase.CANCELLED, Termination(exit_code=1, detail="container worker: Error")),
    ],
    ids=["not-read", "no-exit-code", "exit-0", "evicted", "cancelled"],
)
def test_anything_short_of_a_known_crash_keeps_todays_words(phase, termination):
    ended = _ended(phase)
    crash = _crash_exit(_snapshot(termination), ATTEMPT, ended)

    assert crash is None
    assert _ended_words(ended, crash) == (
        f"; its execution has ended ({phase.value.lower()}): the worker is lost"
    )
