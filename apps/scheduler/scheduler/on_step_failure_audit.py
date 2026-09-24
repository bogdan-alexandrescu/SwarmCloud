"""What would the next drain cancel under `on_step_failure: fail_workflow`? Read-only.

WHY THIS EXISTS. Until the scheduler read `on_step_failure`, every workflow was
stored with `fail_workflow` (the API default), and docs/workflows.md told the
submitter the setting was not honoured. Deploying the scheduler that honours it
applies the rule to those workflows too, unless ON_STEP_FAILURE_ENFORCED_SINCE
exempts them. A cancel cannot be undone, and a cancelled step's checkpoint
becomes reclaimable. So the question "which in-flight workflows does this
cancel?" should be answered from data before the deploy, not after it.

    PROJECT_ID=saga-agents-staging FIRESTORE_DATABASE=swarm \\
      uv run --project . python -m scheduler.on_step_failure_audit

prints one JSON object:

* `swept_on_next_drain`: workflows with a FAILED or DEAD_LETTERED step whose
  not-started steps the next drain would cancel, each step with its
  `park_reason` and whether it holds a checkpoint;
* `exposed_if_a_step_fails`: `fail_workflow` workflows with not-started steps
  and no failure yet, which lose those steps if a step fails later;
* `before_cutoff`: workflows a cutoff exempts (empty when there is none).

READ-ONLY BY CONSTRUCTION. It calls query and point-read methods of
`SchedulerStore` and nothing else, and its test asserts that the database is
byte-for-byte unchanged afterwards.

ONE STATEMENT OF THE RULE. The verdict is `loop.read_workflow_failure` and the
steps listed are read by the same query, over the same states, that the sweep
cancels from. Nothing here restates what "failed" or "not started" means.

INCOMPLETE ANSWERS SAY SO. Every query is bounded by `--sweep-size`. When one
comes back full there may be more, `truncated` is true, and the exit status is
3, so a partial list never reads as the whole one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Sequence, TextIO

from swarm_common.models import Task

from .loop import _NOT_STARTED_STATES, FAIL_WORKFLOW, read_workflow_failure
from .settings import parse_enforced_since
from .store import SchedulerStore

#: Exit status when a page came back full and the answer may be incomplete.
EXIT_TRUNCATED = 3


def _step(task: Task) -> dict[str, Any]:
    return {
        "step_id": task.step_id,
        "task_id": task.id,
        "state": task.state.value,
        "park_reason": task.park_reason.value if task.park_reason else None,
        "has_checkpoint": bool(task.latest_checkpoint),
    }


def audit(
    store: SchedulerStore,
    *,
    enforced_since: datetime | None,
    sweep_size: int,
) -> dict[str, Any]:
    """The report described in the module docstring. Writes nothing."""
    truncated = False
    visited = 0
    workflows: set[tuple[str, str]] = set()
    for state in _NOT_STARTED_STATES:
        page = store.tasks_in_state(state, sweep_size)
        truncated = truncated or len(page) > sweep_size  # MUTANT M11: off by one
        visited += len(page)
        workflows.update((task.tenant_id, task.workflow_id) for task in page if task.workflow_id)

    swept: list[dict[str, Any]] = []
    exposed: list[dict[str, Any]] = []
    before_cutoff: list[dict[str, Any]] = []
    for tenant_id, workflow_id in sorted(workflows):
        not_started: list[Task] = []
        for state in _NOT_STARTED_STATES:
            page = store.workflow_steps_in_state(tenant_id, workflow_id, state, sweep_size)
            truncated = truncated or len(page) > sweep_size  # MUTANT M11: off by one
            not_started.extend(page)
        steps = sorted((_step(task) for task in not_started),
                       key=lambda s: (s["step_id"] or "", s["task_id"]))
        entry: dict[str, Any] = {"tenant_id": tenant_id, "workflow_id": workflow_id}

        verdict = read_workflow_failure(
            store, tenant_id, workflow_id, enforced_since=enforced_since
        )
        if verdict is not None:
            swept.append(
                {**entry, "failed_steps": [dict(s) for s in verdict.failed_steps],
                 "would_cancel": steps}
            )
            continue
        policy = store.workflow_on_step_failure(
            tenant_id, workflow_id, enforced_since=enforced_since
        )
        if policy == FAIL_WORKFLOW:
            exposed.append({**entry, "not_started": steps})
            continue
        if enforced_since is not None and store.workflow_on_step_failure(
            tenant_id, workflow_id
        ) == FAIL_WORKFLOW:
            # `fail_workflow` without the cutoff, no policy with it: exempt.
            before_cutoff.append({**entry, "not_started": steps})

    return {
        "enforced_since": enforced_since.isoformat() if enforced_since else None,
        "not_started_steps_visited": visited,
        "workflows_examined": len(workflows),
        "truncated": truncated,
        "swept_on_next_drain": swept,
        "exposed_if_a_step_fails": exposed,
        "before_cutoff": before_cutoff,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    store: SchedulerStore | None = None,
    out: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scheduler.on_step_failure_audit",
        description="List, read-only, what the next scheduler drain would cancel "
        "under on_step_failure: fail_workflow.",
    )
    parser.add_argument(
        "--enforced-since",
        default=None,
        help="the cutoff to evaluate (ISO-8601 with an offset). Defaults to "
        "ON_STEP_FAILURE_ENFORCED_SINCE, which is what the scheduler would use.",
    )
    parser.add_argument(
        "--sweep-size",
        type=int,
        default=500,
        help="page size for every query; a full page marks the answer truncated",
    )
    args = parser.parse_args(argv)
    if args.sweep_size <= 0:
        parser.error("--sweep-size must be positive")

    enforced_since = parse_enforced_since(
        args.enforced_since
        if args.enforced_since is not None
        else os.environ.get("ON_STEP_FAILURE_ENFORCED_SINCE")
    )

    if store is None:
        from google.cloud import firestore

        from .settings import SchedulerSettings

        settings = SchedulerSettings.from_env()
        # The NAMED database. `(default)` belongs to other teams in this
        # shared project.
        store = SchedulerStore(
            firestore.Client(
                project=settings.project_id, database=settings.core.firestore_database
            )
        )

    result = audit(store, enforced_since=enforced_since, sweep_size=args.sweep_size)
    stream = out or sys.stdout
    stream.write(json.dumps(result, sort_keys=True) + "\n")
    print(
        f"{len(result['swept_on_next_drain'])} workflow(s) swept on the next drain, "
        f"{len(result['exposed_if_a_step_fails'])} exposed if a step fails, "
        f"{len(result['before_cutoff'])} exempt before the cutoff; "
        f"{result['not_started_steps_visited']} not-started step(s) visited across "
        f"{result['workflows_examined']} workflow(s)"
        + ("; TRUNCATED, raise --sweep-size" if result["truncated"] else ""),
        file=sys.stderr,
    )
    return EXIT_TRUNCATED if result["truncated"] else 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
