"""The scheduler's TypeSafe switch is the worker's, by name and by value
(lane TS-INJECT, part of #1347).

`scheduler.dispatch.TYPESAFE_INJECT_ENV` RESTATES
`agent_worker.typesafe_shim.SWITCH_ENV` and `SWITCH_ON`, because the
scheduler's image does not carry the worker and cannot import it (the same
reason `credentials.SUBSCRIPTION_TOKEN_ENV` is restated). A drift on either
side would be silent in production: the pod is labelled for the forwarder, the
worker never sees its switch, and it keeps reading the key file.

So the pair is held together in the two ways docs/mirrored-values.md uses:

  * both source files are READ and the assigned literals compared, so a rename
    on either side fails here even before anything is imported;
  * the REAL worker predicate, `typesafe_shim.switched_on`, is asked whether
    the env the scheduler writes turns it on, on GKE and only on GKE.

MUTATION: change the name or the value on either side and both tests fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agent_worker import typesafe_shim

from scheduler.dispatch import TYPESAFE_INJECT_ENV

ROOT = Path(__file__).resolve().parents[3]
WORKER = "apps/agent-worker/agent_worker/typesafe_shim.py"
SCHEDULER = "apps/scheduler/scheduler/dispatch.py"


def _assigned(relative: str) -> dict[str, object]:
    """Every module-level `NAME = <literal>` in the file, evaluated."""
    path = ROOT / relative
    assert path.is_file(), f"{relative} is not where this pin expects it"
    found: dict[str, object] = {}
    for node in ast.parse(path.read_text()).body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) and node.value else [])
        for target in targets:
            if isinstance(target, ast.Name):
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return found


def test_the_two_files_name_the_same_switch():
    worker = _assigned(WORKER)
    scheduler = _assigned(SCHEDULER)
    assert "TYPESAFE_INJECT_ENV" in scheduler, f"{SCHEDULER} no longer defines the switch"
    assert scheduler["TYPESAFE_INJECT_ENV"] == (worker["SWITCH_ENV"], worker["SWITCH_ON"])


def test_the_env_the_scheduler_writes_switches_the_real_worker_on_gke_only():
    name, value = TYPESAFE_INJECT_ENV
    assert typesafe_shim.switched_on({name: value}, typesafe_shim.GKE_BACKEND)
    assert not typesafe_shim.switched_on({name: value}, "CLOUD_RUN_JOB")
    assert not typesafe_shim.switched_on({}, typesafe_shim.GKE_BACKEND)
