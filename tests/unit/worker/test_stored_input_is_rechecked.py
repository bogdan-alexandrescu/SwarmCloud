"""The worker re-checks a task's stored input against its profile (contract request 32, *Preconditions*).

swarm-api and the plugin's bridge refuse an input its profile does not
declare, at submission. Nothing under apps/agent-worker called the rule, so a
task written before its profile declared -- every `browser` and `generic`
task stored before request 32 -- or one that reached the store by any path
that skipped the submission check, ran on whatever it carried. The worker now
asks the same rule of the stored input, before it adds a key of its own and
before a credential is mounted, and a refusal exits 78 ("cannot start"): the
reconciler fails it without a retry, because every retry would be refused
the same way.

EVERY PROFILE, WITH NO EXEMPTION (owner decision on #345, 2026-09-29). An
earlier pass exempted the mock, because the worker's own unit suite seeded the
keys the mock reads to act out a provider -- `spend`, `provider`,
`credential_revoked_times` -- straight into the task document. Those tests
now hand the runner those keys through a test-only seam (`simulated`,
tests/unit/worker/conftest.py) that no caller has, so nothing here is
exempted and a mock task is held to its declaration like any other.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt, seed_tenant


def _task(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")


def _refused_before_the_runner(db: Any, runner_inputs: list, *names: str) -> str:
    """The task failed as cannot-start, naming each key, and no input.json was written."""
    task = _task(db)
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["end_cause"] == "cannot_start", task.get("end_cause")
    error = task.get("last_error") or ""
    for name in names:
        assert name in error, (name, error)
    assert "checked again by the worker" in error, error
    assert runner_inputs == [], "the runner's input.json was written for an input the worker refused"
    return error


# -- the control: a declared input runs ------------------------------------------


def test_a_mock_task_with_only_declared_keys_runs(db, worker_factory, runner_inputs):
    """Without this, every refusal below could be a worker that refuses everything."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01, "fail": False})
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    assert _task(db)["state"] == TaskState.SUCCEEDED.value


# -- the mock is not exempt --------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # What the lifecycle writes itself, or drops, as it builds input.json.
        ("model", "a-model-of-the-callers"),
        ("staged_inputs", [{"path": "caller.md"}]),
        ("expected_outputs", ["caller.md"]),
        ("attempt_count", 7),
        ("task_id", "task_someone_elses"),
        ("repository", {"url": "file:///elsewhere"}),
        # What the mock reads to act out a provider, withheld from callers on #142.
        ("spend", {"total_cost_usd": 0}),
        ("provider", "anthropic"),
        ("credential_revoked_times", 1),
        ("credential_detail", "revoked"),
        ("quota_detail", "limited"),
        ("reset_at", "2026-09-29T00:00:00Z"),
    ],
)
def test_a_mock_task_carrying_an_undeclared_key_cannot_start(
    db, worker_factory, runner_inputs, key, value
):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01, key: value})
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.CONFIG
    _refused_before_the_runner(db, runner_inputs, key)


def test_a_mock_value_out_of_its_declared_bounds_cannot_start(db, worker_factory, runner_inputs):
    """77 is the worker's own rate-limit code; the declaration refuses it as an `exit_code`."""
    seed_attempt(
        db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01, "fail": True, "exit_code": 77}
    )
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.CONFIG
    _refused_before_the_runner(db, runner_inputs, "exit_code")


def test_an_input_that_is_not_an_object_cannot_start(db, worker_factory, runner_inputs):
    seed_attempt(db)
    _task(db)["input"] = ["prompt", "x"]
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.CONFIG
    task = _task(db)
    assert task["end_cause"] == "cannot_start", task.get("end_cause")
    assert "not an object" in (task.get("last_error") or ""), task.get("last_error")
    assert runner_inputs == []


# -- the other profiles ------------------------------------------------------------


def test_a_claude_code_task_carrying_the_mocks_knobs_cannot_start(db, worker_factory, runner_inputs):
    """And before the credential: this tenant has none registered, which would
    PARK the task (CREDENTIAL_MISSING) if the worker reached that step. A
    refusal that came after it would be a credential mounted for an input the
    platform was always going to refuse."""
    seed_attempt(
        db, runner_profile="claude-code",
        task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01},
    )
    seed_tenant(db, credentials=[])
    worker, _, _ = worker_factory(runner_profile="claude-code")

    assert worker.run() == ExitCode.CONFIG
    _refused_before_the_runner(db, runner_inputs, "sleep_seconds", "steps")
    assert _task(db).get("park_reason") is None, "the task reached the credential step"


def test_a_generic_task_stored_without_its_command_cannot_start(db, worker_factory, runner_inputs):
    seed_attempt(db, runner_profile="generic", task_input={"prompt": "x", "paths": ["tests"]})
    worker, _, _ = worker_factory(runner_profile="generic")

    assert worker.run() == ExitCode.CONFIG
    _refused_before_the_runner(db, runner_inputs, "command")


def test_a_refused_url_is_named_by_its_key_never_echoed(db, worker_factory, runner_inputs):
    seed_attempt(
        db, runner_profile="browser",
        task_input={"prompt": "x", "url": "http://user:hunter2@example.com/"},
    )
    worker, _, _ = worker_factory(runner_profile="browser")

    assert worker.run() == ExitCode.CONFIG
    error = _refused_before_the_runner(db, runner_inputs, "url")
    assert "hunter2" not in error, "a refused URL's credential was stored as the task's error"
