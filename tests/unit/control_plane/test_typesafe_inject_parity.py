"""The scheduler's restatement of the worker's TypeSafe forwarder switch, held
to the worker's own (lane TS-INJECT, part of #1347).

The scheduler's image does not carry the worker, so
`scheduler.dispatch.TYPESAFE_INJECT_ENV` restates the name and value
`agent_worker.typesafe_shim` reads (`SWITCH_ENV`, `SWITCH_ON`). If the two
drift, a tenant's pods are labelled for the forwarder while every worker
still reads the key file -- the exact gap this lane closed -- and nothing
else fails. So they are compared here twice: by import, and by reading both
source files, so that a rename on either side fails even if one module stops
exporting the name.

MUTATION: change either side's name or value and both tests fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SHIM = REPO / "apps/agent-worker/agent_worker/typesafe_shim.py"
DISPATCH = REPO / "apps/scheduler/scheduler/dispatch.py"


def _assignments(path: Path) -> dict[str, object]:
    """Module-level constant assignments in `path`, by literal evaluation."""
    found: dict[str, object] = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
                node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return found


def test_the_scheduler_sets_the_switch_the_worker_reads():
    from agent_worker.typesafe_shim import GKE_BACKEND, SWITCH_ENV, SWITCH_ON, switched_on
    from scheduler.dispatch import TYPESAFE_INJECT_ENV

    assert TYPESAFE_INJECT_ENV == (SWITCH_ENV, SWITCH_ON)
    # And the worker, given exactly what the scheduler sets, takes the path on GKE.
    assert switched_on(dict([TYPESAFE_INJECT_ENV]), GKE_BACKEND)
    assert not switched_on({}, GKE_BACKEND)


def test_both_source_files_name_the_same_switch():
    worker = _assignments(SHIM)
    scheduler = _assignments(DISPATCH)
    assert "SWITCH_ENV" in worker and "SWITCH_ON" in worker
    assert "TYPESAFE_INJECT_ENV" in scheduler
    assert scheduler["TYPESAFE_INJECT_ENV"] == (worker["SWITCH_ENV"], worker["SWITCH_ON"])
