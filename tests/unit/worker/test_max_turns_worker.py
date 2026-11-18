"""Contract request 133 (accepted by the owner 2 November 2026): the worker
passes the VERIFIED task's `max_turns` to its runner, and nothing else.

`Task.max_turns` is signed at spec format 7, so `self._task` -- the document
`verify_spec` checked -- is the only source; a caller's `input.max_turns` is
refused by swarm-api and never read here. None: no variable, so the runner
adds no `--max-turns` and every running agent is unchanged.
"""

from __future__ import annotations

from typing import Any

from agent_worker import lifecycle
from agent_worker import workspace as workspace_mod
from worker_seeds import TENANT, seed_attempt


def _child_env(db, worker_factory, tmp_path, *, task: dict[str, Any]) -> dict[str, str]:
    seed_attempt(db, runner_profile="mock")
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    worker, _, _ = worker_factory()
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._task = task
    return worker._build_child_env()


def test_the_worker_exports_the_verified_tasks_turn_cap(db, worker_factory, tmp_path):
    # MUTATION: not exporting `_max_turns_env` turns this red (KeyError).
    env = _child_env(db, worker_factory, tmp_path, task={lifecycle.MAX_TURNS_FIELD: 30})
    assert env[lifecycle.MAX_TURNS_ENV] == "30"


def test_no_cap_and_an_unusable_cap_export_nothing(db, worker_factory, tmp_path):
    cases = ({}, {lifecycle.MAX_TURNS_FIELD: None}, {lifecycle.MAX_TURNS_FIELD: 0},
             {lifecycle.MAX_TURNS_FIELD: "30"}, {lifecycle.MAX_TURNS_FIELD: True},
             # The input is the caller's, never the cap.
             {"input": {"max_turns": 30}})
    for n, task in enumerate(cases):
        env = _child_env(db, worker_factory, tmp_path / str(n), task=task)
        assert lifecycle.MAX_TURNS_ENV not in env, task
