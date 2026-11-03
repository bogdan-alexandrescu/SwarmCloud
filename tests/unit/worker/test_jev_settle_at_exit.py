"""The worker settles its Jev spend on every exit (lane AQ-JEV-ACCOUNTING).

Before this lane the lifecycle settled Jev only on the path that reached the
end of the agent's run (`_settle_jev`): a park, a cancel, a crash or a fence
settled nothing. `_cleanup`, the `finally` of every exit, now runs
`judge.attempt.settle_at_exit` with the end the attempt committed, after the
runner is gone and BEFORE the workspace holding the ledger is destroyed.
Offline: no credentials, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker.judge import attempt as judge_attempt
from swarm_common.states import TaskState

from worker_seeds import seed_attempt


@pytest.mark.parametrize("end", [None, TaskState.PARKED, TaskState.CANCELLED,
                                 TaskState.SUCCEEDED])
def test_cleanup_settles_jev_with_the_committed_end_before_the_workspace_goes(
        db, worker_factory, monkeypatch, end):
    seed_attempt(db)
    worker, _config, _exporter = worker_factory(sign_spec=False)
    order: list[str] = []
    seen: dict[str, Any] = {}

    def settle_at_exit(env: Any, **kwargs: Any) -> None:
        order.append("settle_jev")
        seen.update(kwargs, env=env)

    monkeypatch.setattr(judge_attempt, "settle_at_exit", settle_at_exit)
    monkeypatch.setattr(lifecycle.workspace_mod, "destroy",
                        lambda ws: order.append("destroy_workspace"))
    worker._graph_env = {judge_attempt.CONFIG_ENV: "/nonexistent/config.json"}
    worker.control.committed_end = end
    worker.ws = object.__new__(type("Ws", (), {"private": None}))
    monkeypatch.setattr(lifecycle.extrarepos_mod, "release", lambda ws: None)
    monkeypatch.setattr(lifecycle.secretrelease_mod, "remove", lambda private: None)
    worker._cleanup()
    assert order == ["settle_jev", "destroy_workspace"]
    assert seen["committed_end"] is end
    assert seen["db"] is worker.db
    assert seen["generation"] == worker.cfg.generation


def test_cleanup_settles_nothing_without_jev(db, worker_factory, monkeypatch):
    seed_attempt(db)
    worker, _config, _exporter = worker_factory(sign_spec=False)
    called: list[Any] = []
    monkeypatch.setattr(judge_attempt, "settle_at_exit", lambda *a, **k: called.append(a))
    worker._graph_env = {}
    worker._cleanup()
    assert called == []


def test_cleanup_hands_the_tools_calls_over_even_with_the_judgments_off(
        db, worker_factory, monkeypatch, tmp_path):
    """The quality hooks and the Jev browser call Jev without the judgments'
    config: their records in `private/` are sent on every exit regardless
    (`account.record_tool_calls`, owner's pick on #3111)."""
    seed_attempt(db)
    worker, _config, _exporter = worker_factory(sign_spec=False)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(judge_attempt, "settle_at_exit",
                        lambda env, **kwargs: seen.update(kwargs, env=env))
    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: None)
    monkeypatch.setattr(lifecycle.extrarepos_mod, "release", lambda ws: None)
    monkeypatch.setattr(lifecycle.secretrelease_mod, "remove", lambda private: None)
    worker._graph_env = {}
    worker.ws = object.__new__(type("Ws", (), {"private": tmp_path}))
    worker._cleanup()
    assert seen["env"] == {}
    assert seen["private_dir"] == tmp_path
