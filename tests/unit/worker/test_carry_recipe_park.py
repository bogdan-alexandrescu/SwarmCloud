"""The acceptance `workflow: carry` recipe parks, offline, with the exact input dev is sent (issue 202).

WHY IT EXISTS. Dev acceptance of 2026-11-23 22:30 UTC read the recipe's parent
as FAILED after one attempt, with no `parked` or `quota_exhausted` event: its
first attempt failed where it should have parked on the mock's simulated quota
exhaustion (invariant 4). The worker tests of the park drove a hand-written
input -- `retry_after_seconds` 1800, an explicit `steps` -- never the one the
acceptance group submits, whose 60 s sits just over the worker's 45 s in-place
retry ceiling and which names no `steps`. So the step input here is READ FROM
`scripts/acceptance/groups/workflow.sh` and evaluated with jq exactly as the
group builds it: a change to the recipe changes what this drives.

What is driven is the production lifecycle (`Worker.run`) and the real mock
runner, with fakes only at the cloud edges: Firestore, the bucket, Secret
Manager and the quota broker. Asserted, in the order the platform runs it:

* attempt 1 checkpoints, uploads notes.md, parks PROVIDER_QUOTA_EXHAUSTED with
  `parked` and `quota_exhausted` events naming it, refunds the attempt,
  releases its lease and exits 75 -- no in-place sleep through the 60 s
  (invariants 4 and 8);
* a stale generation of the same task exits fenced without running the agent
  and leaves the lease and the park untouched (invariant 5);
* attempt 2, admitted with the refunded count, restores the park's checkpoint,
  writes nothing under notes.md and succeeds with notes.md carried by
  reference from attempt 1;
* the child step stages the parked attempt's object.

Also held: every name the worker package reads from `lifecycle` -- a module
import, a lazy import inside a function on the park or restore path, or an
attribute of the module -- resolves, so a binding dropped from `lifecycle`
that something still read fails here rather than at run time on dev.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import EventType, ParkReason, TaskState
from worker_seeds import TENANT, seed_attempt

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / "scripts" / "acceptance" / "groups" / "workflow.sh"
PACKAGE = ROOT / "apps" / "agent-worker" / "agent_worker"
NOTES = "written before the park\nrun offline\n"


def _object(source: str, start: str) -> str:
    """The balanced `{...}` in SOURCE that begins at the first match of START."""
    begin = source.find(start)
    assert begin >= 0, f"{start!r} not found in {WORKFLOW.relative_to(ROOT)}"
    depth = 0
    for index in range(begin, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[begin : index + 1]
    raise AssertionError(f"{start!r} is never closed")


def _recipe_step(step_id: str) -> dict[str, Any]:
    """Step STEP_ID of the carry submission, as jq builds it in run_workflow."""
    text = WORKFLOW.read_text()
    match = re.search(r'acc_workflow carry "(.*?)\|\| carry=""', text, re.S)
    assert match, "run_workflow submits no carry workflow"
    step = _object(match.group(1), f'{{step_id: "{step_id}"')
    name = re.search(r'^WF_CARRY_NAME="([^"]*)"$', text, re.M)
    assert name, "WF_CARRY_NAME is never assigned"
    built = subprocess.run(
        ["jq", "-nc", "--arg", "p", "acceptance offline carry", "--arg", "n", name.group(1),
         "--arg", "t", NOTES, step],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(built.stdout)


def _events(db: Any, task_id: str = "task_1") -> list[dict[str, Any]]:
    return list(db.events(task_id))


def _of(db: Any, kind: EventType, task_id: str = "task_1") -> list[dict[str, Any]]:
    return [e for e in _events(db, task_id) if e["type"] == kind.value]


def test_the_recipe_is_the_park_this_file_drives():
    """The step input really is the quota park the acceptance check expects."""
    a, b = _recipe_step("a"), _recipe_step("b")
    assert a["runner_profile"] == "mock" and b["runner_profile"] == "mock"
    assert a["input"]["quota_exhausted"] is True
    assert a["input"]["artifact_before_park"] is True
    assert a["input"]["retry_after_seconds"] > 45, "under the in-place ceiling it never parks"
    assert b["input_from"] == {"a": a["input"]["artifact_name"]}


def _park_first_attempt(db: Any, worker_factory: Any, task_input: dict[str, Any]) -> dict[str, Any]:
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_input=task_input)
    db.doc("tasks/task_1")["metadata"] = {"expected_outputs": [task_input["artifact_name"]]}
    worker, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)
    started = time.monotonic()
    code = worker.run()
    task = db.doc("tasks/task_1")
    assert code == ExitCode.PARKED, (code, task.get("state"), task.get("last_error"))
    # Invariant 4: parked and exited, never slept through the provider's 60 s.
    assert time.monotonic() - started < task_input["retry_after_seconds"]
    return task


def test_the_recipe_parks_its_first_attempt_carries_the_file_and_the_child_stages_it(
    db, store, worker_factory
):
    task_input = _recipe_step("a")["input"]
    name = task_input["artifact_name"]

    task = _park_first_attempt(db, worker_factory, task_input)
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    assert task["attempt_count"] == 0, "a quota park refunds its attempt"
    metadata = dict(task["metadata"])
    checkpoint = task["latest_checkpoint"]
    assert checkpoint, "the park left no checkpoint to resume from (invariant 8)"

    kinds = [e["type"] for e in _events(db)]
    assert EventType.FAILED.value not in kinds, kinds
    parked, exhausted = _of(db, EventType.PARKED), _of(db, EventType.QUOTA_EXHAUSTED)
    assert [e["attempt_id"] for e in parked] == ["att_1"], kinds
    assert [e["attempt_id"] for e in exhausted] == ["att_1"], kinds
    assert [a["name"] for a in parked[0]["detail"]["artifacts"]] == [name]
    # Checkpoint, then park, then release: the order `_park_for_quota` promises.
    assert kinds.index(EventType.CHECKPOINT_COMPLETED.value) < kinds.index(EventType.PARKED.value)
    assert kinds.index(EventType.PARKED.value) < kinds.index(EventType.LEASE_RELEASED.value)
    assert db.doc("leases/lease_1")["released_at"] is not None

    # Admission after `next_eligible_at`: a new lease and generation, the
    # refunded count plus one, the task's metadata and checkpoint as left.
    seed_attempt(
        db, attempt_id="att_2", lease_id="lease_2", generation=2,
        attempt_count=task["attempt_count"] + 1, latest_checkpoint=checkpoint,
        task_input=task_input,
    )
    db.doc("tasks/task_1")["metadata"] = metadata
    before = set(store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_2/"))
    second, _, _ = worker_factory(attempt_id="att_2", lease_id="lease_2", generation=2)
    assert second.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert len(_of(db, EventType.PARKED)) == 1
    assert [e["attempt_id"] for e in _of(db, EventType.SUCCEEDED)] == ["att_2"]
    assert task["result_summary"]["runner"]["output"]["was_resumed"] is True
    entry = next(e for e in task["result_summary"]["artifacts"] if e["name"] == name)
    assert entry["carried_from"] == "att_1"
    assert f"/attempts/att_1/artifacts/{name}" in entry["uri"]
    after = set(store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_2/"))
    assert not any(key.endswith(f"/artifacts/{name}") for key in after - before)

    child = _recipe_step("b")
    seed_attempt(db, task_id="task_2", attempt_id="att_d", lease_id="lease_d",
                 task_input=child["input"])
    db.doc("tasks/task_2")["metadata"] = {"input_from": {"task_1": name}}
    downstream, _, _ = worker_factory(task_id="task_2", attempt_id="att_d", lease_id="lease_d")
    assert downstream.run() == ExitCode.OK, db.doc("tasks/task_2").get("last_error")
    staged = db.doc("tasks/task_2")["result_summary"]["staged_inputs"]
    assert [item["filename"] for item in staged] == [name]
    assert f"/attempts/att_1/artifacts/{name}" in staged[0]["uri"]


def test_a_stale_generation_of_the_recipe_runs_nothing_and_leaves_the_park(db, worker_factory):
    """Invariant 5: the parked task now belongs to generation 2; a late worker
    of generation 1 exits fenced, runs no agent, and moves neither the task
    nor its lease."""
    task_input = _recipe_step("a")["input"]
    seed_attempt(db, attempt_id="att_1", lease_id="lease_1", generation=1, task_generation=2,
                 task_input=task_input)
    worker, _, _ = worker_factory(attempt_id="att_1", lease_id="lease_1", generation=1)
    assert worker.run() == ExitCode.GENERATION_FENCED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.LEASED.value
    assert task["current_lease_id"] == "lease_1"
    assert db.doc("leases/lease_1").get("released_at") is None, "a stale worker released the lease"
    kinds = [e["type"] for e in _events(db)]
    assert EventType.RUNNING.value not in kinds and EventType.PARKED.value not in kinds, kinds


def _names_read_from_lifecycle() -> list[tuple[str, int, str]]:
    """(file, line, name) for every `from ...lifecycle import NAME` and every
    `lifecycle.NAME` / `lifecycle_mod.NAME` in the worker package, lazy
    imports inside functions included."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "lifecycle":
                found += [(str(path.relative_to(ROOT)), node.lineno, a.name) for a in node.names]
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id in {"lifecycle", "lifecycle_mod"}):
                found.append((str(path.relative_to(ROOT)), node.lineno, node.attr))
    return found


def test_every_name_the_worker_reads_from_lifecycle_resolves():
    lifecycle = importlib.import_module("agent_worker.lifecycle")
    read = _names_read_from_lifecycle()
    # The park path's own lazy import is among them, so the walk did run.
    assert any(name == "_refunded_resumes" for _, _, name in read), read
    missing = [f"{path}:{line} {name}" for path, line, name in read if not hasattr(lifecycle, name)]
    assert not missing, missing


@pytest.mark.parametrize("name", ["_refunded_resumes", "REFUNDED_RESUME_METADATA_KEYS"])
def test_the_park_paths_lifecycle_names_exist(name):
    assert hasattr(importlib.import_module("agent_worker.lifecycle"), name)
