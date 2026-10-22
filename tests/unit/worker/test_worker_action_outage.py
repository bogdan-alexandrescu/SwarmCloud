"""An outage inside a worker action exits 69 for a requeue, not FAILED (#2000).

A worker action (`merge`, `post-verdict`) runs as the callable `_prepare`
returns, AFTER `Worker._execute`'s startup try-block -- until #2000 the only
place `_unavailable_cause` was asked. So a Firestore ABORTED that outlasted
`firestore.transactional`'s own retries inside the merge slot's transaction
(`FirestoreMergeSlots.transact`, fenced on the task and lease the heartbeat
thread writes at the same time) reached `Worker._run`'s generic handler, and
the task was FAILED for good as runner_error with 9 of its 10 attempts unused
(task_aabde94cc0ea418188b8, 2026-10-22).

Now `_run_worker_action` classifies what its action raises: an error whose
chain `_unavailable_cause` recognises exits 69 (ExitCode.UNAVAILABLE) after a
best-effort write to this attempt's own document, and writes neither the task,
the lease nor an event, so the reconciler requeues the task while attempts
remain. Fenced, tenant-mismatch and every other WorkerError keep their own
handlers in `run`, and anything else still fails the task.

Every worker here is the production worker over in-memory Firestore, as in
test_worker_action_starts_no_runner.py. The slot test runs the REAL
`merge.run_merge` over `MergeWorld`'s green pull request, with the worker's
own `FirestoreMergeSlots` in its context and that class's `transact` raising
the ABORTED the issue quotes: the merge reaches `slot.acquire` exactly as in
production.

MUTATIONS: remove the classification in `_run_worker_action` -- the aborted,
post-verdict and chain tests read exit 1 and a FAILED task. Classify every
Exception as an outage -- the ValueError control reads 69 and a task left
RUNNING. Classify before re-raising WorkerError -- the ConfigError raised from
an Aborted reads 69 instead of its own CANNOT_START failure.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from google.api_core import exceptions as core

from agent_worker import lifecycle
from agent_worker import merge as merge_mod
from agent_worker import mergeslot
from agent_worker import post_verdict as post_verdict_mod
from agent_worker.errors import ConfigError, ExitCode, FencedError
from swarm_common.models import EndCause
from swarm_common.states import EventType, TaskState

from fakes import ExplodingChildProcess
from merge_world import MergeWorld
from worker_seeds import seed_attempt

CONTENTION = "Too much contention on these documents. Please try again."
TERMINAL = {TaskState.FAILED.value, TaskState.SUCCEEDED.value, TaskState.CANCELLED.value}


def _seed(db, profile: str) -> None:
    seed_attempt(db, runner_profile=profile, task_input={"prompt": "act"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    if profile == "merge":
        block: dict[str, Any] = {"strategy": "integrate", "carrier": "checkpoints",
                                 "merge_target": {"pull_request": "task_int"}}
    else:
        block = {"strategy": "single-pr", "carrier": "checkpoints", "pr_role": "none",
                 "verdict_source": {"review": "task_r"}}
    doc["metadata"] = {"dispatch": block}


@pytest.fixture
def no_runner(monkeypatch):
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    monkeypatch.setattr(lifecycle, "_runner_argv",
                        lambda _cfg: pytest.fail("a worker action asked for a runner argv"))


def _raising(monkeypatch, exc: BaseException) -> None:
    def run(ctx):
        raise exc

    monkeypatch.setattr(merge_mod, "run_merge", run)
    monkeypatch.setattr(post_verdict_mod, "run_post_verdict", run)


def _log_lines(log_stream) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_stream.getvalue().splitlines()
            if line.startswith("{")]


def _outage_line(log_stream) -> dict[str, Any]:
    [line] = [line for line in _log_lines(log_stream)
              if "inside the worker action" in str(line.get("message"))]
    return line


def _assert_left_for_the_reconciler(db, lease_before: dict[str, Any]) -> None:
    """Nothing terminal: the task is not ended, the lease is the worker's no more
    than it was, and no terminal event was written."""
    task = db.doc("tasks/task_1")
    assert task["state"] not in TERMINAL, f"an outage ended the task {task['state']}"
    assert task.get("completed_at") is None
    assert task.get("end_cause") is None
    lease = db.doc("leases/lease_1")
    assert lease.get("released_at") is None, "the worker released the lease on an outage"
    assert lease["generation"] == lease_before["generation"]
    events = db.event_types("task_1")
    for terminal in (EventType.FAILED.value, EventType.RETRYING.value,
                     EventType.PARKED.value):
        assert terminal not in events, f"an outage wrote a {terminal} event"


def _assert_attempt_recorded(db, cause: str) -> None:
    attempt = db.doc("attempts/att_1")
    assert attempt["exit_code"] == ExitCode.UNAVAILABLE == 69
    assert cause in attempt["error"]


def test_an_aborted_merge_slot_transaction_exits_69_and_writes_nothing_terminal(
    db, worker_factory, monkeypatch, no_runner, log_stream, tmp_path
):
    """The issue's reproduction: the real merge reaches `slot.acquire`, and the
    slot's fenced transaction raises the ABORTED Firestore gave up with."""
    _seed(db, "merge")
    world = MergeWorld(tmp_path)
    transacted: list[str] = []

    def aborted(self, slot, fn):
        transacted.append(slot)
        raise core.Aborted(CONTENTION)

    monkeypatch.setattr(mergeslot.FirestoreMergeSlots, "transact", aborted)
    real_run_merge = merge_mod.run_merge
    seen: list[Any] = []

    def run(ctx):
        # The worker's own slot store, over the worker's Firestore.
        seen.append(ctx.merge_slots)
        return real_run_merge(dataclasses.replace(
            world.context(), merge_slots=ctx.merge_slots, log=ctx.log))

    monkeypatch.setattr(merge_mod, "run_merge", run)
    worker, _, _ = worker_factory(runner_profile="merge")
    lease_before = dict(db.doc("leases/lease_1"))

    assert worker.run() == ExitCode.UNAVAILABLE

    assert isinstance(seen[0], mergeslot.FirestoreMergeSlots)
    assert transacted, "the merge never reached the slot's transaction"
    _assert_left_for_the_reconciler(db, lease_before)
    _assert_attempt_recorded(db, CONTENTION)
    line = _outage_line(log_stream)
    assert line["action"] == "merge"
    assert line["phase"] == "worker_action"
    assert line["exit_code"] == 69
    assert line["unavailable"].startswith("Aborted: ")
    # Which write it was: the frames of the action the error came up through.
    assert "acquire" in line["raised_in"] and "transact" in line["raised_in"]


def test_an_aborted_merge_action_exits_69_never_failed(
    db, worker_factory, monkeypatch, no_runner, log_stream
):
    _seed(db, "merge")
    _raising(monkeypatch, core.Aborted(CONTENTION))
    worker, _, _ = worker_factory(runner_profile="merge")
    lease_before = dict(db.doc("leases/lease_1"))

    assert worker.run() == ExitCode.UNAVAILABLE

    _assert_left_for_the_reconciler(db, lease_before)
    _assert_attempt_recorded(db, CONTENTION)
    assert db.doc("tasks/task_1").get("last_error") in (None, "")


def test_a_post_verdict_unavailable_exits_69(
    db, worker_factory, monkeypatch, no_runner, log_stream
):
    _seed(db, "post-verdict")
    _raising(monkeypatch, core.ServiceUnavailable("firestore is down"))
    worker, _, _ = worker_factory(runner_profile="post-verdict")
    lease_before = dict(db.doc("leases/lease_1"))

    assert worker.run() == ExitCode.UNAVAILABLE

    _assert_left_for_the_reconciler(db, lease_before)
    _assert_attempt_recorded(db, "firestore is down")
    assert _outage_line(log_stream)["action"] == "post_verdict"


def test_an_outage_found_in_the_chain_exits_69(
    db, worker_factory, monkeypatch, no_runner, log_stream
):
    _seed(db, "merge")
    try:
        try:
            raise core.Aborted(CONTENTION)
        except core.Aborted as inner:
            raise RuntimeError("the train entry could not be written") from inner
    except RuntimeError as outer:
        wrapped = outer
    _raising(monkeypatch, wrapped)
    worker, _, _ = worker_factory(runner_profile="merge")
    lease_before = dict(db.doc("leases/lease_1"))

    assert worker.run() == ExitCode.UNAVAILABLE

    _assert_left_for_the_reconciler(db, lease_before)
    _assert_attempt_recorded(db, CONTENTION)


def test_a_value_error_from_the_action_still_fails_runner_error(
    db, worker_factory, monkeypatch, no_runner
):
    """The control: an error that is not an outage ends the task as it always did."""
    _seed(db, "merge")
    _raising(monkeypatch, ValueError("the action's own bug"))
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.FAILED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == EndCause.RUNNER_ERROR.value
    assert EventType.FAILED.value in db.event_types("task_1")


def test_fenced_and_worker_errors_keep_their_handlers(
    db, worker_factory, monkeypatch, no_runner
):
    # A superseded attempt stands down: no terminal state, the lease untouched.
    _seed(db, "merge")
    _raising(monkeypatch, FencedError(1, 2, "merge slot"))
    worker, _, _ = worker_factory(runner_profile="merge")
    lease_before = dict(db.doc("leases/lease_1"))
    assert worker.run() == ExitCode.GENERATION_FENCED
    task = db.doc("tasks/task_1")
    assert task["state"] not in TERMINAL
    assert db.doc("leases/lease_1").get("released_at") == lease_before.get("released_at")
    assert db.doc("attempts/att_1").get("exit_code") != ExitCode.UNAVAILABLE


def test_a_worker_error_raised_from_an_outage_keeps_its_own_handler(
    db, worker_factory, monkeypatch, no_runner
):
    """A WorkerError is a deliberate exit even with an outage in its chain: it is
    re-raised before the chain is searched."""
    _seed(db, "merge")
    try:
        try:
            raise core.Aborted(CONTENTION)
        except core.Aborted as inner:
            raise ConfigError("the merge's configuration is refused") from inner
    except ConfigError as outer:
        wrapped = outer
    _raising(monkeypatch, wrapped)
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.CONFIG

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == EndCause.CANNOT_START.value
