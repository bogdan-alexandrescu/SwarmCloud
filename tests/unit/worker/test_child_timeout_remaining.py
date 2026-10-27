"""The runner is told the time LEFT on its step, not the step's whole limit (#2363).

`_build_child_env` runs at every runner start, including the in-place restart
after a credential reload or an account swap. It exported
`str(self.cfg.timeout_seconds)` as SWARM_CHILD_TIMEOUT_SECONDS, so a runner
restarted 87 minutes into its step (task_ff3508e4: restarted 08:12:46 after a
credential reload) computed its wrap-up from the whole limit, ran its gate
checks past the worker's own deadline and was SIGKILLed (exit -9 at 09:41:47).

What holds now:

1. at the first start the runner is given the whole limit;
2. N seconds later it is given the limit minus N;
3. on an index run the graph write's and the sweep's reserve
   (`indexrun.budgets`) is taken off as well, since `_runner_deadline` is
   what the supervision loop kills the runner at;
4. a start at or past the deadline is still given 1 s, never 0 or a negative
   number, which `runners/limits.py` would not read as a limit.

MUTATIONS: export `self.cfg.timeout_seconds` again (2, 3); compute from
`self._deadline` instead of `self._runner_deadline` (3); drop the `max(1, ...)`
(4).
"""

from __future__ import annotations

from agent_worker import indexrun, lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.runners.limits import TIMEOUT_ENV

from worker_seeds import TENANT, seed_attempt

#: Where the fake monotonic clock starts: an integer, so every difference the
#: worker takes is exact.
ORIGIN = 50_000.0


class _Clock:
    def __init__(self) -> None:
        self.now = ORIGIN

    def __call__(self) -> float:
        return self.now


def _child_timeout(db, worker_factory, tmp_path, monkeypatch, *, advance: float, **kwargs) -> int:
    seed_attempt(db, runner_profile=kwargs.get("runner_profile", "mock"))
    # A CLI profile's start resolves its provider credential: the tenant has one.
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    clock = _Clock()
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.time, "monotonic", clock)
        worker, _, _ = worker_factory(**kwargs)
        worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
        clock.now += advance
        env = worker._build_child_env()
    return int(env[TIMEOUT_ENV])


def test_the_first_start_is_given_the_whole_limit(db, worker_factory, tmp_path, monkeypatch):
    assert _child_timeout(db, worker_factory, tmp_path, monkeypatch, advance=0, timeout_seconds=3600) == 3600


def test_a_later_start_is_given_only_the_time_left(db, worker_factory, tmp_path, monkeypatch):
    # The restart after a credential reload, 87 minutes into a 100-minute step.
    timeout = _child_timeout(
        db, worker_factory, tmp_path, monkeypatch, advance=87 * 60, timeout_seconds=6000
    )

    assert timeout == 6000 - 87 * 60, timeout


def test_an_index_run_is_given_the_time_left_less_its_graph_write_and_sweep(
    db, worker_factory, tmp_path, monkeypatch
):
    reserve = indexrun.budgets(1800)
    assert reserve.graph_write + reserve.sweep > 0

    timeout = _child_timeout(
        db, worker_factory, tmp_path, monkeypatch,
        advance=300, runner_profile=indexrun.INDEXER_PROFILE, timeout_seconds=1800,
    )

    assert timeout == 1800 - 300 - reserve.graph_write - reserve.sweep, timeout


def test_a_start_past_the_deadline_is_still_given_one_second(
    db, worker_factory, tmp_path, monkeypatch
):
    timeout = _child_timeout(
        db, worker_factory, tmp_path, monkeypatch, advance=700, timeout_seconds=600
    )

    assert timeout == 1, timeout
