"""The reconciler may only terminate compute it can attribute to a task.

WHAT HAPPENED. `detect_orphan_executions` treated two different things as one
condition:

    if execution.task_id is None or task is None:
        reason = "execution carries no task this control plane knows about"

The first is "this is not a worker execution". The second is "this IS a worker
execution and its task is gone". Only the second is an orphan.

The dispatcher sets TASK_ID on every worker execution it creates, so an
execution without one was created by something else. The Cloud Run backend
selects jobs by the `swarm-` name prefix plus a platform label, and
`swarm-verify` -- the verification gate, created by terraform with
managed-by=swarm-terraform, swarm-env=dev, swarm-owner=platform -- matches both.

On 2026-09-21 the reconciler cancelled the gate's own executions four times, at
roughly four minutes each, while the task the gate was waiting on took five
minutes seventeen. The one run that would have proved the platform works was
killed by the platform. Confirmed in the audit log:

    Executions.CancelExecution
    swarm-reconciler@saga-agents-staging.iam.gserviceaccount.com
    .../jobs/swarm-verify/executions/swarm-verify-6q2wq

AND WHY IT MATTERS MORE THAN AN OWN GOAL. saga-agents-staging is SHARED with
another team. The only things between this loop and one of their Cloud Run jobs
are a name prefix and a label, neither of which they are obliged to avoid.
CLAUDE.md's second non-negotiable rule is that nothing here may modify their
resources; a terminate path that fires on anything it cannot attribute to a
task is one unlucky name away from breaking it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reconciler.detect import FindingKind, detect_orphan_executions

NOW = datetime(2026, 9, 21, 23, 0, 0, tzinfo=timezone.utc)


class _Config:
    orphan_execution_grace_seconds = 60
    logger = None


class _Execution:
    is_active = True

    def __init__(self, *, task_id, attempt_id="att-1", generation=None, name="x", age=600):
        self.task_id = task_id
        self.tenant_id = "u-sw-c90291"
        self.backend = "cloud_run"
        self.attempt_id = attempt_id
        self.generation = generation
        self.name = name
        self.parent = "projects/p/locations/l/jobs/swarm-verify"
        self.created_at = NOW - timedelta(seconds=age)


class _Snapshot:
    def __init__(self, tasks=None, leases=None):
        self.tasks = tasks or {}
        self.leases = leases or {}


def _findings(execution, snapshot=None):
    return detect_orphan_executions(
        snapshot or _Snapshot(), [execution], _Config(), now=NOW
    )


def test_an_execution_with_no_task_id_is_left_alone():
    """THE regression. This is the verification gate, and anything else in the
    project whose job name happens to start with `swarm-`."""
    assert _findings(_Execution(task_id=None, name="swarm-verify-6q2wq")) == []


def test_an_execution_with_no_task_id_is_left_alone_even_when_old():
    """Age is what promotes a thing to orphan. It must not promote a thing this
    service has no authority over."""
    assert _findings(_Execution(task_id=None, age=86400)) == []


def test_a_worker_execution_whose_task_is_gone_is_still_an_orphan():
    """The behaviour that must NOT be lost: a real worker whose task vanished
    is exactly what this detector is for."""
    found = _findings(_Execution(task_id="task_abc"))
    assert len(found) == 1
    assert found[0].kind is FindingKind.ORPHAN_EXECUTION
    assert "no task this control plane knows about" in found[0].reason


def test_a_worker_execution_inside_the_grace_window_is_not_yet_an_orphan():
    """The dispatcher may not have written the lease yet."""
    assert _findings(_Execution(task_id="task_abc", age=5)) == []


def test_the_no_task_id_check_runs_before_the_task_lookup():
    """Ordering is the fix. If the lookup ran first, `tasks.get(None)` returns
    None and the execution falls into the orphan branch anyway -- which is the
    bug, reintroduced."""
    class _ExplodingTasks(dict):
        def get(self, key, default=None):  # pragma: no cover - must not be called
            raise AssertionError(f"the task table was consulted for {key!r}")

    snapshot = _Snapshot(tasks=_ExplodingTasks())
    assert _findings(_Execution(task_id=None), snapshot) == []
