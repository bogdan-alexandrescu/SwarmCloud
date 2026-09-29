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
"""

from __future__ import annotations

from typing import Any

from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt


def _task(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")


def test_a_generic_task_stored_without_its_command_cannot_start(db, worker_factory):
    seed_attempt(db, runner_profile="generic", task_input={"prompt": "x", "paths": ["tests"]})
    worker, _, _ = worker_factory(runner_profile="generic")

    assert worker.run() == ExitCode.CONFIG
    task = _task(db)
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "cannot_start", task.get("end_cause")
    assert "command" in (task.get("last_error") or ""), task.get("last_error")


def test_a_refused_url_is_named_by_its_key_never_echoed(db, worker_factory):
    seed_attempt(
        db, runner_profile="browser",
        task_input={"prompt": "x", "url": "http://user:hunter2@example.com/"},
    )
    worker, _, _ = worker_factory(runner_profile="browser")

    assert worker.run() == ExitCode.CONFIG
    error = _task(db).get("last_error") or ""
    assert "url" in error, error
    assert "hunter2" not in error, "a refused URL's credential was stored as the task's error"
