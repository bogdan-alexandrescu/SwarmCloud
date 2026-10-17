"""A terminal write Firestore refuses is written once more with a minimal summary (#1438).

MEASURED 2026-10-16: `ControlPlane.finish` raised InvalidArgument ("400
Property result_summary contains an invalid nested entity.") out of
`_record_finalised` as "worker crashed"; the process exited 1, the crash
path's own write was refused the same way, and the attempt ended ~100 s later
only when the reconciler reclaimed it as a lost worker -- and was retried.

The property, driven through the real lifecycle over the in-memory Firestore:

    the attempt decided SUCCEEDED     it ends SUCCEEDED, exit 0, lease released
    the runner failed                 it ends FAILED as runner_error, no crash
    a pull-request step published     `_fail_for_published_nothing` ends it
      nothing                           FAILED as outputs_missing
    the minimal write refused too     today's behaviour: no terminal state
    fenced while falling back         stands down, writes nothing (invariant 5)
    a FencedWriteRefused, or any      never written a second time
      other error

Firestore is faked by refusing, with the real `InvalidArgument`, any write
whose `result_summary` holds `MARKER` -- the mock runner's prompt, which its
output carries into the summary the way the pre-publish gate's rounds did.

WRITTEN TO FAIL BEFORE THE CHANGE: there, the first refusal crashed the
attempt and the crash path's write was refused too, so no test below that
expects a terminal state found one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from google.api_core import exceptions as core

import fakes
from agent_worker.errors import ExitCode, FencedWriteRefused
from swarm_common.states import EventType, TaskState

from worker_seeds import seed_attempt

MARKER = "a-value-firestore-refuses-" + "z" * 8
REFUSAL = "Property result_summary contains an invalid nested entity."


class _Refusals:
    def __init__(self) -> None:
        self.marker = MARKER
        self.message = REFUSAL
        self.refused = 0
        #: Called on each refusal, before it is raised: a test that models a
        #: fence landing between the two writes sets it.
        self.on_refusal: Any = None


@pytest.fixture
def refusals(monkeypatch) -> _Refusals:
    state = _Refusals()
    real_update = fakes.FakeDocumentRef.update
    real_set = fakes.FakeDocumentRef.set

    def refuse(ref: Any, data: dict[str, Any]) -> None:
        summary = data.get("result_summary") if isinstance(data, dict) else None
        if summary is not None and state.marker in json.dumps(summary, default=str):
            state.refused += 1
            if state.on_refusal is not None:
                state.on_refusal(ref)
            raise core.InvalidArgument(state.message)

    def update(self: Any, data: dict[str, Any], **kwargs: Any) -> None:
        refuse(self, data)
        return real_update(self, data, **kwargs)

    def set_(self: Any, data: dict[str, Any], merge: bool = False, **kwargs: Any) -> None:
        refuse(self, data)
        return real_set(self, data, merge=merge, **kwargs)

    monkeypatch.setattr(fakes.FakeDocumentRef, "update", update)
    monkeypatch.setattr(fakes.FakeDocumentRef, "set", set_)
    return state


def _seed(db: Any, **task_input: Any) -> None:
    seed_attempt(db, task_input={"prompt": MARKER, "steps": 1, "sleep_seconds": 0.01,
                                 **task_input})


def _lease_releases(db: Any) -> int:
    return sum(
        1
        for kind, path, data in db.writes
        if path == "leases/lease_1" and data.get("released_at") is not None
    )


def _log_lines(log_stream: Any, message: str) -> list[dict[str, Any]]:
    lines = []
    for raw in log_stream.getvalue().splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("message") == message:
            lines.append(entry)
    return lines


FELL_BACK = (
    "the terminal write was refused; writing the same state once more with a minimal summary"
)


def test_a_refused_success_still_ends_succeeded_with_a_minimal_summary(
    db, worker_factory, refusals, log_stream
):
    _seed(db)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task["state"]
    summary = task["result_summary"]
    assert summary["state"] == "SUCCEEDED"
    assert summary["summary_refused"] == f"400 {REFUSAL}"
    assert MARKER not in json.dumps(summary, default=str), "the refused summary was kept"
    assert refusals.refused == 1, "the full summary was not tried first, or was tried again"
    assert _lease_releases(db) == 1, "the lease was not released exactly once"
    assert db.event_types("task_1").count(EventType.SUCCEEDED.value) == 1
    assert not _log_lines(log_stream, "worker crashed"), "the refusal still crashed the worker"
    logged = _log_lines(log_stream, FELL_BACK)
    assert len(logged) == 1
    assert logged[0]["error_type"] == "InvalidArgument"
    assert MARKER not in json.dumps(logged[0]), "the log line carried the summary"


def test_the_refusal_kept_in_the_summary_is_cut_to_300_characters(
    db, worker_factory, refusals
):
    refusals.message = "Property result_summary is too large: " + "y" * 1000
    _seed(db)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    refused = db.doc("tasks/task_1")["result_summary"]["summary_refused"]
    assert len(refused) == 300
    assert refused.startswith("400 Property result_summary is too large")


def test_a_refused_runner_failure_still_ends_as_the_attempt_decided(
    db, worker_factory, refusals, log_stream
):
    # A failed run's output carries no prompt, so the refusal keys on the
    # runner block, which only the full summary has.
    refusals.marker = '"runner":'
    _seed(db, fail=True, fail_message="the agent gave up")
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "runner_error"
    assert "the agent gave up" in (task["last_error"] or "")
    assert task["result_summary"]["error"] == task["last_error"]
    assert "summary_refused" in task["result_summary"]
    assert _lease_releases(db) == 1
    assert not _log_lines(log_stream, "worker crashed"), "the refusal still crashed the worker"


def test_a_refused_published_nothing_ends_failed_and_keeps_the_branch(
    db, worker_factory, refusals
):
    """`_fail_for_published_nothing`, the write the 2026-10-16 retries crashed
    in. The branch and the pull request the attempt knew survive, by their
    scalar fields."""
    seed_attempt(db)
    worker, _, _ = worker_factory()
    summary = {
        "runner": {"output": {"prompt": MARKER, "rounds": [[{"check": "ruff"}]]}},
        "branch": {"name": "swarm/task_1", "head": "a" * 40, "commits": [{"sha": "a" * 40}]},
        "git": {"pull_request": {"number": 7, "url": "https://github.com/acme/w/pull/7",
                                 "state": "open", "appended": ["x"]}},
    }

    outcome = worker._fail_for_published_nothing("push failed", summary, exit_code=0)

    assert outcome.state is TaskState.FAILED and outcome.exit_code == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "outputs_missing"
    stored = task["result_summary"]
    assert stored["branch"] == {"name": "swarm/task_1", "head": "a" * 40}
    assert stored["pull_request"] == {
        "number": 7, "url": "https://github.com/acme/w/pull/7", "state": "open"
    }
    assert stored["error"] == "push failed"
    assert _lease_releases(db) == 1


def test_a_minimal_write_refused_too_keeps_todays_behaviour(
    db, worker_factory, refusals, log_stream
):
    refusals.marker = ""  # every write that carries a summary at all
    _seed(db)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] not in (TaskState.SUCCEEDED.value, TaskState.FAILED.value)
    assert _lease_releases(db) == 0, "a lease was released with no terminal state"
    assert _log_lines(log_stream, "worker crashed")
    assert _log_lines(log_stream, "the minimal terminal write was refused too")


def test_a_fence_landing_between_the_writes_stands_the_worker_down(
    db, worker_factory, refusals
):
    """The minimal write goes through the same fenced transition: a newer
    generation that took the task meanwhile is never written over."""

    def fence(ref: Any) -> None:
        db.documents["tasks/task_1"]["current_generation"] = 2

    refusals.on_refusal = fence
    _seed(db)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.GENERATION_FENCED
    task = db.doc("tasks/task_1")
    assert task["state"] not in (TaskState.SUCCEEDED.value, TaskState.FAILED.value)
    assert "result_summary" not in task
    assert refusals.refused == 1
    assert _lease_releases(db) == 0, "a fenced attempt touched the lease"


@pytest.mark.parametrize(
    "error",
    [FencedWriteRefused(1, 2, write="terminal"), RuntimeError("the store is down")],
    ids=["fenced", "not-a-refusal"],
)
def test_nothing_but_invalid_argument_is_written_a_second_time(
    db, worker_factory, monkeypatch, error
):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    calls: list[dict[str, Any]] = []

    def finish(**kwargs: Any) -> None:
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(worker.control, "finish", finish)
    with pytest.raises(type(error)):
        worker._finish_terminal(
            state=TaskState.SUCCEEDED, exit_code=0, result_summary={"prompt": MARKER}
        )
    assert len(calls) == 1
